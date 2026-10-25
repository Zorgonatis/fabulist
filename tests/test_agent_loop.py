"""The agent-loop runtime: unit pins with fakes (W9.24 §3.2, acceptance §8.2).

No test here touches a model. The fakes below are streaming callers over a
scripted list of replies, so the loop's own decisions are what is pinned:

* the loop's ORDER (completion → tool execution → tail append → next completion),
  with the stable prefix byte-identical across iterations (the prompt-cache
  contract, AGENTS 6);
* the injected flags PAIR shape (assistant ``tool_calls`` + its ``role: "tool"``
  result referencing ``engine_flags`` — never a bare tool message);
* a transport timeout raising through: the failed iteration is recorded, the last
  complete text is returned with ``timed_out=True``, and ``timeout_s`` is
  PROVENANCE, never an enforced run-total bound (the owner ruling);
* the no-cap proof: a five-iteration fake converges (and four tool calls run);
* the capabilities ladder's degrade (``looped=False`` → ONE completion, no
  ``request_profile`` on the wire) and ``tool_choice: "auto"``, never forced.

The log pins use a REAL ``EventLog``, so every row the runner appends also passes
the append door's schema gate — the ``agent_loop`` variant is exercised end to end,
not just at the validator.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.agents.loop import (
    ENGINE_FLAGS_CALL_ID,
    ENGINE_FLAGS_TOOL,
    LOOP_EVENT_TEXT_MAX_CHARS,
    LoopInjection,
    LoopTool,
    is_transport_timeout,
    loop_request_profile,
    run_agent_loop,
    should_echo_reasoning,
)
from fabulist.log import EventLog

SESSION_ID = "loop-test"
PREFIX = [
    {"role": "system", "content": "You are the narrator."},
    {"role": "user", "content": "Describe the harbour at dawn."},
]


# -- fakes -------------------------------------------------------------------


class FakeCaller:
    """A streaming caller over scripted replies; records every message list it saw.

    A reply is either an exception instance (raised on the first ``__anext__``)
    or ``{"content": str, "tool_args": str, "tool_name": str, "finish_reason": str}``.

    ``tool_args``/``tool_name`` stream ONE call fragment. ``tool_fragments`` streams
    several calls the way the transport really does: one event per fragment, each
    carrying its call ``index`` and (when the provider announced it) the call
    ``name`` — INTERLEAVED across calls, which is what used to corrupt them.

    ``reasoning`` (or ``reasoning_fragments`` for a split stream) emits the
    provider's ``reasoning_content`` deltas BEFORE the content/tool-call frames —
    the order a thinking model streams them in. A reply that carries the KEY with
    an empty value is the transport's "present but empty" case (the field was
    sent, the text is nothing), which is a different fact from omitting it.
    """

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "stream": stream,
                "request_profile": request_profile,
            }
        )
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        for fragment in reply.get("reasoning_fragments") or ():
            yield {"type": "reasoning", "delta": fragment}
        if "reasoning" in reply:
            yield {"type": "reasoning", "delta": reply["reasoning"] or ""}
        if reply.get("content"):
            yield {"type": "content", "delta": reply["content"], "meta": {"channel": "prose"}}
        if reply.get("tool_args"):
            event = {"type": "tool_call", "delta": reply["tool_args"]}
            if reply.get("tool_name"):
                event["meta"] = {"name": reply["tool_name"]}
            yield event
        for fragment in reply.get("tool_fragments") or ():
            meta = {"index": fragment["index"]}
            if fragment.get("name"):
                meta["name"] = fragment["name"]
            yield {
                "type": "tool_call",
                "delta": fragment.get("delta") or "",
                "meta": meta,
            }
        if reply.get("finish_reason"):
            yield {
                "type": "error",
                "delta": "generation truncated",
                "meta": {"finish_reason": reply["finish_reason"]},
            }


def echo_tool(calls: list[dict] | None = None) -> LoopTool:
    """A trivial single-tool wrapper: records its args, answers ``ok``."""

    async def fn(args: dict) -> dict:
        if calls is not None:
            calls.append(args)
        return {"ok": True, "seen": args}

    return LoopTool(
        name="leave_note",
        description="Record a short note in the engine's scratchpad.",
        parameters={
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
        fn=fn,
    )


def recording_tool(name: str, executed: list[tuple[str, dict]]) -> LoopTool:
    """One of several tools: records ``(name, args)`` in EXECUTION order."""

    async def fn(args: dict) -> dict:
        executed.append((name, args))
        return {"ok": True, "tool": name}

    return LoopTool(
        name=name,
        description=f"The {name} tool.",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}},
        fn=fn,
    )


def rows(log: EventLog, session_id: str = SESSION_ID) -> list[dict]:
    """The ``agent_loop`` payloads the runner appended, in append order."""
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.type == "system" and event.payload.get("event") == "agent_loop"
    ]


def kinds(payloads: list[dict]) -> list[str]:
    return [payload["kind"] for payload in payloads]


FUNCTION_CALLING = {"function_calling": True}


# -- order + the prompt-cache contract ---------------------------------------


def test_a_tool_call_round_trips_through_the_tail_and_the_prefix_never_moves(tmp_path):
    """completion → tool_call → tool_result → completion → final, prefix untouched."""
    executed: list[dict] = []
    caller = FakeCaller(
        [
            {"tool_args": '{"note": "BANANA"}'},
            {"content": "I noted the word BANANA."},
        ]
    )
    log = EventLog(tmp_path / "loop.db")

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool(executed)],
            surface="narrate",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    # The tool ran engine-side with the parsed arguments, exactly once.
    assert executed == [{"note": "BANANA"}]
    assert result.text == "I noted the word BANANA."
    assert result.tool_args == '{"note": "BANANA"}'
    assert result.looped is True
    assert result.timed_out is False
    assert len(result.iterations) == 2
    assert [iteration.tool for iteration in result.iterations] == ["leave_note", None]

    # The caller's prefix list is untouched, and iteration 2's list is iteration
    # 1's list plus the assistant tool-call turn and its tool result AT THE TAIL.
    assert PREFIX == [
        {"role": "system", "content": "You are the narrator."},
        {"role": "user", "content": "Describe the harbour at dawn."},
    ]
    first, second = caller.calls[0]["messages"], caller.calls[1]["messages"]
    assert first == PREFIX
    assert second[: len(PREFIX)] == PREFIX  # the stable prefix, byte-identical
    assistant_turn, tool_turn = second[len(PREFIX) :]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "leave_note", "arguments": '{"note": "BANANA"}'},
        }
    ]
    assert tool_turn == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": json.dumps({"ok": True, "seen": {"note": "BANANA"}}),
    }

    # The trace: one row per measured step, in order, with real latencies.
    payloads = rows(log)
    assert kinds(payloads) == [
        "completion",
        "tool_call",
        "tool_result",
        "completion",
        "final",
    ]
    assert [payload["iteration"] for payload in payloads] == [1, 1, 1, 2, 2]
    assert [payload["surface"] for payload in payloads] == ["narrate"] * 5
    assert [payload["kind"] for payload in payloads if "latency_s" in payload] == [
        "completion",
        "tool_result",
        "completion",
    ]
    for payload in payloads:
        if "latency_s" in payload:
            assert isinstance(payload["latency_s"], float)
            assert payload["latency_s"] >= 0.0
    assert payloads[1]["tool"] == "leave_note"
    assert payloads[1]["args"] == {"note": "BANANA"}  # parsed, not the raw fragment
    assert payloads[1]["injected"] is False
    assert payloads[2]["result"] == {"ok": True, "seen": {"note": "BANANA"}}
    assert payloads[-1]["timed_out"] is False
    # The rows reached the log through the append door's schema gate (a malformed
    # row would have raised in append_event, not landed silently).
    assert all(event.actor == "engine" for event in log.get_events(SESSION_ID))


def test_the_request_profile_rides_every_completion_with_auto_never_forced():
    """Tier A: tools + ``tool_choice: "auto"``; a forced choice is never used."""
    caller = FakeCaller([{"content": "no tool needed"}])
    capabilities = {
        "function_calling": True,
        "tool_choice_forced": True,  # measured on :4444 — still never forced
    }

    asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=None,
            session_id=SESSION_ID,
            capabilities=capabilities,
        )
    )

    profile = caller.calls[0]["request_profile"]
    assert profile["tool_choice"] == "auto"
    assert [tool["function"]["name"] for tool in profile["tools"]] == ["leave_note"]


# -- the injected flags pair -------------------------------------------------


def test_the_engine_hook_injects_the_flags_pair_never_a_bare_tool_message(tmp_path):
    caller = FakeCaller(
        [
            {"content": "The harbour was very very wet."},
            {"tool_args": '{"edits": [{"find": "very very", "replace": "utterly"}]}'},
            {"content": "The harbour was utterly wet."},
        ]
    )
    log = EventLog(tmp_path / "loop.db")
    seen: list[str] = []

    def hook(text: str) -> LoopInjection | None:
        seen.append(text)
        if text.startswith("The harbour was very very"):
            return LoopInjection(content='{"ok": false, "flags": {"slop": ["very very"]}}')
        return None

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=log,
            session_id=SESSION_ID,
            on_completion=hook,
            capabilities=FUNCTION_CALLING,
        )
    )

    # The hook ran after EVERY completion (the draft, the patch call, the answer).
    assert len(seen) == 3
    assert result.text == "The harbour was utterly wet."

    # The pair: the draft as its own assistant message, then the synthetic
    # assistant tool_calls message, then the tool result referencing its id.
    second = caller.calls[1]["messages"]
    draft, synthetic, tool_result = second[len(PREFIX) :]
    assert draft == {"role": "assistant", "content": "The harbour was very very wet."}
    assert synthetic["role"] == "assistant"
    assert synthetic["content"] == ""
    assert synthetic["tool_calls"] == [
        {
            "id": ENGINE_FLAGS_CALL_ID,
            "type": "function",
            "function": {"name": ENGINE_FLAGS_TOOL, "arguments": "{}"},
        }
    ]
    assert tool_result["role"] == "tool"
    assert tool_result["tool_call_id"] == ENGINE_FLAGS_CALL_ID
    assert json.loads(tool_result["content"]) == {
        "ok": False,
        "flags": {"slop": ["very very"]},
    }
    # No BARE tool message anywhere in the conversation (forbidden on strict
    # transports): every tool turn references a tool call in the message before it.
    for messages in (caller.calls[0]["messages"], second, caller.calls[2]["messages"]):
        for index, message in enumerate(messages):
            if message["role"] == "tool" and index:
                previous = messages[index - 1]
                called_ids = {
                    call["id"] for call in previous.get("tool_calls") or ()
                }
                assert message["tool_call_id"] in called_ids

    payloads = rows(log)
    assert kinds(payloads) == [
        "completion",
        "tool_call",
        "tool_result",
        "completion",
        "tool_call",
        "tool_result",
        "completion",
        "final",
    ]
    injected_rows = [payload for payload in payloads if payload.get("injected")]
    assert [payload["kind"] for payload in injected_rows] == ["tool_call", "tool_result"]
    assert {payload["tool"] for payload in injected_rows} == {ENGINE_FLAGS_TOOL}


def test_an_async_hook_is_awaited_and_an_unknown_return_type_is_a_bug():
    caller = FakeCaller(
        [
            {"content": "draft"},
            {"content": "clean draft"},
        ]
    )

    async def hook(text: str) -> LoopInjection | None:
        if text == "draft":
            return LoopInjection(content='{"ok": false}')
        return None

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=None,
            session_id=SESSION_ID,
            on_completion=hook,
            capabilities=FUNCTION_CALLING,
        )
    )
    assert result.text == "clean draft"
    assert len(result.iterations) == 2

    bad = FakeCaller([{"content": "draft"}])
    with pytest.raises(TypeError, match="on_completion must return"):
        asyncio.run(
            run_agent_loop(
                caller=bad,
                messages=PREFIX,
                tools=[echo_tool()],
                surface="narrate",
                log=None,
                session_id=SESSION_ID,
                on_completion=lambda text: "flags",
            )
        )


# -- hang protection: per connection, never a run total ----------------------


def test_a_transport_timeout_records_the_failed_iteration_and_keeps_the_draft(tmp_path):
    """The 2nd call times out: last complete text returned, ``timed_out=True``."""
    caller = FakeCaller(
        [
            {"content": "The harbour was very very wet."},
            TimeoutError("connection timed out"),
        ]
    )
    log = EventLog(tmp_path / "loop.db")

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=log,
            session_id=SESSION_ID,
            on_completion=lambda text: LoopInjection(content='{"ok": false}'),
            timeout_s=900.0,
            capabilities=FUNCTION_CALLING,
        )
    )

    assert result.timed_out is True
    assert result.text == "The harbour was very very wet."  # the draft survived
    assert result.tool_args is None
    payloads = rows(log)
    assert kinds(payloads) == [
        "completion",
        "tool_call",
        "tool_result",
        "timeout",
        "final",
    ]
    timeout_row = payloads[3]
    assert timeout_row["iteration"] == 2
    assert "TimeoutError" in timeout_row["error"]
    assert timeout_row["timeout_s"] == 900.0
    assert isinstance(timeout_row["latency_s"], float)
    assert payloads[-1]["timed_out"] is True
    assert payloads[-1]["text"] == "The harbour was very very wet."
    # Rows already appended stay (D9 append-only): the completed iteration, the
    # injected pair and the timeout all remain in the log.
    assert len(payloads) == 5


def test_transport_timeout_detection_duck_types_wrapped_client_errors():
    """``openai``/``httpx`` timeout classes need no import; other errors raise."""

    class APITimeoutError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    assert is_transport_timeout(TimeoutError())
    assert is_transport_timeout(asyncio.TimeoutError())
    assert is_transport_timeout(APITimeoutError("request timed out"))
    wrapped = RuntimeError("client blew up")
    wrapped.__cause__ = APITimeoutError("underlying timeout")
    assert is_transport_timeout(wrapped)
    assert not is_transport_timeout(APIConnectionError("connection refused"))
    assert not is_transport_timeout(ValueError("bad"))


def test_a_non_timeout_failure_raises_through_untouched():
    """Only timeouts are recovered; anything else is the caller's to handle."""
    caller = FakeCaller([{"tool_args": '{"note": "x"}'}, ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(
            run_agent_loop(
                caller=caller,
                messages=PREFIX,
                tools=[echo_tool()],
                surface="narrate",
                log=None,
                session_id=SESSION_ID,
                capabilities=FUNCTION_CALLING,
            )
        )


def test_timeout_s_is_provenance_and_never_an_enforced_run_total_bound():
    """The retired run-total ``wait_for`` is gone: a slow loop is never cut off.

    ``timeout_s`` here is far SHORTER than the loop's real wall clock. If the
    runtime enforced it (the forbidden run-total pattern) the turn would be
    killed; instead each call rides the caller's own transport and the loop
    finishes normally.
    """

    async def slow_content(text: str):
        await asyncio.sleep(0.02)
        yield {"type": "content", "delta": text}

    class SlowCaller:
        def __init__(self) -> None:
            self.n = 0

        async def __call__(self, messages, stream=True, request_profile=None):
            self.n += 1
            if self.n <= 4:
                yield {"type": "tool_call", "delta": '{"note": "again"}'}
                return
            async for event in slow_content("done after five completions"):
                yield event

    caller = SlowCaller()
    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=None,
            session_id=SESSION_ID,
            timeout_s=0.001,  # would fire instantly if the runtime enforced it
            capabilities=FUNCTION_CALLING,
        )
    )
    assert result.timed_out is False
    assert result.text == "done after five completions"


