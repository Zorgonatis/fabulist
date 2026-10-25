"""Speaker attribution (W9.28) — the colour-markup engine.

Attribution is no longer detected from prose cues. The narrator wraps each
spoken line in the speaking character's palette colour and the engine parses
that markup (:func:`fabulist.state.attribution.parse_attribution`); an
unwrapped line is strictly unattributed and surfaces an ``attribution_missing``
flag that rides the narrate repair loop
(:func:`fabulist.state.attribution.attribution_missing_flags`), and a colour
span wrapping prose with NO quoted line — unquoted speech or mis-coloured
narration — surfaces ``attribution_unquoted`` from the sibling fold
(:func:`fabulist.state.attribution.attribution_unquoted_flags`). These tests pin
the parse matrix (span/font, hex/label/rgb, tolerance, ambiguity), the tag
MASKING that keeps ``style="color:#…"`` from reading as speech (R2 P0-1), the
paragraph-bounded colour context, the palette projection, the flags, and the
live-turn regression fixture (zero attributions, silent — the defect W9.28
replaces).
"""

from __future__ import annotations

import asyncio

import pytest

from fabulist.api.cast_api import (
    SPEAKER_PALETTE,
    SPEAKER_PALETTE_NAMES,
    color_name,
    normalize_hex_color,
)
from fabulist.state.attribution import (
    RGB_TOLERANCE,
    MarkupAttributionRunner,
    PaletteEntry,
    attribution_missing_flags,
    attribution_unquoted_flags,
    extract_quotes,
    palette_for_turn,
    parse_attribution,
    scan_quotes,
)
from fabulist.state.cast import admit_member

SID = "s1"

#: The engine's own hues, by slot.
RED = SPEAKER_PALETTE[0]  # #e06c75 (224, 108, 117)
AMBER = SPEAKER_PALETTE[1]  # #e5c07b


def run(coro):
    return asyncio.run(coro)


def _palette(*rows: tuple[str, str, int]) -> list[PaletteEntry]:
    """``(member_id, display_name, slot)`` triples -> the palette they spell."""
    entries: list[PaletteEntry] = []
    for member_id, display_name, slot in rows:
        hex_value = SPEAKER_PALETTE[slot]
        entries.append(
            PaletteEntry(
                member_id=member_id,
                display_name=display_name,
                hex=hex_value,
                name=color_name(hex_value),
            )
        )
    return entries


#: The scene most tests use: Aria (slot 0, red) and Borin (slot 1, amber).
PALETTE = _palette(("cm_aria", "Aria", 0), ("cm_borin", "Borin", 1))


def _speakers(narrative, palette=PALETTE) -> list[tuple[str, str]]:
    return [
        (row["speaker"], row["quote"]) for row in parse_attribution(narrative, palette)
    ]


def _wrapped(hex_value: str, line: str, *, tag: str = "span") -> str:
    """One spoken line wrapped the way the narrator is taught to wrap it."""
    if tag == "font":
        return f'<font color="{hex_value}">{line}</font>'
    return f'<span style="color:{hex_value}">{line}</span>'


def _flags(text, palette=PALETTE, *, kind="prose") -> list[dict]:
    return attribution_missing_flags([{"kind": kind, "text": text}], palette)


# -- the quote scan: masking (R2 P0-1) ---------------------------------------

#: The plan's own example (D5 / §3.5) — the wrapped line, attribute quotes and
#: all. The attribute's ``"color:#B03A48"`` is exactly what the raw scan used to
#: report as a second, phantom quote.
MARA = [PaletteEntry(member_id="cm_mara", display_name="Mara", hex="#B03A48")]
MARA_LINE = '<span style="color:#B03A48">"You\'re late."</span>'


def test_the_colour_attribute_is_never_a_quote_of_its_own():
    """R2 P0-1: mask the tags before scanning, or every wrapped line doubles."""
    assert extract_quotes(MARA_LINE) == ["You're late."]
    rows = parse_attribution(MARA_LINE, MARA)
    assert len(rows) == 1
    assert rows[0] == {
        "speaker": "Mara",
        "quote": "You're late.",
        "speaker_id": "cm_mara",
    }
    assert not [row for row in rows if "color" in row["quote"]]


