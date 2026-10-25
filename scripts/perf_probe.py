#!/usr/bin/env python
"""Stage 0 measurement harness — where a real turn's time and money go (W9.24 §3.1).

A standalone script in the ``scripts/feel_test.py`` / ``scripts/turn_trace.py``
pattern: it builds its OWN engine in-process (``configure`` -> ``EventLog`` ->
``build_engine(..., agent_debug=True)``), drives real turns against the live
model, and reads the event log the engine ALREADY writes. No service
dependence, no engine change, no new instrumentation.

What the per-turn report answers
--------------------------------
* **per-stage wall-clock** — the ``stage`` frames the engine streams at every
  in-graph step boundary (D42), timestamped on arrival and paired
  ``start``/``done``. The plan's known gap is closed by an OVERLAY rather than
  by editing ``scripts/turn_trace.py`` (which stays untouched): ``plan`` and
  ``lanes`` ARE model steps (W9.13/W9.17 postdate ``turn_trace._MODEL_STEPS``),
  and the ``planner``/``npc`` calls are assigned to those stages from their
  usage rows. A stage that emits no frame at all (the decision gate is the live
  case: ``systemone`` calls write usage rows, never a stage) still appears, in
  its own "no stage frame" section, approximated from the calls' own latencies.
* **per-call tokens + model** — the ``usage`` rows ``RegistryModelCaller``
  persists (one per model call) are the authoritative accounting. Every row is
  counted, including the ones with NO ``agent_call`` capture (the decision
  model's ``systemone`` calls): dropping them, as a capture-driven fold does,
  hides real calls and real prompt tokens.
* **prompt-cache signal, derived never invented** — the provider reports no
  cache counters (a usage row carries prompt/completion tokens only), so the
  report shows the derivable signals: the leading messages each call shares
  with the previous call on the same model (llama.cpp reuses the KV cache up to
  the longest identical prefix, and the AGENTS-6 stable prefix is exactly what
  should be shared), each call's ms per 1k prompt tokens, and — across turns —
  the narrate call's prefix overlap with the previous turn's narrate call.
* **post-turn hooks, approximated and labelled** — read from the log rows that
  appear AFTER the turn's barrier: ``historian_run`` spans (exact
  ``duration_ms``), ``summary``/``asset_proposal`` marks, and the post-turn
  usage rows (compaction / assistant consult / historian lanes). Placement
  relative to the barrier is approximated from the rows' ``ts`` (rows are
  stamped at append time) while each call's own latency is the transport's
  exact measure. An in-process run observes NONE: the D82 hooks are scheduled by
  the API layer's ``_drive_turn``, not by ``engine.run_turn`` — the report says
  so instead of fabricating a number.

The two Stage 0 experiments (``--mode``)
---------------------------------------
The per-turn report answers *where the wall-clock went*. §3.1 asks two more
questions, and each has its own mode because each has its own measurement:

* **contention (``--mode concurrent``)** — fire the SAME reference call set
twice: once one call after another, once with every call in flight at the same
instant (plus, when a second server is configured, one call against that server
to show cross-server parallelism). The report states the serial-sum wall against
the concurrent wall: a contention factor that says whether the model server runs
the calls side-by-side or queues them.
* **prompt cache (``--mode cache``)** — two successive calls over a byte-identical
~3k-token prefix, reporting first vs second latency and TTFT. The primary signal
is the provider's own cache accounting (``prompt_tokens_details.cached_tokens``,
which llama.cpp reports); the derived leading-message overlap stays as a
labelled secondary. This is the loop runtime's de-risk measurement: iteration 2
is supposed to ride the prefix the first call already prefilled.

Both are answered in PROSE at the top of ``report.md`` (the CONTENTION and
PROMPT CACHE sections) with the tables behind them — ``--mode all`` runs the
turns and both experiments into one report.

Output: a readable block per turn on stdout, plus ``<out>/<timestamp>/
turn-NN.json`` per turn and ``report.md`` for the run (the environment is
recorded with the numbers: §9 R9 — these are deployment-specific, re-runnable,
not a benchmark).

Invocations
-----------
::

    # Two scripted in-character beats (the feel_test path), default config
    uv run python scripts/perf_probe.py

    # An ISOLATED run: its own db, its own /tmp config whose narrator is the
    # local your-model-id connection (never the user service, never data/fabulist.db)
    uv run python scripts/perf_probe.py \\
        --config /tmp/perf_probe_connections.json \\
        --log /tmp/perf_probe.db --out /tmp/perf-probe --turns 2

    # All three scripted beats (the third is OOC: no graph, no stage frames)
    uv run python scripts/perf_probe.py --turns 3

    # The contention experiment: 3 reference calls serially, then all at once
    uv run python scripts/perf_probe.py --mode concurrent \\
        --config /tmp/perf_probe_connections.json --out /tmp/perf-probe \\
        --cross-connection lfm25-350m

    # The prompt-cache probe, and everything in one report
    uv run python scripts/perf_probe.py --mode cache --out /tmp/perf-probe
    uv run python scripts/perf_probe.py --mode all --out /tmp/perf-probe
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # scripts/ is a namespace package, not installed
    sys.path.insert(0, str(_ROOT))

from scripts import turn_trace as tt  # noqa: E402  (the shared pure core, unmodified)
from fabulist.connections.client import (  # noqa: E402
    KEYLESS_API_KEY,
    configure,
    merge_extra_body,
    merge_sampling,
)
from fabulist.log import EventLog  # noqa: E402
from fabulist.log.schema_gate import HISTORIAN_RUN_EVENT  # noqa: E402
from fabulist.wiring import build_engine  # noqa: E402

__all__ = [
    "AGENT_STAGE_FALLBACK",
    "CACHE_PREFIX_TOKENS",
    "CALL_MODES",
    "HOOK_INTERACTION",
    "INTERACTION_STAGE",
    "MODEL_STEPS",
    "REFERENCE_MAX_TOKENS",
    "SYNTH_LABELS",
    "CallResult",
    "CallRow",
    "CallSpec",
    "StageRow",
    "assign_stage_calls",
    "build_cache_report",
    "build_contention_report",
    "build_turn_report",
    "cache_answer",
    "call_result_dict",
    "collect_calls",
    "contention_answer",
    "fold_post_turn_hooks",
    "main",
    "parse_args",
    "prefix_signal",
    "reference_messages",
    "render_cache_markdown",
    "render_answer_sections",
    "render_contention_markdown",
    "render_run_markdown",
    "render_turn_markdown",
    "resolve_cross_connection",
    "run_arm",
    "run_one_turn",
    "server_facts",
    "shape_warmup_messages",
    "stage_rows",
    "timed_completion",
]

#: Steps that are inherently MODEL calls. ``turn_trace._MODEL_STEPS`` predates
#: W9.13/W9.17 (it has neither ``plan`` nor ``lanes``), which is the plan's
#: named gap; the probe overlays them here instead of editing turn_trace.
MODEL_STEPS = frozenset(tt._MODEL_STEPS) | {"plan", "lanes"}

#: Interaction label -> the stage step it belongs to. This is the second half of
#: the overlay: ``turn_trace._INTERACTION_STEP`` has no ``planner``/``npc`` row,
#: so those calls used to land as "call had no matching stage frame".
INTERACTION_STAGE: dict[str, str] = {
    "narrate": "narrate",
    "narrate_regen": "regen",
    "critic": "critic",
    "bts": "world_state",
    "planner": "plan",
    "npc": "lanes",
    "extract": "extract",
    "ooc": "ooc",
}

#: A capture-less usage row names its registry agent, not an interaction. Only
#: ``decision`` is unambiguous (the typed ``systemone`` calls write a usage row
#: and no ``agent_call`` capture); anything else stays unattributed rather than
#: guessed.
AGENT_STAGE_FALLBACK: dict[str, str] = {"decision": "decision_gate"}

#: Post-turn (D82) hooks: the interaction whose call belongs to which hook. Only
#: used to NAME a call that landed after the barrier — placement is by seq.
HOOK_INTERACTION: dict[str, str] = {
    "summarize": "compaction",
    "assistant": "assistant consult",
    "proposals": "historian lanes",
}

#: Display labels for stages that emit no ``stage`` frame (no repo label exists).
SYNTH_LABELS: dict[str, str] = {
    "decision_gate": "Decision gate",
    "extract": "Extracting facts",
    "ooc": "OOC reply",
}

#: Why the "after the barrier" numbers are approximate — printed with them, in
#: the spirit of the plan's "never faked".
HOOK_NOTE = (
    "approximate by construction: rows are stamped at append time, so a hook's "
    "start relative to the barrier is derived from log ts while each call's own "
    "latency is the transport's exact measure"
)

#: Why an in-process run's "after the barrier" section is empty.
NO_HOOK_REASON = (
    "no rows after the barrier in this turn's window — the probe drives "
    "engine.run_turn in-process, and the D82 hooks are scheduled by the API "
    "layer (_drive_turn), not by the engine; a log written by :2727 shows them here"
)


# ---------------------------------------------------------------------------
# records — the fabricated-or-real inputs the report assembles from
# ---------------------------------------------------------------------------


@dataclass
class CallRow:
    """One persisted ``usage`` row (the per-call accounting) + its capture.

    ``interaction`` is the capture's label (``narrate``/``planner``/``npc``/
    ``critic``/``bts`` …) and is empty for a call that wrote no capture;
    ``agent`` is the resolved registry agent the usage row names.
    """

    order: int
    interaction: str
    agent: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_s: float
    seq: int
    finish: str | None = None
    captured: bool = False
    messages: list[dict] = field(default_factory=list)
    stage: str = ""
    prefix: dict | None = None
    note: str = ""

    @property
    def prompt_share(self) -> float:
        """Prompt tokens as a share of the call's tokens (the cache lever)."""
        if not self.total_tokens:
            return 0.0
        return round(100.0 * self.prompt_tokens / self.total_tokens, 1)

    @property
    def ms_per_1k_prompt(self) -> float:
        """Latency per 1k prompt tokens — the number a cache hit moves."""
        if not self.prompt_tokens:
            return 0.0
        return round(1000.0 * self.latency_s / self.prompt_tokens, 1)


@dataclass
class StageRow:
    """One ordered turn stage: a paired ``stage`` frame, or a synthesized row.

    ``measured_by`` says which it is — ``"stage frame"`` (real wall-clock
    boundaries) or ``"call latency (no stage frame)"`` (approximated from the
    calls themselves, because that path emits no frame).
    """

    step: str
    label: str
    kind: str  # "model" | "deterministic"
    start_s: float
    end_s: float
    duration_s: float
    index: int
    total: int
    calls: list[CallRow] = field(default_factory=list)
    measured_by: str = "stage frame"
    note: str = ""


# ---------------------------------------------------------------------------
# log folding (pure — the unit-tested core)
# ---------------------------------------------------------------------------


def collect_calls(events: list[Any]) -> list[CallRow]:
    """One :class:`CallRow` per ``usage`` row, with its capture attached.

    Usage rows are the authoritative accounting: ``RegistryModelCaller`` writes
    exactly one per model call, including the calls that write no ``agent_call``
    capture (the decision model's ``systemone`` calls). The pairing rule is
    ``turn_trace.collect_calls``': a capture's own usage row is the nearest
    unpaired ``usage`` row BEFORE it, because the caller appends the usage row
    and then the DebugCapture sink appends the capture.
    """
    from fabulist.agents.debug import AGENT_CALL_EVENT
    from fabulist.usage.track import USAGE_EVENT

    usage_rows: list[Any] = []
    captures: list[Any] = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type != "system":
            continue
        kind = payload.get("event")
        if kind == USAGE_EVENT:
            usage_rows.append(event)
        elif kind == AGENT_CALL_EVENT:
            captures.append(event)

    paired: dict[int, Any] = {}
    used: set[int] = set()
    for capture in captures:
        for usage in reversed(usage_rows):
            if usage.seq >= capture.seq or id(usage) in used:
                continue
            paired[id(usage)] = capture
            used.add(id(usage))
            break

    rows: list[CallRow] = []
    for order, usage in enumerate(usage_rows, 1):
        payload = usage.payload if isinstance(usage.payload, dict) else {}
        capture = paired.get(id(usage))
        capture_payload = (
            capture.payload
            if capture is not None and isinstance(capture.payload, dict)
            else {}
        )
        messages = capture_payload.get("messages")
        finish = capture_payload.get("finish")
        rows.append(
            CallRow(
                order=order,
                interaction=str(capture_payload.get("agent") or ""),
                agent=str(payload.get("agent") or ""),
                model=str(payload.get("model") or ""),
                prompt_tokens=int(payload.get("prompt_tokens") or 0),
                completion_tokens=int(payload.get("completion_tokens") or 0),
                total_tokens=int(payload.get("total_tokens") or 0),
                latency_s=float(payload.get("latency_s") or 0.0),
                seq=int(usage.seq),
                finish=finish if isinstance(finish, str) else None,
                captured=capture is not None,
                messages=(
                    [dict(m) for m in messages if isinstance(m, dict)]
                    if isinstance(messages, list)
                    else []
                ),
                note="" if capture is not None else "no agent_call capture",
            )
        )
    return rows


