"""W9.26 §2.1 — the context gauge, the claim protocol and the compact job.

One module, three jobs, all W9.26 §2.3 / §2.4:

1. **The gauge** (:func:`measure_context`) — the per-turn, MEASURED context
   reading (⚑R1a: no estimate fallback, no per-turn reassembly). It resolves the
   narrate slot through the taxonomy, finds the TURN-SCOPED PEAK own-session
   ``usage`` row whose ``agent`` is one of that slot's label spellings (the max
   ``prompt_tokens`` among the rows appended since the narrative that CLOSED THE
   PREVIOUS TURN — the hook runs post-commit, so the newest narrative is this
   turn's own and can never be the boundary; the PEAK is the main call, not a
   trailing repair), follows that row's
   ``model`` to the connection that actually served it, and turns the
   connection's owner-declared Context Size into a usable prompt budget:

       budget = context_window − effective response length − RESERVE_TOKENS

   ``window`` undeclared ⇒ **no math, no rows** (D84 doctrine); no own matching
   row ⇒ **skip the turn** (a fresh era is silent until it measures itself).

2. **The claim protocol** (§2.3 step 1) — a session-level ``compacting`` claim
   on ``app.state.compaction_claims`` with an await-free check-and-acquire (the
   claim check and the turn-slot check are ONE block: bracketing them with an
   ``await`` is exactly the race the claim exists to close). The claim is
   released in the job's ``finally``; an engine restart clears in-RAM claims,
   which is safe because every idempotence check is **log-derived**.

3. **The compact job runner** (:func:`run_compact_job`) — drives Lane A's
   ``compact()`` service under the claim, with the §2.3 step 0/10 guards (the
   ALREADY-COMPACTED test is **successor presence**, never own-marker presence —
   every era child owns its birth marker; the fresh-era convergence guard bounds
   the FORCED path to one real compaction per era; ⚑R3a's ≤3 failed attempts;
   the noop verdicts) and the post-claim idempotent successor re-check that kills
   the duplicate-era race.

Callable contract for the post-turn hook (``api/app.py`` schedules every
``Callable[[str], Awaitable[None]]`` in its own task): :func:`make_context_monitor_hook`
returns exactly that shape and **never raises** — a monitor failure files its
own ``system``/``post_turn_failed`` row naming the hook (:data:`MONITOR_HOOK_NAME`)
and returns, so ``app.py``'s legacy failure recorder never mislabels it
``compaction_failed``.

Read discipline (AGENTS.md): usage/audit reads are OWN-session ``get_events``;
chain context (does a compaction child exist? what did the previous era
absorb?) is ``get_chain_events``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fabulist.connections.client import merge_sampling
from fabulist.connections.taxonomy import (
    LEGACY_TWINS,
    canonical,
    resolve_interaction_agent,
)
from fabulist.graph.compaction import (
    RESERVE_TOKENS,
    covered_turn_seqs,
    has_compaction_marker,
    prompt_budget_tokens,
    turn_groups,
    turn_rows,
)
from fabulist.log.schema_gate import (
    COMPACTION_FAILED_EVENT,
    COMPACTION_MARKER_EVENT,
    CONTEXT_LOW_EVENT,
    POST_TURN_FAILED_EVENT,
    USAGE_EVENT,
)
from fabulist.prompts.agent_sets import bindings_for

logger = logging.getLogger(__name__)

__all__ = [
    "CONTEXT_FORCE_RATIO",
    "CONTEXT_LOW_EVENT",
    "CONTEXT_LOW_RATIO",
    "COMPACTION_MARKER_EVENT",
    "CompactionClaims",
    "CompactJob",
    "ContextJobs",
    "ContextReading",
    "FORCED_TRIGGER",
    "FRESH_ERA_MIN_UNCOVERED_TURNS",
    "MANUAL_TRIGGER",
    "MAX_FORCED_ATTEMPTS",
    "MONITOR_HOOK_NAME",
    "NOOP_ALREADY_COMPACTED",
    "NOOP_ATTEMPT_LIMIT",
    "NOOP_INHERITED_COVER",
    "NOOP_NOTHING_UNCOVERED",
    "OVER_BUDGET_RATIO",
    "POST_TURN_FAILED_EVENT",
    "PollingTurnSlot",
    "RESERVE_TOKENS",
    "VARIANT_NOOP",
    "VARIANT_OVER_BUDGET",
    "VARIANT_PLAIN",
    "acquire_claim",
    "claim_acquire",
    "claim_held",
    "claim_release",
    "committed_narrative_seqs",
    "compaction_successor",
    "context_low_payload",
    "covering_summary_owner",
    "effective_response_length",
    "emit_context_low",
    "failed_attempts",
    "has_completed_compaction",
    "has_era_marker",
    "history_head",
    "idempotent_successor",
    "latest_usage_row",
    "make_context_monitor_hook",
    "measure_context",
    "measurement_boundary",
    "narrate_labels",
    "run_compact_job",
    "serving_connection",
    "session_title",
    "slot_labels",
    "turn_peak_usage_row",
    "uncovered_turn_groups",
]

# --- constants (pinned by the design, not knobs) ----------------------------

#: The plain 80% notice (owner copy lives in the UI; this row is the trigger).
CONTEXT_LOW_RATIO = 0.8
#: Forced compaction at 90% — the SAME path as the button (§0 ruling 3).
CONTEXT_FORCE_RATIO = 0.9
# ``CONTEXT_LOW_EVENT`` / ``COMPACTION_MARKER_EVENT`` / ``POST_TURN_FAILED_EVENT``
# are the SCHEMA GATE's typed ``system`` variants (:mod:`fabulist.log.schema_gate`,
# Lane A) and are imported above, not re-declared: one vocabulary, no drift. The
# payloads this module emits are exactly the shapes those validators accept
# (``ratio`` FLOAT-only, ``over_budget``/``noop`` omitted unless true).
#: The trigger label the forced path writes into ``summary``/``compaction``.
FORCED_TRIGGER = "forced_90"
#: The trigger label the button path writes.
MANUAL_TRIGGER = "manual"

#: ⚑R3a: at most three failed forced attempts per era (own ``compaction_failed``
#: rows, log-derived so it survives a restart). The button stays available.
MAX_FORCED_ATTEMPTS = 3

#: The over-budget sentinel ratio: a fixed FLOAT, never ``prompt_tokens /
#: budget`` (division by a non-positive budget is meaningless; the gate is
#: FLOAT-only and a ``1`` int would be rejected).
OVER_BUDGET_RATIO = 1.0

#: Dedupe VARIANTS (§2.1): the key is ``(session, variant)`` — a literal
#: one-row-per-session rule would suppress the noop/over-budget rows in exactly
#: the scenarios that produce them.
VARIANT_PLAIN = "plain"
VARIANT_NOOP = "noop"
VARIANT_OVER_BUDGET = "over_budget"

#: Job verdicts (the DTO's ``status``).
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_NOOP = "noop"

#: Noop reasons (§2.3 step 0). ``ALREADY_COMPACTED`` is the one that ALSO emits
#: a ``context_low`` ``noop:true`` row (a completed compaction still ≥0.9, or a
#: fresh era that cannot free more window).
NOOP_ALREADY_COMPACTED = "already_compacted"
NOOP_ATTEMPT_LIMIT = "attempt_limit"
NOOP_NOTHING_UNCOVERED = "nothing_uncovered"

#: §2.3 step 0, repaired (W9.26 gate defect): the FORCED path runs at most once
#: per era. The old test was own-marker PRESENCE, which every era child passes at
#: birth — so no era past 1 could ever compact again (manual or forced). The real
#: intent is "a fresh era with nothing meaningful uncovered cannot free window":
#: fewer than this many UNCOVERED turn groups with the ratio still ≥0.9 ⇒ noop,
#: no force. Two or more uncovered turns is substantial history and the era N→N+1
#: compaction is legitimate (the live repro: an era child with one new turn at
#: ≥0.9 must compact, not noop).
FRESH_ERA_MIN_UNCOVERED_TURNS = 2

#: Reason copy for §2.3 step 10's repaired guard: everything is covered, but by an
#: INHERITED recap only, so forking would duplicate the era the session already
#: lives in. Names the cause so the client/Inspector can tell it apart from
#: ``already_compacted``.
NOOP_INHERITED_COVER = (
    "nothing left to compact — every turn is already covered by the inherited "
    "recap; a fork here would duplicate the era"
)

#: The hook's name in failure rows / logs (``app.py`` names hooks by
#: ``__name__``; we set it explicitly so the record is stable).
MONITOR_HOOK_NAME = "context_monitor"

#: Owner copy for a job's noop reason (the client renders ``reason`` inline).
NOOP_MESSAGES = {
    NOOP_ALREADY_COMPACTED: (
        "compaction cannot free more window — check declared Context Size / "
        "Response Length / prefix size"
    ),
    NOOP_ATTEMPT_LIMIT: (
        "forced compaction stopped after "
        f"{MAX_FORCED_ATTEMPTS} failed attempts; use the Compact button"
    ),
    NOOP_NOTHING_UNCOVERED: "nothing left to compact — a summary already covers every turn",
}

#: Cap on the error text carried by a failure row (mirrors ``api/app.py``).
_FAILURE_ERROR_LIMIT = 800
#: How many idle waits the forced/manual path will spend before giving the slot
#: up for this turn (a turn that keeps restarting cannot block compaction
#: forever — the next turn retries).
MAX_SLOT_WAITS = 3
#: Kept terminal job records (the client polls after ``done``; this is a
#: bounded in-RAM courtesy registry, not a store).
MAX_JOB_RECORDS = 128

#: The narrate interaction and its fallback agent (the same pair every narrate
#: seam resolves with — ``wiring._narrate_connection``, §2.1).
_NARRATE_INTERACTION = "narrate"
_NARRATE_FALLBACK = "narrator"
#: Legacy/dialect aliases that also ride a narrate usage row (§2.1's label
#: union). ``narrate`` is the interaction id; ``narrator`` the legacy slot.
_NARRATE_LABEL_ALIASES = frozenset({"narrate", "narrator"})


# --- slot labels ------------------------------------------------------------


def slot_labels(agent: str) -> tuple[str, ...]:
    """Every ``usage``-row ``agent`` spelling that means "this SLOT served it".

    The taxonomy's twin rule says ``narrator`` and ``completion.narration`` are
    ONE lookup, and live rows carry the RESOLVED slot label — which may be
    either spelling (and, per §1, ``completion.narration`` served two different
    connections live). So the union is the resolved slot + its twin spelling(s).

    ``taxonomy._twin_pair`` is private; rather than import a private helper this
    re-derives the pair from the PUBLIC vocabulary (:data:`LEGACY_TWINS` +
    :func:`canonical`), which is the same four lines and cannot drift: a twin is
    a mapping entry, not a naming convention.

    Deliberately NOT including the ``narrate`` aliases: they belong to the
    NARRATE union only (:func:`narrate_labels`) — adding them to every slot
    would let a narrator usage row answer the summarizer's row (the label union
    is how a ledger row names the call that served THAT slot).
    """
    typed = canonical(agent)
    twins = {legacy for legacy, twin in LEGACY_TWINS.items() if twin == typed}
    return tuple(sorted({agent, typed, *twins}))


def narrate_labels(agent: str) -> tuple[str, ...]:
    """The NARRATE label union: the slot's twin pair + the legacy aliases (§2.1).

    ``narrate`` is the interaction id and ``narrator`` the legacy slot; both ride
    real rows (the dialect drift §1 documents), so the gauge must accept either
    on top of the twin pair.
    """
    return tuple(sorted({*slot_labels(agent), *_NARRATE_LABEL_ALIASES}))


def _narrate_slot(bindings: Mapping[str, Any] | None, agents: Mapping[str, str]) -> str:
    """The slot name serving ``narrate`` right now (never raises, §2.1)."""
    return resolve_interaction_agent(bindings, agents, _NARRATE_INTERACTION, _NARRATE_FALLBACK)


def _agents_of(registry: Any) -> Mapping[str, str]:
    """The registry's raw slot -> connection map (empty when unresolved)."""
    agents = getattr(registry, "agents", None)
    return agents if isinstance(agents, Mapping) else {}


