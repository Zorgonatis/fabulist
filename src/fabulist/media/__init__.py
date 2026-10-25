"""Media input package (W2.8).

v1 image input follows the *captioner path*: the raw bytes are stored as an
``asset`` event in the append-only log, a vision-capable *captioner* agent turns
them into a short visible-detail caption, and only that caption text enters the
narrator turn (as a ``[image: ...]`` prefix). The event log owns the bytes
(D9); the caption is a ``system`` event.

The narrator never receives image bytes in v1 — see
:mod:`fabulist.media.images` for the documented VLM content-part seam that a
future narrator model would use instead.
"""

from __future__ import annotations

from .images import (
    CAPTION_EVENT,
    CAPTION_INSTRUCTION,
    CAPTION_MAX_WORDS,
    CAPTION_SYSTEM,
    MEDIA_ACTOR,
    build_caption_messages,
    build_image_turn_prefix,
    caption,
    find_asset,
    image_content_part,
    sha256_hex,
    store_image,
)

__all__ = [
    "CAPTION_EVENT",
    "CAPTION_INSTRUCTION",
    "CAPTION_MAX_WORDS",
    "CAPTION_SYSTEM",
    "MEDIA_ACTOR",
    "build_caption_messages",
    "build_image_turn_prefix",
    "caption",
    "find_asset",
    "image_content_part",
    "sha256_hex",
    "store_image",
]
