"""Tests for the per-chat configuration router (D16 chat memory toggle).

Exercises the real FastAPI app (httpx ASGI transport) with the chat-config
router mounted, against a tmp SQLite event log. The log stays the source of
truth (D9/D10): memory is folded from ``chat_memory`` system events on the chat
session (latest-wins, absent -> default True), and PATCH appends exactly one
such row — immediacy with no restart and no side table.

Mount pattern mirrors ``tests/test_state_api.py``: build the app over a tmp
event-log path via :func:`create_app`, ``include_router`` the lane's router,
close the log in teardown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.chat_config_api import (
    CHAT_MEMORY_CONFIG_EVENT,
    CHAT_MEMORY_EVENT,
    CHAT_PERSONA_EVENT,
    resolve_npc_dispatch,
    router,
)
from fabulist.state.presence import NPC_DISPATCH_EVENT, NpcDispatchPolicy
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _seed_chat(log: EventLog, session_id: str = "s1") -> None:
    """Create a real chat session the way ``sessions.py`` would: a title anchor."""
    log.append_event(
        session_id,
        "system",
        "user",
        {"event": "session_created", "title": "A chat"},
    )


def _memory_events(log: EventLog, session_id: str = "s1") -> list:
    return [
        e
        for e in log.get_events(session_id)
        if e.type == "system" and e.payload.get("event") == CHAT_MEMORY_EVENT
    ]


def test_memory_defaults_true_when_never_toggled(build_app):
    """Absent ``chat_memory`` events fold to the documented default True."""
    build, _ = build_app
    app = build()
    _seed_chat(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/memory")

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}


def test_patch_false_persists_to_get(build_app):
    """PATCH false appends one chat_memory row; GET reads it back false."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/sessions/s1/memory", json={"enabled": False}
            )
            fetched = await client.get("/api/sessions/s1/memory")
            return patched, fetched

    patched, fetched = run(scenario())
    assert patched.status_code == 200
    assert patched.json() == {"session_id": "s1", "enabled": False}
    assert fetched.json() == {"enabled": False}

    rows = _memory_events(log)
    assert len(rows) == 1
    assert rows[0].actor == "user"
    assert rows[0].payload == {"event": CHAT_MEMORY_EVENT, "enabled": False}


def test_latest_write_wins(build_app):
    """false then true folds to true (order-based, latest write wins)."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            for value in (False, True):
                resp = await client.patch(
                    "/api/sessions/s1/memory", json={"enabled": value}
                )
                assert resp.status_code == 200
            return await client.get("/api/sessions/s1/memory")

    resp = run(scenario())
    assert resp.json() == {"enabled": True}
    # Two appends, both retained (append-only, D9).
    assert len(_memory_events(log)) == 2


@pytest.mark.parametrize("verb", ["get", "patch"])
def test_unknown_session_is_404(build_app, verb):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            if verb == "get":
                return await client.get("/api/sessions/ghost/memory")
            return await client.patch(
                "/api/sessions/ghost/memory", json={"enabled": False}
            )

    resp = run(scenario())
    assert resp.status_code == 404

    other = EventLog(log_path)
    assert other.get_events("ghost") == []
    other.close()


@pytest.mark.parametrize(
    "body", [{}, {"enabled": "nope"}, {"enabled": 5}, {"enabled": None}]
)
def test_non_bool_or_missing_enabled_is_422_and_writes_nothing(build_app, body):
    """Missing or un-coercible ``enabled`` fails validation before any append."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.patch("/api/sessions/s1/memory", json=body)

    resp = run(scenario())
    assert resp.status_code == 422
    assert _memory_events(log) == []


def test_toggle_does_not_touch_persona_events(build_app):
    """The memory door never writes a persona event (that is the personas lane)."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)
    log.append_event(
        "s1", "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": "p1"}
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/s1/memory", json={"enabled": False}
            )

    assert run(scenario()).status_code == 200
    persona_rows = [
        e
        for e in log.get_events("s1")
        if e.payload.get("event") == CHAT_PERSONA_EVENT
    ]
    assert len(persona_rows) == 1
    assert persona_rows[0].payload == {"event": CHAT_PERSONA_EVENT, "persona_id": "p1"}


def test_persona_events_are_ignored_by_memory_fold(build_app):
    """Interleaved persona writes never affect the memory default/fold."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)
    for persona in ("p1", "p2"):
        log.append_event(
            "s1",
            "system",
            "user",
            {"event": CHAT_PERSONA_EVENT, "persona_id": persona},
        )

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/memory")

    assert run(scenario()).json() == {"enabled": True}


