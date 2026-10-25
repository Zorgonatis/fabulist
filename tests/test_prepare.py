"""Tests for client-side media preprocessing (:mod:`fabulist.media.prepare`).

These tests generate **real** media in-test with ``ffmpeg`` rather than mocking
the tools, because the whole point of the lane is to bound what the embedder
actually receives: probe parsing, token math, the passthrough fast path, and a
genuine downscale+re-sample transcode of a large clip all run against the real
``ffmpeg``/``ffprobe`` binaries. The module skips cleanly when ``ffmpeg`` is not
installed.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from fabulist.media.prepare import (
    BASE_DIM,
    BASE_TOKENS_PER_FRAME,
    MediaInfo,
    MediaPrepError,
    estimate_tokens,
    prepare_audio_for_embed,
    prepare_video_for_embed,
    probe_media,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


# ---------------------------------------------------------------------------
# In-test media generation
# ---------------------------------------------------------------------------


def _make_video(path, *, duration: float, size: str, rate: int) -> bytes:
    """Render a synthetic ``testsrc2`` clip to ``path`` and return its bytes."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=duration={duration}:size={size}:rate={rate}",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:  # pragma: no cover - environment failure
        pytest.skip(f"ffmpeg could not generate fixture: {proc.stderr[-400:]}")
    return path.read_bytes()


def _make_audio(path, *, duration: float) -> bytes:
    """Render a synthetic sine clip to ``path`` and return its bytes."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:  # pragma: no cover - environment failure
        pytest.skip(f"ffmpeg could not generate fixture: {proc.stderr[-400:]}")
    return path.read_bytes()


@pytest.fixture(scope="module")
def tiny_video(tmp_path_factory) -> bytes:
    """A clip already within budget and under ``max_dim`` (320x240, 1s, 5fps)."""
    return _make_video(
        tmp_path_factory.mktemp("tiny") / "tiny.mp4",
        duration=1,
        size="320x240",
        rate=5,
    )


@pytest.fixture(scope="module")
def mid_video(tmp_path_factory) -> bytes:
    """A 640x480, 4s, 10fps clip used for probe assertions."""
    return _make_video(
        tmp_path_factory.mktemp("mid") / "mid.mp4",
        duration=4,
        size="640x480",
        rate=10,
    )


@pytest.fixture(scope="module")
def large_video(tmp_path_factory) -> bytes:
    """A 1920x1080, 30s, 30fps clip far over the token budget."""
    return _make_video(
        tmp_path_factory.mktemp("large") / "large.mp4",
        duration=30,
        size="1920x1080",
        rate=30,
    )


@pytest.fixture(scope="module")
def small_audio(tmp_path_factory) -> bytes:
    """A short real audio clip (well under the default byte ceiling)."""
    return _make_audio(tmp_path_factory.mktemp("audio") / "tone.wav", duration=0.5)


# ---------------------------------------------------------------------------
# probe_media
# ---------------------------------------------------------------------------


def test_probe_parses_real_video(mid_video: bytes) -> None:
    info = probe_media(mid_video)
    assert isinstance(info, MediaInfo)
    assert (info.width, info.height) == (640, 480)
    assert info.duration_s == pytest.approx(4.0, abs=0.25)
    assert info.fps == pytest.approx(10.0, abs=0.1)
    assert info.codec  # e.g. "h264"


def test_probe_garbage_raises() -> None:
    with pytest.raises(MediaPrepError):
        probe_media(b"this is definitely not a media file")


def test_probe_empty_raises() -> None:
    with pytest.raises(MediaPrepError):
        probe_media(b"")


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_native_math() -> None:
    # 1920x1080 @ 1fps for 10s -> 10 frames, native factor is exactly 1.0.
    info = MediaInfo(width=1920, height=1080, duration_s=10.0, fps=1.0, codec="h264")
    assert estimate_tokens(info) == int(10 * BASE_TOKENS_PER_FRAME * 1.0)
    assert estimate_tokens(info) == 9240


def test_estimate_tokens_downscaled_math() -> None:
    info = MediaInfo(width=1920, height=1080, duration_s=10.0, fps=1.0, codec="h264")
    expected = int(10 * BASE_TOKENS_PER_FRAME * (512 / BASE_DIM))
    assert estimate_tokens(info, target_max_dim=512) == expected
    assert expected == 2464
    # The 512px estimate is ~3.75x cheaper than native (512/1920 scaling).
    downscaled = estimate_tokens(info, target_max_dim=512)
    assert downscaled * 3 < estimate_tokens(info)
    assert downscaled / estimate_tokens(info) == pytest.approx(512 / BASE_DIM)


def test_estimate_tokens_never_upscales_small_source() -> None:
    # A 320px source stays 320px even when asked for a 512px target.
    info = MediaInfo(width=320, height=240, duration_s=2.0, fps=5.0, codec="h264")
    native = estimate_tokens(info)
    assert estimate_tokens(info, target_max_dim=512) == native


def test_estimate_tokens_on_real_clip(mid_video: bytes) -> None:
    info = probe_media(mid_video)
    frames = info.duration_s * info.fps
    assert estimate_tokens(info, target_max_dim=512) == int(
        frames * BASE_TOKENS_PER_FRAME * (512 / BASE_DIM)
    )


# ---------------------------------------------------------------------------
# prepare_video_for_embed
# ---------------------------------------------------------------------------


def test_prepare_video_passthrough_when_already_small(tiny_video: bytes) -> None:
    out = prepare_video_for_embed(tiny_video)
    assert out == tiny_video
    info = probe_media(tiny_video)
    assert estimate_tokens(info, target_max_dim=512) <= 6000


def test_prepare_video_transcodes_large_clip(large_video: bytes) -> None:
    out = prepare_video_for_embed(large_video)
    assert out != large_video
    assert len(out) < len(large_video)
    info = probe_media(out)
    assert max(info.width, info.height) <= 512
    assert info.fps <= 1.0 + 1e-6


def test_prepare_video_downscales_over_dim_source(mid_video: bytes) -> None:
    # 640 > 512 so it must be re-encoded even though 4s is short.
    out = prepare_video_for_embed(mid_video)
    assert out != mid_video
    info = probe_media(out)
    assert max(info.width, info.height) <= 512


def test_prepare_video_garbage_raises() -> None:
    with pytest.raises(MediaPrepError):
        prepare_video_for_embed(b"not a video at all")


# ---------------------------------------------------------------------------
# prepare_audio_for_embed
# ---------------------------------------------------------------------------


def test_prepare_audio_passthrough_for_small_input(small_audio: bytes) -> None:
    assert prepare_audio_for_embed(small_audio) == small_audio
    # A tiny blob is trivially under the ceiling.
    assert prepare_audio_for_embed(b"tiny") == b"tiny"


def test_prepare_audio_transcodes_over_ceiling(small_audio: bytes) -> None:
    # Force the transcode path by shrinking the ceiling below the input size.
    out = prepare_audio_for_embed(small_audio, max_mb=0.0001)
    assert out != small_audio
    assert out.startswith(b"OggS")  # Opus/Vorbis-in-Ogg container
    assert len(out) < len(small_audio)


def test_prepare_audio_garbage_raises() -> None:
    with pytest.raises(MediaPrepError):
        prepare_audio_for_embed(b"definitely not audio", max_mb=0.000001)
