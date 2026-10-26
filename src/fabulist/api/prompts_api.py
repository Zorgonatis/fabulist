"""Prompt-preset router (D18): presets + the preview interrogation endpoint.

Turns :mod:`fabulist.prompts.preset_store` into reachable REST surface. The
event log owns everything (D9): every endpoint derives from ``__presets__``
events and every write appends one — no side table, no in-place mutation. The
store/fold semantics (latest-wins per ``preset_id``; ``preset_active`` pointer)
are the store module's, not duplicated here.

Endpoints
---------
  GET    /api/prompts/presets                 -> {presets: [preset, ...]}
  POST   /api/prompts/presets                 -> {preset}     (modules default from_default())
  GET    /api/prompts/presets/{pid}           -> {preset} | 404
  PUT    /api/prompts/presets/{pid}           -> {preset} | 404 | 422 empty name (full replace)
  PATCH  /api/prompts/presets/{pid}           -> {preset} | 404   (partial merge)
  DELETE /api/prompts/presets/{pid}           -> {deleted: pid} | 403 default | 404
  POST   /api/prompts/presets/{pid}/activate  -> {active, preset} | 404
  POST   /api/prompts/presets/deactivate      -> {active: null}  (back to default)
  GET    /api/prompts/presets/{pid}/export    -> preset JSON download (attachment) | 404
  POST   /api/prompts/presets/import          -> {preset}        (ALWAYS a new preset)
  GET    /api/prompts/active                  -> {preset: preset | null}
  GET    /api/prompts/default                 -> {preset, factory_changed, ...}
  POST   /api/prompts/default/reset           -> {preset, factory_changed, ...}  (re-seed)
  POST   /api/prompts/default/commit          -> {artifact_path, ...}  (write artifact)
  GET    /api/prompts/variables               -> {variables: [VariableSpec, ...]}
  GET    /api/prompts/directives              -> {directives: [row, ...]} (factory seed)
  GET    /api/prompts/steps                   -> {steps: [step, ...]} (9 interactions)
  POST   /api/prompts/seed-from-default       -> {preset}        (new preset)
  POST   /api/prompts/preview                 -> {messages, chars, preview_mode}

Default preset (D26)
--------------------
``GET /api/prompts/default`` now returns the **persisted** default row when it
exists (falling back to the factory-image shape pre-boot), plus the factory
status fields (``present`` / ``factory_changed`` / ``seeded_version`` /
``factory_version``). The default row is edited through the SAME preset update
paths (PUT/PATCH on ``/api/prompts/presets/default``); it is undeletable
(``DELETE`` -> 403). ``POST /api/prompts/default/reset`` re-seeds it from the
factory image and ``POST /api/prompts/default/commit`` exports it to the
versioned repo artifact.

Preview parity contract (critical)
----------------------------------
``POST /api/prompts/preview`` renders through the **production builder** the
engine uses — ``request.app.state.message_builder``, a (possibly async) callable
taking a ``TurnState``-like dict with ``session_id`` / ``user_content``. That is
the same callable :func:`fabulist.wiring.build_engine` hands to the graph
(``make_narrator_message_builder``), so the preview and a real turn compile from
one code path (parity by construction). Only when the app has no builder — the
test harness, which mounts this router without the wiring layer — does the
``narrate`` interaction degrade to
:func:`fabulist.prompts.modules.compile_preset_messages` over
:data:`~fabulist.prompts.modules.DEFAULT_MODULES` and mark the response
``preview_mode: "degraded"``. The live service (``wiring.build_app``) always
carries the builder, so a production ``narrate`` preview is always ``"builder"``.

Per-interaction flows (D18 completion)
--------------------------------------
``interaction`` selects any of :data:`~fabulist.prompts.modules.INTERACTIONS`
(default ``narrate``). The builder fast path stays **narrate-only**; every other
interaction renders via :func:`~fabulist.prompts.modules.compile_flow` over the
active preset (or :data:`~fabulist.prompts.modules.DEFAULT_FLOWS` when none) with
best-effort parts resolved from the session, and reports
``preview_mode: "flow"``. System-prompt defaults (``OOC_SYSTEM`` etc.) are
imported **lazily inside the handler**: ``fabulist.wiring`` imports
``fabulist.api`` at module top and this router is reached through the ``api``
package, so a top-level import would be a cycle. Unknown ``interaction`` is 422.

Wiring (orchestrator-owned, one line)::

    from fabulist.api.prompts_api import router
    app.include_router(router)
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass

import inspect
import json
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from fabulist.graph.types import initial_turn_state
from fabulist.prompts import factory
from fabulist.prompts.agent_sets import DEFAULT_BINDINGS
from fabulist.prompts.catalog import INTERACTION_CATALOG
from fabulist.prompts.modules import (
    DEFAULT_FLOWS,
    DEFAULT_MODULES,
    FLOW_STRUCTURAL_TYPES,
    INTERACTIONS,
    compile_flow,
    compile_preset_messages,
)
from fabulist.prompts.parts import default_directives, directive_to_row
from fabulist.prompts.preset_store import (
    DEFAULT_PRESET_ID,
    Preset,
    active_preset,
    append_preset,
    commit_default_to_factory,
    compile_preset_flow,
    default_factory_status,
    default_preset,
    delete_preset,
    get_preset,
    list_presets,
    planner_config,
    preset_from_json,
    preset_to_json,
    reset_default_preset,
    seed_from_default,
    set_active,
)
from fabulist.prompts.variables import VARIABLE_SPECS, resolve_variables

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = ["router", "PresetInput", "PreviewRequest", "SeedRequest"]

router = APIRouter(tags=["prompts"])


class PresetInput(BaseModel):
    """Body of ``POST``/``PUT /api/prompts/presets[...]``.

    ``modules`` defaults to a fresh :func:`~fabulist.prompts.modules.from_default`
    copy on create (see :func:`_input_to_payload`); the remaining maps default to
    empty (engine falls back to today's wiring).
    """

    name: str = ""
    modules: list[dict[str, Any]] | None = None
    flows: dict[str, list[dict[str, Any]]] | None = None
    toggles: dict[str, bool] = Field(default_factory=dict)
    prompt_overrides: dict[str, str] = Field(default_factory=dict)
    agent_bindings: dict[str, Any] = Field(default_factory=dict)
    # W7-B surfaces (library_doc / recalled / regexes / history / directives):
    # preset-carried token surfaces. None on PUT/PATCH = keep the existing
    # preset's surfaces (edit-in-place semantics like the other optional maps).
    surfaces: dict[str, Any] | None = None
    # Ordered directive rows (the preset-carried catalog). ``None`` (absent) on
    # PUT = KEEP the stored list (an inherit-by-default surface must survive an
    # unrelated replace); ``[]`` is an explicit clear back to inheriting the
    # code catalog; a non-empty list replaces wholesale. Duplicate ids are 422.
    directives: list[dict[str, Any]] | None = None
    # The turn topology (W9.5/R10: preset data). ``None`` (absent) on PUT =
    # KEEP the stored topology — same discipline as ``directives`` — because an
    # unrelated module edit must not silently rewire the engine back to the
    # canonical graph. The editor's validated door is
    # ``PUT /api/prompts/topology`` (topology_api); this field is the
    # carry-through for a whole-row export/import.
    topology: dict[str, Any] | None = None
    # W9.20 §7 (the A2 seam): the planner block (cadence / steer / field list).
    # ``None`` (absent) on PUT = KEEP the stored block — an unrelated module edit
    # must not silently re-arm the steering plane with defaults. A provided value
    # round-trips through ``normalize_planner_config`` (422 on a broken row). The
    # validated single-purpose door is ``PUT /api/prompts/planner``
    # (planner_api); this field is the carry-through for a whole-row replace.
    planner: dict[str, Any] | None = None
    # Issue #50: the canonical anti-slop list (artifact-owned). ``None`` on PUT =
    # KEEP the stored list (same inherit discipline as ``directives``); a dict
    # replaces it wholesale, ``{}`` clears it.
    anti_slop: dict[str, Any] | None = None


class PresetPatch(BaseModel):
    """Body of ``PATCH /api/prompts/presets/{pid}`` — partial merge.

    Any field omitted (``exclude_unset``) keeps the stored value; this is the
    natural surface for the drawer's single-module tweak (PUT remains full
    replace).
    """

    name: str | None = None
    modules: list[dict[str, Any]] | None = None
    flows: dict[str, list[dict[str, Any]]] | None = None
    toggles: dict[str, bool] | None = None
    prompt_overrides: dict[str, str] | None = None
    agent_bindings: dict[str, Any] | None = None
    # R7: PATCH previously rebuilt the payload WITHOUT surfaces, silently
    # wiping the stored block; forwarded here (merged from the stored row when
    # the request omits it) so a partial edit preserves it.
    surfaces: dict[str, Any] | None = None
    # ``directives`` merges like the other optional fields: omitted keeps the
    # stored list, provided replaces it (``[]`` clears back to inherit).
    directives: list[dict[str, Any]] | None = None
    # Omitted keeps the stored topology (W9.5); provided replaces it wholesale.
    topology: dict[str, Any] | None = None
    # Omitted keeps the stored anti-slop list; provided replaces it wholesale.
    anti_slop: dict[str, Any] | None = None


class SeedRequest(BaseModel):
    """Body of ``POST /api/prompts/seed-from-default`` — the new preset name."""

    name: str


class PreviewRequest(BaseModel):
    """Body of ``POST /api/prompts/preview`` — one turn's message list.

    ``interaction`` selects which model-facing flow to render (one of
    :data:`~fabulist.prompts.modules.INTERACTIONS`); the default ``narrate``
    keeps the landed behavior (production builder fast path).
    """

    session_id: str
    user_text: str = ""
    preset_id: str | None = None
    interaction: str = "narrate"


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _ensure_known_session(request: Request, session_id: str) -> None:
    """404 unless the session exists in-memory or has events in the log.

    Same check as :func:`fabulist.api.assistants_api._ensure_known_session`: the
    in-memory registry first, then any logged events.
    """
    registry = getattr(request.app.state, "sessions", None)
    if isinstance(registry, dict) and session_id in registry:
        return
    if _log_for(request).get_events(session_id):
        return
    raise HTTPException(status_code=404, detail="unknown session")


def _input_to_payload(body: PresetInput) -> dict[str, Any]:
    """Project a request body into the store's export/payload shape.

    ``modules`` defaults to :func:`~fabulist.prompts.modules.from_default`
    (editable copy of today's narrator pipeline) when omitted, per the D18
    create contract. ``flows`` is emitted alongside it and normalized so
    ``modules == flows["narrate"]`` (back-compat: the landed UI/API clients
    read/write ``modules`` only). A client may supply other interaction flows
    in ``flows``; its ``narrate`` row is always replaced by ``modules`` (the
    canonical narrate list).
    """
    if body.modules is None:
        from fabulist.prompts.modules import from_default

        modules = [dict(_module_dict(module)) for module in from_default()]
    else:
        modules = [dict(module) for module in body.modules]
    flows: dict[str, list[dict[str, Any]]] = {}
    if body.flows:
        for interaction, rows in body.flows.items():
            flows[str(interaction)] = [dict(_module_dict(row)) for row in rows]
    flows["narrate"] = modules
    payload: dict[str, Any] = {
        "name": body.name.strip(),
        "modules": modules,
        "flows": flows,
        "toggles": dict(body.toggles),
        "prompt_overrides": dict(body.prompt_overrides),
        "agent_bindings": dict(body.agent_bindings),
        "version": 1,
    }
    # W7-B surfaces: None = keep the preset's existing surfaces (the PUT
    # caller that didn't touch them shouldn't wipe them); a dict replaces
    # them wholesale (normalized downstream by preset_from_json).
    if body.surfaces is not None:
        payload["surfaces"] = dict(body.surfaces)
    # Directives: only ever set from an explicit payload — ``None`` (absent) on
    # PUT is resolved by the caller (keep-or-clear lives there, not here).
    if body.directives is not None:
        payload["directives"] = _validated_directive_rows(body.directives)
    # Issue #50: None = keep the preset's existing list (the caller resolves it);
    # a dict replaces it wholesale (normalized downstream).
    if body.anti_slop is not None:
        from fabulist.antislop import normalize_anti_slop

        payload["anti_slop"] = normalize_anti_slop(body.anti_slop)
    # Topology: same rule (the caller resolves ``None`` to the stored value),
    # and the SAME validated door (W9.5/R23): a wiring that breaks an invariant
    # is rejected here too, so no preset route can store a graph the engine
    # would refuse to build.
    if body.topology is not None:
        payload["topology"] = _validated_topology(body.topology)
    # Planner config (W9.20/A2): same keep-on-omitted rule (the caller resolves
    # ``None`` to the stored block), validated by the store's one write door.
    if body.planner is not None:
        payload["planner"] = _validated_planner(body.planner)
    return payload


def _validated_planner(planner: dict[str, Any]) -> dict[str, Any]:
    """Normalize a request ``planner`` block; 422 with the reasons when broken.

    The rules live in :func:`fabulist.prompts.preset_store.normalize_planner_config`
    (one source with ``PUT /api/prompts/planner``): cadence >= 1, unique
    identifier-safe field ids, declared types. The rejection rides this router's
    usual ``detail`` object (``{"reason", "errors"}``).
    """
    from fabulist.prompts.preset_store import normalize_planner_config

    config, errors = normalize_planner_config(planner)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"reason": "planner config rejected", "errors": errors},
        )
    return config


def _validated_topology(topology: dict[str, Any]) -> dict[str, Any]:
    """Serialize request ``topology``; 400 (+ violations) when it is illegal.

    The invariant rules live in :func:`fabulist.graph.contracts.validate_topology`
    (one source with the editor's door, :mod:`fabulist.api.topology_api`). The
    rejection rides this router's usual ``detail`` object
    (``{"reason", "violations"}``); the topology door puts ``violations`` at the
    top level of its 400 because that is the shape the editor's save path reads.
    """
    from fabulist.graph.contracts import validate_topology

    violations = validate_topology(topology)
    if violations:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "topology rejected: "
                + ", ".join(dict.fromkeys(v["code"] for v in violations)),
                "violations": violations,
            },
        )
    return dict(topology)


def _validated_directive_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serialize request ``directives`` rows; 422 on duplicate rows.

    A unique ``id`` per payload is the only invariant the budget trim order
    relies on (a duplicate would collapse one trim slot), so duplicates are
    rejected at the door. Nothing is protected (user decision 2026-10-09): any
    directive row may be disabled. Rows without an ``id`` pass through — the
    store mints each a unique one. Tolerant field normalization stays in the
    store (:func:`preset_from_json`).
    """
    out = [dict(row) for row in rows]
    seen: set[str] = set()
    duplicates: list[str] = []
    for row in out:
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            continue
        if row_id in seen and row_id not in duplicates:
            duplicates.append(row_id)
        seen.add(row_id)
    if duplicates:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "duplicate directive ids in one payload",
                "ids": duplicates,
            },
        )
    return out


