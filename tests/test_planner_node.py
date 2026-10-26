"""W9.20 A1 tests: the planner step (due route, node effects, schema, seeding).

Owner spec under test (``the maintainer's private notes (unshipped)`` §2-§5):

1. due route — turn 1 is mandatory, then every ``cadence`` committed turns, on
   the fork-aware CHAIN (``"plan"`` / ``"skip"`` are the junction's edge values);
2. the node makes ONE model call, appends the plan event, seeds the scene and
   returns the plan's render; every failure is ``plan_refresh_failed`` and never
   fails the turn;
3. seeding — a ``state-delta`` carrying weather/forecast (and time ONLY when the
   chain fold has no time) PLUS re-rendered ``facts`` so the post-commit BTS clock
   re-stamp reads the planned time;
4. ``pre_turn`` renders the folded plan every turn (the lanes' ``beat`` on skip
   turns);
5. the ``plan`` stage frame is index 1 of 8.

Fake models only — no live inference.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.graph.clock import parse_facts, parse_time_of_day
from fabulist.graph.nodes import (
    DEFAULT_PLAN_CADENCE,
    STAGE_NAMES,
    _STAGE_SPECS,
    _STAGE_TOTAL,
    _planner_context,
    committed_turn_count,
    make_npc_lanes_node,
    make_plan_due_route,
    make_planner_node,
    make_pre_turn_node,
    plan_is_due,
)
from fabulist.graph.planner_schema import (
    build_planner_request_profile,
    normalize_field_list,
    planner_tool,
    validate_planner_payload,
)
from fabulist.graph.plans import Plan, load_last_plan, save_plan
from fabulist.state.cast import admit_member

SID = "s1"

STRINGS = [
    "the seal goes missing from the vault",
    "Mara's debt comes due",
    "the archivist stalls the search",
    "the duel is postponed, not resolved",
]


def run(coro):
    return asyncio.run(coro)


def _narrative(log, prose="Rain drums on the roof.", session=SID):
    return log.append_event(
        session,
        "narrative",
        "narrator",
        {"prose": prose, "segments": [{"kind": "prose", "text": prose}]},
    )


def _plan(turn_seq=0, **overrides) -> Plan:
    base = dict(
        turn_seq=turn_seq,
        created_from="graph",
        strings=list(STRINGS),
        datetime="Monday, June 02, 2025 at 19:40",
        weather="light rain",
        forecast="clearing before dawn",
        established=["Mara owes the guild 40 silver"],
    )
    base.update(overrides)
    return Plan(**base)


class _Model:
    """ModelCaller yielding one pre-built reply (content and/or tool args)."""

    def __init__(self, payload=None, *, content=None, tool=False, finish_reason=None, error=None):
        if content is None:
            content = json.dumps(payload)
        self.content = content
        self.tool = tool
        self.finish_reason = finish_reason
        self.error = error
        self.calls = []

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        if self.tool:
            yield {"type": "tool_call", "delta": self.content}
        else:
            yield {"type": "content", "delta": self.content}
        if self.finish_reason:
            yield {
                "type": "error",
                "delta": "truncated",
                "meta": {"finish_reason": self.finish_reason},
            }


def _payload(**overrides):
    payload = {
        "strings": list(STRINGS),
        "datetime": "Monday, June 02, 2025 at 19:40",
        "weather": "light rain",
        "forecast": "clearing before dawn",
        "established": ["Mara owes the guild 40 silver"],
    }
    payload.update(overrides)
    return payload


def _state(log, **overrides):
    state = {
        "session_id": SID,
        "user_content": "I wait under the awning.",
        "narrative_parts": [],
        "facts": "<state>\ntime: 08:00 | date: Monday, June 02, 2025 | day: 1\n</state>",
        "plan": "",
        "action_class": "dialogue",
    }
    state.update(overrides)
    return state


# -- §2 due route ------------------------------------------------------------


def test_turn_one_is_mandatory(log):
    route = make_plan_due_route(log=log)
    assert run(route({"session_id": SID})) == "plan"


def test_committed_plan_skips_within_cadence(log):
    _narrative(log)
    save_plan(log, SID, _plan(turn_seq=1))
    route = make_plan_due_route(log=log)
    assert run(route({"session_id": SID})) == "skip"


def test_cadence_boundary_fires_at_n(log):
    save_plan(log, SID, _plan(turn_seq=0))
    # Four committed turns after the plan: not yet due at cadence 5.
    for _ in range(4):
        _narrative(log)
    route = make_plan_due_route(log=log, cadence=DEFAULT_PLAN_CADENCE)
    assert run(route({"session_id": SID})) == "skip"
    _narrative(log)
    assert committed_turn_count(log, SID) == 5
    assert run(route({"session_id": SID})) == "plan"


def test_cadence_is_live_readable(log):
    """A Studio cadence edit applies without a rebuild (§10 digest note)."""
    save_plan(log, SID, _plan(turn_seq=0))
    for _ in range(2):
        _narrative(log)
    route = make_plan_due_route(log=log, cadence=lambda: 2)
    assert run(route({"session_id": SID})) == "plan"


def test_cadence_is_clamped_to_at_least_one(log):
    """A 0/absent cadence still plans at most once per committed turn."""
    plan = _plan(turn_seq=0)
    assert plan_is_due(last_plan=plan, committed_turns=0, cadence=0) is False
    assert plan_is_due(last_plan=plan, committed_turns=1, cadence=0) is True


def test_route_counts_the_fork_chain(log):
    """One read rule for fold + route + count (§2/M2)."""
    first = _narrative(log)
    fork = log.fork_session(SID, first.seq, "sess-fork")
    for _ in range(5):
        _narrative(log)
    # The parent is due again (5 committed turns since turn_seq 0) …
    assert run(make_plan_due_route(log=log)({"session_id": SID})) == "plan"
    # … while the branch inherits 1 turn and inherits no plan yet.
    assert committed_turn_count(log, fork) == 1
    assert run(make_plan_due_route(log=log)({"session_id": fork})) == "plan"


def test_route_skips_when_the_fork_inherits_a_plan(log):
    _narrative(log)
    plan_event = save_plan(log, SID, _plan(turn_seq=1))
    fork = log.fork_session(SID, plan_event.seq, "sess-fork")
    assert run(make_plan_due_route(log=log)({"session_id": fork})) == "skip"


def test_route_returns_the_junction_edge_values(log):
    """The return values are exactly the planted topology's edge conditions."""
    route = make_plan_due_route(log=log)
    seen = {run(route({"session_id": SID}))}
    _narrative(log)
    save_plan(log, SID, _plan(turn_seq=1))
    seen.add(run(route({"session_id": SID})))
    assert seen == {"plan", "skip"}


