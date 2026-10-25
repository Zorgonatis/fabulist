"""W9.26 §2.1/§2.3 — the context gauge, the claim protocol and the job runner.

Every test drives the REAL :class:`fabulist.log.EventLog` (tmp SQLite) so the
log-derived guards are exercised the way production exercises them: the gauge
reads usage rows, the convergence guard counts own ``compaction_failed`` rows,
and the idempotent successor check walks the fork bookkeeping.

The compact SERVICE is a contract-shaped fake (Lane A's real service lands in
parallel); the runner is called through its public entry points only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fabulist.connections.registry import Connection, Registry
from fabulist.graph.compaction import RESERVE_TOKENS
from fabulist.graph.context_monitor import (
    COMPACTION_MARKER_EVENT,
    CONTEXT_FORCE_RATIO,
    CONTEXT_LOW_EVENT,
    CONTEXT_LOW_RATIO,
    FORCED_TRIGGER,
    FRESH_ERA_MIN_UNCOVERED_TURNS,
    MANUAL_TRIGGER,
    MAX_FORCED_ATTEMPTS,
    MONITOR_HOOK_NAME,
    NOOP_ALREADY_COMPACTED,
    NOOP_ATTEMPT_LIMIT,
    NOOP_INHERITED_COVER,
    NOOP_MESSAGES,
    NOOP_NOTHING_UNCOVERED,
    POST_TURN_FAILED_EVENT,
    VARIANT_NOOP,
    VARIANT_OVER_BUDGET,
    VARIANT_PLAIN,
    CompactionClaims,
    ContextJobs,
    acquire_claim,
    claim_acquire,
    claim_held,
    claim_release,
    compaction_successor,
    covering_summary_owner,
    emit_context_low,
    failed_attempts,
    has_completed_compaction,
    has_era_marker,
    idempotent_successor,
    make_context_monitor_hook,
    measurement_boundary,
    PollingTurnSlot,
    measure_context,
    narrate_labels,
    run_compact_job,
    serving_connection,
    session_title,
    slot_labels,
    uncovered_turn_groups,
)
from fabulist.log import EventLog

SID = "s-context"
CHILD = "s-context-era2"

#: window 20512 − response 4000 − reserve 512 = budget 16000 (clean math).
WINDOW = 20512
RESPONSE = 4000
BUDGET = WINDOW - RESPONSE - RESERVE_TOKENS
assert BUDGET == 16000


def run(coro):
    return asyncio.run(coro)


# --- harness ---------------------------------------------------------------


def connection(
    name: str = "your-model-id",
    *,
    model: str = "narrate-model",
    window: int | None = WINDOW,
    response: int | None = RESPONSE,
) -> Connection:
    """A real :class:`Connection` (Response Length = ``sampling.max_tokens``)."""
    sampling = {} if response is None else {"max_tokens": response}
    return Connection(
        name=name,
        base_url="http://localhost:4444/v1",
        model=model,
        context_window=window,
        sampling=sampling,
    )


def registry(*connections: Connection, agents: dict[str, str] | None = None) -> Registry:
    return Registry(
        connections={c.name: c for c in connections},
        agents=agents or {"narrator": connections[0].name},
    )


def add_usage(log, session, *, agent="narrator", model="narrate-model", prompt_tokens=12000):
    return log.append_event(
        session,
        "system",
        agent,
        {
            "event": "usage",
            "agent": agent,
            "model": model,
            "session_id": session,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 100,
            "total_tokens": prompt_tokens + 100,
            "latency_s": 1.0,
        },
    )


def add_user(log, session, text="I wait."):
    return log.append_event(session, "user", "user", {"text": text})


def add_narrative(log, session, prose="The rain keeps on."):
    """Commit one turn's narrative row.

    LIVE ordering: the engine commits the narrative AFTER the turn's narrate
    usage rows and ``api/app.py`` schedules the post-turn hooks (the context
    monitor) after that commit. So a measured turn is always
    ``user → usage rows → narrative → hook``; the narrative that closes the
    PREVIOUS turn is the measurement boundary.
    """
    return log.append_event(
        session, "narrative", "narrator", {"prose": prose, "dsl": "", "segments": []}
    )


def add_turn(log, session, prose="The rain keeps on."):
    """One committed turn with no measurements: a user row + its narrative row."""
    add_user(log, session)
    return add_narrative(log, session, prose)


def add_summary(log, session, covers, *, text="SUMMARY", trigger=None, absorbs=None):
    payload = {"text": text, "covers_seqs": list(covers), "reason": "seeded"}
    if trigger:
        payload["trigger"] = trigger
    if absorbs:
        payload["absorbs"] = list(absorbs)
    return log.append_event(session, "summary", "summarizer", payload)


def add_marker(log, session, *, source=SID, summary_ref=None):
    return log.append_event(
        session,
        "system",
        "system",
        {
            "event": COMPACTION_MARKER_EVENT,
            "source_session": source,
            "summary_ref": list(summary_ref or [source, 0]),
            "covers_seqs": [0, 0],
            "trigger": MANUAL_TRIGGER,
            "stats": {"calls": 1, "prompt_tokens": 10, "model": "m"},
        },
    )


def complete_era(log, session, *, child=CHILD, title="The Long Night"):
    """Land one whole era the way the service does: summary → fork → marker/title."""
    head = max(event.seq for event in log.get_chain_events(session))
    summary = add_summary(log, session, [1, head], trigger=MANUAL_TRIGGER)
    log.fork_session(session, summary.seq, child)
    add_marker(log, child, source=session, summary_ref=[session, summary.seq])
    log.append_event(
        child, "system", "system", {"event": "session_renamed", "title": title}
    )
    return summary, child


def context_rows(log, session=SID, variant=None):
    """Every ``context_low`` row on the session (optionally one variant)."""
    rows = []
    for event in log.get_events(session):
        if event.type != "system" or event.payload.get("event") != CONTEXT_LOW_EVENT:
            continue
        if variant == VARIANT_OVER_BUDGET and not event.payload.get("over_budget"):
            continue
        if variant == VARIANT_NOOP and not event.payload.get("noop"):
            continue
        if variant == VARIANT_PLAIN and (
            event.payload.get("noop") or event.payload.get("over_budget")
        ):
            continue
        rows.append(event)
    return rows


class FakeCompactService:
    """Contract-shaped fake of Lane A's ``compact()`` service.

    ``async def compact(self, session_id, log, *, trigger, job)`` — the shape the
    runner codes against; it lands an era and reports the successor.
    """

    def __init__(self, *, status="done", child=CHILD, stage=None, error=None, reason=None):
        self.status = status
        self.child = child
        self.stage = stage
        self.error = error
        self.reason = reason
        self.calls: list[dict] = []

    async def compact(self, session_id, log, *, trigger, job):
        self.calls.append({"session_id": session_id, "trigger": trigger, "job": job})
        if self.status != "done":
            return {
                "status": self.status,
                "stage": self.stage,
                "error": self.error,
                "reason": self.reason,
            }
        job.add_chunk("map chunk 1")
        complete_era(log, session_id, child=self.child)
        return {"status": "done", "successor_session_id": self.child}


class RaisingService:
    """A service that raises: the job must fail, release, and record the row."""

    def __init__(self) -> None:
        self.calls = 0

    async def compact(self, session_id, log, *, trigger, job):
        self.calls += 1
        raise RuntimeError("summarizer exploded")


class FakeSlot:
    """A turn-slot view: ``busy()`` + ``async wait_idle()`` (the adapter shape)."""

    def __init__(self, busy: bool = False, *, clear_after: int = 1) -> None:
        self._busy = busy
        self._clear_after = clear_after
        self.waits = 0

    def busy(self, session_id=None):
        return self._busy

    async def wait_idle(self, session_id=None):
        self.waits += 1
        if self.waits >= self._clear_after:
            self._busy = False


@pytest.fixture
def log(tmp_path: Path):
    repo = EventLog(tmp_path / "events.db")
    yield repo
    repo.close()


# --- the gauge -------------------------------------------------------------


def test_reading_math_uses_declared_window_and_sampling(log):
    """budget = window − sampling.max_tokens − RESERVE (never a response_length field)."""
    add_usage(log, SID, prompt_tokens=12000)
    reading = measure_context(log, registry(connection()), SID)

    assert reading is not None
    assert reading.context_window == WINDOW
    assert reading.response_length == RESPONSE
    assert reading.budget_tokens == BUDGET
    assert reading.prompt_tokens == 12000
    assert reading.ratio == round(12000 / BUDGET, 4)
    assert reading.agent == "narrator"
    assert reading.basis == "measured"
    assert reading.over_budget is False


def test_un_authed_sampling_falls_back_to_default_max_tokens(log):
    """An un-authored sampler still yields the number the request will carry."""
    from fabulist.connections.client import DEFAULT_SAMPLING

    add_usage(log, SID, prompt_tokens=1000)
    reading = measure_context(log, registry(connection(response=None)), SID)
    assert reading is not None
    assert reading.response_length == int(DEFAULT_SAMPLING["max_tokens"])


def test_plain_threshold_emits_once_per_session(log):
    """≥0.8 ⇒ one plain row; a second turn at the same ratio adds nothing."""
    add_usage(log, SID, prompt_tokens=int(BUDGET * CONTEXT_LOW_RATIO))
    hook = make_context_monitor_hook(
        log=log, registry=registry(connection()), bindings={"narrate": {"agent": "narrator"}}
    )
    run(hook(SID))
    run(hook(SID))

    rows = context_rows(log, variant=VARIANT_PLAIN)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["event"] == CONTEXT_LOW_EVENT
    assert payload["agent"] == "narrator"
    assert payload["ratio"] == 0.8
    assert isinstance(payload["ratio"], float)  # the gate is FLOAT-only
    assert payload["prompt_tokens"] == int(BUDGET * CONTEXT_LOW_RATIO)
    assert payload["budget_tokens"] == BUDGET
    assert payload["basis"] == "measured"
    # Omitted unless true — never emitted false (§2.2).
    assert "over_budget" not in payload
    assert "noop" not in payload


def test_below_threshold_writes_nothing(log):
    add_usage(log, SID, prompt_tokens=int(BUDGET * 0.79))
    hook = make_context_monitor_hook(log=log, registry=registry(connection()))
    run(hook(SID))
    assert context_rows(log) == []


def test_label_union_covers_both_twin_spellings(log):
    """A row labeled ``completion.narration`` measures the ``narrator`` slot."""
    typed = connection("cn-conn")
    reg = registry(typed, agents={"completion.narration": "cn-conn"})
    add_usage(log, SID, agent="completion.narration", prompt_tokens=12800)

    reading = measure_context(log, reg, SID)
    assert reading is not None
    assert reading.agent == "completion.narration"
    assert reading.prompt_tokens == 12800
    assert set(slot_labels("completion.narration")) == {"completion.narration", "narrator"}
    assert set(narrate_labels("completion.narration")) == {
        "completion.narration",
        "narrate",
        "narrator",
    }

    # The legacy spelling written into the row is the SAME slot.
    add_usage(log, SID, agent="narrator", prompt_tokens=13000)
    again = measure_context(log, reg, SID)
    assert again is not None and again.prompt_tokens == 13000


def test_newest_row_wins_and_zero_rows_are_not_measurements(log):
    add_usage(log, SID, prompt_tokens=9000)
    add_usage(log, SID, prompt_tokens=0)  # a zeroed/failed call says nothing
    add_usage(log, SID, prompt_tokens=11000)
    reading = measure_context(log, registry(connection()), SID)
    assert reading is not None and reading.prompt_tokens == 11000


def test_turn_peak_reads_the_main_call_not_its_trailing_repair(log):
    """LIVE REPRO: the NEWEST row was a 1008-token repair; the turn peaked at 6431.

    A committed turn's narrate rows were
    ``[3369, 3402, 3848, 4243, 4313, 1293, 6431, 1008]`` — the main call (6431)
    followed by small repair/patch calls (1293, 1008), all the same agent. The
    old newest-row read saw 1008 (= ratio 0.14) and stayed silent at a true
    pressure of 0.897 (budget ≈ 7169); the turn-scoped peak sees 6431.

    The fixture is the LIVE sequence, in order: previous turn's rows → previous
    narrative (the boundary) → this turn's user row → this turn's usage rows →
    THIS turn's narrative → the hook.
    """
    live_budget = 7169  # the live turn's budget: 6431 / 7169 = 0.897
    conn = connection(window=live_budget + RESPONSE + RESERVE_TOKENS)
    add_user(log, SID)  # the PREVIOUS turn ...
    add_usage(log, SID, prompt_tokens=5710)
    add_narrative(log, SID)  # ... whose narrative is the measurement boundary
    add_user(log, SID)  # this turn begins
    for tokens in (3369, 3402, 3848, 4243, 4313, 1293, 6431, 1008):
        add_usage(log, SID, prompt_tokens=tokens)
    add_narrative(log, SID)  # this turn's OWN narrative commits BEFORE the hook

    reading = measure_context(log, registry(conn), SID)
    assert reading is not None
    assert reading.budget_tokens == live_budget
    assert reading.prompt_tokens == 6431  # the main call, not the repair tail
    assert reading.ratio == round(6431 / live_budget, 4)
    assert reading.ratio > CONTEXT_LOW_RATIO
    # ... whereas the newest row alone would have been silent:
    assert round(1008 / live_budget, 4) < CONTEXT_LOW_RATIO

    # And the notice FIRES: the old newest-row read emitted nothing here.
    hook = make_context_monitor_hook(log=log, registry=registry(conn))
    run(hook(SID))
    rows = context_rows(log, variant=VARIANT_PLAIN)
    assert len(rows) == 1
    assert rows[0].payload["prompt_tokens"] == 6431
    assert rows[0].payload["budget_tokens"] == live_budget


def test_live_post_commit_ordering_measures_the_own_turn(log):
    """REGRESSION PIN — the exact live ordering that made the gauge skip turns.

    Live: ``prev narrative … [user] [3 narrate usage rows] [narrative COMMIT]
    [post-turn tail] → hook``. At hook time the NEWEST committed narrative is
    this turn's own (seq 576 live), so a boundary at it (the old
    ``last_narrative_seq``) put every usage row of the turn BEFORE the window and
    the gauge found no row at all ⇒ silent every turn (no ``context_low`` at a
    true ratio of 0.966). The boundary must be the narrative that closed the
    PREVIOUS turn.
    """
    live_budget = 7169
    conn = connection(window=live_budget + RESPONSE + RESERVE_TOKENS)
    add_user(log, SID)  # prev turn
    add_usage(log, SID, prompt_tokens=6400)
    prev_narrative = add_narrative(log, SID)  # the boundary
    add_user(log, SID)
    for tokens in (6431, 1293, 1008):  # main call + its two repairs
        add_usage(log, SID, prompt_tokens=tokens)
    this_narrative = add_narrative(log, SID)  # the turn's OWN narrative

    # The boundary is the PREVIOUS narrative, never the newest one.
    assert this_narrative.seq > prev_narrative.seq
    assert measurement_boundary(log.get_events(SID)) == prev_narrative.seq

    hook = make_context_monitor_hook(log=log, registry=registry(conn))
    run(hook(SID))
    rows = context_rows(log, variant=VARIANT_PLAIN)
    assert len(rows) == 1, "the gauge must not skip a live post-commit turn"
    assert rows[0].payload["prompt_tokens"] == 6431
    assert rows[0].payload["ratio"] == round(6431 / live_budget, 4)

    # ... and the POST-TURN tail (rows appended after this turn's narrative) is
    # out of scope by LABEL, not by an upper bound: a foreign-label row after the
    # commit changes nothing.
    add_usage(log, SID, agent="summarizer", prompt_tokens=99999)
    assert measure_context(log, registry(conn), SID).prompt_tokens == 6431


def test_first_turn_measures_the_max_of_all_own_rows(log):
    """ONE committed narrative (the first turn) ⇒ EVERY own matching row counts."""
    assert measure_context(log, registry(connection()), SID) is None  # empty log
    assert measurement_boundary([]) is None
    add_user(log, SID)
    add_usage(log, SID, prompt_tokens=3000)
    add_usage(log, SID, prompt_tokens=13000)
    add_usage(log, SID, prompt_tokens=1200)  # a repair tail cannot lower it
    add_narrative(log, SID)  # the first turn commits — still no PREVIOUS turn

    assert measurement_boundary(log.get_events(SID)) is None
    reading = measure_context(log, registry(connection()), SID)
    assert reading is not None
    assert reading.prompt_tokens == 13000
    assert reading.ratio == round(13000 / BUDGET, 4)


def test_second_turn_ignores_rows_before_the_previous_narrative(log):
    """Turn-scoped: turn 1's rows precede turn 1's narrative = the boundary."""
    add_user(log, SID)
    add_usage(log, SID, prompt_tokens=15000)  # turn 1: ratio 0.9375
    add_narrative(log, SID)  # turn 1 commits — its narrative opens turn 2
    add_user(log, SID)
    add_usage(log, SID, prompt_tokens=4000)
    add_usage(log, SID, prompt_tokens=4500)  # turn 2's main call
    add_narrative(log, SID)  # turn 2 commits before the hook, as live

    reading = measure_context(log, registry(connection()), SID)
    assert reading is not None
    assert reading.prompt_tokens == 4500
    assert reading.ratio == round(4500 / BUDGET, 4)


def test_ephemeral_narrative_does_not_close_the_window(log):
    """Only a COMMITTED narrative closes a turn; a draft row never does.

    The draft sits between turn 2's two usage rows: if it were counted as a
    narrative it would become the boundary and hide the 9000-token main call.
    """
    add_user(log, SID)
    add_usage(log, SID, prompt_tokens=2000)
    add_narrative(log, SID)  # turn 1 commits
    add_user(log, SID)
    add_usage(log, SID, prompt_tokens=9000)
    log.append_event(
        SID, "narrative", "narrator", {"prose": "draft", "dsl": "", "segments": []},
        ephemeral=True,
    )
    add_usage(log, SID, prompt_tokens=1000)
    add_narrative(log, SID)  # turn 2 commits

    seqs = [event for event in log.get_events(SID) if event.type == "narrative"]
    assert len(seqs) == 3 and seqs[1].ephemeral is True  # the draft is present
    reading = measure_context(log, registry(connection()), SID)
    assert reading is not None and reading.prompt_tokens == 9000


def test_a_turn_with_no_matching_rows_still_skips(log):
    """⚑R1a is unchanged: no matching row in THIS turn ⇒ no reading, no rows."""
    add_user(log, SID)
    add_usage(log, SID, prompt_tokens=15000)  # turn 1, measured
    add_narrative(log, SID)  # turn 1 commits (the boundary for turn 2)
    add_user(log, SID)
    add_usage(log, SID, agent="summarizer", prompt_tokens=15000)  # foreign label
    add_usage(log, SID, prompt_tokens=0)  # a zeroed call is not a measurement
    add_narrative(log, SID)  # turn 2 commits

    assert measure_context(log, registry(connection()), SID) is None
    hook = make_context_monitor_hook(log=log, registry=registry(connection()))
    run(hook(SID))
    assert context_rows(log) == []

    # A later turn with no usage at all at >0 is equally silent.
    add_turn(log, SID)
    assert measure_context(log, registry(connection()), SID) is None


def test_window_follows_the_row_model(log):
    """The connection that SERVED the row supplies the window (§2.1)."""
    big = connection("your-model-id", model="narrate-model", window=WINDOW)
    small = connection("glm", model="glm-5.3", window=10000, response=1024)
    reg = registry(big, small)
    add_usage(log, SID, agent="completion.narration", model="glm-5.3", prompt_tokens=8000)

    reading = measure_context(log, reg, SID)
    assert reading is not None
    assert reading.model == "glm-5.3"
    assert reading.connection == "glm"
    assert reading.context_window == 10000
    assert reading.budget_tokens == 10000 - 1024 - RESERVE_TOKENS


def test_unknown_row_model_falls_back_to_the_slot_connection(log):
    """Documented tie-break: an undeclared model ⇒ today's narrate connection."""
    reg = registry(connection(window=WINDOW))
    add_usage(log, SID, model="retired-model", prompt_tokens=4000)
    reading = measure_context(log, reg, SID)
    assert reading is not None
    assert reading.connection == "your-model-id"
    assert reading.context_window == WINDOW
    assert serving_connection(reg, "retired-model", agent="narrator").name == "your-model-id"