def _message_key(message: dict) -> str:
    """A stable, comparable signature for one message (role + content bytes)."""
    return json.dumps(message, sort_keys=True, default=str)


def prefix_signal(previous: list[dict], messages: list[dict]) -> dict:
    """How much of ``previous``'s message list leads ``messages``, byte-identical.

    The derivable cache signal: the provider reports no cache counters, but
    llama.cpp reuses the KV cache up to the longest identical prefix, so a call
    whose LEADING messages repeat (system prompt, cast block, snapshot — the
    AGENTS-6 stable prefix) skips prefill for exactly that span.
    """
    prev_keys = [_message_key(m) for m in previous]
    keys = [_message_key(m) for m in messages]
    shared = 0
    for before, after in zip(prev_keys, keys):
        if before != after:
            break
        shared += 1
    shared_chars = sum(len(k) for k in keys[:shared])
    total_chars = sum(len(k) for k in keys)
    return {
        "shared_messages": shared,
        "shared_chars": shared_chars,
        "total_chars": total_chars,
        "ratio": round(shared_chars / total_chars, 3) if total_chars else 0.0,
        "identical": bool(keys) and keys == prev_keys,
    }


def _attach_prefix_signals(calls: list[CallRow]) -> None:
    """Give each captured call its prefix overlap vs the previous call per model.

    Comparing a critic call to a narrate call would be noise, so the reference is
    the previous captured call on the SAME model: within a turn that pairs the
    fan-out lanes (one shared narrator prefix, N calls) and repeats of the same
    interaction; across turns the report prints the narrate-to-narrate overlap
    separately (the stable-prefix contract).
    """
    last_by_model: dict[str, CallRow] = {}
    for call in calls:
        if not call.messages:
            continue
        previous = last_by_model.get(call.model)
        if previous is not None and previous.messages:
            signal = prefix_signal(previous.messages, call.messages)
            signal["vs"] = f"{previous.interaction or previous.agent}#{previous.order}"
            call.prefix = signal
        last_by_model[call.model] = call


def stage_rows(frames: list[tt.StageFrame]) -> list[StageRow]:
    """Pair the turn's ``stage`` frames (turn_trace's shared core) into rows.

    ``turn_trace._steps_from_frames`` owns the pairing and its edge cases (a
    ``done`` with no open ``start``; a ``start`` that never closed). The probe
    then applies its own MODEL overlay, because the core's kind comes from the
    pre-W9.13 ``_MODEL_STEPS``.
    """
    rows: list[StageRow] = []
    for step in tt._steps_from_frames(frames):
        rows.append(
            StageRow(
                step=step.step,
                label=step.label or step.step,
                kind="model" if step.step in MODEL_STEPS else "deterministic",
                start_s=step.start_s,
                end_s=step.end_s,
                duration_s=step.duration_s,
                index=step.index,
                total=step.total,
                note=step.note,
            )
        )
    return rows


def _attach(stage: StageRow, call: CallRow) -> None:
    """Attach one call to its stage (a stage that owns a call is a model step)."""
    call.stage = stage.step
    stage.calls.append(call)
    stage.kind = "model"


def _synth(step: str, calls: list[CallRow], *, note: str = "") -> StageRow:
    """A stage row for a path that emits NO frame, sized by its calls' latencies."""
    duration = sum(call.latency_s for call in calls)
    for call in calls:
        call.stage = step
    return StageRow(
        step=step,
        label=SYNTH_LABELS.get(step, step),
        kind="model" if calls else "deterministic",
        start_s=0.0,
        end_s=duration,
        duration_s=duration,
        index=0,
        total=0,
        calls=list(calls),
        measured_by="call latency (no stage frame)",
        note=note
        or "no stage frame for this path; wall-clock approximated from the "
        "call's own latency",
    )


def assign_stage_calls(
    stages: list[StageRow], calls: list[CallRow]
) -> list[StageRow]:
    """Attach every call to the stage that produced it; synthesize the rest.

    The overlay half of the probe: ``planner`` -> ``plan``, ``npc`` -> ``lanes``
    (one stage, N calls — the fan-out shares one frame pair), a capture-less
    ``decision`` row -> a synthesized ``decision_gate`` stage, and anything
    unassignable gets its own row so no call is silently dropped. The narrate
    interaction is shared by ``narrate`` and the DSL re-synthesis, so extra
    ``narrate`` calls land on ``dsl`` (then ``regen``) exactly as turn_trace
    does for the same reason.
    """
    out = list(stages)
    groups: dict[str, list[CallRow]] = {}
    unattributed: list[CallRow] = []
    for call in calls:
        target = INTERACTION_STAGE.get(call.interaction) or AGENT_STAGE_FALLBACK.get(
            call.agent, ""
        )
        if not target:
            unattributed.append(call)
            continue
        groups.setdefault(target, []).append(call)

    narrate_calls = groups.pop("narrate", [])
    if narrate_calls:
        narrate_row = next((s for s in out if s.step == "narrate"), None)
        if narrate_row is None:
            out.append(_synth("narrate", narrate_calls[:1]))
        else:
            _attach(narrate_row, narrate_calls[0])
        for extra in narrate_calls[1:]:
            dsl_row = next((s for s in out if s.step == "dsl" and not s.calls), None)
            if dsl_row is not None:
                _attach(dsl_row, extra)
                continue
            regen_row = next((s for s in out if s.step == "regen" and not s.calls), None)
            if regen_row is not None:
                _attach(regen_row, extra)
                continue
            out.append(
                _synth(
                    "dsl",
                    [extra],
                    note="extra narrate-interaction call with no free stage slot",
                )
            )

    for step, group in groups.items():
        rows = [s for s in out if s.step == step]
        if not rows:
            out.append(_synth(step, group))
            continue
        for call, row in zip(group, rows):
            _attach(row, call)
        for call in group[len(rows) :]:
            # More calls than stage rows: the shared stage owns them all (the
            # lane fan-out is the live case — N lane calls, one frame pair).
            _attach(rows[-1], call)

    for call in unattributed:
        out.append(
            _synth(
                call.interaction or "call",
                [call],
                note="call had no matching stage frame (unknown interaction "
                f"{call.interaction!r}, agent {call.agent!r})",
            )
        )
    return out


def _ts_delta(start: str | None, end: str | None) -> float | None:
    """Seconds between two log timestamps, or ``None`` when unparseable."""
    if not start or not end:
        return None
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except ValueError:
        return None


def fold_post_turn_hooks(
    events: list[Any], *, barrier_seq: int, barrier_ts: str | None = None
) -> dict:
    """Post-``done`` hook cost, folded from the log window AFTER the barrier.

    ``barrier_seq`` is the last row the turn itself wrote (captured when the
    engine yields ``done``). Everything appended after it belongs to the D82
    hooks: compaction (``summary`` marks + the summarizer's usage row), the
    assistant consult (a usage row), the historian (``historian_run`` span rows
    carrying their own ``duration_ms``, plus its lanes' usage rows and the
    ``asset_proposal`` rows they file), and the memory hook (no model call).

    The span numbers are exact where the row carries them (``duration_ms``, the
    transport's ``latency_s``); the PLACEMENT relative to the barrier is
    approximated from ``ts`` — that is what :data:`HOOK_NOTE` labels.
    """
    from fabulist.usage.track import USAGE_EVENT

    after = [event for event in events if int(getattr(event, "seq", 0)) > barrier_seq]
    calls = collect_calls(after)
    for call in calls:
        call.stage = HOOK_INTERACTION.get(call.interaction, "")

    spans: list[dict] = []
    marks: list[dict] = []
    proposals = 0
    started: Any = None
    for event in after:
        payload = event.payload if isinstance(event.payload, dict) else {}
        kind = payload.get("event")
        if getattr(event, "type", "") == "system" and kind == HISTORIAN_RUN_EVENT:
            status = str(payload.get("status") or "")
            if status == "started":
                started = event
                continue
            if started is None:
                continue
            duration_ms = payload.get("duration_ms")
            duration_s = (
                float(duration_ms) / 1000.0
                if isinstance(duration_ms, (int, float))
                else _ts_delta(getattr(started, "ts", None), getattr(event, "ts", None))
            )
            spans.append(
                {
                    "hook": "historian",
                    "status": status,
                    "started_ts": getattr(started, "ts", None),
                    "finished_ts": getattr(event, "ts", None),
                    "duration_s": round(duration_s, 3) if duration_s is not None else None,
                    "offset_from_barrier_s": _round_or_none(
                        _ts_delta(barrier_ts, getattr(started, "ts", None))
                    ),
                    "filed": payload.get("filed"),
                    "gate": payload.get("gate"),
                    "failed_lanes": payload.get("failed_lanes") or [],
                }
            )
            started = None
            continue
        if getattr(event, "type", "") == "summary":
            marks.append({"event": "compaction_summary", "seq": event.seq,
                          "ts": getattr(event, "ts", None),
                          "offset_from_barrier_s": _round_or_none(
                              _ts_delta(barrier_ts, getattr(event, "ts", None))) })
            continue
        if kind == "asset_proposal":
            proposals += 1
        elif kind == USAGE_EVENT:
            continue
        elif kind == "agent_call":
            continue

    return {
        "observed": bool(after),
        "barrier_seq": barrier_seq,
        "rows_after_barrier": len(after),
        "calls": [_call_to_dict(call) for call in calls],
        "spans": spans,
        "marks": marks,
        "proposal_rows": proposals,
        "note": HOOK_NOTE,
        "reason": "" if after else NO_HOOK_REASON,
    }


