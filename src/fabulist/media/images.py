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

Captioning at asset arrival
---------------------------
A vision-capable *captioner* agent (registry name ``captioner``) receives the
image as an OpenAI-style multimodal content part and returns a short
visible-detail caption. That caption is appended AT UPLOAD TIME as a ``system``
event ``{"event": "image_caption", "asset": <asset_id>, "text": <caption>}`` —
issue #79's arrival rule. A generated image needs no captioner: its stored
generation prompt IS its arrival text. No captioner call ever runs inside the
turn lock, and a legacy asset without a caption simply falls back to the text
its row already carries or a terse placeholder (no automatic backfill).

Per-connection rendering
------------------------
Whether image bytes ride a request is a property of the CONNECTION, not of the
row: :attr:`fabulist.connections.registry.Connection.vision` is resolved live
at message-build time by every consumer (narrator history, current-turn
attachment, each NPC lane, OOC and preview). A vision connection receives the
native OpenAI image part (:func:`image_content_part`); every other connection
receives the uniform :func:`build_image_turn_prefix` marker,
``[inline image caption: <text>]``. Nothing is baked into the log, so a rebind
changes rendering on the next turn with no migration.
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
    "IMAGE_CAPTION_PLACEHOLDER",
    "MEDIA_META_KEY",
    "MAX_IMAGE_BYTES",
    "MEDIA_ACTOR",
    "MEDIA_ANCHOR",
    "build_caption_messages",
    "build_image_turn_prefix",
    "caption",
    "connection_has_vision",
    "find_asset",
    "find_caption",
    "find_definition_asset",
    "image_content_part",
    "is_image_narrative",
    "render_image_history_message",
    "rerender_image_history",
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

#: Text used when an image row has no caption, prompt, text or note.
IMAGE_CAPTION_PLACEHOLDER = "no caption available"

#: Transient message metadata key carrying a history image's render inputs.
#: The key never reaches a provider: ``connections.client.build_request``
#: strips it at the one wire seam, and preview strips it before serializing.
MEDIA_META_KEY = "_fabulist_media"

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


def find_caption(log: "EventLog", session_id: str, asset_ref: str) -> str:
    """The latest arrival caption for ``asset_ref``, or ``""`` when none.

    Folds the append-only ``image_caption`` rows (later wins). A legacy asset
    with no caption row returns ``""`` — the caller then falls back to the
    text the row already carries (prompt/note) or the terse placeholder. This
    NEVER calls the captioner: captioning happens at arrival only (issue #79),
    so a turn build can never invoke a model inside the lock.
    """
    text = ""
    for event in log.get_events(session_id):
        if event.type != "system":
            continue
        if event.payload.get("event") != CAPTION_EVENT:
            continue
        if str(event.payload.get("asset") or "") == str(asset_ref):
            text = str(event.payload.get("text") or "")
    return text


def connection_has_vision(connection: Any | None) -> bool:
    """Whether ``connection`` accepts native image content parts (issue #79).

    Only an explicit ``True`` enables vision; ``None``/absent (the default) and
    ``False`` are TEXT-ONLY. One spelling for every consumer, so a narrator, a
    lane, OOC and the preview can never disagree about the gate.
    """
    return getattr(connection, "vision", None) is True


def build_image_turn_prefix(caption_text: str) -> str:
    """The uniform text fallback for an image on a text-only connection.

    ``caption_text`` is the already-computed arrival text (caption, imagegen
    prompt, legacy row text or note); an empty value renders the terse
    :data:`IMAGE_CAPTION_PLACEHOLDER`. Every image kind uses this ONE shape:
    ``[inline image caption: <text>]``.
    """
    text = caption_text.strip() or IMAGE_CAPTION_PLACEHOLDER
    return f"[inline image caption: {text}]"


def rerender_image_history(messages: list[Any], vision: bool) -> list[Any]:
    """Re-render a shared history list for a CONSUMER's vision capability.

    Issue #79: the NPC lanes share the narrator's built history, but each lane
    is bound to its own connection. Every image message carries its render
    inputs (:data:`MEDIA_META_KEY`), so the lanes re-render the SAME rows for
    THEIR connection instead of inheriting the narrator's shape. A list with no
    image message is returned untouched (identity preserved).
    """
    rendered: list[Any] = []
    changed = False
    for message in messages:
        meta = message.get(MEDIA_META_KEY) if isinstance(message, dict) else None
        if isinstance(meta, dict):
            rendered.append(render_image_history_message(meta, vision))
            changed = True
        else:
            rendered.append(message)
    return rendered if changed else messages


def render_image_history_message(meta: dict[str, Any], vision: bool) -> dict:
    """Render one image history row for a consumer's vision capability.

    ``meta`` is the row's render inputs (``url``/``kind``/``fallback``/
    ``visible``); ``vision=True`` yields the native multipart ASSISTANT message,
    otherwise the uniform caption marker. The returned message carries the
    transient :data:`MEDIA_META_KEY` so a per-consumer seam (an NPC lane bound
    differently from the narrator) can re-render the SAME shared history for
    ITS connection. :func:`fabulist.connections.client.build_request` strips
    that key at the wire.
    """
    kind = meta.get("kind") or "image"
    if vision:
        content: list[dict[str, Any]] | str = [
            {"type": "text", "text": "Now visible: " + str(meta.get("visible") or "")},
            (
                {"type": "input_video", "input_video": {"url": meta.get("url", "")}}
                if kind == "video"
                else {"type": "image_url", "image_url": {"url": meta.get("url", "")}}
            ),
        ]
    else:
        content = build_image_turn_prefix(str(meta.get("fallback") or ""))
    return {
        "role": "assistant",
        "content": content,
        MEDIA_META_KEY: dict(meta),
    }