def test_undeclared_window_is_a_hard_no_op(log):
    """⚑C: no Context Size ⇒ no math, no rows (D84 doctrine)."""
    reg = registry(connection(window=None))
    add_usage(log, SID, prompt_tokens=99999)
    assert measure_context(log, reg, SID) is None

    hook = make_context_monitor_hook(log=log, registry=reg)
    run(hook(SID))
    assert context_rows(log) == []


def test_no_own_matching_row_skips_the_turn(log):
    """⚑R1a: measured-only — a fresh era (or a foreign agent) is silent."""
    reg = registry(connection())
    # A row for a DIFFERENT agent on this session must not measure narrate.
    add_usage(log, SID, agent="summarizer", prompt_tokens=99999)
    assert measure_context(log, reg, SID) is None
    hook = make_context_monitor_hook(log=log, registry=reg)
    run(hook(SID))
    assert context_rows(log) == []

    # And a row that lives only in the PARENT chain is not "own" either.
    parent = "s-parent"
    add_turn(log, parent)
    add_usage(log, parent, prompt_tokens=99999)
    log.fork_session(parent, 2, "s-child")
    assert measure_context(log, reg, "s-child") is None


def test_over_budget_is_a_float_sentinel_and_never_forces(log):
    """budget ≤ 0 ⇒ over_budget:true + ratio 1.0 (float); NO compaction."""
    # The live summarizer binding shape: a flat window a big response overruns.
    reg = registry(connection(window=8000, response=16384))
    add_usage(log, SID, prompt_tokens=500)
    service = FakeCompactService()

    hook = make_context_monitor_hook(log=log, registry=reg, service=service)
    run(hook(SID))

    rows = context_rows(log, variant=VARIANT_OVER_BUDGET)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["over_budget"] is True
    assert payload["ratio"] == 1.0
    assert isinstance(payload["ratio"], float)
    assert payload["budget_tokens"] < 0
    assert context_rows(log, variant=VARIANT_PLAIN) == []
    assert service.calls == []


