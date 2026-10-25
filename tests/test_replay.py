"""Tests for the extractor replay rig (W3.5).

The rig is fully offline and dependency-free: every test injects an extract callable.
Per the lane rule ("prefer the real one read-only"), the production pipe-YAML parser
:func:`fabulist.state.extractor.parse_delta` is used as that callable wherever a real
delta parse is wanted; synthetic cases live in ``tmp_path`` and the source fixtures are
never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabulist.eval import replay as replay_mod
from fabulist.eval.replay import (
    CaseResult,
    ReplayError,
    ReplayReport,
    discover_cases,
    load_case,
    main,
    render_report,
    replay_extract,
    report_to_dict,
    validate_case,
)
from fabulist.state.extractor import parse_delta

#: The injected extract seam used by most tests: the real read-only pipe-YAML parser.
extract = parse_delta


def write_case(directory: Path, case_id: str, text: str, expect: dict[str, str]) -> Path:
    """Write one replay case JSON file and return its path."""
    path = Path(directory) / f"{case_id}.json"
    path.write_text(
        json.dumps({"id": case_id, "text": text, "expect": expect}), encoding="utf-8"
    )
    return path


def _raises(message: str):
    def _boom(_text: str) -> dict[str, str]:
        raise RuntimeError(message)

    return _boom


# --- the seam is the real parser --------------------------------------------


def test_default_extract_is_the_real_parse_delta():
    assert replay_mod.parse_delta is parse_delta


def test_duplicate_key_merge_replays_cleanly(tmp_path):
    # The live-observed two-entry inventory line merges in parse_delta; the rig must
    # score that merged value as a match, not a mismatch.
    path = write_case(tmp_path, "inv", "Δ[inv: +lantern | inv: -torch]", {"inv": "+lantern -torch"})
    report = replay_extract([path], extract)
    assert report.passes() is True
    assert report.key_matches == 1


# --- passing / failing sets -------------------------------------------------


def test_replay_passing_set_passes(tmp_path):
    paths = [
        write_case(
            tmp_path,
            "scene_open",
            "Prose. Δ[time: 14:35 | loc: night market]",
            {"time": "14:35", "loc": "night market"},
        ),
        write_case(tmp_path, "weather_only", "Rain. Δ[weather: light rain]", {"weather": "light rain"}),
        write_case(tmp_path, "no_change", "Nothing happens. Δ[]", {}),
    ]
    report = replay_extract(paths, extract)
    assert report.cases == 3
    assert report.key_matches == 3
    assert report.key_mismatches == 0
    assert (report.missing, report.unexpected, report.value_mismatches) == ([], [], [])
    assert report.passes() is True
    assert all(result.passed for result in report.results)


def test_missing_key_is_flagged(tmp_path):
    path = write_case(tmp_path, "missing", "Δ[loc: docks]", {"loc": "docks", "weather": "rain"})
    report = replay_extract([path], extract)
    assert report.missing == [("missing", "weather")]
    assert report.unexpected == []
    assert report.value_mismatches == []
    assert report.key_matches == 1
    assert report.key_mismatches == 1
    assert report.passes() is False


def test_extra_key_is_flagged(tmp_path):
    path = write_case(tmp_path, "extra", "Δ[loc: docks | weather: rain]", {"loc": "docks"})
    report = replay_extract([path], extract)
    assert report.unexpected == [("extra", "weather")]
    assert report.missing == []
    assert report.key_matches == 1
    assert report.key_mismatches == 1
    assert report.passes() is False


def test_value_mismatch_is_flagged(tmp_path):
    path = write_case(tmp_path, "wrong", "Δ[time: 17:20]", {"time": "14:35"})
    report = replay_extract([path], extract)
    assert report.value_mismatches == [("wrong", "time", "14:35", "17:20")]
    assert report.missing == []
    assert report.unexpected == []
    assert report.key_matches == 0
    assert report.key_mismatches == 1
    assert report.passes() is False


def test_key_counts_combine_all_three_failure_kinds(tmp_path):
    # expect time/loc/weather; got time=14:00 (match), loc=9 (wrong), tone (unexpected);
    # weather absent (missing). Fold-vocabulary keys only: parse_delta now gates the
    # extractor's documented vocabulary (W5A-1), so the rig's synthetic scoring case
    # must use real fold keys.
    path = write_case(
        tmp_path, "mixed", "Δ[time: 14:00 | loc: 9 | tone: calm]",
        {"time": "14:00", "loc": "docks", "weather": "rain"},
    )
    report = replay_extract([path], extract)
    assert report.key_matches == 1
    assert report.key_mismatches == 3
    assert report.missing == [("mixed", "weather")]
    assert report.unexpected == [("mixed", "tone")]
    assert report.value_mismatches == [("mixed", "loc", "docks", "9")]
    assert report.passes() is False


def test_empty_case_paths_yield_empty_report():
    report = replay_extract([], extract)
    assert report.cases == 0
    assert report.key_matches == 0
    assert report.key_mismatches == 0
    assert report.passes() is True
    assert "0 cases" in render_report(report)


# --- extractor failures are contained ---------------------------------------


def test_extract_exception_is_recorded_and_fails(tmp_path):
    path = write_case(tmp_path, "boom", "Δ[loc: docks]", {"loc": "docks"})
    report = replay_extract([path], _raises("kaboom"))
    assert report.passes() is False
    assert report.results[0].error is not None
    assert "RuntimeError" in report.results[0].error
    assert "kaboom" in report.results[0].error
    assert report.missing == [("boom", "loc")]


def test_non_dict_extract_return_is_an_error(tmp_path):
    path = write_case(tmp_path, "weird", "Δ[]", {})
    report = replay_extract([path], lambda _text: ["not", "a", "dict"])  # type: ignore[arg-type,return-value]
    assert report.passes() is False
    assert "expected dict" in (report.results[0].error or "")


def test_error_with_empty_expect_still_fails(tmp_path):
    path = write_case(tmp_path, "boom0", "Δ[]", {})
    report = replay_extract([path], _raises("nope"))
    assert (report.missing, report.unexpected, report.value_mismatches) == ([], [], [])
    assert report.passes() is False


# --- report shape -----------------------------------------------------------


def test_report_lists_have_expected_tuple_arity(tmp_path):
    p1 = write_case(tmp_path, "m", "Δ[]", {"gone": "x"})
    p2 = write_case(tmp_path, "v", "Δ[time: 17:20]", {"time": "14:35"})
    p3 = write_case(tmp_path, "e", "Δ[extra: y]", {})
    report = replay_extract([p1, p2, p3], extract)
    assert all(len(pair) == 2 for pair in report.missing + report.unexpected)
    assert all(len(quad) == 4 for quad in report.value_mismatches)


def test_replay_report_dataclass_surface_and_passes_method():
    clean = ReplayReport(
        cases=0, key_matches=0, key_mismatches=0, missing=[], unexpected=[], value_mismatches=[]
    )
    assert clean.passes() is True
    dirty = ReplayReport(
        cases=1,
        key_matches=0,
        key_mismatches=1,
        missing=[("a", "b")],
        unexpected=[],
        value_mismatches=[],
    )
    assert dirty.passes() is False


def test_case_result_passed_property():
    clean = CaseResult(
        id="ok", text="Δ[]", expected={}, got={}, missing=[], unexpected=[], value_mismatches=[]
    )
    assert clean.passed is True
    errored = CaseResult(
        id="ok", text="Δ[]", expected={}, got={}, missing=[], unexpected=[], value_mismatches=[], error="X: y"
    )
    assert errored.passed is False


def test_report_to_dict_shape(tmp_path):
    path = write_case(
        tmp_path, "bad", "Δ[time: 17:20 | weather: rain]", {"time": "14:35", "loc": "docks"}
    )
    report = replay_extract([path], extract)
    data = report_to_dict(report)

    assert set(data) == {
        "cases",
        "passed",
        "failed",
        "passes",
        "key_matches",
        "key_mismatches",
        "missing",
        "unexpected",
        "value_mismatches",
        "results",
    }
    assert data["cases"] == 1
    assert data["passed"] == 0
    assert data["failed"] == 1
    assert data["passes"] is False
    assert data["missing"] == [["bad", "loc"]]
    assert data["unexpected"] == [["bad", "weather"]]
    assert data["value_mismatches"] == [["bad", "time", "14:35", "17:20"]]

    case = data["results"][0]
    assert set(case) == {
        "id",
        "text",
        "status",
        "expected",
        "got",
        "missing",
        "unexpected",
        "value_mismatches",
        "error",
    }
    assert case["status"] == "fail"
    assert case["expected"] == {"time": "14:35", "loc": "docks"}
    assert case["got"] == {"time": "17:20", "weather": "rain"}
    assert case["missing"] == ["loc"]
    assert case["unexpected"] == ["weather"]
    assert case["value_mismatches"] == [["time", "14:35", "17:20"]]
    assert case["error"] is None
    json.dumps(data)  # must be JSON-serialisable


# --- rendering --------------------------------------------------------------


def test_render_report_contains_fail_detail(tmp_path):
    ok = write_case(tmp_path, "ok", "Δ[loc: docks]", {"loc": "docks"})
    bad = write_case(
        tmp_path, "bad", "Δ[loc: docks | weather: rain]", {"loc": "docks", "weather": "downpour"}
    )
    report = replay_extract([ok, bad], extract)
    text = render_report(report)

    assert "[PASS] ok" in text
    assert "[FAIL] bad" in text
    assert "1 pass, 1 fail" in text
    assert "! missing key" not in text
    assert "! unexpected key" not in text
    assert "! wrong value: weather expected 'downpour' got 'rain'" in text
    assert "RESULT: FAIL" in text


def test_render_report_shows_missing_and_unexpected(tmp_path):
    path = write_case(tmp_path, "mixed", "Δ[loc: docks | tone: y]", {"loc": "docks", "gone": "x"})
    text = render_report(replay_extract([path], extract))
    assert "! missing key: gone (expected 'x')" in text
    assert "! unexpected key: tone (got 'y')" in text


def test_render_report_shows_extract_error(tmp_path):
    path = write_case(tmp_path, "boom", "Δ[]", {})
    text = render_report(replay_extract([path], _raises("kaboom")))
    assert "! extract error:" in text
    assert "kaboom" in text


def test_render_report_passing_is_stable_and_labeled(tmp_path):
    path = write_case(tmp_path, "ok", "Δ[loc: docks]", {"loc": "docks"})
    report = replay_extract([path], extract)
    text = render_report(report)
    assert "[PASS] ok" in text
    assert "RESULT: PASS" in text
    assert render_report(report) == text


def test_render_report_is_stable_across_runs(tmp_path):
    paths = [
        write_case(tmp_path, "a", "Δ[loc: docks]", {"loc": "docks"}),
        write_case(tmp_path, "b", "Δ[time: 17:20]", {"time": "14:35", "loc": "docks"}),
    ]
    first = render_report(replay_extract(paths, extract))
    second = render_report(replay_extract(paths, extract))
    assert first == second


def test_render_report_preserves_input_order(tmp_path):
    paths = [
        write_case(tmp_path, "zeta", "Δ[]", {}),
        write_case(tmp_path, "alpha", "Δ[]", {}),
    ]
    text = render_report(replay_extract(paths, extract))
    assert text.index("[PASS] zeta") < text.index("[PASS] alpha")


# --- CLI --------------------------------------------------------------------


def test_main_pass_exits_zero_and_prints_report(tmp_path, capsys):
    write_case(tmp_path, "ok", "Δ[loc: docks]", {"loc": "docks"})
    code = main(["--cases", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "RESULT: PASS" in out


def test_main_fail_exits_one(tmp_path, capsys):
    write_case(tmp_path, "bad", "Δ[loc: docks]", {"loc": "market"})
    code = main(["--cases", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "RESULT: FAIL" in out


def test_main_json_writes_report_and_prints_text(tmp_path, capsys):
    write_case(tmp_path, "ok", "Δ[time: 14:35]", {"time": "14:35"})
    out_path = tmp_path / "report.json"
    code = main(["--cases", str(tmp_path), "--json", str(out_path)])
    text = capsys.readouterr().out
    assert code == 0
    assert "RESULT: PASS" in text
    assert out_path.is_file()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["cases"] == 1
    assert data["passes"] is True
    assert data["results"][0]["id"] == "ok"


def test_main_fail_json_is_still_written(tmp_path):
    write_case(tmp_path, "bad", "Δ[loc: docks]", {"loc": "market"})
    out_path = tmp_path / "report.json"
    code = main(["--cases", str(tmp_path), "--json", str(out_path)])
    assert code == 1
    assert json.loads(out_path.read_text(encoding="utf-8"))["passes"] is False


def test_main_scans_directory_in_filename_order(tmp_path):
    write_case(tmp_path, "b_second", "Δ[]", {})
    write_case(tmp_path, "a_first", "Δ[]", {})
    out_path = tmp_path / "report.json"
    main(["--cases", str(tmp_path), "--json", str(out_path)])
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert [result["id"] for result in data["results"]] == ["a_first", "b_second"]


def test_main_missing_dir_exits_two(tmp_path, capsys):
    code = main(["--cases", str(tmp_path / "nope")])
    err = capsys.readouterr().err
    assert code == 2
    assert "error:" in err


def test_main_empty_dir_exits_two(tmp_path, capsys):
    (tmp_path / "note.txt").write_text("ignored", encoding="utf-8")
    code = main(["--cases", str(tmp_path)])
    assert code == 2


# --- authoring / schema edge cases ------------------------------------------


def test_discover_cases_returns_sorted_json_paths(tmp_path):
    write_case(tmp_path, "a", "Δ[]", {})
    (tmp_path / "z.json").write_text(json.dumps({"id": "z", "text": "Δ[]", "expect": {}}), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    assert [p.name for p in discover_cases(tmp_path)] == ["a.json", "z.json"]


def test_discover_cases_missing_dir_raises(tmp_path):
    with pytest.raises(ReplayError):
        discover_cases(tmp_path / "nope")


def test_discover_cases_empty_dir_raises(tmp_path):
    with pytest.raises(ReplayError):
        discover_cases(tmp_path)


def test_load_case_invalid_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ReplayError):
        load_case(path)


def test_load_case_non_object_raises(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ReplayError):
        load_case(path)


def test_load_case_missing_file_raises(tmp_path):
    with pytest.raises(ReplayError):
        load_case(tmp_path / "absent.json")


def test_validate_case_requires_all_fields():
    errors = validate_case({"id": "x"})
    assert any("missing required field: text" in e for e in errors)
    assert any("missing required field: expect" in e for e in errors)


def test_validate_case_rejects_wrong_types():
    errors = validate_case({"id": 1, "text": [], "expect": []})
    assert any("'id' must be str" in e for e in errors)
    assert any("'text' must be str" in e for e in errors)
    assert any("'expect' must be dict" in e for e in errors)


def test_validate_case_rejects_non_string_expect_value():
    errors = validate_case({"id": "x", "text": "Δ[]", "expect": {"time": 1435}})
    assert any("must be a string" in e for e in errors)


def test_validate_case_accepts_valid_case():
    assert validate_case({"id": "x", "text": "Δ[]", "expect": {}}) == []


def test_replay_extract_raises_on_malformed_case(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"id": "x"}), encoding="utf-8")
    with pytest.raises(ReplayError):
        replay_extract([path], extract)
