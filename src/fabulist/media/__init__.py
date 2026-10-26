"""Media input package (W2.8).

Image input follows the *captioner path*: the raw bytes are stored as an
``asset`` event in the append-only log, a vision-capable *captioner* agent turns
them into a short visible-detail caption AT ARRIVAL, and that text is the
fallback for every text-only consumer (``[inline image caption: ...]``). A
connection with its ``vision`` toggle on receives the native image content part
instead. The event log owns the bytes (D9); the caption is a ``system`` event.
"""

from __future__ import annotations

from .images import (
    CAPTION_EVENT,
    CAPTION_INSTRUCTION,
    CAPTION_MAX_WORDS,
    CAPTION_SYSTEM,
    IMAGE_CAPTION_PLACEHOLDER,
    MEDIA_ACTOR,
    MEDIA_META_KEY,
    build_caption_messages,
    build_image_turn_prefix,
    caption,
    connection_has_vision,
    find_asset,
    find_caption,
    image_content_part,
    render_image_history_message,
    rerender_image_history,
    sha256_hex,
    store_image,
)

__all__ = [
    "CAPTION_EVENT",
    "CAPTION_INSTRUCTION",
    "CAPTION_MAX_WORDS",
    "CAPTION_SYSTEM",
    "IMAGE_CAPTION_PLACEHOLDER",
    "MEDIA_ACTOR",
    "MEDIA_META_KEY",
    "build_caption_messages",
    "build_image_turn_prefix",
    "caption",
    "connection_has_vision",
    "find_asset",
    "find_caption",
    "image_content_part",
    "render_image_history_message",
    "rerender_image_history",
    "sha256_hex",
    "store_image",
]