def test_variant_scoped_dedupe_does_not_suppress_each_other(log):
    """The over-budget row does not swallow the later plain 80% row."""
    tiny = connection(window=8000, response=16384)
    hook = make_context_monitor_hook(log=log, registry=registry(tiny))
    add_usage(log, SID, prompt_tokens=500)
    run(hook(SID))
    assert len(context_rows(log, variant=VARIANT_OVER_BUDGET)) == 1

    # The owner fixes the declaration: now the same session reads at ≥0.8.
    healthy = registry(connection())
    fixed = make_context_monitor_hook(log=log, registry=healthy)
    add_usage(log, SID, prompt_tokens=int(BUDGET * 0.82))
    run(fixed(SID))
    assert len(context_rows(log, variant=VARIANT_OVER_BUDGET)) == 1
    assert len(context_rows(log, variant=VARIANT_PLAIN)) == 1


def test_noop_variant_is_its_own_row(log):
    """A completed compaction still ≥0.9 says so ONCE, in its own variant."""
    for _ in range(2):
        add_turn(log, SID)
    complete_era(log, SID)  # a REAL successor, not an own-marker proxy
    add_usage(log, SID, prompt_tokens=int(BUDGET * 0.95))
    service = FakeCompactService()

    hook = make_context_monitor_hook(
        log=log, registry=registry(connection()), service=service
    )
    run(hook(SID))
    run(hook(SID))

    assert service.calls == []  # step 0: never force a second time this era
    assert len(context_rows(log, variant=VARIANT_PLAIN)) == 1
    noop = context_rows(log, variant=VARIANT_NOOP)
    assert len(noop) == 1
    assert noop[0].payload["noop"] is True
    assert noop[0].payload["ratio"] == round(int(BUDGET * 0.95) / BUDGET, 4)


