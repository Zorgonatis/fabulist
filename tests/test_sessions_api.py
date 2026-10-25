"""Tests for the session management router (W2.13a).

Exercises the real FastAPI app (via httpx ASGI transport) plus the landed
router, with a scripted ``FakeTurnEngine`` so turn-count derivation is real.
The log stays the source of truth: renames and deletes append rows; nothing is
mutated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.sessions import router
from fabulist.log import EventLog
from fabulist.worlds.registry import append_world, create_world_chat


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps (with the sessions router mounted) sharing one log."""
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


async def _create(client: httpx.AsyncClient, title: str = "") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


async def _run_turn(client: httpx.AsyncClient, sid: str, content: str = "hi") -> None:
    resp = await client.post(
        f"/api/sessions/{sid}/turns",
        json={"content": content, "kind": "in_character"},
    )
    assert resp.status_code == 200


def test_list_is_empty_before_any_session(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions")).json()

    assert run(scenario()) == {"sessions": []}


def test_created_session_appears_with_title_and_zero_turns(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, title="Tavern night")
            return sid, (await client.get("/api/sessions")).json()

    sid, body = run(scenario())
    assert len(body["sessions"]) == 1
    entry = body["sessions"][0]
    assert entry["session_id"] == sid
    assert entry["title"] == "Tavern night"
    assert entry["turns"] == 0
    assert entry["updated_at"]


def test_turn_count_and_ordering_follow_the_log(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = await _create(client, title="First")
            second = await _create(client, title="Second")
            await _run_turn(client, first, "one")
            await _run_turn(client, first, "two")
            await _run_turn(client, second, "only")
            return (await client.get("/api/sessions")).json()

    body = run(scenario())
    by_title = {s["title"]: s for s in body["sessions"]}
    assert by_title["First"]["turns"] == 2
    assert by_title["Second"]["turns"] == 1
    # Newest activity first: "Second" was the last turn.
    assert body["sessions"][0]["title"] == "Second"


def test_rename_is_append_only_and_visible_in_list(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, title="Old name")
            before = EventLog(log_path)
            created_rows = [
                e.id for e in before.get_events(sid) if e.type == "system"
            ]
            before.close()
            resp = await client.patch(
                f"/api/sessions/{sid}", json={"title": "New name"}
            )
            assert resp.status_code == 200
            assert resp.json() == {"session_id": sid, "title": "New name"}
            listed = (await client.get("/api/sessions")).json()
            return sid, created_rows, listed

    sid, created_rows, listed = run(scenario())
    assert listed["sessions"][0]["title"] == "New name"

    # Original system rows survive; a revision row was added.
    after = EventLog(log_path)
    events = after.get_events(sid)
    after.close()
    assert all(row in [e.id for e in events] for row in created_rows)
    assert any(e.type == "revision" for e in events)


def test_second_rename_supersedes_the_first(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, title="A")
            await client.patch(f"/api/sessions/{sid}", json={"title": "B"})
            await client.patch(f"/api/sessions/{sid}", json={"title": "C"})
            return (await client.get("/api/sessions")).json()

    assert run(scenario())["sessions"][0]["title"] == "C"


def test_delete_soft_hides_but_rows_survive(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, title="Doomed")
            resp = await client.delete(f"/api/sessions/{sid}")
            assert resp.status_code == 200
            return sid, (await client.get("/api/sessions")).json()

    sid, listed = run(scenario())
    assert listed["sessions"] == []

    log = EventLog(log_path)
    events = log.get_events(sid)
    log.close()
    # The log rows survive; the hide is just another appended system event.
    assert any(
        e.type == "system" and e.payload.get("event") == "session_hidden"
        for e in events
    )
    assert any(e.payload.get("title") == "Doomed" for e in events)


def test_unknown_session_rename_and_delete_are_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            rename = await client.patch(
                "/api/sessions/nope", json={"title": "x"}
            )
            delete = await client.delete("/api/sessions/nope")
            return rename, delete

    rename, delete = run(scenario())
    assert rename.status_code == 404
    assert delete.status_code == 404


def test_empty_rename_is_rejected(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, title="Keep")
            return await client.patch(f"/api/sessions/{sid}", json={"title": "   "})

    assert run(scenario()).status_code == 422


# -- D40: the world layer rides the list row --------------------------------


def test_list_row_carries_world_id_and_keeps_existing_keys(build_app):
    """One unfiltered call carries the world layer, additively (D40)."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, title="Plain chat")
            return sid, (await client.get("/api/sessions")).json()

    sid, body = run(scenario())
    assert len(body["sessions"]) == 1
    entry = body["sessions"][0]
    # The new keys ride alongside the existing ones; nothing is renamed.
    assert set(entry) == {
        "session_id",
        "title",
        "updated_at",
        "turns",
        "persona_id",
        "persona_cleared",
        "world_id",
        "memory_enabled",
        # D53: the chat's ACTOR record, its own wire keys alongside the user persona.
        "actor_id",
        "actor_ids",
        # W9.26 §2.6: the superseded-era flag the drawer badges (always present;
        # False until a marker-bearing compaction child exists).
        "compacted",
    }
    # A plain session has no ``world_ref`` snapshot -> consistent null.
    assert entry["world_id"] is None
    # Existing keys keep their names, types and values.
    assert entry["session_id"] == sid
    assert entry["title"] == "Plain chat"
    assert entry["turns"] == 0
    assert isinstance(entry["updated_at"], str) and entry["updated_at"]
    assert entry["persona_id"] is None
    # A never-set chat is NOT flagged as explicitly cleared.
    assert entry["persona_cleared"] is False
    assert entry["memory_enabled"] is True
    # D53: a plain session records no actor -> null primary, empty set.
    assert entry["actor_id"] is None
    assert entry["actor_ids"] == []


def test_world_materialised_session_reports_its_world_id(build_app):
    """A D32-materialized chat reports its snapshot id in the same list call."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            log = app.state.log
            append_world(
                log,
                {
                    "world_id": "w1",
                    "name": "Neverwinter",
                    "scenario": "A cold frontier.",
                },
            )
            chat = create_world_chat(log, "w1")
            plain = await _create(client, title="Plain")
            listed = (await client.get("/api/sessions")).json()
            scoped = (
                await client.get("/api/sessions", params={"world_id": "w1"})
            ).json()
            return chat["session_id"], plain, listed, scoped

    sid, plain, listed, scoped = run(scenario())
    by_id = {s["session_id"]: s for s in listed["sessions"]}
    assert by_id[sid]["world_id"] == "w1"
    assert by_id[plain]["world_id"] is None
    # The server-side filter still agrees with the serialized key.
    assert [s["session_id"] for s in scoped["sessions"]] == [sid]


# -- D53: the chat's ACTOR record (chat_actor) rides the list row ------------


def test_chat_actor_folds_into_the_summary(build_app):
    """The folded ``chat_actor`` record lands as ``actor_id`` / ``actor_ids``."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, title="With a cast")
            app.state.log.append_event(
                sid,
                "system",
                "user",
                {"event": "chat_actor", "actor_ids": ["per-cast-1"]},
            )
            return sid, (await client.get("/api/sessions")).json()

    sid, body = run(scenario())
    entry = next(s for s in body["sessions"] if s["session_id"] == sid)
    assert entry["actor_id"] == "per-cast-1"
    assert entry["actor_ids"] == ["per-cast-1"]
    # D53: the actor record is wholly apart from the user persona.
    assert entry["persona_id"] is None


def test_chat_actor_latest_wins_and_empty_list_clears(build_app):
    """A later ``chat_actor`` supersedes; ``[]`` clears back to no actor."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, title="Cast churn")
            log = app.state.log
            log.append_event(
                sid, "system", "user", {"event": "chat_actor", "actor_ids": ["a1"]}
            )
            log.append_event(
                sid,
                "system",
                "user",
                {"event": "chat_actor", "actor_ids": ["a2", "a3"]},
            )
            after_replace = (await client.get("/api/sessions")).json()
            log.append_event(
                sid, "system", "user", {"event": "chat_actor", "actor_ids": []}
            )
            after_clear = (await client.get("/api/sessions")).json()
            return sid, after_replace, after_clear

    sid, after_replace, after_clear = run(scenario())
    replaced = next(s for s in after_replace["sessions"] if s["session_id"] == sid)
    assert replaced["actor_id"] == "a2"  # primary = first of the folded set
    assert replaced["actor_ids"] == ["a2", "a3"]
    cleared = next(s for s in after_clear["sessions"] if s["session_id"] == sid)
    assert cleared["actor_id"] is None
    assert cleared["actor_ids"] == []


def test_chat_actor_fold_is_chain_aware(build_app):
    """A branch inherits its parent's actor; a branch-local row supersedes it."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            parent = await _create(client, title="Parent")
            log = app.state.log
            log.append_event(
                parent, "system", "user", {"event": "chat_actor", "actor_ids": ["a1"]}
            )
            cut = log.get_events(parent)[-1].seq
            log.fork_session(parent, cut, "branch-d53")
            # A fork owns no history; give it one row so it is listed.
            log.append_event("branch-d53", "user", "user", {"text": "hi"})
            inherited = (await client.get("/api/sessions")).json()
            log.append_event(
                "branch-d53",
                "system",
                "user",
                {"event": "chat_actor", "actor_ids": ["a2"]},
            )
            overridden = (await client.get("/api/sessions")).json()
            return inherited, overridden

    inherited, overridden = run(scenario())
    by_id = {s["session_id"]: s for s in inherited["sessions"]}
    assert by_id["branch-d53"]["actor_ids"] == ["a1"]  # inherited chain record
    by_id = {s["session_id"]: s for s in overridden["sessions"]}
    assert by_id["branch-d53"]["actor_ids"] == ["a2"]  # branch-local wins


def test_list_sessions_filters_by_actor_id(build_app):
    """``?actor_id=`` is the D40/D51 rail scope: membership over the actor set."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            alice = await _create(client, title="Alice")
            bob = await _create(client, title="Bob")
            duo = await _create(client, title="Duo")
            plain = await _create(client, title="Plain")
            log = app.state.log
            log.append_event(
                alice, "system", "user", {"event": "chat_actor", "actor_ids": ["alice"]}
            )
            log.append_event(
                bob, "system", "user", {"event": "chat_actor", "actor_ids": ["bob"]}
            )
            log.append_event(
                duo,
                "system",
                "user",
                {"event": "chat_actor", "actor_ids": ["alice", "bob"]},
            )
            by_alice = (
                await client.get("/api/sessions", params={"actor_id": "alice"})
            ).json()
            by_bob = (
                await client.get("/api/sessions", params={"actor_id": "bob"})
            ).json()
            no_match = (
                await client.get("/api/sessions", params={"actor_id": "ghost"})
            ).json()
            return alice, bob, duo, plain, by_alice, by_bob, no_match

    alice, bob, duo, plain, by_alice, by_bob, no_match = run(scenario())
    # Membership: a two-actor chat belongs to each of its actors.
    assert {s["session_id"] for s in by_alice["sessions"]} == {alice, duo}
    assert {s["session_id"] for s in by_bob["sessions"]} == {bob, duo}
    # A chat with no recorded actor never matches; an unknown id matches nothing.
    assert plain not in {s["session_id"] for s in by_alice["sessions"]}
    assert no_match["sessions"] == []


# -- D53: create writes the OWNER (actor vs user persona) --------------------


def test_explicit_persona_create_records_the_actor_not_the_user_persona(build_app):
    """D53 split: an explicit ``persona_id`` on create is the chat's ACTOR
    (``chat_actor``); the USER persona (``persona_id``) stays null."""
    from fabulist.personas.registry import append_persona

    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            persona_id = append_persona(app.state.log, {"name": "Alice"})
            resp = await client.post(
                "/api/sessions",
                json={"title": "Alice chat", "persona_id": persona_id},
            )
            assert resp.status_code == 200
            sid = resp.json()["session_id"]
            listed = (await client.get("/api/sessions")).json()
            return persona_id, sid, listed

    persona_id, sid, listed = run(scenario())
    entry = next(s for s in listed["sessions"] if s["session_id"] == sid)
    assert entry["actor_id"] == persona_id
    assert entry["actor_ids"] == [persona_id]
    # The old conflation: this used to be ``chat_persona`` (the user persona).
    assert entry["persona_id"] is None


def test_pinned_default_create_records_the_user_persona_not_an_actor(build_app):
    """D53 split: a create with no explicit persona uses the pinned default as
    the USER persona (``chat_persona``) and records NO actor."""
    from fabulist.personas.registry import append_persona

    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            persona_id = append_persona(app.state.log, {"name": "Default User"})
            pinned = await client.patch(
                "/api/settings", json={"default_persona_id": persona_id}
            )
            assert pinned.status_code == 200
            sid = await _create(client, title="Pinned chat")
            listed = (await client.get("/api/sessions")).json()
            return persona_id, sid, listed

    persona_id, sid, listed = run(scenario())
    entry = next(s for s in listed["sessions"] if s["session_id"] == sid)
    assert entry["persona_id"] == persona_id  # the user persona (kept)
    assert entry["actor_id"] is None
    assert entry["actor_ids"] == []