def test_masking_keeps_the_offsets_of_the_original_prose():
    prefix = "She looks up. "
    prose = prefix + MARA_LINE
    span = scan_quotes(prose)[0]
    assert prose[span.start : span.end] == '"You\'re late."'
    assert span.text == "You're late."
    assert span.start == len(prefix) + len('<span style="color:#B03A48">')


def test_a_wrapped_curly_quote_is_scanned_too():
    prose = '<span style="color:#B03A48">“You\'re late.”</span>'
    assert extract_quotes(prose) == ["You're late."]
    assert _speakers(prose, MARA) == [("Mara", "You're late.")]


def test_extract_quotes_matches_the_frontend_span_scan():
    prose = 'He said, "One." Then: “Two.” And "   " is blank.'
    assert extract_quotes(prose) == ["One.", "Two."]
    assert extract_quotes("no speech here") == []


def test_scan_quotes_keeps_offsets_and_spans_multiple_lines():
    prose = 'Aria said, "first line\nsecond line," then left.'
    spans = scan_quotes(prose)
    assert len(spans) == 1
    span = spans[0]
    assert prose[span.start : span.end] == '"first line\nsecond line,"'
    assert span.text == "first line\nsecond line,"


# -- the parse matrix (D5) ---------------------------------------------------


def test_a_span_or_a_font_tag_both_attribute_the_line():
    prose = _wrapped(RED, '"One."') + " " + _wrapped(AMBER, '"Two."', tag="font")
    assert _speakers(prose) == [("Aria", "One."), ("Borin", "Two.")]


def test_hex_case_does_not_matter():
    prose = _wrapped(RED.upper(), '"One."')
    assert _speakers(prose) == [("Aria", "One.")]


def test_a_short_hex_expands_to_its_slot():
    palette = [PaletteEntry("cm_zed", "Zed", "#aa3344")]
    assert _speakers(_wrapped("#a34", '"One."'), palette) == [("Zed", "One.")]


def test_the_palette_label_matches_case_insensitively():
    entry = PaletteEntry("cm_aria", "Aria", RED, color_name(RED))
    assert entry.name == "red"
    for label in ("red", "Red", "RED"):
        assert _speakers(_wrapped(label, '"One."'), [entry]) == [("Aria", "One.")]


def test_a_css_colour_name_is_not_a_palette_label():
    """``crimson`` is a CSS colour (#dc143c) and equals no slot (D5)."""
    prose = _wrapped("crimson", '"One."')
    assert _speakers(prose) == []
    assert [flag["rule_id"] for flag in _flags(prose)] == ["attribution_missing"]


def test_rgb_and_rgba_values_resolve_to_the_slot():
    assert _speakers(_wrapped("rgb(224,108,117)", '"One."')) == [("Aria", "One.")]
    assert _speakers(_wrapped("rgb(224 108 117)", '"One."')) == [("Aria", "One.")]
    assert _speakers(_wrapped("rgba(224,108,117,0.4)", '"One."')) == [("Aria", "One.")]


def test_a_rounded_channel_still_matches_within_tolerance():
    """#e06c75 is (224,108,117); this triple is 15 away — inside the knob."""
    assert RGB_TOLERANCE == 16
    assert _speakers(_wrapped("rgb(232,112,120)", '"One."')) == [("Aria", "One.")]


def test_a_colour_outside_tolerance_resolves_to_nobody():
    prose = _wrapped("rgb(240,120,130)", '"One."')  # 41 away from every slot
    assert _speakers(prose) == []
    assert len(_flags(prose)) == 1


def test_attribute_spacing_and_case_variance_still_parse():
    prose = '<span   STYLE = " COLOR :  #E06C75 " >"One."</span>'
    assert _speakers(prose) == [("Aria", "One.")]
    prose = "<font  color='#e06c75' >\"One.\"</font>"
    assert _speakers(prose) == [("Aria", "One.")]


