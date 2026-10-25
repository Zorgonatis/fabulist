"""W9.23 §3.6 — the historian: the proposal lanes' off-turn runner (§8.10a).

The historian is the SAME lane system the turn used to run at the ``asset_proposals``
stage, driven between turns. These tests pin the rules the engine reads, each test
name stating its rule:

* the DUE RULE — ``committed_turns - last_run.turn_seq >= cadence``, with NO
  turn-1 invoke (the deliberate difference from the planner) and a cadence read
  LIVE from the preset at every evaluation;
* the COMPLETED run is the clock — a MISSED run (the run failed, or every lane
  failed without filing or dropping anything) advances nothing, so the very next
  committed turn retries and the retry's window re-covers the turns the miss never
  judged; a quiet window and any run that filed/dropped consumes the cadence;
  a ``started`` row never settles a window on its own;
* the EVIDENCE WINDOW is unioned into ONE synthetic turn state (§3.6.2), so a
  window whose touched entities appear ONLY in an early turn still files rows —
  last-turn-only evidence would pass trivially and is exactly the regression this
  pins;
* the run's trace: ``historian_run`` started → finished with the dispatcher's
  gate and the run's counts, appended in that order;
* the CAST BOUNDARY (§3.6.6): a run files ZERO ``kind=cast`` rows even when the
  window's roster diff is non-empty — the three lane specs never carry cast;
* the double-spawn guard: a second run for the same session is refused while one
  is in flight, and a quiet window spends no generator call (the zero-cost
  contract survives the move off the turn);
* the BOUND is per CONNECTION, never per run: a lane call that hits its
  connection's ``timeout`` is a per-lane failure (``failed_lanes``) — the other
  lanes still file and the run finishes; a runner-level raise still stamps
  ``failed``, logs, and releases the in-flight guard; and no run-total timeout
  symbol exists to be reintroduced.

Fakes only for the models (a fake generator + a fake decision caller); the lane
system itself is the real :class:`~fabulist.graph.nodes.AssetProposalFanout`,
which is what makes "the runner drives the existing fan-out unmodified" a fact
rather than a hope. The live model run is the orchestrator's (§8.10c).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.graph.nodes import make_asset_proposals_node
from fabulist.log import EventLog
from fabulist.state import historian as historian_module
from fabulist.state.historian import (
    DEFAULT_HISTORIAN_CADENCE,
    HISTORIAN_EVIDENCE_WINDOW_TURNS,
    HistorianBusy,
    historian_is_due,
    historian_run_missed,
    historian_window,
    load_last_completed_historian_run,
    load_last_historian_run,
    make_historian_runner,
)
from fabulist.state.proposal_lanes import (
    SHOULD_THIS_PROPOSE_QUESTION_ID,
    SHOULD_WE_PROPOSE_QUESTION_ID,
    SUMMARY_EVENT,
)

SID = "s-historian"

#: The world the tests seed — the asset a lane may amend (its ``old`` value is
#: what the slice shows, so it is also the only honest ``old``).
WORLD_ID = "w_mill"
WORLD_NAME = "Miller's Bend"
WORLD_DESCRIPTION = "A river town."


def run(coro):
    return asyncio.run(coro)


# -- log fixtures ------------------------------------------------------------


def _narrative(log, prose="Rain drums on the mill roof.", session_id=SID):
    """One committed turn (the due rule counts these; they bound the window)."""
    return log.append_event(
        session_id,
        "narrative",
        "narrator",
        {"prose": prose, "segments": [{"kind": "prose", "text": prose}]},
    )


def _world_state(
    log,
    *,
    state_names=(),
    venue_names=(),
    off_screen=(),
    event="world_state",
    session_id=SID,
):
    """One BTS world-state row (the evidence source the fan-out reads)."""
    return log.append_event(
        session_id,
        "system",
        "bts",
        {
            "event": event,
            "kind": "delta",
            "state": {name: {"physical": {"hp": "fine"}} for name in state_names},
            "venue": {
                name: {"venue": "Miller's Bend", "area": "the mill"}
                for name in venue_names
            },
            "off_screen": [{"name": name, "location": "the road"} for name in off_screen],
            "threads": [],
            "tension": {},
        },
    )


def _seed_world(log, session_id=SID):
    from fabulist.worlds.registry import append_world

    return append_world(
        log,
        {
            "world_id": WORLD_ID,
            "name": WORLD_NAME,
            "description": WORLD_DESCRIPTION,
        },
    )


def _system_rows(log, variant, session_id=SID):
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.type == "system" and (event.payload or {}).get("event") == variant
    ]


def _historian_run_row(
    log,
    *,
    turn_seq,
    status="finished",
    gate="proceeded",
    filed=0,
    dropped=0,
    failed_lanes=(),
    error=None,
    session_id=SID,
):
    """One ``historian_run`` row in the runner's own shape (the clock's input).

    ``failed`` rows carry NO ``gate``/``filed``/``dropped`` (that is what the
    runner appends on a runner-level failure), so the read must tolerate the
    missing keys — the row built here is the schema's, not a hand-made superset.
    """
    payload = {
        "event": "historian_run",
        "status": status,
        "trigger": "cadence",
        "turn_seq": turn_seq,
    }
    if status == "started":
        payload["window_turns"] = 0
    elif status == "failed":
        payload["error"] = error or "lane system exploded"
    else:
        payload.update(
            {
                "gate": gate,
                "filed": filed,
                "dropped": dropped,
                "failed_lanes": list(failed_lanes),
                "duration_ms": 1000,
            }
        )
    return log.append_event(session_id, "system", "bts", payload)


def _proposal_rows(log, session_id=SID):
    return _system_rows(log, "asset_proposal", session_id)


def _summaries(log, session_id=SID):
    return _system_rows(log, SUMMARY_EVENT, session_id)


# -- model fakes -------------------------------------------------------------


class FakeGenerator:
    """The lane generator: one canned JSON batch per ``<lane data_type="...">``."""

    def __init__(self, batches=None, *, delay: float = 0.0):
        self.batches = dict(batches or {})
        self.delay = delay
        self.calls: list[list[dict]] = []

    @staticmethod
    def lane_of(messages) -> str:
        for message in messages:
            text = str(message.get("content") or "")
            if "<lane data_type=" in text:
                return text.split('<lane data_type="', 1)[1].split('"', 1)[0]
        return ""

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls.append(messages)
        await asyncio.sleep(self.delay)
        reply = self.batches.get(self.lane_of(messages)) or {"proposals": []}
        text = reply if isinstance(reply, str) else json.dumps(reply)
        yield {"type": "content", "delta": text}


class FakeDecision:
    """The duck-typed d1-3b seam: every judgment answers ``propose``.

    ``confidence`` is the field a real d1-3b ``choice`` answer carries and the
    proposal gate's floor (P2) cuts on; ``None`` omits it (the no-confidence
    shape, which leaves the threshold as the whole verdict).
    """

    def __init__(
        self,
        *,
        gate: float = 0.9,
        items: float = 0.9,
        confidence: float | None = 1.0,
    ):
        self.gate = gate
        self.items = items
        self.confidence = confidence
        self.calls: list[tuple[str, dict]] = []

    async def ask(self, state, questions):
        self.calls.append((state, dict(questions)))
        question_id = next(iter(questions))
        probability = (
            self.gate
            if question_id == SHOULD_WE_PROPOSE_QUESTION_ID
            else self.items
            if question_id == SHOULD_THIS_PROPOSE_QUESTION_ID
            else None
        )
        if probability is None:  # pragma: no cover - the stage asks only its two
            return None
        answer = {
            "choice": "propose",
            "probabilities": {"propose": probability, "skip": 1 - probability},
        }
        if self.confidence is not None:
            answer["confidence"] = self.confidence
        return {question_id: answer}


def _world_amend_row():
    """One legal world-lane row in the operation contract (§3.4.4)."""
    return {
        "kind": "world",
        "action": "amend",
        "target": WORLD_ID,
        "ops": [
            {
                "op": "modify",
                "path": "description",
                "old": WORLD_DESCRIPTION,
                "new": "A mill town on the bend of the river.",
            }
        ],
        "evidence": "The mill has stood a hundred years.",
    }


def _fanout(log, *, generator=None, decision=None):
    return make_asset_proposals_node(
        log=log,
        caller=generator if generator is not None else FakeGenerator(
            {"world": {"proposals": [_world_amend_row()]}}
        ),
        decision=decision if decision is not None else FakeDecision(),
    )


def _runner(log, *, fanout=None, cadence=DEFAULT_HISTORIAN_CADENCE):
    return make_historian_runner(
        log=log, fanout=fanout if fanout is not None else _fanout(log), cadence=cadence
    )


# -- the due rule (§3.6.1) ---------------------------------------------------


def test_there_is_no_automatic_run_at_turn_one():
    """Unlike the planner: ``last = 0`` means a first run needs ``cadence`` turns."""
    for turns in range(1, DEFAULT_HISTORIAN_CADENCE):
        assert (
            historian_is_due(
                last_run=None, committed_turns=turns, cadence=DEFAULT_HISTORIAN_CADENCE
            )
            is False
        )


def test_the_first_automatic_run_fires_exactly_at_the_cadence():
    assert (
        historian_is_due(
            last_run=None,
            committed_turns=DEFAULT_HISTORIAN_CADENCE,
            cadence=DEFAULT_HISTORIAN_CADENCE,
        )
        is True
    )
    assert historian_is_due(last_run=None, committed_turns=1, cadence=1) is True


def test_a_run_is_due_again_exactly_a_cadence_after_the_last_run():
    last = {"status": "finished", "turn_seq": 10}
    assert historian_is_due(last_run=last, committed_turns=19, cadence=10) is False
    assert historian_is_due(last_run=last, committed_turns=20, cadence=10) is True
    # ...and an overdue clock stays due (never negative).
    assert historian_is_due(last_run=last, committed_turns=25, cadence=10) is True


def test_a_missed_run_leaves_the_clock_so_the_next_turns_are_due(log):
    """§3.6.3: a run that FAILS never consumes its window — retry every turn.

    The trace keeps the failed row (the status API shows it); the CLOCK stays at
    the last completed run, so turns 11, 12 and 13 are each due — a retry, not a
    cadence of 1.
    """
    _historian_run_row(log, turn_seq=0, filed=1)
    for _ in range(13):
        _narrative(log, "Turn.")
    _historian_run_row(log, turn_seq=10, status="failed")

    assert load_last_historian_run(log, SID)["status"] == "failed"  # the trace
    clock = load_last_completed_historian_run(log, SID)
    assert clock["turn_seq"] == 0  # …the clock did not move
    for committed in (11, 12, 13):
        assert (
            historian_is_due(last_run=clock, committed_turns=committed, cadence=10)
            is True
        )


def test_a_finished_run_whose_lanes_all_failed_is_missed(log):
    """The outage shape FINISHES: ``failed_lanes`` + nothing filed/dropped.

    A lane failure never fails the run, so the status alone cannot tell a broken
    window from a quiet one — the counts can, and the counts say MISSED.
    """
    row = {
        "status": "finished",
        "gate": "proceeded",
        "filed": 0,
        "dropped": 0,
        "failed_lanes": [{"lane": "world", "reason": "Request timed out."}],
    }
    assert historian_run_missed(row) is True
    _historian_run_row(log, turn_seq=10, failed_lanes=row["failed_lanes"])
    assert load_last_completed_historian_run(log, SID) is None
    assert historian_is_due(last_run=None, committed_turns=11, cadence=10) is True


def test_a_quiet_window_consumes_the_cadence(log):
    """A quiet no-op is a JUDGMENT, not a miss: nothing re-runs (§3.6.3)."""
    _historian_run_row(log, turn_seq=10, gate="proceeded")
    clock = load_last_completed_historian_run(log, SID)
    assert clock["turn_seq"] == 10
    assert historian_is_due(last_run=clock, committed_turns=11, cadence=10) is False
    assert historian_is_due(last_run=clock, committed_turns=20, cadence=10) is True


def test_a_partial_run_consumes_even_with_a_failed_lane(log):
    """Anything filed or dropped acted on the window: re-running would re-judge."""
    _historian_run_row(log, turn_seq=10, filed=1, failed_lanes=["books"])
    assert (
        historian_run_missed(
            {"status": "finished", "filed": 1, "failed_lanes": ["books"]}
        )
        is False
    )
    clock = load_last_completed_historian_run(log, SID)
    assert clock["turn_seq"] == 10
    assert historian_is_due(last_run=clock, committed_turns=11, cadence=10) is False


def test_a_quiet_gate_outcome_consumes_the_window(log):
    """``unsure``/``verdict_no``/``no_evidence`` are outcomes, not misses."""
    for gate in ("unsure", "verdict_no", "no_evidence"):
        assert (
            historian_run_missed(
                {
                    "status": "finished",
                    "gate": gate,
                    "filed": 0,
                    "dropped": 0,
                    "failed_lanes": [],
                }
            )
            is False
        )
    _historian_run_row(log, turn_seq=10, gate="unsure")
    clock = load_last_completed_historian_run(log, SID)
    assert clock["gate"] == "unsure"
    assert historian_is_due(last_run=clock, committed_turns=11, cadence=10) is False


def test_a_started_row_alone_never_settles_a_window(log):
    """The opening trace is not the outcome: a ``started`` row stamps no clock.

    A crash mid-run leaves exactly this row, so the next committed turn retries
    instead of the turns being skipped for a cycle (§3.6.3, R10).
    """
    _historian_run_row(log, turn_seq=0, filed=1)
    _historian_run_row(log, turn_seq=10, status="started")

    assert load_last_historian_run(log, SID)["status"] == "started"
    clock = load_last_completed_historian_run(log, SID)
    assert clock["turn_seq"] == 0
    assert historian_is_due(last_run=clock, committed_turns=11, cadence=10) is True


def test_a_first_run_failing_at_the_boundary_retries_and_re_covers(log):
    """The motivating case: nothing completed, so the retry reads the whole chain.

    The first-ever run fires at the cadence boundary and misses; the retry has no
    completed clock to start from, so its window is the WHOLE chain (capped) and
    the turns the miss never judged are covered rather than skipped.
    """
    for index in range(13):
        _narrative(log, f"Turn {index}.")
    assert historian_is_due(last_run=None, committed_turns=10, cadence=10) is True
    _historian_run_row(log, turn_seq=10, status="failed")

    clock = load_last_completed_historian_run(log, SID)
    assert clock is None  # nothing on this chain completed
    for committed in (11, 12, 13):
        assert (
            historian_is_due(last_run=clock, committed_turns=committed, cadence=10)
            is True
        )
    window = historian_window(log, SID, last_run=clock)
    assert window["window_turns"] == 13
    assert window["narrative_parts"][-3:] == ["Turn 10.", "Turn 11.", "Turn 12."]


def test_the_runner_reads_the_cadence_live(log):
    """A preset edit applies at the next evaluation — no rebuild (the planner rule)."""
    _narrative(log)
    _narrative(log, "Elara answers.")
    cadence = {"value": 2}
    runner = _runner(log, cadence=lambda: cadence["value"])
    assert runner.is_due(SID) is True
    cadence["value"] = 3
    assert runner.is_due(SID) is False
    assert runner.due_in(SID) == 1


def test_a_run_resets_the_clock(log):
    """A finished run stamps ``turn_seq``; the next one needs a fresh cadence."""
    _seed_world(log)
    for index in range(2):
        _narrative(log, f"Turn {index}.")
        _world_state(log, venue_names=(WORLD_NAME,))
    runner = _runner(log, cadence=2)
    assert runner.is_due(SID) is True
    result = run(runner.run(SID, trigger="cadence"))
    assert result["turn_seq"] == 2
    assert runner.is_due(SID) is False
    assert runner.due_in(SID) == 2
    _narrative(log, "Turn 3.")
    assert runner.is_due(SID) is False


# -- the window (§3.6.2) -----------------------------------------------------


def test_the_window_starts_after_the_last_run(log):
    for index in range(4):
        _narrative(log, f"Turn {index}.")
    last_run = {"status": "finished", "turn_seq": 2}
    window = historian_window(log, SID, last_run=last_run)
    assert window["window_turns"] == 2
    # One string per TURN, in order: the gate renders a line each (D81).
    assert window["narrative_parts"] == ["Turn 2.", "Turn 3."]


def test_the_window_is_capped_from_the_recent_end(log):
    for index in range(HISTORIAN_EVIDENCE_WINDOW_TURNS + 5):
        _narrative(log, f"Turn {index}. ")
    window = historian_window(log, SID)
    assert window["window_turns"] == HISTORIAN_EVIDENCE_WINDOW_TURNS
    # The cap keeps the NEWEST turns: the window opens at the first kept one.
    first_kept = HISTORIAN_EVIDENCE_WINDOW_TURNS + 5 - HISTORIAN_EVIDENCE_WINDOW_TURNS
    assert window["narrative_parts"][0] == f"Turn {first_kept}. "
    assert len(window["narrative_parts"]) == HISTORIAN_EVIDENCE_WINDOW_TURNS


def test_the_prose_window_keeps_the_recent_most_chars(log):
    from fabulist.prompts.proposals import MAX_PROSE_CHARS

    filler = "a" * MAX_PROSE_CHARS
    _narrative(log, filler)
    _narrative(log, "THE NEWEST LINE.")
    window = historian_window(log, SID)
    parts = window["narrative_parts"]
    # The turns stay separate; the budget is spent from the RECENT end, so the
    # turn that crosses the bound keeps its TAIL and the join is the same 1500
    # chars the lane payload shows (its own clip downstream is a no-op).
    assert parts[-1] == "THE NEWEST LINE."
    assert len(parts[0]) == MAX_PROSE_CHARS - len(parts[-1])
    prose = "".join(parts)
    assert len(prose) == MAX_PROSE_CHARS
    assert prose.endswith("THE NEWEST LINE.")


def test_the_turn_parts_join_to_the_bound_the_lanes_see(log):
    """The lane path is unchanged by the split: join(parts) is the window tail."""
    from fabulist.prompts.proposals import MAX_PROSE_CHARS

    for index in range(6):
        _narrative(log, f"turn{index} " + "b" * 400 + f" tail{index}")
    window = historian_window(log, SID)
    joined = "".join(window["narrative_parts"])
    full = "".join(
        f"turn{index} " + "b" * 400 + f" tail{index}" for index in range(6)
    )
    assert joined == full[-MAX_PROSE_CHARS:]
    assert len(window["narrative_parts"]) > 1


def test_the_union_carries_every_turns_state_and_venue(log):
    first = _narrative(log, "Elara speaks.")
    _world_state(log, state_names=("Elara",), venue_names=("Elara",))
    _narrative(log, "Bram speaks.")
    _world_state(log, state_names=("Bram",), venue_names=("Bram",))
    window = historian_window(log, SID)
    payload = window["world_state_event"]["payload"]
    assert sorted(payload["state"]) == ["Bram", "Elara"]
    assert sorted(payload["venue"]) == ["Bram", "Elara"]
    # The narrative reference is the window's FIRST turn, so the roster diff
    # (admitted_at_seq > seq) spans the whole window instead of its last turn.
    assert window["narrative_event"]["seq"] == first.seq


def test_a_turn_without_a_world_state_row_still_spans_the_window(log):
    """A failed BTS pass contributes no evidence but keeps its place in the span."""
    _narrative(log, "Turn one.")
    _world_state(log, venue_names=(WORLD_NAME,))
    _narrative(log, "Turn two (BTS failed).")
    window = historian_window(log, SID)
    assert window["window_turns"] == 2
    assert WORLD_NAME in window["world_state_event"]["payload"]["venue"]


# -- the run over the real fan-out (§3.6.1) ----------------------------------


def test_a_run_files_the_judged_rows_and_the_summary(log):
    """The four halves in the graph's order: dispatcher → route → lanes → join."""
    _seed_world(log)
    _narrative(log, "Elara says the mill has stood a hundred years.")
    _world_state(log, venue_names=(WORLD_NAME,))
    runner = _runner(log)
    result = run(runner.run(SID, trigger="cadence"))

    assert result["gate"] == "proceeded"
    assert result["filed"] == 1
    assert result["dropped"] == 0
    assert result["failed_lanes"] == []
    assert result["turn_seq"] == 1
    rows = _proposal_rows(log)
    assert [(row["kind"], row["action"], row["target"]) for row in rows] == [
        ("world", "amend", WORLD_ID)
    ]
    assert rows[0]["origin"] == "bts"  # §3.6.5: filing identity is unchanged
    assert _summaries(log)[0]["kept"] == 1


