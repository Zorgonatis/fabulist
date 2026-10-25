"""Book entity registry (D31): lore as log-owned entities.

PLAN.md **D31** — *Books as first-class entities*: a **Book** is a nameable,
reusable world-info library decoupled from any one card or chat. Cast lore
travels on the persona (``Persona.book_ids``), a world carries ``book_ids``, and
a chat's world-info render pools every reachable book, deduped by entry
signature. Legacy sessions keep their session-scoped books and render
byte-identically (zero migration required).

The event log owns everything (D9), so books are **system events on a reserved
anchor session** (:data:`BOOKS_ANCHOR`), folded latest-wins per ``book_id`` —
the exact shape of :mod:`fabulist.personas.registry` and the
``__assistants__`` anchor it mirrors.

Event contract (fixed, shared with the world / experience-profile lanes):

* Book definition — a ``system`` event on :data:`BOOKS_ANCHOR`::

      {"event": "book", "book_id": str, "name": str,
       "entries": [<ST world-info entry>, ...], "provenance": dict}

  ``entries`` uses the SillyTavern character-book entry shape
  (:func:`fabulist.context.world_info.parse_book` consumes it verbatim:
  ``keys``/``content``/``constant``/``insertion_order``/``case_sensitive``/
  ``comment`` plus any extra keys, which are preserved). Create and update both
  append; the newest row per ``book_id`` wins.

* Tombstone — a ``system`` event on :data:`BOOKS_ANCHOR`::

      {"event": "book_deleted", "book_id": str}

  A tombstone removes the book from the fold (append-only log, D9: nothing is
  destroyed).

Seams this builds on
--------------------
* Anchor-session hiding: :func:`_visible_session_ids` mirrors
  :func:`fabulist.api.sessions._session_ids` (and
  :func:`fabulist.personas.registry._visible_session_ids`) — reserved
  ``__``-prefixed sessions are internal, never user chats.
* Card payload shape (:func:`fabulist.cards.to_session_seed`): a card rides a
  ``system`` event's ``payload["card"]``; :func:`backfill_books` reads its
  ``character_book`` to migrate session-scoped lore into reusable entities.
* Persona fold (:mod:`fabulist.personas.registry`): :func:`backfill_books`
  attaches migrated books to the chat's persona via the same
  ``append_persona`` path (cast lore travels on reuse).

Public API
----------
:data:`BOOKS_ANCHOR`, :class:`Book`, :func:`append_book`, :func:`delete_book`,
:func:`list_books`, :func:`book_by_id`, :func:`book_to_wire`,
:func:`wire_to_book`, :func:`entry_signature`, :func:`book_signature`,
:func:`materialize_card_book`, :func:`attach_book_to_persona`,
:func:`backfill_books`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Sequence

from fabulist.context.world_info import WorldInfoEntry, parse_book

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


__all__ = [
    "BOOKS_ANCHOR",
    "BOOK_DELETED_EVENT",
    "BOOK_EVENT",
    "Book",
    "append_book",
    "attach_book_to_persona",
    "backfill_books",
    "book_by_id",
    "book_signature",
    "book_to_wire",
    "delete_book",
    "entry_signature",
    "list_books",
    "materialize_card_book",
    "mint_entry_ids",
    "wire_to_book",
]

#: Reserved session id anchoring book definition events (like
#: ``__personas__`` / ``__worlds__``). Its ``__`` prefix keeps it out of the
#: session list (:func:`fabulist.api.sessions._session_ids`).
BOOKS_ANCHOR = "__books__"

#: System-event kind carrying one book definition (per ``book_id``, latest wins).
BOOK_EVENT = "book"

#: System-event kind carrying a tombstone (removes ``book_id`` from the fold).
BOOK_DELETED_EVENT = "book_deleted"

#: The stable key every entry carries once the book has been written through a
#: keying writer (W9.23 §3.4.3). An ST entry has no identity, so without this a
#: proposal could not name ONE entry — deleting a place would mean replacing the
#: whole entry list, which is exactly the regenerate-the-blob shape the owner's
#: diff-first reversal forbids. The key is minted at WRITE time, so the first
#: write of a legacy book migrates it (no backfill, no fold change) and
#: :func:`entry_signature` / :func:`fabulist.context.world_info.parse_book`
#: ignore it, leaving dedupe and render untouched.
ENTRY_ID_KEY = "entry_id"

#: Actor stamped on human-intent appends (mirrors the personas/settings lanes).
USER_ACTOR = "user"


@dataclass(frozen=True)
class Book:
    """One book folded from the ``__books__`` anchor session.

    ``entries`` is the raw ST entry list exactly as imported (unknown keys
    preserved, D4/D31); ``provenance`` is free-form origin metadata (who created
    the book, where it came from) and defaults to ``{}``.
    """

    book_id: str
    name: str
    entries: list[dict] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)


def _text(value: Any) -> str:
    """Coerce an optional field to a plain string (``None`` -> ``""``)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _valid_definition(payload: dict) -> tuple[str, str] | None:
    """Extract ``(book_id, name)`` from a book payload, or ``None``.

    Requires the contract's two mandatory fields to be non-empty strings;
    malformed rows are skipped so the permissive ``system`` schema gate
    (``schema_gate.py``: ``"system": Schema(required={}, optional={})``) can
    never inject a broken book.
    """
    book_id = payload.get("book_id")
    name = payload.get("name")
    if not isinstance(book_id, str) or not book_id:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return book_id, name


