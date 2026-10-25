"""Tests for live embedder resolution + the re-embed batch (D25/D5).

Two seams under test, both about *not* trusting a boot-time snapshot:

* :func:`resolve_embedder` / :func:`resolve_reranker` read the endpoint, model
  and ``api_key_env``-resolved key off the live registry on every call — a
  rebind (or an env change) reflects on the NEXT call.
* :func:`visible_session_ids` / :func:`planned_sessions` / :func:`reembed_all`
  enumerate visible, memory-enabled chats (skipping soft-hidden + opted-out +
  anchor sessions) and re-embed them into their resolved D16 scope.

The embeddings endpoint is faked with ``httpx.MockTransport`` (the house seam);
the store is a recording fake so the COLLECTION each row lands in is asserted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.connections.registry import Connection, RegistryStore
from fabulist.log import EventLog
from fabulist.memory import indexer
from fabulist.memory import reembed as reembed_mod
from fabulist.memory.indexer import collection_for
from fabulist.memory.reembed import (
    EmbedderTarget,
    planned_sessions,
    reembed_all,
    resolve_embedder,
    resolve_reranker,
    visible_session_ids,
)
from fabulist.memory.rerank import DEFAULT_RERANK_MODEL
from fabulist.memory.scope import MemoryScope
from fabulist.memory.store import MemoryStore

DIM = 4
VEC = [1.0, 0.0, 0.0, 0.0]


def run(coro):
    return asyncio.run(coro)


def _make_scope(write_collection, *, layer="chat", enabled=True):
    """Build a scope carrying the D53 frozen field names.

    Uses the real :class:`MemoryScope` once the U3 sibling reshape has landed;
    otherwise falls back to a stand-in with those exact fields so the write
    target can be pinned independently of the scope lane.
    """
    fields = dict(
        layer=layer,
        write_collection=write_collection,
        read_collections=(write_collection,),
        world_id=None,
        actor_ids=(),
        user_persona_id=None,
        enabled=enabled,
    )
    try:
        return MemoryScope(**fields)
    except TypeError:
        from types import SimpleNamespace

        return SimpleNamespace(**fields)


def _scope_lane_landed() -> bool:
    """True once the D53 scope reshape (sibling lane) exposes write_collection."""
    return "write_collection" in getattr(MemoryScope, "__dataclass_fields__", {})


def _ok_body(vectors):
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": vec}
            for i, vec in enumerate(vectors)
        ],
    }


@pytest.fixture
def transport_factory(monkeypatch):
    """Install a MockTransport-backed factory + expose the recorded requests."""

    calls: list[dict] = []

    def install(handler=None):
        def _handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode())
            inputs = payload.get("input") or []
            calls.append(
                {
                    "url": str(request.url),
                    "model": payload.get("model"),
                    "auth": request.headers.get("authorization"),
                    "inputs": inputs,
                }
            )
            if handler is not None:
                return handler(request, payload)
            return httpx.Response(200, json=_ok_body([VEC] * len(inputs)))

        transport = httpx.MockTransport(_handler)
        real_client = httpx.AsyncClient

        def fake_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(indexer.httpx, "AsyncClient", fake_client)
        return calls

    return install


class RecordingStore:
    """Minimal recorder for the store methods ``reembed_all`` uses."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.dropped: list[str] = []

    def drop_collection(self, collection) -> None:
        self.dropped.append(collection)

    def upsert(
        self,
        collection,
        seq,
        kind,
        text,
        embedding,
        ts="",
        *,
        session_id="",
        writer="",
    ) -> None:
        self.rows.append(
            {
                "collection": collection,
                "seq": seq,
                "kind": kind,
                "text": text,
                "session_id": session_id,
                "writer": writer,
            }
        )


