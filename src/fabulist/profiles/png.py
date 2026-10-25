"""Profile-as-PNG carrier (owner ruling #7).

A saved profile exports as JSON (``GET /api/profiles/export``) *and* as a PNG:
the PNG body is the profile's **assigned image** and the full envelope JSON
rides in one ``tEXt`` chunk whose keyword is :data:`PROFILE_CHUNK_KEYWORD`
(``fabulist_profile``) — the same keyword + NUL + base64-UTF-8-JSON convention
the card writer uses for ``chara`` (:mod:`fabulist.cards.png`), with a
different keyword so a profile PNG is never mistaken for a character card.
Import accepts either representation.

Chunk framing is deliberately **not** re-implemented here. The CRC framing
(``_chunk``), the keyword reader (``_keyword``) and the strict, CRC-validating
chunk walk (``_iter_chunks``) are imported from the cards package — one writer
for both text-chunk carriers, so a chunk this module emits is byte-identical in
shape to a card's, and a corrupt base image is rejected with the same
strictness the card reader applies (:class:`~fabulist.cards.v2.CardError`).

There is no image library in this project and no transcode path by design: the
assigned image is written into as-is (whatever mime it was uploaded with), so
the caller must hand this module a real PNG (see
:func:`fabulist.profiles.image.assigned_image` and the export route, which
answer ``422`` for a non-PNG assigned image rather than converting it).

Bytes in, bytes out, no I/O: this module never touches the event log (D9).
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from fabulist.cards.png import _chunk, _keyword
from fabulist.cards.v2 import PNG_SIGNATURE, CardError, _iter_chunks

__all__ = [
    "PROFILE_CHUNK_KEYWORD",
    "ProfilePngError",
    "profile_from_png",
    "profile_image_png",
    "profile_to_png",
]

#: ``tEXt`` keyword under which a profile PNG carries the base64 envelope JSON.
PROFILE_CHUNK_KEYWORD = b"fabulist_profile"

#: Text chunk types whose keyword precedes a NUL (the writer emits only ``tEXt``).
_TEXT_CHUNKS = (b"tEXt", b"zTXt")


class ProfilePngError(ValueError):
    """A profile PNG could not be read (missing chunk, corrupt bytes, bad JSON)."""


def profile_to_png(envelope: dict, base_png: bytes) -> bytes:
    """Return ``base_png`` carrying ``envelope`` as its ``fabulist_profile`` chunk.

    ``envelope`` is the full profile dict (see
    :func:`fabulist.profiles.exporter.export_profile`); it is base64-encoded
    UTF-8 JSON, matching what :func:`profile_from_png` decodes. Any existing
    ``tEXt``/``zTXt`` chunk whose keyword is :data:`PROFILE_CHUNK_KEYWORD` is
    dropped and exactly one new ``tEXt`` chunk is inserted immediately before
    ``IEND`` — a ``chara`` chunk (a card body) is left untouched. Every emitted
    chunk gets a fresh CRC; ``IDAT``/``PLTE`` bytes are copied unchanged, so the
    PNG body stays the assigned image.

    Raises :class:`~fabulist.cards.v2.CardError` when ``base_png`` is not a
    valid, complete PNG (missing signature/``IEND`` or a bad chunk CRC) — the
    same strictness the reader applies.
    """
    text = PROFILE_CHUNK_KEYWORD + b"\x00" + base64.b64encode(
        json.dumps(envelope).encode("utf-8")
    )
    profile_chunk = _chunk(b"tEXt", text)

    parts = [PNG_SIGNATURE]
    saw_iend = False
    for ctype, data in _iter_chunks(base_png):
        if ctype in _TEXT_CHUNKS and _keyword(data) == PROFILE_CHUNK_KEYWORD:
            continue  # replace: drop the old profile text chunk
        if ctype == b"IEND":
            parts.append(profile_chunk)
            saw_iend = True
        parts.append(_chunk(ctype, data))
    if not saw_iend:
        raise CardError("base PNG has no IEND chunk")
    return b"".join(parts)


def profile_image_png(data: bytes) -> bytes:
    """The profile PNG's image body: every chunk except the profile text chunk.

    The exported file is the assigned image **plus** the ``fabulist_profile``
    envelope chunk, so stripping that chunk recovers the exact bytes the source
    log stored as the image. Those bytes hash to the content address the
    envelope's ``image_asset`` ref already carries, which is what lets an import
    store the artwork under the original ref (and keeps a later JSON re-import of
    the same envelope idempotent). Reframing every remaining chunk with a fresh
    CRC reproduces the source image's bytes exactly (its CRCs were valid, since
    :func:`profile_from_png` already validated them).

    Raises :class:`~fabulist.cards.v2.CardError` when ``data`` is not a valid,
    complete PNG (missing signature/``IEND`` or a bad chunk CRC) — the same
    strictness :func:`profile_to_png` applies.
    """
    parts = [PNG_SIGNATURE]
    saw_iend = False
    for ctype, chunk_data in _iter_chunks(data):
        if ctype in _TEXT_CHUNKS and _keyword(chunk_data) == PROFILE_CHUNK_KEYWORD:
            continue  # drop the envelope chunk; keep the image
        if ctype == b"IEND":
            saw_iend = True
        parts.append(_chunk(ctype, chunk_data))
    if not saw_iend:
        raise CardError("base PNG has no IEND chunk")
    return b"".join(parts)


def profile_from_png(data: bytes) -> dict[str, Any]:
    """Extract the profile envelope dict from a profile PNG.

    The first ``tEXt`` chunk whose keyword is :data:`PROFILE_CHUNK_KEYWORD` wins
    (the writer emits exactly one; mirrored from the card reader's first-match
    rule). Raises :class:`ProfilePngError` — a :class:`ValueError`, so the API
    maps it to ``422`` — when the bytes are not a PNG, the file is corrupt
    (truncated / bad CRC / no ``IEND``; reported via
    :class:`~fabulist.cards.v2.CardError`), the chunk is absent, or its payload
    is not base64 UTF-8 JSON naming an object.
    """
    if not data.startswith(PNG_SIGNATURE):
        raise ProfilePngError("not a PNG: missing the 8-byte PNG signature")

    text: bytes | None = None
    try:
        for ctype, chunk_data in _iter_chunks(data):
            if ctype not in _TEXT_CHUNKS or _keyword(chunk_data) != PROFILE_CHUNK_KEYWORD:
                continue
            text = chunk_data.split(b"\x00", 1)[1] if b"\x00" in chunk_data else b""
            break
    except CardError as exc:
        raise ProfilePngError(f"unreadable PNG: {exc}") from exc

    if text is None:
        raise ProfilePngError(
            "PNG carries no 'fabulist_profile' tEXt chunk "
            "(not a profile exported by this app?)"
        )

    try:
        decoded = base64.b64decode(b"".join(text.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProfilePngError(
            "'fabulist_profile' chunk is not valid base64: "
            f"{exc}"
        ) from exc
    try:
        envelope = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfilePngError(
            "'fabulist_profile' chunk is not valid UTF-8 JSON: "
            f"{exc}"
        ) from exc
    if not isinstance(envelope, dict):
        raise ProfilePngError("'fabulist_profile' chunk JSON must be an object")
    return envelope
