"""The chat's ACTOR record (D53) — cast personas, kept apart from the user persona.

Why this module exists
----------------------
PLAN.md **D53** (owner-locked 2026-10-13). The owner ruling is that the **user
persona** and a **cast persona** are *different roles of one entity* — "a
persona definition is ONE object selected for different tasks ... the user is a
reuse of any actor". A chat therefore keeps two separate records:

* ``chat_persona`` (owned by :mod:`fabulist.personas.registry`) — the chat's
  **user persona**, what ``{{user}}`` reads (D43). It remains exactly what it
  was; only its *fencing* meaning is gone.
* ``chat_actor`` (**this module**) — the chat's **cast / actor(s)**: the
  personas the chat is *with*. This is the record memory now fences on.

So this module exists because memory moves to the layer it is USED (world /
actor / chat). The memory fence reads this actor record **instead of**
``chat_persona``; the two must never be conflated again.

Event contract
--------------
A chat records its actors with an append-only ``system`` event on the **chat
session**::

    {"event": "chat_actor", "actor_ids": [persona_id, ...]}

* **Latest-wins** across the fork-aware chain
  (:meth:`fabulist.log.EventLog.get_chain_events`): a branch inherits its
  parent's actor record and a branch-local row (higher seq) supersedes it.
* An explicit **empty list clears** the record (``[]`` -> no actors).
* Non-``system`` rows, malformed payloads and non-list / non-string values are
  ignored (the ``system`` schema gate is permissive by design — D4).

There is deliberately **no event backfill**. A chat that never recorded a
``chat_actor`` event derives its actors from its own card events by persona
name — :func:`actors_from_cards` (D39 catalog) — and a world chat derives from
``world_ref`` -> ``world.cast`` (resolved by the sibling scope lane). The three
chat-creation write-sites are wired elsewhere (orchestrator-owned); nothing in
this module appends at creation.

This module is a pure, log-derived projection plus exactly one explicit append
helper: it never mutates history (D9/D10).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "CHAT_ACTOR_EVENT",
    "actors_from_cards",
    "append_chat_actor",
    "fold_actors",
]

#: System-event kind carrying a chat's actor(s) — the cast layer, on the chat
#: session, latest-wins (``[]`` clears). Deliberately separate from
#: ``chat_persona``, which is the *user*-persona record (D53).
CHAT_ACTOR_EVENT = "chat_actor"

#: Actor stamped on the actor-record append. Mirrors the other user-intent
#: config appends (``chat_persona`` / ``world_ref`` / ``session_books`` in
#: :mod:`fabulist.worlds.registry`), which all stamp ``"user"``. Kept a local
#: literal so this module never imports the registry at import time (the
#: registry is imported lazily inside :func:`actors_from_cards` to dodge the
#: personas <-> memory import cycle).
_USER_ACTOR = "user"


def fold_actors(log: "EventLog", session_id: str) -> tuple[str, ...]:
    """Fold ``session_id``'s ``chat_actor`` record; latest wins, ``[]`` clears.

    Reads the session's **fork-aware** chain
    (:meth:`fabulist.log.EventLog.get_chain_events`) in ``seq`` order: a branch
    inherits its parent's actor record, and a branch-local row (higher seq)
    supersedes it. The result is the actors the chat is *with* — the D53 cast
    layer the memory fence reads **instead of** ``chat_persona`` (the user
    persona).

    A payload whose ``actor_ids`` is not a list is ignored (the previous record
    stays in force); within a list, non-string and empty-string members are
    dropped. An explicit empty list clears the record back to ``()``. Duplicate
    ids collapse while preserving order (a well-formed append is already
    deduped, so this only hardens against malformed rows). This is a pure
    projection: it never writes.
    """
    actors: tuple[str, ...] = ()
    for event in log.get_chain_events(session_id):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != CHAT_ACTOR_EVENT:
            continue
        raw = payload.get("actor_ids")
        if not isinstance(raw, list):
            # Defensive: a malformed payload is ignored — it never silently
            # clears a previous, well-formed record.
            continue
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in raw:
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                cleaned.append(value)
        actors = tuple(cleaned)
    return actors


def append_chat_actor(
    log: "EventLog", session_id: str, actor_ids: Sequence[str]
) -> None:
    """Record the chat's actors — one deduped ``chat_actor`` event per call.

    Appends exactly ONE ``system`` event
    ``{"event": "chat_actor", "actor_ids": [...]}`` on the chat session, with
    the ids deduped while preserving first-seen order. Non-string / empty
    members are dropped (defensive — the append door must never write garbage
    that a later fence could key on). An empty ``actor_ids`` writes ``[]``,
    which :func:`fold_actors` reads as an explicit clear.

    This is the *only* writer here. Chat-creation call sites are wired by the
    orchestrator (this module deliberately appends nothing at creation) and the
    write never touches ``chat_persona`` — the user persona is a separate record
    (D53).
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in actor_ids:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            cleaned.append(value)
    log.append_event(
        session_id,
        "system",
        _USER_ACTOR,
        {"event": CHAT_ACTOR_EVENT, "actor_ids": cleaned},
    )


def actors_from_cards(log: "EventLog", events: Sequence) -> tuple[str, ...]:
    """Derive a chat's actors from its card events (LEGACY, D53/D39).

    For a chat that never recorded a ``chat_actor`` event, its actors are the
    personas its own card events were captured as (D39: importing/attaching a
    card also writes a persona definition keyed by **name**). Each card's name
    is resolved against the ``__personas__`` catalog
    (:func:`fabulist.personas.registry.list_personas`); the matching persona
    ids are returned deduped in card order.

    A name that matches **nothing**, or matches **more than one** persona, is
    **skipped** — this function never raises and never guesses, so a caller can
    safely fall back to the chat layer. Malformed cards and non-``system``
    events are ignored (via :func:`fabulist.context.roster.extract_cards`).

    Why a separate record: the card a chat was seeded from is the cast, not the
    user — D53 rules that user persona and cast persona are different roles, and
    this is the legacy bridge for chats recorded before the ``chat_actor`` event
    existed (no backfill). Imports are function-local to keep the memory package
    free of an import cycle on :mod:`fabulist.personas.registry`.
    """
    from fabulist.context.roster import extract_cards
    from fabulist.personas.registry import list_personas

    by_name: dict[str, list[str]] = {}
    for persona in list_personas(log):
        by_name.setdefault(persona.name, []).append(persona.persona_id)

    found: list[str] = []
    for card in extract_cards(events):
        name = card.get("name")
        if not isinstance(name, str) or not name:
            continue
        matches = by_name.get(name)
        if not matches or len(matches) > 1:
            # No match -> the persona was never captured; ambiguous -> the name
            # cannot be resolved unambiguously. Either way: skip, never raise.
            continue
        persona_id = matches[0]
        if persona_id not in found:
            found.append(persona_id)
    return tuple(found)
