"""Tests for memory scope + layer resolution (D53).

Scope is a pure log projection: :func:`fabulist.memory.scope.resolve_scope`
folds the chat's **fork-aware** chain (``get_chain_events``) of ``system``
events — ``world_ref`` / ``chat_actor`` / ``chat_persona`` / ``chat_memory`` —
latest-wins (the same fold shape as ``settings._enabled_from_log``).

D53 supersedes D16's fencing mechanism: memory lives at the layer it is USED.
A chat writes ONE collection at its own layer (``w_`` / ``a_`` / ``c_``) and
recalls upward from the layers nested inside it (a world also reads its
actors; an actor/chat reads only itself). The ``chat_persona`` event stays the
USER-persona record and no longer selects a layer.

The indexer ``memory_enabled=False`` path must append NOTHING. The embedder is
faked at the httpx transport seam exactly as ``tests/test_memory_indexer.py``
does (``httpx.MockTransport`` installed on ``fabulist.memory.indexer.httpx``,
the house convention from ``tests/test_memory_embedder.py``); the store runs for
real on ``tmp_path`` and the handler records every call so we can prove no embed
was attempted.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fabulist.log import EventLog
from fabulist.memory import indexer
from fabulist.memory.indexer import (
    INDEX_SKIPPED,
    IndexSkipped,
    collection_for,
    index_media,
    index_turn,
    reindex_revision,
)
from fabulist.memory.scope import (
    CHAT_ACTOR_EVENT,
    CHAT_MEMORY_EVENT,
    CHAT_PERSONA_EVENT,
    LAYER_ACTOR,
    LAYER_CHAT,
    LAYER_WORLD,
    SCOPE_ACTOR_PREFIX,
    SCOPE_CHAT_PREFIX,
    SCOPE_PERSONA_PREFIX,
    SCOPE_WORLD_PREFIX,
    WORLD_REF_EVENT,
    MemoryScope,
    collection_for_actor,
    collection_for_persona,
    collection_for_world,
    resolve_scope,
)
from fabulist.memory.store import MemoryStore
from fabulist.prompts.variables import PERSONAS_ANCHOR
from fabulist.worlds.registry import append_world

DIM = 4
VEC = [1.0, 0.0, 0.0, 0.0]


def run(coro):
    """Drive a coroutine to completion (suite has no pytest-asyncio)."""
    return asyncio.run(coro)


# -- log seeding helpers -----------------------------------------------------


def _append(log: EventLog, session_id: str, payload: dict, actor: str = "user") -> None:
    log.append_event(session_id, "system", actor, payload)


def _persona(log: EventLog, persona_id: str, name: str) -> None:
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": persona_id, "name": name},
    )


def _world(log: EventLog, world_id: str, name: str, cast: list[str]) -> None:
    append_world(log, {"world_id": world_id, "name": name, "cast": cast})


def _card(log: EventLog, session_id: str, name: str) -> None:
    log.append_event(session_id, "system", "system", {"card": {"name": name}})


def _world_ref(log: EventLog, session_id: str, world_id: str | None) -> None:
    _append(log, session_id, {"event": WORLD_REF_EVENT, "world_id": world_id})


def _chat_actor(log: EventLog, session_id: str, actor_ids) -> None:
    _append(log, session_id, {"event": CHAT_ACTOR_EVENT, "actor_ids": actor_ids})


def _fork(log: EventLog, parent: str, cut: int, branch: str) -> None:
    log.fork_session(parent, cut, branch)


# -- collection builders -----------------------------------------------------


def test_collection_builders_sanitize_and_cap():
    assert SCOPE_WORLD_PREFIX == "w_"
    assert SCOPE_ACTOR_PREFIX == "a_"
    assert SCOPE_CHAT_PREFIX == "c_"

    assert collection_for_world("aurora") == "w_aurora"
    assert collection_for_actor("alice") == "a_alice"
    assert collection_for_world("char/world 01") == "w_char_world_01"
    assert collection_for_actor("char/alice 01") == "a_char_alice_01"
    # A run of non-alphanumerics folds to a single underscore (indexer rule).
    assert collection_for_world("a---b") == "w_a_b"
    assert collection_for_actor("a---b") == "a_a_b"
    # Same cap as the existing chat-id builder.
    assert len(collection_for_world("x" * 200)) == len(collection_for("x" * 200))
    assert len(collection_for_actor("x" * 200)) == len(collection_for("x" * 200))


def test_collection_for_persona_is_a_deprecated_shim():
    """The D16 ``p_`` fence is retired but kept importable for this wave."""
    assert SCOPE_PERSONA_PREFIX == "p_"
    assert collection_for_persona("alice") == "p_alice"
    assert collection_for_persona("char/alice 01") == "p_char_alice_01"
    assert collection_for_persona("a---b") == "p_a_b"


# -- the three layer shapes --------------------------------------------------


def test_world_layer_shape(log):
    _persona(log, "cast1", "Lyra")
    _persona(log, "cast2", "Rowan")
    _world(log, "w1", "Aurora", ["cast1", "cast2"])
    _world_ref(log, "s1", "w1")
    _chat_actor(log, "s1", ["cast1", "cast2"])

    scope = resolve_scope(log, "s1")

    assert scope == MemoryScope(
        layer=LAYER_WORLD,
        write_collection="w_w1",
        read_collections=("w_w1", "a_cast1", "a_cast2"),
        world_id="w1",
        actor_ids=("cast1", "cast2"),
        user_persona_id=None,
        enabled=True,
    )


def test_actor_layer_shape(log):
    _chat_actor(log, "s1", ["cast1"])

    scope = resolve_scope(log, "s1")

    assert scope == MemoryScope(
        layer=LAYER_ACTOR,
        write_collection="a_cast1",
        read_collections=("a_cast1",),
        world_id=None,
        actor_ids=("cast1",),
        user_persona_id=None,
        enabled=True,
    )


def test_chat_layer_shape(log):
    # A plain chat: a user turn only, no world / actor / persona rows.
    log.append_event("s1", "user", "user", {"text": "hi"})

    scope = resolve_scope(log, "s1")

    assert scope == MemoryScope(
        layer=LAYER_CHAT,
        write_collection=collection_for("s1"),
        read_collections=(collection_for("s1"),),
        world_id=None,
        actor_ids=(),
        user_persona_id=None,
        enabled=True,
    )
    assert scope.write_collection.startswith("c_")


def test_write_collection_is_exactly_the_own_layer(log):
    """One write, at the chat's own layer — never down a layer."""
    _world(log, "w1", "Aurora", ["cast1"])
    _world_ref(log, "s1", "w1")
    _chat_actor(log, "s1", ["cast1"])
    assert resolve_scope(log, "s1").write_collection == "w_w1"

    _chat_actor(log, "s2", ["cast1"])
    assert resolve_scope(log, "s2").write_collection == "a_cast1"

    assert resolve_scope(log, "s3").write_collection == collection_for("s3")


