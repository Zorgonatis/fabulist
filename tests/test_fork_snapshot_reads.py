"""The registry and the plan READ the state their fork persisted (issue #62).

The fork write landed in :mod:`fabulist.log.repo` (one
``system``/``session_state_snapshot`` row per branch). This lane closes the read
side: the branch's own copy is what its live consumers see, not a re-fold of the
parent chain.

Owner ruling, quoted by the issue: "We should be persisting the entire session
state, all objects, everything - into the new chat. It is as important as the
preceding history itself." A branch that persists a copy and then reads the
parent's is not persisting anything it trusts.

Read precedence, per read (``state/entities.registry``, ``graph.plans.load_last_plan``):

1. the session's OWN snapshot answers first (registry objects / the plan), then
   the session's own later rows fold on top in seq order — so a branch keeps
   writing (and planning) normally, it just does not inherit the parent's LATER
   answers;
2. a root session and a pre-#62 branch own no snapshot row, so the fork-aware
   chain answers exactly as it did before this change;
3. a ``null`` snapshot plan is an ANSWER, not a miss: a fork cut BELOW the plan
   row inherits no plan (the cut rule).

Tests, one per acceptance clause: the parent gains newer registry writes and a
newer plan after the fork (the branch reads its own); the reads never walk the
parent at all; the branch's own later writes still fold on top; a pre-change
branch still reads through the chain; a root session is unchanged; the cut rule;
a revised snapshot moves both reads.
"""

from __future__ import annotations

import pytest

from fabulist.graph.plans import Plan, load_last_plan, save_plan
from fabulist.log import EventLog
from fabulist.log.repo import STATE_SNAPSHOT_EVENT, is_state_snapshot_event
from fabulist.state.entities import (
    PLAYER,
    establish_item,
    establish_location,
    establish_thread,
    move_being,
    registry,
    rendered_state,
    retire_object,
    set_attribute,
    set_body,
    update_object,
)

SESSION = "chat"


# -- helpers ----------------------------------------------------------------
def _turn(log: EventLog, sid: str, text: str = "I step into the market.") -> int:
    """Append one user turn + reply; return the narrative's ``seq`` (the cut)."""
    log.append_event(sid, "user", "user", {"text": text})
    reply = log.append_event(
        sid, "narrative", "narrator", {"prose": "Lanterns sway.", "segments": []}
    )
    return reply.seq


def _plan(turn_seq: int, *strings: str) -> Plan:
    return Plan(
        turn_seq=turn_seq,
        created_from="graph",
        strings=list(strings),
        datetime="Evening, the market",
        weather="rain",
        established=["the debt is unpaid"],
        extras={"currency": "40 silver"},
    )


def _seed_state(log: EventLog, sid: str) -> None:
    """The scene the cut inherits: state, registry objects, a plan."""
    log.append_event(
        sid, "state-delta", "extractor", {"time": "18:20", "forecast": "rain"}
    )
    market = establish_location(log, sid, "Night Market")
    move_being(log, sid, PLAYER, market)
    establish_thread(log, sid, "The unpaid debt", detail="a lender waits")
    establish_item(log, sid, "brass lantern", holder=PLAYER)
    set_attribute(log, sid, PLAYER, key="resolve", value=7)
    set_body(log, sid, PLAYER, pose="leaning on the stall", clothing=["coat", "shirt"])
    save_plan(log, sid, _plan(2, "the lender names a price"))


def _snapshot_row(log: EventLog, sid: str):
    """The session's own snapshot row (``None`` when it has none)."""
    rows = [e for e in log.get_events(sid) if is_state_snapshot_event(e)]
    return rows[-1] if rows else None


def _thread_names(reg) -> list[str]:
    return [thread.name for thread in reg.threads.values()]


def _meter_values(reg) -> dict[str, object]:
    return {meter.key: meter.value for meter in reg.meters.values()}