# -- §5 the node -------------------------------------------------------------


def _planner(log, model, **kwargs):
    return make_planner_node(log=log, caller=model, **kwargs)


def test_planner_appends_plan_and_returns_its_render(log):
    model = _Model(_payload())
    node = _planner(log, model)
    update = run(node(_state(log)))

    plan = load_last_plan(log, SID)
    assert plan is not None
    assert plan.strings == STRINGS
    assert plan.created_from == "graph"
    assert plan.turn_seq == 0
    assert "STRINGS: " in update["plan"]
    assert len(model.calls) == 1


def test_planner_turn_seq_is_the_committed_count(log):
    for _ in range(3):
        _narrative(log)
    node = _planner(log, _Model(_payload()))
    run(node(_state(log)))
    plan = load_last_plan(log, SID)
    assert plan is not None
    assert plan.turn_seq == 3


def test_planner_sends_directive_context_and_ask(log):
    model = _Model(_payload())
    node = _planner(log, model, directive="PLAN THE STORY.")
    run(node(_state(log)))

    messages = model.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    system, ask = messages[0]["content"], messages[1]["content"]
    assert system.startswith("PLAN THE STORY.")
    assert "CURRENT STATE:" in system
    # Content mode (no profile) states the schema in the system prompt.
    assert "strings" in system
    assert "Set the plan for turn 0" in ask


def test_planner_refresh_carries_existing_strings_and_steer(log):
    save_plan(log, SID, _plan(turn_seq=0, strings=["keep me", "drop me", "s3", "s4"]))
    model = _Model(_payload())
    node = _planner(
        log, model, planner_config=lambda: {"steer": "keep it tense", "fields": []}
    )
    run(node(_state(log)))

    ask = model.calls[0]["messages"][1]["content"]
    assert "Refresh the plan for turn 0" in ask
    assert "- keep me" in ask
    assert "Steer: keep it tense" in ask


