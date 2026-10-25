"""W9.31 W1: the planner's bootstrap / repair tool phase (§3, §4, §6, §10 W1).

Owner requirement under test: the FIRST planner run of a session initializes the
object stores (places first, the world never forced to be a location), and every
LATER run keeps the tools but may only REPAIR — never re-seed. The phase is a
bounded ``run_agent_loop`` over the tracker's own tool surface, run before the
unchanged plan call, contained in its OWN try so a failed seed never costs the
plan (§6/S2).

The pins, in the plan's order:

1. the mode discriminator — bootstrap iff (function calling AND no prior plan),
   repair otherwise, content mode skipping the phase entirely (§4/D1/D6);
2. the tool sets — bootstrap = the tracker's three reads + fourteen writes (the
   whole surface minus ``propose_time``: no prose exists at seed time to evidence
   a clock move), repair = the three reads + the nine mutating tools, no
   ``establish_*`` (nor ``establish_economy``, nor ``propose_time``) (§3.1/D4,
   W9.32 §3.3);
3. the tail — the protocol + palette ride a FINAL system message after the ask
   (AGENTS 6 / D2), never the seeded prefix;
4. ``ensure_roster`` FIRST (D9a) — a fresh chat has no seed admits yet and the
   planner runs before ``npc_lanes``, so without it a card character is
   unmovable and a spurious cast proposal is filed;
5. idempotency — a duplicate establish is refused WITH the existing id and the
   plan still saves (§4);
6. containment — a failed bootstrap/repair appends ``plan_refresh_failed`` with
   ``stage`` and the plan still runs (§6);
7. the budget — ``EntityTurn(budget=N)`` states N; the default text is
   byte-identical to :data:`ENTITY_BUDGET_ERROR` (§3.4); the planner's own
   numbers stay 8/4 with the optional seed steps spending from the same budget
   (W9.32 §7 W6b).

Fake models only — no live inference.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.graph.nodes import _planner_context, _world_objects_block, make_planner_node
from fabulist.graph.planner_schema import planner_tool
from fabulist.graph.plans import Plan, load_last_plan, save_plan
from fabulist.state.bts import (
    FOLDED_STATE_TOOL,
    RECENT_DELTAS_TOOL,
    ROSTER_TOOL,
    bts_loop_tools,
    registry_palette,
)
from fabulist.state.entity_tools import (
    ADJUST_ECONOMY_TOOL,
    ENTITY_ACTION_BUDGET,
    ENTITY_BUDGET_ERROR,
    ESTABLISH_ECONOMY_TOOL,
    ESTABLISH_LOCATION_TOOL,
    ESTABLISH_THREAD_TOOL,
    MOVE_BEING_TOOL,
    PROPOSE_TIME_TOOL,
    SET_BODY_TOOL,
    SET_CONDITION_TOOL,
    WRITE_TOOL_NAMES,
    EntityTurn,
    budget_error,
)
from fabulist.state.planner_bootstrap import (
    BOOTSTRAP,
    BOOTSTRAP_DIRECTIVE,
    PLANNER_BOOTSTRAP_BUDGET,
    PLANNER_BOOTSTRAP_TOOLS,
    PLANNER_REPAIR_BUDGET,
    REPAIR,
    REPAIR_DIRECTIVE,
    REPAIR_TOOL_NAMES,
    PlannerBootstrapError,
    planner_bootstrap_tools,
    planner_repair_tools,
    run_planner_tool_phase,
)

SID = "s-bootstrap"

#: The planner's resolved Tier A profile (function calling). Its content is the
#: PLAN tool's declaration — the phase must build its OWN from the entity tools.
PROFILE = {"tools": [planner_tool([])], "tool_choice": "auto"}

PLAN_PAYLOAD = {
    "strings": [
        "the seal goes missing from the vault",
        "Mara's debt comes due",
        "the archivist stalls the search",
        "the duel is postponed, not resolved",
    ],
    "datetime": "Monday, June 02, 2025 at 19:40",
    "weather": "light rain",
    "forecast": "clearing before dawn",
    "established": ["Mara owes the guild 40 silver"],
}
PLAN_JSON = json.dumps(PLAN_PAYLOAD)


def run(coro):
    return asyncio.run(coro)


class LoopCaller:
    """ModelCaller scripted as ``(tool_name | None, payload)`` steps.

    A named step is a tool call carrying ``meta.name`` exactly as
    ``RegistryModelCaller`` surfaces it; ``None`` is a plain content completion.
    When the script runs out the caller answers a content close-out, so a fixture
    can never spin. ``order`` (optional) records the call order beside a spy, so a
    test can pin that something happened BEFORE the loop started.
    """

    def __init__(self, *replies, order: list[str] | None = None):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.order = order

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "request_profile": request_profile,
            }
        )
        if self.order is not None:
            self.order.append("model")
        name, payload = self.replies.pop(0) if self.replies else (None, "closed.")
        if name is None:
            yield {"type": "content", "delta": payload}
            return
        yield {
            "type": "tool_call",
            "delta": json.dumps(payload),
            "meta": {"name": name},
        }


class ExplodingCaller:
    """A caller that blows up on the first call, then answers the plan."""

    def __init__(self, error: Exception, *, plan_reply: str = PLAN_JSON):
        self.error = error
        self.plan_reply = plan_reply
        self.calls = 0

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls += 1
        if self.calls == 1:
            raise self.error
        yield {"type": "content", "delta": self.plan_reply}


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


def _node(log, caller, **kwargs):
    kwargs.setdefault("request_profile_fn", lambda: PROFILE)
    return make_planner_node(log=log, caller=caller, **kwargs)


def _rows(log, kind, session_id=SID):
    return [
        event.payload
        for event in log.get_events(session_id)
        if (event.payload or {}).get("event") == kind
    ]


def _loop_rows(log, session_id=SID):
    return [
        event.payload
        for event in log.get_events(session_id)
        if (event.payload or {}).get("event") == "agent_loop"
    ]


def _surfaces(log, session_id=SID):
    seen: list[str] = []
    for row in _loop_rows(log, session_id):
        if row["surface"] not in seen:
            seen.append(row["surface"])
    return seen


def _tool_results(log, session_id=SID):
    return [row for row in _loop_rows(log, session_id) if row["kind"] == "tool_result"]


def _tool_names(call) -> list[str]:
    return [tool["function"]["name"] for tool in call["request_profile"]["tools"]]


def _prior_plan(log):
    save_plan(
        log,
        SID,
        Plan(turn_seq=0, created_from="graph", strings=["keep me", "and me", "s3", "s4"]),
    )


def _card(log, name: str = "Seraphine"):
    """One session card event (the material ``ensure_roster`` admits members from)."""
    log.append_event(SID, "system", "system", {"card": {"name": name}})


# -- §4 the discriminator ----------------------------------------------------


def test_the_first_plan_run_bootstraps_and_still_writes_the_plan(log):
    caller = LoopCaller(
        (ESTABLISH_LOCATION_TOOL, {"name": "hall"}),
        (None, "seeded the hall."),
        (None, PLAN_JSON),
    )
    update = run(_node(log, caller)(_state(log)))

    # The phase ran first, under its own surface label (§3.5 observability).
    assert _surfaces(log) == ["planner_bootstrap"]
    assert _rows(log, "entity_establish")[0]["name"] == "hall"
    # …and the unchanged plan ladder still produced the plan + the scene seeds.
    plan = load_last_plan(log, SID)
    assert plan is not None and plan.strings == PLAN_PAYLOAD["strings"]
    assert "STRINGS: " in update["plan"]
    assert [event.payload for event in log.get_events(SID) if event.type == "state-delta"][0][
        "time"
    ] == "19:40"
    # Two model calls: the seed phase's loop, then the plan.
    assert len(caller.calls) == 3  # establish, close-out, plan


def test_a_later_run_repairs_and_never_bootstraps_again(log):
    _prior_plan(log)
    caller = LoopCaller((None, "nothing to repair."), (None, PLAN_JSON))
    run(_node(log, caller)(_state(log)))

    # §4/D1: the plan row's existence IS the discriminator.
    assert _surfaces(log) == ["planner_repair"]
    assert len(caller.calls) == 2  # repair loop + plan


def test_content_mode_skips_the_phase_and_keeps_the_single_call(log):
    """D6: no function calling = no tool loop; the plan call is exactly as before."""
    model = LoopCaller((None, PLAN_JSON))
    node = make_planner_node(log=log, caller=model, request_profile_fn=lambda: None)
    update = run(node(_state(log)))

    assert len(model.calls) == 1
    assert [m["role"] for m in model.calls[0]["messages"]] == ["system", "user"]
    assert "request_profile" not in model.calls[0] or (
        model.calls[0]["request_profile"] is None
    )
    assert _loop_rows(log) == []  # no loop trace at all
    assert _rows(log, "entity_establish") == []
    assert load_last_plan(log, SID) is not None
    assert "STRINGS: " in update["plan"]


def test_the_phase_needs_a_function_calling_profile(log):
    """The guard mirrors the node's gate, so a caller cannot seed on a text-only link."""
    with pytest.raises(PlannerBootstrapError):
        run(
            run_planner_tool_phase(
                caller=LoopCaller(),
                log=log,
                session_id=SID,
                directive="DIRECTIVE.",
                context="",
                turn_seq=0,
                mode=BOOTSTRAP,
                profile=None,
            )
        )
    assert log.get_events(SID) == []