# --- the claim protocol ----------------------------------------------------


def test_claim_helpers_are_membership_based(log):
    claims = CompactionClaims()
    assert claim_held(claims, SID) is False
    assert claim_acquire(claims, SID) is True
    assert claim_held(claims, SID) is True
    assert claim_acquire(claims, SID) is False
    claims.release(SID)
    assert claim_held(claims, SID) is False
    # A bare set works too (B may construct either).
    plain: set[str] = set()
    assert claim_acquire(plain, SID) is True and SID in plain
    claim_release(plain, SID)
    assert SID not in plain


def test_acquire_waits_for_the_slot_then_claims_in_one_block(log):
    claims: set[str] = set()
    slot = FakeSlot(busy=True, clear_after=1)
    acquired, reason = run(acquire_claim(claims, SID, slot))
    assert (acquired, reason) == (True, "ok")
    assert slot.waits == 1
    assert claim_held(claims, SID) is True


def test_polling_turn_slot_adapter(log):
    """The turn-slot shape B constructs: ``busy(sid)`` + ``async wait_idle(sid)``."""
    state = {"busy": True}
    slot = PollingTurnSlot(lambda sid: state["busy"], poll=0.0)

    async def scenario():
        async def clear_soon():
            await asyncio.sleep(0)
            state["busy"] = False

        task = asyncio.ensure_future(clear_soon())
        assert slot.busy(SID) is True
        await slot.wait_idle(SID)
        await task

    run(scenario())
    assert slot.busy(SID) is False
    claims: set[str] = set()
    assert run(acquire_claim(claims, SID, slot)) == (True, "ok")
    assert claim_held(claims, SID) is True