def test_planner_is_inert_without_a_caller(log):
    node = make_planner_node(log=log)
    assert run(node(_state(log))) == {}
    assert load_last_plan(log, SID) is None


def test_planner_reads_tool_arguments(log):
    node = _planner(log, _Model(_payload(), tool=True))
    run(node(_state(log)))
    plan = load_last_plan(log, SID)
    assert plan is not None
    assert plan.strings == STRINGS


def test_planner_failure_keeps_the_previous_plan(log):
    save_plan(log, SID, _plan(turn_seq=0, strings=["old one", "old two", "old three", "old four"]))
    node = _planner(log, _Model(error=RuntimeError("model down")))
    update = run(node(_state(log, plan="PLAN (turn 0)\nSTRINGS: old one; old two")))

    assert "plan" not in update  # the pre_turn render keeps steering
    failed = [
        e for e in log.get_events(SID) if e.payload.get("event") == "plan_refresh_failed"
    ]
    assert len(failed) == 1
    assert "model down" in failed[0].payload["error"]
    plan = load_last_plan(log, SID)
    assert plan is not None
    assert plan.strings[0] == "old one"


def test_a_failed_plan_refresh_re_arms_the_due_rule(log):
    """A refresh that writes NO plan event leaves the gap open: the next turn re-plans.

    ``plan_refresh_failed`` is the ONLY row a failed planner run writes, and
    ``load_last_plan`` folds ``plan`` events only — so the newest plan stays the
    old one, ``committed_turns - plan.turn_seq`` keeps growing past the cadence,
    and the following committed turn is due again. That is the historian's retry
    rule (§3.6.3) from the artifact side: a job that never produced its artifact
    is still due, so a failed cadence job retries every turn until it completes.
    """
    plan_event = save_plan(
        log,
        SID,
        _plan(turn_seq=0, strings=["old one", "old two", "old three", "old four"]),
    )
    for _ in range(DEFAULT_PLAN_CADENCE):
        _narrative(log)
    route = make_plan_due_route(log=log, cadence=DEFAULT_PLAN_CADENCE)
    assert run(route({"session_id": SID})) == "plan"  # due at the boundary

    run(_planner(log, _Model(error=RuntimeError("model down")))(_state(log)))

    failed = [
        event
        for event in log.get_events(SID)
        if (event.payload or {}).get("event") == "plan_refresh_failed"
    ]
    assert len(failed) == 1
    assert failed[0].seq > plan_event.seq  # the newest plan marker is the failure
    plans = [
        event
        for event in log.get_events(SID)
        if (event.payload or {}).get("event") == "plan"
    ]
    assert len(plans) == 1  # NO plan event followed the failure
    plan = load_last_plan(log, SID)
    assert plan is not None
    assert (plan.turn_seq, plan.strings[0]) == (0, "old one")  # still the old plan

    # …so the NEXT committed turn is due again, not one cadence later.
    _narrative(log)
    assert (
        plan_is_due(
            last_plan=plan,
            committed_turns=committed_turn_count(log, SID),
            cadence=DEFAULT_PLAN_CADENCE,
        )
        is True
    )
    assert run(route({"session_id": SID})) == "plan"


@pytest.mark.parametrize(
    "payload",
    [
        {"strings": ["only one"]},  # below the 4-string floor
        {"strings": list(STRINGS), "weather": 7},  # wrong core type
        {"strings": list(STRINGS), "bonus": "nope"},  # unknown field
    ],
)
def test_planner_schema_faults_are_best_effort(log, payload):
    node = _planner(log, _Model(payload))
    assert run(node(_state(log))) == {}
    assert load_last_plan(log, SID) is None
    assert [
        e for e in log.get_events(SID) if e.payload.get("event") == "plan_refresh_failed"
    ]


def test_planner_truncated_reply_fails_closed(log):
    node = _planner(log, _Model(_payload(), finish_reason="length"))
    run(node(_state(log)))
    assert load_last_plan(log, SID) is None