# -- no iteration caps -------------------------------------------------------


def test_five_iterations_converge_with_no_cap():
    """§8.2's no-cap proof: four tool calls then the answer — nothing stops it."""
    executed: list[dict] = []
    caller = FakeCaller(
        [
            {"tool_args": '{"note": "1"}'},
            {"tool_args": '{"note": "2"}'},
            {"tool_args": '{"note": "3"}'},
            {"tool_args": '{"note": "4"}'},
            {"content": "All four notes are recorded."},
        ]
    )

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool(executed)],
            surface="bts",
            log=None,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    assert len(caller.calls) == 5
    assert [entry["note"] for entry in executed] == ["1", "2", "3", "4"]
    assert [iteration.iteration for iteration in result.iterations] == [1, 2, 3, 4, 5]
    assert result.text == "All four notes are recorded."
    assert result.timed_out is False
    # The tail grows monotonically: five completions saw 2, 4, 6, 8, 10 messages.
    assert [len(call["messages"]) for call in caller.calls] == [2, 4, 6, 8, 10]


# -- the capabilities ladder's degrade --------------------------------------


def test_without_function_calling_the_runtime_does_one_completion(tmp_path):
    """``looped=False``: no tools on the wire, no profile, one completion."""
    caller = FakeCaller([{"content": "A one-shot answer."}])
    log = EventLog(tmp_path / "loop.db")

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="bts",
            log=log,
            session_id=SESSION_ID,
            capabilities={"function_calling": False},
        )
    )

    assert result.looped is False
    assert result.text == "A one-shot answer."
    assert len(caller.calls) == 1
    assert caller.calls[0]["request_profile"] is None
    assert kinds(rows(log)) == ["completion", "final"]


