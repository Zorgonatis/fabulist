"""Tests for the human state-edit router (W2.12).

Exercises the real FastAPI app (httpx ASGI transport) with the state router
mounted, against a tmp SQLite event log. The log stays the source of truth
(D9/D10): GET folds ``state-delta`` payloads latest-wins through the log
projection, and POST appends one ``state-delta`` row (mirroring the extractor's
flat ``{key: value}`` shape) with actor ``user`` — no side table is written.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.hud_api import router as hud_router
from fabulist.api.state_api import USER_ACTOR, router
from fabulist.log import EventLog
from fabulist.state.env_hud import ENV_HUD_EVENT


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
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


def _seed(log: EventLog, session_id: str, *deltas: dict) -> None:
    """Append state-delta rows exactly like the extractor's post-turn append."""
    for delta in deltas:
        log.append_event(session_id, "state-delta", "extractor", delta)


def test_get_state_merges_latest_wins(build_app):
    """GET folds state-delta payloads in order: later value wins per key."""
    build, _ = build_app
    app = build()
    _seed(
        app.state.log,
        "s1",
        {"time": "08:00", "loc": "camp"},
        {"time": "09:00", "weather": "rain"},
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/state")

    resp = run(scenario())
    assert resp.status_code == 200
    # W9.29 §8: the response also carries the registry view (empty here).
    assert resp.json()["state"] == {
        "time": "09:00", "loc": "camp", "weather": "rain"
    }


def test_post_edit_appends_state_delta_and_get_reflects(build_app):
    """POST appends the extractor-shaped row; GET shows the edit."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed(log, "s1", {"weather": "rain"})

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                "/api/sessions/s1/state",
                json={"key": "weather", "value": "clear"},
            )
            fetched = await client.get("/api/sessions/s1/state")
            return posted, fetched

    posted, fetched = run(scenario())
    assert posted.status_code == 200
    assert posted.json() == {
        "session_id": "s1",
        "key": "weather",
        "value": "clear",
    }
    assert fetched.json()["state"] == {"weather": "clear"}

    # The edit is one appended state-delta row with the extractor's flat
    # {key: value} payload shape, stamped with actor "user" (not "extractor").
    deltas = [e for e in log.get_events("s1") if e.type == "state-delta"]
    assert len(deltas) == 2
    edit = deltas[-1]
    assert edit.actor == USER_ACTOR
    assert edit.payload == {"weather": "clear"}


def test_latest_user_edit_wins_per_key(build_app):
    """The fold is order-based: the last POST wins, extractor rows included."""
    build, _ = build_app
    app = build()
    _seed(app.state.log, "s1", {"tone": "calm"})

    async def scenario():
        async with client_for(app) as client:
            for value in ("warm", "tense"):
                resp = await client.post(
                    "/api/sessions/s1/state", json={"key": "tone", "value": value}
                )
                assert resp.status_code == 200
            return await client.get("/api/sessions/s1/state")

    resp = run(scenario())
    assert resp.json()["state"] == {"tone": "tense"}


def test_state_is_empty_for_session_without_deltas(build_app):
    """A real session with no state-delta rows folds to an empty state."""
    build, _ = build_app
    app = build()
    app.state.log.append_event("s1", "user", "user", {"text": "hi"})

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/state")

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["state"] == {}
    assert resp.json()["registry"]["locations"][0]["id"] == "loc_off_screen"


@pytest.mark.parametrize("bad_key", ["", "   "])
def test_post_empty_key_is_422_and_writes_nothing(build_app, bad_key):
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed(log, "s1", {"time": "08:00"})

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/state", json={"key": bad_key, "value": "x"}
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "key must be non-empty"

    # The rejected request left the log untouched: only the seeded delta remains.
    deltas = [e for e in log.get_events("s1") if e.type == "state-delta"]
    assert len(deltas) == 1


def test_get_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/ghost/state")

    resp = run(scenario())
    assert resp.status_code == 404


def test_post_unknown_session_is_404_and_writes_nothing(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/ghost/state",
                json={"key": "time", "value": "10:00"},
            )

    resp = run(scenario())
    assert resp.status_code == 404

    other = EventLog(log_path)
    assert other.get_events("ghost") == []
    other.close()


# --- D44/D45: date + genre ride the flat door unchanged ----------------------


def test_flat_write_accepts_date_and_genre(build_app):
    """The flat door accepts any non-empty key, so date and genre are writable
    (the fold is deliberately open; D44/D45 required no new gate)."""
    build, _ = build_app
    app = build()
    app.state.log.append_event("s1", "user", "user", {"text": "hi"})

    async def scenario():
        async with client_for(app) as client:
            for key, value in (("date", "Monday, June 02, 2025"), ("genre", "urban fantasy")):
                resp = await client.post(
                    "/api/sessions/s1/state", json={"key": key, "value": value}
                )
                assert resp.status_code == 200
            return await client.get("/api/sessions/s1/state")

    resp = run(scenario())
    assert resp.json()["state"] == {
        "date": "Monday, June 02, 2025", "genre": "urban fantasy"
    }


# --- D45: nested BTS overrides ----------------------------------------------
#
# The BTS tracker objects fold from the `system`/`world_state` lane, which the
# flat `state-delta` write never touches. POST .../state/nested folds that lane,
# applies {path, value}, and appends the result as a world_state event with
# kind="bts_override" — a non-checkpoint event the dashboard's fold deep-merges
# last (frontend lib/api.ts `foldBtsWorldState`).


def _seed_world_state(log: EventLog, session_id: str, payload: dict) -> None:
    """Append one BTS world_state event exactly like the post-turn node does."""
    log.append_event(session_id, "system", "bts", {"event": "world_state", **payload})


def test_nested_edit_supersedes_a_bts_value(build_app):
    """The override appends last and supersedes the agent's tracked value."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed(log, "s1", {"weather": "rain"})
    _seed_world_state(
        log,
        "s1",
        {
            "kind": "delta",
            "state": {"Elara": {"emotional": {"mood": "calm"}}},
            "threads": [{"name": "find the key", "status": "active"}],
        },
    )

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["state", "Elara", "emotional", "mood"], "value": "wary"},
            )
            world = await client.get("/api/sessions/s1/world-state")
            return posted, world

    posted, world = run(scenario())
    assert posted.status_code == 200
    assert posted.json() == {
        "session_id": "s1",
        "kind": "bts_override",
        "path": ["state", "Elara", "emotional", "mood"],
        "value": "wary",
    }

    latest = world.json()["latest"]
    assert latest["kind"] == "bts_override"
    # The override supersedes the fold's value...
    assert latest["state"]["Elara"]["emotional"]["mood"] == "wary"
    # ...while the untouched lanes the fold carried forward survive.
    assert latest["threads"] == [{"name": "find the key", "status": "active"}]

    # One appended event, actor "user", on the world_state lane with the audit
    # fields; nothing was written to a side table.
    overrides = [
        e
        for e in log.get_events("s1")
        if e.type == "system" and e.payload.get("kind") == "bts_override"
    ]
    assert len(overrides) == 1
    assert overrides[0].actor == USER_ACTOR
    assert overrides[0].payload["path"] == ["state", "Elara", "emotional", "mood"]


