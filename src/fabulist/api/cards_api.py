"""Character-card import router (W2.10b).

The parser lives in :mod:`fabulist.cards` (PNG ``chara`` tEXt or raw JSON ->
:class:`~fabulist.cards.CardV2`). This router is only transport + the optional
"create a chat from this card" action:

  POST /api/cards/import
      body: raw PNG bytes (``image/png`` / ``application/octet-stream``),
            a JSON card object, or ``{"card": {...}, "seed": true}``
      ->   ``{"ok": true, "card": {name, description, first_mes, ...}}``
      ->   ``{"ok": false, "error": "<precise CardError>"}`` (HTTP 422)

  POST /api/cards/import?seed=1   (or ``"seed": true`` / ``"create_session": true``)
      Also creates a NEW session titled after the card and appends the
      :func:`~fabulist.cards.to_session_seed` system events to it, mirroring
      :mod:`fabulist.api.sessions` and the app factory's session creation.
      -> ``{"ok": true, "card": {...}, "session_id": "...", "title": "..."}``

  POST /api/cards/import?persona=1   (or ``"persona": true`` in a JSON body)
      Definition-only (D51): parses the card and captures it as a persona via
      :func:`fabulist.personas.registry.upsert_persona_from_card` (name-keyed
      upsert, latest-wins per id, D39). Creates NO session and appends no
      session events — the write is a ``system`` event on the ``__personas__``
      anchor, the same one ``POST /api/personas`` uses.
      -> ``{"ok": true, "card": {...}, "persona": {...}}``

  POST /api/cards/import?world=1   (or ``"world": true`` in a JSON body)
      Definition-only, as a WORLD (issue #60): parses the card and materializes
      it through the ONE world write path shared with ``POST /api/worlds/import``
      (:func:`fabulist.api.worlds_api._import_card_as_world`) — prose becomes the
      world, a ``character_book`` a bonus, a PNG its definition image. Creates NO
      session. That older endpoint stays supported; this flag is the entry point
      the UI's single import door uses for both card destinations.
      -> ``{"ok": true, "card": {...}, "world": {...}, "book_id": str|null}``

      At most ONE mode may be asked for: ``?seed=1``, ``?persona=1`` and
      ``?world=1`` are mutually exclusive, and asking for two or more is a
      precise ``422`` ("choose one mode") rather than an implicit precedence.
      No mode takes precedence in any direction.

The log owns everything (D9): seeding only appends rows. The frontend opens the
returned ``session_id`` through the URL param the session controller already
supports (``/chat?s=<id>``).

A PNG card import retains the artwork ONCE, as the card's persona ``image_asset``
(sessionless anchor store). The seed path's ``card_asset`` payload ref is that
same content-addressed id — it is a pointer into the definition store, never a
second copy on the session (D28 PNG re-export resolves the ref through
:func:`fabulist.media.images.find_definition_asset`). The anchor store is
content-addressed, so re-importing the same PNG adds no second asset.

A card's ``character_book`` is materialized into a reusable Book entity on both
the seed and persona paths (D31: :func:`fabulist.context.books.materialize_card_book`,
deduped by signature) and attached to the card's persona for reuse; a card
without a ``character_book`` materializes nothing.

Wiring (orchestrator-owned, one line)::

    from fabulist.api.cards_api import router as cards_router
    app.include_router(cards_router)

Like :mod:`fabulist.api.sessions`, the router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log`` and the session
registry from ``request.app.state.sessions`` (both stored by the app factory),
so no signature change is needed in ``app.py``.

Multipart uploads require the ``python-multipart`` package (a FastAPI extra).
It is not currently a project dependency, so the router degrades to a precise
415 rather than crashing; the frontend posts raw PNG/JSON bytes, which need no
extra dependency. Adding ``python-multipart`` to ``pyproject.toml`` (owned by
the orchestrator) enables ``multipart/form-data`` as well.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fabulist.api.personas_api import _wire as _persona_wire
from fabulist.cards import CardError, PNG_SIGNATURE, parse_card_v2, to_session_seed
from fabulist.cards.homes import (
    ANCHOR_POST_HISTORY,
    ANCHOR_TOP,
    seed_homes,
    session_modules_from_log,
)
from fabulist.context.books import attach_book_to_persona, materialize_card_book
from fabulist.context.roster import greeting_variants, selected_greeting
from fabulist.media.images import store_definition_image
from fabulist.personas.registry import (
    list_personas,
    set_persona_image,
    upsert_persona_from_card,
)
from fabulist.prompts.variables import display_variables, render_display_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.api.app import _Session
    from fabulist.cards import CardV2
    from fabulist.log import EventLog
    from fabulist.personas.registry import Persona


router = APIRouter(tags=["cards"])

#: Query/body flags that request the "also create a chat" action.
_SEED_KEYS = ("seed", "create_session")

#: Query/body flag requesting the definition-only (persona capture) action.
_PERSONA_KEYS = ("persona",)

#: Query/body flag requesting the definition-only (world capture) action.
_WORLD_KEYS = ("world", "as_world")
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: multipart field names probed, in order, for the card payload.
_MULTIPART_FILE_FIELDS = ("file", "card", "png", "upload")
_MULTIPART_TEXT_FIELDS = ("card", "json")


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _card_error(message: str, status_code: int = 422) -> JSONResponse:
    """Consistent failure shape: ``{"ok": false, "error": <precise message>}``.

    ``detail`` mirrors ``error`` so the generic FastAPI error reader in the
    frontend (``readErrorDetail``) surfaces the same precise message.
    """
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": message, "detail": message},
    )


def _seed_requested(request: Request) -> bool:
    """Read the ``?seed=`` / ``?create_session=`` query flag."""
    for key in _SEED_KEYS:
        value = request.query_params.get(key)
        if value is not None and value.strip().lower() in _TRUTHY:
            return True
    return False


def _seed_from_json(obj: dict) -> bool:
    for key in _SEED_KEYS:
        value = obj.get(key)
        if isinstance(value, bool) and value:
            return True
    return False


def _persona_requested(request: Request) -> bool:
    """Read the ``?persona=`` query flag (mirrors :func:`_seed_requested`)."""
    for key in _PERSONA_KEYS:
        value = request.query_params.get(key)
        if value is not None and value.strip().lower() in _TRUTHY:
            return True
    return False


def _persona_from_json(obj: dict) -> bool:
    """Read the ``"persona": true`` JSON-body flag (mirrors :func:`_seed_from_json`)."""
    for key in _PERSONA_KEYS:
        value = obj.get(key)
        if isinstance(value, bool) and value:
            return True
    return False


def _world_requested(request: Request) -> bool:
    """Read the ``?world=`` query flag (mirrors :func:`_persona_requested`)."""
    for key in _WORLD_KEYS:
        value = request.query_params.get(key)
        if value is not None and value.strip().lower() in _TRUTHY:
            return True
    return False


def _world_from_json(obj: dict) -> bool:
    """Read the ``"world": true`` JSON-body flag (mirrors :func:`_persona_from_json`)."""
    for key in _WORLD_KEYS:
        value = obj.get(key)
        if isinstance(value, bool) and value:
            return True
    return False


def _world_from_card(log: "EventLog", card: "CardV2", card_png: bytes | None):
    """Materialize ``card`` as a world through the ONE shared write path.

    The write itself lives in :mod:`fabulist.api.worlds_api` (issue #40's typed
    door, which stays a supported endpoint), so this route adds an ENTRY POINT,
    never a second write. The import is lazy: the world module is loaded on
    first use rather than at router-import time.
    """
    from fabulist.api.worlds_api import _import_card_as_world

    return _import_card_as_world(log, card, card_png)


def _decode_source(data: bytes) -> tuple[Any, bool, bool, bool]:
    """Turn raw request bytes into a parser source plus the body flags.

    Returns ``(png_bytes_or_dict, seed, persona, world)``. Raises
    :class:`CardError` with a precise message for anything that is neither a PNG
    nor a JSON card object.
    """
    if not data or not data.strip():
        raise CardError("no card data in request body")
    # Sniff the PNG signature rather than trusting Content-Type: a card PNG is
    # unambiguous, and browsers occasionally send a generic type.
    if data.startswith(PNG_SIGNATURE):
        return bytes(data), False, False, False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CardError(
            "request body is neither a PNG (missing signature) nor UTF-8 JSON"
        ) from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CardError(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise CardError("JSON body must be a card object")
    seed = _seed_from_json(obj)
    persona = _persona_from_json(obj)
    world = _world_from_json(obj)
    nested = obj.get("card")
    if isinstance(nested, dict):
        return nested, seed, persona, world
    # A V2 envelope has `spec`/`data`; a V1/flat card has `name`. Anything else
    # is handed to the parser, whose validation yields the precise error.
    return obj, seed, persona, world


def _source_from_multipart(form: Any) -> tuple[bytes | None, bool, Any]:
    """Best-effort extraction from a parsed multipart form.

    ``form`` values may be ``UploadFile`` (async ``read``) or plain strings.
    Returns ``(raw_bytes_for_decode, seed, upload)``; ``raw_bytes`` is ``None``
    when the payload arrived as an ``UploadFile`` (``upload`` is then set) or
    when no recognized field carries data.
    """
    seed = False
    seed_value = form.get("seed")
    if isinstance(seed_value, str) and seed_value.strip().lower() in _TRUTHY:
        seed = True

    for key in _MULTIPART_FILE_FIELDS:
        value = form.get(key)
        read = getattr(value, "read", None)
        if callable(read):
            return None, seed, value

    for key in _MULTIPART_TEXT_FIELDS:
        value = form.get(key)
        if isinstance(value, str) and value.strip():
            return value.encode("utf-8"), seed, None

    return None, seed, None


async def _read_request_bytes(request: Request) -> tuple[bytes | None, bool, Any]:
    """Collect the card payload and seed flag from any supported request shape.

    Returns ``(data, seed, upload)`` where ``upload`` is an ``UploadFile`` when
    the source arrived as a multipart file (its bytes must be awaited), else
    ``None``.
    """
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    seed = _seed_requested(request)

    if ctype == "multipart/form-data":
        try:
            form = await request.form()
        except Exception as exc:  # python-multipart missing (or malformed)
            raise HTTPException(
                status_code=415,
                detail={
                    "reason": (
                        "multipart/form-data requires the python-multipart "
                        f"package ({exc}); POST raw PNG bytes or JSON instead"
                    )
                },
            ) from exc

        data, form_seed, upload = _source_from_multipart(form)
        return data, seed or form_seed, upload

    body = await request.body()
    return body, seed, None


@router.post("/api/cards/import")
async def import_card(request: Request):
    """Parse a card and optionally act on it (D9/D39/D51/#60).

    Four modes, selected by the ``seed`` / ``persona`` / ``world`` flags:

    * no flag — parse and echo the card, no side effects;
    * ``seed`` — append a ``session_created`` system event, the truthful
      ``card_imported`` system marker, the card ``system`` event, and the D39
      persona upsert to a brand-new session id (no fabricated ``user`` turn);
    * ``persona`` — definition-only: upsert the card as a persona on the
      ``__personas__`` anchor and return it, creating no session at all;
    * ``world`` — definition-only: materialize the card as a world through the
      same write path ``POST /api/worlds/import`` uses (issue #60).

    Any two modes together are a precise 422 ("choose one mode") — none
    silently wins. Never mutates existing history.
    """
    log = _log_for(request)

    try:
        data, seed, upload = await _read_request_bytes(request)
        if upload is not None:
            data = await upload.read()
        if data is None:
            raise CardError("no card data in request body")
        source, body_seed, body_persona, body_world = _decode_source(data)
        card: "CardV2" = parse_card_v2(source)
    except CardError as exc:
        return _card_error(str(exc))
    except HTTPException:
        raise

    seed = seed or body_seed
    persona = _persona_requested(request) or body_persona
    world = _world_requested(request) or body_world
    modes = [
        name
        for name, asked in (("seed", seed), ("persona", persona), ("world", world))
        if asked
    ]
    if len(modes) > 1:
        # No mode wins: an ambiguous request is refused, never silently resolved.
        return _card_error("choose one mode: " + " or ".join(f"?{m}=1" for m in modes))
    # A PNG import carries the original image bytes; retain them as a media
    # asset so the card can be exported back to a PNG later (D28 / A4).
    card_png = source if isinstance(source, bytes) else None
    result: dict[str, Any] = {"ok": True, "card": card.to_dict()}
    if seed:
        session_id, title = _seed_session(request, log, card, card_png=card_png)
        result["session_id"] = session_id
        result["title"] = title
    elif persona:
        result["persona"] = _persona_wire_for_card(log, card, card_png=card_png)
    elif world:
        result["world"], result["book_id"] = _world_from_card(
            log, card, card_png=card_png
        )
    return result


def _capture_definition(
    log: "EventLog", card: "CardV2", card_png: bytes | None = None
) -> "Persona":
    """Upsert ``card`` as a persona definition (book + artwork); return it.

    One write path for both import modes (D51): the name-keyed persona upsert
    (D39), its materialized ``character_book`` (D31) and — when the import
    carried a PNG — the persona's ``image_asset``, stored ONCE on the sessionless
    definition anchor (content-addressed, so a re-import adds no second asset).
    The caller may then reference that id from a session payload instead of
    keeping a session-scoped copy of the same bytes. ``500`` if the append did
    not fold back (a registry bug, never a user error).
    """
    card_dict = card.to_dict()
    book_id = materialize_card_book(log, card_dict)
    persona_id = upsert_persona_from_card(log, card_dict)
    if book_id is not None:
        attach_book_to_persona(log, persona_id, book_id)
    if card_png is not None:
        # Content-addressed + deduped: identical bytes reuse the anchor row.
        asset_id = store_definition_image(
            log, card_png, "image/png", note=f"persona image: {persona_id}"
        )
        set_persona_image(log, persona_id, asset_id)
    for persona in list_personas(log):
        if persona.persona_id == persona_id:
            return persona
    raise HTTPException(status_code=500, detail="persona append did not persist")


def _persona_wire_for_card(
    log: "EventLog", card: "CardV2", card_png: bytes | None = None
) -> dict:
    """Upsert ``card`` as a persona and return the persona wire (D51, D39).

    Reuses :func:`fabulist.api.personas_api._wire` so the import response is
    byte-identical to ``GET /api/personas`` (the definition contract the studio
    pane reads). The write is the registry's name-keyed upsert — a ``system``
    event on ``__personas__``, never a session row.

    D31 import fix: the card's ``character_book`` is also materialized into a
    reusable Book entity (:func:`fabulist.context.books.materialize_card_book`)
    and attached to the persona's ``book_ids``, so the import no longer drops
    the lorebook (the Studio BookPane catalogs it and the lore renders on
    reuse). A card without a ``character_book`` materializes nothing.
    """
    return _persona_wire(_capture_definition(log, card, card_png=card_png))


def _seed_session(
    request: Request,
    log: "EventLog",
    card: "CardV2",
    card_png: bytes | None = None,
) -> tuple[str, str]:
    """Create a new session titled after the card and append the seed events.

    Mirrors the app factory's ``POST /api/sessions`` (a ``session_created``
    system event) and then appends :func:`to_session_seed`'s system
    payloads — a ``card_imported`` marker and the card facts — so the engine
    sees the card facts on the first turn and no fabricated player turn is
    written. When ``card_png`` carries the original PNG bytes (a PNG import,
    not JSON), the image is retained ONCE as the persona's definition image and
    its content-addressed id is recorded on the system payload as
    ``card_asset`` — a *ref*, never inline bytes on the seed event — so PNG
    export can rewrap the original image without a session-scoped second copy.
    JSON imports and pre-existing sessions omit the key.
    """
    session_id = uuid.uuid4().hex
    title = card.name

    # D39: the persona definition (+ its book + its artwork) is captured FIRST,
    # so the card system event below can reference the definition image asset
    # instead of storing the same bytes a second time on the session. The
    # definition write lives on the ``__personas__``/``__media__`` anchors, so
    # it does not touch this session's sequence.
    definition = _capture_definition(log, card, card_png=card_png)
    persona_id = definition.persona_id
    image_asset = definition.image_asset

    # Register in the app's in-memory session registry so the projection and
    # turn endpoints resolve the new id (they look the session up by id).
    # ``_Session`` is imported lazily: the app factory owns the class and may
    # import this module while it is still initialising (circular import).
    from fabulist.api.app import _Session

    registry = getattr(request.app.state, "sessions", None)
    if isinstance(registry, dict):
        registry[session_id] = _Session(session_id, title)

    log.append_event(
        session_id,
        "system",
        "system",
        {"title": title, "event": "session_created"},
    )

    seed = to_session_seed(card)
    # The truthful import record: a ``system`` marker, never a fabricated
    # ``user`` turn. It rides beside the card payload below so a reader can
    # tell the session was seeded from a card without inventing a player line.
    marker_event = seed["marker_event"]
    log.append_event(
        session_id,
        "system",
        "system",
        dict(marker_event["payload"]),
    )
    system_event = seed["system_event"]
    system_payload = dict(system_event["payload"])
    if image_asset:
        system_payload["card_asset"] = image_asset
    log.append_event(
        session_id,
        "system",
        "system",
        system_payload,
    )

    # D29: seed the card's present V2 text fields as session modules so the
    # narrator builder can splice system_prompt / post_history_instructions /
    # mes_example into the active preset's narrate flow. A sparse card appends
    # nothing. These are session events, never shared preset mutations.
    for home in seed_homes(card):
        log.append_event(session_id, "system", "system", home)

    # D53: the imported card's persona is the chat's ACTOR (its owner), recorded
    # in the separate ``chat_actor`` event — kept apart from the USER persona
    # ``chat_persona`` ({{user}}, D43). This is the chat's only actor write here;
    # the definition above stays a definition, this is the assignment.
    from fabulist.memory.actor import append_chat_actor

    append_chat_actor(log, session_id, [persona_id])

    return session_id, title


class GreetingRequest(BaseModel):
    """Body of ``POST /api/sessions/{id}/greeting`` (the Opening swipe)."""

    index: int


def _card_seed_event(log: "EventLog", session_id: str):
    """The session's first card-carrying ``system`` event, or ``None``.

    The import seed is the single source for the Opening block: its payload
    holds the card dict (greetings), the stored ``first_mes``, and the optional
    ``card_asset`` id for the card image. A world-materialized chat's FIRST
    card event is a CAST member (issue #51), which never owns the opening —
    :func:`_opening_cycle` resolves those sessions from ``world_ref`` first.

    Fork-aware (issue #86): the seed sits at creation, i.e. BELOW any fork cut,
    so a branch reaches it only through the chain — an own-row read made every
    card chat's branch render no Opening block at all.
    """

    for event in log.get_chain_events(session_id):
        if event.type == "system" and isinstance(event.payload.get("card"), dict):
            return event
    return None


def _world_ref(events: list) -> dict | None:
    """The session's latest ``world_ref`` payload, or ``None`` for a card chat.

    Latest wins (D32): a re-attachment appends a new snapshot. A world chat's
    OPENING lives here (issue #51) — ``first_mes`` + ``alternate_greetings``
    snapshotted at creation — so the opening readers never guess from a cast
    card.
    """

    from fabulist.worlds.registry import WORLD_REF_EVENT

    ref: dict | None = None
    for event in events:
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict) and payload.get("event") == WORLD_REF_EVENT:
            ref = payload
    return ref


def _opening_cycle(
    log: "EventLog", session_id: str, events: list
) -> tuple[list[str], dict | None, str | None]:
    """``(greeting variants, card dict | None, card asset | None)`` for a session.

    ONE resolution shared by the Opening read and the swipe write, so the two
    can never disagree about how many variants exist:

    * a session carrying a ``world_ref`` (a chat started from a world) uses the
      SNAPSHOT's opening — the world's own ``first_mes`` + ``alternate_greetings``
      — exactly like a persona chat uses its card seed. Cast cards are never
      consulted: their greetings are suppressed in world context (issue #51).
      An empty world greeting yields an all-empty cycle, i.e. no Opening block.
    * otherwise the first card-carrying ``system`` event is the opening seed
      (imported cards, persona chats).

    ``card`` is the dict the Details view shows (``None`` when there is none) and
    ``card_asset`` the retained card image ref (``None`` for a world: its image
    is a sessionless definition asset served by its own route).
    """

    ref = _world_ref(events)
    if ref is not None:
        card = {
            "name": str(ref.get("name") or ""),
            "scenario": str(ref.get("scenario") or ""),
            "first_mes": str(ref.get("first_mes") or ""),
            "alternate_greetings": [
                greeting
                for greeting in ref.get("alternate_greetings") or []
                if isinstance(greeting, str)
            ],
        }
        return greeting_variants(card, card["first_mes"]), card, None

    event = _card_seed_event(log, session_id)
    if event is None:
        return [], None, None
    card = event.payload["card"]
    asset = event.payload.get("card_asset")
    if not isinstance(asset, str) or not asset:
        asset = _actor_persona_image(log, session_id, events)
    return greeting_variants(card, event.payload.get("first_mes")), card, asset


class SessionModuleRequest(BaseModel):
    """Body of ``PUT .../session-modules/{module_id}`` (one module override)."""

    name: str
    anchor: str
    content: str = ""
    enabled: bool = True
    origin: str | None = None


@router.get("/api/sessions/{session_id}/session-modules")
async def session_modules_route(session_id: str, request: Request) -> dict:
    """The session's current module overrides (D29), folded latest-wins.

    The prompt drawer's Session-overrides section reads this. Disabled modules
    stay listed (the UI must show them to re-enable); the narrator-builder
    splice is what drops them from the compiled flow.
    """

    log = _log_for(request)
    if not log.get_events(session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return {"modules": session_modules_from_log(log, session_id)}


@router.put("/api/sessions/{session_id}/session-modules/{module_id}")
async def put_session_module_route(
    session_id: str, module_id: str, body: SessionModuleRequest, request: Request
) -> dict:
    """Append one session-module override (edit / toggle / re-anchor, D29).

    Append-only (D9): re-appending the same ``module_id`` supersedes at fold
    time; nothing is rewritten. The payload shape matches
    :func:`fabulist.cards.homes.seed_homes` exactly so seeded and hand-edited
    modules are indistinguishable downstream.
    """

    if body.anchor not in (ANCHOR_TOP, ANCHOR_POST_HISTORY):
        raise HTTPException(
            status_code=422,
            detail=f"anchor must be '{ANCHOR_TOP}' or '{ANCHOR_POST_HISTORY}'",
        )
    log = _log_for(request)
    if not log.get_events(session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    payload = {
        "event": "session_module",
        "module_id": module_id,
        "name": body.name,
        "anchor": body.anchor,
        "content": body.content,
        "enabled": body.enabled,
        "origin": body.origin,
    }
    log.append_event(session_id, "system", "user", payload)
    return {"session_id": session_id, "module": payload}


@router.get("/api/sessions/{session_id}/opening")
async def opening_route(session_id: str, request: Request) -> dict:
    """The pinned Opening block's data: greeting cycle + selected index + card.

    Returns ``{"greetings": [...], "index": n, "card": {...}|
    null, "card_asset": str|null}``. ``greetings`` is
    ``[first_mes, *alternate_greetings]`` (variant 0 from the seed payload, for
    consistency with the persona block). A session with no card yields an empty
    cycle so the frontend hides the block.

    A chat started from a WORLD resolves the SAME cycle from its ``world_ref``
    snapshot (issue #51: the world's own ``first_mes`` + ``alternate_greetings``,
    swiped exactly like a persona chat) — never from a cast member's greeting,
    which is suppressed in world context. See :func:`_opening_cycle`.

    ``card_asset`` is the seed payload's ref, or — for a card with no stored
    image ref (a JSON import, or a chat whose art was added to the persona
    later) — the chat's own actor persona ``image_asset``, so the retained card
    art still renders. A session whose actor personas carry no art yields
    ``null`` (the block renders text only).

    Issue #63: ``greetings`` are RENDERED for display — the same one step a
    transcript bubble uses (:func:`fabulist.prompts.variables.render_display_text`),
    so a card greeting written ``{{user}}`` shows the persona name instead of
    the raw token. ``card`` stays the raw definition dict (the Details view shows
    authored text).
    """

    log = _log_for(request)
    # Fork-aware read (issue #86): the card seed, the world_ref snapshot and the
    # ``greeting_selected`` index are all inherited from below a branch's cut;
    # own rows alone hid the Opening block on every branch.
    events = log.get_chain_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="unknown session")
    greetings, card, asset = _opening_cycle(log, session_id, events)
    if card is None:
        return {"greetings": [], "index": 0, "card": None, "card_asset": None}
    index = selected_greeting(events)
    if index >= len(greetings):
        index = 0
    # Issue #63: the Opening block is the chat's FIRST message, so it renders
    # the known macros exactly like a user bubble does (one display step, same
    # map). The index/count is resolved on the RAW cycle above, so the swipe
    # stays aligned; only the DISPLAY strings are rendered.
    variables = display_variables(log, session_id)
    greetings = [
        render_display_text(log, session_id, greeting, variables=variables)
        for greeting in greetings
    ]
    return {
        "greetings": greetings,
        "index": index,
        "card": card,
        "card_asset": asset if isinstance(asset, str) else None,
        "name": card.get("name"),
    }


def _actor_persona_image(log: "EventLog", session_id: str, events: list) -> str | None:
    """The ``image_asset`` of the chat's actor persona, or ``None``.

    Exactly ONE actor persona carrying art resolves; zero actors, several
    actors, or an actor set none of whose personas has art yields ``None`` (the
    Opening must never guess which face a card chat wears). Actor ids come from
    the ``chat_actor`` record, falling back to the legacy card-name fold.
    """
    from fabulist.memory.actor import actors_from_cards, fold_actors

    actors = fold_actors(log, session_id) or actors_from_cards(log, events)
    if not actors:
        return None
    by_id = {persona.persona_id: persona for persona in list_personas(log)}
    images = [
        by_id[persona_id].image_asset
        for persona_id in actors
        if persona_id in by_id and by_id[persona_id].image_asset
    ]
    return images[0] if len(images) == 1 else None


@router.post("/api/sessions/{session_id}/greeting")
async def select_greeting_route(
    session_id: str, body: GreetingRequest, request: Request
) -> dict:
    """Persist the Opening swipe as a ``greeting_selected`` event (D30).

    Append-only (D9): the latest index wins, the prior selections stay queryable.
    An invalid index (negative, or past the card's cycle when a card exists) is
    a precise 422; the frontend renders the returned index as the pinned
    selection after the append.
    """

    log = _log_for(request)
    # Same fork-aware read as the GET (issue #86): the cycle the swipe is
    # validated against is the INHERITED one on a branch. An own-row read saw an
    # empty cycle and let an out-of-range index through.
    events = log.get_chain_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="unknown session")
    if body.index < 0:
        raise HTTPException(status_code=422, detail="index must be >= 0")
    greetings, card, _asset = _opening_cycle(log, session_id, events)
    if card is not None and body.index >= len(greetings):
        raise HTTPException(
            status_code=422,
            detail=f"index {body.index} is past the {len(greetings)} available greetings",
        )
    log.append_event(
        session_id,
        "system",
        "user",
        {"event": "greeting_selected", "index": body.index},
    )
    return {"session_id": session_id, "index": body.index, "event": "greeting_selected"}
