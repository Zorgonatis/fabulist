"""The per-turn SPEAKER PALETTE block for the narrator's dynamic tail (W9.28).

The block hands the model the colours the engine will parse the spoken lines back
out of, so it renders ONE line per speaking cast member plus the one-line
directive that says what to do with them::

    SPEAKER PALETTE — wrap each spoken line in the speaking character's colour:
    - Mara: red #e06c75
    - Innkeeper: amber #e5c07b

Both spellings ride each line because both parse (the label by case-insensitive
equality with :attr:`PaletteEntry.name`, the hex by value): one line the model
can copy either way. A user-picked hex with no palette label renders hex-only.

This rides the **dynamic tail** — appended as the last system message of the
turn, after the lane block — and never the stable prefix: the prefix must stay
byte-identical turn to turn for prompt caching (AGENTS #6 / D7), while the
speaking cast changes as its members are admitted. The palette is
:func:`fabulist.state.attribution.palette_for_turn`'s output in roster order, so
the block is deterministic turn to turn.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.state.attribution import PaletteEntry

__all__ = [
    "PALETTE_DIRECTIVE",
    "render_palette_block",
]

#: The one-line directive that opens the block — the colour IS the attribution.
PALETTE_DIRECTIVE = (
    "SPEAKER PALETTE — wrap each spoken line in the speaking character's colour:"
)

#: Closing steer (owner 2026-10-21): the palette is an attribution dictionary, not
#: an invitation — a listed character may be off-screen this turn.
PALETTE_PRESENCE_NOTE = (
    "Colour only lines spoken by characters in the scene; a listed character may be absent."
)


def _member_line(entry: "PaletteEntry") -> str:
    """``- Mara: red #e06c75`` (or ``- Mara: #b03a48`` for a label-less hue)."""
    colour = f"{entry.name} {entry.hex}" if entry.name else entry.hex
    return f"- {entry.display_name}: {colour}"


def render_palette_block(palette: Sequence["PaletteEntry"]) -> str:
    """Render the palette tail block; an empty palette renders as ``""``.

    Empty is the honest answer for a turn with no active cast member: the
    caller appends nothing rather than a directive pointing at no colours.
    """
    if not palette:
        return ""
    return "\n".join([PALETTE_DIRECTIVE, *(_member_line(entry) for entry in palette), PALETTE_PRESENCE_NOTE])
