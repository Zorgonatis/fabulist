"""Memory indexer (W3.3) — format, embed and (re)build the derived vector index.

The event log owns all data (D9); the sqlite-vec index (:mod:`fabulist.memory`)
is derived state that can be wiped and rebuilt at any time (D5). This module is
the one place that knows *how* a log event becomes an embedding:

* **Asymmetric retrieval** (EmbeddingGemma 2). Documents are embedded with no
  task prefix as ``"title: {title} | text: {content}"``; queries are embedded
  with ``"task: search result | query: {q}"``. Media is embedded *unprefixed*
  as an OpenAI multimodal content part (plain bytes, no title/query wrapper).
* **Wire transport**. :func:`embed_inputs` is a sibling of
  :func:`fabulist.memory.embedder.embed_texts` that posts its ``input`` list
  verbatim, so it can carry either plain strings or ``{"content": [...]}``
  multimodal items. ``embedder.embed_texts`` stays string-only.
* **Rebuild** (:func:`rebuild`). Enumerates every session in the log and
  re-indexes its ``user`` / ``narrative`` / ``asset`` events at their seqs.
  The card-import seed is a synthetic ``user`` event, not a turn, and is never
  indexed (:func:`is_import_seed_text`). The caller performs the wipe; this
  module never drops data.

Media content-part shapes (verified live against the embedding service)::

    image -> {"type": "image_url",   "image_url":   {"url": "data:<mime>;base64,..."}}
    audio -> {"type": "input_audio", "input_audio": {"data": "<b64>", "format": "wav"}}
    video -> {"type": "input_video", "input_video": {"url": "data:<mime>;base64,..."}}
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

import httpx

from fabulist.cards.v2 import IMPORT_SEED_PREFIX

from .embedder import EmbedderError, record_embed_usage
from .store import MemoryStore

__all__ = [
    "INDEX_SKIPPED",
    "MAX_COLLECTION_CHARS",
    "MAX_DOC_CHARS",
    "IndexSkipped",
    "IndexerError",
    "collection_for",
    "doc_text",
    "embed_inputs",
    "index_media",
    "index_turn",
    "is_import_seed_text",
    "query_text",
    "recall",
    "rebuild",
    "reindex_revision",
    "resolve_writer",
]

#: Character budget for one indexed text before the document prefix is applied.
MAX_DOC_CHARS = 6000

#: Longest collection id handed to the store (matches the store's own slug cap).
MAX_COLLECTION_CHARS = 64


class IndexerError(RuntimeError):
    """Raised when an input cannot be indexed.

    Currently wraps an embedder failure (:class:`EmbedderError`) for media, and
    signals an unsupported media mime family for :func:`index_media`.
    """


class IndexSkipped:
    """Singleton sentinel type returned when a chat has memory opted out (D16).

    Distinct from ``None`` so callers can tell "I embedded this" from "this chat
    never embeds": the ``index_*`` helpers return ``None`` on the normal path and
    :data:`INDEX_SKIPPED` when ``memory_enabled`` is ``False``. It is falsy, so
    boolean-style checks read naturally.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "INDEX_SKIPPED"

    def __bool__(self) -> bool:
        return False


#: Sentinel returned by the indexing helpers when a chat has memory disabled:
#: nothing was embedded and the event log itself is unchanged (D16 opt-out).
INDEX_SKIPPED = IndexSkipped()


# -- pure formatting -------------------------------------------------------


def doc_text(text: str, title: str = "none") -> str:
    """Return the document-side embedding string (no task prefix).

    EmbeddingGemma 2 asymmetric retrieval embeds *documents* as
    ``"title: {title} | text: {text}"``.
    """
    return f"title: {title} | text: {text}"


def query_text(q: str) -> str:
    """Return the query-side embedding string (task prefix)."""
    return f"task: search result | query: {q}"


def is_import_seed_text(text: object) -> bool:
    """True when ``text`` is the synthetic card-import marker, not a turn.

    A card import writes a ``user``-type event whose text is
    ``f"[Imported card: {name}]"`` (:func:`fabulist.cards.to_session_seed`,
    whose prefix is :data:`fabulist.cards.v2.IMPORT_SEED_PREFIX`). It is a SEED,
    not a player turn — the card body already rides the stable cast/character
    prompt prefix — so it must never become a memory row. Indexed as ``user``,
    the narrator read the fabricated line back as the player's own words (the
    defect this guards). The event stays in the log (D9); only the DERIVED index
    omits it.
    """
    return str(text or "").startswith(IMPORT_SEED_PREFIX)


