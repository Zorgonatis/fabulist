"""Tests for the W9.20 plan object (the planner's steering plane, §3).

Plans are log events (D9), so every case runs against the real
:class:`~fabulist.log.EventLog` on a tmp SQLite file (the ``log`` fixture from
``conftest.py``). The compiler ``plan=`` seam and the NPC lanes' ``beat`` consume
:func:`render_plan_for_prompt`.
"""

from __future__ import annotations

import pytest

from fabulist.graph.plans import (
    MAX_PLAN_CHARS,
    Plan,
    load_last_plan,
    plans_differ,
    render_plan_for_prompt,
    save_plan,
)

SESSION = "sess-1"


def make_plan(**overrides) -> Plan:
    base = dict(
        turn_seq=3,
        created_from="graph",
        strings=[
            "the seal goes missing from the vault",
            "Mara's debt comes due",
            "the duel is postponed, not resolved",
            "the archivist knows more than she says",
        ],
        datetime="Monday, June 02, 2025 at 19:40",
        weather="light rain",
        forecast="clearing before dawn",
        established=["Mara owes the guild 40 silver", "the vault has one key"],
        extras={"currency": "40 silver", "reputation": 2},
    )
    base.update(overrides)
    return Plan(**base)


# -- save / load -------------------------------------------------------------


def test_save_load_round_trip(log):
    plan = make_plan()
    event = save_plan(log, SESSION, plan)

    # ONE system event, actor ``planner``, carrying the plan payload.
    assert event.type == "system"
    assert event.actor == "planner"
    assert event.payload["event"] == "plan"
    rows = [e for e in log.get_events(SESSION) if e.type == "system"]
    assert len(rows) == 1

    loaded = load_last_plan(log, SESSION)
    assert loaded == plan
    assert loaded is not None
    assert loaded.strings == plan.strings
    assert loaded.datetime == plan.datetime
    assert loaded.weather == plan.weather
    assert loaded.forecast == plan.forecast
    assert loaded.established == plan.established
    assert loaded.extras == plan.extras
    assert loaded.created_from == "graph"


def test_manual_provenance_round_trips(log):
    save_plan(log, SESSION, make_plan(created_from="manual"))
    loaded = load_last_plan(log, SESSION)
    assert loaded is not None
    assert loaded.created_from == "manual"


def test_latest_wins_with_two_saves(log):
    save_plan(log, SESSION, make_plan(turn_seq=1, strings=["a1", "a2", "a3", "a4"]))
    second = make_plan(turn_seq=2, strings=["b1", "b2", "b3", "b4"])
    save_plan(log, SESSION, second)

    loaded = load_last_plan(log, SESSION)
    assert loaded is not None
    assert loaded.turn_seq == 2
    assert loaded.strings == ["b1", "b2", "b3", "b4"]


def test_ephemeral_plan_ignored(log):
    # A committed plan, then an ephemeral (abandoned branch) plan on top of it.
    save_plan(log, SESSION, make_plan(turn_seq=5, strings=["c1", "c2", "c3", "c4"]))
    log.append_event(
        SESSION,
        "system",
        "planner",
        {"event": "plan", "plan": make_plan(turn_seq=6, strings=["ghost"]).to_dict()},
        ephemeral=True,
    )

    loaded = load_last_plan(log, SESSION)
    assert loaded is not None
    assert loaded.turn_seq == 5
    assert loaded.strings == ["c1", "c2", "c3", "c4"]


def test_ephemeral_only_yields_none(log):
    log.append_event(
        SESSION,
        "system",
        "planner",
        {"event": "plan", "plan": make_plan().to_dict()},
        ephemeral=True,
    )
    assert load_last_plan(log, SESSION) is None


def test_load_from_event_list_empty_and_other_sessions(log):
    assert load_last_plan([], SESSION) is None

    save_plan(log, "other-session", make_plan())
    other_events = log.get_events("other-session")
    assert load_last_plan(other_events, SESSION) is None
    assert load_last_plan(other_events, "other-session") is not None


def test_load_accepts_event_iterable(log):
    save_plan(log, SESSION, make_plan(turn_seq=4))
    loaded = load_last_plan(log.get_events(SESSION), SESSION)
    assert loaded is not None
    assert loaded.turn_seq == 4


def test_non_plan_system_events_ignored(log):
    log.append_event(SESSION, "system", "engine", {"event": "error", "text": "boom"})
    save_plan(log, SESSION, make_plan(turn_seq=2))
    assert load_last_plan(log, SESSION).turn_seq == 2  # type: ignore[union-attr]


# -- legacy payloads (§3/M3) -------------------------------------------------


LEGACY_PLAN = {
    "turn_seq": 7,
    "beats": ["open on the gate"],
    "blocking": "the duel is NOT resolved this turn",
    "open_threads": ["who stole the seal"],
    "created_from": "pre_turn",
}