def test_a_window_whose_evidence_is_only_in_an_early_turn_still_files_rows(log):
    """The MERGED-STATE union is the mechanism under test (§3.6.2, §8.10a).

    Turn 1 names the world and its venue; turns 2 and 3 report an EMPTY
    world-state. Last-turn-only evidence would gate the run off ("no_evidence")
    and file nothing — the union is what makes the early turn's touched entities
    reach the lanes.
    """
    _seed_world(log)
    _narrative(log, "Elara says the mill has stood a hundred years.")
    _world_state(log, venue_names=(WORLD_NAME,))
    _narrative(log, "A quiet beat passes.")
    _world_state(log)
    _narrative(log, "Another quiet beat.")
    _world_state(log)

    runner = _runner(log)
    result = run(runner.run(SID, trigger="cadence"))

    assert _system_rows(log, "historian_run")[0]["window_turns"] == 3
    assert result["gate"] == "proceeded"
    assert result["filed"] == 1
    assert [(row["kind"], row["target"]) for row in _proposal_rows(log)] == [
        ("world", WORLD_ID)
    ]


def test_a_quiet_window_spends_no_generator_call_and_appends_nothing(log):
    """The zero-cost contract survives the move off the turn."""
    _narrative(log, "Nothing here touches an asset.")
    _world_state(log)
    generator = FakeGenerator({"world": {"proposals": [_world_amend_row()]}})
    runner = _runner(log, fanout=_fanout(log, generator=generator))
    result = run(runner.run(SID, trigger="cadence"))

    assert result["gate"] == "no_evidence"
    assert generator.calls == []
    assert _proposal_rows(log) == []
    assert _summaries(log) == []


