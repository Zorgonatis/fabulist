"""Tests for D31 world-info entity unification + the D32 world prefix.

Pure functions against a real (tmp-path) SQLite log, mirroring
``tests/test_facts_suffix.py``. Covers
:func:`fabulist.context.facts.session_world_info_block` (the pooled, deduped
render over session card books, legacy ``world_info`` events, persona-attached
Books, session/world book registrations) and
:func:`fabulist.context.facts.session_world_block` (the ``### World`` prefix),
plus the **byte-identical legacy render** (hash-pinned).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.context.books import append_book, backfill_books, entry_signature
from fabulist.context.facts import (
    context_suffix_parts,
    facts_hash,
    join_facts_suffix,
    session_world_block,
    session_world_info_block,
)
from fabulist.context.world_info import parse_book
from fabulist.log import EventLog
from fabulist.personas.registry import append_persona
from fabulist.worlds.registry import append_world

# One book with a hit ("lantern") and a never-mentioned entry (so activation
# has something to reject). The exact ST shape parse_book consumes.
MARKET_BOOK = {
    "name": "Night Market lore",
    "entries": [
        {
            "keys": ["lantern", "night market"],
            "content": "The night market opens only after the second bell.",
            "insertion_order": 1,
        },
        {
            "keys": ["never-mentioned"],
            "content": "Unreachable lore.",
            "insertion_order": 2,
        },
    ],
}

SCAN = "I raise the lantern."

#: The exact pre-D31 render of a single legacy book (byte-parity anchor).
EXPECTED_LEGACY = (
    "### World info\n- The night market opens only after the second bell."
)

#: sha256(...)[:16] of the full canonical context for the legacy session
#: (:func:`context_suffix_parts` joined) — pinned so a refactor cannot silently
#: change what a legacy session sends (D31 "byte-identical" requirement).
LEGACY_FACTS_HASH = "a5e2ea32ad3c1998"


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


def _book_entries() -> list[dict]:
    return [dict(entry) for entry in MARKET_BOOK["entries"]]


# -- legacy byte parity ------------------------------------------------------


def test_legacy_world_info_event_renders_byte_identical(build_log):
    log = build_log()
    sid = "legacy"
    log.append_event(sid, "user", "user", {"text": SCAN})
    log.append_event(
        sid, "system", "system", {"event": "world_info", "book": MARKET_BOOK}
    )
    assert session_world_info_block(log, sid, SCAN) == EXPECTED_LEGACY


def test_legacy_card_book_source_renders_byte_identical(build_log):
    log = build_log()
    sid = "card"
    card = {"name": "Alice", "description": "d", "character_book": MARKET_BOOK}
    log.append_event(sid, "system", "system", {"card": card, "first_mes": ""})
    assert session_world_info_block(log, sid, SCAN) == EXPECTED_LEGACY


def test_legacy_context_render_is_hash_pinned(build_log):
    log = build_log()
    sid = "legacy"
    log.append_event(sid, "user", "user", {"text": SCAN})
    log.append_event(
        sid, "system", "system", {"event": "world_info", "book": MARKET_BOOK}
    )
    joined = join_facts_suffix(context_suffix_parts(log, sid, user_text=SCAN))
    assert joined == EXPECTED_LEGACY
    assert facts_hash(joined) == LEGACY_FACTS_HASH


def test_no_entities_returns_none(build_log):
    log = build_log()
    log.append_event("plain", "user", "user", {"text": "hi"})
    assert session_world_info_block(log, "plain", "hi") is None
    assert session_world_block(log.get_events("plain")) is None


# -- entity sources ----------------------------------------------------------


def test_persona_attached_book_renders(build_log):
    log = build_log()
    sid = "persona"
    book_id = append_book(log, {"name": "Market lore", "entries": _book_entries()})
    persona_id = append_persona(log, {"name": "Alice", "book_ids": [book_id]})
    log.append_event(
        sid, "system", "user", {"event": "chat_persona", "persona_id": persona_id}
    )
    assert session_world_info_block(log, sid, SCAN) == EXPECTED_LEGACY


def test_card_name_fallback_resolves_persona_book(build_log):
    log = build_log()
    sid = "fallback"
    book_id = append_book(log, {"name": "Market lore", "entries": _book_entries()})
    append_persona(log, {"name": "Alice", "book_ids": [book_id]})
    # No chat_persona assignment; the card's name matches the persona.
    log.append_event(
        sid,
        "system",
        "system",
        {"card": {"name": "Alice", "description": "d"}, "first_mes": ""},
    )
    assert session_world_info_block(log, sid, SCAN) == EXPECTED_LEGACY


def test_session_books_registration_renders(build_log):
    log = build_log()
    sid = "registered"
    book_id = append_book(log, {"name": "Market lore", "entries": _book_entries()})
    log.append_event(
        sid, "system", "user", {"event": "session_books", "book_ids": [book_id]}
    )
    assert session_world_info_block(log, sid, SCAN) == EXPECTED_LEGACY


def test_unknown_book_ids_are_skipped(build_log):
    log = build_log()
    sid = "missing"
    log.append_event(
        sid, "system", "user", {"event": "session_books", "book_ids": ["ghost"]}
    )
    assert session_world_info_block(log, sid, SCAN) is None


# -- dedupe ------------------------------------------------------------------


def test_same_book_on_persona_and_card_renders_once(build_log):
    log = build_log()
    sid = "dedupe"
    book_id = append_book(log, {"name": "Market lore", "entries": _book_entries()})
    persona_id = append_persona(log, {"name": "Alice", "book_ids": [book_id]})
    log.append_event(
        sid, "system", "user", {"event": "chat_persona", "persona_id": persona_id}
    )
    # The SAME book also rides the card's character_book (a legacy source).
    log.append_event(
        sid,
        "system",
        "system",
        {
            "card": {"name": "Alice", "description": "d", "character_book": MARKET_BOOK},
            "first_mes": "",
        },
    )
    block = session_world_info_block(log, sid, SCAN)
    assert block == EXPECTED_LEGACY
    assert block.count("night market opens") == 1


def test_distinct_entries_are_not_deduped(build_log):
    log = build_log()
    sid = "distinct"
    first_id = append_book(log, {"name": "A", "entries": _book_entries()})
    second_id = append_book(
        log,
        {
            "name": "B",
            "entries": [
                {
                    "keys": ["lantern"],
                    "content": "A second, different lantern fact.",
                    "insertion_order": 3,
                }
            ],
        },
    )
    log.append_event(
        sid,
        "system",
        "user",
        {"event": "session_books", "book_ids": [first_id, second_id]},
    )
    block = session_world_info_block(log, sid, SCAN)
    assert block is not None
    assert "The night market opens" in block
    assert "A second, different lantern fact." in block


def test_entry_signature_matches_books_module(build_log):
    log = build_log()
    book_id = append_book(log, {"name": "M", "entries": _book_entries()})
    entries = parse_book(_book_entries())
    # The dedupe key used by the render is the books-module signature.
    assert all(entry_signature(entry) for entry in entries)
    assert book_id


def test_backfill_preserves_render_byte_identical(build_log):
    log = build_log()
    sid = "migrate"
    card = {"name": "Alice", "description": "d", "character_book": MARKET_BOOK}
    log.append_event(sid, "system", "system", {"card": card, "first_mes": ""})
    persona_id = append_persona(log, {"name": "Alice"})
    log.append_event(
        sid, "system", "user", {"event": "chat_persona", "persona_id": persona_id}
    )
    before = session_world_info_block(log, sid, SCAN)
    # D31: migrating the session-scoped book onto the persona must not change
    # the render — the pooled entries dedupe back to the single legacy line.
    backfill_books(log)
    assert session_world_info_block(log, sid, SCAN) == before == EXPECTED_LEGACY


# -- world prefix + world books ----------------------------------------------


def test_world_ref_renders_prefix_and_resolves_world_books(build_log):
    log = build_log()
    sid = "world"
    book_id = append_book(log, {"name": "World lore", "entries": _book_entries()})
    world_id = append_world(
        log,
        {"name": "Aurora Falls", "scenario": "It always rains.", "book_ids": [book_id]},
    )
    log.append_event(
        sid,
        "system",
        "user",
        {"event": "world_ref", "world_id": world_id, "name": "Aurora Falls", "scenario": "It always rains."},
    )
    block = session_world_info_block(log, sid, SCAN)
    assert block is not None
    assert block.startswith("### World\nAurora Falls\nScenario: It always rains.")
    assert EXPECTED_LEGACY in block
    # The world prefix sits AHEAD of the world-info block.
    assert block.index("### World") < block.index("### World info")


def test_session_world_block_uses_latest_ref(build_log):
    log = build_log()
    sid = "world-refs"
    log.append_event(
        sid, "system", "user", {"event": "world_ref", "name": "Old", "scenario": "old"}
    )
    log.append_event(
        sid, "system", "user", {"event": "world_ref", "name": "New", "scenario": "new"}
    )
    assert session_world_block(log.get_events(sid)) == "### World\nNew\nScenario: new"


def test_world_block_omits_empty_scenario(build_log):
    log = build_log()
    sid = "world-noscenario"
    log.append_event(sid, "system", "user", {"event": "world_ref", "name": "Aurora"})
    assert session_world_block(log.get_events(sid)) == "### World\nAurora"