# -- §5.4 seeding ------------------------------------------------------------


def _state_deltas(log):
    return [e for e in log.get_events(SID) if e.type == "state-delta"]


def test_planner_seeds_weather_forecast_and_time_on_turn_one(log):
    node = _planner(log, _Model(_payload()))
    update = run(node(_state(log)))

    deltas = _state_deltas(log)
    assert len(deltas) == 1
    assert deltas[0].payload == {
        "weather": "light rain",
        "forecast": "clearing before dawn",
        # W9.32 R4: the plan seeds the FULL opening datetime, not just the hour.
        "time": "19:40",
        "date": "Monday, June 02, 2025",
        "day": "1",
    }
    # The env line convention: weather then forecast on the flat fold.
    assert list(deltas[0].payload)[:2] == ["weather", "forecast"]
    # … and the state facts the BTS clock re-stamp reads carry them too.
    facts = parse_facts(update["facts"])
    assert facts["time"] == "19:40"
    assert facts["date"] == "Monday, June 02, 2025"
    assert facts["day"] == "1"
    assert facts["weather"] == "light rain"
    assert facts["forecast"] == "clearing before dawn"


def test_planner_seeds_the_plans_own_date_and_day_index(log):
    """R4: a plan that opens on a later date seeds THAT date and its day index.

    ``parse_plan_instant`` is the one reader of the free-text ``datetime``, so the
    seeded triple matches what the clock would fold (day is the 1-based days-since-
    epoch number ``advance`` writes, never a weekday index).
    """
    node = _planner(log, _Model(_payload(datetime="Friday, June 27, 2025 at 06:10")))
    update = run(node(_state(log)))

    seeded = _state_deltas(log)[0].payload
    assert (seeded["time"], seeded["date"], seeded["day"]) == (
        "06:10",
        "Friday, June 27, 2025",
        "26",
    )
    facts = parse_facts(update["facts"])
    assert (facts["time"], facts["date"], facts["day"]) == (
        "06:10",
        "Friday, June 27, 2025",
        "26",
    )


def test_planner_never_moves_the_clock_after_seeding(log):
    log.append_event(SID, "state-delta", "bts", {"time": "21:15", "weather": "fog"})
    node = _planner(log, _Model(_payload(datetime="Tuesday, June 03, 2025 at 06:10")))
    update = run(node(_state(log, facts="<state>\ntime: 21:15</state>")))

    deltas = _state_deltas(log)[-1]
    assert "time" not in deltas.payload  # the rules clock owns time (D3)
    # … and the calendar half of the seed is turn-1-only for the same reason.
    assert "date" not in deltas.payload and "day" not in deltas.payload
    assert deltas.payload["weather"] == "light rain"
    assert parse_facts(update["facts"])["time"] == "21:15"


def test_planner_without_scene_fields_seeds_nothing(log):
    payload = _payload(datetime=None, weather=None, forecast=None)
    node = _planner(log, _Model(payload))
    update = run(node(_state(log)))
    assert _state_deltas(log) == []
    assert "facts" not in update


def test_seeded_time_flows_through_the_chain_fold(log):
    """Turn 2's planner read sees the seeded time in the folded chain state."""
    node = _planner(log, _Model(_payload()))
    run(node(_state(log)))
    # A second run must not re-seed time (the fold carries it now).
    run(node(_state(log)))
    seeded = [d for d in _state_deltas(log) if "time" in d.payload]
    assert len(seeded) == 1
    # The date/day seed is the same single write: a second plan seeds neither.
    seeded = [
        d for d in _state_deltas(log) if "date" in d.payload or "day" in d.payload
    ]
    assert len(seeded) == 1


def test_bts_clock_stamp_reads_the_planned_time(log):
    """§5.4/B2: BTS stamps from LIVE state facts, so the plan's clock survives.

    The planner updates ``state["facts"]``; the post-commit BTS pass parses that
    SAME state at bts time, pops any time its model emitted and re-stamps the
    clock's. Without the planner's facts update the turn would be stamped with
    pre_turn's default ``08:00`` instead of the planned ``19:40``.
    """
    from fabulist.graph.nodes import make_bts_node

    class _WorldState:
        async def __call__(self, user_msg, prose, prior_state, checkpoint=False, clock=None):
            return {"kind": "delta", "state": {}, "deltas": {"loc": "vault"}}

    state = _state(log)
    state.update(run(make_pre_turn_node(log=log)(state)))
    assert parse_facts(state["facts"])["time"] == "08:10"  # pre_turn's advance
    state.update(run(_planner(log, _Model(_payload()))(state)))

    node = make_bts_node(extractor=None, log=log, world_state=_WorldState())
    update = run(node(state))
    assert update["deltas"]["time"] == "19:40"
    assert update["deltas"]["loc"] == "vault"


