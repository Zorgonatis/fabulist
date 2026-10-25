"""Tests for the character-card import router (W2.10b).

Exercises the real FastAPI app (httpx ASGI transport) with the cards router
mounted and the scripted ``FakeTurnEngine``. Cards arrive both as raw PNG bytes
and as JSON; the seed path is checked against the event log itself (the log owns
everything, D9) and the session list derived from it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cards_api import router as cards_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.personas.registry import (
    PERSONAS_ANCHOR,
    PERSONA_EVENT,
    list_personas,
)

# --- PNG construction helpers (mirrors test_cards.py) -------------------------------


def _chunk(ctype: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _png(chara_json: dict) -> bytes:
    from fabulist.cards import PNG_SIGNATURE

    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    payload = base64.b64encode(json.dumps(chara_json).encode("utf-8"))
    text = _chunk(b"tEXt", b"chara\x00" + payload)
    return PNG_SIGNATURE + ihdr + text + _chunk(b"IEND", b"")


CARD_JSON = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Lyra Voss",
        "description": "A wandering cartographer.",
        "first_mes": "Lyra looks up from her maps. 'You lost?'",
        "scenario": "A rain-soaked night market.",
        "tags": ["npc", "cartographer"],
    },
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(cards_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# --- parse-only ---------------------------------------------------------------------


def test_json_v2_card_parses_without_seeding(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/cards/import", json=CARD_JSON)

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["card"]["name"] == "Lyra Voss"
    assert body["card"]["description"] == "A wandering cartographer."
    assert body["card"]["first_mes"].startswith("Lyra looks up")
    assert body["card"]["tags"] == ["npc", "cartographer"]
    assert "session_id" not in body


def test_raw_png_card_parses(build_app):
    build, _ = build_app
    app = build()
    png = _png(CARD_JSON)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/cards/import",
                content=png,
                headers={"Content-Type": "image/png"},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["card"]["name"] == "Lyra Voss"


def test_json_wrapper_card_key_accepted(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/cards/import", json={"card": CARD_JSON["data"]}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["card"]["name"] == "Lyra Voss"


def test_missing_name_is_precise_422(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/cards/import", json={"description": "no name"}
            )

    resp = run(scenario())
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert "name" in body["error"]


def test_empty_body_is_rejected(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/cards/import",
                content=b"",
                headers={"Content-Type": "application/json"},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert resp.json()["ok"] is False


def test_non_png_binary_is_rejected(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/cards/import",
                content=b"\x00\x01\x02\x03 not a card",
                headers={"Content-Type": "application/octet-stream"},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert resp.json()["ok"] is False


# --- seeding ------------------------------------------------------------------------


def test_seed_via_body_flag_creates_titled_session(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/cards/import", json={**CARD_JSON, "seed": True}
            )
            listed = (await client.get("/api/sessions")).json()
            return resp, listed

    resp, listed = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    sid = body["session_id"]
    assert body["title"] == "Lyra Voss"

    sessions = listed["sessions"]
    assert [s["session_id"] for s in sessions] == [sid]
    assert sessions[0]["title"] == "Lyra Voss"

    # The log carries the seed rows in order (D9: append-only truth).
    log = EventLog(log_path)
    events = log.get_events(sid)
    log.close()
    assert events[0].type == "system"
    assert events[0].payload["event"] == "session_created"
    assert events[0].payload["title"] == "Lyra Voss"
    # No fabricated player turn: the import is a truthful ``card_imported``
    # system marker, and there is no ``user`` row at all.
    assert [e for e in events if e.type == "user"] == []
    markers = [
        e for e in events if e.payload.get("event") == "card_imported"
    ]
    assert markers and markers[0].payload["name"] == "Lyra Voss"
    system_events = [
        e
        for e in events
        if e.type == "system" and "card" in e.payload
    ]
    assert system_events
    assert system_events[0].payload["card"]["name"] == "Lyra Voss"
    assert system_events[0].payload["first_mes"].startswith("Lyra looks up")
    # The rail's turn count is zero (the seed is not a turn).
    assert sessions[0]["turns"] == 0


def test_seed_via_query_flag_with_raw_png(build_app):
    build, _ = build_app
    app = build()
    png = _png(CARD_JSON)

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/cards/import?seed=1",
                content=png,
                headers={"Content-Type": "image/png"},
            )
            sid = resp.json()["session_id"]
            # The registry must resolve the seeded id for projection reads.
            projection = await client.get(f"/api/sessions/{sid}/events")
            opening = await client.get(f"/api/sessions/{sid}/opening")
            return resp, projection, opening

    resp, projection, opening = run(scenario())
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    assert resp.json()["title"] == "Lyra Voss"
    assert projection.status_code == 200
    assert projection.json()["session_id"] == sid
    # The projection carries NO fabricated player turn...
    body = projection.json()
    assert body["user_messages"] == []
    # ...but the card still renders (the Opening block reads the system event).
    assert opening.status_code == 200
    assert opening.json()["name"] == "Lyra Voss"
    assert opening.json()["greetings"][0].startswith("Lyra looks up")


def test_seeded_session_accepts_a_turn(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/cards/import", json={**CARD_JSON, "create_session": True}
            )
            sid = created.json()["session_id"]
            turn = await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "I approach the map table.", "kind": "in_character"},
            )
            return turn

    turn = run(scenario())
    assert turn.status_code == 200
    assert "text/event-stream" in turn.headers.get("content-type", "")


def test_legacy_import_seed_marker_is_hidden_at_read(build_app):
    """A pre-existing fabricated seed row is never read back as a turn.

    History is append-only (D9), so session ``legacy`` keeps its seq-2 marker
    row; every derived read must skip it while the real turns are untouched:
    the projection (the transcript source), the rail's turn count, and the
    inspector turn list.
    """
    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event(
            "legacy", "system", "system",
            {"title": "Tessa", "event": "session_created"},
        )
        # The legacy fabricated player turn.
        log.append_event(
            "legacy", "user", "user",
            {"text": "[Imported card: Tessa]", "kind": "in_character"},
        )
        log.append_event(
            "legacy", "system", "system",
            {"card": {"name": "Tessa"}, "first_mes": "Tessa looks up."},
        )
        log.append_event(
            "legacy", "user", "user",
            {"text": '"Are you sure?"', "kind": "in_character"},
        )
        log.append_event("legacy", "narrative", "narrator", {"prose": "She nods."})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            projection = await client.get("/api/sessions/legacy/events")
            listed = await client.get("/api/sessions")
            inspected = await client.get("/api/sessions/legacy/inspect/turns")
            return projection, listed, inspected

    projection, listed, inspected = run(scenario())
    # The fake bubble is gone; the real player turn is untouched.
    texts = [m["payload"].get("text") for m in projection.json()["user_messages"]]
    assert texts == ['"Are you sure?"']
    # The rail counts one turn, not two.
    rows = {s["session_id"]: s for s in listed.json()["sessions"]}
    assert rows["legacy"]["turns"] == 1
    # The inspector pairs the seed with no narrative any more.
    turns = inspected.json()["turns"]
    assert len(turns) == 1
    assert turns[0]["user_text"] == '"Are you sure?"'


def test_seed_false_creates_no_session(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.post("/api/cards/import", json={**CARD_JSON, "seed": False})
            return (await client.get("/api/sessions")).json()

    assert run(scenario())["sessions"] == []


def test_seed_path_still_upserts_persona(build_app):
    """D39 pin: the seed path keeps creating the persona definition."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/cards/import", json={**CARD_JSON, "seed": True})
            personas = (await client.get("/api/personas")).json()["personas"]
            return resp, personas

    resp, personas = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["session_id"]
    assert [p["name"] for p in personas] == ["Lyra Voss"]


