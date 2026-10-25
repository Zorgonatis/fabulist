"""Context Size, consumer 1: the prompt-budget warn + the sanctioned trim (W9.25 §3.5.1).

Pinned here, with no model and no network:

* the formula — ``available = context_window − response_length − RESERVE_TOKENS``;
  an UNDECLARED window returns ``None`` and produces ZERO new rows (today's
  behavior, byte-identical);
* one ``prompt_budget`` row per over-budget call, carrying est/over/window/response,
  from BOTH seams that build and send a prompt themselves: the streaming caller
  (:class:`~fabulist.wiring.RegistryModelCaller`) and the extractor's direct call;
* the trim — ``make_narrator_message_builder(window_budget_fn=...)`` caps the
  directive budget at ``min(authored, available)``, trimming only the stable
  system block. History is NEVER auto-trimmed (AGENTS 6 cache contract);
* the non-streaming path (``Connection.streaming is False``) synthesizes the SAME
  event protocol as the streamed path: reasoning, DSL-segmented content, one
  tool-call fragment, the usage row and the ``finish_reason=length`` frame.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import fabulist.wiring as wiring
from fabulist.agents.debug import CURRENT_SESSION
from fabulist.connections.registry import Registry
from fabulist.graph.compaction import (
    RESERVE_TOKENS,
    estimate_prompt_tokens,
    prompt_budget_tokens,
    warn_prompt_budget,
)
from fabulist.graph.types import initial_turn_state
from fabulist.log import EventLog
from fabulist.log.schema_gate import validate_event
from fabulist.wiring import make_narrator_message_builder

SESSION = "budget-test"


def run(coro):
    return asyncio.run(coro)


# -- fakes -------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, prompt_tokens: int = 40, completion_tokens: int = 7) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeFunction:
    def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, function: _FakeFunction) -> None:
        self.function = function


class _FakeMessage:
    """The non-streaming ``choices[0].message`` shape."""

    def __init__(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _FakeCompletionChoice:
    def __init__(self, message: _FakeMessage, finish_reason: str | None = None) -> None:
        self.message = message
        self.finish_reason = finish_reason


class _FakeCompletion:
    """The non-streaming ``chat.completions.create`` return value."""

    def __init__(self, choice: _FakeCompletionChoice, usage: object | None) -> None:
        self.choices = [choice]
        self.usage = usage


class _FakeDelta:
    def __init__(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
        reasoning_content: str | None = None,
        index: int = 0,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content
        self.index = index


class _FakeStreamChoice:
    def __init__(self, delta: _FakeDelta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, choices: list, usage: object | None = None) -> None:
        self.choices = choices
        self.usage = usage


class _FakeCompletions:
    """One fake ``completions.create`` serving a stream OR a single response."""

    def __init__(self, chunks: list, completion: object | None, captured: dict) -> None:
        self._chunks = chunks
        self._completion = completion
        self._captured = captured

    async def create(self, **kwargs):
        self._captured.update(kwargs)
        if kwargs.get("stream"):
            async def _stream():
                for chunk in self._chunks:
                    yield chunk

            return _stream()
        return self._completion


class _FakeAsyncClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def _registry(connection: dict, agent: str = "narrator") -> Registry:
    return Registry.from_dict(
        {
            "connections": {
                "c": {
                    "base_url": "http://localhost:4444/v1",
                    "model": "your-model-id",
                    **connection,
                }
            },
            "agents": {agent: "c"},
        }
    )


def _install(monkeypatch, chunks: list, completion: object | None = None) -> dict:
    captured: dict = {}
    fake = _FakeAsyncClient(_FakeCompletions(chunks, completion, captured))
    monkeypatch.setattr(wiring, "_async_client", lambda agent, registry: fake)
    return captured


def _drain(caller, messages):
    return run(_collect(caller, messages))


async def _collect(caller, messages):
    return [event async for event in caller(messages)]


def _budget_rows(log: EventLog, session: str = SESSION) -> list[dict]:
    return [
        event.payload
        for event in log.get_events(session)
        if event.type == "system" and event.payload.get("event") == "prompt_budget"
    ]


def _usage_rows(log: EventLog, session: str = SESSION) -> list[dict]:
    return [
        event.payload
        for event in log.get_events(session)
        if event.type == "system" and event.payload.get("event") == "usage"
    ]


class _Session:
    """Set/restore the CURRENT_SESSION contextvar for one test body."""

    def __init__(self, session_id: str = SESSION) -> None:
        self.session_id = session_id

    def __enter__(self):
        self.token = CURRENT_SESSION.set(self.session_id)
        return self

    def __exit__(self, *exc):
        CURRENT_SESSION.reset(self.token)
        return False


# -- the formula -------------------------------------------------------------


def test_prompt_budget_formula_is_window_minus_response_minus_reserve():
    assert prompt_budget_tokens(4096, 1024) == 4096 - 1024 - RESERVE_TOKENS
    # No declared Context Size = no math at all (None, never a guess).
    assert prompt_budget_tokens(None, 1024) is None
    # A declaration this small cannot fit a prompt: the overshoot is reported, not
    # clamped to zero — the owner needs to see how far over it is.
    assert prompt_budget_tokens(100, 1024) == 100 - 1024 - RESERVE_TOKENS
    # An unauthored Response Length contributes nothing.
    assert prompt_budget_tokens(4096, None) == 4096 - RESERVE_TOKENS


def test_estimate_is_the_char4_heuristic_including_multipart_content():
    assert estimate_prompt_tokens([]) == 0
    assert estimate_prompt_tokens([{"role": "user", "content": "abcd" * 3}]) == 3
    assert estimate_prompt_tokens(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "abcd" * 2},
                    {"type": "image_url", "image_url": {"url": "file:///x.png"}},
                ],
            }
        ]
    ) == 2


# -- the row -----------------------------------------------------------------


def test_warn_appends_one_schema_valid_row_per_over_budget_call(log):
    messages = [{"role": "user", "content": "prose " * 800}]
    recorded = warn_prompt_budget(
        log,
        session_id=SESSION,
        agent="narrator",
        context_window=1024,
        response_length=512,
        messages=messages,
    )
    assert recorded is True

    rows = _budget_rows(log)
    assert len(rows) == 1
    row = rows[0]
    assert row["agent"] == "narrator"
    assert row["est_prompt_tokens"] == estimate_prompt_tokens(messages)
    assert row["context_window"] == 1024
    assert row["response_length"] == 512
    assert row["over_tokens"] == row["est_prompt_tokens"] - (
        1024 - 512 - RESERVE_TOKENS
    )
    # The gate's typed variant accepts exactly what the producer wrote.
    assert validate_event("system", row) == []


def test_warn_is_silent_without_a_declared_window(log):
    messages = [{"role": "user", "content": "prose " * 8000}]
    assert (
        warn_prompt_budget(
            log,
            session_id=SESSION,
            agent="narrator",
            context_window=None,
            response_length=512,
            messages=messages,
        )
        is False
    )
    assert _budget_rows(log) == []


def test_warn_is_silent_under_budget(log):
    assert (
        warn_prompt_budget(
            log,
            session_id=SESSION,
            agent="narrator",
            context_window=65536,
            response_length=1024,
            messages=[{"role": "user", "content": "short"}],
        )
        is False
    )
    assert _budget_rows(log) == []


# -- the streaming caller seam ----------------------------------------------


def test_caller_warns_once_per_over_budget_call(monkeypatch, log):
    _install(
        monkeypatch,
        [
            _FakeChunk([_FakeStreamChoice(_FakeDelta(content="hi"))], usage=_FakeUsage()),
            _FakeChunk([], usage=_FakeUsage()),
        ],
    )
    registry = _registry({"context_window": 1024, "sampling": {"max_tokens": 512}})
    caller = wiring.RegistryModelCaller("narrator", registry, log=log)
    messages = [{"role": "user", "content": "prose " * 800}]

    with _Session():
        _drain(caller, messages)
        _drain(caller, messages)

    rows = _budget_rows(log)
    # Exactly ONE row per call — never one per streamed chunk.
    assert len(rows) == 2
    assert len(_usage_rows(log)) == 2


def test_caller_keeps_todays_behavior_with_no_declared_window(monkeypatch, log):
    _install(
        monkeypatch,
        [_FakeChunk([_FakeStreamChoice(_FakeDelta(content="hi"))], usage=_FakeUsage())],
    )
    registry = _registry({})
    caller = wiring.RegistryModelCaller("narrator", registry, log=log)

    with _Session():
        _drain(caller, [{"role": "user", "content": "prose " * 8000}])

    assert _budget_rows(log) == []
    assert len(_usage_rows(log)) == 1


# -- the trim (directive budget ONLY) ---------------------------------------


def _seed_session(log: EventLog, session: str = SESSION) -> None:
    log.append_event(session, "system", "system", {"card": {"name": "Lyra"}})
    log.append_event(session, "user", "user", {"text": "look around"})
    log.append_event(
        session,
        "narrative",
        "narrator",
        {"prose": "HISTORIC PROSE THAT MUST STAY", "dsl": "", "segments": []},
    )


def _joined(messages: list[dict]) -> str:
    return "\n".join(str(message.get("content", "")) for message in messages)


def test_window_budget_caps_the_directive_block(log):
    _seed_session(log)
    state = initial_turn_state(SESSION, "look around", "narrate")

    unbounded = _joined(make_narrator_message_builder(log)(state))
    assert "identify, draft and implement" in unbounded

    capped = _joined(make_narrator_message_builder(log, window_budget_fn=lambda: 0)(state))
    # Every trimmable directive is gone; the protected core survives untouched.
    assert "identify, draft and implement" not in capped
    assert "You are the Content Engine's entire roster" in capped
    assert "do not resolve the outcome" in capped
    assert len(capped) < len(unbounded)


def test_window_budget_never_trims_history(log):
    _seed_session(log)
    state = initial_turn_state(SESSION, "look again", "narrate")

    capped = make_narrator_message_builder(log, window_budget_fn=lambda: 0)(state)

    # The cache contract (AGENTS 6): the assembler caps the STABLE system block
    # only. Compacted/raw history stays until compaction or the surfaces knobs
    # move it — the warn event is the owner's signal, not a silent rewrite.
    assert "HISTORIC PROSE THAT MUST STAY" in _joined(capped)


def test_the_authored_budget_still_wins_when_it_is_lower(log):
    _seed_session(log)
    state = initial_turn_state(SESSION, "look around", "narrate")
    build = make_narrator_message_builder(
        log,
        directive_config={"budget": 0},
        window_budget_fn=lambda: 10**9,
    )
    joined = _joined(build(state))
    assert "identify, draft and implement" not in joined


def test_no_window_budget_fn_is_byte_identical(log):
    _seed_session(log)
    state = initial_turn_state(SESSION, "look around", "narrate")
    assert make_narrator_message_builder(log)(state) == make_narrator_message_builder(
        log, window_budget_fn=lambda: None
    )(state)


# -- the non-streaming transport --------------------------------------------


CONTENT = "The harbour was wet.\n\n```openui\n{}\n```"
TOOL_ARGS = '{"edits": []}'


def _streamed_trace(monkeypatch, log) -> list[dict]:
    _install(
        monkeypatch,
        [
            _FakeChunk(
                [_FakeStreamChoice(_FakeDelta(reasoning_content="thinking it over"))]
            ),
            _FakeChunk([_FakeStreamChoice(_FakeDelta(content=CONTENT))]),
            _FakeChunk(
                [
                    _FakeStreamChoice(
                        _FakeDelta(
                            tool_calls=[
                                _FakeToolCall(_FakeFunction("patch_prose", TOOL_ARGS))
                            ]
                        )
                    )
                ]
            ),
            _FakeChunk([_FakeStreamChoice(_FakeDelta(), finish_reason="length")]),
            _FakeChunk([], usage=_FakeUsage()),
        ],
    )
    caller = wiring.RegistryModelCaller("narrator", _registry({}), log=log)
    with _Session():
        return _drain(caller, [{"role": "user", "content": "go"}])


def _single_shot_trace(monkeypatch, log):
    captured = _install(
        monkeypatch,
        [],
        _FakeCompletion(
            _FakeCompletionChoice(
                _FakeMessage(
                    content=CONTENT,
                    reasoning_content="thinking it over",
                    tool_calls=[_FakeToolCall(_FakeFunction("patch_prose", TOOL_ARGS))],
                ),
                finish_reason="length",
            ),
            _FakeUsage(),
        ),
    )
    registry = _registry({"streaming": False})
    caller = wiring.RegistryModelCaller("narrator", registry, log=log)
    with _Session():
        events = _drain(caller, [{"role": "user", "content": "go"}])
    return events, captured


def test_non_streaming_synthesizes_the_streaming_protocol(monkeypatch, log):
    streamed = _streamed_trace(monkeypatch, log)
    streamed_usage = _usage_rows(log)

    single, captured = _single_shot_trace(monkeypatch, log)
    single_usage = _usage_rows(log)[len(streamed_usage) :]

    # ONE request, no stream_options — and the response arrives as frames.
    assert captured["stream"] is False
    assert "stream_options" not in captured
    assert single == [
        {"type": "reasoning", "delta": "thinking it over"},
        {"type": "content", "delta": "The harbour was wet.\n\n", "meta": {"channel": "prose"}},
        {"type": "content", "delta": "{}\n", "meta": {"channel": "dsl"}},
        # The stamped ``index`` is the call's grouping key (both paths carry it, so
        # the parity assertion below still holds).
        {
            "type": "tool_call",
            "delta": TOOL_ARGS,
            "meta": {"name": "patch_prose", "index": 0},
        },
        {
            "type": "error",
            "delta": "generation truncated: max_tokens reached before natural stop",
            "meta": {"finish_reason": "length"},
        },
    ]
    # The streamed path produced the same sequence: no consumer can tell them apart.
    assert [(event["type"], event.get("meta")) for event in single] == [
        (event["type"], event.get("meta")) for event in streamed
    ]
    # Both persisted ONE identical usage row (the seam that reads /api/usage).
    assert len(single_usage) == 1
    assert single_usage[0]["agent"] == "narrator"
    assert single_usage[0]["prompt_tokens"] == 40
    assert single_usage[0]["completion_tokens"] == 7


def test_a_present_but_empty_reasoning_content_is_surfaced_on_both_paths(
    monkeypatch, log
):
    """The vendor's continuation turn: ``reasoning_content: ""``, and it is a FACT.

    DeepSeek's thinking mode answers a continuation turn with
    ``{"delta": {"role": "assistant", "content": null,
    "reasoning_content": ""}}`` and no reasoning text at all. A tool loop can only
    repeat the field back (which the vendor REQUIRES — omitting it is a 400) if
    the transport says the field was there, so an empty string must produce a
    ``reasoning`` event. Truthiness here is what made the planner's seed phase
    400 mid-loop, so both transports are pinned.
    """
    _install(
        monkeypatch,
        [
            _FakeChunk([_FakeStreamChoice(_FakeDelta(reasoning_content=""))]),
            _FakeChunk(
                [
                    _FakeStreamChoice(
                        _FakeDelta(
                            tool_calls=[
                                _FakeToolCall(_FakeFunction("patch_prose", TOOL_ARGS))
                            ]
                        )
                    )
                ]
            ),
        ],
    )
    streamed = _drain(
        wiring.RegistryModelCaller("narrator", _registry({}), log=log),
        [{"role": "user", "content": "go"}],
    )

    _install(
        monkeypatch,
        [],
        _FakeCompletion(
            _FakeCompletionChoice(
                _FakeMessage(
                    reasoning_content="",
                    tool_calls=[_FakeToolCall(_FakeFunction("patch_prose", TOOL_ARGS))],
                )
            ),
            _FakeUsage(),
        ),
    )
    single = _drain(
        wiring.RegistryModelCaller("narrator", _registry({"streaming": False}), log=log),
        [{"role": "user", "content": "go"}],
    )

    assert streamed[0] == {"type": "reasoning", "delta": ""}
    assert single[0] == {"type": "reasoning", "delta": ""}


def test_an_absent_reasoning_content_surfaces_no_reasoning_event(monkeypatch, log):
    """A provider that never sets the field keeps today's event list exactly."""
    _install(
        monkeypatch,
        [_FakeChunk([_FakeStreamChoice(_FakeDelta(content="plain"))])],
    )
    streamed = _drain(
        wiring.RegistryModelCaller("narrator", _registry({}), log=log),
        [{"role": "user", "content": "go"}],
    )

    _install(
        monkeypatch,
        [],
        _FakeCompletion(
            _FakeCompletionChoice(_FakeMessage(content="plain")), _FakeUsage()
        ),
    )
    single = _drain(
        wiring.RegistryModelCaller("narrator", _registry({"streaming": False}), log=log),
        [{"role": "user", "content": "go"}],
    )

    assert [event["type"] for event in streamed] == ["content"]
    assert [event["type"] for event in single] == ["content"]


