"""Tests for the persona registry router (D16).

Exercises the real FastAPI app (httpx ASGI transport) with the personas router
mounted, against a tmp SQLite event log — mount pattern mirrors
``tests/test_state_api.py`` / ``tests/test_settings_api.py`` (``create_app`` +
``include_router``, close logs in teardown). The log owns everything (D9):
persona definitions fold the ``__personas__`` anchor session (latest-wins per
``persona_id``) and chat assignments are ``chat_persona`` system events on the
chat session (latest-wins, ``null`` = sandbox).

Also covers :func:`fabulist.personas.registry.backfill_from_cards` (card
sessions are seeded the way ``cards_api`` / ``to_session_seed`` write them:
``system`` payload ``{"card": {...}, "first_mes": ...}``) and runs
``scripts/backfill_personas.py`` once via subprocess against a temp log.

No pytest-asyncio in this repo, so async scenarios run via ``asyncio.run``
inside sync tests.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.personas_api import router
from fabulist.log import EventLog
from fabulist.memory.actor import append_chat_actor
from fabulist.personas.registry import (
    PERSONAS_ANCHOR,
    backfill_from_cards,
    chat_config_for,
    list_personas,
    persona_for_chat,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKFILL_SCRIPT = REPO_ROOT / "scripts" / "backfill_personas.py"

#: One card dict in the shape ``CardV2.to_dict`` emits.
CARD = {
    "name": "Alice",
    "description": "A wry detective.",
    "personality": "dry, observant",
    "scenario": "A rainy neon city.",
    "first_mes": "The rain never stops here.",
}


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
        # Integration parity: the app mounts chat_config_api beside personas
        # (the canonical chat-config/memory surface — personas' own copy was
        # deduped at integration to avoid FastAPI shadow-mounting).
        from fabulist.api.chat_config_api import router as chat_config_router

        app.include_router(chat_config_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create_session(client: httpx.AsyncClient, title: str = "A chat") -> str:
    """Create a real chat the way the app factory does (session_created anchor)."""
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


async def _create_persona(client: httpx.AsyncClient, **fields) -> dict:
    """POST a persona and return the wire dict."""
    body = {"name": "Alice"}
    body.update(fields)
    resp = await client.post("/api/personas", json=body)
    assert resp.status_code == 200
    return resp.json()["persona"]


def _seed_card_session(
    log: EventLog, session_id: str, card: dict | None = None
) -> None:
    """Seed a chat with a card event exactly like ``cards_api._seed_session``.

    Mirrors :func:`fabulist.cards.to_session_seed`: a ``session_created`` title
    anchor, then a ``system`` event whose payload carries ``payload["card"]``
    (plus the aligned ``first_mes`` greeting).
    """
    card = dict(card or CARD)
    log.append_event(
        session_id, "system", "system", {"title": "Card chat", "event": "session_created"}
    )
    log.append_event(
        session_id,
        "system",
        "system",
        {"card": card, "first_mes": card.get("first_mes", "")},
    )


# -- create / list / read ----------------------------------------------------


def test_create_list_and_get_persona(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await _create_persona(
                client,
                name="Alice",
                description="A wry detective.",
                personality="dry",
                scenario="neon city",
                first_mes="Hello.",
            )
            listed = (await client.get("/api/personas")).json()
            fetched = await client.get(f"/api/personas/{created['persona_id']}")
            return created, listed, fetched

    created, listed, fetched = run(scenario())
    assert created["persona_id"] and created["name"] == "Alice"
    assert created["description"] == "A wry detective."
    assert created["first_mes"] == "Hello."

    assert [p["persona_id"] for p in listed["personas"]] == [created["persona_id"]]
    assert fetched.status_code == 200
    assert fetched.json()["persona"] == created


def test_get_unknown_persona_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/personas/ghost")

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown persona"


def test_create_without_name_is_422_and_writes_nothing(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/personas", json={"name": "  "})

    resp = run(scenario())
    assert resp.status_code == 422

    log = EventLog(log_path)
    assert log.get_events(PERSONAS_ANCHOR) == []
    log.close()


def test_update_persona_with_same_id_is_latest_wins(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = await _create_persona(client, name="Alice", description="old")
            second = await client.post(
                "/api/personas",
                json={
                    "persona_id": first["persona_id"],
                    "name": "Alice",
                    "description": "new",
                },
            )
            listed = (await client.get("/api/personas")).json()
            return first, second, listed

    first, second, listed = run(scenario())
    assert second.status_code == 200
    assert second.json()["persona"]["persona_id"] == first["persona_id"]
    assert second.json()["persona"]["description"] == "new"
    # A single folded persona, updated — no duplicate catalog entry.
    assert len(listed["personas"]) == 1
    assert listed["personas"][0]["description"] == "new"


# -- assignment / chat-config ------------------------------------------------


def test_assign_persona_reflects_in_chat_config(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            persona = await _create_persona(client, name="Alice")
            assigned = await client.post(
                f"/api/sessions/{sid}/persona",
                json={"persona_id": persona["persona_id"]},
            )
            config = (await client.get(f"/api/sessions/{sid}/chat-config")).json()
            projection = (await client.get(f"/api/sessions/{sid}/events")).json()
            return sid, persona, assigned, config, projection

    sid, persona, assigned, config, projection = run(scenario())
    assert assigned.status_code == 200
    assert assigned.json() == {
        "session_id": sid,
        "persona_id": persona["persona_id"],
    }
    assert config == {
        "persona_id": persona["persona_id"],
        "memory_enabled": True,
        # W9.22 widened the shape: the session flag rides this same read.
        "user_as_actor": False,
        # W9.24 §3.5 widened it again: the NPC dispatch policy defaults.
        "npc_strategy": "decision",
        "npc_max_dispatch": 0,
    }
    # Persona assignment never touches narrative/state history.
    assert projection["narratives"] == []
    assert projection["state"] == {}

    # The write is a chat_persona system event on the chat session (D9).
    log = EventLog(log_path)
    rows = [
        e
        for e in log.get_events(sid)
        if e.payload.get("event") == "chat_persona"
    ]
    log.close()
    assert len(rows) == 1
    assert rows[0].payload == {
        "event": "chat_persona",
        "persona_id": persona["persona_id"],
    }


def test_reassign_latest_wins_explicit(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            first = await _create_persona(client, name="Alice")
            second = await _create_persona(client, name="Bob")
            for persona_id in (first["persona_id"], second["persona_id"]):
                resp = await client.post(
                    f"/api/sessions/{sid}/persona", json={"persona_id": persona_id}
                )
                assert resp.status_code == 200
            return (
                await client.get(f"/api/sessions/{sid}/chat-config")
            ).json(), second["persona_id"]

    config, second_id_value = run(scenario())
    assert config["persona_id"] == second_id_value


def test_delete_persona_unassigns_to_sandbox(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            persona = await _create_persona(client, name="Alice")
            await client.post(
                f"/api/sessions/{sid}/persona", json={"persona_id": persona["persona_id"]}
            )
            deleted = await client.delete(f"/api/sessions/{sid}/persona")
            config = (await client.get(f"/api/sessions/{sid}/chat-config")).json()
            return sid, persona["persona_id"], deleted, config

    sid, persona_id, deleted, config = run(scenario())
    assert deleted.status_code == 200
    assert deleted.json() == {"session_id": sid, "persona_id": None}
    assert config == {
        "persona_id": None,
        "memory_enabled": True,
        "user_as_actor": False,
        "npc_strategy": "decision",
        "npc_max_dispatch": 0,
    }

    # Append-only: the assignment row and the clearing null both survive (D9).
    log = EventLog(log_path)
    persona_rows = [
        e
        for e in log.get_events(sid)
        if e.payload.get("event") == "chat_persona"
    ]
    log.close()
    assert [e.payload["persona_id"] for e in persona_rows] == [persona_id, None]


def test_chat_config_reads_memory_toggle(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            app.state.log.append_event(
                sid, "system", "user", {"event": "chat_memory", "enabled": False}
            )
            return (await client.get(f"/api/sessions/{sid}/chat-config")).json()

    assert run(scenario()) == {
        "persona_id": None,
        "memory_enabled": False,
        "user_as_actor": False,
        "npc_strategy": "decision",
        "npc_max_dispatch": 0,
    }


def test_assign_unknown_persona_is_404_and_writes_nothing(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            resp = await client.post(
                f"/api/sessions/{sid}/persona", json={"persona_id": "nope"}
            )
            return sid, resp

    sid, resp = run(scenario())
    assert resp.status_code == 404
    assert "unknown persona" in resp.json()["detail"]

    # Nothing was appended for the rejected assignment.
    log = EventLog(log_path)
    assert [
        e for e in log.get_events(sid) if e.payload.get("event") == "chat_persona"
    ] == []
    log.close()


def test_assign_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            persona = await _create_persona(client, name="Alice")
            return await client.post(
                "/api/sessions/ghost/persona",
                json={"persona_id": persona["persona_id"]},
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


def test_delete_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.delete("/api/sessions/ghost/persona")

    assert run(scenario()).status_code == 404


def test_chat_config_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/ghost/chat-config")

    assert run(scenario()).status_code == 404


# -- backfill ----------------------------------------------------------------


def test_backfill_creates_personas_and_assigns_cards(build_app):
    build, log_path = build_app
    app = build()
    log = app.state.log
    _seed_card_session(log, "s1", CARD)
    other = {"name": "Bob", "description": "A pilot.", "first_mes": "Up we go."}
    _seed_card_session(log, "s2", other)
    # A second session importing the SAME card reuses the persona.
    _seed_card_session(log, "s3", CARD)

    result = backfill_from_cards(log)
    assert result == {"assigned": 3, "sessions": 3, "personas_created": 2}

    by_name = {p.name: p.persona_id for p in list_personas(log)}
    assert set(by_name) == {"Alice", "Bob"}
    assert persona_for_chat(log, "s1") == by_name["Alice"]
    assert persona_for_chat(log, "s3") == by_name["Alice"]
    assert persona_for_chat(log, "s2") == by_name["Bob"]

    # Idempotent: a second run appends nothing.
    again = backfill_from_cards(log)
    assert again == {"assigned": 0, "sessions": 3, "personas_created": 0}
    assert len(list_personas(log)) == 2


def test_backfill_skips_already_assigned_sessions(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_card_session(log, "s1", CARD)
    # Pre-assign s1 to a manually-created persona.
    persona_id = _append_persona(log)
    log.append_event(
        "s1", "system", "user", {"event": "chat_persona", "persona_id": persona_id}
    )

    result = backfill_from_cards(log)
    assert result["assigned"] == 0
    assert result["personas_created"] == 0
    assert result["sessions"] == 1
    # The existing assignment is untouched.
    assert persona_for_chat(log, "s1") == persona_id


def test_backfill_ignores_anchor_sessions_with_cards(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    # A card parked on a reserved anchor must never seed a chat persona.
    _seed_card_session(log, "__weird__", CARD)

    result = backfill_from_cards(log)
    assert result == {"assigned": 0, "sessions": 0, "personas_created": 0}
    assert list_personas(log) == []


def test_backfill_dry_run_writes_nothing(build_app):
    build, log_path = build_app
    app = build()
    log = app.state.log
    _seed_card_session(log, "s1", CARD)

    result = backfill_from_cards(log, dry_run=True)
    assert result == {"assigned": 1, "sessions": 1, "personas_created": 1}
    assert list_personas(log) == []
    assert persona_for_chat(log, "s1") is None

    log2 = EventLog(log_path)
    assert log2.get_events(PERSONAS_ANCHOR) == []
    log2.close()


def test_chat_config_for_registry_reads_assignments(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_card_session(log, "s1", CARD)
    backfill_from_cards(log)
    config = chat_config_for(log, "s1")
    assert config["persona_id"] == list_personas(log)[0].persona_id
    assert config["memory_enabled"] is True


def _append_persona(log: EventLog, name: str = "Manual") -> str:
    from fabulist.personas.registry import append_persona

    return append_persona(log, {"name": name, "description": "manual"})


# -- CLI script --------------------------------------------------------------


def test_backfill_script_runs_against_temp_log(tmp_path: Path):
    log_path = tmp_path / "cli.db"
    seed = EventLog(log_path)
    _seed_card_session(seed, "s1", CARD)
    seed.close()

    proc = subprocess.run(
        [sys.executable, str(BACKFILL_SCRIPT), "--log", str(log_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "APPLIED" in proc.stdout
    assert "personas_created" in proc.stdout

    log = EventLog(log_path)
    try:
        personas = list_personas(log)
        assert [p.name for p in personas] == ["Alice"]
        assert persona_for_chat(log, "s1") == personas[0].persona_id
    finally:
        log.close()


def test_backfill_script_dry_run(tmp_path: Path):
    log_path = tmp_path / "cli_dry.db"
    seed = EventLog(log_path)
    _seed_card_session(seed, "s1", CARD)
    seed.close()

    proc = subprocess.run(
        [sys.executable, str(BACKFILL_SCRIPT), "--log", str(log_path), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "DRY RUN" in proc.stdout

    log = EventLog(log_path)
    try:
        assert list_personas(log) == []
        assert persona_for_chat(log, "s1") is None
    finally:
        log.close()


# -- D39: card import captures a persona -------------------------------------


async def _import_card(client: httpx.AsyncClient, *, seed: bool) -> httpx.Response:
    """POST a JSON card to ``/api/cards/import`` (seed -> also create a chat)."""
    url = "/api/cards/import" + ("?seed=1" if seed else "")
    return await client.post(url, json={"card": dict(CARD)})


def test_card_import_seed_captures_persona(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            imported = await _import_card(client, seed=True)
            listed = (await client.get("/api/personas")).json()
            return imported, listed

    imported, listed = run(scenario())
    body = imported.json()
    assert imported.status_code == 200
    assert body["ok"] is True
    assert body["session_id"]  # the seeding path created the chat
    # The card is captured as a persona definition (D39) — not an assignment.
    assert [p["name"] for p in listed["personas"]] == [CARD["name"]]
    persona = listed["personas"][0]
    assert persona["description"] == CARD["description"]
    assert persona["personality"] == CARD["personality"]
    assert persona["scenario"] == CARD["scenario"]
    assert persona["first_mes"] == CARD["first_mes"]


def test_reimporting_same_card_updates_without_duplicating(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = await _import_card(client, seed=True)
            second = await _import_card(client, seed=True)
            listed = (await client.get("/api/personas")).json()
            return first, second, listed

    first, second, listed = run(scenario())
    assert first.status_code == 200
    assert second.status_code == 200  # re-import must not raise
    # D39 upsert: name-keyed, so the second import updates the first persona
    # rather than appending a duplicate.
    assert len(listed["personas"]) == 1
    assert listed["personas"][0]["name"] == CARD["name"]

    # Append-only log (D9): two definition rows for the SAME persona_id, and
    # exactly one id in the fold.
    log = EventLog(log_path)
    ids = [
        e.payload.get("persona_id")
        for e in log.get_events(PERSONAS_ANCHOR)
        if e.payload.get("event") == "persona"
    ]
    log.close()
    assert len(ids) == 2
    assert ids[0] == ids[1]


def test_card_import_preview_does_not_capture_persona(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            preview = await _import_card(client, seed=False)
            listed = (await client.get("/api/personas")).json()
            return preview, listed

    preview, listed = run(scenario())
    assert preview.status_code == 200
    assert preview.json()["ok"] is True
    assert "session_id" not in preview.json()  # preview seeds nothing
    assert listed["personas"] == []

    # A preview has NO side effects: the persona anchor is untouched (D9).
    log = EventLog(log_path)
    assert log.get_events(PERSONAS_ANCHOR) == []
    log.close()


# -- D39: persona delete is a tombstone --------------------------------------


def test_delete_persona_tombstones_and_hides_from_list(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            persona = await _create_persona(client, name="Alice")
            deleted = await client.delete(f"/api/personas/{persona['persona_id']}")
            listed = (await client.get("/api/personas")).json()
            fetched = await client.get(f"/api/personas/{persona['persona_id']}")
            return persona, deleted, listed, fetched

    persona, deleted, listed, fetched = run(scenario())
    assert deleted.status_code == 200
    assert deleted.json() == {"persona_id": persona["persona_id"], "deleted": True}
    assert listed["personas"] == []
    assert fetched.status_code == 404  # folded away, but rows survive

    # Append-only (D9/D10): never a row removal — the definition row and the
    # tombstone both remain queryable in seq order.
    log = EventLog(log_path)
    kinds = [
        e.payload.get("event")
        for e in log.get_events(PERSONAS_ANCHOR)
        if e.payload.get("event") in ("persona", "persona_deleted")
    ]
    log.close()
    assert kinds == ["persona", "persona_deleted"]


def test_delete_unknown_persona_is_404_and_writes_nothing(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.delete("/api/personas/ghost")

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown persona"

    log = EventLog(log_path)
    assert log.get_events(PERSONAS_ANCHOR) == []
    log.close()


def test_reappend_after_delete_resurrects_persona(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await _create_persona(client, name="Alice", description="old")
            await client.delete(f"/api/personas/{created['persona_id']}")
            revived = await client.post(
                "/api/personas",
                json={
                    "persona_id": created["persona_id"],
                    "name": "Alice",
                    "description": "revived",
                },
            )
            listed = (await client.get("/api/personas")).json()
            return created, revived, listed

    created, revived, listed = run(scenario())
    assert revived.status_code == 200
    # Latest-wins: the post-tombstone definition is the live one again.
    assert len(listed["personas"]) == 1
    assert listed["personas"][0]["persona_id"] == created["persona_id"]
    assert listed["personas"][0]["description"] == "revived"


# -- D39: scoped session lists -----------------------------------------------


_WORLD_REF = {
    "event": "world_ref",
    "world_id": "w1",
    "name": "Neverwinter",
    "scenario": "A cold frontier.",
}


def _wire_keys() -> set[str]:
    return {
        "session_id",
        "title",
        "updated_at",
        "turns",
        "persona_id",
        "persona_cleared",
        "world_id",
        "memory_enabled",
        # D53: the chat's ACTOR record rides alongside (its own wire keys).
        "actor_id",
        "actor_ids",
        # W9.26 §2.6: branches-derived superseded-parent flag (drawer badge).
        "compacted",
    }


def test_list_sessions_filters_by_actor_and_world(build_app):
    """D53: the D40 rail scope is the chat's ACTOR record (``chat_actor`` /
    ``?actor_id=``); the USER persona (``chat_persona`` / ``?persona_id=``) is
    a separate, still-filterable record. The old test wrote ``chat_persona`` and
    filtered by ``?persona_id=`` to mean "the chat belongs to this actor" — the
    exact conflation D53 splits."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            alice_chat = await _create_session(client, "Alice chat")
            bob_chat = await _create_session(client, "Bob chat")
            plain_chat = await _create_session(client, "Plain")
            alice = await _create_persona(client, name="Alice")
            bob = await _create_persona(client, name="Bob")
            log = app.state.log
            # D53: the ACTOR (the cast the chat is WITH) is the `chat_actor`
            # record — NOT `chat_persona`. The rail scopes on this.
            append_chat_actor(log, alice_chat, [alice["persona_id"]])
            append_chat_actor(log, bob_chat, [bob["persona_id"]])
            # Alice's chat ALSO carries a USER persona (the kept `chat_persona`).
            resp = await client.post(
                f"/api/sessions/{alice_chat}/persona",
                json={"persona_id": alice["persona_id"]},
            )
            assert resp.status_code == 200
            # Bob's chat was materialized from world w1 (D32 snapshot).
            log.append_event(bob_chat, "system", "system", dict(_WORLD_REF))
            unfiltered = (await client.get("/api/sessions")).json()
            by_actor_alice = (
                await client.get(
                    "/api/sessions", params={"actor_id": alice["persona_id"]}
                )
            ).json()
            by_actor_bob = (
                await client.get(
                    "/api/sessions", params={"actor_id": bob["persona_id"]}
                )
            ).json()
            by_persona_alice = (
                await client.get(
                    "/api/sessions", params={"persona_id": alice["persona_id"]}
                )
            ).json()
            by_world = (
                await client.get("/api/sessions", params={"world_id": "w1"})
            ).json()
            no_match = (
                await client.get("/api/sessions", params={"actor_id": "ghost"})
            ).json()
            return (
                alice_chat,
                bob_chat,
                plain_chat,
                unfiltered,
                by_actor_alice,
                by_actor_bob,
                by_persona_alice,
                by_world,
                no_match,
            )

    (
        alice_chat,
        bob_chat,
        plain_chat,
        unfiltered,
        by_actor_alice,
        by_actor_bob,
        by_persona_alice,
        by_world,
        no_match,
    ) = run(scenario())

    # Unfiltered behaviour is unchanged apart from the D40/D53 additions: the
    # actor + world keys are ON the wire so the rail scopes from one list call.
    assert {s["session_id"] for s in unfiltered["sessions"]} == {
        alice_chat,
        bob_chat,
        plain_chat,
    }
    assert all(set(s.keys()) == _wire_keys() for s in unfiltered["sessions"])

    # The rail scope reads the ACTOR record (D53), not the user persona.
    assert [s["session_id"] for s in by_actor_alice["sessions"]] == [alice_chat]
    assert [s["session_id"] for s in by_actor_bob["sessions"]] == [bob_chat]
    # The user-persona filter still reads `chat_persona` (kept, now distinct).
    assert [s["session_id"] for s in by_persona_alice["sessions"]] == [alice_chat]
    assert [s["session_id"] for s in by_world["sessions"]] == [bob_chat]
    # Sessions with no matching event are excluded when the filter is present.
    assert no_match["sessions"] == []


