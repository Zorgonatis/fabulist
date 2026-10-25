"""Tests for the pinned default persona (user request: "pin a Default persona").

The pin lives on the reserved ``__settings__`` anchor as a latest-wins
``default_persona_changed`` system event (D9). It has three surfaces:

* ``GET/PATCH /api/settings`` — ``default_persona_id`` (+ label); unknown ids 404.
* **Create-time materialization** — ``POST /api/sessions`` and
  ``POST /api/worlds/{id}/chats`` append a real ``chat_persona`` row (near seq 2)
  when no persona is supplied and a default is pinned.
* **Read-time fallback** — ``persona_for_chat`` (and the ``{{user}}`` /
  ``{{persona}}`` resolver) fall back to the pin when a chat has no chat-local
  assignment, so an existing persona-less chat resolves the user's name live.
  Memory scope is deliberately NOT a fallback seam: the user persona is not a
  memory layer (owner ruling), so ``resolve_scope`` never consults the pin.

``tests/test_settings_api.py`` covers the toggle fold; this file covers the pin.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.settings import (
    DEFAULT_PERSONA_EVENT,
    SETTINGS_SESSION_ID,
    default_persona_id,
)
from fabulist.log import EventLog
from fabulist.memory.actor import fold_actors
from fabulist.memory.scope import (
    LAYER_ACTOR,
    collection_for,
    collection_for_actor,
    resolve_scope,
)
from fabulist.personas.registry import assigned_persona_id, persona_for_chat
from fabulist.prompts.variables import resolve_variables


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create_persona(client: httpx.AsyncClient, name: str) -> str:
    resp = await client.post("/api/personas", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["persona"]["persona_id"]


async def _pin(client: httpx.AsyncClient, persona_id: str | None):
    return await client.patch("/api/settings", json={"default_persona_id": persona_id})


def _settings_events(log: EventLog) -> list:
    return [
        event
        for event in log.get_events(SETTINGS_SESSION_ID)
        if event.type == "system"
        and event.payload.get("event") == DEFAULT_PERSONA_EVENT
    ]


def _chat_persona_events(log: EventLog, session_id: str) -> list:
    return [
        event
        for event in log.get_events(session_id)
        if event.type == "system"
        and event.payload.get("event") == "chat_persona"
    ]


# -- settings surface: fold + payload + validation ---------------------------


def test_get_has_no_default_persona_by_default(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/settings")).json()

    body = run(scenario())
    assert body["default_persona_id"] is None
    assert body["default_persona_label"] is None


def test_pin_sets_id_and_label_and_unpin_clears(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            pid = await _create_persona(client, "Alice")
            pinned = await _pin(client, pid)
            assert pinned.status_code == 200
            after_pin = pinned.json()
            unpinned = await _pin(client, None)
            assert unpinned.status_code == 200
            return pid, after_pin, unpinned.json(), await client.get("/api/settings")

    pid, after_pin, after_unpin, _ = run(scenario())
    assert after_pin["default_persona_id"] == pid
    assert after_pin["default_persona_label"] == "Alice"
    assert after_unpin["default_persona_id"] is None
    assert after_unpin["default_persona_label"] is None

    # Two writes on the anchor: pin then explicit unpin (D9 append-only).
    log = EventLog(log_path)
    events = _settings_events(log)
    log.close()
    assert [e.payload.get("persona_id") for e in events] == [pid, None]


def test_pin_is_latest_wins_across_app_rebuild(build_app):
    build, _ = build_app

    async def pin(pid):
        async with client_for(build()) as client:
            resp = await _pin(client, pid)
            assert resp.status_code == 200
            return resp.json()

    async def create_persona(name):
        async with client_for(build()) as client:
            return await _create_persona(client, name)

    p1 = run(create_persona("Alice"))
    p2 = run(create_persona("Bob"))
    run(pin(p1))
    run(pin(p2))

    async def read():
        async with client_for(build()) as client:
            return (await client.get("/api/settings")).json()

    body = run(read())
    assert body["default_persona_id"] == p2
    assert body["default_persona_label"] == "Bob"


def test_pin_unknown_persona_is_404_and_appends_nothing(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await _pin(client, "ghost-persona")

    resp = run(scenario())
    assert resp.status_code == 404
    assert "ghost-persona" in resp.json()["detail"]["reason"]

    log = EventLog(log_path)
    assert _settings_events(log) == []
    log.close()


def test_toggles_only_patch_never_touches_the_pin(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            # A toggles-only PATCH must not clear the pin (absent key).
            await client.patch("/api/settings", json={"toggles": {"world_facts": False}})
            return (await client.get("/api/settings")).json()

    body = run(scenario())
    assert body["default_persona_id"] is not None
    assert body["toggles"]["world_facts"]["enabled"] is False


# -- create-time materialization ---------------------------------------------


def test_create_session_materializes_pinned_default_near_seq_two(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            resp = await client.post("/api/sessions", json={"title": "No persona"})
            assert resp.status_code == 200
            return pid, resp.json()["session_id"]

    pid, sid = run(scenario())
    log = app.state.log
    rows = _chat_persona_events(log, sid)
    assert len(rows) == 1
    assert rows[0].payload["persona_id"] == pid
    # The row lands immediately after session_created (inside a later fork's
    # chain window, not appended post-turn).
    assert rows[0].seq == 2


def test_create_session_without_pin_appends_no_persona_row(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/sessions", json={"title": "Sandbox"})
            return resp.json()["session_id"]

    sid = run(scenario())
    assert _chat_persona_events(app.state.log, sid) == []
    assert persona_for_chat(app.state.log, sid) is None


def test_create_session_explicit_persona_is_the_actor_not_the_user(build_app):
    """D53: an explicit ``persona_id`` at create is the chat's ACTOR (owner).

    It lands as a ``chat_actor`` event (NOT ``chat_persona``), so the memory
    scope fences to the ACTOR layer while ``{{user}}`` still reads the USER
    persona (here, the pinned default).
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            default = await _create_persona(client, "Alice")
            explicit = await _create_persona(client, "Bob")
            await _pin(client, default)
            resp = await client.post("/api/sessions", json={"persona_id": explicit})
            assert resp.status_code == 200
            return default, explicit, resp.json()["session_id"]

    default, explicit, sid = run(scenario())
    log = app.state.log
    # No USER-persona row on the explicit path — the id is the actor record.
    assert _chat_persona_events(log, sid) == []
    assert fold_actors(log, sid) == (explicit,)
    scope = resolve_scope(log, sid)
    assert scope.layer == LAYER_ACTOR
    assert scope.actor_ids == (explicit,)
    assert scope.collection == collection_for_actor(explicit)
    # The USER persona is still the pin (never the explicit actor).
    assert persona_for_chat(log, sid) == default


