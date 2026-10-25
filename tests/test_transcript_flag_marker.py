"""Item 14: a validator note must never style the message it flags.

Owner report: "some messages render ENTIRELY wrapped in what looks like a
component container (left-side highlight bar, all text inside the container)".
RCA on prod data: the ENGINE is clean — 488 stored ``dsl`` segments and every
one starts a real component statement (bar 40 culled/legacy names such as
``StatusPanel``/``Subtext``), no dsl segment trails prose, no narrative has an
empty prose channel. The FRONTEND segmenter agrees with it (the only re-splits
are the pre-2ac7775 bare-call rows ``coerceSegments`` already handles). The
container was ``.flagged``: the validator anchors most notes to the FIRST prose
run, and that run is most of the reply, so a 2px warn bar + tinted background
drew the whole message as a box.

Fixtures (``fixtures/transcript_flags/flagged_turns.json``) are 15 SYNTHETIC
narrative/validator pairs holding the shape of the prod ones (invented prose,
placeholder session ids); seven of them hold 97-100% of the message text inside
the flagged run — the "ENTIRELY wrapped" case. The tests below pin (a) that those
fixtures really are whole-message flags, and (b) the render contract that
replaces the container with a margin marker.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "transcript_flags" / "flagged_turns.json"
TRANSCRIPT_PATH = REPO_ROOT / "frontend" / "src" / "routes" / "chat" / "TranscriptList.svelte"

#: The flagged run must be at least this share of the message to read as "the
#: whole message in a container" (the fixture filter; the near-100% cases are
#: the owner's word "ENTIRELY").
WHOLE_MESSAGE_SHARE = 0.60


def _fixture_cases() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]


def _style_block() -> str:
    """The component's <style> block (Svelte scoped styles, one per file)."""
    source = TRANSCRIPT_PATH.read_text(encoding="utf-8")
    match = re.search(r"<style>(.*)</style>", source, re.DOTALL)
    assert match is not None, "TranscriptList.svelte has no <style> block"
    return match.group(1)


def _rules(css: str) -> dict[str, str]:
    """Flat ``selector -> declaration body`` map (no nested at-rules here)."""
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    out: dict[str, str] = {}
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", stripped):
        selector = " ".join(match.group(1).split())
        out[selector] = " ".join(match.group(2).split())
    return out


# --- the whole-message cases ----------------------------------------------


def test_fixture_carries_whole_message_flags() -> None:
    """Every fixture case really is a validator note on most of the message."""
    cases = _fixture_cases()
    assert len(cases) >= 10, "fixture lost its whole-message cases"
    for case in cases:
        segments = case["segments"]
        total = sum(segment["len"] for segment in segments)
        flagged = segments[case["segment_index"]]
        assert flagged["kind"] == "prose", case
        assert flagged["len"] / total >= WHOLE_MESSAGE_SHARE, case
        assert case["rule_id"], case


def test_fixture_includes_the_entirely_wrapped_turns() -> None:
    """The owner's "ENTIRELY" case: the flagged run IS the message.

    The defect session rendered ten such turns; the fixture keeps seven of them
    at >=97% so a regression that re-boxes a whole reply is caught by the data
    shape alone.
    """
    near_total = [c for c in _fixture_cases() if c["flagged_fraction"] >= 0.97]
    assert len(near_total) >= 5, near_total
    assert all(c["segments"][c["segment_index"]]["kind"] == "prose" for c in near_total)


# --- the render contract --------------------------------------------------


def test_flagged_section_is_not_a_container() -> None:
    """``.flagged`` marks the margin; it must not paint the text.

    The regression was a container: a warn bar, a tinted background and inset
    padding on the flagged run. A marker is positional only.
    """
    css = _style_block()
    rules = _rules(css)
    flagged = rules.get(".flagged")
    assert flagged is not None, ".flagged rule disappeared"
    for forbidden in ("border-left", "background", "padding-left", "border-radius"):
        assert forbidden not in flagged, f".flagged grew a container again: {forbidden}"
    assert "position: relative" in flagged


def test_flagged_section_carries_a_margin_marker() -> None:
    """The note stays visible at its anchor, as a glyph outside the text column."""
    marker = _rules(_style_block()).get(".flagged::before")
    assert marker is not None, "the flagged anchor lost its margin marker"
    assert "content:" in marker
    assert "position: absolute" in marker
    assert "left: -" in marker, "the marker must sit outside the prose column"
    assert "color: var(--warn)" in marker


def test_no_flagged_container_is_redefined_elsewhere() -> None:
    """No second rule may re-box the flagged run (e.g. the old .dsl-wrap one)."""
    for selector, body in _rules(_style_block()).items():
        if ".flagged" not in selector or selector == ".flagged::before":
            continue
        for forbidden in ("border-left", "background:", "padding-left"):
            assert forbidden not in body, f"{selector} re-boxes a flagged section: {forbidden}"


def test_marker_offset_stays_inside_the_transcript_gutter() -> None:
    """The marker must not be clipped: |left| < the transcript's own padding."""
    rules = _rules(_style_block())
    left = float(re.search(r"left:\s*-([\d.]+)rem", rules[".flagged::before"]).group(1))  # type: ignore[union-attr]
    padding = float(re.search(r"padding:\s*([\d.]+)rem", rules[".transcript"]).group(1))  # type: ignore[union-attr]
    assert left <= padding