def test_polling_turn_slot_survives_a_broken_predicate(log):
    """An unreadable slot must not wedge the gauge ("clear" is the safe answer)."""

    def broken(session_id):
        raise RuntimeError("no session registry")

    assert PollingTurnSlot(broken).busy(SID) is False


def test_acquire_respects_a_held_claim(log):
    claims = {SID}
    acquired, reason = run(acquire_claim(claims, SID, FakeSlot()))
    assert acquired is False and reason == "claimed"


def test_acquire_gives_up_on_a_permanently_busy_slot(log):
    class NeverIdle(FakeSlot):
        def busy(self, session_id=None):
            return True

        async def wait_idle(self, session_id=None):
            self.waits += 1

    claims: set[str] = set()
    acquired, reason = run(acquire_claim(claims, SID, NeverIdle(busy=True)))
    assert acquired is False and reason == "slot"
    assert claim_held(claims, SID) is False


def test_successor_present_is_already_compacted_and_never_reforks(log):
    """(b) A parent WITH a successor ⇒ noop/already_compacted, never a 2nd era.

    §2.3 step 1: the post-claim successor re-check. The verdict is the SAME one
    ``POST /compact`` answers with (200 + successor): this session is SUPERSEDED.
    """
    add_turn(log, SID)
    _, child = complete_era(log, SID)
    assert idempotent_successor(log, SID) == child
    assert compaction_successor(log, SID) == child
    assert has_completed_compaction(log, SID) is True

    service = FakeCompactService()
    claims = CompactionClaims()
    job = ContextJobs().create(SID, MANUAL_TRIGGER)
    assert job is not None

    result = run(run_compact_job(job, log=log, service=service, claims=claims))
    assert result.status == "noop"
    assert result.stage == NOOP_ALREADY_COMPACTED
    assert result.reason == NOOP_MESSAGES[NOOP_ALREADY_COMPACTED]
    # The successor is still reported, so a poller can follow the era.
    assert result.successor_session_id == child
    assert result.title == "The Long Night"
    assert service.calls == []  # no second fork, no second model call
    assert claim_held(claims, SID) is False  # released in finally


