"""W9.23 §8.12 + W9.26 §2.8 — the post-turn hooks run CONCURRENTLY, isolated, per-task.

Four slow post-turn hooks (the W9.26 context monitor — the retired W2.9 threshold
compactor's slot —, memory, the assistant consult, and the historian spawn) must
never serialize behind each other, and one failing hook must cancel nothing.
These tests pin the unit-level rules the live pass (§8.12's log-observable
timestamps) then confirms on the isolated engine:

* elapsed ≈ ``max(hooks)``, not ``sum(hooks)`` — each hook runs in its OWN task;
* a raising hook neither cancels its siblings nor loses its own failure row, and
  that row's VARIANT is chosen by the explicit hook→event map (W9.26 §2.8):
  ``index_degraded`` for memory, ``hook_degraded`` naming the hook for everyone
  else (the issue #34 degradation vocabulary the Inspector renders) — never
  ``compaction_failed``, which names a pipeline failure and feeds
  the ⚑R3a forced-attempt counter;
* every post-turn task discards ITSELF from the strong-ref set, so N overlapping
  post-turn task sets (two sessions, or one turn's several hooks) leave no leaked
  task and never discard a sibling's slot.

No model calls and no app: ``_schedule_post_turn`` is the seam the engine's pump
calls, and it is exactly what changed.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from fabulist.api.app import _BACKGROUND_TASKS, _Session, _schedule_post_turn
from fabulist.log import EventLog

SID_A = "s-hooks-a"
SID_B = "s-hooks-b"


def run(coro):
    return asyncio.run(coro)


async def _drain(timeout: float = 5.0) -> float:
    """Wait for every background post-turn task to finish; return the elapsed time."""
    started = time.monotonic()
    while _BACKGROUND_TASKS and time.monotonic() - started < timeout:
        await asyncio.sleep(0.005)
    return time.monotonic() - started


@pytest.fixture
def log(tmp_path):
    event_log = EventLog(tmp_path / "hooks.db")
    try:
        yield event_log
    finally:
        event_log.close()
    _BACKGROUND_TASKS.clear()


def test_a_slow_hook_does_not_delay_a_fast_sibling(log):
    """Elapsed ≈ max(hooks), not their sum: the hooks are independent tasks.

    Two hooks that both sleep: 0.3s and 0.15s. Run as one task in sequence they
    cost 0.45s; as independent tasks 0.3s. The elapsed bound is what separates
    the two, and the marks prove the short hook RAN inside the long one rather
    than after it.
    """
    session = _Session(SID_A, "Hooks")
    marks: list[tuple[str, float]] = []

    async def slow_hook(session_id: str) -> None:
        marks.append(("slow", time.monotonic()))
        await asyncio.sleep(0.3)
        marks.append(("slow_done", time.monotonic()))

    async def fast_hook(session_id: str) -> None:
        marks.append(("fast", time.monotonic()))
        await asyncio.sleep(0.15)
        marks.append(("fast_done", time.monotonic()))

    async def scenario() -> tuple[float, list[tuple[str, float]]]:
        started = time.monotonic()
        assert _schedule_post_turn([slow_hook, fast_hook], session, log) is None
        # One task per hook (never one task running them in sequence).
        assert len(_BACKGROUND_TASKS) == 2
        await _drain()
        return time.monotonic() - started, marks

    elapsed, marks = run(scenario())
    assert 0.28 <= elapsed < 0.4, elapsed  # max(0.3, 0.15), not their sum (0.45)
    stamps = dict(marks)
    # The short hook started AND finished while the long one was still running.
    assert stamps["slow"] <= stamps["fast"] < stamps["fast_done"] < stamps["slow_done"]
    assert _BACKGROUND_TASKS == set()


def test_a_raising_hook_cancels_nothing_and_keeps_its_own_failure_row(log):
    """Failure isolation (§3.6.8): per-hook rows, no sibling cancelled.

    The row is ``hook_degraded`` naming the hook (W9.26 §2.8 + issue #34). It
    used to be ``compaction_failed`` by default, which was a lie for every hook
    that never compacts (and a hazard: the ⚑R3a forced-attempt counter reads
    those rows).
    """
    session = _Session(SID_A, "Hooks")
    finished: list[str] = []

    async def boom_hook(session_id: str) -> None:
        raise RuntimeError("kaboom")

    async def survivor_hook(session_id: str) -> None:
        await asyncio.sleep(0.05)
        finished.append("survivor")

    async def scenario() -> list[str]:
        _schedule_post_turn([boom_hook, survivor_hook], session, log)
        await _drain()
        return finished

    assert run(scenario()) == ["survivor"]
    rows = [
        event.payload
        for event in log.get_events(SID_A)
        if (event.payload or {}).get("event") == "hook_degraded"
    ]
    assert len(rows) == 1
    assert rows[0]["stage"] == "boom_hook"
    assert rows[0]["hook"] == "boom_hook"
    assert "kaboom" in rows[0]["reason"]
    assert _BACKGROUND_TASKS == set()


def test_no_post_turn_hook_files_a_compaction_failure(log):
    """``compaction_failed`` is reserved for the compact pipeline (W9.26 §2.9).

    A crashing monitor/assistant/historian must never borrow that name: the forced
    path counts own ``compaction_failed`` rows toward ⚑R3a's three-attempt bound,
    so a mislabelled sibling hook would silently spend a compaction's budget.
    """
    session = _Session(SID_A, "Hooks")

    def make(name: str):
        async def hook(session_id: str) -> None:
            raise RuntimeError(f"{name} down")

        hook.__name__ = name
        return hook

    async def scenario() -> None:
        _schedule_post_turn(
            [make("context_monitor"), make("assistant_turn_hook"), make("historian_hook")],
            session,
            log,
        )
        await _drain()

    run(scenario())
    events = [event.payload for event in log.get_events(SID_A)]
    assert [e["event"] for e in events] == ["hook_degraded"] * 3
    assert sorted(e["hook"] for e in events) == [
        "assistant_turn_hook",
        "context_monitor",
        "historian_hook",
    ]


def test_a_memory_hook_failure_lands_as_an_index_degraded_row(log):
    """The row names the degraded class: indexing, not a pipeline failure.

    Issue #34 renamed the S4c row into the degradation vocabulary the Inspector
    renders; the SHAPE is the notice shape (``event``/``stage``/``reason``), and
    the hook's own name still rides ``stage`` so the reader knows who raised.
    """
    session = _Session(SID_A, "Hooks")

    async def memory_hook(session_id: str) -> None:
        raise RuntimeError("vectors down")

    async def scenario() -> None:
        _schedule_post_turn([memory_hook], session, log)
        await _drain()

    run(scenario())
    rows = [
        event.payload
        for event in log.get_events(SID_A)
        if (event.payload or {}).get("event") == "index_degraded"
    ]
    assert len(rows) == 1
    assert rows[0]["stage"] == "memory_hook"
    assert "vectors down" in rows[0]["reason"]


def test_overlapping_post_turn_task_sets_discard_their_own_tasks(log):
    """The per-task discard (§3.6.8): no leak, no wrong-slot discard.

    Two sessions' post-turn tasks alive at once — the case the old single-slot
    ``_task_ref`` got wrong (it discarded by POSITION, so the second set's task
    replaced the first's reference and one task was never released).
    """
    session_a = _Session(SID_A, "A")
    session_b = _Session(SID_B, "B")
    ran: list[str] = []

    def make_hook(name: str):
        async def hook(session_id: str) -> None:
            await asyncio.sleep(0.1)
            ran.append(f"{name}:{session_id}")

        return hook

    async def scenario() -> tuple[int, set[asyncio.Task]]:
        _schedule_post_turn([make_hook("a")], session_a, log)
        _schedule_post_turn([make_hook("b")], session_b, log)
        live = set(_BACKGROUND_TASKS)
        assert len(live) == 2
        assert len({id(task) for task in live}) == 2
        await _drain()
        return len(live), set(_BACKGROUND_TASKS)

    live_count, remaining = run(scenario())
    assert live_count == 2
    assert remaining == set()  # both tasks released themselves
    assert sorted(ran) == [f"a:{SID_A}", f"b:{SID_B}"]


def test_a_quiet_turn_schedules_nothing(log):
    """No hook wired ⇒ no task, no strong-ref entry (the zero-cost path)."""
    session = _Session(SID_A, "Hooks")
    assert _schedule_post_turn([], session, log) is None
    assert _BACKGROUND_TASKS == set()
