"""Tests for the World Info v1 engine (PLAN §2: keywords + recursion; uncapped).

The character budget was removed by the owner ruling of 2026-10-26 (issue #83):
every activated entry renders. The render pins here moved from the cap/break
behaviour to the uncapped behaviour.

Covers :func:`parse_book`, :func:`activate`, :func:`render_block` and the
``WorldInfoError`` contract. Everything here is pure: no I/O, no event log.
"""

from __future__ import annotations

import pytest

from fabulist.context.world_info import (
    BLOCK_HEADER,
    WorldInfoEntry,
    WorldInfoError,
    activate,
    parse_book,
    render_block,
)

# --- representative inputs ---------------------------------------------------------

# The exact shape ``cards/v2.py`` keeps raw on ``CardV2.character_book``.
CARD_BOOK = {
    "name": "Night Market lore",
    "entries": [
        {
            "keys": ["night market", "lantern"],
            "content": "The night market opens only after the second bell.",
            "enabled": True,
            "constant": False,
            "selective": False,
            "insertion_order": 10,
            "case_sensitive": False,
            "comment": "market",
        },
        {
            "keys": ["vermin"],
            "content": "Vermin are unwelcome here.",
            "enabled": True,
            "constant": False,
            "selective": True,
            "insertion_order": 20,
        },
        {
            "keys": ["disabled ghost"],
            "content": "Should never appear.",
            "enabled": False,
            "insertion_order": 5,
        },
    ],
}


def entry(keys, content, **kwargs) -> WorldInfoEntry:
    """Small constructor helper so tests read as intent, not plumbing."""

    return WorldInfoEntry(keys=list(keys), content=content, **kwargs)


# --- parse_book: accepted shapes ---------------------------------------------------


def test_parse_card_character_book_list_form():
    entries = parse_book(CARD_BOOK)
    # The disabled entry is dropped, the other two survive in source order.
    assert [e.content for e in entries] == [
        "The night market opens only after the second bell.",
        "Vermin are unwelcome here.",
    ]
    first = entries[0]
    assert first.keys == ["night market", "lantern"]
    assert first.order == 10
    assert first.constant is False
    assert first.case_sensitive is False
    assert first.comment == "market"


def test_parse_defaults_for_missing_optional_fields():
    (parsed,) = parse_book({"entries": [{"keys": ["k"], "content": "c"}]})
    assert parsed.constant is False
    assert parsed.order == 0
    assert parsed.case_sensitive is False
    assert parsed.comment == ""
    assert parsed.keys == ["k"]


def test_parse_raw_world_info_list_form():
    raw = [
        {"keys": ["a"], "content": "alpha", "insertion_order": 1},
        {"keys": ["b"], "content": "beta", "insertion_order": 2},
    ]
    entries = parse_book(raw)
    assert [e.content for e in entries] == ["alpha", "beta"]


def test_parse_raw_world_info_dict_of_entries_form():
    # Standalone ST files sometimes key entries by numeric id.
    raw = {
        "0": {"keys": ["a"], "content": "alpha", "insertion_order": 2},
        "1": {"keys": ["b"], "content": "beta", "insertion_order": 1},
    }
    entries = parse_book(raw)
    assert [e.content for e in entries] == ["alpha", "beta"]  # source (id) order


def test_parse_character_book_entries_as_mapping():
    book = {"entries": {"7": {"keys": ["a"], "content": "alpha"}}}
    (parsed,) = parse_book(book)
    assert parsed.content == "alpha"


def test_parse_skips_disabled_entries():
    book = {
        "entries": [
            {"keys": ["keep"], "content": "keep", "enabled": True},
            {"keys": ["drop"], "content": "drop", "enabled": False},
        ]
    }
    entries = parse_book(book)
    assert [e.content for e in entries] == ["keep"]


def test_parse_drops_empty_keys_but_keeps_rest():
    (parsed,) = parse_book({"entries": [{"keys": ["", "real"], "content": "c"}]})
    assert parsed.keys == ["real"]


def test_parse_accepts_singular_key_field():
    (parsed,) = parse_book({"entries": [{"key": "solo", "content": "c"}]})
    assert parsed.keys == ["solo"]


def test_parse_empty_book_is_empty_list():
    assert parse_book({"entries": []}) == []
    assert parse_book([]) == []


# --- parse_book: error contract ----------------------------------------------------


@pytest.mark.parametrize("garbage", ["nope", 123, None, 3.14])
def test_parse_rejects_non_book_types(garbage):
    with pytest.raises(WorldInfoError):
        parse_book(garbage)


def test_parse_rejects_entry_that_is_not_an_object():
    with pytest.raises(WorldInfoError, match="entry #1"):
        parse_book([{"keys": ["k"], "content": "c"}, "not an entry"])


def test_parse_rejects_non_list_entries_field():
    with pytest.raises(WorldInfoError, match="'entries' must be a list or mapping"):
        parse_book({"entries": "nope"})


def test_parse_rejects_non_string_content():
    with pytest.raises(WorldInfoError, match="'content' must be a string"):
        parse_book({"entries": [{"keys": ["k"], "content": 5}]})


def test_parse_rejects_non_list_keys():
    with pytest.raises(WorldInfoError, match="'keys' must be a list"):
        parse_book({"entries": [{"keys": {"a": 1}, "content": "c"}]})


def test_parse_rejects_bare_entry_dict_as_book():
    # A lone entry is not a book: its values are not entry objects.
    with pytest.raises(WorldInfoError, match="must be an object"):
        parse_book({"keys": ["k"], "content": "c"})