def test_a_run_files_no_cast_row_though_the_roster_diff_is_non_empty(log):
    """§3.6.6: the cast door is the TURN's; the historian's lanes never carry it.

    The window admits a member (``admitted_at_seq`` after the window's first
    narrative), so ``proposal_evidence`` reports a non-empty ``admitted`` slice —
    and the actors lane, which is the only lane that may see it, still files
    nothing with ``kind="cast"``.
    """
    from fabulist.state.cast import admit_member

    _seed_world(log)
    _narrative(log, "A stranger steps out of the rain.")
    admit_member(
        log,
        SID,
        "Marta",
        provenance="engine",
        admitted_by="test",
        reference_kind="identity",
    )
    _world_state(log, venue_names=(WORLD_NAME,))
    # The roster diff — not the payload — carries the admitted member, so the
    # evidence really is non-empty for the actors lane.
    from fabulist.state.proposal_lanes import proposal_evidence

    window = historian_window(log, SID)
    assert proposal_evidence(
        log,
        SID,
        {
            "narrative_event": window["narrative_event"],
            "world_state_event": window["world_state_event"],
        },
    ).admitted

    generic = FakeGenerator(
        {
            "actors": {
                "proposals": [
                    {
                        "kind": "cast",
                        "action": "add",
                        "patch": {"name": "Marta", "description": "A stranger."},
                        "evidence": "A stranger steps out of the rain.",
                    }
                ]
            },
            "world": {"proposals": [_world_amend_row()]},
        }
    )
    runner = _runner(log, fanout=_fanout(log, generator=generic))
    run(runner.run(SID, trigger="cadence"))

    assert [row["kind"] for row in _proposal_rows(log)] == ["world"]


