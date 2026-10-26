"""Image input, v1: the captioner path (W2.8).

The event log owns all data (D9/D10/D11). An uploaded image is appended as one
``asset`` event whose payload carries the bytes (base64), mime type, a
content-addressed ``asset_id`` and ``sha256``, and a free-text ``note``. Nothing
is written to a side table — :meth:`EventLog.get_projection` already collects
``asset`` rows into ``projection["assets"]`` (see ``log/repo.py`` and
``tests/test_log.py::test_asset_event_appears_in_projection``).

Definition images (personas/worlds)
-----------------------------------
Definitions are edited with no chat open (AGENTS.md non-negotiable 7), so they
have no session to store bytes on. :func:`store_definition_image` /
:func:`find_definition_asset` use the reserved :data:`MEDIA_ANCHOR` session
scope instead, and dedupe by content address so a re-import adds no second
asset. The session-scoped :func:`store_image` / :func:`find_asset` are
unchanged (:func:`store_image`'s optional ``anchor=True`` switches scopes).

The captioner path
------------------
A vision-capable *captioner* agent (registry name ``captioner``) receives the
image as an OpenAI-style multimodal content part and returns a short
visible-detail caption. That caption is appended as a ``system`` event
``{"event": "image_caption", "asset": <asset_id>, "text": <caption>}``. Only the
caption text enters the narrator turn, via :func:`build_image_turn_prefix`, so
the narrator prompt/compiler needs no change and stays text-only.

VLM seam (documented, intentionally NOT built)
----------------------------------------------
A future vision narrator would skip the caption entirely and hand the image to
the narrator as a content part. The turn's final user message is built in
``prompts/narrator.assemble_messages`` / ``wiring.make_narrator_message_builder``
and is currently ``{"role": "user", "content": "<str>"}``. A VLM narrator would
instead emit::

    {"role": "user", "content": [
        {"type": "text", "text": "<facts + user text>"},
        {"type": "image_url",
         "image_url": {"url": "data:<mime>;base64,<bytes_b64>"}},
    ]}

built from the same stored ``asset`` payload (:func:`image_content_part` below
shows the exact shape). The compiler's ``user_content`` seam would accept either
a string or a list of parts, and the captioner path could then be dropped. Until
that model is available, the captioner is the v1 workaround; no code here
implements content-part delivery to the narrator.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any

from fabulist.graph.types import ModelCaller, collect_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import Event, EventLog

__all__ = [
    "CAPTION_EVENT",
    "CAPTION_INSTRUCTION",
    "CAPTION_MAX_WORDS",
    "CAPTION_SYSTEM",
    "MAX_IMAGE_BYTES",
    "MEDIA_ACTOR",
    "MEDIA_ANCHOR",
    "build_caption_messages",
    "build_image_turn_prefix",
    "caption",
    "find_asset",
    "find_definition_asset",
    "image_content_part",
    "is_image_narrative",
    "sha256_hex",
    "store_definition_image",
    "store_image",
]

#: Actor recorded on media-appended events (matches the log's media convention).
MEDIA_ACTOR = "media"


def is_image_narrative(item: dict) -> bool:
    """Whether ``item`` is a generated-image narrative row.

    A generated-image narrative row carries no prose: it is a canon image (the
    asset id points at the ``asset`` event holding the bytes), not a narrative
    turn. Consumers use this to route such rows to multimodal handling instead
    of the prose path.
    """
    payload = item.get("payload") or {}
    return item.get("type") == "narrative" and bool(payload.get("image_asset_id"))

#: Reserved session id anchoring **sessionless** (definition) image assets.
#:
#: Personas and worlds are edited with no chat open (AGENTS.md non-negotiable 7),
#: so their images cannot live on a chat session. They are stored as ``asset``
#: events on this reserved anchor instead — the exact ``__``-prefix convention
#: :data:`fabulist.personas.registry.PERSONAS_ANCHOR` /
#: :data:`fabulist.worlds.registry.WORLDS_ANCHOR` use, so the anchor stays out
#: of the session list (:func:`fabulist.api.sessions._session_ids`).
MEDIA_ANCHOR = "__media__"

#: Byte ceiling for one definition-image upload (10 MiB). Enforced by the
#: definition routers; a definition image is a UI thumbnail, not a scene asset
#: (contrast :data:`fabulist.media.av.KIND_LIMITS`).
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: ``system`` event kind carrying a caption row.
CAPTION_EVENT = "image_caption"

#: Soft budget stated in the captioner prompt (words).
CAPTION_MAX_WORDS = 120

#: System instruction for the captioner agent: visible detail only.
CAPTION_SYSTEM = (
    "You describe images for a role-play narrator's context. Describe only what "
    "is visible: concrete nouns, physical detail, spatial layout, colours, and "
    "any text that appears. Use at most "
    f"{CAPTION_MAX_WORDS} words. Do not interpret intent, emotion, meaning, or "
    "narrative significance, and do not address the viewer. Output the "
    "description only, with no preamble or commentary."
)

#: The user turn that accompanies the image content part.
CAPTION_INSTRUCTION = "Describe the attached image for the scene context."


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 of ``data`` (the content address used for ``asset_id``)."""
    return hashlib.sha256(data).hexdigest()


