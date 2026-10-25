"""Character Card V2 import (PLAN W2.10).

A *card* is a character definition in the Tavern/SillyTavern lineage. Two
on-the-wire shapes are accepted, both described by the Character Card V2
spec:

* **V2 JSON** — ``{"spec": "chara_card_v2", "spec_version": "2.0", "data": {...}}``
* **V1 flat JSON** — ``{"name": ..., "description": ..., ...}`` at the top level

SillyTavern stores a card inside a PNG as a ``tEXt`` chunk whose keyword is
``chara`` and whose text is base64-encoded UTF-8 JSON. This module walks the
PNG chunk stream itself (CRC-validating every chunk with :func:`zlib.crc32`)
rather than trusting an image library: a card that fails CRC is a corrupt
card and must not silently import.

The parser is deliberately strict about identity — a card without a ``name``
and a ``description`` is not importable — and deliberately permissive about
everything else, since real-world cards carry missing or oddly-typed optional
fields. ``parse_card_v2`` never touches the event log; it only turns bytes/dicts
into a :class:`CardV2`. The caller shapes it into a first-turn seed via
:func:`to_session_seed` (shaping only; session creation is the orchestrator's
job).

Layer 0 is lossless (D28): :class:`CardV2` carries *every* V2 field, including
``alternate_greetings``/``creator``/``character_version``/``extensions``, plus
``raw_data`` — the verbatim unwrapped V2 ``data`` object. The V2 spec says
unknown keys MUST be preserved, and the only place a genuinely unknown
*top-level* key can live is ``raw_data``; ``extensions`` keeps its unknown
nested keys verbatim. A V1/flat or hand-shaped dict has no V2 envelope, so its
``raw_data`` is ``None``.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import zlib
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.personas.registry import Persona

__all__ = [
    "CardError",
    "CardV2",
    "CARD_SPEC",
    "CARD_SPEC_V3",
    "CHARA_KEYWORD",
    "IMPORT_SEED_PREFIX",
    "PNG_SIGNATURE",
    "is_import_seed_text",
    "parse_card_v2",
    "persona_to_seed_card",
    "to_session_seed",
]

#: Text prefix the LEGACY card-import seed wrote into a synthetic ``user`` event.
#: A card import is a SEED, not a player turn: old imports fabricated a player
#: line (``"[Imported card: {name}]"``) that was read back as the player's own
#: words — a fake bubble, a bogus turn count and a fabricated recall memory.
#: Current imports write a truthful ``card_imported`` ``system`` event instead
#: (:func:`to_session_seed`) and no ``user`` row at all. This prefix is kept
#: only to recognise those legacy rows at read time (:func:`is_import_seed_text`)
#: — history is append-only, so the rows stay in the log (D9), they are just
#: never read back as turns. Exported so the memory indexer can recognise the
#: seed too; the card body itself already rides the stable cast/character prompt
#: prefix.
IMPORT_SEED_PREFIX = "[Imported card: "


def is_import_seed_text(text: object) -> bool:
    """True when ``text`` is the legacy synthetic card-import marker.

    The single read-side predicate every derived view uses to refuse a legacy
    import row as a turn (a ``user``-type event whose text starts with
    :data:`IMPORT_SEED_PREFIX`). The projection, the session-list turn count
    and the inspector all route through this; the memory indexer keeps its own
    copy of the same check. A card import carries the card body on its own
    ``system`` event (:func:`to_session_seed`), so hiding the marker loses no
    character context — it only stops fabricating a player turn.
    """
    return str(text or "").startswith(IMPORT_SEED_PREFIX)


#: Value of the ``spec`` field in a V2 payload's outer envelope.
CARD_SPEC = "chara_card_v2"
#: Spec string of a Character Card V3 envelope (hybrid cards carry V2-shaped
#: fields under ``data`` and name themselves V3 — SillyTavern exports do this).
CARD_SPEC_V3 = "chara_card_v3"

#: The 8-byte magic that opens every PNG file.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: tEXt keyword under which SillyTavern stores the base64 JSON card.
CHARA_KEYWORD = b"chara"


class CardError(ValueError):
    """A card could not be parsed or failed validation."""


@dataclass
class CardV2:
    """A parsed character card.

    ``name`` and ``description`` are always present (validation enforces
    them); every other field is optional and ``None`` when the source omitted
    it. ``character_book`` is kept raw because world-info entries are consumed
    elsewhere (W2.10 WI v1) and must not be lossily re-typed here.

    ``raw_data`` is the verbatim unwrapped V2 ``data`` object (``None`` when the
    source had no V2 envelope). It is the lossless escape hatch for unknown
    top-level keys the spec requires be preserved; :func:`to_dict` emits it, and
    the exporter passes it straight through rather than re-assembling the card.
    """

    name: str
    description: str
    personality: str | None = None
    scenario: str | None = None
    first_mes: str | None = None
    mes_example: str | None = None
    creator_notes: str | None = None
    system_prompt: str | None = None
    post_history_instructions: str | None = None
    character_book: Any = None
    tags: list[str] = field(default_factory=list)
    alternate_greetings: list[str] = field(default_factory=list)
    creator: str | None = None
    character_version: str | None = None
    extensions: dict = field(default_factory=dict)
    raw_data: dict | None = None

    def to_dict(self) -> dict:
        """Plain-dict view (used by :func:`to_session_seed` and the API)."""
        return asdict(self)


# --- public entry point ------------------------------------------------------------


def parse_card_v2(source: bytes | bytearray | memoryview | dict) -> CardV2:
    """Parse a character card from PNG bytes or an already-decoded dict.

    ``source`` is either the raw bytes of a card PNG or a JSON-decoded object
    (the import path for a ``.json`` card). Raises :class:`CardError` with a
    precise message on any structural or validation failure.
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        raw = _parse_png(bytes(source))
    elif isinstance(source, dict):
        raw = source
    else:
        raise CardError(
            f"card source must be PNG bytes or a dict, got {type(source).__name__}"
        )
    return _build_card(raw)


