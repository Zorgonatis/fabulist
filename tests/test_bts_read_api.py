"""W6-D: BTS world-state + attribution READ endpoints.

The post-turn node persists ``system``/``world_state`` and
``system``/``attribution`` events unconditionally (the agent-debug toggle gates
only DebugCapture rows), but no route exposed them — so the dashboard silently
fell back to parsing debug captures. These endpoints (added to the already
mounted ``cast_api`` router) read the persisted events fork-resolved.

Exercises the real FastAPI app (httpx ASGI transport) over a synthetic tmp
SQLite log, mirroring ``test_player_hud_api``. Contracts asserted:
* ``/world-state`` returns the latest event's structured payload + a capped
  ``?history=1`` list, and ``latest: null`` for a known session with none;
* ``/attributions`` returns seq-ordered ``{seq, attributions}`` rows;
* failure variants (``world_state_failed`` / ``attribution_failed``) are NOT
  surfaced as state; malformed lanes normalise to their empty container;
* both endpoints 404 an unknown session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cast_api import WORLD_STATE_HISTORY_CAP
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None):
        app = create_app(engine or FakeTurnEngine(), log_path)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _world_state(log: EventLog, *, kind: str, state: dict, **lanes) -> None:
    payload = {"event": "world_state", "kind": kind, "state": state}
    payload.update(lanes)
    log.append_event("s1", "system", "bts", payload)


def _seed(log_path: Path) -> None:
    with EventLog(log_path) as log:
        log.append_event("s1", "user", "user", {"text": "hi"})
        log.append_event("s1", "narrative", "narrator", {"prose": "hello"})
        # A checkpoint then a delta (the shape the BTS node writes).
        _world_state(
            log,
            kind="checkpoint",
            state={"PC": {"emotional": {"mood": "stoic"}}},
            venue={"PC": {"venue": "room"}},
            tension={"level": 8},
        )
        _world_state(
            log,
            kind="delta",
            state={"PC": {"emotional": {"focus": "Garrick→Stranger"}}},
            threads=[{"status": "new", "name": "torn_page", "detail": "missing"}],
        )
        # A failed world-state pass: visible in the log, NOT a state read.
        log.append_event(
            "s1",
            "system",
            "bts",
            {"event": "world_state_failed", "stage": "parse", "error": "boom"},
        )
        # Attribution: an empty row (pins the turn) then a real one, then a
        # failure variant that must not surface.
        log.append_event(
            "s1", "system", "attribution", {"event": "attribution", "attributions": []}
        )
        log.append_event(
            "s1",
            "system",
            "attribution",
            {
                "event": "attribution",
                "attributions": [{"speaker": "Aria", "quote": "Hello there."}],
            },
        )
        log.append_event(
            "s1",
            "system",
            "attribution",
            {"event": "attribution_failed", "stage": "parse", "error": "boom"},
        )


def test_world_state_latest_payload(build_app):
    build, log_path = build_app
    _seed(log_path)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/world-state")).json()

    body = run(scenario())
    assert body["session_id"] == "s1"
    latest = body["latest"]
    assert latest["kind"] == "delta"
    assert latest["state"] == {"PC": {"emotional": {"focus": "Garrick→Stranger"}}}
    assert latest["threads"] == [
        {"status": "new", "name": "torn_page", "detail": "missing"}
    ]
    # Normalised empty containers for lanes the delta omitted.
    assert latest["venue"] == {}
    assert latest["off_screen"] == []
    assert latest["tension"] == {}
    assert isinstance(latest["seq"], int)
    # Default read carries no history.
    assert "history" not in body


def test_world_state_history_is_seq_ordered_and_capped(build_app):
    build, log_path = build_app
    _seed(log_path)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/world-state?history=1")).json()

    body = run(scenario())
    history = body["history"]
    assert [row["kind"] for row in history] == ["checkpoint", "delta"]
    assert [row["seq"] for row in history] == sorted(row["seq"] for row in history)
    assert history[0]["state"] == {"PC": {"emotional": {"mood": "stoic"}}}
    assert history[0]["tension"] == {"level": 8}
    assert body["latest"]["seq"] == history[-1]["seq"]
    # A failed pass never becomes a world-state row.
    assert all(row["kind"] in {"delta", "checkpoint"} for row in history)


def test_world_state_cap_constant_is_50():
    assert WORLD_STATE_HISTORY_CAP == 50


def test_world_state_known_session_without_events_is_null(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("empty", "user", "user", {"text": "hi"})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/empty/world-state")).json()

    body = run(scenario())
    assert body == {"session_id": "empty", "latest": None}


def test_attributions_seq_ordered(build_app):
    build, log_path = build_app
    _seed(log_path)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/attributions")).json()

    body = run(scenario())
    assert body["session_id"] == "s1"
    rows = body["attributions"]
    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
    assert rows[0]["attributions"] == []
    assert rows[1]["attributions"] == [{"speaker": "Aria", "quote": "Hello there."}]
    # attribution_failed is not surfaced.
    assert len(rows) == 2


def test_world_state_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/nope/world-state")

    assert run(scenario()).status_code == 404


def test_attributions_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/nope/attributions")

    assert run(scenario()).status_code == 404