# -- read sets ---------------------------------------------------------------


def test_world_recall_includes_its_actors(log):
    _world(log, "w1", "Aurora", ["cast1", "cast2"])
    _world_ref(log, "s1", "w1")
    _chat_actor(log, "s1", ["cast1", "cast2"])

    scope = resolve_scope(log, "s1")
    assert scope.read_collections == (
        collection_for_world("w1"),
        collection_for_actor("cast1"),
        collection_for_actor("cast2"),
    )


def test_actor_only_recall_never_reaches_a_world(log):
    _world(log, "w1", "Aurora", ["cast1"])
    _world_ref(log, "other", "w1")
    # A separate actor-only chat: no world_ref.
    _chat_actor(log, "s2", ["cast2"])

    scope = resolve_scope(log, "s2")
    assert scope.layer == LAYER_ACTOR
    assert scope.read_collections == (collection_for_actor("cast2"),)
    assert not any(c.startswith(SCOPE_WORLD_PREFIX) for c in scope.read_collections)


def test_chat_recall_reads_only_itself(log):
    scope = resolve_scope(log, "s1")
    assert scope.read_collections == (collection_for("s1"),)


def test_world_without_actors_reads_only_the_world(log):
    _world_ref(log, "s1", "w1")
    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_WORLD
    assert scope.read_collections == ("w_w1",)


# -- chat_actor: latest wins / clears / cardinality --------------------------


