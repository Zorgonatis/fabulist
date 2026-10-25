"""The historian: the asset-proposal lanes run OUTSIDE the turn flow (W9.23 §3.6).

The world/actor/book lane calls and their judgments used to sit on the turn's
critical path (the ``asset_proposals`` graph stage, W9.22 §3). They no longer do:
this module is a **runner** around the SAME lane system. It drives the existing
:class:`~fabulist.graph.nodes.AssetProposalFanout` object UNMODIFIED — its four
halves (``__call__`` dispatcher, ``route``, ``lane``, ``join``) are plain async
callables over a synthesized state dict, so nothing in ``nodes.py`` or
``proposal_lanes.py`` changes for the move.

Cadence is the planner's pattern, with ONE deliberate difference
------------------------------------------------------------------
The due rule is ``committed_turns - last_run.turn_seq >= cadence``
(:func:`historian_is_due`), the cadence defaulting to
:data:`DEFAULT_HISTORIAN_CADENCE` and read LIVE from the ACTIVE preset at each
evaluation (a Studio edit applies without a rebuild). Unlike the planner there is
**no turn-1 invoke**: with no ``historian_run`` on the chain ``last = 0``, so the
first automatic run fires only once ``committed_turns >= cadence``. A fresh
session's turn 1 never runs the historian.

The clock is the last COMPLETED run (:meth:`HistorianRunner.run` appends it)
-------------------------------------------------------------------------
``historian_run {status: "started"}`` carries the ``turn_seq`` the run started at;
a ``finished`` row carries the run's counts (``gate`` / ``filed`` / ``dropped`` /
``failed_lanes`` / ``duration_ms``); a runner-level failure appends
``status: "failed"`` with ``error``. The due rule keys on the latest run that
CONSUMED its window (:func:`load_last_completed_historian_run`), never on the
latest row of any status: a MISSED run (:func:`historian_run_missed` — the run
failed, or every lane it drove failed without filing or dropping anything) leaves
the clock where the last completed run put it, so the NEXT committed turn is due
again and the retry's window starts from that completed run and re-covers the
turns the miss never judged. A quiet window is NOT a miss: gate ``no_evidence`` /
``verdict_no`` / ``unsure``, and any run that filed or dropped something, consumed
the cadence — re-running it would re-judge turns already settled.

The ``started`` row is deliberately not the clock: it precedes its own outcome at
the same ``turn_seq``, so counting it would make every failed run look completed.
A ``started`` row with no terminator (the process died mid-run) therefore leaves
the clock stale and the next turn retries — the honest reading of a run that
never finished — and the in-flight set, not the stamp, is the double-spawn guard.
The owner's run-now door remains the manual override either way.

This is the rule for EVERY cadence job, not just this one: a job that misses its
window retries on the next committed turn until a run completes, and the
summarizer (W9.26) must inherit it. The owner retired the back-off-a-cycle policy
that this runner shipped with.

The run is bounded PER CONNECTION, never per run
-----------------------------------------------
Each lane call is one model call on the agent's own connection, and the
transport bound is that connection's ``timeout`` field
(``connections/registry.py`` -> ``connections/client.py``; the wiring resolves
an unset one to 300s). There is deliberately NO run-total cap: a three-lane run
on an owner-configured slow connection (your-model-id, 900s) is not an error, and a
run-level ``wait_for`` was measured killing exactly that.

A lane whose call outlives its connection's timeout RAISES, and the EXISTING
per-lane path contains it (``AssetProposalFanout.lane`` never fails the run):
the reason lands in ``proposal_meta["failed"]`` -> the run's ``failed_lanes``,
the other lanes still file, the run finishes with its counts, and the rows
filed before the failure STAY — the log is append-only (D9), nothing is rolled
back. A runner-level raise (the dispatcher, the fan-in, the log) still stamps
``status: "failed"`` with the error and releases the in-flight guard in
``run``'s ``finally``, so the window it never judged stays UNCONSUMED and the next
committed turn retries it (§3.6.3) instead of the broken run being skipped for a
cycle. The in-flight guard's ``409`` is therefore bounded by the slowest single
connection call, never by a run budget.

Evidence is the WINDOW, unioned into ONE synthetic state (§3.6.2)
------------------------------------------------------------------
The fan-out derives its own evidence from exactly ``state["world_state_event"]``'s
payload and ``state["narrative_event"].seq`` (:func:`~fabulist.state.proposal_lanes.
proposal_evidence`), so per-turn calls cannot be unioned after the fact and an
``Evidence`` object cannot be handed in. The runner therefore synthesizes ONE
``world_state_event`` whose payload is the UNION of the window's turns (``state``/
``venue`` merged key-wise with later turns winning, ``off_screen`` concatenated
and deduped by name) and ONE ``narrative_event`` = the window's FIRST narrative,
so the roster-diff test (``admitted_at_seq > seq``) spans the whole window instead
of its last turn. A window whose touched entities appear only in an EARLY turn
still files rows — that is the property the merged union exists for.

Each turn's contribution is the LAST ``world_state`` event inside that
narrative's seq span; a turn whose world-state pass failed contributes nothing
(the roster diff still spans it).

Prose is ONE string per window TURN (``narrative_parts``), spent against the
EFFECTIVE bound the lanes see (``MAX_PROSE_CHARS``: ``build_lane_payload``
clips to the FIRST 1500 chars, so the parts keep the recent 1500 across the
window's turns and the clip downstream is a no-op). What is composed is what is
shown: the gate reads the parts as one ``<history>`` line per turn (D81) instead
of one blob.

Filing identity stays ``bts`` (§3.6.5) — the historian IS the BTS lane system's
runner, and the run's trigger/outcome live on the ``historian_run`` rows. The
cast boundary (§3.6.6) is untouched by construction: the three lanes are
``world``/``actors``/``books``, whose ``LaneSpec``s never carry ``kind=cast``, so
a run files no cast row however rich the window's roster diff is.

The cast ladder's promotion rung rides the run (W9.30 §5.2)
----------------------------------------------------------
After the lanes are driven and BEFORE the ``finished`` row, ``run`` calls
:func:`fabulist.state.promotion.promotion_pass`: deterministic engine code (zero
model calls) that files at most one ``origin="promotion"`` actor-add proposal for
a session-cast member with enough registry facts. It is not a lane (it needs no
window, no gate and no model) and it is not gated by ``should_we_propose`` — its
bar is a registry count. It lives INSIDE ``run`` for the cadence, not for the
evidence: an engine promotion appears in the owner's review queue on the
historian's clock, and the run's ``promotions`` count records how many it filed.
Its failure is contained INTERNALLY (a warning, ``promotions: 0``): a raise there
would stamp the run ``failed``, which the due rule reads as a MISSED window and
would retry over a review-queue nicety.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from fabulist.graph.nodes import committed_turn_count
from fabulist.graph.types import merge_proposal_meta, merge_proposal_rows
from fabulist.log.schema_gate import HISTORIAN_RUN_EVENT
from fabulist.prompts.proposals import MAX_PROSE_CHARS
from fabulist.state.promotion import promotion_pass

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HISTORIAN_CADENCE",
    "HISTORIAN_EVIDENCE_WINDOW_TURNS",
    "HistorianBusy",
    "HistorianRunner",
    "historian_is_due",
    "historian_inflight",
    "historian_run_missed",
    "historian_window",
    "load_last_completed_historian_run",
    "load_last_historian_run",
    "make_historian_runner",
]

#: Committed turns between automatic runs. The DEFAULT is code; the VALUE is
#: preset data (``preset_store.historian_config``), owner-editable in the pane.
DEFAULT_HISTORIAN_CADENCE = 10

#: How far back a run reads evidence. Turn counts are a COUNT of narratives, so
#: the window is exact; a cadence raised far beyond this degrades the evidence
#: (older turns' touched surfaces fall out), never the run (R9).
HISTORIAN_EVIDENCE_WINDOW_TURNS = 30

#: The prologue ``historian_run`` rows are appended under.
HISTORIAN_ACTOR = "bts"


class HistorianBusy(RuntimeError):
    """A run for this session is already in flight in this process.

    The double-spawn guard (§3.6.8). It is the ONLY one that can stop an overlap:
    the ``started`` row is not the clock (§3.6.3), so a next turn's due check is
    no longer stopped by a fresh stamp, by design — a run that never finished must
    be retried. Process-local by construction, so a second engine process on the
    same db is uncovered (R10).
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(f"historian already running for {session_id!r}")
        self.session_id = session_id