def test_later_flat_write_does_not_clobber_a_nested_override(build_app):
    """Precedence: the override lives on the world_state lane, the flat edit on
    the state-delta lane — a later flat write cannot reach it."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_world_state(
        log, "s1", {"kind": "delta", "state": {"Elara": {"emotional": {"mood": "calm"}}}}
    )

    async def scenario():
        async with client_for(app) as client:
            await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["state", "Elara", "emotional", "mood"], "value": "wary"},
            )
            # A later flat edit lands on the OTHER lane (the flat fold).
            flat = await client.post(
                "/api/sessions/s1/state", json={"key": "weather", "value": "clear"}
            )
            world = await client.get("/api/sessions/s1/world-state")
            state = await client.get("/api/sessions/s1/state")
            return flat, world, state

    flat, world, state = run(scenario())
    assert flat.status_code == 200
    assert world.json()["latest"]["state"]["Elara"]["emotional"]["mood"] == "wary"
    # The flat edit landed on its own folded lane, untouched by the override.
    assert state.json()["state"] == {"weather": "clear"}


def test_nested_edit_list_lane_by_index(build_app):
    """A list lane (threads) is edited by index inside the folded payload."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_world_state(
        log,
        "s1",
        {
            "kind": "checkpoint",
            "state": {},
            "threads": [{"name": "find the key", "status": "active"}],
        },
    )

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["threads", "0", "status"], "value": "resolved"},
            )
            world = await client.get("/api/sessions/s1/world-state")
            return posted, world

    posted, world = run(scenario())
    assert posted.status_code == 200
    assert world.json()["latest"]["threads"] == [
        {"name": "find the key", "status": "resolved"}
    ]


