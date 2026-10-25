"""STT endpoint tests: real tiny WAV through the live router (no network)."""

from __future__ import annotations

import io
import math
import struct
import wave

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fabulist.api.stt_api import STT_MAX_BYTES, router


def _sine_wav(seconds: float = 0.6, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 300 * i / rate)))
            for i in range(int(rate * seconds))
        )
        w.writeframes(frames)
    return buf.getvalue()


def _client() -> AsyncClient:
    app = FastAPI()
    app.include_router(router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _stt_stack_available() -> bool:
    """The `stt` extra is OPTIONAL (Docker ships without it), so a runner that
    did not install it skips the transcribe test instead of failing on the 503."""
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001 - absence means skip, not error
        return False
    return True


@pytest.mark.skipif(
    not _stt_stack_available(), reason="optional 'stt' extra not installed"
)
def test_transcribes_tiny_wav() -> None:
    import asyncio

    async def run():
        async with _client() as c:
            return await c.post(
                "/api/stt",
                files={"file": ("clip.wav", _sine_wav(), "audio/wav")},
            )

    resp = asyncio.run(run())
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"text", "language", "duration_s"}
    assert isinstance(body["text"], str)  # silence -> "" is valid


def test_rejects_non_audio_415() -> None:
    import asyncio

    async def run():
        async with _client() as c:
            return await c.post(
                "/api/stt",
                files={"file": ("x.bin", b"\x00\x01\x02\x03" * 100, "application/octet-stream")},
            )

    resp = asyncio.run(run())
    assert resp.status_code == 415


def test_oversize_413(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import fabulist.api.stt_api as mod

    monkeypatch.setattr(mod, "STT_MAX_BYTES", 100)

    async def run():
        async with _client() as c:
            return await c.post(
                "/api/stt",
                files={"file": ("big.wav", _sine_wav(0.5), "audio/wav")},
            )

    resp = asyncio.run(run())
    assert resp.status_code == 413
