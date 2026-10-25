"""Tests for per-agent usage tracking (R8 measurement half).

Pure in-memory: no event log, no filesystem, no network. The tracker is
exercised both directly and under concurrent-ish load (``asyncio.gather`` and
real threads) to confirm the single-lock accumulation is race-free.
"""

from __future__ import annotations

import asyncio
import threading

from fabulist.usage.track import UsageTracker, get_tracker, reset_tracker


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# aggregation math
# ---------------------------------------------------------------------------


def test_empty_snapshot():
    snapshot = UsageTracker().snapshot()
    assert snapshot == {
        "agents": {},
        "models": {},
        "total": {"calls": 0, "total_tokens": 0},
    }


def test_single_record_aggregates():
    tracker = UsageTracker()
    tracker.record("narrator", "gemma-4", "s1", 812, 240, 1.5)

    snapshot = tracker.snapshot()
    assert snapshot["agents"]["narrator"] == {
        "calls": 1,
        "prompt_tokens": 812,
        "completion_tokens": 240,
        "total_tokens": 1052,
        "avg_latency_s": 1.5,
    }
    assert snapshot["models"]["gemma-4"] == {"calls": 1, "total_tokens": 1052}
    assert snapshot["total"] == {"calls": 1, "total_tokens": 1052}


def test_multi_agent_and_model_math():
    tracker = UsageTracker()
    # narrator: two calls on the same model
    tracker.record("narrator", "gemma-4", "s1", 100, 50, 1.0)
    tracker.record("narrator", "gemma-4", "s1", 200, 25, 3.0)
    # extractor: one call on a different model
    tracker.record("extractor", "qwen-3", "s1", 40, 10, 0.5)

    snapshot = tracker.snapshot()

    assert snapshot["agents"]["narrator"] == {
        "calls": 2,
        "prompt_tokens": 300,
        "completion_tokens": 75,
        "total_tokens": 375,
        "avg_latency_s": 2.0,
    }
    assert snapshot["agents"]["extractor"] == {
        "calls": 1,
        "prompt_tokens": 40,
        "completion_tokens": 10,
        "total_tokens": 50,
        "avg_latency_s": 0.5,
    }
    assert snapshot["models"]["gemma-4"] == {"calls": 2, "total_tokens": 375}
    assert snapshot["models"]["qwen-3"] == {"calls": 1, "total_tokens": 50}
    assert snapshot["total"] == {"calls": 3, "total_tokens": 425}


def test_avg_latency_is_mean_not_sum():
    tracker = UsageTracker()
    for latency in (0.1, 0.2, 0.3):
        tracker.record("a", "m", "s", 1, 1, latency)

    avg = tracker.snapshot()["agents"]["a"]["avg_latency_s"]
    assert avg == round(0.6 / 3, 6)
    assert avg == 0.2


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_asyncio_gather_concurrent_records():
    tracker = UsageTracker()
    agents = ["narrator", "extractor", "summarizer"]
    models = ["gemma-4", "qwen-3"]
    n = 300

    async def one(i: int) -> None:
        # yield so the gather genuinely interleaves tasks
        await asyncio.sleep(0)
        tracker.record(
            agents[i % len(agents)],
            models[i % len(models)],
            f"s{i % 4}",
            prompt_tokens=10,
            completion_tokens=5,
            latency_s=0.25,
        )

    async def main() -> None:
        await asyncio.gather(*(one(i) for i in range(n)))

    run(main())

    snapshot = tracker.snapshot()
    assert snapshot["total"] == {"calls": n, "total_tokens": n * 15}

    # each agent gets exactly n // len(agents) records
    per_agent = n // len(agents)
    for agent in agents:
        stats = snapshot["agents"][agent]
        assert stats["calls"] == per_agent
        assert stats["prompt_tokens"] == per_agent * 10
        assert stats["completion_tokens"] == per_agent * 5
        assert stats["total_tokens"] == per_agent * 15
        assert stats["avg_latency_s"] == 0.25

    per_model = n // len(models)
    for model in models:
        assert snapshot["models"][model]["calls"] == per_model
        assert snapshot["models"][model]["total_tokens"] == per_model * 15

    # tokens summed across all agents account for every token seen
    assert sum(a["total_tokens"] for a in snapshot["agents"].values()) == n * 15


def test_threaded_records_are_lossless():
    tracker = UsageTracker()
    threads_count = 8
    per_thread = 250

    def worker() -> None:
        for _ in range(per_thread):
            tracker.record("narrator", "gemma-4", "s1", 10, 5, 0.25)

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = tracker.snapshot()
    expected_calls = threads_count * per_thread
    assert snapshot["total"] == {
        "calls": expected_calls,
        "total_tokens": expected_calls * 15,
    }
    assert snapshot["agents"]["narrator"]["calls"] == expected_calls


# ---------------------------------------------------------------------------
# isolation / reset / singleton
# ---------------------------------------------------------------------------


def test_snapshot_is_detached_from_internal_state():
    tracker = UsageTracker()
    tracker.record("a", "m", "s", 1, 1, 1.0)

    snapshot = tracker.snapshot()
    snapshot["agents"]["a"]["calls"] = 999
    snapshot["total"]["calls"] = 999

    fresh = tracker.snapshot()
    assert fresh["agents"]["a"]["calls"] == 1
    assert fresh["total"]["calls"] == 1