def test_successor_via_marker_alone_is_already_compacted(log):
    """The supersession test survives a summary the span check cannot prove.

    A crash between the fork and the marker bookkeeping (or a hand-crafted log)
    leaves a marker-bearing child whose summary span does not reach the head; the
    own-marker child alone must still answer "already compacted".
    """
    add_turn(log, SID)
    log.fork_session(SID, 2, CHILD)
    add_marker(log, CHILD, source=SID, summary_ref=[SID, 2])
    assert compaction_successor(log, SID) == CHILD
    assert idempotent_successor(log, SID) == CHILD

    service = FakeCompactService()
    job = ContextJobs().create(SID, MANUAL_TRIGGER)
    result = run(run_compact_job(job, log=log, service=service, claims=CompactionClaims()))
    assert result.status == "noop"
    assert result.stage == NOOP_ALREADY_COMPACTED
    assert result.successor_session_id == CHILD
    assert service.calls == []


def test_era_child_with_uncovered_history_compacts(log):
    """(a) THE LIVE REPRO: an era child with uncovered own turns must compact.

    A live session: born of a forced_90 compaction of its parent (its first own
    row IS the marker), one new own turn, no successor. The old own-marker
    predicate answered noop/already_compacted and the session could never compact
    again, era 2 onward. It must proceed (era 2 → era 3).
    """
    add_turn(log, SID)
    add_turn(log, SID)
    _, child = complete_era(log, SID)  # era 2: the child OWNS its birth marker
    add_turn(log, child)

    assert has_era_marker(log, child) is True  # it IS an era ...
    assert compaction_successor(log, child) is None  # ... with no successor
    assert has_completed_compaction(log, child) is False  # so NOT already compacted
    assert len(uncovered_turn_groups(log, child)) == 1

    service = FakeCompactService(child="s-context-era3")
    job = ContextJobs().create(child, MANUAL_TRIGGER)
    result = run(run_compact_job(job, log=log, service=service, claims=CompactionClaims()))

    assert result.status == "done"
    assert len(service.calls) == 1 and service.calls[0]["session_id"] == child
    assert result.successor_session_id == "s-context-era3"


def test_fresh_era_with_insufficient_uncovered_turns_does_not_force(log):
    """(c) Fresh era (own marker, 0-1 uncovered turns) at ≥0.9 ⇒ noop, no force.

    This is the surviving half of §2.3 step 0: forcing again cannot free window
    when there is nothing meaningful to fold — the era-per-turn loop killer.
    """
    add_turn(log, SID)
    _, child = complete_era(log, SID)
    add_turn(log, child)  # ONE uncovered turn: insubstantial (< 2)
    add_usage(log, child, prompt_tokens=int(BUDGET * 0.95))
    service = FakeCompactService(child="s-context-era3")

    hook = make_context_monitor_hook(
        log=log, registry=registry(connection()), service=service
    )
    run(hook(child))
    run(hook(child))  # idempotent: the noop verdict repeats, the notice does not

    assert service.calls == []  # no force, no model call
    assert len(context_rows(log, session=child, variant=VARIANT_NOOP)) == 1
    assert context_rows(log, session=child, variant=VARIANT_NOOP)[0].payload[
        "noop"
    ] is True


def test_fresh_era_with_nothing_uncovered_does_not_force(log):
    """A brand-new era with no own turn at all: no force, and no duplicate era.

    Everything is covered by the INHERITED recap ⇒ stage ``nothing_uncovered``
    (:data:`NOOP_INHERITED_COVER`), never a step-10 fork.
    """
    add_turn(log, SID)
    _, child = complete_era(log, SID)
    assert uncovered_turn_groups(log, child) == []
    add_usage(log, child, prompt_tokens=int(BUDGET * 0.95))
    service = FakeCompactService(child="s-context-era3")

    hook = make_context_monitor_hook(
        log=log, registry=registry(connection()), service=service
    )
    run(hook(child))
    run(hook(child))
    assert service.calls == []
    assert has_era_marker(log, child) is True
    assert log.list_branches(child) == []  # never a duplicate era

    forced = ContextJobs().create(child, FORCED_TRIGGER)
    result = run(run_compact_job(forced, log=log, service=service, claims=CompactionClaims()))
    assert result.status == "noop"
    assert result.stage == NOOP_NOTHING_UNCOVERED
    assert result.reason == NOOP_INHERITED_COVER


def test_era_with_substantial_uncovered_history_is_forced_again(log):
    """(d) Era with ≥2 uncovered turns at ≥0.9 ⇒ the forced path PROCEEDS.

    Round-3 MAJ-3's real hazard in its repaired costume: the force must reach
    era 2 (and every later era) once the era has real uncovered history.
    """
    add_turn(log, SID)
    _, child = complete_era(log, SID)
    add_turn(log, child)
    add_turn(log, child)
    assert len(uncovered_turn_groups(log, child)) == 2
    add_usage(log, child, prompt_tokens=int(BUDGET * 0.95))
    service = FakeCompactService(child="s-context-era3")

    hook = make_context_monitor_hook(
        log=log, registry=registry(connection()), service=service
    )
    run(hook(child))

    assert len(service.calls) == 1
    assert service.calls[0]["trigger"] == FORCED_TRIGGER
    assert idempotent_successor(log, child) == "s-context-era3"


