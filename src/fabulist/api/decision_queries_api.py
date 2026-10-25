"""Decision-query bank router (R25): the d1-3b gate's questions as an asset.

Thin transport over :mod:`fabulist.decision_queries`. The log owns the bank
(D9): it is one ``system`` event on the reserved ``__decision_queries__`` anchor
session — this router never writes a side table, and no chat/session is
involved (non-negotiable 7: asset CRUD works with no chat open).

Endpoints
---------
  GET /api/decision-queries  -> {"questions": [question, ...], "seeded": bool}
  PUT /api/decision-queries  {"questions": [question, ...]}
                             -> the same envelope | 400 {"detail": "<message>"}

``seeded: true`` means the stored asset is empty, so the built-in seeded bank
(:data:`fabulist.decision_queries.DEFAULT_QUESTIONS` — its slop rows derive from
the canonical anti-slop list in the factory artifact, issue #50, plus narrative
movement and W9.22's two ``section: "proposal"`` rows the proposal gates
consume; the per-turn gate reads only its own section) is what the engine asks.
Any stored bank is served and reported ``seeded: false``; the engine reads the
same asset (:func:`fabulist.decision.turn_gate_questions`), which falls back to
the seeded bank whenever the asset is empty, so GET is always the effective set.

Wiring (the ``app.state`` seam needs no signature change)::

    from fabulist.api.decision_queries_api import decision_queries_router
    app.include_router(decision_queries_router)

Like :mod:`fabulist.api.worlds_api`, the router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from fabulist.decision_queries import (
    effective_questions,
    list_questions,
    replace_questions,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


decision_queries_router = APIRouter(tags=["decision-queries"])

#: Conventional alias (some mounting code imports ``router``).
router = decision_queries_router


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _envelope(log: "EventLog") -> dict:
    """The effective bank plus whether it is the seeded (code) bank."""
    stored = list_questions(log)
    return {
        "questions": stored if stored else effective_questions(log),
        "seeded": not stored,
    }


@decision_queries_router.get("/api/decision-queries")
async def get_decision_queries(request: Request) -> dict:
    """The effective question bank + ``seeded`` (True = the built-in default)."""
    return _envelope(_log_for(request))


@decision_queries_router.put("/api/decision-queries")
async def put_decision_queries(request: Request, body: dict) -> dict:
    """Replace the whole bank; ``400`` when any question fails validation.

    The body is ``{"questions": [...]}``; an empty list is a legal write (the
    engine then falls back to the seeded bank, exactly as before the asset
    existed). Validation rules live in
    :func:`fabulist.decision_queries.validate_questions`.
    """
    log = _log_for(request)
    questions = body.get("questions")
    if not isinstance(questions, list):
        raise HTTPException(
            status_code=400, detail="'questions' must be a list"
        )
    try:
        replace_questions(log, questions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _envelope(log)
