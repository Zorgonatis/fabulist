"""W9.26 §2.3 step 5 — the terminal ``finish`` frame, SUMMARIZER-SCOPED (D86).

The summarizer's finish-reason allowlist can only work if the model seam reports
HOW the generation ended. ``RegistryModelCaller`` therefore appends ONE terminal
``{"type": "finish", "finish_reason": <str|null>}`` frame — but ONLY when it was
built with ``emit_finish=True``, which only the summarizer caller is:

* ``graph/types.py``'s union carries the name (``"finish"``, deliberately not
  ``"done"`` — that one ends a TURN in ``api/app.py``);
* the narrator/OOC wire is byte-stable: no new frame name appears on it at all;
* Lane A's ``_drain_frames`` reads the frame, so the allowlist accepts a natural
  stop and rejects a truncation;
* the existing ``finish_reason == "length"`` error frame keeps its position and
  shape (the graph's truncation signal is unchanged).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import fabulist.wiring as wiring
from fabulist.connections.registry import Registry
from fabulist.graph.compaction import (
    FINISH_FILTER,
    FINISH_TRUSTED,
    FINISH_UNKNOWN,
    _drain_frames,
    classify_finish_reason,
)
from fabulist.graph.types import StreamEventType
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


def _registry() -> Registry:
    return Registry.from_dict(
        {
            "connections": {
                "c": {"base_url": "http://localhost:4444/v1", "model": "your-model-id"}
            },
            "agents": {"summarizer": "c", "narrator": "c"},
        }
    )


# --- fake transport (the shape test_wiring_structured installs) --------------


class _FakeDelta:
    def __init__(self, content: str | None = None) -> None:
        self.content = content
        self.tool_calls = None
        self.reasoning_content = None


class _FakeChoice:
    def __init__(self, delta: _FakeDelta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, choices: list, usage: object | None = None) -> None:
        self.choices = choices
        self.usage = usage


class _FakeMessage:
    def __init__(self, content: str | None, finish_reason: str | None) -> None:
        self.content = content
        self.reasoning_content = None
        self.tool_calls = None
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, choice: _FakeChoice) -> None:
        self.choices = [choice]
        self.usage = None


class _FakeCompletions:
    def __init__(self, chunks: list, response: _FakeResponse | None) -> None:
        self._chunks = chunks
        self._response = response

    async def create(self, **kwargs):
        if kwargs.get("stream") is False:
            return self._response

        async def _stream():
            for chunk in self._chunks:
                yield chunk

        return _stream()


def _install(monkeypatch, chunks: list, response: _FakeResponse | None = None) -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(chunks, response))
    )
    monkeypatch.setattr(wiring, "_async_client", lambda agent, registry: client)


def _drain(caller) -> list[dict]:
    async def _run():
        return [event async for event in caller([{"role": "user", "content": "hi"}])]

    return run(_run())


STOP_CHUNKS = [
    _FakeChunk([_FakeChoice(_FakeDelta(content="summary text "))]),
    _FakeChunk([_FakeChoice(_FakeDelta(content="continues"), finish_reason="stop")]),
]


# --- the union + the frame --------------------------------------------------


def test_the_union_names_finish_and_not_only_done():
    """The frame type is a union member; ``done`` stays the turn terminator."""
    assert "finish" in StreamEventType.__args__
    assert "done" in StreamEventType.__args__


def test_the_summarizer_caller_emits_a_terminal_finish_frame(monkeypatch):
    _install(monkeypatch, STOP_CHUNKS)
    caller = wiring.RegistryModelCaller("summarizer", _registry(), emit_finish=True)

    events = _drain(caller)

    assert events[-1] == {"type": "finish", "finish_reason": "stop"}
    # Terminal: exactly one, and nothing after it.
    assert [e["type"] for e in events].count("finish") == 1
    assert [e["type"] for e in events].count("done") == 0


def test_a_missing_finish_reason_is_reported_as_null_not_invented(monkeypatch):
    """An absent reason is not evidence of truncation (D86) — the frame says null."""
    _install(
        monkeypatch,
        [_FakeChunk([_FakeChoice(_FakeDelta(content="text"))])],
    )
    caller = wiring.RegistryModelCaller("summarizer", _registry(), emit_finish=True)

    events = _drain(caller)

    assert events[-1] == {"type": "finish", "finish_reason": None}
    assert classify_finish_reason(events[-1]["finish_reason"]) == FINISH_TRUSTED


def test_the_narrator_wire_carries_no_finish_frame(monkeypatch):
    """Byte-stability: the CLASS default omits the frame. (The wiring chokepoint
    opts narrator/OOC in with emit_finish=True — see the chokepoint pin below;
    a hand-built caller without the flag is byte-identical to pre-W9.26.)"""
    _install(monkeypatch, STOP_CHUNKS)
    caller = wiring.RegistryModelCaller("narrator", _registry())

    events = _drain(caller)

    assert [e["type"] for e in events] == ["content", "content"]
    assert "finish" not in {e["type"] for e in events}


def test_make_agent_caller_only_enables_the_frame_when_asked(monkeypatch, tmp_path):
    """The chokepoint forwards the flag; hand-built callers without it omit the
    frame. (Since issue #28 the wiring narrator/OOC cache passes emit_finish=True,
    so live narrator turns DO record finish_reason — pinned below.)"""
    _install(monkeypatch, STOP_CHUNKS)
    log = EventLog(tmp_path / "finish.db")
    try:
        narrator = wiring.make_agent_caller("narrator", _registry(), log)
        summarizer = wiring.make_agent_caller("summarizer", _registry(), log, emit_finish=True)
        assert [e["type"] for e in _drain(narrator)] == ["content", "content"]
        assert _drain(summarizer)[-1]["finish_reason"] == "stop"
    finally:
        log.close()


def test_the_non_streaming_transport_reports_the_reason_too(monkeypatch):
    """W9.25's synthesized protocol carries the frame as well (no transport gap)."""
    choice = _FakeChoice(_FakeDelta(), finish_reason="stop")
    choice.message = _FakeMessage("text", "stop")
    _install(monkeypatch, [], _FakeResponse(choice))
    registry = Registry.from_dict(
        {
            "connections": {
                "c": {
                    "base_url": "http://localhost:4444/v1",
                    "model": "your-model-id",
                    "streaming": False,
                }
            },
            "agents": {"summarizer": "c"},
        }
    )
    caller = wiring.RegistryModelCaller("summarizer", registry, emit_finish=True)

    events = _drain(caller)

    assert events[-1] == {"type": "finish", "finish_reason": "stop"}