def test_an_unknown_mode_is_refused(log):
    with pytest.raises(PlannerBootstrapError):
        run(
            run_planner_tool_phase(
                caller=LoopCaller(),
                log=log,
                session_id=SID,
                directive="DIRECTIVE.",
                context="",
                turn_seq=0,
                mode="reseed",
                profile=PROFILE,
            )
        )


# -- §3.1 the tool sets ------------------------------------------------------


def test_the_bootstrap_carries_the_trackers_whole_tool_surface_less_the_clock(log):
    caller = LoopCaller((None, "nothing to seed."), (None, PLAN_JSON))
    run(_node(log, caller)(_state(log)))

    phase = caller.calls[0]["request_profile"]
    assert phase["tool_choice"] == "auto"
    assert _tool_names(caller.calls[0]) == list(PLANNER_BOOTSTRAP_TOOLS)
    # W9.32 §3.3: the write doors are served — all but the clock's proposal.
    assert len(WRITE_TOOL_NAMES) == 15
    assert {
        SET_CONDITION_TOOL,
        SET_BODY_TOOL,
        ESTABLISH_ECONOMY_TOOL,
        ADJUST_ECONOMY_TOOL,
    } <= set(_tool_names(caller.calls[0]))
    assert PROPOSE_TIME_TOOL not in _tool_names(caller.calls[0])