def test_legacy_payload_folds_as_absent(log):
    """A beats/blocking/threads plan (no ``strings``) is ABSENT, not translated.

    Accepted consequence (§3/M3): every existing session re-plans on its next
    turn — mandatory, exactly like turn 1.
    """
    log.append_event(
        SESSION, "system", "planner", {"event": "plan", "plan": dict(LEGACY_PLAN)}
    )
    assert load_last_plan(log, SESSION) is None


def test_legacy_payload_does_not_mask_a_newer_valid_plan(log):
    log.append_event(
        SESSION, "system", "planner", {"event": "plan", "plan": dict(LEGACY_PLAN)}
    )
    save_plan(log, SESSION, make_plan(turn_seq=8))
    loaded = load_last_plan(log, SESSION)
    assert loaded is not None
    assert loaded.turn_seq == 8


def test_from_dict_rejects_legacy_shape():
    from fabulist.graph.plans import Plan as PlanType

    with pytest.raises(ValueError):
        PlanType.from_dict(dict(LEGACY_PLAN))


def test_save_rejects_unknown_created_from(log):
    for bogus in ("pre_turn", "bogus", ""):
        with pytest.raises(ValueError):
            save_plan(log, SESSION, make_plan(created_from=bogus))


# -- fork discipline (§2/M2) -------------------------------------------------


def test_fold_reads_the_fork_chain(log):
    """A fork INHERITS its parent's plan (§2/M2 — deliberate behavior change)."""
    event = save_plan(log, SESSION, make_plan(turn_seq=2))
    fork = log.fork_session(SESSION, event.seq, "sess-fork")

    # The branch owns no plan row of its own …
    assert [e for e in log.get_events(fork) if e.type == "system"] == []
    # … yet folds the parent's plan, because the read walks the CHAIN.
    inherited = load_last_plan(log, fork)
    assert inherited is not None
    assert inherited.turn_seq == 2


# -- render ------------------------------------------------------------------


def test_render_carries_strings_scene_and_established():
    text = render_plan_for_prompt(make_plan())
    assert "PLAN (turn 3)" in text
    assert "STRINGS: " in text
    assert "the seal goes missing from the vault" in text
    # 4 strings render joined in priority order.
    assert text.index("the seal goes missing") < text.index("the archivist knows")
    assert "WHEN: Monday, June 02, 2025 at 19:40" in text
    assert "WEATHER: light rain (then clearing before dawn)" in text
    assert "ESTABLISHED: Mara owes the guild 40 silver" in text
    assert len(text) <= MAX_PLAN_CHARS


def test_render_omits_unset_fields():
    plan = make_plan(
        datetime=None, weather=None, forecast=None, established=[], extras={}
    )
    text = render_plan_for_prompt(plan)
    assert "WHEN:" not in text
    assert "WEATHER:" not in text
    assert "FORECAST:" not in text
    assert "ESTABLISHED:" not in text
    assert "STRINGS: " in text


def test_render_extras_as_lines():
    text = render_plan_for_prompt(make_plan())
    assert "CURRENCY: 40 silver" in text
    assert "REPUTATION: 2" in text


def test_render_boolean_extra_reads_yes_no():
    text = render_plan_for_prompt(make_plan(extras={"wanted": True, "hunted": False}))
    assert "WANTED: yes" in text
    assert "HUNTED: no" in text


def test_render_forecast_without_weather():
    text = render_plan_for_prompt(make_plan(weather=None))
    assert "FORECAST: clearing before dawn" in text


def test_render_respects_char_cap():
    plan = make_plan(
        strings=[f"string number {i} with a good deal of padding text" for i in range(6)],
        established=[f"established fact {i} with padding" for i in range(20)],
        extras={"journal": "x" * 400},
    )
    text = render_plan_for_prompt(plan)
    assert len(text) <= MAX_PLAN_CHARS
    # The forward strings are the steering content and survive truncation.
    assert "STRINGS:" in text


# -- drift -------------------------------------------------------------------


def test_plans_differ_on_string_reorder_is_false():
    a = make_plan(strings=["one", "two", "three", "four"])
    b = make_plan(strings=["four", "three", "one", "two"])
    assert plans_differ(a, b) is False


def test_plans_differ_on_different_string_set_is_true():
    assert plans_differ(
        make_plan(strings=["one", "two", "three", "four"]),
        make_plan(strings=["one", "two", "three", "five"]),
    )


def test_plans_differ_on_scene_field_change_is_true():
    assert plans_differ(make_plan(weather="rain"), make_plan(weather="snow")) is True
    assert plans_differ(make_plan(weather=None), make_plan(weather="")) is False


def test_plans_differ_handles_none():
    assert plans_differ(None, None) is False
    assert plans_differ(make_plan(), None) is True
    assert plans_differ(None, make_plan()) is True
