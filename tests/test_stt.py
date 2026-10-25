"""Tests for speech-to-text transcription (``fabulist.media.stt``).

Covers the two-stage contract without any network dependency beyond the one-time
``tiny.en`` model fetch (cached by faster-whisper/huggingface on first use):

* :func:`decode_audio` -> 16 kHz mono 16-bit PCM WAV, verified by reading the
  result back with the stdlib :mod:`wave` module (RIFF header, 1 channel,
  2-byte samples, 16000 Hz), including a downsample from 44.1 kHz.
* :func:`transcribe` on a synthesized 1 s 440 Hz sine -> an :class:`SttResult`
  whose ``text`` is a string (empty is valid: silence/VAD), with a real
  ``language`` and positive ``duration_s``.
* :class:`MediaSttError` for garbage bytes, both at the decode stage and through
  :func:`transcribe`.

The whole transcription group is skipped only if ``WhisperModel('tiny.en')``
cannot be constructed (e.g. offline with a cold cache); in this environment it
loads, so the tests run.
"""

from __future__ import annotations

import io
import math
import struct
import wave

import pytest

from fabulist.media.stt import (
    MediaSttError,
    SttResult,
    decode_audio,
    transcribe,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sine_wav(
    *, sample_rate: int = 16000, seconds: float = 1.0, freq: float = 440.0
) -> bytes:
    """A mono 16-bit PCM WAV holding a sine tone at ``freq`` Hz."""
    buf = io.BytesIO()
    writer = wave.open(buf, "wb")
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(sample_rate)
    n = int(sample_rate * seconds)
    frames = b"".join(
        struct.pack("<h", int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / sample_rate)))
        for i in range(n)
    )
    writer.writeframes(frames)
    writer.close()
    return buf.getvalue()


def wav_params(data: bytes) -> tuple[int, int, int, int]:
    """Return ``(channels, sample_width, rate, nframes)`` of a WAV blob."""
    with wave.open(io.BytesIO(data), "rb") as reader:
        return (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
            reader.getnframes(),
        )


def _tiny_whisper_available() -> bool:
    """True when ``WhisperModel('tiny.en')`` can be constructed right now."""
    try:
        from faster_whisper import WhisperModel

        WhisperModel("tiny.en", device="cpu", compute_type="int8")
        return True
    except Exception:  # noqa: BLE001 - any failure means "skip", not "error"
        return False


requires_tiny_model = pytest.mark.skipif(
    not _tiny_whisper_available(),
    reason="faster-whisper 'tiny.en' unavailable (cold cache / offline)",
)

GARBAGE = b"this is definitely not audio \x00\x01\x02\xff\xfe"


# ---------------------------------------------------------------------------
# decode_audio
# ---------------------------------------------------------------------------


def test_decode_audio_wav_round_trip_is_16k_mono_pcm():
    source = sine_wav(sample_rate=16000, seconds=0.5)

    decoded = decode_audio(source, "audio/wav")

    assert decoded[:4] == b"RIFF"
    channels, width, rate, frames = wav_params(decoded)
    assert channels == 1
    assert width == 2
    assert rate == 16000
    # ffmpeg may pad by a frame or two; half a second is ~8000 frames.
    assert abs(frames - 8000) <= 64


def test_decode_audio_downsamples_44100_to_16k():
    source = sine_wav(sample_rate=44100, seconds=0.5)

    decoded = decode_audio(source, "audio/wav")

    channels, width, rate, frames = wav_params(decoded)
    assert (channels, width, rate) == (1, 2, 16000)
    assert abs(frames - 8000) <= 64


def test_decode_audio_accepts_out_sr_override():
    source = sine_wav(sample_rate=16000, seconds=0.25)

    decoded = decode_audio(source, "audio/wav", out_sr=8000)

    _, _, rate, frames = wav_params(decoded)
    assert rate == 8000
    assert abs(frames - 2000) <= 64


def test_decode_audio_garbage_raises_media_stt_error():
    with pytest.raises(MediaSttError) as excinfo:
        decode_audio(GARBAGE, "audio/wav")

    assert "ffmpeg" in str(excinfo.value)
    assert isinstance(excinfo.value.stderr, str)


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------


@requires_tiny_model
def test_transcribe_sine_returns_stt_result():
    result = transcribe(
        sine_wav(sample_rate=16000, seconds=1.0),
        "audio/wav",
        model_size="tiny.en",
    )

    assert isinstance(result, SttResult)
    assert isinstance(result.text, str)  # empty on silence is valid
    assert isinstance(result.language, str)
    assert result.language  # 'tiny.en' always reports 'en'
    assert isinstance(result.duration_s, float)
    assert result.duration_s > 0.0


@requires_tiny_model
def test_transcribe_respects_forced_language():
    result = transcribe(
        sine_wav(sample_rate=16000, seconds=1.0),
        "audio/wav",
        model_size="tiny.en",
        language="en",
    )

    assert result.language == "en"


def test_transcribe_garbage_raises_media_stt_error():
    # Decoding fails before any model is constructed, so no download is needed.
    with pytest.raises(MediaSttError):
        transcribe(GARBAGE, "audio/wav", model_size="tiny.en")
