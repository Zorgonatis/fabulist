"""Lane 1f — env HUD event assembler + read route.

The owner fork decision is FINAL: the env HUD travels as an OpenUI DSL
*statement*, engine-assembled, never typed JSON. These tests pin the fixed
statement contract byte-for-byte, the string-literal rule (the statement must
survive the frontend tokenizer's ``JSON.parse`` path), the forecast fallback
ladder, the tolerant prior-statement back-parser, fork-aware latest-wins reads,
and the legacy fold degradation for sessions that predate the HUD event.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from fabulist.api.hud_api import router as hud_router
from fabulist.log import EventLog
from fabulist.state.env_hud import (
    ENV_HUD_EVENT,
    env_hud_statement,
    env_hud_statement_from_fold,
    latest_env_hud,
    parse_env_hud_statement,
)

#: The canonical fixed-contract statement (time, date, loc, weather, forecast).
CONTRACT = (
    'hud = EnvTracker("14:35", "Monday, June 02, 2025", "night market", '
    '"light rain", "clearing")'
)

#: The clock facts fold shape (Lane 1a: time/loc/weather/tone/inv + date/day).
CLOCK_FACTS = {
    "time": "14:35",
    "loc": "night market",
    "weather": "light rain",
    "tone": "wary",
    "date": "Monday, June 02, 2025",
    "day": "1",
}


class _FakeBts:
    """Stand-in for ``BtsResult`` (Lane 1b: ``forecast: str = ""``)."""

    def __init__(self, forecast: str) -> None:
        self.forecast = forecast


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Fixed contract + serializer
# --------------------------------------------------------------------------


def test_statement_matches_fixed_contract_byte_for_byte():
    stmt = env_hud_statement(CLOCK_FACTS, _FakeBts("clearing"), None)
    assert stmt == CONTRACT


def test_forecast_steady_when_no_bts_or_prior():
    stmt = env_hud_statement(CLOCK_FACTS, None, None)
    assert stmt == (
        'hud = EnvTracker("14:35", "Monday, June 02, 2025", "night market", '
        '"light rain", "light rain")'
    )


def test_empty_values_are_empty_strings_never_omitted():
    # Five slots always present; the frontend component degrades gracefully.
    assert env_hud_statement({}, None, None) == 'hud = EnvTracker("", "", "", "", "")'


def test_serializer_escapes_quotes_backslashes_and_newlines_roundtrip():
    loc = 'the "Rusty" Anchor\\cove'
    weather = "squall"
    stmt = env_hud_statement({**CLOCK_FACTS, "loc": loc, "weather": weather}, None, None)
    # Literal JSON escapes — the frontend tokenizer runs JSON.parse on the slice.
    assert '\\"' in stmt
    assert "\\\\" in stmt
    parsed = parse_env_hud_statement(stmt)
    assert parsed == {
        "time": "14:35",
        "date": "Monday, June 02, 2025",
        "loc": loc,
        "weather": weather,
        "forecast": weather,  # steady fallback
    }

    # The two PROSE slots are whitespace-normalised (see ``HUD_PHRASE_MAX``): a raw
    # newline in a single-line chip is a layout break, so it becomes a space — the
    # escaping guarantee below still holds for the values that keep their shape.
    newline_stmt = env_hud_statement(
        {**CLOCK_FACTS, "weather": "rain\nturning to sleet"}, None, None
    )
    assert "\\n" not in newline_stmt
    assert parse_env_hud_statement(newline_stmt)["weather"] == "rain turning to sleet"


# --------------------------------------------------------------------------
# Forecast ladder: BTS -> prior statement -> steady weather
# --------------------------------------------------------------------------


def test_ladder_bts_forecast_wins():
    prior = (
        'hud = EnvTracker("14:35", "Monday, June 02, 2025", "night market", '
        '"light rain", "storm")'
    )
    parsed = parse_env_hud_statement(
        env_hud_statement(CLOCK_FACTS, _FakeBts("clearing"), prior)
    )
    assert parsed is not None and parsed["forecast"] == "clearing"


def test_ladder_prior_forecast_when_bts_empty():
    prior = (
        'hud = EnvTracker("14:35", "Monday, June 02, 2025", "night market", '
        '"light rain", "storm")'
    )
    parsed = parse_env_hud_statement(env_hud_statement(CLOCK_FACTS, _FakeBts(""), prior))
    assert parsed is not None and parsed["forecast"] == "storm"


def test_ladder_steady_weather_when_bts_and_prior_empty():
    parsed = parse_env_hud_statement(env_hud_statement(CLOCK_FACTS, _FakeBts(""), "garbage"))
    assert parsed is not None and parsed["forecast"] == "light rain"


def test_bts_forecast_getattr_tolerant_missing_attribute():
    # 1b may not have landed: an object with no `forecast` attr must not raise.
    parsed = parse_env_hud_statement(env_hud_statement(CLOCK_FACTS, object(), None))
    assert parsed is not None and parsed["forecast"] == "light rain"


def test_bts_forecast_reads_dict_form():
    parsed = parse_env_hud_statement(
        env_hud_statement(CLOCK_FACTS, {"forecast": "clearing"}, None)
    )
    assert parsed is not None and parsed["forecast"] == "clearing"


# --------------------------------------------------------------------------
# D45: manual override is the first rung
# --------------------------------------------------------------------------
#
# The user-pinned forecast lives in the flat fold (a human `state-delta`
# `{"forecast": value}` row, `api/state_api.py`), so it reaches this pure
# function through the `clock_facts` mapping it already receives — no extra
# parameter. An absent or empty value is no override (D33 absent != empty).

_PRIOR = (
    'hud = EnvTracker("14:35", "Monday, June 02, 2025", "night market", '
    '"light rain", "storm")'
)


def test_ladder_manual_override_wins_over_bts_and_prior():
    facts = {**CLOCK_FACTS, "forecast": "heat wave"}
    parsed = parse_env_hud_statement(env_hud_statement(facts, _FakeBts("clearing"), _PRIOR))
    assert parsed is not None and parsed["forecast"] == "heat wave"


def test_ladder_manual_override_wins_when_bts_empty():
    facts = {**CLOCK_FACTS, "forecast": "heat wave"}
    parsed = parse_env_hud_statement(env_hud_statement(facts, _FakeBts(""), _PRIOR))
    assert parsed is not None and parsed["forecast"] == "heat wave"


def test_ladder_empty_override_falls_through_to_bts():
    # D33: an explicit empty string is no override, not an empty chip.
    facts = {**CLOCK_FACTS, "forecast": ""}
    parsed = parse_env_hud_statement(env_hud_statement(facts, _FakeBts("clearing"), _PRIOR))
    assert parsed is not None and parsed["forecast"] == "clearing"


def test_ladder_without_override_is_unchanged():
    # Absent override: byte-identical to the pre-D45 three-rung ladder.
    assert env_hud_statement(CLOCK_FACTS, _FakeBts("clearing"), _PRIOR) == env_hud_statement(
        {**CLOCK_FACTS, "forecast": ""}, _FakeBts("clearing"), _PRIOR
    )


# --------------------------------------------------------------------------
# Tolerant back-parser
# --------------------------------------------------------------------------


def test_parse_roundtrip_preserves_ident_agnostic_call():
    parsed = parse_env_hud_statement('station = EnvTracker("a", "b", "c", "d", "e")')
    assert parsed == {"time": "a", "date": "b", "loc": "c", "weather": "d", "forecast": "e"}


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        "",
        "   ",
        "not a statement at all",
        'root = ScenePane("title", [])',
        'hud = EnvTracker("a", "b", "c", "d")',  # too few args
        'hud = EnvTracker("a", "b", "c", "d",',  # unterminated
        "hud = EnvTracker(1, 2, 3, 4, 5)",  # non-string args
    ],
)
def test_parse_garbage_returns_none(garbage):
    # A malformed statement never raises; the ladder simply skips the prior rung.
    assert parse_env_hud_statement(garbage) is None


def test_parse_tolerates_trailing_extra_args():
    parsed = parse_env_hud_statement('hud = EnvTracker("a", "b", "c", "d", "e", "f")')
    assert parsed == {"time": "a", "date": "b", "loc": "c", "weather": "d", "forecast": "e"}


# --------------------------------------------------------------------------
# Legacy fold degradation
# --------------------------------------------------------------------------


def test_legacy_fold_degrades_to_reduced_station():
    fold = {"time": "14:35", "loc": "night market", "weather": "light rain", "tone": "wary"}
    stmt = env_hud_statement_from_fold(fold)
    assert stmt == 'hud = EnvTracker("14:35", "", "night market", "light rain", "")'


def test_legacy_fold_empty_still_renders_five_slots():
    assert env_hud_statement_from_fold({}) == 'hud = EnvTracker("", "", "", "", "")'


# --------------------------------------------------------------------------
# Fork-aware latest-wins reads
# --------------------------------------------------------------------------


def _seed_hud(log: EventLog, session_id: str, statement: str) -> None:
    log.append_event(
        session_id,
        "system",
        "user",
        {"event": ENV_HUD_EVENT, "statement": statement},
    )


def test_latest_env_hud_is_none_when_absent(log):
    log.append_event("s1", "narrative", "narrator", {"prose": "nothing here"})
    assert latest_env_hud(log, "s1") is None


def test_latest_env_hud_latest_wins_and_fork_aware(log):
    early = 'hud = EnvTracker("08:00", "Monday, June 02, 2025", "camp", "fog", "fog")'
    child_stmt = 'hud = EnvTracker("09:00", "Monday, June 02, 2025", "road", "rain", "rain")'
    parent_late = 'hud = EnvTracker("10:00", "Monday, June 02, 2025", "gate", "sun", "sun")'

    log.append_event("s1", "user", "user", {"text": "hi"})
    _seed_hud(log, "s1", early)
    log.fork_session("s1", at_seq=2, new_session_id="s1b")

    # The branch inherits the parent's HUD up to the fork point.
    assert latest_env_hud(log, "s1b") == early

    # A later parent write must NOT leak into the branch (bounded at base_seq).
    _seed_hud(log, "s1", parent_late)
    assert latest_env_hud(log, "s1b") == early
    assert latest_env_hud(log, "s1") == parent_late

    # The branch's own write is latest for the branch.
    _seed_hud(log, "s1b", child_stmt)
    assert latest_env_hud(log, "s1b") == child_stmt
    assert latest_env_hud(log, "s1") == parent_late


# --------------------------------------------------------------------------
# Read route (minimal app — the orchestrator owns app.py wiring)
# --------------------------------------------------------------------------


def _client_for(log: EventLog) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(hud_router)
    app.state.log = log
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_hud_route_returns_latest_statement(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        _seed_hud(log, "s1", 'hud = EnvTracker("08:00", "Monday, June 02, 2025", "camp", "fog", "fog")')
        _seed_hud(log, "s1", CONTRACT)

        async def scenario():
            async with _client_for(log) as client:
                return await client.get("/api/sessions/s1/hud")

        resp = run(scenario())
        assert resp.status_code == 200
        assert resp.json() == {"statement": CONTRACT}
    finally:
        log.close()


def test_hud_route_degrades_legacy_fold(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "narrative", "narrator", {"prose": "hello"})
        log.append_event(
            "s1",
            "state-delta",
            "bts",
            {"time": "14:35", "loc": "night market", "weather": "light rain"},
        )

        async def scenario():
            async with _client_for(log) as client:
                return await client.get("/api/sessions/s1/hud")

        resp = run(scenario())
        assert resp.status_code == 200
        assert resp.json() == {
            "statement": 'hud = EnvTracker("14:35", "", "night market", "light rain", "")'
        }
    finally:
        log.close()


def test_hud_route_404_unknown_session(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:

        async def scenario():
            async with _client_for(log) as client:
                return await client.get("/api/sessions/nope/hud")

        assert run(scenario()).status_code == 404
    finally:
        log.close()


# --------------------------------------------------------------------------
# W9.29 §8 — the env_hud node reads the registry
# --------------------------------------------------------------------------
#
# The statement shape is untouched (five positional args, forever). What changed
# is where three slots come from: `loc` is the player's canonical path, weather/
# tone are scene traits, and the forecast ladder's rung 2 is the registry's scene
# trait passed in the retired `bts_result` slot — while rung 1 stays the D45
# manual pin, which `flat_render` deliberately omits so nothing can clobber it.


def _hud_from_node(log: EventLog, session_id: str = "s1") -> dict[str, str]:
    """Run the real env_hud node and back-parse the statement it appended."""
    from fabulist.graph.nodes import make_env_hud_node

    node = make_env_hud_node(log=log)
    run(node({"session_id": session_id}))
    latest = [
        event
        for event in log.get_events(session_id)
        if event.type == "system"
        and isinstance(event.payload, dict)
        and event.payload.get("event") == ENV_HUD_EVENT
    ][-1]
    parsed = parse_env_hud_statement(latest.payload["statement"])
    assert parsed is not None
    return parsed


def _room(log: EventLog, name: str, *, parent: str | None = None) -> str:
    from fabulist.state import entities

    return entities.establish_location(log, "s1", name, parent_id=parent)


def _trait(log: EventLog, key: str, value: str) -> None:
    from fabulist.state import entities

    entities.set_attribute(log, "s1", entities.SCENE, key=key, value=value)


def test_node_renders_the_players_canonical_path(tmp_path):
    """A parent chain renders `upstairs hall > bathroom`, not the leaf alone."""
    from fabulist.state import entities

    log = EventLog(tmp_path / "events.db")
    try:
        hall = _room(log, "upstairs hall")
        bath = _room(log, "bathroom", parent=hall)
        entities.move_being(log, "s1", entities.PLAYER, bath)

        parsed = _hud_from_node(log)

        assert parsed["loc"] == "upstairs hall > bathroom"
    finally:
        log.close()


def test_node_keeps_the_flat_fold_loc_without_an_assignment(tmp_path):
    """No registry placement ⇒ today's flat-fold value, unchanged."""
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "state-delta", "bts", {"loc": "night market"})

        parsed = _hud_from_node(log)

        assert parsed["loc"] == "night market"
    finally:
        log.close()


