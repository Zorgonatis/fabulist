"""Global search router (owner ruling, 2026-10-13 item 8).

Global search has NO concept of an active chat: it searches every visible
session and every definition (personas / worlds / books). The chat-scoped
semantic search lives in its own top-bar control instead
(:mod:`fabulist.api.search_api`); this router never reads that scope.

  GET /api/search/global?q=...
        -> {"sessions": [{"session_id", "title", "snippet", "score"}],
            "definitions": [{"kind", "id", "name"}]}

Two halves, merged for sessions and concatenated for definitions:

* **Semantic** — the query is embedded through the live embedder
  (:func:`fabulist.memory.reembed.resolve_embedder`) and the app memorizer's
  store (:attr:`app.state.memorizer.store`) is queried across ALL its
  collections with :meth:`MemoryStore.query_many`, which already merges by
  nearest distance. Rows are grouped to one best hit per session, filtered to
  visible (non-hidden) chats, and capped at :data:`_SEMANTIC_LIMIT`. Any error
  in this half degrades to an empty list — semantic search is an enhancement,
  never a dependency.
* **Literal** — case-insensitive substring over session titles and definition
  names (:func:`fabulist.personas.registry.list_personas` /
  :func:`fabulist.worlds.registry.list_worlds` /
  :func:`fabulist.context.books.list_books`).

Read-only (D9): it appends nothing. A blank ``q`` returns both empty lists; the
frontend's own 2-char minimum is a UI affordance, not a server contract.

Wiring (orchestrator-owned; the ``app.state`` seam needs no ``app.py``
signature change)::

    from fabulist.api.global_search_api import router as global_search_router
    app.include_router(global_search_router)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, Request

from fabulist.api.search_api import _collapse
from fabulist.api.sessions import _session_ids, _summarize
from fabulist.context.books import list_books
from fabulist.memory.embedder import embed_texts
from fabulist.memory.reembed import resolve_embedder
from fabulist.personas.registry import list_personas
from fabulist.worlds.registry import list_worlds

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

#: Merged semantic sessions are capped here ("top ~8").
_SEMANTIC_LIMIT = 8


def _visible_sessions(log: "EventLog") -> dict[str, str]:
    """Map ``session_id -> title`` for every visible (non-hidden) chat.

    Reuses the sessions router's fold so a global search sees exactly the chats
    the rail lists; hidden sessions and reserved ``__`` anchor ids are excluded.
    """
    visible: dict[str, str] = {}
    for session_id in _session_ids(log):
        summary = _summarize(log, session_id)
        if summary is None or summary["hidden"]:
            continue
        visible[session_id] = summary["title"] or ""
    return visible


async def _semantic_sessions(
    memorizer: Any, query: str, visible: dict[str, str]
) -> list[dict]:
    """Nearest indexed rows across ALL collections, one best hit per session.

    Never raises: a missing store/embedder, a down embedder, or a query error
    all degrade to no semantic results (literal matches still return).
    """
    store = getattr(memorizer, "store", None)
    registry = getattr(memorizer, "registry", None)
    if store is None or registry is None:
        return []
    try:
        embedder = resolve_embedder(registry)
        vectors = await embed_texts(
            embedder.endpoint, embedder.model, [query], api_key=embedder.api_key
        )
        collections = store.collections()
        if not vectors or not collections:
            return []
        rows = store.query_many(collections, [vectors[0]], k=_SEMANTIC_LIMIT)
    except Exception:  # noqa: BLE001 - semantic search is an enhancement
        logger.warning("global semantic search failed", exc_info=True)
        return []

    best: dict[str, dict] = {}
    for row in rows or []:
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or session_id not in visible:
            continue
        current = best.get(session_id)
        if current is None or _distance(row) < _distance(current):
            best[session_id] = row

    ordered = sorted(best.values(), key=_distance)[:_SEMANTIC_LIMIT]
    return [
        {
            "session_id": row["session_id"],
            "title": visible[row["session_id"]],
            "snippet": _collapse(row.get("text")),
            "score": _distance(row),
        }
        for row in ordered
    ]


def _distance(row: dict) -> float:
    """Nearest-neighbour distance for a store row (missing/unparseable sorts last)."""
    try:
        return float(row.get("distance"))
    except (TypeError, ValueError):
        return float("inf")


def _literal_sessions(visible: dict[str, str], query: str) -> list[dict]:
    """Case-insensitive substring match over visible session titles."""
    needle = query.casefold()
    hits: list[dict] = []
    for session_id, title in visible.items():
        if needle in title.casefold():
            hits.append(
                {
                    "session_id": session_id,
                    "title": title,
                    "snippet": title,
                    "score": None,
                }
            )
    return hits


def _literal_definitions(log: "EventLog", query: str) -> list[dict]:
    """Case-insensitive substring match over persona / world / book names."""
    needle = query.casefold()
    definitions: list[dict] = []
    for persona in list_personas(log):
        if needle in persona.name.casefold():
            definitions.append(
                {"kind": "persona", "id": persona.persona_id, "name": persona.name}
            )
    for world in list_worlds(log):
        if needle in world.name.casefold():
            definitions.append({"kind": "world", "id": world.world_id, "name": world.name})
    for book in list_books(log):
        if needle in book.name.casefold():
            definitions.append({"kind": "book", "id": book.book_id, "name": book.name})
    return definitions


@router.get("/api/search/global")
async def global_search(request: Request, q: str = Query(default="")) -> dict:
    """Search every visible session and every definition for ``q``.

    Semantic session hits (nearest-first) come first, then literal title-only
    matches. Definitions are literal name matches, grouped persona -> world ->
    book. Read-only: the log is never written.
    """
    log = getattr(request.app.state, "log", None)
    query = q.strip()
    if log is None or not query:
        return {"sessions": [], "definitions": []}

    visible = _visible_sessions(log)
    semantic = await _semantic_sessions(
        getattr(request.app.state, "memorizer", None), query, visible
    )
    merged = list(semantic)
    seen = {row["session_id"] for row in semantic}
    for row in _literal_sessions(visible, query):
        if row["session_id"] not in seen:
            merged.append(row)
    return {"sessions": merged, "definitions": _literal_definitions(log, query)}
