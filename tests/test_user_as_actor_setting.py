"""User-as-Actor session setting (W9.22 plan §2 — the cast model).

The setting is the ONE gate that decides whether the player is an actor: ON
makes the player a first-class presence **and** a lane candidate (impersonation
by consent, per session), OFF keeps today's behaviour where the player is never
a cast member. It is a session-scoped *setting*, not an asset edit (AGENTS.md
non-negotiable 7), so it lives where every other per-chat flag lives: one
latest-wins ``system`` event per write on the chat session (D9), folded on read,
never a side table and never a rewrite of a definition.

What these tests pin (the rules, by name):

  * the default is OFF, and a session that never wrote a row reads OFF;
  * a PATCH appends exactly ONE event, with the contracted payload key
    (``user_as_actor``) and the ``user`` actor stamp, and the reply is the same
    body the next GET returns (one fold, two surfaces — they cannot drift);
  * latest write wins across appends, append-only (both rows stay);
  * a malformed row can never switch impersonation ON (only a real JSON bool
    counts — the safe direction for this setting);
  * the in-engine seam lane 1b calls — :func:`resolve_user_as_actor` — reads the
    very rows the HTTP door writes (a hand-called fold would be the W9.21
    "unit tests lied" failure: this asserts THROUGH the endpoint + the real
    log, not against a stub);
  * a rejected write (bad body, unknown session) appends nothing.

Mount pattern mirrors ``tests/test_chat_config_api.py``: the real FastAPI app
over a tmp SQLite event log, the lane's router included, log closed in teardown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.chat_config_api import (
    CHAT_MEMORY_EVENT,
    CHAT_PERSONA_EVENT,
    CHAT_USER_AS_ACTOR_EVENT,
    resolve_user_as_actor,
    router,
)
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


def _setting_rows(log: EventLog, session_id: str = "s1") -> list:
    return [
        e
        for e in log.get_events(session_id)
        if e.type == "system" and e.payload.get("event") == CHAT_USER_AS_ACTOR_EVENT
    ]


# --- default OFF ------------------------------------------------------------


def test_chat_config_publishes_user_as_actor_off_for_a_session_that_never_set_it(
    build_app,
):
    """Absent setting event -> ``user_as_actor`` False in the shared config read."""
    build, _ = build_app
    app = build()
    _seed_chat(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/chat-config")

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_as_actor"] is False
    # The pre-existing fields are untouched by the widening (byte-stable values).
    assert body["persona_id"] is None
    assert body["memory_enabled"] is True


def test_resolver_defaults_off_for_a_blank_session_id_and_unknown_session(build_app):
    """The in-engine seam never impersonates a session it cannot name."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    assert resolve_user_as_actor(log, "") is False
    assert resolve_user_as_actor(log, "never-existed") is False


# --- the write door ---------------------------------------------------------


def test_patch_appends_one_setting_event_with_the_contracted_key(build_app):
    """PATCH writes exactly one ``{event, user_as_actor}`` row as the user."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/s1/chat-config", json={"user_as_actor": True}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["user_as_actor"] is True

    rows = _setting_rows(log)
    assert len(rows) == 1
    assert rows[0].actor == "user"
    assert rows[0].payload == {"event": CHAT_USER_AS_ACTOR_EVENT, "user_as_actor": True}


def test_patch_reply_is_the_same_body_the_next_get_returns(build_app):
    """One fold behind both surfaces: the PATCH echo equals the following GET."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)
    log.append_event(
        "s1", "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": "p1"}
    )

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/sessions/s1/chat-config", json={"user_as_actor": True}
            )
            fetched = await client.get("/api/sessions/s1/chat-config")
            return patched, fetched

    patched, fetched = run(scenario())
    assert patched.status_code == 200
    assert patched.json() == fetched.json()
    assert patched.json() == {
        "persona_id": "p1",
        "memory_enabled": True,
        "user_as_actor": True,
        # W9.24 §4 widened the same read with the NPC dispatch policy; the
        # fallbacks are the absence defaults (decision / no bound).
        "npc_strategy": "decision",
        "npc_max_dispatch": 0,
    }