def test_nested_edit_creates_the_lane_on_a_legacy_session(build_app):
    """A session with no world state yet still gets a well-formed override."""
    build, _ = build_app
    app = build()
    _seed(app.state.log, "s1", {"time": "08:00"})

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["state", "Elara", "emotional", "mood"], "value": "happy"},
            )
            world = await client.get("/api/sessions/s1/world-state")
            return posted, world

    posted, world = run(scenario())
    assert posted.status_code == 200
    assert world.json()["latest"]["state"] == {
        "Elara": {"emotional": {"mood": "happy"}}
    }


@pytest.mark.parametrize("path", [[], [""], ["state", " "]])
def test_nested_empty_path_is_422_and_writes_nothing(build_app, path):
    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("s1", "user", "user", {"text": "hi"})

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/state/nested", json={"path": path, "value": "x"}
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert [e for e in log.get_events("s1") if e.payload.get("kind") == "bts_override"] == []


def test_nested_out_of_range_list_index_is_422_and_writes_nothing(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("s1", "user", "user", {"text": "hi"})

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["threads", "0", "status"], "value": "resolved"},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert [e for e in log.get_events("s1") if e.payload.get("kind") == "bts_override"] == []


def test_nested_unknown_session_is_404_and_writes_nothing(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/ghost/state/nested",
                json={"path": ["state", "Elara", "emotional", "mood"], "value": "wary"},
            )

    resp = run(scenario())
    assert resp.status_code == 404

    other = EventLog(log_path)
    assert other.get_events("ghost") == []
    other.close()


# --- D45: forecast override (the sole consuming write path) ------------------
#
# `forecast` is HUD-displayed but rides the BTS result, not the flat fold: a
# plain `state-delta` edit changed the narrator's facts while the HUD chip
# stayed stale (a lying control). `path: ["forecast"]` is now accepted by the
# nested door — it appends a `{"forecast": value}` `state-delta` row (the fold
# the narrator reads) AND re-emits the `system`/`env_hud` statement, so
# `GET .../hud` shows the pin immediately. An empty string clears it (D33).


def _seed_hud(log: EventLog, session_id: str, statement: str) -> None:
    """Append one engine-produced env_hud statement exactly like post_turn."""
    log.append_event(
        session_id, "system", "clock", {"event": ENV_HUD_EVENT, "statement": statement}
    )


def _hud_client_for(app) -> httpx.AsyncClient:
    """Client with both the state door and the HUD read route mounted."""
    app.include_router(hud_router)
    return client_for(app)


