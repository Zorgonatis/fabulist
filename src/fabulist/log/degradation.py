"""Degradation notices (issue #34): the log row a swallowed optional-service failure leaves.

Why this module exists
----------------------
Every optional service around a turn is best-effort: memory recall, the NPC lane
strategy, the post-turn hooks and the memory indexer all degrade rather than fail
the turn. The failure used to reach stderr only, so a turn produced prose while
two of four model services were dead and nothing on screen said so (issue #34:
the visible symptom — odd prose, a lane that did not move, a memory that
"forgot" — reads like a model problem instead of a wiring problem).

The ruling (owner, 2026-10-23): a small prompting highlight on the Inspector
button plus a place to read the actual failures. That surface needs a row it can
read, and the event log owns everything (ADR-001/D9) — so each swallow-site
appends ONE typed ``system`` variant here:

* :data:`RECALL_DEGRADED_EVENT` — pre-turn memory recall failed; the turn ran
  without memories (``wiring.recall_fn``).
* :data:`LANE_DEGRADED_EVENT` — the ``decision`` model could not answer the
  dispatch question; the lane SET fell back to the full ladder
  (``wiring._decision_observer``, the one seam every decision ``ask`` crosses).
* :data:`INDEX_DEGRADED_EVENT` — the memory indexer could not embed this turn, so
  it is not in the vector index yet (the ``memory_hook`` post-turn hook).
* :data:`HOOK_DEGRADED_EVENT` — any other post-turn hook raised (assistant
  consult, historian spawn, context monitor).
* :data:`PRESET_DEGRADED_EVENT` — a preset/context read raised, so the turn
  fell back to code defaults (issue #70: a silent wrong-model or
  missing-context turn reads like a model problem).

Payload shape (the one the Inspector notices tab renders)::

    {"event": "recall_degraded",
     "stage": "memory_recall",              # WHICH step degraded
     "reason": "recall failed (ConnectError: ...) — ...",   # short, actionable
     "turn": 42}                            # seq of the newest event at emit time

``turn`` is seq context, not a foreign key: it is the newest event seq when the
row was appended (the user message for a pre-turn failure, the narrative for a
post-turn one), which is exactly what lets a reader anchor the notice to a turn.
A failure report is never allowed to break the turn it describes, so every append
here is best-effort (:func:`append_degradation` returns ``False`` instead of
raising).
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "DEGRADATION_EVENTS",
    "HOOK_DEGRADED_EVENT",
    "INDEX_DEGRADED_EVENT",
    "LANE_DEGRADED_EVENT",
    "PRESET_DEGRADED_EVENT",
    "RECALL_DEGRADED_EVENT",
    "REASON_LIMIT",
    "append_degradation",
    "append_preset_degraded",
    "degradation_reason",
    "degradation_text",
]

#: Pre-turn recall failed; the turn continued without recalled context.
RECALL_DEGRADED_EVENT = "recall_degraded"

#: The lane strategy could not answer; the lane set fell back to the full ladder.
LANE_DEGRADED_EVENT = "lane_degraded"

#: The memory indexer failed for this turn; it is not searchable yet.
INDEX_DEGRADED_EVENT = "index_degraded"

#: A post-turn housekeeping hook failed (assistant / historian / context monitor).
HOOK_DEGRADED_EVENT = "hook_degraded"

#: A preset/context read failed; the turn fell back to the code defaults (#70).
PRESET_DEGRADED_EVENT = "preset_degraded"

#: Every name :func:`append_degradation` accepts (the Inspector's read set).
DEGRADATION_EVENTS = frozenset(
    {
        RECALL_DEGRADED_EVENT,
        LANE_DEGRADED_EVENT,
        INDEX_DEGRADED_EVENT,
        HOOK_DEGRADED_EVENT,
        PRESET_DEGRADED_EVENT,
    }
)

#: Cap on the reason text a notice carries (one short line, never a traceback).
REASON_LIMIT = 400

#: Stage labels the engine emits, exported so tests and readers name one spelling.
STAGES: dict[str, str] = {
    RECALL_DEGRADED_EVENT: "memory_recall",
    LANE_DEGRADED_EVENT: "npc_lane_strategy",
    INDEX_DEGRADED_EVENT: "memory_index",
    HOOK_DEGRADED_EVENT: "post_turn_hook",
    PRESET_DEGRADED_EVENT: "preset_resolution",
}


def degradation_text(exc: BaseException | str | None, limit: int = REASON_LIMIT) -> str:
    """``type: message`` for one failure, clipped to ``limit`` chars.

    An exception's class name is the actionable half of the reason: a
    ``ConnectError`` says "the service is unreachable" where a bare message line
    often says nothing at all.
    """
    if exc is None:
        return ""
    if isinstance(exc, BaseException):
        message = str(exc).strip()
        label = type(exc).__name__
        text = f"{label}: {message}" if message else label
    else:
        text = str(exc).strip()
    return text[:limit]


def degradation_reason(
    exc: BaseException | str | None, advice: str = "", limit: int = REASON_LIMIT
) -> str:
    """A short actionable reason: what broke, then what to check.

    ``advice`` is the reader's next move ("check the embedding connection"), and
    it is what makes the notice actionable rather than a second log line.
    """
    text = degradation_text(exc, limit=max(1, limit - len(advice) - 3))
    if not text:
        text = "failed"
    return f"{text} — {advice}"[:limit] if advice else text


def append_degradation(
    log: Any,
    session_id: str,
    event: str,
    *,
    stage: str,
    reason: str,
    turn: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> bool:
    """Append ONE degradation ``system`` row; never raises.

    ``turn`` rides as seq context: the caller may pass the seq it already knows
    (a post-turn hook's failure row does), and otherwise this reads the newest
    event seq for the session — one small read on a failure path, which is the
    only place it costs anything.

    Returns ``True`` when the row landed. A log that refuses the append is
    logged and swallowed: reporting a degradation must never become one.
    """
    if event not in DEGRADATION_EVENTS:
        raise ValueError(f"unknown degradation event: {event!r}")
    payload: dict[str, Any] = {
        "event": event,
        "stage": stage,
        "reason": str(reason)[:REASON_LIMIT],
    }
    if turn is None:
        try:
            events = log.get_events(session_id)
            turn = int(events[-1].seq) if events else None
        except Exception:  # noqa: BLE001 - seq context is optional
            turn = None
    if isinstance(turn, int) and not isinstance(turn, bool):
        payload["turn"] = turn
    if extra:
        payload.update(dict(extra))
    try:
        log.append_event(session_id, "system", "system", payload)
    except Exception:  # noqa: BLE001 - failure reporting is best-effort
        return False
    return True


def append_preset_degraded(
    log: Any,
    session_id: str | None,
    *,
    stage: str,
    exc: BaseException | str | None,
    advice: str = "",
) -> bool:
    """Append ONE ``preset_degraded`` row for a swallowed preset/context read.

    Issue #70: a failed preset resolution silently changes which model answers
    and what context the prompt carries, which reads on screen as a model
    problem. The caller logs its own warning (one per handler, named for the
    interaction); this writes the log row the Inspector notices tab renders.

    Two guards live here so no site has to remember them:

    * an empty session id cannot anchor a row;
    * an ANCHOR session (``__presets__`` / ``__worlds__`` / ``__books__`` …) is
      definition storage, not a turn — a preset read failure there must never
      append to it.
    """
    if not session_id or str(session_id).startswith("__"):
        return False
    return append_degradation(
        log,
        str(session_id),
        PRESET_DEGRADED_EVENT,
        stage=stage,
        reason=degradation_reason(exc, advice),
    )
