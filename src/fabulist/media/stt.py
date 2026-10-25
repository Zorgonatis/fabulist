"""Speech-to-text transcription via faster-whisper (W3.8, surface #22).

Turns an uploaded audio (or audio-bearing video) blob into text. The event log
owns the source bytes (D9); this module is a stateless transform with two
public steps and nothing else:

1. :func:`decode_audio` — normalize arbitrary container bytes to **16 kHz mono
   16-bit PCM WAV** bytes with an ``ffmpeg`` subprocess.
2. :func:`transcribe` — run faster-whisper over the normalized audio and return
   an :class:`SttResult` (``text``, ``language``, ``duration_s``).

Design notes (boring on purpose)
--------------------------------
* **Tempfiles, never pipes.** Container demuxing needs a seekable input (the
  mtmd-helper lesson: piping an MP4 to ffmpeg fails), so both the input and the
  output are real temp files; stdout/stderr are captured only for diagnostics.
* **Always re-encode.** An input that is already 16 kHz mono WAV is still run
  through ffmpeg, so every downstream caller sees one canonical format. The
  cost is negligible next to transcription and it removes a whole class of
  "sometimes the bytes were already right" branches.
* **Whisper is fed a numpy array, not a path.** faster-whisper's internal
  ``decode_audio`` calls ``av.open(..., metadata_errors="ignore")``; the pinned
  PyAV (19.x) no longer accepts that keyword, so that code path raises
  ``TypeError`` before any decoding happens. Rather than depending on that
  fragile seam, the WAV bytes produced by :func:`decode_audio` are read with the
  stdlib :mod:`wave` module into a float32 array in ``[-1, 1]`` and handed
  straight to ``WhisperModel.transcribe`` (which skips its own decoder for
  arrays). No PyAV, no version roulette.
* **One model per call (v1).** Constructing :class:`~faster_whisper.WhisperModel`
  inside :func:`transcribe` keeps the function stateless and trivially testable,
  but re-reads and re-initializes the CTranslate2 model on every invocation
  (~tens of ms for ``tiny.en`` from warm cache, seconds for ``small.en`` on a
  cold disk). Caching a singleton per ``(model_size, device, compute_type)`` is
  the obvious later optimization; it is deliberately *not* done here and should
  land behind its own tests.
* **Silence is not an error.** VAD filtering may legitimately yield no segments
  for quiet input; an empty ``text`` is a valid :class:`SttResult`.

Every failure (ffmpeg, model construction, transcription) surfaces as
:class:`MediaSttError`, carrying a ``stage`` label (``"decode"``/
``"transcribe"``) so callers can record *where* it failed as a log event (S4c).

Optionality (W3b)
-----------------
Transcription is an OPTIONAL install (``fabulist[stt]``): ``numpy`` and
``faster_whisper`` are imported lazily by the functions that need them, so
importing this module — the STT router is mounted unconditionally — costs
nothing on a plain engine install. When the stack is absent, transcription
raises :class:`SttUnavailableError` (a :class:`MediaSttError` subclass carrying
the install hint and ``stage="import"``); decoding keeps working because
``ffmpeg`` is the only thing it needs.
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only; the runtime import is lazy
    import numpy as np

__all__ = [
    "STT_EXTRA_HINT",
    "MediaSttError",
    "SttResult",
    "SttUnavailableError",
    "decode_audio",
    "transcribe",
]

#: The one message for a missing optional STT stack; the HTTP door returns it
#: verbatim in its 503 body, so the fix is readable without the server journal.
STT_EXTRA_HINT: str = (
    "voice transcription not installed; install the fabulist[stt] extra"
)

#: ffmpeg binary; overridable for non-standard installs.
_FFMPEG: str = os.environ.get("FABULIST_FFMPEG", "ffmpeg")

#: How much stderr text to keep when wrapping a tool failure.
_STDERR_TAIL_CHARS: int = 800

#: Whisper's fixed input sample rate (Hz).
_WHISPER_RATE: int = 16000

#: mime type -> temp-file suffix, so ffmpeg's probe gets a friendly hint.
_SUFFIX_BY_MIME: dict[str, str] = {
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/webm": ".webm",
    "audio/aac": ".aac",
    "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
}


class MediaSttError(Exception):
    """Raised when audio decoding or transcription fails.

    The message names the failing stage; the tail of the subprocess stderr is
    embedded in the message and attached as ``stderr`` for callers that want the
    full-ish captured text. ``stage`` is a short machine label
    (``"decode"``/``"transcribe"``) so a caller can record *where* the failure
    happened as a stage-failure log event (S4c) instead of parsing the message.
    """

    def __init__(self, message: str, *, stage: str = "stt", stderr: str = "") -> None:
        self.stage = stage
        self.stderr = stderr
        super().__init__(message)


class SttUnavailableError(MediaSttError):
    """Raised when the optional STT stack (``numpy`` + ``faster-whisper``) is absent.

    Subclasses :class:`MediaSttError` so the background attachment path (which
    logs any STT failure as a ``stage``-labelled event) records it unchanged;
    the sync endpoint distinguishes it to answer ``503`` (a missing install)
    instead of ``422`` (a bad clip).
    """

    def __init__(self, message: str = STT_EXTRA_HINT) -> None:
        super().__init__(message, stage="import")


def _import_stack() -> tuple[Any, Any]:
    """Import ``(numpy, WhisperModel)`` on demand, or raise :class:`SttUnavailableError`.

    Only a missing module means "not installed"; any other import-time failure
    (a broken ctranslate2/onnxruntime ABI, say) propagates unchanged so a real
    install fault stays a loud error instead of a silent 503.
    """
    try:
        import numpy
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SttUnavailableError() from exc
    return numpy, WhisperModel


@dataclass(frozen=True)
class SttResult:
    """One transcription result.

    ``text`` is whitespace-normalized (``""`` for silence/VAD-filtered input),
    ``language`` is the detected (or forced) ISO code, and ``duration_s`` is the
    duration of the decoded audio in seconds.
    """

    text: str
    language: str
    duration_s: float


def _tail(text: str, limit: int = _STDERR_TAIL_CHARS) -> str:
    """The last ``limit`` characters of ``text`` (whitespace-trimmed)."""
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]


def _suffix_for_mime(mime: str) -> str:
    """Pick a temp-file suffix for ``mime``, defaulting to ``.bin``."""
    base = (mime or "").split(";", 1)[0].strip().lower()
    return _SUFFIX_BY_MIME.get(base, ".bin")


def _write_temp(data: bytes, suffix: str) -> str:
    """Write ``data`` to a fresh temp file and return its path (caller unlinks)."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            os.unlink(path)
        except FileNotFoundError:  # pragma: no cover - defensive
            pass
        raise
    return path


