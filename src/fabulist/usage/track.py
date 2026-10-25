"""Per-agent usage: process tally + **log persistence** (R8, D23/D25).

This module measures *how much* each agent and model is used — call counts,
token counts and latency — and (I1, owner decision 2026-10-10) persists every
call as a ``system``/``usage`` event so the numbers survive a restart. The
event log is the source of truth (D9): :func:`usage_from_log` reads it back
aggregated per session / agent / model, and ``GET /api/usage`` serves that view.

Two layers, one write path
--------------------------
* :class:`UsageTracker` is the legacy in-RAM accumulator kept for the live
  process view and for callers that have no log handle. Its per-session tally is
  now actually correct because the capture site threads the real session id.
* :func:`record_usage` is the capture-point helper: it updates the RAM tally
  **and** appends one validated usage event through :func:`append_usage_event`.
  An append failure never breaks a turn (usage is measurement, D23).

Typical use::

    from fabulist.usage.track import record_usage, usage_from_log

    record_usage(log, session_id=sid, agent="narrator", model="your-model-id",
                 prompt_tokens=812, completion_tokens=240, latency_s=1.83)
    aggregate = usage_from_log(log)          # {"agents": ..., "models": ...}

Concurrency
-----------
``UsageTracker`` is safe to share across threads and asyncio tasks: every
mutation and snapshot runs under a single :class:`threading.Lock`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from fabulist.log.schema_gate import (
    LogSchemaError,
    USAGE_EVENT,
    validate_usage_payload,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog
    from fabulist.log.events import Event

logger = logging.getLogger(__name__)

__all__ = [
    "UsageTracker",
    "get_tracker",
    "reset_tracker",
    "USAGE_EVENT",
    "usage_payload",
    "append_usage_event",
    "record_usage",
    "aggregate_usage",
    "usage_from_log",
]

# Latencies are summed as floats; round the derived average so callers see a
# clean value (e.g. 0.15 rather than 0.15000000000000002).
_LATENCY_PRECISION = 6


class UsageTracker:
    """Thread-safe in-memory accumulator of per-agent / per-model usage.

    The tracker stores three aggregate views derived from the same stream of
    :meth:`record` calls:

    * ``agents``  — per agent: calls, prompt/completion/total tokens, avg latency
    * ``models``  — per model: calls and total tokens
    * ``total``   — overall calls and total tokens

    A per-session tally is kept internally for future per-session reporting but
    is not (yet) part of :meth:`snapshot`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, dict[str, float]] = {}
        self._models: dict[str, dict[str, float]] = {}
        self._sessions: dict[str, dict[str, float]] = {}
        self._total_calls = 0
        self._total_tokens = 0

    def record(
        self,
        agent: str,
        model: str,
        session_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_s: float,
    ) -> None:
        """Accumulate one call's usage for ``agent``/``model`` in ``session_id``.

        All counters update atomically under the tracker lock. ``session_id`` is
        recorded in an internal per-session tally that :meth:`snapshot` does not
        expose yet.
        """
        total = int(prompt_tokens) + int(completion_tokens)
        latency = float(latency_s)

        with self._lock:
            agent_stats = self._agents.setdefault(
                agent,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "latency_sum": 0.0,
                },
            )
            agent_stats["calls"] += 1
            agent_stats["prompt_tokens"] += int(prompt_tokens)
            agent_stats["completion_tokens"] += int(completion_tokens)
            agent_stats["total_tokens"] += total
            agent_stats["latency_sum"] += latency

            model_stats = self._models.setdefault(
                model, {"calls": 0, "total_tokens": 0}
            )
            model_stats["calls"] += 1
            model_stats["total_tokens"] += total

            session_stats = self._sessions.setdefault(
                session_id, {"calls": 0, "total_tokens": 0}
            )
            session_stats["calls"] += 1
            session_stats["total_tokens"] += total

            self._total_calls += 1
            self._total_tokens += total

    def snapshot(self) -> dict[str, Any]:
        """Return a deep copy of the current aggregates.

        The returned structure is detached from internal state, so callers may
        mutate it freely. Shape::

            {
                "agents": {
                    "<agent>": {
                        "calls": int,
                        "prompt_tokens": int,
                        "completion_tokens": int,
                        "total_tokens": int,
                        "avg_latency_s": float,
                    },
                    ...
                },
                "models": {"<model>": {"calls": int, "total_tokens": int}, ...},
                "total": {"calls": int, "total_tokens": int},
            }
        """
        with self._lock:
            agents = {
                name: {
                    "calls": int(stats["calls"]),
                    "prompt_tokens": int(stats["prompt_tokens"]),
                    "completion_tokens": int(stats["completion_tokens"]),
                    "total_tokens": int(stats["total_tokens"]),
                    "avg_latency_s": round(
                        stats["latency_sum"] / stats["calls"],
                        _LATENCY_PRECISION,
                    ),
                }
                for name, stats in self._agents.items()
            }
            models = {
                name: {
                    "calls": int(stats["calls"]),
                    "total_tokens": int(stats["total_tokens"]),
                }
                for name, stats in self._models.items()
            }
            total = {
                "calls": int(self._total_calls),
                "total_tokens": int(self._total_tokens),
            }
        return {"agents": agents, "models": models, "total": total}

    def reset(self) -> None:
        """Drop every accumulated counter, returning the tracker to empty."""
        with self._lock:
            self._agents.clear()
            self._models.clear()
            self._sessions.clear()
            self._total_calls = 0
            self._total_tokens = 0


