"""Tests for the W2.6 Dress Rehearsal validator (flags, never rewrites).

Covers the acceptance criteria verbatim:
  * each check fires and cites its evidence (matched text / rule id / source);
  * clean prose scores 100 with no flags;
  * a check can be disabled via config;
  * ``prior_first_words`` echo is caught, case-insensitively;
  * the parrot overlap is case-insensitive and threshold-gated;
  * DSL segments are excluded from every prose check;
  * the pass is pure (no mutation, deterministic).
"""

from __future__ import annotations

import dataclasses

from fabulist.graph.validator import (
    CHECK_CONTROL,
    CHECK_ECHO_START,
    CHECK_PARROT,
    CHECK_SLOP,
    DEFAULT_SLOP_PATTERNS,
    PRESET_ANTISLOP_LINE,
    PRESET_SOURCE,
    SlopPattern,
    ValidatorConfig,
    validate_narrative,
    words,
)

# Prose proven (below) to trip none of the default checks.
CLEAN_PROSE = (
    "The innkeeper wiped the counter and set down two mugs. Steam curled from "
    "the tea. A dog barked somewhere past the stable, and the lamplight wobbled."
)
CLEAN_USER = "Good evening."


def _prose(*texts: str) -> list[dict]:
    return [{"kind": "prose", "text": t} for t in texts]


# ---------------------------------------------------------------------------
# clean baseline
# ---------------------------------------------------------------------------


def test_clean_prose_scores_100_with_no_flags():
    report = validate_narrative(CLEAN_USER, _prose(CLEAN_PROSE))
    assert report.flags == []
    assert report.score == 100
    assert report.clean is True


def test_none_config_matches_default_config():
    a = validate_narrative(CLEAN_USER, _prose(CLEAN_PROSE))
    b = validate_narrative(CLEAN_USER, _prose(CLEAN_PROSE), ValidatorConfig())
    assert a.as_dict() == b.as_dict()


def test_report_is_pure_and_deterministic():
    segments = _prose(CLEAN_PROSE)
    snapshot = [dict(s) for s in segments]
    a = validate_narrative(CLEAN_USER, segments)
    b = validate_narrative(CLEAN_USER, segments)
    assert a.as_dict() == b.as_dict()
    assert segments == snapshot  # input never mutated


# ---------------------------------------------------------------------------
# 1. PARROT
# ---------------------------------------------------------------------------


def test_parrot_fires_and_quotes_shingles():
    user = "I open the door and step inside the room"
    report = validate_narrative(user, _prose("I open the door and step inside the room, blade ready."))
    flags = report.by_check()[CHECK_PARROT]
    assert len(flags) == 1
    flag = flags[0]
    assert flag.rule_id == "parrot"
    assert flag.severity == "warn"
    # The overlapping shingles are quoted in the evidence.
    assert '"i open the door"' in flag.evidence
    assert '"and step inside the"' in flag.evidence
    assert report.score < 100


def test_parrot_needs_more_than_one_distinct_shingle():
    # A single 4-word shingle is exactly shingle_min (=1): no flag.
    report = validate_narrative("I open the door", _prose("I open the door."))
    assert CHECK_PARROT not in report.by_check()


def test_parrot_shingle_min_override_allows_single():
    report = validate_narrative(
        "I open the door",
        _prose("I open the door."),
        ValidatorConfig(shingle_min=0),
    )
    assert CHECK_PARROT in report.by_check()


def test_parrot_is_case_insensitive():
    user = "She Watched The River Flow Past The Bridge"
    report = validate_narrative(user, _prose("she watched the river flow past the bridge today."))
    flags = report.by_check()[CHECK_PARROT]
    assert len(flags) == 1
    assert '"she watched the river"' in flags[0].evidence


def test_parrot_empty_user_message_is_ignored():
    report = validate_narrative("", _prose(CLEAN_PROSE))
    assert CHECK_PARROT not in report.by_check()


# ---------------------------------------------------------------------------
# 2. SLOP
# ---------------------------------------------------------------------------


def test_default_slop_list_has_at_least_12_patterns_and_cites_preset_line():
    assert len(DEFAULT_SLOP_PATTERNS) >= 12
    for pattern in DEFAULT_SLOP_PATTERNS:
        assert isinstance(pattern, SlopPattern)
        assert f"{PRESET_SOURCE}:{PRESET_ANTISLOP_LINE}" in pattern.source
        assert pattern.source  # every rule cites its source