def test_nothing_uncovered_with_an_own_summary_forks_it(log):
    """(e) Crash repair: nothing uncovered, the covering summary is OWN ⇒ step 10."""
    add_turn(log, SID)
    add_turn(log, SID)
    head = max(event.seq for event in log.get_chain_events(SID))
    add_summary(log, SID, [1, head], trigger=MANUAL_TRIGGER)  # fork/marker died

    assert uncovered_turn_groups(log, SID) == []
    assert covering_summary_owner(log, SID) == "own"

    service = FakeCompactService()
    job = ContextJobs().create(SID, MANUAL_TRIGGER)
    result = run(run_compact_job(job, log=log, service=service, claims=CompactionClaims()))
    assert len(service.calls) == 1  # the service runs step 10 (no model call)
    assert result.status == "done"
    assert result.successor_session_id == CHILD


def test_the_force_guard_never_blocks_a_pending_crash_repair(log):
    """(e2) An era whose OWN summary covers everything is a repair — even forced.

    The fresh-era force guard must not swallow the step-10 repair: nothing is
    uncovered, but the covering summary is this session's own row, so the fork is
    the repair of a crash rather than a noop.
    """
    add_turn(log, SID)
    _, child = complete_era(log, SID)
    add_turn(log, child)
    head = max(event.seq for event in log.get_chain_events(child))
    add_summary(log, child, [1, head], trigger=FORCED_TRIGGER)  # fork/marker died

    assert uncovered_turn_groups(log, child) == []
    assert covering_summary_owner(log, child) == "own"
    assert has_era_marker(log, child) is True

    service = FakeCompactService(child="s-context-era3")
    job = ContextJobs().create(child, FORCED_TRIGGER)
    result = run(run_compact_job(job, log=log, service=service, claims=CompactionClaims()))
    assert len(service.calls) == 1
    assert result.status == "done"


def test_nothing_uncovered_with_an_inherited_summary_never_forks(log):
    """(f) Everything covered by an INHERITED recap ⇒ noop, never a duplicate era."""
    add_turn(log, SID)
    add_turn(log, SID)
    _, child = complete_era(log, SID)  # the recap belongs to the PARENT

    assert uncovered_turn_groups(log, child) == []
    assert covering_summary_owner(log, child) == "inherited"

    service = FakeCompactService(child="s-context-era3")
    job = ContextJobs().create(child, MANUAL_TRIGGER)
    result = run(run_compact_job(job, log=log, service=service, claims=CompactionClaims()))

    assert result.status == "noop"
    assert result.stage == NOOP_NOTHING_UNCOVERED
    assert result.reason == NOOP_INHERITED_COVER
    assert "inherited" in (result.reason or "")
    assert service.calls == []  # NEVER fork a second era from an inherited recap
    assert log.list_branches(child) == []


def test_era_markers_are_own_rows_only(log):
    """A swipe fork INHERITS the marker on its chain — it is not an era itself."""
    add_turn(log, SID)
    _, child = complete_era(log, SID)
    add_turn(log, child)
    swipe = "s-context-swipe"
    log.fork_session(child, log.get_chain_events(child)[-1].seq, swipe)

    assert has_era_marker(log, child) is True
    assert has_era_marker(log, swipe) is False  # chain-inherited marker is not own
    assert compaction_successor(log, child) is None  # a plain fork is not an era
    assert idempotent_successor(log, child) is None
    assert compaction_successor(log, SID) == child  # the real era still counts


def test_claim_is_released_when_the_service_raises(log):
    add_turn(log, SID)
    service = RaisingService()
    claims = CompactionClaims()
    job = ContextJobs().create(SID, MANUAL_TRIGGER)

    result = run(run_compact_job(job, log=log, service=service, claims=claims))
    assert result.status == "failed"
    assert "summarizer exploded" in (result.error or "")
    assert claim_held(claims, SID) is False
    # The failure is a ROW, not just an exception (D9).
    rows = [
        event
        for event in log.get_events(SID)
        if event.type == "system" and event.payload.get("event") == "compaction_failed"
    ]
    assert len(rows) == 1 and "exploded" in rows[0].payload["error"]
    assert failed_attempts(log, SID) == 1


def test_job_without_a_service_fails_with_a_config_stage(log):
    add_turn(log, SID)
    job = ContextJobs().create(SID, MANUAL_TRIGGER)
    result = run(run_compact_job(job, log=log, service=None, claims=CompactionClaims()))
    assert result.status == "failed"
    assert "no compact service wired" in (result.error or "")


def test_fresh_era_convergence_stops_the_force(log):
    """§2.3 step 0, repaired: an era child OWNS its birth marker, yet the force
    still stops for THAT ERA only while it has nothing meaningful to fold."""
    add_turn(log, SID)
    add_marker(log, SID, source="s-older")  # an era with ONE turn (< 2)
    service = FakeCompactService()
    job = ContextJobs().create(SID, FORCED_TRIGGER)

    result = run(run_compact_job(job, log=log, service=service, claims=CompactionClaims()))
    assert result.status == "noop"
    assert result.stage == NOOP_ALREADY_COMPACTED
    assert result.reason == NOOP_MESSAGES[NOOP_ALREADY_COMPACTED]
    assert service.calls == []
    # The own marker is the era-BIRTH fact, never the already-compacted verdict.
    assert has_era_marker(log, SID) is True
    assert has_completed_compaction(log, SID) is False
    assert len(uncovered_turn_groups(log, SID)) < FRESH_ERA_MIN_UNCOVERED_TURNS


def test_a_marker_no_longer_blocks_a_manual_compact(log):
    """The manual path is not bound by the fresh-era force guard."""
    add_turn(log, SID)
    add_marker(log, SID, source="s-older")
    service = FakeCompactService()
    job = ContextJobs().create(SID, MANUAL_TRIGGER)

    result = run(run_compact_job(job, log=log, service=service, claims=CompactionClaims()))
    assert result.status == "done"
    assert len(service.calls) == 1