# -- 1. the branch reads its own copy, not the parent's later one -----------
def test_the_registry_and_plan_reads_prefer_the_branchs_own_snapshot(
    log: EventLog,
) -> None:
    """A parent that keeps playing must not move the branch's persisted state."""
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    facts_at_cut = rendered_state(log, SESSION)

    log.fork_session(SESSION, cut, "branch")

    # AFTER the fork the parent keeps playing: a newer registry write of every
    # observable kind, a newer plan, and a newer clock.
    set_attribute(log, SESSION, PLAYER, key="resolve", value=99)
    establish_thread(log, SESSION, "A brand new thread")
    establish_item(log, SESSION, "a stolen key", holder=PLAYER)
    set_body(log, SESSION, PLAYER, pose="gone upstairs")
    save_plan(log, SESSION, _plan(9, "the LATE heading"))
    log.append_event(SESSION, "state-delta", "extractor", {"time": "23:45"})

    # The parent really did move (the fixture is honest about the divergence).
    parent = registry(log, SESSION)
    assert _meter_values(parent)["resolve"] == 99
    assert "A brand new thread" in _thread_names(parent)
    assert load_last_plan(log, SESSION).strings == ["the LATE heading"]

    # The branch reads what it persisted: its own snapshot, plus nothing newer.
    branch = registry(log, "branch")
    assert _meter_values(branch)["resolve"] == 7
    assert _thread_names(branch) == ["The unpaid debt"]
    assert [item.name for item in branch.items.values()] == ["brass lantern"]
    assert branch.bodies[next(iter(branch.bodies))].pose == "leaning on the stall"
    assert load_last_plan(log, "branch").strings == ["the lender names a price"]
    # ... and the derived facts read the same persisted state.
    assert rendered_state(log, "branch") == facts_at_cut


