"""Extractor replay rig (W3.5): replay labeled turns through an injected extract callable.

The post-turn extractor (``src/fabulist/state/extractor.py``) turns one turn into a
pipe-YAML state delta (``Δ[time: 14:35 | loc: market | inv: +lantern]``). The W2.12a rig
(``scripts/eval_extractor.py``) measures that seam live against an oracle; this rig is the
offline counterpart: the extract callable is *injected*, so a recorded turn can be replayed
deterministically through any seam (the real :func:`fabulist.state.parse_delta`, the
RuleBased fallback, or a test stub) with no model call.

Each case is one JSON file::

    {"id": "scene_open", "text": "<narrative with a Δ[...] line>", "expect": {"time": "14:35"}}

A case is scored at KEY level: expected keys missing from the extractor output, keys the
extractor invented that were not expected, and keys present on both sides whose values
differ. ``ReplayReport.passes()`` is true only when every case is clean.

Usage::

    uv run python -m fabulist.eval.replay --cases tests/fixtures/replay --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..state.extractor import parse_delta

__all__ = [
    "CaseResult",
    "ExtractFn",
    "ReplayError",
    "ReplayReport",
    "discover_cases",
    "load_case",
    "main",
    "render_report",
    "replay_extract",
    "report_to_dict",
    "validate_case",
]

#: The injected extractor seam: turn text -> ``{key: value}`` state delta.
ExtractFn = Callable[[str], dict[str, str]]

#: Required fields of a replay case (``description`` and friends are ignored).
CASE_REQUIRED_FIELDS: tuple[str, ...] = ("id", "text", "expect")


class ReplayError(ValueError):
    """A replay case file is missing, unreadable, malformed, or fails schema checks."""


@dataclass(frozen=True)
class CaseResult:
    """One replayed case: expected vs actual delta and the key-level diff."""

    id: str
    text: str
    expected: dict[str, str]
    got: dict[str, str]
    missing: list[str]
    unexpected: list[str]
    value_mismatches: list[tuple[str, str, str]]
    error: str | None = None

    @property
    def passed(self) -> bool:
        """True when the case produced no error and no key-level difference."""
        return not (self.error or self.missing or self.unexpected or self.value_mismatches)


@dataclass
class ReplayReport:
    """Aggregate result of a replay run.

    ``key_matches`` counts expected keys that were present and equal; ``key_mismatches``
    counts every problem key in both directions (``missing`` + ``unexpected`` +
    ``value_mismatches``). ``missing``/``unexpected`` entries are ``(case_id, key)``;
    ``value_mismatches`` entries are ``(case_id, key, expected_value, got_value)``.
    """

    cases: int
    key_matches: int
    key_mismatches: int
    missing: list[tuple[str, str]]
    unexpected: list[tuple[str, str]]
    value_mismatches: list[tuple[str, str, str, str]]
    results: list[CaseResult] = field(default_factory=list)

    def passes(self) -> bool:
        """True when every replayed case passed (no errors and no key differences)."""
        if any(result.error for result in self.results):
            return False
        return not (self.missing or self.unexpected or self.value_mismatches)


# --- case loading + schema validation ---------------------------------------


def validate_case(case: Any) -> list[str]:
    """Return schema violations for one case (empty list == valid)."""
    if not isinstance(case, dict):
        return ["case must be a JSON object"]

    errors: list[str] = []
    for required in CASE_REQUIRED_FIELDS:
        if required not in case:
            errors.append(f"missing required field: {required}")

    if "id" in case:
        if not isinstance(case["id"], str):
            errors.append("field 'id' must be str")
        elif not case["id"].strip():
            errors.append("id must be non-empty")

    if "text" in case and not isinstance(case["text"], str):
        errors.append("field 'text' must be str")

    if "expect" in case and not isinstance(case["expect"], dict):
        errors.append("field 'expect' must be dict")
    elif isinstance(case.get("expect"), dict):
        for key, value in case["expect"].items():
            if not isinstance(key, str):
                errors.append("expect keys must be strings")
            if not isinstance(value, str):
                errors.append(f"expect value for {key!r} must be a string")

    return errors


def load_case(path: str | Path) -> dict[str, Any]:
    """Load and validate one case file; raise :class:`ReplayError` on any problem."""
    case_path = Path(path)
    try:
        raw = case_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReplayError(f"{case_path.name}: case file does not exist") from exc
    except OSError as exc:
        raise ReplayError(f"{case_path.name}: cannot read case: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{case_path.name}: invalid JSON: {exc}") from exc

    errors = validate_case(data)
    if errors:
        raise ReplayError(f"{case_path.name}: " + "; ".join(errors))
    return data


def discover_cases(directory: str | Path) -> list[Path]:
    """Return every ``*.json`` case path in ``directory`` in filename order.

    Raises :class:`ReplayError` when the directory is missing or holds no cases.
    """
    cases_dir = Path(directory)
    if not cases_dir.is_dir():
        raise ReplayError(f"cases dir does not exist: {cases_dir}")

    paths = sorted(cases_dir.glob("*.json"))
    if not paths:
        raise ReplayError(f"no *.json cases found in {cases_dir}")
    return paths


# --- replay + scoring -------------------------------------------------------


def _diff(expected: dict[str, str], got: dict[str, str]) -> tuple[list[str], list[str], list[tuple[str, str, str]]]:
    missing = sorted(key for key in expected if key not in got)
    unexpected = sorted(key for key in got if key not in expected)
    value_mismatches = sorted(
        (key, expected[key], got[key])
        for key in expected
        if key in got and got[key] != expected[key]
    )
    return missing, unexpected, value_mismatches


def replay_extract(case_paths: Iterable[Path], extract: ExtractFn) -> ReplayReport:
    """Replay every case through ``extract`` and score each at key level.

    ``extract`` receives the case ``text`` and must return a state-delta dict. A call
    that raises (or returns a non-dict) is recorded on the case as an ``error`` and the
    case fails; it never aborts the run.
    """
    results: list[CaseResult] = []
    for path in case_paths:
        case = load_case(path)
        expected = dict(case["expect"])

        got: dict[str, str] = {}
        error: str | None = None
        try:
            raw = extract(case["text"])
            if not isinstance(raw, dict):
                raise TypeError(f"extract returned {type(raw).__name__}, expected dict")
            got = dict(raw)
        except Exception as exc:  # noqa: BLE001 - any extractor failure is a case failure
            error = f"{type(exc).__name__}: {exc}"
            got = {}

        missing, unexpected, value_mismatches = _diff(expected, got)
        results.append(
            CaseResult(
                id=case["id"],
                text=case["text"],
                expected=expected,
                got=got,
                missing=missing,
                unexpected=unexpected,
                value_mismatches=value_mismatches,
                error=error,
            )
        )

    key_matches = sum(
        len(result.expected) - len(result.missing) - len(result.value_mismatches)
        for result in results
    )
    missing_pairs = [(result.id, key) for result in results for key in result.missing]
    unexpected_pairs = [(result.id, key) for result in results for key in result.unexpected]
    value_quads = [
        (result.id, key, expected, got)
        for result in results
        for key, expected, got in result.value_mismatches
    ]

    return ReplayReport(
        cases=len(results),
        key_matches=key_matches,
        key_mismatches=len(missing_pairs) + len(unexpected_pairs) + len(value_quads),
        missing=missing_pairs,
        unexpected=unexpected_pairs,
        value_mismatches=value_quads,
        results=results,
    )


# --- rendering --------------------------------------------------------------


def _render_delta(delta: dict[str, str]) -> str:
    if not delta:
        return "Δ[]"
    return "Δ[" + " | ".join(f"{key}: {value}" for key, value in delta.items()) + "]"


def render_report(report: ReplayReport) -> str:
    """Render a report as diffable text: a summary, one section per case, totals.

    Sections keep input order; keys inside each section are sorted, so two runs over
    the same cases render byte-for-byte identically.
    """
    passed = sum(1 for result in report.results if result.passed)
    failed = report.cases - passed

    lines: list[str] = [
        f"Fabulist extractor replay — {report.cases} cases: {passed} pass, {failed} fail",
        "=" * 72,
    ]
    for result in report.results:
        lines.append(f"[{'PASS' if result.passed else 'FAIL'}] {result.id}")
        if result.passed:
            continue
        lines.append(f"       text:     {result.text!r}")
        lines.append(f"       expected: {_render_delta(result.expected)}")
        lines.append(f"       got:      {_render_delta(result.got)}")
        for key in result.missing:
            lines.append(f"       ! missing key: {key} (expected {result.expected[key]!r})")
        for key in result.unexpected:
            lines.append(f"       ! unexpected key: {key} (got {result.got[key]!r})")
        for key, expected, got in result.value_mismatches:
            lines.append(f"       ! wrong value: {key} expected {expected!r} got {got!r}")
        if result.error:
            lines.append(f"       ! extract error: {result.error}")

    lines.append("=" * 72)
    lines.append(
        f"keys: {report.key_matches} matched, {report.key_mismatches} mismatched"
    )
    lines.append(
        f"missing={len(report.missing)} unexpected={len(report.unexpected)} "
        f"value_mismatches={len(report.value_mismatches)}"
    )
    lines.append("RESULT: " + ("PASS" if report.passes() else "FAIL"))
    return "\n".join(lines)


def report_to_dict(report: ReplayReport) -> dict[str, Any]:
    """Serialise a report to a JSON-friendly dict (tuples become arrays)."""
    return {
        "cases": report.cases,
        "passed": sum(1 for result in report.results if result.passed),
        "failed": sum(1 for result in report.results if not result.passed),
        "passes": report.passes(),
        "key_matches": report.key_matches,
        "key_mismatches": report.key_mismatches,
        "missing": [list(pair) for pair in report.missing],
        "unexpected": [list(pair) for pair in report.unexpected],
        "value_mismatches": [list(quad) for quad in report.value_mismatches],
        "results": [
            {
                "id": result.id,
                "text": result.text,
                "status": "pass" if result.passed else "fail",
                "expected": result.expected,
                "got": result.got,
                "missing": list(result.missing),
                "unexpected": list(result.unexpected),
                "value_mismatches": [list(quad) for quad in result.value_mismatches],
                "error": result.error,
            }
            for result in report.results
        ],
    }


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the replay rig: ``--cases DIR`` replayed through the real ``parse_delta``.

    Prints the text report to stdout; when ``--json PATH`` is given, also writes the
    JSON report there. Exits 1 when any case fails, 2 on a load/usage error.
    """
    parser = argparse.ArgumentParser(
        prog="fabulist.eval.replay",
        description="Replay labeled extractor cases and report key-level diffs.",
    )
    parser.add_argument("--cases", required=True, help="directory of *.json replay cases")
    parser.add_argument("--json", dest="json_path", default=None, help="write the JSON report here")
    args = parser.parse_args(argv)

    try:
        paths = discover_cases(args.cases)
        report = replay_extract(paths, parse_delta)
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(render_report(report))

    if args.json_path:
        out_path = Path(args.json_path)
        out_path.write_text(
            json.dumps(report_to_dict(report), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    return 0 if report.passes() else 1


if __name__ == "__main__":
    raise SystemExit(main())