# -- §5.5 pre_turn render ----------------------------------------------------


def test_pre_turn_renders_the_folded_plan(log):
    save_plan(log, SID, _plan(turn_seq=1))
    update = run(make_pre_turn_node(log=log)(_state(log)))
    assert "STRINGS: " in update["plan"]
    assert "the seal goes missing from the vault" in update["plan"]
    # The retired stub text is gone.
    assert "action:" not in update["plan"]
    assert "facts:" not in update["plan"]


def test_pre_turn_plan_is_empty_with_no_plan(log):
    update = run(make_pre_turn_node(log=log)(_state(log)))
    assert update["plan"] == ""


def test_pre_turn_leaves_facts_and_action_class_alone(log):
    update = run(make_pre_turn_node(log=log)(_state(log)))
    assert update["action_class"] == "dialogue"
    assert "time: 08:10" in update["facts"]


def test_lane_beat_is_fed_on_a_skip_turn(log):
    """The lanes' ``beat`` stays non-empty on the 4-of-5 turns the planner skips."""
    member = admit_member(
        log, SID, "Marcus", provenance="card", admitted_by="test"
    )
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {},
            "venue": {"Marcus": {"venue": "tavern", "area": "bar"}},
        },
    )
    save_plan(log, SID, _plan(turn_seq=0))
    _narrative(log)
    state = _state(log)
    state.update(run(make_pre_turn_node(log=log)(state)))
    assert run(make_plan_due_route(log=log)(state)) == "skip"

    lane_state = {"session_id": SID, "plan": state["plan"]}
    fanout = make_npc_lanes_node(
        log=log, build_messages=lambda s: [], caller=_Model(_payload())
    )
    update = run(fanout(lane_state))
    rows = [row for row in update["lane_plan"] if row["member_id"] == member]
    assert rows and rows[0]["beat"].strip()
    assert "STRINGS: " in rows[0]["beat"]


# -- §5.6 stage frames -------------------------------------------------------


def test_plan_stage_is_index_one_of_eight():
    assert STAGE_NAMES[0] == "plan"
    assert _STAGE_SPECS["plan"] == ("Planning the story", 1)
    assert _STAGE_TOTAL == 8
    # W9.24 §3.3.7: the ``regen`` slot is retired (it shared the critic's index
    # 5); the narrator's repair iterations annotate the ``narrate`` slot instead.
    assert [spec[1] for spec in _STAGE_SPECS.values()] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_plan_stage_frame_brackets_the_model_call(log):
    """``_emit_stage`` writes start/done around the call (frame shape pinned)."""
    frames = []

    # The writer is module-level state on the graph's stream context; drive the
    # frame helper directly, then prove the node emits both boundaries by
    # monkeypatching the writer seam.
    import fabulist.graph.nodes as nodes

    original = nodes._stream_writer
    nodes._stream_writer = lambda: frames.append
    try:
        node = _planner(log, _Model(_payload()))
        run(node(_state(log)))
    finally:
        nodes._stream_writer = original

    stages = [f["meta"] for f in frames if f.get("type") == "stage"]
    assert [(m["stage"], m["state"]) for m in stages] == [("plan", "start"), ("plan", "done")]
    assert stages[0]["index"] == 1 and stages[0]["total"] == 8
    assert stages[0]["label"] == "Planning the story"


# -- §4 schema + profile ladder ----------------------------------------------


PRESET_FIELDS = [
    {"id": "currency", "title": "Currency", "type": "string", "hint": "coin on hand"},
    {"id": "reputation", "title": "Reputation", "type": "number", "required": True},
    {"id": "wanted", "title": "Wanted", "type": "boolean"},
]


