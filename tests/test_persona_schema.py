"""Structured persona schema (D36): parsing, rendering, hosting, prompt block.

Phase 1 — lenient schema, unknown-key preservation, missing/empty handling, and
the card -> persona-event ``extensions`` carry-over.
Phase 2 — the rendered canon joins the character description block
(:func:`fabulist.context.facts.session_persona_block` /
:func:`fabulist.context.roster.persona_block`), and cards without the extension
render exactly as before.

Patterns mirror ``tests/test_personas_api.py`` (real tmp-path ``EventLog``) and
``tests/test_cards_api.py`` (``create_app`` + cards router over httpx ASGI).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cards_api import router as cards_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.context.facts import session_persona_block, static_prefix_parts
from fabulist.context.roster import persona_block
from fabulist.log import EventLog
from fabulist.personas.registry import (
    PERSONAS_ANCHOR,
    append_persona,
    backfill_from_cards,
    list_personas,
)
from fabulist.personas.schema import (
    PERSONA_HEADER,
    BodyNode,
    PersonaSchema,
    persona_lines_from_card,
    render,
    render_identity_core,
    schema_from_extensions,
)

#: An invented persona example (source schema for D36).
ELARA: dict = {
    "name": "Elara Voss",
    "age_band": "22",
    "archetype": ["flower girl", "white mage", "last of the ancients"],
    "gender": "female",
    "ethnicity": "ambiguous (european-coded with eastern facial aesthetics)",
    "description": (
        "Flower seller from the Sector 5 slums of Midgar and the last surviving "
        "Cetra (Ancient); tends a flower bed in an abandoned church, heals with "
        "white magic, and joins Cloud and AVALANCHE against Shinra and Sephiroth."
    ),
    "personality": [
        "cheerful",
        "gentle",
        "playful",
        "teasing",
        "compassionate",
        "optimistic",
        "plucky",
        "wise beyond her years",
    ],
    "likes": [
        "growing and selling flowers",
        "her church in the Sector 5 slums",
        "children",
        "the planet",
        "teasing Cloud",
        "quiet moments with friends",
    ],
    "dislikes": [
        "Shinra",
        "the Turks pursuing her",
        "being treated as a research specimen",
        "harm to the planet",
        "being confined",
    ],
    "body": {
        "build": "slender",
        "height": "petite",
        "skin_tone": "fair",
        "head": {
            "hair": (
                "light ash brown, very long, worn loose in a single braid tied "
                "with a large pink ribbon"
            ),
            "face": {
                "eyes": "emerald-green",
                "mouth": "soft warm smile",
                "nose": "small delicate",
                "cheeks": "soft rounded",
            },
            "neck": "slender",
        },
        "torso": {
            "shoulders": "narrow",
            "breasts": "average",
            "nipples": "small",
            "waist": "slim",
        },
        "lower": {
            "hips": "average",
            "buttocks": "soft",
            "thighs": "soft",
            "legs": "slender untoned",
            "feet": "small",
        },
        "arms": {"hands": "delicate", "uncalloused": None},
    },
}

ELARA_CARD = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Elara Voss",
        "description": "Flower seller from the Sector 5 slums of Midgar.",
        "first_mes": "Elara looks up from the garden.",
        "extensions": {"fabulist": {"persona": ELARA}},
    },
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_log(tmp_path: Path):
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "api.db"
    built = []

    def _build(engine=None):
        app = create_app(engine or FakeTurnEngine(), log_path)
        app.include_router(cards_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# --- Phase 1: parsing -------------------------------------------------------


def test_parse_elara_identity_and_body_tree():
    schema = schema_from_extensions({"fabulist": {"persona": ELARA}})
    assert schema is not None
    assert schema.name == "Elara Voss"
    assert schema.age_band == "22"
    assert schema.archetype == ["flower girl", "white mage", "last of the ancients"]
    assert schema.gender == "female"
    assert schema.ethnicity.startswith("ambiguous")
    assert schema.personality[0] == "cheerful"
    assert schema.likes and schema.dislikes

    body = schema.body
    assert isinstance(body, BodyNode)
    assert body.fields["build"] == "slender"
    head = body.fields["head"]
    assert isinstance(head, BodyNode)
    face = head.fields["face"]
    assert isinstance(face, BodyNode)
    assert face.fields["eyes"] == "emerald-green"
    # A null leaf is preserved as an empty-valued descriptor.
    arms = body.fields["arms"]
    assert isinstance(arms, BodyNode)
    assert arms.fields["uncalloused"] == ""


def test_unknown_top_level_keys_are_preserved():
    raw = {
        "name": "X",
        "custom_field": "kept",
        "nested_extra": {"a": "b"},
        "body": {"build": "wiry"},
    }
    schema = PersonaSchema.from_mapping(raw)
    out = schema.to_dict()
    assert out["custom_field"] == "kept"
    assert out["nested_extra"] == {"a": "b"}
    assert out["body"] == {"build": "wiry"}
    # Known fields still render, unknown keys included in the full tree.
    text = render(schema)
    assert "Custom field: kept" in text


def test_scalar_fields_accept_lists_and_ints():
    schema = PersonaSchema.from_mapping(
        {"age_band": 22, "archetype": "mage", "personality": ["calm", "wry"]}
    )
    assert schema.age_band == "22"
    assert schema.archetype == ["mage"]
    assert schema.personality == ["calm", "wry"]


def test_missing_or_empty_schema_is_absent():
    assert schema_from_extensions(None) is None
    assert schema_from_extensions({}) is None
    assert schema_from_extensions({"other": {"x": 1}}) is None
    assert schema_from_extensions({"fabulist": {"persona": {}}}) is None
    assert schema_from_extensions({"fabulist": {"persona": "nope"}}) is None
    # An empty schema renders nothing.
    assert render(PersonaSchema.from_mapping({})) == ""
    assert render_identity_core(PersonaSchema.from_mapping({})) == ""
    # A card without the extension contributes no lines.
    assert persona_lines_from_card({"name": "Bare"}) == []


# --- Phase 2: canonical rendering -------------------------------------------


def test_render_full_tree_canonical_and_deterministic():
    schema = schema_from_extensions({"fabulist": {"persona": ELARA}})
    assert schema is not None
    text = render(schema)
    lines = text.split("\n")
    assert lines[0] == PERSONA_HEADER
    # No pre-selected marker bias — the narrator selects markers at render time.
    assert lines[1] == "Name: Elara Voss"
    assert "Name: Elara Voss" in lines
    assert "Archetype: flower girl; white mage; last of the ancients" in lines
    assert "Body:" in lines
    # Nested body leaves are indented under their group, in author order.
    assert "  Head:" in lines
    assert any(
        line.startswith("    Hair: light ash brown, very long") for line in lines
    )
    assert "      Eyes: emerald-green" in lines
    assert "    Uncalloused" in lines  # bare null leaf
    # Deterministic: same input -> same output.
    assert render(schema) == text


def test_render_identity_core_is_smaller_and_excludes_preferences():
    schema = schema_from_extensions({"fabulist": {"persona": ELARA}})
    assert schema is not None
    core = render_identity_core(schema)
    full = render(schema)
    assert len(core) < len(full)
    assert "Visual markers:" not in core
    assert "Name: Elara Voss" in core
    assert "Personality: cheerful; gentle" in core
    assert "Body: Build: slender; Height: petite; Skin tone: fair" in core
    # Preferences and nested body detail are the fidelity the owner trades away.
    assert "Likes:" not in core
    assert "      Eyes: emerald-green" not in core  # nested leaf not re-rendered

# --- prompt block integration (real log) ------------------------------------


def test_session_persona_block_includes_rendered_schema(build_log):
    log = build_log()
    sid = "elara"
    card = {
        "name": "Elara Voss",
        "description": "Flower seller from the Sector 5 slums of Midgar.",
        "extensions": {"fabulist": {"persona": ELARA}},
    }
    log.append_event(
        sid,
        "system",
        "system",
        {"card": card, "first_mes": "Elara looks up from the garden."},
    )

    block = session_persona_block(log, sid)
    assert block is not None
    assert block.startswith("### Character: Elara Voss\n")
    assert "Description: Flower seller from the Sector 5 slums of Midgar." in block
    assert PERSONA_HEADER in block
    # No pre-selected marker bias — full canon only; the narrator selects.
    assert "Visual markers:" not in block
    assert "Name: Elara Voss" in block
    assert "Name: Elara Voss" in block
    assert "Body:" in block
    assert "Uncalloused" in block
    assert block.endswith(
        "Opening scene (already established): Elara looks up from the garden."
    )
    # Static prefix carries the same block (the D34 pre-history seam).
    assert static_prefix_parts(log, sid) == [block]


def test_card_without_extension_renders_unchanged(build_log):
    log = build_log()
    sid = "plain"
    card = {
        "name": "Lyra Voss",
        "description": "A wandering cartographer.",
        "personality": "Curious and wry.",
    }
    log.append_event(sid, "system", "system", {"card": card, "first_mes": "Hi."})

    assert session_persona_block(log, sid) == (
        "### Character: Lyra Voss\n"
        "Description: A wandering cartographer.\n"
        "Personality: Curious and wry.\n"
        "Opening scene (already established): Hi."
    )
    assert persona_block([card], ["Hi."]) == session_persona_block(log, sid)


def test_cast_block_renders_persona_per_card():
    plain = {"name": "Lyra Voss", "description": "A wandering cartographer."}
    elara_card = {
        "name": "Elara Voss",
        "description": "Flower seller.",
        "extensions": {"fabulist": {"persona": ELARA}},
    }
    block = persona_block([plain, elara_card], ["Hi.", ""])
    assert block is not None
    assert block.startswith("### Cast")
    assert "#### Elara Voss" in block
    assert PERSONA_HEADER in block
    assert "Name: Elara Voss" in block


# --- Phase 1 hosting: card -> persona event -> prompt -----------------------


def test_backfill_forwards_card_extensions_into_persona_event(build_log):
    log = build_log()
    sid = "hosted"
    log.append_event(
        sid,
        "system",
        "system",
        {
            "card": {
                "name": "Elara Voss",
                "description": "Flower seller from the Sector 5 slums of Midgar.",
                "extensions": {"fabulist": {"persona": ELARA}},
            },
            "first_mes": "Hi.",
        },
    )

    result = backfill_from_cards(log)
    assert result["personas_created"] == 1
    (persona,) = list_personas(log)
    assert persona.extensions == {"fabulist": {"persona": ELARA}}

    # The raw anchor payload carries the schema (structured, not rendered).
    rows = [
        e.payload
        for e in log.get_events(PERSONAS_ANCHOR)
        if e.payload.get("event") == "persona"
    ]
    assert rows and rows[-1]["extensions"]["fabulist"]["persona"]["name"] == (
        "Elara Voss"
    )
    # And it still renders through the fold.
    assert schema_from_extensions(persona.extensions) is not None


def test_old_persona_without_extensions_folds_to_empty(build_log):
    log = build_log()
    append_persona(log, {"name": "Old Log Persona"})
    (persona,) = list_personas(log)
    assert persona.extensions == {}
    assert schema_from_extensions(persona.extensions) is None


def test_import_preserves_schema_stores_it_and_renders_in_prompt(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/cards/import", json={**ELARA_CARD, "seed": True}
            )
            return resp.json()

    body = run(scenario())
    assert body["ok"] is True
    sid = body["session_id"]
    # Import response echoes the schema verbatim under extensions (D28).
    assert body["card"]["extensions"]["fabulist"]["persona"] == ELARA

    log = EventLog(log_path)
    try:
        # The log's card event stores the structured schema.
        card_events = [
            e
            for e in log.get_events(sid)
            if e.type == "system" and "card" in e.payload
        ]
        assert card_events
        stored = card_events[0].payload["card"]
        assert stored["extensions"]["fabulist"]["persona"] == ELARA

        # "Folds into the persona event": backfill carries extensions forward.
        backfill_from_cards(log)
        (persona,) = list_personas(log)
        assert persona.extensions["fabulist"]["persona"] == ELARA

        # The assembled character description block contains the render.
        block = session_persona_block(log, sid)
        assert block is not None
        assert "### Character: Elara Voss" in block
        assert render(schema_from_extensions(persona.extensions)) in block
    finally:
        log.close()