def test_slop_fires_with_evidence_and_source():
    report = validate_narrative(CLEAN_USER, _prose("A shiver down her spine made her pause."))
    flags = report.by_check()[CHECK_SLOP]
    assert flags, "expected the banned 'shiver down' phrase to be flagged"
    flag = next(f for f in flags if f.rule_id == "shivers_down")
    assert flag.severity == "warn"
    assert "shiver down" in flag.evidence
    assert f"{PRESET_SOURCE}:{PRESET_ANTISLOP_LINE}" in flag.source


def test_slop_is_case_insensitive_and_uses_word_boundaries():
    report = validate_narrative(CLEAN_USER, _prose("The OZONE smell lingered. Nozone is not a word."))
    flags = report.by_check()[CHECK_SLOP]
    assert [f.rule_id for f in flags] == ["ozone"]


def test_slop_disabled_via_config():
    report = validate_narrative(
        CLEAN_USER,
        _prose("A shiver down her spine made her pause."),
        ValidatorConfig(enabled={CHECK_SLOP: False}),
    )
    assert CHECK_SLOP not in report.by_check()
    assert report.score == 100


def test_slop_list_override_replaces_default_list():
    cfg = ValidatorConfig(slop_list=["testament to"])
    report = validate_narrative(
        CLEAN_USER,
        _prose("It is a testament to her skill. The OZONE smell lingered."),
        cfg,
    )
    flags = report.by_check()[CHECK_SLOP]
    assert len(flags) == 1
    assert flags[0].rule_id == "custom:testament to"
    assert flags[0].source == "config"
    assert "testament to" in flags[0].evidence
    # The default patterns were replaced, so ozone is no longer flagged.
    assert not any(f.rule_id == "ozone" for f in flags)


# ---------------------------------------------------------------------------
# 3. CONTROL
# ---------------------------------------------------------------------------


def test_control_ceding_marker_fires():
    report = validate_narrative(CLEAN_USER, _prose("As you wish, the story obeys."))
    flags = report.by_check()[CHECK_CONTROL]
    assert len(flags) == 1
    assert flags[0].rule_id == "control.ceding"
    assert flags[0].severity == "warn"
    assert "as you wish" in flags[0].evidence.lower()


def test_control_meta_instruction_fires():
    report = validate_narrative(CLEAN_USER, _prose("You decide what happens next."))
    flags = report.by_check()[CHECK_CONTROL]
    assert flags
    assert flags[0].rule_id == "control.meta_instruction"
    assert "you decide what" in flags[0].evidence.lower()


def test_control_disabled_via_config():
    report = validate_narrative(
        CLEAN_USER,
        _prose("As you wish, the story obeys."),
        ValidatorConfig(enabled={CHECK_CONTROL: False}),
    )
    assert CHECK_CONTROL not in report.by_check()


def test_enabled_toggle_is_scoped():
    report = validate_narrative(
        CLEAN_USER,
        _prose("A shiver down her spine. You decide what happens next."),
        ValidatorConfig(enabled={CHECK_PARROT: False}),
    )
    assert CHECK_PARROT not in report.by_check()
    assert CHECK_SLOP in report.by_check()
    assert CHECK_CONTROL in report.by_check()


# ---------------------------------------------------------------------------
# 4. ECHO-START
# ---------------------------------------------------------------------------


def test_echo_start_fires_and_is_info():
    prior = "The lantern guttered and the room went dark"
    report = validate_narrative(
        CLEAN_USER,
        _prose("The lantern guttered and the room went dark, then still."),
        ValidatorConfig(prior_first_words=prior),
    )
    flags = report.by_check()[CHECK_ECHO_START]
    assert len(flags) == 1
    assert flags[0].severity == "info"
    assert flags[0].rule_id == "echo_start"
    assert "the lantern guttered and the room" in flags[0].evidence.lower()


def test_echo_start_is_case_insensitive():
    prior = "THE LANTERN GUTTERED AND THE ROOM WENT DARK"
    report = validate_narrative(
        CLEAN_USER,
        _prose("The lantern guttered and the room went dark, then still."),
        ValidatorConfig(prior_first_words=prior),
    )
    assert CHECK_ECHO_START in report.by_check()


def test_echo_start_absent_without_prior():
    report = validate_narrative(
        CLEAN_USER, _prose("The lantern guttered and the room went dark."), ValidatorConfig()
    )
    assert CHECK_ECHO_START not in report.by_check()


