"""Degradation notices (issue #34): a swallowed failure leaves a row the client renders.

Owner ruling (2026-10-23): "A small prompting exclamation/highlight of the
Inspector button; a tab or something for the actual failures. Re-use this
notification for the proposals too when they fire."

These tests pin the ENGINE half of that ruling: every optional-service swallow
site appends ONE typed ``system`` degradation row, and the turn/fan-out/hook
still completes (visibility, never fatality — the "should any be fatal" half of
the issue stays deliberately unruled). The four sites:

* pre-turn memory recall (``wiring.make_memorizer().recall_fn``) — a dead
  embedding service;
* the lane strategy (``wiring._decision_observer`` + the ``who_dispatches``
  ask) — a dead ``decision`` service, which FAILS OPEN;
* the memory indexer, through the post-turn ``memory_hook`` — a dead embedder;
* any other post-turn hook (assistant / historian / context monitor).

The transport is faked (``httpx.MockTransport`` / a raising ``embed_inputs``),
which is exactly the failure the issue observed: "All connection attempts
failed".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api.app import _Session, _schedule_post_turn, _BACKGROUND_TASKS
from fabulist.connections.registry import RegistryStore
from fabulist.log import EventLog
from fabulist.log.degradation import (
    DEGRADATION_EVENTS,
    HOOK_DEGRADED_EVENT,
    INDEX_DEGRADED_EVENT,
    LANE_DEGRADED_EVENT,
    RECALL_DEGRADED_EVENT,
    append_degradation,
    degradation_reason,
    degradation_text,
)
from fabulist.log.schema_gate import validate_degradation_payload
from fabulist.memory import indexer
from fabulist.wiring import make_memorizer

SID = "s-degraded"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def log(tmp_path: Path):
    event_log = EventLog(tmp_path / "degradation.db")
    try:
        yield event_log
    finally:
        event_log.close()
        _BACKGROUND_TASKS.clear()


def notices(log: EventLog, session_id: str = SID) -> list[dict]:
    """Every degradation row on the session, in seq order."""
    return [
        event.payload
        for event in log.get_events(session_id)
        if (event.payload or {}).get("event") in DEGRADATION_EVENTS
    ]


# -- the helper / payload shape --------------------------------------------


def test_append_degradation_writes_the_one_notice_shape(log):
    """event + stage + reason + the seq context a reader anchors the notice to.

    The shape is the whole contract with the Inspector's notices tab: a name the
    client renders, the STEP that degraded, a short actionable reason, and the
    turn (seq context) it belongs to.
    """
    log.append_event(SID, "user", "user", {"text": "hello"})
    assert append_degradation(
        log,
        SID,
        RECALL_DEGRADED_EVENT,
        stage="memory_recall",
        reason="ConnectError: All connection attempts failed — check the embedding connection",
    )
    rows = notices(log)
    assert len(rows) == 1
    payload = rows[0]
    assert payload["event"] == RECALL_DEGRADED_EVENT
    assert payload["stage"] == "memory_recall"
    assert payload["reason"].startswith("ConnectError")
    # seq context defaults to the newest event (the user row above).
    assert payload["turn"] == 1
    assert validate_degradation_payload(payload) == []


def test_append_degradation_is_best_effort_and_rejects_unknown_names(log):
    """A log hiccup while reporting a failure must not become a second failure."""

    class BrokenLog:
        def get_events(self, session_id):
            return []

        def append_event(self, *args, **kwargs):
            raise RuntimeError("disk full")

    assert (
        append_degradation(
            BrokenLog(), SID, HOOK_DEGRADED_EVENT, stage="x", reason="y"
        )
        is False
    )
    with pytest.raises(ValueError):
        append_degradation(log, SID, "recall_failed", stage="x", reason="y")


def test_degradation_reason_names_the_class_and_the_next_move():
    exc = httpx.ConnectError("All connection attempts failed")
    text = degradation_text(exc)
    assert text.startswith("ConnectError: All connection attempts failed")
    reason = degradation_reason(exc, "check the embedding connection")
    assert reason.startswith("ConnectError: All connection attempts failed")
    assert reason.endswith("check the embedding connection")


def test_every_degradation_event_passes_the_schema_gate(log):
    """All four names validate with the notice shape, and only with it."""
    for name in sorted(DEGRADATION_EVENTS):
        assert (
            validate_degradation_payload({"event": name, "stage": "s", "reason": "r"}) == []
        )
        assert validate_degradation_payload({"event": name, "stage": "s"}) != []
    assert validate_degradation_payload({"event": "usage", "stage": "s", "reason": "r"})


# -- site 1: memory recall ---------------------------------------------------


def _write_registry(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "connections": {
                    "emb": {"base_url": "http://127.0.0.1:4445/v1", "model": "emb"},
                },
                "agents": {"embedder": "emb", "narrator": "emb"},
            }
        ),
        encoding="utf-8",
    )
    return path


def _dead_embed(monkeypatch):
    """Fake transport: the embeddings endpoint refuses every connection."""

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(indexer, "embed_inputs", boom)


def test_a_dead_recall_appends_recall_degraded_and_the_turn_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Issue #34's exact case: recall dies, the turn commits, the log says why."""
    log_path = tmp_path / "events.db"
    log = EventLog(log_path)
    memorizer = make_memorizer(RegistryStore(_write_registry(tmp_path / "c.json")), log_path)
    assert memorizer is not None
    log.append_event(SID, "user", "user", {"text": "what did the merchant wear?"})
    _dead_embed(monkeypatch)

    # recall degrades to None (the swallow stays — memory is an enhancement).
    assert run(memorizer.recall_fn(SID, "what did the merchant wear?")) is None

    # …and the degradation is VISIBLE: one row, the stage, a short reason.
    rows = notices(log)
    assert [row["event"] for row in rows] == [RECALL_DEGRADED_EVENT]
    assert rows[0]["stage"] == "memory_recall"
    assert "ConnectError" in rows[0]["reason"]
    assert "embedding connection" in rows[0]["reason"]
    log.close()