def _module_dict(module: Any) -> dict[str, Any]:
    """Serialize one module (``PromptModule`` or dict) to a plain dict."""
    if isinstance(module, dict):
        return dict(module)
    return {
        "id": module.id,
        "name": module.name,
        "type": module.type,
        "role": module.role,
        "content": module.content,
        "enabled": module.enabled,
    }


def _preset_wire(preset: Preset) -> dict[str, Any]:
    """Wire shape of one preset (the store's export payload)."""
    return preset_to_json(preset)


def _require_name(name: str) -> str:
    """Return the stripped name or raise 422 when it is empty."""
    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(
            status_code=422, detail={"reason": "name must be non-empty"}
        )
    return cleaned


# -- catalog -----------------------------------------------------------------


@router.get("/api/prompts/presets")
async def list_preset_route(request: Request) -> dict:
    """List every defined preset (definition order, latest value per id)."""
    log = _log_for(request)
    return {"presets": [_preset_wire(preset) for preset in list_presets(log)]}


@router.post("/api/prompts/presets")
async def create_preset(body: PresetInput, request: Request) -> dict:
    """Create a preset (modules default from ``from_default()`` when omitted)."""
    log = _log_for(request)
    payload = _input_to_payload(body)
    payload["name"] = _require_name(payload["name"])
    preset_id = append_preset(log, payload)
    created = get_preset(log, preset_id)
    assert created is not None  # just appended
    return {"preset": _preset_wire(created)}