# --- connections ------------------------------------------------------------


def effective_response_length(connection: Any) -> int:
    """The connection's Response Length in tokens: ``sampling.max_tokens``.

    The ONLY correct source (W9.25 §3.5 / §1): there is no
    ``conn.response_length`` field. Merged over
    :data:`fabulist.connections.client.DEFAULT_SAMPLING` so an un-authored
    sampler still yields the number the request will carry.
    """
    sampling = merge_sampling(getattr(connection, "sampling", None) or {}, {})
    return int(sampling.get("max_tokens") or 0)


def serving_connection(registry: Any, model: str | None, *, agent: str) -> Any | None:
    """The connection that served a usage row naming ``model`` (§2.1 tie-break).

    Order, documented because the design leaves the tie-break to the
    implementation:

    1. the **narrate slot's current connection** when its ``model`` equals the
       row's — the row was written by a call this slot resolved, so the slot's
       binding is the strongest evidence of WHICH entry served it;
    2. otherwise the first declared connection (name-sorted, so it is
       deterministic) whose ``model`` matches the row;
    3. otherwise the narrate slot's current connection (a re-bound slot whose
       old model is no longer declared still measures against today's
       connection — never a guess at history);
    4. nothing resolvable ⇒ ``None`` (no math, no rows).
    """
    connections = getattr(registry, "connections", None)
    connections = connections if isinstance(connections, Mapping) else {}
    preferred = None
    try:
        preferred = registry.connection_for(agent)
    except Exception:  # noqa: BLE001 - an unresolvable slot means no window math
        preferred = None
    if preferred is not None and model and getattr(preferred, "model", None) == model:
        return preferred
    if model:
        for name in sorted(connections):
            connection = connections[name]
            if getattr(connection, "model", None) == model:
                return connection
    return preferred


# --- usage rows -------------------------------------------------------------


def _usage_payload(event: Any) -> dict[str, Any] | None:
    """The payload of a ``system``/``usage`` row, else ``None``."""
    if getattr(event, "type", None) != "system":
        return None
    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping) or payload.get("event") != USAGE_EVENT:
        return None
    return dict(payload)