def test_a_value_within_tolerance_of_two_slots_is_ambiguous_and_omitted():
    """Two members whose hues are 11 apart: the value names neither (never merge)."""
    palette = [
        PaletteEntry("cm_a", "A", "#e06c75"),
        PaletteEntry("cm_b", "B", "#e06c80"),
    ]
    prose = _wrapped("#e06c75", '"One."')
    assert _speakers(prose, palette) == []
    assert len(_flags(prose, palette)) == 1


def test_the_innermost_span_is_the_speaker():
    prose = (
        '<span style="color:' + RED + '"><span style="color:' + AMBER + '">'
        '"One."</span></span>'
    )
    assert _speakers(prose) == [("Borin", "One.")]


def test_a_wrapped_line_is_attributed_as_written():
    """No cue, no verb, no name — the colour is the whole attribution."""
    prose = (
        "She does not look up from the counter. "
        + _wrapped(RED, '"You\'re late, and I ain\'t sorry."')
    )
    assert _speakers(prose) == [("Aria", "You're late, and I ain't sorry.")]


def test_an_empty_quote_is_not_a_row():
    assert _speakers(_wrapped(RED, '"   "')) == []


def test_no_palette_means_nobody_speaks():
    assert parse_attribution(_wrapped(RED, '"One."'), []) == []


def test_prose_without_markup_is_strictly_unattributed():
    """Q2 = B: a cue (or any other prose shape) is not a fallback."""
    prose = 'Aria: "One moment."\nAria said, "Well met."\n"Not tonight."'
    assert _speakers(prose) == []


def test_a_turn_with_no_dialogue_yields_no_rows_and_no_flags():
    """The clean turn is silent in BOTH outputs (nothing to attribute or flag)."""
    prose = "Rain drums on the roof. She turns the page and keeps reading."
    assert _speakers(prose) == []
    assert _flags(prose) == []


# -- the colour context is bounded (D5) --------------------------------------


def test_a_blank_line_closes_the_colour_context():
    prose = (
        '<span style="color:' + RED + '">"One."\n\n'
        '"Two."</span>'
    )
    assert _speakers(prose) == [("Aria", "One.")]
    flags = _flags(prose)
    assert [flag["evidence"] for flag in flags] == ["Two."]


def test_a_dangling_open_does_not_carry_into_the_next_segment():
    segments = [
        {"kind": "prose", "text": '<span style="color:' + RED + '">"One."'},
        {"kind": "prose", "text": '"Two."</span>'},
    ]
    flags = attribution_missing_flags(segments, PALETTE)
    assert [flag["segment_index"] for flag in flags] == [1]
    assert flags[0]["evidence"] == "Two."


# -- citations are not speech ------------------------------------------------


def test_a_quoted_brand_is_not_a_spoken_line():
    """W9.22 live false positive: ``a scent Hobbs calls "Spring Rain"``."""
    prose = (
        "Here, the air is a thick soup of gun oil and a cloying, artificial "
        'scent Hobbs calls "Spring Rain" that smells like floor cleaner.'
    )
    assert _speakers(prose) == []
    assert _flags(prose) == []


def test_a_quoted_label_beside_a_gesture_is_a_citation():
    prose = 'Aria taps a faded neon "OPEN" sign and shrugs.'
    assert _flags(prose) == []


def test_a_lower_case_speech_tag_after_a_quote_keeps_it_speech():
    """The citation guard must not eat real speech that carries no comma."""
    prose = 'She crossed the room. "Not tonight" she said.'
    flags = _flags(prose)
    assert [flag["evidence"] for flag in flags] == ["Not tonight"]


def test_a_wrapped_label_is_attributed_as_written():
    """A span the model wrote wins over the citation guard (D4)."""
    prose = (
        'the scent Hobbs calls '
        + _wrapped(RED, '"Spring Rain"')
        + " that smells like floor cleaner"
    )
    assert _speakers(prose) == [("Aria", "Spring Rain")]


# -- the missing flag (D3 / §3.3) --------------------------------------------


def _assert_flag_is_a_literal_prose_slice(flag: dict, text: str) -> None:
    anchor = flag["anchor"]
    assert "…" not in anchor or anchor.startswith("…") or anchor.endswith("…")
    assert anchor.strip("…") in text


