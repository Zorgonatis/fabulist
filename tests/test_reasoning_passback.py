"""Issue #42: the ``reasoning_content`` history passback.

DeepSeek's thinking mode requires assistant turns that PRODUCED
``reasoning_content`` to carry it back on later requests
(https://api-docs.deepseek.com/guides/thinking_mode — "Tool Calls": "for requests
carrying the ``tools`` parameter, the ``reasoning_content`` must be fully passed
back ... If your code does not correctly pass back ``reasoning_content``, the API
will return a 400 error"; "Multi-turn Conversation" shows the same append-back
shape without tools). The engine used to send prior assistant turns content-only,
so narration on a thinking model 400ed from turn 2.

Pinned here, end to end:

* the narrator commit persists the draft completion's reasoning on the narrative
  row (PRESENCE is the fact: ``""`` is a real provider value, no field is ``None``);
* ``history_from_log`` stamps the field **PRESENT but EMPTY** (``""``) on exactly
  the assistant turns that stored it, and the compiler's history projection
  preserves that presence: a PRIOR execution's reasoning is stored for diagnosis
  and never re-sent as text (owner ruling on #29 — the current round keeps its
  reasoning verbatim; the round boundary is where the text stops);
* the REQUEST BUILDER is the gate: a thinking-active connection sends the field
  back, while a non-thinking connection (``thinking: false``), a
  ``return_thinking: false`` owner switch, and every reasoning-less turn stay
  content-only — byte-identical to the pre-#42 body;
* mixed-model history (a thinking turn followed by a non-thinking turn) carries
  the field only on the turn that produced it.

The transport assertions use a real ``openai`` SDK client serializing through a
fake ``httpx2`` transport, so what is asserted is the actual JSON body.
"""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest

from fabulist.connections import Registry, build_request, get_client
from fabulist.agents.loop import ENGINE_FLAGS_CALL_ID, LoopInjection, LoopTool, run_agent_loop
from fabulist.graph.nodes import make_commit_node, make_narrator_node
from fabulist.graph.types import initial_turn_state
from fabulist.log import EventLog
from fabulist.prompts.compiler import stripped_history
from fabulist.wiring import history_from_log

SESSION_ID = "reasoning-session"
DRAFT_REASONING = "The lantern is lit; answer in prose."


def _registry(**overrides) -> Registry:
    """One narrator connection; overrides pin thinking/return_thinking/etc."""
    connection = {
        "base_url": "http://transport.test/v1",
        "model": "deepseek-flash",
    }
    connection.update(overrides)
    return Registry.from_dict(
        {"connections": {"ds": connection}, "agents": {"narrator": "ds"}}
    )


def _capturing_send(registry: Registry, messages: list[dict]) -> dict:
    """Run one REAL SDK serialization through a fake transport; return the body."""
    captured: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx2.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    connection = registry.connection_for("narrator")
    http_client = httpx2.Client(
        transport=httpx2.MockTransport(handler), base_url=connection.base_url
    )
    try:
        client = get_client("narrator", registry=registry, http_client=http_client)
        request = build_request("narrator", messages, registry=registry)
        client.chat.completions.create(**request)
    finally:
        http_client.close()
    return captured["body"]


def _seed_mixed_history(log: EventLog, sid: str = SESSION_ID) -> list[dict]:
    """A thinking turn, then a turn that stored no reasoning (mixed-model case)."""
    log.append_event(sid, "user", "user", {"text": "Light the lantern."})
    log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": "The lantern flares.", "reasoning": "thinking one"},
    )
    log.append_event(sid, "user", "user", {"text": "Who is there?"})
    log.append_event(sid, "narrative", "narrator", {"prose": "Only silence."})
    return history_from_log(log, sid)


# -- persistence + history assembly ------------------------------------------


def test_history_carries_the_field_empty_and_never_a_prior_turns_text(tmp_path):
    """The round boundary trims TEXT, not the field (owner ruling on #29)."""
    log = EventLog(tmp_path / "events.db")
    try:
        history = _seed_mixed_history(log)
    finally:
        log.close()

    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    # The thinking turn: the field is PRESENT (the vendor 400s on its absence in a
    # tools request) but its text is gone — a prior execution is not re-billed.
    assert history[1]["reasoning_content"] == ""
    assert history[1]["content"] == "The lantern flares."
    # The turn that stored none: no key at all (content-only, byte-identical).
    assert "reasoning_content" not in history[3]


