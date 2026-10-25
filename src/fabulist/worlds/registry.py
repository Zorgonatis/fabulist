"""World entity registry (D32): worlds as log-owned entities.

PLAN.md **D32** — *Worlds on ``__worlds__``*: a **World** is a reusable setting —
a named scenario plus a cast of personas and a set of books. Starting a chat from
a world **materializes** that definition onto an ordinary chat session: each cast
member is seeded as a card-shaped ``system`` event (so the existing multi-card
roster render produces ``### Cast``), a ``world_ref`` snapshot is recorded, and
the world's books are registered on the session. From that point on the engine,
roster and facts paths are untouched below seeding — the chat is a normal chat.

The event log owns everything (D9), so worlds are **system events on a reserved
anchor session** (:data:`WORLDS_ANCHOR`), folded latest-wins per ``world_id`` —
the exact shape of :mod:`fabulist.personas.registry` / :mod:`fabulist.context.books`.

Event contract (fixed, shared with the experience-profile lane):

* World definition — a ``system`` event on :data:`WORLDS_ANCHOR`::

      {"event": "world", "world_id": str, "name": str, "description": str,
       "scenario": str, "first_mes": str, "alternate_greetings": [str, ...],
       "cast": [persona_id, ...], "book_ids": [book_id, ...],
       "provenance": dict}

  Create and update both append; the newest row per ``world_id`` wins.
  ``first_mes`` / ``alternate_greetings`` are the world's OWN opening (issue
  #51): the card fields keep the names and shape they have on a persona, so
  the opening/swipe surfaces need no second convention.

* Tombstone — ``{"event": "world_deleted", "world_id": str}`` on the anchor,
  removed from the fold (append-only log, D9).

* Chat materialization — ``system`` events on the new chat session::

      {"card": {...}}                           # one per cast member
      {"event": "world_ref", "world_id": str, "name": str, "scenario": str,
       "first_mes": str, "alternate_greetings": [str, ...]}  # snapshot
      {"event": "session_books", "book_ids": [...]}   # when the world has books

  ``world_ref`` is a **snapshot**: editing the world later does not rewrite a
  running chat ("refresh cast" is deferred, D32). It carries the world's
  OPENING (``first_mes`` + ``alternate_greetings``) alongside the scenario, and
  that is the whole opening story: the chat opens on the world's greeting,
  swiped through the alternates exactly like a persona chat
  (:func:`fabulist.cards.v2.persona_to_seed_card`). ``GET
  /api/sessions/{id}/opening`` resolves a session that carries a ``world_ref``
  from it, and both opening readers (the pinned Opening block and the
  narrator's ``Opening scene (already established)`` line) see the selected
  variant. The **scenario is context, never the opening** (owner ruling): it
  reaches the narrator through the ``### World`` block
  (:func:`fabulist.context.facts.session_world_definition_block`), never as the
  chat's first message.

  Cast card events deliberately carry **no greeting at all** — neither
  ``first_mes`` nor ``alternate_greetings``: a cast member's greeting belongs
  to that persona's OWN chat, and surfacing it in a world chat poisoned the
  context (owner ruling, issue #51). The roster render reads the card dict's
  fields and simply omits its opening-scene line when the greeting is absent.
  The card payload mirrors :func:`fabulist.cards.to_session_seed` for every
  other field, so :func:`fabulist.context.roster.extract_cards` and
  :func:`fabulist.context.facts.session_world_info_block` read it with no new
  machinery.

  :data:`WORLD_OPENING_FLAG` survives only as a READ shim: world chats created
  before issue #51 seeded the scenario as a marked ``narrative`` opening row,
  and the frontend still pins those (they keep their text; no retro-migration).
  New chats never write it.

Public API
----------
:data:`WORLDS_ANCHOR`, :data:`WORLD_REF_EVENT`, :data:`SESSION_BOOKS_EVENT`,
:data:`WORLD_OPENING_FLAG`, :data:`NARRATOR_ACTOR`,
:class:`World`, :func:`append_world`, :func:`delete_world`, :func:`list_worlds`,
:func:`world_by_id`, :func:`world_to_wire`, :func:`wire_to_world`,
:func:`create_world_chat`, :func:`set_session_books`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog
    from fabulist.personas.registry import Persona


__all__ = [
    "NARRATOR_ACTOR",
    "SESSION_BOOKS_EVENT",
    "WORLDS_ANCHOR",
    "WORLD_DELETED_EVENT",
    "WORLD_EVENT",
    "WORLD_OPENING_FLAG",
    "WORLD_REF_EVENT",
    "World",
    "append_world",
    "create_world_chat",
    "delete_world",
    "list_worlds",
    "set_session_books",
    "set_world_image",
    "wire_to_world",
    "world_by_id",
    "world_to_wire",
]

#: Reserved session id anchoring world definition events (like
#: ``__personas__`` / ``__books__``). Its ``__`` prefix keeps it out of the
#: session list (:func:`fabulist.api.sessions._session_ids`).
WORLDS_ANCHOR = "__worlds__"

#: System-event kind carrying one world definition (per ``world_id``, latest wins).
WORLD_EVENT = "world"

#: System-event kind carrying a tombstone (removes ``world_id`` from the fold).
WORLD_DELETED_EVENT = "world_deleted"

#: Chat-session ``system`` kind carrying the world snapshot a chat was started
#: from (``{world_id, name, scenario}``); read by
#: :func:`fabulist.context.facts.session_world_info_block`.
WORLD_REF_EVENT = "world_ref"

#: Chat-session ``system`` kind registering the world's ``book_ids`` on the chat
#: (so the WI render reaches them without re-resolving the world).
SESSION_BOOKS_EVENT = "session_books"

#: Actor stamped on human-intent appends (mirrors the personas/settings lanes).
USER_ACTOR = "user"

#: Payload key marking the seeded opening ``narrative`` event. LEGACY READ
#: SHIM (issue #51): world chats created before the fix seeded the SCENARIO as a
#: marked ``narrative`` row, and the frontend still pins those rows (their text
#: is preserved; there is no retro-migration). A chat started now seeds no
#: opening narrative at all — the opening lives in the ``world_ref`` snapshot —
#: so this constant is never written by new code.
WORLD_OPENING_FLAG = "opening"

#: Actor stamped on the seeded opening narrative. LEGACY (see
#: :data:`WORLD_OPENING_FLAG`): only pre-issue-#51 world chats carry such a row.
NARRATOR_ACTOR = "narrator"


@dataclass(frozen=True)
class World:
    """One world folded from the ``__worlds__`` anchor session.

    ``cast`` is an ordered list of persona ids (roster order); ``book_ids`` is an
    ordered list of :mod:`fabulist.context.books` ids; ``provenance`` is
    free-form origin metadata and defaults to ``{}``. ``image_asset`` is the
    content-addressed id of a **sessionless** definition image stored on
    :data:`fabulist.media.images.MEDIA_ANCHOR` (``None`` when the world has no
    image).

    ``first_mes`` / ``alternate_greetings`` are the world's OPENING (issue #51):
    the card fields under the names a persona uses, so a chat started from the
    world opens on ``first_mes`` with the alternates as swipes. Empty/absent
    values degrade exactly like an empty persona greeting — the Opening block is
    hidden, nothing is fabricated. ``scenario`` stays CONTEXT (it renders in the
    ``### World`` block), never the opening.
    """

    world_id: str
    name: str
    description: str = ""
    scenario: str = ""
    first_mes: str = ""
    alternate_greetings: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    book_ids: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    image_asset: str | None = None


def _text(value: Any) -> str:
    """Coerce an optional field to a plain string (``None`` -> ``""``)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _optional_str(value: Any) -> str | None:
    """Coerce an optional single id/ref to ``str | None`` (empty -> ``None``)."""
    return value if isinstance(value, str) and value else None


def _str_list(value: Any) -> list[str]:
    """Coerce an optional id-list field to ``list[str]`` (missing -> ``[]``)."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item if isinstance(item, str) else str(item) for item in value]


def _valid_definition(payload: dict) -> tuple[str, str] | None:
    """Extract ``(world_id, name)`` from a world payload, or ``None``."""
    world_id = payload.get("world_id")
    name = payload.get("name")
    if not isinstance(world_id, str) or not world_id:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return world_id, name


def _world_from_payload(payload: dict) -> World | None:
    """Build a :class:`World` from one ``world`` payload (``None`` if bad)."""
    ids = _valid_definition(payload)
    if ids is None:
        return None
    world_id, name = ids
    provenance = payload.get("provenance")
    return World(
        world_id=world_id,
        name=name,
        description=_text(payload.get("description")),
        scenario=_text(payload.get("scenario")),
        first_mes=_text(payload.get("first_mes")),
        alternate_greetings=_str_list(payload.get("alternate_greetings")),
        cast=_str_list(payload.get("cast")),
        book_ids=_str_list(payload.get("book_ids")),
        provenance=dict(provenance) if isinstance(provenance, dict) else {},
        image_asset=_optional_str(payload.get("image_asset")),
    )


# -- world definitions -------------------------------------------------------


def append_world(log: "EventLog", world: dict) -> str:
    """Append one world definition to :data:`WORLDS_ANCHOR`; return its id.

    ``world`` carries at least ``name`` (plus any of ``description`` /
    ``scenario`` / ``first_mes`` / ``alternate_greetings`` / ``cast`` /
    ``book_ids`` / ``provenance``). When ``world_id`` is absent a fresh
    ``uuid4().hex`` is generated. Create and update both append (latest-wins per
    id); raising ``ValueError`` on a missing/empty name keeps a nameless world
    out of the catalog. The write is a ``system`` event (D9).

    The opening fields default to an ABSENT greeting (``""`` / ``[]``), which
    degrades exactly like an empty persona ``first_mes``: no Opening block, no
    invented text.
    """
    data = dict(world or {})
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("world requires a non-empty 'name'")

    world_id = data.get("world_id")
    if not isinstance(world_id, str) or not world_id:
        world_id = uuid.uuid4().hex

    log.append_event(
        WORLDS_ANCHOR,
        "system",
        USER_ACTOR,
        {
            "event": WORLD_EVENT,
            "world_id": world_id,
            "name": name,
            "description": _text(data.get("description")),
            "scenario": _text(data.get("scenario")),
            "first_mes": _text(data.get("first_mes")),
            "alternate_greetings": _str_list(data.get("alternate_greetings")),
            "cast": _str_list(data.get("cast")),
            "book_ids": _str_list(data.get("book_ids")),
            "provenance": data.get("provenance")
            if isinstance(data.get("provenance"), dict)
            else {},
            # Sessionless definition image ref (anchor asset id), or ``None``.
            "image_asset": _optional_str(data.get("image_asset")),
        },
    )
    return world_id


def set_world_image(
    log: "EventLog", world_id: str, image_asset: str | None
) -> World | None:
    """Set a world's ``image_asset`` by re-appending it (latest-wins).

    Append-only (D9): the whole definition is re-appended with ``image_asset``
    set, so the newest row wins and the prior rows stay queryable. Returns the
    updated :class:`World`, or ``None`` when ``world_id`` is unknown. A world's
    **cast members are personas**, so a cast member's image is the persona's own
    ``image_asset`` (:func:`fabulist.personas.registry.set_persona_image`) —
    there is no separate per-cast image store.
    """
    target = world_by_id(log, world_id)
    if target is None:
        return None
    append_world(
        log,
        {
            "world_id": target.world_id,
            "name": target.name,
            "description": target.description,
            "scenario": target.scenario,
            "first_mes": target.first_mes,
            "alternate_greetings": list(target.alternate_greetings),
            "cast": list(target.cast),
            "book_ids": list(target.book_ids),
            "provenance": dict(target.provenance),
            "image_asset": image_asset,
        },
    )
    return world_by_id(log, world_id)


def delete_world(log: "EventLog", world_id: str) -> None:
    """Tombstone a world (``world_deleted``); the fold drops it.

    Non-destructive (D9): definitions stay queryable, only the fold changes.
    """
    log.append_event(
        WORLDS_ANCHOR,
        "system",
        USER_ACTOR,
        {"event": WORLD_DELETED_EVENT, "world_id": world_id},
    )


def list_worlds(log: "EventLog") -> list[World]:
    """Fold the ``__worlds__`` anchor session into worlds (latest wins).

    Same fold shape as :func:`fabulist.personas.registry.list_personas`. A
    ``world_deleted`` tombstone removes the id; malformed rows are ignored.
    """
    folded: dict[str, World] = {}
    for event in log.get_events(WORLDS_ANCHOR):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        kind = payload.get("event")
        if kind == WORLD_EVENT:
            world = _world_from_payload(payload)
            if world is not None:
                folded[world.world_id] = world
        elif kind == WORLD_DELETED_EVENT:
            world_id = payload.get("world_id")
            if isinstance(world_id, str) and world_id:
                folded.pop(world_id, None)
    return list(folded.values())


def world_by_id(log: "EventLog", world_id: str) -> World | None:
    """One folded world by id, or ``None`` when absent/tombstoned."""
    for world in list_worlds(log):
        if world.world_id == world_id:
            return world
    return None


# -- wire conversions --------------------------------------------------------


def world_to_wire(world: World) -> dict:
    """Wire shape of one world (the frontend/other-lane contract)."""
    return {
        "world_id": world.world_id,
        "name": world.name,
        "description": world.description,
        "scenario": world.scenario,
        "first_mes": world.first_mes,
        "alternate_greetings": list(world.alternate_greetings),
        "cast": list(world.cast),
        "book_ids": list(world.book_ids),
        "provenance": dict(world.provenance),
        "image_asset": world.image_asset,
    }


def wire_to_world(data: dict) -> World:
    """Parse a wire dict into a :class:`World` (raises ``ValueError`` if bad)."""
    if not isinstance(data, dict):
        raise ValueError("world must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("world requires a non-empty 'name'")
    world_id = data.get("world_id")
    if world_id is not None and (not isinstance(world_id, str) or not world_id):
        raise ValueError("world 'world_id' must be a non-empty string")
    for key in ("cast", "book_ids", "alternate_greetings"):
        value = data.get(key)
        if value is not None and not isinstance(value, list):
            raise ValueError(f"world '{key}' must be a list")
    first_mes = data.get("first_mes")
    if first_mes is not None and not isinstance(first_mes, str):
        raise ValueError("world 'first_mes' must be a string")
    provenance = data.get("provenance")
    if provenance is not None and not isinstance(provenance, dict):
        raise ValueError("world 'provenance' must be an object")
    image_asset = data.get("image_asset")
    if image_asset is not None and not isinstance(image_asset, str):
        raise ValueError("world 'image_asset' must be a string")
    return World(
        world_id=world_id or "",
        name=name,
        description=_text(data.get("description")),
        scenario=_text(data.get("scenario")),
        first_mes=_text(first_mes),
        alternate_greetings=_str_list(data.get("alternate_greetings")),
        cast=_str_list(data.get("cast")),
        book_ids=_str_list(data.get("book_ids")),
        provenance=dict(provenance) if isinstance(provenance, dict) else {},
        image_asset=_optional_str(image_asset),
    )


# -- chat materialization ----------------------------------------------------


def _persona_card(persona: "Persona") -> dict:
    """Shape one :class:`~fabulist.personas.registry.Persona` as a card dict.

    Mirrors the fields :func:`fabulist.context.roster.extract_cards` /
    :func:`fabulist.context.roster.persona_block` read (plus the D31
    carry-overs) so a materialized cast member is indistinguishable from an
    imported card to every downstream consumer — EXCEPT that NO greeting is
    carried (owner ruling, issue #51): a world chat's opening is the world's own
    ``first_mes`` (see :func:`create_world_chat`), and a reused persona's
    greeting must never surface in a world context — it poisoned the context.
    Both ``first_mes`` and ``alternate_greetings`` are therefore dropped: the
    roster's opening-scene line reads an absent greeting as "no line", and the
    session-wide ``greeting_selected`` index a world swipe appends cannot make a
    cast member's alternate greeting appear (``_selected_greeting`` in facts.py
    indexes the CAST CARD's cycle, which is empty).
    """
    return {
        "name": persona.name,
        "description": persona.description,
        "personality": persona.personality,
        "scenario": persona.scenario,
        "extensions": dict(persona.extensions),
    }


def set_session_books(
    log: "EventLog", session_id: str, book_ids: list[str]
) -> list[str]:
    """Register a chat session's book set (append-only, latest-wins fold).

    Appends the exact ``session_books`` payload :func:`create_world_chat` writes,
    so the readers that already fold it reach the attachment with no new
    machinery: :func:`fabulist.api.worlds_api.session_world_route`
    (``GET /api/sessions/{id}/world``) takes the latest registration, and
    :func:`fabulist.context.facts._entity_books` pools it into the WI render.

    D9/D10: this is an append, never an in-place write. ``GET .../world`` reads
    latest-wins (a later call supersedes the previous set); the WI render pools
    every registration, so a superseded set still contributes there. An explicit
    ``[]`` is real intent (clear the attachment) and is distinct from omitting
    the call — the caller decides. Returns the ids as stored.
    """
    ids = _str_list(book_ids)
    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {"event": SESSION_BOOKS_EVENT, "book_ids": ids},
    )
    return ids


def create_world_chat(
    log: "EventLog",
    world_id: str,
    *,
    title: str | None = None,
    persona_id: str | None = None,
) -> dict:
    """Materialize a world into a new chat session and return its identity.

    Appends, in order: the ``session_created`` title anchor (mirrors the app
    factory / :mod:`fabulist.api.cards_api`); when a persona is supplied (or a
    default is pinned and none is supplied) the ``chat_persona`` assignment —
    the user's persona, materialized near seq 2 so it sits inside a later
    fork's chain window; one card-shaped ``system`` event per cast member (in
    world order, so the roster renders ``### Cast``); the ``world_ref``
    snapshot (carrying the world's opening: ``first_mes`` +
    ``alternate_greetings``); and — when the world carries books — a
    ``session_books`` registration.

    THE OPENING IS THE WORLD'S ``first_mes`` (issue #51), swiped through
    ``alternate_greetings`` exactly like a persona chat's Opening block: the
    snapshot is what ``GET /api/sessions/{id}/opening`` resolves for a session
    carrying a ``world_ref``, and the selected variant is also what the
    narrator reads as ``Opening scene (already established)``. The SCENARIO is
    context, never the opening (owner ruling) — it reaches the narrator through
    the ``### World`` block, and no ``narrative`` row is seeded for it any more.
    An empty greeting appends nothing special (the Opening block hides), the
    same degradation as an empty persona ``first_mes``.

    Cast card events carry NO greeting at all (see :func:`_persona_card`): a
    reused persona's ``first_mes``/``alternate_greetings`` must never surface
    in a world chat.

    ``persona_id`` is the chat's user persona (``{{user}}``); it
    is NOT seeded as a cast card (the world's cast is the scene, not the
    player). When omitted the pinned global default applies
    (:func:`fabulist.state.settings.default_persona_id`); a supplied id that
    names no known persona raises ``ValueError``, while a stale pin is skipped
    (the chat still materializes). The materialized row lands near seq 2, and a
    later explicit clear (``{"cleared": true}``) supersedes it in the
    latest-wins fold, so a cleared world chat is not re-pinned.

    Nothing else about the chat is special: the engine/roster/facts paths are
    untouched below seeding (D32).

    Raises ``ValueError`` for an unknown world or a cast member that names no
    known persona, so a chat is never seeded with a dangling roster slot.
    """
    from fabulist.personas.registry import CHAT_PERSONA_EVENT, list_personas

    world = world_by_id(log, world_id)
    if world is None:
        raise ValueError(f"unknown world: {world_id!r}")

    personas = {persona.persona_id: persona for persona in list_personas(log)}
    missing = [persona_id for persona_id in world.cast if persona_id not in personas]
    if missing:
        raise ValueError(f"unknown cast persona(s): {missing}")

    # Resolve the user persona: explicit wins, else the pinned default. An
    # explicit id must exist (dangling roster slot -> ValueError); a stale pin
    # is silently dropped so a deleted default never blocks materialization.
    explicit_persona = persona_id is not None
    if persona_id is None:
        from fabulist.state.settings import default_persona_id

        persona_id = default_persona_id(log)
    if persona_id is not None and persona_id not in personas:
        if explicit_persona:
            raise ValueError(f"unknown persona: {persona_id!r}")
        persona_id = None

    session_id = uuid.uuid4().hex
    chat_title = (title or world.name).strip()
    log.append_event(
        session_id,
        "system",
        "system",
        {"title": chat_title, "event": "session_created"},
    )
    if persona_id is not None:
        log.append_event(
            session_id,
            "system",
            USER_ACTOR,
            {"event": CHAT_PERSONA_EVENT, "persona_id": persona_id},
        )
    for persona_id in world.cast:
        persona = personas[persona_id]
        log.append_event(
            session_id,
            "system",
            "system",
            {"card": _persona_card(persona)},
        )
    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {
            "event": WORLD_REF_EVENT,
            "world_id": world.world_id,
            "name": world.name,
            "scenario": world.scenario,
            # The chat's OPENING (issue #51): the world's own greeting cycle,
            # snapshotted so a later studio edit cannot rewrite a running chat
            # (D32). ``GET .../opening`` reads it; the narrator reads the
            # selected variant as its opening-scene line.
            "first_mes": world.first_mes,
            "alternate_greetings": list(world.alternate_greetings),
        },
    )
    if world.book_ids:
        log.append_event(
            session_id,
            "system",
            USER_ACTOR,
            {"event": SESSION_BOOKS_EVENT, "book_ids": list(world.book_ids)},
        )
    return {"session_id": session_id, "title": chat_title, "world_id": world.world_id}