def test_echo_start_no_flag_when_openings_differ():
    prior = "The lantern guttered and the room went dark"
    report = validate_narrative(
        CLEAN_USER,
        _prose("Rain hammered the shutters while the fire spat."),
        ValidatorConfig(prior_first_words=prior),
    )
    assert CHECK_ECHO_START not in report.by_check()


# ---------------------------------------------------------------------------
# segment-kind handling
# ---------------------------------------------------------------------------


def test_dsl_segments_are_excluded_from_prose_checks():
    segments = [
        {
            "kind": "dsl",
            "text": "A shiver down her spine. You decide what happens next. "
            "I open the hidden door and slip through the gap.",
        },
        {"kind": "prose", "text": CLEAN_PROSE},
    ]
    report = validate_narrative("I open the hidden door and slip through the gap", segments)
    assert report.flags == []
    assert report.score == 100


def test_only_prose_segments_are_inspected():
    # Order-independent: a clean dsl segment must not rescue a sloppy prose one.
    segments = [
        {"kind": "dsl", "text": "root() {}"},
        {"kind": "prose", "text": "A shiver down her spine."},
    ]
    report = validate_narrative(CLEAN_USER, segments)
    assert CHECK_SLOP in report.by_check()


def test_blank_and_malformed_segments_are_ignored():
    segments = [
        {"kind": "prose", "text": "   "},
        {"kind": "prose"},
        {"kind": "image", "text": "A shiver down her spine."},
        {"kind": "prose", "text": CLEAN_PROSE},
    ]
    report = validate_narrative(CLEAN_USER, segments)
    assert report.flags == []
    assert report.score == 100


def test_user_message_is_not_a_segment():
    # Prose identical to a dsl-only segment never parrots the user message.
    segments = [{"kind": "dsl", "text": "I open the door and step inside the room"}]
    report = validate_narrative("I open the door and step inside the room", segments)
    assert report.flags == []


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def test_score_is_clamped_to_zero_and_bounded():
    sloppy = (
        "A shiver down her spine. The air thick with meaning. Ozone. "
        "A predatory grin. A pregnant pause. A velvety voice. "
        "The night is young. A stark contrast."
    )
    report = validate_narrative(CLEAN_USER, _prose(sloppy))
    assert 0 <= report.score <= 100
    assert report.score == 0
    assert len(report.flags) >= 7


def test_more_flags_score_lower_than_fewer():
    one = validate_narrative(CLEAN_USER, _prose("A shiver down her spine."))
    two = validate_narrative(CLEAN_USER, _prose("A shiver down her spine. A pregnant pause."))
    assert two.score < one.score < 100


def test_words_helper_normalizes_case_and_punctuation():
    assert words("Hello,  World!  It's Fine.") == ["hello", "world", "it's", "fine"]


# ---------------------------------------------------------------------------
# the narrator repair loop's validate TOOL (W9.24 §3.3.2)
# ---------------------------------------------------------------------------
#
# The loop's validate step is not a second implementation: it is this module's
# ``validate_narrative`` plus the DSL gate's findings plus the ``prose_markup``
# detect, run engine-side after the draft and after every patch. These pins make
# that seam explicit — a "tool" that drifts from the validator would let the loop
# repair something the post-commit gate never complains about (or miss what it
# does).


def _loop_flags(user_message, segments):
    from fabulist.graph.nodes import _narrate_flags

    return _narrate_flags(
        user_content=user_message,
        segments=segments,
        validation_errors=[],
        prior_first_words=None,
    )


def test_narrate_validate_tool_is_the_dress_rehearsal_validator():
    """The loop's flag bundle IS ``validate_narrative``'s findings — no fork."""
    sloppy = [{"kind": "prose", "text": "A shiver down her spine. She waits."}]
    expected = [
        dataclasses.asdict(flag)
        for flag in validate_narrative(CLEAN_USER, sloppy).flags
    ]
    assert _loop_flags(CLEAN_USER, sloppy) == expected


def test_narrate_validate_tool_is_silent_on_clean_prose():
    clean = [{"kind": "prose", "text": "Rain pooled in the gutter and the lamp died."}]
    assert _loop_flags(CLEAN_USER, clean) == []


