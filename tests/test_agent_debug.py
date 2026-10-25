"""Tests for the agent debug capture + interrogation API (D17).

Lane under test: :mod:`fabulist.agents.debug` (a drop-in wrapping
:class:`~fabulist.wiring.RegistryModelCaller`'s streaming protocol) and
:mod:`fabulist.api.agent_debug_api` (read-only interrogation over the append-only
log). The router test mounts the router on the real FastAPI app with the httpx
ASGI transport against a tmp SQLite event log, mirroring
``tests/test_state_api.py``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from fabulist.agents.debug import (
    AGENT_CALL_EVENT,
    MAX_CAPTURE_CHARS,
    MESSAGE_CONTENT_LIMIT,
    AgentCall,
    DebugCapture,
    append_call_event,
    calls_for,
)
from fabulist.api import create_app
from fabulist.api.agent_debug_api import router
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


async def collect(agen: AsyncIterator[dict]) -> list[dict]:
    """Drain an async iterator of stream events into a list."""
    return [event async for event in agen]


class FakeCaller:
    """Minimal ModelCaller stub with the RegistryModelCaller event shapes.

    Exposes ``.agent``/``.facts_hash`` exactly as the capture reads them by
    duck-typing (``RegistryModelCaller`` carries ``.agent``).
    """

    def __init__(
        self,
        events: list[dict],
        *,
        agent: str = "narrator",
        facts_hash: str = "abc123",
    ) -> None:
        self._events = [dict(event) for event in events]
        self.agent = agent
        self.facts_hash = facts_hash

    async def __call__(
        self, messages: list[dict], stream: bool = True
    ) -> AsyncIterator[dict]:
        for event in self._events:
            yield dict(event)


#: Interleaved reasoning/content/error deltas, the RegistryModelCaller protocol.
SCRIPT: list[dict] = [
    {"type": "reasoning", "delta": "Weighing the beat. "},
    {"type": "content", "delta": "Rain hammers ", "meta": {"channel": "prose"}},
    {"type": "reasoning", "delta": "Keep the tone warm. "},
    {"type": "content", "delta": "```openui\nsay: hi", "meta": {"channel": "dsl"}},
    {"type": "error", "delta": "truncated", "meta": {"finish_reason": "length"}},
]

MESSAGES: list[dict] = [
    {"role": "system", "content": "You are the narrator."},
    {"role": "user", "content": "The lantern guttered."},
]


def test_capture_is_byte_for_byte_passthrough():
    """The wrapped stream equals the unwrapped stream: same events, same order."""
    raw = run(collect(FakeCaller(SCRIPT)(MESSAGES)))
    captured: list[dict] = []
    wrapped = run(collect(DebugCapture(FakeCaller(SCRIPT), captured.append)(MESSAGES)))

    assert wrapped == raw
    assert wrapped == SCRIPT


def test_sink_called_once_with_accumulated_text_and_latency():
    """One completion -> one sink call carrying the accumulated wire snapshot."""
    captured: list[dict] = []
    run(collect(DebugCapture(FakeCaller(SCRIPT), captured.append)(MESSAGES)))

    assert len(captured) == 1
    wire = captured[0]
    assert wire["event"] == AGENT_CALL_EVENT
    assert wire["agent"] == "narrator"
    assert wire["messages"] == MESSAGES
    assert wire["response_text"] == "Rain hammers ```openui\nsay: hi"
    assert wire["reasoning_text"] == "Weighing the beat. Keep the tone warm. "
    assert wire["finish"] == "length"
    assert wire["facts_hash"] == "abc123"
    assert wire["latency_s"] >= 0.0
    assert wire["meta"] == {"finish_reason": "length"}


def test_capture_failure_never_breaks_the_turn(caplog):
    """A raising sink is logged and swallowed; every event still flows."""
    def boom(_wire: dict) -> None:
        raise RuntimeError("disk full")

    raw = run(collect(FakeCaller(SCRIPT)(MESSAGES)))
    with caplog.at_level(logging.ERROR, logger="fabulist.agents.debug"):
        wrapped = run(collect(DebugCapture(FakeCaller(SCRIPT), boom)(MESSAGES)))

    assert wrapped == raw
    assert any(
        "agent debug capture failed" in record.getMessage()
        for record in caplog.records
    )


def test_truncation_over_guard():
    """Response + message content are capped; reasoning takes the remainder."""
    huge_response = "x" * (MAX_CAPTURE_CHARS + 5_000)
    huge_message = "y" * (MESSAGE_CONTENT_LIMIT + 100)
    events = [
        {"type": "content", "delta": huge_response, "meta": {"channel": "prose"}},
        {"type": "reasoning", "delta": "reasoning that must be dropped"},
    ]
    captured: list[dict] = []
    caller = FakeCaller(events)
    run(collect(DebugCapture(caller, captured.append)([{"role": "user", "content": huge_message}])))

    wire = captured[0]
    assert len(wire["response_text"]) <= MAX_CAPTURE_CHARS + 40
    assert "[truncated" in wire["response_text"]
    # response consumed the whole per-call budget -> reasoning fully dropped
    assert wire["reasoning_text"].startswith("[truncated")
    assert len(wire["messages"][0]["content"]) <= MESSAGE_CONTENT_LIMIT + 40
    assert "[truncated" in wire["messages"][0]["content"]


def test_capture_under_guard_is_untouched():
    """Small captures carry no truncation marker (no false positives)."""
    captured: list[dict] = []
    run(collect(DebugCapture(FakeCaller(SCRIPT), captured.append)(MESSAGES)))
    assert "[truncated" not in captured[0]["response_text"]
    assert "[truncated" not in captured[0]["reasoning_text"]


# -- log round-trip ----------------------------------------------------------


@pytest.fixture
def log(tmp_path: Path):
    repo = EventLog(tmp_path / "events.db")
    yield repo
    repo.close()


def _wire(agent: str, response: str) -> dict:
    return {
        "event": AGENT_CALL_EVENT,
        "agent": agent,
        "messages": [{"role": "user", "content": "hi"}],
        "response_text": response,
        "reasoning_text": "",
        "finish": None,
        "latency_s": 0.25,
        "facts_hash": "deadbeef",
        "meta": {},
    }


def test_append_and_calls_for_roundtrip_seq_ordered(log):
    """Captures read back in seq order; the agent filter narrows to one caller."""
    append_call_event(log, "s1", _wire("narrator", "one"))
    append_call_event(log, "s1", _wire("summarizer", "two"))
    append_call_event(log, "s1", _wire("narrator", "three"))

    all_calls = calls_for(log, "s1")
    assert [call.response_text for call in all_calls] == ["one", "two", "three"]
    assert [call.seq for call in all_calls] == sorted(call.seq for call in all_calls)
    assert all(isinstance(call, AgentCall) for call in all_calls)
    assert all_calls[0].session_id == "s1"
    assert all_calls[0].latency_s == 0.25
    assert all_calls[0].facts_hash == "deadbeef"

    narrators = calls_for(log, "s1", agent="narrator")
    assert [call.response_text for call in narrators] == ["one", "three"]

    # The event is a loose-schema system row, actor = the calling agent.
    events = [e for e in log.get_events("s1") if e.payload.get("event") == AGENT_CALL_EVENT]
    assert all(e.type == "system" for e in events)
    assert events[0].actor == "narrator"


def test_calls_for_ignores_other_system_events(log):
    """Non-``agent_call`` system rows never surface as calls."""
    log.append_event("s1", "system", "user", {"event": "session_created"})
    log.append_event("s1", "user", "user", {"text": "hello"})
    append_call_event(log, "s1", _wire("narrator", "only"))
    assert [call.response_text for call in calls_for(log, "s1")] == ["only"]


# -- router ------------------------------------------------------------------


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build():
        app = create_app(FakeTurnEngine(), log_path)
        app.include_router(router)
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_list_agent_calls_200_and_filter(build_app):
    app = build_app()
    append_call_event(app.state.log, "s1", _wire("narrator", "narr"))
    append_call_event(app.state.log, "s1", _wire("summarizer", "summ"))

    async def scenario():
        async with client_for(app) as client:
            all_resp = await client.get("/api/sessions/s1/agent-calls")
            filtered = await client.get(
                "/api/sessions/s1/agent-calls", params={"agent": "narrator"}
            )
            return all_resp, filtered

    all_resp, filtered = run(scenario())
    assert all_resp.status_code == 200
    calls = all_resp.json()["calls"]
    assert [call["response_text"] for call in calls] == ["narr", "summ"]
    assert all("seq" in call and "ts" in call for call in calls)

    assert filtered.status_code == 200
    assert [call["response_text"] for call in filtered.json()["calls"]] == ["narr"]


def test_get_agent_call_by_seq_200_and_unknown_seq_404(build_app):
    app = build_app()
    append_call_event(app.state.log, "s1", _wire("narrator", "first"))
    append_call_event(app.state.log, "s1", _wire("narrator", "second"))
    calls = calls_for(app.state.log, "s1")
    second_seq = calls[-1].seq

    async def scenario():
        async with client_for(app) as client:
            ok = await client.get(f"/api/sessions/s1/agent-calls/{second_seq}")
            missing = await client.get("/api/sessions/s1/agent-calls/99999")
            return ok, missing

    ok, missing = run(scenario())
    assert ok.status_code == 200
    assert ok.json()["response_text"] == "second"
    assert ok.json()["seq"] == second_seq
    assert missing.status_code == 404


def test_unknown_session_is_404(build_app):
    app = build_app()

    async def scenario():
        async with client_for(app) as client:
            listed = await client.get("/api/sessions/ghost/agent-calls")
            single = await client.get("/api/sessions/ghost/agent-calls/1")
            return listed, single

    listed, single = run(scenario())
    assert listed.status_code == 404
    assert single.status_code == 404


def test_delete_is_method_not_allowed(build_app):
    """Append-only (D9): the surface is read-only, so no DELETE exists."""
    app = build_app()
    append_call_event(app.state.log, "s1", _wire("narrator", "kept"))

    async def scenario():
        async with client_for(app) as client:
            return await client.delete("/api/sessions/s1/agent-calls")

    resp = run(scenario())
    assert resp.status_code == 405


def test_usage_endpoint_not_duplicated(build_app):
    """``/api/usage`` stays owned by inspector_api; this router adds none."""
    build_app()  # app builds cleanly with the router mounted
    our_paths = {route.path for route in router.routes}
    assert "/api/usage" not in our_paths
    assert "/api/sessions/{session_id}/agent-calls" in our_paths
    assert "/api/sessions/{session_id}/agent-calls/{seq}" in our_paths
    # Append-only (D9): no delete/clear route is declared.
    methods = {method for route in router.routes for method in route.methods}
    assert "DELETE" not in methods


def test_capture_records_tool_call_arguments():
    """W9.14: a structured call's payload rides tool_call events; the capture
    must keep it, or an empty response_text reads as 'the model said nothing'."""
    captured: list[dict] = []
    run(
        collect(
            DebugCapture(
                FakeCaller(
                    [
                        {"type": "tool_call", "delta": '{"kind": "delta", '},
                        {"type": "tool_call", "delta": '"state": {}}'},
                        {"type": "error", "delta": "", "meta": {"finish_reason": "tool_calls"}},
                    ]
                ),
                captured.append,
            )(MESSAGES)
        )
    )

    assert len(captured) == 1
    row = captured[0]
    assert row["response_tool_calls"] == '{"kind": "delta", "state": {}}'
    assert row["response_text"] == ""
    assert row["finish"] == "tool_calls"