def test_chat_actor_latest_wins(log):
    _chat_actor(log, "s1", ["a1"])
    _chat_actor(log, "s1", ["a2"])
    scope = resolve_scope(log, "s1")
    assert scope.actor_ids == ("a2",)
    assert scope.layer == LAYER_ACTOR
    assert scope.write_collection == collection_for_actor("a2")


def test_chat_actor_empty_list_clears_to_chat_layer(log):
    _chat_actor(log, "s1", ["a1"])
    _chat_actor(log, "s1", [])
    scope = resolve_scope(log, "s1")
    assert scope.actor_ids == ()
    assert scope.layer == LAYER_CHAT
    assert scope.write_collection == collection_for("s1")


def test_two_actors_without_a_world_is_chat_layer(log):
    _chat_actor(log, "s1", ["a1", "a2"])
    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_CHAT
    assert scope.actor_ids == ("a1", "a2")  # recorded, but not an actor fence
    assert scope.write_collection == collection_for("s1")


def test_two_actors_with_a_world_is_world_layer(log):
    _world_ref(log, "s1", "w1")
    _chat_actor(log, "s1", ["a1", "a2"])
    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_WORLD
    assert scope.read_collections == ("w_w1", "a_a1", "a_a2")


def test_chat_actor_is_authoritative_over_the_world_cast(log):
    """A recorded chat_actor set wins; the world cast is NOT unioned in."""
    _persona(log, "c1", "Lyra")
    _world(log, "w1", "Aurora", ["c1"])
    _world_ref(log, "s1", "w1")
    _chat_actor(log, "s1", ["c2"])

    scope = resolve_scope(log, "s1")
    assert scope.actor_ids == ("c2",)
    assert scope.read_collections == ("w_w1", "a_c2")


# -- world_ref: latest wins / clears ----------------------------------------


def test_world_ref_latest_wins_and_clear(log):
    _world_ref(log, "s1", "w1")
    _world_ref(log, "s1", "w2")
    assert resolve_scope(log, "s1").world_id == "w2"
    assert resolve_scope(log, "s1").write_collection == "w_w2"

    _world_ref(log, "s1", None)
    scope = resolve_scope(log, "s1")
    assert scope.world_id is None
    assert scope.layer == LAYER_CHAT


# -- memory opt-out ----------------------------------------------------------


def test_chat_memory_toggle_latest_wins_defaults_on(log):
    assert resolve_scope(log, "s1").enabled is True

    _append(log, "s1", {"event": CHAT_MEMORY_EVENT, "enabled": False})
    assert resolve_scope(log, "s1").enabled is False

    _append(log, "s1", {"event": CHAT_MEMORY_EVENT, "enabled": True})
    assert resolve_scope(log, "s1").enabled is True


def test_optout_does_not_change_the_layer(log):
    _world_ref(log, "s1", "w1")
    _chat_actor(log, "s1", ["a1"])
    _append(log, "s1", {"event": CHAT_MEMORY_EVENT, "enabled": False})
    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_WORLD
    assert scope.write_collection == "w_w1"
    assert scope.enabled is False


# -- the user persona is recorded, not a layer selector ----------------------


def test_user_persona_recorded_but_not_a_layer(log):
    _append(log, "s1", {"event": CHAT_PERSONA_EVENT, "persona_id": "user1"})
    scope = resolve_scope(log, "s1")
    assert scope.user_persona_id == "user1"
    assert scope.persona_id == "user1"  # deprecated alias
    # D53: the user persona no longer fences — no world/actor -> chat layer.
    assert scope.layer == LAYER_CHAT
    assert scope.write_collection == collection_for("s1")


def test_user_persona_latest_wins_and_clears(log):
    _append(log, "s1", {"event": CHAT_PERSONA_EVENT, "persona_id": "u1"})
    _append(log, "s1", {"event": CHAT_PERSONA_EVENT, "persona_id": "u2"})
    assert resolve_scope(log, "s1").user_persona_id == "u2"

    _append(log, "s1", {"event": CHAT_PERSONA_EVENT, "persona_id": None})
    assert resolve_scope(log, "s1").user_persona_id is None
    assert resolve_scope(log, "s1").persona_id is None


# -- legacy derivation from card names (D39 catalog) -------------------------