def test_an_unwrapped_line_is_flagged_with_a_patchable_anchor():
    text = 'Mara leans on the counter. "You\'re late." She grins.'
    flag = _flags(text)[0]
    assert flag["rule_id"] == "attribution_missing"
    # Folded like prose_markup — NOT a Dress Rehearsal check.
    assert flag["check"] == "attribution"
    assert flag["severity"] == "warn"
    assert flag["evidence"] == "You're late."
    assert flag["segment_index"] == 0
    assert 'wrap the quoted line' in flag["fix"]
    # The fix names a REAL palette span, not a made-up hex.
    assert PALETTE[0].hex in flag["fix"]
    assert PALETTE[0].display_name in flag["fix"]
    _assert_flag_is_a_literal_prose_slice(flag, text)
    # The anchor is the exact prose to patch, so a find/replace clears the flag.
    assert flag["anchor"] in text


def test_the_anchor_covers_the_quote_and_clips_only_at_the_cut():
    text = (
        "She folds the cloth once, twice, and sets it down beside the register, "
        "then looks past you at the door and waits for the bell to stop ringing. "
        '"You\'re late." '
        "The rain keeps on against the window and nobody answers the bell, and "
        "the kettle starts to tick as it cools on the back of the stove."
    )
    flag = _flags(text)[0]
    assert '"You\'re late."' in flag["anchor"]
    assert flag["anchor"].startswith("…") and flag["anchor"].endswith("…")
    assert flag["anchor"].strip("…") in text


def test_one_flag_per_unwrapped_line_in_prose_order():
    text = '"One."\n\n"Two."\n\n"Three."'
    assert [flag["evidence"] for flag in _flags(text)] == ["One.", "Two.", "Three."]


def test_a_wrapped_line_is_not_flagged():
    assert _flags("She nods. " + _wrapped(RED, '"One."')) == []


def test_a_colour_outside_the_palette_is_flagged_not_silently_dropped():
    """A wrap in a colour no palette entry carries still leaves the line silent."""
    prose = _wrapped("#123456", '"One."')
    flags = _flags(prose)
    assert len(flags) == 1
    assert flags[0]["evidence"] == "One."


def test_only_prose_segments_are_scanned():
    segments = [
        {"kind": "dsl", "text": '"One."'},
        {"kind": "prose", "text": "She nods. " + _wrapped(RED, '"Two."')},
    ]
    assert attribution_missing_flags(segments, PALETTE) == []


def test_an_empty_palette_flags_nothing():
    assert _flags('"One."', []) == []


def test_the_flag_payload_is_json_safe():
    import json

    flag = _flags('"One."')[0]
    assert json.loads(json.dumps(flag)) == flag


# -- the unquoted flag (a colour span with no quote) -------------------------


def _unquoted(text, palette=PALETTE, *, kind="prose") -> list[dict]:
    return attribution_unquoted_flags([{"kind": kind, "text": text}], palette)


#: The owner-spotted hole: speech inside a colour span WITHOUT quotation marks.
UNQUOTED_PROSE = '<span style="color:' + RED + '">Get on or get out of the way.</span>'


def test_an_unquoted_line_in_a_colour_span_is_flagged_once():
    """No quote to anchor: ``attribution_missing`` is blind, this fold is not."""
    flags = _unquoted(UNQUOTED_PROSE)
    assert len(flags) == 1
    flag = flags[0]
    # Folded like prose_markup — NOT a Dress Rehearsal check.
    assert flag["rule_id"] == "attribution_unquoted"
    assert flag["check"] == "attribution"
    assert flag["severity"] == "warn"
    assert flag["evidence"] == "Get on or get out of the way."
    assert flag["segment_index"] == 0
    assert "quote the spoken line" in flag["message"]
    assert "remove the span" in flag["message"]
    # The anchor is the span's prose — a literal slice of the segment, so a
    # find/replace that quotes the line clears the flag.
    assert "Get on or get out of the way." in flag["anchor"]
    assert flag["anchor"].strip("…") in UNQUOTED_PROSE
    # Nothing was attributed: the parser has no quote to anchor to.
    assert parse_attribution(UNQUOTED_PROSE, PALETTE) == []
    # The missing fold cannot see it either — the hole this flag closes.
    assert attribution_missing_flags(
        [{"kind": "prose", "text": UNQUOTED_PROSE}], PALETTE
    ) == []


