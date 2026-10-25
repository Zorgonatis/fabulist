"""Node templates (W9.10, R19/R21): the reusable node library the palette draws.

The turn topology (:data:`fabulist.graph.graph.CANONICAL_TOPOLOGY`) is authored
as data and the graph builder instantiates one FACTORY per canonical node id
(:func:`fabulist.graph.graph._node_factories`). A **node template** is the
session-independent asset on top of that: a named, editable row that says
*"this node behaviour, configured like this"*, so a graph assembled from the
palette is a topology of template REFERENCES rather than of hard-coded stage
names.

Event contract (fixed; the log owns everything, D9)
---------------------------------------------------
Two system-event kinds ride the reserved :data:`NODE_TEMPLATES_ANCHOR` session::

    {"event": "node_template", "id": "tpl_narrate", "name": "Narrator turn",
     "klass": "model_pass", "implements": "narrate",
     "interaction": "narrate", "step_id": "narrate",
     "config": {}, "description": "...", "builtin": true}

    {"event": "node_template_deleted", "template_id": "tpl_narrate"}

Fold is latest-wins per ``id``; a tombstone removes the id from the fold and
suppresses its (built-in) seed, so deleting a seeded template STICKS instead of
resurrecting on the next read (append-only log, D9/D10 — the prior row stays
queryable as history).

``implements`` is the CANONICAL node id whose factory executes the row
(``narrate`` / ``critic`` / ``bts`` / …). That is what makes a template
EXECUTABLE: :func:`fabulist.graph.graph.build_graph` resolves a topology node
carrying ``"template": "tpl_x"`` to the factory of ``tpl_x.implements``, and
:func:`fabulist.graph.contracts.validate_topology` reads the same behaviour for
its requires/provides invariants. ``klass`` is the node's primitive
(``model_pass`` / ``gate`` / ``deterministic`` / ``terminal``);
``interaction`` / ``step_id`` are the prompt-door binding (nullable):
``interaction`` is the flow interaction the model pass renders, ``step_id`` the
W6 step editor that owns its prompt.

Seeding
-------
:func:`seed_template_rows` derives one template per canonical node from the code
itself (:data:`fabulist.flow.manifest.TURN_NODES` for title / description /
prompt door, :data:`fabulist.graph.contracts.NODE_CONTRACTS` for the klass, the
canonical topology row for the node ``data``), with deterministic ids
(``tpl_<canonical id>``). :func:`ensure_node_templates` appends each MISSING
row once — seed-only-if-missing, like ``preset_store.ensure_default_preset`` — so
the palette library is never empty and re-running it writes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from .contracts import NODE_CONTRACTS, NodeKlass

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "NODE_TEMPLATES_ANCHOR",
    "NODE_TEMPLATE_DELETED_EVENT",
    "NODE_TEMPLATE_EVENT",
    "NODE_TEMPLATE_KLASSES",
    "TEMPLATE_ID_PREFIX",
    "NodeTemplate",
    "append_node_template",
    "delete_node_template",
    "ensure_node_templates",
    "get_node_template",
    "lane_config",
    "library_rows",
    "list_node_templates",
    "referencing_presets",
    "seed_template_rows",
    "template_behaviours",
    "template_from_wire",
    "template_to_wire",
    "update_node_template",
]

#: Reserved session id anchoring node-template events (the ``__`` prefix keeps it
#: out of the session list, like ``__presets__`` / ``__worlds__``).
NODE_TEMPLATES_ANCHOR = "__node_templates__"

#: System-event kind carrying one template definition (latest wins per id).
NODE_TEMPLATE_EVENT = "node_template"

#: System-event kind tombstoning a template (also suppresses its built-in seed).
NODE_TEMPLATE_DELETED_EVENT = "node_template_deleted"

#: The primitive vocabulary a template may declare (mirrors
#: :data:`fabulist.graph.contracts.NodeContract`).
NODE_TEMPLATE_KLASSES: tuple[NodeKlass, ...] = (
    "model_pass",
    "gate",
    "deterministic",
    "terminal",
)

#: Seeded ids are ``tpl_<canonical node id>`` — deterministic, so seeding the
#: same code twice is a no-op rather than a second copy.
TEMPLATE_ID_PREFIX = "tpl_"

#: Actor stamped on store writes (mirrors the personas/worlds lanes).
USER_ACTOR = "user"


@dataclass(frozen=True)
class NodeTemplate:
    """One node template folded from the ``__node_templates__`` anchor.

    ``template_id`` is the asset id (wired as ``"id"`` — the shape the topology
    node rows reference); ``implements`` is the canonical node id whose factory
    executes the row; ``config`` is node data (e.g. ``{"max_lanes": 4}``);
    ``builtin`` marks a seeded row (informational — it is editable and
    deletable like any other).
    """

    template_id: str
    name: str
    klass: NodeKlass
    implements: str
    interaction: str | None = None
    step_id: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    builtin: bool = False


def _text(value: Any) -> str:
    """Coerce an optional field to a plain string (``None`` -> ``""``)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _optional_str(value: Any) -> str | None:
    """Coerce an optional field to a stripped string, or ``None`` when empty."""
    text = _text(value).strip()
    return text or None