def decode_audio(data: bytes, mime: str, *, out_sr: int = 16000) -> bytes:
    """Decode ``data`` (declared as ``mime``) to mono PCM WAV bytes at ``out_sr``.

    Runs ``ffmpeg`` over temp files (never pipes: containers need a seekable
    input), dropping any video stream and normalizing to a single channel of
    16-bit little-endian PCM at ``out_sr`` Hz. Already-16 kHz-WAV input is
    re-encoded too, so callers always get the same canonical format.

    Args:
        data: Raw container bytes (any format ffmpeg can demux).
        mime: Declared mime type; used only to choose the input temp-file
            suffix (ffmpeg probes the content regardless).
        out_sr: Output sample rate in Hz (default 16000, Whisper's rate).

    Returns:
        The complete WAV file bytes (RIFF header + PCM payload).

    Raises:
        MediaSttError: if ffmpeg exits non-zero or produces no output (the tail
            of its stderr is wrapped in the error).
    """
    in_path = _write_temp(data, suffix=_suffix_for_mime(mime))
    out_fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(out_fd)
    try:
        proc = subprocess.run(
            [
                _FFMPEG,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-i",
                in_path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(out_sr),
                "-acodec",
                "pcm_s16le",
                "-f",
                "wav",
                out_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise MediaSttError(
                f"ffmpeg audio decode failed (exit {proc.returncode})",
                stage="decode",
                stderr=_tail(proc.stderr),
            )
        try:
            with open(out_path, "rb") as handle:
                decoded = handle.read()
        except FileNotFoundError as exc:  # pragma: no cover - defensive
            raise MediaSttError(
                "ffmpeg produced no output file",
                stage="decode",
                stderr=_tail(proc.stderr),
            ) from exc
        if not decoded:
            raise MediaSttError(
                "ffmpeg produced an empty output file",
                stage="decode",
                stderr=_tail(proc.stderr),
            )
        return decoded
    finally:
        for path in (in_path, out_path):
            try:
                os.unlink(path)
            except FileNotFoundError:  # pragma: no cover - defensive
                pass


def _wav_to_float32(wav_bytes: bytes) -> "np.ndarray":
    """Convert 16 kHz mono 16-bit PCM ``wav_bytes`` to a float32 array in [-1, 1].

    Bypasses faster-whisper's PyAV-based decoder (see the module docstring).
    """
    np, _ = _import_stack()

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise MediaSttError(
            f"could not read decoded WAV: {exc}", stage="decode"
        ) from exc

    if width != 2:
        raise MediaSttError(
            f"unexpected WAV sample width: {width} bytes (want 2)", stage="decode"
        )
    if rate != _WHISPER_RATE:
        raise MediaSttError(
            f"unexpected WAV sample rate: {rate} Hz (want {_WHISPER_RATE})",
            stage="decode",
        )

    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples


def transcribe(
    data: bytes,
    mime: str,
    *,
    model_size: str = "small.en",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str | None = None,
) -> SttResult:
    """Decode ``data`` and transcribe it with faster-whisper.

    Raises:
        SttUnavailableError: when the optional ``fabulist[stt]`` stack is not
            installed (checked BEFORE any ffmpeg work).
        MediaSttError: if decoding fails, the model cannot be constructed, or
            transcription itself faults.
    """
    _, whisper_model_cls = _import_stack()  # fail fast: no ffmpeg work without it
    wav_bytes = decode_audio(data, mime)
    audio = _wav_to_float32(wav_bytes)

    try:
        model = whisper_model_cls(model_size, device=device, compute_type=compute_type)
        segments, info = model.transcribe(
            audio,
            vad_filter=True,
            language=language,
        )
    except MediaSttError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize the whole library surface
        raise MediaSttError(f"transcription failed: {exc}", stage="transcribe") from exc

    parts = [" ".join(seg.text.split()) for seg in segments if seg.text]
    text = " ".join(part for part in parts if part).strip()
    detected = info.language or (language or "")
    return SttResult(
        text=text,
        language=str(detected),
        duration_s=float(info.duration),
    )