# --- activate: keyword semantics ---------------------------------------------------


def test_activate_keyword_hit_is_case_insensitive_by_default():
    entries = [entry(["Alpha"], "hit")]
    assert activate(entries, "the alpha protocol") == entries


def test_activate_case_sensitive_respects_case_both_ways():
    entries = [entry(["Alpha"], "hit", case_sensitive=True)]
    assert activate(entries, "the alpha protocol") == []  # lowercase miss
    assert activate(entries, "the Alpha protocol") == entries  # exact hit


def test_activate_any_key_in_list_matches():
    entries = [entry(["miss", "second"], "hit")]
    assert activate(entries, "the second bell") == entries


def test_activate_constant_entry_always_active():
    entries = [entry(["never-mentioned"], "always", constant=True)]
    assert activate(entries, "unrelated scan text") == entries


def test_activate_no_match_returns_constants_only():
    const = entry(["x"], "always", constant=True, order=0)
    normal = entry(["absent"], "never", order=1)
    assert activate([normal, const], "nothing relevant") == [const]


def test_activate_selective_is_ignored_at_v1():
    # selective=True must not change v1 matching in either direction.
    hit = entry(["present"], "hit", comment="selective on")
    miss = entry(["absent"], "miss")
    active = activate([hit, miss], "this scan has present inside")
    assert active == [hit]


def test_activate_returns_entries_sorted_by_insertion_order():
    entries = [
        entry(["a"], "third", order=30),
        entry(["b"], "first", order=10),
        entry(["c"], "second", order=20),
    ]
    scan = "a b c"
    assert [e.content for e in activate(entries, scan)] == ["first", "second", "third"]


def test_activate_sort_is_stable_for_ties():
    entries = [
        entry(["a"], "first", order=0),
        entry(["b"], "second", order=0),
    ]
    assert [e.content for e in activate(entries, "a b")] == ["first", "second"]


# --- activate: recursion-lite ------------------------------------------------------


def test_activate_recursion_pulls_in_content_referenced_entry():
    a = entry(["ALPHA"], "BETA clearance granted")
    b = entry(["BETA"], "secondary")
    # B's key is absent from the scan text; only A's content names it.
    active = activate([a, b], "the ALPHA protocol is active")
    assert [e.content for e in active] == ["BETA clearance granted", "secondary"]


def test_activate_recursion_depth_is_capped_at_one():
    a = entry(["ALPHA"], "BETA clearance granted")
    b = entry(["BETA"], "GAMMA reactor online")
    c = entry(["GAMMA"], "should not fire")
    active = activate([a, b, c], "the ALPHA protocol is active")
    assert [e.content for e in active] == ["BETA clearance granted", "GAMMA reactor online"]


def test_activate_recursion_depth_zero_disables_second_pass():
    a = entry(["ALPHA"], "BETA clearance granted")
    b = entry(["BETA"], "secondary")
    active = activate([a, b], "the ALPHA protocol is active", recursion_depth=0)
    assert active == [a]


def test_activate_recursion_uses_first_pass_content_only():
    # A fires on the scan; A's content names B; B's content names C. C must not
    # fire because the second pass is never rescanned.
    a = entry(["ALPHA"], "see BETA")
    b = entry(["BETA"], "see GAMMA")
    c = entry(["GAMMA"], "deep")
    active = activate([a, b, c], "ALPHA")
    assert c not in active
    assert {e.content for e in active} == {"see BETA", "see GAMMA"}


# --- render_block ------------------------------------------------------------------


def test_render_block_formats_header_and_lines_in_order():
    entries = [
        entry([], "second", order=2),
        entry([], "first", order=1),
    ]
    rendered = render_block(entries)
    assert rendered == f"{BLOCK_HEADER}\n- first\n- second"


def test_render_block_none_when_no_entries():
    assert render_block([]) is None


def test_render_block_renders_an_entry_larger_than_the_old_budget():
    # #83: no size filter. The single 1300-char entry used to return None when
    # it could not fit the 1200-char budget; it now renders whole.
    content = "x" * 1300
    rendered = render_block([entry([], content)])
    assert rendered == f"{BLOCK_HEADER}\n- {content}"


def test_render_block_renders_every_entry_past_the_old_budget():
    # #83: the fill loop no longer breaks on the first entry that does not fit,
    # so entries after the old 1200-char line — including small ones — render.
    entries = [
        entry([], "a" * 992, order=0),
        entry([], "b" * 300, order=1),
        entry([], "c" * 90, order=2),
    ]
    rendered = render_block(entries)
    assert rendered == f"{BLOCK_HEADER}\n- {'a' * 992}\n- {'b' * 300}\n- {'c' * 90}"
    # Every activated entry is present, in insertion order, uncapped.
    assert len(rendered) > 1200


def test_render_block_never_truncates_mid_entry():
    entries = [entry([], "one", order=0), entry([], "x" * 5000, order=1)]
    rendered = render_block(entries)
    # Whole entries only: both render in full, no partial content.
    assert rendered == f"{BLOCK_HEADER}\n- one\n- {'x' * 5000}"


def test_render_block_has_no_budget_parameter():
    # #83: the parameter is gone; passing one is a TypeError, not a silent drop.
    with pytest.raises(TypeError):
        render_block([], budget=25)  # type: ignore[call-arg]


def test_render_block_stable_order_for_ties():
    entries = [
        entry([], "first", order=0),
        entry([], "second", order=0),
    ]
    assert render_block(entries) == f"{BLOCK_HEADER}\n- first\n- second"