def test_the_unquoted_fix_names_the_colour_the_span_used():
    """The hue IS whose line it was: the fix must name it, not a generic span."""
    amber = '<span style="color:' + AMBER + '">Then take it.</span>'
    flag = _unquoted(amber)[0]
    entry = PALETTE[1]
    assert entry.display_name in flag["fix"]
    assert entry.name in flag["fix"] and entry.hex in flag["fix"]


def test_a_quoted_line_inside_a_span_is_not_flagged():
    assert _unquoted(_wrapped(RED, '"You\'re late."')) == []
    assert _unquoted('<span style="color:' + RED + '">“You’re late.”</span>') == []


def test_an_empty_or_whitespace_span_claims_nothing():
    assert _unquoted('<span style="color:' + RED + '"></span>') == []
    assert _unquoted('<span style="color:' + RED + '">   \n  </span>') == []


def test_mis_coloured_narration_is_flagged():
    """The same span shape appears when the model colours ordinary prose."""
    prose = (
        "Rain hammers the tin roof. "
        + '<span style="color:' + AMBER + '">She waits by the door.</span>'
    )
    flag = _unquoted(prose)[0]
    assert flag["evidence"] == "She waits by the door."
    assert PALETTE[1].hex in flag["fix"]


def test_one_flag_per_unquoted_span_in_prose_order():
    prose = (
        '<span style="color:' + RED + '">Get out.</span> She waits. '
        '<span style="color:' + AMBER + '">Then take it.</span>'
    )
    assert [flag["evidence"] for flag in _unquoted(prose)] == [
        "Get out.",
        "Then take it.",
    ]


def test_a_span_in_a_colour_outside_the_palette_is_not_flagged_here():
    """That colour is ``attribution_missing``'s business, not a span claim."""
    assert _unquoted(_wrapped("#123456", "She waits by the door.")) == []
    assert _unquoted(_wrapped("#123456", '"One."')) == []


def test_a_colourless_span_is_not_flagged():
    assert _unquoted('<span class="calm">She waits by the door.</span>') == []


def test_an_outer_span_around_a_quoted_inner_span_is_not_flagged():
    prose = (
        '<span style="color:' + RED + '"><span style="color:' + AMBER
        + '">"Then take it."</span></span>'
    )
    assert _unquoted(prose) == []


def test_only_prose_segments_are_scanned_for_unquoted_spans():
    segments = [
        {"kind": "dsl", "text": UNQUOTED_PROSE},
        {"kind": "prose", "text": UNQUOTED_PROSE},
    ]
    assert [
        flag["segment_index"]
        for flag in attribution_unquoted_flags(segments, PALETTE)
    ] == [1]


def test_an_empty_palette_flags_no_unquoted_span():
    assert _unquoted(UNQUOTED_PROSE, []) == []


def test_the_unquoted_flag_payload_is_json_safe():
    import json

    flag = _unquoted(UNQUOTED_PROSE)[0]
    assert json.loads(json.dumps(flag)) == flag


# -- the palette (D1 / D2) ---------------------------------------------------


def _seed_cards(log, *names):
    for name in names:
        log.append_event(
            SID, "system", "system", {"card": {"name": name, "description": name}}
        )


def _world_state(log, *venues):
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {},
            "venue": {name: {"venue": "tavern", "area": "bar"} for name in venues},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )


def test_palette_for_turn_mints_the_seed_cast_on_a_fresh_chat(log):
    """Turn 1: nothing has read the cast yet, so the palette must mint the seeds."""
    _seed_cards(log, "Aria", "Borin")
    entries = palette_for_turn(log, SID)
    assert [entry.display_name for entry in entries] == ["Aria", "Borin"]
    assert [entry.hex for entry in entries] == [RED, AMBER]
    assert [entry.name for entry in entries] == ["red", "amber"]
    assert all(entry.member_id.startswith("cm_") for entry in entries)