def _klass(value: Any) -> NodeKlass:
    """Read a ``klass`` field; anything unknown falls back to ``model_pass``."""
    text = _text(value).strip()
    return text if text in NODE_TEMPLATE_KLASSES else "model_pass"  # type: ignore[return-value]


def template_from_wire(payload: Mapping[str, Any]) -> NodeTemplate:
    """Normalize one stored/request payload into a :class:`NodeTemplate`.

    Tolerant by design (the anchor is log data and may be hand-edited): a
    missing ``implements`` reads as ``""`` (which the resolver refuses), and an
    unknown ``klass`` falls back to ``model_pass``. ``config`` is copied, never
    aliased, so a caller cannot mutate the fold.
    """
    config = payload.get("config")
    return NodeTemplate(
        template_id=_text(payload.get("id")).strip(),
        name=_text(payload.get("name")).strip(),
        klass=_klass(payload.get("klass")),
        implements=_text(payload.get("implements")).strip(),
        interaction=_optional_str(payload.get("interaction")),
        step_id=_optional_str(payload.get("step_id")),
        config=dict(config) if isinstance(config, Mapping) else {},
        description=_text(payload.get("description")),
        builtin=bool(payload.get("builtin", False)),
    )


def template_to_wire(template: NodeTemplate) -> dict[str, Any]:
    """The wire shape of one template (``id`` is the asset id)."""
    return {
        "id": template.template_id,
        "name": template.name,
        "klass": template.klass,
        "implements": template.implements,
        "interaction": template.interaction,
        "step_id": template.step_id,
        "config": dict(template.config),
        "description": template.description,
        "builtin": template.builtin,
    }


def _require(template: NodeTemplate) -> NodeTemplate:
    """Reject a template the engine could never run (422 at the door).

    A template exists to be EXECUTED, so ``name`` must be non-empty and
    ``implements`` must name a canonical node contract — the builder resolves a
    template node to exactly that factory, and refusing here is what keeps
    ``build_graph`` from ever meeting an unrunnable row.
    """
    if not template.template_id:
        raise ValueError("a node template needs an id")
    if not template.name:
        raise ValueError("a node template needs a non-empty name")
    if template.implements not in NODE_CONTRACTS:
        raise ValueError(
            f"node template {template.name!r} implements {template.implements!r}, "
            f"which is not a node contract; known: {', '.join(NODE_CONTRACTS)}"
        )
    return template


def seed_template_rows() -> list[dict[str, Any]]:
    """The built-in library: one template per canonical node, derived from code.

    Sources (never hand-copied): :data:`fabulist.flow.manifest.TURN_NODES` for
    title / description / prompt door / interaction,
    :data:`fabulist.graph.contracts.NODE_CONTRACTS` for the klass, and the
    canonical topology row for the node ``data`` (the fan-out's ``max_lanes``).
    Imports are lazy because ``graph.graph`` carries langgraph and
    ``flow.manifest`` is a consumer of the graph, not the other way round.
    """
    from fabulist.flow.manifest import TURN_NODES
    from fabulist.graph.graph import CANONICAL_TOPOLOGY

    data_by_id = {
        node["id"]: dict(node.get("data") or {}) for node in CANONICAL_TOPOLOGY["nodes"]
    }
    rows: list[dict[str, Any]] = []
    for node in TURN_NODES:
        node_id = str(node.get("id") or "")
        contract = NODE_CONTRACTS.get(node_id)
        if not node_id or contract is None:
            continue
        door = node.get("prompt_surface") or {}
        runs = node.get("runs") or {}
        rows.append(
            {
                "id": f"{TEMPLATE_ID_PREFIX}{node_id}",
                "name": _text(node.get("title")) or node_id,
                "klass": contract.klass,
                "implements": node_id,
                "interaction": _optional_str(runs.get("interaction")),
                "step_id": (
                    _optional_str(door.get("step_id"))
                    if door.get("door") == "step_editor"
                    else None
                ),
                "config": data_by_id.get(node_id, {}),
                "description": _text((node.get("meta") or {}).get("description")),
                "builtin": True,
            }
        )
    return rows


