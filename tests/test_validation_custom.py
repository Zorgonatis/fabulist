"""Gate-level tests for the ``Custom`` raw-HTML exemption + paper-scrap ladder (D60).

Hermetic: a temp ``library.spec.json`` (via ``FABULIST_DSL_SPEC``) carries the
``Custom`` and ``Document`` entries, so these tests pass before the integration
regen lands the real spec. They pin:

* the exemption path (a valid Custom carrying markup passes untouched);
* the non-Custom regression (a smuggled tag is still flagged and kept verbatim);
* the failure ladder (policy violation → flag/retry → deterministic scrap);
* the ``Document("scrap", …)`` rewrite byte-exactly.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.dsl import spec as dsl_spec
from fabulist.graph.validation import (
    dsl_error_flags,
    reset_component_specs_cache,
    revalidate_with_retry,
    validate_dsl,
)

#: The generated-spec-shaped entries these tests need. ``Custom`` is the escape
#: hatch under test; ``Document`` is the paper-scrap degradation sink. The core
#: four (ScenePane/MeterBar/ObjectCard/BeatCard) are always injected.
COMPONENTS = {
    "Document": {
        "props": [
            {"name": "docType", "type": "string", "required": True},
            {"name": "title", "type": "string", "required": True},
            {"name": "body", "type": "string", "required": True},
            {"name": "signature", "type": "string", "required": False},
            {"name": "options", "type": "object", "required": False},
        ]
    },
    "Custom": {
        "props": [
            {"name": "title", "type": "string", "required": True},
            {"name": "html", "type": "string", "required": True},
            {"name": "css", "type": "string", "required": False},
            {"name": "options", "type": "object", "required": False},
        ]
    },
}


@pytest.fixture(autouse=True)
def _spec(monkeypatch, tmp_path):
    """Point the gate at a hermetic spec carrying ``Custom`` + ``Document``."""
    path = tmp_path / "library.spec.json"
    path.write_text(json.dumps({"components": COMPONENTS}), encoding="utf-8")
    monkeypatch.setenv(dsl_spec.SPEC_ENV_VAR, str(path))
    monkeypatch.setenv(dsl_spec.FLAVORS_ENV_VAR, str(tmp_path / "absent-flavors.json"))
    reset_component_specs_cache()
    yield
    reset_component_specs_cache()


def run(coro):
    return asyncio.run(coro)


def dsl(text: str) -> dict:
    return {"kind": "dsl", "text": text}


def prose(text: str) -> dict:
    return {"kind": "prose", "text": text}


def codes(report) -> list[str]:
    return [e.code for e in report.errors]


# ---------------------------------------------------------------------------
# exemption path
# ---------------------------------------------------------------------------


def test_valid_custom_markup_is_exempt_from_the_html_scan():
    segment = dsl(
        "panel = Custom(\"Status\", \"<div class='x'><h2>OK</h2></div>\", 'div {}')"
    )
    report = validate_dsl([segment])
    assert report.ok is True, report.errors
    assert report.errors == []
    assert report.segments == [segment]


def test_exactly_at_caps_passes_through_the_gate():
    segment = dsl(f'panel = Custom("P", "{"a" * 4000}", "")')
    report = validate_dsl([segment])
    assert report.ok is True, report.errors
    assert report.segments == [segment]


def test_html_cap_violation_is_flagged_at_the_gate():
    segment = dsl(f'panel = Custom("P", "{"a" * 4001}", "")')
    report = validate_dsl([segment])
    assert codes(report) == ["html_cap"]


# ---------------------------------------------------------------------------
# violation → flag + scaffold (ladder step 1)
# ---------------------------------------------------------------------------


def test_custom_remote_uri_rewrites_to_document_scrap_byte_exact():
    segment = dsl("""panel = Custom('Status', '<img src="https://evil/x.png">', 'div{}')""")
    report = validate_dsl([segment])

    assert report.ok is False
    assert codes(report) == ["remote_uri"]
    assert report.segments == [
        dsl(
            'panel = Document("scrap", "Status", '
            '"<img src=\\"https://evil/x.png\\">\\ndiv{}", "unrendered custom panel")'
        )
    ]


def test_banned_tag_violation_rewrites_to_document_scrap():
    segment = dsl("""panel = Custom('P', '<script>alert(1)</script>', '')""")
    report = validate_dsl([segment])

    # The generic HTML scan must NOT fire for Custom: this is a policy finding.
    assert codes(report) == ["banned_tag"]
    assert report.segments == [
        dsl(
            'panel = Document("scrap", "P", '
            '"<script>alert(1)</script>", "unrendered custom panel")'
        )
    ]


def test_custom_scrap_keeps_identifier_so_scenepane_ref_survives():
    segment = dsl(
        "panel = Custom('P', '<img src=\"http://x\">', '')\n"
        "root = ScenePane('T', [panel])"
    )
    report = validate_dsl([segment])

    assert codes(report) == ["remote_uri"]
    text = report.segments[0]["text"]
    assert text.startswith('panel = Document("scrap", "P", "<img src=\\"http://x\\">", ')
    assert 'root = ScenePane(\'T\', [panel])' in text


def test_custom_kwargs_are_positionalized_then_policy_checked():
    segment = dsl("""panel = Custom(title='P', html='<iframe src="x"></iframe>', css='')""")
    report = validate_dsl([segment])

    found = set(codes(report))
    assert found == {"named_args", "banned_tag"}
    # The kwargs repair is advisory (repaired), the policy finding is not.
    advisories = [e for e in report.errors if e.repaired]
    assert [e.code for e in advisories] == ["named_args"]


def test_dsl_error_flags_map_the_policy_class_to_a_warn_flag():
    segment = dsl("""panel = Custom('P', '<img src="https://evil/x">', '')""")
    report = validate_dsl([segment])
    flags = dsl_error_flags(report.errors)
    assert flags[0]["rule_id"] == "remote_uri"
    assert flags[0]["severity"] == "warn"
    assert flags[0]["check"] == "dsl_validation"


# ---------------------------------------------------------------------------
# non-Custom regression (the scan is unchanged for every other statement)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'hook = BeatCard("hello <b>bold</b>")',
        'hook = BeatCard("<img src=x onerror=alert(1)>")',
        'root = ScenePane("<script>alert(1)</script>", [])',
    ],
)
def test_non_custom_html_is_kept_and_flagged(text):
    report = validate_dsl([dsl(text)])
    assert report.ok is False
    assert codes(report) == ["html_content"]
    # Keep + tag: the smuggling statement stays in the payload (the frontend's
    # own raw-fallback renders and the anchored flag explains it).
    assert report.segments == [dsl(text)]


def test_mixed_segment_still_scans_the_non_custom_statements():
    segment = dsl(
        "panel = Custom(\"P\", \"<div>x</div>\", \"\")\n"
        'hook = BeatCard("<b>bold</b>")'
    )
    report = validate_dsl([segment])
    assert codes(report) == ["html_content"]
    assert report.segments == [segment]


def test_balance_check_still_runs_on_the_whole_segment():
    # An extra ``)`` after a Custom call: balance (not the HTML scan) owns this.
    text = 'panel = Custom("P", "<div>x"))'
    report = validate_dsl([dsl(text)])
    assert codes(report) == ["unbalanced_parens"]
    assert report.segments == [dsl(text)]


# ---------------------------------------------------------------------------
# retry ladder (step 2 → terminal scrap)
# ---------------------------------------------------------------------------


def _bad_custom() -> list[dict]:
    return [dsl("""panel = Custom('P', '<img src="https://e/x">', '')""")]


def test_retry_recovers_the_custom_policy_violation():
    fixed = [dsl("""panel = Custom('P', '<div>safe</div>', 'div{}')""")]

    async def resynthesize(errors):  # noqa: ARG001 - fake ignores the errors
        return fixed

    report = run(revalidate_with_retry(_bad_custom(), resynthesize))
    assert report.retry_applied is True
    assert report.ok is True
    assert report.segments == fixed


def test_retry_still_violating_falls_back_to_the_terminal_scrap():
    async def resynthesize(errors):  # noqa: ARG001 - fake re-emits the same bad call
        return _bad_custom()

    report = run(revalidate_with_retry(_bad_custom(), resynthesize))
    assert report.retry_applied is True
    assert report.ok is False
    assert codes(report) == ["remote_uri"]
    assert report.segments == [
        dsl(
            'panel = Document("scrap", "P", '
            '"<img src=\\"https://e/x\\">", "unrendered custom panel")'
        )
    ]