def test_the_bootstrap_set_is_the_write_surface_minus_propose_time_only(log):
    """One exclusion, and no other: the bootstrap may seed anything the tracker writes.

    ``propose_time`` is withheld because a bootstrap holds no prose of its own to
    cite, and :data:`BOOTSTRAP_DIRECTIVE` gives it no step to spend one on — the
    opening ``datetime`` is the plan's own seed (``_seed_plan_state``), not a
    proposal. The tuple is a literal list (order = ``bts_loop_tools`` order); this
    pins it against the tracker's surface so a later write tool cannot silently
    vanish from the seed phase.
    """
    reads = (FOLDED_STATE_TOOL, ROSTER_TOOL, RECENT_DELTAS_TOOL)

    assert PLANNER_BOOTSTRAP_TOOLS[: len(reads)] == reads
    assert tuple(PLANNER_BOOTSTRAP_TOOLS[len(reads) :]) == tuple(
        name for name in WRITE_TOOL_NAMES if name != PROPOSE_TIME_TOOL
    )
    assert PROPOSE_TIME_TOOL not in PLANNER_BOOTSTRAP_TOOLS
    # Listed in bts_loop_tools order, which is the order the filter yields.
    assert PLANNER_BOOTSTRAP_TOOLS == (
        FOLDED_STATE_TOOL,
        ROSTER_TOOL,
        RECENT_DELTAS_TOOL,
        *[name for name in WRITE_TOOL_NAMES if name != PROPOSE_TIME_TOOL],
    )


def test_planner_bootstrap_tools_is_a_name_filter_over_the_tracker_set(log):
    turn = EntityTurn(log, SID, budget=PLANNER_BOOTSTRAP_BUDGET, actor="planner")
    bootstrap = planner_bootstrap_tools(log, SID, turn)
    by_name = {tool.name: tool for tool in bts_loop_tools(log, SID, turn)}

    assert [tool.name for tool in bootstrap] == list(PLANNER_BOOTSTRAP_TOOLS)
    assert len(bootstrap) == 17
    # The tool FUNCTION is the tracker's own object (one source of truth).
    for tool in bootstrap:
        assert tool.fn.__qualname__ == by_name[tool.name].fn.__qualname__


