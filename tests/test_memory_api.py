"""Tests for the memory lifecycle router (D25): reset + background re-embed.

Mount pattern mirrors the other API tests: build the app over a tmp event-log
path via :func:`create_app`, include this lane's router, install a real
``MemoryStore`` (sqlite-vec, tmp file) behind a fake memorizer, and a fake
registry resolving the embedder. The embedder HTTP seam is faked with
``httpx.MockTransport`` so no network is touched.

Contracts asserted: reset wipes every collection; collections reads empty after
a reset; the re-embed job transitions idle -> running -> done and enumerates
only visible, memory-enabled chats (opt-out respected); reset/reembed 503 when
memory or the embedder is unwired.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.memory_api import router
from fabulist.connections.registry import Connection, ConnectionConfigError
from fabulist.memory import indexer
from fabulist.memory.store import MemoryStore

DIM = 4
VEC = [1.0, 0.0, 0.0, 0.0]


def run(coro):
    return asyncio.run(coro)


class FakeMemorizer:
    """Exposes only the ``store`` the memory router reaches through."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store


class FakeRegistry:
    """Minimal registry: resolves ``embedder`` and nothing else."""

    def __init__(self, *, has_embedder: bool = True) -> None:
        self.has_embedder = has_embedder

    def connection_for(self, agent: str) -> Connection:
        if agent == "embedder" and self.has_embedder:
            return Connection(name="emb", base_url="http://localhost:4444/v1", model="fake")
        raise ConnectionConfigError(f"no agent {agent!r}")


@pytest.fixture
def embed_transport(monkeypatch):
    """Fake the embeddings transport (returns dim-4 vectors, no network).

    Patches ``indexer.embed_inputs`` (not the global ``httpx.AsyncClient``) so
    this fixture cannot also hijack the test's own ASGI client — the httpx wire
    is asserted in ``tests/test_memory_reembed.py``.
    """

    async def fake_embed(endpoint, model, inputs, api_key=None, timeout_s=120.0):
        return [VEC for _ in inputs]

    monkeypatch.setattr(indexer, "embed_inputs", fake_embed)


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    store = MemoryStore(tmp_path / "memory.db")
    built = []

    def _build(*, memorizer: object | None = None, registry: object | None = None):
        app = create_app(FakeTurnEngine(), log_path)
        app.include_router(router)
        app.state.memorizer = memorizer if memorizer is not None else FakeMemorizer(store)
        app.state.registry = registry if registry is not None else FakeRegistry()
        built.append(app)
        return app

    yield _build, store
    store.close()
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _seed(app, session_id: str, *, memory: bool = True) -> None:
    app.state.log.append_event(session_id, "user", "user", {"text": "hi"})
    app.state.log.append_event(session_id, "narrative", "narrator", {"prose": "prose"})
    if not memory:
        app.state.log.append_event(
            session_id, "system", "user", {"event": "chat_memory", "enabled": False}
        )


async def _wait_done(client: httpx.AsyncClient) -> dict:
    for _ in range(100):
        resp = await client.get("/api/memory/reembed")
        body = resp.json()
        if body["state"] in ("done", "failed"):
            return body
        await asyncio.sleep(0.01)
    return body


# -- reset -----------------------------------------------------------------


def test_reset_wipes_every_collection(build_app):
    build, store = build_app
    app = build()
    store.ensure_collection("c_a", DIM)
    store.ensure_collection("p_b", DIM)

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/memory/reset")

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["reset"] is True
    assert body["collections"] == []
    assert sorted(body["previous"]) == ["c_a", "p_b"]
    assert store.collections() == []


def test_collections_reads_empty_after_reset(build_app):
    build, store = build_app
    app = build()
    store.ensure_collection("c_a", DIM)

    async def scenario():
        async with client_for(app) as client:
            before = (await client.get("/api/memory/collections")).json()
            await client.post("/api/memory/reset")
            after = (await client.get("/api/memory/collections")).json()
            return before, after

    before, after = run(scenario())
    assert before["collections"] == ["c_a"]
    assert after["collections"] == []


def test_reset_without_memorizer_is_503(build_app):
    build, _ = build_app
    app = build(memorizer=object())  # no `.store`

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/memory/reset")

    resp = run(scenario())
    assert resp.status_code == 503


# -- reembed job -----------------------------------------------------------


def test_reembed_status_idle_before_start(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/memory/reembed")

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"state": "idle", "done": 0, "total": 0}


def test_reembed_job_transitions_and_respects_optout(build_app, embed_transport):
    build, store = build_app
    app = build()
    _seed(app, "s-on")
    _seed(app, "s-off", memory=False)

    async def scenario():
        async with client_for(app) as client:
            started = await client.post("/api/memory/reembed")
            done = await _wait_done(client)
            return started.json(), done

    started, done = run(scenario())
    assert started["started"] is True
    assert started["state"] in ("running", "done")
    assert done["state"] == "done"
    assert done["done"] == done["total"] == 1  # only the enabled chat
    assert "error" not in done
    # The enabled chat now has an embedded collection; the opted-out one does not.
    assert store.collections() == ["c_s_on"]


def test_reembed_is_idempotent_and_restartable(build_app, embed_transport):
    build, store = build_app
    app = build()
    _seed(app, "s-on")

    async def scenario():
        async with client_for(app) as client:
            await client.post("/api/memory/reembed")
            first = await _wait_done(client)
            await client.post("/api/memory/reembed")
            second = await _wait_done(client)
            return first, second

    first, second = run(scenario())
    assert first["state"] == second["state"] == "done"
    assert second["done"] == 1


def test_reembed_running_job_is_not_restarted(build_app, embed_transport):
    build, store = build_app
    app = build()
    _seed(app, "s-on")

    async def scenario():
        async with client_for(app) as client:
            first = (await client.post("/api/memory/reembed")).json()
            second = (await client.post("/api/memory/reembed")).json()
            await _wait_done(client)
            return first, second

    first, second = run(scenario())
    assert first["started"] is True
    # A concurrent start either reports running (not restarted) or, if the fast
    # job already finished, reports the done state without restarting.
    assert second["started"] is False


def test_reembed_without_embedder_is_503(build_app):
    build, _ = build_app
    app = build(registry=FakeRegistry(has_embedder=False))

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/memory/reembed")

    resp = run(scenario())
    assert resp.status_code == 503
    assert resp.json()["detail"] == "embedder unavailable"


def test_reset_then_reembed_recreates_index(build_app, embed_transport):
    """The Part-2 + Part-3 flow: switch -> reset -> reembed -> recall has rows."""
    build, store = build_app
    app = build()
    _seed(app, "s-on")

    async def scenario():
        async with client_for(app) as client:
            await client.post("/api/memory/reset")
            empty = (await client.get("/api/memory/collections")).json()
            await client.post("/api/memory/reembed")
            done = await _wait_done(client)
            return empty, done

    empty, done = run(scenario())
    assert empty["collections"] == []
    assert done["state"] == "done"
    assert store.collections() == ["c_s_on"]