def _book_from_payload(payload: dict) -> Book | None:
    """Build a :class:`Book` from one ``book`` payload (``None`` if bad)."""
    ids = _valid_definition(payload)
    if ids is None:
        return None
    book_id, name = ids
    entries = payload.get("entries")
    provenance = payload.get("provenance")
    return Book(
        book_id=book_id,
        name=name,
        entries=[entry for entry in entries if isinstance(entry, dict)]
        if isinstance(entries, list)
        else [],
        provenance=dict(provenance) if isinstance(provenance, dict) else {},
    )


def _entries_of(raw: Any) -> list[dict]:
    """Return the entry dicts inside any accepted book shape.

    Mirrors the shape acceptance of
    :func:`fabulist.context.world_info.parse_book` (list, ``{"entries": [...]}``,
    an ``entries`` id->entry mapping, or a raw numeric-id mapping) but keeps the
    entries *verbatim* so unknown keys survive the round trip. Callers have
    already validated the book with :func:`parse_book`, so every entry is a dict.
    """
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and "entries" in raw:
        inner = raw["entries"]
        items = list(inner.values()) if isinstance(inner, dict) else inner
    elif isinstance(raw, dict):
        items = list(raw.values())
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


# -- book definitions --------------------------------------------------------


def mint_entry_ids(entries: Any) -> list:
    """The entry list with a stable :data:`ENTRY_ID_KEY` on every entry dict.

    Non-dict items are passed through verbatim (the fold keeps whatever the
    owner's raw list held), and an entry that already carries a non-empty key
    keeps it: re-writing a book must never re-address its entries, because a
    filed proposal's path (``entries.<entry_id>``) is what makes its ``old``
    verifiable.
    """
    if not isinstance(entries, list):
        return []
    keyed: list = []
    for entry in entries:
        if isinstance(entry, dict):
            entry = dict(entry)
            existing = entry.get(ENTRY_ID_KEY)
            if not isinstance(existing, str) or not existing.strip():
                entry[ENTRY_ID_KEY] = uuid.uuid4().hex
        keyed.append(entry)
    return keyed


def append_book(log: "EventLog", book: dict) -> str:
    """Append one book definition to :data:`BOOKS_ANCHOR`; return its id.

    ``book`` carries at least ``name`` (plus any of ``entries`` /
    ``provenance``). When ``book_id`` is absent a fresh ``uuid4().hex`` is
    generated. Create and update both append (latest-wins per id); raising
    ``ValueError`` on a missing/empty name keeps a nameless book out of the
    catalog. The write is a ``system`` event, never a side-table row (D9).

    Every entry leaves keyed (:func:`mint_entry_ids`): this is the ONLY
    definition writer besides the profile importer, so routing every studio
    edit, editor save, card materialization and proposal accept through it is
    what keeps ``entries.<entry_id>`` addressing valid for every book.
    """
    data = dict(book or {})
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("book requires a non-empty 'name'")

    book_id = data.get("book_id")
    if not isinstance(book_id, str) or not book_id:
        book_id = uuid.uuid4().hex

    entries = data.get("entries")
    log.append_event(
        BOOKS_ANCHOR,
        "system",
        USER_ACTOR,
        {
            "event": BOOK_EVENT,
            "book_id": book_id,
            "name": name,
            "entries": mint_entry_ids(entries),
            "provenance": data.get("provenance")
            if isinstance(data.get("provenance"), dict)
            else {},
        },
    )
    return book_id