_TRACKER: UsageTracker | None = None
_TRACKER_LOCK = threading.Lock()


def get_tracker() -> UsageTracker:
    """Return the process-wide :class:`UsageTracker`, creating it on first use."""
    global _TRACKER
    if _TRACKER is None:
        with _TRACKER_LOCK:
            if _TRACKER is None:
                _TRACKER = UsageTracker()
    return _TRACKER


def reset_tracker() -> None:
    """Drop the process-wide tracker so the next :func:`get_tracker` builds fresh.

    Intended for tests and for a future "new session" boundary; production code
    normally only needs :meth:`UsageTracker.reset`.
    """
    global _TRACKER
    with _TRACKER_LOCK:
        _TRACKER = None


# ---------------------------------------------------------------------------
# log persistence (I1) — the usage event is the source of truth
# ---------------------------------------------------------------------------


def usage_payload(
    *,
    agent: str,
    model: str,
    session_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_s: float,
    seq: int | None = None,
) -> dict[str, Any]:
    """Build one ``system``/``usage`` payload (the durable per-call record).

    ``total_tokens`` is derived from the prompt/completion counts so a caller
    cannot publish an inconsistent sum. Every field is coerced to its declared
    type (see :data:`fabulist.log.schema_gate.USAGE_SCHEMA`) so the payload
    always passes the gate; ``seq`` is optional turn-context and is omitted
    entirely when unknown rather than stored as ``None``.
    """
    prompt = int(prompt_tokens)
    completion = int(completion_tokens)
    payload: dict[str, Any] = {
        "event": USAGE_EVENT,
        "agent": str(agent),
        "model": str(model),
        "session_id": str(session_id),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "latency_s": float(latency_s),
    }
    if seq is not None:
        payload["seq"] = int(seq)
    return payload


def append_usage_event(
    log: "EventLog",
    *,
    session_id: str,
    agent: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_s: float,
    seq: int | None = None,
) -> "Event":
    """Append one validated usage event to ``session_id`` and return it.

    Raises :class:`fabulist.log.schema_gate.LogSchemaError` when the payload
    fails the gate (a programmer error — :func:`usage_payload` always produces a
    valid shape). Callers at a live capture point should prefer
    :func:`record_usage`, which never lets a usage failure break a turn.
    """
    payload = usage_payload(
        agent=agent,
        model=model,
        session_id=session_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_s=latency_s,
        seq=seq,
    )
    errors = validate_usage_payload(payload)
    if errors:
        raise LogSchemaError("invalid usage event: " + "; ".join(errors))
    return log.append_event(session_id, "system", str(agent), payload)


def record_usage(
    log: "EventLog | None",
    *,
    session_id: str,
    agent: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_s: float,
    seq: int | None = None,
) -> None:
    """Capture-point helper: update the RAM tally and persist to the log.

    Used by the model-call sites. Persistence is best-effort by design (D23:
    measurement must never break generation): an append failure is logged and
    swallowed, while the in-RAM tally still records the call. ``log`` may be
    ``None`` (a caller without a log handle) — then only the RAM tally updates.
    """
    get_tracker().record(
        agent, model, session_id, prompt_tokens, completion_tokens, latency_s
    )
    # No session context (e.g. a post-turn hook outside a turn) or no log:
    # the RAM tally still records the call, but there is nowhere meaningful to
    # persist it — never write usage onto the empty/reserved session.
    if log is None or not session_id:
        return
    try:
        append_usage_event(
            log,
            session_id=session_id,
            agent=agent,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_s=latency_s,
            seq=seq,
        )
    except Exception:  # noqa: BLE001 - usage is measurement, never fatal
        logger.warning(
            "usage persistence failed for agent=%s session=%s", agent, session_id,
            exc_info=True,
        )


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce ``value`` to int, falling back to ``default`` (bool is not int)."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to float, falling back to ``default``."""
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bucket() -> dict[str, float]:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "latency_sum": 0.0,
    }


