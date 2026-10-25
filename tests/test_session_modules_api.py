"""Session-module override API (D29 orchestrator integration).

The Wave B lane landed the client contract (``getSessionModules`` /
``putSessionModule`` on ``.../session-modules``) and the pure fold; these tests
pin the orchestrator-owned server routes: the GET fold, the PUT append
(latest-wins), and the precise errors. The import path seeds the homes, so a
seeded card is the fixture. Mirrors test_cards_api.py's sync ``run(coro)``
convention.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cards_api import router as cards_router
from fabulist.api.fake import FakeTurnEngine

# --- fixtures -----------------------------------------------------------------------

CARD = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Homes Probe",
        "description": "A card exercising the D29 field homes.",
        "system_prompt": "You are the keeper of the fixture.",
        "post_history_instructions": "Stay in fixture voice.",
        "mes_example": "<START>\n{{user}}: hi\n{{char}}: hello",
    },
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(cards_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _seeded_session(client: httpx.AsyncClient) -> str:
    res = await client.post("/api/cards/import", json={**CARD, "seed": True})
    assert res.status_code == 200, res.text
    return res.json()["session_id"]


# --- routes -------------------------------------------------------------------------


def test_seeded_card_lists_three_modules(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _seeded_session(client)
            res = await client.get(f"/api/sessions/{sid}/session-modules")
            return sid, res

    sid, res = run(scenario())
    assert res.status_code == 200
    modules = res.json()["modules"]
    by_anchor = [m["anchor"] for m in modules]
    assert len(modules) == 3
    assert by_anchor.count("top") == 2 and by_anchor.count("post_history") == 1
    assert all(m.get("origin") for m in modules)


def test_put_module_supersedes_latest_wins(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _seeded_session(client)
            listed = await client.get(f"/api/sessions/{sid}/session-modules")
            modules = listed.json()["modules"]
            target = next(m for m in modules if m["anchor"] == "post_history")
            put = await client.put(
                f"/api/sessions/{sid}/session-modules/{target['module_id']}",
                json={
                    "name": target["name"],
                    "anchor": "post_history",
                    "content": "EDITED",
                    "enabled": False,
                    "origin": target["origin"],
                },
            )
            refolded = await client.get(f"/api/sessions/{sid}/session-modules")
            return put, refolded, target["module_id"]

    put, refolded, module_id = run(scenario())
    assert put.status_code == 200
    assert put.json()["module"]["content"] == "EDITED"
    modules = refolded.json()["modules"]
    same = next(m for m in modules if m["module_id"] == module_id)
    assert same["content"] == "EDITED" and same["enabled"] is False
    assert len(modules) == 3  # supersede, not append-and-duplicate


def test_put_rejects_unknown_anchor(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _seeded_session(client)
            return await client.put(
                f"/api/sessions/{sid}/session-modules/whatever",
                json={"name": "x", "anchor": "middle"},
            )

    assert run(scenario()).status_code == 422


def test_unknown_session_404s(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            get = await client.get("/api/sessions/nope/session-modules")
            put = await client.put(
                "/api/sessions/nope/session-modules/x",
                json={"name": "x", "anchor": "top"},
            )
            return get, put

    get, put = run(scenario())
    assert get.status_code == 404 and put.status_code == 404


def test_minimal_card_seeds_no_modules(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            res = await client.post(
                "/api/cards/import",
                json={
                    **CARD,
                    "data": {
                        "name": "Bare",
                        "description": "No prompt homes on this one.",
                    },
                    "seed": True,
                },
            )
            sid = res.json()["session_id"]
            return await client.get(f"/api/sessions/{sid}/session-modules")

    assert run(scenario()).json()["modules"] == []