def test_node_reads_weather_from_the_scene_traits(tmp_path):
    """The scene trait (not a stale fold) is the registry's weather answer.

    ``tone`` rides the same merged facts dict but the station has no tone slot
    (five fixed args), so only the weather slot is asserted here.
    """
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "state-delta", "bts", {"weather": "hail"})
        _trait(log, "weather", "fine drizzle")
        _trait(log, "tone", "intimate")

        parsed = _hud_from_node(log)

        assert parsed["weather"] == "fine drizzle"
        assert set(parsed) == {"time", "date", "loc", "weather", "forecast"}
    finally:
        log.close()


def test_node_forecast_rung_two_is_the_scene_trait(tmp_path):
    """With no manual pin, the tracker's own trait supplies the outlook."""
    log = EventLog(tmp_path / "events.db")
    try:
        _trait(log, "weather", "light rain")
        _trait(log, "forecast", "clearing by dusk")

        parsed = _hud_from_node(log)

        assert parsed["forecast"] == "clearing by dusk"
    finally:
        log.close()


def test_node_forecast_manual_pin_wins_over_the_scene_trait(tmp_path):
    """Rung 1 is the user's pin and nothing the tracker writes may overwrite it."""
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "state-delta", "user", {"forecast": "pinned: hard frost"})
        _trait(log, "weather", "light rain")
        _trait(log, "forecast", "clearing by dusk")

        parsed = _hud_from_node(log)

        assert parsed["forecast"] == "pinned: hard frost"
    finally:
        log.close()