def _finalize(bucket: dict[str, float], *, with_avg: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "calls": int(bucket["calls"]),
        "prompt_tokens": int(bucket["prompt_tokens"]),
        "completion_tokens": int(bucket["completion_tokens"]),
        "total_tokens": int(bucket["total_tokens"]),
    }
    if with_avg:
        calls = int(bucket["calls"])
        out["avg_latency_s"] = (
            round(bucket["latency_sum"] / calls, _LATENCY_PRECISION) if calls else 0.0
        )
    return out


def aggregate_usage(events: "list[Event]") -> dict[str, Any]:
    """Aggregate ``system``/``usage`` events into a session/agent/model view.

    Pure over the event list, so it is unit-testable without a log. Shape::

        {
          "calls": int,
          "prompt_tokens": int, "completion_tokens": int, "total_tokens": int,
          "agents":  {agent:  {calls, prompt_tokens, completion_tokens,
                               total_tokens, avg_latency_s}},
          "models":  {model:  {calls, prompt_tokens, completion_tokens,
                               total_tokens}},
          "sessions": {sid:   {calls, prompt_tokens, completion_tokens,
                               total_tokens}},
        }

    Rows without the usage marker are ignored; a malformed numeric field counts
    as ``0`` (D4: a hand-edited log must never crash the reader).
    """
    agents: dict[str, dict[str, float]] = {}
    models: dict[str, dict[str, float]] = {}
    sessions: dict[str, dict[str, float]] = {}
    total = _bucket()

    for event in events:
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict) or payload.get("event") != USAGE_EVENT:
            continue
        prompt = _as_int(payload.get("prompt_tokens"))
        completion = _as_int(payload.get("completion_tokens"))
        total_tokens = _as_int(payload.get("total_tokens"), prompt + completion)
        if total_tokens == 0:
            total_tokens = prompt + completion
        latency = _as_float(payload.get("latency_s"))
        agent = str(payload.get("agent") or "")
        model = str(payload.get("model") or "")
        sid = str(payload.get("session_id") or event.session_id)

        for target, key in (
            (agents, agent),
            (models, model),
            (sessions, sid),
            (total, None),
        ):
            bucket = target if key is None else target.setdefault(key, _bucket())
            bucket["calls"] += 1
            bucket["prompt_tokens"] += prompt
            bucket["completion_tokens"] += completion
            bucket["total_tokens"] += total_tokens
            bucket["latency_sum"] += latency

    return {
        "calls": int(total["calls"]),
        "prompt_tokens": int(total["prompt_tokens"]),
        "completion_tokens": int(total["completion_tokens"]),
        "total_tokens": int(total["total_tokens"]),
        "agents": {a: _finalize(b, with_avg=True) for a, b in agents.items()},
        "models": {m: _finalize(b, with_avg=False) for m, b in models.items()},
        "sessions": {s: _finalize(b, with_avg=False) for s, b in sessions.items()},
    }


def _session_ids(log: "EventLog | None") -> list[str]:
    """Every non-reserved session id known to the log (secondary connection).

    Mirrors ``fabulist.api.sessions._session_ids``: the fork-bookkeeping
    ``sessions`` table is written on a session's first event, and WAL makes the
    short-lived read connection safe. Ids with the reserved ``__`` prefix (the
    settings anchor) are internal bookkeeping, never chats.
    """
    if log is None:
        return []
    try:
        conn = sqlite3.connect(log.path)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [str(row[0]) for row in rows if not str(row[0]).startswith("__")]


def usage_from_log(
    log: "EventLog | None", session_id: str | None = None
) -> dict[str, Any]:
    """Aggregate persisted usage from the log (the source of truth, D9).

    With ``session_id`` the view covers that session's own rows; without it,
    every non-reserved session's rows are folded together. A missing log (or a
    log with no usage rows yet) yields the empty aggregate, never an error.
    """
    if log is None:
        return aggregate_usage([])
    if session_id is not None:
        return aggregate_usage(log.get_events(session_id))
    events: list["Event"] = []
    for sid in _session_ids(log):
        events.extend(log.get_events(sid))
    return aggregate_usage(events)
