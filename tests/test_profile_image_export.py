"""Profile PNG export/import (owner ruling #7).

A saved profile exports as JSON **and** as PNG: the PNG body is the profile's
assigned image and the full envelope JSON rides in a ``fabulist_profile`` tEXt
chunk. Import accepts either representation.

Acceptance coverage:

1. the PNG body IS the assigned image (its ``IDAT`` bytes ride through
   unchanged) and the envelope is recoverable from the chunk;
2. layer order is pinned: world first, then the cast in ``world.cast`` order,
   skipping layers whose ``image_asset`` does not resolve;
3. no resolvable image -> ``404`` (no fabricated carrier PNG);
4. a non-PNG assigned image -> ``422`` naming the actual mime;
5. the JSON export route is unchanged (same envelope, ``application/json``,
   ``.profile.json`` filename);
6. ``POST /api/profiles/import`` consumes a PNG body (PNG signature detected,
   content type irrelevant) and a PNG without the chunk is a precise ``422``;
7. a PNG body whose images were imported into another log lands the same
   entities the JSON body does.

No pytest-asyncio in this repo, so async API scenarios run via ``asyncio.run``.
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
from fabulist.cards import PNG_SIGNATURE
from fabulist.cards.png import minimal_png
from fabulist.log import EventLog
from fabulist.media.images import (
    MEDIA_ANCHOR,
    find_definition_asset,
    sha256_hex,
    store_definition_image,
)
from fabulist.personas.registry import append_persona
from fabulist.profiles.exporter import export_profile
from fabulist.profiles.image import assigned_image_refs
from fabulist.profiles.importer import persist_profile_image
from fabulist.profiles.png import (
    PROFILE_CHUNK_KEYWORD,
    ProfilePngError,
    profile_from_png,
    profile_image_png,
    profile_to_png,
)
from fabulist.prompts.preset_store import append_preset
from fabulist.worlds.registry import append_world, world_by_id

STAMP = "2026-01-01T00:00:00+00:00"

#: A 4-byte payload nothing else in the tests uses; doubles as an image "tag".
WORLD_PIXELS = b"\x01\x02\x03\x04"
ALICE_PIXELS = b"\x11\x12\x13\x14"
BOB_PIXELS = b"\x21\x22\x23\x24"

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 8  # enough to be a non-PNG


# -- PNG helpers (test-local framing, independent of the implementation) ------


def _chunk(ctype: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _png(pixels: bytes, keyword: bytes | None = None, payload: bytes = b"") -> bytes:
    """A structurally valid 1x1 RGB PNG carrying optional text-chunk payload."""
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    chunks = [PNG_SIGNATURE, ihdr]
    if keyword is not None:
        chunks.append(_chunk(b"tEXt", keyword + b"\x00" + payload))
    chunks.append(_chunk(b"IDAT", zlib.compress(b"\x00" + pixels)))
    chunks.append(_chunk(b"IEND", b""))
    return b"".join(chunks)


def _chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    """``[(type, data), ...]`` for a PNG, without CRC validation."""
    out: list[tuple[bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        ctype = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        out.append((ctype, data))
        offset += 12 + length
        if ctype == b"IEND":
            break
    return out


def _idat(png: bytes) -> bytes:
    """The concatenated ``IDAT`` payload (the image's pixel data)."""
    return b"".join(data for ctype, data in _chunks(png) if ctype == b"IDAT")


def _profile_chunks(png: bytes) -> list[bytes]:
    """Every ``fabulist_profile`` text payload in a PNG, in file order."""
    return [
        data.split(b"\x00", 1)[1]
        for ctype, data in _chunks(png)
        if ctype == b"tEXt" and data.startswith(PROFILE_CHUNK_KEYWORD + b"\x00")
    ]


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for a fresh file-backed :class:`EventLog`; closes all after."""
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


@pytest.fixture
def build_app(tmp_path: Path):
    """App factory: each call gets its OWN log file (source vs target)."""
    built = []
    counter = {"n": 0}

    def _build():
        counter["n"] += 1
        app = create_app(FakeTurnEngine(), tmp_path / f"events-{counter['n']}.db")
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


@pytest.fixture
def build_app_with_log(tmp_path: Path):
    """Like :func:`build_app`, but also yields the app's log (for seeding)."""
    built = []
    counter = {"n": 0}

    def _build() -> tuple[object, EventLog]:
        counter["n"] += 1
        app = create_app(FakeTurnEngine(), tmp_path / f"events-{counter['n']}.db")
        built.append(app)
        return app, app.state.log

    yield _build
    for app in built:
        app.state.log.close()


# -- seeders -----------------------------------------------------------------


def _seed_preset(log: EventLog, preset_id: str = "custom-preset") -> str:
    append_preset(
        log,
        {
            "preset_id": preset_id,
            "name": "My Custom Preset",
            "modules": [
                {
                    "id": "m1",
                    "name": "System",
                    "type": "text",
                    "role": "system",
                    "content": "Narrate.",
                    "enabled": True,
                }
            ],
        },
    )
    return preset_id


def _seed_image(log: EventLog, pixels: bytes, mime: str = "image/png") -> str:
    """Store a definition image and return its content-addressed ref."""
    data = _png(pixels) if mime == "image/png" else JPEG_BYTES
    return store_definition_image(log, data, mime, note="test image")


def _seed_world_experience(
    log: EventLog,
    *,
    world_image: str | None = None,
    alice_image: str | None = None,
    bob_image: str | None = None,
) -> tuple[str, str, str, str]:
    """A 2-cast world + preset in ``log``; returns ``(world, preset, alice, bob)``."""
    alice = append_persona(
        log, {"name": "Alice", "description": "A wry detective.", "image_asset": alice_image}
    )
    bob = append_persona(log, {"name": "Bob", "image_asset": bob_image})
    world_id = append_world(
        log,
        {
            "name": "Emberfall",
            "scenario": "The city burns each night.",
            "cast": [alice, bob],
            "image_asset": world_image,
        },
    )
    return world_id, _seed_preset(log), alice, bob


def run(coro):
    return asyncio.run(coro)


async def _get(app, path: str, params: dict | None = None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path, params=params)


# -- the PNG codec -----------------------------------------------------------


def test_profile_to_png_round_trips_and_replaces_its_own_chunk():
    envelope = {"format": "fabulist-profile", "format_version": 1, "name": "X"}
    base = _png(WORLD_PIXELS)

    once = profile_to_png(envelope, base)
    twice = profile_to_png(envelope, once)

    assert profile_from_png(once) == envelope
    assert profile_from_png(twice) == envelope
    # Exactly one profile chunk, written immediately before IEND; the image and
    # its IDAT bytes are untouched.
    assert len(_profile_chunks(once)) == 1
    assert len(_profile_chunks(twice)) == 1
    assert _chunks(twice)[-2][0] == b"tEXt"
    assert _idat(twice) == _idat(base) == zlib.compress(b"\x00" + WORLD_PIXELS)
    assert base64.b64decode(_profile_chunks(once)[0]) == json.dumps(envelope).encode()


def test_profile_to_png_keeps_a_cards_chara_chunk():
    """Writing the profile chunk must not clobber an unrelated card body."""
    card_payload = base64.b64encode(b'{"spec": "chara_card_v2"}')
    base = _png(WORLD_PIXELS, keyword=b"chara", payload=card_payload)

    out = profile_to_png({"name": "X"}, base)
    keywords = [data.split(b"\x00", 1)[0] for ctype, data in _chunks(out) if ctype == b"tEXt"]

    assert keywords == [b"chara", PROFILE_CHUNK_KEYWORD]


def test_profile_from_png_rejects_non_png_and_corrupt_bytes():
    with pytest.raises(ProfilePngError, match="PNG signature"):
        profile_from_png(b"not a png at all")

    corrupt = bytearray(_png(WORLD_PIXELS))
    corrupt[-5] ^= 0xFF  # flip a CRC byte of IEND
    with pytest.raises(ProfilePngError, match="unreadable PNG"):
        profile_from_png(bytes(corrupt))


def test_profile_from_png_422_message_names_the_missing_chunk():
    with pytest.raises(ProfilePngError, match="fabulist_profile"):
        profile_from_png(minimal_png())


# -- layer order (pure shape walk) -------------------------------------------


def test_assigned_image_refs_walk_world_then_cast_in_order():
    envelope = {
        "world": {
            "world": {"world_id": "w", "image_asset": "world-ref"},
            "cast": [
                {"persona_id": "a", "image_asset": "alice-ref"},
                {"persona_id": "b", "image_asset": "bob-ref"},
            ],
        }
    }
    assert assigned_image_refs(envelope) == ["world-ref", "alice-ref", "bob-ref"]
    assert assigned_image_refs({"world": None}) == []
    assert assigned_image_refs({"world": {"world": {}, "cast": [{"persona_id": "a"}]}}) == []


# -- export.png --------------------------------------------------------------


def test_export_png_carries_the_assigned_image_and_the_envelope(
    build_app_with_log, build_log
):
    app, log = build_app_with_log()
    world_image = _seed_image(log, WORLD_PIXELS)
    world_id, preset_id, _, _ = _seed_world_experience(log, world_image=world_image)

    response = run(
        _get(
            app,
            "/api/profiles/export.png",
            {"world": world_id, "preset": preset_id, "assistants": 1, "name": "API"},
        )
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert (
        'attachment; filename="API.profile.png"' in response.headers["content-disposition"]
    )
    body = response.content
    assert body.startswith(PNG_SIGNATURE)
    # The body IS the assigned image: identical pixel data.
    assert _idat(body) == _idat(_png(WORLD_PIXELS))
    # ...and the full envelope rides the chunk.
    envelope = profile_from_png(body)
    assert envelope["format"] == "fabulist-profile"
    assert envelope["name"] == "API"
    assert envelope["world"]["world"]["world_id"] == world_id
    assert envelope["world"]["world"]["image_asset"] == world_image
    assert envelope["preset"]["preset_id"] == preset_id
    assert envelope["assistants"] == []  # roster key present, empty roster


def test_export_png_prefers_world_then_cast_order(build_app_with_log):
    app, log = build_app_with_log()
    world_image = _seed_image(log, WORLD_PIXELS)
    alice_image = _seed_image(log, ALICE_PIXELS)
    bob_image = _seed_image(log, BOB_PIXELS)

    # (a) world beats every cast image.
    world_id, preset_id, _, _ = _seed_world_experience(
        log, world_image=world_image, alice_image=alice_image, bob_image=bob_image
    )
    params = {"world": world_id, "preset": preset_id}
    world_png = run(_get(app, "/api/profiles/export.png", params))
    assert world_png.status_code == 200
    assert _idat(world_png.content) == _idat(_png(WORLD_PIXELS))

    # (b) no world image: the FIRST cast member in world.cast order wins.
    world_id2, preset_id2, _, _ = _seed_world_experience(
        log, alice_image=alice_image, bob_image=bob_image
    )
    cast_png = run(
        _get(app, "/api/profiles/export.png", {"world": world_id2, "preset": preset_id2})
    )
    assert cast_png.status_code == 200
    assert _idat(cast_png.content) == _idat(_png(ALICE_PIXELS))

    # (c) a dangling world ref falls through to the cast, never fails.
    world_id3, preset_id3, _, _ = _seed_world_experience(
        log, world_image="sha-that-no-longer-resolves", alice_image=alice_image
    )
    dangling = run(
        _get(app, "/api/profiles/export.png", {"world": world_id3, "preset": preset_id3})
    )
    assert dangling.status_code == 200
    assert _idat(dangling.content) == _idat(_png(ALICE_PIXELS))


def test_export_png_404s_when_no_layer_has_an_image(build_app_with_log):
    app, log = build_app_with_log()
    world_id, preset_id, _, _ = _seed_world_experience(log)

    response = run(
        _get(app, "/api/profiles/export.png", {"world": world_id, "preset": preset_id})
    )

    assert response.status_code == 404
    assert "no assigned image" in response.json()["detail"]


def test_export_png_422s_a_non_png_assigned_image(build_app_with_log):
    app, log = build_app_with_log()
    jpeg_ref = store_definition_image(log, JPEG_BYTES, "image/jpeg", note="test jpeg")
    world_id, preset_id, _, _ = _seed_world_experience(log, world_image=jpeg_ref)

    response = run(
        _get(app, "/api/profiles/export.png", {"world": world_id, "preset": preset_id})
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "image/jpeg" in detail
    assert "not a PNG" in detail


def test_export_png_404s_unknown_ids_and_422s_a_missing_preset(build_app_with_log):
    app, log = build_app_with_log()
    world_id, preset_id, _, _ = _seed_world_experience(
        log, world_image=_seed_image(log, WORLD_PIXELS)
    )

    unknown_preset = run(_get(app, "/api/profiles/export.png", {"preset": "ghost"}))
    unknown_world = run(
        _get(app, "/api/profiles/export.png", {"world": "ghost", "preset": preset_id})
    )
    missing_preset = run(_get(app, "/api/profiles/export.png", {"world": world_id}))

    assert unknown_preset.status_code == 404
    assert "unknown preset" in unknown_preset.json()["detail"]
    assert unknown_world.status_code == 404
    assert "unknown world" in unknown_world.json()["detail"]
    assert missing_preset.status_code == 422  # preset is required


def test_json_export_route_is_unchanged(build_app_with_log):
    app, log = build_app_with_log()
    world_id, preset_id, _, _ = _seed_world_experience(
        log, world_image=_seed_image(log, WORLD_PIXELS)
    )

    response = run(
        _get(app, "/api/profiles/export", {"world": world_id, "preset": preset_id})
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert (
        'attachment; filename="Emberfall.profile.json"'
        in response.headers["content-disposition"]
    )
    assert response.json()["world"]["world"]["world_id"] == world_id


# -- import ------------------------------------------------------------------


def test_import_route_accepts_a_png_body(build_app, build_log):
    source = build_log("source.db")
    world_id, preset_id, _, _ = _seed_world_experience(
        source, world_image=_seed_image(source, WORLD_PIXELS)
    )
    envelope = export_profile(source, world_id=world_id, preset_id=preset_id)
    png = profile_to_png(envelope, _png(WORLD_PIXELS))

    target = build_app()
    response = run(_post(target, "/api/profiles/import", content=png, content_type="image/png"))

    assert response.status_code == 200
    body = response.json()
    assert {row["kind"] for row in body["imported"]} == {"world", "preset", "persona"}
    # The entities really landed: the same definitions fold on the target log.
    assert world_by_id(target.state.log, world_id) is not None

    # ...and a JSON body of the same envelope is still accepted (unchanged path).
    again = run(_post(target, "/api/profiles/import", json=envelope))
    assert again.status_code == 200
    assert again.json()["imported"] == []  # idempotent re-import
    assert {row["kind"] for row in again.json()["skipped"]} == {
        "world",
        "preset",
        "persona",
    }


def test_import_route_422s_a_png_without_the_profile_chunk(build_app):
    target = build_app()

    bare = run(_post(target, "/api/profiles/import", content=minimal_png(), content_type="image/png"))
    card_png = run(
        _post(
            target,
            "/api/profiles/import",
            content=_png(WORLD_PIXELS, keyword=b"chara", payload=base64.b64encode(b"{}")),
            content_type="image/png",
        )
    )

    assert bare.status_code == 422
    assert "fabulist_profile" in bare.json()["detail"]
    assert card_png.status_code == 422
    assert "fabulist_profile" in card_png.json()["detail"]


def test_profile_image_png_strips_the_envelope_chunk():
    """The image body recovers byte-for-byte (so its ref stays content-addressed)."""
    base = _png(WORLD_PIXELS)
    body = profile_to_png({"format": "fabulist-profile", "name": "P"}, base)
    assert body != base
    assert any(ctype == b"tEXt" for ctype, _ in _chunks(body))
    recovered = profile_image_png(body)
    assert recovered == base
    assert _chunks(recovered) == _chunks(base)


def test_import_route_persists_the_png_body_image(build_app, build_log):
    """A profile PNG import stores its body image so the imported refs resolve.

    Regression: the route used to decode only the envelope chunk, so the
    imported world kept the exporting log's content address — the artwork could
    not be served and a re-export 404'd with "no assigned image".
    """
    source = build_log("source.db")
    world_id, preset_id, _, _ = _seed_world_experience(
        source, world_image=_seed_image(source, WORLD_PIXELS)
    )
    envelope = export_profile(source, world_id=world_id, preset_id=preset_id)
    png = profile_to_png(envelope, _png(WORLD_PIXELS))

    target = build_app()
    response = run(
        _post(target, "/api/profiles/import", content=png, content_type="image/png")
    )
    assert response.status_code == 200
    # The body image's bytes ARE the ref the envelope carried; stored here under
    # the same content address, so the returned id matches.
    assert response.json()["image_asset"] == envelope["world"]["world"]["image_asset"]

    served = run(_get(target, f"/api/worlds/{world_id}/image"))
    assert served.status_code == 200
    assert _idat(served.content) == _idat(_png(WORLD_PIXELS))

    # ...and the re-export carries the artwork again (the reported defect).
    again = run(
        _get(target, "/api/profiles/export.png", {"world": world_id, "preset": preset_id})
    )
    assert again.status_code == 200
    assert _idat(again.content) == _idat(_png(WORLD_PIXELS))


def test_import_route_persists_a_cast_assigned_image(build_app, build_log):
    """The assigned layer can be a cast member, not only the world."""
    source = build_log("source.db")
    world_id, preset_id, alice, _ = _seed_world_experience(
        source, alice_image=_seed_image(source, ALICE_PIXELS)
    )
    envelope = export_profile(source, world_id=world_id, preset_id=preset_id)
    png = profile_to_png(envelope, _png(ALICE_PIXELS))

    target = build_app()
    assert (
        run(_post(target, "/api/profiles/import", content=png, content_type="image/png")).status_code
        == 200
    )

    served = run(_get(target, f"/api/personas/{alice}/image"))
    assert served.status_code == 200
    assert _idat(served.content) == _idat(_png(ALICE_PIXELS))
    again = run(
        _get(target, "/api/profiles/export.png", {"world": world_id, "preset": preset_id})
    )
    assert again.status_code == 200
    assert _idat(again.content) == _idat(_png(ALICE_PIXELS))


def test_import_route_png_reimport_keeps_the_image_and_is_a_no_op(build_app, build_log):
    """A second import of the same PNG neither duplicates the asset nor re-appends."""
    source = build_log("source.db")
    world_id, preset_id, _, _ = _seed_world_experience(
        source, world_image=_seed_image(source, WORLD_PIXELS)
    )
    envelope = export_profile(source, world_id=world_id, preset_id=preset_id)
    png = profile_to_png(envelope, _png(WORLD_PIXELS))
    target = build_app()

    first = run(_post(target, "/api/profiles/import", content=png, content_type="image/png"))
    second = run(_post(target, "/api/profiles/import", content=png, content_type="image/png"))
    assert first.json()["imported"]
    assert second.json()["imported"] == []

    anchors = [e for e in target.state.log.get_events(MEDIA_ANCHOR) if e.type == "asset"]
    assert len(anchors) == 1  # content-addressed: one stored image, not two
    assert run(_get(target, f"/api/worlds/{world_id}/image")).status_code == 200


def test_persist_profile_image_repoints_a_dangling_ref(build_log):
    """The helper stores the body and rewrites the assigned layer's ref."""
    log = build_log()
    image = _png(WORLD_PIXELS)
    envelope = {
        "format": "fabulist-profile",
        "format_version": 1,
        "name": "P",
        "world": {
            "world": {"world_id": "w", "image_asset": "no-such-asset"},
            "cast": [],
            "books": [],
        },
        "preset": None,
    }

    ref = persist_profile_image(log, envelope, image, "image/png", note="probe")
    assert ref and ref != "no-such-asset"
    assert envelope["world"]["world"]["image_asset"] == ref
    assert find_definition_asset(log, ref) is not None
    # Content-addressed: a second store of identical bytes reuses the row.
    assert persist_profile_image(log, envelope, image, "image/png") == ref
    assert len([e for e in log.get_events(MEDIA_ANCHOR) if e.type == "asset"]) == 1


def test_persist_profile_image_prefers_the_exact_content_address_layer(build_log):
    """A dangling earlier ref must not steal a later layer's image.

    The body image belongs to the layer whose content-addressed ref matches it
    (as it did at export), even when an earlier layer's ref dangles there too.
    """
    log = build_log()
    image = _png(WORLD_PIXELS)
    ref = sha256_hex(image)
    envelope = {
        "format": "fabulist-profile",
        "format_version": 1,
        "name": "P",
        "world": {
            "world": {"world_id": "w", "image_asset": "dangling-world-ref"},
            "cast": [{"persona_id": "a", "image_asset": ref}],
            "books": [],
        },
        "preset": None,
    }

    assert persist_profile_image(log, envelope, image, "image/png") == ref
    assert envelope["world"]["cast"][0]["image_asset"] == ref
    assert envelope["world"]["world"]["image_asset"] == "dangling-world-ref"


def test_persist_profile_image_noops_without_an_assigned_layer(build_log):
    """A ``world: null`` profile has no layer to point at the image."""
    log = build_log()
    envelope = {"format": "fabulist-profile", "format_version": 1, "name": "P", "world": None}
    assert persist_profile_image(log, envelope, _png(WORLD_PIXELS), "image/png") is None
    assert [e for e in log.get_events(MEDIA_ANCHOR) if e.type == "asset"] == []


def test_import_route_422s_a_png_with_a_corrupt_body(build_app, build_log):
    """A profile chunk before a corrupt chunk is a 422, not a 500 (body read walks on)."""
    source = build_log("source.db")
    world_id, preset_id, _, _ = _seed_world_experience(
        source, world_image=_seed_image(source, WORLD_PIXELS)
    )
    envelope = export_profile(source, world_id=world_id, preset_id=preset_id)
    png = bytearray(profile_to_png(envelope, _png(WORLD_PIXELS)))
    png[-1] ^= 0xFF  # corrupt the trailing IEND chunk's CRC (after the envelope chunk)

    target = build_app()
    response = run(
        _post(target, "/api/profiles/import", content=bytes(png), content_type="image/png")
    )

    assert response.status_code == 422


def test_import_route_422s_a_non_json_non_png_body(build_app):
    target = build_app()

    response = run(
        _post(target, "/api/profiles/import", content=b"{not json", content_type="application/json")
    )
    array = run(_post(target, "/api/profiles/import", json=[1, 2, 3]))

    assert response.status_code == 422
    assert "neither a PNG nor valid JSON" in response.json()["detail"]
    assert array.status_code == 422
    assert "must be a JSON object" in array.json()["detail"]


async def _post(app, path: str, *, content=None, content_type=None, json=None):
    headers = {"Content-Type": content_type} if content_type else None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(path, content=content, json=json, headers=headers)


# -- unit: the resolver against a real log -----------------------------------


def test_assigned_image_resolves_the_world_then_the_cast(build_log):
    from fabulist.profiles.image import assigned_image

    log = build_log()
    world_image = _seed_image(log, WORLD_PIXELS)
    alice_image = _seed_image(log, ALICE_PIXELS)

    world_id, preset_id, _, _ = _seed_world_experience(
        log, world_image=world_image, alice_image=alice_image
    )
    with_world = export_profile(
        log, world_id=world_id, preset_id=preset_id, exported_at=STAMP
    )
    assert assigned_image(log, with_world) == (_png(WORLD_PIXELS), "image/png")

    cast_id, cast_preset, _, _ = _seed_world_experience(log, alice_image=alice_image)
    cast_only = export_profile(
        log, world_id=cast_id, preset_id=cast_preset, exported_at=STAMP
    )
    assert assigned_image(log, cast_only) == (_png(ALICE_PIXELS), "image/png")

    bare_id, bare_preset, _, _ = _seed_world_experience(log)
    bare = export_profile(
        log, world_id=bare_id, preset_id=bare_preset, exported_at=STAMP
    )
    assert assigned_image(log, bare) is None