def to_session_seed(card: CardV2) -> dict:
    """Describe the seed events for an imported card (shaping only).

    Returns the event-shaped payloads the orchestrator will later append: a
    truthful ``system`` marker recording the import (``{"event":
    "card_imported", "name": ...}``) and a ``system`` event carrying the full
    card plus the greeting that should open the scene. No log writes happen
    here and no session is created — the caller owns that.

    An import is a SEED, not a player turn: no ``user`` event is produced. The
    legacy shape fabricated a ``user`` line (``"[Imported card: {name}]"``)
    that readers mistook for the player's words; :func:`is_import_seed_text`
    hides those old rows, and this shape means no new ones are ever written.
    """
    return {
        "marker_event": {
            "type": "system",
            "actor": "system",
            "payload": {"event": "card_imported", "name": card.name},
        },
        "system_event": {
            "type": "system",
            "actor": "system",
            "payload": {
                "card": card.to_dict(),
                "first_mes": card.first_mes or "",
            },
        },
    }


def persona_to_seed_card(persona: "Persona") -> dict:
    """Shape a persona as the card seed event that opens a persona chat.

    A chat created with ``POST /api/sessions`` and a ``persona_id`` (the rail's
    actor "+") must carry the same character context an imported card gets: a
    ``system`` seed event whose payload holds the card dict *plus* the
    greeting, so the Opening block (``GET /api/sessions/{id}/opening``) and the
    narrator's character block
    (:func:`fabulist.context.facts.session_persona_block`) can render it. This
    is :func:`to_session_seed`'s system payload for a persona: the V2-only
    fields a persona never carries (``mes_example``, ``creator_notes``, ...)
    are omitted, and the import-only ``user`` event is not produced.

    Unlike :func:`fabulist.worlds.registry._persona_card` — a world's cast
    members are scenery, so their ``first_mes`` must NOT open the scene — this
    helper KEEPS the greeting: a persona chat's opening IS the persona's own
    ``first_mes``. An empty ``first_mes`` still seeds the card (the narrator's
    character block matters); the frontend hides an empty greeting.

    Returns the ``system`` event payload; the caller appends the event. No log
    writes happen here and no session is created.
    """
    return {
        "card": {
            "name": persona.name,
            "description": persona.description,
            "personality": persona.personality,
            "scenario": persona.scenario,
            "alternate_greetings": list(persona.alternate_greetings),
            "extensions": dict(persona.extensions),
        },
        "first_mes": persona.first_mes or "",
    }


# --- PNG chunk walk ----------------------------------------------------------------