def _write_registry(path: Path, *, with_key: bool = True) -> Path:
    connection: dict = {"base_url": "http://localhost:4444/v1", "model": "your-model-id"}
    if with_key:
        connection["api_key_env"] = "REEMBED_TEST_KEY"
    data = {
        "connections": {"emb": connection, "emb2": {"base_url": "http://other:9/v1", "model": "m2"}},
        "agents": {"embedder": "emb"},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- live resolution -------------------------------------------------------


def test_resolve_embedder_reflects_rebind_on_next_call(tmp_path, monkeypatch):
    monkeypatch.delenv("REEMBED_TEST_KEY", raising=False)
    path = _write_registry(tmp_path / "connections.json")
    store = RegistryStore(path)

    first = resolve_embedder(store)
    assert first.endpoint == "http://localhost:4444"
    assert first.model == "your-model-id"
    assert first.api_key is None

    # Rebind the embedder agent to another connection and reload (hot path).
    data = store.raw()
    data["agents"]["embedder"] = "emb2"
    path.write_text(json.dumps(data), encoding="utf-8")
    store.reload()

    second = resolve_embedder(store)
    assert second.endpoint == "http://other:9"
    assert second.model == "m2"


def test_resolve_embedder_reads_api_key_env_at_call_time(tmp_path, monkeypatch):
    path = _write_registry(tmp_path / "connections.json")
    store = RegistryStore(path)

    monkeypatch.setenv("REEMBED_TEST_KEY", "first-secret")
    assert resolve_embedder(store).api_key == "first-secret"
    # The env value changes between calls; resolution is call-time, not cached.
    monkeypatch.setenv("REEMBED_TEST_KEY", "second-secret")
    assert resolve_embedder(store).api_key == "second-secret"
    monkeypatch.delenv("REEMBED_TEST_KEY")
    assert resolve_embedder(store).api_key is None


def test_resolve_reranker_falls_back_to_embedder_then_prefers_dedicated(tmp_path):
    path = _write_registry(tmp_path / "connections.json")
    store = RegistryStore(path)

    # No reranker agent -> embedder endpoint + the default rerank model.
    fallback = resolve_reranker(store, fallback_endpoint="http://localhost:4444")
    assert fallback.endpoint == "http://localhost:4444"
    assert fallback.model == DEFAULT_RERANK_MODEL

    # Bind a dedicated reranker; it wins.
    data = store.raw()
    data["connections"]["rer"] = {"base_url": "http://rr:4445/v1", "model": "qwen3-reranker"}
    data["agents"]["reranker"] = "rer"
    path.write_text(json.dumps(data), encoding="utf-8")
    store.reload()
    dedicated = resolve_reranker(store, fallback_endpoint="http://localhost:4444")
    assert dedicated.endpoint == "http://rr:4445"
    assert dedicated.model == "qwen3-reranker"


# -- enumeration -----------------------------------------------------------


@pytest.fixture
def log(tmp_path):
    repo = EventLog(tmp_path / "events.db")
    yield repo
    repo.close()


def _seed(log: EventLog, session_id: str, *, text: str = "hello") -> None:
    log.append_event(session_id, "user", "user", {"text": text})
    log.append_event(session_id, "narrative", "narrator", {"prose": "once upon"})


def test_visible_session_ids_skips_anchors_and_hidden(log):
    _seed(log, "s-visible")
    _seed(log, "__settings__")
    _seed(log, "s-hidden")
    log.append_event("s-hidden", "system", "system", {"event": "session_hidden"})

    assert visible_session_ids(log) == ["s-visible"]


def test_planned_sessions_respects_optout_and_layer_scope(log, monkeypatch):
    """Enumeration is unchanged (visible + memory-enabled); D53 layers ride through.

    The scope resolver is injected so this test pins the enumeration contract
    (soft-hidden excluded, ``chat_memory`` off filtered) and proves
    ``planned_sessions`` carries the scope's ``write_collection`` untouched.
    """
    _seed(log, "s-world")
    _seed(log, "s-actor")
    _seed(log, "s-optout")
    _seed(log, "s-hidden")
    log.append_event("s-optout", "system", "user", {"event": "chat_memory", "enabled": False})
    log.append_event("s-hidden", "system", "system", {"event": "session_hidden"})

    scopes = {
        "s-world": ("world", "w_w1"),
        "s-actor": ("actor", "a_alice"),
        "s-optout": ("chat", "c_s_optout"),
    }
    monkeypatch.setattr(
        reembed_mod,
        "resolve_scope",
        lambda lg, sid: _make_scope(
            scopes[sid][1], layer=scopes[sid][0], enabled=(sid != "s-optout")
        ),
    )

    planned = dict(planned_sessions(log))
    assert set(planned) == {"s-world", "s-actor"}  # hidden + opt-out excluded
    assert planned["s-world"].write_collection == "w_w1"
    assert planned["s-actor"].write_collection == "a_alice"


def test_reembed_all_writes_each_layer_collection_and_skips_optout(
    log, monkeypatch, transport_factory
):
    """A world chat writes ``w_*``, a single-actor chat ``a_*``, a plain chat
    ``c_*``; opted-out chats are never embedded."""
    transport_factory()
    _seed(log, "s-world", text="world memory")
    _seed(log, "s-actor", text="actor memory")
    _seed(log, "s-chat", text="chat memory")
    _seed(log, "s-optout", text="never embedded")
    log.append_event("s-optout", "system", "user", {"event": "chat_memory", "enabled": False})

    scopes = {
        "s-world": ("world", "w_w1"),
        "s-actor": ("actor", "a_alice"),
        "s-chat": ("chat", "c_s_chat"),
        "s-optout": ("chat", "c_s_optout"),
    }
    monkeypatch.setattr(
        reembed_mod,
        "resolve_scope",
        lambda lg, sid: _make_scope(
            scopes[sid][1], layer=scopes[sid][0], enabled=(sid != "s-optout")
        ),
    )

    store = RecordingStore()
    progress: list[tuple[int, int]] = []
    stats = run(
        reembed_all(
            log,
            store,
            EmbedderTarget("http://localhost:4444", "fake", None),
            progress=lambda done, total: progress.append((done, total)),
        )
    )

    assert stats["sessions"] == 3  # world + actor + chat; opt-out skipped
    assert stats["errors"] == []
    pairs = {(row["collection"], row["text"]) for row in store.rows}
    assert ("w_w1", "world memory") in pairs
    assert ("a_alice", "actor memory") in pairs
    assert ("c_s_chat", "chat memory") in pairs
    assert "never embedded" not in {row["text"] for row in store.rows}
    assert progress[0] == (0, 3)
    assert progress[-1] == (3, 3)


@pytest.mark.skipif(
    not _scope_lane_landed(),
    reason="D53 scope reshape (U3 sibling lane) not landed yet",
)
def test_reembed_all_resolves_real_layer_prefixes(log, transport_factory):
    """End-to-end: the REAL ``resolve_scope`` yields w_/a_/c_ per layer."""
    transport_factory()
    _seed(log, "s-world", text="world memory")
    _seed(log, "s-actor", text="actor memory")
    _seed(log, "s-chat", text="chat memory")
    log.append_event(
        "s-world", "system", "user", {"event": "world_ref", "world_id": "w1"}
    )
    log.append_event(
        "s-actor", "system", "user", {"event": "chat_actor", "actor_ids": ["alice"]}
    )

    store = RecordingStore()
    run(reembed_all(log, store, EmbedderTarget("http://h", "m")))
    by_text: dict[str, str] = {}
    for row in store.rows:
        by_text.setdefault(row["text"], row["collection"])
    assert by_text["world memory"].startswith("w_")
    assert by_text["actor memory"].startswith("a_")
    assert by_text["chat memory"].startswith("c_")


def test_reembed_all_second_run_is_a_noop(log, tmp_path, transport_factory):
    """The reset+re-embed rebuild IS the D53 migration path, so a second run
    over the same log must change nothing (seq-keyed upserts replace in place)."""
    transport_factory()
    _seed(log, "s-1", text="row")
    with MemoryStore(tmp_path / "mem.db") as store:
        target = EmbedderTarget("http://localhost:4444", "fake", None)
        first = run(reembed_all(log, store, target))
        before = store.query(collection_for("s-1"), VEC, k=10)
        second = run(reembed_all(log, store, target))
        after = store.query(collection_for("s-1"), VEC, k=10)

    assert first["embedded"] == second["embedded"] == 2
    snapshot = [(r["seq"], r["kind"], r["text"]) for r in before]
    assert snapshot == [(r["seq"], r["kind"], r["text"]) for r in after]
    assert len(after) == len(before) == 2  # no duplicate rows appended


def test_reembed_all_is_idempotent(log, transport_factory):
    transport_factory()
    _seed(log, "s-1", text="row")
    store = RecordingStore()
    target = EmbedderTarget("http://localhost:4444", "fake", None)
    first = run(reembed_all(log, store, target))
    after_first = len(store.rows)
    second = run(reembed_all(log, store, target))
    assert first["embedded"] == second["embedded"] == after_first == 2
    # Re-running upserts the same seqs (the store replaces, never duplicates).
    assert len(store.rows) == after_first * 2


def test_reembed_all_sends_live_api_key(log, transport_factory):
    calls = transport_factory()
    _seed(log, "s-1")
    store = RecordingStore()
    run(reembed_all(log, store, EmbedderTarget("http://h", "m", "sekret")))
    assert calls and all(c["auth"] == "Bearer sekret" for c in calls)


def test_reembed_all_dry_run_embeds_nothing(log, transport_factory):
    calls = transport_factory()
    _seed(log, "s-1")
    store = RecordingStore()
    stats = run(
        reembed_all(log, store, EmbedderTarget("http://h", "m"), dry_run=True)
    )
    assert store.rows == []
    assert calls == []
    assert stats["embedded"] == 2


# -- the rebuild REPLACES the layer, it does not merely overwrite rows ---------


def test_reembed_all_drops_each_target_layer_once_before_indexing(log, transport_factory):
    """A rebuild must clear the layer it repopulates.

    Upserts are seq-keyed, so a row whose event no longer qualifies — a
    card-import seed the indexer refuses, or a line pooled into a shared
    world/actor layer by a since-deleted session — would otherwise stay
    recallable forever. Drops happen once per distinct target collection, and
    never on a dry run.
    """
    transport_factory()
    _seed(log, "s-1", text="one")
    store = RecordingStore()
    target = EmbedderTarget("http://localhost:4444", "fake", None)

    stats = run(reembed_all(log, store, target))
    assert stats["dropped"] == [collection_for("s-1")]
    assert store.dropped == [collection_for("s-1")]

    store2 = RecordingStore()
    dry = run(reembed_all(log, store2, target, dry_run=True))
    assert store2.dropped == []
    assert dry["dropped"] == []


def test_reembed_all_leaves_no_stale_rows_in_a_shared_layer(log, tmp_path, transport_factory):
    """The live defect this closes: a hidden session's row in a shared layer."""
    transport_factory()
    _seed(log, "s-1", text="visible row")
    with MemoryStore(tmp_path / "mem.db") as store:
        col = collection_for("s-1")
        # A stale row from a session that is no longer visible (tombstoned).
        store.upsert(
            col, 999, "user", "stale row from a hidden chat", VEC, session_id="gone"
        )
        store.upsert(col, 2, "user", "[Imported card: Nope]", VEC, session_id="s-1")
        run(
            reembed_all(
                log, store, EmbedderTarget("http://localhost:4444", "fake", None)
            )
        )
        texts = {r["text"] for r in store.query(col, VEC, k=20)}

    assert "visible row" in texts
    assert "stale row from a hidden chat" not in texts
    assert "[Imported card: Nope]" not in texts