def test_tool_schema_carries_core_fields_and_extras():
    tool = planner_tool(PRESET_FIELDS)
    assert tool["type"] == "function"
    params = tool["function"]["parameters"]
    props = params["properties"]
    for core in ("strings", "datetime", "weather", "forecast", "established"):
        assert core in props
    assert props["strings"]["minItems"] == 4
    assert props["strings"]["maxItems"] == 6
    assert props["currency"] == {"type": "string", "description": "Currency: coin on hand"}
    assert props["reputation"]["type"] == "number"
    assert params["required"] == ["strings", "reputation"]


def test_tool_schema_is_preset_independent_at_the_core():
    bare = planner_tool([])["function"]["parameters"]
    rich = planner_tool(PRESET_FIELDS)["function"]["parameters"]
    assert set(bare["properties"]) < set(rich["properties"])
    assert bare["required"] == ["strings"]


def test_field_list_rejects_bad_rows():
    for bad in (
        [{"id": "strings", "type": "string"}],  # shadows a core field
        [{"id": "a-b", "type": "string"}],  # not identifier-safe
        [{"id": "x", "type": "string"}, {"id": "x", "type": "string"}],  # duplicate
        [{"id": "x", "type": "list"}],  # unsupported type
    ):
        with pytest.raises(ValueError):
            normalize_field_list(bad)


def test_validate_payload_maps_to_plan():
    plan = validate_planner_payload(
        _payload(currency="40 silver", reputation=2, wanted=True),
        PRESET_FIELDS,
        turn_seq=4,
        created_from="manual",
    )
    assert plan.turn_seq == 4
    assert plan.created_from == "manual"
    assert plan.extras == {"currency": "40 silver", "reputation": 2, "wanted": True}
    assert plan.established == ["Mara owes the guild 40 silver"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"currency": 40},  # string field given a number
        {"reputation": "high"},  # number field given a string
        {"wanted": "yes"},  # boolean field given a string
        {"reputation": True},  # bool is not a number
        {"mystery": 1},  # unknown key
        {"weather": ["rain"]},  # wrong core type
        {"strings": "one line"},  # not an array
    ],
)
def test_validate_payload_rejects_bad_payloads(overrides):
    # ``reputation`` is required, so every case carries a valid value first;
    # the case's own override (when it names the field) replaces it.
    payload = _payload(**{"reputation": 1, **overrides})
    with pytest.raises(ValueError):
        validate_planner_payload(payload, PRESET_FIELDS)


def test_validate_payload_enforces_required_extra():
    payload = _payload()
    payload.pop("reputation", None)
    with pytest.raises(ValueError):
        validate_planner_payload(payload, PRESET_FIELDS)


def test_validate_payload_trims_extra_strings_beyond_six():
    plan = validate_planner_payload(_payload(strings=[f"s{i}" for i in range(9)]))
    assert len(plan.strings) == 6


def test_request_profile_ladder():
    assert build_planner_request_profile({}, []) is None
    assert build_planner_request_profile({"function_calling": False}, []) is None

    auto = build_planner_request_profile({"function_calling": True}, PRESET_FIELDS)
    assert auto is not None
    assert auto["tool_choice"] == "auto"
    assert auto["tools"][0]["function"]["name"] == "plan_scenario"
    assert "currency" in auto["tools"][0]["function"]["parameters"]["properties"]

    forced = build_planner_request_profile(
        {"function_calling": True, "tool_choice_forced": True}, []
    )
    assert forced is not None
    assert forced["tool_choice"] == "required"


def test_profile_is_passed_to_the_call(log):
    """W9.31: the seed phase builds its OWN entity-tool profile; the PLAN call is
    the one that receives the resolved planner (``plan_scenario``) profile.

    The phase rides ``loop_request_profile("function_calling")`` — the entity
    tools' declarations — because the plan tool's schema would be the wrong wire
    there; both calls therefore ride the same resolved binding with different
    tool sets.
    """
    model = _Model(content="done")  # closes the phase cleanly; the plan shape fails
    profile = {"tools": [planner_tool([])], "tool_choice": "auto"}
    node = _planner(log, model, request_profile_fn=lambda: profile)
    run(node(_state(log)))

    # Issue #82: the seed phase, then the plan call AND its two PARSE-class
    # retries — a malformed reply is retried now, so a failing plan phase is
    # three calls, not one.
    assert len(model.calls) == 4  # the seed phase, then the plan (+2 retries)
    phase_profile = model.calls[0]["kwargs"]["request_profile"]
    assert phase_profile["tool_choice"] == "auto"
    assert phase_profile != profile
    assert model.calls[-1]["kwargs"]["request_profile"] == profile


