"""Tests for the W8.5 structured-output boundary (`fabulist.state.structured`).

Reasoning tags below are built from ``chr(60)``/``chr(62)`` so the literal control-token
sequences never appear in this source file, matching ``tests/test_extractor.py``.

No new dependencies: stdlib + pytest only (the boundary deliberately ships no
JSON-schema validator — the per-agent parser stays the schema authority).
"""

from __future__ import annotations

import pytest

from fabulist.state import structured
from fabulist.state.structured import (
    BTS_REASONING_MIN_TOKENS,
    NON_REASONING_MIN_TOKENS,
    REASON_CODES,
    REASON_EMPTY,
    REASON_FINISH,
    REASON_MALFORMED,
    REASON_SHAPE,
    REASON_TRUNCATED,
    REASONING_MIN_TOKENS,
    StructuredFailure,
    is_reasoning_model,
    parse_content_json,
    parse_tool_args,
    reasoning_floor,
    structured_result,
)

_LT = chr(60)
_GT = chr(62)
THINK_OPEN = _LT + "think" + _GT
THINK_CLOSE = _LT + "/think" + _GT

# A complete BTS-shaped payload and the same payload cut off mid-stream (the spike's
# streamed-tool-args failure mode).
FULL_STATE = '{"state": {"loc": "night market", "tone": "warm"}, "beats": ["the rain starts"]}'
TRUNCATED_STATE = '{"state": {"loc": "night market"}, "beats": [{"action": "dialogue"'


# --- parse_tool_args: the happy path ----------------------------------------


def test_parse_tool_args_decodes_a_complete_object():
    assert parse_tool_args(FULL_STATE) == {
        "state": {"loc": "night market", "tone": "warm"},
        "beats": ["the rain starts"],
    }


def test_parse_tool_args_ignores_braces_inside_strings():
    raw = '{"note": "a } b { c", "quote": "she said \\"go\\""}'
    assert parse_tool_args(raw) == {"note": "a } b { c", "quote": 'she said "go"'}


def test_parse_tool_args_returns_any_json_value_for_the_ladder_to_shape_check():
    # Any JSON value decodes; `structured_result` is what requires an object.
    assert parse_tool_args("[1, 2]") == [1, 2]
    assert parse_tool_args("null") is None


# --- parse_tool_args: truncation recovery -----------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # unclosed outer + inner object
        ('{"state": {"loc": "night market"', {"state": {"loc": "night market"}}),
        # the recovery must NOT drop members after the last closed inner object
        (TRUNCATED_STATE, {"state": {"loc": "night market"}, "beats": [{"action": "dialogue"}]}),
        # unclosed array of objects
        ('{"beats": [{"action": "travel"', {"beats": [{"action": "travel"}]}),
        # cut on a trailing comma
        ('{"loc": "market",', {"loc": "market"}),
        # cut on a dangling member key — the incomplete member is dropped
        ('{"loc": "market", "tone": ', {"loc": "market"}),
        # cut immediately after a nested object, before the next key
        ('{"a": {"b": 1}, "c": ', {"a": {"b": 1}}),
    ],
)
def test_parse_tool_args_recovers_truncated_json(raw, expected):
    assert parse_tool_args(raw) == expected


def test_parse_tool_args_closes_a_string_cut_mid_value():
    assert parse_tool_args('{"loc": "night mar') == {"loc": "night mar"}


def test_parse_tool_args_closes_a_string_cut_after_a_backslash():
    # The stream ended on an escape character; the closer must not be swallowed by it.
    assert parse_tool_args('{"loc": "night \\') == {"loc": "night \\"}


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"loc": }',
        "{loc: market}",
        '{"a": 1}}',
    ],
)
def test_parse_tool_args_unrecoverable_is_malformed(raw):
    failure = parse_tool_args(raw)
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_MALFORMED
    assert failure.detail


@pytest.mark.parametrize("raw", ["", "   ", "\n\t", None, 5])
def test_parse_tool_args_empty_is_failure(raw):
    failure = parse_tool_args(raw)
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_EMPTY


def test_parse_tool_args_empty_never_decodes_to_an_empty_object():
    # The spike's reasoning trap: empty content must be a failure, not `{}`.
    assert not isinstance(parse_tool_args(""), dict)


# --- parse_content_json: fenced and bare ------------------------------------


