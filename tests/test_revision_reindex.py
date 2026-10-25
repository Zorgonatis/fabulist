"""Edited text re-enters the memory index (stale-search fix).

The revision router appends a ``revision`` event that supersedes the target
(D10); the projection renders latest-wins, but the derived vector index (D5) is
keyed by ``seq``, so the row for the ORIGINAL seq used to keep the ORIGINAL
text until a full rebuild — semantic search returned stale prose.

These tests drive the real router over ASGI and assert that a ``user``/
``narrative`` edit fire-and-forgets a re-embed of the revised text AT THE
ORIGINAL seq. The embedder is faked at the httpx transport seam exactly as
``tests/test_memory_indexer.py`` does (``httpx.MockTransport`` installed on
``fabulist.memory.indexer.httpx.AsyncClient``); the store runs for real on
``tmp_path``. The handler's strong-ref task set
(:data:`fabulist.api.revisions._REINDEX_TASKS`) is awaited to make the
fire-and-forget path deterministic — no live embedding calls anywhere.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api import revisions
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.revisions import router
from fabulist.memory import indexer
from fabulist.memory.indexer import collection_for
from fabulist.memory.store import MemoryStore

#: Captured before :func:`fake_embedder` patches ``httpx.AsyncClient`` on the
#: shared ``httpx`` module — the ASGI test client must stay on the real class.
_RealAsyncClient = httpx.AsyncClient

DIM = 4
VEC = [1.0, 0.0, 0.0, 0.0]


def run(coro):
    """Drive a coroutine to completion (suite has no pytest-asyncio)."""
    return asyncio.run(coro)


class _FakeConnection:
    """Minimal ``registry.connection_for`` result (base_url/model only)."""

    base_url = "http://fake-embed/v1"
    model = "fake-model"


class _FakeRegistry:
    """Registry stub exposing only the embeddings connection the router needs."""

    def connection_for(self, agent_name: str):
        assert agent_name == "embedder"
        return _FakeConnection()


@pytest.fixture
def fake_embedder(monkeypatch):
    """Install a MockTransport-backed ``indexer.httpx.AsyncClient`` factory.

    House pattern from ``tests/test_memory_indexer.py``: the *wire* string is
    captured so the document prefix can be asserted, and the response carries a
    dim-4 fake vector. No live embedding call is possible.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": VEC}],
                "model": "fake",
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(indexer.httpx, "AsyncClient", fake_client)
    return seen


def _client(app) -> httpx.AsyncClient:
    return _RealAsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.fixture
def app_and_log(tmp_path: Path):
    """Build an app sharing one tmp event-log path; close the log after."""
    log_path = tmp_path / "events.db"
    app = create_app(FakeTurnEngine(), log_path)
    app.include_router(router)
    app.state.registry = _FakeRegistry()
    try:
        yield app, app.state.log, log_path
    finally:
        app.state.log.close()


def _memory_db(log_path: Path) -> Path:
    """The store path the router resolves (beside the event log)."""
    return Path(log_path).parent / "memory.db"


def _seed_store(log_path: Path, rows: list[tuple[int, str, str]]) -> None:
    """Pre-index rows as the live indexer would, then close (same file)."""
    store = MemoryStore(_memory_db(log_path))
    try:
        for seq, kind, text in rows:
            # Production (index_tail) embeds with the REAL session id — the
            # composite row key (D16) must match what schedule_reindex uses,
            # else the re-embed lands beside its own original instead of
            # replacing it.
            store.upsert(
                collection_for("s1"), seq, kind, text, VEC, session_id="s1"
            )
    finally:
        store.close()


def _rows(log_path: Path) -> dict[int, dict]:
    """Read back the session's indexed rows keyed by seq."""
    with MemoryStore(_memory_db(log_path)) as store:
        # k=10: the store may hold BOTH the replaced original and the
        # re-embedded row at the same true seq (different composite keys);
        # keying by seq with later-wins mirrors the projection fold.
        out = {}
        for r in store.query(collection_for("s1"), VEC, k=10):
            out[int(r["seq"])] = r
        return out