#: Session ids whose historian run is in flight in THIS process. Read by the
#: on-demand endpoint (409) and by the post-turn hook (skip, never queue).
_INFLIGHT: set[str] = set()


def historian_inflight(session_id: str) -> bool:
    """True while a historian run for ``session_id`` is in flight here."""
    return session_id in _INFLIGHT


class ProposalFanout(Protocol):
    """What the runner needs from the lane system (the fan-out's four halves).

    Deliberately structural: the runner drives
    :class:`~fabulist.graph.nodes.AssetProposalFanout` as-is, and a test can hand
    it any object with this shape. ``route``'s return values are LangGraph
    ``Send`` tasks; only their ``arg`` (the lane payload) is read.
    """

    async def __call__(self, state: Mapping[str, Any]) -> dict:  # pragma: no cover
        ...

    async def route(self, state: Mapping[str, Any]) -> list:  # pragma: no cover
        ...

    async def lane(self, payload: dict) -> dict:  # pragma: no cover
        ...

    def join(self, state: Mapping[str, Any]) -> dict:  # pragma: no cover
        ...


# -- the event-log reads -----------------------------------------------------


def _chain_events(log: Any, session_id: str) -> list:
    """The session's fork-aware chain of events (the context-assembly read)."""
    get_chain_events = getattr(log, "get_chain_events", None)
    events = (
        get_chain_events(session_id)
        if callable(get_chain_events)
        else log.get_events(session_id)
    )
    return sorted(events, key=lambda event: int(getattr(event, "seq", 0) or 0))