def test_legacy_single_card_name_derives_actor_layer(log):
    _persona(log, "cast1", "Lyra")
    _card(log, "s1", "Lyra")

    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_ACTOR
    assert scope.actor_ids == ("cast1",)
    assert scope.write_collection == collection_for_actor("cast1")


def test_legacy_ambiguous_card_name_falls_back_to_chat(log):
    _persona(log, "p1", "Lyra")
    _persona(log, "p2", "Lyra")  # two personas share the name -> ambiguous
    _card(log, "s1", "Lyra")

    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_CHAT
    assert scope.actor_ids == ()
    assert scope.write_collection == collection_for("s1")


def test_legacy_missing_persona_falls_back_to_chat(log):
    _card(log, "s1", "Nobody Captured")

    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_CHAT
    assert scope.write_collection == collection_for("s1")


def test_legacy_two_resolved_cards_is_chat_layer(log):
    _persona(log, "c1", "Lyra")
    _persona(log, "c2", "Rowan")
    _card(log, "s1", "Lyra")
    _card(log, "s1", "Rowan")

    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_CHAT  # not exactly one actor
    assert scope.actor_ids == ("c1", "c2")


def test_legacy_world_chat_derives_actors_from_the_world_cast(log):
    _persona(log, "c1", "Lyra")
    _persona(log, "c2", "Rowan")
    _world(log, "w1", "Aurora", ["c1", "c2"])
    _world_ref(log, "s1", "w1")  # no chat_actor event, no card events

    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_WORLD
    assert scope.world_id == "w1"
    assert scope.actor_ids == ("c1", "c2")
    assert scope.read_collections == ("w_w1", "a_c1", "a_c2")


def test_legacy_world_chat_merges_cards_and_cast(log):
    _persona(log, "c1", "Lyra")
    _persona(log, "c2", "Rowan")
    _world(log, "w1", "Aurora", ["c2"])
    _card(log, "s1", "Lyra")
    _world_ref(log, "s1", "w1")

    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_WORLD
    assert scope.actor_ids == ("c1", "c2")  # union, deduped, order preserved


def test_legacy_world_cast_missing_world_never_raises(log):
    _world_ref(log, "s1", "ghost-world")  # not in the __worlds__ anchor
    _card(log, "s1", "Lyra")  # no persona capture either

    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_WORLD
    assert scope.actor_ids == ()
    assert scope.read_collections == ("w_ghost_world",)


# -- malformed / non-system rows ---------------------------------------------


def test_non_system_and_malformed_events_are_ignored(log):
    # A user event that merely looks like an actor payload must not fence.
    log.append_event("s1", "user", "user", {"event": CHAT_ACTOR_EVENT, "actor_ids": ["x"]})
    # Malformed system payloads (wrong type / not a list / not a dict).
    _append(log, "s1", {"event": CHAT_ACTOR_EVENT, "actor_ids": "not-a-list"})
    _append(log, "s1", {"event": CHAT_MEMORY_EVENT, "enabled": "no"})
    _append(log, "s1", {"event": WORLD_REF_EVENT, "world_id": 123})
    _append(log, "s1", {"event": CHAT_PERSONA_EVENT, "persona_id": 42})

    scope = resolve_scope(log, "s1")
    assert scope.layer == LAYER_CHAT
    assert scope.world_id is None
    assert scope.user_persona_id is None
    assert scope.enabled is True
    assert scope.write_collection == collection_for("s1")


# -- branch / fork inheritance -----------------------------------------------


def test_branch_inherits_world_layer(log):
    _persona(log, "c1", "Lyra")
    _world(log, "w1", "Aurora", ["c1"])
    _world_ref(log, "parent", "w1")
    _chat_actor(log, "parent", ["c1"])
    log.append_event("parent", "user", "user", {"text": "hi"})
    cut = log.get_events("parent")[-1].seq

    _fork(log, "parent", cut, "branch")

    scope = resolve_scope(log, "branch")
    assert scope.layer == LAYER_WORLD
    assert scope.world_id == "w1"
    assert scope.actor_ids == ("c1",)
    assert scope.read_collections == ("w_w1", "a_c1")