def test_forecast_override_beats_a_bts_forecast_in_the_hud(build_app):
    """The override supersedes the last (BTS-baked) HUD statement at once."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed(
        log,
        "s1",
        {
            "time": "14:35",
            "date": "Monday, June 02, 2025",
            "loc": "night market",
            "weather": "light rain",
        },
    )
    # post_turn baked a BTS forecast this turn.
    _seed_hud(
        log,
        "s1",
        'hud = EnvTracker("14:35", "Monday, June 02, 2025", "night market", '
        '"light rain", "clearing")',
    )

    async def scenario():
        async with _hud_client_for(app) as client:
            posted = await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["forecast"], "value": "storm"},
            )
            hud = await client.get("/api/sessions/s1/hud")
            return posted, hud

    posted, hud = run(scenario())
    assert posted.status_code == 200
    assert posted.json() == {
        "session_id": "s1",
        "kind": "forecast_override",
        "path": ["forecast"],
        "value": "storm",
    }
    # The HUD read now serves the override, and the other four slots survive.
    assert hud.json() == {
        "statement": 'hud = EnvTracker("14:35", "Monday, June 02, 2025", '
        '"night market", "light rain", "storm")'
    }

    # The narrator's facts changed too: one flat state-delta row, actor "user".
    deltas = [e for e in log.get_events("s1") if e.type == "state-delta"]
    assert [e.payload for e in deltas] == [
        {
            "time": "14:35",
            "date": "Monday, June 02, 2025",
            "loc": "night market",
            "weather": "light rain",
        },
        {"forecast": "storm"},
    ]
    assert deltas[-1].actor == USER_ACTOR


def test_forecast_override_keeps_the_registrys_derived_hud_slots(build_app):
    """W9.32 §6: the re-emitted chip reads the RENDERED state, not the fat fold.

    The shadow keys stopped being written, so a re-emit assembled from the raw
    projection would blank ``loc``/``weather`` the moment the owner pins a
    forecast. The statement is built from ``rendered_state`` now.
    """
    from fabulist.state import entities

    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("s1", "user", "user", {"text": "hi"})
    _seed(log, "s1", {"time": "14:35", "date": "Monday, June 02, 2025"})
    hall = entities.establish_location(log, "s1", "upstairs hall")
    bath = entities.establish_location(log, "s1", "bathroom", parent_id=hall)
    entities.move_being(log, "s1", entities.PLAYER, bath)
    entities.set_attribute(log, "s1", entities.SCENE, key="weather", value="light rain")

    async def scenario():
        async with _hud_client_for(app) as client:
            await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["forecast"], "value": "storm"},
            )
            return await client.get("/api/sessions/s1/hud")

    hud = run(scenario())
    assert hud.json() == {
        "statement": 'hud = EnvTracker("14:35", "Monday, June 02, 2025", '
        '"upstairs hall > bathroom", "light rain", "storm")'
    }


def test_forecast_override_latest_wins(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed(log, "s1", {"weather": "light rain"})

    async def scenario():
        async with _hud_client_for(app) as client:
            for value in ("storm", "fog"):
                resp = await client.post(
                    "/api/sessions/s1/state/nested",
                    json={"path": ["forecast"], "value": value},
                )
                assert resp.status_code == 200
            return await client.get("/api/sessions/s1/hud")

    resp = run(scenario())
    assert resp.json() == {
        "statement": 'hud = EnvTracker("", "", "", "light rain", "fog")'
    }


def test_forecast_clear_removes_the_override(build_app):
    """An explicit empty string clears the pin (D33), falling to weather."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed(log, "s1", {"weather": "light rain"})

    async def scenario():
        async with _hud_client_for(app) as client:
            await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["forecast"], "value": "storm"},
            )
            cleared = await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["forecast"], "value": ""},
            )
            hud = await client.get("/api/sessions/s1/hud")
            state = await client.get("/api/sessions/s1/state")
            return cleared, hud, state

    cleared, hud, state = run(scenario())
    assert cleared.status_code == 200
    # The pin is gone from both the chip and the fold; the steady weather rung
    # shows instead (BTS re-supplies the engine forecast next turn).
    assert hud.json() == {
        "statement": 'hud = EnvTracker("", "", "", "light rain", "light rain")'
    }
    assert state.json()["state"] == {"weather": "light rain", "forecast": ""}


def test_forecast_unknown_session_is_404_and_writes_nothing(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/ghost/state/nested",
                json={"path": ["forecast"], "value": "storm"},
            )

    assert run(scenario()).status_code == 404
    other = EventLog(log_path)
    assert other.get_events("ghost") == []
    other.close()


@pytest.mark.parametrize("value", [123, None, ["x"]])
def test_forecast_non_string_value_is_422_and_writes_nothing(build_app, value):
    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("s1", "user", "user", {"text": "hi"})

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["forecast"], "value": value},
            )

    assert run(scenario()).status_code == 422
    assert [e for e in log.get_events("s1") if e.type == "state-delta"] == []
    assert [e for e in log.get_events("s1") if e.payload.get("event") == ENV_HUD_EVENT] == []


def test_forecast_nested_path_is_422_and_writes_nothing(build_app):
    """forecast is a scalar slot; a nested leaf under it is malformed."""
    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("s1", "user", "user", {"text": "hi"})

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/state/nested",
                json={"path": ["forecast", "extra"], "value": "storm"},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"].startswith("forecast is a scalar HUD slot")
    assert [e for e in log.get_events("s1") if e.type == "state-delta"] == []
    assert [e for e in log.get_events("s1") if e.payload.get("event") == ENV_HUD_EVENT] == []


# --- W9.29 §8: the registry view + the user write door ----------------------