@router.get("/api/prompts/active")
async def get_active_route(request: Request) -> dict:
    """The active preset, or ``{"preset": null}`` for the default pipeline."""
    log = _log_for(request)
    preset = active_preset(log)
    return {"preset": _preset_wire(preset) if preset is not None else None}


def _factory_image_wire() -> dict[str, Any]:
    """The factory-image default pipeline as a read-only preset-shaped payload.

    Resolved from :func:`fabulist.prompts.factory.factory_image` (the committed
    ``prompts/factory-defaults.json`` when present, else the code constants) and
    normalized through the store's serializer so the D18 UI inheritance source
    can never drift from the factory image. Used **pre-boot** (no persisted
    default row yet). Pure read — no preset event is written.
    """
    return _preset_wire(preset_from_json(factory.factory_image().payload))


def _resolved_default_wire(log: "EventLog") -> dict[str, Any]:
    """The persisted default row when seeded, else the factory-image shape."""
    persisted = default_preset(log)
    if persisted is not None:
        return _preset_wire(persisted)
    return _factory_image_wire()


@router.get("/api/prompts/default")
async def get_default_route(request: Request) -> dict:
    """The persisted default preset row (D26), plus factory status.

    Falls back to the factory-image shape only pre-boot (no persisted default
    row). ``factory_changed`` surfaces an engine upgrade without overwriting the
    owner's edits. The D18 UI uses ``preset`` as the inheritance source for
    flows a preset does not define; W7-C renders the notice from the flags.
    """
    log = _log_for(request)
    return {"preset": _resolved_default_wire(log), **default_factory_status(log)}