def test_the_run_trace_is_started_then_finished(log):
    """§3.6.3: the STARTED stamp, then the finished row with the run's counts."""
    _seed_world(log)
    _narrative(log, "Elara says the mill has stood a hundred years.")
    _world_state(log, venue_names=(WORLD_NAME,))
    run(_runner(log).run(SID, trigger="manual"))

    rows = _system_rows(log, "historian_run")
    assert [row["status"] for row in rows] == ["started", "finished"]
    started, finished = rows
    assert started["trigger"] == "manual"
    assert (started["turn_seq"], started["window_turns"]) == (1, 1)
    assert finished["gate"] == "proceeded"
    assert (finished["filed"], finished["dropped"]) == (1, 0)
    assert finished["failed_lanes"] == []
    assert isinstance(finished["duration_ms"], int)


def test_the_completed_run_is_the_clock(log):
    """A FINISHED run that consumed its window is what the due rule reads back."""
    _narrative(log, "Only turn.")
    _world_state(log)
    runner = _runner(log, cadence=1)
    assert runner.is_due(SID) is True
    run(runner.run(SID, trigger="cadence"))
    assert load_last_historian_run(log, SID)["status"] == "finished"
    assert load_last_completed_historian_run(log, SID)["status"] == "finished"
    assert runner.is_due(SID) is False