def test_an_unknown_capability_set_degrades_rather_than_assuming():
    """The safe default (``resolve_capabilities``): unmeasured = not supported."""
    assert loop_request_profile(None, [echo_tool()]) is None
    assert loop_request_profile({}, [echo_tool()]) is None
    assert loop_request_profile(FUNCTION_CALLING, []) is None
    profile = loop_request_profile({"function_calling": True}, [echo_tool()])
    assert profile is not None and profile["tool_choice"] == "auto"


# -- tool execution: guard, failures, attribution ---------------------------


def test_malformed_tool_arguments_become_an_error_result_the_model_can_repair(tmp_path):
    executed: list[dict] = []
    log = EventLog(tmp_path / "loop.db")
    caller = FakeCaller(
        [
            {"tool_args": '{"note": '},  # truncated JSON
            {"content": "Understood, no note taken."},
        ]
    )

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool(executed)],
            surface="narrate",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    assert executed == []  # the tool never ran
    # The row keeps the unparseable text it could not parse (the raw fragment).
    assert rows(log)[1]["args"] == {"raw": '{"note": '}
    assert result.text == "Understood, no note taken."
    tool_turn = caller.calls[1]["messages"][-1]
    assert tool_turn["role"] == "tool"
    payload = json.loads(tool_turn["content"])
    assert payload["ok"] is False
    assert payload["error"].startswith("malformed tool arguments")


def test_a_raising_tool_is_a_result_not_a_dead_turn():
    def boom(args: dict) -> dict:
        raise RuntimeError("disk full")

    caller = FakeCaller(
        [{"tool_args": '{"note": "x"}'}, {"content": "I could not record that."}]
    )
    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[
                LoopTool(
                    name="leave_note", description="note", parameters={}, fn=boom
                )
            ],
            surface="narrate",
            log=None,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )
    assert result.text == "I could not record that."
    payload = json.loads(caller.calls[1]["messages"][-1]["content"])
    assert payload == {"ok": False, "error": "tool 'leave_note' raised RuntimeError: disk full"}