def _registry_seed(log: EventLog, session_id: str = "s1") -> dict:
    """Seed one of every registry object and return the ids the tests cite."""
    from fabulist.state import entities

    log.append_event(session_id, "user", "user", {"text": "hi"})
    hall = entities.establish_location(log, session_id, "upstairs hall")
    bath = entities.establish_location(
        log, session_id, "bathroom", parent_id=hall, note="clawfoot tub"
    )
    entities.move_being(log, session_id, entities.PLAYER, bath)
    entities.move_being(log, session_id, "cm_yara", entities.OFF_SCREEN, at=bath)
    entities.set_attribute(log, session_id, entities.PLAYER, key="stress", value=40)
    meter_id, _prior = entities.set_attribute(
        log, session_id, entities.PLAYER, key="stress", value=55
    )
    entities.set_attribute(
        log, session_id, entities.SCENE, key="tension", value=4
    )
    entities.set_attribute(
        log, session_id, entities.SCENE, key="weather", value="light rain"
    )
    thread = entities.establish_thread(log, session_id, "the slow courtship")
    item = entities.establish_item(log, session_id, "brass key", holder=entities.PLAYER)
    edge = entities.establish_relationship(
        log, session_id, entities.PLAYER, "cm_yara", note="a project"
    )
    return {
        "hall": hall,
        "bath": bath,
        "meter": meter_id,
        "thread": thread,
        "item": item,
        "edge": edge,
    }