def test_palette_for_turn_is_the_cast_even_when_the_ladder_answers_none(log):
    """The palette describes WHO CAN SPEAK, not who the ladder saw this instant.

    A post-turn ``world_state`` that names nobody (the live shape: only the
    player is tracked) leaves the ladder at ``"none"`` — the palette still holds
    the cast, because the narrator needs their hues regardless.
    """
    _seed_cards(log, "Aria", "Borin")
    _world_state(log, "the user")
    entries = palette_for_turn(log, SID)
    assert [entry.display_name for entry in entries] == ["Aria", "Borin"]


def test_palette_for_turn_includes_a_member_the_ladder_reports(log):
    """The roster and the presence ladder are UNIONED, deduped, roster ordered."""
    _seed_cards(log, "Aria")
    admit_member(log, SID, "Borin", provenance="world", admitted_by="test")
    _world_state(log, "Borin")
    entries = palette_for_turn(log, SID)
    assert [entry.display_name for entry in entries] == ["Aria", "Borin"]
    assert len({entry.member_id for entry in entries}) == 2


def test_palette_for_turn_excludes_the_player_member(log):
    """User-as-Actor: the ladder keeps the player; the narrator quotes the cast."""
    _seed_cards(log, "Aria")
    admit_member(log, SID, "Lyra Voss", provenance="user", admitted_by="test")
    _world_state(log, "Lyra Voss")  # the ladder sees ONLY the player
    entries = palette_for_turn(log, SID)
    assert [entry.display_name for entry in entries] == ["Aria"]


def test_palette_for_turn_is_empty_with_an_empty_roster(log):
    """No cast, no cards, no palette — and no crash."""
    _world_state(log, "the user")
    assert palette_for_turn(log, SID) == []


def test_palette_for_turn_is_capped_at_the_palette_size(log):
    """12 speakers, 10 hues: the first 10 in roster order, never the tail."""
    names = [f"Speaker {index}" for index in range(12)]
    _seed_cards(log, *names)
    entries = palette_for_turn(log, SID)
    assert [entry.display_name for entry in entries] == names[: len(SPEAKER_PALETTE)]
    assert [entry.hex for entry in entries] == list(SPEAKER_PALETTE)


def test_palette_for_turn_colours_are_idempotent(log):
    _seed_cards(log, "Aria", "Borin")
    first = palette_for_turn(log, SID)
    second = palette_for_turn(log, SID)
    assert first == second
    colour_rows = [
        event
        for event in log.get_events(SID)
        if isinstance(event.payload, dict)
        and event.payload.get("event") == "speaker_color"
    ]
    # One mint per member, never a second on the next turn.
    assert len(colour_rows) == 2
    assert len({row.payload["member_id"] for row in colour_rows}) == 2


def test_attribution_resolves_the_live_multi_speaker_turn(log):
    """REGRESSION (live W9.28 defect): perfect spans, zero rows, zero flags.

    The live shape: both characters admitted to the cast, the narrator wraps each
    line in the hue it was taught (``#e06c75`` for Mara, ``#e5c07b`` for Sable),
    and the turn's own post-turn ``world_state`` names only ``the user``. The
    ladder answered ``"none"`` for that evidence, so ``palette_for_turn`` used to
    return ``[]`` and ``parse_attribution`` reported nothing — no rows and no
    ``attribution_missing`` flags either.
    """
    admit_member(log, SID, "Mara", provenance="card", admitted_by="system")
    admit_member(log, SID, "Sable", provenance="card", admitted_by="system")
    _world_state(log, "the user")  # post-turn: only the player is tracked

    entries = palette_for_turn(log, SID)
    assert [(entry.display_name, entry.hex) for entry in entries] == [
        ("Mara", RED),
        ("Sable", AMBER),
    ]

    prose = (
        "Mara glances at the door. "
        + _wrapped(RED, '"It\'s right here."')
        + " Sable does not look up. "
        + _wrapped(AMBER, '"Then take it."')
    )
    rows = parse_attribution(prose, entries)
    assert [(row["speaker"], row["quote"]) for row in rows] == [
        ("Mara", "It's right here."),
        ("Sable", "Then take it."),
    ]
    assert [row["speaker_id"] for row in rows] == [
        entry.member_id for entry in entries
    ]
    # The other half of the live miss: an UNWRAPPED line is flagged instead of
    # dropping silently — which needs the same non-empty palette.
    flags = attribution_missing_flags(
        [{"kind": "prose", "text": prose + ' "And close the door."'}], entries
    )
    assert [flag["evidence"] for flag in flags] == ["And close the door."]


