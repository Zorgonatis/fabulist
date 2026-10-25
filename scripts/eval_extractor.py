#!/usr/bin/env python
"""Extractor eval rig (W2.12a): labeled BTS-oracle cases vs the real extractor.

The extractor is the load-bearing bet of the out-of-band state design (PLAN D2):
the narrator emits prose + DSL only, and a post-turn agent reads ``user message +
turn prose`` to recover the state delta. This rig measures that bet against a
small hand-authored oracle whose expected values are grounded in the BTS v3.0.1
directive text conventions (``~/Projects/Stabs-EDH/Behind-the-Scenes-BTS-Directive.md``)
cross-checked against the preset's Env Tracking / Tone State vocabulary.

It loads every ``*.json`` case in a fixture directory, runs each through the real
extractor, and prints a per-case PASS/FAIL with a delta diff, a summary score, and
a failure taxonomy (missed key / wrong value / spurious key / action_class
mismatch).

Model modes
-----------
``--model stub`` (default)
    The deterministic :class:`~fabulist.state.RuleBasedExtractor` fallback path.
    No network. This is the mode the unit tests exercise.
``--model live``
    The registry-backed :class:`~fabulist.state.LLMExtractor` through the real
    connection wiring (``agents.extractor`` in the connections config). Off by
    default because it makes a network call to a served model.

Accuracy is informational at this stage: the tests assert report *structure* and
determinism, never a particular score.

Usage::

    uv run python scripts/eval_extractor.py [--model {stub,live}]
        [--fixtures DIR] [--config PATH] [--json] [--list] [--strict]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fabulist.connections import load_registry
from fabulist.state import (
    ACTION_CLASSES,
    ExtractorResult,
    LLMExtractor,
    RuleBasedExtractor,
)

__all__ = [
    "CASE_FIELD_TYPES",
    "CASE_OPTIONAL_FIELDS",
    "CASE_REQUIRED_FIELDS",
    "DEFAULT_CONFIG",
    "DEFAULT_FIXTURES_DIR",
    "FAILURE_TAXONOMY",
    "FixtureError",
    "ExtractorLike",
    "build_extractor",
    "build_report",
    "diff_delta",
    "evaluate_case",
    "format_report",
    "load_cases",
    "main",
    "validate_case",
    "validate_cases",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "bts_labeled"
DEFAULT_CONFIG = REPO_ROOT / "configs" / "connections.json"

#: Fixture schema (the hand-rolled equivalent of a JSON Schema ``required`` list).
CASE_REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "description",
    "user_msg",
    "turn",
    "expected_delta",
    "expected_action_class",
)

#: Accepted optional fields.
CASE_OPTIONAL_FIELDS: tuple[str, ...] = ("notes",)

#: Field -> expected Python type. ``_source`` is rig bookkeeping, not schema.
CASE_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "id": str,
    "description": str,
    "user_msg": str,
    "turn": str,
    "expected_delta": dict,
    "expected_action_class": str,
    "notes": str,
}

#: The failure categories reported per case and rolled up in the summary.
FAILURE_TAXONOMY: tuple[str, ...] = (
    "missed_key",
    "wrong_value",
    "spurious_key",
    "action_class_mismatch",
)

#: The extractor seam, mirroring :data:`fabulist.state.ExtractorFn`.
ExtractorLike = Callable[[str, str], ExtractorResult]


class FixtureError(ValueError):
    """A fixture file is missing, unreadable, malformed, or not a JSON object."""


# --- loading + schema validation --------------------------------------------


def load_cases(fixtures_dir: str | Path) -> list[dict[str, Any]]:
    """Load every ``*.json`` case in ``fixtures_dir`` in filename order.

    Each returned dict gains a ``_source`` key (the filename) for error messages;
    it is rig bookkeeping outside the fixture schema. Raises :class:`FixtureError`
    on a missing directory, an empty set, invalid JSON, or a non-object case.
    """
    directory = Path(fixtures_dir)
    if not directory.is_dir():
        raise FixtureError(f"fixtures dir does not exist: {directory}")

    cases: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{path.name}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise FixtureError(f"{path.name}: top-level value must be a JSON object")
        data["_source"] = path.name
        cases.append(data)

    if not cases:
        raise FixtureError(f"no *.json fixtures found in {directory}")
    return cases


def validate_case(case: Any) -> list[str]:
    """Return schema violations for one case (empty list == valid).

    Checks required fields, field types, a non-empty whitespace-free id, string
    delta keys/values, and that the action class is in the shared vocabulary.
    Unknown keys are ignored so the rig can carry bookkeeping fields.
    """
    if not isinstance(case, dict):
        return ["case must be a JSON object"]

    errors: list[str] = []
    for field in CASE_REQUIRED_FIELDS:
        if field not in case:
            errors.append(f"missing required field: {field}")

    for field, expected_type in CASE_FIELD_TYPES.items():
        if field in case and not isinstance(case[field], expected_type):
            name = getattr(expected_type, "__name__", str(expected_type))
            errors.append(f"field {field!r} must be {name}")

    case_id = case.get("id")
    if isinstance(case_id, str):
        if not case_id.strip():
            errors.append("id must be non-empty")
        elif any(ch.isspace() for ch in case_id):
            errors.append(f"id {case_id!r} must not contain whitespace")

    delta = case.get("expected_delta")
    if isinstance(delta, dict):
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in delta.items()):
            errors.append("expected_delta keys and values must be strings")

    action_class = case.get("expected_action_class")
    if isinstance(action_class, str) and action_class not in ACTION_CLASSES:
        errors.append(
            f"expected_action_class {action_class!r} is not one of {sorted(ACTION_CLASSES)}"
        )

    return errors


def validate_cases(cases: list[dict[str, Any]]) -> list[str]:
    """Validate every case and check for duplicate ids; returns all errors."""
    errors: list[str] = []
    seen: dict[str, str] = {}
    for case in cases:
        source = case.get("_source", "<case>")
        errors.extend(f"{source}: {err}" for err in validate_case(case))
        case_id = case.get("id")
        if isinstance(case_id, str):
            if case_id in seen:
                errors.append(f"{source}: duplicate id {case_id!r} (also in {seen[case_id]})")
            else:
                seen[case_id] = source
    return errors


# --- scoring ----------------------------------------------------------------


def diff_delta(expected: dict[str, str], actual: dict[str, str]) -> dict[str, Any]:
    """Categorise the difference between two delta dicts.

    Returns ``missed_keys`` (expected, absent from actual), ``spurious_keys``
    (actual, not expected), and ``wrong_values`` (present in both but unequal).
    """
    return {
        "missed_keys": [key for key in expected if key not in actual],
        "spurious_keys": [key for key in actual if key not in expected],
        "wrong_values": [
            {"key": key, "expected": expected[key], "actual": actual[key]}
            for key in expected
            if key in actual and actual[key] != expected[key]
        ],
    }


def evaluate_case(case: dict[str, Any], extractor: ExtractorLike) -> dict[str, Any]:
    """Run one case through ``extractor`` and score it against the oracle.

    A raising extractor (parser gate rejection after a failed repair, or a
    transport error) scores as an all-keys-missed case instead of crashing the
    whole rig; the failure taxonomy is unchanged (``missed_key``).
    """
    try:
        result: ExtractorResult | None = extractor(case["user_msg"], case["turn"])
    except Exception:  # noqa: BLE001 - extractor failure is a case failure
        result = None
    expected_delta = dict(case["expected_delta"])
    actual_delta = dict(result.deltas) if result is not None else {}
    expected_action = case["expected_action_class"]
    actual_action = result.action_class if result is not None else ""

    diff = diff_delta(expected_delta, actual_delta)
    action_class_match = actual_action == expected_action

    reasons: list[str] = []
    if diff["missed_keys"]:
        reasons.append("missed_key")
    if diff["wrong_values"]:
        reasons.append("wrong_value")
    if diff["spurious_keys"]:
        reasons.append("spurious_key")
    if not action_class_match:
        reasons.append("action_class_mismatch")

    return {
        "id": case["id"],
        "description": case.get("description", ""),
        "status": "pass" if not reasons else "fail",
        "reasons": reasons,
        "expected_delta": expected_delta,
        "actual_delta": actual_delta,
        "expected_action_class": expected_action,
        "actual_action_class": actual_action,
        "action_class_match": action_class_match,
        "missed_keys": diff["missed_keys"],
        "spurious_keys": diff["spurious_keys"],
        "wrong_values": diff["wrong_values"],
        "confidence": getattr(result, "confidence", None) if result is not None else None,
    }


def build_report(
    cases: list[dict[str, Any]],
    extractor: ExtractorLike,
    *,
    model_name: str,
    fixtures_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Score every case and roll up the summary + failure taxonomy."""
    case_reports = [evaluate_case(case, extractor) for case in cases]
    total = len(case_reports)
    passed = sum(1 for case in case_reports if case["status"] == "pass")

    taxonomy = {name: 0 for name in FAILURE_TAXONOMY}
    for case in case_reports:
        for reason in case["reasons"]:
            taxonomy[reason] += 1

    return {
        "model": model_name,
        "fixtures_dir": str(fixtures_dir) if fixtures_dir is not None else None,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "score": (passed / total) if total else 0.0,
        "taxonomy": taxonomy,
        "cases": case_reports,
    }