def test_a_recall_failure_never_stops_the_turn_committing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The engine half: a raising recall_fn seeds no recall and still streams/commits."""
    from fabulist.graph.engine import GraphTurnEngine

    captured: dict = {}

    class FakeGraph:
        async def astream(self, initial, config, stream_mode=None):  # noqa: ANN001
            captured.update(initial)
            yield {"type": "committed", "meta": {}}
            yield {"type": "content", "delta": "the turn committed", "meta": {"channel": "prose"}}
            yield {"type": "done", "meta": {}}

    async def dead_recall(session_id, text):
        raise httpx.ConnectError("All connection attempts failed")

    engine = GraphTurnEngine(
        graph=FakeGraph(),
        log=None,  # type: ignore[arg-type]
        model=object(),  # type: ignore[arg-type]
        recall_fn=dead_recall,
    )

    async def scenario() -> list[dict]:
        return [event async for event in engine.run_turn(SID, "hello")]

    events = run(scenario())
    # Recall degradation is not a turn failure: the frames stream and the turn
    # commits (no ``error`` frame anywhere — the engine only emits one for an
    # exception or an empty turn).
    kinds = [event["type"] for event in events]
    assert kinds[0] == "committed"
    assert kinds[-1] == "done"
    assert "error" not in kinds
    assert captured["recall"] == ""


# -- site 2: the lane strategy (decision model) ------------------------------


def _decision_caller_with_dead_transport(log: EventLog):
    from fabulist.decision import DecisionCaller
    from fabulist.wiring import _decision_observer

    class Registry:
        def connection_for(self, name):
            if name != "decision":
                raise KeyError(name)
            from types import SimpleNamespace

            return SimpleNamespace(base_url="http://127.0.0.1:4445/v1", model="d1-3b")

    return DecisionCaller(
        Registry(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503, text="down"))
        ),
        observer=_decision_observer(log),
    )


def test_a_dead_dispatch_ask_appends_lane_degraded(log, monkeypatch):
    """The lane strategy fails OPEN — the set falls back to the ladder, visibly."""
    from fabulist.agents.debug import CURRENT_SESSION
    from fabulist.decision_queries import DISPATCH_QUESTION_ID

    log.append_event(SID, "user", "user", {"text": "hello"})
    caller = _decision_caller_with_dead_transport(log)
    CURRENT_SESSION.set(SID)
    try:
        answers = run(
            caller.ask(
                "state",
                {
                    DISPATCH_QUESTION_ID: {
                        "type": "choice",
                        "instructions": "who acts?",
                        "criteria": ["Mira", "Joss"],
                    }
                },
            )
        )
    finally:
        CURRENT_SESSION.set("")

    assert answers is None  # the fail-open signal
    rows = notices(log)
    assert [row["event"] for row in rows] == [LANE_DEGRADED_EVENT]
    assert rows[0]["stage"] == "npc_lane_strategy"
    assert "503" in rows[0]["reason"]
    assert "decision connection" in rows[0]["reason"]


def test_a_failed_non_dispatch_decision_emits_no_lane_notice(log, monkeypatch):
    """The gate's own failures keep their vocabulary: one notice per degradation class."""
    from fabulist.agents.debug import CURRENT_SESSION

    log.append_event(SID, "user", "user", {"text": "hello"})
    caller = _decision_caller_with_dead_transport(log)
    CURRENT_SESSION.set(SID)
    try:
        assert run(caller.verdict("state", "is this slop?")) is None
    finally:
        CURRENT_SESSION.set("")

    assert notices(log) == []