def collection_for(session_id: str) -> str:
    """Return the chat-LAYER collection id for one session (D53).

    The id is ``"c_" + session_id`` with every run of non-alphanumeric
    characters folded to a single ``_``, capped at :data:`MAX_COLLECTION_CHARS`.
    Under D53 the chat layer (``c_``) is the fallback when a chat belongs to no
    world and carries no single actor; world (``w_``) and actor (``a_``) layer
    ids are resolved by :func:`fabulist.memory.scope.resolve_scope`.
    """
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", f"c_{session_id}")
    return slug[:MAX_COLLECTION_CHARS]


# -- writer resolution -----------------------------------------------------


def resolve_writer(log: Any, session_id: str, seq: int) -> str:
    """The persona NAME that authored a ``user`` row at ``seq`` (``""`` if none).

    The memory index pools rows from MANY chats into shared persona/actor
    collections (D53 ``a_<persona_id>`` / ``w_<world_id>``), so a row's
    ``session_id`` alone cannot say *which persona* the player spoke as. This
    stamp preserves that identity even after the rows are pooled.

    Resolution is fork-aware and bounded to ``seq`` so each row is stamped with
    the persona in effect at ITS OWN position in the log (not a later edit):

    * Fold the session's inherited ``chat_persona`` system events with
      ``seq <= seq`` (:meth:`EventLog.get_chain_events`) latest-wins — a branch
      inherits its ancestor's assignment/clear and its own branch-local row
      still wins.
    * An **explicit clear** (``{"cleared": true}`` or a bare
      ``persona_id: null``) is authoritative: the writer is ``""`` and the
      pinned default is NOT inherited.
    * A chat that never recorded a choice falls back to the pinned global
      default (:func:`fabulist.state.settings.default_persona_id`).
    * The resolved ``persona_id`` is looked up to its display ``name`` via
      :func:`fabulist.personas.registry.list_personas`; an unknown or since-
      deleted id resolves to ``""``.

    Returns ``""`` when there is genuinely no persona (explicit clear, no
    assignment and no pin, or a dangling id). ``log`` may be any object exposing
    ``get_chain_events``.
    """
    from fabulist.personas.registry import (
        CHAT_PERSONA_CLEARED_KEY,
        CHAT_PERSONA_EVENT,
        list_personas,
    )
    from fabulist.state.settings import default_persona_id

    explicit = False
    persona_id: str | None = None
    for event in log.get_chain_events(session_id):
        if event.seq > seq:
            break
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != CHAT_PERSONA_EVENT:
            continue
        if payload.get(CHAT_PERSONA_CLEARED_KEY) is True:
            explicit, persona_id = True, None
            continue
        if "persona_id" not in payload:
            continue
        value = payload.get("persona_id")
        if isinstance(value, str) and value:
            explicit, persona_id = True, value
        else:
            # A bare ``persona_id: null`` (legacy clear row) is still an
            # explicit "no persona", not "never set".
            explicit, persona_id = True, None

    if not explicit:
        persona_id = default_persona_id(log)
    if not persona_id:
        return ""
    for persona in list_personas(log):
        if persona.persona_id == persona_id:
            return persona.name
    return ""


# -- transport -------------------------------------------------------------


def _usage_texts(inputs: list[Any]) -> list[str]:
    """Inputs as strings for usage accounting (a multimodal item counts its JSON size)."""
    return [
        item if isinstance(item, str) else json.dumps(item, default=str)
        for item in inputs
    ]


