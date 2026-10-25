"""Tests for Character Card V2 export (SURFACE #13, D28).

The exporter is the inverse of :mod:`fabulist.cards.v2` import: it wraps a card
field object back into the ``chara_card_v2`` envelope. These tests pin the exact
envelope, the sparse-field rule (empty keys are omitted, ``name`` is not), the
``Unknown`` fallback, and the no-mutation / no-aliasing guarantee.

D28 adds the lossless half: the raw-passthrough envelope (verbatim ``raw_data``,
unknown keys and order preserved), the PNG write path (``chara`` tEXt chunk
rewritten, ``IDAT`` untouched), and the ``export/card.png`` endpoint served
through the real app (asset retention + carrier fallback). The opt-in
``STCHARS_DIR`` sweep round-trips real-world cards when the env var is set.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.cards import CardError, PNG_SIGNATURE, parse_card_v2
from fabulist.cards.export import (
    CARD_SPEC_VERSION,
    DEFAULT_NAME,
    card_to_png,
    card_to_v2,
)
from fabulist.cards.v2 import _iter_chunks
from fabulist.media.images import find_definition_asset

SPEC = "chara_card_v2"

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cards"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


FULL_V2 = _fixture("full_v2.json")
FULL_DATA = FULL_V2["data"]
CARD_PNG = (FIXTURES / "full_v2.png").read_bytes()

FULL_CARD = {
    "name": "Lyra Voss",
    "description": "A wandering cartographer.",
    "personality": "Curious, dry-humored.",
    "scenario": "A rain-soaked night market.",
    "first_mes": "Lyra looks up from her maps. 'You lost?'",
    "mes_example": "<START>\n{{user}}: hello",
    "creator_notes": "Hand-tuned for slow burn.",
    "system_prompt": "Stay in third person.",
    "post_history_instructions": "Keep the weather continuous.",
    "tags": ["npc", "cartographer"],
    "character_book": {"entries": [{"keys": ["market"], "content": "busy"}]},
}


def test_full_card_round_trips_with_exact_envelope():
    out = card_to_v2(copy.deepcopy(FULL_CARD))

    assert out["spec"] == SPEC
    assert out["spec_version"] == CARD_SPEC_VERSION == "2.0"
    assert set(out) == {"spec", "spec_version", "data"}
    assert out["data"] == FULL_CARD


def test_partial_card_omits_empty_keys_but_keeps_name():
    card = {
        "name": "Bram",
        "description": "A retired soldier turned cook.",
        "personality": "Blunt.",
        # scenario / first_mes / mes_example / creator_notes ... absent
        "system_prompt": "",
        "tags": [],
        "character_book": {},
    }
    out = card_to_v2(card)
    data = out["data"]

    assert data["name"] == "Bram"
    assert data["description"] == card["description"]
    assert data["personality"] == "Blunt."
    # Empty/absent optionals are dropped entirely, not emitted as null/""/[]/{}.
    for key in (
        "scenario",
        "first_mes",
        "mes_example",
        "creator_notes",
        "system_prompt",
        "tags",
        "character_book",
    ):
        assert key not in data


def test_empty_card_yields_unknown_name_only():
    assert card_to_v2({}) == {
        "spec": SPEC,
        "spec_version": "2.0",
        "data": {"name": DEFAULT_NAME},
    }


def test_blank_and_whitespace_name_fall_back_to_unknown():
    for blank in ("", "   ", None):
        out = card_to_v2({"name": blank, "description": "d"})
        assert out["data"]["name"] == "Unknown"


def test_input_is_not_mutated():
    card = copy.deepcopy(FULL_CARD)
    snapshot = copy.deepcopy(card)

    card_to_v2(card)

    assert card == snapshot


def test_output_does_not_alias_input():
    card = copy.deepcopy(FULL_CARD)
    out = card_to_v2(card)

    out["data"]["tags"].append("mutated")
    out["data"]["character_book"]["entries"].append({"keys": ["x"]})

    assert card["tags"] == FULL_CARD["tags"]
    assert card["character_book"] == FULL_CARD["character_book"]


def test_missing_empty_name_key_still_exports_unknown():
    out = card_to_v2({"description": "no name at all"})
    assert out["data"] == {"name": "Unknown", "description": "no name at all"}


# --- raw passthrough (D28) ----------------------------------------------------------


def test_raw_passthrough_preserves_unknown_keys_and_order():
    card = {**FULL_DATA, "raw_data": FULL_DATA}

    out = card_to_v2(card)

    assert out["data"] == FULL_DATA
    assert list(out["data"]) == list(FULL_DATA)


def test_raw_passthrough_does_not_alias_source():
    card = {"raw_data": copy.deepcopy(FULL_DATA)}
    out = card_to_v2(card)

    out["data"]["tags"].append("mutated")
    out["data"]["extensions"]["depth_prompt"]["depth"] = 99

    assert card["raw_data"]["tags"] == FULL_DATA["tags"]
    assert card["raw_data"]["extensions"] == FULL_DATA["extensions"]


def test_raw_passthrough_without_name_inserts_unknown_first():
    out = card_to_v2({"raw_data": {"x": 1}})

    assert list(out["data"]) == ["name", "x"]
    assert out["data"]["name"] == DEFAULT_NAME


def test_field_assembly_includes_new_fields_when_no_raw_data():
    card = {
        "name": "Nyx",
        "description": "d",
        "alternate_greetings": ["a"],
        "creator": "fixture-author",
        "character_version": "1.0",
        "extensions": {"depth_prompt": {"depth": 4}},
    }
    data = card_to_v2(card)["data"]

    assert data["alternate_greetings"] == ["a"]
    assert data["creator"] == "fixture-author"
    assert data["character_version"] == "1.0"
    assert data["extensions"] == {"depth_prompt": {"depth": 4}}


def test_empty_raw_data_falls_back_to_field_assembly():
    out = card_to_v2({"name": "Bram", "description": "d", "raw_data": {}})

    assert out["data"] == {"name": "Bram", "description": "d"}


def test_parse_png_to_v2_to_parse_identity_and_key_order():
    card = parse_card_v2(CARD_PNG)

    envelope = card_to_v2(card.to_dict())
    assert envelope["data"] == FULL_DATA
    assert list(envelope["data"]) == list(FULL_DATA)

    again = parse_card_v2(envelope)
    assert again.raw_data == FULL_DATA
    assert list(again.raw_data) == list(FULL_DATA)


# --- PNG write path -----------------------------------------------------------------


def _chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    return list(_iter_chunks(png))


def _chara_text(png: bytes) -> bytes:
    for ctype, data in _iter_chunks(png):
        if ctype == b"tEXt" and data.split(b"\x00", 1)[0] == b"chara":
            return data
    raise AssertionError("exported PNG has no chara tEXt chunk")


def _idat_data(png: bytes) -> list[bytes]:
    return [data for ctype, data in _iter_chunks(png) if ctype == b"IDAT"]


def test_card_to_png_synthesizes_parseable_carrier():
    card = {
        "name": "Min",
        "description": "Exists.",
        "raw_data": {"name": "Min", "description": "Exists."},
    }

    png = card_to_png(card)

    assert png.startswith(PNG_SIGNATURE)
    parsed = parse_card_v2(png)
    assert parsed.name == "Min"
    assert parsed.raw_data == {"name": "Min", "description": "Exists."}


def test_card_to_png_parse_identity_with_raw_data():
    card = parse_card_v2(CARD_PNG)

    again = parse_card_v2(card_to_png(card.to_dict(), CARD_PNG))

    assert again.raw_data == card.raw_data
    assert list(again.raw_data) == list(card.raw_data)


def test_card_to_png_rewrites_chara_chunk_with_current_card():
    card = {"name": "Rewritten", "description": "New body."}

    out = card_to_png(card, CARD_PNG)

    text = _chara_text(out)
    envelope = json.loads(base64.b64decode(text.split(b"\x00", 1)[1]))
    assert envelope["data"]["name"] == "Rewritten"
    assert parse_card_v2(out).name == "Rewritten"


def test_card_to_png_leaves_idat_untouched():
    out = card_to_png({"name": "X", "description": "d"}, CARD_PNG)

    assert _idat_data(out) == _idat_data(CARD_PNG)


def test_card_to_png_replaces_chara_chunk_not_duplicates():
    out = card_to_png({"name": "X", "description": "d"}, CARD_PNG)

    chara = [
        data
        for ctype, data in _chunks(out)
        if ctype == b"tEXt" and data.split(b"\x00", 1)[0] == b"chara"
    ]
    assert len(chara) == 1


def test_card_to_png_rejects_corrupt_base():
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    broken = (
        PNG_SIGNATURE
        + struct.pack(">I", 13)
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
        + struct.pack(">I", 0)
        + b"IEND"
        + struct.pack(">I", 0)  # forced bad IEND CRC
    )
    with pytest.raises(CardError, match="CRC"):
        card_to_png({"name": "X", "description": "d"}, broken)


# --- export endpoint (app-level) ----------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.fixture
def build_app(tmp_path: Path):
    """A fresh app (cards + export routers mounted by the factory) per test."""
    log_path = tmp_path / "events.db"
    built = []

    def _build():
        app = create_app(FakeTurnEngine(), log_path)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def test_png_import_seed_references_the_definition_image(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/cards/import?seed=1",
                content=CARD_PNG,
                headers={"Content-Type": "image/png"},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    sid = resp.json()["session_id"]

    log = app.state.log
    card_events = [
        event
        for event in log.get_events(sid)
        if event.type == "system" and isinstance(event.payload.get("card"), dict)
    ]
    assert card_events, "seed system event missing"
    ref = card_events[0].payload.get("card_asset")
    assert isinstance(ref, str) and ref

    # ONE asset, on the definition anchor: the seed event references it and the
    # session stores no copy of the same bytes.
    assert [event for event in log.get_events(sid) if event.type == "asset"] == []
    asset = find_definition_asset(log, ref)
    assert asset is not None
    assert asset["payload"]["kind"] == "image"
    assert asset["payload"]["bytes_b64"]  # original image retained
    # The ref is the persona's own ``image_asset`` (the badge reads the same id).
    from fabulist.personas.registry import list_personas

    personas = list_personas(log)
    assert [persona.image_asset for persona in personas] == [ref]


def test_json_import_seed_has_no_card_asset(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/cards/import", json={**FULL_V2, "seed": True}
            )

    resp = run(scenario())
    sid = resp.json()["session_id"]
    systems = [
        event
        for event in app.state.log.get_events(sid)
        if event.type == "system" and "card" in event.payload
    ]
    assert systems
    assert "card_asset" not in systems[0].payload


def test_export_card_png_serves_parseable_png_with_stored_image(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/cards/import?seed=1",
                content=CARD_PNG,
                headers={"Content-Type": "image/png"},
            )
            sid = created.json()["session_id"]
            export = await client.get(f"/api/sessions/{sid}/export/card.png")
            return export

    export = run(scenario())
    assert export.status_code == 200
    assert export.headers["content-type"] == "image/png"
    assert "attachment" in export.headers["content-disposition"]
    assert parse_card_v2(export.content).name == FULL_DATA["name"]
    # Pixel data survives the chara-chunk rewrap.
    assert _idat_data(export.content) == _idat_data(CARD_PNG)


def test_export_card_png_reflects_current_card(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/cards/import?seed=1",
                content=CARD_PNG,
                headers={"Content-Type": "image/png"},
            )
            sid = created.json()["session_id"]
        # A later card event (an edit / re-import) becomes the current truth.
        app.state.log.append_event(
            sid,
            "system",
            "system",
            {"card": {"name": "Lyra Reborn", "description": "d"}, "first_mes": ""},
        )
        async with client_for(app) as client:
            return await client.get(f"/api/sessions/{sid}/export/card.png")

    export = run(scenario())
    assert export.status_code == 200
    assert parse_card_v2(export.content).name == "Lyra Reborn"


def test_export_card_png_without_stored_image_synthesizes_carrier(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/cards/import", json={**FULL_V2, "seed": True}
            )
            sid = created.json()["session_id"]
            return await client.get(f"/api/sessions/{sid}/export/card.png")

    export = run(scenario())
    assert export.status_code == 200
    assert parse_card_v2(export.content).name == FULL_DATA["name"]


def test_export_card_png_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/missing/export/card.png")

    assert run(scenario()).status_code == 404


def test_export_card_png_without_card_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.post("/api/sessions", json={"title": "no card"})
            listed = (await client.get("/api/sessions")).json()["sessions"]
            sid = listed[0]["session_id"]
            return await client.get(f"/api/sessions/{sid}/export/card.png")

    assert run(scenario()).status_code == 404


# --- opt-in real-world sweep --------------------------------------------------------

STCHARS_DIR = os.environ.get("STCHARS_DIR")


@pytest.mark.skipif(not STCHARS_DIR, reason="set STCHARS_DIR to sweep real cards")
def test_real_world_stchars_round_trip():
    """Round-trip every card in ``$STCHARS_DIR`` (opt-in; skipped when unset)."""
    root = Path(STCHARS_DIR)
    files = sorted([*root.glob("*.png"), *root.glob("*.json")])
    assert files, f"no *.png/*.json cards under {root}"

    for path in files:
        raw = path.read_bytes()
        source = raw if path.suffix == ".png" else json.loads(raw.decode("utf-8"))
        card = parse_card_v2(source)

        envelope = card_to_v2(card.to_dict())
        if card.raw_data is not None:
            assert envelope["data"] == card.raw_data, path.name
            assert list(envelope["data"]) == list(card.raw_data), path.name

        again = parse_card_v2(envelope)
        assert again.name == card.name, path.name
        for field in (
            "description",
            "personality",
            "scenario",
            "first_mes",
            "mes_example",
            "creator_notes",
            "system_prompt",
            "post_history_instructions",
            "tags",
            "alternate_greetings",
            "creator",
            "character_version",
            "extensions",
        ):
            assert getattr(again, field) == getattr(card, field), (path.name, field)

        base = raw if path.suffix == ".png" else None
        rewrapped = parse_card_v2(card_to_png(card.to_dict(), base))
        assert rewrapped.name == card.name, path.name