@router.post("/api/prompts/default/reset")
async def reset_default_route(request: Request) -> dict:
    """Re-seed the persisted default from the factory image (user-invoked, D26).

    Overwrites the owner's edits (append-only; prior versions stay queryable).
    """
    log = _log_for(request)
    row = reset_default_preset(log)
    return {"preset": _preset_wire(row), **default_factory_status(log)}


@router.post("/api/prompts/default/commit")
async def commit_default_route(request: Request) -> dict:
    """Export the persisted default to the versioned repo factory artifact (D26).

    409 when there is no persisted default to commit. The write is atomic and
    lands at ``prompts/factory-defaults.json`` (in-repo, commit via git).
    """
    log = _log_for(request)
    try:
        result = commit_default_to_factory(log)
    except LookupError:
        raise HTTPException(
            status_code=409, detail="no persisted default preset to commit"
        ) from None
    return {
        "artifact_path": result.artifact_path,
        "factory_version": result.factory_version,
        "preset": _preset_wire(result.preset),
        **default_factory_status(log),
    }


@router.get("/api/prompts/variables")
async def list_variables_route() -> dict:
    """The fixed ``{{variable}}`` vocabulary the editor can reference."""
    return {
        "variables": [
            {"name": spec.name, "description": spec.description, "example": spec.example}
            for spec in VARIABLE_SPECS
        ]
    }


@router.get("/api/prompts/directives")
async def factory_directives_route() -> dict:
    """The code factory's directive catalog as data (the materialization seed).

    Rows come from :func:`fabulist.prompts.parts.default_directives` through
    :func:`~fabulist.prompts.parts.directive_to_row` — the exact preset-storage
    shape — so the editor materializes an inherited (read-only) list into an
    editable one verbatim, and what it writes back round-trips through
    PUT/PATCH unchanged. (The live settings catalog in
    :mod:`fabulist.api.settings` reads the same source.)
    """
    return {"directives": [directive_to_row(d) for d in default_directives()]}


def _row_dict(row: Any) -> dict[str, Any]:
    """One flow row as a plain dict (preset rows already are; DEFAULT_FLOWS
    holds :class:`PromptModule` dataclasses)."""
    if isinstance(row, dict):
        return dict(row)
    if is_dataclass(row) and not isinstance(row, type):
        return asdict(row)
    return dict(getattr(row, "__dict__", {}))