def delete_book(log: "EventLog", book_id: str) -> None:
    """Tombstone a book (``book_deleted``); the fold drops it.

    Non-destructive (D9): the definition rows stay queryable, the fold simply
    stops returning the id.
    """
    log.append_event(
        BOOKS_ANCHOR,
        "system",
        USER_ACTOR,
        {"event": BOOK_DELETED_EVENT, "book_id": book_id},
    )


def list_books(log: "EventLog") -> list[Book]:
    """Fold the ``__books__`` anchor session into books (latest wins).

    Iterates the anchor's raw rows in seq order
    (:meth:`EventLog.get_events`) and keeps the newest definition per
    ``book_id``; a ``book_deleted`` tombstone removes the id. Malformed rows and
    non-``system`` events are ignored. Same fold shape as
    :func:`fabulist.personas.registry.list_personas`.
    """
    folded: dict[str, Book] = {}
    for event in log.get_events(BOOKS_ANCHOR):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        kind = payload.get("event")
        if kind == BOOK_EVENT:
            book = _book_from_payload(payload)
            if book is not None:
                folded[book.book_id] = book
        elif kind == BOOK_DELETED_EVENT:
            book_id = payload.get("book_id")
            if isinstance(book_id, str) and book_id:
                folded.pop(book_id, None)
    return list(folded.values())


def book_by_id(log: "EventLog", book_id: str) -> Book | None:
    """One folded book by id, or ``None`` when absent/tombstoned."""
    for book in list_books(log):
        if book.book_id == book_id:
            return book
    return None


# -- wire conversions --------------------------------------------------------


def book_to_wire(book: Book) -> dict:
    """Wire shape of one book (the frontend/other-lane contract)."""
    return {
        "book_id": book.book_id,
        "name": book.name,
        "entries": [dict(entry) for entry in book.entries],
        "provenance": dict(book.provenance),
    }


