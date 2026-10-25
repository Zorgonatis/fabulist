"""Definition images for personas/worlds (sessionless anchor store).

Personas and worlds are edited with no chat open (AGENTS.md non-negotiable 7),
so their images cannot live on a chat session. This covers the anchor-scope
storage in :mod:`fabulist.media.images`, the persona/world image routes in
:mod:`fabulist.api.personas_api` / :mod:`fabulist.api.worlds_api`, and the
card-import capture in :mod:`fabulist.api.cards_api` (persona mode retains the
PNG with no session; seed mode retains it ONCE, as the persona's image, and the
seed event only references it).

The real app factory mounts the cards/personas/worlds routers, so the scenarios
run over a single tmp SQLite log with the httpx ASGI transport (mirroring
``tests/test_personas_api.py``). No pytest-asyncio in this repo, so async
scenarios run via ``asyncio.run`` inside sync tests.
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

import fabulist.api.personas_api as personas_api
from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.media.images import (
    MEDIA_ANCHOR,
    find_definition_asset,
    store_definition_image,
)
from fabulist.personas.registry import list_personas

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rimage-payload"

#: A minimal PNG-embedded V2 card (same construction as tests/test_cards_api.py).
CARD_JSON = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Lyra Voss",
        "description": "A wandering cartographer.",
        "first_mes": "Lyra looks up from her maps. 'You lost?'",
        "scenario": "A rain-soaked night market.",
    },
}


def _chunk(ctype: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _png(chara_json: dict) -> bytes:
    from fabulist.cards import PNG_SIGNATURE

    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    payload = base64.b64encode(json.dumps(chara_json).encode("utf-8"))
    text = _chunk(b"tEXt", b"chara\x00" + payload)
    return PNG_SIGNATURE + ihdr + text + _chunk(b"IEND", b"")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for the real app factory (cards/personas/worlds mounted)."""
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


# ---------------------------------------------------------------------------
# media.images: anchor scope + idempotent (content-addressed) store
# ---------------------------------------------------------------------------


def test_store_definition_image_is_anchor_scoped_and_idempotent(log: EventLog):
    first = store_definition_image(log, PNG_BYTES, "image/png")
    second = store_definition_image(log, PNG_BYTES, "image/png")

    assert first == second  # same content address
    # A re-store adds no second anchor asset (idempotent re-import).
    anchor_rows = [
        e for e in log.get_events(MEDIA_ANCHOR) if e.type == "asset"
    ]
    assert len(anchor_rows) == 1
    item = find_definition_asset(log, first)
    assert base64.b64decode(item["payload"]["bytes_b64"]) == PNG_BYTES
    # The anchor is sessionless: an unrelated chat's projection stays empty.
    assert log.get_projection("some-chat")["assets"] == []


def test_store_image_default_scope_is_unchanged(log: EventLog):
    from fabulist.media.images import find_asset, store_image

    event = store_image(log, "s1", PNG_BYTES, "image/png")
    assert event.session_id == "s1"
    assert find_asset(log, "s1", event.payload["asset_id"]) is not None
    # Same bytes twice stays append-only on the session scope (existing behavior).
    store_image(log, "s1", PNG_BYTES, "image/png")
    assert len(log.get_projection("s1")["assets"]) == 2


# ---------------------------------------------------------------------------
# personas_api: attach + serve round-trip
# ---------------------------------------------------------------------------


def _make_persona(app, name: str = "Alice") -> dict:
    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/personas", json={"name": name})
            assert resp.status_code == 200
            return resp.json()["persona"]

    return run(scenario())


def test_persona_wire_carries_image_asset_default_none(build_app):
    build, _ = build_app
    persona = _make_persona(build())
    assert persona["image_asset"] is None


def test_persona_image_round_trip_raw_bytes(build_app):
    build, log_path = build_app
    app = build()
    persona = _make_persona(app)
    pid = persona["persona_id"]

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                f"/api/personas/{pid}/image",
                content=PNG_BYTES,
                headers={"content-type": "image/png"},
            )
            served = await client.get(f"/api/personas/{pid}/image")
            fetched = await client.get(f"/api/personas/{pid}")
            return posted, served, fetched

    posted, served, fetched = run(scenario())
    assert posted.status_code == 200
    body = posted.json()
    assert body["mime"] == "image/png"
    assert body["image_asset"]
    assert body["persona"]["image_asset"] == body["image_asset"]

    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    assert served.content == PNG_BYTES

    assert fetched.json()["persona"]["image_asset"] == body["image_asset"]

    # Log truth: exactly one anchor asset row and a re-appended persona.
    log = EventLog(log_path)
    anchor_assets = [e for e in log.get_events(MEDIA_ANCHOR) if e.type == "asset"]
    log.close()
    assert len(anchor_assets) == 1


def test_persona_image_accepts_data_url_body(build_app):
    build, _ = build_app
    app = build()
    pid = _make_persona(app, "Bea")["persona_id"]
    data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                f"/api/personas/{pid}/image",
                content=data_url,
                headers={"content-type": "text/plain"},
            )
            served = await client.get(f"/api/personas/{pid}/image")
            return posted, served

    posted, served = run(scenario())
    assert posted.status_code == 200
    assert posted.json()["mime"] == "image/png"
    assert served.content == PNG_BYTES


