"""World-info router: book catalog (D31) + legacy session-scoped imports.

The log owns the book (D9). There are two surfaces here:

**Book catalog (D31)** — reusable, first-class lore entities folded from the
reserved ``__books__`` anchor session (see :mod:`fabulist.context.books`):

  GET    /api/books                  -> {"books": [wire, ...]}
  POST   /api/books {name, entries}  -> {"book": wire}
  GET    /api/books/{book_id}        -> {"book": wire} | 404
  PUT    /api/books/{book_id}        -> {"book": wire} | 404
  DELETE /api/books/{book_id}        -> tombstone -> {"book_id", "deleted"} | 404

**Legacy session-scoped imports (W2.10 WI v1)** — retained verbatim so existing
chats keep working and re-imports stay possible. These attach raw ST world-info
JSON (or a card ``character_book``) to a single session; the narrator's message
builder activates entries per turn (keywords + budget + recursion-lite). Marked
legacy: new lore should live in a Book entity so it can travel on a persona or
world (D31).

  POST   /api/sessions/{session_id}/worldinfo   (legacy)
  GET    /api/sessions/{session_id}/worldinfo   (legacy)

**Attach a catalog book to an existing chat (D40 gap)** — replaces the dissolved
chat-rail lore action by letting a reusable Book (D31) be registered on the
session the user is sitting in; the reader folds it unchanged.

  POST   /api/sessions/{session_id}/books {book_ids: [...]}

The raw import is kept verbatim so re-imports and hand edits stay possible (D4).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fabulist.api.attachments import _session_exists
from fabulist.context.books import (
    append_book,
    book_by_id,
    book_to_wire,
    delete_book,
    list_books,
    wire_to_book,
)
from fabulist.context.world_info import WorldInfoError, parse_book
from fabulist.log import EventLog
from fabulist.worlds.registry import set_session_books

worldinfo_router = APIRouter(tags=["world-info"])

#: Conventional alias (the app factory mounts ``worldinfo_api.router``).
router = worldinfo_router


class WorldInfoImport(BaseModel):
    """Legacy session-scoped import body (raw ST book / card ``character_book``)."""

    book: Any


class SessionBooksBody(BaseModel):
    """Body of ``POST /api/sessions/{session_id}/books`` — the chat's book set.

    ``book_ids`` is required (no default): an explicit ``[]`` clears the
    attachment, while omitting the key is a malformed request — the project's
    absent != empty discipline (D33). Unknown ids are rejected, not stored.
    """

    book_ids: list[str]


class BookBody(BaseModel):
    """Body of ``POST/PUT /api/books`` — a reusable book definition.

    ``name`` is required; ``entries`` uses the SillyTavern character-book entry
    shape :func:`fabulist.context.world_info.parse_book` consumes. An explicit
    ``book_id`` updates in place (latest-wins).
    """

    name: str
    book_id: str | None = None
    entries: list = []
    provenance: dict = {}


def _log_for(request: Request) -> EventLog:
    return request.app.state.log


def _validated_entries(entries: Any) -> None:
    """Raise ``422`` when ``entries`` is not a valid ST entry list.

    Uses the same parser the render path uses, so a book that persists is
    guaranteed to activate later; surfaces the precise
    :class:`WorldInfoError` text as the 422 detail.
    """
    try:
        parse_book(entries)
    except WorldInfoError as exc:
        raise HTTPException(status_code=422, detail=f"invalid world info: {exc}") from exc


def _persist_book(log: EventLog, body: BookBody) -> dict:
    """Validate + append a book definition and echo the folded wire form.

    ``422`` when ``name`` is empty/whitespace or ``entries`` is structurally
    invalid (validated against the RAW entries, so a non-object is rejected
    rather than silently dropped); an existing ``book_id`` updates in place.
    """
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="book 'name' must be non-empty")
    _validated_entries(body.entries)
    try:
        wire = book_to_wire(wire_to_book(body.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    book_id = append_book(log, wire)
    book = book_by_id(log, book_id)
    if book is None:  # pragma: no cover - the append just landed
        raise HTTPException(status_code=500, detail="book append did not persist")
    return book_to_wire(book)


# -- book catalog (D31) ------------------------------------------------------


@worldinfo_router.get("/api/books")
async def get_books(request: Request) -> dict:
    """List all books folded from the ``__books__`` anchor (latest wins)."""
    return {"books": [book_to_wire(book) for book in list_books(_log_for(request))]}


@worldinfo_router.post("/api/books")
async def create_book(body: BookBody, request: Request) -> dict:
    """Append a book definition (create or update) and echo it back.

    ``422`` when ``name`` is empty/whitespace or ``entries`` is structurally
    invalid; an existing ``book_id`` updates in place (latest-wins, D9/D10).
    """
    return {"book": _persist_book(_log_for(request), body)}


@worldinfo_router.get("/api/books/{book_id}")
async def get_book(book_id: str, request: Request) -> dict:
    """Return one book by id, or ``404`` when unknown/tombstoned."""
    book = book_by_id(_log_for(request), book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="unknown book")
    return {"book": book_to_wire(book)}


@worldinfo_router.put("/api/books/{book_id}")
async def update_book(book_id: str, body: BookBody, request: Request) -> dict:
    """Supersede a book definition (latest-wins append); ``404`` when unknown."""
    log = _log_for(request)
    if book_by_id(log, book_id) is None:
        raise HTTPException(status_code=404, detail="unknown book")
    body.book_id = book_id
    return {"book": _persist_book(log, body)}


@worldinfo_router.delete("/api/books/{book_id}")
async def remove_book(book_id: str, request: Request) -> dict:
    """Tombstone a book; ``404`` when unknown. Rows survive (D9)."""
    log = _log_for(request)
    if book_by_id(log, book_id) is None:
        raise HTTPException(status_code=404, detail="unknown book")
    delete_book(log, book_id)
    return {"book_id": book_id, "deleted": True}


# -- attach a catalog book to an existing chat (D40 gap) ----------------------


@worldinfo_router.post("/api/sessions/{session_id}/books")
async def attach_session_books(
    session_id: str, body: SessionBooksBody, request: Request
) -> dict:
    """Register a chat's book set from the catalog (append-only, latest-wins).

    Appends one ``session_books`` system event (the same payload
    :func:`fabulist.worlds.registry.create_world_chat` writes), so the read
    half needs no change: ``GET /api/sessions/{id}/world``
    (:func:`fabulist.api.worlds_api.session_world_route`) takes the latest
    registration and :func:`fabulist.context.facts._entity_books` pools it into
    the WI render. An explicit empty list clears the attachment.

    ``404`` for an unknown session; ``422`` when a ``book_id`` names no known
    book (the catalog is read first so a dangling id is never stored).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    known = {book.book_id for book in list_books(log)}
    unknown = [book_id for book_id in body.book_ids if book_id not in known]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"unknown book id(s): {unknown}"
        )
    ids = set_session_books(log, session_id, body.book_ids)
    return {"session_id": session_id, "book_ids": ids}