def test_a_manual_run_is_not_gated_by_due_ness(log):
    """The owner asked for it now: the cadence clock is not consulted (§3.6.1)."""
    _narrative(log, "Only turn.")
    _world_state(log)
    runner = _runner(log, cadence=10)
    assert runner.is_due(SID) is False
    result = run(runner.run(SID, trigger="manual"))
    assert result["gate"] == "no_evidence"
    assert [row["trigger"] for row in _system_rows(log, "historian_run")] == [
        "manual",
        "manual",
    ]


def test_a_second_run_is_refused_while_one_is_in_flight(log):
    """The in-flight guard (§3.6.8): one run per session per process."""
    _seed_world(log)
    _narrative(log, "Elara says the mill has stood a hundred years.")
    _world_state(log, venue_names=(WORLD_NAME,))
    generator = FakeGenerator({"world": {"proposals": [_world_amend_row()]}}, delay=0.2)
    runner = _runner(log, fanout=_fanout(log, generator=generator))

    async def scenario():
        first = asyncio.create_task(runner.run(SID, trigger="manual"))
        await asyncio.sleep(0.05)
        assert runner.inflight(SID) is True
        with pytest.raises(HistorianBusy):
            await runner.run(SID, trigger="manual")
        result = await first
        assert runner.inflight(SID) is False
        return result

    assert run(scenario())["gate"] == "proceeded"
    assert [row["status"] for row in _system_rows(log, "historian_run")] == [
        "started",
        "finished",
    ]


