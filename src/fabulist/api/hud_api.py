"""Env HUD read router (Lane 1f — Diagram Ratification Wave).

The narrator-side env HUD is an engine-assembled OpenUI DSL statement persisted
as a ``system``/``env_hud`` event (owner fork decision: statement, not typed
JSON). This router exposes the latest statement so a reload or a fork shows the
same station the turn produced::

    GET /api/sessions/{id}/hud  ->  {"statement": "<EnvTracker statement>"}

Read-only and log-derived (D9): :func:`latest_env_hud` walks the fork-resolved
event chain latest-wins. Sessions whose log predates the HUD event carry no
events but still have a folded ``time``/``loc``/``weather`` — those degrade to a
reduced station via :func:`env_hud_statement_from_fold`, so the endpoint always
returns a renderable statement for a known session.

Wiring (orchestrator-owned, one line)::

    from fabulist.api.hud_api import router
    app.include_router(router)

Like :mod:`fabulist.api.state_api`, the router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log`` (the app factory
stores it there), so no ``app.py`` signature change is needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from fabulist.api.attachments import _session_exists
from fabulist.state.env_hud import env_hud_statement_from_fold, latest_env_hud

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["hud"])


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


@router.get("/api/sessions/{session_id}/hud")
async def get_session_hud(session_id: str, request: Request) -> dict:
    """Return ``{"statement": <latest or legacy-fold-degraded EnvTracker>}``.

    ``404`` for an unknown session. A known session with no ``env_hud`` event
    still gets a statement degraded from its folded state (empty slots render as
    ``""``), never a ``null`` the frontend has to special-case.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    statement = latest_env_hud(log, session_id)
    if statement is None:
        fold = log.get_projection(session_id).get("state") or {}
        statement = env_hud_statement_from_fold(fold)
    return {"statement": statement}
