"""Character-card PNG write path (SURFACE #13, D28).

The import half of the card surface lives in :mod:`fabulist.cards.v2`, which
*walks* a PNG chunk stream rather than trusting an image library (a CRC-failing
card is corrupt and must not import). This module is the writer's half of that
same discipline: it re-frames every chunk with a freshly computed CRC, validates
the base by reusing the reader's :func:`~fabulist.cards.v2._iter_chunks`, and
only ever edits the ``chara`` text chunk — pixel data (``IDAT``) and palette
(``PLTE``) pass through byte-for-byte.

SillyTavern stores a card as a ``tEXt`` chunk whose keyword is ``chara`` and
whose text is base64-encoded UTF-8 JSON. ``zTXt`` is the compressed sibling of
``tEXt``; both carry the keyword before a NUL, so an existing compressed
``chara`` chunk is removed too (only ``tEXt`` is written back, matching the
reader, which only reads ``tEXt``). When no base image is supplied a minimal
1x1 PNG carrier is synthesized so a card can always be exported as an image.

Shaping only, no I/O: bytes in, bytes out. It never touches the event log (D9).
"""

from __future__ import annotations

import base64
import json
import struct
import zlib

from .v2 import CHARA_KEYWORD, PNG_SIGNATURE, CardError, _iter_chunks

__all__ = ["encode_chara_png", "minimal_png"]

#: Chunk types whose keyword precedes a NUL and can carry the card text.
_CHARA_TEXT_CHUNKS = (b"tEXt", b"zTXt")


def _chunk(ctype: bytes, data: bytes) -> bytes:
    """Frame ``data`` as a PNG chunk with a freshly computed CRC32.

    Mirrors the reader: ``crc32(type + data)`` masked to 32 bits, big-endian.
    """
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _keyword(data: bytes) -> bytes:
    """The keyword that precedes the NUL separator of a ``tEXt``/``zTXt`` chunk."""
    nul = data.find(b"\x00")
    return data if nul == -1 else data[:nul]


def minimal_png() -> bytes:
    """A minimal, structurally valid 1x1 RGB PNG with no ``chara`` chunk.

    The zero-image carrier used when a card has no stored image to rewrite, so
    PNG export always produces a real image file.
    """
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00\x00\x00\x00"  # filter byte 0 + one black RGB pixel
    idat = _chunk(b"IDAT", zlib.compress(raw))
    return PNG_SIGNATURE + ihdr + idat + _chunk(b"IEND", b"")


def encode_chara_png(envelope: dict, base_png: bytes | None = None) -> bytes:
    """Return ``base_png`` carrying ``envelope`` as its ``chara`` tEXt chunk.

    ``envelope`` is the full ``{"spec", "spec_version", "data"}`` object (see
    :func:`fabulist.cards.export.card_to_v2`); it is base64-encoded UTF-8 JSON,
    matching what the reader decodes. Any existing ``tEXt``/``zTXt`` chunk whose
    keyword is ``chara`` is dropped and exactly one new ``tEXt`` chunk is
    inserted immediately before ``IEND``. Every emitted chunk gets a fresh CRC;
    ``IDAT``/``PLTE`` bytes are copied unchanged.

    When ``base_png`` is ``None`` a :func:`minimal_png` carrier is used. Raises
    :class:`~fabulist.cards.v2.CardError` when ``base_png`` is not a valid,
    complete PNG (missing signature/``IEND`` or a bad chunk CRC) — the same
    strictness the reader applies.
    """
    base = minimal_png() if base_png is None else base_png
    text = CHARA_KEYWORD + b"\x00" + base64.b64encode(
        json.dumps(envelope).encode("utf-8")
    )
    chara_chunk = _chunk(b"tEXt", text)

    parts = [PNG_SIGNATURE]
    saw_iend = False
    for ctype, data in _iter_chunks(base):
        if ctype in _CHARA_TEXT_CHUNKS and _keyword(data) == CHARA_KEYWORD:
            continue  # replace: drop the old card text chunk
        if ctype == b"IEND":
            parts.append(chara_chunk)
            saw_iend = True
        parts.append(_chunk(ctype, data))
    if not saw_iend:
        raise CardError("base PNG has no IEND chunk")
    return b"".join(parts)