def _payload(event: Any) -> dict:
    value = getattr(event, "payload", None)
    return value if isinstance(value, dict) else {}


def load_last_historian_run(log: Any, session_id: str) -> dict | None:
    """The latest non-ephemeral ``historian_run`` on the CHAIN, or ``None``.

    The fork-aware read rule of :func:`~fabulist.graph.plans.load_last_plan`: a
    branch is traced against the runs it inherited. Latest by ``seq`` wins and the
    status is irrelevant — this is the TRACE read (what the status API shows), not
    the clock: a ``started`` row and the row it becomes describe ONE run, and only
    a run that consumed its window advances the cadence
    (:func:`load_last_completed_historian_run`).
    """
    latest: dict | None = None
    for event in _chain_events(log, session_id):
        if getattr(event, "type", None) != "system" or getattr(event, "ephemeral", False):
            continue
        payload = _payload(event)
        if payload.get("event") != HISTORIAN_RUN_EVENT:
            continue
        latest = {
            **payload,
            "ts": str(payload.get("ts") or getattr(event, "ts", "") or ""),
            "seq": int(getattr(event, "seq", 0) or 0),
        }
    return latest


#: The statuses that SETTLE a run. A ``started`` row never does — it is the run's
#: opening trace, and its own outcome lands after it at the same ``turn_seq``.
_TERMINAL_RUN_STATUSES = ("finished", "failed")


def _count(payload: Mapping[str, Any], key: str) -> int:
    """One run-row count as an int (a missing or drifted value reads as 0)."""
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def historian_run_missed(run: Mapping[str, Any] | None) -> bool:
    """Did this run MISS its window? (§3.6.3, owner ruling: retry, don't back off)

    Missed means the window was never really judged: the run itself failed
    (``status: "failed"`` — the dispatcher/fan-in/log raised, so no lane call was
    made) or every lane it drove failed without FILING or DROPPING anything
    (``failed_lanes`` non-empty and both counts 0 — the outage shape, which still
    *finishes* and so cannot be told apart by status alone).

    Everything else COMPLETED its window and consumes the cadence: a quiet no-op
    (all counts 0, no failed lane — gate ``no_evidence`` / ``verdict_no`` /
    ``unsure``) was a judgment, and a partial run (anything filed or dropped) acted
    on the turns it read, so re-running it would re-judge settled turns. Keys are
    read tolerantly: a ``failed`` row carries no ``filed``/``dropped`` at all.
    """
    if run is None:
        return False
    if str(run.get("status") or "") == "failed":
        return True
    failed_lanes = run.get("failed_lanes")
    if not isinstance(failed_lanes, (list, tuple)) or not failed_lanes:
        return False
    return _count(run, "filed") == 0 and _count(run, "dropped") == 0


