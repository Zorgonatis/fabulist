"""Persona registry (D16): personas as log-owned entities.

PLAN.md **D16** — *persona = parent of chat; memory fences to the persona*:
a chat carries a parent persona, and new chats under the same persona recall
earlier storylines. The event log owns everything (D9), so personas are not a
side table: they are **system events on a reserved anchor session**
(``__personas__``), folded latest-wins per ``persona_id``. A chat's assignment
is a **system event on the chat session** (``{event: "chat_persona",
persona_id}``), also latest-wins.

Event contract (fixed, shared with the memory-scope / chat-config lanes):

* Persona definition — a ``system`` event on :data:`PERSONAS_ANCHOR`::

      {"event": "persona", "persona_id": str, "name": str,
       "description": str, "personality": str, "scenario": str,
       "first_mes": str, "book_ids": [str],
       "alternate_greetings": [str], "extensions": dict,
       "provenance": dict, "image_asset": str | None}

  Create and update both append; the newest row per ``persona_id`` wins
  (append-only log, D9/D10). Everything but ``persona_id`` / ``name`` is
  optional (defaults to ``""``).

  D31 extends a persona with the fields that let cast lore travel on reuse:
  ``book_ids`` (references into the ``__books__`` catalog), plus the
  full-fidelity card carry-overs ``alternate_greetings`` / ``extensions`` /
  ``provenance`` (``{creator, character_version, origin, imported_at}``). All
  four are optional and default empty, so OLD logs fold unchanged.

* Persona tombstone (D39) — a ``system`` event on :data:`PERSONAS_ANCHOR`::

      {"event": "persona_deleted", "persona_id": str}

  Removes ``persona_id`` from the fold (append-only: the definition rows stay
  queryable, only the derived catalog changes). A later ``persona`` append for
  the same id resurrects it, latest-wins.

* Chat -> persona assignment — a ``system`` event on the chat session::

      {"event": "chat_persona", "persona_id": str | null}

  Latest event wins per session. ``null`` / absent means a *sandbox* chat
  (D16; note D53 keeps ``chat_persona`` a USER-persona record, not a memory
  fence). ``persona_id: null`` may carry ``{"cleared": true}`` — the explicit
  "no persona" marker that suppresses the pinned-default fallback
  (:func:`clear_persona` / :func:`chat_persona_state`).

* Per-chat memory opt-out (read-only here) — a ``system`` event on the chat::

      {"event": "chat_memory", "enabled": bool}

  Latest boolean wins; absent means ``True``. This module only *reads* it
  (:func:`chat_config_for`); the chat-config lane owns the write
  (:mod:`fabulist.api.chat_config_api`, ``PATCH .../memory``), and the memory
  lane's :func:`fabulist.memory.scope.resolve_scope` folds the same row.

Seams this builds on
--------------------
* ``assistants_api`` ``__assistants__`` anchor pattern
  (:mod:`fabulist.api.assistants_api`, ``_raw_definition_lists``): definitions
  live on a reserved double-underscore session and are read by folding the raw
  per-session audit view (:meth:`fabulist.log.EventLog.get_events`) in seq
  order — the newest event wins.
* Anchor-session hiding: :func:`fabulist.api.sessions._session_ids` lists the
  ``sessions`` bookkeeping table and drops ids whose prefix is ``__``
  (``row[0].startswith("__")``). :func:`_visible_session_ids` mirrors that
  exact filter so backfill never treats an anchor as a user chat.
* Card payload shape (:func:`fabulist.cards.to_session_seed` /
  ``cards_api._seed_session``): a card rides a ``system`` event's
  ``payload["card"]`` (see :func:`fabulist.context.roster.extract_cards`),
  which :func:`backfill_from_cards` reads to migrate existing chats.
* ``request.app.state.log`` router seam (:mod:`fabulist.api.app` stores the
  live :class:`~fabulist.log.EventLog` there); :mod:`fabulist.personas` stays
  API-free, so this module has no dependency direction on ``fabulist.api``.

Public API
----------
``PERSONAS_ANCHOR``, :class:`Persona`, :func:`append_persona`,
:func:`delete_persona`, :func:`list_personas`, :func:`persona_for_chat`,
:func:`assign_persona`, :func:`chat_config_for`, :func:`backfill_from_cards`,
:func:`upsert_persona_from_card`.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


__all__ = [
    "CHAT_MEMORY_EVENT",
    "CHAT_PERSONA_CLEARED_KEY",
    "CHAT_PERSONA_EVENT",
    "PERSONA_DELETED_EVENT",
    "PERSONA_EVENT",
    "PERSONAS_ANCHOR",
    "Persona",
    "append_persona",
    "assigned_persona_id",
    "assign_persona",
    "backfill_from_cards",
    "chat_config_for",
    "chat_persona_state",
    "clear_persona",
    "delete_persona",
    "list_personas",
    "persona_for_chat",
    "set_persona_image",
    "upsert_persona_from_card",
]

#: Reserved session id anchoring persona definition events (like
#: ``__assistants__`` / ``__settings__``). Its ``__`` prefix keeps it out of the
#: session list (:func:`fabulist.api.sessions._session_ids`).
PERSONAS_ANCHOR = "__personas__"

#: System-event kind carrying one persona definition (per ``persona_id``,
#: latest wins).
PERSONA_EVENT = "persona"

#: System-event kind carrying a tombstone (removes ``persona_id`` from the
#: fold; D39, append-only). Mirrors ``worlds.registry.WORLD_DELETED_EVENT``.
PERSONA_DELETED_EVENT = "persona_deleted"

#: System-event kind carrying a chat's persona assignment (on the chat session,
#: latest wins). ``persona_id: null`` clears it back to a sandbox chat.
CHAT_PERSONA_EVENT = "chat_persona"

#: Payload key on a ``chat_persona`` event marking an **explicit** "no persona"
#: clear (``{"event": "chat_persona", "persona_id": null, "cleared": true}``).
#: It lets the resolution folds tell a deliberate clear (do NOT inherit the
#: pinned default) from a chat that never recorded a choice (inherit the pin).
#: The row keeps ``persona_id: null`` so every existing null-aware reader
#: (chat-config, memory scope, the session-list fold) stays byte-identical.
CHAT_PERSONA_CLEARED_KEY = "cleared"

#: System-event kind carrying a chat's memory toggle (read-only here; written
#: by the chat-config lane — see module docstring).
CHAT_MEMORY_EVENT = "chat_memory"

#: Actor stamped on human/persona appends. Mirrors :mod:`fabulist.api.sessions`
#: / :mod:`fabulist.api.settings` user-intent appends (``"user"``).
USER_ACTOR = "user"


@dataclass(frozen=True)
class Persona:
    """One persona definition folded from the ``__personas__`` anchor session.

    ``description`` / ``personality`` / ``scenario`` / ``first_mes`` mirror the
    Character Card V2 fields (:class:`fabulist.cards.CardV2`) and default to
    ``""`` when a definition omits them.

    D31 additions (all default empty, so old logs fold identically):
    ``book_ids`` — reusable world-info books this cast member carries;
    ``alternate_greetings`` / ``extensions`` / ``provenance`` — the card's
    full-fidelity carry-overs (``provenance`` is ``{creator,
    character_version, origin, imported_at}``).

    ``image_asset`` is the content-addressed id of a **sessionless** definition
    image stored on :data:`fabulist.media.images.MEDIA_ANCHOR` (``None`` when
    the persona has no image); it defaults to ``None`` so old logs fold
    identically.
    """

    persona_id: str
    name: str
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    book_ids: list[str] = field(default_factory=list)
    alternate_greetings: list[str] = field(default_factory=list)
    extensions: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    image_asset: str | None = None


def _text(value: Any) -> str:
    """Coerce an optional persona field to a plain string (``None`` -> ``""``)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _optional_str(value: Any) -> str | None:
    """Coerce an optional single id/ref to ``str | None`` (empty -> ``None``)."""
    return value if isinstance(value, str) and value else None