class _RaisingFanout:
    """A fan-out whose dispatcher raises (a runner-level failure)."""

    fanout_node = "asset_proposal_lane"

    async def __call__(self, state):  # pragma: no cover - raises
        raise RuntimeError("lane system exploded")


class _FlakyFanout:
    """A fan-out whose FIRST dispatcher call raises, then behaves (a one-off outage).

    The retry case needs a real outage followed by a real run, so the failure has
    to be spent once: after it the calls delegate to the real lane system.
    """

    fanout_node = "asset_proposal_lane"

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    async def __call__(self, state):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("lane system exploded")
        return await self.inner(state)

    async def route(self, state):
        return await self.inner.route(state)

    def join(self, state):
        return self.inner.join(state)

    async def lane(self, payload):
        return await self.inner.lane(payload)


class TimingOutGenerator(FakeGenerator):
    """A lane generator whose call for one lane raises the CONNECTION's timeout.

    The only transport bound is the connection's ``timeout`` field
    (``connections/registry.py`` -> ``connections/client.py``); when it expires
    the OpenAI client raises out of the stream, so the builtin ``TimeoutError``
    raised here is what the lane seam really sees. There is no run-total bound
    above it: the per-lane failure path is the whole containment.
    """

    def __init__(self, batches=None, *, late_lane: str):
        super().__init__(batches)
        self.late_lane = late_lane

    async def __call__(self, messages, stream=True, **kwargs):
        if self.lane_of(messages) == self.late_lane:
            self.calls.append(messages)
            raise TimeoutError("Request timed out.")
        async for event in super().__call__(messages, stream=stream, **kwargs):
            yield event


def _actor_add_row():
    """One legal actors-lane row (an ``add`` carries the whole definition)."""
    return {
        "kind": "actor",
        "action": "add",
        "patch": {"name": "Marta", "description": "A stranger out of the rain."},
        "evidence": "Marta steps out of the rain.",
    }


def _book_add_row():
    """One legal books-lane row (``entries`` is a list of entry OBJECTS)."""
    return {
        "kind": "book",
        "action": "add",
        "patch": {
            "name": "Mill lore",
            "entries": [
                {"keys": ["mill"], "content": "The mill has stood a hundred years."}
            ],
        },
        "evidence": "Elara says the mill has stood a hundred years.",
    }


def test_a_lane_that_hits_its_connection_timeout_fails_alone(log):
    """THE per-lane bound: a timed-out lane never costs the run its other lanes.

    The bound is the CONNECTION's ``timeout`` (owner-configured per connection),
    so the failure arrives as a raise out of the lane call. The fan-out's lane
    node contains it (``failed_lanes``), the other two lanes still file, the run
    finishes with its real counts, and NO ``failed`` run row is stamped — the
    run was never timed out, one call was.
    """
    _seed_world(log)
    _narrative(log, "Elara says the mill has stood a hundred years.")
    _world_state(log, venue_names=(WORLD_NAME,))
    generator = TimingOutGenerator(
        {
            "actors": {"proposals": [_actor_add_row()]},
            "books": {"proposals": [_book_add_row()]},
        },
        late_lane="world",
    )
    runner = _runner(log, fanout=_fanout(log, generator=generator), cadence=1)

    result = run(runner.run(SID, trigger="manual"))

    assert result["gate"] == "proceeded"
    assert result["filed"] == 2  # actors + books; the world lane filed nothing
    assert [row["lane"] for row in result["failed_lanes"]] == ["world"]
    assert "timed out" in result["failed_lanes"][0]["reason"]
    assert "timed_out" not in result  # the run-total marker is gone with the bound
    assert "error" not in result
    # The run's trace is a FINISHED run, not an abandoned one.
    rows = _system_rows(log, "historian_run")
    assert [row["status"] for row in rows] == ["started", "finished"]
    assert rows[-1]["failed_lanes"] == result["failed_lanes"]
    # Both surviving lanes' rows are on the log (append-only, D9).
    assert sorted(row["kind"] for row in _proposal_rows(log)) == ["actor", "book"]
    # The guard was released by the run's own ``finally``.
    assert runner.inflight(SID) is False
    assert runner.is_due(SID) is False


