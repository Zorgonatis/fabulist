"""Tests for session module homes (Wave B / B2 + B1 fold helpers).

Covers :func:`fabulist.cards.homes.seed_homes` (sparse seeding of the card's
present V2 text fields, anchors/names/origin/module_id),
:func:`~fabulist.cards.homes.session_modules_from_log` (latest-wins fold of
module events and tombstones), and the import router actually appending the
seeded events to the new session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cards_api import router as cards_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.cards import parse_card_v2
from fabulist.cards.homes import (
    ANCHOR_POST_HISTORY,
    ANCHOR_TOP,
    seed_homes,
    session_modules_from_log,
)
from fabulist.log import EventLog

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for an :class:`EventLog` on a tmp path; closes logs after."""
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build():
        app = create_app(FakeTurnEngine(), log_path)
        app.include_router(cards_router)
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


FULL_CARD = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Lyra Voss",
        "description": "A wandering cartographer.",
        "first_mes": "Lyra looks up.",
        "system_prompt": "Stay in Lyra's voice and never break character.",
        "post_history_instructions": "End the scene on a hook.",
        "mes_example": "{{user}}: Where to?\n{{char}}: Anywhere with a map.",
    },
}


# --- seed_homes -------------------------------------------------------------


def test_seed_homes_all_present_fields_shapes_and_order():
    payloads = seed_homes(parse_card_v2(FULL_CARD))
    assert [p["anchor"] for p in payloads] == [ANCHOR_TOP, ANCHOR_POST_HISTORY, ANCHOR_TOP]
    assert [p["event"] for p in payloads] == ["session_module"] * 3
    assert payloads[0]["content"].startswith("Stay in Lyra's voice")
    assert payloads[0]["name"] == "Card system prompt"
    assert payloads[1]["name"] == "Card post-history instructions"
    assert payloads[2]["name"] == "Card example messages"
    assert payloads[0]["module_id"] == "card:Lyra Voss:system_prompt"
    assert payloads[1]["module_id"] == "card:Lyra Voss:post_history_instructions"
    assert payloads[2]["module_id"] == "card:Lyra Voss:mes_example"
    assert all(p["origin"] == "card:Lyra Voss" for p in payloads)
    assert all(p["role"] == "system" for p in payloads)
    assert all(p["enabled"] is True for p in payloads)


def test_seed_homes_is_sparse():
    card = parse_card_v2(
        {
            "spec": "chara_card_v2",
            "data": {"name": "Solo", "description": "x", "system_prompt": "Only this."},
        }
    )
    payloads = seed_homes(card)
    assert len(payloads) == 1
    assert payloads[0]["module_id"] == "card:Solo:system_prompt"


def test_seed_homes_skips_blank_fields():
    card = parse_card_v2(
        {
            "spec": "chara_card_v2",
            "data": {
                "name": "Blank",
                "description": "x",
                "system_prompt": "   ",
                "mes_example": "",
            },
        }
    )
    assert seed_homes(card) == []


def test_seed_homes_accepts_a_dict():
    payloads = seed_homes({"name": "Dicto", "system_prompt": "from a dict"})
    assert len(payloads) == 1
    assert payloads[0]["module_id"] == "card:Dicto:system_prompt"
    assert payloads[0]["content"] == "from a dict"


# --- session_modules_from_log fold ------------------------------------------


def test_session_modules_from_log_returns_seeded_rows(build_log):
    log = build_log()
    sid = "s"
    for home in seed_homes(parse_card_v2(FULL_CARD)):
        log.append_event(sid, "system", "system", home)
    modules = session_modules_from_log(log, sid)
    assert [m["module_id"] for m in modules] == [
        "card:Lyra Voss:system_prompt",
        "card:Lyra Voss:post_history_instructions",
        "card:Lyra Voss:mes_example",
    ]


def test_session_modules_from_log_tombstone_removes(build_log):
    log = build_log()
    sid = "s"
    log.append_event(
        sid, "system", "system",
        {"event": "session_module", "module_id": "a", "anchor": "top", "content": "A"},
    )
    log.append_event(sid, "system", "user", {"event": "session_module_deleted", "module_id": "a"})
    assert session_modules_from_log(log, sid) == []


def test_session_modules_from_log_latest_wins_and_keeps_disabled(build_log):
    log = build_log()
    sid = "s"
    log.append_event(
        sid, "system", "system",
        {"event": "session_module", "module_id": "a", "anchor": "top", "content": "OLD"},
    )
    log.append_event(
        sid, "system", "user",
        {"event": "session_module", "module_id": "a", "anchor": "top", "content": "NEW", "enabled": False},
    )
    modules = session_modules_from_log(log, sid)
    assert len(modules) == 1
    assert modules[0]["content"] == "NEW"
    assert modules[0]["enabled"] is False


# --- import route seeds homes -----------------------------------------------


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_import_seeds_session_module_events(build_app):
    app = build_app()

    async def scenario():
        async with _client(app) as client:
            resp = await client.post("/api/cards/import", json={**FULL_CARD, "seed": True})
            return resp

    resp = asyncio.run(scenario())
    assert resp.status_code == 200
    sid = resp.json()["session_id"]

    log = EventLog(app.state.log.path)
    events = log.get_events(sid)
    log.close()
    module_events = [
        e.payload for e in events
        if e.type == "system" and e.payload.get("event") == "session_module"
    ]
    assert [p["module_id"] for p in module_events] == [
        "card:Lyra Voss:system_prompt",
        "card:Lyra Voss:post_history_instructions",
        "card:Lyra Voss:mes_example",
    ]


def test_import_without_optional_fields_seeds_no_modules(build_app):
    app = build_app()
    card = {
        "spec": "chara_card_v2",
        "data": {"name": "Plain", "description": "x", "first_mes": "hi"},
    }

    async def scenario():
        async with _client(app) as client:
            return await client.post("/api/cards/import", json={**card, "seed": True})

    resp = asyncio.run(scenario())
    log = EventLog(app.state.log.path)
    events = log.get_events(resp.json()["session_id"])
    log.close()
    assert not [e for e in events if e.payload.get("event") == "session_module"]