def test_narrate_validate_tool_folds_the_dsl_gate_findings():
    """A DSL finding the gate kept verbatim is part of the same bundle."""
    from fabulist.graph.nodes import _narrate_flags

    segments = [{"kind": "prose", "text": "She waits."}]
    dsl_errors = [
        {
            "code": "dangling_ref",
            "message": "reference to undefined id 'ghost'",
            "repaired": False,
        }
    ]
    flags = _narrate_flags(
        user_content=CLEAN_USER,
        segments=segments,
        validation_errors=dsl_errors,
        prior_first_words=None,
    )
    assert [flag["rule_id"] for flag in flags] == ["dangling_ref"]


def test_prose_markup_flag_reads_the_segments_not_a_stale_leak_list():
    """D80's detect: markup in prose flags, and repairs that remove it clear it."""
    from fabulist.graph.nodes import _prose_markup_flags

    marked = [
        {"kind": "prose", "text": "TERMINAL READY <div style='color:#0f0'>x</div>"},
        {"kind": "dsl", "text": 'panel = Custom(html="<b>ok</b>")'},
    ]
    flags = _prose_markup_flags(marked)
    assert [flag["rule_id"] for flag in flags] == ["prose_markup"]
    assert "<div" in flags[0]["evidence"]
    # A DSL statement's legitimate html prop is NOT prose markup (D49 scope).
    assert _prose_markup_flags(marked[1:]) == []


# ---------------------------------------------------------------------------
# anchored flags: WHERE to patch + WHAT to do (a live turn)
# ---------------------------------------------------------------------------
#
# A flag that only names the offence leaves the repair loop guessing: the live
# model re-issued ONE identical `find` fourteen times and the tool refused it
# every time. Every flag the narrate validate step injects therefore carries
# `anchor` (the EXACT prose substring that triggered it) and `fix` (the
# corrective action), and the loop renders both.


def test_slop_flag_anchor_is_the_exact_prose_span():
    prose = "The hall was quiet. A shiver down her spine said otherwise."
    flag = validate_narrative(CLEAN_USER, _prose(prose)).by_check()[CHECK_SLOP][0]
    assert "shiver down" in flag.anchor
    assert flag.anchor in prose


def test_parrot_flag_anchor_is_the_echoed_span():
    user = "I open the door and step inside the room"
    prose = "The lantern swayed. I open the door and step inside the room, blade ready."
    flag = validate_narrative(user, _prose(prose)).by_check()[CHECK_PARROT][0]
    assert "open the door and step" in flag.anchor
    assert flag.anchor in prose


def test_control_flag_anchor_is_the_exact_prose_span():
    prose = "She smiles. You decide what happens next. The door clicks shut."
    flag = validate_narrative(CLEAN_USER, _prose(prose)).by_check()[CHECK_CONTROL][0]
    assert "you decide what" in flag.anchor.lower()
    assert flag.anchor in prose


def test_echo_start_flag_anchor_is_the_opening_words():
    prose = "The lantern guttered and the room went dark, then still."
    flag = validate_narrative(
        CLEAN_USER,
        _prose(prose),
        ValidatorConfig(prior_first_words="The lantern guttered and the room went dark"),
    ).by_check()[CHECK_ECHO_START][0]
    assert flag.anchor.startswith("The lantern guttered")
    assert flag.anchor in prose


def test_every_flag_carries_a_fix_and_never_fabricates_an_anchor():
    prose = "A shiver down her spine. You decide what happens next."
    flags = validate_narrative(
        "I open the door and step inside the room", _prose(prose)
    ).flags
    assert {flag.check for flag in flags} >= {CHECK_SLOP, CHECK_CONTROL}
    for flag in flags:
        assert flag.fix, flag
        assert flag.anchor, flag
        # Byte-exact prose, or (when clipped) byte-exact between the `…` markers.
        assert flag.anchor.strip("…") in prose, flag.anchor
        assert "…" not in flag.anchor.strip("…"), flag.anchor


def test_a_long_anchor_is_clipped_with_boundary_ellipses_only():
    filler = (
        "The harbour bell rang across the wet stones and the gulls wheeled overhead "
        "while the tide pulled at the mooring ropes and the lamps came on one by one "
        "along the quay. "
    )
    prose = filler + "A shiver down her spine said otherwise. " + filler
    flag = validate_narrative(CLEAN_USER, _prose(prose)).by_check()[CHECK_SLOP][0]
    assert flag.anchor.startswith("…") and flag.anchor.endswith("…")
    assert "shiver down" in flag.anchor
    assert flag.anchor.strip("…") in prose
    # The window never starts or ends mid-word (the anchor stays copy-pastable).
    body = flag.anchor.strip("…")
    assert body.split()[0] in prose and body.split()[-1] in prose


