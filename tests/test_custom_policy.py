"""Unit tests for :mod:`fabulist.dsl.custom_policy` (D60 raw-HTML escape hatch).

Pure, hermetic, no spec files: ``validate_custom`` takes the decoded ``html`` and
``css`` strings the browser would receive and returns the policy violations. Every
violation class is covered, plus the clean cases and the exact-cap boundaries
(``>`` is strict, so exactly 4000/2000 chars is clean).
"""

from __future__ import annotations

import pytest

from fabulist.dsl.custom_policy import (
    CSS_CAP,
    HTML_CAP,
    validate_custom,
)


def codes(violations: list[str]) -> list[str]:
    """The policy class of each violation (the text before the first ``:``)."""
    return [v.split(":", 1)[0].strip() for v in violations]


# ---------------------------------------------------------------------------
# clean payloads
# ---------------------------------------------------------------------------


def test_clean_payload_has_no_violations():
    assert validate_custom('<div class="panel"><h2>Status</h2></div>', "div { color: red }") == []


def test_empty_payload_is_clean():
    assert validate_custom("", "") == []


def test_none_like_inputs_are_clean():
    # The gate passes "" for absent/non-literal args; None must not explode.
    assert validate_custom(None, None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# caps — boundary is strict (exactly the cap is clean)
# ---------------------------------------------------------------------------


def test_html_exactly_at_cap_is_clean():
    assert validate_custom("a" * HTML_CAP, "") == []


def test_html_one_over_cap_is_flagged():
    violations = validate_custom("a" * (HTML_CAP + 1), "")
    assert codes(violations) == ["html_cap"]
    assert str(HTML_CAP) in violations[0]


def test_css_exactly_at_cap_is_clean():
    assert validate_custom("", "a" * CSS_CAP) == []


def test_css_one_over_cap_is_flagged():
    violations = validate_custom("", "a" * (CSS_CAP + 1))
    assert codes(violations) == ["css_cap"]


# ---------------------------------------------------------------------------
# banned tags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    ["script", "iframe", "object", "embed", "link", "meta", "style", "form"],
)
def test_banned_tags_are_flagged(tag):
    violations = validate_custom(f"<{tag}>", "")
    assert codes(violations) == ["banned_tag"]


@pytest.mark.parametrize("tag", ["SCRIPT", "IfRaMe", "Style"])
def test_banned_tags_are_case_insensitive(tag):
    assert codes(validate_custom(f"<{tag}>", "")) == ["banned_tag"]


def test_banned_tag_matches_opener_with_whitespace():
    # ``<\s*`` tolerates a space/tab before the name.
    assert codes(validate_custom("< script >x</ script>", "")) == ["banned_tag"]


def test_allowed_tags_are_not_flagged():
    assert validate_custom("<div><span><img><p><table><a>x</a></table>", "") == []


# ---------------------------------------------------------------------------
# event handlers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attr", ["onerror=", "onclick =", "ONLOAD ="])
def test_event_handlers_are_flagged(attr):
    assert codes(validate_custom(f"<img {attr}x>", "")) == ["event_handler"]


def test_word_containing_on_is_not_an_event_handler():
    # No ``name=`` shape, so a plain word must not trip the class.
    assert validate_custom("the moon rises at once over the horizon", "") == []


# ---------------------------------------------------------------------------
# remote URIs (either channel)
# ---------------------------------------------------------------------------


def test_remote_uri_in_html_is_flagged():
    assert codes(validate_custom('<img src="https://evil/x.png">', "")) == ["remote_uri"]


def test_remote_uri_in_css_is_flagged():
    assert codes(validate_custom("", "p { background: http://evil/x }")) == ["remote_uri"]


def test_remote_uri_is_case_insensitive():
    assert codes(validate_custom("HTTP://evil", "")) == ["remote_uri"]


# ---------------------------------------------------------------------------
# css @import and url() targets
# ---------------------------------------------------------------------------


def test_css_at_import_is_flagged():
    assert codes(validate_custom("", '@import url("evil.css");')) == [
        "css_at_import",
        "remote_css_url",
    ]


def test_remote_css_url_is_flagged():
    assert codes(validate_custom("", "p { background: url(//evil/x.png) }")) == [
        "remote_css_url"
    ]


def test_relative_css_url_is_flagged():
    assert codes(validate_custom("", "p { background: url(image.png) }")) == ["remote_css_url"]


@pytest.mark.parametrize(
    "target",
    ["data:image/png;base64,AAAA", "/api/personas/1/image", "/api/worlds/2/image"],
)
def test_allowed_css_url_targets_are_clean(target):
    assert validate_custom("", f"p {{ background: url({target}) }}") == []


def test_quoted_css_url_target_is_inspected():
    assert codes(validate_custom("", "p { background: url('https://evil/x') }")) == [
        "remote_uri",
        "remote_css_url",
    ]


def test_empty_css_url_target_is_ignored():
    assert validate_custom("", "p { background: url() }") == []


# ---------------------------------------------------------------------------
# multiple classes in one payload
# ---------------------------------------------------------------------------


def test_multiple_violations_are_all_reported():
    html = '<script>x</script><img src="http://evil">'
    css = '@import url(evil.css); p { background: url(evil.png) }'
    assert codes(validate_custom(html, css)) == [
        "banned_tag",
        "remote_uri",
        "css_at_import",
        "remote_css_url",
    ]


def test_a_root_percentage_height_is_refused_as_a_collapse_hazard():
    """A live session: a root `height:100%` inside the content-sized host
    collapsed the artifact to a 2px strip that read as an empty element."""
    html = (
        "<div style='display: flex; justify-content: center; height: 100%; "
        "background: #001a33; position: relative; overflow: hidden;'>"
        "<div style='position: absolute; top: 20%;'>hot</div></div>"
    )
    violations = validate_custom(html, "")
    assert len(violations) == 1
    code, detail = violations[0].split(":", 1)
    assert code == "root_percentage_height"
    assert "220px" in detail


def test_a_px_root_height_is_clean():
    html = "<div style='height: 220px; display: flex;'><span>hot</span></div>"
    assert validate_custom(html, "") == []


def test_a_nested_percentage_height_is_legal():
    """Only the ROOT is checked: a percentage against a declared ancestor resolves."""
    html = (
        "<div style='height: 200px;'>"
        "<div style='height: 100%;'>hot</div></div>"
    )
    assert validate_custom(html, "") == []


def test_a_percentage_width_is_not_the_collapse_class():
    html = "<div style='width: 100%; height: 120px;'><span>hot</span></div>"
    assert validate_custom(html, "") == []