def test_reset_clears_all_counters():
    tracker = UsageTracker()
    tracker.record("a", "m", "s1", 1, 1, 1.0)
    tracker.record("b", "m2", "s2", 2, 2, 2.0)

    tracker.reset()

    assert tracker.snapshot() == {
        "agents": {},
        "models": {},
        "total": {"calls": 0, "total_tokens": 0},
    }


def test_get_tracker_is_singleton():
    reset_tracker()
    first = get_tracker()
    second = get_tracker()
    assert first is second

    first.record("a", "m", "s", 1, 1, 1.0)
    assert second.snapshot()["total"]["calls"] == 1

    reset_tracker()
    assert get_tracker() is not first


# ---------------------------------------------------------------------------
# I1: log persistence (the source of truth) + aggregation from the log
# ---------------------------------------------------------------------------


from pathlib import Path  # noqa: E402

from fabulist.log import EventLog  # noqa: E402
from fabulist.usage.track import (  # noqa: E402
    aggregate_usage,
    append_usage_event,
    record_usage,
    usage_from_log,
)


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "events.db")


def test_append_usage_event_persists_as_system_usage_row(tmp_path):
    with _log(tmp_path) as log:
        event = append_usage_event(
            log,
            session_id="s1",
            agent="narrator",
            model="your-model-id",
            prompt_tokens=100,
            completion_tokens=40,
            latency_s=2.0,
        )
        assert event.type == "system"
        assert event.actor == "narrator"
        assert event.payload["event"] == "usage"
        assert event.payload["total_tokens"] == 140
        # Read back through the audit view.
        rows = [e for e in log.get_events("s1") if e.payload.get("event") == "usage"]
        assert len(rows) == 1
        assert rows[0].payload["session_id"] == "s1"


def test_usage_from_log_aggregates_session(tmp_path):
    with _log(tmp_path) as log:
        append_usage_event(
            log, session_id="s1", agent="narrator", model="your-model-id",
            prompt_tokens=100, completion_tokens=40, latency_s=2.0,
        )
        append_usage_event(
            log, session_id="s1", agent="narrator", model="your-model-id",
            prompt_tokens=200, completion_tokens=10, latency_s=4.0,
        )
        append_usage_event(
            log, session_id="s1", agent="extractor", model="qwen",
            prompt_tokens=30, completion_tokens=10, latency_s=0.5,
        )
        agg = usage_from_log(log, "s1")

    assert agg["calls"] == 3
    assert agg["total_tokens"] == 390
    assert agg["agents"]["narrator"]["calls"] == 2
    assert agg["agents"]["narrator"]["total_tokens"] == 350
    assert agg["agents"]["narrator"]["avg_latency_s"] == 3.0
    assert agg["agents"]["extractor"]["total_tokens"] == 40
    assert agg["models"]["your-model-id"] == {
        "calls": 2,
        "prompt_tokens": 300,
        "completion_tokens": 50,
        "total_tokens": 350,
    }
    assert agg["sessions"]["s1"]["calls"] == 3


def test_usage_from_log_cross_session(tmp_path):
    with _log(tmp_path) as log:
        append_usage_event(
            log, session_id="s1", agent="narrator", model="m",
            prompt_tokens=1, completion_tokens=1, latency_s=0.1,
        )
        append_usage_event(
            log, session_id="s2", agent="narrator", model="m",
            prompt_tokens=2, completion_tokens=2, latency_s=0.2,
        )
        agg = usage_from_log(log, None)

    assert agg["calls"] == 2
    assert set(agg["sessions"]) == {"s1", "s2"}
    assert agg["models"]["m"]["total_tokens"] == 6


def test_record_usage_updates_ram_and_log(tmp_path):
    reset_tracker()
    with _log(tmp_path) as log:
        record_usage(
            log,
            session_id="s1",
            agent="narrator",
            model="gemma-4",
            prompt_tokens=10,
            completion_tokens=5,
            latency_s=0.25,
        )
        agg = usage_from_log(log, "s1")
    # Log is the truth...
    assert agg["calls"] == 1
    assert agg["total_tokens"] == 15
    # ...and the RAM tally (legacy live view) still saw the call.
    assert get_tracker().snapshot()["total"] == {"calls": 1, "total_tokens": 15}


def test_record_usage_without_log_only_updates_ram():
    reset_tracker()
    record_usage(
        None,
        session_id="s1",
        agent="narrator",
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        latency_s=0.1,
    )
    assert get_tracker().snapshot()["total"]["calls"] == 1


def test_aggregate_usage_tolerates_malformed_rows():
    """A hand-edited row must never crash the reader (D4): bad numbers -> 0."""
    from types import SimpleNamespace

    events = [
        SimpleNamespace(
            type="system",
            session_id="s1",
            payload={"event": "usage", "agent": "a", "model": "m",
                     "session_id": "s1", "prompt_tokens": "nan",
                     "completion_tokens": 0, "total_tokens": 0, "latency_s": 0},
        ),
        SimpleNamespace(type="user", session_id="s1", payload={"text": "hi"}),
        SimpleNamespace(type="system", session_id="s1", payload={"event": "plan"}),
    ]
    agg = aggregate_usage(events)
    assert agg["calls"] == 1
    assert agg["prompt_tokens"] == 0
    assert agg["agents"]["a"]["total_tokens"] == 0
    assert agg["agents"]["a"]["avg_latency_s"] == 0.0