def _effective_flow_rows(preset: Preset | None, interaction: str) -> list[Any]:
    """The flow rows a step renders: the preset's rows, else the code default.

    ``[]`` from the preset means "no rows authored" — the default flow is the
    effective one, mirroring :func:`compile_preset_flow`. Rows are ALWAYS dicts
    (W9.16: a new interaction with no stored rows fell through to the dataclass
    ``DEFAULT_FLOWS`` and the step route 500'd on ``row.get``).
    """
    if preset is not None:
        rows = preset.flows.get(interaction)
        if rows:
            return [_row_dict(row) for row in rows]
    return [_row_dict(row) for row in DEFAULT_FLOWS.get(interaction, ())]


def _bound_agent(preset: Preset | None, interaction: str) -> str | None:
    """The agent the active preset binds to this step, else the D19 default."""
    default_agent = (DEFAULT_BINDINGS.get(interaction) or {}).get("agent")
    if preset is not None:
        binding = preset.agent_bindings.get(interaction)
        if isinstance(binding, dict) and binding.get("agent"):
            return str(binding["agent"])
    return str(default_agent) if default_agent is not None else None


def _system_source(rows: list[Any]) -> str:
    """``"authored"`` when an enabled ``system`` row carries text, else ``"engine"``.

    The ``system`` inserter with a NON-EMPTY body is the one authoring seam for
    a flow's system prompt (:func:`~fabulist.prompts.modules.compile_flow`);
    an empty body (or no ``system`` row at all — e.g. narrate's
    ``narrator_system``) falls back to the engine default.
    """
    for row in rows:
        if str(row.get("type")) != "system":
            continue
        if str(row.get("content") or "").strip():
            return "authored"
    return "engine"


@router.get("/api/prompts/steps")
async def list_steps_route(request: Request) -> dict:
    """The prompt manager's step catalog: every interaction with display meta.

    Steps are the nine :data:`~fabulist.prompts.agent_sets.INTERACTIONS`
    (including ``critic``), each described from
    :data:`fabulist.prompts.catalog.INTERACTION_CATALOG` (the single authoring
    source) and decorated with the ACTIVE preset's state: its bound agent, its
    structural blocks, its block count, whether it is enabled, and whether its
    system prompt is authored or engine-supplied. Read-only; nothing is written.
    """
    log = _log_for(request)
    preset = active_preset(log)
    steps: list[dict[str, Any]] = []
    for interaction in INTERACTIONS:
        meta = INTERACTION_CATALOG[interaction]
        rows = _effective_flow_rows(preset, interaction)
        steps.append(
            {
                "id": interaction,
                "title": meta.title,
                "blurb": meta.blurb,
                "trigger": meta.trigger,
                "default_agent": (DEFAULT_BINDINGS.get(interaction) or {}).get("agent"),
                "bound_agent": _bound_agent(preset, interaction),
                "structural_types": sorted(FLOW_STRUCTURAL_TYPES.get(interaction, ())),
                "block_count": len(rows),
                "enabled": any(bool(row.get("enabled", True)) for row in rows),
                "system_source": _system_source(rows),
            }
        )
    return {"steps": steps}


@router.post("/api/prompts/seed-from-default")
async def seed_route(body: SeedRequest, request: Request) -> dict:
    """Create a new preset seeded from today's pipeline (editable modules)."""
    log = _log_for(request)
    name = _require_name(body.name)
    preset_id = seed_from_default(log, name)
    created = get_preset(log, preset_id)
    assert created is not None  # just appended
    return {"preset": _preset_wire(created)}


# -- active pointer ----------------------------------------------------------


@router.post("/api/prompts/presets/deactivate")
async def deactivate_route(request: Request) -> dict:
    """Clear the active pointer — back to the byte-faithful default pipeline."""
    log = _log_for(request)
    set_active(log, None)
    return {"active": None, "preset": None}


@router.post("/api/prompts/presets/{preset_id}/activate")
async def activate_route(preset_id: str, request: Request) -> dict:
    """Point the active pointer at one preset; 404 when unknown."""
    log = _log_for(request)
    preset = get_preset(log, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="unknown preset")
    set_active(log, preset_id)
    return {"active": preset_id, "preset": _preset_wire(preset)}


# -- one preset --------------------------------------------------------------


@router.get("/api/prompts/presets/{preset_id}")
async def get_preset_route(preset_id: str, request: Request) -> dict:
    """Read one preset; 404 when it has never been defined."""
    log = _log_for(request)
    preset = get_preset(log, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="unknown preset")
    return {"preset": _preset_wire(preset)}


def _preserve_default_metadata(
    payload: dict[str, Any], existing: Preset
) -> dict[str, Any]:
    """Keep the default row's ``is_default``/``factory_version`` across edits.

    An owner edit is not a re-seed: clearing ``factory_version`` would falsely
    fire the "factory changed" notice. Only the well-known default id (or a row
    already flagged default) is protected.
    """
    if existing.is_default or existing.preset_id == DEFAULT_PRESET_ID:
        payload["is_default"] = True
        payload["factory_version"] = existing.factory_version
    return payload