def test_the_run_has_no_total_time_bound():
    """The run-total bound is GONE: no symbol, no result key, no cap.

    Pinned by import: a reintroduced run budget would have to re-publish
    ``HISTORIAN_RUN_TIMEOUT_SECONDS``, which the owner's ruling retired in favour
    of the connections' own ``timeout`` fields.
    """
    assert not hasattr(historian_module, "HISTORIAN_RUN_TIMEOUT_SECONDS")
    assert "HISTORIAN_RUN_TIMEOUT_SECONDS" not in historian_module.__all__
    # ...and the runner's own module surface carries no timing knob at all.
    assert [name for name in vars(historian_module) if "TIMEOUT" in name] == []


def test_a_runner_level_failure_is_contained_and_retried_next_turn(log):
    """§3.6.8: a failed run appends ``failed``, logs, and consumes NO cycle."""
    _narrative(log, "Only turn.")
    _world_state(log)
    runner = _runner(log, fanout=_RaisingFanout(), cadence=1)
    result = run(runner.run(SID, trigger="cadence"))

    assert result["gate"] == "error"
    assert (result["filed"], result["dropped"], result["failed_lanes"]) == (0, 0, [])
    assert result["turn_seq"] == 1
    assert isinstance(result["duration_ms"], int)
    failed = _system_rows(log, "historian_run")[-1]
    assert failed["status"] == "failed"
    assert "exploded" in failed["error"]
    # The guard is released in the ``finally`` — a failed run never wedges it.
    assert runner.inflight(SID) is False
    # The clock did NOT advance (the window was never judged), so the next
    # committed turn is due — a retry, every turn, until a run completes.
    assert load_last_completed_historian_run(log, SID) is None
    assert runner.is_due(SID) is True
    assert runner.due_in(SID) == 0


def test_a_missed_run_is_retried_over_the_turns_it_never_covered(log):
    """The motivating case, end to end: the retry re-reads the missed turns.

    A first run misses (the dispatcher raised), so the next committed turn retries
    and its window starts from the last COMPLETED run — ``None`` here — covering
    all three turns, not just the one after the failure.
    """
    _seed_world(log)
    for index in range(2):
        _narrative(log, f"Turn {index}.")
        _world_state(log, venue_names=(WORLD_NAME,))
    runner = _runner(log, fanout=_FlakyFanout(_fanout(log)), cadence=2)
    assert runner.is_due(SID) is True

    failed = run(runner.run(SID, trigger="cadence"))
    assert failed["gate"] == "error"
    assert load_last_historian_run(log, SID)["status"] == "failed"  # the trace
    assert load_last_completed_historian_run(log, SID) is None  # not the clock
    assert runner.is_due(SID) is True
    assert runner.due_in(SID) == 0

    _narrative(log, "Turn 2.")
    _world_state(log, venue_names=(WORLD_NAME,))
    assert runner.is_due(SID) is True
    result = run(runner.run(SID, trigger="cadence"))

    assert result["turn_seq"] == 3
    assert result["filed"] == 1
    # The retry's STARTED row counts every turn — the two the miss never judged
    # included — because the window opens after the last COMPLETED run.
    starts = [
        row for row in _system_rows(log, "historian_run") if row["status"] == "started"
    ]
    assert [row["window_turns"] for row in starts] == [2, 3]
    assert load_last_completed_historian_run(log, SID)["turn_seq"] == 3
    assert runner.is_due(SID) is False
    assert runner.due_in(SID) == 2


def test_a_failed_lane_is_recorded_and_the_run_still_finishes(log):
    """Lane failures are the summary's business (existing behaviour), not the run's."""
    _seed_world(log)
    _narrative(log, "Elara says the mill has stood a hundred years.")
    _world_state(log, venue_names=(WORLD_NAME,))
    generator = FakeGenerator({"world": RuntimeError("provider down")})
    runner = _runner(log, fanout=_fanout(log, generator=generator))
    result = run(runner.run(SID, trigger="cadence"))

    assert result["gate"] == "proceeded"
    assert result["filed"] == 0
    assert [row["lane"] for row in result["failed_lanes"]] == ["world"]
    assert _system_rows(log, "historian_run")[-1]["status"] == "finished"


def test_an_unsure_gate_answer_is_recorded_as_unsure_on_the_run_row(log):
    """P2: a guessing gate answer is a distinct run outcome, not a quiet one.

    The finished row is the run's trace, so the new gate value must survive the
    append gate (``HISTORIAN_RUN_GATES``) — an unknown value would cost the row,
    and the drawer's line reads it back.
    """
    _seed_world(log)
    _narrative(log, "Elara says the mill has stood a hundred years.")
    _world_state(log, venue_names=(WORLD_NAME,))
    generator = FakeGenerator({"world": {"proposals": [_world_amend_row()]}})
    runner = _runner(
        log,
        fanout=_fanout(
            log,
            generator=generator,
            decision=FakeDecision(gate=0.9, confidence=0.1),
        ),
    )
    result = run(runner.run(SID, trigger="manual"))

    assert result["gate"] == "unsure"
    assert result["filed"] == 0
    assert generator.calls == []  # no lane ran
    assert _proposal_rows(log) == []
    finished = _system_rows(log, "historian_run")[-1]
    assert finished["status"] == "finished"
    assert finished["gate"] == "unsure"  # the schema gate accepted the new value