def _round_or_none(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def _call_to_dict(call: CallRow) -> dict:
    """JSON-safe view of one call (messages stay out — they are large and the
    report only needs their cache signal)."""
    out: dict[str, Any] = {
        "order": call.order,
        "interaction": call.interaction,
        "agent": call.agent,
        "model": call.model,
        "seq": call.seq,
        "prompt_tokens": call.prompt_tokens,
        "completion_tokens": call.completion_tokens,
        "total_tokens": call.total_tokens,
        "latency_s": round(call.latency_s, 3),
        "finish": call.finish,
        "captured": call.captured,
        "stage": call.stage,
        "prompt_share_pct": call.prompt_share,
        "ms_per_1k_prompt": call.ms_per_1k_prompt,
    }
    if call.prefix is not None:
        out["prefix"] = call.prefix
    if call.note:
        out["note"] = call.note
    return out


# ---------------------------------------------------------------------------
# the per-turn report (pure — the unit-tested core)
# ---------------------------------------------------------------------------


def build_turn_report(
    *,
    session_id: str,
    turn: int,
    kind: str,
    user_content: str,
    started_at: str,
    total_s: float,
    frames: list[tt.StageFrame],
    calls: list[CallRow],
    events: list[Any],
    summary: dict,
    barrier_seq: int,
    barrier_ts: str | None = None,
    first_content_s: float | None = None,
    first_reasoning_s: float | None = None,
    prior_narrate: list[dict] | None = None,
    environment: dict | None = None,
) -> dict:
    """Assemble one turn's cost report from fabricated-or-real records.

    Pure and synchronous: every number is derived from the frames, the usage
    rows and the log events passed in. ``events`` is the turn's raw log window
    (the post-turn hook fold reads it past ``barrier_seq``) and ``summary`` is
    ``turn_trace.summarize_turn_events``'s impact fold of the same window. The
    narrate call's message list is too large for the artifact, so the caller
    keeps it (see :func:`run_one_turn`) for the next turn's prefix comparison.
    """
    notes: list[str] = []
    rows = assign_stage_calls(stage_rows(frames), calls)
    framed = sorted(
        (row for row in rows if row.measured_by == "stage frame"),
        key=lambda row: row.start_s,
    )
    approximated = sorted(
        (row for row in rows if row.measured_by != "stage frame"),
        key=lambda row: row.calls[0].order if row.calls else 0,
    )
    if not framed and calls:
        notes.append(
            "no stage frames observed (an OOC turn, or a pre-graph path); every "
            "row below is approximated from call latencies"
        )

    _attach_prefix_signals(calls)
    narrate_call = next((call for call in calls if call.interaction == "narrate"), None)
    turn_prefix = None
    if prior_narrate and narrate_call is not None and narrate_call.messages:
        turn_prefix = prefix_signal(prior_narrate, narrate_call.messages)
        turn_prefix["vs"] = f"turn {turn - 1} narrate call"

    covered = sum(row.duration_s for row in framed)
    totals = {
        "calls": len(calls),
        "prompt_tokens": sum(call.prompt_tokens for call in calls),
        "completion_tokens": sum(call.completion_tokens for call in calls),
        "total_tokens": sum(call.total_tokens for call in calls),
        "model_latency_s": round(sum(call.latency_s for call in calls), 3),
    }
    by_model: dict[str, dict[str, Any]] = {}
    for call in calls:
        bucket = by_model.setdefault(
            call.model or "?",
            {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "total_tokens": 0, "latency_s": 0.0},
        )
        bucket["calls"] += 1
        bucket["prompt_tokens"] += call.prompt_tokens
        bucket["completion_tokens"] += call.completion_tokens
        bucket["total_tokens"] += call.total_tokens
        bucket["latency_s"] = round(bucket["latency_s"] + call.latency_s, 3)

    headline = sorted(
        (
            {
                "step": row.step,
                "label": row.label,
                "kind": row.kind,
                "duration_s": round(row.duration_s, 3),
                "pct_of_turn": round(100.0 * row.duration_s / total_s, 1)
                if total_s > 0
                else 0.0,
                "measured_by": row.measured_by,
            }
            for row in [*framed, *approximated]
        ),
        key=lambda entry: entry["duration_s"],
        reverse=True,
    )

    report = {
        "session_id": session_id,
        "turn": turn,
        "kind": kind,
        "user_content": user_content,
        "started_at": started_at,
        "total_s": round(total_s, 3),
        "first_content_s": _round_or_none(first_content_s),
        "first_reasoning_s": _round_or_none(first_reasoning_s),
        "environment": environment or {},
        "stages": [_stage_to_dict(row) for row in framed],
        "approximated_stages": [_stage_to_dict(row) for row in approximated],
        "headline": headline,
        "calls": [_call_to_dict(call) for call in calls],
        "tokens": {**totals, "by_model": by_model},
        "coverage": {
            "measured_stage_s": round(covered, 3),
            "unaccounted_s": round(max(0.0, total_s - covered), 3),
            "note": "unaccounted = wall-clock outside any stage frame (turn "
            "setup/teardown and paths that emit no frame)",
        },
        "cache": {
            "turn_prefix": turn_prefix,
            "calls": [
                {"order": call.order,
                 "interaction": call.interaction or call.agent,
                 "model": call.model,
                 "prompt_tokens": call.prompt_tokens,
                 "ms_per_1k_prompt": call.ms_per_1k_prompt,
                 **(call.prefix or {"prefix": None})}
                for call in calls
            ],
            "note": "derived: the provider reports no cache counters, so the "
            "signal is leading-message overlap (what a prefix cache can reuse) "
            "plus ms per 1k prompt tokens (what a hit moves)",
        },
        "after_barrier": fold_post_turn_hooks(
            events, barrier_seq=barrier_seq, barrier_ts=barrier_ts
        ),
        "events": summary,
        "notes": notes,
    }
    return report


def _stage_to_dict(row: StageRow) -> dict:
    out: dict[str, Any] = {
        "step": row.step,
        "label": row.label,
        "kind": row.kind,
        "start_s": round(row.start_s, 3),
        "end_s": round(row.end_s, 3),
        "duration_s": round(row.duration_s, 3),
        "index": row.index,
        "total": row.total,
        "measured_by": row.measured_by,
        "calls": [_call_to_dict(call) for call in row.calls],
    }
    if row.note:
        out["note"] = row.note
    return out


# ---------------------------------------------------------------------------
# the contention experiment + the prompt-cache probe (§3.1's other two questions)
# ---------------------------------------------------------------------------

#: What ``--mode`` drives: the scripted turns (today's behaviour), the contention
#: experiment, the prompt-cache probe, or all of them into one report.
CALL_MODES = ("serial", "concurrent", "cache", "all")

#: The reference call set: one fixed instruction and one fixed filler passage, so
#: every arm sends the SAME messages and the only difference is scheduling.
REFERENCE_INSTRUCTION = (
    "Read the passage and answer in short bullet points: which objects in it "
    "are made of metal?"
)
REFERENCE_LINE = (
    "The lanterns along the harbour wall gutter and go out one by one while the "
    "ferry crew coils rope on the wet stones. "
)

#: The prompt-cache probe's warm-up filler: the same SIZE, different words — llama.cpp
#: reuses cached KV chunks by matching text, so reusing the line would half-warm the
#: prefix the probe must measure cold.
REFERENCE_WARMUP_LINE = (
    "A brass compass ticks on the chart table while the tide pulls at the mooring "
    "chain and one gull worries the same three notes. "
)

#: A short generation budget: the experiments measure PREFILL and scheduling, and
#: a long completion would bury both under generation time.
REFERENCE_MAX_TOKENS = 96

#: The prompt-cache probe's prefix size, in tokens (4 chars/token is the estimate
#: the report prints; the server's own usage row carries the real count).
CACHE_PREFIX_TOKENS = 3000

#: Contention verdict bands over the contention factor (concurrent wall divided by
#: serial wall over the same calls): 1/n means fully parallel, 1.0 means queued.
CONTENTION_PARALLEL_MAX = 0.55
CONTENTION_SERIALIZED_MIN = 0.85

#: Cache verdict bands over second-call TTFT divided by first-call TTFT.
CACHE_HIT_TTFT_MAX = 0.5
CACHE_MISS_TTFT_MIN = 0.85


def reference_messages(*, prefix_tokens: int = 800,
                       line: str = REFERENCE_LINE, nonce: str = "") -> list[dict]:
    """The fixed reference prompt: one instruction plus ~``prefix_tokens`` tokens.

    Built by repeating one line, so both experiments build their prompt from the
    same text — the contention calls use a short one, the prompt-cache probe a
    long one — and the report can state the size it asked for. ``line`` is the
    same-size substitution the prompt-cache warm-up uses; ``nonce`` stamps every
    repetition, which makes a fresh prefix unmatchable against anything the server
    cached in an earlier run (llama.cpp reuses KV by matching text).
    """
    seed = f"[run {nonce}] " if nonce else ""
    unit = seed + line
    target_chars = max(len(unit), int(prefix_tokens) * 4)
    filler = (unit * (target_chars // len(unit) + 1))[:target_chars]
    return [
        {"role": "system", "content": REFERENCE_INSTRUCTION},
        {"role": "user", "content": filler},
    ]


@dataclass
class CallResult:
    """One timed streaming completion — the row both experiments report.

    ``started_s``/``ended_s`` are offsets from the arm's origin, which is what lets
    the report take the wall a SUBSET of calls spanned (the local-only contention
    factor) instead of only whole-arm totals. ``ttft_s`` is the first GENERATED
    token, reasoning or content — a thinking model emits reasoning first, and that
    token is the one prefill delays. ``cached_tokens`` is the provider's own cache
    accounting when it reports one (llama.cpp does), ``None`` when it does not, and
    ``server_prompt_ms``/``server_predicted_ms`` are the server's OWN split of the
    work (llama.cpp ``timings``): the difference between them and ``latency_s`` is
    queueing and transport, which no client-side stopwatch can attribute.
    """

    label: str
    server: str
    model: str
    latency_s: float = 0.0
    ttft_s: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int | None = None
    server_prompt_ms: float | None = None
    server_predicted_ms: float | None = None
    text_chars: int = 0
    started_s: float = 0.0
    ended_s: float = 0.0
    error: str = ""


def call_result_dict(result: CallResult) -> dict:
    """JSON-safe view of one timed call (no prompt text — it is tens of KB)."""
    return {
        "label": result.label,
        "server": result.server,
        "model": result.model,
        "latency_s": round(result.latency_s, 3),
        "ttft_s": _round_or_none(result.ttft_s),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "cached_tokens": result.cached_tokens,
        "server_prompt_ms": _round_or_none(result.server_prompt_ms, 1),
        "server_predicted_ms": _round_or_none(result.server_predicted_ms, 1),
        "text_chars": result.text_chars,
        "started_s": round(result.started_s, 3),
        "ended_s": round(result.ended_s, 3),
        "error": result.error,
    }


def _mean(values: Any) -> float | None:
    """Arithmetic mean of the iterable, or ``None`` when it is empty."""
    items = [value for value in values if value is not None]
    return (sum(items) / len(items)) if items else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """``numerator / denominator`` rounded for the report, ``None`` when undefined."""
    if not numerator or not denominator:
        return None
    return round(numerator / denominator, 2)


def _span(rows: list[dict]) -> float:
    """The wall a set of call rows spanned (last end minus first start)."""
    if not rows:
        return 0.0
    return round(
        max(row["ended_s"] for row in rows) - min(row["started_s"] for row in rows), 3
    )


def _span_end(rows: list[dict]) -> float:
    """When the last of a set of call rows finished, as an offset from the arm's origin."""
    return round(max((row["ended_s"] for row in rows), default=0.0), 3)


def _server_work_s(result: "CallResult") -> float | None:
    """The time the SERVER says it spent on the call (prefill + generation)."""
    if result.server_prompt_ms is None or result.server_predicted_ms is None:
        return None
    return round((result.server_prompt_ms + result.server_predicted_ms) / 1000.0, 3)


def _queue_s(result: "CallResult") -> float | None:
    """The call's wall-clock that the server's own split does not account for.

    Queueing, model load and transport all land here, and on a swapping server they
    dwarf the compute — which is exactly why the cache verdict is read off the
    server's prefill number and not off the client's stopwatch.
    """
    work = _server_work_s(result)
    if work is None:
        return None
    return round(max(0.0, result.latency_s - work), 3)


async def timed_completion(
    stream_fn: Any,
    messages: list[dict],
    *,
    label: str,
    server: str,
    model: str,
    max_tokens: int = REFERENCE_MAX_TOKENS,
    origin: float | None = None,
    clock: Any = time.monotonic,
) -> CallResult:
    """Drain one streaming completion, timing TTFT and total latency.

    ``stream_fn(messages, max_tokens=...)`` yields the caller protocol's events
    (``{"type": "content"|"reasoning"|"usage"|"error"}``) — the shape
    ``RegistryModelCaller`` emits, so a fake stands in for the live model in the
    unit pins. A failing call is RECORDED, never raised: a measurement run reports
    the failure and keeps going.
    """
    origin = clock() if origin is None else origin
    started = clock()
    result = CallResult(label=label, server=server, model=model)
    result.started_s = started - origin
    try:
        async for event in stream_fn(messages, max_tokens=max_tokens):
            kind = event.get("type")
            if kind in ("content", "reasoning"):
                if result.ttft_s is None:
                    result.ttft_s = clock() - started
                result.text_chars += len(str(event.get("delta") or ""))
            elif kind == "usage":
                result.prompt_tokens = int(event.get("prompt_tokens") or 0)
                result.completion_tokens = int(event.get("completion_tokens") or 0)
                cached = event.get("cached_tokens")
                result.cached_tokens = (
                    int(cached) if isinstance(cached, (int, float)) else None
                )
            elif kind == "timings":
                prompt_ms = event.get("prompt_ms")
                predicted_ms = event.get("predicted_ms")
                result.server_prompt_ms = (
                    float(prompt_ms) if isinstance(prompt_ms, (int, float)) else None
                )
                result.server_predicted_ms = (
                    float(predicted_ms)
                    if isinstance(predicted_ms, (int, float))
                    else None
                )
            elif kind == "error":
                result.error = str(event.get("delta") or "error")
    except Exception as exc:  # a live run reports; it does not die on one call
        result.error = f"{type(exc).__name__}: {exc}"
    ended = clock()
    result.ended_s = ended - origin
    result.latency_s = ended - started
    return result


@dataclass
class CallSpec:
    """One planned reference call: what to send, where, and how to stream it."""

    label: str
    server: str
    model: str
    messages: list[dict]
    stream_fn: Any


async def run_arm(plan: list[CallSpec], *, concurrent: bool) -> dict:
    """Run the call plan serially or all at once, returning the arm's totals.

    ``concurrent`` fires every call with ``asyncio.gather``: the same set, the same
    messages, only the scheduling differs — that is the whole experiment.
    """
    origin = time.monotonic()
    results: list[CallResult] = []
    if concurrent:
        results = list(
            await asyncio.gather(
                *(
                    timed_completion(
                        spec.stream_fn, spec.messages, label=spec.label,
                        server=spec.server, model=spec.model, origin=origin,
                    )
                    for spec in plan
                )
            )
        )
    else:
        for spec in plan:
            results.append(
                await timed_completion(
                    spec.stream_fn, spec.messages, label=spec.label,
                    server=spec.server, model=spec.model, origin=origin,
                )
            )
    return {
        "concurrent": concurrent,
        "wall_s": round(time.monotonic() - origin, 3),
        "sum_call_s": round(sum(result.latency_s for result in results), 3),
        "max_call_s": round(max((result.latency_s for result in results), default=0.0), 3),
        "mean_ttft_s": _round_or_none(_mean(result.ttft_s for result in results)),
        "calls": [call_result_dict(result) for result in results],
        "errors": sum(1 for result in results if result.error),
    }


def _contention_verdict(
    factor: float | None, *, parallel_slots: int | None = None
) -> str:
    """Classify the contention factor: parallel, partially parallel, or serialized.

    The slot count is a GUARD, not a tie-breaker: a server with one slot cannot run
    two calls at once, so a factor in the parallel band there means the server's
    state changed between the arms (a model load or swap charged to one arm) and
    the honest report is "re-run", never "parallel".
    """
    if factor is None:
        return "unknown (no completed calls to compare)"
    if isinstance(parallel_slots, int) and parallel_slots <= 1 and (
        factor <= CONTENTION_PARALLEL_MAX
    ):
        return "inconsistent (server state changed between the arms)"
    if factor <= CONTENTION_PARALLEL_MAX:
        return "parallel"
    if factor >= CONTENTION_SERIALIZED_MIN:
        return "serialized"
    return "partially parallel"


def build_contention_report(
    serial_arm: dict,
    concurrent_arm: dict,
    *,
    local_server: str,
    cross_server: str = "",
    environment: dict | None = None,
    server_facts: dict | None = None,
    warmup: list[dict] | None = None,
) -> dict:
    """Answer §3.1's second question: contended or serialized, in numbers.

    Pure: every figure comes from the two arms' rows, which carry their own start
    and end offsets — so the local server's factor is the wall the LOCAL calls
    spanned, not a whole-arm number polluted by the cross-server call.
    """
    rows: list[dict] = []
    for before, after in zip(serial_arm["calls"], concurrent_arm["calls"]):
        rows.append(
            {
                "label": before["label"],
                "server": before["server"],
                "model": before["model"],
                "serial_s": before["latency_s"],
                "concurrent_s": after["latency_s"],
                "inflation": _ratio(after["latency_s"], before["latency_s"]),
                "serial_ttft_s": before["ttft_s"],
                "concurrent_ttft_s": after["ttft_s"],
                "prompt_tokens": before["prompt_tokens"],
                "cached_tokens": before["cached_tokens"],
                "completion_tokens": before["completion_tokens"],
                "concurrent_start_s": after["started_s"],
                "concurrent_end_s": after["ended_s"],
                "errors": [
                    error for error in (before["error"], after["error"]) if error
                ],
            }
        )

    local_serial = [call for call in serial_arm["calls"] if call["server"] == local_server]
    local_concurrent = [
        call for call in concurrent_arm["calls"] if call["server"] == local_server
    ]
    local_serial_wall = _span(local_serial)
    local_concurrent_wall = _span(local_concurrent)
    factor = _ratio(concurrent_arm["wall_s"], serial_arm["wall_s"])
    local_factor = _ratio(local_concurrent_wall, local_serial_wall)

    report: dict[str, Any] = {
        "kind": "contention",
        "local_server": local_server,
        "cross_server": cross_server,
        "calls_per_server": len(local_serial),
        "calls_total": len(rows),
        "rows": rows,
        "serial": serial_arm,
        "concurrent": concurrent_arm,
        "factor": factor,
        "speedup": _ratio(serial_arm["wall_s"], concurrent_arm["wall_s"]),
        "local_factor": local_factor,
        "local_serial_wall_s": local_serial_wall,
        "local_concurrent_wall_s": local_concurrent_wall,
        "local_speedup": _ratio(local_serial_wall, local_concurrent_wall),
        "local_mean_serial_s": _round_or_none(_mean(call["latency_s"] for call in local_serial)),
        "local_mean_concurrent_s": _round_or_none(
            _mean(call["latency_s"] for call in local_concurrent)
        ),
        "local_inflation": _ratio(
            _mean(call["latency_s"] for call in local_concurrent),
            _mean(call["latency_s"] for call in local_serial),
        ),
        "ideal_factor": round(1.0 / max(1, len(local_serial)), 2),
        "local_max_end_s": _span_end(local_concurrent),
        "prefix_cached": {
            "serial": [call["cached_tokens"] for call in local_serial],
            "concurrent": [call["cached_tokens"] for call in local_concurrent],
            "prompt_tokens": next(
                (call["prompt_tokens"] for call in local_serial), 0
            ),
        },
        "environment": environment or {},
        "server_facts": server_facts or {},
        "warmup": warmup or [],
    }
    facts = (report["server_facts"] or {}).get("local") or {}
    report["verdict"] = _contention_verdict(
        local_factor if local_factor is not None else factor,
        parallel_slots=facts.get("parallel_slots"),
    )
    report["answer"] = contention_answer(report)
    return report


def contention_answer(report: dict) -> list[str]:
    """The contention question answered in sentences (§3.1's second mandate)."""
    if report["factor"] is None:
        return [
            "**Answer: not measured.** No reference call completed in both arms, so "
            "there is no serial-versus-concurrent comparison — see the errors in "
            "the call table below."
        ]

    facts = (report["server_facts"] or {}).get("local") or {}
    slots = facts.get("parallel_slots")
    slots_note = (
        f" (the server reports `--parallel {slots}`)" if isinstance(slots, int) else ""
    )
    sentences = [
        f"The same {report['calls_total']} reference calls over the same prompts: "
        f"**{report['serial']['wall_s']}s one after another versus "
        f"{report['concurrent']['wall_s']}s with every call in flight** — a "
        f"contention factor of **{report['factor']}×** (fully parallel would be "
        f"{report['ideal_factor']}×, no speed-up at all is 1.0×)."
    ]
    sentences.append(
        f"On `{report['local_server']}`{slots_note} the "
        f"{report['calls_per_server']} local calls spanned "
        f"{report['local_concurrent_wall_s']}s in flight against "
        f"{report['local_serial_wall_s']}s serially — a local contention factor of "
        f"{report['local_factor']}× — and each call's own latency moved from "
        f"{report['local_mean_serial_s']}s to {report['local_mean_concurrent_s']}s "
        f"({report['local_inflation']}× mean inflation under load)."
    )
    concurrent_ttfts = sorted(
        row["concurrent_ttft_s"] for row in report["rows"]
        if row["server"] == report["local_server"] and row["concurrent_ttft_s"]
    )
    serial_ttfts = sorted(
        row["serial_ttft_s"] for row in report["rows"]
        if row["server"] == report["local_server"] and row["serial_ttft_s"]
    )
    if concurrent_ttfts and serial_ttfts:
        sentences.append(
            "The queue is visible inside the calls themselves: in the concurrent arm "
            f"the local calls reached their first token at {concurrent_ttfts}s "
            f"(staggered), against {serial_ttfts}s in the serial arm (flat) — a call "
            "that waits for the slot shows the wait as TTFT, not as a shorter "
            "generation."
        )
    prefix = report.get("prefix_cached") or {}
    if any(isinstance(value, int) for value in (prefix.get("serial") or [])):
        sentences.append(
            f"The reference calls rode a warm prefix as a lane fan-out does "
            f"(the server counted {prefix['serial']} cached of "
            f"{prefix['prompt_tokens']} prompt tokens, serially), so what the arms "
            f"contend over is generation and queueing, not prefill."
        )
    if report["verdict"].startswith("inconsistent"):
        sentences.append(
            f"**Answer: not trustworthy — re-run this mode.** The server reports "
            f"{slots} slot(s), and one slot cannot run two calls at once, yet the "
            f"concurrent arm beat the serial arm ({report['factor']}×). That is a "
            f"server-state artefact, not parallelism: a model load or swap was "
            f"charged to one arm and not the other. Each arm is preceded by "
            f"identical warm-up calls, so re-run with the model already resident "
            f"before quoting any number here."
        )
    elif report["verdict"] == "serialized":
        sentences.append(
            "**Answer: the server is serialized, not contended-parallel.** Requests "
            "queue inside the model server: N calls in flight cost about the serial "
            "sum, and the waiting is paid INSIDE each call rather than hidden. "
            "Concurrency against this server buys queueing order, not throughput — "
            "the concurrent post-turn hooks (D82) and the multi-lane fan-out cannot "
            "finish faster than their serial sum, and the turn's per-stage "
            "wall-clock is additive by construction."
        )
    elif report["verdict"] == "parallel":
        sentences.append(
            "**Answer: the server ran the calls in parallel.** The concurrent wall "
            "is close to the slowest single call rather than the serial sum, so the "
            "multi-lane fan-out and the concurrent post-turn hooks (D82) DO buy "
            "wall-clock: N lanes cost roughly one lane's latency."
        )
    else:
        sentences.append(
            f"**Answer: partial parallelism** — the concurrent wall landed at "
            f"{report['factor']}× the serial sum, between the fully-parallel "
            f"{report['ideal_factor']}× and a fully queued 1.0×: some calls overlap "
            f"and some queue (a slot limit, or unequal call durations). Read any "
            f"fan-out saving off the local factor above, never off the call count."
        )

    if report["cross_server"]:
        cross = next(
            (row for row in report["rows"] if row["server"] == report["cross_server"]),
            None,
        )
        if cross is not None:
            sentences.append(
                f"The `{report['cross_server']}` call — a DIFFERENT server — "
                f"finished {cross['concurrent_end_s']}s into the concurrent window "
                f"(which ran {report['local_max_end_s']}s for the local calls) and "
                f"took {cross['concurrent_s']}s against {cross['serial_s']}s when run "
                f"serially: cross-server calls do overlap with the local ones, so a "
                f"second server is the only wall-clock parallelism this deployment "
                f"offers."
            )
    sentences.append(
        "These are this box's numbers (§9 R9): the slots, the GPU and the model "
        "decide them, so re-run the mode rather than quoting them."
    )
    return sentences


def _cache_verdict(report: dict) -> str:
    """Classify the prefix pair, best evidence first: server prefill, then client TTFT.

    The server's own prefill time (llama.cpp ``timings.prompt_ms``) is the signal that
    actually measures the cache — it is the work the cached prefix removes. The
    client's TTFT is only a proxy, and on a deployment that queues or reloads the
    model between requests it is dominated by time the model never sees (measured
    live: 18.3s of prefill inside an 80s call). So: prefill decides when the server
    reports it; a prefill-versus-TTFT disagreement is stated, not hidden.
    """
    cached = (report.get("cached_tokens") or {}).get("second")
    prompt_tokens = report["second"]["prompt_tokens"]
    prefill_ratio = report.get("prefill_ratio")
    ttft_ratio = report.get("ttft_ratio")
    if isinstance(cached, int) and prompt_tokens:
        reused = cached / prompt_tokens
        if reused < 0.5:
            return "cache miss"
        if prefill_ratio is not None:
            if prefill_ratio <= CACHE_HIT_TTFT_MAX:
                return "cache hit (server prefill)"
            return "cache hit without a prefill win"
        if ttft_ratio is not None and ttft_ratio > 1.0:
            return "inconsistent (server state changed between the calls)"
        if ttft_ratio is not None and ttft_ratio <= CACHE_HIT_TTFT_MAX:
            return "cache hit"
        return "cache hit without a TTFT win"
    if prefill_ratio is not None:
        if prefill_ratio <= CACHE_HIT_TTFT_MAX:
            return "cache hit (server prefill)"
        return "cache miss (server prefill)"
    if ttft_ratio is None:
        return "unknown (no timings to compare)"
    if ttft_ratio <= CACHE_HIT_TTFT_MAX:
        return "cache hit (timing only)"
    if ttft_ratio >= CACHE_MISS_TTFT_MIN:
        return "cache miss (timing only)"
    return "inconclusive"


def build_cache_report(
    first: CallResult,
    second: CallResult,
    *,
    messages: list[dict],
    prefix_tokens: int,
    max_tokens: int = REFERENCE_MAX_TOKENS,
    environment: dict | None = None,
    server_facts: dict | None = None,
    warmup: list[dict] | None = None,
) -> dict:
    """Answer the loop-runtime question: does a second call over the same prefix get cheaper?

    The primary signal is MEASURED — the provider's own ``cached_tokens`` — plus
    first-versus-second TTFT and latency. The derived leading-message overlap is
    carried alongside and labelled secondary: it describes the request, not what
    the server kept.
    """
    overlap = prefix_signal(messages, messages)
    report: dict[str, Any] = {
        "kind": "prompt_cache",
        "server": first.server,
        "model": first.model,
        "messages": len(messages),
        "prefix_chars": overlap["total_chars"],
        "prefix_tokens_est": int(prefix_tokens),
        "max_tokens": max_tokens,
        "first": call_result_dict(first),
        "second": call_result_dict(second),
        "same_prompt_tokens": bool(
            first.prompt_tokens and first.prompt_tokens == second.prompt_tokens
        ),
        "ttft_ratio": _ratio(second.ttft_s, first.ttft_s),
        "latency_ratio": _ratio(second.latency_s, first.latency_s),
        "ttft_saved_s": _round_or_none(
            (first.ttft_s - second.ttft_s)
            if first.ttft_s is not None and second.ttft_s is not None
            else None
        ),
        "server_prefill_saved_ms": _round_or_none(
            (first.server_prompt_ms - second.server_prompt_ms)
            if first.server_prompt_ms is not None and second.server_prompt_ms is not None
            else None,
            1,
        ),
        "prefill_ratio": _ratio(second.server_prompt_ms, first.server_prompt_ms),
        "server_work_s": {
            "first": _server_work_s(first),
            "second": _server_work_s(second),
        },
        "queue_s": {
            "first": _queue_s(first),
            "second": _queue_s(second),
        },
        "cached_tokens": {
            "first": first.cached_tokens,
            "second": second.cached_tokens,
        },
        "derived_overlap": {
            **overlap,
            "role": "secondary (derived from the request, not measured by the server)",
        },
        "environment": environment or {},
        "server_facts": server_facts or {},
        "warmup": warmup or [],
    }
    report["verdict"] = _cache_verdict(report)
    report["answer"] = cache_answer(report)
    return report


def cache_answer(report: dict) -> list[str]:
    """The prompt-cache question answered in sentences (primary measured, secondary derived)."""
    first, second = report["first"], report["second"]
    prompt_tokens = second["prompt_tokens"] or report["prefix_tokens_est"]
    saved = report["ttft_saved_s"]
    if saved is None:
        delta = "no TTFT comparison available"
    elif saved >= 0:
        delta = f"{saved}s saved"
    else:
        delta = f"{abs(saved)}s LONGER"
    sentences = [
        f"Two successive calls over a byte-identical prefix ({report['prefix_chars']} "
        f"chars, ≈{report['prefix_tokens_est']} tokens estimated, "
        f"{prompt_tokens} counted by the server): the first took "
        f"{first['latency_s']}s to its first token ({first['ttft_s']}s TTFT), the "
        f"second {second['latency_s']}s ({second['ttft_s']}s TTFT) — the second "
        f"call's first token took {report['ttft_ratio']}× the first call's time "
        f"({delta})."
    ]
    cached_first = report["cached_tokens"]["first"]
    cached_second = report["cached_tokens"]["second"]
    if isinstance(cached_second, int):
        share = round(100.0 * cached_second / prompt_tokens, 1) if prompt_tokens else 0.0
        sentences.append(
            f"The provider's own accounting (`prompt_tokens_details.cached_tokens`): "
            f"the first call reused {cached_first} of {prompt_tokens} prompt tokens, "
            f"the second {cached_second} ({share}%) — the measured cache signal, not "
            f"an inference from the request."
        )
    else:
        sentences.append(
            "This provider reports no cache accounting (`prompt_tokens_details` is "
            "absent), so the cache signal below is TIMING ONLY — TTFT is the "
            "measure, and it cannot separate a warm prefix from a fast box."
        )

    if report.get("server_prefill_saved_ms") is not None:
        sentences.append(
            f"The server's own split of the work agrees, and localises the saving: "
            f"prefill {first.get('server_prompt_ms')} ms → "
            f"{second.get('server_prompt_ms')} ms ({report['server_prefill_saved_ms']} "
            f"ms saved), generation {first.get('server_predicted_ms')} ms → "
            f"{second.get('server_predicted_ms')} ms. Whatever the two calls' "
            f"wall-clock does outside those two numbers is queueing and transport, "
            f"which is why this verdict rests on the server's counts rather than on "
            f"the client's stopwatch."
        )

    verdict = report["verdict"]
    if verdict.startswith("inconsistent"):
        sentences.append(
            f"**Answer: not trustworthy — re-run this mode.** The prefix WAS reused "
            f"({cached_second} of {prompt_tokens} tokens) yet the second call's "
            f"first token took {report['ttft_ratio']}× the first call's time, which "
            f"a warm prefix cannot cause: the server's state changed between the "
            f"two calls (a model reload or swap). The provider's cached-token count "
            f"is still the measured reuse signal; the timing comparison is void."
        )
    elif verdict == "cache hit (server prefill)":
        sentences.append(
            f"**Answer: iteration 2 rides the cached prefix.** The server's own "
            f"prefill time collapsed from {first.get('server_prompt_ms')} ms to "
            f"{second.get('server_prompt_ms')} ms ({report['prefill_ratio']}×) while "
            f"generation stayed at {second.get('server_predicted_ms')} ms — so a "
            f"loop iteration's incremental cost is the TAIL's prefill plus "
            f"generation plus the tool round trip, not a re-prefill of the stable "
            f"context. This pair is the loop's BEST case (byte-identical messages); "
            f"a real iteration appends a tail, so its saving is bounded below by "
            f"this."
        )
        queue = report.get("queue_s") or {}
        if (report.get("ttft_ratio") is not None
                and report["ttft_ratio"] > CACHE_HIT_TTFT_MAX
                and queue.get("second") is not None):
            sentences.append(
                f"The client's stopwatch does not show that saving on this box, and "
                f"the server's split says why: the second call spent "
                f"{queue['second']}s of its {second['latency_s']}s wall-clock "
                f"OUTSIDE the model — queueing, model load or transport — so its "
                f"TTFT ratio ({report['ttft_ratio']}×) measures the deployment's "
                f"queue, not the cache. That is why the verdict rests on the "
                f"server's prefill number."
            )
    elif verdict in ("cache hit", "cache hit (timing only)"):
        sentences.append(
            "**Answer: iteration 2 rides the cached prefix.** The second call's "
            "first token arrived in "
            f"{report['ttft_ratio']}× the first call's time over a prefix the server "
            "had already prefilled, so a loop iteration's cost is the tail's prefill "
            "plus generation plus the tool round trip — not a re-prefill of the "
            "stable context. This pair is the loop's BEST case (identical messages); "
            "a real iteration appends a tail, so its saving is bounded below by "
            "this."
        )
    elif verdict in ("cache miss", "cache miss (timing only)",
                     "cache miss (server prefill)"):
        sentences.append(
            "**Answer: no cache reuse observed** — the server counted almost none of "
            "the prefix as cached and the second call's TTFT did not improve, so "
            "treat a loop iteration as a FULL call: draft cost plus repairs, each "
            "paying its own prefill."
        )
    elif verdict == "cache hit without a prefill win":
        sentences.append(
            f"**Answer: the prefix WAS reused ({cached_second} of "
            f"{prompt_tokens} tokens) but the server's prefill only fell to "
            f"{report['prefill_ratio']}× the first call's "
            f"({first.get('server_prompt_ms')} ms → "
            f"{second.get('server_prompt_ms')} ms): reuse is happening, yet prefill "
            f"is not what this prompt's cost is made of — do not budget a loop "
            f"iteration as a pure tail call."
        )
    elif verdict == "cache hit without a TTFT win":
        sentences.append(
            f"**Answer: the prefix WAS reused ({cached_second} of "
            f"{prompt_tokens} tokens) but the second call's first token did not "
            f"arrive proportionally sooner ({report['ttft_ratio']}× the first "
            "call's time): the prefill saved is real but not the binding cost at "
            "this prefix size, so do not budget a loop iteration as a pure tail "
            "call."
        )
    else:
        sentences.append(
            f"**Answer: {verdict}** — the two calls cannot be ranked on this "
            "evidence; re-run the mode before budgeting loop iterations on a cache "
            "assumption."
        )

    overlap = report["derived_overlap"]
    sentences.append(
        f"Secondary, derived (not measured): the two message lists share "
        f"{overlap['shared_messages']} of {report['messages']} messages — "
        f"{overlap['ratio']} of their characters — which is the request-side view of "
        f"the same reuse. It is reported beside the provider's count, never instead "
        f"of it, because only the server knows what it actually kept."
    )
    return sentences



# ---------------------------------------------------------------------------
# rendering — markdown companions + the readable stdout block
# ---------------------------------------------------------------------------


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _fmt_tokens(tokens: dict) -> str:
    return (
        f"{tokens['prompt_tokens']}+{tokens['completion_tokens']}"
        f"={tokens['total_tokens']}"
    )


def _call_row_label(call: dict) -> str:
    """The interaction (or the registry agent when a call wrote no capture)."""
    return str(call.get("interaction") or call.get("agent") or "call")


def render_turn_markdown(turn: dict) -> str:
    """The human-readable per-turn companion (the JSON artifact holds the rest)."""
    lines: list[str] = []
    lines.append(f"# Turn {turn['turn']} — {turn['kind']}")
    lines.append("")
    lines.append(f"- **user**: {turn['user_content']}")
    lines.append(f"- **wall-clock**: {turn['total_s']}s"
                 + (f"  ·  **first content token**: {turn['first_content_s']}s"
                    if turn.get("first_content_s") is not None else "")
                 + (f"  ·  **first reasoning token**: {turn['first_reasoning_s']}s"
                    if turn.get("first_reasoning_s") is not None else ""))
    tokens = turn["tokens"]
    lines.append(f"- **{tokens['calls']} model calls**  ·  tokens {_fmt_tokens(tokens)}"
                 f"  ·  model latency {tokens['model_latency_s']}s")
    coverage = turn["coverage"]
    lines.append(f"- **stage coverage**: {coverage['measured_stage_s']}s measured, "
                 f"{coverage['unaccounted_s']}s unaccounted")
    if turn.get("environment"):
        env = turn["environment"]
        lines.append(f"- **environment**: narrator `{env.get('narrator_model')}` · "
                     f"config `{env.get('config')}` · log `{env.get('log')}` · "
                     f"{env.get('engine')}")
    lines.append("")

    lines.append("## Where the wall-clock went (stage frames, ranked)")
    lines.append("")
    lines.append("| # | step | kind | seconds | % of turn | measured by |")
    lines.append("|---|---|---|---:|---:|---|")
    for row in turn["headline"]:
        lines.append(f"| {row['step']} | {row['label']} | {row['kind']} | "
                     f"{row['duration_s']} | {row['pct_of_turn']}% | "
                     f"{row['measured_by']} |")
    lines.append("")

    lines.append("## Stage timeline")
    lines.append("")
    lines.append("| step | kind | start | end | seconds | calls |")
    lines.append("|---|---|---:|---:|---:|---|")
    for stage in turn["stages"]:
        calls = ", ".join(
            f"{_call_row_label(call)} #{call['order']} "
            f"({_fmt_tokens(call)}, {call['latency_s']}s)"
            for call in stage["calls"]
        ) or "—"
        lines.append(f"| `{stage['step']}` | {stage['kind']} | {stage['start_s']}s | "
                     f"{stage['end_s']}s | {stage['duration_s']}s | {calls} |")
    lines.append("")
    if turn["approximated_stages"]:
        lines.append("### No stage frame (approximated from call latency)")
        lines.append("")
        lines.append("| step | label | seconds | calls | note |")
        lines.append("|---|---|---:|---|---|")
        for stage in turn["approximated_stages"]:
            calls = ", ".join(
                f"{_call_row_label(call)} #{call['order']} "
                f"({_fmt_tokens(call)}, {call['latency_s']}s)"
                for call in stage["calls"]
            ) or "—"
            lines.append(f"| `{stage['step']}` | {stage['label']} | "
                         f"{stage['duration_s']}s | {calls} | {stage.get('note', '')} |")
        lines.append("")

    lines.append("## Model calls (usage rows, in call order)")
    lines.append("")
    lines.append("| # | interaction | agent | model | prompt | completion | "
                 "latency | ms/1k prompt | stage |")
    lines.append("|---:|---|---|---|---:|---:|---:|---:|---|")
    for call in turn["calls"]:
        lines.append(f"| {call['order']} | {_call_row_label(call)} | {call['agent']} | "
                     f"{call['model']} | {call['prompt_tokens']} | "
                     f"{call['completion_tokens']} | {call['latency_s']}s | "
                     f"{call['ms_per_1k_prompt']} | {call['stage'] or '—'} |")
    lines.append("")

    lines.append("## Prompt-cache signal (derived)")
    lines.append("")
    cache = turn["cache"]
    prefix = cache.get("turn_prefix")
    if prefix:
        lines.append(f"- **stable prefix across turns**: the narrate call shares "
                     f"{_plural(prefix['shared_messages'], 'leading message')} "
                     f"({prefix['ratio']} of the prompt chars) with the previous "
                     f"turn's narrate call — {prefix['vs']}")
    else:
        lines.append("- **stable prefix across turns**: not measurable on this turn "
                     "(first turn, or the narrate call wrote no capture)")
    for entry in cache["calls"]:
        if "shared_messages" in entry:
            lines.append(f"- `{entry['interaction']}` #{entry['order']} on "
                         f"`{entry['model']}`: "
                         f"{_plural(entry['shared_messages'], 'leading message')} "
                         f"shared with {entry.get('vs')} "
                         f"({entry.get('ratio')} of prompt chars), "
                         f"{entry['ms_per_1k_prompt']} ms/1k prompt tokens")
    lines.append("")
    lines.append(f"_{cache['note']}_")
    lines.append("")

    lines.append("## After the barrier (post-turn hooks, approximate)")
    lines.append("")
    hooks = turn["after_barrier"]
    if not hooks["observed"]:
        lines.append(f"- not observed: {hooks['reason']}")
    else:
        lines.append(f"- {hooks['rows_after_barrier']} rows after seq "
                     f"{hooks['barrier_seq']}, {len(hooks['calls'])} model calls, "
                     f"{hooks['proposal_rows']} proposal rows")
        for call in hooks["calls"]:
            lines.append(f"    - `{_call_row_label(call)}` on `{call['model']}`: "
                         f"{_fmt_tokens(call)}, {call['latency_s']}s"
                         + (f" ({call['stage']} hook)" if call.get("stage") else ""))
        for span in hooks["spans"]:
            lines.append(f"    - historian `{span['status']}`: {span['duration_s']}s "
                         f"(started {span['offset_from_barrier_s']}s after the "
                         f"barrier), filed {span['filed']}, failed lanes "
                         f"{span['failed_lanes']}")
        for mark in hooks["marks"]:
            lines.append(f"    - {mark['event']} at seq {mark['seq']} "
                         f"({mark['offset_from_barrier_s']}s after the barrier)")
    lines.append("")
    lines.append(f"_{hooks['note']}_")
    lines.append("")

    lines.append("## Turn events (impact)")
    lines.append("")
    events = turn["events"]
    narrative = events.get("narrative")
    if narrative:
        lines.append(f"- **narrative** seq={narrative['seq']} "
                     f"prose={narrative['prose_chars']} chars "
                     f"dsl={narrative['dsl_chars']} chars")
    for group in ("validation_flags", "critic_verdicts", "revisions", "errors"):
        rows = events.get(group) or []
        if rows:
            lines.append(f"- **{group}**: {json.dumps(rows)[:1500]}")
    for group in ("state_delta", "world_state", "env_hud", "attribution"):
        value = events.get(group)
        if value:
            lines.append(f"- **{group}**: {json.dumps(value)[:800]}")
    if turn.get("notes"):
        lines.append("")
        lines.append("## Notes")
        for note in turn["notes"]:
            lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _answer_lines(answer: list[str]) -> list[str]:
    """The prose answer as paragraphs (sentences, not bullets — it is a conclusion)."""
    lines: list[str] = []
    for sentence in answer:
        lines.append(sentence)
        lines.append("")
    return lines


def _server_facts_line(facts: dict) -> str:
    """One line of the server's own configuration, or the honest reason it is absent."""
    if not facts:
        return "- **server**: not probed"
    if not facts.get("reachable"):
        return f"- **server**: `/v1/models` not readable ({facts.get('note') or 'no detail'})"
    slots = facts.get("parallel_slots")
    return (
        f"- **server**: model `{facts.get('model') or '?'}` "
        f"{facts.get('loaded') or 'unknown state'} · `--parallel` "
        f"{slots if isinstance(slots, int) else 'not reported'}"
    )


def render_contention_section(report: dict) -> list[str]:
    """The CONTENTION answer: the prose verdict first, the call table behind it."""
    lines: list[str] = ["## CONTENTION — is the model server contended or serialized?", ""]
    lines += _answer_lines(report["answer"])
    lines.append(
        "| call | server | serial | concurrent | inflation | serial TTFT | "
        "concurrent TTFT | prompt tokens |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for row in report["rows"]:
        lines.append(
            f"| `{row['label']}` | `{row['server']}` | {row['serial_s']}s | "
            f"{row['concurrent_s']}s | {row['inflation']}× | "
            f"{row['serial_ttft_s']}s | {row['concurrent_ttft_s']}s | "
            f"{row['prompt_tokens']} |"
        )
    lines.append("")
    lines.append(
        f"- **arms**: serial `{report['serial']['wall_s']}s` wall "
        f"(`{report['serial']['sum_call_s']}s` of call time) · concurrent "
        f"`{report['concurrent']['wall_s']}s` wall "
        f"(`{report['concurrent']['sum_call_s']}s` of call time, max single call "
        f"`{report['concurrent']['max_call_s']}s`)"
    )
    lines.append(
        f"- **factor**: whole set {report['factor']}× (speed-up "
        f"{report['speedup']}×) · local server {report['local_factor']}× "
        f"(speed-up {report['local_speedup']}×) · verdict **{report['verdict']}**"
    )
    facts = report["server_facts"] or {}
    for name in ("local", "cross"):
        if name in facts:
            lines.append(_server_facts_line(facts[name]).replace("**server**", f"**{name} server**"))
    if report.get("cross_server"):
        lines.append(f"- **cross-server call**: connection `{report['cross_server']}` "
                     f"(a different process, hence a different queue)")
    else:
        lines.append("- **cross-server call**: not configured for this run")
    for warm in report.get("warmup") or []:
        lines.append(f"- **warm-up (excluded from the arms)**: `{warm['label']}` "
                     f"{warm['latency_s']}s, {warm['prompt_tokens']} prompt tokens"
                     + (f" — error: {warm['error']}" if warm.get("error") else ""))
    lines.append("")
    lines.append(
        "_Method: the same reference messages are sent twice over — once serially, "
        "once with every call fired at the same instant — so the two arms differ "
        "only by scheduling. The factor is `concurrent wall ÷ serial wall`: 1/n is "
        "fully parallel, 1.0 is fully queued. The warm-up calls are excluded: they "
        "absorb model load and first-touch cost. These arms warm the reference "
        "prompt itself, because a lane fan-out shares one narrator prefix and that "
        "is the case being measured._"
    )
    lines.append("")
    return lines


def render_contention_markdown(report: dict) -> str:
    """The standalone contention artifact."""
    lines = ["# perf_probe — contention experiment", ""]
    environment = report.get("environment") or {}
    lines.append(f"- **config**: `{environment.get('config')}`  ·  **mode**: "
                 f"`{environment.get('mode')}`  ·  **calls**: "
                 f"{report['calls_per_server']} on `{report['local_server']}`")
    lines.append("")
    lines += render_contention_section(report)
    return "\n".join(lines)


def render_cache_section(report: dict) -> list[str]:
    """The PROMPT CACHE answer: the prose verdict first, the pair table behind it."""
    lines: list[str] = [
        "## PROMPT CACHE — does a loop iteration ride the cached prefix?",
        "",
    ]
    lines += _answer_lines(report["answer"])
    lines.append("| call | latency | TTFT | server prefill | server generation | "
                 "prompt tokens | cached tokens |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for key in ("first", "second"):
        call = report[key]
        cached = call["cached_tokens"]
        prompt_ms = call.get("server_prompt_ms")
        predicted_ms = call.get("server_predicted_ms")
        lines.append(
            f"| `{call['label']}` | {call['latency_s']}s | {call['ttft_s']}s | "
            f"{prompt_ms if prompt_ms is not None else '—'} ms | "
            f"{predicted_ms if predicted_ms is not None else '—'} ms | "
            f"{call['prompt_tokens']} | "
            f"{cached if isinstance(cached, int) else 'not reported'} |"
        )
    lines.append("")
    lines.append(
        f"- **ratios**: TTFT {report['ttft_ratio']}× · latency "
        f"{report['latency_ratio']}× · verdict **{report['verdict']}**"
    )
    lines.append(
        f"- **prefix**: {report['prefix_chars']} chars in {report['messages']} "
        f"messages, ≈{report['prefix_tokens_est']} tokens estimated "
        f"(`--max-tokens {report['max_tokens']}`)"
    )
    if report.get("server_prefill_saved_ms") is not None:
        lines.append(
            f"- **server prefill**: {report['first'].get('server_prompt_ms')} ms → "
            f"{report['second'].get('server_prompt_ms')} ms "
            f"({report['server_prefill_saved_ms']} ms saved) while generation stayed "
            f"{report['second'].get('server_predicted_ms')} ms — the server's own "
            f"split of the work, so the saving is prefill, not noise"
        )
    for name, facts in (report.get("server_facts") or {}).items():
        lines.append(_server_facts_line(facts).replace("**server**", f"**{name} server**"))
    for warm in report.get("warmup") or []:
        lines.append(f"- **warm-up (excluded from the pair)**: `{warm['label']}` "
                     f"{warm['latency_s']}s, {warm['prompt_tokens']} prompt tokens"
                     + (f" — error: {warm['error']}" if warm.get("error") else ""))
    lines.append("")
    lines.append(
        "_Method: two successive calls with byte-identical messages, the second "
        "issued only after the first finished, so the server's prefix cache is warm "
        "exactly as it is for a loop iteration. TTFT is the first GENERATED token "
        "(reasoning or content). The provider's `cached_tokens` is the measured "
        "signal — it counts what the server kept, so it is immune to the residual "
        "first-generation warm-up (model load, draft-model/spec-decode init) that "
        "can still inflate call 1 despite the warm-up calls. The leading-message "
        "overlap is derived and labelled as such._"
    )
    lines.append("")
    return lines


def render_cache_markdown(report: dict) -> str:
    """The standalone prompt-cache artifact."""
    lines = ["# perf_probe — prompt-cache probe", ""]
    environment = report.get("environment") or {}
    lines.append(f"- **config**: `{environment.get('config')}`  ·  **mode**: "
                 f"`{environment.get('mode')}`  ·  **connection**: "
                 f"`{report['server']}` (`{report['model']}`)")
    lines.append("")
    lines += render_cache_section(report)
    return "\n".join(lines)


def render_answer_sections(run: dict) -> list[str]:
    """The two answers §3.1 asks for, in prose, ahead of every table."""
    lines: list[str] = []
    if run.get("contention"):
        lines += render_contention_section(run["contention"])
    if run.get("cache"):
        lines += render_cache_section(run["cache"])
    return lines


def render_run_markdown(run: dict) -> str:
    """The run summary: the answers first, then the tables behind them."""
    lines: list[str] = []
    lines.append("# perf_probe — Stage 0 cost report")
    lines.append("")
    environment = run["environment"]
    header = f"- **started**: {run['started_at']}  ·  **mode**: `{run.get('mode', 'serial')}`"
    if run.get("session_id"):
        header += f"  ·  **session**: `{run['session_id']}`"
    lines.append(header)
    lines.append(f"- **environment**: narrator `{environment['narrator_model']}` · "
                 f"config `{environment['config']}` · "
                 f"log `{environment['log']}` · "
                 f"{environment['engine']}")
    if run.get("turns"):
        lines.append(f"- **turns**: {len(run['turns'])}")
    lines.append("")
    lines += render_answer_sections(run)
    if not run.get("turns"):
        return "\n".join(lines)
    lines.append("## Cross-turn table")
    lines.append("")
    lines.append("| turn | kind | wall-clock | model calls | tokens (p+c) | top stage |")
    lines.append("|---:|---|---:|---:|---:|---|")
    for turn in run["turns"]:
        top = turn["headline"][0] if turn["headline"] else None
        lines.append(
            f"| {turn['turn']} | {turn['kind']} | {turn['total_s']}s | "
            f"{turn['tokens']['calls']} | {_fmt_tokens(turn['tokens'])} | "
            + (f"`{top['step']}` {top['duration_s']}s ({top['pct_of_turn']}%)"
               if top else "—")
            + " |"
        )
    lines.append("")
    if len(run["turns"]) > 1:
        lines.append("## Per-stage across turns")
        lines.append("")
        steps: list[str] = []
        for turn in run["turns"]:
            for stage in turn["stages"]:
                if stage["step"] not in steps:
                    steps.append(stage["step"])
        lines.append("| step | " + " | ".join(
            f"turn {turn['turn']} (s)" for turn in run["turns"]) + " |")
        lines.append("|---|" + "---:|" * len(run["turns"]))
        for step in steps:
            cells = []
            for turn in run["turns"]:
                stage = next((s for s in turn["stages"] if s["step"] == step), None)
                cells.append(f"{stage['duration_s']}" if stage else "—")
            lines.append(f"| `{step}` | " + " | ".join(cells) + " |")
        lines.append("")
    lines.append("## Per-model totals")
    lines.append("")
    lines.append("| model | calls | prompt | completion | total | latency |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    totals: dict[str, dict] = {}
    for turn in run["turns"]:
        for model, bucket in turn["tokens"]["by_model"].items():
            acc = totals.setdefault(
                model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                        "total_tokens": 0, "latency_s": 0.0})
            for key in ("calls", "prompt_tokens", "completion_tokens",
                        "total_tokens", "latency_s"):
                acc[key] += bucket[key]
    for model, bucket in totals.items():
        lines.append(f"| `{model}` | {bucket['calls']} | {bucket['prompt_tokens']} | "
                     f"{bucket['completion_tokens']} | {bucket['total_tokens']} | "
                     f"{round(bucket['latency_s'], 3)}s |")
    lines.append("")
    for turn in run["turns"]:
        lines.append("---")
        lines.append("")
        lines.append(render_turn_markdown(turn))
    return "\n".join(lines)


def _print_turn(turn: dict) -> None:
    """The readable stdout block for one turn (the artifact carries the detail)."""
    BOLD, DIM, CYAN, GREEN, RESET = tt.BOLD, tt.DIM, tt.CYAN, tt.GREEN, tt.RESET
    tokens = turn["tokens"]
    print(f"\n{BOLD}── turn {turn['turn']} [{turn['kind']}]  {turn['total_s']}s  ·  "
          f"{tokens['calls']} model calls  ·  {_fmt_tokens(tokens)} tokens{RESET}")
    print(f"{DIM}   stage frames (wall-clock){RESET}")
    for stage in turn["stages"]:
        share = (100.0 * stage["duration_s"] / turn["total_s"]) if turn["total_s"] else 0.0
        color = CYAN if stage["kind"] == "model" else DIM
        calls = ", ".join(f"{_call_row_label(call)}#{call['order']}" for call in stage["calls"])
        print(f"   {color}{stage['step']:<12} {stage['kind']:<13} "
              f"{stage['duration_s']:>8.2f}s {share:>5.1f}%  {calls}{RESET}")
    for stage in turn["approximated_stages"]:
        calls = ", ".join(f"{_call_row_label(call)}#{call['order']}" for call in stage["calls"])
        print(f"   {DIM}{stage['step']:<12} {'no frame':<13} "
              f"{stage['duration_s']:>8.2f}s {'·':>6}  {calls}{RESET}")
    coverage = turn["coverage"]
    print(f"{DIM}   coverage {coverage['measured_stage_s']}s of "
          f"{turn['total_s']}s ({coverage['unaccounted_s']}s unaccounted){RESET}")
    print(f"{DIM}   calls (usage rows, in order){RESET}")
    for call in turn["calls"]:
        prefix = call.get("prefix") or {}
        shared = (f" shared {_plural(prefix.get('shared_messages'), 'msg')} "
                  f"w/ {prefix.get('vs')}" if prefix else "")
        print(f"   {DIM}#{call['order']} {_call_row_label(call):<12} "
              f"{call['agent']:<10} {call['model']:<8} "
              f"{_fmt_tokens(call):>12} tok {call['latency_s']:>8.2f}s "
              f"{call['ms_per_1k_prompt']:>7.1f} ms/1k prompt{shared}{RESET}")
    turn_prefix = turn["cache"].get("turn_prefix")
    if turn_prefix:
        print(f"{DIM}   cache: narrate prefix shares "
              f"{_plural(turn_prefix['shared_messages'], 'message')} "
              f"({turn_prefix['ratio']} of chars) with the previous turn{RESET}")
    hooks = turn["after_barrier"]
    if hooks["observed"]:
        print(f"{CYAN}   after the barrier (approximate):{RESET}")
        for call in hooks["calls"]:
            print(f"   {CYAN}  hook call {_call_row_label(call)} "
                  f"{_fmt_tokens(call)} tok {call['latency_s']}s{RESET}")
        for span in hooks["spans"]:
            print(f"   {CYAN}  historian {span['status']} {span['duration_s']}s "
                  f"(+{span['offset_from_barrier_s']}s){RESET}")
        for mark in hooks["marks"]:
            print(f"   {CYAN}  {mark['event']} (+{mark['offset_from_barrier_s']}s){RESET}")
    else:
        print(f"{DIM}   after the barrier: none observed (in-process engine — the "
              f"D82 hooks are API-scheduled){RESET}")
    for error in turn["events"].get("errors") or []:
        print(f"   {tt.RED}error: {error}{RESET}")
    print(f"{GREEN}   wrote {turn['artifacts']['json']}{RESET}")
    print(f"{GREEN}   wrote {turn['artifacts']['markdown']}{RESET}")


# ---------------------------------------------------------------------------
# live run — real turns through the real engine
# ---------------------------------------------------------------------------


async def run_one_turn(
    engine: Any,
    log: EventLog,
    session_id: str,
    turn: int,
    kind: str,
    content: str,
    *,
    prior_narrate: list[dict] | None = None,
    environment: dict | None = None,
) -> tuple[dict, list[dict] | None]:
    """Drive one real turn, timestamp the stage frames, assemble the report.

    Returns ``(report, narrate_messages)`` — the narrate call's captured message
    list so the next turn can measure the stable-prefix overlap (it never enters
    the artifact: a prompt is tens of KB).
    """
    pre_count = len(log.get_events(session_id))
    frames: list[tt.StageFrame] = []
    first_reasoning_s: float | None = None
    first_content_s: float | None = None
    error: str | None = None
    barrier_seq = 0
    barrier_ts: str | None = None
    t0 = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    async for event in engine.run_turn(session_id, content, kind):
        elapsed = time.monotonic() - t0
        etype = event.get("type")
        if etype == "stage":
            meta = event.get("meta") or {}
            frames.append(
                tt.StageFrame(
                    step=str(meta.get("stage") or ""),
                    state=str(meta.get("state") or ""),
                    label=str(meta.get("label") or ""),
                    index=int(meta.get("index") or 0),
                    total=int(meta.get("total") or 0),
                    t=elapsed,
                )
            )
        elif etype == "reasoning" and first_reasoning_s is None:
            first_reasoning_s = elapsed
        elif etype == "content" and first_content_s is None:
            first_content_s = elapsed
        elif etype == "error":
            error = str(event.get("delta") or "")
        elif etype == "done":
            # The barrier: in-process the turn's own rows end here. Anything the
            # API layer appends after this belongs to the post-turn hooks.
            own_rows = log.get_events(session_id)
            barrier_seq = max((int(row.seq) for row in own_rows), default=0)
            barrier_ts = datetime.now(timezone.utc).isoformat()
    total_s = time.monotonic() - t0

    own_rows = log.get_events(session_id)
    events = own_rows[pre_count:]
    calls = collect_calls(events)
    summary = tt.summarize_turn_events(events)
    if error:
        summary["errors"].append({"event": "turn_error", "detail": error})
        barrier_seq = max((int(row.seq) for row in own_rows), default=0)
        barrier_ts = barrier_ts or datetime.now(timezone.utc).isoformat()

    report = build_turn_report(
        session_id=session_id,
        turn=turn,
        kind=kind,
        user_content=content,
        started_at=started_at,
        total_s=total_s,
        frames=frames,
        calls=calls,
        events=events,
        summary=summary,
        barrier_seq=barrier_seq,
        barrier_ts=barrier_ts,
        first_content_s=first_content_s,
        first_reasoning_s=first_reasoning_s,
        prior_narrate=prior_narrate,
        environment=environment,
    )
    narrate = next((call for call in calls if call.interaction == "narrate"), None)
    return report, (list(narrate.messages) if narrate is not None else None)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


async def run_probe(args: argparse.Namespace) -> dict:
    """Dispatch on ``--mode`` and write ONE ``report.md`` for whatever ran."""
    registry = configure(args.config)
    log_path = Path(args.log)
    out_root = Path(args.out) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    environment = {
        "config": args.config,
        "log": str(log_path),
        "narrator_model": registry.connection_for("narrator").model,
        "engine": "in-process build_engine (no service)",
        "agent_debug": True,
        "mode": args.mode,
    }
    run: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "session_id": "",
        "mode": args.mode,
        "environment": environment,
        "turns": [],
    }
    print(f"{tt.BOLD}perf probe{tt.RESET} mode {args.mode}  "
          f"(config: {args.config}, narrator: {environment['narrator_model']})")

    if args.mode in ("serial", "all"):
        await _run_scripted_turns(args, run, out_root, registry)
    if args.mode in ("concurrent", "all"):
        run["contention"] = _write_experiment(
            out_root, "contention",
            await run_contention_experiment(args, environment, registry),
        )
        _print_contention(run["contention"])
    if args.mode in ("cache", "all"):
        run["cache"] = _write_experiment(
            out_root, "cache", await run_cache_experiment(args, environment, registry)
        )
        _print_cache(run["cache"])

    run_md = out_root / "report.md"
    _atomic_write(run_md, render_run_markdown(run))
    print(f"\n{tt.GREEN}run report: {run_md}{tt.RESET}")
    return run


async def _run_scripted_turns(
    args: argparse.Namespace, run: dict, out_root: Path, registry: Any
) -> None:
    """Drive the scripted beats (``--mode serial`` — today's behaviour)."""
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = EventLog(log_path)
    # D17: capture ON for this run only — the point of a measurement run (the
    # message snapshots are the cache signal's only source).
    engine = build_engine(registry=registry, log=log, agent_debug=True)

    session_id = f"perf-{uuid.uuid4().hex[:8]}"
    log.append_event(
        session_id, "system", "human",
        {"event": "session_created", "title": "perf probe"},
    )
    run["session_id"] = session_id
    environment = run["environment"]
    print(f"{tt.BOLD}perf probe{tt.RESET} session {session_id}  "
          f"(narrator: {environment['narrator_model']}, log: {log_path})")

    beats = tt.DEFAULT_TURNS[: max(1, min(args.turns, len(tt.DEFAULT_TURNS)))]
    if args.turns > len(tt.DEFAULT_TURNS):
        print(f"{tt.YELLOW}--turns {args.turns} exceeds the {len(tt.DEFAULT_TURNS)} "
              f"scripted beats; running {len(tt.DEFAULT_TURNS)}{tt.RESET}")
    turns: list[dict] = []
    prior_narrate: list[dict] | None = None
    for number, (kind, content) in enumerate(beats, 1):
        report, narrate_messages = await run_one_turn(
            engine, log, session_id, number, kind, content,
            prior_narrate=prior_narrate, environment=environment,
        )
        json_path = out_root / f"turn-{number:02d}.json"
        md_path = out_root / f"turn-{number:02d}.md"
        report["artifacts"] = {"json": str(json_path), "markdown": str(md_path)}
        _atomic_write(json_path, json.dumps(report, indent=2, ensure_ascii=False))
        _atomic_write(md_path, render_turn_markdown(report))
        _print_turn(report)
        turns.append(report)
        if narrate_messages:
            prior_narrate = narrate_messages

    run["turns"] = turns
    log.close()


# ---------------------------------------------------------------------------
# the reference-call transport (live only — the folds above never touch it)
# ---------------------------------------------------------------------------

#: The warm-up prompt. Deliberately different from the reference set: it absorbs
#: model load and first-touch cost WITHOUT warming the prefix under measurement.
WARMUP_MESSAGES: list[dict] = [
    {"role": "user", "content": "Reply with the single word: ready."}
]
WARMUP_MAX_TOKENS = 8

#: llama.cpp pays more than load on the first generations (draft-model / spec-decode
#: initialisation), so one warm-up call is not enough to make call 1 a clean measure.
WARMUP_CALLS = 2


def _request_kwargs(connection: Any, messages: list[dict], max_tokens: int) -> dict:
    """``chat.completions.create`` kwargs for a connection (the runtime's merge rules).

    Mirrors ``RegistryModelCaller``: the connection's samplers and non-standard
    body params, then a streamed response asking for the usage chunk. The
    reference calls are the same shape as a real call, which is the point — a
    measurement of a different request shape would not transfer.
    """
    kwargs: dict[str, Any] = {"model": connection.model, "messages": messages}
    kwargs.update(merge_sampling(connection.sampling, {"max_tokens": int(max_tokens)}))
    extra_body = merge_extra_body(connection.extra_body, {})
    if extra_body:
        kwargs["extra_body"] = extra_body
    kwargs["stream"] = True
    kwargs["stream_options"] = {"include_usage": True}
    return kwargs


async def _stream_completion(connection: Any, messages: list[dict], *,
                             max_tokens: int = REFERENCE_MAX_TOKENS):
    """One streaming completion over ``connection``, as the caller protocol's events.

    Yields ``content``/``reasoning`` deltas as they arrive, then a final ``usage``
    event carrying the provider's token counts and its cache accounting
    (``cached_tokens``), which llama.cpp reports and most providers do not.
    """
    from openai import AsyncOpenAI

    api_key = connection.api_key
    if api_key is None and connection.api_key_env:
        api_key = os.environ.get(connection.api_key_env)
    client_init: dict[str, Any] = {
        "base_url": connection.base_url,
        "api_key": api_key or KEYLESS_API_KEY,
    }
    if connection.timeout is not None:
        client_init["timeout"] = connection.timeout
    usage: Any = None
    timings: dict = {}
    async with AsyncOpenAI(**client_init) as client:
        response = await client.chat.completions.create(
            **_request_kwargs(connection, messages, max_tokens)
        )
        async for chunk in response:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage and int(getattr(chunk_usage, "prompt_tokens", 0) or 0):
                usage = chunk_usage
            # llama.cpp sends ``timings`` on a chunk; the SDK keeps unknown fields
            # in ``model_extra`` rather than as attributes, so check both.
            chunk_timings = getattr(chunk, "timings", None)
            if chunk_timings is None:
                chunk_timings = (getattr(chunk, "model_extra", None) or {}).get("timings")
            if isinstance(chunk_timings, dict) and chunk_timings:
                timings = chunk_timings
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield {"type": "reasoning", "delta": reasoning}
                content = getattr(delta, "content", None)
                if content:
                    yield {"type": "content", "delta": content}
    details = getattr(usage, "prompt_tokens_details", None) if usage is not None else None
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if timings:
        yield {"type": "timings", "prompt_ms": timings.get("prompt_ms"),
               "predicted_ms": timings.get("predicted_ms")}
    yield {
        "type": "usage",
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
        "completion_tokens": (
            int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        ),
        "cached_tokens": cached,
    }


def server_facts(connection: Any, *, timeout: float = 5.0) -> dict:
    """The server's own slot configuration, read (never guessed) from ``/v1/models``.

    llama.cpp serves ``--parallel`` slots and queues everything beyond them, so the
    slot count is what turns the contention factor from a coincidence into an
    explanation. Best-effort: an unreadable endpoint is reported, not fatal.
    """
    url = connection.base_url.rstrip("/") + "/models"
    facts: dict[str, Any] = {"model": connection.model, "base_url": connection.base_url}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.load(response)
    except Exception as exc:
        facts.update({"reachable": False, "parallel_slots": None,
                      "note": f"{type(exc).__name__}: {exc}"})
        return facts
    for entry in data.get("data") or []:
        if not isinstance(entry, dict) or entry.get("id") != connection.model:
            continue
        status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
        args = status.get("args") or []
        slots: int | None = None
        for index, arg in enumerate(args if isinstance(args, list) else []):
            if not isinstance(arg, str):
                continue
            if arg.startswith("--parallel="):
                raw = arg.split("=", 1)[1]
            elif arg == "--parallel" and index + 1 < len(args):
                raw = args[index + 1]
            else:
                continue
            try:
                slots = int(str(raw).strip())
            except ValueError:
                slots = None
            if slots is not None:
                break
        facts.update({
            "reachable": True,
            "loaded": status.get("value"),
            "parallel_slots": slots,
            "note": "" if slots is not None else "server did not report --parallel",
        })
        return facts
    facts.update({"reachable": True, "parallel_slots": None,
                  "note": f"model {connection.model!r} not listed by the server"})
    return facts


def resolve_cross_connection(
    registry: Any, local: Any, requested: str = "auto"
) -> tuple[Any | None, str]:
    """Pick the cross-server call's connection: a name, ``auto``, or ``none``.

    ``auto`` takes the first GENERATIVE connection on a different ``base_url``
    from the local one (non-empty ``sampling``, config order) — the point is to
    measure a different server, and guessing an agent binding is not needed.
    """
    if requested in ("", "none", "off"):
        return None, "cross-server call disabled (--cross-connection none)"
    if requested != "auto":
        connection = registry.connections.get(requested)
        if connection is None:
            return None, f"connection {requested!r} is not in the registry"
        return connection, f"explicit cross-server connection {connection.name!r}"
    for connection in registry.connections.values():
        if connection.base_url == local.base_url or not connection.sampling:
            continue
        return connection, (
            f"auto-picked the first generative connection on another server: "
            f"{connection.name!r}"
        )
    return None, "no generative connection on a different server"


def shape_warmup_messages(prefix_tokens: int, nonce: str = "") -> list[dict]:
    """The reference prompt's SHAPE with different words and a different opening.

    For the prompt-cache probe the measured prefix must stay COLD, but the model's
    graph, sampler and speculative draft should already be warm — otherwise the
    first call absorbs initialisation the second never pays and the probe would
    report a cache hit it did not earn. llama.cpp reuses cached KV by matching text
    (not only from token 0), so the warm-up prompt has the same SIZE and none of
    the same words.
    """
    return [
        {"role": "system", "content": "Reply with the single word: ready."},
        reference_messages(prefix_tokens=prefix_tokens,
                           line=REFERENCE_WARMUP_LINE, nonce=nonce)[1],
    ]


async def _warmup(
    connection: Any,
    *,
    stage: str,
    shape_messages: list[dict] | None = None,
) -> list[dict]:
    """Discardable calls that absorb model load, first-touch and the prompt's shape.

    ``shape_messages`` is what the last warm-up call sends. For the contention
    experiment that is the reference prompt itself (a lane fan-out shares one
    narrator prefix, so a warm prefix is the representative case); for the cache
    probe it is :func:`shape_warmup_messages`, which warms the shape but not the
    prefix under measurement.
    """
    plan: list[tuple[str, list[dict], int]] = [
        (f"{stage}-warmup-{connection.name}-{index}", WARMUP_MESSAGES, WARMUP_MAX_TOKENS)
        for index in range(1, WARMUP_CALLS + 1)
    ]
    if shape_messages is not None:
        plan.append((f"{stage}-warmup-{connection.name}-shape", shape_messages,
                     REFERENCE_MAX_TOKENS))
    results: list[dict] = []
    for label, messages, max_tokens in plan:
        print(f"{tt.DIM}   {label}…{tt.RESET}")
        result = await timed_completion(
            partial(_stream_completion, connection), messages, label=label,
            server=connection.name, model=connection.model, max_tokens=max_tokens,
        )
        results.append(call_result_dict(result))
    return results


async def run_contention_experiment(
    args: argparse.Namespace, environment: dict, registry: Any
) -> dict:
    """Fire the reference call set serially, then all at once, and compare the walls."""
    local = registry.connection_for(args.agent)
    cross, cross_note = resolve_cross_connection(registry, local, args.cross_connection)
    messages = reference_messages(prefix_tokens=args.reference_tokens)
    plan = [
        CallSpec(
            label=f"local-{index}", server=local.name, model=local.model,
            messages=messages, stream_fn=partial(_stream_completion, local),
        )
        for index in range(1, max(1, args.calls) + 1)
    ]
    if cross is not None:
        plan.append(
            CallSpec(
                label="cross", server=cross.name, model=cross.model,
                messages=messages, stream_fn=partial(_stream_completion, cross),
            )
        )
    print(f"{tt.BOLD}contention{tt.RESET} {len(plan)} reference calls "
          f"(~{args.reference_tokens} tokens, max {REFERENCE_MAX_TOKENS} out): "
          f"{len(plan)} serial, then {len(plan)} in flight  [{cross_note}]")
    warmup = await _warmup(local, stage="before-serial", shape_messages=messages)
    if cross is not None:
        warmup += await _warmup(cross, stage="before-serial", shape_messages=messages)

    print(f"{tt.DIM}   serial arm…{tt.RESET}")
    serial = await run_arm(plan, concurrent=False)
    # Re-warm before the second arm with the SAME calls: a server that reloads or
    # swaps the model between requests charges that cost to the warm-up, never to
    # one arm — an asymmetric arm would read as contention (or as its absence).
    warmup += await _warmup(local, stage="before-concurrent", shape_messages=messages)
    if cross is not None:
        warmup += await _warmup(cross, stage="before-concurrent",
                                shape_messages=messages)
    print(f"{tt.DIM}   concurrent arm…{tt.RESET}")
    concurrent = await run_arm(plan, concurrent=True)

    facts: dict[str, Any] = {"local": server_facts(local)}
    if cross is not None:
        facts["cross"] = server_facts(cross)
    return build_contention_report(
        serial, concurrent,
        local_server=local.name,
        cross_server=cross.name if cross is not None else "",
        environment={**environment, "cross_note": cross_note,
                     "reference_tokens_est": args.reference_tokens,
                     "agent": args.agent},
        server_facts=facts,
        warmup=warmup,
    )


async def run_cache_experiment(
    args: argparse.Namespace, environment: dict, registry: Any
) -> dict:
    """Two successive calls over one byte-identical prefix (the loop's iteration shape)."""
    connection = registry.connection_for(args.cache_agent)
    # A per-run nonce stamps every repetition of the filler: llama.cpp reuses KV by
    # matching text, so without it the prefix would already be cached by an earlier
    # run and the "cold" first call would be measured warm.
    nonce = uuid.uuid4().hex[:8]
    messages = reference_messages(prefix_tokens=args.cache_prefix_tokens, nonce=nonce)
    stream_fn = partial(_stream_completion, connection)
    print(f"{tt.BOLD}prompt cache{tt.RESET} two calls over a "
          f"~{args.cache_prefix_tokens}-token identical prefix on "
          f"{connection.name} ({connection.model}), run nonce {nonce}")
    warmup = await _warmup(
        connection, stage="prefix",
        shape_messages=shape_warmup_messages(args.cache_prefix_tokens, nonce=nonce),
    )
    first = await timed_completion(
        stream_fn, messages, label="prefix call 1", server=connection.name,
        model=connection.model,
    )
    print(f"{tt.DIM}   first call {round(first.latency_s, 2)}s…{tt.RESET}")
    second = await timed_completion(
        stream_fn, messages, label="prefix call 2", server=connection.name,
        model=connection.model,
    )
    return build_cache_report(
        first, second,
        messages=messages,
        prefix_tokens=args.cache_prefix_tokens,
        environment={**environment, "cache_agent": args.cache_agent,
                     "prefix_nonce": nonce},
        server_facts={"local": server_facts(connection)},
        warmup=warmup,
    )


def _write_experiment(out_root: Path, name: str, report: dict) -> dict:
    """Write one experiment's artifacts (``<name>.json`` + ``<name>.md``) and return it."""
    json_path = out_root / f"{name}.json"
    md_path = out_root / f"{name}.md"
    report["artifacts"] = {"json": str(json_path), "markdown": str(md_path)}
    _atomic_write(json_path, json.dumps(report, indent=2, ensure_ascii=False))
    renderer = (
        render_contention_markdown if name == "contention" else render_cache_markdown
    )
    _atomic_write(md_path, renderer(report))
    return report


def _print_contention(report: dict) -> None:
    """The readable stdout block for the contention experiment."""
    BOLD, DIM, CYAN, RESET = tt.BOLD, tt.DIM, tt.CYAN, tt.RESET
    print(f"\n{BOLD}── contention  serial {report['serial']['wall_s']}s vs "
          f"concurrent {report['concurrent']['wall_s']}s  ·  factor "
          f"{report['factor']}×  ·  verdict {report['verdict']}{RESET}")
    for row in report["rows"]:
        print(f"   {DIM}{row['label']:<10} {row['server']:<10} "
              f"serial {row['serial_s']:>7.2f}s  concurrent "
              f"{row['concurrent_s']:>7.2f}s  ({row['inflation']}×){RESET}")
    for sentence in report["answer"]:
        print(f"   {CYAN}{sentence}{RESET}")
    print(f"   {DIM}wrote {report['artifacts']['markdown']}{RESET}")


def _print_cache(report: dict) -> None:
    """The readable stdout block for the prompt-cache probe."""
    BOLD, DIM, CYAN, RESET = tt.BOLD, tt.DIM, tt.CYAN, tt.RESET
    print(f"\n{BOLD}── prompt cache  first {report['first']['latency_s']}s "
          f"(TTFT {report['first']['ttft_s']}s) vs second "
          f"{report['second']['latency_s']}s (TTFT {report['second']['ttft_s']}s)  ·  "
          f"verdict {report['verdict']}{RESET}")
    print(f"   {DIM}cached tokens: first "
          f"{report['cached_tokens']['first']} · second "
          f"{report['cached_tokens']['second']} of "
          f"{report['second']['prompt_tokens']} prompt tokens{RESET}")
    for sentence in report["answer"]:
        print(f"   {CYAN}{sentence}{RESET}")
    print(f"   {DIM}wrote {report['artifacts']['markdown']}{RESET}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The CLI surface (imported by the tests; no side effects)."""
    parser = argparse.ArgumentParser(
        prog="perf_probe.py",
        description=(
            "Measure a real turn's wall-clock, tokens and cache behaviour, and "
            "answer §3.1's two questions: is the model server contended or "
            "serialized, and does a loop iteration ride the cached prefix."
        ),
    )
    parser.add_argument("--mode", default="serial", choices=list(CALL_MODES),
                        help="serial = the scripted turns (default); concurrent = "
                             "the contention experiment; cache = the prompt-cache "
                             "probe; all = the turns plus both experiments into "
                             "one report")
    parser.add_argument("--config", default="configs/connections.json",
                        help="connection registry (default: %(default)s)")
    parser.add_argument("--log", default="data/perf_probe.db",
                        help="the run's OWN event log (default: %(default)s); "
                             "point it somewhere isolated (e.g. /tmp) to keep "
                             "user state untouched")
    parser.add_argument("--out", default="data/perf",
                        help="artifact root (default: %(default)s); the run writes "
                             "<out>/<timestamp>/turn-NN.json, contention.*, cache.* "
                             "and report.md")
    parser.add_argument("--turns", type=int, default=2,
                        help="scripted turns to run (1..%d; default 2 — two real "
                             "turns is the acceptance minimum)"
                             % len(tt.DEFAULT_TURNS))
    parser.add_argument("--agent", default="extractor",
                        help="the agent whose connection carries the contention "
                             "reference calls (default: %(default)s)")
    parser.add_argument("--calls", type=int, default=3,
                        help="reference calls per arm on the local server "
                             "(default: %(default)s)")
    parser.add_argument("--reference-tokens", type=int, default=800,
                        help="estimated prompt tokens per reference call "
                             "(default: %(default)s)")
    parser.add_argument("--cross-connection", default="auto",
                        help="the cross-server call's connection: a name, 'auto' "
                             "(the first generative connection on another server) "
                             "or 'none' (default: %(default)s)")
    parser.add_argument("--cache-agent", default="narrator",
                        help="the agent whose connection carries the prompt-cache "
                             "probe (default: %(default)s) — the loop that needs "
                             "the cached prefix is the narrator's")
    parser.add_argument("--cache-prefix-tokens", type=int, default=CACHE_PREFIX_TOKENS,
                        help="estimated tokens of the identical prefix "
                             "(default: %(default)s)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    asyncio.run(run_probe(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