def test_compiler_history_projection_preserves_reasoning_content():
    history = [
        {"role": "user", "content": "Light the lantern."},
        {"role": "assistant", "content": "The lantern flares.", "reasoning_content": "t1"},
        {"role": "assistant", "content": "Only silence."},
    ]

    projected = stripped_history(history)

    assert projected[1] == {
        "role": "assistant",
        "content": "The lantern flares.",
        "reasoning_content": "t1",
    }
    assert "reasoning_content" not in projected[2]


class _ScriptedCaller:
    """One scripted completion; ``reasoning=None`` omits the field entirely."""

    def __init__(self, *, content: str, reasoning: str | None) -> None:
        self.content = content
        self.reasoning = reasoning

    async def __call__(self, messages, stream=True, request_profile=None):
        if self.reasoning is not None:
            yield {"type": "reasoning", "delta": self.reasoning}
        yield {"type": "content", "delta": self.content, "meta": {"channel": "prose"}}


def _run_narrator_then_commit(log: EventLog, caller) -> dict:
    """Draft through the narrator node, then commit — the real persistence path."""
    state = initial_turn_state(SESSION_ID, "Light the lantern.", "in_character")
    update = asyncio.run(make_narrator_node(model=caller)(state))
    committed = asyncio.run(make_commit_node(log=log)({**state, **update}))
    narrative = committed["narrative_event"]
    return narrative["payload"]


