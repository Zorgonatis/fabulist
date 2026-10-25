"""Tests for Character Card V2 import (W2.10, D28).

Inline PNGs are built by hand with ``struct`` + ``zlib`` so the parser is
exercised against a real chunk stream (signature / IHDR / tEXt / IEND, CRC and
all) rather than a library's happy path. The full-field fidelity checks read the
checked-in fixtures under ``fixtures/cards/`` (see
tests/fixtures/cards/make_png_fixtures.py for how the PNGs are built).
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import pytest

from fabulist.cards import CardError, CardV2, parse_card_v2, to_session_seed
from fabulist.cards.v2 import IMPORT_SEED_PREFIX, PNG_SIGNATURE

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cards"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


FULL_V2 = _fixture("full_v2.json")
FULL_DATA = FULL_V2["data"]

#: Card fields that must survive a parse -> to_dict -> parse cycle unchanged.
#: ``raw_data`` is excluded: it is envelope provenance, and a flat ``to_dict``
#: has no V2 envelope for the second parse to record.
_FIDELITY_FIELDS = (
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "creator_notes",
    "system_prompt",
    "post_history_instructions",
    "character_book",
    "tags",
    "alternate_greetings",
    "creator",
    "character_version",
    "extensions",
)

# --- PNG construction helpers ------------------------------------------------------


def _chunk(ctype: bytes, data: bytes, *, crc: int | None = None) -> bytes:
    """Annotate ``data`` as a PNG chunk, optionally forcing a (bad) CRC."""
    if crc is None:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _png(chara_json: dict, *, corrupt_crc: bool = False) -> bytes:
    """A minimal but structurally valid PNG carrying a ``chara`` tEXt chunk."""
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    payload = base64.b64encode(json.dumps(chara_json).encode("utf-8"))
    text = _chunk(
        b"tEXt",
        b"chara\x00" + payload,
        crc=0 if corrupt_crc else None,
    )
    iend = _chunk(b"IEND", b"")
    return PNG_SIGNATURE + ihdr + text + iend


V2_JSON = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Lyra Voss",
        "description": "A wandering cartographer.",
        "personality": "Curious, dry-humored.",
        "scenario": "A rain-soaked night market.",
        "first_mes": "Lyra looks up from her maps. 'You lost?'",
        "mes_example": "<START>\n{{user}}: hello",
        "creator_notes": "Hand-tuned for slow burn.",
        "system_prompt": "Stay in third person.",
        "post_history_instructions": "Keep the weather continuous.",
        "character_book": {"entries": [{"keys": ["market"], "content": "busy"}]},
        "tags": ["npc", "cartographer"],
    },
}

V1_JSON = {
    "name": "Bram",
    "description": "A retired soldier turned cook.",
    "personality": "Blunt.",
    "scenario": "A field kitchen.",
    "first_mes": "Bram ladles stew without looking up.",
    "mes_example": "{{user}}: thanks",
}

MINIMAL_JSON = {"name": "Min", "description": "Exists."}


# --- PNG round-trip ----------------------------------------------------------------


def test_png_v2_round_trip():
    card = parse_card_v2(_png(V2_JSON))

    assert isinstance(card, CardV2)
    data = V2_JSON["data"]
    assert card.name == data["name"]
    assert card.description == data["description"]
    assert card.personality == data["personality"]
    assert card.scenario == data["scenario"]
    assert card.first_mes == data["first_mes"]
    assert card.mes_example == data["mes_example"]
    assert card.creator_notes == data["creator_notes"]
    assert card.system_prompt == data["system_prompt"]
    assert card.post_history_instructions == data["post_history_instructions"]
    assert card.character_book == data["character_book"]
    assert card.tags == data["tags"]


def test_png_v1_flat_round_trip():
    card = parse_card_v2(_png(V1_JSON))

    assert card.name == "Bram"
    assert card.description == V1_JSON["description"]
    assert card.personality == "Blunt."
    assert card.scenario == "A field kitchen."
    assert card.first_mes == V1_JSON["first_mes"]
    # Absent optional fields stay empty/None rather than erroring.
    assert card.creator_notes is None
    assert card.system_prompt is None
    assert card.character_book is None
    assert card.tags == []


def test_png_minimal_card_is_accepted():
    card = parse_card_v2(_png(MINIMAL_JSON))
    assert card.name == "Min"
    assert card.description == "Exists."


# --- dict import -------------------------------------------------------------------


def test_v2_envelope_dict_import():
    card = parse_card_v2(V2_JSON)
    assert card.name == "Lyra Voss"
    assert card.tags == ["npc", "cartographer"]


def test_v1_flat_dict_import():
    card = parse_card_v2(dict(V1_JSON))
    assert card.name == "Bram"
    assert card.description == V1_JSON["description"]


# --- validation + corruption -------------------------------------------------------


def test_bad_crc_chunk_rejected():
    with pytest.raises(CardError, match="bad CRC"):
        parse_card_v2(_png(V2_JSON, corrupt_crc=True))


def test_missing_name_raises():
    with pytest.raises(CardError, match="name"):
        parse_card_v2({"description": "no name here"})


def test_blank_name_raises():
    with pytest.raises(CardError, match="name"):
        parse_card_v2({"name": "   ", "description": "d"})


def test_missing_description_raises():
    with pytest.raises(CardError, match="description"):
        parse_card_v2({"name": "Only A Name"})


def test_not_a_png_rejected():
    with pytest.raises(CardError, match="signature"):
        parse_card_v2(b"this is not a png at all")


def test_png_without_chara_chunk_rejected():
    png = PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    png += _chunk(b"IEND", b"")
    with pytest.raises(CardError, match="chara"):
        parse_card_v2(png)


def test_bad_json_in_chara_chunk_rejected():
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    text = _chunk(b"tEXt", b"chara\x00" + base64.b64encode(b"{not json"))
    png = PNG_SIGNATURE + ihdr + text + _chunk(b"IEND", b"")
    with pytest.raises(CardError, match="JSON"):
        parse_card_v2(png)


def test_unsupported_source_type_rejected():
    with pytest.raises(CardError, match="PNG bytes or a dict"):
        parse_card_v2("a/path/card.png")


def test_tags_must_be_a_list():
    with pytest.raises(CardError, match="tags"):
        parse_card_v2({"name": "n", "description": "d", "tags": "npc"})


# --- session seed shaping ----------------------------------------------------------


def test_to_session_seed_shape():
    card = parse_card_v2(_png(V2_JSON))
    seed = to_session_seed(card)

    # No fabricated player turn: the import is recorded as a truthful system
    # marker, and the card body rides its own system event.
    assert "user_event" not in seed
    marker = seed["marker_event"]
    assert marker["type"] == "system"
    assert marker["actor"] == "system"
    assert marker["payload"] == {"event": "card_imported", "name": "Lyra Voss"}

    system = seed["system_event"]
    assert system["type"] == "system"
    assert system["payload"]["first_mes"] == V2_JSON["data"]["first_mes"]
    assert system["payload"]["card"]["name"] == "Lyra Voss"
    assert system["payload"]["card"]["description"] == V2_JSON["data"]["description"]
    assert system["payload"]["card"]["tags"] == ["npc", "cartographer"]


def test_to_session_seed_missing_first_mes_is_empty_string():
    card = parse_card_v2({"name": "No Greeting", "description": "d"})
    seed = to_session_seed(card)
    assert card.first_mes is None
    assert seed["system_event"]["payload"]["first_mes"] == ""


def test_is_import_seed_text_only_matches_the_legacy_marker():
    from fabulist.cards.v2 import is_import_seed_text

    assert is_import_seed_text(f"{IMPORT_SEED_PREFIX}Lyra Voss]")
    assert is_import_seed_text("[Imported card: Tessa]")
    assert not is_import_seed_text("")
    assert not is_import_seed_text(None)
    assert not is_import_seed_text("I approach the map table.")
    assert not is_import_seed_text("[Imported] Tessa")


# --- full-field CardV2 fidelity (D28) ----------------------------------------------


def test_full_v2_json_parses_every_field():
    card = parse_card_v2(FULL_V2)

    assert card.name == FULL_DATA["name"]
    assert card.description == FULL_DATA["description"]
    assert card.personality == FULL_DATA["personality"]
    assert card.scenario == FULL_DATA["scenario"]
    assert card.first_mes == FULL_DATA["first_mes"]
    assert card.mes_example == FULL_DATA["mes_example"]
    assert card.creator_notes == FULL_DATA["creator_notes"]
    assert card.system_prompt == FULL_DATA["system_prompt"]
    assert card.post_history_instructions == FULL_DATA["post_history_instructions"]
    assert card.character_book == FULL_DATA["character_book"]
    assert card.tags == FULL_DATA["tags"]
    assert card.alternate_greetings == FULL_DATA["alternate_greetings"]
    assert card.creator == FULL_DATA["creator"]
    assert card.character_version == FULL_DATA["character_version"]
    assert card.extensions == FULL_DATA["extensions"]


def test_extensions_round_trip_preserves_unknown_nested_keys():
    card = parse_card_v2(FULL_V2)

    # Loud on any drop: whole-mapping equality fails if a key disappears.
    assert card.extensions == FULL_DATA["extensions"]
    assert card.extensions["depth_prompt"]["unknown_nested"] == {
        "a": [1, 2, {"b": True}]
    }
    assert "unknown_top_level_extension" in card.extensions

    # ... and it survives to_dict + re-parse.
    assert parse_card_v2(card.to_dict()).extensions == FULL_DATA["extensions"]


def test_raw_data_is_the_verbatim_v2_data_object():
    card = parse_card_v2(FULL_V2)

    assert card.raw_data == FULL_DATA
    assert list(card.raw_data) == list(FULL_DATA)  # key order preserved
    # Unknown top-level keys have no typed home but raw_data.
    assert card.raw_data["unknown_data_key"] == {"kept": "verbatim"}
    assert card.raw_data["another_unknown"] == [1, 2, 3]


def test_parse_to_dict_to_parse_preserves_every_field():
    card = parse_card_v2(FULL_V2)
    again = parse_card_v2(card.to_dict())

    for field in _FIDELITY_FIELDS:
        assert getattr(again, field) == getattr(card, field), field
    assert card.raw_data == FULL_DATA
    assert again.raw_data is None


def test_full_v2_png_fixture_parses_every_field():
    card = parse_card_v2((FIXTURES / "full_v2.png").read_bytes())

    assert card.name == FULL_DATA["name"]
    assert card.extensions == FULL_DATA["extensions"]
    assert card.raw_data == FULL_DATA
    assert list(card.raw_data) == list(FULL_DATA)


def test_v1_flat_fixture_has_no_envelope_and_empty_new_fields():
    card = parse_card_v2(_fixture("v1_flat.json"))

    assert card.name == "Bram"
    assert card.raw_data is None
    assert card.extensions == {}
    assert card.alternate_greetings == []
    assert card.creator is None
    assert card.character_version is None


def test_three_alternate_greetings_round_trip():
    card = parse_card_v2(_fixture("three_greetings.json"))

    assert len(card.alternate_greetings) == 3
    assert card.alternate_greetings == _fixture("three_greetings.json")["data"][
        "alternate_greetings"
    ]
    assert parse_card_v2(card.to_dict()).alternate_greetings == card.alternate_greetings


def test_malformed_extensions_coerced_empty_but_raw_retained():
    card = parse_card_v2(_fixture("malformed_extensions.json"))

    assert card.extensions == {}
    assert card.raw_data["extensions"] == "not-an-object"
    assert card.raw_data["unknown_data_key"] == 7


def test_alternate_greetings_must_be_a_list():
    with pytest.raises(CardError, match="alternate_greetings"):
        parse_card_v2({"name": "n", "description": "d", "alternate_greetings": "hi"})


def test_alternate_greetings_non_strings_are_coerced():
    card = parse_card_v2(
        {"name": "n", "description": "d", "alternate_greetings": [1, "two"]}
    )
    assert card.alternate_greetings == ["1", "two"]


def test_new_fields_default_when_absent():
    card = parse_card_v2({"name": "n", "description": "d"})

    assert card.extensions == {}
    assert card.alternate_greetings == []
    assert card.creator is None
    assert card.character_version is None
    assert card.raw_data is None


# --- V3 hybrid envelopes (SillyTavern exports) --------------------------------------

HYBRID_V3_JSON = {
    # Legacy top-level fields AND a full data envelope; the envelope is
    # authoritative (spec names V3). Regression: _unwrap used to return the
    # flat object because spec != "chara_card_v2" and "name" was in raw, so
    # data.character_book / data.extensions were silently dropped.
    "spec": "chara_card_v3",
    "spec_version": "3.0",
    "name": "Epoch Flat",
    "description": "Flat copy that must lose to the envelope.",
    "first_mes": "top-level greeting",
    "data": {
        "name": "Epoch Envelope",
        "description": "A wanderer with a book.",
        "personality": "Wary.",
        "scenario": "A relay station.",
        "first_mes": "envelope greeting",
        "character_book": {
            "name": "Epoch lore",
            "entries": [
                {
                    "keys": ["relay"],
                    "content": "the relay hums",
                    "extensions": {"vectorized": True, "probability": 100},
                }
            ],
        },
        "extensions": {"depth_prompt": {"prompt": "stay wary", "depth": 4}},
    },
}


def test_v3_hybrid_unwraps_data_envelope():
    card = parse_card_v2(HYBRID_V3_JSON)

    assert card.name == "Epoch Envelope"
    assert card.description == "A wanderer with a book."
    assert card.first_mes == "envelope greeting"
    # The fields that only exist under data survive the hybrid import.
    assert card.character_book == HYBRID_V3_JSON["data"]["character_book"]
    assert card.extensions == HYBRID_V3_JSON["data"]["extensions"]
    # Verbatim raw_data is preserved for V3 envelopes too (export round-trip).
    assert card.raw_data == HYBRID_V3_JSON["data"]


def test_v3_spec_without_data_is_an_error():
    import pytest

    from fabulist.cards.v2 import CardError

    with pytest.raises(CardError, match="missing its 'data' object"):
        parse_card_v2({"spec": "chara_card_v3", "spec_version": "3.0", "name": "X"})


def test_v3_hybrid_png_round_trip_keeps_book():
    card = parse_card_v2(_png(HYBRID_V3_JSON))

    assert card.character_book == HYBRID_V3_JSON["data"]["character_book"]
    assert card.extensions == HYBRID_V3_JSON["data"]["extensions"]
