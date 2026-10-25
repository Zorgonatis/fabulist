"""Client-side media preprocessing for token-bounded embeddings (new lane).

The narrator/embedding path feeds media to llama.cpp's ``mtmd`` at roughly
**924 tokens per frame at native resolution** while the model context is 8192
(:data:`BASE_TOKENS_PER_FRAME`, :data:`BASE_DIM`). A single video must therefore
stay under ~6k tokens. Server-side sampling alone cannot bound a client-chosen
clip, so this module downscales and re-samples video (and downsamples oversized
audio) *before* the bytes ever reach the embedding service, making the token
cost a function of :func:`prepare_video_for_embed`'s budget rather than of the
source length.

Contract (mirrors :mod:`fabulist.media.av`'s "small, boring" style)
-------------------------------------------------------------------
* :class:`MediaInfo` / :func:`probe_media` — describe bytes via ``ffprobe``.
* :func:`estimate_tokens` — predict mtmd token cost, scaling linearly with the
  frame's larger dimension (924/frame is the 1920px constant; a 512px frame is
  empirically ~4x cheaper).
* :func:`prepare_video_for_embed` — passthrough when the clip is already within
  budget *and* already small enough; otherwise transcode with ``ffmpeg``.
* :func:`prepare_audio_for_embed` — passthrough under the byte ceiling, else
  transcode to 16 kHz mono Opus/Vorbis-in-Ogg at 24 kbit/s.
* :class:`MediaPrepError` — every probe/transcode failure, wrapping the tail of
  the tool's stderr so callers can log something actionable.

All work happens through ``subprocess`` with temp files (MP4 demuxing needs a
seekable input, so stdin/stdout piping is not used for video). No global state,
no imports from the rest of Fabulist — this lane owns these two files only.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction

__all__ = [
    "BASE_DIM",
    "BASE_TOKENS_PER_FRAME",
    "MediaInfo",
    "MediaPrepError",
    "estimate_tokens",
    "prepare_audio_for_embed",
    "prepare_video_for_embed",
    "probe_media",
]

#: Native-resolution mtmd token cost of a single embedded frame.
BASE_TOKENS_PER_FRAME: float = 924.0

#: The "native" frame width the mtmd constant was measured at.
BASE_DIM: float = 1920.0

#: Bytes in a binary megabyte (used by the audio ceiling).
_MIB: int = 1024 * 1024

#: ffmpeg/ffprobe path (overridable via env for non-standard installs).
_FFMPEG: str = os.environ.get("FABULIST_FFMPEG", "ffmpeg")
_FFPROBE: str = os.environ.get("FABULIST_FFPROBE", "ffprobe")

#: How much stderr to keep when wrapping a tool failure.
_STDERR_TAIL_CHARS: int = 800


class MediaPrepError(Exception):
    """Raised when probing or transcoding media fails.

    The message includes the offending command/tool and the tail of its stderr
    so the caller can surface a precise, debuggable error. ``stderr`` (full
    captured text) is attached when available.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        self.stderr = stderr
        super().__init__(message)


@dataclass(frozen=True)
class MediaInfo:
    """Descriptive metadata of the first video stream of a media blob.

    ``duration_s`` falls back to the container duration when the stream does not
    declare one; ``fps`` is the stream's average frame rate (using the nominal
    rate when the average is unknown).
    """

    width: int
    height: int
    duration_s: float
    fps: float
    codec: str


