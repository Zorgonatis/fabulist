"""Live-resolution tests for the memorizer wiring (D25 — the original bug).

``make_memorizer`` used to snapshot the embedder endpoint+model (and ignore
``api_key_env``) at boot, so a Connections rebind only applied after a restart.
These tests drive the REAL :func:`fabulist.wiring.make_memorizer` over a
hot-reloadable :class:`RegistryStore` and assert that ``search`` resolves the
LIVE binding on every call — and that an embedder failure degrades to ``[]``
instead of raising through the search router.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.connections.registry import RegistryStore
from fabulist.log import EventLog
from fabulist.memory import indexer
from fabulist.wiring import make_memorizer

DIM = 4
VEC = [1.0, 0.0, 0.0, 0.0]


def run(coro):
    return asyncio.run(coro)


def _ok_body(vectors):
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": vec}
            for i, vec in enumerate(vectors)
        ],
    }


@pytest.fixture
def recorder(monkeypatch):
    """Fake the embeddings endpoint; return the list of recorded requests."""

    calls: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        inputs = payload.get("input") or []
        calls.append(
            {
                "url": str(request.url),
                "model": payload.get("model"),
                "auth": request.headers.get("authorization"),
            }
        )
        return httpx.Response(200, json=_ok_body([VEC] * len(inputs)))

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(indexer.httpx, "AsyncClient", fake_client)
    return calls


def _write_registry(path: Path, *, first_key: str | None = "MEM_TEST_KEY") -> Path:
    emb1: dict = {"base_url": "http://host-one:4445/v1", "model": "emb-one"}
    if first_key:
        emb1["api_key_env"] = first_key
    data = {
        "connections": {
            "emb1": emb1,
            "emb2": {"base_url": "http://host-two:4445/v1", "model": "emb-two"},
        },
        "agents": {"embedder": "emb1", "narrator": "emb1"},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _seed_session(log_path: Path, session_id: str = "s-1") -> None:
    log = EventLog(log_path)
    try:
        log.append_event(session_id, "user", "user", {"text": "hello"})
        log.append_event(session_id, "narrative", "narrator", {"prose": "hi there"})
    finally:
        log.close()


def test_make_memorizer_none_without_embedder(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text(
        json.dumps(
            {
                "connections": {"n": {"base_url": "http://h/v1", "model": "m"}},
                "agents": {"narrator": "n"},
            }
        ),
        encoding="utf-8",
    )
    assert make_memorizer(RegistryStore(path), tmp_path / "events.db") is None


def test_search_resolves_live_binding_and_reflects_rebind(tmp_path, recorder, monkeypatch):
    monkeypatch.setenv("MEM_TEST_KEY", "k-one")
    config = _write_registry(tmp_path / "connections.json")
    log_path = tmp_path / "events.db"
    _seed_session(log_path)
    store = RegistryStore(config)

    memorizer = make_memorizer(store, log_path)
    assert memorizer is not None
    assert memorizer.store is not None

    run(memorizer.index_tail("s-1"))  # embed the seeded turn into the store
    del recorder[:]

    async def first():
        return await memorizer.search("s-1", "query")

    rows = run(first())
    assert rows  # the query returned the indexed rows
    assert recorder[0]["model"] == "emb-one"
    assert recorder[0]["url"].startswith("http://host-one:4445/v1/embeddings")
    assert recorder[0]["auth"] == "Bearer k-one"

    # Rebind the embedder agent to emb2 and hot-reload (no process restart).
    data = store.raw()
    data["agents"]["embedder"] = "emb2"
    config.write_text(json.dumps(data), encoding="utf-8")
    store.reload()

    del recorder[:]

    async def second():
        return await memorizer.search("s-1", "query")

    run(second())
    assert recorder[0]["model"] == "emb-two"
    assert recorder[0]["url"].startswith("http://host-two:4445/v1/embeddings")
    assert recorder[0]["auth"] is None  # emb2 names no key


def test_search_api_key_read_at_call_time(tmp_path, recorder, monkeypatch):
    monkeypatch.setenv("MEM_TEST_KEY", "before")
    config = _write_registry(tmp_path / "connections.json")
    log_path = tmp_path / "events.db"
    _seed_session(log_path)
    memorizer = make_memorizer(RegistryStore(config), log_path)
    assert memorizer is not None

    run(memorizer.search("s-1", "q"))
    assert recorder[0]["auth"] == "Bearer before"

    monkeypatch.setenv("MEM_TEST_KEY", "after")
    del recorder[:]
    run(memorizer.search("s-1", "q"))
    assert recorder[0]["auth"] == "Bearer after"


def test_search_returns_empty_on_embedder_failure(tmp_path, monkeypatch):
    config = _write_registry(tmp_path / "connections.json", first_key=None)
    log_path = tmp_path / "events.db"
    _seed_session(log_path)
    memorizer = make_memorizer(RegistryStore(config), log_path)
    assert memorizer is not None

    async def boom(*args, **kwargs):
        raise indexer.EmbedderError(503, "down", "http://host/v1/embeddings")

    monkeypatch.setattr(indexer, "embed_inputs", boom)
    # The error boundary swallows the failure: [] (not an unhandled 500).
    assert run(memorizer.search("s-1", "q")) == []