def test_the_drawer_run_line_shows_the_unsure_outcome():
    """The drawer's line renders ``unsure`` as a question, not as "0 filed".

    There is no JS test runner in this repo, so the component's contract is
    pinned at its source, the way ``test_transcript_flag_marker.py`` pins the
    transcript's render rule (the frontend is checked by ``npm run check`` /
    ``npm run build`` in the lane's verification step).
    """
    from pathlib import Path

    drawer = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "src"
        / "routes"
        / "chat"
        / "ProposalDrawer.svelte"
    )
    source = drawer.read_text(encoding="utf-8")
    run_line = source.split("const runLine = $derived.by(() => {", 1)[1]
    run_line = run_line.split("\t});", 1)[0]

    # The gate value the engine writes is read from the status wire as-is.
    assert "last.gate === 'unsure'" in run_line
    # ...and it renders a question (a run now), not a filed count.
    assert "historian unsure, run now?" in run_line
    assert "filed" in run_line  # the ordinary line is untouched


@pytest.fixture
def log(tmp_path):
    event_log = EventLog(tmp_path / "historian.db")
    try:
        yield event_log
    finally:
        event_log.close()


# -- W9.30 §5.2: the cast ladder's promotion rung rides the run --------------
#
# The promotion pass is not a lane (no window, no gate, no model): it is
# deterministic engine code that runs INSIDE ``run``, between the lanes and the
# ``finished`` row, and counts what it filed. Its own failure is contained
# INTERNALLY (a warning, ``promotions: 0``) because a raise there would land in
# ``run``'s except, stamp ``failed`` and — via the missed-run rule — retry the
# whole window over a review-queue nicety.


def _actor_member(log, name="Tessa", *, session_id=SID):
    """A member on the promotion rung: the D79 accept's provenance pair."""
    from fabulist.state.cast import admit_member, member_by_name, roster

    admit_member(log, session_id, name, provenance="actor", admitted_by="owner")
    member = member_by_name(roster(log, session_id), name)
    assert member is not None
    return member


def _three_facts(log, member_id, *, tag="", session_id=SID):
    """The promotion bar: one relationship edge, one meter, one held item."""
    from fabulist.state import entities

    entities.establish_relationship(
        log, session_id, member_id, "player", name="employer"
    )
    entities.set_attribute(log, session_id, member_id, key="resolve", value=7)
    entities.establish_item(
        log, session_id, f"brass key{tag}", holder=member_id
    )


def _promotion_rows(log, session_id=SID):
    return [
        row
        for row in _proposal_rows(log, session_id)
        if row.get("origin") == "promotion"
    ]


def test_the_run_files_a_promotion_row_and_counts_it(log):
    """The pass rides the cadence: one review row, counted on ``finished``."""
    _seed_world(log)
    _narrative(log, "Elara says the mill has stood a hundred years.")
    _world_state(log, venue_names=(WORLD_NAME,))
    member = _actor_member(log, "Tessa")
    _three_facts(log, member.member_id)

    result = run(_runner(log).run(SID, trigger="manual"))

    assert result["promotions"] == 1
    finished = _system_rows(log, "historian_run")[-1]
    assert finished["status"] == "finished"
    assert finished["promotions"] == 1
    # The lanes' own rows are untouched: the promotion is an ADDITIONAL row.
    assert finished["filed"] == 1
    rows = _promotion_rows(log)
    assert len(rows) == 1
    assert rows[0]["kind"] == "actor"
    assert rows[0]["patch"]["name"] == "Tessa"
    assert "cast member Tessa has 3 tracked facts" in rows[0]["evidence"]


def test_a_run_with_no_candidate_counts_zero_promotions(log):
    """The count is always on the row — "0" means "asked, nobody qualified"."""
    _narrative(log, "Only turn.")
    _world_state(log)

    result = run(_runner(log).run(SID, trigger="manual"))

    assert result["promotions"] == 0
    assert _system_rows(log, "historian_run")[-1]["promotions"] == 0
    assert _promotion_rows(log) == []


def test_a_promotion_candidate_below_the_bar_files_nothing(log):
    """The bar is the pass's, and it is judged inside the run."""
    from fabulist.state import entities
    from fabulist.state import promotion as promotion_module

    _narrative(log, "Only turn.")
    _world_state(log)
    member = _actor_member(log, "Tessa")
    entities.establish_item(log, SID, "brass key", holder=member.member_id)

    result = run(_runner(log).run(SID, trigger="manual"))

    assert result["promotions"] == 0
    assert _promotion_rows(log) == []
    assert promotion_module.PROMOTION_MIN_FACTS == 3


def test_a_promotion_pass_failure_never_fails_the_run(log, monkeypatch):
    """§5.2 containment: the run finishes, counts 0, and the clock advances."""
    def _boom(log_, session_id):
        raise RuntimeError("promotion pass exploded")

    monkeypatch.setattr(historian_module, "promotion_pass", _boom)
    _narrative(log, "Only turn.")
    _world_state(log)
    member = _actor_member(log, "Tessa")
    _three_facts(log, member.member_id)

    result = run(_runner(log, cadence=1).run(SID, trigger="manual"))

    assert "error" not in result
    assert result["promotions"] == 0
    rows = _system_rows(log, "historian_run")
    assert [row["status"] for row in rows] == ["started", "finished"]
    assert rows[-1]["promotions"] == 0
    # The window was CONSUMED (a finished run) — the failure cost the count, not
    # the cycle, so the due rule does not retry over it.
    assert load_last_completed_historian_run(log, SID)["status"] == "finished"
