"""Agent debug capture router (D17) — read-only history of agent calls.

Complements :mod:`fabulist.api.inspector_api` (the per-turn result view) by
exposing the captured *inputs* and raw reasoning/content of every
``agent_call`` system event written by :class:`fabulist.agents.debug.DebugCapture`.

Endpoints
---------
  GET /api/sessions/{session_id}/agent-calls
      -> ``{"calls": [wire dict, ...]}`` (404 unknown session; ``?agent=`` filter)
  GET /api/sessions/{session_id}/agent-calls/{seq}
      -> one wire dict | 404 when no capture has that seq

Append-only, no DELETE
----------------------
The event log owns everything (D9) and is append-only, so there is deliberately
no ``DELETE /agent-calls`` (nor any "soft clear"): a capture is a fact, and
deletion would be a revision — not a debug concern. Read-only is the whole
surface. ``GET /api/usage`` already lives in ``inspector_api`` and is not
duplicated here.

Wiring (orchestrator-owned; one line in ``app.py``)::

    from fabulist.api.agent_debug_api import router as agent_debug_router
    app.include_router(agent_debug_router)

Rows are the stored wire dicts augmented with the capture's log coordinates
(``seq``, ``ts``) so the single-call lookup is possible; every other key is the
payload exactly as captured. Session existence is resolved with
:func:`fabulist.api.attachments._session_exists` (the same seam the inspector
uses), so a freshly forked branch with no own rows still counts as existing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request

from fabulist.agents.debug import AGENT_CALL_EVENT
from fabulist.api.attachments import _session_exists

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["agent-debug"])


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _wire_rows(log: "EventLog", session_id: str, agent: str | None) -> list[dict]:
    """Every ``agent_call`` row as a wire dict + ``seq``/``ts``, seq-ordered.

    Ephemeral rows are included: the debug view is the raw audit history, not
    the conversation surface.
    """
    rows: list[dict] = []
    for event in log.get_events(session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != AGENT_CALL_EVENT:
            continue
        if agent is not None and str(event.payload.get("agent") or "") != agent:
            continue
        rows.append({**event.payload, "seq": event.seq, "ts": event.ts})
    return rows


@router.get("/api/sessions/{session_id}/agent-calls")
async def list_agent_calls(
    session_id: str,
    request: Request,
    agent: str | None = Query(None, description="only captures from this agent"),
) -> dict:
    """List captured agent calls, newest last, optionally filtered by agent."""
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return {"calls": _wire_rows(log, session_id, agent)}


@router.get("/api/sessions/{session_id}/agent-calls/{seq}")
async def get_agent_call(session_id: str, seq: int, request: Request) -> dict:
    """Return one captured call by its event ``seq``, or 404."""
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    for row in _wire_rows(log, session_id, None):
        if row["seq"] == seq:
            return row
    raise HTTPException(status_code=404, detail="unknown agent call")