def test_persona_image_rejects_non_image_and_bad_data_url(build_app):
    build, _ = build_app
    app = build()
    pid = _make_persona(app, "Cy")["persona_id"]

    async def scenario():
        async with client_for(app) as client:
            non_image = await client.post(
                f"/api/personas/{pid}/image",
                content=PNG_BYTES,
                headers={"content-type": "application/octet-stream"},
            )
            bad_url = await client.post(
                f"/api/personas/{pid}/image",
                content="data:image/png;base64,not base64!!",
                headers={"content-type": "text/plain"},
            )
            empty = await client.post(
                f"/api/personas/{pid}/image",
                content=b"",
                headers={"content-type": "image/png"},
            )
            return non_image, bad_url, empty

    non_image, bad_url, empty = run(scenario())
    assert non_image.status_code == 415
    assert bad_url.status_code == 415
    assert empty.status_code == 422


def test_persona_image_oversize_is_413(build_app, monkeypatch):
    build, _ = build_app
    app = build()
    pid = _make_persona(app, "Dee")["persona_id"]
    monkeypatch.setattr(personas_api, "MAX_IMAGE_BYTES", 4)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                f"/api/personas/{pid}/image",
                content=b"12345",
                headers={"content-type": "image/png"},
            )

    assert run(scenario()).status_code == 413


def test_persona_image_unknown_and_missing_are_404(build_app):
    build, _ = build_app
    app = build()
    pid = _make_persona(app, "Eve")["persona_id"]

    async def scenario():
        async with client_for(app) as client:
            unknown = await client.post(
                "/api/personas/nope/image",
                content=PNG_BYTES,
                headers={"content-type": "image/png"},
            )
            no_image = await client.get(f"/api/personas/{pid}/image")
            return unknown, no_image

    unknown, no_image = run(scenario())
    assert unknown.status_code == 404
    assert no_image.status_code == 404


# ---------------------------------------------------------------------------
# worlds_api: attach + serve round-trip
# ---------------------------------------------------------------------------


def _make_world(app, name: str = "Night Market") -> dict:
    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/worlds", json={"name": name})
            assert resp.status_code == 200
            return resp.json()["world"]

    return run(scenario())


def test_world_wire_image_asset_default_none_and_round_trip(build_app):
    build, _ = build_app
    app = build()
    world = _make_world(app)
    wid = world["world_id"]
    assert world["image_asset"] is None

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                f"/api/worlds/{wid}/image",
                content=PNG_BYTES,
                headers={"content-type": "image/png"},
            )
            served = await client.get(f"/api/worlds/{wid}/image")
            listed = await client.get("/api/worlds")
            return posted, served, listed

    posted, served, listed = run(scenario())
    assert posted.status_code == 200
    body = posted.json()
    assert body["world"]["image_asset"] == body["image_asset"]
    assert served.content == PNG_BYTES
    assert served.headers["content-type"].startswith("image/png")
    assert listed.json()["worlds"][0]["image_asset"] == body["image_asset"]


def test_world_image_unknown_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/worlds/nope/image")

    assert run(scenario()).status_code == 404


# ---------------------------------------------------------------------------
# cards_api: import seeding retains the PNG
# ---------------------------------------------------------------------------


def _import_card(app, png: bytes, query: str) -> httpx.Response:
    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                f"/api/cards/import?{query}",
                content=png,
                headers={"content-type": "image/png"},
            )

    return run(scenario())


def test_persona_mode_png_import_sets_image_asset(build_app):
    build, log_path = build_app
    app = build()
    png = _png(CARD_JSON)

    resp = _import_card(app, png, "persona=1")
    assert resp.status_code == 200
    persona = resp.json()["persona"]
    assert persona["image_asset"]

    async def served():
        async with client_for(app) as client:
            image = await client.get(f"/api/personas/{persona['persona_id']}/image")
            sessions = await client.get("/api/sessions")
            return image, sessions

    body, listed = run(served())
    assert body.status_code == 200
    assert body.content == png
    # Definition-only: the pick wrote no session row.
    assert listed.json()["sessions"] == []

    log = EventLog(log_path)
    anchor_assets = [e for e in log.get_events(MEDIA_ANCHOR) if e.type == "asset"]
    log.close()
    assert len(anchor_assets) == 1


def test_persona_mode_png_reimport_adds_no_asset(build_app):
    build, log_path = build_app
    app = build()
    png = _png(CARD_JSON)

    first = _import_card(app, png, "persona=1").json()["persona"]
    second = _import_card(app, png, "persona=1").json()["persona"]

    assert first["persona_id"] == second["persona_id"]  # name-keyed upsert
    assert first["image_asset"] == second["image_asset"]

    log = EventLog(log_path)
    anchor_assets = [e for e in log.get_events(MEDIA_ANCHOR) if e.type == "asset"]
    personas = list_personas(log)
    log.close()
    # Content-addressed: the re-import reused the anchor asset row.
    assert len(anchor_assets) == 1
    assert len(personas) == 1
    assert personas[0].image_asset == first["image_asset"]


def test_seed_mode_png_import_sets_image_on_persona(build_app):
    build, log_path = build_app
    app = build()
    png = _png(CARD_JSON)

    resp = _import_card(app, png, "seed=1")
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]

    log = EventLog(log_path)
    personas = list_personas(log)
    events = log.get_events(session_id)
    session_assets = [e for e in events if e.type == "asset"]
    anchor_assets = [e for e in log.get_events(MEDIA_ANCHOR) if e.type == "asset"]
    log.close()
    assert len(personas) == 1
    assert personas[0].image_asset
    # ONE copy: the bytes live on the definition anchor, never on the session.
    assert session_assets == []
    assert len(anchor_assets) == 1
    card_event = next(
        e for e in events if e.type == "system" and "card" in e.payload
    )
    # The seed event's ref is the persona's own image asset (PNG re-export
    # resolves it through the definition store).
    assert card_event.payload["card_asset"] == personas[0].image_asset
