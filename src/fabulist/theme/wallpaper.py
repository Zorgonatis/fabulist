"""Wallpaper: the reader's own image behind the prose (issue #13, ADR-027 family).

The appearance store (issue #12) made one decision for this issue: **a wallpaper
is a new field of the same document, not a second location**. So the *bytes* of
the image live in ``data/appearance/wallpaper.<ext>`` — a directory derived from
the document's own path — and the *fact* that a wallpaper is set lives in
``data/appearance.json`` as ``{"wallpaper": "wallpaper.jpg", "fit": "cover"}``.
Both are per-install user state (ADR-027's carve-out from ADR-001): a wallpaper
is furniture, it must not fork with a branch, and re-picking an image must never
spray system events into a session's audit trail.

What this module owns
---------------------
Three boring, testable things and nothing else:

1. :func:`sniff_format` — classify the bytes as PNG / JPEG / GIF / WebP from
   magic bytes. The declared upload mime type is **not** consulted: a file's
   extension is a claim, its header is a fact, and the stored extension has to
   be the one the bytes actually are (that extension is what the browser's
   ``Content-Type`` will agree with).
2. :func:`image_size` — read width/height out of the container header with the
   standard library only. There is no Pillow in this project's dependency set
   and this feature does not earn one: the caps below only need the four
   integers a header carries, never a decoded pixel.
3. :func:`validate_upload` — enforce the owner's caps (issue #13 ruling) with a
   reason the drawer can show verbatim:

   * **8 MB** — ``MAX_WALLPAPER_BYTES`` (binary megabytes, an upload at exactly
     the limit is accepted);
   * **4096 px on each edge** — ``MAX_WALLPAPER_EDGE``. This is a *reject*, not
     a downscale: re-encoding a reader's image (and losing its crop) is a much
     larger behaviour than a clear "that is too big, here is the cap".

Formats: PNG (the safe default for a drawn wallpaper), JPEG (the photo case),
WebP (what the studio's generators emit) and GIF. AVIF/HEIC are deliberately
absent — their size lives in an ISO-BMFF box tree that stdlib parsing gets
wrong more often than right, and shipping a wallpaper the layout then can't
measure is worse than refusing it with a reason.

None of this is a security boundary (the image is the reader's own furniture and
only ever served back to their own browser, ``image/*`` ``Content-Type`` with
``X-Content-Type-Options: nosniff``); it is how "pick an image" stays a small,
predictable feature instead of a pile of surprise encodings.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_FIT",
    "FITS",
    "MAX_WALLPAPER_BYTES",
    "MAX_WALLPAPER_EDGE",
    "WallpaperError",
    "WallpaperFormat",
    "WallpaperInfo",
    "describe_file",
    "image_size",
    "sniff_format",
    "validate_upload",
]

#: Byte ceiling for an uploaded wallpaper. Binary megabytes (MiB): an upload at
#: exactly the limit is accepted, one byte over is refused.
MAX_WALLPAPER_BYTES = 8 * 1024 * 1024

#: Per-edge pixel ceiling. Anything larger is refused with the measured size in
#: the reason — never silently downscaled (see the module docstring).
MAX_WALLPAPER_EDGE = 4096

#: How the image meets the viewport. ``cover`` is the only mode this issue ships
#: (owner ruling, thread #13): a wallpaper's job is to fill the room, and every
#: other fit either letterboxes the prose column or tiles the chrome. The tuple
#: is the extension point — a new mode is a new entry plus a token re-solve, not
#: a second code path.
FITS: tuple[str, ...] = ("cover",)

#: The mode a document that predates the field (or a garbage value) falls back to.
DEFAULT_FIT = "cover"

#: How much of a file to read to find the dimensions. Container headers sit at
#: the front; a JPEG's SOF marker is within the first few KB for every encoder
#: in the wild. ``describe_file`` re-reads the whole file as a fallback rather
#: than refusing an image it could have measured.
_HEADER_BYTES = 64 * 1024


class WallpaperError(ValueError):
    """An upload was refused (unsupported format, oversized, unmeasurable)."""


@dataclass(frozen=True)
class WallpaperFormat:
    """One accepted container: the stored extension and the served mime type."""

    ext: str
    mime: str
    label: str


PNG = WallpaperFormat(ext="png", mime="image/png", label="PNG")
JPEG = WallpaperFormat(ext="jpg", mime="image/jpeg", label="JPEG")
GIF = WallpaperFormat(ext="gif", mime="image/gif", label="GIF")
WEBP = WallpaperFormat(ext="webp", mime="image/webp", label="WebP")

#: Accepted formats in presentation order (the drawer shows these labels).
FORMATS: tuple[WallpaperFormat, ...] = (PNG, JPEG, GIF, WEBP)


@dataclass(frozen=True)
class WallpaperInfo:
    """What the API needs to know about the stored image (dims from the header)."""

    format: WallpaperFormat
    width: int
    height: int
    size: int


def sniff_format(data: bytes) -> WallpaperFormat | None:
    """Classify ``data`` from its magic bytes, or ``None`` when unsupported."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return GIF
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    """PNG: the IHDR chunk is the first one — width/height are big-endian u32."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _gif_size(data: bytes) -> tuple[int, int] | None:
    """GIF: the logical screen descriptor — width/height are little-endian u16."""
    if len(data) < 10:
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return int(width), int(height)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """JPEG: walk the marker chain to a Start-Of-Frame and read its dimensions.

    SOF0..SOF15 carry the frame size, except 0xC4 (DHT), 0xC8 (JPG) and 0xCC
    (DAC), which are not frames. Standalone markers (RSTn, TEM, SOI/EOI) carry
    no length field, so they are stepped over one byte at a time.
    """
    offset = 2  # skip SOI (\xff\xd8)
    limit = len(data)
    while offset + 4 <= limit:
        if data[offset] != 0xFF:
            offset += 1  # resynchronise on entropy-coded padding
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker == 0xD9:  # EOI — no frame was found
            return None
        (length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
        if length < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if offset + 9 > limit:
                return None
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return int(width), int(height)
        offset += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    """WebP: three container flavours carry the canvas size differently."""
    if len(data) < 16:
        return None
    chunk = data[12:16]
    if chunk == b"VP8 ":
        # Lossy: a 3-byte frame tag, the 3-byte start code, then 14-bit dims.
        if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            return None
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return int(width), int(height)
    if chunk == b"VP8L":
        # Lossless: 0x2f signature, then 14 bits of (width-1), 14 of (height-1).
        if len(data) < 25 or data[20] != 0x2F:
            return None
        (bits,) = struct.unpack("<I", data[21:25])
        return int(bits & 0x3FFF) + 1, int((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        # Extended: a 24-bit little-endian (canvas size - 1) on each axis.
        if len(data) < 30:
            return None
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


_SIZERS = {PNG.ext: _png_size, JPEG.ext: _jpeg_size, GIF.ext: _gif_size, WEBP.ext: _webp_size}


def image_size(data: bytes, fmt: WallpaperFormat) -> tuple[int, int] | None:
    """Read ``(width, height)`` out of the container header, or ``None``.

    ``None`` means "these bytes do not carry a size this module can read" — a
    truncated upload, or a container whose header lies outside the bytes given.
    Callers decide whether that is a refusal (an upload) or a missing reading.
    """
    sizer = _SIZERS.get(fmt.ext)
    return sizer(data) if sizer is not None else None


def validate_upload(data: bytes) -> WallpaperInfo:
    """Validate an uploaded image against the owner's caps (issue #13 ruling).

    Returns the format + measured size for a keeper. Raises
    :class:`WallpaperError` with a sentence the drawer can show as-is:
    unsupported container, unmeasurable header, oversized edge, oversized bytes.

    The size cap is checked FIRST (before any parsing): a 40 MB upload should be
    refused for the reason that matters, not for whatever its header looks like.
    """
    if not data:
        raise WallpaperError("the upload was empty — pick an image file")
    if len(data) > MAX_WALLPAPER_BYTES:
        raise WallpaperError(
            f"the image is {len(data)} bytes; the limit is {MAX_WALLPAPER_BYTES} bytes "
            f"({MAX_WALLPAPER_BYTES // (1024 * 1024)} MB)"
        )
    fmt = sniff_format(data)
    if fmt is None:
        known = ", ".join(item.label for item in FORMATS)
        raise WallpaperError(f"unsupported image format — use one of: {known}")
    size = image_size(data, fmt)
    if size is None:
        raise WallpaperError(
            f"could not read the {fmt.label} dimensions — the file looks truncated"
        )
    width, height = size
    if width > MAX_WALLPAPER_EDGE or height > MAX_WALLPAPER_EDGE:
        raise WallpaperError(
            f"the image is {width}×{height}px; the limit is {MAX_WALLPAPER_EDGE}px "
            f"on each edge"
        )
    if width <= 0 or height <= 0:
        raise WallpaperError(f"the image reports a {width}×{height}px size")
    return WallpaperInfo(format=fmt, width=width, height=height, size=len(data))


def describe_file(path: Path) -> WallpaperInfo:
    """Read a stored wallpaper's format + dimensions from disk (header first).

    Used on every appearance read, so it reads the front of the file only. A
    header that does not yield a size (a JPEG whose SOF sits unusually deep)
    triggers one full read rather than reporting a wallpaper the layout cannot
    measure — the file is capped at 8 MB, so the fallback is bounded.
    """
    with path.open("rb") as handle:
        head = handle.read(_HEADER_BYTES)
    fmt = sniff_format(head)
    if fmt is None:
        raise WallpaperError("the stored wallpaper is not a recognised image")
    size = image_size(head, fmt)
    if size is None and path.stat().st_size > len(head):
        size = image_size(path.read_bytes(), fmt)
    if size is None:
        raise WallpaperError("could not read the stored wallpaper's dimensions")
    width, height = size
    return WallpaperInfo(
        format=fmt, width=width, height=height, size=path.stat().st_size
    )
