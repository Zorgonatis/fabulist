"""Tests for the GM dice-roll seam (PLAN §4 pre-turn "GM rolls").

Covers both request-scanning forms (OOC ``/roll`` commands and inline
``[...]`` bracketed tokens), the expression grammar, determinism under a seeded
``random.Random`` (plus a scripted RNG for exact detail strings), and the
schema-shaped payload helper. Pure/unit: no event log, no network.
"""

from __future__ import annotations

import random

import pytest

from fabulist.graph.rolls import (
    MAX_DICE_PER_TERM,
    MAX_SIDES,
    RollResult,
    parse_roll_requests,
    roll_dice,
    roll_payload,
    rolls_for_turn,
)
from fabulist.log.schema_gate import validate_event


class ScriptedRNG:
    """Deterministic ``random.Random`` stand-in returning faces in order.

    Also asserts every requested range is well-formed, so a bug that rolls
    ``randint(0, sides)`` or the wrong sidedness fails the test.
    """

    def __init__(self, *faces: int) -> None:
        self._faces = list(faces)

    def randint(self, a: int, b: int) -> int:
        if not self._faces:
            raise AssertionError("ScriptedRNG exhausted")
        value = self._faces.pop(0)
        assert a <= value <= b, f"face {value} outside requested range {a}..{b}"
        return value


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parse_ooc_command():
    assert parse_roll_requests("/roll 2d6+1") == ["2d6+1"]


def test_parse_ooc_command_case_insensitive_and_d_normed():
    assert parse_roll_requests("/ROLL 2D6") == ["2d6"]


def test_parse_bracketed_keyword_and_bare():
    text = "She swings [roll 2d6]. Then [1d4+2]."
    assert parse_roll_requests(text) == ["2d6", "1d4+2"]


def test_parse_bracketed_trailing_prose_ignored():
    assert parse_roll_requests("[roll 2d6 for damage]") == ["2d6"]


def test_parse_multi_term():
    assert parse_roll_requests("[2d6+1d4+2]") == ["2d6+1d4+2"]


def test_parse_single_die():
    assert parse_roll_requests("[d20]") == ["d20"]


def test_parse_whitespace_inside_brackets():
    assert parse_roll_requests("[ 2d6 + 1 ]") == ["2d6+1"]


def test_parse_slash_roll_in_brackets_not_double_counted():
    assert parse_roll_requests("[/roll 1d20]") == ["1d20"]


def test_parse_order_preserved():
    assert parse_roll_requests("[1d20] then /roll 2d6+1") == ["1d20", "2d6+1"]


def test_parse_invalid_skipped_silently():
    text = "[hello] and [2x6] and /roll nope and [0d6] and [d0] and [1d]"
    assert parse_roll_requests(text) == []


def test_parse_no_match_empty():
    assert parse_roll_requests("just some prose, no rolls here.") == []


def test_parse_empty_text():
    assert parse_roll_requests("") == []


def test_parse_guardrail_caps_are_invalid():
    assert parse_roll_requests(f"[{MAX_DICE_PER_TERM + 1}d6]") == []
    assert parse_roll_requests(f"[1d{MAX_SIDES + 1}]") == []


# ---------------------------------------------------------------------------
# rolling / detail strings
# ---------------------------------------------------------------------------


def test_roll_detail_multi_dice_plus_constant():
    result = roll_dice("2d6+1", rng=ScriptedRNG(3, 4))
    assert result == RollResult(dice="2d6+1", result=8, detail="2d6[3,4]+1 = 8")


def test_roll_detail_single_die():
    result = roll_dice("d20", rng=ScriptedRNG(13))
    assert result.dice == "d20"
    assert result.result == 13
    assert result.detail == "d20[13] = 13"


def test_roll_detail_multi_term_with_negative_constant():
    result = roll_dice("2d6+1d4-2", rng=ScriptedRNG(3, 4, 2))
    assert result.dice == "2d6+1d4-2"
    assert result.result == 7
    assert result.detail == "2d6[3,4]+1d4[2]-2 = 7"


