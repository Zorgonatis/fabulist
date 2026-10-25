"""Tests for the extractor eval rig (W2.12a).

The rig lives at ``scripts/eval_extractor.py`` (not an importable package), so it
is loaded here by path. These tests assert the fixture *schema*, the rig's diff /
taxonomy logic, and the *structure and determinism* of a stub-mode report — never
a particular extractor accuracy number (accuracy is informational at this stage).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from fabulist.state import ExtractorResult, RuleBasedExtractor

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "bts_labeled"
RIG_PATH = REPO_ROOT / "scripts" / "eval_extractor.py"

_spec = importlib.util.spec_from_file_location("fabulist_eval_extractor", RIG_PATH)
assert _spec is not None and _spec.loader is not None
rig = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rig
_spec.loader.exec_module(rig)

#: The ten labeled beats the lane is specified to cover (W2.12a).
EXPECTED_IDS = {
    "first_scene",
    "location_change",
    "time_advance",
    "tone_shift",
    "inventory_add_remove",
    "weather_only",
    "ooc_no_delta",
    "junk_input",
    "multi_key_turn",
    "no_change",
}


def _stub_report():
    cases = rig.load_cases(FIXTURES_DIR)
    return rig.build_report(
        cases, RuleBasedExtractor(), model_name="stub", fixtures_dir=FIXTURES_DIR
    )


# --- fixtures load + schema -------------------------------------------------


def test_fixtures_load_one_case_per_file():
    cases = rig.load_cases(FIXTURES_DIR)
    assert len(cases) == 10
    assert {case["id"] for case in cases} == EXPECTED_IDS


def test_fixtures_validate_against_schema():
    cases = rig.load_cases(FIXTURES_DIR)
    assert rig.validate_cases(cases) == []


@pytest.mark.parametrize("case_id", sorted(EXPECTED_IDS))
def test_each_fixture_has_required_shape(case_id):
    cases = {case["id"]: case for case in rig.load_cases(FIXTURES_DIR)}
    case = cases[case_id]
    for field in rig.CASE_REQUIRED_FIELDS:
        assert field in case
    assert isinstance(case["user_msg"], str)
    assert isinstance(case["turn"], str)
    assert isinstance(case["expected_delta"], dict)
    assert case["expected_action_class"] in rig.ACTION_CLASSES


def test_validate_case_rejects_missing_field():
    errors = rig.validate_case({"id": "x", "description": "d"})
    assert any("missing required field: user_msg" in e for e in errors)


def test_validate_case_rejects_wrong_type():
    errors = rig.validate_case(
        {
            "id": "x",
            "description": "d",
            "user_msg": "u",
            "turn": "t",
            "expected_delta": ["not", "a", "dict"],
            "expected_action_class": "other",
        }
    )
    assert any("expected_delta" in e and "dict" in e for e in errors)


def test_validate_case_rejects_bad_action_class():
    errors = rig.validate_case(
        {
            "id": "x",
            "description": "d",
            "user_msg": "u",
            "turn": "t",
            "expected_delta": {},
            "expected_action_class": "frobnicate",
        }
    )
    assert any("frobnicate" in e for e in errors)


def test_validate_case_rejects_non_string_delta_value():
    errors = rig.validate_case(
        {
            "id": "x",
            "description": "d",
            "user_msg": "u",
            "turn": "t",
            "expected_delta": {"time": 1435},
            "expected_action_class": "other",
        }
    )
    assert any("keys and values must be strings" in e for e in errors)


def test_optional_fields_are_known_types():
    assert set(rig.CASE_OPTIONAL_FIELDS) <= set(rig.CASE_FIELD_TYPES)


def test_validate_cases_detects_duplicate_id():
    base = {
        "id": "same",
        "description": "d",
        "user_msg": "u",
        "turn": "t",
        "expected_delta": {},
        "expected_action_class": "other",
    }
    errors = rig.validate_cases([dict(base), dict(base)])
    assert any("duplicate id" in e for e in errors)


def test_load_cases_missing_dir_raises():
    with pytest.raises(rig.FixtureError):
        rig.load_cases(Path("/nonexistent/bts_labeled"))


def test_load_cases_empty_dir_raises(tmp_path):
    with pytest.raises(rig.FixtureError):
        rig.load_cases(tmp_path)


def test_load_cases_invalid_json_raises(tmp_path):
    (tmp_path / "bad.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(rig.FixtureError):
        rig.load_cases(tmp_path)


def test_load_cases_non_object_raises(tmp_path):
    (tmp_path / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(rig.FixtureError):
        rig.load_cases(tmp_path)


# --- diff / taxonomy --------------------------------------------------------


def test_diff_delta_categories():
    diff = rig.diff_delta({"a": "1", "b": "2"}, {"a": "x", "c": "3"})
    assert diff["missed_keys"] == ["b"]
    assert diff["spurious_keys"] == ["c"]
    assert diff["wrong_values"] == [{"key": "a", "expected": "1", "actual": "x"}]


def test_diff_delta_equal_is_empty():
    assert rig.diff_delta({}, {}) == {
        "missed_keys": [],
        "spurious_keys": [],
        "wrong_values": [],
    }
    assert rig.diff_delta({"k": "v"}, {"k": "v"}) == {
        "missed_keys": [],
        "spurious_keys": [],
        "wrong_values": [],
    }


def _case(**overrides):
    case = {
        "id": "synthetic",
        "description": "synthetic",
        "user_msg": "u",
        "turn": "t",
        "expected_delta": {},
        "expected_action_class": "other",
        "_source": "synthetic.json",
    }
    case.update(overrides)
    return case


def test_evaluate_case_pass_when_delta_and_action_match():
    extractor = lambda user, narrative: ExtractorResult(  # noqa: E731
        deltas={"loc": "docks"}, action_class="travel"
    )
    case = _case(expected_delta={"loc": "docks"}, expected_action_class="travel")
    result = rig.evaluate_case(case, extractor)
    assert result["status"] == "pass"
    assert result["reasons"] == []
    assert result["action_class_match"] is True


def test_evaluate_case_flags_action_class_mismatch():
    extractor = lambda user, narrative: ExtractorResult(  # noqa: E731
        deltas={}, action_class="task"
    )
    case = _case(expected_action_class="rest")
    result = rig.evaluate_case(case, extractor)
    assert result["status"] == "fail"
    assert result["reasons"] == ["action_class_mismatch"]


def test_evaluate_case_flags_every_taxonomy_category():
    extractor = lambda user, narrative: ExtractorResult(  # noqa: E731
        deltas={"spurious": "y", "wrong": "actual"}, action_class="task"
    )
    case = _case(
        expected_delta={"missed": "x", "wrong": "expected"},
        expected_action_class="rest",
    )
    result = rig.evaluate_case(case, extractor)
    assert set(result["reasons"]) == {
        "missed_key",
        "wrong_value",
        "spurious_key",
        "action_class_mismatch",
    }
    assert result["missed_keys"] == ["missed"]
    assert result["spurious_keys"] == ["spurious"]
    assert result["wrong_values"] == [
        {"key": "wrong", "expected": "expected", "actual": "actual"}
    ]


# --- stub report structure + determinism ------------------------------------


def test_stub_report_has_expected_structure():
    report = _stub_report()
    assert set(report) == {
        "model",
        "fixtures_dir",
        "total",
        "passed",
        "failed",
        "score",
        "taxonomy",
        "cases",
    }
    assert report["model"] == "stub"
    assert report["total"] == 10
    assert report["passed"] + report["failed"] == report["total"]
    assert report["score"] == report["passed"] / report["total"]
    assert set(report["taxonomy"]) == set(rig.FAILURE_TAXONOMY)


def test_stub_case_reports_have_expected_fields():
    report = _stub_report()
    assert len(report["cases"]) == 10
    for case in report["cases"]:
        assert set(case) >= {
            "id",
            "status",
            "reasons",
            "expected_delta",
            "actual_delta",
            "expected_action_class",
            "actual_action_class",
            "action_class_match",
            "missed_keys",
            "spurious_keys",
            "wrong_values",
        }
        assert case["status"] in {"pass", "fail"}
        # A case passes exactly when no failure reason applies.
        assert (case["status"] == "pass") == (case["reasons"] == [])
        assert all(reason in rig.FAILURE_TAXONOMY for reason in case["reasons"])


def test_stub_taxonomy_rolls_up_case_reasons():
    report = _stub_report()
    for name in rig.FAILURE_TAXONOMY:
        expected = sum(1 for case in report["cases"] if name in case["reasons"])
        assert report["taxonomy"][name] == expected


def test_stub_report_is_deterministic():
    first = _stub_report()
    second = _stub_report()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_stub_mode_does_not_require_network():
    # The stub extractor is the deterministic RuleBased fallback.
    assert isinstance(rig.build_extractor("stub"), RuleBasedExtractor)


def test_build_extractor_rejects_unknown_model():
    with pytest.raises(ValueError):
        rig.build_extractor("nope")


def test_build_extractor_live_needs_config(tmp_path):
    with pytest.raises(rig.FixtureError):
        rig.build_extractor("live", config_path=tmp_path / "missing.json")


# --- junk / OOC probes ------------------------------------------------------


@pytest.mark.parametrize("junk", ["test", "asdf"])
def test_junk_probes_yield_no_delta_or_action(junk):
    result = RuleBasedExtractor()(junk, "")
    assert result.deltas == {}
    assert result.action_class == "other"


def test_junk_probe_is_prose_agnostic():
    # A neutral prose line must not make junk input invent a delta.
    neutral = "The night market is loud tonight."
    assert RuleBasedExtractor()("asdf", neutral).deltas == {}


def test_ooc_fixture_expects_empty_delta():
    cases = {case["id"]: case for case in rig.load_cases(FIXTURES_DIR)}
    assert cases["ooc_no_delta"]["expected_delta"] == {}


# --- report formatting + CLI ------------------------------------------------


def test_format_report_contains_summary_and_taxonomy():
    text = rig.format_report(_stub_report())
    assert "10 cases" in text
    assert "failure taxonomy:" in text
    assert "missed_key=" in text


def test_main_stub_run_exits_zero_and_prints_report(capsys):
    code = rig.main(["--model", "stub", "--fixtures", str(FIXTURES_DIR)])
    out = capsys.readouterr().out
    assert code == 0
    assert "failure taxonomy:" in out


def test_main_strict_exits_nonzero_on_failures(tmp_path, capsys):
    # A fixture the stub cannot satisfy guarantees a failure, independent of
    # any particular RuleBased accuracy.
    case = {
        "id": "always_fail",
        "description": "impossible for the stub",
        "user_msg": "asdf",
        "turn": "",
        "expected_delta": {"weather": "snow"},
        "expected_action_class": "other",
    }
    (tmp_path / "case.json").write_text(json.dumps(case), encoding="utf-8")
    code = rig.main(["--model", "stub", "--fixtures", str(tmp_path), "--strict"])
    capsys.readouterr()
    assert code == 1


def test_main_json_emits_parseable_report(capsys):
    code = rig.main(["--model", "stub", "--fixtures", str(FIXTURES_DIR), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    report = json.loads(out)
    assert report["total"] == 10
    assert set(report["taxonomy"]) == set(rig.FAILURE_TAXONOMY)


def test_main_list_prints_case_ids(capsys):
    code = rig.main(["--list", "--fixtures", str(FIXTURES_DIR)])
    out = capsys.readouterr().out
    assert code == 0
    listed = {line.split("\t", 1)[0] for line in out.strip().splitlines()}
    assert listed == EXPECTED_IDS


def test_main_missing_fixtures_dir_exits_two(tmp_path, capsys):
    code = rig.main(["--fixtures", str(tmp_path / "nope")])
    err = capsys.readouterr().err
    assert code == 2
    assert "error:" in err


def test_main_invalid_fixture_exits_two(tmp_path, capsys):
    (tmp_path / "bad.json").write_text('{"id": "x"}', encoding="utf-8")
    code = rig.main(["--fixtures", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "fixture error:" in err
