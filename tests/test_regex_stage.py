"""Tests for the W2.3 regex pipeline stage (preset ``regex_scripts`` port)."""

from __future__ import annotations

import re

import pytest

from fabulist.context.regex_stage import (
    PRESET_REGEXES,
    StageRegex,
    apply_regex_stage,
    compile_stage,
    js_flags_to_re,
    parse_js_regex,
)

# --- representative inputs ---------------------------------------------------------

VTK_TEXT = (
    "The night market hums under light rain.\n"
    "<!-- VTK_START -->\n"
    "time: 14:35 | loc: night market | Δ[weather=light rain]\n"
    "<!-- VTK_END -->\n"
    "A lantern-bearer beckons you closer."
)

ASST_TEXT = (
    "The bell tolls once.\n"
    "<!-- ASST_START -->\n"
    "(assistant scaffolding: plan object, tracker deltas)\n"
    "<!-- ASST_END -->\n"
    "Somewhere, a door closes."
)

CLEAN_TEXT = "Nothing to scrub here: just prose with no markers at all."


# --- parse_js_regex ----------------------------------------------------------------


def test_parse_js_regex_splits_body_and_flags():
    assert parse_js_regex("/abc/gi") == ("abc", "gi")


def test_parse_js_regex_no_flags():
    assert parse_js_regex("/abc/") == ("abc", "")


def test_parse_js_regex_allows_slashes_in_body():
    assert parse_js_regex("/a/b/c/i") == ("a/b/c", "i")


def test_parse_js_regex_rejects_non_literal():
    with pytest.raises(ValueError):
        parse_js_regex("abc", name="Bad Pattern")
    with pytest.raises(ValueError):
        parse_js_regex("/", name="Bad Pattern")


# --- flag conversion ---------------------------------------------------------------


def test_js_flags_to_re_maps_known_flags():
    assert js_flags_to_re("g") == 0
    assert js_flags_to_re("i") == re.IGNORECASE
    assert js_flags_to_re("s") == re.DOTALL
    assert js_flags_to_re("m") == re.MULTILINE
    assert js_flags_to_re("gis") == re.IGNORECASE | re.DOTALL
    assert js_flags_to_re("gi") == re.IGNORECASE


def test_js_flags_to_re_empty_is_zero():
    assert js_flags_to_re("") == 0


def test_js_flags_to_re_unknown_flag_raises_with_pattern_name():
    with pytest.raises(ValueError) as exc:
        js_flags_to_re("gz", name="Stabs - Bad Regex")
    assert "Stabs - Bad Regex" in str(exc.value)
    assert "z" in str(exc.value)


def test_compile_stage_unknown_flag_raises():
    bad = StageRegex(
        id="x",
        name="Stabs - Unknown Flag",
        find="foo",
        replace="",
        flags="z",
        prompt_only=False,
        min_depth=0,
    )
    with pytest.raises(ValueError) as exc:
        compile_stage(bad)
    assert "Stabs - Unknown Flag" in str(exc.value)


def test_compile_stage_applies_ignorecase():
    stage = StageRegex(
        id="x",
        name="casefold",
        find="abc",
        replace="X",
        flags="gi",
        prompt_only=False,
        min_depth=0,
    )
    assert compile_stage(stage).sub("X", "aBc ABC") == "X X"


# --- preset entries ----------------------------------------------------------------


def test_preset_regexes_ship_both_entries():
    names = [r.name for r in PRESET_REGEXES]
    assert "Stabs - Strip VTK from Sent Context" in names
    assert "Stabs - Strip Assistant Block from Sent Context" in names
    assert all(r.enabled for r in PRESET_REGEXES)
    assert all(r.prompt_only for r in PRESET_REGEXES)
    assert all(r.min_depth == 3 for r in PRESET_REGEXES)
    assert all(r.flags == "gi" for r in PRESET_REGEXES)


def test_preset_regexes_round_trip_against_preset_literals():
    """PRESET_REGEXES must reproduce the spec-of-record preset literals exactly."""

    preset_literals = {
        "118e07e4-9b2f-449f-88e7-3f21829f7326": (
            "/<!-- VTK_START -->[\\s\\S]*?<!-- VTK_END -->/gi"
        ),
        "aa908405-b96e-42c6-8fc1-1fd040643619": (
            "/<!-- ASST_START -->[\\s\\S]*?<!-- ASST_END -->/gi"
        ),
    }
    by_id = {r.id: r for r in PRESET_REGEXES}
    for pid, literal in preset_literals.items():
        body, flags = parse_js_regex(literal, name=by_id[pid].name)
        assert by_id[pid].find == body
        assert by_id[pid].flags == flags


def test_stage_regex_from_preset_round_trip():
    entry = {
        "id": "abc",
        "scriptName": "Stabs - Strip VTK from Sent Context",
        "findRegex": "/<!-- VTK_START -->[\\s\\S]*?<!-- VTK_END -->/gi",
        "replaceString": "",
        "disabled": False,
        "promptOnly": True,
        "minDepth": 3,
        "maxDepth": None,
    }
    stage = StageRegex.from_preset(entry)
    assert stage.id == "abc"
    assert stage.name == "Stabs - Strip VTK from Sent Context"
    assert stage.find == r"<!-- VTK_START -->[\s\S]*?<!-- VTK_END -->"
    assert stage.flags == "gi"
    assert stage.replace == ""
    assert stage.prompt_only is True
    assert stage.min_depth == 3
    assert stage.enabled is True


# --- apply_regex_stage: depth gating ----------------------------------------------