def test_the_dispatch_fallback_still_selects_the_full_ladder():
    """The turn CONTINUES: a dead decision caller yields the ladder's set, not [].

    ``presence.select_dispatch_members`` documents fail-open; the notice is the
    only new behaviour, so the fallback is pinned here against a caller that
    always fails.
    """
    from fabulist.state.presence import select_dispatch_members

    log = EventLog(":memory:")
    try:
        candidates = ["m-1", "m-2", "m-3"]

        class DeadDecision:
            enabled = True

            async def ask(self, state, questions):
                return None

        selected = run(
            select_dispatch_members(log, SID, candidates, DeadDecision())
        )
        assert selected == candidates
    finally:
        log.close()


# -- site 3 + 4: the post-turn hooks (memory indexer / housekeeping) ---------


async def _drain(timeout: float = 5.0) -> None:
    started = asyncio.get_event_loop().time()
    while _BACKGROUND_TASKS and asyncio.get_event_loop().time() - started < timeout:
        await asyncio.sleep(0.005)


def test_a_dead_indexer_appends_index_degraded_and_the_hook_swallows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The memory hook's embed fails; the turn already committed, the row says why."""
    log_path = tmp_path / "events.db"
    log = EventLog(log_path)
    memorizer = make_memorizer(RegistryStore(_write_registry(tmp_path / "c.json")), log_path)
    assert memorizer is not None
    log.append_event(SID, "user", "user", {"text": "hello"})
    log.append_event(SID, "narrative", "narrator", {"prose": "hi there"})
    _dead_embed(monkeypatch)

    async def memory_hook(session_id: str) -> None:
        await memorizer.index_tail(session_id)

    session = _Session(SID, "Degraded")

    async def scenario() -> None:
        _schedule_post_turn([memory_hook], session, log)
        await _drain()

    run(scenario())

    rows = notices(log)
    assert [row["event"] for row in rows] == [INDEX_DEGRADED_EVENT]
    assert rows[0]["stage"] == "memory_hook"
    assert "ConnectError" in rows[0]["reason"]
    assert "embedding connection" in rows[0]["reason"]
    log.close()


def test_a_crashing_housekeeping_hook_appends_hook_degraded(log):
    """The assistant/historian/monitor hooks file one ``hook_degraded`` each."""
    session = _Session(SID, "Degraded")

    def make(name: str):
        async def hook(session_id: str) -> None:
            raise RuntimeError(f"{name} down")

        hook.__name__ = name
        return hook

    async def scenario() -> None:
        _schedule_post_turn([make("assistant_turn_hook")], session, log)
        await _drain()

    run(scenario())
    rows = notices(log)
    assert [row["event"] for row in rows] == [HOOK_DEGRADED_EVENT]
    assert rows[0]["stage"] == "assistant_turn_hook"
    assert rows[0]["hook"] == "assistant_turn_hook"
    assert "RuntimeError: assistant_turn_hook down" in rows[0]["reason"]


# -- the inspector read ------------------------------------------------------


def test_inspector_notices_fold_returns_only_degradations(log):
    """One read for the notices tab: degradations only, newest last, shaped for the UI."""
    from fabulist.api.inspector_api import NOTICE_LIMIT, _notices

    log.append_event(SID, "user", "user", {"text": "hello"})
    append_degradation(
        log, SID, RECALL_DEGRADED_EVENT, stage="memory_recall", reason="recall died"
    )
    log.append_event(SID, "system", "post_turn", {"event": "post_turn_failed", "stage": "x", "error": "y"})
    append_degradation(
        log,
        SID,
        HOOK_DEGRADED_EVENT,
        stage="context_monitor",
        reason="monitor died",
        turn=7,
        extra={"hook": "context_monitor"},
    )

    rows = _notices(log.get_events(SID))
    assert [row["event"] for row in rows] == [
        RECALL_DEGRADED_EVENT,
        HOOK_DEGRADED_EVENT,
    ]
    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
    assert rows[1]["turn"] == 7
    assert rows[1]["hook"] == "context_monitor"
    assert rows[0]["turn"] == 1
    assert NOTICE_LIMIT >= len(rows)
