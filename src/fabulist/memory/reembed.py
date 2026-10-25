"""Live embedder resolution + full re-embed enumeration/batch (D25/D5).

Two jobs live here, both about *not* trusting a boot-time snapshot:

* **Live resolution** — :func:`resolve_embedder` / :func:`resolve_reranker`
  read the endpoint/model/api-key off the *live* registry on every call. The
  registry may be a hot-reloadable :class:`~fabulist.connections.registry.RegistryStore`,
  so a Connections rebind applies to the next call with no restart. The API key
  is resolved from ``os.environ`` at call time (a configured ``api_key_env``
  used to be silently ignored).
* **Re-embed enumeration + batch** — :func:`visible_session_ids` /
  :func:`planned_sessions` / :func:`reembed_all` absorb the enumeration logic
  that used to live only in ``scripts/reindex_memory.py`` so the API's
  background re-embed job and the CLI script share one implementation.

The index is derived and rebuildable (D5): re-embedding is seq-keyed and
therefore idempotent, so re-running the job over the same log is safe. Opted-out
chats (``chat_memory`` off) are skipped — their index stays empty (D53). Each
session's rows land in its RESOLVED write collection (D53 layer model:
``w_<world_id>`` / ``a_<persona_id>`` / ``c_<session_id>``), taken straight from
the scope so the batch never recomputes a collection id.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from .indexer import index_media, index_turn
from .scope import MemoryScope, resolve_scope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "EmbedderTarget",
    "planned_sessions",
    "reembed_all",
    "resolve_embedder",
    "resolve_reranker",
    "visible_session_ids",
]

#: System-event kind a soft-delete appends (mirrors ``api.sessions``).
_HIDDEN_EVENT = "session_hidden"


@dataclass(frozen=True)
class EmbedderTarget:
    """One resolved embeddings/rerank endpoint: ``(endpoint, model, api_key)``.

    ``endpoint`` is normalized (a trailing ``/v1`` is stripped because the
    clients append ``/v1/embeddings``); ``api_key`` is the live env value or
    ``None`` when the connection names no key / the env var is unset.
    """

    endpoint: str
    model: str
    api_key: str | None = None


def _split_v1(base_url: str) -> str:
    """Strip a trailing ``/v1`` (clients re-append it) and trailing slashes."""
    endpoint = (base_url or "").rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[: -len("/v1")]
    return endpoint


def _live_api_key(api_key_env: str | None) -> str | None:
    """Resolve the key from its env var NOW (never a boot-time snapshot)."""
    if not api_key_env:
        return None
    return os.environ.get(api_key_env)


def resolve_embedder(registry: Any) -> EmbedderTarget:
    """Resolve the LIVE embedder from the registry (raises if unbound).

    Called on every embed/recall/rebind-affected path so a hot PATCH takes
    effect immediately: the store is the index, but the endpoint/model/key are
    always current.
    """
    conn = registry.connection_for("embedder")
    return EmbedderTarget(
        endpoint=_split_v1(conn.base_url),
        model=conn.model,
        api_key=_live_api_key(conn.api_key_env),
    )


def resolve_reranker(registry: Any, *, fallback_endpoint: str) -> EmbedderTarget:
    """Resolve the LIVE reranker, falling back to the embedder host (D25 C3).

    No ``reranker`` agent -> the embedder endpoint + the default rerank model
    (the pre-existing live behavior). Rerank is stateless, so a rebind needs no
    destructive gate.
    """
    from .rerank import DEFAULT_RERANK_MODEL

    try:
        conn = registry.connection_for("reranker")
    except Exception:  # noqa: BLE001 - absent = fall back to the embedder host
        return EmbedderTarget(
            endpoint=fallback_endpoint, model=DEFAULT_RERANK_MODEL, api_key=None
        )
    return EmbedderTarget(
        endpoint=_split_v1(conn.base_url),
        model=conn.model,
        api_key=_live_api_key(conn.api_key_env),
    )


def _all_session_ids(log: "EventLog") -> list[str]:
    """Every session id in the log, minus reserved ``__``-anchor sessions.

    ``EventLog`` exposes no public enumerator, so read the fork-bookkeeping
    ``sessions`` table the way ``api.sessions._session_ids`` does (short-lived
    secondary connection; WAL makes the concurrent read safe).
    """
    conn = sqlite3.connect(log.path)
    try:
        rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [str(r[0]) for r in rows if not str(r[0]).startswith("__")]


def visible_session_ids(log: "EventLog") -> list[str]:
    """Visible sessions only: anchor-free AND not soft-hidden.

    A soft-delete appends a ``session_hidden`` system event (D9: nothing is
    destroyed). Hidden sessions are excluded here so a re-embed never resurrects
    an index row for a chat the user deleted.
    """
    visible: list[str] = []
    for session_id in _all_session_ids(log):
        events = log.get_events(session_id)
        if not events:
            continue  # a fork row before its first append — nothing to embed
        hidden = any(
            event.type == "system"
            and isinstance(event.payload, dict)
            and event.payload.get("event") == _HIDDEN_EVENT
            for event in events
        )
        if hidden:
            continue
        visible.append(session_id)
    return visible


def planned_sessions(log: "EventLog") -> list[tuple[str, MemoryScope]]:
    """Visible sessions WITH memory enabled, paired with their resolved scope.

    Respects the per-chat opt-out (``chat_memory`` off) and the D53 layer model:
    each entry carries the :class:`MemoryScope` whose ``write_collection`` is the
    chat's OWN layer — ``w_<world_id>`` (world chat), ``a_<persona_id>``
    (single-actor chat) or ``c_<session_id>`` (plain chat).
    """
    planned: list[tuple[str, MemoryScope]] = []
    for session_id in visible_session_ids(log):
        scope = resolve_scope(log, session_id)
        if scope.enabled:
            planned.append((session_id, scope))
    return planned


async def reembed_all(
    log: "EventLog",
    store: Any,
    target: EmbedderTarget,
    *,
    progress: Callable[[int, int], None] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-embed every planned session's events into its resolved collection.

    ``progress(done, total)`` fires after every session (``done`` counts
    sessions processed, ``total`` is the planned session count) so a caller can
    surface a status line. ``dry_run`` enumerates and counts without embedding.

    Idempotent (upserts are seq-keyed, D5): re-running re-embeds cleanly. Per-row
    failures are collected, never raised, so one bad row cannot abort the batch.

    A rebuild REPLACES each target layer rather than overwriting rows inside it:
    the planned collections are dropped first. Upserts alone would leave rows
    whose event no longer qualifies forever — a card-import seed the indexer
    refuses, or any line a tombstoned/hidden session pooled into a shared
    world/actor layer, which stays recallable from the visible chats reading
    that layer. ``dry_run`` never drops.
    Returns ``{"sessions", "embedded", "media", "errors", "dropped"}``.
    """
    planned = planned_sessions(log)
    total = len(planned)
    stats: dict[str, Any] = {
        "sessions": 0,
        "embedded": 0,
        "media": 0,
        "errors": [],
        "dropped": [],
    }
    if not dry_run:
        for collection in sorted({scope.write_collection for _, scope in planned}):
            store.drop_collection(collection)
            stats["dropped"].append(collection)
    if progress is not None:
        progress(0, total)
    for done, (session_id, scope) in enumerate(planned, 1):
        # D53 write rule: the chat's OWN layer collection, taken from the scope.
        collection = scope.write_collection
        for event in log.get_events(session_id):
            if getattr(event, "ephemeral", False):
                continue
            try:
                if dry_run:
                    if event.type in ("user", "narrative", "asset"):
                        stats["embedded"] += 1
                    continue
                if event.type == "user" and event.payload.get("text"):
                    await index_turn(
                        store,
                        target.endpoint,
                        target.model,
                        collection,
                        event.seq,
                        "user",
                        str(event.payload["text"]),
                        session_id=session_id,
                        api_key=target.api_key,
                        log=log,
                    )
                    stats["embedded"] += 1
                elif event.type == "narrative" and event.payload.get("prose"):
                    await index_turn(
                        store,
                        target.endpoint,
                        target.model,
                        collection,
                        event.seq,
                        "narrative",
                        str(event.payload["prose"]),
                        session_id=session_id,
                        api_key=target.api_key,
                    )
                    stats["embedded"] += 1
                elif event.type == "asset" and event.payload.get("bytes_b64"):
                    await index_media(
                        store,
                        target.endpoint,
                        target.model,
                        collection,
                        event.seq,
                        str(event.payload.get("kind", "image")),
                        str(event.payload.get("mime", "")),
                        str(event.payload["bytes_b64"]),
                        session_id=session_id,
                        api_key=target.api_key,
                    )
                    stats["media"] += 1
            except Exception as exc:  # noqa: BLE001 - collect and keep going
                stats["errors"].append(f"{session_id}#{event.seq}: {exc}")
        stats["sessions"] += 1
        if progress is not None:
            progress(done, total)
    return stats