def test_repair_offers_the_reads_and_the_nine_mutating_tools_only(log):
    """D4: "never re-seed" is enforced by the tool SET, not by wording."""
    _prior_plan(log)
    caller = LoopCaller((None, "nothing to repair."), (None, PLAN_JSON))
    run(_node(log, caller)(_state(log)))

    names = _tool_names(caller.calls[0])
    assert names == list(REPAIR_TOOL_NAMES)
    assert len(names) == 12
    assert not [name for name in names if name.startswith("establish_")]
    assert "reconcile" not in names


def test_repair_gains_the_three_new_mutators_and_never_the_two_seeders(log):
    """W9.32 §3.3: set_condition/set_body/adjust_economy in; the establishes and
    the clock proposal stay out — three named exclusions, not a forgotten filter."""
    assert SET_CONDITION_TOOL in REPAIR_TOOL_NAMES
    assert SET_BODY_TOOL in REPAIR_TOOL_NAMES
    assert ADJUST_ECONOMY_TOOL in REPAIR_TOOL_NAMES
    # An establish spelled otherwise is still an establish (the purse is the
    # planner's singleton seed, §5).
    assert ESTABLISH_ECONOMY_TOOL not in REPAIR_TOOL_NAMES
    # The clock is the tracker's post-turn channel, evidenced by prose (§3.3).
    assert PROPOSE_TIME_TOOL not in REPAIR_TOOL_NAMES
    assert [
        name for name in REPAIR_TOOL_NAMES if name.startswith("establish_")
    ] == []

    repair = {tool.name for tool in planner_repair_tools(log, SID, EntityTurn(log, SID))}
    served = set(WRITE_TOOL_NAMES)
    assert repair - {FOLDED_STATE_TOOL, ROSTER_TOOL, RECENT_DELTAS_TOOL} <= served
    assert ESTABLISH_ECONOMY_TOOL in served  # withheld from repair, served elsewhere
    assert PROPOSE_TIME_TOOL in served


def test_planner_repair_tools_is_a_name_filter_over_the_tracker_set(log):
    turn = EntityTurn(log, SID, budget=PLANNER_REPAIR_BUDGET)
    repair = planner_repair_tools(log, SID, turn)
    served = {tool.name for tool in bts_loop_tools(log, SID, turn)}

    assert [tool.name for tool in repair] == list(REPAIR_TOOL_NAMES)
    assert {tool.name for tool in repair} <= served
    # The tool FUNCTION is the tracker's own object (one source of truth).
    by_name = {tool.name: tool for tool in bts_loop_tools(log, SID, turn)}
    for tool in repair:
        assert tool.fn.__qualname__ == by_name[tool.name].fn.__qualname__


def test_both_phases_stamp_the_rows_they_append_as_planner(log):
    """Attribution follows the LEDGER, so the seeder's own rows say ``planner``.

    The tools are the TRACKER's own objects; what changes between a tracker turn and
    a planner phase is the ledger driving them (§5's ownership map: the planner is
    the only seeder). Without that split a planner seed and a tracker write audit
    identically, and the map's claim cannot be checked against the log.
    """
    from fabulist.state.entities import set_body

    set_body(log, SID, "player", pose="slumped")  # a store call outside a loop: bts

    bootstrap = LoopCaller(
        (ESTABLISH_LOCATION_TOOL, {"name": "hall"}),
        (None, "seeded the hall."),
        (None, PLAN_JSON),
    )
    run(_node(log, bootstrap)(_state(log)))

    _prior_plan(log)
    repair = LoopCaller(
        (SET_BODY_TOOL, {"host": "player", "surface": "a split lip"}),
        (None, "nothing else to repair."),
        (None, PLAN_JSON),
    )
    run(_node(log, repair)(_state(log)))

    def actors(event_name: str, **match) -> list[str]:
        return [
            event.actor
            for event in log.get_events(SID)
            if (event.payload or {}).get("event") == event_name
            and all(
                (event.payload or {}).get(key) == value for key, value in match.items()
            )
        ]

    assert actors("entity_establish", name="hall") == ["planner"]  # seeded
    assert actors("entity_establish", kind="body") == ["bts"]  # store default
    assert actors("entity_update") == ["planner"]  # repaired