def _str_list(value: Any) -> list[str]:
    """Coerce an optional list field to ``list[str]`` (missing -> ``[]``).

    Tolerant like :func:`fabulist.cards.v2._str_list`: a non-list folds to
    ``[]`` rather than rejecting the whole persona, and non-string members are
    stringified so a malformed import cannot crash the anchor fold (the
    permissive ``system`` schema gate guarantees no type here).
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [item if isinstance(item, str) else str(item) for item in value]


def _mapping(value: Any) -> dict:
    """Coerce an optional mapping field to ``dict`` (non-object -> ``{}``)."""
    return dict(value) if isinstance(value, dict) else {}


def _valid_definition(payload: dict) -> tuple[str, str] | None:
    """Extract ``(persona_id, name)`` from a persona payload, or ``None``.

    Requires the contract's two mandatory fields to be non-empty strings;
    malformed rows are skipped so the permissive ``system`` schema gate
    (``schema_gate.py``: ``"system": Schema(required={}, optional={})``) can
    never inject a broken persona.
    """
    persona_id = payload.get("persona_id")
    name = payload.get("name")
    if not isinstance(persona_id, str) or not persona_id:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return persona_id, name


def _persona_from_payload(payload: dict) -> Persona | None:
    """Build a :class:`Persona` from one ``persona`` payload (``None`` if bad)."""
    ids = _valid_definition(payload)
    if ids is None:
        return None
    persona_id, name = ids
    return Persona(
        persona_id=persona_id,
        name=name,
        description=_text(payload.get("description")),
        personality=_text(payload.get("personality")),
        scenario=_text(payload.get("scenario")),
        first_mes=_text(payload.get("first_mes")),
        book_ids=_str_list(payload.get("book_ids")),
        alternate_greetings=_str_list(payload.get("alternate_greetings")),
        extensions=_mapping(payload.get("extensions")),
        provenance=_mapping(payload.get("provenance")),
        image_asset=_optional_str(payload.get("image_asset")),
    )


# -- persona definitions -----------------------------------------------------


def append_persona(log: "EventLog", persona: dict) -> str:
    """Append one persona definition to :data:`PERSONAS_ANCHOR`; return its id.

    ``persona`` carries at least ``name`` (plus any of ``description`` /
    ``personality`` / ``scenario`` / ``first_mes`` / ``book_ids`` /
    ``alternate_greetings`` / ``extensions`` / ``provenance``). When ``persona_id`` is
    absent a fresh ``uuid4().hex`` is generated. Create and update both append
    (latest-wins per id); raising ``ValueError`` on a missing/empty name keeps
    a nameless persona out of the catalog. The write is a ``system`` event
    (actor ``user``), never a side-table row (D9).
    """
    data = dict(persona or {})
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("persona requires a non-empty 'name'")

    persona_id = data.get("persona_id")
    if not isinstance(persona_id, str) or not persona_id:
        persona_id = uuid.uuid4().hex

    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        USER_ACTOR,
        {
            "event": PERSONA_EVENT,
            "persona_id": persona_id,
            "name": name,
            "description": _text(data.get("description")),
            "personality": _text(data.get("personality")),
            "scenario": _text(data.get("scenario")),
            "first_mes": _text(data.get("first_mes")),
            # D31 carry-overs; defaults keep old/new minimal appends identical.
            "book_ids": _str_list(data.get("book_ids")),
            "alternate_greetings": _str_list(data.get("alternate_greetings")),
            "extensions": _mapping(data.get("extensions")),
            "provenance": _mapping(data.get("provenance")),
            # Sessionless definition image ref (anchor asset id), or ``None``.
            "image_asset": _optional_str(data.get("image_asset")),
        },
    )
    return persona_id


def set_persona_image(
    log: "EventLog", persona_id: str, image_asset: str | None
) -> Persona | None:
    """Set a persona's ``image_asset`` by re-appending it (latest-wins).

    Append-only (D9): the whole definition is re-appended with ``image_asset``
    set, so the newest row wins and the prior rows stay queryable. Returns the
    updated :class:`Persona`, or ``None`` when ``persona_id`` is unknown.
    """
    target = next(
        (p for p in list_personas(log) if p.persona_id == persona_id), None
    )
    if target is None:
        return None
    append_persona(
        log,
        {
            "persona_id": target.persona_id,
            "name": target.name,
            "description": target.description,
            "personality": target.personality,
            "scenario": target.scenario,
            "first_mes": target.first_mes,
            "book_ids": list(target.book_ids),
            "alternate_greetings": list(target.alternate_greetings),
            "extensions": dict(target.extensions),
            "provenance": dict(target.provenance),
            "image_asset": image_asset,
        },
    )
    return next(
        (p for p in list_personas(log) if p.persona_id == persona_id), None
    )


def delete_persona(log: "EventLog", persona_id: str) -> None:
    """Tombstone a persona (``persona_deleted``); the fold drops it.

    Non-destructive (D9/D10): the definition rows stay queryable, only the
    derived catalog changes. A later :func:`append_persona` for the same
    ``persona_id`` resurrects it (latest-wins).
    """
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        USER_ACTOR,
        {"event": PERSONA_DELETED_EVENT, "persona_id": persona_id},
    )


def list_personas(log: "EventLog") -> list[Persona]:
    """Fold the ``__personas__`` anchor session into personas (latest wins).

    Iterates the anchor's raw rows in seq order
    (:meth:`EventLog.get_events`) and keeps the newest definition per
    ``persona_id``, preserving first-seen order. A ``persona_deleted``
    tombstone removes the id from the fold. Malformed rows and non-``system``
    events are ignored. This is the same fold shape as
    ``assistants_api._raw_definition_lists`` / ``worlds.registry.list_worlds``
    — the reserved anchor session is the sole source of definitions.
    """
    folded: dict[str, Persona] = {}
    for event in log.get_events(PERSONAS_ANCHOR):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        kind = payload.get("event")
        if kind == PERSONA_EVENT:
            persona = _persona_from_payload(payload)
            if persona is None:
                continue
            # Reassignment keeps the original insertion position (dict order).
            folded[persona.persona_id] = persona
        elif kind == PERSONA_DELETED_EVENT:
            persona_id = payload.get("persona_id")
            if isinstance(persona_id, str) and persona_id:
                folded.pop(persona_id, None)
    return list(folded.values())


# -- chat assignment ---------------------------------------------------------


def chat_persona_state(
    log: "EventLog", session_id: str
) -> tuple[bool, str | None]:
    """The chat's persona **selection state**: ``(explicit, persona_id)``.

    ``explicit`` is ``True`` when the chat's own log says something about its
    persona — a real ``chat_persona`` assignment OR an explicit clear
    (``persona_id: null`` / ``{"cleared": true}``). ``persona_id`` is the latest
    assigned id, or ``None`` when the latest row is an explicit clear.

    ``explicit is False`` means the chat has **never** recorded a persona
    choice, so readers may fall back to the pinned global default. That is the
    distinction :func:`persona_for_chat` needs: a cleared chat stays persona-
    less even with a pin set, while a never-set chat still inherits the pin.

    Fork-aware: reads the inherited base chain
    (:meth:`EventLog.get_chain_events`) so a branch inherits its parent's last
    assignment/clear; a branch-local row (higher seq) still wins. Latest-wins
    across BOTH a real assignment and a clear, so "assign -> clear -> reassign"
    folds in written order.
    """
    explicit = False
    persona_id: str | None = None
    for event in log.get_chain_events(session_id):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != CHAT_PERSONA_EVENT:
            continue
        if payload.get(CHAT_PERSONA_CLEARED_KEY) is True:
            explicit, persona_id = True, None
            continue
        if "persona_id" not in payload:
            continue
        value = payload.get("persona_id")
        if isinstance(value, str) and value:
            explicit, persona_id = True, value
        else:
            # A bare ``persona_id: null`` (legacy clear rows) is still an
            # explicit statement: "no persona", not "never set".
            explicit, persona_id = True, None
    return explicit, persona_id


def assigned_persona_id(log: "EventLog", session_id: str) -> str | None:
    """The chat's **own** latest ``chat_persona`` value; ``None`` = unassigned.

    Folds the session's own ``system`` events in seq order (latest wins). A row
    carrying a non-``str`` / empty ``persona_id`` (an explicit ``null``
    clearing the assignment) folds to ``None``. This is the RAW id; for the
    explicit-vs-never-set distinction use :func:`chat_persona_state`. Mirrors
    ``chat_config_api._persona_id`` and ``memory.scope.resolve_scope``.

    Fork-aware (bug-wave fix): reads the inherited base chain
    (:meth:`EventLog.get_chain_events`) so a branch inherits its parent's
    assignment; a branch-local ``chat_persona`` (higher seq) still wins.

    This is the raw assignment — it never consults the pinned global default
    (contrast :func:`persona_for_chat`); callers that need "what is on this
    chat's own log" (not "what persona it effectively resolves to") use this.
    """
    persona_id: str | None = None
    for event in log.get_chain_events(session_id):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != CHAT_PERSONA_EVENT:
            continue
        if "persona_id" not in payload:
            continue
        value = payload.get("persona_id")
        persona_id = value if isinstance(value, str) and value else None
    return persona_id


def _default_persona_id(log: "EventLog") -> str | None:
    """The pinned global default persona, or ``None`` when unpinned.

    Read from the reserved ``__settings__`` anchor (the pin is a settings
    event). Imported lazily to keep the module import-light; the fold itself
    lives in :func:`fabulist.state.settings.default_persona_id` (a neutral
    module, so this registry never reaches into the API layer).
    """
    from fabulist.state.settings import default_persona_id

    return default_persona_id(log)


def persona_for_chat(log: "EventLog", session_id: str) -> str | None:
    """The chat's **effective** persona: its own assignment, else the pinned default.

    Reads the chat's own selection state
    (:func:`chat_persona_state`, chain-aware). An explicit assignment wins; an
    **explicit clear** is authoritative and returns ``None`` (no fallback).
    Only a chat that never recorded a choice (``explicit is False``) falls back
    to the global default persona pin
    (:func:`fabulist.state.settings.default_persona_id`). This is the
    "runtime selection changes the context sent" behaviour: every reader
    (``{{user}}``/``{{persona}}``, the persona's books, the memory fence)
    resolves through here, so pinning a default gives a never-set persona-less
    chat a parent persona with no per-chat edit — while a deliberate clear
    keeps the chat local.

    Returns ``None`` for a true sandbox chat: an explicit clear, or no
    assignment and no pin.
    """
    explicit, assigned = chat_persona_state(log, session_id)
    if explicit:
        return assigned
    return _default_persona_id(log)


def assign_persona(log: "EventLog", session_id: str, persona_id: str) -> None:
    """Assign ``persona_id`` to a chat by appending a ``chat_persona`` event.

    Validates the persona exists in the anchor catalog first; unknown ids raise
    ``ValueError`` (the router maps this to a 404) so the log never records a
    dangling assignment. The event lands on the chat session itself
    (``{event: "chat_persona", persona_id}``), latest-wins.
    """
    known = {persona.persona_id for persona in list_personas(log)}
    if persona_id not in known:
        raise ValueError(f"unknown persona: {persona_id!r}")
    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {"event": CHAT_PERSONA_EVENT, "persona_id": persona_id},
    )


def clear_persona(log: "EventLog", session_id: str) -> None:
    """Explicitly clear a chat's persona (append a ``chat_persona`` clear).

    The payload keeps ``persona_id: null`` (every existing null-aware reader —
    chat-config, memory scope, the session-list fold — stays byte-identical)
    and adds ``{"cleared": true}`` so the resolution folds can tell a
    deliberate "no persona" (do not inherit the pin) from "never set" (inherit
    the pin). Append-only (D9): prior assignments stay queryable. The clear
    rides the same event kind a real assignment uses, so a fork's
    ``FORK_CONFIG_EVENTS`` carry keeps it on the branch.
    """
    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {
            "event": CHAT_PERSONA_EVENT,
            "persona_id": None,
            CHAT_PERSONA_CLEARED_KEY: True,
        },
    )


def _memory_enabled(log: "EventLog", session_id: str) -> bool:
    """Fold ``chat_memory`` system events latest-wins; absent -> ``True``.

    Only boolean ``enabled`` values are honored (a malformed row cannot flip
    the toggle). Same read as ``chat_config_api._memory_enabled``.
    """
    enabled = True
    for event in log.get_events(session_id):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict) or payload.get("event") != CHAT_MEMORY_EVENT:
            continue
        value = payload.get("enabled")
        if isinstance(value, bool):
            enabled = value
    return enabled


def chat_config_for(log: "EventLog", session_id: str) -> dict:
    """Return a chat's derived config: ``{persona_id, memory_enabled}``.

    ``persona_id`` is the chat's effective persona (:func:`persona_for_chat`):
    its own assignment, else the pinned global default, else ``None`` for a
    true sandbox chat. ``memory_enabled`` is the latest ``chat_memory``
    boolean, defaulting to ``True`` (the chat-config lane's contract — written
    via ``PATCH /api/sessions/{sid}/memory``).
    """
    return {
        "persona_id": persona_for_chat(log, session_id),
        "memory_enabled": _memory_enabled(log, session_id),
    }


# -- backfill ----------------------------------------------------------------


def _visible_session_ids(log: "EventLog") -> list[str]:
    """All non-anchor session ids known to the log.

    :class:`EventLog` exposes no public enumerator, so read the ``sessions``
    bookkeeping table (written by ``_ensure_session`` on a session's first
    event) through a short-lived secondary connection — WAL makes this a safe
    concurrent read. Reserved ids (``__`` prefix: ``__settings__``,
    ``__assistants__``, ``__personas__``, ...) are internal, never user chats.
    Mirrors :func:`fabulist.api.sessions._session_ids` exactly.
    """
    conn = sqlite3.connect(log.path)
    try:
        rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [row[0] for row in rows if not row[0].startswith("__")]


def _persona_from_card(card: dict) -> dict:
    """Shape one card dict into an :func:`append_persona` argument.

    Mirrors the Character Card V2 fields: ``name`` / ``description`` /
    ``personality`` / ``scenario`` / ``first_mes``, and forwards the card's
    ``extensions`` (D31/D36) so a hosted ``extensions.fabulist.persona``
    schema rides the persona event into the log (missing -> ``{}``).
    """
    return {
        "name": str(card.get("name") or ""),
        "description": _text(card.get("description")),
        "personality": _text(card.get("personality")),
        "scenario": _text(card.get("scenario")),
        "first_mes": _text(card.get("first_mes")),
        "extensions": _mapping(card.get("extensions")),
    }


def upsert_persona_from_card(log: "EventLog", card: dict) -> str:
    """Capture a card as a persona definition, keyed by NAME (D39 upsert).

    Builds the :func:`_persona_from_card` payload and, when a persona with the
    same ``name`` already exists in :func:`list_personas`, reuses that
    ``persona_id`` so the append is an *update* (latest-wins per id); otherwise
    ``persona_id`` is left absent and :func:`append_persona` mints a fresh one.
    Mirrors :func:`backfill_from_cards`'s name-keyed fold, so re-importing the
    same card never leaves a duplicate persona. Returns the persona id.
    """
    payload = _persona_from_card(card)
    name = payload["name"]
    for persona in list_personas(log):
        # First definition of a given name wins as the update target — the same
        # convention backfill_from_cards uses to find its migration target.
        if name and persona.name == name:
            payload["persona_id"] = persona.persona_id
            break
    return append_persona(log, payload)


def backfill_from_cards(log: "EventLog", *, dry_run: bool = False) -> dict:
    """Migrate existing chats to D16 personas by their imported card.

    For every visible non-anchor session carrying card events
    (``payload["card"]`` — the shape :func:`fabulist.cards.to_session_seed`
    writes and :func:`fabulist.context.roster.extract_cards` reads), find a
    persona with the card's ``name`` or create one, then assign it via a
    ``chat_persona`` event. Sessions already assigned are skipped, so the run is
    **idempotent**.

    ``dry_run=True`` performs the same detection but appends nothing (the CLI's
    ``--dry-run``).

    Returns ``{"assigned", "sessions", "personas_created"}``:

    * ``sessions`` — visible non-anchor sessions that carry at least one card
      (candidates examined, whether or not they were already assigned).
    * ``assigned`` — candidates newly assigned during this call.
    * ``personas_created`` — new persona definitions appended during this call.
    """
    # Local imports: keep the module import-light and avoid any layered import
    # surprise (registry is imported lazily by the app factory too).
    from fabulist.context.roster import extract_cards

    by_name: dict[str, str] = {}
    for persona in list_personas(log):
        # First definition of a given name wins as the migration target.
        by_name.setdefault(persona.name, persona.persona_id)

    assigned = 0
    sessions = 0
    personas_created = 0

    for session_id in _visible_session_ids(log):
        cards = extract_cards(log.get_events(session_id))
        if not cards:
            continue
        sessions += 1

        # Idempotent: an already-assigned chat is never re-pointed. Read the
        # RAW assignment (not the effective persona): a pinned global default
        # must not make every persona-less candidate look "already assigned".
        if assigned_persona_id(log, session_id) is not None:
            continue

        card = cards[0]
        name = str(card.get("name") or "").strip()
        if not name:
            continue

        persona_id = by_name.get(name)
        if persona_id is None:
            # Dry-run fabricates a stable placeholder id so the counts and the
            # name index behave exactly as the real run would.
            persona_id = (
                f"<new:{name}>"
                if dry_run
                else append_persona(log, _persona_from_card(card))
            )
            by_name[name] = persona_id
            personas_created += 1

        if not dry_run:
            log.append_event(
                session_id,
                "system",
                USER_ACTOR,
                {"event": CHAT_PERSONA_EVENT, "persona_id": persona_id},
            )
        assigned += 1

    return {
        "assigned": assigned,
        "sessions": sessions,
        "personas_created": personas_created,
    }
