"""Tests for D43 macro semantics (:mod:`fabulist.prompts.variables` + cast).

Covers the ST-alignment contract: ``{{user}}`` = the session's selected user
persona NAME (live), ``{{message}}`` = the current message text, ``{{char}}`` =
the session cast rendered ``[cast: a, b, c]`` and **frozen at first
consumption** (prompt-cache protection; the studio-delta notice is covered in
``tests/test_cast_delta.py``), ``{{world}}`` = the ``world_ref`` snapshot name.

Seeded like ``tests/test_prompt_variables.py``: a tmp-path
:class:`~fabulist.log.EventLog` closed in teardown, with ``append_event`` rows
(the resolver reads the log only, D9).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.prompts.cast import (
    CAST_SNAPSHOT_EVENT,
    ensure_cast_snapshot,
    render_cast,
)
from fabulist.prompts.variables import PERSONAS_ANCHOR, resolve_variables

LYRA = {"name": "Lyra Voss", "first_mes": "Lyra looks up from her maps."}
CORIN = {"name": "Corin Hale", "first_mes": "Corin sharpens his blade."}
MIRA = {"name": "Mira Quill", "first_mes": "Mira closes her ledger."}


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


def _assign_persona(log: EventLog, sid: str, persona_id: str, name: str) -> None:
    """Seed a persona definition on the anchor + a latest-wins chat assignment."""
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": persona_id, "name": name},
    )
    log.append_event(
        sid, "system", "user", {"event": "chat_persona", "persona_id": persona_id}
    )


def _seed(log: EventLog, cards: list[dict]) -> str:
    """Seed a chat: cards, persona assignment, one user turn."""
    sid = "chat1"
    log.append_event(sid, "system", "user", {"event": "session_created"})
    for card in cards:
        log.append_event(
            sid, "system", "system", {"card": card, "first_mes": card["first_mes"]}
        )
    _assign_persona(log, sid, "p1", "Alice")
    log.append_event(sid, "user", "user", {"text": "I open the door."})
    return sid


def _cast_snapshots(log: EventLog, sid: str) -> list:
    """Every ``cast_snapshot`` system event on the session, in seq order."""
    return [
        event
        for event in log.get_events(sid)
        if event.type == "system"
        and isinstance(event.payload, dict)
        and event.payload.get("event") == CAST_SNAPSHOT_EVENT
    ]


# --- user / message split (ST semantics) ------------------------------------


def test_user_is_persona_name_not_message_text(build_log):
    log = build_log()
    sid = _seed(log, [LYRA])
    values = resolve_variables(log, sid, "I open the door.")
    assert values["user"] == "Alice"
    assert values["user"] != "I open the door."


def test_message_is_current_user_text(build_log):
    log = build_log()
    sid = _seed(log, [LYRA])
    values = resolve_variables(log, sid, "I open the door.")
    assert values["message"] == "I open the door."


def test_user_stays_live_across_a_persona_reassignment(build_log):
    log = build_log()
    sid = _seed(log, [LYRA])
    assert resolve_variables(log, sid, "one")["user"] == "Alice"
    # A deliberate reassignment is an owner act and must take effect (D43).
    _assign_persona(log, sid, "p2", "Bob")
    assert resolve_variables(log, sid, "two")["user"] == "Bob"


def test_user_falls_back_to_literal_word_without_a_persona(build_log):
    """PART 2: no persona in effect -> ``{{user}}`` is the literal word "user".

    Not an empty string (which would garble card text) and not the raw token.
    """
    log = build_log()
    sid = "plain"
    log.append_event(sid, "user", "user", {"text": "Hello."})
    values = resolve_variables(log, sid, "Hello.")
    assert values["user"] == "user"
    # The parent-persona spec stays empty for a sandbox chat.
    assert values["persona"] == ""
    assert values["message"] == "Hello."


# --- char: multi-card cast + freeze -----------------------------------------


def test_char_renders_all_cards_in_declaration_order(build_log):
    log = build_log()
    sid = _seed(log, [LYRA, CORIN, MIRA])
    values = resolve_variables(log, sid, "I open the door.")
    assert values["char"] == "[cast: Lyra Voss, Corin Hale, Mira Quill]"


def test_char_single_card_still_uses_cast_form(build_log):
    log = build_log()
    sid = _seed(log, [LYRA])
    assert resolve_variables(log, sid, "hi")["char"] == "[cast: Lyra Voss]"


def test_char_is_empty_without_cards(build_log):
    log = build_log()
    sid = "plain"
    log.append_event(sid, "user", "user", {"text": "Hello."})
    assert resolve_variables(log, sid, "Hello.")["char"] == ""


def test_char_value_is_frozen_at_first_consumption(build_log):
    """A card event landing mid-chat must NOT change ``{{char}}`` (D43).

    The freeze is deliberate prompt-cache protection: ``{{char}}`` feeds the
    static prefix. A later studio edit to a session's cast is surfaced as the
    DYNAMIC ``cast_delta_notice`` (tests/test_cast_delta.py), never by thawing
    this value.
    """
    log = build_log()
    sid = _seed(log, [LYRA, CORIN])
    assert resolve_variables(log, sid, "first")["char"] == "[cast: Lyra Voss, Corin Hale]"

    # The roster changes at "turn 50": a new card lands after first consumption.
    log.append_event(
        sid, "system", "system", {"card": MIRA, "first_mes": MIRA["first_mes"]}
    )
    assert resolve_variables(log, sid, "second")["char"] == (
        "[cast: Lyra Voss, Corin Hale]"
    )


def test_cast_snapshot_appended_exactly_once(build_log):
    log = build_log()
    sid = _seed(log, [LYRA, CORIN])
    resolve_variables(log, sid, "first")
    resolve_variables(log, sid, "second")
    assert len(_cast_snapshots(log, sid)) == 1
    # ensure_cast_snapshot is idempotent on its own too.
    ensure_cast_snapshot(log, sid)
    assert len(_cast_snapshots(log, sid)) == 1


def test_ensure_cast_snapshot_reads_a_pre_existing_snapshot_verbatim(build_log):
    log = build_log()
    sid = _seed(log, [LYRA, CORIN])
    log.append_event(
        sid,
        "system",
        "system",
        {"event": CAST_SNAPSHOT_EVENT, "cast": "[cast: Frozen, Forever]"},
    )
    # A stored snapshot wins over a fresh evaluation of the live roster.
    assert ensure_cast_snapshot(log, sid) == "[cast: Frozen, Forever]"
    assert len(_cast_snapshots(log, sid)) == 1


def test_render_cast_skips_blank_names_and_handles_empty():
    assert render_cast([]) == ""
    assert render_cast([{"name": "  "}, {}]) == ""
    assert render_cast([{"name": "A"}, {"name": ""}, {"name": "B"}]) == "[cast: A, B]"


# --- world ------------------------------------------------------------------


def test_world_returns_the_snapshot_name(build_log):
    log = build_log()
    sid = _seed(log, [LYRA])
    log.append_event(
        sid,
        "system",
        "user",
        {
            "event": "world_ref",
            "world_id": "w1",
            "name": "The Rain-Soaked Market",
            "scenario": "A night market under storm.",
        },
    )
    assert resolve_variables(log, sid, "hi")["world"] == "The Rain-Soaked Market"


def test_world_is_empty_without_a_world_ref(build_log):
    log = build_log()
    sid = _seed(log, [LYRA])
    assert resolve_variables(log, sid, "hi")["world"] == ""


# --- registry + render integration ------------------------------------------


def test_specs_expose_message_and_world_names():
    from fabulist.prompts.variables import VARIABLE_SPECS

    names = {spec.name for spec in VARIABLE_SPECS}
    assert {"user", "message", "char", "world"} <= names


def test_resolved_macros_render_into_a_template(build_log):
    from fabulist.prompts.variables import render_template

    log = build_log()
    sid = _seed(log, [LYRA, CORIN])
    values = resolve_variables(log, sid, "I knock twice.")
    out = render_template("{{user}} vs {{char}}: {{message}}", values)
    assert out == "Alice vs [cast: Lyra Voss, Corin Hale]: I knock twice."
