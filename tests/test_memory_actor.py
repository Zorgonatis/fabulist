"""Tests for the chat ACTOR record (D53) — :mod:`fabulist.memory.actor`.

D53 separates the chat's **cast** (``chat_actor``) from the **user persona**
(``chat_persona``). These tests pin the pure projection (``fold_actors``,
fork-aware latest-wins with ``[]`` clearing), the single-write helper
(``append_chat_actor``: dedupe + order, one event per call) and the legacy
card-derived fallback (``actors_from_cards``: match by name, skip no-match and
ambiguous).
"""

from __future__ import annotations

from fabulist.memory.actor import (
    CHAT_ACTOR_EVENT,
    actors_from_cards,
    append_chat_actor,
    fold_actors,
)
from fabulist.personas.registry import append_persona


def _append(log, session_id, payload):
    log.append_event(session_id, "system", "user", payload)


def _actor_event(log, session_id, ids):
    _append(log, session_id, {"event": CHAT_ACTOR_EVENT, "actor_ids": list(ids)})


def _card_event(log, session_id, name):
    log.append_event(session_id, "system", "system", {"card": {"name": name}})


# -- the frozen contract -----------------------------------------------------


def test_event_kind_is_frozen():
    assert CHAT_ACTOR_EVENT == "chat_actor"


# -- fold_actors -------------------------------------------------------------


def test_fold_actors_empty_without_event(log):
    assert fold_actors(log, "s1") == ()


def test_fold_actors_latest_wins(log):
    _actor_event(log, "s1", ["a"])
    _actor_event(log, "s1", ["b", "c"])
    assert fold_actors(log, "s1") == ("b", "c")


def test_fold_actors_empty_list_clears(log):
    _actor_event(log, "s1", ["a"])
    _actor_event(log, "s1", [])
    assert fold_actors(log, "s1") == ()


def test_fold_actors_only_reads_its_own_session(log):
    _actor_event(log, "s1", ["a"])
    _actor_event(log, "s2", ["b"])
    assert fold_actors(log, "s1") == ("a",)
    assert fold_actors(log, "s2") == ("b",)


def test_fold_actors_branch_inherits_parent(log):
    _append(log, "base", {"title": "t", "event": "session_created"})  # seq 1
    _actor_event(log, "base", ["a", "b"])  # seq 2
    log.fork_session("base", 2, "branch")
    assert fold_actors(log, "branch") == ("a", "b")


def test_fold_actors_branch_local_wins(log):
    _append(log, "base", {"title": "t", "event": "session_created"})  # seq 1
    _actor_event(log, "base", ["a"])  # seq 2
    log.fork_session("base", 2, "branch")
    _actor_event(log, "branch", ["b"])
    assert fold_actors(log, "branch") == ("b",)
    # The parent is untouched.
    assert fold_actors(log, "base") == ("a",)


def test_fold_actors_branch_clear_beats_inherited(log):
    _append(log, "base", {"title": "t", "event": "session_created"})  # seq 1
    _actor_event(log, "base", ["a"])  # seq 2
    log.fork_session("base", 2, "branch")
    _actor_event(log, "branch", [])
    assert fold_actors(log, "branch") == ()


def test_fold_actors_ignores_non_list_payload(log):
    _actor_event(log, "s1", ["a"])
    # A malformed row must NOT clear the previous record.
    _append(log, "s1", {"event": CHAT_ACTOR_EVENT, "actor_ids": "b"})
    _append(log, "s1", {"event": CHAT_ACTOR_EVENT, "actor_ids": None})
    _append(log, "s1", {"event": CHAT_ACTOR_EVENT})
    assert fold_actors(log, "s1") == ("a",)


def test_fold_actors_filters_non_string_members(log):
    _append(log, "s1", {"event": CHAT_ACTOR_EVENT, "actor_ids": ["a", 5, None, "", "b"]})
    assert fold_actors(log, "s1") == ("a", "b")


def test_fold_actors_dedupes_preserving_order(log):
    _append(log, "s1", {"event": CHAT_ACTOR_EVENT, "actor_ids": ["b", "a", "b"]})
    assert fold_actors(log, "s1") == ("b", "a")


def test_fold_actors_ignores_other_event_kinds_and_types(log):
    _append(log, "s1", {"event": "chat_persona", "persona_id": "a"})
    log.append_event("s1", "user", "user", {"event": CHAT_ACTOR_EVENT, "actor_ids": ["x"]})
    assert fold_actors(log, "s1") == ()


# -- append_chat_actor -------------------------------------------------------


def test_append_writes_exactly_one_event_per_call(log):
    append_chat_actor(log, "s1", ["a", "b"])
    append_chat_actor(log, "s1", ["c"])
    rows = [
        e
        for e in log.get_events("s1")
        if isinstance(e.payload, dict) and e.payload.get("event") == CHAT_ACTOR_EVENT
    ]
    assert len(rows) == 2
    assert log.get_events("s1")[-1].type == "system"


def test_append_dedupes_preserving_order(log):
    append_chat_actor(log, "s1", ["b", "a", "b", "a", "c"])
    assert fold_actors(log, "s1") == ("b", "a", "c")
    payload = log.get_events("s1")[-1].payload
    assert payload["actor_ids"] == ["b", "a", "c"]


def test_append_drops_non_string_and_empty(log):
    append_chat_actor(log, "s1", ["a", "", None, 7, "b"])
    assert fold_actors(log, "s1") == ("a", "b")


def test_append_empty_writes_a_clear(log):
    append_chat_actor(log, "s1", ["a"])
    append_chat_actor(log, "s1", [])
    assert fold_actors(log, "s1") == ()
    assert log.get_events("s1")[-1].payload["actor_ids"] == []


# -- actors_from_cards -------------------------------------------------------


def test_actors_from_cards_matches_by_name(log):
    alice = append_persona(log, {"name": "Alice"})
    bob = append_persona(log, {"name": "Bob"})
    _card_event(log, "chat", "Alice")
    _card_event(log, "chat", "Bob")
    assert actors_from_cards(log, log.get_events("chat")) == (alice, bob)


def test_actors_from_cards_skips_no_match(log):
    alice = append_persona(log, {"name": "Alice"})
    _card_event(log, "chat", "Alice")
    _card_event(log, "chat", "Nobody")
    assert actors_from_cards(log, log.get_events("chat")) == (alice,)


def test_actors_from_cards_skips_ambiguous_name(log):
    append_persona(log, {"name": "Twin"})
    append_persona(log, {"name": "Twin"})
    _card_event(log, "chat", "Twin")
    assert actors_from_cards(log, log.get_events("chat")) == ()


def test_actors_from_cards_dedupes_in_card_order(log):
    alice = append_persona(log, {"name": "Alice"})
    _card_event(log, "chat", "Alice")
    _card_event(log, "chat", "Alice")
    assert actors_from_cards(log, log.get_events("chat")) == (alice,)


def test_actors_from_cards_empty_when_catalog_empty(log):
    _card_event(log, "chat", "Alice")
    assert actors_from_cards(log, log.get_events("chat")) == ()


def test_actors_from_cards_ignores_non_card_and_malformed(log):
    alice = append_persona(log, {"name": "Alice"})
    log.append_event("chat", "user", "user", {"text": "hi"})
    log.append_event("chat", "system", "system", {"card": "not-a-dict"})
    log.append_event("chat", "system", "system", {"card": {"name": 42}})
    _card_event(log, "chat", "Alice")
    assert actors_from_cards(log, log.get_events("chat")) == (alice,)