@router.put("/api/prompts/presets/{preset_id}")
async def replace_preset_route(
    preset_id: str, body: PresetInput, request: Request
) -> dict:
    """Full-replace one preset by appending a superseding ``preset`` event.

    404 when the preset does not exist (PUT never creates), 422 on an empty
    name. The append-only log keeps the prior version queryable (D9/D10). The
    default row's factory metadata is preserved (edit, not re-seed).

    ``directives`` deliberately diverges from ``modules``-on-PUT: ``modules``
    is a full replace (its default is refilled from ``from_default()``), while
    ``directives: null`` (omitted) KEEPS the stored list — the directive
    catalog is inherit-by-default, and a blind replace would silently destroy
    an owner's customization on every unrelated PUT. ``[]`` is the explicit
    clear back to inheriting the code catalog. ``topology`` follows the same
    keep-on-omitted rule (W9.5): the validated write lives at
    ``PUT /api/prompts/topology``, and an unrelated preset replace must not
    rewire the engine.
    """
    log = _log_for(request)
    existing = get_preset(log, preset_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="unknown preset")
    payload = _input_to_payload(body)
    payload["name"] = _require_name(payload["name"])
    payload["preset_id"] = preset_id
    _preserve_default_metadata(payload, existing)
    if body.directives is None:
        payload["directives"] = [dict(row) for row in existing.directives]
    if body.topology is None:
        payload["topology"] = dict(existing.topology)
    if body.planner is None:
        # W9.20: the same keep-on-omitted discipline — the whole-row replace must
        # not re-arm the steering plane with the defaults.
        payload["planner"] = planner_config(existing)
    if body.anti_slop is None:
        # Issue #50: keep-on-omitted (an unrelated module edit must not clear
        # the canonical phrase list).
        payload["anti_slop"] = dict(existing.anti_slop)
    append_preset(log, payload)
    updated = get_preset(log, preset_id)
    assert updated is not None  # just appended
    return {"preset": _preset_wire(updated)}


@router.patch("/api/prompts/presets/{preset_id}")
async def patch_preset_route(
    preset_id: str, body: PresetPatch, request: Request
) -> dict:
    """Partially update one preset (merge over the current value).

    404 when unknown. Any field omitted keeps the stored value; this is the
    natural one-module tweak surface (the drawer edits the default directly,
    D26). Full replace stays PUT.
    """
    log = _log_for(request)
    existing = get_preset(log, preset_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="unknown preset")
    merged = preset_to_json(existing)
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            merged[key] = value
    payload = _input_to_payload(
        PresetInput(
            name=str(merged.get("name") or existing.name),
            modules=merged.get("modules"),
            flows=merged.get("flows"),
            toggles=merged.get("toggles") or {},
            prompt_overrides=merged.get("prompt_overrides") or {},
            agent_bindings=merged.get("agent_bindings") or {},
            # R7: carry the merged surfaces through (the stored block when the
            # request omitted it) — rebuilding without this wiped them.
            surfaces=merged.get("surfaces"),
            # Only the REQUEST's rows are validated (an omitted ``directives``
            # must not re-validate — and reject — carried-over stored rows);
            # the keep-on-omitted branch below restores the stored list.
            directives=body.directives,
            # Same shape for the topology: the request's value when it touched
            # it, the stored one restored below when it did not.
            topology=body.topology,
            anti_slop=body.anti_slop,
        )
    )
    if body.directives is None:
        payload["directives"] = [dict(row) for row in existing.directives]
    if body.topology is None:
        payload["topology"] = dict(existing.topology)
    if body.anti_slop is None:
        payload["anti_slop"] = dict(existing.anti_slop)
    # PATCH carries no ``planner`` field (its door is PUT /api/prompts/planner);
    # preserve the stored block so an unrelated partial edit cannot reset it.
    payload["planner"] = planner_config(existing)
    payload["name"] = _require_name(payload["name"])
    payload["preset_id"] = preset_id
    _preserve_default_metadata(payload, existing)
    append_preset(log, payload)
    updated = get_preset(log, preset_id)
    assert updated is not None  # just appended
    return {"preset": _preset_wire(updated)}


@router.delete("/api/prompts/presets/{preset_id}")
async def delete_preset_route(preset_id: str, request: Request) -> dict:
    """Tombstone a preset; the default preset is undeletable (403, D26)."""
    if preset_id == DEFAULT_PRESET_ID:
        raise HTTPException(
            status_code=403, detail="default preset cannot be deleted"
        )
    log = _log_for(request)
    try:
        delete_preset(log, preset_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown preset") from None
    return {"deleted": preset_id}


# -- export / import ---------------------------------------------------------


def _filename(name: str, suffix: str = "preset.json") -> str:
    """A safe ``<slug>.<suffix>`` download name (ASCII, no header breakers)."""
    slug = "".join(
        c if (c.isascii() and (c.isalnum() or c in "-_")) else "-" for c in name
    ).strip("-_")
    return f"{slug or 'preset'}.{suffix}"


@router.get("/api/prompts/presets/{preset_id}/export")
async def export_preset_route(preset_id: str, request: Request) -> JSONResponse:
    """Export one preset as a JSON download (mirrors the profiles router).

    The body is the store's export shape (:func:`preset_to_json`) — exactly
    what ``POST /api/prompts/presets/import`` accepts back. The default row
    exports like any other (its ``is_default`` flag is what the import side
    forces off). ``404`` for an unknown id.
    """
    log = _log_for(request)
    preset = get_preset(log, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="unknown preset")
    return JSONResponse(
        preset_to_json(preset),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_filename(preset.name)}"'
            )
        },
    )


