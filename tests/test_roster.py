"""Tests for the multi-character roster context (W3.7 v1).

Covers card extraction from the event log, the single-card block (byte-compared
against :func:`fabulist.wiring.session_persona_block`), the multi-card cast
block (header, first-greeting-only, budget cap), and the roster one-liner.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fabulist.context.roster import (
    extract_cards,
    persona_block,
    roster_line,
)
from fabulist.log import EventLog
from fabulist.wiring import session_persona_block

# --- fixtures / helpers -----------------------------------------------------


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for an :class:`EventLog` on a tmp path; closes logs after."""
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


def _event(
    type: str,
    payload: dict,
    seq: int,
    actor: str = "system",
) -> SimpleNamespace:
    """Minimal duck-typed event for pure-function tests."""
    return SimpleNamespace(type=type, payload=payload, seq=seq, actor=actor)


LYRA = {
    "name": "Lyra Voss",
    "description": "A wandering cartographer.",
    "personality": "Curious and wry.",
    "scenario": "A rain-soaked night market.",
}
LYRA_GREETING = "Lyra looks up from her maps. 'You lost?'"

MORROW = {
    "name": "Captain Morrow",
    "description": "A one-eyed smuggler.",
    "personality": "Blunt, loyal.",
    "scenario": "The docks at midnight.",
}
MORROW_GREETING = "Morrow kicks a crate toward you."


# --- extract_cards ----------------------------------------------------------


def test_extract_cards_orders_by_seq_and_ignores_non_card_events():
    events = [
        _event("user", {"text": "hi"}, seq=1),
        _event("system", {"card": MORROW}, seq=4),
        _event("system", {"event": "world_info", "book": {}}, seq=2),
        _event("narrative", {"prose": "..."}, seq=3),
        _event("system", {"card": "not-a-dict"}, seq=5),
        _event("system", {"card": LYRA}, seq=2),
    ]
    cards = extract_cards(events)
    assert [c["name"] for c in cards] == ["Lyra Voss", "Captain Morrow"]


def test_extract_cards_returns_same_objects_in_place_for_equal_seq():
    events = [_event("system", {"card": LYRA}, seq=1)]
    (card,) = extract_cards(events)
    assert card is LYRA


def test_extract_cards_reads_real_event_log_rows(build_log):
    log = build_log()
    sid = "sess-1"
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": LYRA_GREETING})
    log.append_event(sid, "system", "system", {"card": MORROW, "first_mes": MORROW_GREETING})
    cards = extract_cards(log.get_events(sid))
    assert [c["name"] for c in cards] == ["Lyra Voss", "Captain Morrow"]


# --- persona_block: single card --------------------------------------------


def test_single_card_matches_wiring_byte_for_byte(build_log):
    log = build_log()
    sid = "sess-single"
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": LYRA_GREETING})

    cards = extract_cards(log.get_events(sid))
    ours = persona_block(cards, [LYRA_GREETING])
    wiring = session_persona_block(log, sid)
    assert ours == wiring
    assert ours == (
        "### Character: Lyra Voss\n"
        "Description: A wandering cartographer.\n"
        "Personality: Curious and wry.\n"
        "Scenario: A rain-soaked night market.\n"
        "Opening scene (already established): Lyra looks up from her maps. 'You lost?'"
    )


def test_single_card_skips_absent_fields_and_empty_greeting():
    card = {"name": "Bare", "description": "", "personality": None}
    block = persona_block([card], [""])
    assert block == "### Character: Bare"
    assert "Description" not in block
    assert "Opening scene" not in block


def test_single_card_falls_back_to_card_first_mes_when_list_absent():
    card = {**LYRA, "first_mes": LYRA_GREETING}
    block = persona_block([card])
    assert block is not None
    assert block.endswith(f"Opening scene (already established): {LYRA_GREETING}")


def test_first_mes_list_is_authoritative_over_card_field():
    card = {**LYRA, "first_mes": "card greeting"}
    block = persona_block([card], [""])  # explicit empty -> skipped
    assert "Opening scene" not in (block or "")


def test_persona_block_empty_roster_is_none():
    assert persona_block([]) is None


# --- persona_block: multiple cards -----------------------------------------


def test_two_cards_cast_header_and_only_first_greeting():
    block = persona_block(
        [LYRA, MORROW], [LYRA_GREETING, MORROW_GREETING]
    )
    assert block is not None
    lines = block.split("\n")
    assert lines[0] == "### Cast"
    assert "#### Lyra Voss" in lines
    assert "#### Captain Morrow" in lines
    # Full fields for every card (scenario is not dropped for later cards).
    assert "Description: A one-eyed smuggler." in lines
    assert "Personality: Blunt, loyal." in lines
    assert "Scenario: The docks at midnight." in lines
    # Only the first card's opening scene renders.
    assert block.count("Opening scene (already established):") == 1
    assert f"Opening scene (already established): {LYRA_GREETING}" in block
    assert MORROW_GREETING not in block


def test_cast_block_renders_every_card_in_full():
    """Uncapped (owner: "Uncap.") — no card is dropped, clipped or marked.

    A ~6.5 KB card in the Forest world used to push the second card past the
    old 2400-char budget, so card "Tessa" vanished while the state block still
    named her — the narrator invented her from a bare name. Every card now
    renders in full, in order, with no truncation marker.
    """
    fat = {"name": "Verbose", "description": "x" * 4000}
    block = persona_block([LYRA, fat], [LYRA_GREETING, ""])
    assert block is not None
    # Both cards render; the second is not clipped away.
    assert block.startswith("### Cast\n#### Lyra Voss\n")
    assert "#### Verbose\n" in block
    assert f"Description: {'x' * 4000}" in block
    # No budget clipping and no truncation marker anywhere.
    assert block.endswith("x" * 4000)
    assert "(cast truncated)" not in block
    # Deterministic and byte-stable for the same input (stable prompt prefix).
    assert block == persona_block([LYRA, fat], [LYRA_GREETING, ""])


def test_single_card_renders_in_full():
    fat = {"name": "Verbose", "description": "x" * 4000}
    block = persona_block([fat], [""])
    assert block is not None
    assert f"Description: {'x' * 4000}" in block
    assert "(cast truncated)" not in block


# --- roster_line ------------------------------------------------------------


def test_roster_line_none_for_single_card():
    assert roster_line([LYRA]) is None
    assert roster_line([]) is None


def test_roster_line_lists_two_plus_cards():
    assert roster_line([LYRA, MORROW]) == "Cast: Lyra Voss, Captain Morrow"
    assert roster_line([LYRA, MORROW, {"name": "Third"}]) == (
        "Cast: Lyra Voss, Captain Morrow, Third"
    )


def test_roster_line_unknown_name_fallback():
    assert roster_line([{"description": "x"}, {"name": "Real"}]) == (
        "Cast: Unknown, Real"
    )
