"""Character Card V2 import + export (PLAN W2.10, D28).

Parses SillyTavern-format character cards — PNG ``tEXt``/``chara`` or raw
JSON — into a :class:`CardV2` (lossless: every V2 field plus ``raw_data``), and
shapes an imported card back out as the V2 JSON envelope or a ``chara`` PNG.
See :mod:`fabulist.cards.v2` and :mod:`fabulist.cards.export`.
"""

from .export import card_to_png, card_to_v2
from .v2 import (
    CARD_SPEC,
    PNG_SIGNATURE,
    CardError,
    CardV2,
    parse_card_v2,
    to_session_seed,
)

__all__ = [
    "CARD_SPEC",
    "PNG_SIGNATURE",
    "CardError",
    "CardV2",
    "card_to_png",
    "card_to_v2",
    "parse_card_v2",
    "to_session_seed",
]