def test_chat_config_reads_persona_and_memory_latest_wins(build_app):
    """chat-config folds both event kinds latest-wins, no scope import needed."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)
    log.append_event(
        "s1", "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": "old"}
    )
    log.append_event(
        "s1", "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": "new"}
    )
    log.append_event(
        "s1", "system", "user", {"event": CHAT_MEMORY_EVENT, "enabled": False}
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/chat-config")

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {
        "persona_id": "new",
        "memory_enabled": False,
        # W9.22 widened the shape: the session flag rides this same read.
        "user_as_actor": False,
        # W9.24 §3.5 widened it again: the NPC dispatch policy's two keys.
        "npc_strategy": "decision",
        "npc_max_dispatch": 0,
    }


def test_chat_config_defaults_without_events(build_app):
    """A bare chat reads persona_id null, memory_enabled True, user_as_actor False."""
    build, _ = build_app
    app = build()
    _seed_chat(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/chat-config")

    resp = run(scenario())
    assert resp.json() == {
        "persona_id": None,
        "memory_enabled": True,
        "user_as_actor": False,
        "npc_strategy": "decision",
        "npc_max_dispatch": 0,
    }


# --- D25 C2: recall knobs ---------------------------------------------------


def _config_events(log: EventLog, session_id: str = "s1") -> list:
    return [
        e
        for e in log.get_events(session_id)
        if e.type == "system" and e.payload.get("event") == CHAT_MEMORY_CONFIG_EVENT
    ]


def test_memory_config_defaults(build_app):
    """A chat that never patched knobs reads the documented defaults."""
    build, _ = build_app
    app = build()
    _seed_chat(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/memory-config")

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": "s1",
        "enabled": True,
        "k": 6,
        "block_chars": 4200,
        "rerank_enabled": True,
        "query_mode": "message",
        "overfetch": 25,
    }


def test_memory_config_patch_round_trip_and_merge(build_app):
    """A partial PATCH merges onto the folded config and appends one snapshot."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            first = await client.patch(
                "/api/sessions/s1/memory-config",
                json={"k": 12, "rerank_enabled": True},
            )
            second = await client.patch(
                "/api/sessions/s1/memory-config", json={"query_mode": "message+scene"}
            )
            fetched = await client.get("/api/sessions/s1/memory-config")
            return first, second, fetched

    first, second, fetched = run(scenario())
    assert first.status_code == 200
    # k + rerank changed, the rest stayed default
    assert first.json()["k"] == 12
    assert first.json()["rerank_enabled"] is True
    assert first.json()["block_chars"] == 4200
    # second patch only moved query_mode; k/rerank survived
    assert second.json()["query_mode"] == "message+scene"
    assert second.json()["k"] == 12
    assert second.json()["rerank_enabled"] is True
    assert fetched.json() == second.json()

    rows = _config_events(log)
    assert len(rows) == 2
    assert rows[0].payload == {
        "event": CHAT_MEMORY_CONFIG_EVENT,
        "k": 12,
        "block_chars": 4200,
        "rerank_enabled": True,
        "query_mode": "message",
        "overfetch": 25,
    }
    assert rows[0].actor == "user"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"k": 0},
        {"k": 99},
        {"block_chars": 5},
        {"query_mode": "scene"},
        {"overfetch": 0},
        {"nope": 1},
        {"rerank_enabled": "nope"},
    ],
)
def test_memory_config_bad_body_is_422_and_writes_nothing(build_app, body):
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.patch("/api/sessions/s1/memory-config", json=body)

    resp = run(scenario())
    assert resp.status_code == 422
    assert _config_events(log) == []


def test_memory_config_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/ghost/memory-config", json={"k": 3}
            )

    assert run(scenario()).status_code == 404


def test_memory_config_enabled_follows_canonical_toggle(build_app):
    """``enabled`` in memory-config mirrors the canonical chat_memory row."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            await client.patch("/api/sessions/s1/memory", json={"enabled": False})
            return await client.get("/api/sessions/s1/memory-config")

    resp = run(scenario())
    assert resp.json()["enabled"] is False


# --- W9.24 §3.5/§4: the NPC dispatch policy on the same door ---------------


def _dispatch_rows(log: EventLog, session_id: str = "s1") -> list:
    return [
        e
        for e in log.get_events(session_id)
        if e.type == "system" and e.payload.get("event") == NPC_DISPATCH_EVENT
    ]


def test_npc_dispatch_defaults_decide_and_bound_nothing(build_app):
    """Absent policy event -> ``decision`` + ``0`` (no bound): the plan's default.

    This is also the MIGRATION statement: a session that never wrote a policy row
    reads ``decision``, so its lane behaviour changes at upgrade (all-active
    dispatch becomes a threshold-gated subset, zero-lane turns legal) — intended,
    and the reason the defaults are pinned here.
    """
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/chat-config")

    body = run(scenario()).json()
    assert body["npc_strategy"] == "decision"
    assert body["npc_max_dispatch"] == 0
    assert resolve_npc_dispatch(log, "s1") == NpcDispatchPolicy(
        strategy="decision", max_dispatch=0
    )
    assert _dispatch_rows(log) == []  # a read never writes


def test_npc_dispatch_patch_appends_one_full_snapshot_and_folds(build_app):
    """One PATCH writes ONE ``chat_npc_dispatch`` row carrying BOTH keys."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/sessions/s1/chat-config",
                json={"npc_strategy": "lru", "npc_max_dispatch": 2},
            )
            fetched = await client.get("/api/sessions/s1/chat-config")
            return patched, fetched

    patched, fetched = run(scenario())
    assert patched.status_code == 200
    assert patched.json()["npc_strategy"] == "lru"
    assert patched.json()["npc_max_dispatch"] == 2
    assert patched.json() == fetched.json()

    rows = _dispatch_rows(log)
    assert len(rows) == 1
    assert rows[0].actor == "user"
    assert rows[0].payload == {
        "event": NPC_DISPATCH_EVENT,
        "strategy": "lru",
        "max_dispatch": 2,
    }
    assert resolve_npc_dispatch(log, "s1") == NpcDispatchPolicy("lru", 2)