def load_last_completed_historian_run(log: Any, session_id: str) -> dict | None:
    """The latest ``historian_run`` on the CHAIN that COMPLETED its window.

    The clock of the due rule (§3.6.3), on the same fork-aware read rule as
    :func:`load_last_historian_run`: a branch is due against the completed runs it
    inherited. Misses are skipped (they advance nothing, so the next committed
    turn is due again and the retry re-covers their turns); unsettled ``started``
    rows are skipped too, since a run that never finished never judged its window.
    ``None`` therefore means "nothing on this chain has completed yet" — the whole
    chain is uncovered.
    """
    latest: dict | None = None
    for event in _chain_events(log, session_id):
        if getattr(event, "type", None) != "system" or getattr(event, "ephemeral", False):
            continue
        payload = _payload(event)
        if payload.get("event") != HISTORIAN_RUN_EVENT:
            continue
        if str(payload.get("status") or "") not in _TERMINAL_RUN_STATUSES:
            continue
        if historian_run_missed(payload):
            continue
        latest = {
            **payload,
            "ts": str(payload.get("ts") or getattr(event, "ts", "") or ""),
            "seq": int(getattr(event, "seq", 0) or 0),
        }
    return latest


# -- the due rule ------------------------------------------------------------


def historian_is_due(
    *,
    last_run: Mapping[str, Any] | None,
    committed_turns: int,
    cadence: int = DEFAULT_HISTORIAN_CADENCE,
) -> bool:
    """The cadence rule: ``committed_turns - last_run.turn_seq >= cadence``.

    ``last_run is None`` means nothing has COMPLETED on this chain, and ``last =
    0`` — so a session's FIRST automatic run happens exactly when it reaches
    ``cadence`` committed turns, never at turn 1 (the deliberate difference from
    :func:`~fabulist.graph.nodes.plan_is_due`, whose turn 1 is mandatory because a
    plan steers the next turn).

    ``last_run`` is the last run that consumed its window
    (:func:`load_last_completed_historian_run`), never a missed one: a missed run
    leaves the clock at the previous completed run, so the next committed turn is
    due and the retry re-reads the turns the miss left uncovered. That is why the
    answer is often "due" on consecutive turns — it is a retry, not a cadence of
    1 (§3.6.3). ``cadence`` is floored at 1 (the write door already refuses 0) so a
    corrupt value can never make the historian run every turn on its own.
    """
    last = int((last_run or {}).get("turn_seq") or 0)
    return int(committed_turns) - last >= max(1, int(cadence))


# -- the evidence window -----------------------------------------------------


def _narratives(events: list) -> list:
    """The chain's committed narratives, in seq order (ephemeral rows skipped)."""
    return [
        event
        for event in events
        if getattr(event, "type", None) == "narrative"
        and not getattr(event, "ephemeral", False)
    ]


def _narrative_ref(event: Any) -> dict:
    """The ``{id, seq, payload}`` reference the fan-out's state carries."""
    return {
        "id": getattr(event, "id", None),
        "seq": int(getattr(event, "seq", 0) or 0),
        "payload": _payload(event),
    }


def _last_world_state(events: list, after_seq: int, before_seq: int | None) -> dict | None:
    """The LAST ``world_state`` payload in ``(after_seq, before_seq)``, or ``None``.

    One turn's evidence contribution: the world-state pass writes one row per
    turn, and a re-run inside the same span supersedes the earlier one — the
    latest row in the span is what that turn settled on.
    """
    found: dict | None = None
    for event in events:
        seq = int(getattr(event, "seq", 0) or 0)
        if seq <= after_seq or (before_seq is not None and seq >= before_seq):
            continue
        if getattr(event, "type", None) != "system" or getattr(event, "ephemeral", False):
            continue
        payload = _payload(event)
        if payload.get("event") == "world_state":
            found = payload
    return found


def _section(value: Any) -> dict:
    """One payload section as a dict (a malformed/drifted value reads as empty)."""
    return dict(value) if isinstance(value, Mapping) else {}


def _union_sections(payloads: list[dict], key: str) -> dict:
    """Union one ``state``/``venue`` section key-wise; a LATER turn wins per key."""
    merged: dict[str, Any] = {}
    for payload in payloads:
        merged.update(_section(payload.get(key)))
    return merged


def _union_off_screen(payloads: list[dict]) -> list:
    """Concatenate the window's ``off_screen`` rows, deduped by ``name``."""
    rows: list = []
    seen: set[str] = set()
    for payload in payloads:
        off_screen = payload.get("off_screen")
        if not isinstance(off_screen, (list, tuple)):
            continue
        for row in off_screen:
            name = (
                str((row or {}).get("name") or "").strip()
                if isinstance(row, Mapping)
                else ""
            )
            if name and name in seen:
                continue
            if name:
                seen.add(name)
            rows.append(dict(row) if isinstance(row, Mapping) else row)
    return rows