def test_vtk_block_stripped_at_depth_three():
    out = apply_regex_stage([VTK_TEXT], depth_index=3, prompt_only=True)
    assert out == [
        "The night market hums under light rain.\n"
        "\n"
        "A lantern-bearer beckons you closer."
    ]
    assert "VTK_START" not in out[0]
    assert "VTK_END" not in out[0]
    assert "Δ[weather=light rain]" not in out[0]


def test_vtk_block_not_stripped_below_min_depth():
    out = apply_regex_stage([VTK_TEXT], depth_index=1, prompt_only=True)
    assert out == [VTK_TEXT]


def test_vtk_block_stripped_at_depth_above_min_depth():
    out = apply_regex_stage([VTK_TEXT], depth_index=5, prompt_only=True)
    assert "VTK_START" not in out[0]


def test_assistant_block_stripped_at_depth_three():
    out = apply_regex_stage([ASST_TEXT], depth_index=3, prompt_only=True)
    assert "ASST_START" not in out[0]
    assert "ASST_END" not in out[0]
    assert out == [
        "The bell tolls once.\n\nSomewhere, a door closes."
    ]


# --- apply_regex_stage: prompt_only gating ----------------------------------------


def test_prompt_only_patterns_skipped_in_display_context():
    out = apply_regex_stage([VTK_TEXT, ASST_TEXT], depth_index=9, prompt_only=False)
    assert out == [VTK_TEXT, ASST_TEXT]


def test_prompt_only_gating_is_independent_of_depth():
    # In display context, depth does not matter: prompt_only stages never run.
    assert apply_regex_stage([VTK_TEXT], depth_index=99, prompt_only=False) == [VTK_TEXT]


def test_non_prompt_only_pattern_runs_in_both_contexts():
    stage = StageRegex(
        id="x",
        name="always",
        find=r"\[\[drop\]\]",
        replace="",
        flags="g",
        prompt_only=False,
        min_depth=0,
    )
    text = "keep [[drop]] me"
    assert apply_regex_stage([text], depth_index=0, prompt_only=False, regexes=(stage,)) == [
        "keep  me"
    ]
    assert apply_regex_stage([text], depth_index=0, prompt_only=True, regexes=(stage,)) == [
        "keep  me"
    ]


def test_prompt_only_custom_pattern_runs_only_for_prompt():
    stage = StageRegex(
        id="x",
        name="only-prompt",
        find="SECRET",
        replace="",
        flags="g",
        prompt_only=True,
        min_depth=0,
    )
    assert apply_regex_stage(
        ["a SECRET b"], depth_index=0, prompt_only=True, regexes=(stage,)
    ) == ["a  b"]
    assert apply_regex_stage(
        ["a SECRET b"], depth_index=0, prompt_only=False, regexes=(stage,)
    ) == ["a SECRET b"]


# --- apply_regex_stage: g / global semantics --------------------------------------


def test_global_flag_replaces_every_occurrence():
    stage = StageRegex(
        id="x",
        name="global",
        find="x",
        replace="y",
        flags="g",
        prompt_only=False,
        min_depth=0,
    )
    assert apply_regex_stage(
        ["x-x-xx"], depth_index=0, prompt_only=False, regexes=(stage,)
    ) == ["y-y-yy"]


def test_preset_global_strips_multiple_blocks():
    two = VTK_TEXT + "\n" + VTK_TEXT
    out = apply_regex_stage([two], depth_index=3, prompt_only=True)
    assert out[0].count("VTK_START") == 0
    assert out[0].count("lantern-bearer") == 2


def test_ignorecase_preset_strips_lowercase_tags():
    lowered = "head\n<!-- vtk_start -->\nsecret\n<!-- vtk_end -->\ntail"
    out = apply_regex_stage([lowered], depth_index=3, prompt_only=True)
    assert out == ["head\n\ntail"]


# --- apply_regex_stage: enabled + no-match passthrough ----------------------------


def test_disabled_pattern_is_skipped():
    disabled = StageRegex(
        id="x",
        name="disabled",
        find="SECRET",
        replace="",
        flags="g",
        prompt_only=False,
        min_depth=0,
        enabled=False,
    )
    assert apply_regex_stage(
        ["a SECRET b"], depth_index=9, prompt_only=True, regexes=(disabled,)
    ) == ["a SECRET b"]


def test_no_match_passthrough_is_byte_identical():
    out = apply_regex_stage([CLEAN_TEXT], depth_index=3, prompt_only=True)
    assert out == [CLEAN_TEXT]
    assert out[0].encode("utf-8") == CLEAN_TEXT.encode("utf-8")
    assert out[0] == CLEAN_TEXT


def test_input_list_not_mutated():
    texts = [VTK_TEXT, ASST_TEXT]
    snapshot = list(texts)
    apply_regex_stage(texts, depth_index=3, prompt_only=True)
    assert texts == snapshot


def test_empty_input_returns_empty_list():
    assert apply_regex_stage([], depth_index=3, prompt_only=True) == []


def test_multiple_texts_processed_independently():
    out = apply_regex_stage(
        [VTK_TEXT, CLEAN_TEXT], depth_index=3, prompt_only=True
    )
    assert out[0] == (
        "The night market hums under light rain.\n"
        "\n"
        "A lantern-bearer beckons you closer."
    )
    assert out[1] == CLEAN_TEXT


# --- replacement backreference conversion -----------------------------------------


def test_js_dollar_backreference_converts_to_python():
    stage = StageRegex(
        id="x",
        name="capture",
        find=r"(\w+)-(\w+)",
        replace="$2-$1",
        flags="g",
        prompt_only=False,
        min_depth=0,
    )
    assert apply_regex_stage(
        ["foo-bar baz-qux"], depth_index=0, prompt_only=False, regexes=(stage,)
    ) == ["bar-foo qux-baz"]