def test_get_state_carries_the_registry_view(build_app):
    """GET /state answers the identity-stable view beside the flat fold."""
    build, _ = build_app
    app = build()
    ids = _registry_seed(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/state")

    registry = run(scenario()).json()["registry"]
    bathroom = next(row for row in registry["locations"] if row["id"] == ids["bath"])
    # A location renders its canonical path, its note and who is in it.
    assert bathroom["path"] == "upstairs hall > bathroom"
    assert bathroom["note"] == "clawfoot tub"
    assert bathroom["occupants"] == ["player"]
    bucket = next(
        row for row in registry["locations"] if row["id"] == "loc_off_screen"
    )
    assert bucket["occupants"] == ["cm_yara"]
    assert registry["off_screen_ref"] == ids["bath"]
    assert registry["assignments"] == {"player": ids["bath"], "cm_yara": "off_screen"}

    meter = next(row for row in registry["meters"] if row["id"] == ids["meter"])
    assert meter["value"] == 55
    # Identity is the point: the same mt_ id through both values, last change shown.
    assert meter["prior"] == 40
    assert isinstance(meter["changed_seq"], int)
    assert {row["key"] for row in registry["traits"]} == {"weather"}
    assert registry["threads"][0]["id"] == ids["thread"]
    assert registry["items"][0]["id"] == ids["item"]
    assert registry["relationships"][0]["id"] == ids["edge"]
    assert registry["relationships"][0]["note"] == "a project"


def test_get_state_registry_view_carries_the_legacy_fold(build_app):
    """A pre-regime session reads through the same response (read-side shim)."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_world_state(
        log,
        "s1",
        {
            "kind": "delta",
            "state": {"Yara": {"emotional": {"mood": "wary"}}},
            "venue": {"Yara": {"venue": "tavern"}},
            "off_screen": [{"name": "Seraphine", "location": "street"}],
            "threads": [{"status": "active", "name": "the debt", "detail": ""}],
            "tension": {"level": 6},
        },
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/state")

    registry = run(scenario()).json()["registry"]
    assert registry["meters"] == [] and registry["threads"] == []
    assert registry["legacy"]["state"]["Yara"]["emotional"]["mood"] == "wary"
    assert registry["legacy"]["venue"]["Yara"]["venue"] == "tavern"
    assert registry["legacy"]["off_screen"][0]["name"] == "Seraphine"
    assert registry["legacy"]["threads"][0]["name"] == "the debt"
    assert registry["legacy"]["tension"] == {"level": 6}


def test_get_state_beings_map_names_the_roster_and_the_player(build_app):
    """``beings`` turns the raw occupant keys into names (W9.31 §8.1).

    Occupants stay keys on the wire (identity is the object's), so the pane needs
    a name map: the cast roster for ``member_id``s plus the session persona for
    ``player``. A pure fold — this GET mints nothing.
    """
    from fabulist.personas.registry import append_persona, assign_persona
    from fabulist.state import cast, entities

    build, _ = build_app
    app = build()
    log = app.state.log
    assign_persona(log, "s1", append_persona(log, {"name": "Wanderer"}))
    yara = cast.admit_member(log, "s1", "Yara", provenance="bts", admitted_by="bts")
    hall = entities.establish_location(log, "s1", "upstairs hall")
    bath = entities.establish_location(log, "s1", "bathroom", parent_id=hall)
    entities.move_being(log, "s1", entities.PLAYER, bath)
    entities.move_being(log, "s1", yara, bath)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/state")

    registry = run(scenario()).json()["registry"]
    # W8: the player's KEY stays the reserved literal; the VALUE presents as
    # ``Player (Name)`` so no surface renders a raw key as a person.
    assert registry["beings"] == {yara: "Yara", "player": "Player (Wanderer)"}
    # The keys the pane renders by name are the same ones the locations carry.
    bathroom = next(row for row in registry["locations"] if row["id"] == bath)
    assert bathroom["occupants"] == ["player", yara]


def test_get_state_beings_map_is_sparse_without_a_roster(build_app):
    """An unmaterialized roster answers only the player's own label, never a raise.

    The map is a pure fold, so a session that never ran a producer has no cast
    names; the client falls back to the raw key and every occupant stays visible.
    The player is the one row that is always present (W8): persona-less, its
    label is the generic word ``Player``.
    """
    from fabulist.state import entities

    build, _ = build_app
    app = build()
    log = app.state.log
    ids = _registry_seed(log)
    assert entities.registry(log, "s1").locations

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/state")

    resp = run(scenario())
    assert resp.status_code == 200
    registry = resp.json()["registry"]
    # No roster row, no persona: the cast half of the map is empty.
    assert registry["beings"] == {"player": "Player"}
    bathroom = next(row for row in registry["locations"] if row["id"] == ids["bath"])
    assert bathroom["occupants"] == ["player"]


def test_entity_door_runs_an_action_and_stamps_the_user_actor(build_app):
    """The ✎ door writes through the tool code path, in the owner's voice."""
    from fabulist.state import entities

    build, _ = build_app
    app = build()
    app.state.log.append_event("s1", "user", "user", {"text": "hi"})

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/entities",
                json={"op": "establish_location", "args": {"name": "bathroom"}},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["op"] == "establish_location"
    rows = [
        event
        for event in app.state.log.get_events("s1")
        if event.payload.get("event") == "entity_establish"
    ]
    assert len(rows) == 1
    assert rows[0].actor == USER_ACTOR
    assert entities.registry(app.state.log, "s1").locations[body["id"]].name == "bathroom"


def test_entity_door_reuses_the_tool_validation(build_app):
    """An out-of-range meter is the SAME readable 400 the model corrects from."""
    build, _ = build_app
    app = build()
    app.state.log.append_event("s1", "user", "user", {"text": "hi"})

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/entities",
                json={"op": "set_attr", "args": {"key": "tension", "value": 42}},
            )

    resp = run(scenario())
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "tension expects an integer in 0–10, got 42"
    assert [
        event
        for event in app.state.log.get_events("s1")
        if event.payload.get("event") == "entity_set"
    ] == []


def test_entity_door_refuses_an_occupied_location_retire(build_app):
    """The occupied-location guard is the writer's own, reached unchanged."""
    from fabulist.state import entities

    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("s1", "user", "user", {"text": "hi"})
    bath = entities.establish_location(log, "s1", "bathroom")
    entities.move_being(log, "s1", entities.PLAYER, bath)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/entities",
                json={"op": "retire_object", "args": {"id": bath}},
            )

    resp = run(scenario())
    assert resp.status_code == 400
    assert "is occupied by player" in resp.json()["detail"]["reason"]


def test_entity_door_rejects_an_unknown_op(build_app):
    build, _ = build_app
    app = build()
    app.state.log.append_event("s1", "user", "user", {"text": "hi"})

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/entities", json={"op": "delete_everything", "args": {}}
            )

    resp = run(scenario())
    assert resp.status_code == 400
    assert "unknown op 'delete_everything'" in resp.json()["detail"]["reason"]
    assert "establish_location" in resp.json()["detail"]["reason"]


def test_entity_door_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/nope/entities",
                json={"op": "establish_location", "args": {"name": "bathroom"}},
            )

    assert run(scenario()).status_code == 404