def test_parse_content_json_reads_a_fenced_block():
    text = '```json\n{"verdict": "ok", "reason": "fine", "edits": []}\n```'
    assert parse_content_json(text) == {"verdict": "ok", "reason": "fine", "edits": []}


def test_parse_content_json_reads_a_plain_fenced_block():
    assert parse_content_json('```\n{"loc": "market"}\n```') == {"loc": "market"}


def test_parse_content_json_reads_an_unfenced_object_in_prose():
    # DSL discipline: statements may arrive unfenced, so a bare object is accepted.
    text = 'Here is the state update: {"loc": "night market"} — done.'
    assert parse_content_json(text) == {"loc": "night market"}


def test_parse_content_json_skips_stray_prose_braces():
    text = 'Use {braces} like this: {"loc": "market"}'
    assert parse_content_json(text) == {"loc": "market"}


def test_parse_content_json_strips_think_tags_before_parsing():
    text = THINK_OPEN + "weighing the options" + THINK_CLOSE + '{"loc": "market"}'
    assert parse_content_json(text) == {"loc": "market"}


def test_parse_content_json_strips_a_think_fence_before_parsing():
    text = "```think\ninternal deliberation\n```\n```json\n{\"loc\": \"market\"}\n```"
    assert parse_content_json(text) == {"loc": "market"}


@pytest.mark.parametrize("text", ["", "   ", None, 7])
def test_parse_content_json_empty_is_failure(text):
    failure = parse_content_json(text)
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_EMPTY


def test_parse_content_json_reasoning_only_content_is_empty_failure():
    failure = parse_content_json(THINK_OPEN + "2444 chars of reasoning" + THINK_CLOSE)
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_EMPTY


@pytest.mark.parametrize("text", ["no json here", "[]", "[1, 2]", "```json\nnot json\n```"])
def test_parse_content_json_without_an_object_is_malformed(text):
    failure = parse_content_json(text)
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_MALFORMED


def test_parse_content_json_does_not_accept_an_object_nested_in_a_truncated_payload():
    # The truncated outer object must be repaired as a whole (see the ladder); its inner
    # object is not a payload on its own.
    failure = parse_content_json(TRUNCATED_STATE)
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_MALFORMED


# --- structured_result: the ladder ------------------------------------------


def test_structured_result_prefers_tool_args_over_content():
    assert structured_result(
        tool_args='{"loc": "market"}', content='{"loc": "wrong"}'
    ) == {"loc": "market"}


def test_structured_result_uses_content_when_tool_args_are_absent():
    assert structured_result(content='{"loc": "market"}') == {"loc": "market"}


def test_structured_result_degrades_to_content_on_malformed_tool_args():
    # Tier A degrade: a reasoning model handed tool_choice="auto" may answer in prose.
    assert structured_result(tool_args="not json", content='{"loc": "market"}') == {
        "loc": "market"
    }


@pytest.mark.parametrize(
    ("content", "tool_args"),
    [(None, None), ("", ""), ("   ", None), (None, ""), (3, 4)],
)
def test_structured_result_empty_is_failure(content, tool_args):
    failure = structured_result(content=content, tool_args=tool_args)
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_EMPTY


@pytest.mark.parametrize("finish_reason", ["length"])
def test_structured_result_finish_reason_length_is_failure(finish_reason):
    # Even a perfectly parseable payload is untrustworthy once the model hit max_tokens.
    failure = structured_result(
        content='{"loc": "market"}', tool_args='{"loc": "market"}', finish_reason=finish_reason
    )
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_TRUNCATED
    assert "length" in failure.detail


@pytest.mark.parametrize("finish_reason", [None, "", "stop", "tool_calls"])
def test_structured_result_passes_an_expected_finish_reason(finish_reason):
    # The allowlist: no reason reported, or a clean terminal reason, reaches the parser.
    assert structured_result(
        content='{"loc": "market"}', finish_reason=finish_reason
    ) == {"loc": "market"}


@pytest.mark.parametrize("finish_reason", ["content_filter", "function_call", "weird_stop"])
def test_structured_result_discards_an_unexpected_finish_reason(finish_reason):
    # ALLOWLIST, not a ``length`` blocklist: a reason we do not expect discards the reply
    # even though this payload parses, and the detail names the value the server sent.
    failure = structured_result(
        content='{"loc": "market"}', finish_reason=finish_reason
    )
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_FINISH
    assert failure.reason in REASON_CODES
    assert finish_reason in failure.detail