def test_roll_case_insensitive_d():
    result = roll_dice("2D6+1", rng=ScriptedRNG(3, 4))
    assert result.dice == "2d6+1"
    assert result.result == 8


def test_roll_implicit_single_count():
    result = roll_dice("d6", rng=ScriptedRNG(5))
    assert result.dice == "d6"
    assert result.detail == "d6[5] = 5"


def test_roll_negative_leading_dice():
    result = roll_dice("-2d6+10", rng=ScriptedRNG(3, 4))
    assert result.dice == "-2d6+10"
    assert result.result == 3  # 10 - (3 + 4)


def test_roll_invalid_raises():
    with pytest.raises(ValueError):
        roll_dice("nonsense")


def test_roll_default_rng_in_range():
    result = roll_dice("1d6")
    assert 1 <= result.result <= 6


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_rolls_for_turn_deterministic_same_seed():
    text = "[2d6+1] /roll 1d20"
    first = rolls_for_turn(text, random.Random(1234))
    second = rolls_for_turn(text, random.Random(1234))
    assert first == second
    assert [r.dice for r in first] == ["2d6+1", "1d20"]


def test_rolls_for_turn_different_seed_differs():
    first = rolls_for_turn("[10d100]", random.Random(1))
    second = rolls_for_turn("[10d100]", random.Random(2))
    assert first[0].result != second[0].result


def test_rolls_for_turn_no_requests():
    assert rolls_for_turn("no rolls here", random.Random(0)) == []


# ---------------------------------------------------------------------------
# ask (owner steer 2026-10-10): trailing prose = the roll's purpose
# ---------------------------------------------------------------------------


def test_bracket_ask_captured():
    result = rolls_for_turn("[roll 2d6 for damage]", random.Random(0))[0]
    assert result.dice == "2d6"
    assert result.ask == "for damage"


def test_bracket_without_ask_is_none():
    assert rolls_for_turn("[2d6]", random.Random(0))[0].ask is None


def test_ooc_ask_captured():
    result = rolls_for_turn("/roll 2d6 for persuasion", random.Random(0))[0]
    assert result.dice == "2d6"
    assert result.ask == "for persuasion"


def test_ooc_without_ask_is_none():
    assert rolls_for_turn("/ROLL 2D6", random.Random(0))[0].ask is None


def test_ask_whitespace_collapsed_and_truncated_on_word_boundary():
    raw = "[1d20 " + " ".join(["word"] * 40) + "]"
    ask = rolls_for_turn(raw, random.Random(0))[0].ask
    assert ask is not None
    assert len(ask) <= 80
    assert "  " not in ask
    # Truncation keeps whole words only.
    assert all(token == "word" for token in ask.split())


def test_ask_round_trips_through_payload_and_gate():
    result = roll_dice("1d20", rng=ScriptedRNG(5), ask="for damage")
    payload = result.as_payload()
    assert payload["ask"] == "for damage"
    assert validate_event("roll", payload) == []
    # No ask -> key omitted entirely (clean optional).
    assert "ask" not in roll_dice("1d20", rng=ScriptedRNG(5)).as_payload()


# ---------------------------------------------------------------------------
# schema-shaped payload
# ---------------------------------------------------------------------------


def test_roll_payload_matches_schema_gate():
    result = roll_dice("2d6+1", rng=ScriptedRNG(3, 4))
    payload = roll_payload(result)
    # S4e/HB1: the payload keeps dice+result and now carries the face detail.
    assert payload == {"dice": "2d6+1", "result": 8, "detail": "2d6[3,4]+1 = 8"}
    assert result.as_payload() == payload
    # The append gate requires {"dice": str, "result": int}; detail is optional.
    assert validate_event("roll", payload) == []
    assert validate_event("roll", {"dice": "1d20", "result": 12}) == []
    assert validate_event(
        "roll", {"dice": "1d20", "result": 12, "detail": "1d20[12] = 12", "ask": "x"}
    ) == []
