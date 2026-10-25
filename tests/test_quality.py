"""Pure unit tests for the D41 critic-fixer core (``graph/quality.py``).

No models, no log, no graph: the parser and the patch applier are deterministic
and side-effect free. Covers the tolerant parse shapes and the atomic
find/replace contracts the graph wiring lane depends on.
"""

from __future__ import annotations

import json

from fabulist.graph.quality import (
    VERDICT_DIFFS,
    VERDICT_FAIL,
    VERDICT_OK,
    CriticVerdict,
    QualityEdit,
    apply_patch,
    parse_critic_output,
)


def seg(kind: str, text: str) -> dict:
    return {"kind": kind, "text": text}


# ---------------------------------------------------------------------------
# parse_critic_output
# ---------------------------------------------------------------------------


def test_parse_ok():
    out = parse_critic_output('{"verdict": "ok", "reason": "reads clean"}')
    assert out == CriticVerdict(verdict=VERDICT_OK, reason="reads clean")
    assert out.parse_failed is False
    assert out.edits == ()


def test_parse_diffs_normalises_edits_and_defaults_count():
    text = json.dumps(
        {
            "verdict": "diffs",
            "reason": "one pronoun",
            "edits": [
                {"find": "he", "replace": "she", "count": 2},
                {"find": "Mr.", "replace": "Ms."},
            ],
        }
    )
    out = parse_critic_output(text)
    assert out.verdict == VERDICT_DIFFS
    assert out.parse_failed is False
    assert out.edits == (QualityEdit("he", "she", 2), QualityEdit("Mr.", "Ms.", 1))


def test_parse_fail_carries_regen_feedback():
    text = (
        '{"verdict": "fail", "reason": "blended two scenes", '
        '"regen_feedback": "keep it to the inn"}'
    )
    out = parse_critic_output(text)
    assert out.verdict == VERDICT_FAIL
    assert out.regen_feedback == "keep it to the inn"
    assert out.parse_failed is False


def test_parse_uppercase_verdict_is_tolerated():
    out = parse_critic_output('{"verdict": "DIFFS", "edits": []}')
    assert out.verdict == VERDICT_DIFFS
    assert out.parse_failed is False


def test_parse_fenced_block():
    text = 'Here is my review.\n```json\n{"verdict": "ok", "reason": "fine"}\n```\n'
    out = parse_critic_output(text)
    assert out.verdict == VERDICT_OK
    assert out.parse_failed is False


def test_parse_surrounded_by_prose_with_stray_brace():
    text = (
        "I looked at the turn { nothing to see here.\n"
        'The object is {"verdict": "diffs", "edits": [{"find": "a", "replace": "b"}]} '
        "and that is all.\n"
    )
    out = parse_critic_output(text)
    assert out.verdict == VERDICT_DIFFS
    assert out.edits == (QualityEdit("a", "b"),)
    assert out.parse_failed is False


def test_parse_braces_inside_string_do_not_unbalance():
    out = parse_critic_output('{"verdict": "ok", "reason": "uses {braces} in a string"}')
    assert out.parse_failed is False
    assert out.reason == "uses {braces} in a string"


def test_parse_malformed_json_is_rejected_not_raised():
    out = parse_critic_output('{"verdict": "ok",}')
    assert out.parse_failed is True
    assert out.verdict == ""


def test_parse_no_json_object_is_rejected():
    out = parse_critic_output("the model just rambled with no object")
    assert out.parse_failed is True
    assert out.verdict == ""


def test_parse_empty_output_is_rejected():
    assert parse_critic_output("").parse_failed is True
    assert parse_critic_output(None).parse_failed is True


def test_parse_missing_verdict_is_rejected():
    out = parse_critic_output('{"reason": "fine"}')
    assert out.parse_failed is True
    assert out.verdict == ""


def test_parse_unknown_verdict_is_rejected():
    out = parse_critic_output('{"verdict": "maybe", "edits": []}')
    assert out.parse_failed is True
    assert out.verdict == ""


def test_parse_non_list_edits_is_rejected():
    out = parse_critic_output('{"verdict": "diffs", "edits": {"find": "a"}}')
    assert out.parse_failed is True
    assert out.verdict == ""


def test_parse_non_string_find_is_rejected():
    out = parse_critic_output('{"verdict": "diffs", "edits": [{"find": 3, "replace": "b"}]}')
    assert out.parse_failed is True


def test_parse_non_string_replace_is_rejected():
    out = parse_critic_output('{"verdict": "diffs", "edits": [{"find": "a", "replace": null}]}')
    assert out.parse_failed is True


