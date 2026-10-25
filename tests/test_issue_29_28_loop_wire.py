"""Issues #29 and #28 on the agent loop's streaming wire (``agents/loop.py``).

#29 — the reasoning echo's BOUNDARY (owner ruling, re-cut here). The current round
is carried VERBATIM: every iteration's ``reasoning_content`` stays on its own
assistant message, one block per iteration, and every one of them is paid for as
prompt tokens. That is deliberate — a repair round must see its own deliberation.
The cost trim belongs at the ROUND boundary, not the iteration boundary: prior
graph executions are re-attached by history assembly with the field PRESENT but
EMPTY (``wiring.history_from_log``), so a past turn's thinking is never re-sent as
text. Pinned here: a three-iteration round's final request carries ALL THREE
blocks with their original text, the vendor's per-assistant FIELD presence rule
still holds, and an iteration that reasoned nothing adds no field without erasing
an earlier block.

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


# -- #29: the CURRENT round travels verbatim, in full ------------------------

#: Four rounds of CONSTANT-SIZE reasoning: the whole round is carried, so request
#: N carries N-1 blocks and the prompt grows by one block per round — the
#: deliberate cost of a round seeing its own deliberation.
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


def test_every_iterations_reasoning_text_rides_the_final_request_verbatim(tmp_path):
    """Three tool iterations: ALL THREE blocks are present with their text.

    Owner ruling: the current round keeps its reasoning verbatim. A repair round
    must see its own deliberation, so nothing is blanked as the next iteration is
    stamped — the final request carries one block per iteration, exactly as the
    provider sent it.
    """
    blocks = [
        "Weighed the lantern against the tide.",
        "The note is not recorded yet.",
        "Record it, then answer in prose.",
    ]
    caller = WireCaller(
        [
            reasoning_round(blocks[0], "A"),
            reasoning_round(blocks[1], "B"),
            reasoning_round(blocks[2], "C"),
            [
                {"type": "reasoning", "delta": "The lantern is lit."},
                {"type": "content", "delta": "The lantern is lit."},
                {"type": "finish", "finish_reason": "stop"},
            ],
        ]
    )
    result = run_loop(caller, EventLog(tmp_path / "loop.db"))

    assert len(caller.requests) == 4
    assert caller.requests[0] == PREFIX  # the first request is untouched
    final_request = caller.requests[-1]
    carried = reasoning_fields(final_request)
    # One FIELD per assistant tool_calls turn, each with ITS OWN verbatim text.
    assert [message["reasoning_content"] for _, message in carried] == blocks
    for _, message in carried:
        assert message["role"] == "assistant"
        assert message["tool_calls"], "the echo belongs on a tool_calls turn"
    # The trace records every iteration's reasoning too.
    assert [iteration.reasoning for iteration in result.iterations] == [
        *blocks,
        "The lantern is lit.",
    ]


def test_the_current_round_pays_for_every_block_it_has_reasoned(tmp_path):
    """The full-round echo is DELIBERATE: request N carries N-1 paid blocks.

    The cost trim lives at the ROUND boundary (history assembly re-attaches past
    executions present-but-empty), so within one round the prompt grows by one
    reasoning block per iteration — the intentional price of a round seeing its
    own deliberation. There is no per-iteration retirement left to hide it.
    """
    caller = _four_round_caller()
    run_loop(caller, EventLog(tmp_path / "loop.db"))

    carried = [
        sum(len(str(m.get("reasoning_content") or "")) for m in request)
        for request in caller.requests
    ]
    # Round N pays for the N-1 blocks already reasoned inside THIS round (the
    # fourth request carries rounds 1..3); nothing predates the round.
    assert carried == [
        0,
        len(REASONING_UNIT),
        2 * len(REASONING_UNIT),
        3 * len(REASONING_UNIT),
    ]


def test_an_iteration_that_reasons_nothing_fills_the_field_and_erases_nothing(tmp_path):
    """Round 2 sent no field: it is filled ``""`` (no MIXED request) and round 1's
    block stays verbatim.

    The vendor validates presence across the WHOLE request (measured on
    ``deepseek-flash`` with ``tools``: none present = 200, all present = 200,
    MIXED = 400), so a turn that reasoned nothing inside a conversation that
    already carries the field must carry it too — with the vendor's own
    "nothing reasoned" value, never invented text.
    """
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
    # Request 3 continues from round 2, which reasoned nothing: presence must stay
    # UNIFORM, so that turn carries the field EMPTY (filled by
    # ``normalise_reasoning_presence``, the runtime never invents text) while round
    # 1's block is still there verbatim — nothing is blanked or rewritten.
    carried = reasoning_fields(caller.requests[2])
    assert [m["reasoning_content"] for _, m in carried] == [REASONING_UNIT, ""]
    assert [iteration.reasoning for iteration in result.iterations] == [
        REASONING_UNIT,
        None,
        None,
    ]


def test_the_injected_flags_pair_keeps_every_rounds_block_verbatim(tmp_path):
    """The engine-authored pair obeys the same full-round rule.

    ISSUE #42 (live DeepSeek-V4.1-Flash): once the request carries ``tools``,
    the vendor validates ``reasoning_content`` on EVERY assistant message — the
    flagged draft included — so the draft carries the FIELD with the vendor's
    empty value while the synthetic ``tool_calls`` turn carries that iteration's
    block verbatim. Issue #29's owner ruling: earlier pairs keep THEIR blocks too;
    nothing is retired inside the round.
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

    # Request 3: BOTH pairs carry their iteration's block verbatim — round 1's
    # text was not blanked when round 2's pair was stamped.
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
        first_reasoning,
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