def _fold(log: "EventLog") -> tuple[dict[str, NodeTemplate], set[str]]:
    """``(templates by id, tombstoned ids)`` folded latest-wins from the anchor."""
    templates: dict[str, NodeTemplate] = {}
    deleted: set[str] = set()
    for event in log.get_events(NODE_TEMPLATES_ANCHOR):
        payload = event.payload or {}
        if not isinstance(payload, Mapping):
            continue
        # The anchor rides ``system`` events; the KIND is the payload's ``event``
        # key — the same shape ``__presets__`` / ``__worlds__`` use.
        kind = payload.get("event")
        if kind == NODE_TEMPLATE_DELETED_EVENT:
            template_id = _text(payload.get("template_id")).strip()
            if not template_id:
                continue
            templates.pop(template_id, None)
            deleted.add(template_id)
        elif kind == NODE_TEMPLATE_EVENT:
            template = template_from_wire(payload)
            if not template.template_id:
                continue
            templates[template.template_id] = template
            deleted.discard(template.template_id)
    return templates, deleted


def _append(log: "EventLog", template: NodeTemplate) -> None:
    """Append one template definition event (the store's write shape, D9)."""
    log.append_event(
        NODE_TEMPLATES_ANCHOR,
        "system",
        USER_ACTOR,
        {"event": NODE_TEMPLATE_EVENT, **template_to_wire(template)},
    )


def ensure_node_templates(log: "EventLog") -> list[NodeTemplate]:
    """Append every MISSING built-in template once, then return the library.

    Seed-only-if-missing (the ``ensure_default_preset`` discipline): an existing
    row — seeded or owner-edited — is never overwritten, and a tombstoned id is
    not re-seeded, so deleting a built-in sticks. Re-running writes nothing.
    """
    templates, deleted = _fold(log)
    for row in seed_template_rows():
        if row["id"] in templates or row["id"] in deleted:
            continue
        _append(log, _require(template_from_wire(row)))
    templates, _deleted = _fold(log)
    return list(templates.values())


def _executable(template: Any) -> bool:
    """Does ``template`` implement a node id that still exists (W9.23 §3.6.4)?

    Templates are stored rows and seeding is seed-only-if-missing, so a row for a
    RETIRED behaviour (the W9.22 ``asset_proposals`` / ``asset_proposal_lane``
    pair) can still be in the log. No stored row is touched — the library READS
    filter on ``implements in NODE_CONTRACTS``, so a dead template drops out of
    the palette instead of sitting there unplaceable. A stored topology that
    still references one is refused by the builder (unknown template) and falls
    back to canonical, which is the same door every broken save takes.
    """
    return getattr(template, "implements", None) in NODE_CONTRACTS


def library_rows(templates: "Mapping[str, Any]" | None) -> list[dict]:
    """The digest payload for a template library (W9.10).

    The engine rebuilds the compiled graph when the graph's IDENTITY changes.
    Identity is the topology PLUS the library: a template edit (``implements``,
    ``config``, prompt door) must reboot the graph exactly like a topology edit,
    or the engine silently keeps running the old node. Kept here so the digest's
    field set has one definition and one test. Rows whose behaviour no longer
    exists are skipped (:func:`_executable`) — they cannot execute, so they are
    not part of the graph's identity either.
    """
    rows: list[dict] = []
    for key in sorted((templates or {}), key=str):
        tpl = (templates or {})[key]
        if not _executable(tpl):
            continue
        rows.append(
            {
                "id": str(getattr(tpl, "template_id", key)),
                "implements": getattr(tpl, "implements", None),
                "klass": getattr(tpl, "klass", None),
                "interaction": getattr(tpl, "interaction", None),
                "step_id": getattr(tpl, "step_id", None),
                "config": dict(getattr(tpl, "config", None) or {}),
            }
        )
    return rows


def lane_config(
    topology: "Mapping[str, Any] | None", templates: "Mapping[str, Any]" | None
) -> dict:
    """``max_lanes``/``presence_window`` config for the fan-out node.

    The fan-out is found by BEHAVIOUR: the node whose id is ``npc_lanes`` or
    whose TEMPLATE implements it — a from-scratch graph may place the fan-out
    under any node id. A template's ``config`` overrides the row's ``data``
    (a template says "this behaviour, configured like this"), and the template is
    authoritative when the two disagree.

    ``enabled`` is deliberately NOT part of this read's contract beyond riding
    along in the merged mapping: the off switch (issue #56.3) decides the SHAPE
    of the compiled graph, so the builder reads it from the topology row alone
    (:func:`fabulist.graph.contracts.stage_disabled`) — see
    :func:`fabulist.graph.graph._strip_disabled_stages`.
    """
    library = templates or {}
    for node in (topology or {}).get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        template = library.get(str(node.get("template") or ""))
        behaviour = (
            getattr(template, "implements", None)
            if template is not None
            else None
        ) or str(node.get("id") or "")
        if behaviour != "npc_lanes":
            continue
        data = dict(node.get("data") or {})
        if template is not None:
            data.update(dict(getattr(template, "config", None) or {}))
        return data
    return {}


