"""W8.4 transport tests: RegistryModelCaller tool-call surfacing + request profile.

Covers the plumbing that lets a structured interaction (Tier A function-calling)
ride the existing ``RegistryModelCaller`` without disturbing the narrator/OOC
protocol:

* ``RegistryModelCaller`` surfaces ``{"type": "tool_call", "delta": ...}`` events
  for function-call argument fragments, and still forwards reasoning/content;
* a ``request_profile`` reaches ``build_request`` (so ``tools`` / ``tool_choice``
  / ``response_format`` land on the wire), and an absent profile sends none;
* ``collect_structured`` prefers tool arguments over content and falls back to
  content when the model answered in prose.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import fabulist.wiring as wiring
from fabulist.agents.debug import DebugCapture
from fabulist.connections.registry import Registry


def _registry() -> Registry:
    return Registry.from_dict(
        {
            "connections": {
                "c": {"base_url": "http://localhost:4444/v1", "model": "your-model-id"}
            },
            "agents": {"bts": "c"},
        }
    )


class _FakeFunction:
    def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, function: _FakeFunction, index: int = 0) -> None:
        self.function = function
        self.index = index


class _FakeDelta:
    def __init__(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _FakeChoice:
    def __init__(self, delta: _FakeDelta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, choices: list, usage: object | None = None) -> None:
        self.choices = choices
        self.usage = usage


class _FakeCompletions:
    def __init__(self, chunks: list, captured: dict) -> None:
        self._chunks = chunks
        self._captured = captured

    async def create(self, **kwargs):
        self._captured.update(kwargs)

        async def _stream():
            for chunk in self._chunks:
                yield chunk

        return _stream()


class _FakeAsyncClient:
    def __init__(self, chunks: list, captured: dict) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(chunks, captured))


def _install(monkeypatch, chunks: list, captured: dict) -> None:
    monkeypatch.setattr(
        wiring, "_async_client", lambda agent, registry: _FakeAsyncClient(chunks, captured)
    )


def _drain(caller, messages=None):
    async def _run():
        return [event async for event in caller(messages or [{"role": "user", "content": "hi"}])]

    return asyncio.run(_run())


def test_registry_model_caller_surfaces_tool_call_events(monkeypatch):
    captured: dict = {}
    chunks = [
        _FakeChunk([_FakeChoice(_FakeDelta(content="weighing options "))]),
        _FakeChunk(
            [_FakeChoice(_FakeDelta(tool_calls=[_FakeToolCall(_FakeFunction("emit", '{"a":'))]))]
        ),
        _FakeChunk(
            [_FakeChoice(_FakeDelta(tool_calls=[_FakeToolCall(_FakeFunction(arguments="1}"))]))]
        ),
        _FakeChunk([_FakeChoice(_FakeDelta(), finish_reason="tool_calls")]),
    ]
    _install(monkeypatch, chunks, captured)
    caller = wiring.RegistryModelCaller("bts", _registry())

    events = _drain(caller)

    kinds = [e["type"] for e in events]
    assert "tool_call" in kinds
    assert "content" in kinds  # reasoning/content protocol is unchanged
    args = "".join(e["delta"] for e in events if e["type"] == "tool_call")
    assert args == '{"a":1}'


def test_registry_model_caller_forwards_request_profile(monkeypatch):
    captured: dict = {}
    chunks = [_FakeChunk([_FakeChoice(_FakeDelta(content="{}"), finish_reason="stop")])]
    _install(monkeypatch, chunks, captured)
    caller = wiring.RegistryModelCaller("bts", _registry())
    profile = {
        "response_format": {"type": "json_object"},
        "tools": [{"type": "function", "function": {"name": "emit"}}],
        "tool_choice": "required",
    }

    async def _run():
        async for _ in caller([{"role": "user", "content": "hi"}], True, request_profile=profile):
            pass

    asyncio.run(_run())

    assert captured["response_format"] == {"type": "json_object"}
    assert captured["tools"] == [{"type": "function", "function": {"name": "emit"}}]
    assert captured["tool_choice"] == "required"


def test_registry_model_caller_without_profile_sends_no_transport_keys(monkeypatch):
    captured: dict = {}
    chunks = [_FakeChunk([_FakeChoice(_FakeDelta(content="hi"), finish_reason="stop")])]
    _install(monkeypatch, chunks, captured)
    caller = wiring.RegistryModelCaller("bts", _registry())

    _drain(caller)

    for key in ("tools", "tool_choice", "response_format"):
        assert key not in captured


def test_collect_structured_prefers_tool_args_over_content(monkeypatch):
    captured: dict = {}
    chunks = [
        _FakeChunk([_FakeChoice(_FakeDelta(content="let me think "))]),
        _FakeChunk(
            [_FakeChoice(_FakeDelta(tool_calls=[_FakeToolCall(_FakeFunction("emit", '{"a":1}'))]))]
        ),
        _FakeChunk([_FakeChoice(_FakeDelta(), finish_reason="tool_calls")]),
    ]
    _install(monkeypatch, chunks, captured)
    caller = wiring.RegistryModelCaller("bts", _registry())

    result = asyncio.run(wiring.collect_structured(caller, [{"role": "user", "content": "hi"}]))

    assert result == '{"a":1}'  # not the content text


def test_collect_structured_falls_back_to_content(monkeypatch):
    captured: dict = {}
    chunks = [
        _FakeChunk([_FakeChoice(_FakeDelta(content="hi "))]),
        _FakeChunk([_FakeChoice(_FakeDelta(content="there"), finish_reason="stop")]),
    ]
    _install(monkeypatch, chunks, captured)
    caller = wiring.RegistryModelCaller("bts", _registry())

    result = asyncio.run(wiring.collect_structured(caller, [{"role": "user", "content": "hi"}]))

    assert result == "hi there"


def test_collect_structured_threads_request_profile():
    class _RecordingCaller:
        def __init__(self) -> None:
            self.seen_profile = "unset"

        async def __call__(self, messages, stream=True, request_profile=None):
            self.seen_profile = request_profile
            yield {"type": "content", "delta": "{}"}

    caller = _RecordingCaller()
    profile = {"response_format": {"type": "json_object"}}

    asyncio.run(
        wiring.collect_structured(
            caller, [{"role": "user", "content": "x"}], request_profile=profile
        )
    )

    assert caller.seen_profile == profile


def _profile() -> dict:
    return {
        "tools": [{"type": "function", "function": {"name": "emit"}}],
        "tool_choice": "required",
    }


def test_debug_capture_forwards_request_profile_when_capture_off(monkeypatch):
    captured: dict = {}
    chunks = [_FakeChunk([_FakeChoice(_FakeDelta(content="{}"), finish_reason="stop")])]
    _install(monkeypatch, chunks, captured)
    inner = wiring.RegistryModelCaller("bts", _registry())
    wrapper = DebugCapture(inner, lambda wire: None, enabled=lambda: False)

    asyncio.run(
        wiring.collect_structured(
            wrapper, [{"role": "user", "content": "hi"}], request_profile=_profile()
        )
    )

    assert captured["tool_choice"] == "required"
    assert captured["tools"] == _profile()["tools"]


def test_debug_capture_forwards_request_profile_when_capture_on(monkeypatch):
    captured: dict = {}
    sink_calls: list = []
    chunks = [_FakeChunk([_FakeChoice(_FakeDelta(content="{}"), finish_reason="stop")])]
    _install(monkeypatch, chunks, captured)
    inner = wiring.RegistryModelCaller("bts", _registry())
    wrapper = DebugCapture(inner, lambda wire: sink_calls.append(wire), enabled=lambda: True)

    asyncio.run(
        wiring.collect_structured(
            wrapper, [{"role": "user", "content": "hi"}], request_profile=_profile()
        )
    )

    assert captured["tool_choice"] == "required"
    assert len(sink_calls) == 1


def test_tool_call_events_carry_the_function_name_for_multi_tool_callers(monkeypatch):
    """W9.24 §3.4: a caller running several tools attributes a call by NAME.

    A provider names the call on its first fragment (often with empty arguments)
    and streams the arguments after, so the name is remembered per index and
    stamped on every argument fragment — the BTS read-tools loop resolves its
    report tool this way, and the single-tool customers read the same name.
    """
    captured: dict = {}
    chunks = [
        _FakeChunk(
            [_FakeChoice(_FakeDelta(tool_calls=[_FakeToolCall(_FakeFunction("get_roster", ""))]))]
        ),
        _FakeChunk(
            [_FakeChoice(_FakeDelta(tool_calls=[_FakeToolCall(_FakeFunction(arguments="{}"))]))]
        ),
        _FakeChunk([_FakeChoice(_FakeDelta(), finish_reason="tool_calls")]),
    ]
    _install(monkeypatch, chunks, captured)
    caller = wiring.RegistryModelCaller("bts", _registry())

    events = [event for event in _drain(caller) if event["type"] == "tool_call"]

    assert [event["meta"]["name"] for event in events] == ["get_roster"]
    assert events[0]["delta"] == "{}"  # the name-only fragment adds no payload


def test_tool_call_events_carry_the_call_index_for_parallel_calls(monkeypatch):
    """Two calls in ONE message: each fragment stays attributable to ITS call.

    The index is the provider's grouping key. Without it a consumer can only
    concatenate the fragments, which glued three parallel calls into one
    unparseable argument string (the live gate on :4444).
    """
    captured: dict = {}
    chunks = [
        _FakeChunk(
            [
                _FakeChoice(
                    _FakeDelta(
                        tool_calls=[
                            _FakeToolCall(
                                _FakeFunction("establish_location", '{"name": "69'), index=0
                            ),
                            _FakeToolCall(
                                _FakeFunction("move_being", '{"name": "Mara"'), index=2
                            ),
                        ]
                    )
                )
            ]
        ),
        _FakeChunk(
            [
                _FakeChoice(
                    _FakeDelta(
                        tool_calls=[_FakeToolCall(_FakeFunction(arguments=' Street"}'), index=0)]
                    )
                )
            ]
        ),
        _FakeChunk([_FakeChoice(_FakeDelta(), finish_reason="tool_calls")]),
    ]
    _install(monkeypatch, chunks, captured)
    caller = wiring.RegistryModelCaller("bts", _registry())

    events = [event for event in _drain(caller) if event["type"] == "tool_call"]

    assert [
        (event["meta"]["index"], event["meta"]["name"], event["delta"])
        for event in events
    ] == [
        (0, "establish_location", '{"name": "69'),
        (2, "move_being", '{"name": "Mara"'),
        (0, "establish_location", ' Street"}'),  # the name is remembered per index
    ]