def latest_usage_row(
    events: list[Any], labels: tuple[str, ...] | set[str]
) -> dict[str, Any] | None:
    """Newest usage row in ``events`` whose ``agent`` is in ``labels``.

    A row with no POSITIVE ``prompt_tokens`` is not a measurement (a zeroed or
    failed call says nothing about how full the prompt was), so it is skipped in
    favour of the newest row that does measure. ``events`` must be in ``seq``
    order (``EventLog`` guarantees it).
    """
    wanted = set(labels)
    for event in reversed(events):
        payload = _usage_payload(event)
        if payload is None:
            continue
        if str(payload.get("agent") or "") not in wanted:
            continue
        try:
            prompt_tokens = int(payload.get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if prompt_tokens <= 0:
            continue
        payload["prompt_tokens"] = prompt_tokens
        payload["seq_of_row"] = getattr(event, "seq", None)
        return payload
    return None


def committed_narrative_seqs(events: list[Any]) -> list[int]:
    """Every COMMITTED ``narrative`` row seq in ``events``, ascending.

    A committed narrative is ``type == "narrative"`` and non-ephemeral (the row
    the dispatcher appends; a draft/streaming row is ephemeral and is not a
    commit, so it never closes a turn).
    """
    seqs: list[int] = []
    for event in events:
        if getattr(event, "type", None) != "narrative":
            continue
        if getattr(event, "ephemeral", False):
            continue
        seq = getattr(event, "seq", None)
        if isinstance(seq, int):
            seqs.append(seq)
    seqs.sort()
    return seqs


def measurement_boundary(events: list[Any]) -> int | None:
    """The seq of the narrative that CLOSES THE PREVIOUS TURN, or ``None``.

    This is the SECOND-newest committed narrative — NOT the newest — because the
    gauge runs from the POST-TURN hook, which ``api/app.py`` schedules after the
    turn's ``done``/``committed`` frame, i.e. AFTER the engine appended this
    turn's own narrative (live: ``[571 user] [573-575 narrate usage rows]
    [576 narrative COMMIT] [577+ extraction/bts/embedder tail] → hook``). At
    hook time the newest committed narrative IS the turn being measured, so a
    boundary at it puts every one of the turn's usage rows *before* the window
    and the gauge silently skips EVERY turn (the live defect: no context_low at
    a true ratio of 0.966).

    The narrative that closes the PREVIOUS turn is the only honest boundary: the
    measured turn's usage rows sit between it and this turn's narrative. Fewer
    than two committed narratives means there is no previous turn to bound
    (a first turn, or a fresh era) ⇒ ``None`` ⇒ every own matching row counts.
    """
    seqs = committed_narrative_seqs(events)
    return seqs[-2] if len(seqs) >= 2 else None


def turn_peak_usage_row(
    events: list[Any], labels: tuple[str, ...] | set[str]
) -> dict[str, Any] | None:
    """The PEAK usage row of the CURRENT turn whose ``agent`` is in ``labels``.

    The window is turn-scoped, bounded BELOW ONLY: rows with
    ``seq > measurement_boundary`` count, where the boundary is the narrative
    that closed the PREVIOUS turn — see :func:`measurement_boundary` for why it
    is the second-newest committed narrative and not the newest (the hook runs
    post-commit, so the newest one is the measured turn's own). With no previous
    narrative (a first turn / a fresh era) every own matching row counts.

    There is deliberately NO upper bound. The measured turn's rows live between
    the previous narrative and this turn's narrative, but the post-turn tail
    (extraction/bts/embedder) appends usage rows AFTER this turn's narrative and
    those rows do not carry a narrate label spelling, so the label union already
    excludes them. An upper bound at the newest narrative would be exactly the
    defect: it would empty the window on every live turn.

    Of the matching rows, the one with the MAX ``prompt_tokens`` is the reading —
    the main narrate call, which always precedes the repair/patch calls of the
    narrate repair loop. A row with no POSITIVE ``prompt_tokens`` is not a
    measurement (a zeroed or failed call says nothing about how full the prompt
    was) and is skipped. Ties keep the EARLIEST peak, which is the main call by
    construction. ``events`` must be in ``seq`` order (``EventLog`` guarantees
    it).
    """
    wanted = set(labels)
    since = measurement_boundary(events)
    best: dict[str, Any] | None = None
    best_tokens = 0
    for event in events:
        payload = _usage_payload(event)
        if payload is None:
            continue
        if since is not None:
            seq = getattr(event, "seq", None)
            if not isinstance(seq, int) or seq <= since:
                continue  # a previous turn's row (or one with no provable seq)
        if str(payload.get("agent") or "") not in wanted:
            continue
        try:
            prompt_tokens = int(payload.get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if prompt_tokens <= 0:
            continue
        if prompt_tokens > best_tokens:
            payload["prompt_tokens"] = prompt_tokens
            payload["seq_of_row"] = getattr(event, "seq", None)
            best, best_tokens = payload, prompt_tokens
    return best


# --- the gauge --------------------------------------------------------------


@dataclass(frozen=True)
class ContextReading:
    """One measured context reading for the narrate slot (§2.1).

    Every number here is MEASURED (⚑R1a): ``prompt_tokens`` from a usage row,
    ``context_window``/``response_length`` from the connection that served that
    row. ``ratio`` is the only derived number and is never divided by a
    non-positive budget (see :data:`OVER_BUDGET_RATIO`).
    """

    agent: str
    model: str
    connection: str
    context_window: int
    response_length: int
    budget_tokens: int
    prompt_tokens: int
    ratio: float
    over_budget: bool = False
    basis: str = "measured"

    def as_payload(self, variant: str = VARIANT_PLAIN) -> dict[str, Any]:
        """The §2.2 ``context_low`` payload for this reading.

        Optional flags are OMITTED unless true (never emitted ``false``); the
        over-budget variant pins ``ratio`` to the FLOAT sentinel because the
        computed ratio does not exist.
        """
        payload: dict[str, Any] = {
            "event": CONTEXT_LOW_EVENT,
            "agent": self.agent,
            "ratio": float(OVER_BUDGET_RATIO if variant == VARIANT_OVER_BUDGET else self.ratio),
            "prompt_tokens": int(self.prompt_tokens),
            "budget_tokens": int(self.budget_tokens),
            "basis": self.basis,
        }
        if variant == VARIANT_OVER_BUDGET:
            payload["over_budget"] = True
        elif variant == VARIANT_NOOP:
            payload["noop"] = True
        return payload


def measure_context(
    log: Any,
    registry: Any,
    session_id: str,
    *,
    bindings: Mapping[str, Any] | None = None,
    events: list[Any] | None = None,
) -> ContextReading | None:
    """Measure the narrate slot's context use for ``session_id``, or ``None``.

    ``prompt_tokens`` is the TURN-SCOPED PEAK of the slot's own usage rows, not
    the newest row: window pressure is the PEAK assembled narrate prompt of the
    turn, and the main call always precedes its repairs. The live defect this
    fixes: a committed turn's narrate rows read ``[…, 1293, 6431, 1008]`` — the
    main call was 6431 but a trailing repair (1008) was newest, so the gauge
    reported ratio 0.14 and stayed silent at true pressure 0.897. The window is
    the current turn only: ``max(prompt_tokens)`` over matching rows whose
    ``seq`` is greater than the narrative that CLOSED THE PREVIOUS TURN (the
    second-newest committed ``narrative`` row — the hook runs after this turn's
    own narrative commits, so the newest one is not a boundary; see
    :func:`measurement_boundary`), and every own matching row on a first turn
    (no previous narrative). The PEAK row also supplies the ``model``/connection
    the window follows.

    ``None`` means "this turn has nothing honest to report": no declared window
    (D84), no resolvable connection, or no own matching usage row in the current
    turn for the narrate slot (⚑R1a — a fresh era is silent until it measures
    itself, and there is NO estimate fallback).

    ``events`` lets a caller that already read the OWN-session rows pass them in;
    ``bindings`` lets a test inject preset bindings (production passes nothing
    and the live preset is read once via :func:`bindings_for`).
    """
    slot = _narrate_slot(bindings if bindings is not None else bindings_for(log), _agents_of(registry))
    labels = narrate_labels(slot)
    if events is None:
        events = log.get_events(session_id)
    row = turn_peak_usage_row(events, labels)
    if row is None:
        return None
    connection = serving_connection(registry, str(row.get("model") or ""), agent=slot)
    if connection is None:
        return None
    window = getattr(connection, "context_window", None)
    if window is None:
        # Undeclared Context Size (⚑C): no monitor, no math, no rows.
        return None
    return _reading(connection, window=window, prompt_tokens=int(row["prompt_tokens"]), slot=slot)


def _reading(connection: Any, *, window: int, prompt_tokens: int, slot: str) -> ContextReading:
    """Build a :class:`ContextReading` from a resolved connection + measurement."""
    response = effective_response_length(connection)
    budget = prompt_budget_tokens(window, response)
    if budget is None:  # pragma: no cover - window is non-None by construction
        raise ValueError("prompt_budget_tokens returned None for a declared window")
    if budget <= 0:
        # A declaration problem compaction cannot fix: report it, never force.
        return ContextReading(
            agent=slot,
            model=str(getattr(connection, "model", "") or ""),
            connection=str(getattr(connection, "name", "") or ""),
            context_window=int(window),
            response_length=int(response),
            budget_tokens=int(budget),
            prompt_tokens=int(prompt_tokens),
            ratio=OVER_BUDGET_RATIO,
            over_budget=True,
        )
    return ContextReading(
        agent=slot,
        model=str(getattr(connection, "model", "") or ""),
        connection=str(getattr(connection, "name", "") or ""),
        context_window=int(window),
        response_length=int(response),
        budget_tokens=int(budget),
        prompt_tokens=int(prompt_tokens),
        ratio=round(prompt_tokens / budget, 4),
    )


# --- context_low emission (variant-scoped dedupe) ---------------------------


def _variant_of_payload(payload: Mapping[str, Any]) -> str:
    """The dedupe variant of an existing ``context_low`` row."""
    if payload.get("over_budget") is True:
        return VARIANT_OVER_BUDGET
    if payload.get("noop") is True:
        return VARIANT_NOOP
    return VARIANT_PLAIN


def own_context_low_variants(log: Any, session_id: str) -> set[str]:
    """Variants already emitted in THIS session's own rows (dedupe input)."""
    variants: set[str] = set()
    for event in log.get_events(session_id):
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if isinstance(payload, Mapping) and payload.get("event") == CONTEXT_LOW_EVENT:
            variants.add(_variant_of_payload(payload))
    return variants


def context_low_payload(reading: ContextReading, variant: str = VARIANT_PLAIN) -> dict[str, Any]:
    """The wire payload for one notice variant (§2.2)."""
    return reading.as_payload(variant)


def emit_context_low(
    log: Any,
    session_id: str,
    reading: ContextReading,
    *,
    variant: str = VARIANT_PLAIN,
) -> bool:
    """Append ONE ``system``/``context_low`` row for ``variant``; dedupe per variant.

    The dedupe key is ``(session, variant)``: the plain 80% row, the
    ``noop:true`` row and the ``over_budget:true`` row each land at most once
    per session. Returns whether a row was written. Best-effort: measurement
    must never break the turn (the append is loud in the journal, not fatal).

    The dedupe reads the session's OWN rows (audit discipline) and is only ever
    reached at ≥80%, so it costs one extra read on the turns that matter —
    never on an ordinary turn.
    """
    if variant in own_context_low_variants(log, session_id):
        return False
    try:
        log.append_event(
            session_id, "system", reading.agent, context_low_payload(reading, variant)
        )
    except Exception:  # noqa: BLE001 - measurement must never break the turn
        logger.warning(
            "could not append context_low (%s) for %s", variant, session_id, exc_info=True
        )
        return False
    return True


# --- the claim protocol (§2.3 step 1) ---------------------------------------


class CompactionClaims(set):
    """The in-RAM session-level ``compacting`` claim set (built in ``build_app``).

    A plain ``set[str]`` with the claim semantics attached: :meth:`acquire` is
    the ONE await-free check-and-set, :meth:`release` is idempotent, and an
    engine restart clears everything — safe, because every idempotence check
    downstream is log-derived (§5 "single-engine-per-log").

    The free functions :func:`claim_held` / :func:`claim_acquire` /
    :func:`claim_release` work on ANY mutable membership container, so B may
    construct a bare ``set`` instead of this class without a code change.
    """

    def held(self, session_id: str) -> bool:
        return session_id in self

    def acquire(self, session_id: str) -> bool:
        """Claim ``session_id``; ``False`` when it is already claimed."""
        if session_id in self:
            return False
        self.add(session_id)
        return True

    def release(self, session_id: str) -> None:
        """Drop the claim (idempotent — a never-acquired release is a no-op)."""
        self.discard(session_id)


def claim_held(claims: Any, session_id: str) -> bool:
    """Whether ``session_id`` is claimed (``False`` when no registry is wired)."""
    if claims is None:
        return False
    try:
        return session_id in claims
    except TypeError:  # pragma: no cover - a non-container is "not claimed"
        return False


def claim_acquire(claims: Any, session_id: str) -> bool:
    """Await-free check-and-set of the claim; ``False`` when already held.

    NEVER await between the check and the set: that is the entire point of the
    protocol (an ``await`` there re-opens the done/claim race).
    """
    if claims is None:
        return True
    try:
        if session_id in claims:
            return False
        claims.add(session_id)
    except AttributeError:  # pragma: no cover - duck-typed dud
        return True
    return True


def claim_release(claims: Any, session_id: str) -> None:
    """Release the claim; never raises (a release must always succeed)."""
    if claims is None:
        return
    try:
        claims.discard(session_id)
    except AttributeError:  # pragma: no cover - duck-typed dud
        try:
            claims.remove(session_id)
        except Exception:  # noqa: BLE001
            pass


class PollingTurnSlot:
    """A turn-slot view over a caller-supplied ``busy`` predicate.

    ``api/app.py`` owns the per-session turn slot (``_Session.turn`` /
    ``_Turn.done``); this adapter is the shape the monitor consumes so the gauge
    never imports the API layer: ``busy(session_id)`` truthiness plus
    ``async wait_idle(session_id)`` (bounded polling, no busy-wait).

    B constructs ONE of these over its session registry::

        slot = PollingTurnSlot(lambda sid: (s := sessions.get(sid)) is not None
                               and s.turn is not None and not s.turn.done.is_set())
    """

    #: Poll cadence while waiting for the slot (seconds).
    POLL_SECONDS = 0.25

    def __init__(
        self,
        busy: Callable[[str], bool],
        *,
        poll: float = POLL_SECONDS,
        max_wait: float = 120.0,
    ) -> None:
        self._busy = busy
        self._poll = float(poll)
        self._max_wait = float(max_wait)

    def busy(self, session_id: str) -> bool:
        try:
            return bool(self._busy(session_id))
        except Exception:  # noqa: BLE001 - an unreadable slot is "clear"
            return False

    async def wait_idle(self, session_id: str) -> None:
        """Await until the slot clears (bounded — the caller re-checks anyway)."""
        waited = 0.0
        while self.busy(session_id) and waited < self._max_wait:
            await asyncio.sleep(self._poll)
            waited += self._poll


def _slot_busy(slot: Any, session_id: str) -> bool:
    """``slot.busy(session_id)`` for the adapter shape, a duck-typed variant, or None."""
    if slot is None:
        return False
    busy = getattr(slot, "busy", None)
    if busy is None:
        return False
    try:
        return bool(busy(session_id))
    except TypeError:  # pragma: no cover - a zero-arg busy()
        return bool(busy())


async def _slot_wait_idle(slot: Any, session_id: str) -> None:
    """Await ``slot.wait_idle(session_id)`` (no-op for a slot-less hook)."""
    if slot is None:
        return
    waiter = getattr(slot, "wait_idle", None)
    if waiter is None:
        return
    result = waiter(session_id)
    if inspect.isawaitable(result):
        await result


async def acquire_claim(
    claims: Any,
    session_id: str,
    slot: Any = None,
    *,
    max_waits: int = MAX_SLOT_WAITS,
) -> tuple[bool, str]:
    """Wait for the turn slot to clear, then claim — one await-free block.

    Returns ``(acquired, reason)`` where ``reason`` is ``"ok"``, ``"claimed"``
    (someone else holds it) or ``"slot"`` (the slot never cleared inside
    :data:`MAX_SLOT_WAITS` idle waits — give this turn up; the next one retries).

    The claim check and the slot check happen in the SAME block as the acquire:
    the design's requirement, not an optimization.
    """
    waits = 0
    while True:
        if _slot_busy(slot, session_id):
            if waits >= max_waits:
                return (False, "slot")
            waits += 1
            await _slot_wait_idle(slot, session_id)
            continue
        # --- one await-free block: claim check + slot check + acquire ---
        if claim_held(claims, session_id):
            return (False, "claimed")
        if _slot_busy(slot, session_id):  # re-read: nothing awaited since, cheap honesty
            continue
        claim_acquire(claims, session_id)
        return (True, "ok")


# --- log-derived idempotence / convergence helpers -------------------------


def _summary_rows(events: list[Any]) -> list[Any]:
    """Non-ephemeral ``summary`` rows, in seq order."""
    return [
        event
        for event in events
        if getattr(event, "type", None) == "summary" and not getattr(event, "ephemeral", False)
    ]


def _span(payload: Mapping[str, Any]) -> tuple[int, int] | None:
    """``covers_seqs`` as an ordered ``(lo, hi)`` pair, or ``None``."""
    raw = payload.get("covers_seqs")
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        lo, hi = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None
    return (min(lo, hi), max(lo, hi))


def _field(event: Any, key: str, default: Any = None) -> Any:
    """One attribute of a row, tolerating projection dicts (Lane A's shape)."""
    value = getattr(event, key, None)
    if value is None and isinstance(event, Mapping):
        value = event.get(key)
    return default if value is None else value


def history_head(events: list[Any]) -> int:
    """The chain's history head: the newest ``user``/``narrative`` seq.

    Deliberately NOT ``max(seq)``: a diagnostic ``system`` row appended after a
    cut (a sibling hook's row, a marker) must not make a summary look like it
    failed to span the head.
    """
    head = 0
    for event in events:
        if getattr(event, "type", None) in ("user", "narrative"):
            seq = getattr(event, "seq", 0)
            if isinstance(seq, int) and seq > head:
                head = seq
    return head


def _compaction_child(log: Any, session_id: str, at_seq: int) -> str | None:
    """A child of ``session_id`` forked at ``at_seq`` (compaction successor)."""
    try:
        children = log.list_branches(session_id)
    except Exception:  # noqa: BLE001 - bookkeeping read must not break the caller
        return None
    for child in sorted(children):
        info = log.get_session(child)
        if info is None:
            continue
        if info.get("base_seq") == at_seq:
            return child
    return None


def has_era_marker(log: Any, session_id: str) -> bool:
    """Whether THIS session OWNS a ``compaction`` marker row — it IS an era.

    Lane A's :func:`fabulist.graph.compaction.has_compaction_marker` (own rows
    only). This is the era-BIRTH predicate and nothing else: every era child owns
    the marker of the compaction that created it, so it can never answer "has
    this session been compacted already" — that is
    :func:`compaction_successor`'s question.
    """
    try:
        return bool(has_compaction_marker(log, session_id))
    except Exception:  # noqa: BLE001 - an unreadable log is "no marker"
        return False


def compaction_successor(log: Any, session_id: str) -> str | None:
    """A child of ``session_id`` whose OWN rows carry a ``compaction`` marker.

    §2.3 step 0's ALREADY-COMPACTED predicate, repaired. The era marker is only
    ever appended to the NEW session (``_start_era``), so a marker among a
    child's own rows means "this parent was compacted INTO that child" — the
    session is SUPERSEDED. A plain swipe fork inherits the marker on its CHAIN
    and therefore never qualifies (own-rows read, exactly like
    :func:`fabulist.api.app.compaction_successor`).

    Several marker children (a hand-crafted log) resolve to the newest by
    ``created_ts`` with an id tiebreak, deterministically.
    """
    list_branches = getattr(log, "list_branches", None)
    if not callable(list_branches):
        return None
    try:
        children = list(list_branches(session_id) or ())
    except Exception:  # noqa: BLE001 - an unreadable fork table is "no successor"
        return None
    get_session = getattr(log, "get_session", None)
    candidates: list[tuple[str, str]] = []
    for child in children:
        try:
            if not has_compaction_marker(log, child):
                continue
        except Exception:  # noqa: BLE001 - an unreadable child is not an era
            continue
        info = (get_session(child) if callable(get_session) else None) or {}
        candidates.append((str(info.get("created_ts") or ""), str(child)))
    if not candidates:
        return None
    return max(candidates)[1]


def idempotent_successor(log: Any, session_id: str) -> str | None:
    """The compaction successor of ``session_id``, when one already exists.

    TWO independent pieces of evidence, either of which means "never fork a
    second era":

    1. §2.3 step 1's span test — the newest non-ephemeral summary SPANS the
       chain's history head AND a child forks at exactly that summary's seq (the
       duplicate-era race a concurrent run loses);
    2. §2.3 step 0's supersession test — a child OWNS an era marker
       (:func:`compaction_successor`). This is what makes the test survive a
       crash between the fork and the marker bookkeeping, and it is the SAME
       predicate ``POST /compact``'s idempotent 200 answer uses, so the door and
       the runner cannot disagree about whether a session is superseded.
    """
    events = log.get_chain_events(session_id)
    summaries = _summary_rows(events)
    own = _summary_rows(log.get_events(session_id))
    candidate = (own or summaries or [None])[-1]
    if candidate is not None:
        span = _span(_field(candidate, "payload") or {})
        if span is not None and span[1] >= history_head(events):
            child = _compaction_child(log, session_id, int(_field(candidate, "seq", 0)))
            if child is not None:
                return child
    return compaction_successor(log, session_id)


def has_completed_compaction(log: Any, session_id: str) -> bool:
    """Whether this session has ALREADY been compacted — it is SUPERSEDED.

    §2.3 step 0, repaired (live gate defect, session d7de2e69): the old test was
    own-marker PRESENCE, which is true for every era child from birth (its first
    own row IS the marker of the compaction that made it) — so no era past 1
    could ever compact again, manually or forced, and the forced path died from
    era 2 onward. "Already compacted" means a COMPACTION SUCCESSOR exists; an own
    marker only means "this session is an era", which
    :func:`uncovered_turn_groups` and :func:`covering_summary_owner` use instead.
    """
    return idempotent_successor(log, session_id) is not None


def uncovered_turn_groups(log: Any, session_id: str) -> list[list[Any]]:
    """The turn groups the compact SERVICE would summarize (Lane A's helpers).

    Mirrors ``CompactService.compact``'s span computation exactly: turn PAIRS
    (:func:`fabulist.graph.compaction.turn_groups`) over the CHAIN read, minus
    everything an existing summary covers (:func:`covered_turn_seqs`). The chain
    read is what makes the count match the service — an era child inherits its
    parent's summary, so its covered set comes from the chain, and its uncovered
    set is exactly its own post-fork turns.
    """
    events = log.get_chain_events(session_id)
    covered = covered_turn_seqs(events)
    groups: list[list[Any]] = []
    for group in turn_groups(turn_rows(events)):
        last = group[-1] if group else None
        if _field(last, "type") == "narrative" and _field(last, "seq") in covered:
            continue
        groups.append(group)
    return groups


def covering_summary_owner(log: Any, session_id: str) -> str | None:
    """Who owns the summary that covers this session: ``"own"``/``"inherited"``.

    §2.3 step 10's repair guard. The covering summary is the NEWEST non-ephemeral
    summary on the chain — the one ``_recover`` forks from. ``"own"`` means THIS
    session wrote it (the crash-repair state: a summary landed, the fork/marker
    died), so forking from it is the repair. ``"inherited"`` means it came from
    an ancestor: the session already lives in that era, so step 10 would fork a
    DUPLICATE era from a recap it inherited — never do that.

    ``None`` when the chain has no summary at all (nothing to fork from).
    """
    summaries = _summary_rows(log.get_chain_events(session_id))
    if not summaries:
        return None
    newest = summaries[-1]
    return "own" if str(_field(newest, "session_id") or "") == session_id else "inherited"


def failed_attempts(log: Any, session_id: str) -> int:
    """Own ``compaction_failed`` rows — the ⚑R3a attempt counter (log-derived)."""
    count = 0
    for event in log.get_events(session_id):
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if isinstance(payload, Mapping) and payload.get("event") == COMPACTION_FAILED_EVENT:
            count += 1
    return count


_TITLE_EVENTS = frozenset({"session_created", "session_renamed"})


def session_title(log: Any, session_id: str) -> str | None:
    """The session's own title (latest ``session_created``/``session_renamed``).

    OWN rows, latest wins, revisions honoured (a rename is a revision event
    superseding the title, D10). Returns ``None`` for a session with no title
    anchor of its own — a lazily registered era child reads ``""`` today (§1);
    the caller falls back to the parent.
    """
    title: str | None = None
    try:
        events = log.get_events(session_id)
    except Exception:  # noqa: BLE001 - a title is never worth a failure
        return None
    for event in events:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            continue
        if getattr(event, "type", None) == "system" and payload.get("event") in _TITLE_EVENTS:
            value = payload.get("title")
            if isinstance(value, str) and value.strip():
                title = value
        elif getattr(event, "type", None) == "revision":
            revised = payload.get("revised")
            if isinstance(revised, Mapping):
                value = revised.get("title")
                if isinstance(value, str) and value.strip():
                    title = value
    return title


# --- the compact job registry (in-RAM, bounded) ----------------------------


def _job_id() -> str:
    return uuid.uuid4().hex


@dataclass
class CompactJob:
    """One in-RAM compaction job (the client polls its DTO, §2.2).

    ``chunks`` is the job-reuse surface of §2.3 step 4: the compact service
    lands each map chunk here, so a retry after a crash mid-map reuses the
    chunks that already cost money instead of re-summarizing them.
    """

    job_id: str
    session_id: str
    trigger: str
    status: str = STATUS_RUNNING
    stage: str | None = None
    error: str | None = None
    reason: str | None = None
    successor_session_id: str | None = None
    title: str | None = None
    chunks: list[str] = field(default_factory=list)
    task: Any = None

    def add_chunk(self, text: str) -> None:
        """Record one landed map chunk (the retry-reuse contract)."""
        self.chunks.append(str(text))

    def dto(self) -> dict[str, Any]:
        """The ``GET …/compaction/{job_id}`` payload (§2.2 contract keys).

        ``{status, session_id?, title?, error?, stage?}`` — absent facts are
        OMITTED rather than ``null`` (the same discipline the typed payloads
        use), plus ``reason`` for a ``noop`` verdict (the client renders it
        inline; a noop is not an error).
        """
        out: dict[str, Any] = {"status": self.status}
        if self.stage:
            out["stage"] = self.stage
        if self.status == STATUS_DONE:
            if self.successor_session_id:
                out["session_id"] = self.successor_session_id
            if self.title:
                out["title"] = self.title
            if self.chunks:
                out["chunks"] = len(self.chunks)
        elif self.status == STATUS_FAILED:
            out["error"] = self.error or "compaction failed"
        elif self.status == STATUS_NOOP:
            out["reason"] = self.reason or NOOP_MESSAGES[NOOP_NOTHING_UNCOVERED]
            if self.successor_session_id:
                # An ``already_compacted`` noop IS the idempotent answer: name the
                # successor exactly as ``POST /compact``'s 200 does, so a poller
                # can follow the era either way.
                out["session_id"] = self.successor_session_id
                if self.title:
                    out["title"] = self.title
        return out


class ContextJobs:
    """The in-RAM job registry behind ``app.state.context_jobs`` (§2.2).

    Turn-slot doctrine: one compaction at a time per session (:meth:`create`
    answers ``None`` while one is running), and a BOUNDED record history so the
    client may poll for a few seconds after ``done``. Not a store — the log
    holds the truth; an engine restart loses nothing that matters because every
    idempotence check is log-derived.
    """

    def __init__(self, *, max_records: int = MAX_JOB_RECORDS) -> None:
        self._jobs: dict[str, CompactJob] = {}
        self._max_records = int(max_records)

    # -- lifecycle ---------------------------------------------------------
    def create(self, session_id: str, trigger: str) -> CompactJob | None:
        """Create a running job, or ``None`` when one is already running here."""
        if self.running_for(session_id) is not None:
            return None
        job = CompactJob(job_id=_job_id(), session_id=session_id, trigger=trigger)
        self._jobs[job.job_id] = job
        self._prune()
        return job

    def get(self, job_id: str) -> CompactJob | None:
        return self._jobs.get(job_id)

    def running_for(self, session_id: str) -> CompactJob | None:
        for job in self._jobs.values():
            if job.session_id == session_id and job.status == STATUS_RUNNING:
                return job
        return None

    def records_for(self, session_id: str) -> list[CompactJob]:
        return [job for job in self._jobs.values() if job.session_id == session_id]

    def _prune(self) -> None:
        """Drop the oldest TERMINAL records past the cap (running ones survive)."""
        if len(self._jobs) <= self._max_records:
            return
        for job in list(self._jobs.values()):
            if len(self._jobs) <= self._max_records:
                break
            if job.status != STATUS_RUNNING:
                self._jobs.pop(job.job_id, None)


# --- the compact service invocation (§2.3, Lane A's contract) ---------------


@dataclass(frozen=True)
class JobOutcome:
    """Normalized verdict of one compact-service call."""

    status: str
    successor_session_id: str | None = None
    stage: str | None = None
    error: str | None = None
    reason: str | None = None
    title: str | None = None
    chunks: tuple[str, ...] = ()


#: Keyword names the runner will pass to the service when it accepts them.
_SERVICE_KEYWORDS = ("trigger", "job")


def _accepted_keywords(fn: Callable[..., Any]) -> set[str]:
    """The keyword names ``fn`` accepts (everything when it takes ``**kwargs``)."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        return set(_SERVICE_KEYWORDS)
    names = set()
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return set(_SERVICE_KEYWORDS)
        names.add(parameter.name)
    return names


def _compact_callable(service: Any) -> Callable[..., Any] | None:
    """The service's ``compact`` method (or the service itself when callable)."""
    if service is None:
        return None
    method = getattr(service, "compact", None)
    if callable(method):
        return method
    if callable(service):
        return service
    return None


async def invoke_compact_service(
    service: Any,
    session_id: str,
    log: Any,
    *,
    trigger: str,
    job: CompactJob,
) -> Any:
    """Call Lane A's service with the documented shape, tolerating its signature.

    The documented (canonical) shape is::

        await compact_service.compact(session_id, log, trigger=..., job=...)

    The adapter only withholds a keyword the callable does not declare, so a
    service that spells the job surface differently (or omits it) still runs
    instead of raising ``TypeError`` mid-job. It never invents positional
    arguments and it never silently swallows a TypeError from INSIDE the
    service (the call itself is a single expression).
    """
    fn = _compact_callable(service)
    if fn is None:
        raise TypeError("no compact service wired (app.state.compact_service is None)")
    kwargs: dict[str, Any] = {}
    accepted = _accepted_keywords(fn)
    if "trigger" in accepted:
        kwargs["trigger"] = trigger
    if "job" in accepted:
        kwargs["job"] = job
    result = fn(session_id, log, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _normalize_outcome(result: Any, *, job: CompactJob, log: Any) -> JobOutcome:
    """Normalize a service verdict (mapping or object) into :class:`JobOutcome`."""
    if result is None:
        return JobOutcome(
            status=STATUS_FAILED,
            stage=job.stage or "job",
            error="compact service returned no outcome",
        )
    if isinstance(result, Mapping):
        get = result.get
    else:
        def get(key: str, default: Any = None) -> Any:
            return getattr(result, key, default)

    status = str(get("status") or "").strip().lower()
    if status not in (STATUS_DONE, STATUS_FAILED, STATUS_NOOP):
        return JobOutcome(
            status=STATUS_FAILED,
            stage="job",
            error=f"compact service returned an unknown status {status!r}",
        )
    successor = (
        get("successor_session_id") or get("successor") or get("session_id") or None
    )
    if status == STATUS_DONE and not successor:
        # Log-derived repair: the service forked, it just did not say where.
        successor = idempotent_successor(log, job.session_id)
    reason = get("reason") or None
    if status == STATUS_NOOP and str(reason or "") == "":
        reason = NOOP_NOTHING_UNCOVERED
    return JobOutcome(
        status=status,
        successor_session_id=str(successor) if successor else None,
        stage=get("stage") or None,
        error=get("error") or None,
        reason=str(reason) if reason else None,
        title=get("title") or None,
    )


def _append_compaction_failure(log: Any, session_id: str, *, stage: str, error: str) -> None:
    """Record a job-level failure as ``system``/``compaction_failed`` (S4c shape).

    The service records its own stage failures; this covers what the JOB cannot
    hand to the service (no service wired, the service raised, an unusable
    verdict). Best-effort — reporting must never break the job.
    """
    try:
        log.append_event(
            session_id,
            "system",
            "summarizer",
            {
                "event": COMPACTION_FAILED_EVENT,
                "stage": stage,
                "error": str(error)[:_FAILURE_ERROR_LIMIT],
            },
        )
    except Exception:  # noqa: BLE001 - reporting must never break compaction
        logger.exception("could not record compaction failure for %s", session_id)


async def run_compact_job(
    job: CompactJob,
    *,
    log: Any,
    service: Any,
    claims: Any = None,
    slot: Any = None,
) -> CompactJob:
    """Drive one compaction end to end, under the claim (§2.3 steps 0-1, 4).

    Order is load-bearing:

    1. **acquire** — wait for the turn slot to clear, then claim in ONE
       await-free block; ``claimed``/``slot`` ⇒ the job ends ``noop`` (someone
       else is compacting, or a turn keeps the slot) and nothing is written;
    2. **re-check the successor** (post-claim, log-derived) — a compaction
       successor (a child that OWNS an era marker, or the span-consistent fork a
       concurrent run just made) ⇒ ``noop``/``already_compacted`` WITH that
       successor, never a second era. This is the idempotent test, and it is
       companion to ``POST /compact``'s 200 answer (same predicate, so the two
       agree);
    3. **guards** (§2.3 step 0/10), all derived from the turn groups the service
       would summarize:

       * nothing uncovered + the covering summary is INHERITED ⇒
         ``noop``/``nothing_uncovered`` (:data:`NOOP_INHERITED_COVER`): step 10's
         repair fork applies to an OWN summary only;
       * FORCED path, own era marker, fewer than
         :data:`FRESH_ERA_MIN_UNCOVERED_TURNS` uncovered turns, no pending own
         summary to repair ⇒ ``noop``/``already_compacted`` — the real "force at
         most once per era";
       * FORCED path with ≥ :data:`MAX_FORCED_ATTEMPTS` own ``compaction_failed``
         rows ⇒ ``noop``/``attempt_limit``; the button path still runs;
    4. **call the service**, then normalize its verdict onto the job;
    5. **release** the claim in ``finally`` — failure included.

    Every step is restart-safe: a crash loses only the in-RAM job record, and
    the next run re-derives the same answers from the log.
    """
    session_id = job.session_id
    acquired = False
    try:
        acquired, reason = await acquire_claim(claims, session_id, slot)
        if not acquired:
            job.status = STATUS_NOOP
            job.stage = reason
            job.reason = (
                NOOP_MESSAGES[NOOP_ALREADY_COMPACTED]
                if reason == "claimed"
                else "a turn kept the session; the next turn retries compaction"
            )
            return job

        # --- inside the claim: re-derive everything from the log -------------
        # THE ALREADY-COMPACTED TEST is SUCCESSOR PRESENCE, never own-marker
        # presence: every era child owns the marker of the compaction that made
        # it, and using that as the predicate made era 2 and later
        # un-compactable (live gate, session d7de2e69). The predicate is
        # ``idempotent_successor`` — the SAME call ``POST /compact`` makes for
        # its idempotent 200 answer, so the door and the runner cannot drift.
        successor = idempotent_successor(log, session_id)
        if successor is not None:
            # This session is SUPERSEDED: an era child exists (a concurrent run
            # forked it, or it was compacted earlier). Report the successor and
            # never fork a second era — the stage name is the one the client
            # already reconciles with the door's 200 (``already_compacted``).
            job.status = STATUS_NOOP
            job.stage = NOOP_ALREADY_COMPACTED
            job.reason = NOOP_MESSAGES[NOOP_ALREADY_COMPACTED]
            job.successor_session_id = successor
            job.title = session_title(log, successor) or session_title(log, session_id)
            return job

        # What the service would actually summarize (Lane A's helpers, chain
        # read). Both remaining guards key off it.
        uncovered = uncovered_turn_groups(log, session_id)
        # §2.3 step 10 repairs a crash ("I wrote a summary but the fork / marker
        # died") — a fork from an OWN summary, no model call. It must NEVER fire
        # from an INHERITED recap: a fresh era whose inherited summary covers
        # everything already lives in that era, and forking again would duplicate
        # it.
        cover = covering_summary_owner(log, session_id) if not uncovered else None
        if cover == "inherited":
            job.status = STATUS_NOOP
            job.stage = NOOP_NOTHING_UNCOVERED
            job.reason = NOOP_INHERITED_COVER
            return job
        # ``cover == "own"`` ⇒ a pending crash repair: fall through so the
        # service forks it. ``None`` ⇒ no summary at all; the service answers with
        # its own (more precise) noop reason, so falling through is safe.

        if (
            job.trigger == FORCED_TRIGGER
            and has_era_marker(log, session_id)
            and len(uncovered) < FRESH_ERA_MIN_UNCOVERED_TURNS
            and cover != "own"  # a pending crash repair is never blocked
        ):
            # §2.3 step 0, the REAL "force at most once per era": the forced path
            # already implies ratio ≥ 0.9, and a fresh era with fewer than two
            # uncovered turns cannot free meaningful window — noop + no force
            # (the hook turns this stage into the ``noop:true`` notice). Two or
            # more uncovered turns is substantial history, so era N→N+1 stays
            # legitimate.
            job.status = STATUS_NOOP
            job.stage = NOOP_ALREADY_COMPACTED
            job.reason = NOOP_MESSAGES[NOOP_ALREADY_COMPACTED]
            return job

        if job.trigger == FORCED_TRIGGER:
            attempts = failed_attempts(log, session_id)
            if attempts >= MAX_FORCED_ATTEMPTS:
                job.status = STATUS_NOOP
                job.stage = NOOP_ATTEMPT_LIMIT
                job.reason = NOOP_MESSAGES[NOOP_ATTEMPT_LIMIT]
                return job

        # --- the service (Lane A): summary + fork + marker + title ----------
        job.stage = "compact"
        # §2.3 step 6: install the session + interaction contextvars BEFORE the
        # model call so the summarizer's ``system``/``usage`` rows land on the
        # session being compacted and are labeled ``summarizer`` (the precedent
        # is api/attachments.py and api/assistants_api.py: a call made outside
        # the turn pump has no session context of its own). The tokens are
        # reset after the call so the surrounding task's context is unchanged.
        from fabulist.agents.debug import CURRENT_INTERACTION, CURRENT_SESSION

        session_token = CURRENT_SESSION.set(session_id)
        interaction_token = CURRENT_INTERACTION.set("summarizer")
        try:
            raw = await invoke_compact_service(
                service, session_id, log, trigger=job.trigger, job=job
            )
        except Exception as exc:  # noqa: BLE001 - a job failure is a row, not a crash
            logger.exception("compact job failed for %s", session_id)
            job.status = STATUS_FAILED
            job.stage = job.stage or "compact"
            job.error = str(exc)[:_FAILURE_ERROR_LIMIT]
            _append_compaction_failure(
                log, session_id, stage=job.stage, error=job.error
            )
            return job
        finally:
            CURRENT_INTERACTION.reset(interaction_token)
            CURRENT_SESSION.reset(session_token)

        outcome = _normalize_outcome(raw, job=job, log=log)
        job.status = outcome.status
        job.stage = outcome.stage or None
        job.error = outcome.error
        job.reason = outcome.reason
        if outcome.chunks:
            job.chunks.extend(outcome.chunks)
        if outcome.status == STATUS_DONE:
            job.successor_session_id = outcome.successor_session_id
            job.title = (
                outcome.title
                or (
                    session_title(log, outcome.successor_session_id)
                    if outcome.successor_session_id
                    else None
                )
                or session_title(log, session_id)
            )
            job.stage = None
            if job.successor_session_id is None:
                job.status = STATUS_FAILED
                job.stage = "write"
                job.error = "compaction reported done but left no successor session"
        elif outcome.status == STATUS_FAILED:
            job.error = job.error or "compaction failed"
        return job
    finally:
        if acquired:
            claim_release(claims, session_id)


# --- the post-turn hook (§2.1) ---------------------------------------------


def _append_monitor_failure(log: Any, session_id: str, exc: BaseException) -> None:
    """File the monitor's honest failure row: ``post_turn_failed`` naming the hook.

    Never ``compaction_failed`` (§2.9): a crash in the GAUGE is not a compaction
    that produced no summary, and mislabelling it would hide the real cause in
    the inspector. Best-effort.
    """
    try:
        log.append_event(
            session_id,
            "system",
            "system",
            {
                "event": POST_TURN_FAILED_EVENT,
                "stage": MONITOR_HOOK_NAME,
                # The gate's STAGE_FAILURE_SCHEMA names the culprit in `hook`;
                # `stage` carries it too so either read finds the monitor.
                "hook": MONITOR_HOOK_NAME,
                "error": str(exc)[:_FAILURE_ERROR_LIMIT],
            },
        )
    except Exception:  # noqa: BLE001 - failure reporting is best-effort
        logger.exception("could not record context-monitor failure for %s", session_id)


async def _monitor_turn(
    session_id: str,
    *,
    log: Any,
    registry: Any,
    service: Any,
    jobs: ContextJobs,
    claims: Any,
    slot: Any,
    bindings: Mapping[str, Any] | None,
) -> None:
    """The gauge's per-turn body: measure, notify, and (at 90%) force."""
    events = log.get_events(session_id)
    reading = measure_context(
        log, registry, session_id, bindings=bindings, events=events
    )
    if reading is None:
        # Undeclared window (⚑C) or no own narrate row yet (⚑R1a): stay silent.
        return

    if reading.over_budget or reading.budget_tokens <= 0:
        # A declaration problem compaction cannot fix ⇒ notify, never force.
        emit_context_low(log, session_id, reading, variant=VARIANT_OVER_BUDGET)
        return

    if reading.ratio < CONTEXT_LOW_RATIO:
        return

    emit_context_low(log, session_id, reading, variant=VARIANT_PLAIN)
    if reading.ratio < CONTEXT_FORCE_RATIO:
        return
    if service is None:
        logger.info(
            "context at %.2f for %s but no compact service is wired; not forcing",
            reading.ratio,
            session_id,
        )
        return

    job = jobs.create(session_id, FORCED_TRIGGER)
    if job is None:
        # A job already runs for this session (the claim/recheck will sort the
        # fork out); this turn has nothing else to do.
        return
    job.task = asyncio.create_task(
        run_compact_job(job, log=log, service=service, claims=claims, slot=slot)
    )
    try:
        await job.task
    except Exception:  # noqa: BLE001 - the runner reports, it does not raise
        logger.exception("forced compaction task failed for %s", session_id)
    if job.status == STATUS_NOOP and job.stage == NOOP_ALREADY_COMPACTED:
        # A completed compaction still reads ≥0.9 — or a fresh era with nothing
        # meaningful uncovered — say so ONCE per era and stop forcing (§2.3 step
        # 0). Its own variant, so the plain 80% row does not suppress it.
        emit_context_low(log, session_id, reading, variant=VARIANT_NOOP)


def make_context_monitor_hook(
    *,
    log: Any,
    registry: Any = None,
    service: Any = None,
    jobs: ContextJobs | None = None,
    claims: Any = None,
    slot: Any = None,
    bindings: Mapping[str, Any] | None = None,
) -> Callable[[str], Awaitable[None]]:
    """Build the fifth post-turn hook: ``async def hook(session_id: str) -> None``.

    That is the EXACT callable shape ``api/app.py`` schedules
    (``_schedule_post_turn`` -> ``hook(session.session_id)`` in its own task),
    so B registers it beside compaction/memory/assistant/historian with no
    signature change::

        hook = make_context_monitor_hook(
            log=log,
            registry=registry,
            service=app.state.compact_service,
            jobs=app.state.context_jobs,
            claims=app.state.compaction_claims,
            slot=app.state.turn_slot,        # optional PollingTurnSlot
        )

    Parameters, all optional except ``log``:

    * ``registry`` — the ``Registry`` the narrate slot resolves through (None ⇒
      nothing resolves, no math, no rows);
    * ``service`` — Lane A's compact service (None ⇒ notify at 80%, never force);
    * ``jobs`` — the SAME ``app.state.context_jobs`` the router serves (a private
      registry would keep the button's 409 from seeing a forced job, so pass it);
    * ``claims`` — the SAME ``app.state.compaction_claims`` the ``/turns`` guard
      reads (any ``set``-like membership container works);
    * ``slot`` — a turn-slot adapter (``busy(sid)`` / ``async wait_idle(sid)``);
    * ``bindings`` — inject preset bindings (tests); production passes nothing.

    The hook NEVER raises: a crash becomes its own ``post_turn_failed`` row
    naming the hook, so the turn and its siblings are unaffected (D82 isolation).
    """

    job_registry = jobs if jobs is not None else ContextJobs()

    async def context_monitor(session_id: str) -> None:
        try:
            await _monitor_turn(
                session_id,
                log=log,
                registry=registry,
                service=service,
                jobs=job_registry,
                claims=claims,
                slot=slot,
                bindings=bindings,
            )
        except asyncio.CancelledError:  # pragma: no cover - cancellation is not failure
            raise
        except Exception as exc:  # noqa: BLE001 - the hook must never break a turn
            logger.exception("context monitor failed for %s", session_id)
            _append_monitor_failure(log, session_id, exc)

    context_monitor.__name__ = MONITOR_HOOK_NAME
    context_monitor.__qualname__ = MONITOR_HOOK_NAME
    return context_monitor
