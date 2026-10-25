"""Flow manifest router (W9.1) — the read-only topology snapshot for the UI.

Serves :func:`fabulist.flow.manifest.build_manifest` directly (decision 2:
single source of truth, so the endpoint and the committed artifact
``prompts/generated/flow-manifest.json`` cannot drift). The artifact carries the
preset-independent ``agent.default_agent``; this endpoint overlays the ACTIVE
preset's ``agent.bound_agent`` from
:func:`~fabulist.prompts.agent_sets.bindings_for` — the same
``default_agent``/``bound_agent`` split ``GET /api/prompts/steps`` uses.

Read-only (C1): the manifest is descriptive data, never truth, and nothing here
appends an event.

Endpoint
--------
  GET /api/flow/manifest -> the full manifest (schema_version, generated_from,
      activation_map, graphs[]), with ``bound_agent`` populated per node and
      every fan-out descriptor overlaid with the stored topology's EFFECTIVE
      lane settings (``max_parallel``, ``enabled`` — issues #56.2/#56.3).
  GET /api/sessions/{id}/lanes -> the CURRENT turn's lane set (the planned
      members while the fan-out is in flight, their intents once the join has
      written them) plus the effective width and on/off state (issue #56.1).

Wiring (orchestrator-owned; one line in ``app.py``)::

    from fabulist.api.flow_api import router as flow_router
    app.include_router(flow_router)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from fabulist.flow.manifest import build_manifest
from fabulist.prompts.agent_sets import DEFAULT_BINDINGS, bindings_for, resolve_agent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["flow"])


def _log_for(request: Request) -> "EventLog | None":
    """The app's event log, or ``None`` when the app was built without one."""
    return getattr(request.app.state, "log", None)


def _overlay_bound_agents(manifest: dict[str, Any], bindings: dict[str, Any]) -> None:
    """Write the active preset's ``bound_agent`` onto every agent-bearing node.

    ``resolve_agent`` never raises: a missing/blank binding degrades to the
    node's ``default_agent`` (the D19 default baked into the artifact).
    """
    for graph in manifest["graphs"]:
        for node in graph["nodes"]:
            agent = node.get("agent")
            if not agent:
                continue
            interaction = agent.get("interaction")
            if interaction:
                agent["bound_agent"] = resolve_agent(
                    bindings, interaction, fallback=agent.get("default_agent")
                )
            else:
                agent["bound_agent"] = agent.get("default_agent")


@router.get("/api/flow/manifest")
async def get_flow_manifest(request: Request) -> dict[str, Any]:
    """The code-authored turn/off-turn topology, with the active preset's bindings."""
    manifest = build_manifest()
    log = _log_for(request)
    bindings = bindings_for(log) if log is not None else dict(DEFAULT_BINDINGS)
    _overlay_bound_agents(manifest, bindings)
    _overlay_lane_settings(manifest, log)
    return manifest


def _overlay_lane_settings(
    manifest: dict[str, Any], log: "EventLog | None"
) -> None:
    """Write the EFFECTIVE lane settings onto every fan-out descriptor.

    The artifact describes the CODE (``max_parallel: 4`` is the canonical node
    data). What the engine will run is the stored topology's value — the owner
    can change it (issue #56.2) or switch the stage off entirely (#56.3) — so
    the fan-out descriptor is overlaid exactly like ``bound_agent`` is. A reader
    that only sees the artifact keeps the code default; a reader of the endpoint
    sees what the next turn will actually do.
    """
    settings = lane_settings(log)
    for graph in manifest.get("graphs") or []:
        for node in graph.get("nodes") or []:
            fanout = node.get("fanout")
            if not isinstance(fanout, dict):
                continue
            fanout["max_parallel"] = settings["max_parallel"]
            fanout["enabled"] = settings["enabled"]