# --------------------------------------------------------------------------
# W9.32 W6a — the epic's new state does NOT grow the HUD
# --------------------------------------------------------------------------
#
# §3.6 is an exposure DECISION: the EnvTracker statement is a fixed five-arg
# frontend contract, and conditions/bodies/economy/the scene-hosted genre surface
# in the sidebar tree and the palette instead. The two clock slots keep reading
# the folded clock keys, so an accepted time proposal (which lands in that same
# fold) needs no HUD change either — the station simply reads the new instant.


def test_new_state_kinds_do_not_grow_the_statement(tmp_path):
    from fabulist.state import entities

    log = EventLog(tmp_path / "events.db")
    try:
        # The fold an accepted proposal leaves behind (W6a's stamp) …
        log.append_event(
            "s1",
            "state-delta",
            "bts",
            {
                "time": "21:30",
                "date": "Monday, June 02, 2025",
                "day": "1",
                "weather": "light rain",
            },
        )
        # … plus the state kinds this epic adds (R5, R8).
        entities.set_condition(
            log, "s1", entities.PLAYER, "suspicious", expires_at="2025-06-02 23:00"
        )
        entities.set_attribute(
            log, "s1", entities.SCENE, key="genre", value="urban fantasy"
        )

        parsed = _hud_from_node(log)

        assert set(parsed) == {"time", "date", "loc", "weather", "forecast"}
        assert (parsed["time"], parsed["date"]) == ("21:30", "Monday, June 02, 2025")
    finally:
        log.close()