def test_a_sync_tool_runs_and_the_execution_guard_bounds_it(monkeypatch):
    """Sync wrappers run in a worker thread; the guard is engine-side and plain."""
    import fabulist.agents.loop as loop_module

    monkeypatch.setattr(loop_module, "TOOL_EXECUTION_GUARD_SECONDS", 0.05)

    def slow_sync(args: dict) -> dict:
        import time as time_module

        time_module.sleep(0.5)
        return {"ok": True}

    def fast_sync(args: dict) -> dict:
        return {"ok": True, "seen": args}

    caller = FakeCaller(
        [
            {"tool_args": '{"note": "slow"}'},
            {"content": "The tool was too slow."},
        ]
    )
    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[LoopTool(name="leave_note", description="n", parameters={}, fn=slow_sync)],
            surface="narrate",
            log=None,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )
    assert result.timed_out is False  # a slow TOOL is not a transport timeout
    payload = json.loads(caller.calls[1]["messages"][-1]["content"])
    assert payload["ok"] is False
    assert "execution guard" in payload["error"]

    # The same seam with a sync fn under the guard returns its dict.
    fast = FakeCaller([{"tool_args": '{"note": "fast"}'}, {"content": "noted"}])
    asyncio.run(
        run_agent_loop(
            caller=fast,
            messages=PREFIX,
            tools=[LoopTool(name="leave_note", description="n", parameters={}, fn=fast_sync)],
            surface="narrate",
            log=None,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )
    assert json.loads(fast.calls[1]["messages"][-1]["content"]) == {
        "ok": True,
        "seen": {"note": "fast"},
    }


def test_a_transport_surfaced_tool_name_wins_and_an_unnamed_multi_tool_call_fails_open(
    tmp_path,
):
    """Attribution: ``meta.name`` first, single-tool default, else the text stands."""
    other = LoopTool(
        name="read_state", description="read", parameters={}, fn=lambda args: {"ok": True}
    )
    named = FakeCaller(
        [
            {"tool_args": '{"note": "hi"}', "tool_name": "leave_note"},
            {"content": "noted"},
        ]
    )
    result = asyncio.run(
        run_agent_loop(
            caller=named,
            messages=PREFIX,
            tools=[echo_tool(), other],
            surface="bts",
            log=None,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )
    assert result.iterations[0].tool == "leave_note"

    log = EventLog(tmp_path / "loop.db")
    unnamed = FakeCaller([{"content": "Some prose.", "tool_args": '{"note": "hi"}'}])
    result = asyncio.run(
        run_agent_loop(
            caller=unnamed,
            messages=PREFIX,
            tools=[echo_tool(), other],
            surface="bts",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )
    assert result.text == "Some prose."  # fail-open: never a hang, never a raise
    assert len(unnamed.calls) == 1
    payloads = rows(log)
    assert kinds(payloads) == ["completion", "tool_call", "final"]
    assert payloads[1]["tool"] == ""
    assert payloads[1]["error"] == "unattributable tool call"


# -- parallel tool calls: grouped by index, never concatenated ----------------

def test_three_parallel_tool_calls_each_get_their_own_rows_results_and_messages(
    tmp_path,
):
    """The live-gate defect: three calls in ONE message are three executions.

    The transport stamps ``meta["index"]`` on every fragment, so the interleaved
    groups are three distinct argument strings. Before the fix they were glued into
    one payload (``...{"name":"Common...``) and the tool refused it, aborting the
    phase.
    """
    executed: list[tuple[str, dict]] = []
    tools = [
        recording_tool("establish_location", executed),
        recording_tool("move_being", executed),
    ]
    streamed = {
        "tool_fragments": [
            {"index": 0, "name": "establish_location", "delta": '{"name": "The Crimson'},
            {"index": 1, "name": "establish_location", "delta": '{"name": "Common'},
            {"index": 2, "name": "move_being"},  # a name-only first fragment
            {"index": 0, "delta": ' Lantern", "note": "the main room"}'},
            {
                "index": 2,
                "delta": '{"name": "Mara", "to": "loc_the_crimson_lantern"}',
            },
            {
                "index": 1,
                "delta": ' Room", "parent_id": "loc_the_crimson_lantern"}',
            },
        ]
    }
    caller = FakeCaller([streamed, {"content": "Three places recorded."}])
    log = EventLog(tmp_path / "loop.db")

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=tools,
            surface="bts",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    # EVERY tool ran, in index order, with ITS OWN parsed arguments — no glue.
    assert executed == [
        ("establish_location", {"name": "The Crimson Lantern", "note": "the main room"}),
        ("establish_location", {"name": "Common Room", "parent_id": "loc_the_crimson_lantern"}),
        ("move_being", {"name": "Mara", "to": "loc_the_crimson_lantern"}),
    ]
    assert result.text == "Three places recorded."
    assert result.looped is True
    assert len(result.iterations) == 2  # one completion is one iteration
    assert result.iterations[0].tool == "establish_location"

    # ONE assistant message carries all three calls; each has its OWN tool result.
    second = caller.calls[1]["messages"]
    assert second[: len(PREFIX)] == PREFIX
    assistant_turn, *tool_turns = second[len(PREFIX) :]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["tool_calls"] == [
        {
            "id": "call_1_0",
            "type": "function",
            "function": {
                "name": "establish_location",
                "arguments": '{"name": "The Crimson Lantern", "note": "the main room"}',
            },
        },
        {
            "id": "call_1_1",
            "type": "function",
            "function": {
                "name": "establish_location",
                "arguments": (
                    '{"name": "Common Room", "parent_id": "loc_the_crimson_lantern"}'
                ),
            },
        },
        {
            "id": "call_1_2",
            "type": "function",
            "function": {
                "name": "move_being",
                "arguments": '{"name": "Mara", "to": "loc_the_crimson_lantern"}',
            },
        },
    ]
    assert [turn["role"] for turn in tool_turns] == ["tool", "tool", "tool"]
    assert [turn["tool_call_id"] for turn in tool_turns] == [
        "call_1_0",
        "call_1_1",
        "call_1_2",
    ]
    assert json.loads(tool_turns[2]["content"]) == {"ok": True, "tool": "move_being"}

    # The trace shows ALL of them: one tool_call row + one tool_result row per call.
    payloads = rows(log)
    assert kinds(payloads) == [
        "completion",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "completion",
        "final",
    ]
    assert [payload["tool"] for payload in payloads if payload["kind"] == "tool_call"] == [
        "establish_location",
        "establish_location",
        "move_being",
    ]
    assert payloads[1]["args"] == {"name": "The Crimson Lantern", "note": "the main room"}
    assert payloads[5]["args"] == {"name": "Mara", "to": "loc_the_crimson_lantern"}
    assert payloads[2]["result"] == {"ok": True, "tool": "establish_location"}


def test_a_malformed_call_is_refused_individually_and_its_sibling_still_runs(tmp_path):
    """A bad argument string costs ONE call, not the phase."""
    executed: list[tuple[str, dict]] = []
    tools = [
        recording_tool("establish_location", executed),
        recording_tool("move_being", executed),
    ]
    broken = '{"name": "Stair"}{"name": "Cellar"}'  # the glued-defect string
    caller = FakeCaller(
        [
            {
                "tool_fragments": [
                    {"index": 0, "name": "establish_location", "delta": broken},
                    {
                        "index": 1,
                        "name": "move_being",
                        "delta": '{"name": "Mara", "to": "loc_stair"}',
                    },
                ]
            },
            {"content": "Fixed what I could."},
        ]
    )
    log = EventLog(tmp_path / "loop.db")

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=tools,
            surface="bts",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    assert executed == [("move_being", {"name": "Mara", "to": "loc_stair"})]
    assert result.text == "Fixed what I could."

    # Each call still has its OWN result message: the refused one carries the
    # existing `_execute_tool` refusal, the sibling carries its real result.
    messages = caller.calls[1]["messages"]
    tool_turns = [message for message in messages if message["role"] == "tool"]
    assert [turn["tool_call_id"] for turn in tool_turns] == ["call_1_0", "call_1_1"]
    refusal = json.loads(tool_turns[0]["content"])
    assert refusal["ok"] is False
    assert "malformed tool arguments" in refusal["error"]
    assert json.loads(tool_turns[1]["content"]) == {"ok": True, "tool": "move_being"}

    payloads = rows(log)
    assert [payload["tool"] for payload in payloads if payload["kind"] == "tool_call"] == [
        "establish_location",
        "move_being",
    ]
    # The refused call's row keeps the raw fragment; the arguments never parsed.
    assert payloads[1]["args"] == {"raw": broken}
    assert payloads[2]["result"]["ok"] is False


def test_an_unnamed_call_among_several_is_refused_and_its_siblings_run(tmp_path):
    """Several tools, no surfaced name: that call is refused, the others are not."""
    executed: list[tuple[str, dict]] = []
    tools = [
        recording_tool("establish_location", executed),
        recording_tool("move_being", executed),
    ]
    caller = FakeCaller(
        [
            {
                "tool_fragments": [
                    {"index": 0, "delta": '{"name": "Stair"}'},  # never named
                    {
                        "index": 1,
                        "name": "move_being",
                        "delta": '{"name": "Mara", "to": "loc_stair"}',
                    },
                ]
            },
            {"content": "Moved."},
        ]
    )

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=tools,
            surface="bts",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    assert executed == [("move_being", {"name": "Mara", "to": "loc_stair"})]
    assert result.text == "Moved."
    messages = caller.calls[1]["messages"]
    tool_turns = [message for message in messages if message["role"] == "tool"]
    assert [turn["tool_call_id"] for turn in tool_turns] == ["call_1_0", "call_1_1"]
    assert json.loads(tool_turns[0]["content"]) == {
        "ok": False,
        "error": "unattributable tool call",
    }
    assert json.loads(tool_turns[1]["content"]) == {"ok": True, "tool": "move_being"}


def test_fragments_of_one_index_concatenate_into_one_call(tmp_path):
    """The streaming single-call case: same-index fragments are ONE argument string."""
    executed: list[dict] = []
    caller = FakeCaller(
        [
            {
                "tool_fragments": [
                    {"index": 0, "name": "leave_note", "delta": '{"note": "BAN'},
                    {"index": 0, "delta": 'ANA"}'},
                ]
            },
            {"content": "Noted BANANA."},
        ]
    )
    log = EventLog(tmp_path / "loop.db")

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool(executed)],
            surface="narrate",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    assert executed == [{"note": "BANANA"}]
    assert result.tool_args == '{"note": "BANANA"}'
    # A single call keeps the historic id and the single-pair row shape.
    assistant_turn, tool_turn = caller.calls[1]["messages"][len(PREFIX) :]
    assert assistant_turn["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "leave_note", "arguments": '{"note": "BANANA"}'},
        }
    ]
    assert tool_turn["tool_call_id"] == "call_1"
    assert kinds(rows(log)) == [
        "completion",
        "tool_call",
        "tool_result",
        "completion",
        "final",
    ]


# -- trace hygiene -----------------------------------------------------------


def test_long_payloads_are_clipped_and_finish_reason_is_surfaced(tmp_path):
    long_text = "x" * (LOOP_EVENT_TEXT_MAX_CHARS + 500)
    caller = FakeCaller(
        [{"content": long_text, "finish_reason": "length"}]
    )
    log = EventLog(tmp_path / "loop.db")

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    payloads = rows(log)
    assert payloads[0]["text"] == "x" * LOOP_EVENT_TEXT_MAX_CHARS
    assert payloads[0]["finish_reason"] == "length"
    assert payloads[-1]["text"] == "x" * LOOP_EVENT_TEXT_MAX_CHARS
    # The ANSWER is not clipped — only the trace is.
    assert result.text == long_text


def test_a_log_hiccup_or_a_missing_session_costs_the_row_not_the_turn():
    class BrokenLog:
        def append_event(self, *args, **kwargs):
            raise RuntimeError("log is down")

    caller = FakeCaller([{"content": "answer"}])
    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=(),
            surface="narrate",
            log=BrokenLog(),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )
    assert result.text == "answer"

    silent = FakeCaller([{"content": "answer"}])
    result = asyncio.run(
        run_agent_loop(
            caller=silent,
            messages=PREFIX,
            tools=(),
            surface="narrate",
            log=None,
            session_id="",
            capabilities=FUNCTION_CALLING,
        )
    )
    assert result.text == "answer"
    assert result.looped is False


