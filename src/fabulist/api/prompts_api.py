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

Per-interaction flows (D18 completion, rewritten by #104)
--------------------------------------------------------
``interaction`` selects any of :data:`~fabulist.prompts.modules.INTERACTIONS`
(default ``narrate``). The builder fast path stays **narrate-only**; every other
interaction renders through the SAME assembly its live call site uses
(:mod:`fabulist.prompts.request_parts` for the post-turn/per-lane interactions,
the planner node's composer for ``planner``) with the previewed preset (or the
active one), and reports ``preview_mode: "flow"``. There is no second parts
assembly in this router: the old ``_flow_parts`` copy covered six of eleven
flows and returned a bare ``KeyError: 'system'`` for planner/npc/proposals and a
list the engine never sends for critic (issue #104). Anything a preview cannot
resolve (image bytes, an enabled assistant, an eligible lane member) is reported
in a RESPONSE FIELD beside the list — never as text inside a message (ADR-005).
The imports that reach the wiring/node layers stay **lazy inside the handler**:
``fabulist.wiring`` imports ``fabulist.api`` at module top and this router is
reached through the ``api`` package, so a top-level import would be a cycle.
Unknown ``interaction`` is 422.

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
    compile_preset_messages,
)
from fabulist.prompts.parts import default_directives, directive_to_row
from fabulist.prompts.preset_store import (
    DEFAULT_PRESET_ID,
    Preset,
    active_preset,
    append_preset,
    commit_default_to_factory,
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


#: Flows whose ``system`` row has a single engine default the studio can show.
#: ``planner`` is the one that ships EMPTY (issue #105): its directive lived
#: only in ``nodes.DEFAULT_PLANNER_DIRECTIVE``, so the block editor had nothing
#: to pre-fill and the badge's "type to override" was a blind box. The text is
#: resolved from the same constant the fallback seam uses (never copied here),
#: so the served default cannot drift from what an empty row would send.
_ENGINE_DEFAULT_SYSTEM_INTERACTIONS: frozenset[str] = frozenset({"planner"})


def _engine_default_system(interaction: str) -> str | None:
    """The engine default text for a flow's ``system`` row, else ``None``.

    ``None`` means "this flow has no single engine-default text a surface can
    honestly show" (narrate assembles its system prompt; the other flows'
    factory rows ship authored). Kept lazy: importing the node layer at module
    top would cycle (``fabulist.wiring`` imports ``fabulist.api``).
    """
    if interaction not in _ENGINE_DEFAULT_SYSTEM_INTERACTIONS:
        return None
    from fabulist.graph.nodes import DEFAULT_PLANNER_DIRECTIVE

    return DEFAULT_PLANNER_DIRECTIVE


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
    system prompt is authored or engine-supplied. ``system_default`` carries the
    engine default text for a flow whose ``system`` row can be empty (issue
    #105) so the block editor can pre-fill it. Read-only; nothing is written.
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
                # Issue #105: the text an EMPTY ``system`` row would send. The
                # block editor pre-fills it so no install keeps a blind box;
                # ``null`` when the flow has no single engine default.
                "system_default": _engine_default_system(interaction),
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


#: Per-interaction preview builders (#104). Each entry calls the SAME assembly
#: the interaction's live call site calls — the parts live in
#: :mod:`fabulist.prompts.request_parts` (and, for the planner, in the node's
#: composer), never here. This module only resolves the INPUTS a preview can
#: honestly resolve (the session's log, the previewed preset, the live
#: connection capabilities), so no second assembly can drift from the engine.
#: ``tests/test_prompts_api.py`` pins that every interaction has an entry.
_PREVIEW_BUILDERS: dict[str, Any] = {}


def _active_preset_or_none(log: "EventLog") -> Any:
    """The active preset, or ``None`` when the store cannot be read.

    ``None`` is the store's documented "render the code defaults" state, so a
    broken preset read previews the defaults instead of failing the drawer.
    """
    try:
        return active_preset(log)
    except Exception:  # noqa: BLE001 - the preview never hides the request (defaults)
        return None


def _builder_accepts_preset(builder: Any) -> bool:
    """Whether ``builder`` takes this module's ``preset_override`` keyword."""
    try:
        parameters = inspect.signature(builder).parameters
    except (TypeError, ValueError):  # builtins / C callables: assume no keyword
        return False
    if "preset_override" in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


async def _run_builder(
    builder: Any, state: dict, preset: Any = None
) -> list[dict]:
    """Call the app's message builder, honouring a non-active previewed preset.

    The production builder (``wiring.make_narrator_message_builder.build``) takes
    ``preset_override`` so the drawer can render a preset that is not active
    (issue #104). A test double with the historical one-argument shape still
    works — it is called plainly, exactly as before.
    """
    if preset is not None and _builder_accepts_preset(builder):
        result = builder(state, preset_override=preset)
    else:
        result = builder(state)
    if inspect.isawaitable(result):
        result = await result
    return list(result)


def _ooc_vision(request: Request, log: "EventLog") -> bool:
    """The OOC interaction's live vision capability (issue #79 parity)."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return False
    from fabulist.wiring import interaction_vision

    try:
        return bool(interaction_vision(registry, log, "ooc", "narrator")())
    except Exception:  # noqa: BLE001 - an unreadable slot is text-only
        return False


def _anti_injection(log: "EventLog") -> bool:
    """The persisted Anti Injection setting (the lane tail's wrap gate)."""
    from fabulist.state.settings import ANTI_INJECTION_DEFAULT, anti_injection_enabled

    try:
        return bool(anti_injection_enabled(log))
    except Exception:  # noqa: BLE001 - the shipped default, never a failed preview
        return ANTI_INJECTION_DEFAULT


def _last_plan_render(log: "EventLog", session_id: str) -> str:
    """The stored plan rendered the way the pre_turn stage hands it to a beat."""
    try:
        from fabulist.graph.plans import load_last_plan, render_plan_for_prompt

        plan = load_last_plan(log, session_id)
        return render_plan_for_prompt(plan) if plan is not None else ""
    except Exception:  # noqa: BLE001 - a beat is advisory
        return ""


async def _preview_ooc(log, session_id, user_text, preset, request):
    """OOC: the live OOC builder's list (history, envelope, ``[OOC]`` flag)."""
    from fabulist.prompts.request_parts import ooc_request

    vision = _ooc_vision(request, log)
    return ooc_request(log, session_id, user_text, preset, vision=vision), {
        "vision": vision
    }


async def _preview_extract(log, session_id, user_text, preset, request):
    """Extract: the extractor's own two-message request."""
    from fabulist.prompts.request_parts import extract_request

    return extract_request(user_text, _last_narrative_prose(log, session_id), preset), {}


async def _preview_summarize(log, session_id, user_text, preset, request):
    """Summarize: compaction's request over the session's newest prose."""
    from fabulist.prompts.request_parts import summarize_request

    return summarize_request(_transcript(log, session_id), preset), {}


async def _preview_caption(log, session_id, user_text, preset, request):
    """Caption: the captioner's request shape (instruction + one image part).

    A preview has no uploaded image to splice, so the image content part carries
    no bytes. That is stated in a response field — never as text inside the
    message list (ADR-005 / issue #104 condition 3).
    """
    from fabulist.prompts.request_parts import caption_request

    return caption_request({"payload": {}}, preset), {
        "notes": [
            "caption previews the request shape: the image part carries no bytes "
            "(the live call splices the stored asset)"
        ]
    }


async def _preview_assistant(log, session_id, user_text, preset, request):
    """Assistant: the enabled assistant's consult request (else the first one)."""
    from fabulist.assistants.manager import AssistantContext
    from fabulist.assistants.panel import (
        latest_chat_assistant,
        scene_facts_line,
        scene_recent_prose,
    )
    from fabulist.prompts.request_parts import assistant_request

    from fabulist.api.assistants_api import (
        assistant_definitions,
        resolve_assistant_definition,
    )

    definitions = assistant_definitions(log)
    enabled = latest_chat_assistant(log, session_id)
    definition = (
        resolve_assistant_definition(log, enabled, definitions=definitions)
        if enabled
        else None
    )
    extra: dict[str, Any] = {}
    if definition is None and definitions:
        definition = definitions[0]
        extra["notes"] = [
            "this chat has no enabled assistant; previewed with "
            f"{definition.id!r} (the first available definition)"
        ]
    if definition is None:  # pragma: no cover - the catalog always offers demos
        return [], {"assistant_id": None, "notes": ["no assistant definition exists"]}
    context = AssistantContext(
        facts=scene_facts_line(log, session_id),
        recent_prose=scene_recent_prose(log, session_id),
        user_msg=user_text,
    )
    extra["assistant_id"] = definition.id
    return assistant_request(definition, context, preset), extra


async def _preview_critic(log, session_id, user_text, preset, request):
    """Critic: the node's evidence bundle + payload (system + user)."""
    from fabulist.graph.nodes import critic_evidence
    from fabulist.prompts.request_parts import critic_request

    state = initial_turn_state(session_id, user_text, "critic")
    return critic_request(critic_evidence(log, session_id, state), preset), {}


async def _preview_planner(log, session_id, user_text, preset, request):
    """Planner: the node's own composer — directive + context block + the ask.

    Issue #104: the node composes the list (the flow only carries the directive),
    so the preview calls :func:`fabulist.graph.nodes.planner_request` — the same
    function the planner node calls. The context block therefore shows exactly
    what the planner reads (state, cast/world, world objects, the issue #106
    ``LORE (fixed canon)`` section, recent turns) and the ask is last.
    """
    from fabulist.graph.nodes import planner_request
    from fabulist.prompts.request_parts import planner_directive_messages

    state = initial_turn_state(session_id, user_text, "planner")
    compiled = planner_directive_messages(preset)
    directive = str(compiled[0].get("content") or "") if compiled else ""
    prepared = planner_request(
        log=log,
        session_id=session_id,
        state=state,
        directive=directive,
        planner_config=lambda: planner_config(preset),
        request_profile_fn=getattr(
            request.app.state, "planner_request_profile", None
        ),
    )
    return prepared.messages, {"planner_turn_seq": prepared.turn_seq}


async def _preview_npc(log, session_id, user_text, preset, request):
    """NPC: the narrator's prefix + ONE lane tail (a lane is never a request).

    The prefix is the SAME builder the turn uses; the tail is
    ``request_parts.npc_request`` wrapped by the same ``<preset_runtime>`` gate
    the lane applies. The member it rendered is named in a response field — never
    inside the list (ADR-005).
    """
    from fabulist.prompts.request_parts import (
        npc_lane_context,
        npc_request,
        wrap_lane_tail,
    )
    from fabulist.state.cast import ensure_roster
    from fabulist.state.presence import active_members

    builder = getattr(request.app.state, "message_builder", None)
    shared = (
        await _run_builder(
            builder, initial_turn_state(session_id, user_text, "narrate")
        )
        if callable(builder)
        else []
    )
    members: list[Any] = []
    try:
        from fabulist.state.cast import member_by_id, roster

        ensure_roster(log, session_id)
        member_ids, _source = active_members(log, session_id)
        cast = roster(log, session_id)
        members = [
            member
            for member in (member_by_id(cast, member_id) for member_id in member_ids)
            if member is not None
        ]
    except Exception:  # noqa: BLE001 - an empty set is a legal turn (no lanes)
        members = []
    beat = _last_plan_render(log, session_id)
    extra: dict[str, Any] = {"lane_member": None, "lane_member_id": None}
    if members:
        member = members[0]
        context = npc_lane_context(log, session_id, member, beat=beat)
        extra.update(lane_member=member.display_name, lane_member_id=member.member_id)
    else:
        context = {
            "name": "",
            "aliases": [],
            "categories": {},
            "witnessed": [],
            "beat": beat,
            "last_intent": "",
        }
        extra["notes"] = [
            "no eligible cast member in this chat; the lane tail is previewed "
            "with an unnamed member"
        ]
    tail = npc_request(
        initial_turn_state(session_id, user_text, "npc"), context, preset
    )
    tail = wrap_lane_tail(tail, _anti_injection(log))
    return [*shared, *tail], extra


async def _preview_proposals(log, session_id, user_text, preset, request):
    """Proposals: ONE data-type lane's request (the LANE sends it standalone).

    ``proposals`` is a ``LANE_INTERACTIONS`` member, but the engine's router hands
    the lane these messages with NO narrator prefix (``AssetProposalFanout.route``
    builds ``messages`` and the per-type node sends exactly that). The preview
    renders the same list for the first data type and names that type in a
    response field.
    """
    from fabulist.prompts.request_parts import proposal_lane_blocks, proposals_request
    from fabulist.state.proposal_lanes import PROPOSAL_LANES

    spec = PROPOSAL_LANES[0]
    blocks = proposal_lane_blocks(
        log,
        spec,
        touched=(),
        prose=_last_narrative_prose(log, session_id),
    )
    messages = proposals_request(blocks, session_id, preset)
    return messages, {
        "lane": spec.lane,
        "lane_kind": spec.kind,
        "lane_label": spec.label,
    }


async def _preview_bts(log, session_id, user_text, preset, request):
    """BTS: the world-state pass's request over the last committed turn."""
    from fabulist.prompts.request_parts import bts_request

    return (
        bts_request(
            user_text, _last_narrative_prose(log, session_id), None, preset=preset
        ),
        {},
    )


_PREVIEW_BUILDERS.update(
    {
        "ooc": _preview_ooc,
        "extract": _preview_extract,
        "summarize": _preview_summarize,
        "caption": _preview_caption,
        "assistant": _preview_assistant,
        "bts": _preview_bts,
        "critic": _preview_critic,
        "planner": _preview_planner,
        "npc": _preview_npc,
        "proposals": _preview_proposals,
    }
)


async def _flow_preview(
    interaction: str,
    log: "EventLog",
    session_id: str,
    user_text: str,
    preset: Any,
    request: Request,
) -> tuple[list[dict], dict]:
    """Render one non-narrate interaction through its live assembly."""
    build = _PREVIEW_BUILDERS.get(interaction)
    if build is None:  # pragma: no cover - the completeness test pins the table
        raise HTTPException(
            status_code=500,
            detail={"reason": f"no preview builder for interaction {interaction!r}"},
        )
    return await build(log, session_id, user_text, preset, request)


@router.post("/api/prompts/preview")
async def preview_route(body: PreviewRequest, request: Request) -> dict:
    """Render a model-facing message list for one turn (parity by construction).

    404 on an unknown session, 422 on empty ``user_text`` or an unknown
    ``interaction``. When a ``preset_id`` is given it must exist (404) and it is
    the preset RENDERED — including ``narrate``, whose builder takes the override
    (issue #104: the drawer used to preview the active preset's prompt while the
    drawer said otherwise).

    Every branch renders through the interaction's ONE live assembly
    (:mod:`fabulist.prompts.request_parts`, or the planner node's composer), so
    the list is the list the engine sends. Nothing preview-only is ever added to
    a message: substituted inputs (no image bytes, no enabled assistant, no
    eligible lane member) and the lane member/data type are reported in response
    FIELDS beside the list (ADR-005).

    ``preview_mode``: ``"builder"`` (narrate, production builder),
    ``"degraded"`` (narrate with no builder mounted — the test harness), or
    ``"flow"`` (any other interaction).
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
    extra: dict[str, Any] = {}
    if interaction == "narrate" and callable(builder):
        state = initial_turn_state(body.session_id, user_text, "narrate")
        messages = await _run_builder(builder, state, preview_preset)
        mode = "builder"
    elif interaction == "narrate":
        modules = (
            preview_preset.modules if preview_preset is not None else DEFAULT_MODULES
        )
        messages = compile_preset_messages(
            modules, _degraded_parts(log, body.session_id, user_text)
        )
        mode = "degraded"
        extra["notes"] = [
            "no live message builder is mounted; structural parts are "
            "placeholders (the live service always mounts one)"
        ]
    else:
        flow_preset = (
            preview_preset
            if preview_preset is not None
            else _active_preset_or_none(log)
        )
        messages, extra = await _flow_preview(
            interaction,
            log,
            body.session_id,
            user_text,
            flow_preset,
            request,
        )
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
    return {
        "messages": messages,
        "chars": chars,
        "preview_mode": mode,
        "interaction": interaction,
        "preset_id": preview_preset.preset_id if preview_preset is not None else None,
        **extra,
    }