def test_a_repair_can_set_a_body_and_only_adjust_a_purse_that_exists(log):
    """The three new repair verbs are LIVE: mint-or-patch works, and the purse the
    repair cannot seed refuses through the store's own corrective text (§3.3)."""
    from fabulist.state.entities import establish_economy, set_body

    set_body(log, SID, "player", pose="slumped")  # a body exists from turn 1 on
    establish_economy(log, SID, "credits", balance=20)  # the planner's seed
    _prior_plan(log)
    caller = LoopCaller(
        (SET_BODY_TOOL, {"host": "player", "surface": "a split lip"}),
        (ADJUST_ECONOMY_TOOL, {"amount": -5}),
        (None, "nothing else to repair."),
        (None, PLAN_JSON),
    )
    run(_node(log, caller)(_state(log)))

    patch, adjust = (row["result"] for row in _tool_results(log))
    assert patch["ok"] is True
    assert patch["pose"] == "slumped"  # an omitted field is left alone
    assert patch["surface"] == "a split lip"
    assert adjust["ok"] is True
    assert adjust["balance"] == 15
    economy = [row for row in _rows(log, "entity_establish") if row.get("kind") == "economy"]
    assert len(economy) == 1  # never re-seeded by the repair


# -- §3.3 the tail -----------------------------------------------------------


def test_the_protocol_and_palette_ride_an_assistant_tail_before_the_final_user_message(
    log,
):
    """Issue #67: the seed tail is an ASSISTANT message, never a second system one.

    A stock Jinja chat template (the Qwen family) ``raise_exception``s on any
    system message after index 0, so the old ``[system, user, system]`` seed was a
    hard 500 in the seed phase. The tail rides the narrator's own carrier
    (``prompts.envelope``): immediately before the FINAL USER message.
    """
    caller = LoopCaller((None, "nothing to seed."), (None, PLAN_JSON))
    run(_node(log, caller)(_state(log)))

    messages = caller.calls[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "assistant", "user"]
    system, tail, ask = (message["content"] for message in messages)
    assert system.startswith("You are the story planner")
    assert BOOTSTRAP_DIRECTIVE in tail
    # The palette is byte-identical to the tracker's own tail render.
    assert registry_palette(log, SID) in tail
    assert "Bootstrap the object stores for turn 0" in ask
    # W9.32's two optional seed steps ride the SAME tail — never the stable
    # prefix (AGENTS 6): a preset edit cannot drop them either.
    assert ESTABLISH_ECONOMY_TOOL in tail and SET_BODY_TOOL in tail
    assert ESTABLISH_ECONOMY_TOOL not in system + ask
    assert SET_BODY_TOOL not in system + ask
    # The player's message space is untouched (owner ruling): the ask is the
    # compiled prompt alone, no engine bytes folded in.
    assert BOOTSTRAP_DIRECTIVE not in ask and registry_palette(log, SID) not in ask
    # And only ONE system message exists, at index 0.
    assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]


def test_the_bootstrap_directive_teaches_the_two_optional_seeds(log):
    """W9.32 §7 W6b: economy and body are taught as CONDITIONAL seed steps."""
    directive = BOOTSTRAP_DIRECTIVE
    for tool in (ESTABLISH_ECONOMY_TOOL, SET_BODY_TOOL):
        assert tool in directive
    # Conditional, not mandatory: the word "optional" is attached to each step and
    # both are explicitly skippable.
    assert directive.count("optional") >= 2
    assert "skippable" in directive and "skip it" in directive
    # The required steps keep their numbers — the two new ones are LETTERED, so
    # nothing the model already learned about step order moves.
    for step in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9."):
        assert f"\n{step} " in directive
    assert "\n6a. " in directive and "\n6b. " in directive
    # …and they sit AFTER the places/beings step, before the duplicate rule.
    assert (
        directive.index("\n6. ")
        < directive.index("\n6a. ")
        < directive.index("\n6b. ")
        < directive.index("\n7. ")
    )
    # The worked example demonstrates SKIPPING both, so "optional" is not read as
    # "when in doubt, seed it".
    assert "6a and 6b are both skipped" in directive


