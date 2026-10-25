"""Tests for audio/video media validation (``fabulist.media.av``).

Hand-built magic-byte fixtures (no real media files) exercise :func:`sniff_kind`
across every recognised container, the ISO-BMFF brand tiebreak, the mime
fallback, the per-kind size ceilings, the exact OpenAI content-part shapes, and
:func:`store_media` against a real tmp :class:`~fabulist.log.EventLog`.
:func:`embed_inputs` is asserted at the ``httpx`` transport seam, mirroring
``tests/test_memory_embedder.py``.
"""

from __future__ import annotations

import asyncio
import base64
import struct

import httpx
import pytest

from fabulist.log import EventLog
from fabulist.media import av
from fabulist.media import images as images_mod
from fabulist.media.av import (
    KIND_LIMITS,
    MediaTooLarge,
    embed_content_part,
    embed_inputs,
    sniff_kind,
    store_media,
    validate_size,
)


def run(coro):
    """Drive a coroutine to completion (suite has no pytest-asyncio)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Magic-byte fixtures
# ---------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12
GIF87 = b"GIF87a" + b"\x00" * 10
GIF89 = b"GIF89a" + b"\x00" * 10
WEBP = b"RIFF" + struct.pack("<I", 100) + b"WEBP" + b"VP8 " + b"\x00" * 8
AVI = b"RIFF" + struct.pack("<I", 100) + b"AVI " + b"LIST" + b"\x00" * 8
ID3 = b"ID3\x04\x00\x00" + b"\x00" * 10
OGG = b"OggS\x00\x02" + b"\x00" * 10
FLAC = b"fLaC\x00\x00\x00\x22" + b"\x00" * 8
MATROSKA = b"\x1aE\xdf\xa3" + b"\x00" * 12


def wav_bytes(samples: int = 8) -> bytes:
    """A real 44-byte canonical WAV header followed by ``samples`` of silence."""
    data = b"\x80" * samples
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
    chunk = b"data" + struct.pack("<I", len(data))
    return header + fmt + chunk + data


def mp4(brand: bytes) -> bytes:
    """A minimal ISO-BMFF header carrying ``brand`` as its major brand."""
    assert len(brand) == 4
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00"


# ---------------------------------------------------------------------------
# sniff_kind: magic bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,kind",
    [
        (PNG, "image"),
        (JPEG, "image"),
        (GIF87, "image"),
        (GIF89, "image"),
        (WEBP, "image"),
        (wav_bytes(), "audio"),
        (AVI, "video"),
        (ID3, "audio"),
        (OGG, "audio"),
        (FLAC, "audio"),
    ],
)
def test_sniff_magic_recognised(data, kind):
    assert sniff_kind(data, "") == kind
    # Magic wins even when the declared mime says otherwise.
    assert sniff_kind(data, "audio/mpeg") == kind


def test_sniff_matroska_defers_to_declared_mime():
    """EBML (webm/matroska) carries audio-only AND video containers.

    The magic alone cannot split them without EBML track parsing, so the
    container defers to the declared mime (our own browser's claim):
    audio/* -> audio (the browser mic records audio-only webm — misfiling it
    as video would ride turns as input_video, violating D14), video/* ->
    video, unknown/empty -> video (webm default lean).
    """
    assert sniff_kind(MATROSKA, "audio/webm;codecs=opus") == "audio"
    assert sniff_kind(MATROSKA, "video/webm") == "video"
    assert sniff_kind(MATROSKA, "") == "video"
    assert sniff_kind(MATROSKA, "application/octet-stream") == "video"


@pytest.mark.parametrize("sync", [b"\xff\xfb", b"\xff\xf3", b"\xff\xe3"])
def test_sniff_raw_mp3_frame_sync(sync):
    assert sniff_kind(sync + b"\x90" + b"\x00" * 8, "") == "audio"


def test_wav_fixture_is_a_real_44_byte_header():
    empty = wav_bytes(0)
    assert len(empty) == 44
    assert empty[:4] == b"RIFF"
    assert empty[8:12] == b"WAVE"
    assert empty[12:16] == b"fmt "
    assert empty[36:40] == b"data"
    assert sniff_kind(wav_bytes(64), "application/octet-stream") == "audio"


# ---------------------------------------------------------------------------
# sniff_kind: ISO-BMFF brand tiebreak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("brand", [b"isom", b"mp42", b"avc1", b"m4v "])
def test_sniff_mp4_video_brands(brand):
    assert sniff_kind(mp4(brand), "") == "video"


@pytest.mark.parametrize("brand", [b"M4A "])
def test_sniff_mp4_audio_brands(brand):
    assert sniff_kind(mp4(brand), "") == "audio"


def test_sniff_mp4_unknown_brand_falls_back_to_declared_mime():
    blob = mp4(b"3gp4")
    assert sniff_kind(blob, "video/mp4") == "video"
    assert sniff_kind(blob, "audio/mp4") == "audio"
    assert sniff_kind(blob, "") == "unknown"


def test_ftyp_only_counts_at_offset_four():
    # ``ftyp`` at offset 0 is not ISO-BMFF and must not classify as media.
    assert sniff_kind(b"ftypisom" + b"\x00" * 8, "text/plain") == "unknown"


# ---------------------------------------------------------------------------
# sniff_kind: declared-mime fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mime,kind",
    [
        ("audio/mpeg", "audio"),
        ("video/mp4", "video"),
        ("image/png", "image"),
        ("audio/wav; charset=binary", "audio"),
        ("IMAGE/JPEG", "image"),
        ("application/octet-stream", "unknown"),
        ("", "unknown"),
    ],
)
def test_sniff_mime_fallback(mime, kind):
    assert sniff_kind(b"\x00\x01\x02\x03notmedia", mime) == kind


# ---------------------------------------------------------------------------
# KIND_LIMITS + validate_size
# ---------------------------------------------------------------------------


def test_kind_limits_exact_values():
    assert KIND_LIMITS == {
        "image": 20 * 1024 * 1024,
        "audio": 60 * 1024 * 1024,
        "video": 200 * 1024 * 1024,
    }


@pytest.mark.parametrize("kind", ["image", "audio", "video"])
def test_validate_size_accepts_at_and_below_limit(kind):
    validate_size(kind, KIND_LIMITS[kind])  # exactly at the ceiling is fine
    validate_size(kind, 0)


@pytest.mark.parametrize("kind", ["image", "audio", "video"])
def test_validate_size_rejects_over_limit(kind):
    over = KIND_LIMITS[kind] + 1
    with pytest.raises(MediaTooLarge) as excinfo:
        validate_size(kind, over)
    err = excinfo.value
    assert err.kind == kind
    assert err.size == over
    assert err.limit == KIND_LIMITS[kind]
    assert kind in str(err)
    assert str(over) in str(err)


def test_validate_size_unknown_kind():
    with pytest.raises(ValueError):
        validate_size("unknown", 1)


# ---------------------------------------------------------------------------
# embed_content_part
# ---------------------------------------------------------------------------


def test_embed_content_part_image_exact_shape():
    assert embed_content_part("image", "image/png", "QUJD") == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,QUJD"},
    }


def test_embed_content_part_audio_exact_shape():
    assert embed_content_part("audio", "audio/wav", "QUJD") == {
        "type": "input_audio",
        "input_audio": {"data": "QUJD", "format": "wav"},
    }


def test_embed_content_part_video_exact_shape():
    assert embed_content_part("video", "video/mp4", "QUJD") == {
        "type": "input_video",
        "input_video": {"url": "data:video/mp4;base64,QUJD"},
    }


@pytest.mark.parametrize(
    "mime,fmt",
    [
        ("audio/wav", "wav"),
        ("audio/x-wav", "x-wav"),
        ("audio/mpeg", "mpeg"),
        ("AUDIO/WAV; codecs=1", "wav"),
    ],
)
def test_embed_content_part_audio_format_is_mime_subtype(mime, fmt):
    part = embed_content_part("audio", mime, "x")
    assert part["input_audio"]["format"] == fmt


def test_embed_content_part_unknown_kind_raises():
    with pytest.raises(ValueError):
        embed_content_part("unknown", "application/octet-stream", "x")


# ---------------------------------------------------------------------------
# embed_inputs: transport seam
# ---------------------------------------------------------------------------


@pytest.fixture
def transport_factory(monkeypatch):
    """Install a MockTransport-backed ``httpx.AsyncClient`` factory."""

    def install(handler):
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def fake_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(av.httpx, "AsyncClient", fake_client)

    return install


def test_embed_inputs_posts_raw_multimodal_items(transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )

    transport_factory(handler)

    item = {"content": [embed_content_part("audio", "audio/wav", "QUJD")]}
    vectors = run(embed_inputs("http://host:4445/", "embeddinggemma", [item]))

    assert seen["method"] == "POST"
    assert seen["url"] == "http://host:4445/v1/embeddings"
    assert seen["body"] == {"model": "embeddinggemma", "input": [item]}
    assert seen["auth"] is None
    assert vectors == [[0.1, 0.2, 0.3]]


def test_embed_inputs_orders_by_index_and_sends_bearer(transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [9.0]},
                    {"index": 0, "embedding": [1.0]},
                ]
            },
        )

    transport_factory(handler)
    vectors = run(embed_inputs("http://host", "m", [{"content": []}], api_key="k"))
    assert vectors == [[1.0], [9.0]]
    assert seen["auth"] == "Bearer k"


def test_embed_inputs_error_path(transport_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model not loaded")

    transport_factory(handler)
    from fabulist.memory.embedder import EmbedderError

    with pytest.raises(EmbedderError) as excinfo:
        run(embed_inputs("http://host", "m", [{"content": []}]))
    assert excinfo.value.status_code == 500


# ---------------------------------------------------------------------------
# store_media: real EventLog round-trip
# ---------------------------------------------------------------------------


def test_store_media_round_trips_an_asset_event(log: EventLog):
    data = wav_bytes(32)
    event = store_media(log, "s1", data, "audio/wav", "audio", note="rain loop")

    assert event.type == "asset"
    assert event.actor == "media"
    payload = event.payload
    assert payload["kind"] == "audio"
    assert payload["mime"] == "audio/wav"
    assert payload["note"] == "rain loop"
    assert payload["sha256"] == av.sha256_hex(data)
    assert payload["asset_id"] == payload["sha256"]
    assert base64.b64decode(payload["bytes_b64"]) == data

    projection = log.get_projection("s1")
    assert [item["payload"]["asset_id"] for item in projection["assets"]] == [
        payload["asset_id"]
    ]
    assert log.get_events("s1")[0].type == "asset"


def test_store_media_content_addressed_and_kind_recorded(log: EventLog):
    clip = mp4(b"isom")
    first = store_media(log, "s1", clip, "video/mp4", "video")
    second = store_media(log, "s1", clip, "video/mp4", "video")
    assert first.payload["asset_id"] == second.payload["asset_id"]
    assert first.payload["kind"] == "video"
    # Append-only: both rows survive.
    assert len(log.get_projection("s1")["assets"]) == 2


def test_store_media_reuses_image_helpers():
    # The image path is the single source of actor + content-address truth.
    assert av.MEDIA_ACTOR == images_mod.MEDIA_ACTOR == "media"
    assert av.sha256_hex is images_mod.sha256_hex
