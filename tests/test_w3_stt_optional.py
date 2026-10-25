"""W3b: the transcription stack is OPTIONAL (``fabulist[stt]``).

The STT router is mounted unconditionally by ``api/app.create_app``, so
``fabulist.media.stt`` must import without ``numpy``/``faster_whisper`` present:
those are imported lazily by the function that needs them. These tests pin the
four claims of that split:

* importing the module leaves both packages unimported,
* ``transcribe`` raises :class:`~fabulist.media.stt.SttUnavailableError` (a
  ``MediaSttError`` with ``stage="import"`` and the install hint) when they are
  missing,
* ``decode_audio`` keeps working with them missing (ffmpeg only),
* ``POST /api/stt`` answers ``503`` with the same hint instead of a 500.

Absence is simulated by putting ``None`` in ``sys.modules`` — the documented way
to make an ``import`` statement raise ``ImportError`` without uninstalling.
"""

from __future__ import annotations

import asyncio
import io
import math
import struct
import subprocess
import sys
import wave
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.stt_api import router as stt_router
from fabulist.media.stt import (
    STT_EXTRA_HINT,
    MediaSttError,
    SttUnavailableError,
    decode_audio,
    transcribe,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The two optional imports, made absent for the duration of a test.
_OPTIONAL_STACK = ("numpy", "faster_whisper")


def run(coro):
    return asyncio.run(coro)


def sine_wav(*, seconds: float = 0.25, freq: float = 440.0) -> bytes:
    """A mono 16-bit PCM 16 kHz WAV holding a sine tone."""
    buf = io.BytesIO()
    writer = wave.open(buf, "wb")
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(16000)
    count = int(16000 * seconds)
    writer.writeframes(
        b"".join(
            struct.pack(
                "<h", int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / 16000))
            )
            for i in range(count)
        )
    )
    writer.close()
    return buf.getvalue()


@pytest.fixture
def without_stack(monkeypatch: pytest.MonkeyPatch):
    """Make ``numpy``/``faster_whisper`` un-importable for this test."""
    for name in _OPTIONAL_STACK:
        monkeypatch.setitem(sys.modules, name, None)


# --- import-time optionality -------------------------------------------------


def test_importing_the_stt_module_does_not_import_the_stack():
    """A plain engine install must import the router without pulling whisper in.

    ``faster_whisper`` is the claim under test (it drags in ctranslate2/onnxruntime
    and the model cache). ``numpy`` arrives transitively through other engine
    modules, so the assertion on it is that THIS module binds neither name.
    """
    script = (
        "import sys\n"
        "import fabulist.media.stt as m\n"
        "print(m.STT_EXTRA_HINT)\n"
        "print('faster_whisper' in sys.modules)\n"
        "print('WhisperModel' in vars(m), 'np' in vars(m))\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0] == STT_EXTRA_HINT
    assert lines[1] == "False"  # the heavy stack was not imported
    assert lines[2] == "False False"  # neither name is bound by this module


# --- transcribe / decode without the stack -----------------------------------


def test_transcribe_without_the_stack_raises_the_install_hint(without_stack):
    with pytest.raises(SttUnavailableError) as excinfo:
        transcribe(sine_wav(), "audio/wav")

    assert str(excinfo.value) == STT_EXTRA_HINT
    assert excinfo.value.stage == "import"
    # The attachment path logs any MediaSttError unchanged, so it must subclass.
    assert isinstance(excinfo.value, MediaSttError)


def test_transcribe_without_the_stack_fails_before_any_ffmpeg_work(without_stack):
    """The check is up front: no decode attempt is made on junk input."""
    with pytest.raises(SttUnavailableError):
        transcribe(b"not audio at all", "audio/wav")


def test_decode_audio_still_works_without_the_stack(without_stack):
    """ffmpeg is the only decode dependency; the Python stack is not needed."""
    decoded = decode_audio(sine_wav(), "audio/wav")

    assert decoded[:4] == b"RIFF"


# --- the HTTP door -----------------------------------------------------------


def _client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_stt_endpoint_answers_503_when_the_stack_is_missing(tmp_path, without_stack):
    app = create_app(FakeTurnEngine(), tmp_path / "events.db")
    app.include_router(stt_router)

    async def scenario():
        async with _client_for(app) as client:
            return await client.post(
                "/api/stt",
                files={"file": ("clip.wav", sine_wav(), "audio/wav")},
            )

    try:
        resp = run(scenario())
    finally:
        app.state.log.close()

    assert resp.status_code == 503
    assert resp.json() == {"detail": STT_EXTRA_HINT}


def test_stt_endpoint_still_rejects_non_audio_before_the_stack_check(tmp_path, without_stack):
    """Upload validation runs first: a non-audio clip is 415, never 503."""
    app = create_app(FakeTurnEngine(), tmp_path / "events.db")
    app.include_router(stt_router)

    async def scenario():
        async with _client_for(app) as client:
            return await client.post(
                "/api/stt",
                files={"file": ("notes.txt", b"plain text, not a clip", "text/plain")},
            )

    try:
        resp = run(scenario())
    finally:
        app.state.log.close()

    assert resp.status_code == 415