def test_structured_result_rejects_a_non_object_payload_with_the_shape_reason():
    failure = structured_result(tool_args="[1, 2]")
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_SHAPE
    assert "list" in failure.detail


def test_structured_result_repairs_once_then_succeeds():
    # No complete object here: only closing the truncated outer object recovers it.
    assert structured_result(content=TRUNCATED_STATE) == {
        "state": {"loc": "night market"},
        "beats": [{"action": "dialogue"}],
    }


def test_structured_result_repairs_once_then_fails():
    failure = structured_result(content='{"state": {"loc": ')
    assert isinstance(failure, StructuredFailure)
    assert failure.reason == REASON_MALFORMED
    assert "repair" in failure.detail


def test_structured_result_attempts_exactly_one_repair(monkeypatch):
    calls: list[str] = []
    real = structured.extract_json_object

    def spy(text: str) -> str | None:
        calls.append(text)
        return real(text)

    monkeypatch.setattr(structured, "extract_json_object", spy)
    failure = structured_result(content="{not json} " * 3)
    assert isinstance(failure, StructuredFailure)
    assert len(calls) == 1


def test_structured_result_failure_string_surfaces_the_reason_and_detail():
    failure = structured_result(content="no json here")
    assert isinstance(failure, StructuredFailure)
    assert failure.reason in REASON_CODES
    rendered = str(failure)
    assert rendered.startswith(f"{failure.reason}: ")
    assert failure.detail in rendered


# --- W8.3 reasoning-budget guard --------------------------------------------


def test_reasoning_floor_constants_are_pinned():
    assert REASONING_MIN_TOKENS == 4096
    assert BTS_REASONING_MIN_TOKENS == 8192
    assert NON_REASONING_MIN_TOKENS == 0
    # Measured: 600 returned empty content, 2000 answered — the floor must clear both.
    assert REASONING_MIN_TOKENS > 2000
    assert BTS_REASONING_MIN_TOKENS > REASONING_MIN_TOKENS


def test_reasoning_floor_for_deepseek_flash():
    assert reasoning_floor("deepseek-flash") == REASONING_MIN_TOKENS
    assert reasoning_floor("deepseek-flash", interaction="bts") == BTS_REASONING_MIN_TOKENS


def test_reasoning_floor_clamps_up_and_never_down():
    assert reasoning_floor("deepseek-flash", current_max_tokens=600) == REASONING_MIN_TOKENS
    assert reasoning_floor("deepseek-flash", current_max_tokens=99999) == 99999
    assert reasoning_floor("deepseek-flash", interaction="bts", current_max_tokens=2000) == (
        BTS_REASONING_MIN_TOKENS
    )


def test_reasoning_floor_ignores_a_non_int_budget():
    # bool is an int subclass in Python — a truthy flag must not become the budget.
    assert reasoning_floor("deepseek-flash", current_max_tokens=True) == REASONING_MIN_TOKENS


@pytest.mark.parametrize("hint", ["your-model-id", "llama-your-model-id", None, "", "non-reasoning"])
def test_reasoning_floor_leaves_non_reasoning_models_alone(hint):
    # Measured: the local llama your-model-id is not a reasoning model and needs no bump.
    assert reasoning_floor(hint) == NON_REASONING_MIN_TOKENS
    assert reasoning_floor(hint, interaction="bts", current_max_tokens=800) == 0
    assert is_reasoning_model(hint) is False


@pytest.mark.parametrize("hint", ["deepseek-flash", "deepseek-chat", "reasoning", "qwq-32b"])
def test_is_reasoning_model_recognises_reasoning_hints(hint):
    assert is_reasoning_model(hint) is True


def test_reasoning_floor_uses_the_bts_checkpoint_interaction():
    # "bts" is the interaction id that emits the largest structured payload.
    assert reasoning_floor("deepseek-flash", interaction="bts") == BTS_REASONING_MIN_TOKENS
    assert reasoning_floor("deepseek-flash", interaction="extract") == REASONING_MIN_TOKENS
    assert reasoning_floor("deepseek-flash", interaction="critic") == REASONING_MIN_TOKENS