def test_content_mode_states_the_schema_in_the_prompt(log):
    model = _Model(_payload())
    node = _planner(log, model, request_profile_fn=lambda: None)
    run(node(_state(log)))
    assert "request_profile" not in model.calls[0]["kwargs"]
    assert "matching this schema" in model.calls[0]["messages"][0]["content"]


# -- §4/§3 clock parsing -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Monday, June 02, 2025 at 19:40", "19:40"),
        ("2025-06-02 06:05", "06:05"),
        ("9:15 in the morning", "09:15"),
        ("evening", None),
        (None, None),
        ("24:00", None),
    ],
)
def test_parse_time_of_day(text, expected):
    assert parse_time_of_day(text) == expected


# -- the planner's WORLD part + macro resolution -----------------------------
#
# The defect: a card-style world keeps the whole setting in the world's
# ``description`` ("{{char}} is a three story timber inn"), has no cast and
# an empty scenario. The planner's only world-ish read was the session's card
# events, so it saw NOTHING and invented a metropolitan commute. The WORLD part
# comes from the same live builder as the narrator's ``### World`` prefix.

INN = "The Crimson Lantern"
INN_DESCRIPTION = "{{char}} is a three story timber inn by the harbour."


def _world_session(log, *, description=INN_DESCRIPTION, cards=()):
    """A world-materialized session: a ``__worlds__`` definition + its snapshot.

    ``cards`` seeds cast card events (a world may have none — that IS the
    defect case). Returns the world id.
    """
    from fabulist.worlds.registry import append_world

    world_id = append_world(log, {"name": INN, "description": description})
    log.append_event(
        SID,
        "system",
        "user",
        {"event": "world_ref", "world_id": world_id, "name": INN, "scenario": ""},
    )
    for card in cards:
        log.append_event(SID, "system", "system", {"card": card})
    return world_id


def test_planner_context_carries_the_world_definition(log):
    """A cast-less world chat still hands the planner its setting."""
    _world_session(log)

    context = _planner_context(log, SID, _state(log))
    assert "WORLD:\n### World" in context
    assert INN in context
    # The description IS the scenario; ``{{char}}`` falls back to the world name.
    assert f"{INN} is a three story timber inn" in context
    # No cards -> no cast part at all, so the WORLD part is the only world view.
    assert "CAST AND WORLD:" not in context


def test_planner_context_renders_card_and_world_macros(log):
    """D43: the planner's card block and world block both resolve macros.

    Without the map the planner read the card's literal ``{{char}}`` / ``{{user}}``
    — the same unresolved-token defect the narrator path fixed.
    """
    _world_session(
        log,
        cards=[
            {
                "name": "Seraphine",
                "description": "{{char}} greets {{user}} at the door.",
            }
        ],
    )

    context = _planner_context(log, SID, _state(log))
    assert "CAST AND WORLD:" in context
    assert "[cast: Seraphine] greets user at the door." in context
    assert "{{char}}" not in context and "{{user}}" not in context
    assert f"WORLD:\n### World\n{INN}\nDescription: " in context


def test_planner_context_world_part_is_absent_for_a_plain_chat(log):
    """No ``world_ref`` -> the WORLD part contributes nothing (byte-identical)."""
    log.append_event(SID, "system", "system", {"card": {"name": "Lyra Voss"}})

    context = _planner_context(log, SID, _state(log))
    assert "WORLD:\n### World" not in context
    assert "CAST AND WORLD:" in context


def test_planner_sends_the_world_definition(log):
    """End-to-end through the node: the model call carries the world's material."""
    _world_session(log)
    model = _Model(_payload())
    run(_planner(log, model)(_state(log)))

    system = model.calls[0]["messages"][0]["content"]
    assert "WORLD:\n### World" in system
    assert "three story timber inn" in system