def _window_turns(narratives: list) -> list[str]:
    """The window's prose as ONE string per TURN, clipped to the lanes' bound.

    ``build_lane_payload`` keeps the FIRST :data:`MAX_PROSE_CHARS` of what it is
    given, so the budget is spent from the RECENT end: the newest turn whole, the
    turn that crosses the bound keeping its TAIL (where a reveal lands), older
    turns whole until the budget runs out. The join of the parts is therefore
    exactly the window's last :data:`MAX_PROSE_CHARS` — the composed text and the
    shown text are the same text — while each part stays one turn, which is what
    the gate renders a line for.
    """
    parts: list[str] = []
    budget = MAX_PROSE_CHARS
    for row in reversed(narratives):
        if budget <= 0:
            break
        prose = str(_payload(row).get("prose") or "")
        parts.append(prose[-budget:] if len(prose) > budget else prose)
        budget -= len(parts[-1])
    parts.reverse()
    return parts


def historian_window(
    log: Any,
    session_id: str,
    *,
    last_run: Mapping[str, Any] | None = None,
    limit: int = HISTORIAN_EVIDENCE_WINDOW_TURNS,
) -> dict:
    """The synthetic turn state for the run window (§3.6.2).

    Returns ``{"narrative_parts", "world_state_event", "narrative_event",
    "window_turns"}`` — exactly the three state keys the fan-out reads, plus the
    count the ``historian_run`` row reports. The window is the chain's narratives
    AFTER the first ``last_run.turn_seq`` of them, capped at ``limit`` from the
    RECENT end; a first run reads the whole chain (capped). ``last_run`` is the
    last COMPLETED run (§3.6.3), so a RETRY after a missed run re-covers the turns
    the miss left uncovered instead of starting after it.
    """
    events = _chain_events(log, session_id)
    narratives = _narratives(events)
    start = max(0, int((last_run or {}).get("turn_seq") or 0))
    window = narratives[start:]
    if limit > 0:
        window = window[-limit:]
    if not window:
        return {
            "narrative_parts": [],
            "world_state_event": {},
            "narrative_event": {},
            "window_turns": 0,
        }
    payloads: list[dict] = []
    for index, narrative in enumerate(window):
        following = (
            int(getattr(window[index + 1], "seq", 0) or 0)
            if index + 1 < len(window)
            else None
        )
        payload = _last_world_state(
            events, int(getattr(narrative, "seq", 0) or 0), following
        )
        if payload is not None:
            payloads.append(payload)
    world_state_event = (
        {
            "id": None,
            # The FIRST narrative's seq: the union spans the window, so anything
            # keyed on "after this turn" (the roster diff) must span all of it.
            "seq": int(getattr(window[0], "seq", 0) or 0),
            "payload": {
                "event": "world_state",
                "kind": "delta",
                "state": _union_sections(payloads, "state"),
                "venue": _union_sections(payloads, "venue"),
                "off_screen": _union_off_screen(payloads),
            },
        }
        if payloads
        else {}
    )
    return {
        "narrative_parts": _window_turns(window),
        "world_state_event": world_state_event,
        "narrative_event": _narrative_ref(window[0]),
        "window_turns": len(window),
    }


# -- the runner --------------------------------------------------------------