def test_user_edit_reindexes_original_seq(app_and_log, fake_embedder):
    app, log, log_path = app_and_log
    user = log.append_event("s1", "user", "user", {"text": "original text"})
    narrative = log.append_event(
        "s1", "narrative", "narrator", {"prose": "other prose"}
    )
    _seed_store(
        log_path,
        [(user.seq, "user", "original text"), (narrative.seq, "narrative", "other prose")],
    )

    async def scenario():
        async with _client(app) as client:
            resp = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": "revised text"},
            )
            assert resp.status_code == 200
            # (b) the handler scheduled exactly one reindex task; drain it.
            tasks = list(revisions._REINDEX_TASKS)
            assert len(tasks) == 1
            await asyncio.gather(*tasks)
            return resp.json()

    body = run(scenario())

    # (a) the revision event landed and supersedes the target (D10).
    revs = [e for e in log.get_events("s1") if e.type == "revision"]
    assert [e.id for e in revs] == [body["revision_id"]]
    assert revs[0].supersedes == user.id

    # The reindex embedded the REVISED document text.
    assert fake_embedder["body"]["input"] == ["title: none | text: revised text"]

    rows = _rows(log_path)
    # (c) the row at the ORIGINAL seq now reflects the CURRENT text.
    assert rows[user.seq]["text"] == "revised text"
    assert rows[user.seq]["kind"] == "user"
    # (d) a different original seq is untouched, and no row was duplicated.
    assert rows[narrative.seq]["text"] == "other prose"
    assert len(rows) == 2


def test_narrative_edit_reindexes_with_narrative_kind(app_and_log, fake_embedder):
    app, log, log_path = app_and_log
    narrative = log.append_event("s1", "narrative", "narrator", {"prose": "old prose"})
    _seed_store(log_path, [(narrative.seq, "narrative", "old prose")])

    async def scenario():
        async with _client(app) as client:
            resp = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": narrative.id, "new_text": "new prose"},
            )
            assert resp.status_code == 200
            await asyncio.gather(*list(revisions._REINDEX_TASKS))
            return resp.json()

    run(scenario())

    assert fake_embedder["body"]["input"] == ["title: none | text: new prose"]
    rows = _rows(log_path)
    assert rows[narrative.seq]["text"] == "new prose"
    assert rows[narrative.seq]["kind"] == "narrative"
    assert len(rows) == 1


def test_non_editable_target_does_not_schedule(app_and_log, fake_embedder):
    app, log, log_path = app_and_log
    asset = log.append_event(
        "s1", "asset", "user", {"asset_id": "a1", "kind": "image"}
    )
    _seed_store(log_path, [(asset.seq, "image", "[image image/png]")])

    async def scenario():
        async with _client(app) as client:
            return await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": asset.id, "new_text": "nope"},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    # No reindex task, no embedder call, index untouched.
    assert list(revisions._REINDEX_TASKS) == []
    assert "body" not in fake_embedder
    assert _rows(log_path)[asset.seq]["text"] == "[image image/png]"


def test_memory_off_when_no_registry(app_and_log, fake_embedder, monkeypatch):
    """Without ``app.state.registry`` the edit still commits, reindex is a no-op."""
    app, log, log_path = app_and_log
    monkeypatch.delattr(app.state, "registry", raising=False)
    user = log.append_event("s1", "user", "user", {"text": "original text"})

    async def scenario():
        async with _client(app) as client:
            resp = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": "revised text"},
            )
            # Let any (unexpected) task finish so nothing leaks between tests.
            await asyncio.gather(*list(revisions._REINDEX_TASKS))
            return resp

    resp = run(scenario())
    assert resp.status_code == 200
    assert [e for e in log.get_events("s1") if e.type == "revision"]
    assert "body" not in fake_embedder