def test_attempt_limit_stops_the_force_but_not_the_button(log):
    add_turn(log, SID)
    for _ in range(MAX_FORCED_ATTEMPTS):
        log.append_event(
            SID,
            "system",
            "summarizer",
            {"event": "compaction_failed", "stage": "map", "error": "boom"},
        )

    forced = ContextJobs().create(SID, FORCED_TRIGGER)
    result = run(
        run_compact_job(
            forced, log=log, service=FakeCompactService(), claims=CompactionClaims()
        )
    )
    assert result.status == "noop"
    assert result.stage == NOOP_ATTEMPT_LIMIT

    manual_service = FakeCompactService()
    manual = ContextJobs().create(SID, MANUAL_TRIGGER)
    done = run(
        run_compact_job(
            manual, log=log, service=manual_service, claims=CompactionClaims()
        )
    )
    assert done.status == "done"
    assert len(manual_service.calls) == 1


def test_forced_path_runs_the_service_with_the_forced_trigger(log):
    add_turn(log, SID)
    add_usage(log, SID, prompt_tokens=int(BUDGET * 0.95))
    service = FakeCompactService()

    hook = make_context_monitor_hook(
        log=log, registry=registry(connection()), service=service
    )
    run(hook(SID))

    assert len(service.calls) == 1
    assert service.calls[0]["trigger"] == FORCED_TRIGGER
    # The 80% notice landed before the force (the client sees it immediately).
    assert len(context_rows(log, variant=VARIANT_PLAIN)) == 1
    # The era exists and the child carries the title.
    assert idempotent_successor(log, SID) == CHILD
    assert session_title(log, CHILD) == "The Long Night"


def test_job_installs_session_context_for_usage_rows(log):
    """§2.3 step 6: the service runs with CURRENT_SESSION + ``summarizer`` set."""
    from fabulist.agents.debug import CURRENT_INTERACTION, CURRENT_SESSION

    add_turn(log, SID)
    seen: dict[str, str] = {}

    class ContextService:
        async def compact(self, session_id, log, *, trigger, job):
            seen["session"] = CURRENT_SESSION.get()
            seen["interaction"] = CURRENT_INTERACTION.get()
            return {"status": "noop", "reason": "nothing_uncovered"}

    CURRENT_SESSION.set("")
    CURRENT_INTERACTION.set("")
    job = ContextJobs().create(SID, MANUAL_TRIGGER)
    run(run_compact_job(job, log=log, service=ContextService(), claims=CompactionClaims()))

    assert seen == {"session": SID, "interaction": "summarizer"}
    # ... and the surrounding context is restored afterwards.
    assert CURRENT_SESSION.get() == ""


def test_job_carries_landed_chunks_for_retry_reuse(log):
    """§2.3 step 4: chunk outputs persist on the job, so a retry reuses them."""
    add_turn(log, SID)
    job = ContextJobs().create(SID, MANUAL_TRIGGER)
    run(run_compact_job(job, log=log, service=FakeCompactService(), claims=CompactionClaims()))
    assert job.chunks == ["map chunk 1"]


def test_busy_slot_skips_the_force_for_this_turn(log):
    add_turn(log, SID)
    add_usage(log, SID, prompt_tokens=int(BUDGET * 0.95))
    service = FakeCompactService()

    class NeverIdle:
        def busy(self, session_id=None):
            return True

        async def wait_idle(self, session_id=None):
            return None

    hook = make_context_monitor_hook(
        log=log, registry=registry(connection()), service=service, slot=NeverIdle()
    )
    run(hook(SID))
    assert service.calls == []
    assert context_rows(log, variant=VARIANT_NOOP) == []


# --- failure row honesty ---------------------------------------------------


def test_monitor_failure_files_a_hook_named_row(log):
    """A gauge crash is ``post_turn_failed`` naming the hook — never compaction_failed."""
    appended: list[dict] = []

    class ExplodingLog:
        def get_events(self, session_id):
            raise RuntimeError("log is on fire")

        def append_event(self, session_id, type_, actor, payload=None, **kwargs):
            appended.append({"session_id": session_id, "type": type_, "payload": payload})
            return None

    hook = make_context_monitor_hook(log=ExplodingLog(), registry=registry(connection()))
    assert hook.__name__ == MONITOR_HOOK_NAME
    run(hook(SID))  # never raises — the turn and its sibling hooks survive

    assert len(appended) == 1
    assert appended[0]["payload"]["event"] == POST_TURN_FAILED_EVENT
    assert appended[0]["payload"]["stage"] == MONITOR_HOOK_NAME
    assert "on fire" in appended[0]["payload"]["error"]
    assert appended[0]["payload"]["event"] != "compaction_failed"


def test_hook_is_a_one_arg_awaitable(log):
    import inspect

    hook = make_context_monitor_hook(log=log)
    assert inspect.iscoroutinefunction(hook)
    params = list(inspect.signature(hook).parameters.values())
    assert len(params) == 1 and params[0].name == "session_id"


def test_no_registry_means_no_math(log):
    add_usage(log, SID, prompt_tokens=99999)
    hook = make_context_monitor_hook(log=log, registry=None)
    run(hook(SID))
    assert context_rows(log) == []


def test_emit_is_best_effort_on_a_broken_append(log):
    add_usage(log, SID, prompt_tokens=13000)
    reading = measure_context(log, registry(connection()), SID)
    assert reading is not None

    class ExplodingAppend:
        def get_events(self, session_id):
            return []

        def append_event(self, *args, **kwargs):
            raise RuntimeError("nope")

    assert emit_context_low(ExplodingAppend(), SID, reading) is False


def test_forced_ratio_constant_matches_the_design():
    assert CONTEXT_LOW_RATIO == 0.8
    assert CONTEXT_FORCE_RATIO == 0.9
