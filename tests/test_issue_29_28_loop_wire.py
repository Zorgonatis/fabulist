"""Issues #29 and #28 on the agent loop's streaming wire (``agents/loop.py``).

#29 — the reasoning echo ACCUMULATED. Every iteration's ``reasoning_content`` was
carried onto its assistant ``tool_calls`` message and left there, so iteration N's
request re-sent the reasoning of iterations 1..N-1 as prompt tokens. A measured
four-retry turn grew 7,536 -> 30,107 prompt tokens across six calls. The vendor's
constraint is ONE turn deep (the assistant turn the request continues from must
carry its reasoning with its ``tool_calls``; an absent field is a 400), so the
echo is now RE-STAMPED: the newest assistant turn carries it, older ones carry
none. Pinned here: exactly one reasoning field per request, holding the
immediately-previous iteration's value, and a prompt that grows by one round per
round instead of by every round that ever ran.

#28 — the narrator stream reader only found ``finish_reason`` inside an ``error``
frame, so ``completion.finish_reason`` was ``None`` on every call and ADR-021's
allowlist could not tell a clean ``stop`` from a ``length`` truncation or a
provider content-filter. The reader now also reads the terminal ``finish``
side-band frame (``graph/types.py``'s contract, top level or ``meta``). Pinned
here: the reason is recorded on the completion row and on ``LoopResult``, and the
prose a turn streams is BYTE-identical with and without the side-band.

Everything here is a fake transport: no test touches a model.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.agents.loop import LoopInjection, LoopTool, run_agent_loop
from fabulist.log import EventLog

SESSION_ID = "loop-wire-test"
PREFIX = [
    {"role": "system", "content": "You are the narrator."},
    {"role": "user", "content": "Describe the harbour at dawn."},
]
FUNCTION_CALLING = {"function_calling": True}


# -- fakes -------------------------------------------------------------------


class WireCaller:
    """A streaming caller over scripted FRAME lists; records every request.

    Each reply is the exact list of stream events the transport would yield, so a
    test states the wire it means — including the terminal ``finish`` side-band —
    instead of a reply shape the fake interprets for it.
    """

    def __init__(self, replies: list[list[dict]]) -> None:
        self.replies = list(replies)
        self.requests: list[list[dict]] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.requests.append([dict(message) for message in messages])
        for event in self.replies.pop(0):
            yield dict(event)


class Tap:
    """Pass-through caller that records the events the loop actually consumed."""

    def __init__(self, inner: WireCaller) -> None:
        self.inner = inner
        self.events: list[dict] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        async for event in self.inner(messages, stream, request_profile=request_profile):
            self.events.append(event)
            yield event


def leave_note_tool() -> LoopTool:
    """The single tool the reasoning-loop pins drive (unambiguous by construction)."""

    async def fn(args: dict) -> dict:
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


def reasoning_round(reasoning: str, note: str) -> list[dict]:
    """One thinking model's tool-call round: reasoning deltas, then the call."""
    return [
        {"type": "reasoning", "delta": reasoning},
        {"type": "tool_call", "delta": json.dumps({"note": note}), "meta": {"name": "leave_note"}},
    ]