# -- legacy session-scoped imports (W2.10 WI v1) -----------------------------


@worldinfo_router.post("/api/sessions/{session_id}/worldinfo")
async def import_world_info(
    session_id: str, body: WorldInfoImport, request: Request
) -> dict:
    """LEGACY: validate + append one session-scoped ``world_info`` event.

    Kept for existing chats / re-imports; new lore should be a Book entity
    (``POST /api/books``) attached to a persona or world (D31).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        entries = parse_book(body.book)
    except WorldInfoError as exc:
        raise HTTPException(422, detail=f"invalid world info: {exc}") from exc
    log.append_event(
        session_id,
        "system",
        "user",
        {"event": "world_info", "book": body.book, "entry_count": len(entries)},
    )
    return {"session_id": session_id, "entries": len(entries)}


@worldinfo_router.get("/api/sessions/{session_id}/worldinfo")
async def list_world_info(session_id: str, request: Request) -> dict:
    """LEGACY: summarize a session's imported books (counts; content stays in the log)."""
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    imports = [
        {
            "seq": e.seq,
            "entry_count": e.payload.get("entry_count", 0),
        }
        for e in log.get_events(session_id)
        if e.type == "system" and e.payload.get("event") == "world_info"
    ]
    return {"session_id": session_id, "imports": imports}