async def embed_inputs(
    endpoint: str,
    model: str,
    inputs: list[Any],
    api_key: str | None = None,
    timeout_s: float = 120.0,
) -> list[list[float]]:
    """POST ``inputs`` verbatim to ``{endpoint}/v1/embeddings``.

    Unlike :func:`fabulist.memory.embedder.embed_texts`, each element of
    ``inputs`` is passed through untouched: it may be a plain string *or* an
    OpenAI-style ``{"content": [...]}`` multimodal item. Returns one vector per
    input in the response's ``index`` order. A non-200 response raises
    :class:`EmbedderError` carrying the status code and raw body.
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
        # W1f: the per-turn transport (recall query + indexing) records usage
        # on failure too — a dead embedder must be visible, never silent.
        record_embed_usage(
            model=model,
            texts=_usage_texts(inputs),
            latency_s=time.monotonic() - started,
            endpoint=url,
            error=EmbedderError(response.status_code, response.text, url),
        )
        raise EmbedderError(response.status_code, response.text, url)
    body = json.loads(response.text)
    data = body.get("data", [])
    tokens = (body.get("usage") or {}).get("prompt_tokens")
    record_embed_usage(
        model=model,
        texts=_usage_texts(inputs),
        latency_s=time.monotonic() - started,
        endpoint=url,
        prompt_tokens=tokens if isinstance(tokens, int) else None,
    )
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    return [list(item["embedding"]) for item in ordered]


# -- single-row indexing ---------------------------------------------------


async def index_turn(
    store: MemoryStore,
    endpoint: str,
    model: str,
    collection: str,
    seq: int,
    kind: str,
    text: str,
    *,
    memory_enabled: bool = True,
    session_id: str = "",
    api_key: str | None = None,
    log: Any | None = None,
) -> "IndexSkipped | None":
    """Embed one text event and upsert it into ``collection`` at ``seq``.

    ``text`` is truncated to :data:`MAX_DOC_CHARS` *before* the document prefix
    is applied; the truncated form is what the endpoint sees and what the store
    records for recall display.

    ``collection`` is the caller's **resolved** write collection (D53): callers
    resolve it once per turn via :func:`fabulist.memory.scope.resolve_scope`
    and pass its ``scope.write_collection`` — a world chat writes
    ``w_<world_id>``, an actor chat writes ``a_<persona_id>``, a plain chat
    writes the chat-local ``c_<session_id>`` (:func:`collection_for`). Memory is
    written at exactly ONE layer (never down a layer). When ``memory_enabled``
    is ``False`` this appends NOTHING and returns :data:`INDEX_SKIPPED` —
    opted-out chats never embed, and the event log is left unchanged (D9).
    Returns ``None`` when a row was embedded.

    ``api_key`` is the LIVE key resolved from the connection's ``api_key_env``
    at call time (D25); ``None`` sends no auth header (keyless local servers).

    ``log`` is the fork-aware event log; when supplied (and ``kind == "user"``)
    the row is stamped with :func:`resolve_writer`'s persona name so a pooled
    persona/actor collection keeps the writing persona's identity. Narrative /
    asset rows and log-less calls carry ``""``.
    """
    if not memory_enabled:
        return INDEX_SKIPPED
    if kind == "user" and is_import_seed_text(text):
        # The card-import seed rides as a synthetic ``user`` event but is not a
        # turn: never fabricate a ``[user]`` memory the narrator reads as the
        # player's own words. This one guard covers every index path that
        # funnels here — the per-turn tail (wiring), a full rebuild and the D52
        # re-embed batch — so an existing junk row disappears on re-embed.
        return INDEX_SKIPPED
    trimmed = text[:MAX_DOC_CHARS]
    writer = (
        resolve_writer(log, session_id, seq)
        if log is not None and kind == "user"
        else ""
    )
    vectors = await embed_inputs(endpoint, model, [doc_text(trimmed)], api_key=api_key)
    store.upsert(
        collection, seq, kind, trimmed, vectors[0], session_id=session_id, writer=writer
    )
    return None


async def reindex_revision(
    store: MemoryStore,
    endpoint: str,
    model: str,
    session_id: str,
    target_seq: int,
    kind: str,
    revised_text: str,
    *,
    memory_enabled: bool = True,
    collection: str | None = None,
    api_key: str | None = None,
    log: Any | None = None,
) -> "IndexSkipped | None":
    """Re-embed an edited event's text at its ORIGINAL ``target_seq``.

    Editing a ``user``/``narrative`` row appends a ``revision`` event that
    *supersedes* the target (D10) and the projection resolves it latest-wins;
    the target row itself is immutable. The derived index (D5) is keyed by
    seq, so the row at ``target_seq`` still holds the ORIGINAL text until a
    full rebuild. This upserts the CURRENT ``revised_text`` at ``target_seq``,
    so semantic recall/search stop returning stale prose — the row at
    ``target_seq`` now reflects the CURRENT text.

    A thin wrapper over :func:`index_turn`: identical document prefix,
    truncation and upsert semantics, only parameterized by ``session_id`` and
    the original seq. Re-running it is idempotent.

    ``memory_enabled`` mirrors :func:`index_turn` (D16): the caller resolves the
    chat's scope with :func:`fabulist.memory.scope.resolve_scope` and passes
    its ``enabled`` flag; when ``False`` this appends NOTHING and returns
    :data:`INDEX_SKIPPED`.

    ``collection`` (D53 write rule): the caller's RESOLVED write collection —
    pass ``scope.write_collection`` so the re-embed lands at the chat's OWN
    layer (``w_<world_id>`` / ``a_<persona_id>`` / ``c_<session_id>``), never
    down a layer. ``None`` keeps the chat-local
    ``collection_for(session_id)`` derivation.
    """
    return await index_turn(
        store,
        endpoint,
        model,
        collection if collection is not None else collection_for(session_id),
        target_seq,
        kind,
        revised_text,
        memory_enabled=memory_enabled,
        session_id=session_id,  # composite row key (D16): re-embeds don't
        # collide cross-chat inside a persona collection
        api_key=api_key,
        log=log,  # stamp the writer in effect at the ORIGINAL seq
    )


def _media_part(mime: str, data_b64: str) -> dict[str, Any]:
    """Build the OpenAI multimodal content part for one media mime family."""
    family = mime.split("/", 1)[0].lower()
    if family == "image":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data_b64}"},
        }
    if family == "audio":
        subtype = mime.split("/", 1)[1] if "/" in mime else mime
        return {
            "type": "input_audio",
            "input_audio": {"data": data_b64, "format": subtype},
        }
    if family == "video":
        return {
            "type": "input_video",
            "input_video": {"url": f"data:{mime};base64,{data_b64}"},
        }
    # A media part for an unsupported mime family: SKIP, not raise — the
    # index is DERIVED (D5); the log keeps the asset row regardless, and a
    # garbage/unembeddable mime (e.g. an octet-stream fuzz artifact) must not
    # kill the whole index_tail pass for its session on every turn.
    return None


def _prepare_b64(kind: str, data_b64: str) -> str:
    """Best-effort downscale/re-fps for embedding; original bytes on failure."""
    if kind not in ("video", "audio"):
        return data_b64
    import base64
    import logging

    from fabulist.media.prepare import (
        MediaPrepError,
        prepare_audio_for_embed,
        prepare_video_for_embed,
    )

    try:
        raw = base64.b64decode(data_b64)
        prepared = (
            prepare_video_for_embed(raw)
            if kind == "video"
            else prepare_audio_for_embed(raw)
        )
        return base64.b64encode(prepared).decode()
    except MediaPrepError:
        logging.getLogger(__name__).warning(
            "media prep failed for %s; embedding original bytes", kind, exc_info=True
        )
        return data_b64


async def index_media(
    store: MemoryStore,
    endpoint: str,
    model: str,
    collection: str,
    seq: int,
    kind: str,
    mime: str,
    data_b64: str,
    *,
    memory_enabled: bool = True,
    session_id: str = "",
    api_key: str | None = None,
) -> "IndexSkipped | None":
    """Embed one media asset *unprefixed* and upsert its display row.

    The content part is built per family (``image_url`` / ``input_audio`` /
    ``input_video``) and posted inside ``{"content": [part]}``. The stored
    display text is ``"[{kind} {mime}]"``. Embedder failures are wrapped in
    :class:`IndexerError`.

    Video/audio are PREPARED first (downscale/refps/budget transcode — the
    "downscale retranscribe" preprocessing): bounds tokens for any source
    length so the model's single-pass context (8192) is never the ceiling.
    Prep failure falls back to the ORIGINAL bytes (the service-side fps
    sampling may still fit); embed failure still raises.

    ``memory_enabled`` mirrors :func:`index_turn` (D53): the caller resolves the
    chat's scope with :func:`fabulist.memory.scope.resolve_scope` and passes
    its ``enabled`` flag; when ``False`` this appends NOTHING (no prep, no embed)
    and returns :data:`INDEX_SKIPPED`.
    """
    if not memory_enabled:
        return INDEX_SKIPPED
    data_b64 = _prepare_b64(kind, data_b64)
    part = _media_part(mime, data_b64)
    if part is None:
        # Unsupported media mime family: SKIP, not raise — the index is
        # DERIVED (D5); the log keeps the asset row regardless, and a garbage
        # or unembeddable mime (e.g. an octet-stream fuzz artifact) must not
        # kill the whole index_tail pass for its session on every turn.
        return INDEX_SKIPPED
    try:
        vectors = await embed_inputs(endpoint, model, [{"content": [part]}], api_key=api_key)
    except EmbedderError as exc:
        raise IndexerError(f"media embed failed for {kind} {mime}: {exc}") from exc
    store.upsert(collection, seq, kind, f"[{kind} {mime}]", vectors[0], session_id=session_id)
    return None


async def recall(
    store: MemoryStore,
    endpoint: str,
    model: str,
    collection: str,
    query: str,
    k: int = 6,
    exclude_seqs: set[int] | None = None,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Query-prefix embed ``query`` and return the store's top-``k`` rows."""
    vectors = await embed_inputs(endpoint, model, [query_text(query)], api_key=api_key)
    return store.query(collection, vectors[0], k=k, exclude_seqs=exclude_seqs)


