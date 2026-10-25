"""Memory scope + layer resolution (D53) — where a chat's embeddings live.

Design decision **D53** (PLAN.md §6) supersedes D16's *fencing mechanism*:
memory lives at the layer it is **used**, not at "the persona". A chat writes
to **exactly one** collection — its own layer — and recalls upward from the
layers nested inside it:

* **world** — the chat carries a ``world_ref`` (D32): write ``w_<world_id>``,
  read ``w_<world_id>`` plus one ``a_<actor>`` collection per actor (a world
  remembers everything inside it, including the characters in it).
* **actor** — the chat is with **exactly one** actor and no world: write
  ``a_<actor_id>``, read ``a_<actor_id>`` only (an isolated character memory —
  it never reaches into a world).
* **chat** — otherwise (zero actors, or two-plus actors, and no world): write
  ``c_<session_id>``, read ``c_<session_id>`` only (chat-local memory).

The ``chat_persona`` event REMAINS the **user-persona** record (``{{user}}``)
and stops being a memory fence; ``user_persona_id`` is recorded on the scope
but is deliberately **not** a layer selector. This module is a pure,
log-derived projection (D9/D11): it reads the chat session's ``system`` events
— fork-aware, so a branch inherits its parent's layer — and resolves the scope,
nothing else. It never writes.

Event contract (owned by other lanes; this module only reads these shapes)
-------------------------------------------------------------------------
* ``{"event": "chat_actor", "actor_ids": [persona_id, ...]}`` — appended on the
  chat session. Latest event wins; a list value (including ``[]``) replaces the
  previous one, so ``[]`` clears. Folded by the sibling module
  :func:`fabulist.memory.actor.fold_actors` (lazily imported).
* ``{"event": "world_ref", "world_id": str, "name": str}`` — the world snapshot
  a chat was started from (D32). Latest non-empty ``world_id`` wins.
* ``{"event": "chat_persona", "persona_id": str | null}`` — the **user**
  persona. Latest event wins; recorded as :attr:`MemoryScope.user_persona_id`,
  never used to pick a layer.
* ``{"event": "chat_memory", "enabled": bool}`` — the per-chat opt-out. Latest
  wins; absent means ``True`` (memory on by default).

Actor derivation
----------------
Actors come from ``chat_actor`` events when present
(:func:`fabulist.memory.actor.fold_actors`). **Legacy fallback** when no
``chat_actor`` event exists: the chat's own card events are resolved by persona
*name* through the D39 catalog (:func:`fabulist.memory.actor.actors_from_cards`
— ``log`` first, chain events second), and for a world chat the referenced
world's ``cast`` is unioned in (``world_ref`` -> ``world_by_id``). On an
ambiguous or missing persona this module falls back to the **chat** layer — it
never raises.

Collections
-----------
``w_<world_id>`` / ``a_<persona_id>`` / ``c_<session_id>`` — the same slug rule
and :data:`~fabulist.memory.indexer.MAX_COLLECTION_CHARS` cap as
:func:`fabulist.memory.indexer.collection_for`. The D16 ``p_<persona_id>``
persona fence is retired; :func:`collection_for_persona` survives this wave as
a deprecated shim for callers the orchestrator has not yet rewired.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .actor import actors_from_cards, fold_actors
from .indexer import MAX_COLLECTION_CHARS, collection_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "CHAT_ACTOR_EVENT",
    "CHAT_MEMORY_EVENT",
    "CHAT_PERSONA_EVENT",
    "LAYER_ACTOR",
    "LAYER_CHAT",
    "LAYER_WORLD",
    "SCOPE_ACTOR_PREFIX",
    "SCOPE_CHAT_PREFIX",
    "SCOPE_PERSONA_PREFIX",
    "SCOPE_WORLD_PREFIX",
    "WORLD_REF_EVENT",
    "MemoryScope",
    "collection_for_actor",
    "collection_for_persona",
    "collection_for_world",
    "resolve_scope",
]

# -- layer names -------------------------------------------------------------

#: A world chat (carries a ``world_ref``); reads itself plus its actors.
LAYER_WORLD = "world"
#: An actor-only chat (exactly one actor, no world); reads itself only.
LAYER_ACTOR = "actor"
#: Everything else; reads itself only (chat-local memory).
LAYER_CHAT = "chat"

# -- collection-id prefixes --------------------------------------------------

#: Collection-id prefix for a world-fenced collection (D53).
SCOPE_WORLD_PREFIX = "w_"
#: Collection-id prefix for an actor-fenced collection (D53).
SCOPE_ACTOR_PREFIX = "a_"
#: Collection-id prefix for a chat-local collection (mirrors the indexer).
SCOPE_CHAT_PREFIX = "c_"
#: DEPRECATED (D16): the retired persona fence. Kept for this wave so a caller
#: the orchestrator has not rewired still imports; :func:`collection_for_persona`
#: emits it but nothing in D53 layers uses it.
SCOPE_PERSONA_PREFIX = "p_"

# -- system-event kinds (owned by other lanes; read-only here) ---------------

#: System-event kind carrying the chat's actor ids (D53; on the chat session).
CHAT_ACTOR_EVENT = "chat_actor"

#: System-event kind carrying the chat's USER persona (D53; on the chat session).
CHAT_PERSONA_EVENT = "chat_persona"

#: System-event kind carrying a chat's memory toggle (on the chat session).
CHAT_MEMORY_EVENT = "chat_memory"

#: System-event kind carrying the world snapshot a chat was started from (D32).
#: Mirrors :data:`fabulist.worlds.registry.WORLD_REF_EVENT` (kept local so this
#: module stays import-light and never cycles through the worlds/personas lanes).
WORLD_REF_EVENT = "world_ref"


@dataclass(frozen=True)
class MemoryScope:
    """Resolved embedding/recall scope for one chat session (D53).

    Attributes
    ----------
    layer:
        ``"world"``, ``"actor"`` or ``"chat"`` — which layer the chat lives at.
    write_collection:
        The single sqlite-vec collection the chat embeds into. Exactly ONE: a
        chat never writes down a layer.
    read_collections:
        The ordered set of collections recall may query: the chat's own layer
        plus the layers nested inside it (``(w_<id>, *a_<actor>)`` for a world;
        a single ``(a_<id>,)`` / ``(c_<id>,)`` otherwise).
    world_id:
        The chat's referenced world id (``world_ref``), or ``None``.
    actor_ids:
        The chat's resolved actor (persona) ids — from ``chat_actor`` events,
        or the legacy card/world-cast derivation. Order preserved, deduped.
    user_persona_id:
        The chat's latest ``chat_persona`` (USER persona) id, or ``None``.
        Recorded information only — NOT a layer selector.
    enabled:
        ``False`` when the chat has opted out of embeddings (``chat_memory``);
        callers pass this through as ``memory_enabled``.
    """

    layer: str
    write_collection: str
    read_collections: tuple[str, ...]
    world_id: str | None
    actor_ids: tuple[str, ...]
    user_persona_id: str | None
    enabled: bool

    # -- DEPRECATED compat aliases (D16 -> D53) -----------------------------
    # Existing callers read ``scope.collection`` / ``scope.persona_id``; the
    # orchestrator rewires them to ``write_collection`` / ``user_persona_id``.

    @property
    def collection(self) -> str:
        """DEPRECATED alias for :attr:`write_collection`."""
        return self.write_collection

    @property
    def persona_id(self) -> str | None:
        """DEPRECATED alias for :attr:`user_persona_id`."""
        return self.user_persona_id


# -- collection builders -----------------------------------------------------


def _slug(prefix: str, value: str) -> str:
    """Fold ``prefix + value`` to a capped collection id (indexer slug rule)."""
    return re.sub(r"[^0-9a-zA-Z]+", "_", f"{prefix}{value}")[:MAX_COLLECTION_CHARS]


def collection_for_world(world_id: str) -> str:
    """Return the world-fenced collection id for ``world_id`` (``w_<slug>``).

    Mirrors :func:`fabulist.memory.indexer.collection_for`: every run of
    non-alphanumeric characters folds to a single ``_``, capped at
    :data:`~fabulist.memory.indexer.MAX_COLLECTION_CHARS`.
    """
    return _slug(SCOPE_WORLD_PREFIX, world_id)


def collection_for_actor(persona_id: str) -> str:
    """Return the actor-fenced collection id for ``persona_id`` (``a_<slug>``).

    Same slug rule/cap as :func:`collection_for_world`.
    """
    return _slug(SCOPE_ACTOR_PREFIX, persona_id)


def collection_for_persona(persona_id: str) -> str:
    """DEPRECATED (D16 -> D53): the retired persona fence.

    Kept only so a caller the orchestrator has not yet rewired keeps importing.
    D53 supersedes the persona fence: chats write at their own layer via
    :func:`collection_for_world` / :func:`collection_for_actor` /
    :func:`fabulist.memory.indexer.collection_for`. Do not use in new code.
    """
    return _slug(SCOPE_PERSONA_PREFIX, persona_id)


# -- actor derivation --------------------------------------------------------


def _clean_ids(values: Any) -> tuple[str, ...]:
    """Coerce ``values`` to a deduped ``tuple[str, ...]`` (order preserved)."""
    if not isinstance(values, (list, tuple)):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


def _has_chat_actor_event(events: Sequence[Any]) -> bool:
    """True when the chain carries any ``chat_actor`` system event (D53)."""
    for event in events:
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict) and payload.get("event") == CHAT_ACTOR_EVENT:
            return True
    return False


def _actors_from_cards(log: "EventLog", events: Sequence[Any]) -> tuple[str, ...]:
    """Legacy actor derivation for chains with no ``chat_actor`` event (D53).

    Delegates to :func:`fabulist.memory.actor.actors_from_cards` (one
    implementation, one set of semantics — the fold and the name resolution
    live there).
    """
    return _clean_ids(actors_from_cards(log, events))


def _world_cast(log: "EventLog", world_id: str) -> tuple[str, ...]:
    """The referenced world's ordered ``cast`` ids, or ``()`` (never raises)."""
    try:
        from fabulist.worlds.registry import world_by_id
    except Exception:  # noqa: BLE001 - never break a per-turn projection
        return ()
    world = world_by_id(log, world_id)
    if world is None:
        return ()
    return _clean_ids(world.cast)