def test_the_repair_directive_names_the_new_verbs_and_the_clock_exclusion(log):
    """The prose must not disagree with the tool set: step 3 lists what IS served.

    A model that reads only the six old verbs would treat three served mutators as
    out of scope, and one that tries ``propose_time`` loses a completion out of
    four to a refusal.
    """
    for tool in (SET_CONDITION_TOOL, SET_BODY_TOOL, ADJUST_ECONOMY_TOOL):
        assert tool in REPAIR_DIRECTIVE
    assert f"{ESTABLISH_ECONOMY_TOOL} is not offered" in REPAIR_DIRECTIVE or (
        "establish_* tools are not available" in REPAIR_DIRECTIVE
    )
    assert f"{PROPOSE_TIME_TOOL} is not offered" in REPAIR_DIRECTIVE


def test_repair_swaps_the_directive_in_the_same_tail_position(log):
    _prior_plan(log)
    caller = LoopCaller((None, "nothing to repair."), (None, PLAN_JSON))
    run(_node(log, caller)(_state(log)))

    messages = caller.calls[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "assistant", "user"]
    # The repair directive rides the SAME carrier as the bootstrap one (issue #67).
    assert REPAIR_DIRECTIVE in messages[1]["content"]
    assert BOOTSTRAP_DIRECTIVE not in messages[1]["content"]
    assert "Repair the object stores for turn 0" in messages[2]["content"]
    assert REPAIR_DIRECTIVE not in messages[2]["content"]


# -- D9a ensure_roster FIRST -------------------------------------------------


def test_the_roster_is_materialized_before_the_loop_starts(log, monkeypatch):
    from fabulist.state import cast as cast_module

    order: list[str] = []
    real = cast_module.ensure_roster

    def spy(log_arg, session_id, **kwargs):
        order.append("roster")
        return real(log_arg, session_id, **kwargs)

    monkeypatch.setattr(cast_module, "ensure_roster", spy)
    _card(log)
    caller = LoopCaller((None, "nothing to seed."), (None, PLAN_JSON), order=order)
    run(_node(log, caller)(_state(log)))

    assert order[0] == "roster"  # before the very first model call
    assert "model" in order


def test_a_card_character_is_movable_on_the_very_first_run(log):
    """D9a's consequence: without the roster call this move is refused AND files a
    spurious ``cast`` proposal for a person the card already names."""
    from fabulist.log.schema_gate import ASSET_PROPOSAL_EVENT
    from fabulist.state.cast import member_by_name, roster

    _card(log, "Seraphine")
    caller = LoopCaller(
        (ESTABLISH_LOCATION_TOOL, {"name": "hall"}),
        (MOVE_BEING_TOOL, {"who": "Seraphine", "to": "hall"}),
        (None, "seeded."),
        (None, PLAN_JSON),
    )
    run(_node(log, caller)(_state(log)))

    member = member_by_name(roster(log, SID), "Seraphine")
    assert member is not None  # ensure_roster minted the card's seed admit
    results = _tool_results(log)
    assert [row["result"]["ok"] for row in results] == [True, True]
    moved = _rows(log, "entity_move")[0]
    assert moved["who"] == member.member_id
    assert moved["to"] == results[0]["result"]["id"]
    proposals = [
        event
        for event in log.get_events(SID)
        if (event.payload or {}).get("event") == ASSET_PROPOSAL_EVENT
    ]
    assert proposals == []


# -- §4 idempotency ----------------------------------------------------------


def test_a_duplicate_establish_is_refused_with_the_existing_id(log):
    from fabulist.state.entities import establish_location

    existing = establish_location(log, SID, "hall")
    caller = LoopCaller(
        (ESTABLISH_LOCATION_TOOL, {"name": "Hall"}),
        (None, "already there."),
        (None, PLAN_JSON),
    )
    run(_node(log, caller)(_state(log)))

    refusal = _tool_results(log)[0]["result"]
    assert refusal["ok"] is False
    assert existing in refusal["error"]
    assert [row["name"] for row in _rows(log, "entity_establish")] == ["hall"]
    # …and the duplicate costs nothing: the plan still lands.
    assert load_last_plan(log, SID) is not None


# -- §6 containment ----------------------------------------------------------


