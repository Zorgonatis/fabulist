"""Tests for the typed card -> World import door (issue #40, owner ruling D3).

``POST /api/worlds/import`` accepts raw PNG card bytes (signature-sniffed, the
``profiles_api.py`` raw-PNG precedent) or card JSON (V2/V3 envelope, V1 flat).
ANY valid card imports as a World — the card's own prose is world content, so a
``character_book`` is a bonus, never a requirement. Only a non-card body is a
``422``.

Exercises the real FastAPI app (httpx ASGI transport) over a tmp SQLite log,
mirroring :mod:`tests.test_cards_api` (PNG construction) and
:mod:`tests.test_worlds` (world + book assertions). The log owns everything
(D9): the world folds the ``__worlds__`` anchor, the lorebook folds the
``__books__`` catalog, and no persona is written by this door.
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
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.worldinfo_api import router as worldinfo_router
from fabulist.log import EventLog
from fabulist.personas.registry import list_personas
from fabulist.worlds.registry import WORLD_REF_EVENT, list_worlds

# --- PNG construction helpers (mirrors test_cards_api) ------------------------------


def _chunk(ctype: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _png(chara_json: dict) -> bytes:
    from fabulist.cards import PNG_SIGNATURE

    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    payload = base64.b64encode(json.dumps(chara_json).encode("utf-8"))
    text = _chunk(b"tEXt", b"chara\x00" + payload)
    return PNG_SIGNATURE + ihdr + text + _chunk(b"IEND", b"")


BOOK = {
    "name": "Night Market Lore",
    "entries": [
        {
            "keys": ["lantern"],
            "content": "The night market opens only after the second bell.",
            "insertion_order": 1,
        }
    ],
}

CARD_V2 = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Lyra Voss",
        "description": "A wandering cartographer who maps the drowned quarter.",
        "scenario": "A rain-soaked night market, two bells past dusk.",
        "first_mes": "The rain drums on the canvas as you duck under it.",
        "alternate_greetings": [
            "You wake to the smell of salt and tar — the docks, before dawn.",
            "A lantern swings once, twice: someone is waiting.",
        ],
        "character_book": BOOK,
        "creator": "guild",
        "character_version": "1.2",
    },
}

CARD_V1_FLAT = {
    "name": "The Ashen Guild",
    "description": "A mercenary guild that runs the city's river trade.",
    "scenario": "The guild hall at the turn of the tide.",
    "first_mes": "A clerk does not look up from the ledger.",
    "character_book": BOOK,
}

CARD_V2_NO_BOOK = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Rook",
        "description": "A quiet archivist with too many keys.",
        "scenario": "The stacks below the observatory.",
    },
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """App over a tmp log; the worlds router is mounted by the app factory.

    The world-info router is included for the ``/api/books`` catalog read (the
    materialized lorebook's wire surface).
    """
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(worldinfo_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# --- PNG card with a lorebook -------------------------------------------------------


def test_png_card_with_book_creates_world(build_app):
    """A PNG card (with ``character_book``) imports as a World + a deduped Book."""
    build, _ = build_app
    app = build()
    png = _png(CARD_V2)

    async def scenario():
        async with client_for(app) as client:
            first = await client.post(
                "/api/worlds/import",
                content=png,
                headers={"Content-Type": "image/png"},
            )
            listed = await client.get("/api/worlds")
            books = await client.get("/api/books")
            personas = await client.get("/api/personas")
            world_id = first.json()["world"]["world_id"]
            image = await client.get(f"/api/worlds/{world_id}/image")
            second = await client.post(
                "/api/worlds/import",
                content=png,
                headers={"Content-Type": "application/octet-stream"},
            )
            books_after = await client.get("/api/books")
            return first, listed, books, personas, image, second, books_after

    first, listed, books, personas, image, second, books_after = run(scenario())

    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == {"ok", "world", "book_id"}
    assert body["ok"] is True
    world = body["world"]
    assert body["book_id"], "a PNG card with entries must materialize a book"
    assert world["name"] == "Lyra Voss"
    assert world["description"].startswith("A wandering cartographer")
    assert world["scenario"] == "A rain-soaked night market, two bells past dusk."
    # Issue #51: the card's OPENING carries over as the world's opening — the
    # card field names, unchanged, so the persona Opening block/swipe machinery
    # applies as-is. The scenario stays context and is NOT the opening.
    assert world["first_mes"] == "The rain drums on the canvas as you duck under it."
    assert world["alternate_greetings"] == [
        "You wake to the smell of salt and tar — the docks, before dawn.",
        "A lantern swings once, twice: someone is waiting.",
    ]
    assert world["cast"] == [], "this door writes NO persona"
    assert world["book_ids"] == [body["book_id"]]
    assert world["provenance"] == {
        "origin": "card_import",
        "card_name": "Lyra Voss",
        "creator": "guild",
        "character_version": "1.2",
    }
    assert world["image_asset"], "the PNG body is retained as the world image"
    assert "world_id" in world

    # The world folds from the __worlds__ anchor like any other world.
    assert listed.json()["worlds"] == [world]

    # The lorebook lands in the reusable catalog, named from the card's book.
    catalog = books.json()["books"]
    assert len(catalog) == 1
    book = catalog[0]
    assert book["book_id"] == body["book_id"]
    assert book["name"] == "Night Market Lore"
    assert book["provenance"]["origin"] == "card_import"
    assert book["entries"][0]["keys"] == ["lantern"]

    # No persona was created by the World door (D3: cast stays empty).
    assert personas.json()["personas"] == []
    assert list_personas(app.state.log) == []

    # The retained image is the original PNG bytes, content-addressed.
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == png

    # Re-import: a second world (no world-level dedupe, like the JSON door),
    # but the SAME book id (signature dedupe) and the SAME image asset.
    second_body = second.json()
    assert second.status_code == 200, second.text
    assert second_body["book_id"] == body["book_id"]
    assert second_body["world"]["world_id"] != world["world_id"]
    assert second_body["world"]["image_asset"] == world["image_asset"]
    assert len(list_worlds(app.state.log)) == 2
    assert len(books_after.json()["books"]) == 1


# --- the imported world's OPENING (issue #51) ----------------------------------


def test_imported_world_opens_the_chat_on_the_cards_first_message(build_app):
    """End-to-end owner scenario: import a card, start a chat, open on first_mes.

    The persona path is the spec: the chat's first message is the card's
    ``first_mes``, swiped through ``alternate_greetings`` in the pinned Opening
    block. The card arrives through the WORLD door here, so the world must carry
    the opening and the world-seeded chat must resolve it — the SCENARIO is
    context (the narrator's ``### World`` block), never the opening.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            imported = await client.post(
                "/api/worlds/import",
                content=_png(CARD_V2),
                headers={"Content-Type": "image/png"},
            )
            world = imported.json()["world"]
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            opening = await client.get(f"/api/sessions/{sid}/opening")
            swipe = await client.post(f"/api/sessions/{sid}/greeting", json={"index": 2})
            after = await client.get(f"/api/sessions/{sid}/opening")
            return world, sid, opening, swipe, after

    world, sid, opening, swipe, after = run(scenario())
    assert opening.status_code == 200, opening.text
    body = opening.json()
    assert body["greetings"] == [
        "The rain drums on the canvas as you duck under it.",
        "You wake to the smell of salt and tar — the docks, before dawn.",
        "A lantern swings once, twice: someone is waiting.",
    ]
    assert body["index"] == 0
    assert body["card"]["name"] == "Lyra Voss"
    assert world["scenario"] not in body["greetings"]

    assert swipe.status_code == 200, swipe.text
    assert after.json()["index"] == 2

    events = app.state.log.get_events(sid)
    # The opening is the SNAPSHOT, not a seeded narrative row.
    assert not [e for e in events if e.type == "narrative"]
    (ref,) = [e for e in events if e.payload.get("event") == WORLD_REF_EVENT]
    assert ref.payload["first_mes"] == CARD_V2["data"]["first_mes"]
    assert ref.payload["alternate_greetings"] == CARD_V2["data"]["alternate_greetings"]
    assert ref.payload["scenario"] == CARD_V2["data"]["scenario"]


def test_json_imported_world_opens_the_chat_on_the_cards_first_message(build_app):
    """The JSON door behaves identically to the PNG door for the opening."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            imported = await client.post("/api/worlds/import", json=CARD_V2)
            world = imported.json()["world"]
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            return await client.get(f"/api/sessions/{sid}/opening")

    opening = run(scenario())
    assert opening.status_code == 200, opening.text
    assert opening.json()["greetings"] == [
        CARD_V2["data"]["first_mes"],
        *CARD_V2["data"]["alternate_greetings"],
    ]


def test_a_greetingless_card_imports_a_world_with_no_opening(build_app):
    """No ``first_mes`` on the card -> no opening on the world (degrade)."""
    build, _ = build_app
    app = build()
    card = json.loads(json.dumps(CARD_V2_NO_BOOK))
    card["data"]["alternate_greetings"] = []

    async def scenario():
        async with client_for(app) as client:
            imported = await client.post("/api/worlds/import", json=card)
            world = imported.json()["world"]
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            return world, await client.get(f"/api/sessions/{sid}/opening")

    world, opening = run(scenario())
    assert world["first_mes"] == ""
    assert world["alternate_greetings"] == []
    assert [g.strip() for g in opening.json()["greetings"]] == [""]


# --- card JSON (V2 envelope / V1 flat) with a lorebook ------------------------------


def test_v2_json_card_with_book_imports_as_world(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/worlds/import", json=CARD_V2)
            books = await client.get("/api/books")
            return resp, books

    resp, books = run(scenario())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["book_id"]
    world = body["world"]
    assert world["name"] == "Lyra Voss"
    assert world["scenario"].startswith("A rain-soaked night market")
    assert world["cast"] == []
    assert world["book_ids"] == [body["book_id"]]
    assert world["provenance"]["origin"] == "card_import"
    assert world["provenance"]["card_name"] == "Lyra Voss"
    # JSON carries no image bytes.
    assert world["image_asset"] is None
    assert [b["book_id"] for b in books.json()["books"]] == [body["book_id"]]

    with EventLog(log_path) as log:
        assert [w.name for w in list_worlds(log)] == ["Lyra Voss"]
        assert list_personas(log) == []


def test_v1_flat_json_card_with_book_imports_as_world(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/worlds/import", json=CARD_V1_FLAT)

    resp = run(scenario())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    world = body["world"]
    assert world["name"] == "The Ashen Guild"
    assert world["description"].startswith("A mercenary guild")
    assert world["scenario"] == "The guild hall at the turn of the tide."
    # A V1 flat card's ``first_mes`` lands in the world's opening too; a V1
    # card carries no alternates, so the cycle is the first message alone.
    assert world["first_mes"] == "A clerk does not look up from the ledger."
    assert world["alternate_greetings"] == []
    assert world["book_ids"] == [body["book_id"]]
    assert body["book_id"]
    assert world["cast"] == []


# --- no lorebook is NOT an error ----------------------------------------------------


def test_card_without_character_book_imports_as_world(build_app):
    """D3: the card's own prose is world content; ``book_id`` is simply null."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/worlds/import", json=CARD_V2_NO_BOOK)
            books = await client.get("/api/books")
            return resp, books

    resp, books = run(scenario())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["book_id"] is None
    world = body["world"]
    assert world["name"] == "Rook"
    assert world["description"] == "A quiet archivist with too many keys."
    assert world["scenario"] == "The stacks below the observatory."
    # A card with no greeting imports as a world with no opening — the same
    # degradation as an empty persona ``first_mes`` (the Opening block hides).
    assert world["first_mes"] == ""
    assert world["alternate_greetings"] == []
    assert world["book_ids"] == []
    assert world["image_asset"] is None
    assert world["provenance"] == {"origin": "card_import", "card_name": "Rook"}
    assert books.json()["books"] == []

    with EventLog(log_path) as log:
        assert [w.name for w in list_worlds(log)] == ["Rook"]


def test_card_with_empty_character_book_imports_without_a_book(build_app):
    """An unusable/empty ``character_book`` materializes nothing (still a 200)."""
    build, _ = build_app
    app = build()
    card = json.loads(json.dumps(CARD_V2_NO_BOOK))
    card["data"]["character_book"] = {"name": "Empty", "entries": []}

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/worlds/import", json=card)

    resp = run(scenario())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["book_id"] is None
    assert body["world"]["book_ids"] == []


# --- refusals: only a non-card body is a 422 ----------------------------------------


def test_non_card_json_is_422_without_writing_anything(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/worlds/import", json={"hello": "world"})
            worlds = await client.get("/api/worlds")
            books = await client.get("/api/books")
            return resp, worlds, books

    resp, worlds, books = run(scenario())
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert "name" in body["error"], body["error"]
    assert body["detail"] == body["error"]
    assert worlds.json()["worlds"] == []
    assert books.json()["books"] == []


def test_empty_body_is_422(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/worlds/import",
                content=b"",
                headers={"Content-Type": "image/png"},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert "no card data" in body["error"]


def test_png_without_a_card_chunk_is_422(build_app):
    """A plain PNG (no ``chara`` tEXt chunk) is not a card — precise 422."""
    build, _ = build_app
    app = build()
    bare = _png(CARD_V2)
    bare = bare[: bare.index(b"tEXt") - 4] + bare[bare.index(b"IEND") - 4 :]

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/worlds/import",
                content=bare,
                headers={"Content-Type": "image/png"},
            )
            worlds = await client.get("/api/worlds")
            return resp, worlds

    resp, worlds = run(scenario())
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert "chara" in body["error"], body["error"]
    assert worlds.json()["worlds"] == []


def test_non_json_garbage_is_422(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/worlds/import",
                content=b"not a card at all",
                headers={"Content-Type": "text/plain"},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert resp.json()["ok"] is False