# -- consumer 3: the agent loops' passive headroom readers (W25.5) ----------


def test_narrate_window_budget_reader_is_the_pinned_formula(tmp_path):
    """The reader the narrate repair loop is handed: NARRATE slot, declared window."""
    log = EventLog(tmp_path / "narrate-window.db")
    try:
        declared = _registry(
            {"context_window": 8192, "sampling": {"max_tokens": 1024}}
        )
        reader = wiring.narrate_window_budget(declared, log)
        assert reader() == 8192 - 1024 - RESERVE_TOKENS
        assert reader() == prompt_budget_tokens(8192, 1024)
        # Undeclared window -> None (no headroom rows, the pre-W9.25 shape).
        assert wiring.narrate_window_budget(_registry({}), log)() is None
        # Unresolvable slot -> None, never a raise into the turn.
        assert (
            wiring.narrate_window_budget(_registry({}, agent="elsewhere"), log)()
            is None
        )
    finally:
        log.close()


def test_bts_window_budget_reader_is_the_same_formula_on_the_bts_slot(tmp_path):
    """The BTS read-tools loop's reader: the BTS slot's OWN connection."""
    log = EventLog(tmp_path / "bts-window.db")
    try:
        declared = _registry(
            {"context_window": 16384, "sampling": {"max_tokens": 512}},
            agent="extractor",
        )
        assert (
            wiring.bts_window_budget(declared, log)()
            == 16384 - 512 - RESERVE_TOKENS
        )
        assert (
            wiring.bts_window_budget(
                _registry({}, agent="extractor"), log
            )()
            is None
        )
    finally:
        log.close()