@router.get("/api/sessions/{session_id}/lanes")
async def get_lanes_route(session_id: str, request: Request) -> dict[str, Any]:
    """The CURRENT turn's NPC lane set — the members fanned out, live or done.

    The flow panel draws one arm per member here (owner: "unless you can draw
    the actual members don't bother"). Read-only; the log is the source:

    * ``npc_lanes_plan`` (issue #56.1) is appended at DISPATCH time, before any
      lane runs, and names the members this turn is about to lane. While the
      newest plan is ahead of the newest ``npc_lanes`` summary the turn is IN
      FLIGHT: the plan's members are served, marked ``pending`` (the drawer
      draws them as running, with no intent yet). This is what the preview used
      to get wrong — with nothing written until the fan-in join, the drawer read
      the PREVIOUS turn's set for the whole of the current turn.
    * ``npc_lane`` rows between the previous summary and the newest one carry
      each member's intent and outcome. That is the completed set, and it is
      also the answer after a reload (the plan row is superseded).

    ``max_parallel`` and ``enabled`` are the EFFECTIVE lane settings of the
    stored topology (issue #56.2/#56.3), not the code default: ``max_lanes`` is
    concurrency (bounded fan-out width, never the lane set) and ``enabled``
    false means the stage is pruned from the graph, so no arms will ever land.
    """
    from fastapi import HTTPException

    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    events = get_chain_events_or_session(log, session_id)
    settings = lane_settings(log)

    def _payload(event: Any) -> dict:
        payload = getattr(event, "payload", None)
        return payload if isinstance(payload, dict) else {}

    summaries = [
        event
        for event in events
        if event.type == "system" and _payload(event).get("event") == "npc_lanes"
    ]
    plans = [
        event
        for event in events
        if event.type == "system"
        and _payload(event).get("event") == "npc_lanes_plan"
    ]
    empty = {
        "session_id": session_id,
        "lanes": [],
        "source": None,
        "max_parallel": settings["max_parallel"],
        "enabled": settings["enabled"],
        "in_flight": False,
        "turn_seq": None,
    }

    # W9.16: the PLAYER is never a cast arm. A stale lane row from before the
    # engine excluded player surfaces ("User") must not be drawn as cast.
    from fabulist.state.cast import canonical_key
    from fabulist.state.presence import PLAYER_SURFACES

    plan = plans[-1] if plans else None
    if plan is None and not summaries:
        return empty
    newest_summary_seq = summaries[-1].seq if summaries else -1
    if plan is not None and plan.seq > newest_summary_seq:
        members = [row for row in (_payload(plan).get("members") or []) if isinstance(row, dict)]
        lanes = [
            {
                "member_id": row.get("member_id"),
                "name": row.get("name"),
                # No answer yet: the lane is in flight, not empty.
                "intent": "",
                "ok": True,
                "pending": True,
            }
            for row in members
            if canonical_key(row.get("name")) not in PLAYER_SURFACES
        ]
        return {
            **empty,
            "lanes": lanes,
            "source": _payload(plan).get("source"),
            "in_flight": True,
            "turn_seq": plan.seq,
        }

    latest = summaries[-1]
    previous_seq = summaries[-2].seq if len(summaries) > 1 else -1
    rows = [
        _payload(event)
        for event in events
        if previous_seq < event.seq < latest.seq
        and event.type == "system"
        and _payload(event).get("event") == "npc_lane"
    ]
    lanes = [
        {
            "member_id": row.get("member_id"),
            "name": row.get("name"),
            "intent": row.get("intent"),
            "ok": bool(row.get("ok", True)),
            "pending": False,
        }
        for row in rows
        if canonical_key(row.get("name")) not in PLAYER_SURFACES
    ]
    return {
        **empty,
        "lanes": lanes,
        "source": _payload(latest).get("source"),
        "turn_seq": latest.seq,
    }


def lane_settings(log: "EventLog | None") -> dict[str, Any]:
    """The EFFECTIVE lane settings: ``{max_parallel, enabled}``.

    Read from the topology the engine would compile (the active preset's stored
    topology, validated, falling back to canonical) — NOT from the code-authored
    manifest, which is why a saved ``max_lanes`` edit used to be invisible here
    and a switched-off stage still read as four lanes wide. The fan-out is found
    by BEHAVIOUR (:func:`fabulist.graph.node_templates.lane_config`), so a
    template-placed fan-out under another node id reads correctly too.

    ``enabled`` is false when the topology does not declare a fan-out at all (a
    from-scratch graph without lanes) or when its node data switches it off.
    Any failure degrades to the code default rather than 500ing a read: this
    endpoint draws a preview, it is not a source of truth.
    """
    default = {"max_parallel": _manifest_width(), "enabled": True}
    if log is None:
        return default
    try:
        from fabulist.graph.contracts import implements_of, stage_disabled
        from fabulist.graph.node_templates import (
            lane_config,
            list_node_templates,
            template_behaviours,
        )
        from fabulist.prompts.preset_store import active_preset, preset_topology

        topology = preset_topology(active_preset(log), template_behaviours(log))
        library = {tpl.template_id: tpl for tpl in list_node_templates(log)}
        fanout_node: dict | None = None
        for node in (topology or {}).get("nodes") or []:
            if not isinstance(node, dict):
                continue
            template_id = node.get("template")
            behaviour = (
                implements_of(library.get(template_id))
                if isinstance(template_id, str) and template_id
                else str(node.get("id") or "")
            )
            if behaviour == "npc_lanes":
                fanout_node = node
                break
        if fanout_node is None:
            # A from-scratch topology with no fan-out at all: no lane will ever
            # run, which is exactly what "disabled" means to the panel.
            return {"max_parallel": default["max_parallel"], "enabled": False}
        # The WIDTH rides the same read the node is built from (a template's
        # ``config`` overrides the row's ``data``); the SWITCH is row data only,
        # because it decides the graph's shape rather than a node's setting
        # (:func:`fabulist.graph.contracts.stage_disabled`).
        data = lane_config(topology, library)
        width = int(data.get("max_lanes") or 0) or 4
        return {
            "max_parallel": max(1, width),
            "enabled": not stage_disabled(fanout_node.get("data")),
        }
    except Exception:  # noqa: BLE001 - a preview read never fails the panel
        return default


def _manifest_width() -> int:
    """The pool width from the manifest's npc_lanes fanout descriptor."""
    for node in build_manifest()["graphs"][0]["nodes"]:
        if node["id"] == "npc_lanes":
            fanout = node.get("fanout") or {}
            return int(fanout.get("max_parallel") or 4)
    return 4


def get_chain_events_or_session(log: "EventLog", session_id: str) -> list[Any]:
    """Fork-aware chain read (session-constant context must walk the chain)."""
    try:
        return list(log.get_chain_events(session_id))
    except Exception:  # noqa: BLE001 - fall back to the local view
        return list(log.get_events(session_id))