def _tail(text: str, limit: int = _STDERR_TAIL_CHARS) -> str:
    """The last ``limit`` characters of ``text`` (newline-trimmed)."""
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` capturing text output; never raise on a non-zero exit."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_temp(data: bytes, suffix: str = ".bin") -> str:
    """Write ``data`` to a fresh temp file (caller unlinks) and return its path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        os.unlink(path)
        raise
    return path


def _parse_fraction(value: object, *, default: float = 0.0) -> float:
    """Parse an ffprobe rational like ``"30000/1001"`` into a float."""
    if isinstance(value, str) and "/" in value:
        num, _, den = value.partition("/")
        try:
            denominator = float(den)
            if denominator == 0:
                return default
            return float(Fraction(int(num), int(den)))
        except (ValueError, ZeroDivisionError):
            return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def probe_media(data: bytes) -> MediaInfo:
    """Probe ``data`` with ``ffprobe`` and describe its first video stream.

    Writes ``data`` to a temp file, runs
    ``ffprobe -v quiet -print_format json -show_streams -show_format``, and
    parses the first stream whose ``codec_type == "video"``. The container
    duration is used when the stream omits one.

    Raises:
        MediaPrepError: on empty input, a non-zero/non-JSON ffprobe result, or
            when no video stream can be found.
    """
    if not data:
        raise MediaPrepError("probe_media: empty input")
    path = _write_temp(data)
    try:
        proc = _run(
            [
                _FFPROBE,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                path,
            ]
        )
    finally:
        os.unlink(path)

    if proc.returncode != 0:
        raise MediaPrepError(
            f"ffprobe failed (exit {proc.returncode}) for {len(data)} bytes",
            stderr=_tail(proc.stderr),
        )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise MediaPrepError(
            "ffprobe returned non-JSON output",
            stderr=_tail(proc.stdout or proc.stderr),
        ) from exc

    streams = payload.get("streams") or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise MediaPrepError(
            "no video stream found",
            stderr=_tail(proc.stderr),
        )

    container = payload.get("format") or {}
    duration = video.get("duration")
    if duration in (None, "N/A"):
        duration = container.get("duration")
    fps_raw = video.get("avg_frame_rate")
    if fps_raw in (None, "0/0", "N/A"):
        fps_raw = video.get("r_frame_rate")

    return MediaInfo(
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        duration_s=_parse_fraction(duration),
        fps=_parse_fraction(fps_raw),
        codec=str(video.get("codec_name") or "unknown"),
    )


def estimate_tokens(
    info: MediaInfo,
    target_max_dim: float | None = None,
    base_tokens_per_frame: float = BASE_TOKENS_PER_FRAME,
    base_dim: float = BASE_DIM,
) -> int:
    """Predict the mtmd token cost of embedding ``info``'s frames.

    Frames = ``duration_s * fps``. The per-frame cost scales linearly with the
    frame's larger dimension relative to the native constant:

        tokens = frames * base_tokens_per_frame * (effective_dim / base_dim)

    ``target_max_dim`` is the dimension the clip will be handled at (matching
    :func:`prepare_video_for_embed`'s ``max_dim``). Because the scale filter
    never upscales, the effective dimension is ``min(target_max_dim, native)``.
    When ``target_max_dim`` is ``None`` the clip's own native larger dimension is
    used, i.e. the un-downscaled estimate (for a 1920px source the factor is
    exactly 1.0).

    Returns an ``int`` (truncated) token count.
    """
    native = max(info.width, info.height)
    effective = native if target_max_dim is None else min(target_max_dim, native)
    frames = max(info.duration_s, 0.0) * max(info.fps, 0.0)
    scale = effective / base_dim if base_dim else 1.0
    return int(frames * base_tokens_per_frame * scale)


def _sample_fps(
    info: MediaInfo,
    token_budget: int,
    max_dim: int,
    base_tokens_per_frame: float,
    base_dim: float,
) -> float:
    """Frames/second that keeps ``info`` within ``token_budget`` after downscale.

    ``token_budget / (duration_s * tokens_per_frame_at_max_dim)`` clamped to
    ``[0.1, 1.0]`` so output is always a legal, non-degenerate frame rate.
    """
    per_frame = base_tokens_per_frame * (max_dim / base_dim) if base_dim else base_tokens_per_frame
    duration = max(info.duration_s, 0.0)
    if duration <= 0 or per_frame <= 0:
        return 1.0
    ideal = token_budget / (duration * per_frame)
    return min(1.0, max(0.1, ideal))


def _transcode_video(data: bytes, fps: float, max_dim: int) -> bytes:
    """Run the downscale+refps ffmpeg transcode and return the new bytes."""
    in_path = _write_temp(data, suffix=".mp4")
    out_fd, out_path = tempfile.mkstemp(suffix=".mp4")
    os.close(out_fd)
    try:
        proc = _run(
            [
                _FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                in_path,
                "-vf",
                f"scale='min({max_dim},iw)':-2,fps={fps}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                out_path,
            ]
        )
        if proc.returncode != 0:
            raise MediaPrepError(
                f"ffmpeg video transcode failed (exit {proc.returncode})",
                stderr=_tail(proc.stderr),
            )
        with open(out_path, "rb") as handle:
            return handle.read()
    finally:
        for path in (in_path, out_path):
            try:
                os.unlink(path)
            except FileNotFoundError:  # pragma: no cover - defensive
                pass


def prepare_video_for_embed(
    data: bytes,
    *,
    token_budget: int = 6000,
    max_dim: int = 512,
) -> bytes:
    """Downscale/re-sample ``data`` so its mtmd cost fits ``token_budget``.

    Probes the clip, then returns it **unchanged** when it is already bounded:
    :func:`estimate_tokens` at ``max_dim`` is ``<= token_budget`` *and* the
    native larger dimension is ``<= max_dim``. Otherwise it transcodes with
    ``libx264`` (CRF 28, ``veryfast``, ``yuv420p``, faststart) at a frame rate
    chosen by :func:`_sample_fps` — bounding tokens regardless of source length.

    Raises:
        MediaPrepError: when probing fails or ffmpeg exits non-zero (the tail of
            ffmpeg's stderr is wrapped in the error).
    """
    info = probe_media(data)
    native = max(info.width, info.height)
    if (
        native <= max_dim
        and estimate_tokens(info, target_max_dim=max_dim) <= token_budget
    ):
        return data
    fps = _sample_fps(
        info,
        token_budget,
        max_dim,
        BASE_TOKENS_PER_FRAME,
        BASE_DIM,
    )
    return _transcode_video(data, fps, max_dim)


def prepare_audio_for_embed(data: bytes, *, max_mb: float = 8.0) -> bytes:
    """Return ``data`` unchanged under ``max_mb`` else transcode to small Opus.

    Bytes above ``max_mb`` binary megabytes are re-encoded to 16 kHz mono
    Opus/Vorbis-in-Ogg at 24 kbit/s, which is more than enough for speech
    embedding while collapsing long clips by more than an order of magnitude.

    Raises:
        MediaPrepError: when ffmpeg exits non-zero (stderr tail wrapped).
    """
    limit = int(max_mb * _MIB)
    if len(data) <= limit:
        return data

    in_path = _write_temp(data, suffix=".bin")
    out_fd, out_path = tempfile.mkstemp(suffix=".ogg")
    os.close(out_fd)
    try:
        proc = _run(
            [
                _FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                in_path,
                "-c:a",
                "libopus",
                "-b:a",
                "24k",
                "-ar",
                "16000",
                "-ac",
                "1",
                out_path,
            ]
        )
        if proc.returncode != 0:
            raise MediaPrepError(
                f"ffmpeg audio transcode failed (exit {proc.returncode})",
                stderr=_tail(proc.stderr),
            )
        with open(out_path, "rb") as handle:
            return handle.read()
    finally:
        for path in (in_path, out_path):
            try:
                os.unlink(path)
            except FileNotFoundError:  # pragma: no cover - defensive
                pass