# --- the allowlist actually reads it ---------------------------------------


def test_lanes_a_drain_reader_sees_the_frame_and_accepts_it(monkeypatch):
    """Integration with Lane A: ``_drain_frames`` → the D86 allowlist.

    This is the whole point of the frame: the service must be able to tell a
    natural stop from a truncation, and a ``finish`` frame that the reader did not
    understand would silently degrade every compaction into a retry-then-fail.
    """
    _install(monkeypatch, STOP_CHUNKS)
    caller = wiring.RegistryModelCaller("summarizer", _registry(), emit_finish=True)

    text, finish = run(_drain_frames(caller, [{"role": "user", "content": "hi"}]))

    assert text == "summary text continues"
    assert finish == "stop"
    assert classify_finish_reason(finish) == FINISH_TRUSTED


def test_a_length_truncation_keeps_its_error_frame_and_is_unclassified(monkeypatch):
    """``finish_reason == "length"`` still errors (unchanged) AND stays UNKNOWN.

    Issue #26 named only the provider FILTER class; a truncation keeps ADR-021's
    discard-and-repair-once behavior, so it is deliberately not reclassified.
    """
    _install(
        monkeypatch,
        [
            _FakeChunk([_FakeChoice(_FakeDelta(content="half a sum"))]),
            _FakeChunk([_FakeChoice(_FakeDelta(), finish_reason="length")]),
        ],
    )
    caller = wiring.RegistryModelCaller("summarizer", _registry(), emit_finish=True)

    events = _drain(caller)

    errors = [e for e in events if e["type"] == "error"]
    assert errors and errors[0]["meta"] == {"finish_reason": "length"}
    # The error frame keeps its position (before the terminal finish frame).
    assert [e["type"] for e in events].index("error") < len(events) - 1
    assert events[-1] == {"type": "finish", "finish_reason": "length"}
    assert classify_finish_reason(events[-1]["finish_reason"]) == FINISH_UNKNOWN
    text, finish = run(_drain_frames(caller, [{"role": "user", "content": "hi"}]))
    assert text == "half a sum"
    assert classify_finish_reason(finish) == FINISH_UNKNOWN


@pytest.mark.parametrize("reason", ["stop", "tool_calls", None, ""])
def test_trusted_reasons_pass_and_a_content_filter_is_its_own_class(reason):
    """Issue #26: a filter is a RECOGNIZED refusal, never an unknown value."""
    assert classify_finish_reason(reason) == FINISH_TRUSTED
    assert classify_finish_reason("content_filter") == FINISH_FILTER
    assert classify_finish_reason("sensitive") == FINISH_FILTER
    assert classify_finish_reason("banana") == FINISH_UNKNOWN


def test_narrator_caller_emits_finish_when_chokepoint_flag_set(monkeypatch):
    """Issue #28 wiring: narrator/OOC callers are built emit_finish=True at the
    chokepoint, so live narrator turns record a terminal finish_reason. The frame
    stays side-band: prose frames are identical to the no-flag run."""
    _install(monkeypatch, STOP_CHUNKS)
    without = wiring.RegistryModelCaller("narrator", _registry())
    with_flag = wiring.RegistryModelCaller("narrator", _registry(), emit_finish=True)

    plain = _drain(without)
    framed = _drain(with_flag)

    assert [e for e in framed if e["type"] != "finish"] == plain  # prose untouched
    assert framed[-1] == {"type": "finish", "finish_reason": "stop"}