# --- definition-only (D51 ?persona=1) -----------------------------------------------


def test_persona_query_flag_creates_persona_without_session(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/cards/import?persona=1", json=CARD_JSON)
            personas = (await client.get("/api/personas")).json()["personas"]
            sessions = (await client.get("/api/sessions")).json()["sessions"]
            return resp, personas, sessions

    resp, personas, sessions = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "session_id" not in body
    assert "title" not in body
    # The returned persona wire matches GET /api/personas byte-for-byte.
    assert body["card"]["name"] == "Lyra Voss"
    assert body["persona"] == personas[0]
    assert personas[0]["name"] == "Lyra Voss"
    assert personas[0]["description"] == "A wandering cartographer."
    # Definition-only: NO session, not even the anchor leaked into the list.
    assert sessions == []

    # The log owns the write (D9): one persona event on the __personas__ anchor
    # and no session events anywhere.
    log = EventLog(log_path)
    anchor_events = log.get_events(PERSONAS_ANCHOR)
    log.close()
    persona_events = [
        e for e in anchor_events if e.payload.get("event") == PERSONA_EVENT
    ]
    assert len(persona_events) == 1
    assert persona_events[0].payload["name"] == "Lyra Voss"


def test_persona_json_body_flag_is_equivalent(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/cards/import", json={**CARD_JSON, "persona": True}
            )
            sessions = (await client.get("/api/sessions")).json()["sessions"]
            return resp, sessions

    resp, sessions = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "session_id" not in body
    assert body["persona"]["name"] == "Lyra Voss"
    assert sessions == []

    log = EventLog(log_path)
    personas = list_personas(log)
    log.close()
    assert [p.name for p in personas] == ["Lyra Voss"]


def test_persona_reimport_upserts_single_persona(build_app):
    build, log_path = build_app
    app = build()
    updated = {
        **CARD_JSON,
        "data": {**CARD_JSON["data"], "description": "Now a royal spy."},
    }

    async def scenario():
        async with client_for(app) as client:
            first = await client.post("/api/cards/import?persona=1", json=CARD_JSON)
            second = await client.post("/api/cards/import?persona=1", json=updated)
            personas = (await client.get("/api/personas")).json()["personas"]
            return first, second, personas

    first, second, personas = run(scenario())
    assert first.status_code == 200 and second.status_code == 200
    # Same name -> same persona id (D39 name-keyed upsert, not a duplicate).
    assert first.json()["persona"]["persona_id"] == second.json()["persona"]["persona_id"]
    assert len(personas) == 1
    assert personas[0]["name"] == "Lyra Voss"
    assert personas[0]["description"] == "Now a royal spy."

    # Append-only: two definition rows, one folded persona.
    log = EventLog(log_path)
    assert len(list_personas(log)) == 1
    log.close()


def test_persona_and_seed_flags_conflict_422(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            query = await client.post(
                "/api/cards/import?persona=1&seed=1", json=CARD_JSON
            )
            body = await client.post(
                "/api/cards/import", json={**CARD_JSON, "persona": True, "seed": True}
            )
            sessions = (await client.get("/api/sessions")).json()["sessions"]
            return query, body, sessions

    query, body, sessions = run(scenario())
    assert query.status_code == 422
    assert query.json()["ok"] is False
    assert "one mode" in query.json()["error"]
    assert body.status_code == 422
    assert body.json()["ok"] is False
    # Neither mode ran: no session, no persona.
    assert sessions == []