def test_tool_args_is_none_when_no_tool_was_ever_called():
    caller = FakeCaller([{"content": "straight answer"}])
    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=None,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )
    assert result.tool_args is None
    assert result.looped is True
    assert len(result.iterations) == 1
    assert result.iterations[0].tool is None


# -- the live pass (§8.2): a real caller, one tool the model actually calls ----


LIVE_SKIP = pytest.mark.skipif(
    __import__("os").environ.get("FABULIST_LIVE_MODEL") != "1",
    reason="set FABULIST_LIVE_MODEL=1 to drive the live :4444 model",
)


def _live_config(tmp_path):
    """A scratch copy of the connections file with ``narrator`` on the local model.

    ``configs/connections.json`` is gitignored user state, so the acceptance run
    points at it explicitly (``FABULIST_LIVE_CONFIG``); the worktree copy is the
    fallback. The narrator is re-bound to the local ``your-model-id`` connection — this
    test must not spend a cloud call.
    """
    import os
    from pathlib import Path

    source = os.environ.get("FABULIST_LIVE_CONFIG") or str(
        Path(__file__).resolve().parents[1] / "configs" / "connections.json"
    )
    path = Path(source)
    if not path.is_file():
        return None
    document = json.loads(path.read_text())
    document["agents"]["narrator"] = "your-model-id"
    target = tmp_path / "connections.live.json"
    target.write_text(json.dumps(document))
    return target


