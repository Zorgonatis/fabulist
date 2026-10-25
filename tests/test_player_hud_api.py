"""Wave 4b player-HUD backend reads: roll detail, recap, cast colours.

Exercises the real FastAPI app (httpx ASGI transport) over a synthetic tmp
SQLite log:
* HB1 — ``RollResult.detail`` survives an append/read round-trip;
* HB2 — ``GET /api/sessions/{id}/recap`` returns summaries in seq order;
* HB4 — ``inspect/turns`` carries per-turn ``rolls`` + session ``recap`` /
  ``speaker_colors`` (additive);
* HB3 read half — ``GET /api/sessions/{id}/speaker_colors``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cast_api import SPEAKER_PALETTE, assign_speaker_color
from fabulist.api.fake import FakeTurnEngine
from fabulist.graph.rolls import roll_dice
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


def _seed(log_path: Path) -> None:
    with EventLog(log_path) as log:
        log.append_event("s1", "user", "user", {"text": "roll 1d20"})
        rolled = roll_dice("1d20", rng=_FixedRNG(), ask="for initiative")
        # Exactly what engine.py does at the roll-append seam.
        log.append_event("s1", "roll", "gm", rolled.as_payload())
        log.append_event("s1", "narrative", "narrator", {"prose": "You roll."})
        log.append_event(
            "s1",
            "summary",
            "summarizer",
            {"text": "They entered the keep.", "covers_seqs": [1, 3]},
        )
        assign_speaker_color(log, "s1", "Aria")


class _FixedRNG:
    """Deterministic ``randint`` returning 17 (so the detail is predictable)."""

    def randint(self, a: int, b: int) -> int:
        return 17


def test_roll_detail_survives_round_trip(build_app):
    _, log_path = build_app
    _seed(log_path)
    with EventLog(log_path) as log:
        roll = next(e for e in log.get_events("s1") if e.type == "roll")
    assert roll.payload == {
        "dice": "1d20",
        "result": 17,
        "detail": "1d20[17] = 17",
        "ask": "for initiative",
    }


def test_recap_endpoint_orders_summaries(build_app):
    build, log_path = build_app
    _seed(log_path)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/recap")).json()

    body = run(scenario())
    assert body["session_id"] == "s1"
    assert body["recap"] == [
        {"seq": 4, "text": "They entered the keep.", "covers_seqs": [1, 3]}
    ]


def test_recap_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/nope/recap")

    assert run(scenario()).status_code == 404


def test_speaker_colors_endpoint(build_app):
    build, log_path = build_app
    _seed(log_path)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/speaker_colors")).json()

    body = run(scenario())
    # W7.9: rows are roster-resolved; this seed has no card/roster, so the name
    # matches no member and the row carries a null member id.
    assert body["speaker_colors"] == [
        {
            "member_id": None,
            "name": "Aria",
            "color": SPEAKER_PALETTE[0],
            "assigned_by": "engine",
        }
    ]


def test_inspector_additive_hud_fields(build_app):
    build, log_path = build_app
    _seed(log_path)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/inspect/turns")).json()

    body = run(scenario())
    turn = body["turns"][0]
    assert turn["rolls"] == [
        {
            "seq": 2,
            "dice": "1d20",
            "result": 17,
            "detail": "1d20[17] = 17",
            "ask": "for initiative",
        }
    ]
    assert body["recap"] == [
        {"seq": 4, "text": "They entered the keep.", "covers_seqs": [1, 3]}
    ]
    assert body["speaker_colors"] == {"Aria": SPEAKER_PALETTE[0]}
    # Additive only: the pre-existing keys are untouched.
    assert body["turns"][0]["user_text"] == "roll 1d20"