class SeedReasoningCaller:
    """The live DeepSeek seed shape: a thinking turn, then a no-reasoning one.

    Captured from ``deepseek-flash`` in thinking mode (spare engine, temp log):

    * call 1 reasons in prose, then calls a tool;
    * call 2's response carries ``reasoning_content: ""`` — the provider reasoned
      NOTHING and said so — then calls a tool;
    * call 3 closes out.

    The vendor then REJECTS the next request unless the ASSISTANT TURN THE
    REQUEST CONTINUES FROM carries its ``reasoning_content`` with its
    ``tool_calls`` ("The `reasoning_content` in the thinking mode must be passed
    back to the API"), which is the 400 the planner's seed phase hit on its third
    request — the turn being call 2's, whose reasoning is the EMPTY string. The
    CURRENT round is carried in full (owner ruling on #29), so the older turn's
    block stays verbatim too; only a PRIOR execution's text is trimmed (history
    assembly emits the field empty).

    ``findings`` is the recorded reasoning the provider sent per call (``None`` =
    the field never arrived).
    """

    def __init__(self, findings: list[str | None], *steps) -> None:
        self.findings = list(findings)
        self.steps = list(steps)
        self.calls: list[dict] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "request_profile": request_profile,
            }
        )
        finding = self.findings.pop(0) if self.findings else None
        if finding is not None:
            yield {"type": "reasoning", "delta": finding}
        name, payload = self.steps.pop(0) if self.steps else (None, "seeded.")
        if name is None:
            yield {"type": "content", "delta": payload}
            return
        yield {
            "type": "tool_call",
            "delta": json.dumps(payload),
            "meta": {"name": name},
        }