def prose_and_finish(text: str, reason: str | None, *, split: bool = True) -> list[dict]:
    """A prose answer closed by the terminal ``finish`` side-band."""
    deltas = [text[: len(text) // 2], text[len(text) // 2:]] if split else [text]
    frames = [
        {"type": "content", "delta": delta, "meta": {"channel": "prose"}}
        for delta in deltas
    ]
    frames.append({"type": "finish", "finish_reason": reason})
    return frames


def rows(log: EventLog, session_id: str = SESSION_ID) -> list[dict]:
    """The ``agent_loop`` payloads the runner appended, in append order."""
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.type == "system" and event.payload.get("event") == "agent_loop"
    ]


def completion_rows(log: EventLog) -> list[dict]:
    return [row for row in rows(log) if row["kind"] == "completion"]


def reasoning_fields(messages: list[dict]) -> list[tuple[int, dict]]:
    """Every ``(index, message)`` in one request that carries the echo field."""
    return [
        (index, message)
        for index, message in enumerate(messages)
        if "reasoning_content" in message
    ]


def prompt_chars(messages: list[dict]) -> int:
    """What a provider bills as prompt text: content, reasoning and tool calls.

    ``estimate_prompt_tokens`` (compaction) counts ``content`` only; the echo rides
    ``reasoning_content``, so a size proxy that ignored it would be blind to the
    very growth issue #29 is about.
    """
    total = 0
    for message in messages:
        total += len(str(message.get("content") or ""))
        total += len(str(message.get("reasoning_content") or ""))
        for call in message.get("tool_calls") or ():
            total += len(json.dumps(call, sort_keys=True))
    return total


def run_loop(caller, log: EventLog, **kwargs):
    return asyncio.run(
        run_agent_loop(
            caller=caller,
            messages=PREFIX,
            tools=[leave_note_tool()],
            surface="narrate",
            log=log,
            session_id=SESSION_ID,
            capabilities=FUNCTION_CALLING,
            **kwargs,
        )
    )


# -- #29: only the immediately-previous iteration's reasoning travels ---------

#: Four rounds of CONSTANT-SIZE reasoning: under the old accumulate-everything
#: echo, request N carried N-1 blocks and the prompt grew by an ever-larger step.
REASONING_UNIT = "Weighed the lantern against the tide; " * 40


def _four_round_caller() -> WireCaller:
    return WireCaller(
        [
            reasoning_round(REASONING_UNIT, "A"),
            reasoning_round(REASONING_UNIT, "B"),
            reasoning_round(REASONING_UNIT, "C"),
            [
                {"type": "reasoning", "delta": REASONING_UNIT},
                {"type": "content", "delta": "The lantern is lit."},
                {"type": "finish", "finish_reason": "stop"},
            ],
        ]
    )


def test_only_the_newest_block_of_reasoning_text_rides_each_request(tmp_path):
    """Presence per turn, ONE block of paid text per run (the live-probed shape).

    A probe against DeepSeek's thinking mode showed the field is validated on
    EVERY assistant ``tool_calls`` turn (dropping it from an older turn is a
    400), while the TEXT of an older turn is never read back. So the older turns
    keep the field with the vendor's own empty value and the newest one carries
    the provider's words verbatim.
    """
    caller = _four_round_caller()
    result = run_loop(caller, EventLog(tmp_path / "loop.db"))

    assert len(caller.requests) == 4
    assert caller.requests[0] == PREFIX  # the first request is untouched
    for request_number, request in enumerate(caller.requests[1:], start=2):
        carried = reasoning_fields(request)
        newest_index = len(PREFIX) + 2 * (request_number - 2)
        # One field per assistant tool_calls turn — the vendor's presence rule.
        assert [index for index, _ in carried] == list(
            range(len(PREFIX), newest_index + 1, 2)
        ), f"request {request_number} carries an unexpected field set"
        # The newest turn is the ONE place the provider's text survives.
        for index, message in carried:
            assert message["role"] == "assistant"
            assert message["tool_calls"], "the echo belongs on a tool_calls turn"
            expected = REASONING_UNIT if index == newest_index else ""
            assert message["reasoning_content"] == expected

    # The trace still records EVERY iteration's reasoning: the retirement is a
    # wire rule, not a loss of what the model said.
    assert [iteration.reasoning for iteration in result.iterations] == [REASONING_UNIT] * 4


def test_the_prompt_grows_by_one_round_per_round_not_by_every_round(tmp_path):
    """Issue #29's number: monotonic prompt growth that compounds is the defect."""
    caller = _four_round_caller()
    run_loop(caller, EventLog(tmp_path / "loop.db"))

    sizes = [prompt_chars(request) for request in caller.requests]
    steps = [later - earlier for earlier, later in zip(sizes, sizes[1:])]
    # Rounds 2..4 each append one assistant turn + one tool result and NO new
    # reasoning block (the previous block replaced the one before it): a CONSTANT
    # step. Under the accumulate-everything echo every step would instead grow by
    # another reasoning block — the 7,536 -> 30,107 curve the issue measured.
    assert len(set(steps[1:])) == 1, f"prompt growth is not linear: sizes={sizes} steps={steps}"
    assert steps[0] > steps[1] > 0
    # Restated on the billing surface itself: the reasoning a request carries is
    # ONE block, never the sum of the blocks before it.
    carried = [
        sum(len(str(m.get("reasoning_content") or "")) for m in request)
        for request in caller.requests
    ]
    # One block of paid text at every round — the older turns carry "".
    assert carried == [0, len(REASONING_UNIT), len(REASONING_UNIT), len(REASONING_UNIT)]
    # Counterfactual, in the issue's own terms: three rounds of echo would have
    # cost three reasoning blocks of prompt text. The wire now pays for ONE.
    assert sizes[-1] - sizes[0] < 3 * len(REASONING_UNIT)


def test_an_iteration_that_reasons_nothing_retires_the_stale_text(tmp_path):
    """A later round with no field must not resurrect (or keep) an earlier block."""
    caller = WireCaller(
        [
            [{"type": "reasoning", "delta": REASONING_UNIT},
             {"type": "tool_call", "delta": '{"note": "A"}', "meta": {"name": "leave_note"}}],
            [{"type": "tool_call", "delta": '{"note": "B"}', "meta": {"name": "leave_note"}}],
            [{"type": "content", "delta": "Done."},
             {"type": "finish", "finish_reason": "stop"}],
        ]
    )
    result = run_loop(caller, EventLog(tmp_path / "loop.db"))

    # Request 2 continues from round 1, which DID reason: the text rides.
    assert [m["reasoning_content"] for _, m in reasoning_fields(caller.requests[1])] == [
        REASONING_UNIT
    ]
    # Request 3 continues from round 2, which reasoned nothing (no field): the
    # round-1 block is retired to the vendor's empty value, and the runtime never
    # invents reasoning for the turn that sent none.
    assert [m["reasoning_content"] for _, m in reasoning_fields(caller.requests[2])] == [""]
    assert [iteration.reasoning for iteration in result.iterations] == [
        REASONING_UNIT,
        None,
        None,
    ]


def test_the_injected_flags_pair_is_the_one_paid_echo_and_retires_the_last(tmp_path):
    """The engine-authored pair obeys the same rule: newest TEXT wins, once.

    ISSUE #42 (live DeepSeek-V4.1-Flash): once the request carries ``tools``,
    the vendor validates ``reasoning_content`` on EVERY assistant message — the
    flagged draft included — so the draft carries the FIELD with the vendor's
    empty value while only the synthetic ``tool_calls`` turn carries the paid
    block. Presence per assistant turn, one block of text per round (#29).
    """
    first_reasoning = "The draft needs the flags applied."
    second_reasoning = "The flags came back clean this time."

    def hook(text: str) -> LoopInjection | None:
        return LoopInjection(content='{"flags": []}') if "Draft" in text else None

    caller = WireCaller(
        [
            [
                {"type": "reasoning", "delta": first_reasoning},
                {"type": "content", "delta": "Draft one."},
            ],
            [
                {"type": "reasoning", "delta": second_reasoning},
                {"type": "content", "delta": "Draft two."},
            ],
            [
                {"type": "content", "delta": "Final prose."},
                {"type": "finish", "finish_reason": "stop"},
            ],
        ]
    )
    run_loop(caller, EventLog(tmp_path / "loop.db"), on_completion=hook)

    assert len(caller.requests) == 3
    # Request 2: the draft carries the FIELD (empty value — presence is what the
    # vendor validates) and the pair's tool_calls message carries the paid TEXT.
    carried = reasoning_fields(caller.requests[1])
    assert [index for index, _ in carried] == [
        len(caller.requests[1]) - 3,  # the draft message
        len(caller.requests[1]) - 2,  # the pair's tool_calls message
    ]
    assert carried[0][1]["reasoning_content"] == ""
    assert carried[1][1]["reasoning_content"] == first_reasoning

    # Request 3: round 2's pair carries the paid text; round 1's is retired to the
    # vendor's empty value — the field stays, the block does not.
    carried = reasoning_fields(caller.requests[2])
    # The pair is [draft?, calls, result]: pair 1's draft+calls, then pair 2's.
    assert [index for index, _ in carried] == [
        len(caller.requests[2]) - 6,
        len(caller.requests[2]) - 5,
        len(caller.requests[2]) - 3,
        len(caller.requests[2]) - 2,
    ]
    assert [message["reasoning_content"] for _, message in carried] == [
        "",
        "",
        "",
        second_reasoning,
    ]
    # No draft ever carries paid reasoning TEXT — only the field's presence.
    for request in caller.requests:
        for message in request:
            if message["role"] == "assistant" and not message.get("tool_calls"):
                assert message.get("reasoning_content", "") == ""


def test_return_thinking_false_still_sends_no_field_across_iterations(tmp_path):
    """The connection's opt-out beats the echo rule at every round of the loop."""
    caller = _four_round_caller()
    run_loop(caller, EventLog(tmp_path / "loop.db"), return_thinking=False)

    for request in caller.requests:
        assert reasoning_fields(request) == []


def test_a_non_reasoning_provider_sees_no_new_field_on_any_request(tmp_path):
    """No reasoning arrived = every request is byte-identical to the pre-echo wire."""
    silent = WireCaller(
        [
            [{"type": "tool_call", "delta": '{"note": "A"}', "meta": {"name": "leave_note"}}],
            [{"type": "tool_call", "delta": '{"note": "B"}', "meta": {"name": "leave_note"}}],
            [{"type": "content", "delta": "Done."},
             {"type": "finish", "finish_reason": "stop"}],
        ]
    )
    run_loop(silent, EventLog(tmp_path / "loop.db"))

    assert len(silent.requests) == 3
    for request in silent.requests:
        assert reasoning_fields(request) == []


# -- #28: the terminal finish side-band reaches the narrator's reader ---------


def test_the_terminal_finish_sideband_is_recorded_on_the_completion_row(tmp_path):
    log = EventLog(tmp_path / "loop.db")
    caller = WireCaller([prose_and_finish("Rain on the shutters.", "stop")])

    result = run_loop(caller, log)

    assert completion_rows(log)[0]["finish_reason"] == "stop"
    assert result.finish_reason == "stop"


def test_a_length_truncation_is_recorded_as_length(tmp_path):
    """The reason ADR-021 exists for: truncated output must be nameable."""
    log = EventLog(tmp_path / "loop.db")
    caller = WireCaller(
        [
            [
                {"type": "content", "delta": "Rain on the"},
                {"type": "finish", "finish_reason": "length"},
            ]
        ]
    )

    result = run_loop(caller, log)

    assert completion_rows(log)[0]["finish_reason"] == "length"
    assert result.finish_reason == "length"


def test_a_finish_frame_with_no_reason_adds_no_key(tmp_path):
    """An absent reason is not evidence of truncation (ADR-021) — so no key."""
    log = EventLog(tmp_path / "loop.db")
    caller = WireCaller([prose_and_finish("Rain on the shutters.", None)])

    result = run_loop(caller, log)

    assert "finish_reason" not in completion_rows(log)[0]
    assert result.finish_reason is None


def test_the_reason_is_also_read_from_the_frames_meta(tmp_path):
    """The contract accepts the reason at the top level (or under ``meta``)."""
    log = EventLog(tmp_path / "loop.db")
    caller = WireCaller(
        [
            [
                {"type": "content", "delta": "Rain."},
                {"type": "finish", "meta": {"finish_reason": "content_filter"}},
            ]
        ]
    )

    result = run_loop(caller, log)

    assert completion_rows(log)[0]["finish_reason"] == "content_filter"
    assert result.finish_reason == "content_filter"


def test_the_sideband_is_the_last_word_over_the_error_frame(tmp_path):
    """A silent ``finish`` frame never erases the ``length`` an error frame gave."""
    log = EventLog(tmp_path / "loop.db")
    caller = WireCaller(
        [
            [
                {"type": "content", "delta": "Rain on the"},
                {"type": "error", "delta": "generation truncated",
                 "meta": {"finish_reason": "length"}},
                {"type": "finish", "finish_reason": None},
            ]
        ]
    )

    result = run_loop(caller, log)

    assert completion_rows(log)[0]["finish_reason"] == "length"
    assert result.finish_reason == "length"


def test_the_finish_sideband_leaves_the_prose_byte_identical(tmp_path):
    """The side-band is additive: the prose a turn streams cannot change."""
    prose = "Rain hammers the shutters.\n\n<Card title=\"Harbour\" />\n\n«Lantern?» she asked."
    with_frame = Tap(WireCaller([prose_and_finish(prose, "stop")]))
    without_frame = Tap(
        WireCaller(
            [
                [
                    {"type": "content", "delta": prose[:40], "meta": {"channel": "prose"}},
                    {"type": "content", "delta": prose[40:], "meta": {"channel": "prose"}},
                ]
            ]
        )
    )

    result_with = run_loop(with_frame, EventLog(tmp_path / "with.db"))
    result_without = run_loop(without_frame, EventLog(tmp_path / "without.db"))

    def prose_bytes(tap: Tap) -> bytes:
        return b"".join(
            str(event.get("delta") or "").encode("utf-8")
            for event in tap.events
            if event.get("type") == "content"
        )

    assert prose_bytes(with_frame) == prose_bytes(without_frame) == prose.encode("utf-8")
    assert result_with.text == result_without.text == prose
    assert [i.text for i in result_with.iterations] == [i.text for i in result_without.iterations]
    # The frame carries no delta at all — nothing for a prose consumer to read.
    finish_frames = [e for e in with_frame.events if e.get("type") == "finish"]
    assert finish_frames == [{"type": "finish", "finish_reason": "stop"}]
    assert "delta" not in finish_frames[0]


def test_every_round_of_a_repair_loop_records_its_own_reason(tmp_path):
    """A multi-round loop names the reason of EACH call, not just the last."""
    log = EventLog(tmp_path / "loop.db")
    caller = WireCaller(
        [
            reasoning_round(REASONING_UNIT, "A") + [{"type": "finish", "finish_reason": "tool_calls"}],
            [{"type": "tool_call", "delta": '{"note": "B"}', "meta": {"name": "leave_note"}},
             {"type": "finish", "finish_reason": "length"}],
            [{"type": "content", "delta": "Done."},
             {"type": "finish", "finish_reason": "stop"}],
        ]
    )

    result = run_loop(caller, log)

    assert [row["finish_reason"] for row in completion_rows(log)] == [
        "tool_calls",
        "length",
        "stop",
    ]
    assert result.finish_reason == "stop"


@pytest.mark.parametrize("reason", ["stop", "length", None])
def test_the_loop_does_not_apply_the_allowlist_itself(tmp_path, reason):
    """The reader RECORDS the reason; ADR-021's verdict belongs to the caller."""
    log = EventLog(tmp_path / "loop.db")

    result = run_loop(WireCaller([prose_and_finish("Rain.", reason)]), log)

    assert result.text == "Rain."
    assert result.finish_reason == reason