def test_palette_for_turn_reads_the_registry_once(log, monkeypatch):
    """R2 P2: the hexes come from ONE fold read, not one per member."""
    _seed_cards(log, "Aria", "Borin", "Cara")
    palette_for_turn(log, SID)  # mint the hues

    import fabulist.state.attribution as attribution

    calls: list[str] = []
    original = attribution.list_member_colors

    def counting(log_arg, session_id):
        calls.append(session_id)
        return original(log_arg, session_id)

    monkeypatch.setattr(attribution, "list_member_colors", counting)
    entries = palette_for_turn(log, SID)
    assert len(entries) == 3
    assert calls == [SID]


def test_palette_for_turn_names_only_the_engine_hues():
    assert color_name(RED) == "red"
    assert color_name(RED.upper()) == "red"
    assert color_name(SPEAKER_PALETTE[-1]) == SPEAKER_PALETTE_NAMES[-1]
    assert color_name("#123456") is None
    assert color_name("not-a-colour") is None
    assert normalize_hex_color("#A34") == "#aa3344"
    assert normalize_hex_color("nope") is None
    assert len(SPEAKER_PALETTE_NAMES) == len(SPEAKER_PALETTE)


# -- the post-turn seam (``build_engine`` wires this, W4) ---------------------


def _palette_fn(log, session_id=SID):
    return lambda: palette_for_turn(log, session_id)


def test_runner_parses_the_markup_the_model_wrote(log):
    _seed_cards(log, "Aria", "Borin")
    runner = MarkupAttributionRunner(_palette_fn(log))
    rows = run(
        runner(
            "u",
            "She looks up. "
            + _wrapped(RED, '"Well met."')
            + " "
            + _wrapped(AMBER, '"Aye."'),
        )
    )
    assert [(row["speaker"], row["quote"]) for row in rows] == [
        ("Aria", "Well met."),
        ("Borin", "Aye."),
    ]
    assert all(row["speaker_id"].startswith("cm_") for row in rows)


def test_runner_reads_the_palette_once_per_call(log):
    """A member admitted mid-turn is attributable on the very next call."""
    _seed_cards(log, "Aria")
    calls: list[int] = []

    def palette_fn():
        calls.append(1)
        return palette_for_turn(log, SID)

    runner = MarkupAttributionRunner(palette_fn)
    first = run(runner("u", _wrapped(RED, '"One."')))
    assert [row["speaker"] for row in first] == ["Aria"]
    assert calls == [1]


def test_runner_user_message_is_not_an_input(log):
    _seed_cards(log, "Aria")
    runner = MarkupAttributionRunner(_palette_fn(log))
    rows = run(runner('Lyra says: "I do."', _wrapped(RED, '"Well met."')))
    assert [(row["speaker"], row["quote"]) for row in rows] == [
        ("Aria", "Well met.")
    ]


def test_runner_without_a_palette_seam_attributes_nothing():
    assert run(MarkupAttributionRunner()("u", _wrapped(RED, '"One."'))) == []


def test_runner_lets_a_palette_failure_through(log):
    """The node owns the failure event — the seam must not swallow it."""

    def boom():
        raise RuntimeError("palette unreadable")

    try:
        run(MarkupAttributionRunner(boom)("u", _wrapped(RED, '"One."')))
    except RuntimeError as exc:
        assert "palette unreadable" in str(exc)
    else:  # pragma: no cover - the seam is documented to propagate
        raise AssertionError("a raising palette read must reach the node")