def test_prose_markup_flag_anchors_the_first_markup_run():
    from fabulist.graph.nodes import PROSE_MARKUP_FIX, _prose_markup_flags

    text = "Hobbs glances up.\n<div style='color:#0f0'>TERMINAL READY</div>\nHe waits."
    flags = _prose_markup_flags(
        [
            {"kind": "prose", "text": "A clean opening."},
            {"kind": "dsl", "text": 'panel = Custom("<b>ok</b>", "")'},
            {"kind": "prose", "text": text},
        ]
    )
    flag = flags[0]
    assert "<div" in flag["anchor"]
    assert flag["anchor"] in text
    assert flag["fix"] == PROSE_MARKUP_FIX
    # The anchor points at the prose segment it came from (index 2, not the dsl).
    assert flag["segment_index"] == 2


def test_dsl_gate_flags_do_not_fabricate_a_prose_anchor():
    """No exact prose span exists for a DSL finding — the field is omitted."""
    from fabulist.graph.nodes import _narrate_flags

    flags = _narrate_flags(
        user_content=CLEAN_USER,
        segments=[{"kind": "prose", "text": "She waits."}],
        validation_errors=[
            {"code": "dangling_ref", "message": "undefined id 'ghost'", "repaired": False}
        ],
        prior_first_words=None,
    )
    assert [flag["rule_id"] for flag in flags] == ["dangling_ref"]
    assert "anchor" not in flags[0]


# ---------------------------------------------------------------------------
# W9.28 D6: the colour-only dialogue span is NOT prose markup
# ---------------------------------------------------------------------------
#
# The narrator is TOLD to wrap every spoken line in the speaking character's
# palette colour, so the D80 detect must not report that span as drift — the
# repair loop would then hand the model an anchor on the very markup the
# contract requires and a fix that says to delete it. The predicate lives INSIDE
# ``_markup_span`` (the single point the flag anchor, the evidence sample and the
# segmenter's leak channel read), it is PER TAG, and it is strict: anything
# richer than one ``color`` declaration is ordinary markup again.

#: The contract's own markup: one span, one colour, one spoken line.
COLOUR_SPAN = '<span style="color:#B03A48">"You\'re late."</span>'


def test_a_colour_only_span_is_not_prose_markup():
    from fabulist.graph.nodes import _detect_markup_prose, _markup_span, _prose_markup_flags

    for text in (
        COLOUR_SPAN,
        'She waits. <font color="red">"Not tonight."</font> Rain falls.',
        '<SPAN STYLE="COLOR : #B03A48">"Case-insensitive."</SPAN>',
        '<span style="color:rgb(176,58,72)">"Rgb."</span>',
        '<span style="color: #B03A48;">"Trailing semicolon."</span>',
    ):
        assert _markup_span(text) is None, text
        leaks: list[str] = []
        assert _detect_markup_prose(text, leaks) is False, text
        assert leaks == [], text
        assert _prose_markup_flags([{"kind": "prose", "text": text}]) == [], text


def test_a_real_tag_beside_a_colour_span_is_the_anchor_never_the_span():
    """Two-sided pin: the legit span is unflagged, the offender is what anchors."""
    from fabulist.graph.nodes import _detect_markup_prose, _markup_span, _prose_markup_flags

    text = COLOUR_SPAN + ' Then the HUD says <div style="color:#0f0">READY</div>.'
    flags = _prose_markup_flags([{"kind": "prose", "text": text}])

    assert [flag["rule_id"] for flag in flags] == ["prose_markup"]
    # The chosen candidate IS the real offender — the colour span before it is
    # skipped, not anchored (the anchor's own context window may reach back).
    span = _markup_span(text)
    assert span is not None
    assert span[0] > text.index("</span>")
    assert text[span[0] : span[1]] == '<div style="color:#0f0">'
    assert "<div" in flags[0]["anchor"]
    assert flags[0]["anchor"] in text
    # ...and so does the evidence sample (R2 P2-1: one filtered candidate).
    assert flags[0]["evidence"] == '<div style="color:#0f0">'

    leaks: list[str] = []
    assert _detect_markup_prose(text, leaks) is True
    assert leaks == ['<div style="color:#0f0">']