def store_image(
    log: "EventLog",
    session_id: str,
    data: bytes,
    mime: str,
    note: str = "",
    *,
    anchor: bool = False,
) -> "Event":
    """Append one ``asset`` event holding an image; return the appended event.

    The asset is content-addressed: ``asset_id`` is the SHA-256 hex of the
    bytes, so the same image stored twice shares an id (the log stays
    append-only; the latest projection wins). ``data`` is base64-encoded into
    the payload — v1 stores bytes in the log for simplicity (D9); a future
    large-blob policy could move them to disk via ``EventLog.add_asset`` while
    keeping the event payload as the pointer, but that is out of scope here.

    ``anchor=True`` stores the image on the sessionless
    :data:`MEDIA_ANCHOR` scope instead of ``session_id`` (``session_id`` is then
    ignored) — for definition images (personas/worlds) that have no chat.
    Session-scoped callers are unchanged (the default).
    """
    scope = MEDIA_ANCHOR if anchor else session_id
    digest = sha256_hex(data)
    payload = {
        "asset_id": digest,
        "kind": "image",
        "mime": mime,
        "bytes_b64": base64.b64encode(data).decode("ascii"),
        "sha256": digest,
        "note": note,
    }
    return log.append_event(scope, "asset", MEDIA_ACTOR, payload)


def find_asset(
    log: "EventLog",
    session_id: str,
    asset_ref: str,
    *,
    anchor: bool = False,
) -> dict | None:
    """Return the projected ``asset`` item for ``asset_ref``, or ``None``.

    ``asset_ref`` is matched against the payload ``asset_id`` first, then
    against the event's numeric id (stringified) so callers may reference
    either the content address or the log row.

    ``anchor=True`` reads the sessionless :data:`MEDIA_ANCHOR` scope instead of
    ``session_id`` (``session_id`` is then ignored) — the lookup path for a
    definition image. Session-scoped callers are unchanged (the default).
    """
    projection = log.get_projection(MEDIA_ANCHOR if anchor else session_id)
    for item in projection.get("assets", []):
        payload = item.get("payload", {})
        if payload.get("asset_id") == asset_ref or str(item.get("id")) == str(
            asset_ref
        ):
            return item
    return None


def store_definition_image(
    log: "EventLog", data: bytes, mime: str, note: str = ""
) -> str:
    """Store a sessionless definition image; return its content-addressed id.

    Writes on the :data:`MEDIA_ANCHOR` scope and is **idempotent by content
    address**: when identical bytes are already stored the existing row is
    reused and nothing is appended, so re-importing the same card PNG never
    duplicates the asset. Returns the ``asset_id`` (the SHA-256 hex), which
    callers record on the definition (``Persona``/``World`` ``image_asset``).
    """
    digest = sha256_hex(data)
    if find_asset(log, MEDIA_ANCHOR, digest) is None:
        store_image(log, MEDIA_ANCHOR, data, mime, note=note)
    return digest


def find_definition_asset(log: "EventLog", asset_ref: str) -> dict | None:
    """Find a sessionless definition image by id or row (anchor scope)."""
    return find_asset(log, MEDIA_ANCHOR, asset_ref)


def image_content_part(asset: dict) -> dict[str, Any]:
    """OpenAI multimodal image content part for one projected asset item.

    Exposed for the documented VLM-narrator seam (not wired into the narrator).
    ``asset`` is a projection item (``{"payload": {...}}``) or a raw payload.
    """
    payload = asset.get("payload", asset)
    mime = payload.get("mime") or "application/octet-stream"
    b64 = payload.get("bytes_b64") or ""
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}"},
    }


def build_caption_messages(asset: dict) -> list[dict[str, Any]]:
    """Captioner chat messages: system instruction + image content part."""
    return [
        {"role": "system", "content": CAPTION_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": CAPTION_INSTRUCTION},
                image_content_part(asset),
            ],
        },
    ]


async def caption(
    log: "EventLog",
    session_id: str,
    asset_ref: str,
    captioner: ModelCaller,
    messages_builder: Any | None = None,
) -> "Event":
    """Caption one stored image; append + return the ``image_caption`` event.

    ``captioner`` is the injected :class:`~fabulist.graph.types.ModelCaller`
    (the registry's ``captioner`` agent in production, a fake in tests). Raises
    :class:`KeyError` when ``asset_ref`` names no asset in the session.
    """
    asset = find_asset(log, session_id, asset_ref)
    if asset is None:
        raise KeyError(f"unknown asset {asset_ref!r} in session {session_id!r}")
    payload = asset.get("payload", {})
    resolved_ref = payload.get("asset_id") or asset_ref
    # Issue #70: the preset flow builder is session-aware, so a swallowed
    # preset read leaves its ``preset_degraded`` row on the upload's session.
    # A builder with the historical one-argument shape still works.
    if messages_builder is not None:
        try:
            messages = messages_builder(asset, session_id)
        except TypeError:
            messages = messages_builder(asset)
    else:
        messages = build_caption_messages(asset)
    text = (await collect_text(captioner, messages)).strip()
    event_payload = {"event": CAPTION_EVENT, "asset": resolved_ref, "text": text}
    return log.append_event(session_id, "system", MEDIA_ACTOR, event_payload)


def build_image_turn_prefix(caption_text: str) -> str:
    """The ``[image: ...]`` prefix that carries a caption into a turn message.

    ``caption_text`` is the caption string (whitespace-trimmed); the returned
    prefix is prepended to the user content for a turn that should be aware of
    an attached image.
    """
    return f"[image: {caption_text.strip()}]"