def wire_to_book(data: dict) -> Book:
    """Parse a wire dict into a :class:`Book` (raises ``ValueError`` if bad).

    Structural only — entries are validated by
    :func:`fabulist.context.world_info.parse_book` at the API door, not here,
    so pass-through keys survive untouched.
    """
    if not isinstance(data, dict):
        raise ValueError("book must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("book requires a non-empty 'name'")
    book_id = data.get("book_id")
    if book_id is not None and (not isinstance(book_id, str) or not book_id):
        raise ValueError("book 'book_id' must be a non-empty string")
    entries = data.get("entries")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ValueError("book 'entries' must be a list")
    provenance = data.get("provenance")
    if provenance is None:
        provenance = {}
    if not isinstance(provenance, dict):
        raise ValueError("book 'provenance' must be an object")
    return Book(
        book_id=book_id or "",
        name=name,
        entries=[entry for entry in entries if isinstance(entry, dict)],
        provenance=dict(provenance),
    )


# -- canonical signatures (dedupe) -------------------------------------------


def entry_signature(entry: WorldInfoEntry) -> str:
    """Canonical sha256 signature of one parsed entry (D31 dedupe key).

    The signature covers every field :func:`parse_book` keeps, so two entries
    that render identically collapse to one line and two that differ in any
    meaningful way stay distinct. JSON is canonical (``sort_keys``,
    ``ensure_ascii=False``) so the digest is stable across processes.
    """
    canonical = json.dumps(
        {
            "keys": list(entry.keys),
            "content": entry.content,
            "constant": entry.constant,
            "order": entry.order,
            "case_sensitive": entry.case_sensitive,
            "comment": entry.comment,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def book_signature(entries: Sequence[WorldInfoEntry]) -> str:
    """Signature of a whole book (ordered list of :func:`entry_signature`).

    Order is significant: the render packs entries in ``insertion_order``, so
    two books with the same entries in a different order are different books.
    Used by :func:`backfill_books` to find-or-create a reusable entity instead
    of appending the same lore twice.
    """
    canonical = json.dumps([entry_signature(entry) for entry in entries])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- backfill ----------------------------------------------------------------


def _visible_session_ids(log: "EventLog") -> list[str]:
    """All non-anchor session ids known to the log.

    :class:`EventLog` exposes no public enumerator, so read the ``sessions``
    bookkeeping table through a short-lived secondary connection (WAL makes this
    a safe concurrent read). Mirrors
    :func:`fabulist.personas.registry._visible_session_ids` exactly.
    """
    conn = sqlite3.connect(log.path)
    try:
        rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [row[0] for row in rows if not row[0].startswith("__")]


def _source_books(events: Sequence[Any]) -> list[Any]:
    """Raw session-scoped books: card ``character_book``s + ``world_info`` books.

    Mirrors :func:`fabulist.context.facts.session_world_info_block`'s legacy
    sources exactly, so the migration sees precisely what a legacy render sees.
    """
    books: list[Any] = []
    for event in events:
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        card = payload.get("card")
        if isinstance(card, dict) and card.get("character_book"):
            books.append(card["character_book"])
        if payload.get("event") == "world_info" and payload.get("book"):
            books.append(payload["book"])
    return books


def _book_name(raw: Any, cards: Sequence[dict]) -> str:
    """Best available name for a migrated book (source name, card, fallback)."""
    if isinstance(raw, dict):
        name = raw.get("name")
        if isinstance(name, str) and name.strip():
            return name
    for card in cards:
        card_name = card.get("name")
        if isinstance(card_name, str) and card_name.strip():
            return f"{card_name} lore"
    return "Imported book"


def _persona_payload(persona: Any) -> dict:
    """Round-trip a folded :class:`Persona` back into an ``append_persona`` arg.

    Includes every field so a latest-wins update never silently drops data the
    parallel persona lane added.
    """
    return {
        "persona_id": persona.persona_id,
        "name": persona.name,
        "description": persona.description,
        "personality": persona.personality,
        "scenario": persona.scenario,
        "first_mes": persona.first_mes,
        "book_ids": list(persona.book_ids),
        "alternate_greetings": list(persona.alternate_greetings),
        "extensions": dict(persona.extensions),
        "provenance": dict(persona.provenance),
    }


# -- card import materialization ---------------------------------------------


def materialize_card_book(log: "EventLog", card: dict) -> str | None:
    """Find-or-create a Book entity from a card's ``character_book`` (D31).

    The card-import fix: a card's ``character_book`` must become a reusable
    :class:`Book` in the ``__books__`` catalog, not just a raw dict on the
    session event. Returns the book id, or ``None`` when the card carries no
    usable ``character_book`` (the clean no-op a bookless card needs).

    Entries are stored verbatim via :func:`_entries_of` — unknown SillyTavern
    keys (``vectorized`` / ``probability`` / ``sticky`` / ...) are preserved,
    never interpreted. Dedupe uses :func:`book_signature` (the parsed-entry
    signature :func:`backfill_books` uses), so re-importing a card finds the
    existing book instead of appending a duplicate. The name comes from
    ``character_book.name``, else ``f"{card name} lore"`` (:func:`_book_name`).
    """
    if not isinstance(card, dict):
        return None
    raw = card.get("character_book")
    if not isinstance(raw, dict):
        return None
    try:
        entries = parse_book(raw)
    except Exception:  # noqa: BLE001 - unparseable lore must not fail the import
        return None
    if not entries:
        return None

    signature = book_signature(entries)
    for book in list_books(log):
        try:
            existing = book_signature(parse_book(book.entries))
        except Exception:  # noqa: BLE001 - a malformed catalog row is skipped
            continue
        if existing == signature:
            return book.book_id

    return append_book(
        log,
        {
            "name": _book_name(raw, [card]),
            "entries": _entries_of(raw),
            "provenance": {"origin": "card_import"},
        },
    )


def attach_book_to_persona(log: "EventLog", persona_id: str, book_id: str) -> bool:
    """Append a latest-wins persona update adding ``book_id`` to ``book_ids``.

    Round-trips the folded persona through :func:`_persona_payload` (the same
    full-field shape :func:`backfill_books` writes) so the append never drops
    the persona's other D31 carry-overs. Idempotent: appends nothing and
    returns ``False`` when the persona already carries ``book_id`` or is
    unknown/tombstoned.
    """
    from fabulist.personas.registry import append_persona, list_personas

    for persona in list_personas(log):
        if persona.persona_id != persona_id:
            continue
        if book_id in persona.book_ids:
            return False
        updated = replace(persona, book_ids=[*persona.book_ids, book_id])
        append_persona(log, _persona_payload(updated))
        return True
    return False


def backfill_books(log: "EventLog", *, dry_run: bool = False) -> dict:
    """Migrate session-scoped lore into reusable Book entities (D31).

    For every visible non-anchor session, extract its session-scoped books (a
    card's ``character_book`` and any ``world_info`` events — the same sources
    :func:`fabulist.context.facts.session_world_info_block` reads), then
    **find-or-create** a :class:`Book` per source, deduped by
    :func:`book_signature`. Each found/created book is attached to the chat's
    backfilled persona (``Persona.book_ids``); a sandbox chat (no persona) keeps
    its lore session-scoped and is counted but not modified.

    ``dry_run=True`` performs the same detection without appending anything.
    The run is **idempotent**: a second pass finds the books by signature and
    skips the persona update (the id is already present), so it appends nothing.

    Returns ``{"sessions", "books_created", "attached", "left_session_scoped"}``:

    * ``sessions`` — visible sessions carrying at least one source book.
    * ``books_created`` — new book definitions appended during this call.
    * ``attached`` — persona book attachments newly appended.
    * ``left_session_scoped`` — source books on sandbox chats (no persona).
    """
    # Lazy imports: keep this module import-light for the facts render path and
    # mirror the personas registry's lazy roster import.
    from fabulist.context.roster import extract_cards
    from fabulist.personas.registry import (
        append_persona,
        list_personas,
        persona_for_chat,
    )

    by_signature: dict[str, str] = {}
    for book in list_books(log):
        try:
            by_signature[book_signature(parse_book(book.entries))] = book.book_id
        except Exception:  # noqa: BLE001 - a malformed catalog row is not fatal
            continue

    personas = {persona.persona_id: persona for persona in list_personas(log)}

    sessions = 0
    books_created = 0
    attached = 0
    left_session_scoped = 0

    for session_id in _visible_session_ids(log):
        events = log.get_events(session_id)
        sources = _source_books(events)
        if not sources:
            continue
        sessions += 1
        cards = extract_cards(events)
        persona_id = persona_for_chat(log, session_id)

        for raw in sources:
            try:
                entries = parse_book(raw)
            except Exception:  # noqa: BLE001 - skip unparseable legacy lore
                continue
            if not entries:
                continue
            signature = book_signature(entries)
            book_id = by_signature.get(signature)
            if book_id is None:
                # Dry-run fabricates a stable placeholder id so the counts and
                # the signature index behave exactly as the real run would.
                book_id = (
                    f"<new:{signature[:8]}>"
                    if dry_run
                    else append_book(
                        log,
                        {
                            "name": _book_name(raw, cards),
                            "entries": _entries_of(raw),
                            "provenance": {"origin": "backfill"},
                        },
                    )
                )
                by_signature[signature] = book_id
                books_created += 1

            if persona_id is None:
                left_session_scoped += 1
                continue

            persona = personas.get(persona_id)
            if persona is None or book_id in persona.book_ids:
                continue

            updated = replace(persona, book_ids=[*persona.book_ids, book_id])
            if not dry_run:
                append_persona(log, _persona_payload(updated))
            # Track the attachment locally so a later session sharing the same
            # persona neither double-counts nor re-appends it this run.
            personas[persona_id] = updated
            attached += 1

    return {
        "sessions": sessions,
        "books_created": books_created,
        "attached": attached,
        "left_session_scoped": left_session_scoped,
    }