def _merge(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Concatenate id groups, deduping while preserving first-seen order."""
    merged: list[str] = []
    for group in groups:
        merged.extend(group)
    return _clean_ids(merged)


def _resolve_actors(
    log: "EventLog",
    session_id: str,
    events: Sequence[Any],
    world_id: str | None,
) -> tuple[str, ...]:
    """Resolve the chat's actor ids (D53), preferring ``chat_actor`` events."""
    if _has_chat_actor_event(events):
        return _clean_ids(fold_actors(log, session_id))

    # Legacy derivation (no chat_actor event ever written for this chain).
    actors = _actors_from_cards(log, events)
    if world_id is not None:
        # "Worlds remember ... the characters included" — a world chat also
        # derives actors from the referenced world's cast (union; idempotent if
        # the sibling already folded it in).
        actors = _merge(actors, _world_cast(log, world_id))
    return actors


# -- scope resolution --------------------------------------------------------


def resolve_scope(log: "EventLog", session_id: str) -> MemoryScope:
    """Resolve ``session_id``'s :class:`MemoryScope` (D53, pure projection).

    Folds the session's inherited chain
    (:meth:`EventLog.get_chain_events`, fork-aware) in seq order; the newest
    ``chat_persona`` / ``chat_memory`` / ``world_ref`` / ``chat_actor`` rows win
    (latest-wins). Non-``system`` rows, malformed payloads and wrong-typed
    values are ignored.

    Layer resolution:

    * **world** when the chain carries a ``world_ref`` with a non-empty
      ``world_id``; ``world_id`` is set and ``read_collections`` is the world
      collection followed by each actor collection.
    * **actor** when there is no world and the actor set has exactly one id.
    * **chat** otherwise.
    """
    events = log.get_chain_events(session_id)

    user_persona_id: str | None = None
    enabled = True
    world_id: str | None = None

    for event in events:
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        kind = payload.get("event")
        if kind == CHAT_PERSONA_EVENT:
            raw = payload.get("persona_id")
            # A bare/``null``/empty value is an explicit clear.
            user_persona_id = raw if isinstance(raw, str) and raw else None
        elif kind == CHAT_MEMORY_EVENT:
            raw = payload.get("enabled")
            if isinstance(raw, bool):
                enabled = raw
        elif kind == WORLD_REF_EVENT:
            raw = payload.get("world_id")
            # Latest world_ref wins; a null/empty world_id clears the world.
            world_id = raw if isinstance(raw, str) and raw else None

    actor_ids = _resolve_actors(log, session_id, events, world_id)

    if world_id is not None:
        layer = LAYER_WORLD
        write_collection = collection_for_world(world_id)
        read_collections = (write_collection,) + tuple(
            collection_for_actor(actor_id) for actor_id in actor_ids
        )
    elif len(actor_ids) == 1:
        layer = LAYER_ACTOR
        write_collection = collection_for_actor(actor_ids[0])
        read_collections = (write_collection,)
    else:
        layer = LAYER_CHAT
        write_collection = collection_for(session_id)
        read_collections = (write_collection,)

    return MemoryScope(
        layer=layer,
        write_collection=write_collection,
        read_collections=read_collections,
        world_id=world_id,
        actor_ids=actor_ids,
        user_persona_id=user_persona_id,
        enabled=enabled,
    )