@pytest.mark.parametrize(
    "first,second,expected",
    [
        # A bound-only PATCH must not reset the strategy (full-snapshot write).
        ({"npc_strategy": "random"}, {"npc_max_dispatch": 3}, ("random", 3)),
        # ...and a strategy-only PATCH must not reset the bound.
        ({"npc_max_dispatch": 3}, {"npc_strategy": "decision"}, ("decision", 3)),
        # 0 clears the bound (no lanes cap).
        ({"npc_max_dispatch": 3}, {"npc_max_dispatch": 0}, ("decision", 0)),
    ],
)
def test_npc_dispatch_patch_is_per_key(build_app, first, second, expected):
    """Absent keys leave their setting unchanged — the R1 per-key body rule."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            await client.patch("/api/sessions/s1/chat-config", json=first)
            patched = await client.patch("/api/sessions/s1/chat-config", json=second)
            return patched, await client.get("/api/sessions/s1/chat-config")

    patched, fetched = run(scenario())
    assert patched.status_code == 200
    assert (
        patched.json()["npc_strategy"],
        patched.json()["npc_max_dispatch"],
    ) == expected
    assert patched.json() == fetched.json()
    # Two writes, two rows: append-only, latest wins (D9).
    assert len(_dispatch_rows(log)) == 2


def test_the_dispatch_pair_and_the_actor_flag_are_independent(build_app):
    """One body, two settings: each key writes its OWN event kind, and neither
    one rewrites the other (the reason they share the per-key rule)."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/sessions/s1/chat-config",
                json={"user_as_actor": True, "npc_strategy": "random"},
            )
            return patched, await client.get("/api/sessions/s1/chat-config")

    patched, fetched = run(scenario())
    assert patched.status_code == 200
    assert patched.json()["user_as_actor"] is True
    assert patched.json()["npc_strategy"] == "random"
    assert patched.json()["npc_max_dispatch"] == 0
    assert patched.json() == fetched.json()
    assert len(_dispatch_rows(log)) == 1
    actor_rows = [
        e
        for e in log.get_events("s1")
        if e.type == "system" and e.payload.get("event") == "chat_user_as_actor"
    ]
    assert len(actor_rows) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"npc_strategy": None},
        {"npc_max_dispatch": None},
        {"npc_strategy": "rotating"},
        {"npc_max_dispatch": -1},
        {"npc_max_dispatch": 1.5},
        {"npc_max_dispatch": True},
        {"npc_max_dispatch": "3"},
        {"npc_unknown": 1},
    ],
)
def test_npc_dispatch_bad_body_is_422_and_writes_nothing(build_app, body):
    """Strict validation, refused before any append (the whole body, not a key)."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.patch("/api/sessions/s1/chat-config", json=body)

    resp = run(scenario())
    assert resp.status_code == 422
    assert _dispatch_rows(log) == []
    assert resolve_npc_dispatch(log, "s1") == NpcDispatchPolicy()


def test_a_malformed_dispatch_row_folds_per_key_never_off_contract(build_app):
    """A hand-edited row cannot select a nonexistent strategy or a bad bound."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)
    log.append_event(
        "s1",
        "system",
        "user",
        {"event": NPC_DISPATCH_EVENT, "strategy": "rotating", "max_dispatch": -3},
    )
    assert resolve_npc_dispatch(log, "s1") == NpcDispatchPolicy()

    log.append_event(
        "s1", "system", "user", {"event": NPC_DISPATCH_EVENT, "strategy": "random"}
    )
    # The valid key lands; the malformed bound never did.
    assert resolve_npc_dispatch(log, "s1") == NpcDispatchPolicy("random", 0)


def test_resolve_npc_dispatch_is_the_engine_seam_for_the_same_rows(build_app):
    """HTTP write -> in-engine read: one event contract, no second fold."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)
    assert resolve_npc_dispatch(log, "s1") == NpcDispatchPolicy()
    assert resolve_npc_dispatch(log, "") == NpcDispatchPolicy()  # blank: defaults
    assert resolve_npc_dispatch(log, "never-existed") == NpcDispatchPolicy()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/s1/chat-config", json={"npc_max_dispatch": 1}
            )

    assert run(scenario()).status_code == 200
    assert resolve_npc_dispatch(log, "s1") == NpcDispatchPolicy("decision", 1)


def test_chat_config_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/ghost/chat-config")

    assert run(scenario()).status_code == 404