# -- the retired detector (R2 P0-2's deferral expired in W4) -----------------


def test_the_retired_detector_shims_are_deleted():
    """W4 swaps the wiring seam, so the shims are GONE — importing them fails."""
    import fabulist.state.attribution as attribution

    assert not hasattr(attribution, "SpeakerIndex")
    assert not hasattr(attribution, "AttributionRunner")
    with pytest.raises(ImportError):
        from fabulist.state.attribution import SpeakerIndex  # noqa: F401


# -- the live-turn regression (W9.28 §5.3) -----------------------------------

#: A live turn whose prose carried dialogue and whose attribution row came back
#: EMPTY — "silent, not wrong", the defect strict omission replaces with a flag
#: the repair loop can act on. Synthetic stand-in for that turn: same shape (one
#: unwrapped spoken line, two inline citations), invented setting and cast.
LIVE_PROSE = """The transition from the polished, white-lit order of a harbour jewellers to the back room of Hobbs & Sons is a physical jolt. Here, the air is a thick soup of lamp oil, wet rope, and a thin chemical cleaner Hobbs calls "Night Rain" that smells more like a laundromat floor. Dust drifts in the grey light that comes through glass clouded by a decade of salt. A faded neon "OPEN" sign buzzes with a dying stutter, throwing a jittery pink glow over crates of mismatched tools and brass fittings.
Hobbs leans heavily on the glass counter, tapping one thick claw rhythmically against the surface and squinting at you over his spectacles. He looks like he hasn't slept since the spring, his oilskin still beaded from the quay. He doesn't offer a hand; he just flicks two fingers toward a tarnished pocket watch lying on a velvet pad.

"Evening. You're late, or I'm early, and I've never once been early in my life. Come round here and tell me whether this watch is worth keeping or whether it is brass and glass."

A sharp buzz cuts through the quiet as your slate rattles in your coat.
Hobbs's tapping stops. He hasn't looked at the slate, but he's watching your face, waiting for you to either pick up the watch or keep ignoring him."""

LIVE_PALETTE = _palette(("cm_hobbs", "Hobbs", 0), ("cm_tessa", "Tessa", 1))


def test_the_live_untagged_turn_flags_its_line_and_drops_its_citations():
    """The engine is no longer silent: the miss is reported, the citations are not."""
    assert parse_attribution(LIVE_PROSE, LIVE_PALETTE) == []
    flags = attribution_missing_flags(
        [{"kind": "prose", "text": LIVE_PROSE}], LIVE_PALETTE
    )
    assert len(flags) == 1
    assert flags[0]["evidence"].startswith("Evening. You're late,")
    # The cleaner's brand and the sign are citations, not speech: neither is flagged.
    assert "Night Rain" not in flags[0]["evidence"]
    assert "OPEN" not in flags[0]["evidence"]


def test_the_live_turn_attributes_once_the_line_is_wrapped():
    """The loop's fix: wrapping the line (only) clears the flag and lands a row."""
    wrapped = LIVE_PROSE.replace(
        "\"Evening.",
        '<span style="color:' + RED + '">"Evening.',
    ).replace(
        "brass and glass.\"",
        "brass and glass.\"</span>",
    )
    assert attribution_missing_flags(
        [{"kind": "prose", "text": wrapped}], LIVE_PALETTE
    ) == []
    rows = parse_attribution(wrapped, LIVE_PALETTE)
    assert [row["speaker"] for row in rows] == ["Hobbs"]
    assert rows[0]["quote"].startswith("Evening. You're late,")


# -- the persisted payload ---------------------------------------------------


def test_attribution_payloads_still_pass_the_schema_gate():
    from fabulist.log.schema_gate import validate_event

    assert (
        validate_event(
            "system",
            {
                "event": "attribution",
                "attributions": [{"speaker": "A", "quote": "q"}],
            },
        )
        == []
    )
    assert (
        validate_event(
            "system", {"event": "attribution_failed", "stage": "parse", "error": "x"}
        )
        == []
    )
    assert validate_event("system", {"event": "attribution"}) != []