def test_the_segmenters_leaks_channel_ignores_the_colour_span():
    """The segmenter shares the detector, so colour dialogue never reads as drift."""
    from fabulist.graph.nodes import _segment_unfenced

    leaks: list[str] = []
    segments = _segment_unfenced("Rain falls. " + COLOUR_SPAN, leaks)
    assert leaks == []
    assert segments and segments[0]["kind"] == "prose"
    assert 'style="color:#B03A48"' in segments[0]["text"]  # D80: never stripped

    leaks = []
    _segment_unfenced("Rain falls. " + COLOUR_SPAN + "\n<div>READY</div>", leaks)
    assert leaks == ["<div>"]


def test_any_unlisted_attribute_disqualifies_the_colour_tag():
    """D6b: ``on*``, ``class``, ``id``, ``data-*``, ``face`` — all ordinary markup."""
    from fabulist.graph.nodes import _markup_span

    for text in (
        '<span style="color:#B03A48" class="quote">"Hi."</span>',
        '<span style="color:#B03A48" ID="x">"Hi."</span>',
        '<span style="color:#B03A48" onclick="steal()">"Hi."</span>',
        '<span style="color:#B03A48" data-role="npc">"Hi."</span>',
        '<span style="color:#B03A48" title="hint">"Hi."</span>',
        '<font face="arial" color="red">"Hi."</font>',
        '<font color="red" size="4">"Hi."</font>',
        '<a href="http://x" style="color:#B03A48">"Hi."</a>',
        # A tag with no colour at all is not the contract's markup.
        '<span>"Hi."</span>',
    ):
        assert _markup_span(text) is not None, text


def test_the_style_grammar_admits_exactly_one_colour_declaration():
    """No second property, no ``url(``, no comment, no escape, no duplicate."""
    from fabulist.graph.nodes import _markup_span

    for style in (
        "color:#B03A48;background:#000",
        "background:url(http://x);color:red",
        "color:url(http://x)",
        "color:red/*x*/",
        "color:red;color:blue",
        "COLOR:red;Color:blue",
        "color:red!important",
        "color:expression(alert(1))",
        "#B03A48",
        "",
    ):
        text = f'<span style="{style}">"Hi."</span>'
        assert _markup_span(text) is not None, style


def test_colour_markup_is_per_tag_so_a_nested_offender_still_flags():
    """A whitelisted span does not launder the markup it wraps."""
    from fabulist.graph.nodes import _markup_span

    text = '<span style="color:#B03A48"><svg onload="alert(1)"></svg></span>'
    span = _markup_span(text)
    assert span is not None
    assert text[span[0] : span[1]] == '<svg onload="alert(1)">'


# ---------------------------------------------------------------------------
# W9.28 D3: ``attribution_missing`` rides the SAME flag bundle
# ---------------------------------------------------------------------------


def _palette():
    from fabulist.state.attribution import PaletteEntry

    return [PaletteEntry("cm_mara", "Mara", "#e06c75", "red")]


def test_narrate_flags_carry_attribution_missing_for_an_unwrapped_line():
    """The pre-commit bundle folds the attribution contract like prose_markup."""
    from fabulist.graph.nodes import _narrate_flags

    prose = 'Mara leans on the counter. "You\'re late." She grins.'
    flags = _narrate_flags(
        user_content=CLEAN_USER,
        segments=[{"kind": "prose", "text": prose}],
        validation_errors=[],
        prior_first_words=None,
        palette=_palette(),
    )
    assert [flag["rule_id"] for flag in flags] == ["attribution_missing"]
    flag = flags[0]
    # Folded like prose_markup — never a Dress Rehearsal check.
    assert flag["check"] == "attribution"
    assert flag["severity"] == "warn"
    assert flag["evidence"] == "You're late."
    assert flag["anchor"] in prose
    assert '"You\'re late."' in flag["anchor"]
    assert "#e06c75" in flag["fix"]

    # A wrapped line clears it; no palette flags nothing (no hue to teach).
    wrapped = prose.replace(
        '"You\'re late."', '<span style="color:#e06c75">"You\'re late."</span>'
    )
    assert (
        _narrate_flags(
            user_content=CLEAN_USER,
            segments=[{"kind": "prose", "text": wrapped}],
            validation_errors=[],
            prior_first_words=None,
            palette=_palette(),
        )
        == []
    )
    assert (
        _narrate_flags(
            user_content=CLEAN_USER,
            segments=[{"kind": "prose", "text": prose}],
            validation_errors=[],
            prior_first_words=None,
        )
        == []
    )
