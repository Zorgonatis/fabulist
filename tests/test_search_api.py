"""Tests for the session semantic search router (SURFACE #28).

Exercises the real FastAPI app (httpx ASGI transport) with the search router
mounted over a tmp SQLite event log. ``app.state.memorizer`` is monkeypatched
with a fake exposing ``async search`` (canned rows) — no embedder, no network.

Contracts asserted: 404 unknown session, 503 when the memorizer is absent or
lacks ``search``, 422 empty query, ``k`` clamped to 1..25, text whitespace-
collapsed and clipped to 400 chars, rows sorted by distance ascending.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.search_api import _K_DEFAULT, _K_MAX, _K_MIN, router


class FakeMemorizer:
    """Canned ``search`` seam capturing the ``(session_id, query, k)`` call."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, str, int]] = []

    async def search(self, session_id: str, query: str, k: int = 8) -> list[dict]:
        self.calls.append((session_id, query, k))
        return list(self.rows)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None):
        app = create_app(engine or FakeTurnEngine(), log_path)
        app.include_router(router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def make_session(client: httpx.AsyncClient) -> str:
    resp = await client.post("/api/sessions", json={"title": "t"})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def test_search_returns_sorted_collapsed_results(build_app):
    build, _ = build_app
    app = build()
    mem = FakeMemorizer(
        [
            {"seq": 5, "kind": "narrative", "text": "far \n\t away", "distance": 0.9},
            {"seq": 2, "kind": "user", "text": "  hello   world ", "distance": 0.1},
            {"seq": 3, "kind": "narrative", "text": "middle", "distance": 0.5},
        ]
    )
    app.state.memorizer = mem

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client)
            resp = await client.post(f"/api/sessions/{sid}/search", json={"query": "hi"})
            return sid, resp

    sid, resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert [r["seq"] for r in body["results"]] == [2, 3, 5]  # distance ascending
    assert body["results"][0]["text"] == "hello world"  # whitespace-collapsed
    assert body["results"][0]["kind"] == "user"
    assert set(body["results"][0]) == {"seq", "event_id", "kind", "text", "distance"}
    assert mem.calls == [(sid, "hi", _K_DEFAULT)]


def test_text_is_clipped_to_400_chars(build_app):
    build, _ = build_app
    app = build()
    app.state.memorizer = FakeMemorizer(
        [{"seq": 1, "kind": "user", "text": "x" * 1000, "distance": 0.0}]
    )

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client)
            return await client.post(
                f"/api/sessions/{sid}/search", json={"query": "q"}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert len(resp.json()["results"][0]["text"]) == 400


def test_k_is_clamped_into_range(build_app):
    build, _ = build_app
    app = build()
    mem = FakeMemorizer([])
    app.state.memorizer = mem

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client)
            await client.post(f"/api/sessions/{sid}/search", json={"query": "q", "k": 999})
            await client.post(f"/api/sessions/{sid}/search", json={"query": "q", "k": 0})
            await client.post(f"/api/sessions/{sid}/search", json={"query": "q", "k": -5})
            await client.post(f"/api/sessions/{sid}/search", json={"query": "q", "k": 7})

    run(scenario())
    assert [k for _, _, k in mem.calls] == [_K_MAX, _K_MIN, _K_MIN, 7]


def test_missing_memorizer_is_503(build_app):
    build, _ = build_app
    app = build()
    app.state.memorizer = None

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client)
            return await client.post(
                f"/api/sessions/{sid}/search", json={"query": "q"}
            )

    resp = run(scenario())
    assert resp.status_code == 503
    assert resp.json()["detail"] == "memory unavailable"


def test_memorizer_without_search_is_503(build_app):
    build, _ = build_app
    app = build()

    class NoSearch:
        pass

    app.state.memorizer = NoSearch()

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client)
            return await client.post(
                f"/api/sessions/{sid}/search", json={"query": "q"}
            )

    resp = run(scenario())
    assert resp.status_code == 503
    assert resp.json()["detail"] == "memory unavailable"


def test_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()
    app.state.memorizer = FakeMemorizer([])

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/does-not-exist/search", json={"query": "q"}
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


def test_empty_query_is_422(build_app):
    build, _ = build_app
    app = build()
    mem = FakeMemorizer([])
    app.state.memorizer = mem

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client)
            return await client.post(
                f"/api/sessions/{sid}/search", json={"query": "   "}
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert resp.json()["detail"] == "empty query"
    assert mem.calls == []  # never reaches the memorizer