def test_entity_door_refuses_the_clock_proposal_op(build_app):
    """``propose_time`` is the tracker's tool, not the owner's ✎ door (D45).

    The op is a real write action, so the ONLY thing that refuses it is the
    door's scope (:data:`USER_WRITABLE_TOOLS`); the owner's clock edit stays the
    flat ``POST /state {key: "time"}`` row, and the refusal is the door's own
    unknown-op message — nothing is appended.
    """
    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("s1", "user", "user", {"text": "hi"})
    log.append_event(
        "s1", "state-delta", "extractor", {"time": "08:00", "date": "2025-06-02"}
    )
    before = len(log.get_events("s1"))

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/entities",
                json={
                    "op": "propose_time",
                    "args": {"to": "14:00", "evidence": "the bell rang"},
                },
            )

    resp = run(scenario())
    assert resp.status_code == 400
    reason = resp.json()["detail"]["reason"]
    assert reason.startswith("unknown op 'propose_time' — the registry actions are:")
    assert "set_condition" in reason and "propose_time" not in reason.split(":")[-1]
    assert len(log.get_events("s1")) == before


# -- W8: the session tree ----------------------------------------------------


def test_get_tree_answers_the_projection_with_its_pinned_keys(build_app):
    """``GET /tree`` wraps the pure projection in ``{"tree": ...}``."""
    from fabulist.state import entities
    from fabulist.state.session_tree import TREE_KEYS

    build, _ = build_app
    app = build()
    ids = _registry_seed(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/tree")

    resp = run(scenario())
    assert resp.status_code == 200
    tree = resp.json()["tree"]
    assert tuple(tree) == TREE_KEYS
    assert tree["places"]["tree"][1]["path"] == "upstairs hall > bathroom"
    assert tree["items"][0]["holder"] == "Player"
    # Addressing rides the wire too: the sidebar edits by object id.
    assert tree["places"]["tree"][1]["id"] == ids["bath"]
    assert tree["items"][0]["id"] == ids["item"]
    # This session has no roster, so no member id exists at all — and the one
    # being key the fold knows as a ``cm_`` id ("cm_yara") must still reach the
    # pane as a display label, never as an id in a field a human reads.
    assert "cm_" not in json.dumps(tree)
    assert tree["relations"][0]["to"] == "yara"
    assert entities.registry(app.state.log, "s1").items


def test_get_tree_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/nope/tree")

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


# -- the manual reconciliation door (W9.29 §12 "check state now") -------------


class _ScriptedCaller:
    """ModelCaller scripted as ``(tool_name | None, payload)`` steps (bts shape)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.tails: list[str] = []
        self.profiles: list[dict | None] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.tails.append(str(messages[-1].get("content") or ""))
        self.profiles.append(request_profile)
        name, payload = self.replies.pop(0) if self.replies else (None, "checked.")
        if name is None:
            yield {"type": "content", "delta": payload}
            return
        yield {
            "type": "tool_call",
            "delta": json.dumps(payload),
            "meta": {"name": name},
        }


def _profiled():
    return {"tools": [{"type": "function", "function": {"name": "reconcile"}}]}


def _drift_notices(log, session_id="s1"):
    """The ``validation_notice`` rows carrying a reconciliation flag."""
    return [
        event
        for event in log.get_events(session_id)
        if event.payload.get("event") == "validation_notice"
        and any(
            row.get("check") == "reconciliation_drift"
            for row in event.payload.get("flags") or []
        )
    ]


def test_reconcile_now_files_drift_and_never_writes_the_registry(build_app):
    """The door runs the pass off-turn: one notice per disagreement, no write."""
    from fabulist.state import entities
    from fabulist.state.bts import RECONCILE_DIRECTIVE, BtsRunner
    from fabulist.state.entity_tools import RECONCILE_TOOL

    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("s1", "narrative", "narrator", {"prose": "Yara waits in the hall."})
    thread_id = entities.establish_thread(log, "s1", "the debt")
    narrative_seq = [
        event.seq for event in log.get_events("s1") if event.type == "narrative"
    ][0]

    caller = _ScriptedCaller(
        (
            RECONCILE_TOOL,
            {
                "beliefs": [
                    {"kind": "thread", "name": "the debt", "status": "active"},
                    {"kind": "thread", "name": "the heist", "status": "active"},
                ]
            },
        ),
        (None, "stated what I believe."),
    )
    app.state.world_state = BtsRunner(caller, log=log, profile_fn=_profiled)

    async def scenario():
        async with client_for(app) as client:
            before = (await client.get("/api/sessions/s1/state")).json()["registry"]
            resp = await client.post("/api/sessions/s1/reconcile")
            after = (await client.get("/api/sessions/s1/state")).json()["registry"]
            return before, resp, after

    before, resp, after = run(scenario())

    assert resp.status_code == 200
    body = resp.json()
    assert body["checked"] == 2
    assert "error" not in body
    assert len(body["flags"]) == 1
    flag = body["flags"][0]
    assert flag["check"] == "reconciliation_drift"
    assert flag["severity"] == "warn"
    assert "the heist" in flag["what"]
    assert "the registry holds no such thread" in flag["registry_says"]

    # The row landed through the tool's own append, anchored to the narrative.
    notices = _drift_notices(log)
    assert len(notices) == 1
    assert notices[0].payload["flags"] == [flag]
    assert notices[0].payload["seq"] == narrative_seq
    assert notices[0].actor == "bts"

    # Nothing was written: the registry fold is byte-identical.
    assert after == before
    assert list(entities.registry(log, "s1").threads) == [thread_id]

    # The loop carried ONLY the reconcile tool and the directive was its tail.
    names = [tool["function"]["name"] for tool in caller.profiles[0]["tools"]]
    assert names == [RECONCILE_TOOL]
    assert caller.tails[0] == RECONCILE_DIRECTIVE


def test_reconcile_now_soft_fails_instead_of_500(build_app):
    """A check is advisory: a silent model / no runner is a 200 with the reason."""
    from fabulist.state.bts import BtsRunner
    from fabulist.state.entity_tools import RECONCILE_TOOL

    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("s1", "narrative", "narrator", {"prose": "Yara waits."})
    # The model closes without ever stating a belief: nothing was checked.
    app.state.world_state = BtsRunner(
        _ScriptedCaller((None, "nothing to say.")), log=log, profile_fn=_profiled
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/sessions/s1/reconcile")

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["flags"] == [] and body["checked"] == 0
    assert "beliefs" in body["error"]
    assert _drift_notices(log) == []

    # No BTS runner wired (BTS disabled) is the same soft answer, not a crash.
    app.state.world_state = None

    async def without_runner():
        async with client_for(app) as client:
            return await client.post("/api/sessions/s1/reconcile")

    resp = run(without_runner())
    assert resp.status_code == 200
    assert resp.json()["checked"] == 0
    assert "no BTS runner" in resp.json()["error"]


def test_reconcile_now_404s_an_unknown_session(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/sessions/nope/reconcile")

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


# --- W9.32 §6: the theme endpoint reads the RENDERED state -------------------
#
# ``session_theme`` used to fold the projection itself, which made the palette
# follow whatever flat ``tone`` the (now retired) shadow writes had left behind.
# It overlays the registry now — ``theme/tokens.py`` needed no change: it takes
# the dict it is handed.


def test_theme_returns_the_same_tokens_for_a_legacy_session(build_app):
    """A registry-empty (pre-W9.32) session themes exactly as it always did."""
    from fabulist.theme.tokens import style_tokens, token_style

    build, _ = build_app
    app = build()
    _seed(app.state.log, "s1", {"tone": "tense"})

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/theme")

    resp = run(scenario())
    assert resp.status_code == 200
    tokens = style_tokens({"tone": "tense"})
    assert resp.json() == {"tokens": tokens, "style": token_style(tokens)}


def test_theme_follows_the_registrys_scene_tone_over_a_stale_flat_one(build_app):
    """The registry's scene trait wins, so a stale flat tone cannot theme the UI."""
    from fabulist.state import entities
    from fabulist.theme.tokens import style_tokens, token_style

    build, _ = build_app
    app = build()
    log = app.state.log
    app.state.log.append_event("s1", "user", "user", {"text": "hi"})
    _seed(log, "s1", {"tone": "tense"})
    entities.set_attribute(log, "s1", entities.SCENE, key="tone", value="intimate")

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/theme")

    resp = run(scenario())
    tokens = style_tokens({"tone": "intimate"})
    assert resp.json() == {"tokens": tokens, "style": token_style(tokens)}
