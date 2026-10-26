"""Per-session memory delete on both delete doors + the one-time orphan cleanup.

Issue #85 (option A). The RCA's repro: two world chats and one plain chat are
indexed, the chats are deleted, and the surviving chat's recall still returns
the deleted chat's rows from the shared layer. These tests pin the fix end to
end with a REAL :class:`MemoryStore`:

* the chat-delete endpoint removes the deleted chat's rows from a SHARED world
  collection and the sibling's rows survive;
* branch prune removes the pruned branch's rows;
* the surviving sibling's recall no longer returns the deleted chat's rows;
* a missing store / memory-off chat / failing store never fails the delete;
* the one-time sweep clears pre-existing orphans (hidden or absent), with a
  dry run that deletes nothing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.branches import router as branches_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.sessions import router as sessions_router
from fabulist.log import EventLog
from fabulist.memory.reembed import prune_orphan_sessions, visible_session_ids
from fabulist.memory.store import MemoryStore

DIM = 4
CLUSTER_A = [1.0, 0.0, 0.0, 0.0]
CLUSTER_B = [0.0, 1.0, 0.0, 0.0]
WORLD = "w_shared"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps over one tmp log, with both delete routers mounted."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(*, mount_sessions: bool = True) -> object:
        app = create_app(FakeTurnEngine(), log_path)
        if mount_sessions:
            app.include_router(sessions_router)
        app.include_router(branches_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create(client: httpx.AsyncClient, title: str = "") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def _attach_store(app, tmp_path: Path) -> MemoryStore:
    """Attach a real store through the production handle (memorizer.store)."""
    store = MemoryStore(tmp_path / "memory.db")
    app.state.memorizer = SimpleNamespace(store=store)
    return store


def _seed_world(store: MemoryStore, session_id: str, tag: str, seqs=range(1, 4)) -> None:
    for seq in seqs:
        store.upsert(
            WORLD,
            seq,
            "narrative",
            f"{tag}-{seq}",
            CLUSTER_A,
            session_id=session_id,
        )


def test_chat_delete_removes_rows_from_shared_world_and_spares_sibling(
    build_app, tmp_path
):
    build, _ = build_app
    app = build()
    store = _attach_store(app, tmp_path)
    try:

        async def scenario():
            async with client_for(app) as client:
                doomed = await _create(client, "Doomed")
                sibling = await _create(client, "Sibling")
                # Same per-chat seqs in the same shared collection (the D16
                # collision case): only the doomed chat's row keys may go.
                _seed_world(store, doomed, "A")
                _seed_world(store, sibling, "B")
                resp = await client.delete(f"/api/sessions/{doomed}")
                return doomed, sibling, resp

        doomed, sibling, resp = run(scenario())
        assert resp.status_code == 200
        assert resp.json() == {"session_id": doomed, "hidden": True}

        rows = store.query(WORLD, CLUSTER_A, k=100)
        texts = {r["text"] for r in rows}
        assert texts == {"B-1", "B-2", "B-3"}
        assert store.count_session_rows(doomed) == 0
        assert store.count_session_rows(sibling) == 3
    finally:
        store.close()


def test_branch_prune_removes_the_pruned_branch_rows(build_app, tmp_path):
    build, log_path = build_app
    app = build()
    store = _attach_store(app, tmp_path)
    try:

        async def scenario():
            async with client_for(app) as client:
                base = await _create(client, "Base")
                app.state.log.append_event(base, "user", "user", {"text": "enter"})
                branch = (
                    await client.post(
                        f"/api/sessions/{base}/fork",
                        json={"at_seq": 2, "new_session_id": "branch-85"},
                    )
                ).json()["session_id"]
                _seed_world(store, base, "base")
                _seed_world(store, branch, "branch")
                resp = await client.delete(f"/api/sessions/{branch}/branch")
                return base, branch, resp

        base, branch, resp = run(scenario())
        assert resp.status_code == 200
        assert resp.json()["pruned"] is True

        texts = {r["text"] for r in store.query(WORLD, CLUSTER_A, k=100)}
        assert texts == {"base-1", "base-2", "base-3"}
        assert store.count_session_rows(branch) == 0
        # The log's own prune is unchanged: no ghost session row either.
        log = EventLog(log_path)
        try:
            assert log.get_session(branch) is None
            assert log.get_session(base) is not None
        finally:
            log.close()
    finally:
        store.close()


def test_surviving_sibling_recall_no_longer_returns_the_deleted_chat(
    build_app, tmp_path
):
    """The RCA's four-of-six pollution case: after delete, recall returns zero."""
    build, _ = build_app
    app = build()
    store = _attach_store(app, tmp_path)
    try:

        async def scenario():
            async with client_for(app) as client:
                doomed = await _create(client, "Doomed")
                sibling = await _create(client, "Sibling")
                # Deleted chat owns the nearest rows (the RCA's pollution).
                _seed_world(store, doomed, "A", seqs=range(1, 7))
                store.upsert(
                    WORLD, 20, "narrative", "B-20", CLUSTER_B, session_id=sibling
                )
                before = store.query(WORLD, CLUSTER_A, k=6)
                await client.delete(f"/api/sessions/{doomed}")
                after = store.query(WORLD, CLUSTER_A, k=6)
                return doomed, sibling, before, after

        doomed, sibling, before, after = run(scenario())
        assert {r["session_id"] for r in before} == {doomed}
        assert after  # the sibling's row is still recallable
        assert {r["session_id"] for r in after} == {sibling}
        assert len(after) == 1  # zero of the six deleted rows come back
    finally:
        store.close()


