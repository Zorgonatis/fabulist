"""Session semantic search API (SURFACE #28).

The event log owns the turns (D9); the vector index is a rebuildable cache
(D11). This router exposes a read-only semantic search over a session's
indexed turns by delegating to ``app.state.memorizer.search`` — the same seam
W3.1 recall uses. It never writes and never mutates history.

  POST /api/sessions/{id}/search
        body ``{"query": str, "k": int = 8}``
        -> ``{"session_id", "results": [{"seq","kind","text","distance"}...]}``

Memorizer seam
--------------
``app.state.memorizer`` is set by :func:`fabulist.wiring.build_app`. It may be
``None`` when the registry has no ``embedder`` entry (memory off). The object
exposes ``async search(session_id, query, k=8) -> list[{"seq","kind","text",
"distance"}]``. When absent or lacking ``search`` the endpoint returns ``503``
— there is no fallback search path, so a request cannot succeed without it.

Check order: unknown session -> ``404``; unwired memorizer -> ``503``; blank
query -> ``422``. ``k`` is clamped (not rejected) to ``1..25``; result text is
whitespace-collapsed and clipped to 400 chars; rows are sorted by ``distance``
ascending (nearest first).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fabulist.api.attachments import _session_exists

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["search"])

#: Result text is collapsed then clipped to this many characters.
_TEXT_LIMIT = 400
#: Requested ``k`` is clamped into this inclusive range.
_K_MIN = 1
_K_MAX = 25
#: Default neighbour count when the body omits ``k``.
_K_DEFAULT = 8


class SearchRequest(BaseModel):
    """Body for ``POST /api/sessions/{id}/search``."""

    query: str
    k: int = _K_DEFAULT


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _collapse(text: Any) -> str:
    """Collapse internal whitespace and clip to :data:`_TEXT_LIMIT` chars."""
    collapsed = " ".join(str(text if text is not None else "").split())
    return collapsed[:_TEXT_LIMIT]


def _distance_key(row: dict) -> float:
    """Sort key for a result row; missing/unparseable distance sorts last."""
    try:
        return float(row.get("distance"))
    except (TypeError, ValueError):
        return float("inf")


def _result_view(row: dict, ids_by_seq: dict | None = None) -> dict:
    """Normalize one memorizer row to the wire shape.

    ``event_id`` joins the row's per-session seq back to the log row id so
    the chat UI can jump to the transcript entry (hydrated entries carry
    their log id).
    """
    return {
        "seq": row.get("seq"),
        "event_id": (ids_by_seq or {}).get(row.get("seq")),
        "kind": row.get("kind"),
        "text": _collapse(row.get("text")),
        "distance": row.get("distance"),
    }


@router.post("/api/sessions/{session_id}/search")
async def search_session(
    session_id: str, body: SearchRequest, request: Request
) -> dict:
    """Search the session's vector index for the nearest indexed turns.

    Read-only: the index is derived from the event log and rebuildable (D11),
    so this endpoint never appends. Returns results nearest-first.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    memorizer = getattr(request.app.state, "memorizer", None)
    if memorizer is None or not hasattr(memorizer, "search"):
        raise HTTPException(status_code=503, detail="memory unavailable")

    if not body.query.strip():
        raise HTTPException(status_code=422, detail="empty query")

    k = max(_K_MIN, min(_K_MAX, body.k))
    rows = await memorizer.search(session_id, body.query, k=k)
    ids_by_seq = {e.seq: e.id for e in log.get_events(session_id)}
    results = sorted(
        (_result_view(row, ids_by_seq) for row in rows or []), key=_distance_key
    )
    return {"session_id": session_id, "results": results}