def _render_delta(delta: dict[str, str]) -> str:
    if not delta:
        return "Δ[]"
    return "Δ[" + " | ".join(f"{key}: {value}" for key, value in delta.items()) + "]"


def format_report(report: dict[str, Any]) -> str:
    """Render a report dict as the human-readable per-case diff + summary."""
    lines: list[str] = [f"Fabulist extractor eval — model={report['model']}"]
    if report.get("fixtures_dir"):
        lines.append(f"fixtures: {report['fixtures_dir']}")
    lines.append(
        f"{report['total']} cases: {report['passed']} pass, {report['failed']} fail "
        f"(score {report['score'] * 100:.1f}%)"
    )
    lines.append("-" * 72)

    for case in report["cases"]:
        mark = "PASS" if case["status"] == "pass" else "FAIL"
        action_mark = "ok" if case["action_class_match"] else "!!"
        lines.append(
            f"[{mark}] {case['id']:<24} action: {case['actual_action_class']} {action_mark} "
            f"(expected {case['expected_action_class']})"
        )
        if case["status"] == "fail":
            lines.append(f"       expected {_render_delta(case['expected_delta'])}")
            lines.append(f"       actual   {_render_delta(case['actual_delta'])}")
            for key in case["missed_keys"]:
                lines.append(
                    f"       ! missed key: {key} (expected {case['expected_delta'][key]!r})"
                )
            for key in case["spurious_keys"]:
                lines.append(
                    f"       ! spurious key: {key} (actual {case['actual_delta'][key]!r})"
                )
            for wrong in case["wrong_values"]:
                lines.append(
                    f"       ! wrong value: {wrong['key']} "
                    f"expected {wrong['expected']!r} got {wrong['actual']!r}"
                )
            if not case["action_class_match"]:
                lines.append(
                    f"       ! action_class mismatch: expected "
                    f"{case['expected_action_class']!r} got {case['actual_action_class']!r}"
                )

    lines.append("-" * 72)
    taxonomy = report["taxonomy"]
    lines.append(
        "failure taxonomy: "
        + ", ".join(f"{name}={taxonomy[name]}" for name in FAILURE_TAXONOMY)
    )
    return "\n".join(lines)