def test_parse_bad_count_is_rejected():
    out = parse_critic_output(
        '{"verdict": "diffs", "edits": [{"find": "a", "replace": "b", "count": 0}]}'
    )
    assert out.parse_failed is True
    assert parse_critic_output(
        '{"verdict": "diffs", "edits": [{"find": "a", "replace": "b", "count": true}]}'
    ).parse_failed is True


# ---------------------------------------------------------------------------
# apply_patch
# ---------------------------------------------------------------------------


def test_apply_exact_match():
    segments = [seg("prose", "The cat sat on the mat.")]
    result = apply_patch(segments, [QualityEdit("cat", "dog")])
    assert result.ok is True
    assert result.segments == [seg("prose", "The dog sat on the mat.")]
    assert result.applied == 1


def test_apply_accepts_raw_dict_edits():
    segments = [seg("prose", "The cat sat.")]
    result = apply_patch(segments, [{"find": "cat", "replace": "dog"}])
    assert result.ok is True
    assert result.segments[0]["text"] == "The dog sat."


def test_apply_zero_match_rejects_whole_patch():
    segments = [seg("prose", "Hello world"), seg("dsl", "root = ScenePane()")]
    result = apply_patch(segments, [QualityEdit("Hello", "Hi"), QualityEdit("Goodbye", "Bye")])
    assert result.ok is False
    assert result.failed_index == 1
    assert "not present" in result.error
    # The whole patch is rejected: the first (valid) edit did NOT land.
    assert result.segments == segments
    assert result.prose == "Hello world"
    # And the caller's input is never mutated.
    assert segments[0]["text"] == "Hello world"


def test_apply_wrong_count_rejects():
    segments = [seg("prose", "a a a")]
    result = apply_patch(segments, [QualityEdit("a", "b", count=2)])
    assert result.ok is False
    assert result.failed_index == 0
    assert "occurs 3" in result.error
    assert result.segments == segments


def test_apply_matching_multi_count_succeeds():
    segments = [seg("prose", "a a a")]
    result = apply_patch(segments, [QualityEdit("a", "b", count=3)])
    assert result.ok is True
    assert result.segments[0]["text"] == "b b b"


def test_apply_find_spanning_two_segments_is_rejected():
    segments = [seg("prose", "shared"), seg("prose", "shared")]
    result = apply_patch(segments, [QualityEdit("shared", "x", count=2)])
    assert result.ok is False
    assert "spans 2 prose segments" in result.error


def test_apply_multi_segment_edits():
    dsl = seg("dsl", "root = ScenePane()")
    segments = [seg("prose", "Hello world"), dsl, seg("prose", "Goodbye world")]
    result = apply_patch(segments, [QualityEdit("Hello", "Hi"), QualityEdit("Goodbye", "Bye")])
    assert result.ok is True
    assert result.segments[0]["text"] == "Hi world"
    assert result.segments[2]["text"] == "Bye world"
    assert result.applied == 2


def test_apply_leaves_dsl_segments_untouched():
    dsl = seg("dsl", "card = ArtifactCard(name=\"Tense\")\nroot = ScenePane([card])")
    segments = [seg("prose", "The cat sat."), dsl]
    result = apply_patch(segments, [QualityEdit("cat", "dog")])
    assert result.ok is True
    assert result.segments[1] == dsl
    assert result.segments[1] is not dsl  # copied, not aliased


def test_apply_returned_prose_equals_joined_rebuilt_segments():
    segments = [seg("prose", "Hello world"), seg("dsl", "d"), seg("prose", "Goodbye world")]
    result = apply_patch(segments, [QualityEdit("Hello", "Hi"), QualityEdit("Goodbye", "Bye")])
    assert result.prose == "Hi world\nBye world"
    assert result.prose == "\n".join(
        s["text"] for s in result.segments if s["kind"] == "prose"
    )


def test_apply_identical_patch_twice_is_rejected_second_time():
    edit = QualityEdit("cat", "dog")
    first = apply_patch([seg("prose", "The cat sat.")], [edit])
    assert first.ok is True
    assert first.segments[0]["text"] == "The dog sat."

    second = apply_patch(first.segments, [edit])
    assert second.ok is False
    assert second.segments == first.segments
    assert second.segments[0]["text"] == "The dog sat."  # no double substitution


def test_apply_empty_find_is_rejected():
    result = apply_patch([seg("prose", "abc")], [QualityEdit("", "x")])
    assert result.ok is False
    assert result.segments == [seg("prose", "abc")]
