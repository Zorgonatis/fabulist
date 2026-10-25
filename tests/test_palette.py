"""Tests for the SPEAKER PALETTE tail block (W9.28 W2).

The block is the model's copy of the colours :func:`palette_for_turn` hands it,
so its shape is a contract: the one-line directive, then one line per member in
the palette's own order, each carrying the display name plus the label and/or
hex — and nothing else (the member id is an engine join key, never model-facing).
"""

from __future__ import annotations

from fabulist.prompts.palette import (
    PALETTE_DIRECTIVE,
    PALETTE_PRESENCE_NOTE,
    render_palette_block,
)
from fabulist.state.attribution import PaletteEntry


def _entry(display_name: str, hex: str, name: str | None, member_id: str) -> PaletteEntry:
    return PaletteEntry(member_id=member_id, display_name=display_name, hex=hex, name=name)


def test_block_opens_with_the_one_line_directive():
    block = render_palette_block([_entry("Mara", "#e06c75", "red", "mara")])
    first_line = block.splitlines()[0]
    assert first_line == PALETTE_DIRECTIVE
    # The directive says what to do, in the owner's D1 copy.
    assert "wrap each spoken line" in first_line
    assert first_line.endswith(":")


def test_block_renders_one_line_per_member_in_palette_order():
    palette = [
        _entry("Mara", "#e06c75", "red", "mara"),
        _entry("Innkeeper", "#e5c07b", "amber", "innkeeper"),
        _entry("Sable", "#98c379", "green", "sable"),
    ]
    block = render_palette_block(palette)
    assert block.splitlines() == [
        PALETTE_DIRECTIVE,
        "- Mara: red #e06c75",
        "- Innkeeper: amber #e5c07b",
        "- Sable: green #98c379",
        PALETTE_PRESENCE_NOTE,
    ]


def test_block_follows_the_given_order_not_a_sort():
    forward = [_entry("Mara", "#e06c75", "red", "mara"), _entry("Ash", "#e5c07b", "amber", "ash")]
    reversed_palette = list(reversed(forward))
    assert render_palette_block(forward).splitlines()[1:-1] == [
        "- Mara: red #e06c75",
        "- Ash: amber #e5c07b",
    ]
    assert render_palette_block(reversed_palette).splitlines()[1:-1] == [
        "- Ash: amber #e5c07b",
        "- Mara: red #e06c75",
    ]


def test_labelless_hex_renders_hex_only():
    # A user-picked hex has no palette label (color_name -> None): hex-only.
    block = render_palette_block([_entry("Mara", "#b03a48", None, "mara")])
    assert block.splitlines() == [PALETTE_DIRECTIVE, "- Mara: #b03a48", PALETTE_PRESENCE_NOTE]


def test_block_carries_no_field_beyond_name_label_and_hex():
    # member_id is the engine's join key, not model-facing copy; the block must
    # not teach the model an id it can write into prose.
    block = render_palette_block([_entry("Mara", "#e06c75", "red", "member-abc123")])
    assert "member-abc123" not in block
    assert block.splitlines()[1:-1] == ["- Mara: red #e06c75"]
    # The closing steer is the only non-member line (owner 2026-10-21).
    assert block.splitlines()[-1] == PALETTE_PRESENCE_NOTE


def test_empty_palette_renders_no_block():
    assert render_palette_block([]) == ""