@router.post("/api/prompts/presets/import")
async def import_preset_route(request: Request) -> dict:
    """Import an exported preset as a NEW preset; return ``{preset}`` (no overwrite).

    The body is the export shape (bare) or a ``{"preset": {...}}`` wrapper —
    the same tolerance as the profiles importer. A fresh ``preset_id`` is
    ALWAYS minted (R6: importing the default row must never claim the default
    id), ``is_default`` is forced ``False`` and ``factory_version`` reset to
    ``0`` (the row is a copy, not a factory-seeded default), while ``name`` is
    kept (fallback ``"Imported preset"``). Everything else — modules, flows,
    surfaces, toggles, ``directives`` — rides through the store's tolerant
    normalization. A body that is not a JSON object is a ``422``.
    """
    log = _log_for(request)
    raw = await request.body()
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"preset body is not valid JSON: {exc}"
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=422, detail="a preset export must be a JSON object"
        )
    source = body["preset"] if isinstance(body.get("preset"), dict) else body
    payload = preset_to_json(preset_from_json(source))  # tolerant normalization
    if payload.get("directives"):
        # Duplicate ids are still invalid; nothing is protected (2026-10-09).
        _validated_directive_rows(payload["directives"])
    payload["preset_id"] = uuid.uuid4().hex
    payload["name"] = str(source.get("name") or "").strip() or "Imported preset"
    payload["is_default"] = False
    payload["factory_version"] = 0
    preset_id = append_preset(log, payload)
    created = get_preset(log, preset_id)
    assert created is not None  # just appended
    return {"preset": _preset_wire(created)}


# -- preview -----------------------------------------------------------------


def _degraded_parts(log: "EventLog", session_id: str, user_text: str) -> dict[str, Any]:
    """Minimal engine parts for the builder-less (test) degraded preview.

    Structural modules resolve to clearly-marked placeholders; ``text`` modules
    still render against the log-resolved ``{{variable}}`` map. This path exists
    only because a router can be mounted without the wiring layer — the live
    service always has ``app.state.message_builder`` (see module docstring).
    """
    return {
        "narrator_system": (
            "[degraded preview] live narrator builder unavailable — "
            "structural system prompt omitted"
        ),
        "library": (
            "[degraded preview] live narrator builder unavailable — "
            "OpenUI component library omitted"
        ),
        "history": [],
        "facts": "",
        "user_content": user_text,
        "recalled": (
            "[degraded preview] live narrator builder unavailable — the "
            "post-user <recalled> reference message is assembled by the builder"
        ),
        "variables": resolve_variables(log, session_id, user_text),
    }


def _item_prose(item: dict[str, Any]) -> str:
    """Prose text of one projected narrative item (segments fallback included)."""
    payload = item.get("payload") or {}
    prose = payload.get("prose")
    if isinstance(prose, str) and prose.strip():
        return prose
    segments = payload.get("segments") or []
    return "\n".join(
        str(seg.get("text", ""))
        for seg in segments
        if isinstance(seg, dict) and seg.get("kind") == "prose"
    )


def _last_narrative_prose(log: "EventLog", session_id: str) -> str:
    """Prose of the session's most recent narrative, or ``""`` when none."""
    narratives = log.get_projection(session_id).get("narratives", [])
    return _item_prose(narratives[-1]) if narratives else ""


def _transcript(log: "EventLog", session_id: str, limit: int = 20) -> str:
    """Best-effort labeled-free transcript of the newest narrative turns."""
    narratives = log.get_projection(session_id).get("narratives", [])[-limit:]
    blocks = [
        _item_prose(item)
        for item in narratives
    ]
    return "\n\n".join(block for block in blocks if block.strip())