# -- 2. the strong form: the parent chain is never consulted ----------------
def test_the_reads_do_not_walk_the_parent_chain(
    log: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch's registry/plan reads must answer without the parent walk."""
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    log.fork_session(SESSION, cut, "branch")

    def _no_chain(*_args, **_kwargs):
        raise AssertionError(
            "a branch's registry/plan read must not walk the parent chain"
        )

    monkeypatch.setattr(EventLog, "get_chain_events", _no_chain)
    monkeypatch.setattr(EventLog, "_stream_events", _no_chain)

    branch = registry(log, "branch")
    assert _thread_names(branch) == ["The unpaid debt"]
    assert _meter_values(branch)["resolve"] == 7
    assert load_last_plan(log, "branch").strings == ["the lender names a price"]


# -- 3. a branch still writes: its own later rows fold on top ---------------
def test_a_branchs_own_later_writes_fold_on_top_of_its_snapshot(
    log: EventLog,
) -> None:
    """Snapshot-first is a BASELINE, not a freeze: the branch keeps tracking."""
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    log.fork_session(SESSION, cut, "branch")

    establish_thread(log, "branch", "A branch-only thread")
    set_attribute(log, "branch", PLAYER, key="resolve", value=12)
    # A seeded object is there to be PATCHED and TOMBSTONED, not just read.
    item_id = next(iter(registry(log, "branch").items))
    update_object(log, "branch", item_id, note="the glass is cracked")
    retired = establish_thread(log, "branch", "A passing worry")
    retire_object(log, "branch", retired)
    save_plan(log, "branch", _plan(5, "the branch heads north"))

    branch = registry(log, "branch")
    assert _thread_names(branch) == ["The unpaid debt", "A branch-only thread"]
    assert _meter_values(branch)["resolve"] == 12
    assert next(iter(branch.items.values())).note == "the glass is cracked"
    assert load_last_plan(log, "branch").strings == ["the branch heads north"]

    # The branch's rows never leak upwards.
    assert _thread_names(registry(log, SESSION)) == ["The unpaid debt"]
    assert load_last_plan(log, SESSION).strings == ["the lender names a price"]


# -- 4. a pre-#62 branch still reads through the chain ----------------------
def test_a_pre_change_branch_reads_both_through_the_chain(log: EventLog) -> None:
    """A branch forked BEFORE issue #62 owns no snapshot: the chain is the fallback."""
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    at_cut = registry(log, SESSION)
    plan_at_cut = load_last_plan(log, SESSION)

    log.fork_session(SESSION, cut, "branch")
    # Simulate the pre-change row set: a fork that wrote only its bookkeeping.
    log._conn.execute(
        "DELETE FROM events WHERE session_id = ? AND payload LIKE ?",
        ("branch", f'%"event": "{STATE_SNAPSHOT_EVENT}"%'),
    )
    assert log.get_events("branch") == []
    assert log.get_state_snapshot("branch") is None

    branch = registry(log, "branch")
    assert branch.threads.keys() == at_cut.threads.keys()
    assert branch.items.keys() == at_cut.items.keys()
    assert branch.meters.keys() == at_cut.meters.keys()
    assert branch.assignments == at_cut.assignments
    assert branch.legacy == at_cut.legacy
    assert load_last_plan(log, "branch") == plan_at_cut


# -- 5. a root session is unchanged ----------------------------------------
def test_a_root_session_reads_exactly_as_before(log: EventLog) -> None:
    """No snapshot row: the chain fold answers, and stays live."""
    _seed_state(log, SESSION)
    _turn(log, SESSION)
    assert _snapshot_row(log, SESSION) is None
    assert log.get_state_snapshot(SESSION) is None

    reg = registry(log, SESSION)
    assert _thread_names(reg) == ["The unpaid debt"]
    assert _meter_values(reg)["resolve"] == 7
    assert load_last_plan(log, SESSION).strings == ["the lender names a price"]

    # A root's later writes still land — the chain is live, not frozen at a cut.
    set_attribute(log, SESSION, PLAYER, key="resolve", value=42)
    save_plan(log, SESSION, _plan(4, "the root replans"))
    assert _meter_values(registry(log, SESSION))["resolve"] == 42
    assert load_last_plan(log, SESSION).strings == ["the root replans"]


# -- 6. the cut rule: a null snapshot plan is an answer --------------------
def test_the_plan_read_honours_a_null_snapshot_plan(log: EventLog) -> None:
    """A fork cut BELOW the plan row must still inherit no plan."""
    log.append_event(
        SESSION, "state-delta", "extractor", {"time": "18:20", "forecast": "rain"}
    )
    cut = _turn(log, SESSION)
    # Above the cut: the parent plans afterwards.
    save_plan(log, SESSION, _plan(3, "written above the cut"))

    log.fork_session(SESSION, cut, "branch")

    assert _snapshot_row(log, "branch").payload["plan"] is None
    assert log.get_state_snapshot("branch")["plan"] is None
    assert load_last_plan(log, "branch") is None
    # The parent does hold a plan — the null is the branch's answer, not a miss.
    assert load_last_plan(log, SESSION).strings == ["written above the cut"]


# -- 7. a revised snapshot moves both reads (ADR-002) ----------------------
def test_a_revised_snapshot_moves_the_registry_and_the_plan(log: EventLog) -> None:
    """Corrections are revisions: both reads render the superseding payload."""
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    log.fork_session(SESSION, cut, "branch")
    row = _snapshot_row(log, "branch")

    revised = dict(row.payload)
    revised["registry"] = {**revised["registry"]}
    meters = dict(revised["registry"]["meters"])
    (meter_id,) = meters.keys()
    meters[meter_id] = {**meters[meter_id], "value": 55}
    revised["registry"]["meters"] = meters
    revised["plan"] = {**(revised["plan"] or {}), "strings": ["the corrected heading"]}
    log.append_revision(
        "branch", row.id, "user", {"revised": revised, "reason": "the table misread"}
    )

    assert _meter_values(registry(log, "branch"))["resolve"] == 55
    assert load_last_plan(log, "branch").strings == ["the corrected heading"]
