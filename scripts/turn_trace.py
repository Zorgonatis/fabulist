#!/usr/bin/env python
"""One REAL turn, step by step — what runs, when, and its impact.

This is the diagnostic turn runner PLAN D46 asks for (wave W8.3): it drives the
**real** engine through the same construction ``scripts/feel_test.py`` uses
(``fabulist.wiring.build_engine`` + the connection registry) against the live
model on ``:4444``, then writes an ORDERED step trace — a JSON artifact plus a
human-readable markdown companion — that answers three questions the owner could
not answer from the code alone:

* **what runs, in what order** — every in-graph step boundary the engine already
  emits as a ``stage`` frame (D42): ``narrate → dsl → critic → world_state →
  env_hud → attribution`` (plus ``regen`` only when the critic returns FAIL);
* **when the wall-clock went** — each step's start/finish, duration and share of
  the turn, with model steps ranked at the top (a real turn is ~79s; the point
  of the artifact is to make the expensive step obvious);
* **its impact / what the agent saw** — for each MODEL step: the interaction, the
  resolved agent + model, the token usage (from the persisted usage rows) and the
  exact message roles/shape the caller received (from ``DebugCapture``), plus the
  critic's verdict and any ``revision`` event it landed.

How it is built (no engine instrumentation)
-------------------------------------------
The trace reads the two seams that already exist (D17/D23), rather than adding
new ones:

* ``stage`` frames — the engine yields these on the turn stream at every in-graph
  step boundary (``graph/nodes.py`` ``_emit_stage``); this script timestamps them
  as they arrive and pairs ``start``/``done`` into steps;
* ``agent_call`` captures — ``agents/debug.DebugCapture`` records the context
  snapshot + raw output of every registry call and ``append_call_event`` writes
  it as a ``system`` event; this script reads those events back from the log and
  pairs each with its usage row;
* ``usage`` rows — ``RegistryModelCaller`` persists one per call
  (``usage/track.record_usage``); paired with their capture for the model name
  and token counts;
* log events (``narrative`` / ``revision`` / ``state-delta`` / ``validator_flags``
  / ``critic_verdict`` / ``world_state`` / ``env_hud`` / ``attribution``) for the
  turn's impact.

Debug capture defaults ON here (``build_engine(agent_debug=True)``) — that is the
point of a diagnostic run; it is opt-in elsewhere.

The turn pipeline this trace observes
-------------------------------------
Pre-turn: GM rolls (deterministic) run in the engine before the graph. Vector
recall (an embedder call) runs only when the engine is built with a ``recall_fn``
— this script, like ``scripts/feel_test.py``, builds WITHOUT one, so the observed
trace begins at the first in-graph stage below (``pre_turn`` emits no stage
frame). In-graph, in order:

1. ``narrate``   MODEL   — one streaming call, prose + openui DSL only (D9/D38).
2. ``dsl``       DETERMINISTIC — ``validate_dsl`` repair; a MODEL re-synthesis
                   only when the gate cannot repair unrecoverable DSL
                   (the invisible non-registry caller ``dsl_resynthesizer``).
3. ``critic``    MODEL   — the D41 quality gate, in-graph after the narrative
                   event exists. Verdict ``ok`` keeps / ``diffs`` applies a
                   ``revision`` / ``fail`` re-runs the narrator (step 3b).
3b. ``regen``    MODEL   — only on FAIL, hard cap 2 (same progress slot as critic).
4. ``world_state`` MODEL — the BTS tracker owns the state-delta (D21/B4).
5. ``env_hud``   DETERMINISTIC — one engine-assembled EnvTracker statement (D38).
6. ``attribution`` DETERMINISTIC — speaker tagging from the session cast (D25/B5;
                   no model call: it is code, so it has no message shape to trace
                   and no ``--interaction`` mode).

NOT in the trace, deliberately: the fire-and-forget post-``done`` hooks
(compaction / memory index / assistant consult) run after the user is released
and cannot ride the turn stream, so ``done`` honestly means "every in-graph step
finished" (D42/D49). ``--interaction`` below runs ONE of the post-turn agents in
isolation when you do not want to pay for a whole turn.

Invocations
-----------
::

    # One real turn, default configs/connections.json -> data/traces/<sid>/turn-01.{json,md}
    uv run python scripts/turn_trace.py

    # A scripted sequence of N turns (the feel_test beats), one artifact each
    uv run python scripts/turn_trace.py --turns 3

    # Point at the live log so flow resolution uses the persisted default preset
    uv run python scripts/turn_trace.py --log data/fabulist.db

    # SINGLE INTERACTION — no turn generated; print the assembled input + raw output
    uv run python scripts/turn_trace.py --interaction extract \\
        --user-message "I push through the doors." --narrative "The doors give way..."
    uv run python scripts/turn_trace.py --interaction critic \\
        --user-message "I draw my blade." --narrative "<prose to review>"
    uv run python scripts/turn_trace.py --interaction bts --session <session_id>

``--interaction EXTRA`` is honest about what it cannot synthesise: ``extract``
and ``bts`` run fully standalone; ``critic`` needs the session's prior-prose tail
+ state line for a byte-faithful evidence bundle, so without ``--session`` it
reviews the given prose with an EMPTY prior/state (printed as such, never
faked). ``attribution`` has no mode: it is deterministic code now (owner
decision), so there are no messages to assemble and no model to call.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fabulist.connections.client import configure
from fabulist.log import EventLog
from fabulist.wiring import build_engine, make_agent_caller

__all__ = [
    "DEFAULT_TURNS",
    "ModelCall",
    "StageFrame",
    "TraceStep",
    "assemble_interaction_messages",
    "build_trace",
    "collect_calls",
    "main",
    "parse_args",
    "render_markdown",
    "summarize_turn_events",
]

#: Scripted turns borrowed from ``scripts/feel_test.py`` (two in-character beats
#: that move the scene, then one OOC probe with no graph run).
DEFAULT_TURNS: list[tuple[str, str]] = [
    ("in_character", "I push through the saloon doors of the Rusty Anchor tavern, "
     "soaked from the rain, and look for the harbormaster."),
    ("in_character", "I ignore him and head out into the storm to cross the market "
     "square to the chandler's shop. On the way I pick up a dropped brass lantern."),
    ("ooc", "OOC: quick recap for me - where am I and what's the weather doing?"),
]

#: Interaction kinds ``--interaction`` accepts, and the registry agent each falls
#: back to when the active preset declares no binding (mirrors wiring).
_ISOLATED_INTERACTIONS: dict[str, str] = {
    "extract": "extractor",
    "bts": "extractor",
    "critic": "extractor",
}

#: BTS/critic registry agent fallback (D19/D49).
_POST_TURN_FALLBACK = "extractor"

#: ANSI palette shared with ``scripts/feel_test.py``.
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"

#: Steps that are inherently MODEL calls, regardless of whether a capture landed
#: (a failed/empty call still leaves a model step). Everything else the engine
#: emits is DETERMINISTIC code; a ``dsl`` re-synthesis call flips ``dsl`` to model.
#: ``attribution`` is deliberately ABSENT: speaker attribution is deterministic
#: code now (owner decision), so it never costs a call.
_MODEL_STEPS = frozenset({"narrate", "critic", "regen", "world_state"})

#: The order in which the narrate-interaction calls are consumed (narrate always
#: one; ``dsl`` at most one re-synthesis; then each ``regen`` attempt). Used to
#: assign the shared "narrate" interaction's calls to the right step.
_NARRATE_GROUP = ("narrate", "dsl", "regen")

#: Interaction -> the stage step it belongs to, for the 1:1 post-turn passes.
_INTERACTION_STEP: dict[str, str] = {
    "critic": "critic",
    "bts": "world_state",
    "extract": "extract",
    "ooc": "ooc",
}


# ---------------------------------------------------------------------------
# records — the fabricated-or-real inputs the trace assembles from
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageFrame:
    """One observed ``stage`` frame, timestamped on arrival (D42).

    ``t`` is seconds since turn start (monotonic-derived); ``step`` is the stable
    machine id (``narrate``/``dsl``/``critic``/``regen``/``world_state``/
    ``env_hud``/``attribution``), ``state`` is ``start``/``done``.
    """

    step: str
    state: str
    label: str
    index: int
    total: int
    t: float


@dataclass(frozen=True)
class ModelCall:
    """One captured agent call, paired with its usage row (D17/D23).

    Built from ``agents.debug.AgentCall`` + the persisted usage event; the
    fields here are the trace's JSON-safe view.
    """

    interaction: str
    agent: str
    model: str
    seq: int
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    finish: str | None
    roles: list[str]
    messages: list[dict]
    response_text: str
    reasoning_text: str


@dataclass
class TraceStep:
    """One ordered step of the turn — a ``stage`` frame pair, or a lone call."""

    n: int
    step: str
    label: str
    kind: str  # "model" | "deterministic"
    start_s: float
    end_s: float
    duration_s: float
    index: int
    total: int
    call: ModelCall | None = None
    verdict: dict | None = None
    revisions: list[dict] = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The full CLI surface (imported by the tests; no side effects)."""
    parser = argparse.ArgumentParser(
        prog="turn_trace.py",
        description=(
            "Drive a real turn (or an isolated interaction) and write an ordered "
            "step-trace artifact: what runs, when, and what each agent saw."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="configs/connections.json",
                        help="connection registry (default: %(default)s)")
    parser.add_argument("--log", default="data/turn_trace.db",
                        help="event log for the trace session; point at "
                             "data/fabulist.db to resolve the live persisted preset")
    parser.add_argument("--out", default="data/traces",
                        help="artifact root (default: %(default)s)")
    parser.add_argument("--turns", type=int, default=1,
                        help="number of scripted turns to run (1..%d; default 1)"
                             % len(DEFAULT_TURNS))
    parser.add_argument(
        "--interaction",
        choices=sorted(_ISOLATED_INTERACTIONS),
        default=None,
        help="run ONE interaction in isolation and print its input/output "
             "(no turn generated)",
    )
    parser.add_argument("--session", default=None,
                        help="existing session id, for --interaction evidence "
                             "(critic prior/state, bts prior-state)")
    parser.add_argument("--user-message", default=None,
                        help="isolated-mode user message (default: a sample line)")
    parser.add_argument("--narrative", default=None,
                        help="isolated-mode narrative prose (default: a sample)")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# reading the turn back from the log
# ---------------------------------------------------------------------------


def collect_calls(events: list[Any]) -> list[ModelCall]:
    """Pair every ``agent_call`` capture with its nearest preceding usage row.

    ``RegistryModelCaller`` appends the usage event *before* it returns control
    to ``DebugCapture``, which then appends the ``agent_call`` capture — so the
    capture's own usage row is the closest unpaired ``usage`` event before it.
    Pairing is purely positional (the capture's ``agent`` field is the
    INTERACTION while the usage row's ``agent`` is the resolved registry agent),
    which is exact for the in-graph turn order this script drives.
    """
    from fabulist.agents.debug import AGENT_CALL_EVENT
    from fabulist.usage.track import USAGE_EVENT

    captures: list[Any] = []
    usages: list[Any] = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type != "system":
            continue
        kind = payload.get("event")
        if kind == AGENT_CALL_EVENT:
            captures.append(event)
        elif kind == USAGE_EVENT:
            usages.append(event)

    calls: list[ModelCall] = []
    used_usage: set[int] = set()
    for capture in captures:
        payload = capture.payload
        # nearest preceding unpaired usage row
        pair = None
        for usage in reversed(usages):
            if usage.seq >= capture.seq:
                continue
            if id(usage) in used_usage:
                continue
            pair = usage
            break
        usage_payload = pair.payload if pair is not None else {}
        if pair is not None:
            used_usage.add(id(pair))
        messages = payload.get("messages")
        messages = messages if isinstance(messages, list) else []
        calls.append(
            ModelCall(
                interaction=str(payload.get("agent") or ""),
                agent=str(usage_payload.get("agent") or payload.get("agent") or ""),
                model=str(usage_payload.get("model") or ""),
                seq=int(capture.seq),
                latency_s=float(payload.get("latency_s") or 0.0),
                prompt_tokens=int(usage_payload.get("prompt_tokens") or 0),
                completion_tokens=int(usage_payload.get("completion_tokens") or 0),
                total_tokens=int(usage_payload.get("total_tokens") or 0),
                finish=payload.get("finish") if isinstance(payload.get("finish"), str) else None,
                roles=[str(m.get("role") or "") for m in messages if isinstance(m, dict)],
                messages=[dict(m) for m in messages if isinstance(m, dict)],
                response_text=str(payload.get("response_text") or ""),
                reasoning_text=str(payload.get("reasoning_text") or ""),
            )
        )
    return calls


def summarize_turn_events(events: list[Any]) -> dict:
    """Fold the turn's log events into the impact summary the artifact carries."""
    out: dict[str, Any] = {
        "narrative": None,
        "revisions": [],
        "state_delta": None,
        "validation_flags": [],
        "critic_verdicts": [],
        "errors": [],
        "world_state": None,
        "env_hud": None,
        "attribution": None,
    }
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type == "narrative":
            config = {}
            if isinstance(event.meta, dict):
                config = event.meta.get("config") or {}
            out["narrative"] = {
                "seq": event.seq,
                "prose_chars": len(str(payload.get("prose") or "")),
                "dsl_chars": len(str(payload.get("dsl") or "")),
                "facts_text_chars": len(str(config.get("facts_text") or "")),
                "regions": config.get("regions"),
                "facts_hash": config.get("facts_hash"),
            }
        elif event.type == "revision":
            out["revisions"].append(
                {
                    "id": event.id,
                    "seq": event.seq,
                    "actor": event.actor,
                    "supersedes": event.supersedes,
                    "verdict": payload.get("verdict"),
                    "reason": payload.get("reason"),
                    "prose_chars": len(str(payload.get("prose") or "")),
                }
            )
        elif event.type == "state-delta":
            out["state_delta"] = {"seq": event.seq, "delta": payload}
        elif event.type == "system":
            kind = payload.get("event")
            if kind == "validator_flags":
                out["validation_flags"].append(
                    {"score": payload.get("score"), "flags": payload.get("flags") or []}
                )
            elif kind == "critic_verdict":
                out["critic_verdicts"].append(
                    {
                        "verdict": payload.get("verdict"),
                        "attempt": payload.get("attempt"),
                        "reason": payload.get("reason"),
                        "edits": payload.get("edits") or [],
                    }
                )
            elif kind in {"critic_failed", "validator_failed", "world_state_failed",
                          "env_hud_failed", "attribution_failed", "extraction_failed"}:
                out["errors"].append({"event": kind, **{
                    k: v for k, v in payload.items() if k != "event"
                }})
            elif kind == "world_state":
                out["world_state"] = {"seq": event.seq,
                                      "categories": sorted((payload.get("state") or {}).keys())}
            elif kind == "env_hud":
                out["env_hud"] = {"seq": event.seq,
                                  "statement_chars": len(str(payload.get("statement") or ""))}
            elif kind == "attribution":
                rows = payload.get("attributions") or []
                out["attribution"] = {"seq": event.seq, "count": len(rows),
                                      "speakers": sorted({str(r.get("speaker")) for r in rows
                                                          if isinstance(r, dict)})}
    return out


# ---------------------------------------------------------------------------
# trace assembly (pure — the unit-tested core)
# ---------------------------------------------------------------------------


def _steps_from_frames(frames: list[StageFrame]) -> list[TraceStep]:
    """Pair ordered ``start``/``done`` frames into steps.

    A ``done`` with no matching open ``start`` (or an unclosed ``start`` at the
    end, e.g. a crash) still yields a step, so the timeline never silently drops
    a boundary.
    """
    steps: list[TraceStep] = []
    open_step: StageFrame | None = None
    for frame in frames:
        if frame.state == "start":
            open_step = frame
            continue
        # state == "done"
        start = open_step.t if open_step is not None else frame.t
        label = open_step.label if open_step is not None else frame.label
        index = open_step.index if open_step is not None else frame.index
        total = open_step.total if open_step is not None else frame.total
        steps.append(
            TraceStep(
                n=0,
                step=frame.step,
                label=label,
                kind="model" if frame.step in _MODEL_STEPS else "deterministic",
                start_s=start,
                end_s=frame.t,
                duration_s=max(0.0, frame.t - start),
                index=index,
                total=total,
            )
        )
        open_step = None
    if open_step is not None:
        steps.append(
            TraceStep(
                n=0,
                step=open_step.step,
                label=open_step.label,
                kind="model" if open_step.step in _MODEL_STEPS else "deterministic",
                start_s=open_step.t,
                end_s=open_step.t,
                duration_s=0.0,
                index=open_step.index,
                total=open_step.total,
                note="step never emitted a done frame",
            )
        )
    return steps


def _steps_from_calls(calls: list[ModelCall]) -> list[TraceStep]:
    """Fallback timeline for a run with no stage frames (e.g. an OOC turn).

    Durations are the call latencies laid end-to-end; there is no per-step
    wall-clock boundary to observe because the engine emits no stage frames
    outside the in-character graph.
    """
    steps: list[TraceStep] = []
    cursor = 0.0
    for call in calls:
        duration = max(0.0, call.latency_s)
        steps.append(
            TraceStep(
                n=0,
                step=call.interaction or "call",
                label=call.interaction or "call",
                kind="model",
                start_s=cursor,
                end_s=cursor + duration,
                duration_s=duration,
                index=0,
                total=0,
                call=call,
                note="timeline synthesized from call latencies (no stage frames)",
            )
        )
        cursor += duration
    return steps


def _assign_calls_to_steps(steps: list[TraceStep], calls: list[ModelCall]) -> None:
    """Attach each captured call to the step that produced it (in place).

    1:1 interactions (``critic``/``bts``/``extract``/``ooc``) map to their named
    step. The ``narrate`` interaction is shared by ``narrate``,
    ``dsl`` (re-synthesis) and ``regen`` — the first call is the narrate pass,
    the last ``len(regen steps)`` calls are the regeneration attempts, and any
    call left between them is the DSL re-synthesis.
    """
    assigned_seqs: set[int] = set()

    for interaction, step_name in _INTERACTION_STEP.items():
        group = [c for c in calls if c.interaction == interaction]
        targets = [s for s in steps if s.step == step_name]
        for call, step in zip(group, targets):
            step.call = call
            step.kind = "model"
            assigned_seqs.add(call.seq)

    narr = [c for c in calls if c.interaction == "narrate"]
    if not narr:
        return
    narrate_steps = [s for s in steps if s.step == "narrate"]
    dsl_steps = [s for s in steps if s.step == "dsl"]
    regen_steps = [s for s in steps if s.step == "regen"]
    idx = 0
    if narrate_steps and idx < len(narr):
        narrate_steps[0].call = narr[idx]
        narrate_steps[0].kind = "model"
        assigned_seqs.add(narr[idx].seq)
        idx += 1
    remaining = len(narr) - idx
    # Calls beyond one-per-regen-step can only be the DSL re-synthesis.
    if remaining > len(regen_steps) and dsl_steps:
        dsl_steps[0].call = narr[idx]
        dsl_steps[0].kind = "model"
        assigned_seqs.add(narr[idx].seq)
        idx += 1
    for step in regen_steps:
        if idx < len(narr):
            step.call = narr[idx]
            step.kind = "model"
            assigned_seqs.add(narr[idx].seq)
            idx += 1

    # Anything still unattached becomes its own step, so no call is dropped.
    for call in calls:
        if call.seq in assigned_seqs:
            continue
        if any(s.call is not None and s.call.seq == call.seq for s in steps):
            continue
        steps.append(
            TraceStep(
                n=0,
                step=call.interaction or "call",
                label=call.interaction or "call",
                kind="model",
                start_s=0.0,
                end_s=call.latency_s,
                duration_s=call.latency_s,
                index=0,
                total=0,
                call=call,
                note="call had no matching stage frame",
            )
        )


def _attach_verdicts(steps: list[TraceStep], events: dict) -> None:
    """Attach the critic's verdict (and any revision) to its step (D41/D49)."""
    verdicts = events.get("critic_verdicts") or []
    revisions = list(events.get("revisions") or [])
    critic_steps = [s for s in steps if s.step == "critic"]
    regen_steps = [s for s in steps if s.step == "regen"]
    if critic_steps and verdicts:
        critic_steps[0].verdict = verdicts[0]
    # ``diffs`` supersedes via the original narrative; ``fail`` via a regen.
    for revision in revisions:
        if revision.get("verdict") == "diffs" and critic_steps:
            critic_steps[0].revisions.append(revision)
        elif regen_steps:
            regen_steps[-1].revisions.append(revision)
        elif critic_steps:
            critic_steps[0].revisions.append(revision)


def build_trace(
    *,
    session_id: str,
    turn: int,
    kind: str,
    user_content: str,
    wall_started_at: str,
    total_s: float,
    frames: list[StageFrame],
    calls: list[ModelCall],
    events: dict,
) -> dict:
    """Assemble the ordered step trace from fabricated-or-real records.

    Pure and synchronous: the whole assembly is exercised by the unit tests
    without a model. Returns a JSON-safe dict with the ordered ``steps``, the
    ``headline`` ranking (duration descending), the token totals and the
    ``events`` impact summary.
    """
    notes: list[str] = []
    steps = _steps_from_frames(frames)
    if not steps and calls:
        steps = _steps_from_calls(calls)
        notes.append(
            "no stage frames observed (OOC or pre-graph path); timeline synthesized "
            "from call latencies"
        )
    _assign_calls_to_steps(steps, calls)
    _attach_verdicts(steps, events)
    if not frames and not calls:
        notes.append("the turn produced no model calls and no stage frames")

    for i, step in enumerate(steps, 1):
        step.n = i

    total_tokens = {
        "prompt_tokens": sum(s.call.prompt_tokens for s in steps if s.call),
        "completion_tokens": sum(s.call.completion_tokens for s in steps if s.call),
        "total_tokens": sum(s.call.total_tokens for s in steps if s.call),
    }
    measured = sum(s.duration_s for s in steps)
    denom = total_s if total_s > 0 else measured
    headline = sorted(
        (
            {
                "step": s.step,
                "label": s.label,
                "kind": s.kind,
                "duration_s": round(s.duration_s, 3),
                "pct_of_turn": round(100.0 * s.duration_s / denom, 1) if denom else 0.0,
            }
            for s in steps
        ),
        key=lambda row: row["duration_s"],
        reverse=True,
    )

    return {
        "session_id": session_id,
        "turn": turn,
        "kind": kind,
        "user_content": user_content,
        "started_at": wall_started_at,
        "total_s": round(total_s, 3),
        "steps": [_step_to_dict(s) for s in steps],
        "headline": headline,
        "tokens": total_tokens,
        "events": events,
        "notes": notes,
    }


def _step_to_dict(step: TraceStep) -> dict:
    out: dict[str, Any] = {
        "n": step.n,
        "step": step.step,
        "label": step.label,
        "kind": step.kind,
        "start_s": round(step.start_s, 3),
        "end_s": round(step.end_s, 3),
        "duration_s": round(step.duration_s, 3),
        "index": step.index,
        "total": step.total,
    }
    if step.verdict is not None:
        out["verdict"] = step.verdict
    if step.revisions:
        out["revisions"] = step.revisions
    if step.note:
        out["note"] = step.note
    if step.call is not None:
        call = step.call
        out["call"] = {
            "interaction": call.interaction,
            "agent": call.agent,
            "model": call.model,
            "seq": call.seq,
            "latency_s": round(call.latency_s, 3),
            "finish": call.finish,
            "prompt_tokens": call.prompt_tokens,
            "completion_tokens": call.completion_tokens,
            "total_tokens": call.total_tokens,
            "roles": call.roles,
            "messages": call.messages,
            "message_shapes": _message_summary(call.messages),
            "response_text": call.response_text,
            "reasoning_text": call.reasoning_text,
        }
    return out


def _message_summary(messages: list[dict]) -> list[dict]:
    """Per-message role/shape (the "how much context" view)."""
    rows: list[dict] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars = len(content)
            shape = "str"
        elif isinstance(content, list):
            chars = sum(
                len(str(part.get("text") or ""))
                for part in content
                if isinstance(part, dict)
            )
            shape = f"list[{len(content)}]"
        else:
            chars = 0
            shape = type(content).__name__
        rows.append({"role": str(message.get("role") or ""), "shape": shape, "chars": chars})
    return rows


# ---------------------------------------------------------------------------
# markdown companion
# ---------------------------------------------------------------------------


def render_markdown(trace: dict) -> str:
    """Human-readable companion: the headline table, then the ordered steps."""
    lines: list[str] = []
    lines.append(f"# Turn trace — {trace['session_id']} / turn {trace['turn']}")
    lines.append("")
    lines.append(f"- **kind**: {trace['kind']}  ·  **total wall-clock**: "
                 f"{trace['total_s']}s  ·  **started**: {trace['started_at']}")
    lines.append(f"- **user**: {trace['user_content']}")
    tokens = trace["tokens"]
    lines.append(f"- **tokens**: prompt {tokens['prompt_tokens']} + completion "
                 f"{tokens['completion_tokens']} = {tokens['total_tokens']}")
    lines.append("")
    lines.append("## Where the wall-clock went (ranked)")
    lines.append("")
    lines.append("| step | kind | seconds | % of turn |")
    lines.append("|---|---|---:|---:|")
    for row in trace["headline"]:
        lines.append(f"| {row['step']} | {row['kind']} | {row['duration_s']} | "
                     f"{row['pct_of_turn']}% |")
    lines.append("")
    lines.append("## Ordered steps")
    lines.append("")
    for step in trace["steps"]:
        head = (f"### {step['n']}. `{step['step']}` — {step['label']} "
                f"[{step['kind']}] {step['duration_s']}s "
                f"({step['start_s']}s → {step['end_s']}s)")
        lines.append(head)
        if step.get("note"):
            lines.append(f"- _{step['note']}_")
        call = step.get("call")
        if call:
            lines.append(f"- interaction `{call['interaction']}` · agent "
                         f"`{call['agent']}` · model `{call['model']}`")
            lines.append(f"- latency {call['latency_s']}s · finish "
                         f"`{call['finish']}` · tokens {call['prompt_tokens']}+"
                         f"{call['completion_tokens']}={call['total_tokens']}")
            lines.append(f"- messages ({len(call['message_shapes'])}):")
            for shape in call["message_shapes"]:
                lines.append(f"    - `{shape['role']}` ({shape['shape']}, "
                             f"{shape['chars']} chars)")
        if step.get("verdict"):
            verdict = step["verdict"]
            lines.append(f"- **critic verdict**: `{verdict.get('verdict')}` "
                         f"(attempt {verdict.get('attempt')}) "
                         f"{verdict.get('reason') or ''}")
            for edit in verdict.get("edits") or []:
                lines.append(f"    - edit: {edit}")
        for revision in step.get("revisions") or []:
            lines.append(f"- **revision** id={revision.get('id')} "
                         f"seq={revision.get('seq')} supersedes={revision.get('supersedes')} "
                         f"verdict={revision.get('verdict')} reason={revision.get('reason')}")
        lines.append("")
    events = trace["events"]
    lines.append("## Turn events (impact)")
    lines.append("")
    narrative = events.get("narrative")
    if narrative:
        lines.append(f"- **narrative** seq={narrative['seq']} "
                     f"prose={narrative['prose_chars']} chars dsl={narrative['dsl_chars']} chars")
        if narrative.get("facts_text_chars"):
            lines.append(f"    - injected facts text: {narrative['facts_text_chars']} chars")
        if narrative.get("regions"):
            lines.append(f"    - prompt regions: {json.dumps(narrative['regions'])}")
    for group in ("validation_flags", "critic_verdicts", "revisions", "errors"):
        rows = events.get(group) or []
        if rows:
            lines.append(f"- **{group}**: {json.dumps(rows)[:2000]}")
    for group in ("state_delta", "world_state", "env_hud", "attribution"):
        value = events.get(group)
        if value:
            lines.append(f"- **{group}**: {json.dumps(value)[:1200]}")
    if trace.get("notes"):
        lines.append("")
        lines.append("## Notes")
        for note in trace["notes"]:
            lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _write_artifacts(out_root: str, session_id: str, turn: int, trace: dict) -> tuple[Path, Path]:
    """Atomically write ``turn-NN.json`` + ``turn-NN.md`` under the artifact root."""
    directory = Path(out_root) / session_id
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"turn-{turn:02d}.json"
    md_path = directory / f"turn-{turn:02d}.md"
    _atomic_write(json_path, json.dumps(trace, indent=2, ensure_ascii=False))
    _atomic_write(md_path, render_markdown(trace))
    return json_path, md_path


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# live run — one turn through the real engine
# ---------------------------------------------------------------------------


async def run_one_turn(
    engine: Any,
    log: EventLog,
    session_id: str,
    turn: int,
    kind: str,
    content: str,
) -> dict:
    """Drive one real turn, timestamp the stage frames, then assemble the trace."""
    pre_count = len(log.get_events(session_id))
    frames: list[StageFrame] = []
    first_reasoning_s: float | None = None
    first_content_s: float | None = None
    error: str | None = None
    t0 = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    async for event in engine.run_turn(session_id, content, kind):
        elapsed = time.monotonic() - t0
        etype = event.get("type")
        if etype == "stage":
            meta = event.get("meta") or {}
            frames.append(
                StageFrame(
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
    total_s = time.monotonic() - t0

    events = log.get_events(session_id)[pre_count:]
    calls = collect_calls(events)
    summary = summarize_turn_events(events)
    if error:
        summary["errors"].append({"event": "turn_error", "detail": error})

    trace = build_trace(
        session_id=session_id,
        turn=turn,
        kind=kind,
        user_content=content,
        wall_started_at=started_at,
        total_s=total_s,
        frames=frames,
        calls=calls,
        events=summary,
    )
    trace["first_reasoning_s"] = round(first_reasoning_s, 3) if first_reasoning_s is not None else None
    trace["first_content_s"] = round(first_content_s, 3) if first_content_s is not None else None
    return trace


def _print_turn_summary(trace: dict, json_path: Path, md_path: Path) -> None:
    print(f"{BOLD}[trace] turn {trace['turn']} [{trace['kind']}]  "
          f"{trace['total_s']}s total{RESET}")
    for row in trace["headline"]:
        color = CYAN if row["kind"] == "model" else DIM
        print(f"  {color}{row['step']:<12} {row['kind']:<13} "
              f"{row['duration_s']:>7.2f}s {row['pct_of_turn']:>5.1f}%{RESET}")
    ttft = trace.get("first_content_s")
    if ttft is not None:
        print(f"  {DIM}first content token at {ttft}s{RESET}")
    tokens = trace["tokens"]
    print(f"  {DIM}tokens: {tokens['prompt_tokens']}+"
          f"{tokens['completion_tokens']}={tokens['total_tokens']}{RESET}")
    for error in trace["events"].get("errors") or []:
        print(f"  {RED}error: {error}{RESET}")
    print(f"  {GREEN}wrote {json_path}{RESET}")
    print(f"  {GREEN}wrote {md_path}{RESET}")


async def run_turns_mode(args: argparse.Namespace) -> int:
    """Default mode: drive N real turns and write an artifact per turn."""
    registry = configure(args.config)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = EventLog(log_path)
    # D17: capture ON for this script only — the point of a diagnostic run.
    engine = build_engine(registry=registry, log=log, agent_debug=True)
    session_id = f"trace-{uuid.uuid4().hex[:8]}"
    log.append_event(
        session_id, "system", "human",
        {"event": "session_created", "title": "turn trace"},
    )
    print(f"{BOLD}trace session {session_id}{RESET}  "
          f"(narrator: {registry.connection_for('narrator').model})")
    turns = DEFAULT_TURNS[: max(1, min(args.turns, len(DEFAULT_TURNS)))]
    if args.turns > len(DEFAULT_TURNS):
        print(f"{YELLOW}--turns {args.turns} exceeds the {len(DEFAULT_TURNS)} scripted "
              f"beats; running {len(DEFAULT_TURNS)}{RESET}")
    for i, (kind, content) in enumerate(turns, 1):
        trace = await run_one_turn(engine, log, session_id, i, kind, content)
        json_path, md_path = _write_artifacts(args.out, session_id, i, trace)
        _print_turn_summary(trace, json_path, md_path)
    log.close()
    return 0


# ---------------------------------------------------------------------------
# single-interaction mode
# ---------------------------------------------------------------------------

_SAMPLE_USER = "I push through the saloon doors, soaked from the rain."
_SAMPLE_NARRATIVE = (
    "The doors give way with a groan. Warm lamplight spills across the wet boards, "
    "and the harbormaster looks up from his ledger.\n\n"
    '"You again," he says.'
)


def assemble_interaction_messages(
    interaction: str,
    *,
    preset: Any,
    user_msg: str,
    narrative: str,
    prior_state: dict | None = None,
    checkpoint: bool = False,
    prior_prose: list[str] | None = None,
    state_line: str = "",
) -> tuple[list[dict], dict]:
    """Build the EXACT messages an interaction's flow would send (D18/D23).

    Reproduces the engine's per-interaction flow compile (``compile_preset_flow``)
    using the same base message builders the wiring closures use; returns
    ``(messages, evidence)`` where ``evidence`` is the pre-render bundle (empty
    for the non-critic interactions). Pure apart from the preset read.
    """
    from fabulist.prompts.preset_store import compile_preset_flow

    if interaction == "extract":
        from fabulist.state.extractor import EXTRACTOR_SYSTEM_PROMPT, _turn_block

        parts = {
            "system": EXTRACTOR_SYSTEM_PROMPT,
            "turn_payload": _turn_block(user_msg, narrative),
            "variables": {"message": user_msg, "last_narrative": narrative},
        }
        return compile_preset_flow(preset, "extract", parts), {}

    if interaction == "bts":
        from fabulist.state.bts import BTS_SYSTEM_PROMPT, build_bts_messages

        base = build_bts_messages(user_msg, narrative, prior_state, checkpoint=checkpoint)
        parts = {
            "system": str(base[0].get("content", BTS_SYSTEM_PROMPT)),
            "payload": str(base[1].get("content", "")) if len(base) > 1 else "",
            "variables": {},
        }
        return compile_preset_flow(preset, "bts", parts), {"prior_state": prior_state or {},
                                                           "checkpoint": checkpoint}

    if interaction == "critic":
        from fabulist.graph.nodes import segment_narrative
        from fabulist.graph.validator import ValidatorConfig, validate_narrative
        from fabulist.prompts.critic import CRITIC_SYSTEM_PROMPT
        from fabulist.wiring import _render_critic_payload

        segments = segment_narrative(narrative)
        report = validate_narrative(user_msg, segments, ValidatorConfig())
        flags = [dataclasses.asdict(flag) for flag in report.flags]
        evidence = {
            "user_message": user_msg,
            "prose": narrative,
            "flags": flags,
            "prior_prose": list(prior_prose or []),
            "state_line": state_line,
        }
        parts = {
            "system": CRITIC_SYSTEM_PROMPT,
            "payload": _render_critic_payload(evidence),
            "variables": {},
        }
        return compile_preset_flow(preset, "critic", parts), evidence

    raise ValueError(f"unknown interaction: {interaction!r}")


def _print_interaction(messages: list[dict], evidence: dict, raw: str, agent: str,
                       model: str, latency_s: float, notes: list[str]) -> None:
    print(f"{BOLD}=== isolated interaction ==={RESET}")
    for note in notes:
        print(f"{YELLOW}note: {note}{RESET}")
    print(f"{CYAN}agent={agent} model={model} messages={len(messages)} "
          f"latency={latency_s:.2f}s{RESET}")
    for i, message in enumerate(messages, 1):
        content = message.get("content")
        chars = len(content) if isinstance(content, str) else len(json.dumps(content))
        print(f"{BOLD}--- message {i}: role={message.get('role')} ({chars} chars) ---{RESET}")
        print(content if isinstance(content, str) else json.dumps(content, indent=2))
    if evidence:
        print(f"{BOLD}--- evidence bundle ---{RESET}")
        print(json.dumps(evidence, indent=2, ensure_ascii=False))
    print(f"{BOLD}--- raw output ---{RESET}")
    print(raw)


async def run_interaction_mode(args: argparse.Namespace) -> int:
    """Run ONE interaction in isolation and print its assembled input/output."""
    from fabulist.agents.debug import CURRENT_INTERACTION, CURRENT_SESSION
    from fabulist.graph.types import collect_text
    from fabulist.prompts.agent_sets import bindings_for, resolve_agent
    from fabulist.prompts.preset_store import active_preset

    interaction = args.interaction
    registry = configure(args.config)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        EventLog(log_path).close()
    log = EventLog(log_path)

    user_msg = args.user_message or _SAMPLE_USER
    narrative = args.narrative or _SAMPLE_NARRATIVE
    notes: list[str] = []
    prior_state: dict | None = None
    prior_prose: list[str] = []
    state_line = ""
    checkpoint = False

    if args.session:
        events = log.get_events(args.session)
        if not events:
            notes.append(f"session {args.session!r} has no events in {log_path}")
        if interaction == "bts":
            from fabulist.state.bts import latest_world_state

            prior_state = latest_world_state(log, args.session).get("state") or {}
            notes.append("prior_state read from the session's latest world_state event")
        if interaction == "critic":
            from fabulist.graph.nodes import _critic_state_line

            prior_prose = [
                str(e.payload.get("prose") or "")
                for e in events
                if e.type == "narrative"
            ][-2:]
            state_line = _critic_state_line(log, args.session, {})
            notes.append("prior-prose tail + state line read from the session log")
    else:
        if interaction == "critic":
            notes.append(
                "no --session: validator flags are computed deterministically from "
                "the given prose, but the PRIOR-PROSE tail and STATE line are EMPTY "
                "(they need a session log) — pass --session for a byte-faithful bundle"
            )
        if interaction == "bts":
            notes.append("no --session: prior world-state is empty (first BTS call)")

    preset = active_preset(log)
    messages, evidence = assemble_interaction_messages(
        interaction,
        preset=preset,
        user_msg=user_msg,
        narrative=narrative,
        prior_state=prior_state,
        checkpoint=checkpoint,
        prior_prose=prior_prose,
        state_line=state_line,
    )
    agent = resolve_agent(bindings_for(log), interaction, fallback=_POST_TURN_FALLBACK)
    model = registry.connection_for(agent).model
    # Isolated diagnostic: no session set, so capture/usage write nothing.
    CURRENT_SESSION.set("")
    CURRENT_INTERACTION.set(interaction)
    caller = make_agent_caller(agent, registry, log, agent_debug_default=True)
    started = time.monotonic()
    raw = await collect_text(caller, messages)
    latency_s = time.monotonic() - started
    _print_interaction(messages, evidence, raw, agent, model, latency_s, notes)
    log.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.interaction:
        return asyncio.run(run_interaction_mode(args))
    return asyncio.run(run_turns_mode(args))


if __name__ == "__main__":
    sys.exit(main())