def test_unpin_restores_today_behaviour(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            await _pin(client, None)  # clear before creating
            resp = await client.post("/api/sessions", json={"title": "After unpin"})
            return pid, resp.json()["session_id"]

    pid, sid = run(scenario())
    log = app.state.log
    assert _chat_persona_events(log, sid) == []
    assert persona_for_chat(log, sid) is None
    assert resolve_scope(log, sid).collection == collection_for(sid)


# -- read-time fallback -------------------------------------------------------


def test_existing_persona_less_chat_resolves_the_default_live(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            # Create the chat BEFORE any pin exists (the observed case).
            sid = (await client.post("/api/sessions", json={"title": "Legacy"})).json()[
                "session_id"
            ]
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            return pid, sid

    pid, sid = run(scenario())
    log = app.state.log
    # No chat-local assignment row, yet the effective persona is the pin.
    assert assigned_persona_id(log, sid) is None
    assert persona_for_chat(log, sid) == pid
    # MEMORY is deliberately NOT re-scoped by the pin (owner ruling: the user
    # persona is not a memory layer; memory fences where it is used).
    scope = resolve_scope(log, sid)
    assert scope.persona_id is None
    assert scope.collection == collection_for(sid)
    # {{user}} / {{persona}} gain the pinned name.
    values = resolve_variables(log, sid, "I wave.")
    assert values["user"] == "Alice"
    assert values["persona"] == "Alice"


def test_pin_never_rescopes_memory(build_app):
    """A pinned default persona must not fence memory (no chat-local parent ->
    the chat keeps its chat-local collection)."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = (await client.post("/api/sessions", json={"title": "Sandbox"})).json()[
                "session_id"
            ]
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            # Explicitly clear any create-time materialization so the chat is
            # genuinely parent-less, then re-check the scope.
            await client.patch(f"/api/sessions/{sid}/persona", json={"persona_id": None})
            return pid, sid

    pid, sid = run(scenario())
    log = app.state.log
    assert resolve_scope(log, sid).persona_id is None
    assert resolve_scope(log, sid).collection == collection_for(sid)


def test_explicit_chat_assignment_wins_over_the_default(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            default = await _create_persona(client, "Alice")
            other = await _create_persona(client, "Bob")
            await _pin(client, default)
            sid = (await client.post("/api/sessions", json={})).json()["session_id"]
            # Explicitly reassign the chat to Bob.
            resp = await client.post(
                f"/api/sessions/{sid}/persona", json={"persona_id": other}
            )
            assert resp.status_code == 200
            return default, other, sid

    default, other, sid = run(scenario())
    log = app.state.log
    assert assigned_persona_id(log, sid) == other
    assert persona_for_chat(log, sid) == other
    # D53 supersedes D16's persona memory fence: ``chat_persona`` is the USER
    # persona (recorded on the scope), not a layer selector, so with no world
    # and no chat_actor the chat stays at its chat-local layer.
    scope = resolve_scope(log, sid)
    assert scope.user_persona_id == other
    assert scope.collection == collection_for(sid)
    assert resolve_variables(log, sid, "hi")["user"] == "Bob"


def test_unpinning_returns_a_persona_less_chat_to_sandbox(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = (await client.post("/api/sessions", json={})).json()["session_id"]
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            assert persona_for_chat(app.state.log, sid) == pid
            await _pin(client, None)
            return sid

    sid = run(scenario())
    log = app.state.log
    assert persona_for_chat(log, sid) is None
    assert resolve_scope(log, sid).collection == collection_for(sid)
    # PART 2: with no persona in effect, ``{{user}}`` resolves to "user".
    assert resolve_variables(log, sid, "hi")["user"] == "user"


# -- world-chat materialization ----------------------------------------------


def _seed_world(app, persona_id: str) -> str:
    log = app.state.log
    from fabulist.worlds.registry import append_world

    return append_world(log, {"name": "Aurora", "cast": [persona_id]})


def test_world_chat_materializes_pinned_default(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            cast = await _create_persona(client, "Lyra")
            default = await _create_persona(client, "Alice")
            await _pin(client, default)
            world_id = _seed_world(app, cast)
            resp = await client.post(f"/api/worlds/{world_id}/chats", json={})
            assert resp.status_code == 200
            return default, resp.json()["session_id"]

    default, sid = run(scenario())
    rows = _chat_persona_events(app.state.log, sid)
    assert [r.payload["persona_id"] for r in rows] == [default]
    assert persona_for_chat(app.state.log, sid) == default


def test_world_chat_without_pin_appends_no_persona_row(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            cast = await _create_persona(client, "Lyra")
            world_id = _seed_world(app, cast)
            resp = await client.post(f"/api/worlds/{world_id}/chats", json={})
            assert resp.status_code == 200
            return resp.json()["session_id"]

    sid = run(scenario())
    assert _chat_persona_events(app.state.log, sid) == []


def test_world_chat_explicit_persona_wins_and_unknown_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            cast = await _create_persona(client, "Lyra")
            default = await _create_persona(client, "Alice")
            explicit = await _create_persona(client, "Bob")
            await _pin(client, default)
            world_id = _seed_world(app, cast)
            ok = await client.post(
                f"/api/worlds/{world_id}/chats", json={"persona_id": explicit}
            )
            bad = await client.post(
                f"/api/worlds/{world_id}/chats", json={"persona_id": "ghost"}
            )
            return explicit, ok, bad

    explicit, ok, bad = run(scenario())
    assert ok.status_code == 200
    rows = _chat_persona_events(app.state.log, ok.json()["session_id"])
    assert [r.payload["persona_id"] for r in rows] == [explicit]
    assert bad.status_code == 404


# -- helpers ------------------------------------------------------------------


def test_default_persona_id_fold_reads_the_anchor(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    assert default_persona_id(log) is None

    async def pin_flow():
        async with client_for(app) as client:
            pid = await _create_persona(client, "Alice")
            return pid, (await _pin(client, pid)).json()

    pid, body = run(pin_flow())
    assert body["default_persona_id"] == pid
    assert default_persona_id(log) == pid


# -- guardrail: asset CRUD stays offline / pin never rewrites definitions -----


def test_persona_crud_is_independent_of_the_pin_and_of_chats(build_app):
    """Persona create/edit/delete works with no chat open and is untouched by the pin."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            # No chat has been created, nothing pinned: CRUD must still work
            # (offline editable nature).
            before_chats = (await client.get("/api/sessions")).json()["sessions"]
            created = await client.post(
                "/api/personas", json={"name": "Alice", "description": "offline"}
            )
            assert created.status_code == 200
            pid = created.json()["persona"]["persona_id"]

            # Pin Alice, then EDIT the definition: the pin must not gate or
            # rewrite the asset, and editing must not move the pin.
            await _pin(client, pid)
            edited = await client.post(
                "/api/personas",
                json={"name": "Alice Edited", "description": "offline", "persona_id": pid},
            )
            assert edited.status_code == 200
            assert edited.json()["persona"]["name"] == "Alice Edited"
            settings = (await client.get("/api/settings")).json()
            assert settings["default_persona_id"] == pid
            fetched = (await client.get(f"/api/personas/{pid}")).json()["persona"]
            assert fetched["name"] == "Alice Edited"
            assert fetched["description"] == "offline"

            # Delete is a tombstone; the pin event is a pure fold and survives
            # it (no cross-write between the asset catalog and the setting).
            deleted = await client.delete(f"/api/personas/{pid}")
            assert deleted.status_code == 200
            assert (await client.get(f"/api/personas/{pid}")).status_code == 404
            after_delete = (await client.get("/api/settings")).json()
            assert after_delete["default_persona_label"] is None  # id gone from catalog

            # Unpin and confirm the catalog is exactly as the CRUD left it.
            await _pin(client, None)
            catalog = (await client.get("/api/personas")).json()["personas"]
            assert [p["name"] for p in catalog] == []
            after_chats = (await client.get("/api/sessions")).json()["sessions"]
            return before_chats, after_chats, pid

    before_chats, after_chats, _ = run(scenario())
    # Persona CRUD and pinning created no chats (no coupling to session state).
    assert before_chats == after_chats == []


def test_read_time_fallback_never_writes_definitions_or_chat_rows(build_app):
    """The fallback only RESOLVES: no persona rewrite, no chat_persona materialization."""
    build, _ = build_app
    app = build()
    log = app.state.log

    async def scenario():
        async with client_for(app) as client:
            sid = (await client.post("/api/sessions", json={})).json()["session_id"]
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            return sid, pid

    sid, pid = run(scenario())
    from fabulist.personas.registry import PERSONAS_ANCHOR

    anchor_before = len(log.get_events(PERSONAS_ANCHOR))

    # Resolve through every fallback seam, repeatedly.
    assert persona_for_chat(log, sid) == pid
    # Memory scope is NOT a fallback seam any more (user persona is not a
    # memory layer) — it must stay chat-local.
    assert resolve_scope(log, sid).persona_id is None
    assert resolve_variables(log, sid, "hi")["user"] == "Alice"
    assert persona_for_chat(log, sid) == pid

    # No definition writes, and the chat gained no chat_persona row (the
    # fallback is read-only; only resolve_variables' pre-existing cast_snapshot
    # memo may append, never a persona row).
    assert len(log.get_events(PERSONAS_ANCHOR)) == anchor_before
    assert _chat_persona_events(log, sid) == []