# -- full rebuild (D5) -----------------------------------------------------


def _session_ids(log: Any) -> list[str]:
    """Enumerate sessions via the fork-bookkeeping table.

    ``EventLog`` exposes no public enumerator, so this mirrors
    ``fabulist.api.sessions._session_ids``: read the ``sessions`` table over a
    short-lived secondary connection (WAL makes this a safe concurrent read).
    Reserved ``__``-prefixed anchor sessions (settings/assistants) are internal
    bookkeeping and are skipped.
    """
    conn = sqlite3.connect(log.path)
    try:
        rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [row[0] for row in rows if not str(row[0]).startswith("__")]


def _resolve_scope(log: Any, session_id: str) -> Any:
    """Resolve a session's D53 scope, lazily importing the scope module.

    The lazy import avoids the ``scope <-> indexer`` import cycle (scope.py
    imports :func:`collection_for` / :data:`MAX_COLLECTION_CHARS` from here).
    Kept as a module-level seam so tests can inject layer scopes without the
    scope module's log projection.
    """
    from .scope import resolve_scope

    return resolve_scope(log, session_id)


async def rebuild(
    log: Any,
    store: MemoryStore,
    endpoint: str,
    model: str,
) -> dict[str, Any]:
    """Full D5 re-index of every session in ``log`` into ``store``.

    Indexes each ``user`` event (``payload["text"]``), ``narrative`` event
    (``payload["prose"]``) and ``asset`` event (embedded from its stored bytes)
    at its seq. Each session's rows land in its RESOLVED write collection (D53) —
    ``w_<world_id>`` / ``a_<persona_id>`` / ``c_<session_id>`` — taken from the
    scope, never recomputed here. Ephemeral rows (abandoned swipe branches) are
    skipped, as are reserved anchor sessions. The caller wipes the store; this
    function never drops data.

    Returns ``{"sessions", "text_rows", "media_rows", "errors"}``. Per-event
    failures are collected as strings, not raised, so one bad row cannot abort
    the whole rebuild.
    """
    sessions = _session_ids(log)
    text_rows = 0
    media_rows = 0
    errors: list[str] = []

    for session_id in sessions:
        collection = _resolve_scope(log, session_id).write_collection
        for event in log.get_events(session_id):
            if event.ephemeral:
                continue
            try:
                if event.type == "user":
                    text = event.payload.get("text")
                    if text and not is_import_seed_text(text):
                        await index_turn(
                            store,
                            endpoint,
                            model,
                            collection,
                            event.seq,
                            "user",
                            str(text),
                            session_id=session_id,
                            log=log,
                        )
                        text_rows += 1
                elif event.type == "narrative":
                    prose = event.payload.get("prose")
                    if prose:
                        await index_turn(
                            store,
                            endpoint,
                            model,
                            collection,
                            event.seq,
                            "narrative",
                            str(prose),
                            session_id=session_id,
                        )
                        text_rows += 1
                elif event.type == "asset":
                    mime = event.payload.get("mime") or ""
                    data_b64 = event.payload.get("bytes_b64") or ""
                    if mime and data_b64:
                        kind = str(event.payload.get("kind") or "asset")
                        await index_media(
                            store,
                            endpoint,
                            model,
                            collection,
                            event.seq,
                            kind,
                            mime,
                            data_b64,
                        )
                        media_rows += 1
            except Exception as exc:  # noqa: BLE001 - collect and keep going
                errors.append(f"{session_id}#{event.seq}: {exc}")

    return {
        "sessions": len(sessions),
        "text_rows": text_rows,
        "media_rows": media_rows,
        "errors": errors,
    }
