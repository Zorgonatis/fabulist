"""Build the ``chara``-carrying PNG fixtures from their JSON sources.

The PNGs are checked in so tests exercise a real on-disk chunk stream
(signature / IHDR / IDAT / tEXt / IEND, CRC and all) rather than a library's
happy path. They are built here with ``struct`` + ``zlib`` + ``base64`` — never
the package's own writer — so the fixture is an independent oracle, not
code-under-test round-tripping itself. Reproduce byte-for-byte with::

    uv run python tests/fixtures/cards/make_png_fixtures.py

Each PNG is a 2x2 RGB image whose ``chara`` tEXt chunk holds the JSON envelope
from the sibling ``*.json`` file (base64 UTF-8, exactly what SillyTavern
writes).
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: JSON envelope -> PNG fixture name pairs.
_SOURCES = {
    "full_v2.json": "full_v2.png",
    "minimal.json": "minimal.png",
}


def _chunk(ctype: bytes, data: bytes) -> bytes:
    """Frame ``data`` as a PNG chunk with a fresh CRC32."""
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _png(envelope: dict) -> bytes:
    """A 2x2 RGB PNG carrying ``envelope`` as its ``chara`` tEXt chunk."""
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    raw = b"\x00" + b"\x20\x40\x60" * 2 + b"\x00" + b"\x80\xa0\xc0" * 2
    idat = _chunk(b"IDAT", zlib.compress(raw))
    payload = base64.b64encode(json.dumps(envelope).encode("utf-8"))
    text = _chunk(b"tEXt", b"chara\x00" + payload)
    return PNG_SIGNATURE + ihdr + idat + text + _chunk(b"IEND", b"")


def main() -> None:
    for source, target in _SOURCES.items():
        envelope = json.loads((HERE / source).read_text(encoding="utf-8"))
        (HERE / target).write_bytes(_png(envelope))
        print(f"wrote {target}")


if __name__ == "__main__":
    main()