def test_narrator_commit_persists_draft_reasoning(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        payload = _run_narrator_then_commit(
            log,
            _ScriptedCaller(content="The lantern flares.", reasoning=DRAFT_REASONING),
        )
    finally:
        log.close()

    assert payload["reasoning"] == DRAFT_REASONING
    assert payload["prose"] == "The lantern flares."


def test_narrator_commit_omits_reasoning_when_provider_sent_none(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        payload = _run_narrator_then_commit(
            log, _ScriptedCaller(content="The lantern flares.", reasoning=None)
        )
    finally:
        log.close()

    assert "reasoning" not in payload


# -- the request body (real SDK serialization) -------------------------------


def test_thinking_connection_sends_the_field_empty_not_the_stored_text(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        history = _seed_mixed_history(log)
    finally:
        log.close()

    body = _capturing_send(_registry(), history)
    messages = body["messages"]

    # Issue #42's presence rule: the prior thinking turn still carries the FIELD.
    assert messages[1]["role"] == "assistant"
    assert messages[1]["reasoning_content"] == ""
    # Issue #29's round boundary: its TEXT is not re-sent anywhere in the body.
    assert "thinking one" not in json.dumps(body)
    # Mixed history: the non-thinking turn stays content-only.
    assert messages[3]["role"] == "assistant"
    assert "reasoning_content" not in messages[3]
    assert messages[3]["content"] == "Only silence."


def test_non_thinking_connection_sends_no_reasoning_content_key(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        history = _seed_mixed_history(log)
    finally:
        log.close()

    body = _capturing_send(_registry(thinking=False), history)
    assert all("reasoning_content" not in m for m in body["messages"])
    # The rest of the message list is untouched.
    assert body["messages"][1]["content"] == "The lantern flares."


def test_return_thinking_false_suppresses_the_history_passback(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        history = _seed_mixed_history(log)
    finally:
        log.close()

    body = _capturing_send(_registry(return_thinking=False), history)
    assert all("reasoning_content" not in m for m in body["messages"])


def test_empty_stored_reasoning_is_sent_as_a_present_empty_field(tmp_path):
    """PRESENCE is the fact: the provider returned ``""`` for a reasoning-less turn."""
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event(SESSION_ID, "user", "user", {"text": "Tell me."})
        log.append_event(
            SESSION_ID,
            "narrative",
            "narrator",
            {"prose": "A short answer.", "reasoning": ""},
        )
        history = history_from_log(log, SESSION_ID)
    finally:
        log.close()

    body = _capturing_send(_registry(), history)
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    assert "reasoning_content" in assistant
    assert assistant["reasoning_content"] == ""


def test_reasoning_less_history_is_byte_identical_and_unmutated():
    messages = [
        {"role": "user", "content": "Light the lantern."},
        {"role": "assistant", "content": "The lantern flares."},
    ]

    request = build_request("narrator", messages, registry=_registry())

    # No assistant turn carried the field: the EXACT list object travels, so the
    # pre-#42 body is unchanged for every non-thinking provider.
    assert request["messages"] is messages
    assert request["messages"] == messages


def test_disallowed_reasoning_is_stripped_without_mutating_the_caller():
    messages = [
        {"role": "assistant", "content": "The lantern flares.", "reasoning_content": "t1"},
        {"role": "user", "content": "Who is there?"},
    ]

    request = build_request(
        "narrator", messages, registry=_registry(thinking=False)
    )

    assert all("reasoning_content" not in m for m in request["messages"])
    assert request["messages"] is not messages
    # The caller's own list (checkpoint/state-owned) is never rewritten.
    assert messages[0]["reasoning_content"] == "t1"


@pytest.mark.parametrize("stored", ["thinking one", ""])
def test_history_reasoning_reaches_the_body_when_thinking_is_active(stored):
    messages = [
        {"role": "assistant", "content": "The lantern flares.", "reasoning_content": stored},
        {"role": "user", "content": "Who is there?"},
    ]

    body = _capturing_send(_registry(), messages)

    assert body["messages"][0]["reasoning_content"] == stored


# -- within-turn: the repair loop's tools request (live 400 shape) ------------


class _RepairLoopCaller:
    """Draft then close: records every message list the loop sends."""

    def __init__(self, *, reasoning: str | None) -> None:
        self.reasoning = reasoning
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            if self.reasoning is not None:
                yield {"type": "reasoning", "delta": self.reasoning}
            yield {"type": "content", "delta": "A draft."}
        else:
            yield {"type": "content", "delta": "Repaired draft."}


def _echo_tool() -> LoopTool:
    return LoopTool(
        name="noop",
        description="does nothing",
        parameters={"type": "object", "properties": {}},
        fn=lambda args: {"ok": True},
    )


def test_tools_request_carries_reasoning_on_every_assistant_turn(tmp_path):
    """The live 400 shape: ``tools`` + one assistant turn missing the field.

    Measured on DeepSeek-V4.1-Flash (issue #42 live gate): the per-turn repair
    loop carries ``tools``, and the vendor validates the field on EVERY assistant
    message of that request — the flagged DRAFT turn included, not just the
    synthetic ``tool_calls`` turn.
    """
    nudged: list[str] = []

    def hook(text: str) -> LoopInjection | None:
        if nudged:
            return None
        nudged.append(text)
        return LoopInjection(content='{"flags": []}')

    caller = _RepairLoopCaller(reasoning=DRAFT_REASONING)
    log = EventLog(tmp_path / "loop.db")
    try:
        asyncio.run(
            run_agent_loop(
                caller=caller,
                messages=[{"role": "user", "content": "Light the lantern."}],
                tools=[_echo_tool()],
                surface="narrate",
                log=log,
                session_id=SESSION_ID,
                capabilities={"function_calling": True},
                on_completion=hook,
            )
        )
    finally:
        log.close()

    repair_request = caller.calls[1]
    assistant_turns = [m for m in repair_request if m.get("role") == "assistant"]
    assert assistant_turns, repair_request
    assert all("reasoning_content" in m for m in assistant_turns), assistant_turns
    # The draft is the real producer: field present (empty value, not invented
    # text). The synthetic tool_calls turn carries that iteration's block verbatim.
    draft = next(m for m in assistant_turns if not m.get("tool_calls"))
    nudged_turn = next(m for m in assistant_turns if m.get("tool_calls"))
    assert draft["reasoning_content"] == ""
    assert nudged_turn["reasoning_content"] == DRAFT_REASONING
    assert nudged_turn["tool_calls"][0]["id"] == ENGINE_FLAGS_CALL_ID
