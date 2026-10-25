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
      activation_map, graphs[]), with ``bound_agent`` populated per node.

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
    return manifest


@router.get("/api/sessions/{session_id}/lanes")
async def get_lanes_route(session_id: str, request: Request) -> dict[str, Any]:
    """The LAST turn's NPC lane set — the actual members fanned out.

    The flow panel draws one arm per member here (owner: "unless you can draw
    the actual members don't bother"). Read-only; the log is the source:
    the newest ``npc_lanes`` summary names the set, and the ``npc_lane`` rows
    between the previous summary and it carry each member's intent.

    ``max_parallel`` comes from the canonical topology's node data (the pool
    width), not from this session's traffic.
    """
    from fastapi import HTTPException

    from fabulist.flow.manifest import build_manifest

    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    events = get_chain_events_or_session(log, session_id)

    summaries = [
        event
        for event in events
        if event.type == "system"
        and isinstance(event.payload, dict)
        and event.payload.get("event") == "npc_lanes"
    ]
    if not summaries:
        return {"session_id": session_id, "lanes": [], "source": None,
                "max_parallel": _lane_width(), "turn_seq": None}
    latest = summaries[-1]
    previous_seq = summaries[-2].seq if len(summaries) > 1 else -1
    rows = [
        event.payload
        for event in events
        if previous_seq < event.seq < latest.seq
        and event.type == "system"
        and isinstance(event.payload, dict)
        and event.payload.get("event") == "npc_lane"
    ]
    # W9.16: the PLAYER is never a cast arm. A stale lane row from before the
    # engine excluded player surfaces ("User") must not be drawn as cast.
    from fabulist.state.presence import PLAYER_SURFACES
    from fabulist.state.cast import canonical_key

    lanes = [
        {
            "member_id": row.get("member_id"),
            "name": row.get("name"),
            "intent": row.get("intent"),
            "ok": bool(row.get("ok", True)),
        }
        for row in rows
        if canonical_key(row.get("name")) not in PLAYER_SURFACES
    ]
    return {
        "session_id": session_id,
        "lanes": lanes,
        "source": latest.payload.get("source"),
        "max_parallel": _lane_width(),
        "turn_seq": latest.seq,
    }


def _lane_width() -> int:
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
