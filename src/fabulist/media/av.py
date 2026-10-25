"""Audio/video media validation and classification (W2.x inline a/v).

Images shipped first (:mod:`fabulist.media.images`); this module extends the
same storage contract to **inline audio and video** attachments. It owns three
small, boring things and nothing else:

1. :func:`sniff_kind` — classify uploaded bytes as ``image``/``audio``/``video``
   from magic bytes, using the client's declared mime type only as a tiebreak;
2. :func:`validate_size` — enforce per-kind byte ceilings
   (:data:`KIND_LIMITS`) with a clear :class:`MediaTooLarge`;
3. :func:`embed_content_part` — build the OpenAI-style multimodal content part
   the embedding service expects for each kind, and :func:`embed_inputs` — the
   async sibling of ``embedder.embed_texts`` that posts *raw* input items
   (``{"content": [...]}``), which ``embed_texts`` cannot express.

Storage mirrors the image path exactly (D9: the event log owns the bytes): an
uploaded a/v clip becomes one ``asset`` event whose payload carries the base64
bytes, the mime type, the sniffed ``kind``, a ``note``, and a content-addressed
``asset_id``/``sha256`` (:func:`store_media`). The orchestrator wires the
attachments router; this module never touches the log's schema or the router.

Magic-byte coverage (best effort)
---------------------------------
PNG/JPEG/GIF/WebP images; RIFF ``WAVE``/``AVI ``; ID3-tagged and raw MPEG frame
sync MP3; ``OggS`` audio; ``fLaC`` audio; Matroska/WebM (``\\x1aE\\xdf\\xa3``)
video; and ISO-BMFF (``ftyp``) MP4, where the 4-byte *major brand* decides:
``isom``/``mp42``/``avc1``/``m4v `` are treated as video and ``M4A``-family
brands as audio. ISO-BMFF is genuinely ambiguous (it carries both audio and
video tracks); the brand heuristic is the best a sniffer can do, and an
unrecognised brand falls through to the declared mime type rather than guessing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

import httpx

from fabulist.memory.embedder import EmbedderError, record_embed_usage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import Event, EventLog

try:  # Reuse the image path's content-address helper + actor convention.
    from .images import MEDIA_ACTOR, sha256_hex
except ImportError:  # pragma: no cover - defensive local copies
    MEDIA_ACTOR = "media"

    def sha256_hex(data: bytes) -> str:
        """Hex SHA-256 of ``data`` (local fallback for :mod:`images`)."""
        return hashlib.sha256(data).hexdigest()

__all__ = [
    "KIND_LIMITS",
    "MEDIA_ACTOR",
    "MediaTooLarge",
    "embed_content_part",
    "embed_inputs",
    "sha256_hex",
    "sniff_kind",
    "store_media",
    "validate_size",
]

#: Per-kind byte ceilings. Binary megabytes (MiB); an upload at exactly the
#: limit is accepted, one byte over is rejected.
KIND_LIMITS: dict[str, int] = {
    "image": 20 * 1024 * 1024,
    "audio": 60 * 1024 * 1024,
    "video": 200 * 1024 * 1024,
}

#: ISO-BMFF major brands treated as video (the ``m4v `` brand is 4 bytes).
_MP4_VIDEO_BRANDS = frozenset({b"isom", b"mp42", b"avc1", b"m4v "})

#: Raw MPEG audio frame-sync prefixes (MPEG1/2/2.5 Layer III), best effort.
_MP3_FRAME_SYNCS = (b"\xff\xfb", b"\xff\xf3", b"\xff\xe3")


class MediaTooLarge(ValueError):
    """Raised when an upload exceeds the byte ceiling for its kind.

    Carries ``kind``, ``size`` and ``limit`` so callers can surface a precise
    error without re-parsing the message.
    """

    def __init__(self, kind: str, size: int, limit: int) -> None:
        self.kind = kind
        self.size = size
        self.limit = limit
        super().__init__(
            f"{kind} attachment is {size} bytes, over the "
            f"{limit // (1024 * 1024)} MB limit ({limit} bytes)"
        )


def _sniff_magic(data: bytes, declared_mime: str = "") -> str:
    """Return a kind from magic bytes alone, or ``"unknown"`` when unsure."""
    if data.startswith(b"\x89PNG"):
        return "image"
    if data.startswith(b"\xff\xd8\xff"):
        return "image"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image"
    if data[:4] == b"RIFF" and len(data) >= 12:
        chunk = data[8:12]
        if chunk == b"WEBP":
            return "image"
        if chunk == b"WAVE":
            return "audio"
        if chunk == b"AVI ":
            return "video"
    if data.startswith(b"ID3"):
        return "audio"
    if data[:2] in _MP3_FRAME_SYNCS:
        return "audio"
    if data.startswith(b"OggS"):
        return "audio"
    if data.startswith(b"fLaC"):
        return "audio"
    if data.startswith(b"\x1aE\xdf\xa3"):
        # EBML (webm/matroska) carries BOTH audio-only and video containers.
        # The browser mic records audio-only webm; a video track would be a
        # video file. The magic alone cannot tell them apart without EBML
        # track parsing, so the container DEFERS to the declared mime
        # (our own browser's claim, not adversarial input): audio/* -> audio,
        # video/* -> video. D14 depends on this: mic audio misfiled as video
        # would ride turns as input_video.
        return _kind_from_mime(declared_mime) if _kind_from_mime(declared_mime) != "unknown" else "video"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _MP4_VIDEO_BRANDS:
            return "video"
        if brand[:3] == b"M4A":
            return "audio"
    return "unknown"


def _kind_from_mime(declared_mime: str) -> str:
    """Map a declared mime type to a kind by its ``type/`` prefix."""
    mime = (declared_mime or "").split(";", 1)[0].strip().lower()
    for kind in ("image", "audio", "video"):
        if mime.startswith(kind + "/"):
            return kind
    return "unknown"


def sniff_kind(data: bytes, declared_mime: str) -> str:
    """Classify ``data`` as ``"image"``, ``"audio"``, ``"video"`` or ``"unknown"``.

    Magic bytes win: a PNG uploaded as ``audio/mpeg`` is still an image. Only
    when the bytes are unrecognised does ``declared_mime``'s prefix decide.
    """
    kind = _sniff_magic(data, declared_mime)
    if kind != "unknown":
        return kind
    return _kind_from_mime(declared_mime)


def validate_size(kind: str, n: int) -> None:
    """Raise :class:`MediaTooLarge` when ``n`` exceeds the ceiling for ``kind``.

    Raises :class:`ValueError` for a kind without a configured ceiling.
    """
    limit = KIND_LIMITS.get(kind)
    if limit is None:
        raise ValueError(f"unknown media kind: {kind!r}")
    if n > limit:
        raise MediaTooLarge(kind, n, limit)


def _mime_subtype(mime: str) -> str:
    """The lower-cased subtype of ``mime`` (``audio/x-wav`` -> ``x-wav``)."""
    base = (mime or "").split(";", 1)[0].strip().lower()
    return base.split("/", 1)[-1]


def embed_content_part(kind: str, mime: str, data_b64: str) -> dict[str, Any]:
    """OpenAI-style multimodal content part for one media asset.

    Shapes verified against the embedding service:

    * ``image`` -> ``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<b64>"}}``
    * ``audio`` -> ``{"type": "input_audio", "input_audio": {"data": <b64>, "format": <subtype>}}``
    * ``video`` -> ``{"type": "input_video", "input_video": {"url": "data:<mime>;base64,<b64>"}}``

    The audio ``format`` is the declared mime subtype verbatim (``audio/wav`` ->
    ``wav``). Raises :class:`ValueError` for an unknown ``kind``.
    """
    if kind == "image":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data_b64}"},
        }
    if kind == "audio":
        return {
            "type": "input_audio",
            "input_audio": {"data": data_b64, "format": _mime_subtype(mime)},
        }
    if kind == "video":
        return {
            "type": "input_video",
            "input_video": {"url": f"data:{mime};base64,{data_b64}"},
        }
    raise ValueError(f"unknown media kind: {kind!r}")


async def embed_inputs(
    endpoint: str,
    model: str,
    inputs: list[dict[str, Any]],
    api_key: str | None = None,
    timeout_s: float = 30.0,
) -> list[list[float]]:
    """Embed raw multimodal ``inputs`` via ``{endpoint}/v1/embeddings``.

    Sibling of :func:`fabulist.memory.embedder.embed_texts` for media: instead
    of a list of plain strings, ``inputs`` is a list of OpenAI input items, each
    ``{"content": [<content-part>, ...]}`` as built by :func:`embed_content_part`
    (text may travel as ``{"type": "text", "text": ...}`` parts). Posts
    ``{"model": model, "input": inputs}`` and returns one vector per input, in
    the response's ``index`` order. A non-200 response raises
    :class:`~fabulist.memory.embedder.EmbedderError`.
    """
    url = endpoint.rstrip("/") + "/v1/embeddings"
    payload: dict[str, Any] = {"model": model, "input": inputs}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code != 200:
        # W1f: media ingest embedding records usage on failure too.
        record_embed_usage(
            model=model,
            texts=[json.dumps(item, default=str) for item in inputs],
            latency_s=time.monotonic() - started,
            endpoint=url,
            error=EmbedderError(response.status_code, response.text, url),
        )
        raise EmbedderError(response.status_code, response.text, url)
    body = response.json()
    tokens = (body.get("usage") or {}).get("prompt_tokens")
    record_embed_usage(
        model=model,
        texts=[json.dumps(item, default=str) for item in inputs],
        latency_s=time.monotonic() - started,
        endpoint=url,
        prompt_tokens=tokens if isinstance(tokens, int) else None,
    )
    data = body.get("data", [])
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    return [list(item["embedding"]) for item in ordered]


def store_media(
    log: "EventLog",
    session_id: str,
    data: bytes,
    mime: str,
    kind: str,
    note: str = "",
) -> "Event":
    """Append one ``asset`` event holding an audio/video clip; return it.

    Mirrors :func:`fabulist.media.images.store_image`: the asset is
    content-addressed (``asset_id == sha256 == sha256_hex(data)``), the bytes are
    base64-encoded into the payload (D9), and the same bytes stored twice share
    an id while both append-only rows survive. Any ``kind`` is accepted here —
    classification and size policy belong to :func:`sniff_kind` and
    :func:`validate_size`, which the router applies before storing.
    """
    digest = sha256_hex(data)
    payload = {
        "asset_id": digest,
        "kind": kind,
        "mime": mime,
        "bytes_b64": base64.b64encode(data).decode("ascii"),
        "note": note,
        "sha256": digest,
    }
    return log.append_event(session_id, "asset", MEDIA_ACTOR, payload)