def test_branch_inherits_actor_layer(log):
    _chat_actor(log, "parent", ["c1"])
    log.append_event("parent", "user", "user", {"text": "hi"})
    cut = log.get_events("parent")[-1].seq

    _fork(log, "parent", cut, "branch")

    scope = resolve_scope(log, "branch")
    assert scope.layer == LAYER_ACTOR
    assert scope.write_collection == collection_for_actor("c1")
    assert scope.read_collections == (collection_for_actor("c1"),)


def test_branch_inherits_memory_opt_out(log):
    _chat_actor(log, "parent", ["c1"])
    _append(log, "parent", {"event": CHAT_MEMORY_EVENT, "enabled": False})
    log.append_event("parent", "user", "user", {"text": "hi"})
    cut = log.get_events("parent")[-1].seq

    _fork(log, "parent", cut, "branch")

    scope = resolve_scope(log, "branch")
    assert scope.layer == LAYER_ACTOR
    assert scope.enabled is False


def test_branch_local_actor_overrides_the_inherited_one(log):
    _chat_actor(log, "parent", ["c1"])
    log.append_event("parent", "user", "user", {"text": "hi"})
    cut = log.get_events("parent")[-1].seq
    _fork(log, "parent", cut, "branch")

    _chat_actor(log, "branch", ["c2"])  # branch-local, higher seq -> wins

    scope = resolve_scope(log, "branch")
    assert scope.actor_ids == ("c2",)
    assert scope.write_collection == collection_for_actor("c2")


# -- deprecated aliases ------------------------------------------------------


def test_deprecated_aliases_mirror_the_new_fields(log):
    _append(log, "s1", {"event": CHAT_PERSONA_EVENT, "persona_id": "user1"})
    _chat_actor(log, "s1", ["c1"])
    scope = resolve_scope(log, "s1")
    assert scope.collection == scope.write_collection == "a_c1"
    assert scope.persona_id == scope.user_persona_id == "user1"


# -- indexer opt-out: nothing is appended ------------------------------------


def _ok_body(vectors):
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": vec}
            for i, vec in enumerate(vectors)
        ],
        "model": "fake",
    }


@pytest.fixture
def transport_factory(monkeypatch):
    """Install a MockTransport-backed ``httpx.AsyncClient`` factory for indexer.

    Mirrors ``tests/test_memory_indexer.py``: the handler records wire calls so a
    disabled chat can be proven never to reach the embeddings service.
    """

    def install(handler):
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def fake_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(indexer.httpx, "AsyncClient", fake_client)

    return install


def test_index_turn_disabled_appends_nothing(tmp_path, transport_factory):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        result = run(
            index_turn(
                store,
                "http://host",
                "m",
                collection_for_actor("alice"),
                7,
                "user",
                "never embedded",
                memory_enabled=False,
            )
        )
        # No collection was even created, and a recall finds nothing.
        assert store.collections() == []
        assert store.query(collection_for_actor("alice"), VEC, k=10) == []

    assert isinstance(result, IndexSkipped)
    assert result is INDEX_SKIPPED
    assert calls == []


def test_index_media_disabled_appends_nothing(tmp_path, transport_factory):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        result = run(
            index_media(
                store,
                "http://host",
                "m",
                "w_s1",
                3,
                "image",
                "image/png",
                "AAAA",
                memory_enabled=False,
            )
        )
        assert store.collections() == []

    assert result is INDEX_SKIPPED
    assert calls == []


def test_reindex_revision_disabled_appends_nothing(tmp_path, transport_factory):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        result = run(
            reindex_revision(
                store,
                "http://host",
                "m",
                "s1",
                2,
                "narrative",
                "edited prose",
                memory_enabled=False,
            )
        )
        assert store.collections() == []

    assert result is INDEX_SKIPPED
    assert calls == []


def test_index_turn_enabled_still_embeds(tmp_path, transport_factory):
    """Guard: the default path is unchanged (opt-out must not leak into normal)."""
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        result = run(
            index_turn(
                store, "http://host", "m", collection_for_actor("alice"), 7, "user", "hello"
            )
        )
        rows = store.query(collection_for_actor("alice"), VEC, k=1)
        collections = store.collections()

    assert result is None
    assert collections == [collection_for_actor("alice")]
    assert rows[0]["seq"] == 7
    assert rows[0]["text"] == "hello"
    assert calls[0]["input"] == ["title: none | text: hello"]