def test_list_sessions_actor_filter_is_latest_wins(build_app):
    """The actor scope folds the ``chat_actor`` record latest-wins (D53)."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client, "One chat")
            alice = await _create_persona(client, name="Alice")
            bob = await _create_persona(client, name="Bob")
            for persona_id in (alice["persona_id"], bob["persona_id"]):
                append_chat_actor(app.state.log, sid, [persona_id])
            by_alice = (
                await client.get(
                    "/api/sessions", params={"actor_id": alice["persona_id"]}
                )
            ).json()
            by_bob = (
                await client.get(
                    "/api/sessions", params={"actor_id": bob["persona_id"]}
                )
            ).json()
            return sid, by_alice, by_bob

    sid, by_alice, by_bob = run(scenario())
    assert by_alice["sessions"] == []  # superseded actor record
    assert [s["session_id"] for s in by_bob["sessions"]] == [sid]



# -- BUG B regression: persona chats carry a card seed event (greeting) ------


def test_create_session_with_persona_seeds_greeting_card(build_app):
    """Creating a chat from a persona materializes the persona as a card seed
    event, so ``GET /opening`` serves ``[first_mes, *alternate_greetings]`` and
    the narrator's character block sees the persona (BUG B)."""
    from fabulist.api.cards_api import router as cards_router
    from fabulist.context.facts import session_persona_block

    build, log_path = build_app
    app = build()
    # The Opening route lives on the cards router; mount it beside personas.
    app.include_router(cards_router)

    async def scenario():
        async with client_for(app) as client:
            persona = await _create_persona(
                client,
                name="Alice",
                description="A wry detective.",
                personality="dry, observant",
                scenario="A rainy neon city.",
                first_mes="The rain never stops here.",
                alternate_greetings=["A second opening.", "A third opening."],
            )
            resp = await client.post(
                "/api/sessions",
                json={"title": "Alice chat", "persona_id": persona["persona_id"]},
            )
            assert resp.status_code == 200
            sid = resp.json()["session_id"]
            opening = (await client.get(f"/api/sessions/{sid}/opening")).json()
            return sid, opening

    sid, opening = run(scenario())

    # (1) The Opening block serves the persona's greeting cycle.
    assert opening["greetings"] == [
        "The rain never stops here.",
        "A second opening.",
        "A third opening.",
    ]
    assert opening["index"] == 0
    assert opening["card"]["name"] == "Alice"
    assert opening["card"]["scenario"] == "A rainy neon city."

    # (2) The card seed event rides the log (D9): exactly one, import-shaped,
    #     with the greeting on the payload.
    log = EventLog(log_path)
    events = log.get_events(sid)
    card_events = [
        e
        for e in events
        if e.type == "system" and isinstance(e.payload.get("card"), dict)
    ]
    assert len(card_events) == 1
    payload = card_events[0].payload
    assert payload["card"]["name"] == "Alice"
    assert payload["card"]["alternate_greetings"] == [
        "A second opening.",
        "A third opening.",
    ]
    assert payload["first_mes"] == "The rain never stops here."

    # (3) The narrator's character block renders persona + opening scene.
    block = session_persona_block(log, sid)
    log.close()
    assert block is not None
    assert "### Character: Alice" in block
    assert "Opening scene (already established): The rain never stops here." in block


def test_create_session_with_persona_empty_greeting_still_seeds_card(build_app):
    """An empty ``first_mes`` still seeds the card event (the narrator's
    character block matters); the Opening cycle is a single empty variant the
    frontend hides."""
    from fabulist.api.cards_api import router as cards_router

    build, log_path = build_app
    app = build()
    app.include_router(cards_router)

    async def scenario():
        async with client_for(app) as client:
            persona = await _create_persona(client, name="Nameless", description="Quiet.")
            resp = await client.post(
                "/api/sessions",
                json={"title": "Quiet chat", "persona_id": persona["persona_id"]},
            )
            assert resp.status_code == 200
            sid = resp.json()["session_id"]
            opening = (await client.get(f"/api/sessions/{sid}/opening")).json()
            return sid, opening

    sid, opening = run(scenario())
    assert opening["greetings"] == [""]  # single empty variant -> block hidden
    assert opening["card"]["name"] == "Nameless"

    log = EventLog(log_path)
    card_events = [
        e
        for e in log.get_events(sid)
        if e.type == "system" and isinstance(e.payload.get("card"), dict)
    ]
    log.close()
    assert len(card_events) == 1
    assert card_events[0].payload["first_mes"] == ""