def test_delete_succeeds_when_store_is_absent(build_app, tmp_path):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, "No memory")
            return sid, await client.delete(f"/api/sessions/{sid}")

    sid, resp = run(scenario())
    assert not hasattr(app.state, "memorizer") or app.state.memorizer is None
    assert resp.status_code == 200
    log = EventLog(log_path)
    try:
        assert log.get_session(sid) is not None  # the hide still appended
    finally:
        log.close()


def test_delete_succeeds_when_chat_memory_is_off(build_app, tmp_path):
    build, _ = build_app
    app = build()
    store = _attach_store(app, tmp_path)
    try:

        async def scenario():
            async with client_for(app) as client:
                sid = await _create(client, "Memory off")
                app.state.log.append_event(
                    sid, "system", "user", {"event": "chat_memory", "enabled": False}
                )
                # A memory-off chat wrote no rows; cleanup is still a no-op 0.
                resp = await client.delete(f"/api/sessions/{sid}")
                return sid, resp

        sid, resp = run(scenario())
        assert resp.status_code == 200
        assert store.count_session_rows(sid) == 0
    finally:
        store.close()


def test_delete_succeeds_and_warns_when_memory_cleanup_fails(build_app, caplog):
    build, _ = build_app
    app = build()

    class _BrokenStore:
        def delete_session_rows(self, session_id: str) -> int:
            raise RuntimeError("index is locked")

    app.state.memorizer = SimpleNamespace(store=_BrokenStore())

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, "Broken index")
            return sid, await client.delete(f"/api/sessions/{sid}")

    with caplog.at_level(logging.WARNING, logger="fabulist.api.memory_cleanup"):
        sid, resp = run(scenario())
    assert resp.status_code == 200
    assert "memory cleanup for session" in caplog.text


def test_prune_orphans_clears_hidden_and_absent_sessions_only(tmp_path):
    log = EventLog(tmp_path / "events.db")
    store = MemoryStore(tmp_path / "memory.db")
    try:
        # live: present and visible
        log.append_event("live", "user", "user", {"text": "hello"})
        log.append_event("live", "narrative", "narrator", {"prose": "once"})
        # hidden: present in the log but soft-hidden
        log.append_event("hidden", "user", "user", {"text": "gone"})
        log.append_event("hidden", "system", "system", {"event": "session_hidden"})
        # absent: rows in the index, no log session at all (pruned pre-#85)
        assert visible_session_ids(log) == ["live"]

        _seed_world(store, "live", "live")
        _seed_world(store, "hidden", "hidden")
        _seed_world(store, "absent", "absent")

        dry = prune_orphan_sessions(log, store, dry_run=True)
        assert dry["dry_run"] is True
        assert dry["sessions"] == 2
        assert dry["rows"] == 6
        assert set(dry["pruned"]) == {"hidden", "absent"}
        # A dry run never deletes.
        assert store.count_session_rows("hidden") == 3

        stats = prune_orphan_sessions(log, store)
        assert stats["sessions"] == 2
        assert stats["rows"] == 6
        assert set(stats["pruned"]) == {"hidden", "absent"}

        assert store.session_ids() == ["live"]
        assert {r["text"] for r in store.query(WORLD, CLUSTER_A, k=100)} == {
            "live-1",
            "live-2",
            "live-3",
        }

        # Repeatable: nothing left to prune.
        again = prune_orphan_sessions(log, store)
        assert again == {"sessions": 0, "rows": 0, "pruned": [], "dry_run": False}
    finally:
        store.close()
        log.close()