@LIVE_SKIP
def test_live_two_iteration_loop_against_the_local_model(tmp_path):
    """W2 live (§8.2): REAL ``RegistryModelCaller`` → tool call → final answer.

    One trivial ``leave_note`` tool over the real transport: completion 1 calls
    it, completion 2 answers from its result. The ``agent_loop`` rows land in
    order with real latencies, and the second completion rides the unchanged
    prefix (the prompt-cache contract) — compare its latency against W1's probe.

    Run deliberately::

        FABULIST_LIVE_MODEL=1 \
        FABULIST_LIVE_CONFIG=~/Projects/Fabulist/configs/connections.json \
            uv run pytest tests/test_agent_loop.py -q -s -k live
    """
    from fabulist.agents.debug import CURRENT_SESSION
    from fabulist.connections.capabilities import resolve_capabilities
    from fabulist.connections.client import configure
    from fabulist.wiring import RegistryModelCaller

    config = _live_config(tmp_path)
    if config is None:
        pytest.skip("no connections file at FABULIST_LIVE_CONFIG")

    registry = configure(config)
    connection = registry.connection_for("narrator")
    log = EventLog(tmp_path / "live.db")
    noted: list[dict] = []
    caller = RegistryModelCaller("narrator", registry, log=log)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a terse assistant. You have one tool, leave_note: it "
                "records a short note. When the user asks you to note something, "
                "call leave_note with it, then confirm in one short sentence."
            ),
        },
        {
            "role": "user",
            "content": "Note the word BANANA with leave_note, then tell me what you noted.",
        },
    ]

    token = CURRENT_SESSION.set(SESSION_ID)
    try:
        result = asyncio.run(
            run_agent_loop(
                caller=caller,
                messages=messages,
                tools=[echo_tool(noted)],
                surface="narrate",
                log=log,
                session_id=SESSION_ID,
                timeout_s=float(connection.timeout or 0) or None,
                capabilities=resolve_capabilities(connection),
            )
        )
    finally:
        CURRENT_SESSION.reset(token)

    payloads = rows(log)
    print("\nlive agent_loop trace:")
    for payload in payloads:
        latency = f"{payload['latency_s']:.2f}s" if "latency_s" in payload else "-"
        print(
            f"  iter {payload['iteration']} {payload['kind']:11s} "
            f"tool={payload.get('tool')!r:18s} latency={latency}"
        )
    print(f"  final text: {result.text!r}\n")

    assert result.looped is True, "the ladder degraded: no function calling resolved"
    assert result.timed_out is False
    assert noted and noted[0].get("note", "").upper().find("BANANA") >= 0
    assert len(result.iterations) == 2, [i.tool for i in result.iterations]
    assert kinds(payloads) == [
        "completion",
        "tool_call",
        "tool_result",
        "completion",
        "final",
    ]
    assert "banana" in result.text.lower()
    # Usage rows rode the existing accounting: one per model call.
    usage = [
        event.payload
        for event in log.get_events(SESSION_ID)
        if event.type == "system" and event.payload.get("event") == "usage"
    ]
    assert len(usage) == 2
    assert all(payload["latency_s"] > 0 for payload in payloads if "latency_s" in payload)


# -- context-size headroom: PASSIVE only (W9.25 §3.5.3) ----------------------
#
# Owner ruling on F1: ASSUME ALL CONTEXT FITS this session. The loop records the
# headroom it saw and PROCEEDS regardless — no stop condition, no heuristic gate,
# no `window_exhausted`. W9.26 owns the compaction-aware design.


def _three_iteration_caller() -> FakeCaller:
    return FakeCaller(
        [
            {"tool_args": '{"note": "one"}'},
            {"tool_args": '{"note": "two"}'},
            {"content": "Both notes are recorded."},
        ]
    )


def test_headroom_rows_decrease_and_the_loop_proceeds_past_zero(tmp_path):
    log = EventLog(tmp_path / "loop.db")
    result = asyncio.run(
        run_agent_loop(
            caller=_three_iteration_caller(),
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
            # Deliberately tiny: iteration 1 is ALREADY over budget, and the loop
            # must still run every iteration to a real answer.
            window_fn=lambda: 16,
        )
    )

    payloads = rows(log)
    by_iteration: dict[int, int] = {}
    for payload in payloads:
        assert "headroom" in payload, payload
        by_iteration.setdefault(payload["iteration"], payload["headroom"])
    assert sorted(by_iteration) == [1, 2, 3]
    headrooms = [by_iteration[iteration] for iteration in sorted(by_iteration)]
    # Append-only tail → monotonic decrease; it crossed zero mid-run and the loop
    # kept going to the model's own answer.
    assert headrooms[0] > headrooms[1] > headrooms[2]
    assert headrooms[0] >= 0 > headrooms[-1]
    # NO STOP SHIPPED: the run ended on the model's own answer, not on a window.
    assert result.text == "Both notes are recorded."
    assert result.timed_out is False
    assert kinds(payloads)[-1] == "final"
    assert all(payload["kind"] != "window_exhausted" for payload in payloads)