class HistorianRunner:
    """Drives the proposal fan-out off-turn over the evidence window (§3.6.1).

    Built by :func:`make_historian_runner`; wired once per engine in
    ``wiring.build_engine`` and handed to the post-turn hook and the API. All
    state it reads is the event log's (D9) — the only process-local state is the
    in-flight set, which is a guard, not truth.
    """

    def __init__(
        self,
        *,
        log: Any,
        fanout: ProposalFanout,
        cadence: int | Callable[[], int] = DEFAULT_HISTORIAN_CADENCE,
    ) -> None:
        self.log = log
        self.fanout = fanout
        #: An int or a zero-arg callable reading the ACTIVE preset's cadence —
        #: pass the live reader so a Studio edit applies on the next evaluation
        #: without a rebuild (the ``_planner_plan_cadence`` pattern).
        self.cadence = cadence

    # -- the reads --------------------------------------------------------
    def _cadence(self) -> int:
        """The cadence RIGHT NOW (a callable reads the active preset live)."""
        raw = self.cadence() if callable(self.cadence) else self.cadence
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_HISTORIAN_CADENCE
        return value if value >= 1 else DEFAULT_HISTORIAN_CADENCE

    def last_run(self, session_id: str) -> dict | None:
        """The latest ``historian_run`` on the chain (the TRACE, any status).

        What the status API shows: the run's own row, whoever it ended. The due
        rule reads :meth:`last_completed_run` instead (§3.6.3).
        """
        return load_last_historian_run(self.log, session_id)

    def last_completed_run(self, session_id: str) -> dict | None:
        """The last run that CONSUMED its window — the clock (§3.6.3)."""
        return load_last_completed_historian_run(self.log, session_id)

    def committed_turns(self, session_id: str) -> int:
        """Committed narratives on the chain (the due rule's left side)."""
        return committed_turn_count(self.log, session_id)

    def is_due(self, session_id: str) -> bool:
        """Is an automatic run due? (Two cheap chain reads; no model call.)

        Due includes a RETRY: a missed run advanced no clock, so the turn after it
        is due again — every turn until a run completes (§3.6.3).
        """
        return historian_is_due(
            last_run=self.last_completed_run(session_id),
            committed_turns=self.committed_turns(session_id),
            cadence=self._cadence(),
        )

    def due_in(self, session_id: str) -> int:
        """Committed turns until the next automatic run (0 = due now)."""
        last = int((self.last_completed_run(session_id) or {}).get("turn_seq") or 0)
        return max(0, self._cadence() - (self.committed_turns(session_id) - last))

    def inflight(self, session_id: str) -> bool:
        """Is a run for this session in flight in this process?"""
        return historian_inflight(session_id)

    # -- the run ----------------------------------------------------------
    async def run(self, session_id: str, *, trigger: str) -> dict:
        """Run the historian NOW over the window; return the run's counts.

        ``trigger`` is ``"cadence"`` (the post-turn hook) or ``"manual"`` (the
        owner's run-now door). The due rule is NOT consulted here — the door
        always runs, and the hook already asked. Raises
        :class:`HistorianBusy` when a run for this session is already in flight;
        every other failure is contained (a ``failed`` row + a logged traceback),
        because a run is post-turn housekeeping that must never surface to the
        user. There is no run-total time bound: each lane call is bounded by its
        CONNECTION's ``timeout``, and a lane that hits it lands in
        ``failed_lanes`` while the run finishes normally (the rows already filed
        stay). The returned dict carries ``error`` only on a runner-level
        failure, the one case that also stamps ``status: "failed"``.

        The window starts after the last COMPLETED run, not the last row of any
        status: a retried run therefore re-covers the turns a missed run never
        judged (§3.6.3).
        """
        if session_id in _INFLIGHT:
            raise HistorianBusy(session_id)
        _INFLIGHT.add(session_id)
        started = time.monotonic()
        turn_seq = 0
        try:
            turn_seq = self.committed_turns(session_id)
            window = historian_window(
                self.log, session_id, last_run=self.last_completed_run(session_id)
            )
            self._append(
                session_id,
                {
                    "status": "started",
                    "trigger": trigger,
                    "turn_seq": turn_seq,
                    "window_turns": int(window["window_turns"]),
                },
            )
            outcome = await self._drive(session_id, window)
            outcome["promotions"] = self._promote(session_id)
            outcome["duration_ms"] = int((time.monotonic() - started) * 1000)
            self._append(
                session_id,
                {
                    "status": "finished",
                    "trigger": trigger,
                    "turn_seq": turn_seq,
                    **outcome,
                },
            )
            return {"turn_seq": turn_seq, **outcome}
        except Exception as exc:  # noqa: BLE001 - housekeeping never surfaces
            # Runner-level containment: the dispatcher/fan-in/log raised. The
            # row is the trace and the guard is released in ``finally``; the
            # rows the failed run already filed stay (append-only, D9). A lane's
            # own failure never reaches here — ``AssetProposalFanout.lane``
            # catches it into ``failed_lanes``.
            error = str(exc)[:500]
            logger.exception("historian run failed for %s", session_id)
            self._append(
                session_id,
                {
                    "status": "failed",
                    "trigger": trigger,
                    "turn_seq": turn_seq,
                    "error": error,
                },
            )
            return {
                "gate": "error",
                "filed": 0,
                "dropped": 0,
                "failed_lanes": [],
                "promotions": 0,
                "turn_seq": turn_seq,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error": error,
            }
        finally:
            _INFLIGHT.discard(session_id)

    def _promote(self, session_id: str) -> int:
        """The cast ladder's promotion pass, CONTAINED (§5.2) — never fails the run.

        The pass itself is deterministic engine code (zero model calls) that files
        at most one ``origin="promotion"`` actor-add row; it rides this cadence so
        the owner's review queue is where an engine promotion appears, exactly like
        a lane's row. Its own failure must not reach ``run``'s except: that would
        stamp the run ``failed`` and (because a failed run is a MISSED run) retry
        the whole window over a review-queue nicety. So the containment is
        INTERNAL here — the run finishes, ``promotions`` reads 0, and the warning
        is the trace.
        """
        try:
            return promotion_pass(self.log, session_id)
        except Exception:  # noqa: BLE001 - a queued nicety never fails the run
            logger.warning(
                "promotion pass failed for %s", session_id, exc_info=True
            )
            return 0

    async def _drive(self, session_id: str, window: Mapping[str, Any]) -> dict:
        """Drive the fan-out's four halves over the window; return its counts.

        The order is the graph's: dispatcher (the double gate) → router (the
        per-type Sends) → lanes → fan-in flush. Lane results are merged through
        the PUBLIC reducers (``merge_proposal_rows``/``merge_proposal_meta``), so
        the summary sees every lane's ``dropped``/``failed`` rows exactly as an
        in-graph turn did — a naive dict update would clobber the earlier lanes.

        The router's ``Send`` tasks are awaited one after another: the lanes'
        concurrency inside a turn comes from the graph's fan-out, and the
        historian is a background task where serial lane calls cost nothing a
        user waits on. Proposals are already filed per lane by ``join``.
        """
        state = {
            "session_id": session_id,
            "narrative_parts": list(window.get("narrative_parts") or []),
            "world_state_event": dict(window.get("world_state_event") or {}),
            "narrative_event": dict(window.get("narrative_event") or {}),
        }
        plan = await self.fanout(state)
        plan = dict(plan) if isinstance(plan, Mapping) else {}
        rows: dict[str, list[dict]] = {}
        # An EMPTY update RESETS both channels (the reducers' turn-boundary rule),
        # so a lane that returned no key must not be merged at all — merging an
        # empty dict would wipe the earlier lanes' rows and the gate verdict.
        meta: dict[str, Any] = merge_proposal_meta({}, plan.get("proposal_meta"))
        for send in await self.fanout.route({**state, **plan}):
            partial = await self.fanout.lane(send.arg)
            if not isinstance(partial, Mapping):
                continue
            if partial.get("proposal_rows"):
                rows = merge_proposal_rows(rows, partial["proposal_rows"])
            if partial.get("proposal_meta"):
                meta = merge_proposal_meta(meta, partial["proposal_meta"])
        merged = {**state, "proposal_rows": rows, "proposal_meta": meta}
        self.fanout.join(merged)
        lanes = [str(lane) for lane in (meta.get("lanes") or [])]
        return {
            "gate": str(meta.get("gate") or "no_evidence"),
            "filed": sum(len(rows.get(lane) or []) for lane in lanes),
            "dropped": len(meta.get("dropped") or []),
            "failed_lanes": list(meta.get("failed") or []),
        }

    def _append(self, session_id: str, payload: Mapping[str, Any]) -> None:
        """Append one ``historian_run`` row; a log hiccup costs the row, not the run."""
        try:
            self.log.append_event(
                session_id,
                "system",
                HISTORIAN_ACTOR,
                {"event": HISTORIAN_RUN_EVENT, **payload},
            )
        except Exception:  # noqa: BLE001 - the run's trace is best-effort
            logger.warning(
                "could not record historian_run for %s", session_id, exc_info=True
            )


def make_historian_runner(
    *,
    log: Any,
    fanout: ProposalFanout,
    cadence: int | Callable[[], int] = DEFAULT_HISTORIAN_CADENCE,
) -> HistorianRunner:
    """Build the historian runner over the wired proposal fan-out (§3.6.1).

    ``fanout`` is the SAME object the engine used to hand ``build_graph`` — the
    ``proposals`` generator binding, the shared decision caller and the preset
    ``proposals`` flow all arrive through it, so the historian inherits the exact
    model seams of a turn. ``cadence`` is an int or the live preset reader.
    """
    return HistorianRunner(log=log, fanout=fanout, cadence=cadence)