def _iter_chunks(png: bytes):
    """Yield ``(type, data)`` for every chunk, CRC-validating each one.

    Stops after ``IEND``. Raises :class:`CardError` on a missing PNG
    signature, a truncated stream, or a chunk whose stored CRC does not match
    ``crc32(type + data)``.
    """
    if not png.startswith(PNG_SIGNATURE):
        raise CardError("not a PNG: missing the 8-byte PNG signature")

    offset = len(PNG_SIGNATURE)
    total = len(png)
    while offset < total:
        if offset + 8 > total:
            raise CardError("truncated PNG: incomplete chunk header")
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        ctype = png[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > total:
            raise CardError(f"truncated PNG: {ctype!r} chunk extends past end of file")
        data = png[data_start:data_end]
        (stored_crc,) = struct.unpack(">I", png[data_end:crc_end])
        computed_crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        if stored_crc != computed_crc:
            raise CardError(
                f"corrupt PNG: bad CRC for {ctype!r} chunk "
                f"(stored {stored_crc:#010x}, computed {computed_crc:#010x})"
            )
        yield ctype, data
        offset = crc_end
        if ctype == b"IEND":
            return


def _find_chara_text(png: bytes) -> bytes:
    for ctype, data in _iter_chunks(png):
        if ctype != b"tEXt":
            continue
        nul = data.find(b"\x00")
        if nul == -1:
            continue
        if data[:nul] == CHARA_KEYWORD:
            return data[nul + 1 :]
    raise CardError("PNG has no 'chara' tEXt chunk")


def _parse_png(png: bytes) -> dict:
    text = _find_chara_text(png)
    compact = b"".join(text.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CardError(f"'chara' chunk is not valid base64: {exc}") from exc
    try:
        raw = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CardError(f"'chara' chunk is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CardError("'chara' chunk JSON must be an object")
    return raw


# --- card shaping / validation -----------------------------------------------------


def _unwrap(raw: dict) -> dict:
    """Return the field-bearing object, unwrapping a V2/V3 envelope if present.

    Hybrid cards (SillyTavern V3 exports) carry legacy top-level fields AND a
    full ``data`` envelope; the envelope is authoritative whenever the spec
    names a known chara spec, so ``data.character_book`` and
    ``data.extensions`` are not silently dropped in favour of the flat copy.
    """
    spec = raw.get("spec")
    data = raw.get("data")
    if isinstance(data, dict) and (spec in (CARD_SPEC, CARD_SPEC_V3) or "name" not in raw):
        return data
    if spec in (CARD_SPEC, CARD_SPEC_V3):
        raise CardError(f"{spec} payload is missing its 'data' object")
    return raw


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _str_list(value: Any, field_name: str) -> list[str]:
    """Coerce a card list-of-strings field, tolerating non-string members.

    ``None`` yields an empty list (the field is optional). A list/tuple is
    mapped to ``str`` members so a stray number in ``tags`` or
    ``alternate_greetings`` imports rather than aborting the whole card.
    Anything else is a structural error naming the offending field.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [t if isinstance(t, str) else str(t) for t in value]
    raise CardError(
        f"'{field_name}' must be a list of strings, got {type(value).__name__}"
    )


def _extensions(value: Any) -> dict:
    """The ``extensions`` object, or ``{}`` when the source is not an object.

    The V2 spec reserves ``extensions`` for consumer data and requires only
    that it be a JSON object; unknown *nested* keys MUST be preserved (D28). A
    non-object value is not importable as extensions and degrades to an empty
    object here — the malformed value still survives verbatim in ``raw_data``.
    """
    return value if isinstance(value, dict) else {}


def _build_card(raw: dict) -> CardV2:
    data = _unwrap(raw)

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CardError("card is missing a non-empty 'name'")

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise CardError("card is missing a non-empty 'description'")

    return CardV2(
        name=name,
        description=description,
        personality=_text(data.get("personality")),
        scenario=_text(data.get("scenario")),
        first_mes=_text(data.get("first_mes")),
        mes_example=_text(data.get("mes_example")),
        creator_notes=_text(data.get("creator_notes")),
        system_prompt=_text(data.get("system_prompt")),
        post_history_instructions=_text(data.get("post_history_instructions")),
        character_book=data.get("character_book"),
        tags=_str_list(data.get("tags"), "tags"),
        alternate_greetings=_str_list(
            data.get("alternate_greetings"), "alternate_greetings"
        ),
        creator=_text(data.get("creator")),
        character_version=_text(data.get("character_version")),
        extensions=_extensions(data.get("extensions")),
        # Only a V2/V3 envelope has a verbatim ``data`` object to preserve; a
        # flat V1/hand-shaped dict has none, so ``raw_data`` stays ``None`` and
        # the exporter falls back to field assembly.
        raw_data=data if raw.get("spec") in (CARD_SPEC, CARD_SPEC_V3) else None,
    )