def test_headroom_is_absent_without_a_declared_window(tmp_path):
    log = EventLog(tmp_path / "loop.db")
    asyncio.run(
        run_agent_loop(
            caller=_three_iteration_caller(),
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )
    assert rows(log)
    assert all("headroom" not in payload for payload in rows(log))


def test_window_fn_none_or_failing_leaves_the_rows_unchanged(tmp_path):
    """An undeclared window and a broken lookup both mean "not measured"."""
    for window_fn in (lambda: None, _raising_window):
        log = EventLog(tmp_path / f"loop-{id(window_fn)}.db")
        result = asyncio.run(
            run_agent_loop(
                caller=_three_iteration_caller(),
                messages=PREFIX,
                tools=[echo_tool()],
                surface="narrate",
                log=log,
                session_id=SESSION_ID,
                capabilities=FUNCTION_CALLING,
                window_fn=window_fn,
            )
        )
        assert result.text == "Both notes are recorded."
        assert all("headroom" not in payload for payload in rows(log))


def _raising_window() -> int | None:
    raise RuntimeError("connection lookup exploded")


# -- reasoning echo: "Return thinking" in a tool loop (DeepSeek thinking mode) --
#
# The owner-reported 400: a thinking model's second request in a tool loop was
# refused because the assistant ``tool_calls`` message dropped the
# ``reasoning_content`` the provider had returned ("The `reasoning_content` in
# the thinking mode must be passed back to the API"). The rule is
# ``return_thinking`` — AUTO (the default) echoes what the provider actually
# sent, ``false`` never does, and a non-reasoning provider's conversation is
# byte-identical to before the field existed. PRESENCE and TEXT are different
# facts: ``None`` is "the field never arrived" (nothing goes back) while ``""`` is
# "it arrived empty" and must ride back verbatim — the vendor's continuation
# turns answer with exactly that, and omitting it is the same 400.

REASONING_TEXT = "The note must be recorded first; then I can answer in prose."


def _reasoning_then_answer() -> FakeCaller:
    """A thinking model: reasoning + a tool call, then reasoning + an answer."""
    return FakeCaller(
        [
            {
                "reasoning_fragments": [
                    "The note must be recorded first; ",
                    "then I can answer in prose.",
                ],
                "tool_args": '{"note": "LANTERN"}',
                "tool_name": "leave_note",
            },
            {"reasoning": "The tool result says ok.", "content": "Noted the lantern."},
        ]
    )


def test_reasoning_content_is_echoed_on_the_assistant_tool_call_message(tmp_path):
    """AUTO (the default): the provider's own reasoning rides back, verbatim."""
    caller = _reasoning_then_answer()

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    first, second = caller.calls[0], caller.calls[1]
    assert first["messages"] == PREFIX  # the prefix carries no reasoning
    assistant_turn = second["messages"][len(PREFIX)]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["tool_calls"][0]["function"]["name"] == "leave_note"
    # EXACTLY the joined deltas — never a paraphrase, never a truncation.
    assert assistant_turn["reasoning_content"] == REASONING_TEXT
    # The rule is stated once: the field appears ONLY on the tool_calls turn,
    # never on the tool result or the prompt-prefix messages.
    assert [m for m in second["messages"] if "reasoning_content" in m] == [assistant_turn]
    # The trace exposes it for inspection (debug), not the log rows.
    assert [iteration.reasoning for iteration in result.iterations] == [
        REASONING_TEXT,
        "The tool result says ok.",
    ]


def test_a_present_but_empty_reasoning_content_is_echoed_verbatim(tmp_path):
    """The vendor's shape on a continuation turn: it reasoned nothing, and SAID so.

    DeepSeek's thinking mode answers "keep going" turns with
    ``reasoning_content: ""`` and then 400s ("must be passed back to the API") if
    the field is missing from the assistant ``tool_calls`` message it produced.
    So the empty string is resent — verbatim, invented nowhere — and the vendor's
    own absence rule stays intact.
    """
    caller = FakeCaller(
        [
            {"reasoning": "", "tool_args": '{"note": "LANTERN"}', "tool_name": "leave_note"},
            {"content": "Noted the lantern."},
        ]
    )

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    assistant_turn = caller.calls[1]["messages"][len(PREFIX)]
    assert "reasoning_content" in assistant_turn
    assert assistant_turn["reasoning_content"] == ""
    # The trace keeps the two facts apart: "" is a value, None is an absence.
    assert [iteration.reasoning for iteration in result.iterations] == ["", None]


def test_an_absent_reasoning_field_never_reaches_the_trace_or_the_wire(tmp_path):
    """No field from the provider = no field sent and ``None`` on the trace."""
    caller = FakeCaller(
        [
            {"tool_args": '{"note": "LANTERN"}', "tool_name": "leave_note"},
            {"content": "Noted the lantern."},
        ]
    )

    result = asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    assistant_turn = caller.calls[1]["messages"][len(PREFIX)]
    assert "reasoning_content" not in assistant_turn
    assert [iteration.reasoning for iteration in result.iterations] == [None, None]


def test_return_thinking_false_omits_the_field_even_when_reasoning_arrived(tmp_path):
    """Explicit OFF: a provider that rejects the field gets today's message shape."""
    caller = _reasoning_then_answer()

    asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
            return_thinking=False,
        )
    )

    assistant_turn = caller.calls[1]["messages"][len(PREFIX)]
    assert assistant_turn["role"] == "assistant"
    assert "reasoning_content" not in assistant_turn
    assert all(
        "reasoning_content" not in message
        for call in caller.calls
        for message in call["messages"]
    )


def test_non_reasoning_provider_requests_are_byte_identical(tmp_path):
    """No reasoning deltas = no new field anywhere, in any request."""
    caller = FakeCaller(
        [
            {"tool_args": '{"note": "PLAIN"}'},
            {"content": "Noted the plain word."},
        ]
    )

    asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    for call in caller.calls:
        for message in call["messages"]:
            assert set(message) <= {"role", "content", "tool_calls", "tool_call_id"}
    # The assistant tool-call turn is exactly the pre-existing shape.
    assert caller.calls[1]["messages"][len(PREFIX)] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "leave_note", "arguments": '{"note": "PLAIN"}'},
            }
        ],
    }


# -- presence is ALL-OR-NOTHING per REQUEST (measured vendor fact) -----------
#
# With ``tools`` on the request, DeepSeek accepts no field anywhere (200) or the
# field on EVERY assistant message (200), and 400s on a MIXED request ("The
# `reasoning_content` in the thinking mode must be passed back to the API").
# History assembly stamps only the turns that STORED reasoning, so one legacy
# turn is enough to make the next narrator turn a 400. The loop therefore
# normalises presence once per request, right before the model call.

#: A caller-supplied history that mixes presence: the first assistant turn stored
#: reasoning (history assembly stamps it present-but-empty), the second stored
#: none (no key at all — a session begun on a non-thinking connection).
MIXED_HISTORY = [
    {"role": "system", "content": "You are the narrator."},
    {"role": "user", "content": "Light the lantern."},
    {"role": "assistant", "content": "The lantern flares.", "reasoning_content": ""},
    {"role": "user", "content": "Who is there?"},
    {"role": "assistant", "content": "Only silence."},
]


def test_a_mixed_history_request_carries_the_field_on_every_assistant_turn(tmp_path):
    """History without the field + a round that stamps it = BOTH carry it.

    This is the 400 shape the vendor measured: the loop's own stamped turn had
    the field while a caller-supplied turn did not. The request that reaches the
    transport must be uniform, and the fill is ``""`` (the vendor's own "nothing
    reasoned" value) — never invented text, never a rewritten stored value.
    """
    history = [dict(message) for message in MIXED_HISTORY]
    caller = FakeCaller(
        [
            {
                "reasoning": "Weigh the answer, then note it.",
                "tool_args": '{"note": "LANTERN"}',
                "tool_name": "leave_note",
            },
            {"content": "Noted the lantern."},
        ]
    )

    asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=history,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    second = caller.calls[1]["messages"]
    assistant_turns = [m for m in second if m.get("role") == "assistant"]
    assert len(assistant_turns) == 3, second
    assert all("reasoning_content" in m for m in assistant_turns), assistant_turns
    # The stored-reasoning turn kept its (empty) value; the legacy turn was
    # FILLED; the round's own turn carries the provider's text verbatim.
    assert assistant_turns[0]["reasoning_content"] == ""
    assert assistant_turns[1] == {
        "role": "assistant",
        "content": "Only silence.",
        "reasoning_content": "",
    }
    assert assistant_turns[2]["reasoning_content"] == "Weigh the answer, then note it."
    # Only assistant messages are touched: system/user stay byte-identical.
    assert second[0] == MIXED_HISTORY[0]
    assert second[1] == MIXED_HISTORY[1]
    assert second[3] == MIXED_HISTORY[3]
    # The caller's own history is never rewritten (the loop works on its copy).
    assert "reasoning_content" not in history[4]
    assert history[2]["reasoning_content"] == ""


def test_a_reasoning_less_conversation_is_byte_identical(tmp_path):
    """No field anywhere = the EXACT message lists, before and after the loop.

    Non-thinking providers and older sessions must not notice this repair: the
    first request is the caller's history verbatim and the second is that list
    plus the one assistant ``tool_calls`` turn the loop appends.
    """
    history = [
        {"role": "system", "content": "You are the narrator."},
        {"role": "user", "content": "Light the lantern."},
        {"role": "assistant", "content": "The lantern flares."},
        {"role": "user", "content": "Who is there?"},
    ]
    caller = FakeCaller(
        [
            {"tool_args": '{"note": "PLAIN"}', "tool_name": "leave_note"},
            {"content": "Noted the plain word."},
        ]
    )

    asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=history,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    assert caller.calls[0]["messages"] == history
    assert caller.calls[1]["messages"] == [
        *history,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "leave_note",
                        "arguments": '{"note": "PLAIN"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps({"ok": True, "seen": {"note": "PLAIN"}}),
        },
    ]
    for call in caller.calls:
        for message in call["messages"]:
            assert "reasoning_content" not in message