def test_latest_write_wins_and_both_rows_stay_append_only(build_app):
    """ON then OFF folds to OFF; the log keeps both writes (D9)."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            on = await client.patch(
                "/api/sessions/s1/chat-config", json={"user_as_actor": True}
            )
            off = await client.patch(
                "/api/sessions/s1/chat-config", json={"user_as_actor": False}
            )
            return on, off, await client.get("/api/sessions/s1/chat-config")

    on, off, fetched = run(scenario())
    assert on.json()["user_as_actor"] is True
    assert off.json()["user_as_actor"] is False
    assert fetched.json()["user_as_actor"] is False
    assert len(_setting_rows(log)) == 2


def test_resolver_reads_the_rows_the_patch_endpoint_wrote(build_app):
    """The lane-1b seam and the HTTP door agree (one event, one fold).

    This is the cross-lane contract test: lane 1b calls
    :func:`resolve_user_as_actor` per turn while the UI flips the setting over
    HTTP. Reading the resolver after a REAL request proves both sides fold the
    same row — the W9.21 lesson (a hand-called helper the graph never called)
    applied to a second consumer.
    """
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)
    assert resolve_user_as_actor(log, "s1") is False

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/s1/chat-config", json={"user_as_actor": True}
            )

    assert run(scenario()).status_code == 200
    assert resolve_user_as_actor(log, "s1") is True

    async def scenario_off():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/s1/chat-config", json={"user_as_actor": False}
            )

    assert run(scenario_off()).status_code == 200
    assert resolve_user_as_actor(log, "s1") is False


def test_malformed_setting_row_can_never_switch_user_as_actor_on(build_app):
    """Only a real JSON bool counts; a hand-edited row folds to the safe OFF."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)
    log.append_event(
        "s1", "system", "user", {"event": CHAT_USER_AS_ACTOR_EVENT, "user_as_actor": "yes"}
    )
    log.append_event("s1", "system", "user", {"event": CHAT_USER_AS_ACTOR_EVENT})
    log.append_event(
        "s1", "system", "user", {"event": CHAT_USER_AS_ACTOR_EVENT, "user_as_actor": 1}
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/chat-config")

    assert run(scenario()).json()["user_as_actor"] is False
    assert resolve_user_as_actor(log, "s1") is False


def test_user_as_actor_patch_leaves_persona_and_memory_folds_untouched(build_app):
    """The session flag is its own event kind: no persona/memory row is written."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)
    log.append_event(
        "s1", "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": "p1"}
    )
    log.append_event("s1", "system", "user", {"event": CHAT_MEMORY_EVENT, "enabled": False})

    async def scenario():
        async with client_for(app) as client:
            await client.patch("/api/sessions/s1/chat-config", json={"user_as_actor": True})
            return await client.get("/api/sessions/s1/chat-config")

    body = run(scenario()).json()
    assert body == {
        "persona_id": "p1",
        "memory_enabled": False,
        "user_as_actor": True,
        # W9.24 §4: the dispatch policy rides the same read, still at defaults
        # (the actor PATCH writes no dispatch row).
        "npc_strategy": "decision",
        "npc_max_dispatch": 0,
    }
    kinds = [
        e.payload.get("event")
        for e in log.get_events("s1")
        if e.type == "system" and isinstance(e.payload, dict)
    ]
    assert kinds.count(CHAT_PERSONA_EVENT) == 1
    assert kinds.count(CHAT_MEMORY_EVENT) == 1
    assert kinds.count(CHAT_USER_AS_ACTOR_EVENT) == 1


# --- rejected writes append nothing ----------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"user_as_actor": "nope"},
        # 0/1 coerce to a bool exactly like the sibling toggles (MemoryPatch);
        # any other integer is not a boolean and is refused before the append.
        {"user_as_actor": 5},
        {"user_as_actor": None},
        {"persona_id": "p1"},
        {"user_as_actor": True, "memory_enabled": False},
    ],
)
def test_bad_body_is_422_and_writes_nothing(build_app, body):
    """Missing/non-bool/unknown keys fail validation before any append."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_chat(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.patch("/api/sessions/s1/chat-config", json=body)

    resp = run(scenario())
    assert resp.status_code == 422
    assert _setting_rows(log) == []
    assert resolve_user_as_actor(log, "s1") is False


def test_unknown_session_is_404_on_both_verbs_and_writes_nothing(build_app):
    """A ghost session cannot hold a setting: GET and PATCH both 404."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            got = await client.get("/api/sessions/ghost/chat-config")
            patched = await client.patch(
                "/api/sessions/ghost/chat-config", json={"user_as_actor": True}
            )
            return got, patched

    got, patched = run(scenario())
    assert got.status_code == 404
    assert patched.status_code == 404

    other = EventLog(log_path)
    assert other.get_events("ghost") == []
    other.close()