# --- extractor construction + CLI -------------------------------------------


def build_extractor(
    model: str, *, config_path: str | Path = DEFAULT_CONFIG
) -> ExtractorLike:
    """Build the extractor for ``model``: ``stub`` (no network) or ``live``."""
    if model == "stub":
        return RuleBasedExtractor()
    if model == "live":
        path = Path(config_path)
        if not path.is_file():
            raise FixtureError(f"live mode needs a connections config at {path}")
        registry = load_registry(path)
        return LLMExtractor(registry=registry)
    raise ValueError(f"unknown model {model!r} (choose one of: stub, live)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model",
        choices=("stub", "live"),
        default="stub",
        help="stub = RuleBased fallback, no network (default); live = registry extractor",
    )
    parser.add_argument(
        "--fixtures",
        default=str(DEFAULT_FIXTURES_DIR),
        help="fixture directory (default: %(default)s)",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="connections config for --model live (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--list", action="store_true", help="list fixture ids and exit")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any case fails (accuracy is informational otherwise)",
    )
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.fixtures)
    except FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    errors = validate_cases(cases)
    if errors:
        for error in errors:
            print(f"fixture error: {error}", file=sys.stderr)
        return 2

    if args.list:
        for case in cases:
            print(f"{case['id']}\t{case['description']}")
        return 0

    try:
        extractor = build_extractor(args.model, config_path=args.config)
    except FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # unknown --model, defence in depth beyond argparse choices
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = build_report(
        cases, extractor, model_name=args.model, fixtures_dir=args.fixtures
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report))
    return 1 if (args.strict and report["failed"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
