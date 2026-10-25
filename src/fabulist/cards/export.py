"""Character Card V2 export (SURFACE #13).

The import half of the card surface lives in :mod:`fabulist.cards.v2`: it turns
PNG ``tEXt``/``chara`` bytes or raw JSON into a :class:`~fabulist.cards.CardV2`.
This module is the inverse shaping step — it renders an in-memory card dict back
into the on-the-wire V2 envelope::

    {"spec": "chara_card_v2", "spec_version": "2.0", "data": {...}}

SillyTavern's V2 spec tolerates missing optional fields, so the exporter is
deliberately sparse: a field is emitted only when the source carries it
*non-empty*. A card that never had a ``scenario`` round-trips without a
``scenario`` key rather than a ``null``/``""`` placeholder. ``name`` is the one
mandatory field — identity is meaningless without it — so it always appears,
falling back to ``"Unknown"`` when the source omits or blanks it.

When the source card carries ``raw_data`` (the verbatim unwrapped V2 ``data``
object recorded at import, D28), the exporter is a *raw passthrough*: it emits a
copy of that object rather than re-assembling fields, so unknown top-level keys
and their order survive. Field assembly is the fallback for cards with no
``raw_data`` (V1/flat cards and sessions seeded before D28).

:func:`card_to_png` closes the loop to an image file: it renders the card
through :func:`card_to_v2` and writes it into a PNG's ``chara`` chunk via
:mod:`fabulist.cards.png`, optionally rewrapping the session's stored card
image.

Shaping only, no I/O: this module never reads or writes the event log (D9) and
never mutates the dict it is handed.
"""

from __future__ import annotations

import copy
from typing import Any

from .png import encode_chara_png

__all__ = ["CARD_SPEC_VERSION", "card_to_png", "card_to_v2"]

#: Value of the ``spec`` field in the V2 outer envelope (mirrors v2 import).
CARD_SPEC = "chara_card_v2"

#: Value of the ``spec_version`` field in the V2 outer envelope.
CARD_SPEC_VERSION = "2.0"

#: Fallback used when a source card has no usable ``name``.
DEFAULT_NAME = "Unknown"

#: Export field order, matching the Character Card V2 ``data`` object. Each key
#: is copied from the source card only when the source value is non-empty.
_DATA_FIELDS = (
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "creator_notes",
    "system_prompt",
    "post_history_instructions",
    "tags",
    "character_book",
    "alternate_greetings",
    "creator",
    "character_version",
    "extensions",
)


def _is_present(value: Any) -> bool:
    """Whether ``value`` should be exported (i.e. is not empty).

    ``None`` and blank strings are absent; empty lists, tuples, sets and dicts
    are absent. Any other value — including ``0`` and ``False``, which are
    legitimate payloads in a raw ``character_book`` — counts as present.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value) > 0
    return True


def card_to_v2(card: dict) -> dict:
    """Render ``card`` as a Character Card V2 export dict.

    Returns ``{"spec": "chara_card_v2", "spec_version": "2.0", "data": {...}}``.
    When the source carries a non-empty ``raw_data`` object the data half is a
    verbatim copy of it (unknown keys and key order preserved); otherwise every
    optional field is copied only when the source carries it non-empty. ``name``
    is always present either way, defaulting to ``"Unknown"``. The input dict is
    never mutated and the returned structure shares no mutable state with it.
    """
    source = card if isinstance(card, dict) else {}

    raw = source.get("raw_data")
    if isinstance(raw, dict) and raw:
        data = copy.deepcopy(raw)
        if not _is_present(data.get("name")):
            data = {"name": DEFAULT_NAME, **data}
    else:
        data = _assemble_data(source)

    return {
        "spec": CARD_SPEC,
        "spec_version": CARD_SPEC_VERSION,
        "data": data,
    }


def _assemble_data(source: dict) -> dict[str, Any]:
    """Field-assemble the V2 ``data`` object for a card with no ``raw_data``.

    ``name`` leads and is the only always-present key; every other field is
    copied only when non-empty (the sparse rule).
    """
    name = source.get("name")
    data: dict[str, Any] = {
        "name": copy.deepcopy(name) if _is_present(name) else DEFAULT_NAME
    }

    for field in _DATA_FIELDS:
        if field == "name":
            continue
        value = source.get(field)
        if not _is_present(value):
            continue
        data[field] = copy.deepcopy(value)

    return data


def card_to_png(card: dict, base_png: bytes | None = None) -> bytes:
    """Render ``card`` as a PNG carrying it in the ``chara`` tEXt chunk.

    The card is first shaped into the V2 envelope by :func:`card_to_v2`, then
    written into ``base_png`` (the session's stored card image, if any) by
    :func:`fabulist.cards.png.encode_chara_png`. When ``base_png`` is ``None``
    a synthesized minimal carrier PNG is used. Pixel data is never touched.
    """
    return encode_chara_png(card_to_v2(card), base_png)