def test_an_all_present_conversation_keeps_its_reasoning_text_untouched(tmp_path):
    """Uniform presence is already valid: values (and prose) survive verbatim."""
    history = [
        {"role": "system", "content": "You are the narrator."},
        {"role": "user", "content": "Light the lantern."},
        {
            "role": "assistant",
            "content": "The lantern flares.",
            "reasoning_content": "stored block, never rewritten",
        },
    ]
    caller = FakeCaller(
        [
            {
                "reasoning": "Weigh the answer.",
                "tool_args": '{"note": "LANTERN"}',
                "tool_name": "leave_note",
            },
            {"content": "Noted the lantern."},
        ]
    )

    asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=history,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
        )
    )

    second = caller.calls[1]["messages"]
    assert second[2] == {
        "role": "assistant",
        "content": "The lantern flares.",
        "reasoning_content": "stored block, never rewritten",
    }
    assert second[3]["role"] == "assistant"
    assert second[3]["reasoning_content"] == "Weigh the answer."
    assert second[3]["content"] == ""


def test_return_thinking_false_leaves_a_mixed_caller_history_alone(tmp_path):
    """Explicit OFF: the loop stamps nothing, so it normalises nothing.

    A history that mixes presence while the connection says "never send this
    field" is the CALLER's contradiction — filling blanks here would put the
    field on turns that lacked it on a connection that forbids it.
    """
    history = [dict(message) for message in MIXED_HISTORY]
    caller = FakeCaller(
        [
            {
                "reasoning": "Weigh the answer.",
                "tool_args": '{"note": "LANTERN"}',
                "tool_name": "leave_note",
            },
            {"content": "Noted the lantern."},
        ]
    )

    asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=history,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
            return_thinking=False,
        )
    )

    second = caller.calls[1]["messages"]
    assistant_turns = [m for m in second if m.get("role") == "assistant"]
    # The caller's stored field is untouched, the legacy turn stays content-only
    # (nothing invented), and the loop's own turn sends no field.
    assert assistant_turns[0]["reasoning_content"] == ""
    assert "reasoning_content" not in assistant_turns[1]
    assert "reasoning_content" not in assistant_turns[2]
    assert assistant_turns[1] == {"role": "assistant", "content": "Only silence."}


@pytest.mark.parametrize(("return_thinking", "echoed"), [(None, True), (False, False)])
def test_injected_flags_pair_follows_the_reasoning_echo_rule(
    tmp_path, return_thinking, echoed
):
    """The synthetic pair closes a text completion: AUTO echoes its reasoning too."""

    nudged: list[str] = []

    def hook(text: str) -> LoopInjection | None:
        """Nudge ONCE (the draft's flags), then let the model close out."""
        if nudged:
            return None
        nudged.append(text)
        return LoopInjection(content='{"flags": []}')

    reasoning = "I drafted prose; the engine says to validate."
    caller = FakeCaller(
        [
            {"reasoning": reasoning, "content": "A draft."},
            {"content": "Repaired draft."},
        ]
    )

    asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
            on_completion=hook,
            return_thinking=return_thinking,
        )
    )

    # The pair's three tail messages: the draft (the FIELD only — its text is
    # empty so the vendor's per-assistant validation passes without paying for
    # the same block twice), the synthetic tool_calls turn (the field AND text,
    # under AUTO), its tool result (never a field).
    draft_turn, calls_turn, result_turn = caller.calls[1]["messages"][len(PREFIX) :]
    assert draft_turn["role"] == "assistant"
    assert draft_turn["content"] == "A draft."
    assert calls_turn["tool_calls"][0]["id"] == ENGINE_FLAGS_CALL_ID
    assert result_turn["tool_call_id"] == ENGINE_FLAGS_CALL_ID
    if echoed:
        assert calls_turn["reasoning_content"] == reasoning
        # DeepSeek validates the FIELD on every assistant turn of a tools
        # request — the draft included (live 400, issue #42).
        assert draft_turn["reasoning_content"] == ""
    else:
        assert "reasoning_content" not in calls_turn
        assert "reasoning_content" not in draft_turn
    assert "reasoning_content" not in result_turn


def test_the_injected_flags_pair_carries_a_present_empty_reasoning(tmp_path):
    """The engine-authored pair on a provider whose draft turn reasoned nothing.

    The pair IS the engine continuing that draft's assistant turn, so the
    truthful value is the draft's own answer: an empty string built by the
    provider, resent verbatim — never a field the engine made up, and never the
    omission the vendor rejects.
    """

    nudged: list[str] = []

    def hook(text: str) -> LoopInjection | None:
        if nudged:
            return None
        nudged.append(text)
        return LoopInjection(content='{"flags": []}')

    caller = FakeCaller(
        [
            {"reasoning": "", "content": "A draft."},
            {"content": "Repaired draft."},
        ]
    )

    asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[echo_tool()],
            surface="narrate",
            log=EventLog(tmp_path / "loop.db"),
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
            on_completion=hook,
        )
    )

    draft_turn, calls_turn, result_turn = caller.calls[1]["messages"][len(PREFIX) :]
    assert draft_turn["role"] == "assistant"
    assert draft_turn["content"] == "A draft."
    assert "reasoning_content" in calls_turn
    assert calls_turn["reasoning_content"] == ""
    # The draft carries the FIELD too (presence is what the vendor validates);
    # the empty text is the paid block's only home.
    assert draft_turn["reasoning_content"] == ""
    assert "reasoning_content" not in result_turn


@pytest.mark.parametrize(
    ("return_thinking", "reasoning", "expected"),
    [
        # AUTO (None) and True both echo — but only what the provider sent.
        (None, REASONING_TEXT, True),
        (True, REASONING_TEXT, True),
        # PRESENT but empty: the provider reasoned nothing this turn and said so.
        # That empty string is its own value, and the vendor 400s on its absence.
        (None, "", True),
        (True, "", True),
        # ABSENT: the provider never sent the field, so nothing goes back (the
        # runtime never invents reasoning).
        (None, None, False),
        (True, None, False),
        # Explicit OFF: the provider that rejects the field.
        (False, REASONING_TEXT, False),
        (False, "", False),
        (False, None, False),
    ],
)
def test_the_reasoning_echo_rule(return_thinking, reasoning, expected):
    assert should_echo_reasoning(return_thinking, reasoning) is expected