def list_node_templates(log: "EventLog") -> list[NodeTemplate]:
    """The PALETTE library: every executable template (built-ins seeded first).

    Rows whose behaviour no longer exists are filtered out (:func:`_executable`),
    so a Studio palette never offers a node the builder cannot place. They stay
    addressable by id (:func:`get_node_template`) and deletable, so an owner (or
    the API) can still clean one up.
    """
    return [
        template for template in ensure_node_templates(log) if _executable(template)
    ]


def get_node_template(log: "EventLog", template_id: str) -> NodeTemplate | None:
    """One template by id (``None`` when the library has no such row)."""
    for template in ensure_node_templates(log):
        if template.template_id == template_id:
            return template
    return None


def append_node_template(
    log: "EventLog",
    payload: Mapping[str, Any],
    *,
    template_id: str | None = None,
) -> NodeTemplate:
    """Create (fresh id) or replace (existing id) one template; returns the row.

    A supplied id that already exists is a latest-wins replace, which is what
    PATCH/DELETE-style edits ride; no id mints ``tpl_<uuid>``. Validation is
    :func:`_require` — a row the engine could not execute is refused here, so it
    is never stored.
    """
    data = dict(payload)
    target_id = (template_id or _text(data.get("id"))).strip()
    template = _require(
        template_from_wire({**data, "id": target_id or f"{TEMPLATE_ID_PREFIX}{uuid.uuid4().hex[:8]}"})
    )
    _append(log, template)
    return template


def update_node_template(
    log: "EventLog", template_id: str, patch: Mapping[str, Any]
) -> NodeTemplate:
    """Merge ``patch`` over the stored template (latest-wins append).

    ``LookupError`` when the id is unknown: PATCH never creates. The merged row
    is validated like a create, so an edit cannot make a row unrunnable.
    """
    existing = get_node_template(log, template_id)
    if existing is None:
        raise LookupError(template_id)
    merged = template_to_wire(existing)
    for key, value in patch.items():
        if key == "id":
            continue
        merged[key] = value
    template = _require(template_from_wire(merged))
    _append(log, template)
    return template


def delete_node_template(log: "EventLog", template_id: str) -> None:
    """Tombstone one template; ``LookupError`` when the library has no such row.

    Reference checking is the caller's (the router refuses a template a stored
    topology uses — the store cannot know a preset's mind without the preset
    fold, and the fold is not its data).
    """
    if get_node_template(log, template_id) is None:
        raise LookupError(template_id)
    log.append_event(
        NODE_TEMPLATES_ANCHOR,
        "system",
        USER_ACTOR,
        {"event": NODE_TEMPLATE_DELETED_EVENT, "template_id": template_id},
    )


def template_behaviours(log: "EventLog") -> dict[str, str]:
    """``{template id: canonical node id}`` — what the builder/validator resolve.

    The one mapping :func:`fabulist.graph.graph.build_graph` and
    :func:`fabulist.graph.contracts.validate_topology` take, so a topology node
    carrying ``"template"`` executes (and is judged by) the behaviour its
    template implements. Seeds first (read = ensure), so the mapping is never
    empty on a fresh log.

    Templates whose ``implements`` names a RETIRED behaviour are omitted
    (:func:`_executable`): resolving one would hand the builder a behaviour id it
    has no factory for, so the topology that references it is refused by name and
    takes the canonical fallback.
    """
    return {
        template.template_id: template.implements
        for template in ensure_node_templates(log)
        if _executable(template)
    }


def _topology_template_ids(topology: Any) -> list[str]:
    """Every template id a topology's node rows reference (declaration order)."""
    if not isinstance(topology, Mapping):
        return []
    nodes = topology.get("nodes")
    if not isinstance(nodes, (list, tuple)):
        return []
    out: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        template_id = node.get("template")
        if isinstance(template_id, str) and template_id and template_id not in out:
            out.append(template_id)
    return out


def referencing_presets(log: "EventLog", template_id: str) -> list[str]:
    """Preset ids whose STORED topology references ``template_id``.

    The delete guard reads what presets actually carry
    (:func:`fabulist.prompts.preset_store.stored_topology`), not the validated
    engine read: a topology the owner saved is a reference even while it is
    broken. The store import is lazy — ``preset_store`` pulls the prompt
    factories and this module is imported by the API/wiring layer.
    """
    from fabulist.prompts.preset_store import list_presets, stored_topology

    return [
        preset.preset_id
        for preset in list_presets(log)
        if template_id in _topology_template_ids(stored_topology(preset))
    ]