def test_the_seed_loop_echoes_the_providers_empty_reasoning_verbatim(log):
    """REGRESSION: the seed phase's third request 400'd on thinking mode.

    The provider sent ``reasoning_content: ""`` for the turn that seeded the
    second place; the engine dropped the field (it read the empty string as
    "no reasoning") and the vendor refused the next request. Presence and text are
    different facts — the empty value is the provider's own and rides back
    unchanged, the absent one still sends nothing.
    """
    caller = SeedReasoningCaller(
        [
            "The checklist: reads first, then top-level places.",
            "",  # present but empty — the vendor's continuation turn
        ],
        (ESTABLISH_LOCATION_TOOL, {"name": "The Crimson Lantern"}),
        (ESTABLISH_LOCATION_TOOL, {"name": "Common Room"}),
        (None, "Stores seeded for turn 0."),
        (None, PLAN_JSON),
    )
    run(_node(log, caller)(_state(log)))

    # Request 2 answers call 1: the provider's prose reasoning rides back.
    first_echo = [
        message
        for message in caller.calls[1]["messages"]
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert [message.get("reasoning_content") for message in first_echo] == [
        "The checklist: reads first, then top-level places."
    ]
    # Request 3 answers call 2: the EMPTY string rides back — the field is
    # present (that is what the vendor checks) and its value is the provider's.
    second_echo = [
        message
        for message in caller.calls[2]["messages"]
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert len(second_echo) == 2
    # Issue #29's owner ruling: the CURRENT round is carried verbatim, so call 1's
    # block is still there in full when call 2's empty value is stamped. Nothing
    # is retired inside a round; the trim sits at the ROUND boundary (prior
    # executions' history re-attaches the field present-but-empty).
    assert second_echo[0]["reasoning_content"] == (
        "The checklist: reads first, then top-level places."
    )
    assert second_echo[1]["reasoning_content"] == ""
    # The seed completed (no PlannerBootstrapError) and the places landed.
    assert [row["result"]["ok"] for row in _tool_results(log)] == [True, True]
    assert _rows(log, "plan_refresh_failed") == []


def test_a_failed_bootstrap_is_contained_and_the_plan_still_runs(log):
    caller = LoopCaller((None, ""), (None, PLAN_JSON))  # no action, no close-out
    update = run(_node(log, caller)(_state(log)))

    failures = _rows(log, "plan_refresh_failed")
    assert len(failures) == 1
    assert failures[0]["stage"] == BOOTSTRAP
    assert "no action was recorded" in failures[0]["error"]
    plan = load_last_plan(log, SID)
    assert plan is not None  # S2: the first turn still gets its plan
    assert "STRINGS: " in update["plan"]


def test_a_transport_error_in_the_phase_is_contained_the_same_way(log):
    """The handler is broad on purpose: only a timeout is recovered IN the loop."""
    caller = ExplodingCaller(RuntimeError("connection reset"))
    update = run(_node(log, caller)(_state(log)))

    failures = _rows(log, "plan_refresh_failed")
    assert len(failures) == 1
    assert failures[0]["stage"] == BOOTSTRAP
    assert "connection reset" in failures[0]["error"]
    assert load_last_plan(log, SID) is not None
    assert "STRINGS: " in update["plan"]


def test_a_failed_repair_is_staged_as_repair(log):
    _prior_plan(log)
    caller = LoopCaller((None, ""), (None, PLAN_JSON))
    run(_node(log, caller)(_state(log)))

    failures = _rows(log, "plan_refresh_failed")
    assert len(failures) == 1
    assert failures[0]["stage"] == REPAIR


def test_a_failed_plan_after_a_failed_seed_writes_two_distinct_rows(log):
    """§6: both rows are visible, and only the seed's carries a ``stage``."""
    caller = LoopCaller((None, ""), (None, "not a plan"))
    run(_node(log, caller)(_state(log)))

    failures = _rows(log, "plan_refresh_failed")
    assert [row.get("stage") for row in failures] == [BOOTSTRAP, None]
    assert load_last_plan(log, SID) is None


# -- §3.4 the budget ---------------------------------------------------------


def test_the_bootstrap_budget_caps_the_writes_and_says_so(log):
    caller = LoopCaller(
        *[
            (ESTABLISH_THREAD_TOOL, {"name": f"thread {index}"})
            for index in range(PLANNER_BOOTSTRAP_BUDGET + 2)
        ],
        (None, "budget spent."),
        (None, PLAN_JSON),
    )
    run(_node(log, caller)(_state(log)))

    results = _tool_results(log)
    assert [row["result"]["ok"] for row in results[:PLANNER_BOOTSTRAP_BUDGET]] == (
        [True] * PLANNER_BOOTSTRAP_BUDGET
    )
    assert results[PLANNER_BOOTSTRAP_BUDGET]["result"] == {
        "ok": False,
        "error": budget_error(PLANNER_BOOTSTRAP_BUDGET),
    }
    assert "(8)" in results[PLANNER_BOOTSTRAP_BUDGET]["result"]["error"]
    assert len(_rows(log, "entity_establish")) == PLANNER_BOOTSTRAP_BUDGET


def test_the_planner_budgets_are_unchanged_at_eight_and_four(log):
    """W9.32 §7 W6b: the optional seed steps are steps, not budget headroom."""
    assert (PLANNER_BOOTSTRAP_BUDGET, PLANNER_REPAIR_BUDGET) == (8, 4)
    assert PLANNER_REPAIR_BUDGET < PLANNER_BOOTSTRAP_BUDGET


def test_entity_turn_budget_text_states_the_real_budget(log):
    turn = EntityTurn(log, SID, budget=PLANNER_REPAIR_BUDGET)
    for _ in range(PLANNER_REPAIR_BUDGET):   # only SUCCESS spends a write
        assert turn.spend() is None
        turn.settle(True)
    refusal = turn.spend()
    assert refusal is not None
    assert refusal == budget_error(PLANNER_REPAIR_BUDGET)
    assert f"({PLANNER_REPAIR_BUDGET})" in refusal


def test_the_default_budget_text_is_byte_identical(log):
    turn = EntityTurn(log, SID)
    for _ in range(ENTITY_ACTION_BUDGET):
        assert turn.spend() is None
        turn.settle(True)
    assert turn.spend() == ENTITY_BUDGET_ERROR
    assert budget_error(ENTITY_ACTION_BUDGET) == ENTITY_BUDGET_ERROR
    assert turn.budget == ENTITY_ACTION_BUDGET


# -- §3.3 the context block --------------------------------------------------


def test_the_plan_context_lists_the_seeded_places_and_occupants(log):
    from fabulist.state.entities import establish_location, move_being

    hall = establish_location(log, SID, "hall")
    bedroom = establish_location(log, SID, "master bedroom", parent_id=hall)
    move_being(log, SID, "player", bedroom)

    block = _world_objects_block(log, SID)
    assert block.startswith("WORLD OBJECTS:\n")
    assert "- hall" in block
    assert "- hall > master bedroom: player" in block
    assert block in _planner_context(log, SID, _state(log))


def test_an_empty_registry_contributes_no_block(log):
    """Only the always-present off-screen bucket: nothing to say yet."""
    assert _world_objects_block(log, SID) == ""
    assert "WORLD OBJECTS:" not in _planner_context(log, SID, _state(log))
