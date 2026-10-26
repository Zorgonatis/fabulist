"""World registry router (D32: worlds as log-owned entities).

Thin transport over :mod:`fabulist.worlds.registry`. The log owns everything
(D9): world definitions are ``system`` events on the reserved ``__worlds__``
anchor session — this router never writes a side table.

Endpoints
---------
  GET    /api/worlds                    -> {"worlds": [wire, ...]}
  POST   /api/worlds {name, cast, ...}  -> {"world": wire}
  POST   /api/worlds/import             card PNG bytes or card JSON (V2
                                       envelope / V1 flat) -> a World
                                       -> {"ok": true, "world": wire,
                                          "book_id": str | null} | 422
  GET    /api/worlds/{world_id}         -> {"world": wire} | 404
  PUT    /api/worlds/{world_id}         -> {"world": wire} | 404
  POST   /api/worlds/{world_id}/image   raw image bytes or a data URL
                                       -> store anchor asset + latest-wins
                                          world append -> {"world": wire,
                                          "image_asset", "mime"} | 404
  GET    /api/worlds/{world_id}/image   -> image bytes (stored mime) | 404
  DELETE /api/worlds/{world_id}         -> tombstone -> {"world_id", "deleted"} | 404
  POST   /api/worlds/{world_id}/chats   -> materialize a chat
                                        -> {"session_id", "title", "world_id"} | 404

``POST /api/worlds/import`` is the typed card door (issue #40, owner ruling
D3): ANY valid card imports as a World — the card's own prose is world content,
so a ``character_book`` is a bonus, never a requirement, and the card's
``first_mes`` + ``alternate_greetings`` become the WORLD's opening (issue #51:
a chat started from it opens on the first message with swipes, exactly like the
persona path — ``scenario`` stays context). The card's ``character_book`` is
materialized into a reusable Book when present
(:func:`fabulist.context.books.materialize_card_book`, signature-deduped) and a
raw-PNG body is retained once as the world's definition image
(:func:`fabulist.media.images.store_definition_image`, content-addressed). No
persona is written (``cast`` stays ``[]``) — the Persona studio owns persona
capture. Only a non-card body is a ``422``.

``POST .../chats`` is the D32 materialization: a new session is created, the
world's cast is seeded as card-shaped events (so the existing roster render
produces ``### Cast``), the ``world_ref`` snapshot and the world's
``session_books`` are appended. The chat is then an ordinary chat — the engine,
roster and facts paths are untouched below seeding.

Wiring (orchestrator-owned; the ``app.state`` seam needs no ``app.py``
signature change)::

    from fabulist.api.worlds_api import worlds_router
    app.include_router(worlds_router)

Like :mod:`fabulist.api.personas_api`, the router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log`` (the app factory
stores it there). The chat-creation route also mirrors ``cards_api._seed_session``
by registering the new session in ``request.app.state.sessions`` so the
projection/turn endpoints resolve it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from fabulist.api.personas_api import (
    definition_image_response,
    read_definition_image,
)
from fabulist.cards import PNG_SIGNATURE, CardError, parse_card_v2
from fabulist.context.books import book_to_wire, list_books, materialize_card_book
from fabulist.context.facts import _entity_book_ids
from fabulist.media.images import find_definition_asset, store_definition_image
from fabulist.worlds.registry import (
    WORLD_REF_EVENT,
    World,
    append_world,
    create_world_chat,
    delete_world,
    list_worlds,
    set_world_image,
    wire_to_world,
    world_by_id,
    world_to_wire,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.cards import CardV2
    from fabulist.log import EventLog


worlds_router = APIRouter(tags=["worlds"])

#: Conventional alias (some mounting code imports ``router``).
router = worlds_router


class WorldBody(BaseModel):
    """Body of ``POST/PUT /api/worlds`` — a world definition.

    ``name`` is required; ``description``/``scenario`` default to ``""`` and
    ``cast``/``book_ids`` to ``[]``. An explicit ``world_id`` updates in place
    (latest-wins); on ``POST`` it is omitted and the registry generates one.

    ``first_mes`` / ``alternate_greetings`` are the world's OPENING (issue #51)
    — the card fields under the persona names. Both default to ``None``, which
    means "not supplied": a create stores the empty greeting (``""`` / ``[]``,
    the same degradation as an empty persona ``first_mes``) and an update KEEPS
    the stored opening, so a client that predates these fields (the studio's
    world form) can never erase a card's greeting by saving.
    """

    name: str
    world_id: str | None = None
    description: str = ""
    scenario: str = ""
    first_mes: str | None = None
    alternate_greetings: list[str] | None = None
    cast: list[str] = []
    book_ids: list[str] = []
    provenance: dict = {}
    image_asset: str | None = None


class WorldChatRequest(BaseModel):
    """Body of ``POST /api/worlds/{world_id}/chats``.

    Optional ``title`` override and optional ``persona_id`` (the chat's user
    persona). When ``persona_id`` is omitted the pinned global default applies
    (and, absent both, the chat is a true sandbox).
    """

    title: str | None = None
    persona_id: str | None = None


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _persist(log: "EventLog", body: WorldBody) -> dict:
    """Append a validated world definition and echo its folded wire form.

    ``422`` when ``name`` is empty/whitespace or the id-lists are malformed; an
    existing ``world_id`` updates in place (append-only log, latest-wins).

    An OMITTED opening (``first_mes`` / ``alternate_greetings`` of ``None``)
    inherits the stored values on an update — the world form in the studio does
    not carry these fields yet, and saving a world must never silently drop the
    greeting an import captured (issue #51).
    """
    data = body.model_dump()
    world_id = data.get("world_id")
    existing = world_by_id(log, world_id) if isinstance(world_id, str) and world_id else None
    if existing is not None:
        if data.get("first_mes") is None:
            data["first_mes"] = existing.first_mes
        if data.get("alternate_greetings") is None:
            data["alternate_greetings"] = list(existing.alternate_greetings)
    try:
        world: World = wire_to_world(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        world_id = append_world(log, world_to_wire(world))
    except ValueError as exc:  # pragma: no cover - wire_to_world already guards
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    folded = world_by_id(log, world_id)
    if folded is None:  # pragma: no cover - the append just landed
        raise HTTPException(status_code=500, detail="world append did not persist")
    return world_to_wire(folded)


@worlds_router.get("/api/worlds")
async def get_worlds(request: Request) -> dict:
    """List all worlds folded from the ``__worlds__`` anchor (latest wins)."""
    return {"worlds": [world_to_wire(w) for w in list_worlds(_log_for(request))]}


@worlds_router.post("/api/worlds")
async def create_world(body: WorldBody, request: Request) -> dict:
    """Append a world definition (create or update) and echo it back."""
    return {"world": _persist(_log_for(request), body)}


# -- typed card import (issue #40 / D3) ---------------------------------------


def _card_error(message: str, status_code: int = 422) -> JSONResponse:
    """Failure shape for the card door: ``{"ok": false, "error", "detail"}``.

    Mirrors :func:`fabulist.api.cards_api._card_error` so the frontend's generic
    error reader surfaces the same precise message. Every failure is a ``422`` —
    the only way this door refuses is a body that is not a card.
    """
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": message, "detail": message},
    )


def _card_source(raw: bytes) -> bytes | dict:
    """Turn a request body into a :func:`parse_card_v2` source.

    The PNG signature is sniffed rather than trusting ``Content-Type`` (the
    established raw-PNG precedent, ``profiles_api.py`` import). Anything else
    must be a UTF-8 JSON object: a V2/V3 envelope, a V1/flat card, or a
    ``{"card": {...}}`` wrapper. Raises :class:`CardError` with a precise
    message; card-field validation itself belongs to the parser.
    """
    if not raw or not raw.strip():
        raise CardError("no card data in request body")
    if raw.startswith(PNG_SIGNATURE):
        return bytes(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CardError(
            "request body is neither a PNG (missing signature) nor UTF-8 JSON"
        ) from None
    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CardError(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise CardError("JSON body must be a card object")
    nested = obj.get("card")
    if isinstance(nested, dict):
        return nested
    return obj


def _import_card_as_world(
    log: "EventLog", card: "CardV2", card_png: bytes | None
) -> tuple[dict, str | None]:
    """Write ``card`` as a World; return ``(world wire, book id | None)``.

    One write path (D3):

    * ``name`` / ``description`` / ``scenario`` come from the card verbatim —
      the card's own prose IS world content (a card written in the context of
      a world is a more suitable container as-is).
    * ``first_mes`` / ``alternate_greetings`` carry over as the world's OPENING
      (issue #51, owner ruling): a chat started from this world opens on the
      card's first message with the alternates as swipes, exactly like a
      persona chat. ``scenario`` is NOT the opening — it stays context and
      renders in the ``### World`` block.
    * ``character_book`` -> a reusable Book when present and usable
      (signature-deduped: a re-import finds the existing book), else ``[]``.
      A bookless card is NOT an error.
    * a raw-PNG body is retained ONCE as the world's definition image
      (content-addressed, so a re-import adds no second asset).
    * ``cast`` stays ``[]``: this door writes no persona — persona capture is
      the Persona studio's job (``POST /api/cards/import?persona=1``).

    Card fields with no World-schema home (``personality``, ``mes_example``,
    ...) do not carry over.
    """
    card_dict = card.to_dict()
    book_id = materialize_card_book(log, card_dict)
    provenance: dict = {"origin": "card_import", "card_name": card.name}
    if card.creator:
        provenance["creator"] = card.creator
    if card.character_version:
        provenance["character_version"] = card.character_version
    image_asset: str | None = None
    if card_png is not None:
        image_asset = store_definition_image(
            log, card_png, "image/png", note=f"world image: card {card.name}"
        )
    world_id = append_world(
        log,
        {
            "name": card.name,
            "description": card.description,
            "scenario": card.scenario or "",
            "first_mes": card.first_mes or "",
            "alternate_greetings": list(card.alternate_greetings),
            "cast": [],
            "book_ids": [book_id] if book_id else [],
            "provenance": provenance,
            "image_asset": image_asset,
        },
    )
    world = world_by_id(log, world_id)
    if world is None:  # pragma: no cover - the append just landed
        raise HTTPException(status_code=500, detail="world append did not persist")
    return world_to_wire(world), book_id


@worlds_router.post("/api/worlds/import")
async def import_world_card(request: Request):
    """Import a character card AS a World (issue #40, owner ruling D3).

    Body: raw PNG card bytes (signature-sniffed, ``Content-Type`` ignored) or
    card JSON (V2/V3 envelope, V1 flat, or a ``{"card": {...}}`` wrapper).
    ANY valid card becomes a World — a card without a ``character_book`` is
    perfectly importable (``book_id`` is then ``None``); the card's own prose
    is the world content. The one and only refusal is a body that is not a
    card, always a precise ``422`` (``{"ok": false, "error": ...}``).

    Returns ``{"ok": true, "world": <wire>, "book_id": <str | null>}``. The
    world wire is the same contract ``GET /api/worlds`` serves. Re-importing the
    same card creates a second world (the JSON door behaves the same), but the
    Book dedupes by signature and the PNG asset dedupes by content address.

    Deliberately unannotated: the success body is a plain dict while failures
    are a :class:`JSONResponse`, and an annotation would become a response model.
    """
    log = _log_for(request)
    raw = await request.body()
    try:
        source = _card_source(raw)
        card: "CardV2" = parse_card_v2(source)
    except CardError as exc:
        return _card_error(str(exc))
    card_png = source if isinstance(source, bytes) else None
    world, book_id = _import_card_as_world(log, card, card_png)
    return {"ok": True, "world": world, "book_id": book_id}


@worlds_router.get("/api/worlds/{world_id}")
async def get_world(world_id: str, request: Request) -> dict:
    """Return one world by id, or ``404`` when unknown."""
    world = world_by_id(_log_for(request), world_id)
    if world is None:
        raise HTTPException(status_code=404, detail="unknown world")
    return {"world": world_to_wire(world)}


@worlds_router.put("/api/worlds/{world_id}")
async def update_world(world_id: str, body: WorldBody, request: Request) -> dict:
    """Supersede a world definition (latest-wins append); ``404`` when unknown."""
    log = _log_for(request)
    if world_by_id(log, world_id) is None:
        raise HTTPException(status_code=404, detail="unknown world")
    body.world_id = world_id
    return {"world": _persist(log, body)}


@worlds_router.post("/api/worlds/{world_id}/image")
async def set_world_image_route(world_id: str, request: Request) -> dict:
    """Attach an image to a world (sessionless anchor asset; latest-wins).

    Body is raw image bytes (``Content-Type: image/*``) or a base64 data URL.
    Stores the bytes as a content-addressed asset on the reserved anchor and
    re-appends the world with ``image_asset`` set (append-only, D9). ``404``
    for an unknown world; ``415``/``422``/``413`` for a bad body. Cast members
    are personas, so their images are set through
    ``POST /api/personas/{id}/image`` — a world's cast is a list of persona ids
    and carries no separate image store of its own.
    """
    log = _log_for(request)
    if world_by_id(log, world_id) is None:
        raise HTTPException(status_code=404, detail="unknown world")
    data, mime = await read_definition_image(request)
    asset_id = store_definition_image(
        log, data, mime, note=f"world image: {world_id}"
    )
    world = set_world_image(log, world_id, asset_id)
    if world is None:  # pragma: no cover - the existence check just passed
        raise HTTPException(status_code=404, detail="unknown world")
    return {
        "world": world_to_wire(world),
        "image_asset": asset_id,
        "mime": mime,
    }


@worlds_router.get("/api/worlds/{world_id}/image")
async def get_world_image_route(world_id: str, request: Request) -> Response:
    """Serve a world's stored image bytes; ``404`` for unknown / no image."""
    log = _log_for(request)
    world = world_by_id(log, world_id)
    if world is None:
        raise HTTPException(status_code=404, detail="unknown world")
    if not world.image_asset:
        raise HTTPException(status_code=404, detail="world has no image")
    item = find_definition_asset(log, world.image_asset)
    if item is None:
        raise HTTPException(status_code=404, detail="image asset not found")
    return definition_image_response(item)


@worlds_router.delete("/api/worlds/{world_id}")
async def remove_world(world_id: str, request: Request) -> dict:
    """Tombstone a world; ``404`` when unknown. Rows survive (D9)."""
    log = _log_for(request)
    if world_by_id(log, world_id) is None:
        raise HTTPException(status_code=404, detail="unknown world")
    delete_world(log, world_id)
    return {"world_id": world_id, "deleted": True}


@worlds_router.post("/api/worlds/{world_id}/chats")
async def create_world_chat_route(
    world_id: str, body: WorldChatRequest, request: Request
) -> dict:
    """Materialize a world into a new chat (D32); ``404`` when unknown.

    The new session is registered in the app's in-memory session registry
    (mirroring :func:`fabulist.api.cards_api._seed_session`) so the frontend can
    open it immediately via ``/chat?s=<session_id>``.
    """
    log = _log_for(request)
    try:
        result = create_world_chat(
            log, world_id, title=body.title, persona_id=body.persona_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ``_Session`` is imported lazily: the app factory owns the class and may
    # import this module while it is still initialising (circular import).
    from fabulist.api.app import _Session

    registry = getattr(request.app.state, "sessions", None)
    if isinstance(registry, dict):
        registry[result["session_id"]] = _Session(
            result["session_id"], result["title"]
        )
    return result


@worlds_router.get("/api/sessions/{session_id}/world")
async def session_world_route(session_id: str, request: Request) -> dict:
    """The session's world snapshot + its resolved lore books (D32 read half).

    Folds the chat session's events for the latest ``world_ref`` snapshot
    (appended by ``POST /api/worlds/{id}/chats``). The book set is the SAME set
    the narrator's world-info render uses (D31) because it delegates to
    :func:`fabulist.context.facts._entity_book_ids` — the render-side helper —
    rather than mirroring its fold. That helper pools all three sources in one
    order: the chat's assigned persona's ``book_ids`` (plus the card-name
    fallback), the latest ``session_books`` registration, and every
    ``world_ref`` world's books; it dedupes first-seen. Sharing the helper means
    the two readers cannot silently disagree (pinned by ``tests/test_books.py``).
    Books resolve through the entity fold so edits reflect their current state
    and tombstoned books drop; a session with no ``world_ref`` returns
    ``{"world": None, "books": []}`` — a 200, not a 404, because the UI renders
    conditionally.
    """

    log = _log_for(request)
    # Fork-aware read (issue #86): the world_ref snapshot, the chat's
    # ``session_books`` registration and the card seed are all session-constant
    # context that arrives through the chain on a branch. Own rows alone made a
    # branch answer "no world, no books" while the narrator rendered both.
    events = log.get_chain_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="unknown session")

    world_ref: dict | None = None
    for event in events:
        if event.type != "system":
            continue
        payload = event.payload
        if payload.get("event") == WORLD_REF_EVENT:
            world_ref = {
                "world_id": str(payload.get("world_id") or ""),
                "name": str(payload.get("name") or ""),
                "scenario": str(payload.get("scenario") or ""),
                # The chat's opening snapshot (issue #51): the world's greeting
                # cycle at creation, so the drawer can render what the Opening
                # block pins without re-reading the definition.
                "first_mes": str(payload.get("first_mes") or ""),
                "alternate_greetings": [
                    str(greeting)
                    for greeting in payload.get("alternate_greetings") or []
                    if isinstance(greeting, str)
                ],
            }

    books_by_id = {book.book_id: book for book in list_books(log)}
    books = [
        books_by_id[book_id]
        for book_id in _entity_book_ids(log, session_id, events)
        if book_id in books_by_id
    ]
    return {
        "world": world_ref,
        "books": [book_to_wire(book) for book in books],
    }
