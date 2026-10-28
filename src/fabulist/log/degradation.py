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
* :data:`WORLD_INFO_LARGE_EVENT` — the turn's rendered world-info block is very
  large (issue #83). This is an ADVISORY, not a failure: the block is sent in
  full because user-provided lore is never trimmed. The row exists to make the
  size visible and never changes a prompt byte.

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

from typing import Any, Mapping, Sequence

__all__ = [
    "DEGRADATION_EVENTS",
    "HOOK_DEGRADED_EVENT",
    "INDEX_DEGRADED_EVENT",
    "LANE_DEGRADED_EVENT",
    "PRESET_DEGRADED_EVENT",
    "RECALL_DEGRADED_EVENT",
    "REASON_LIMIT",
    "WORLD_INFO_ADVISORY_ENTRY_LIMIT",
    "WORLD_INFO_LARGE_EVENT",
    "append_degradation",
    "append_preset_degraded",
    "append_world_info_large",
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

#: The turn's world-info block is very large; it is sent UNCAPPED (#83).
WORLD_INFO_LARGE_EVENT = "world_info_large"

#: Every name :func:`append_degradation` accepts (the Inspector's read set).
DEGRADATION_EVENTS = frozenset(
    {
        RECALL_DEGRADED_EVENT,
        LANE_DEGRADED_EVENT,
        INDEX_DEGRADED_EVENT,
        HOOK_DEGRADED_EVENT,
        PRESET_DEGRADED_EVENT,
        WORLD_INFO_LARGE_EVENT,
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
    WORLD_INFO_LARGE_EVENT: "world_info_render",
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


#: How many active entry names the ``world_info_large`` advisory lists.
WORLD_INFO_ADVISORY_ENTRY_LIMIT = 5

#: Longest one listed entry name may be inside the reason line.
_WORLD_INFO_ADVISORY_NAME_LIMIT = 30


def _world_info_advisory_reason(
    chars: int,
    threshold: int,
    active: int,
    total: int,
    entries: list[str],
) -> str:
    """The advisory's short reason: size, ratio, up to five names, one action.

    Plain short sentences (issue #95). The names are dropped one at a time
    only when the sentence would not fit :data:`REASON_LIMIT`; the count
    sentence always stays, so the reason reads whole.
    """
    total = max(total, active)
    percent = round(active * 100 / total) if total else 0
    head = (
        f"World info is {chars} characters. That is over the "
        f"{threshold}-character advisory size. {active} of {total} entries "
        f"are active ({percent}%)."
    )
    tail = " The block is sent in full. Turn off entries that you do not need."
    names = [
        name
        if len(name) <= _WORLD_INFO_ADVISORY_NAME_LIMIT
        else name[: _WORLD_INFO_ADVISORY_NAME_LIMIT - 1] + "\u2026"
        for name in entries[:WORLD_INFO_ADVISORY_ENTRY_LIMIT]
    ]
    while True:
        if not names:
            return (head + tail)[:REASON_LIMIT]
        listing = ", ".join(names)
        if active > len(names):
            listing += f" (+{active - len(names)} more)"
        reason = f"{head} Active entries: {listing}.{tail}"
        if len(reason) <= REASON_LIMIT:
            return reason
        names.pop()


def append_world_info_large(
    log: Any,
    session_id: str | None,
    chars: int,
    *,
    threshold: int,
    active: int = 0,
    total: int = 0,
    entries: Sequence[str] = (),
) -> bool:
    """Append ONE ``world_info_large`` advisory per turn; never raises.

    Issue #83: the world-info budget is gone, so a huge activated book reaches
    the prompt in full. That is the ruling (user-provided content is never
    trimmed) — this row only makes the size visible in the Inspector notices
    tab, beside the real failure notices. It is an ADVISORY: nothing failed,
    nothing was dropped, and the prompt bytes are untouched.

    Issue #95: the row carries the ratio of active entries to all entries and
    the first five active entry names, so the reader can see WHICH entries
    filled the block. The names and the counts ride the ``extra`` payload
    (``active`` / ``total`` / ``entries``); the reason line states the size,
    the ratio, up to five names and ONE action.

    At most ONE row per turn: the anchor is the session's newest ``user`` event
    seq, so the several renders one turn performs (the ``{{facts}}`` / ``{{wi}}``
    macros plus the builder) collapse to a single notice. A render with no user
    event (a preview, a replay, a definition read) emits nothing — that is not
    a turn and must not leave a row.
    """
    if not session_id or str(session_id).startswith("__"):
        return False
    try:
        events = log.get_events(str(session_id))
    except Exception:  # noqa: BLE001 - the advisory is best-effort
        return False
    turn: int | None = None
    for event in events:
        if getattr(event, "type", None) == "user":
            turn = int(event.seq)
    if turn is None:
        return False
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("event") != WORLD_INFO_LARGE_EVENT:
            continue
        if payload.get("turn") == turn:
            return False
    return append_degradation(
        log,
        str(session_id),
        WORLD_INFO_LARGE_EVENT,
        stage=STAGES[WORLD_INFO_LARGE_EVENT],
        reason=_world_info_advisory_reason(
            chars, threshold, active, total, list(entries)
        ),
        turn=turn,
        extra={
            "chars": chars,
            "threshold": threshold,
            "active": active,
            "total": max(total, active),
            "entries": list(entries)[:WORLD_INFO_ADVISORY_ENTRY_LIMIT],
        },
    )
