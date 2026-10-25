"""Tests for the global search router (owner ruling, item 8).

Exercises the real FastAPI app (httpx ASGI transport) with the global-search
router mounted over a tmp SQLite event log. The semantic half runs against a
fake store + fake embedder (no network, no embedder); the literal half reads
real session rows and real anchor-session definitions.

Contracts asserted: session titles and definition names match case-insensitively;
hidden sessions never surface; semantic rows are grouped to one best hit per
session and filtered to visible chats; a semantic failure degrades to literal
results only; a missing memorizer stays literal-only; a blank query is empty.
No pytest-asyncio in this repo, so async scenarios run via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api import global_search_api
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.global_search_api import router
from fabulist.context.books import append_book
from fabulist.personas.registry import append_persona
from fabulist.worlds.registry import append_world


def run(coro):
    return asyncio.run(coro)


class FakeStore:
    """Canned ``query_many`` seam capturing the ``(collections, vectors, k)`` call."""

    def __init__(
        self,
        rows: list[dict] | None = None,
        collections: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self._collections = collections if collections is not None else ["w_x", "a_y"]
        self.error = error
        self.queries: list[tuple[list[str], list[list[float]], int]] = []

    def collections(self) -> list[str]:
        return list(self._collections)

    def query_many(
        self, collections, vectors, k: int = 8
    ) -> list[dict]:
        self.queries.append((list(collections), list(vectors), k))
        if self.error is not None:
            raise self.error
        return list(self.rows)


class FakeRegistry:
    """Minimal registry: one live ``embedder`` connection (never dialed)."""

    def connection_for(self, agent: str) -> SimpleNamespace:
        return SimpleNamespace(
            base_url="http://embed.local/v1", model="fake-embed", api_key_env=None
        )


class FakeMemorizer:
    """The memorizer seam the router reads: ``store`` + ``registry``."""

    def __init__(self, store: FakeStore, registry: FakeRegistry | None = None) -> None:
        self.store = store
        self.registry = registry or FakeRegistry()


@pytest.fixture
def fake_embed(monkeypatch):
    """Replace the live embed call with a canned vector (no network)."""

    async def _embed(endpoint, model, texts, api_key=None, timeout_s=30.0):
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(global_search_api, "embed_texts", _embed)


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


async def make_session(client: httpx.AsyncClient, title: str) -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def test_literal_session_title_matches_case_insensitively(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client, "Rainy Neon City")
            await make_session(client, "Quiet Library")
            return sid, await client.get("/api/search/global", params={"q": "neon"})

    sid, resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert [row["session_id"] for row in body["sessions"]] == [sid]
    assert body["sessions"][0]["title"] == "Rainy Neon City"
    assert body["sessions"][0]["score"] is None
    assert body["definitions"] == []


def test_literal_definitions_match_persona_world_book(build_app):
    build, log_path = build_app
    app = build()
    log = app.state.log
    persona_id = append_persona(log, {"name": "Alice Nightingale"})
    world_id = append_world(log, {"name": "Alice's Wonderland"})
    book_id = append_book(log, {"name": "Alices Bestiary"})
    append_persona(log, {"name": "Bob"})

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/search/global", params={"q": "alice"})

    resp = run(scenario())
    definitions = resp.json()["definitions"]
    assert [(d["kind"], d["id"], d["name"]) for d in definitions] == [
        ("persona", persona_id, "Alice Nightingale"),
        ("world", world_id, "Alice's Wonderland"),
        ("book", book_id, "Alices Bestiary"),
    ]


def test_hidden_session_never_surfaces(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client, "Hidden Neon")
            await client.delete(f"/api/sessions/{sid}")
            return await client.get("/api/search/global", params={"q": "neon"})

    resp = run(scenario())
    assert resp.json()["sessions"] == []


def test_semantic_groups_best_hit_per_visible_session(build_app, fake_embed):
    build, _ = build_app
    app = build()
    store = FakeStore()

    async def scenario():
        async with client_for(app) as client:
            await make_session(client, "Quiet")
            sid = await make_session(client, "Storm")
            # Rows are built AFTER the real session exists so the visible-session
            # filter is exercised with genuine ids plus two it must drop.
            store.rows = [
                {"session_id": "gone", "seq": 9, "kind": "user", "text": "x", "distance": 0.01},
                {"session_id": None, "seq": 8, "kind": "user", "text": "y", "distance": 0.02},
                {"session_id": sid, "seq": 3, "kind": "narrative", "text": "far", "distance": 0.9},
                {"session_id": sid, "seq": 2, "kind": "user", "text": "  the   rain ", "distance": 0.2},
            ]
            app.state.memorizer = FakeMemorizer(store)
            return sid, await client.get("/api/search/global", params={"q": "rain"})

    sid, resp = run(scenario())
    body = resp.json()
    assert [row["session_id"] for row in body["sessions"]] == [sid]  # 'gone'/None dropped
    assert body["sessions"][0]["snippet"] == "the rain"  # collapsed + best hit
    assert body["sessions"][0]["title"] == "Storm"
    assert body["sessions"][0]["score"] == 0.2
    # ALL collections queried once, with the embed vector.
    assert store.queries == [(["w_x", "a_y"], [[0.1, 0.2, 0.3]], 8)]


def test_semantic_and_literal_dedup_by_session(build_app, fake_embed):
    build, _ = build_app
    app = build()
    store = FakeStore()

    async def scenario():
        async with client_for(app) as client:
            await make_session(client, "Stormy Bay")
            sid = await make_session(client, "Neon Nights")
            store.rows = [
                {
                    "session_id": sid,
                    "seq": 1,
                    "kind": "narrative",
                    "text": "rain",
                    "distance": 0.4,
                }
            ]
            app.state.memorizer = FakeMemorizer(store)
            return sid, await client.get("/api/search/global", params={"q": "neon"})

    sid, resp = run(scenario())
    rows = resp.json()["sessions"]
    assert [row["session_id"] for row in rows] == [sid]  # semantic, literal deduped
    assert rows[0]["snippet"] == "rain"


def test_semantic_error_degrades_to_literal(build_app, fake_embed):
    build, _ = build_app
    app = build()
    app.state.memorizer = FakeMemorizer(FakeStore(error=RuntimeError("embedder down")))

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client, "Neon City")
            return sid, await client.get("/api/search/global", params={"q": "neon"})

    sid, resp = run(scenario())
    body = resp.json()
    assert [row["session_id"] for row in body["sessions"]] == [sid]  # literal only
    assert body["sessions"][0]["score"] is None


def test_missing_memorizer_is_literal_only(build_app):
    build, _ = build_app
    app = build()
    app.state.memorizer = None

    async def scenario():
        async with client_for(app) as client:
            sid = await make_session(client, "Neon City")
            return sid, await client.get("/api/search/global", params={"q": "neon"})

    sid, resp = run(scenario())
    assert [row["session_id"] for row in resp.json()["sessions"]] == [sid]


def test_blank_query_returns_empty(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await make_session(client, "Neon City")
            return await client.get("/api/search/global", params={"q": "   "})

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"sessions": [], "definitions": []}
