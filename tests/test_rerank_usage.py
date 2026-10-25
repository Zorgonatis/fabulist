"""Tests for the reranker's ``system``/``usage`` row (W1f parity for rerank).

Rerank used to be the one model-ish stage in the recall path that emitted NO
usage row, so ``/api/usage`` (I1/D23: per-agent aggregation over
``system``/``usage`` events) could never show it firing. These tests drive the
real ``recall_block`` policy over a real :class:`MemoryStore` and a real
:class:`EventLog`, faking only the HTTP wire (``httpx.MockTransport``, house
convention): one embeddings route and one rerank route, so the fake is the
production seam and not a monkeypatched internal.

The contract under test: a SUCCESSFUL rerank appends exactly one usage row with
``agent="reranker"``; a FAILED rerank (non-200) appends none — it stays dark
behind its warning log, no error row, no new surface.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fabulist.agents.debug import CURRENT_SESSION
from fabulist.log import EventLog
from fabulist.memory.recall import RecallConfig, recall_block
from fabulist.memory.store import MemoryStore

SESSION = "s-rerank"

#: Embedding dim of the seeded store (the query vector below must match it).
DIM = 4

#: What the fake rerank endpoint answers with: all three candidates, best first.
RERANK_RESULTS = [
    {"index": 2, "relevance_score": 0.9},
    {"index": 1, "relevance_score": 0.5},
    {"index": 0, "relevance_score": 0.1},
]


def run(coro):
    """Drive a coroutine to completion (suite has no pytest-asyncio)."""
    return asyncio.run(coro)


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def event_log(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        yield log
    finally:
        log.close()


@pytest.fixture
def store(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.upsert("c_s", 1, "narrative", "one", [1.0, 0.0, 0.0, 0.0])
    store.upsert("c_s", 2, "narrative", "two", [0.9, 0.1, 0.0, 0.0])
    store.upsert("c_s", 3, "narrative", "three", [0.0, 1.0, 0.0, 0.0])
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def wire(monkeypatch):
    """Install a MockTransport serving both the embeddings and rerank routes.

    ``ctx["status"]`` is the rerank status (200 by default); ``ctx["calls"]``
    counts the rerank requests so a test can assert the call actually happened.
    """

    ctx: dict = {"status": 200, "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/rerank"):
            ctx["calls"] += 1
            if ctx["status"] != 200:
                return httpx.Response(ctx["status"], text="boom")
            return httpx.Response(
                200, json={"model": "qwen3-reranker", "results": RERANK_RESULTS}
            )
        payload = json.loads(request.content.decode())
        data = [
            {"object": "embedding", "index": i, "embedding": [1.0, 0.0, 0.0, 0.0]}
            for i in range(len(payload["input"]))
        ]
        return httpx.Response(200, json={"object": "list", "data": data})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    # The embeddings client and the rerank client import the SAME httpx module,
    # so one patch covers both routes; the handler dispatches on the path.
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    return ctx


def _usage_events(log: EventLog, session_id: str = SESSION):
    return [
        event
        for event in log.get_events(session_id)
        if event.type == "system" and event.payload.get("event") == "usage"
    ]


def _recall(store, log, **overrides):
    """Run the real recall policy with rerank ON (the default) and a log."""
    kwargs = dict(
        config=RecallConfig(k=2, rerank_enabled=True, overfetch=3),
        rerank_endpoint="http://r",
        rerank_model="qwen3-reranker",
        log=log,
        exclude_session_id=SESSION,
    )
    kwargs.update(overrides)
    return run(recall_block(store, "http://e", "m", "c_s", "one", **kwargs))


# --- success: exactly one row, agent="reranker" ----------------------------


def test_successful_rerank_appends_one_usage_row(store, event_log, wire):
    token = CURRENT_SESSION.set(SESSION)
    try:
        result = _recall(store, event_log)
    finally:
        CURRENT_SESSION.reset(token)

    assert wire["calls"] == 1
    # recall itself is unchanged: rerank order, truncated to k.
    lines = [line for line in result.block.splitlines() if line.startswith("- ")]
    assert "three" in lines[0]
    assert "two" in lines[1]

    events = _usage_events(event_log)
    assert len(events) == 1
    event = events[0]
    assert event.actor == "reranker"

    row = event.payload
    assert row["agent"] == "reranker"
    assert row["model"] == "qwen3-reranker"
    assert row["session_id"] == SESSION
    # /v1/rerank reports no token usage: the char count stands in (W1f parity),
    # the completion side is structurally 0.
    assert row["prompt_tokens"] == len("one") + len("one") + len("two") + len("three")
    assert row["chars"] == row["prompt_tokens"]
    assert row["tokens"] is None
    assert row["completion_tokens"] == 0
    assert row["total_tokens"] == row["prompt_tokens"]
    assert isinstance(row["latency_s"], float) and row["latency_s"] >= 0.0
    # rerank-only extras (D4 allows extras on the same row).
    assert row["documents"] == 3
    assert row["kept"] == 3
    assert row["top_n"] == 2
    assert row["endpoint"] == "http://r"


def test_usage_row_validates_against_the_schema_gate(store, event_log, wire):
    from fabulist.log.schema_gate import validate_usage_payload

    token = CURRENT_SESSION.set(SESSION)
    try:
        _recall(store, event_log)
    finally:
        CURRENT_SESSION.reset(token)

    assert validate_usage_payload(_usage_events(event_log)[0].payload) == []


def test_row_falls_back_to_the_caller_session_outside_a_turn(store, event_log, wire):
    # No CURRENT_SESSION installed: the chat's own id (the caller's
    # exclude_session_id) places the row rather than dropping it.
    token = CURRENT_SESSION.set("")
    try:
        _recall(store, event_log)
    finally:
        CURRENT_SESSION.reset(token)

    assert len(_usage_events(event_log)) == 1


# --- failure: no row, recall still falls back ------------------------------


def test_failed_rerank_appends_no_usage_row(store, event_log, wire):
    wire["status"] = 500
    token = CURRENT_SESSION.set(SESSION)
    try:
        result = _recall(store, event_log)
    finally:
        CURRENT_SESSION.reset(token)

    assert wire["calls"] == 1
    assert _usage_events(event_log) == []
    # degradation unchanged: vector order, still a usable block.
    lines = [line for line in result.block.splitlines() if line.startswith("- ")]
    assert "one" in lines[0]
    assert "two" in lines[1]


def test_rerank_off_appends_no_usage_row(store, event_log, wire):
    token = CURRENT_SESSION.set(SESSION)
    try:
        _recall(store, event_log, config=RecallConfig(k=2, rerank_enabled=False))
    finally:
        CURRENT_SESSION.reset(token)

    assert wire["calls"] == 0
    assert _usage_events(event_log) == []


def test_no_log_bound_reranks_without_rows(store, wire):
    token = CURRENT_SESSION.set(SESSION)
    try:
        result = _recall(store, None)
    finally:
        CURRENT_SESSION.reset(token)

    assert wire["calls"] == 1
    assert result.block is not None


# --- D23: measurement must never break recall ------------------------------


def test_append_failure_never_breaks_recall(store, wire):
    class ExplodingLog:
        def append_event(self, *args, **kwargs):
            raise RuntimeError("disk full")

    token = CURRENT_SESSION.set(SESSION)
    try:
        result = _recall(store, ExplodingLog())
    finally:
        CURRENT_SESSION.reset(token)

    lines = [line for line in result.block.splitlines() if line.startswith("- ")]
    assert "three" in lines[0]  # reranked block still delivered
