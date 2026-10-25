"""W9.29 §4 — the entity-catalog asset's transport (``/api/prompts/entity-catalog``).

The asset's OWN rules (seed, fold, replace, validation) are pinned by
``tests/test_entity_catalog.py``; this file pins the HTTP half the Studio pane
talks to: GET serves the EFFECTIVE catalog (the code seed while the owner has
saved nothing), PUT replaces the whole catalog, a validation failure is a ``400``
with the engine's own message, and the engine's tool door reads the very same
asset afterwards.

The router is mounted by hand here: ``create_app`` does not include it yet (the
mount is an orchestrator-owned seam — ``app.include_router(entity_catalog_router)``),
and a test that assumed otherwise would be pinning wiring that does not exist.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from fabulist.api.entity_catalog_api import ENTITY_CATALOG_PATH, entity_catalog_router
from fabulist.log import EventLog
from fabulist.state import entity_catalog


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(tmp_path: Path):
    """A bare app carrying only this router + the ``app.state.log`` seam."""
    log = EventLog(tmp_path / "events.db")
    app = FastAPI()
    app.state.log = log
    app.include_router(entity_catalog_router)

    async def _client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    yield _client, log
    log.close()


def _custom_row(**overrides):
    row = {
        "key": "dread",
        "label": "Dread",
        "type": "meter",
        "host": "relational",
        "range": [0, 50],
        "description": "Cold anticipation of harm.",
    }
    row.update(overrides)
    return row


def test_the_router_serves_the_asset_path_the_frontend_client_calls():
    """One spelling per concept: the pane's client and the router agree."""
    paths = [getattr(route, "path", "") for route in entity_catalog_router.routes]
    assert paths.count(ENTITY_CATALOG_PATH) == 2
    assert ENTITY_CATALOG_PATH == "/api/prompts/entity-catalog"


def test_get_serves_the_code_seed_on_a_fresh_log(client):
    make, _log = client

    async def go():
        async with await make() as http:
            return await http.get(ENTITY_CATALOG_PATH)

    response = run(go())
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert [row["key"] for row in rows] == [
        row["key"] for row in entity_catalog.default_rows()
    ]
    # The seed is a READ: nothing was written to the anchor.
    assert entity_catalog._stored_rows(_log) in (None, [])


def test_put_replaces_the_catalog_and_get_reflects_it(client):
    make, log = client

    async def go():
        async with await make() as http:
            put = await http.put(ENTITY_CATALOG_PATH, json={"rows": [_custom_row()]})
            get = await http.get(ENTITY_CATALOG_PATH)
        return put, get

    put, get = run(go())
    assert put.status_code == 200
    assert [row["key"] for row in put.json()["rows"]] == ["dread"]
    assert get.json() == put.json()
    # The engine's tool door reads the same asset (one code path, §4).
    assert [row["key"] for row in entity_catalog.catalog_for(log)] == ["dread"]


def test_an_empty_list_is_a_legal_write_and_falls_back_to_the_seed(client):
    make, _log = client

    async def go():
        async with await make() as http:
            put = await http.put(ENTITY_CATALOG_PATH, json={"rows": []})
            get = await http.get(ENTITY_CATALOG_PATH)
        return put, get

    put, get = run(go())
    assert put.status_code == 200
    keys = [row["key"] for row in put.json()["rows"]]
    assert keys == [row["key"] for row in entity_catalog.default_rows()]
    assert get.json() == put.json()


def test_a_validation_failure_is_a_400_with_the_engine_message(client):
    make, log = client

    async def post(body):
        async with await make() as http:
            return await http.put(ENTITY_CATALOG_PATH, json=body)

    cases = [
        # duplicate keys (the second row is the first one again)
        ({"rows": [_custom_row(), _custom_row()]}, "duplicate key 'dread'"),
        # a meter with no range
        (
            {"rows": [{key: value for key, value in _custom_row().items() if key != "range"}]},
            "a meter needs a 'range'",
        ),
        # a range that does not ascend
        ({"rows": [_custom_row(range=[50, 0])]}, "'range' must ascend"),
        # a trait carrying a range
        (
            {"rows": [_custom_row(type="trait", host="self")]},
            "a trait carries no 'range'",
        ),
        # a key that is not a slug (the tracker cites it in a tool call)
        ({"rows": [_custom_row(key="Dread!")]}, "'key' must be a lowercase slug"),
        # a host outside the three the registry knows
        ({"rows": [_custom_row(host="party")]}, "'host' must be one of"),
        # the envelope itself
        ({"rows": "nope"}, "'rows' must be a list"),
        ({}, "'rows' must be a list"),
    ]
    for body, expected in cases:
        response = run(post(body))
        assert response.status_code == 400, body
        assert expected in response.json()["detail"], body
    # A refused write stores nothing: the seed is still what the engine reads.
    assert [row["key"] for row in entity_catalog.catalog_for(log)] == [
        row["key"] for row in entity_catalog.default_rows()
    ]


def test_a_missing_log_is_a_500_not_a_crash(tmp_path: Path):
    app = FastAPI()
    app.include_router(entity_catalog_router)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            return await http.get(ENTITY_CATALOG_PATH)

    assert run(go()).status_code == 500
