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


# -- fork views (issue #90): where does a branch edit's re-embed land? --------
#
# A fork view can now edit an INHERITED event. ``schedule_reindex`` receives the
# target's ORIGINAL (parent) seq, while ``resolve_scope`` resolves the FORK's own
# write layer. The store keys rows by ``(session_id, seq)``, so the write lands as
# the fork's own row at the inherited seq:
#
#   * the parent's row for the same event is NEVER rewritten or deleted — the
#     edit is branch-local and the parent's own view keeps its text (D10/D53);
#   * the row is written at the fork's RESOLVED write collection, exactly where
#     ``index_tail`` puts the branch's own turns, so recall from the branch reads
#     the revised text;
#   * nothing is silently skipped: a chat whose memory is off is the only case
#     that writes nothing, and that opt-out is itself chain-inherited.
#
# These tests pin all three against a real store over a faked embedder.


def _index_rows(collection: str, log_path: Path) -> list[dict]:
    """Every row of ``collection`` as ``{session_id, seq, kind, text}``."""
    with MemoryStore(_memory_db(log_path)) as store:
        return [
            {
                "session_id": r["session_id"],
                "seq": int(r["seq"]),
                "kind": r["kind"],
                "text": r["text"],
            }
            for r in store.query(collection, VEC, k=10)
        ]


def _collections(log_path: Path) -> list[str]:
    with MemoryStore(_memory_db(log_path)) as store:
        return store.collections()


def _fork_parent(log, *, world: bool):
    """``p1`` with a user row, plus ``b1`` cut ABOVE it (the row is inherited)."""
    if world:
        log.append_event(
            "p1", "system", "system", {"event": "world_ref", "world_id": "w1", "name": "W"}
        )
    user = log.append_event("p1", "user", "user", {"text": "original text"})
    log.fork_session("p1", at_seq=log.get_events("p1")[-1].seq, new_session_id="b1")
    return user


def _edit_from_fork(app, target_id: int, new_text: str) -> httpx.Response:
    async def scenario():
        async with _client(app) as client:
            resp = await client.post(
                "/api/sessions/b1/revisions",
                json={"target_id": target_id, "new_text": new_text},
            )
            # The reindex is fire-and-forget; drain the handler's strong refs.
            await asyncio.gather(*list(revisions._REINDEX_TASKS))
            return resp

    return run(scenario())


def test_fork_edit_reembeds_the_forks_own_row_in_a_shared_world_layer(
    app_and_log, fake_embedder
):
    """World layer: parent and fork share ONE collection — keys keep them apart."""
    from fabulist.memory.scope import collection_for_world

    app, log, log_path = app_and_log
    user = _fork_parent(log, world=True)
    world = collection_for_world("w1")
    # The parent's row, as a live ``index_tail`` would have written it.
    with MemoryStore(_memory_db(log_path)) as store:
        store.upsert(world, user.seq, "user", "original text", VEC, session_id="p1")

    resp = _edit_from_fork(app, user.id, "revised text")

    assert resp.status_code == 200
    # The re-embed carries the REVISED text (not a skip).
    assert fake_embedder["body"]["input"] == ["title: none | text: revised text"]
    # One write, at the fork's resolved layer (never a chat-local collection).
    assert _collections(log_path) == [world]

    rows = _index_rows(world, log_path)
    by_session = {(r["session_id"], r["seq"]): r for r in rows}
    # The fork's own row holds the revision at the INHERITED seq.
    assert by_session[("b1", user.seq)]["text"] == "revised text"
    assert by_session[("b1", user.seq)]["kind"] == "user"
    # The parent's row for the SAME event is untouched (branch-local edit).
    assert by_session[("p1", user.seq)]["text"] == "original text"
    assert len(rows) == 2


def test_fork_edit_reembeds_into_the_forks_own_layer(app_and_log, fake_embedder):
    """Chat layer: the branch writes ``c_<fork>``, never the parent's ``c_<parent>``."""
    app, log, log_path = app_and_log
    user = _fork_parent(log, world=False)
    parent_layer = collection_for("p1")
    with MemoryStore(_memory_db(log_path)) as store:
        store.upsert(
            parent_layer, user.seq, "user", "original text", VEC, session_id="p1"
        )

    resp = _edit_from_fork(app, user.id, "revised text")

    assert resp.status_code == 200
    assert fake_embedder["body"]["input"] == ["title: none | text: revised text"]
    fork_layer = collection_for("b1")
    assert fork_layer != parent_layer
    assert sorted(_collections(log_path)) == sorted([parent_layer, fork_layer])

    # The parent's layer still holds exactly its own original row.
    assert _index_rows(parent_layer, log_path) == [
        {
            "session_id": "p1",
            "seq": user.seq,
            "kind": "user",
            "text": "original text",
        }
    ]
    # The fork's layer holds the revision at the inherited seq.
    assert _index_rows(fork_layer, log_path) == [
        {
            "session_id": "b1",
            "seq": user.seq,
            "kind": "user",
            "text": "revised text",
        }
    ]


def test_fork_edit_honors_the_inherited_memory_opt_out(app_and_log, fake_embedder):
    """A branch inherits a ``chat_memory: false`` and writes nothing (D53)."""
    app, log, log_path = app_and_log
    # The opt-out sits BELOW the cut, so the branch inherits it.
    log.append_event("p1", "system", "user", {"event": "chat_memory", "enabled": False})
    user = log.append_event("p1", "user", "user", {"text": "original text"})
    log.fork_session("p1", at_seq=log.get_events("p1")[-1].seq, new_session_id="b1")

    resp = _edit_from_fork(app, user.id, "revised text")

    assert resp.status_code == 200
    # The revision still appended (the log is the source of truth)…
    assert [e for e in log.get_events("b1") if e.type == "revision"]
    # …but the derived index was never touched, and no embed call was made.
    assert "body" not in fake_embedder
    assert _collections(log_path) == []