def _flow_parts(
    interaction: str,
    log: "EventLog",
    session_id: str,
    user_text: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Best-effort engine parts for a non-narrate flow preview.

    System-prompt defaults are imported from their landed call sites (see the
    module docstring for the lazy-import cycle rationale). Structural payloads
    resolve from the live session where possible; caption/assistant payloads that
    need live assets/definitions fall back to clearly-marked placeholders.
    """
    parts: dict[str, Any] = {
        "variables": resolve_variables(log, session_id, user_text),
    }
    if interaction == "ooc":
        from fabulist.wiring import (  # lazy: wiring imports fabulist.api
            OOC_SYSTEM,
            facts_line_from_log,
            history_from_log,
            interaction_vision,
        )

        # Issue #79: OOC preview parity — resolve the OOC interaction's live
        # vision capability exactly like the live OOC turn builder does, so a
        # text-only OOC connection previews the bracketed caption marker rather
        # than image parts. Missing registry wiring degrades to text-only.
        registry = (
            getattr(request.app.state, "registry", None)
            if request is not None
            else None
        )
        vision = (
            interaction_vision(registry, log, "ooc", "narrator")()
            if registry is not None
            else False
        )
        parts.update(
            {
                "system": OOC_SYSTEM,
                "history": history_from_log(log, session_id, limit=8, vision=vision),
                "facts": facts_line_from_log(log, session_id) or "",
                "user_content": user_text,
            }
        )
    elif interaction == "extract":
        from fabulist.state.extractor import (  # lazy: keep import graph flat
            EXTRACTOR_SYSTEM_PROMPT,
            _turn_block,
        )

        parts.update(
            {
                "system": EXTRACTOR_SYSTEM_PROMPT,
                "turn_payload": _turn_block(
                    user_text, _last_narrative_prose(log, session_id)
                ),
            }
        )
    elif interaction == "summarize":
        from fabulist.graph.compaction import (  # lazy: avoid graph import at boot
            SUMMARIZER_SYSTEM_PROMPT,
            build_summarizer_messages,
        )

        payload = build_summarizer_messages(_transcript(log, session_id))[1]["content"]
        parts.update({"system": SUMMARIZER_SYSTEM_PROMPT, "payload": payload})
    elif interaction == "caption":
        from fabulist.media.images import (  # lazy: media layer is optional
            CAPTION_INSTRUCTION,
            CAPTION_SYSTEM,
        )

        parts.update(
            {
                "system": CAPTION_SYSTEM,
                "payload": (
                    "[preview] caption payload — the live call splices the uploaded "
                    "image content part (media.images.build_caption_messages); "
                    f"text: {CAPTION_INSTRUCTION}"
                ),
            }
        )
    elif interaction == "bts":
        from fabulist.state.bts import (  # lazy: keep import graph flat
            BTS_SYSTEM_PROMPT,
            build_bts_messages,
        )

        base = build_bts_messages(
            user_text, _last_narrative_prose(log, session_id), None, checkpoint=False
        )
        parts.update(
            {
                "system": str(base[0].get("content", BTS_SYSTEM_PROMPT)),
                "payload": str(base[1].get("content", "")) if len(base) > 1 else "",
            }
        )
    elif interaction == "assistant":
        from fabulist.assistants.manager import (  # lazy: optional feature module
            AssistantContext,
            render_scene_digest,
        )

        digest = render_scene_digest(
            AssistantContext(
                recent_prose=_last_narrative_prose(log, session_id),
                user_msg=user_text,
            )
        )
        parts.update(
            {
                "system": (
                    "[preview] assistant system prompt is per-definition "
                    "(assistants.manager.render_assistant_system)"
                ),
                "history": [],
                "user_content": digest,
            }
        )
    return parts


@router.post("/api/prompts/preview")
async def preview_route(body: PreviewRequest, request: Request) -> dict:
    """Render a model-facing message list for one turn (parity by construction).

    404 on an unknown session, 422 on empty ``user_text`` or an unknown
    ``interaction``. When a ``preset_id`` is given it must exist (404).
    ``interaction == "narrate"`` uses the production builder (``preview_mode:
    "builder"``) or, with no builder mounted, the degraded defaults path
    (``"degraded"``); any other interaction renders its flow over the active
    preset (or the built-in default flow) and reports ``"flow"``.
    """
    log = _log_for(request)
    _ensure_known_session(request, body.session_id)
    user_text = body.user_text.strip()
    if not user_text:
        raise HTTPException(
            status_code=422, detail={"reason": "user_text must be non-empty"}
        )
    interaction = body.interaction or "narrate"
    if interaction not in INTERACTIONS:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": f"unknown interaction: {interaction!r}",
                "known_interactions": list(INTERACTIONS),
            },
        )

    preview_preset: Preset | None = None
    if body.preset_id is not None:
        preview_preset = get_preset(log, body.preset_id)
        if preview_preset is None:
            raise HTTPException(status_code=404, detail="unknown preset")

    builder = getattr(request.app.state, "message_builder", None)
    if interaction == "narrate" and callable(builder):
        state = initial_turn_state(body.session_id, user_text, "narrate")
        result = builder(state)
        if inspect.isawaitable(result):
            result = await result
        messages = list(result)
        mode = "builder"
    elif interaction == "narrate":
        modules = (
            preview_preset.modules if preview_preset is not None else DEFAULT_MODULES
        )
        messages = compile_preset_messages(
            modules, _degraded_parts(log, body.session_id, user_text)
        )
        mode = "degraded"
    else:
        parts = _flow_parts(interaction, log, body.session_id, user_text, request)
        flow_preset = preview_preset or active_preset(log)
        if flow_preset is not None:
            messages = compile_preset_flow(flow_preset, interaction, parts)
        else:
            messages = compile_flow(interaction, DEFAULT_FLOWS[interaction], parts)
        mode = "flow"

    # Issue #79: image history rows carry transient render metadata so a
    # per-consumer seam can re-render them; it is engine bookkeeping and must
    # never appear in the preview payload (preview parity is a stated contract).
    from fabulist.media.images import MEDIA_META_KEY

    messages = [
        {key: value for key, value in message.items() if key != MEDIA_META_KEY}
        for message in messages
    ]
    chars = sum(len(str(message.get("content", ""))) for message in messages)
    return {"messages": messages, "chars": chars, "preview_mode": mode}
