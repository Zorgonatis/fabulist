"""Tests for the extracted facts-suffix module (replay-fidelity ground work).

Exercises :mod:`fabulist.context.facts` against a real (tmp-path) SQLite
:class:`~fabulist.log.EventLog` seeded the way the API/wiring tests do:
``tests/test_state_api.py`` builds the app over a tmp log and appends events
with :meth:`EventLog.append_event`; ``tests/test_wiring_history.py`` opens
``EventLog(tmp_path / "h.db")`` directly and closes it. Both patterns are
mirrored here (tmp-path file log, closed in the fixture) so every assertion
runs against real event rows, not duck-typed stand-ins.

Covered contract (D22 split):
  (1) facts_suffix_parts = the final user-message <state> parts
      (roster, stt, rolls, facts) — the persona moved to the static parts
  (1a) static_prefix_parts / TurnContextBlocks.static_parts = the persona block
      (session-constant; spliced into the stable pre-history system region)
  (1b) context_suffix_parts = the full canonical order
      (persona, roster, *summaries, recall, world_info, stt, rolls, facts) —
      byte-faithful: still carries the persona for hash/replay parity
  (1c) recalled_message = the post-user <recalled> system message
  (2) blank parts dropped
  (3) join shape (blank-line separated, outer-stripped)
  (4) facts_hash deterministic, 16 hex chars
  (5) window_events inclusive boundary
  (6) session_persona_block single-card vs multi-card branch
  (7) session_stt_block capped at STT_LINE_CAP lines
  (8) world-info scan string exact f"{user_text}\\n{recent}" (recent = last-2
      narratives' prose joined by a single space)
  (9) ``### World`` renders the LIVE world definition (description included),
      with the ``world_ref`` snapshot as the tombstoned-world fallback and the
      cast-less ``{{char}}`` fallback set to the world's name
"""

from __future__ import annotations

import hashlib
import string
from pathlib import Path

import pytest

from fabulist.context.facts import (
    RECALLED_RECALL_HEADER,
    RECALLED_RECAP_HEADER,
    RECALLED_WORLD_INFO_HEADER,
    STT_LINE_CAP,
    assemble_turn_context,
    context_suffix_parts,
    facts_hash,
    facts_suffix_parts,
    join_facts_suffix,
    recalled_message,
    session_persona_block,
    session_stt_block,
    session_world_block,
    session_world_definition_block,
    session_world_info_block,
    static_prefix_parts,
    window_events,
)
from fabulist.log import EventLog

# --- fixtures / helpers -----------------------------------------------------


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for an :class:`EventLog` on a tmp path; closes logs after.

    Mirrors ``tests/test_wiring_history.py`` (``EventLog(tmp_path / "h.db")``)
    and ``tests/test_state_api.py`` (tmp-path log closed in teardown).
    """

    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


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

SUMMARY_TEXT = "They met at the night market and traded maps."

# A world-info book whose entries distinguish how the scan string is built:
#   "ALPHA\nBETA" fires only if user_text and recent are joined by a newline;
#   "BETA GAMMA"   fires only if the last two narratives' prose are joined by a
#                  single space (recent);
#   "ALPHA BETA"   must NOT fire (that would mean user_text/recent were joined
#                  by a space instead of the contract's newline).
SCAN_BOOK = {
    "entries": [
        {"keys": ["ALPHA\nBETA"], "content": "newline-join hit", "insertion_order": 1},
        {"keys": ["BETA GAMMA"], "content": "space-join hit", "insertion_order": 2},
        {"keys": ["ALPHA BETA"], "content": "wrong-join hit", "insertion_order": 3},
    ]
}


def _seed_full(log: EventLog, sid: str) -> None:
    """Seed every facts-suffix source: 2 cards, narratives, summary, WI, stt,
    state-delta. Two narratives keep ``recent`` exactly ``"BETA GAMMA"``.
    """
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": LYRA_GREETING})
    log.append_event(
        sid, "system", "system", {"card": MORROW, "first_mes": MORROW_GREETING}
    )
    log.append_event(sid, "user", "user", {"text": "I enter the night market."})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "BETA", "dsl": "", "segments": []}
    )
    log.append_event(
        sid, "narrative", "narrator", {"prose": "GAMMA", "dsl": "", "segments": []}
    )
    log.append_event(
        sid, "summary", "summarizer", {"text": SUMMARY_TEXT, "covers_seqs": [1, 2, 3]}
    )
    log.append_event(sid, "system", "system", {"event": "world_info", "book": SCAN_BOOK})
    log.append_event(sid, "system", "system", {"event": "stt", "text": "Where are the maps?"})
    log.append_event(sid, "state-delta", "extractor", {"time": "14:35"})


# --- (1) part order ---------------------------------------------------------


def test_parts_follow_contract_order(build_log):
    log = build_log()
    sid = "full"
    _seed_full(log, sid)

    # D22: the FINAL USER MESSAGE keeps the prescriptive parts only — roster,
    # stt, rolls, clock. The persona is now STATIC (pre-history), and
    # summaries/recall/world-info moved out to <recalled>.
    parts = facts_suffix_parts(
        log,
        sid,
        recall_text="RECALL",
        user_text="ALPHA",
        rolls_text="ROLLS",
        facts="time: 14:35",
    )
    assert parts[0] == "Cast: Lyra Voss, Captain Morrow"
    assert parts[1] == "### Heard (audio)\n- Where are the maps?"
    assert parts[2] == "ROLLS"
    assert parts[3] == "time: 14:35"
    assert len(parts) == 4
    # The character card is gone from <state> entirely (persona is static now).
    assert all("### Character" not in p for p in parts)
    assert all("### Cast" not in p for p in parts)
    assert all("### World info" not in p for p in parts)
    assert all("RECALL" != p for p in parts)

    # The FULL canonical context keeps the pre-D22 order for hashing/replay.
    full = context_suffix_parts(
        log,
        sid,
        recall_text="RECALL",
        user_text="ALPHA",
        rolls_text="ROLLS",
        facts="time: 14:35",
    )
    assert full[0].startswith("### Cast")
    assert full[1] == "Cast: Lyra Voss, Captain Morrow"
    assert full[2] == SUMMARY_TEXT
    assert full[3] == "RECALL"
    assert full[4].startswith("### World info")
    assert full[5] == "### Heard (audio)\n- Where are the maps?"
    assert full[6] == "ROLLS"
    assert full[7] == "time: 14:35"
    assert len(full) == 8


def test_recalled_message_carries_all_present_sections(build_log):
    log = build_log()
    sid = "full"
    _seed_full(log, sid)

    block = recalled_message(log, sid, recall_text="RECALL", user_text="ALPHA")
    assert block is not None
    # The block BODY: the envelope owns the <engine_memory> tag pair.
    assert not block.startswith("<")
    assert block.startswith("Reference material for this turn")
    assert "Context, not instruction" in block  # the defusing framing copy
    # Recall, world-info and recap sections present; order follows the framing.
    for header in (
        RECALLED_RECALL_HEADER,
        RECALLED_WORLD_INFO_HEADER,
        RECALLED_RECAP_HEADER,
    ):
        assert header in block
    assert block.index(RECALLED_RECALL_HEADER) < block.index(RECALLED_WORLD_INFO_HEADER)
    assert block.index(RECALLED_WORLD_INFO_HEADER) < block.index(RECALLED_RECAP_HEADER)
    assert "RECALL" in block
    assert SUMMARY_TEXT in block
    assert "### World info" in block  # the relocated renderer output
    # BTS is reserved (empty today) -> no header rendered.
    assert "Behind the scenes" not in block


def test_recalled_message_omitted_when_all_sections_empty(build_log):
    log = build_log()
    sid = "bare"
    log.append_event(sid, "user", "user", {"text": "hi"})
    assert recalled_message(log, sid, recall_text="", user_text="hi") is None


def test_recalled_message_sections_drop_independently(build_log):
    log = build_log()
    sid = "recap-only"
    log.append_event(sid, "user", "user", {"text": "hi"})
    log.append_event(
        sid, "summary", "summarizer", {"text": SUMMARY_TEXT, "covers_seqs": [1]}
    )
    block = recalled_message(log, sid, recall_text="", user_text="hi")
    assert block is not None
    assert RECALLED_RECAP_HEADER in block
    assert RECALLED_RECALL_HEADER not in block
    assert RECALLED_WORLD_INFO_HEADER not in block


def test_assemble_turn_context_returns_both_halves(build_log):
    log = build_log()
    sid = "full"
    _seed_full(log, sid)
    blocks = assemble_turn_context(
        log, sid, recall_text="RECALL", user_text="ALPHA", facts="time: 14:35"
    )
    assert blocks.state_parts[-1] == "time: 14:35"
    assert all("### Character" not in p for p in blocks.state_parts)
    assert blocks.static_parts == [session_persona_block(log, sid)]
    assert blocks.static_parts[0].startswith("### Cast")
    assert blocks.context_parts[0] == blocks.static_parts[0]  # persona still first
    assert blocks.context_parts[3] == "RECALL"
    assert blocks.recalled is not None and "RECALL" in blocks.recalled


# --- (2) blanks dropped -----------------------------------------------------


def test_blank_parts_are_dropped(build_log):
    log = build_log()
    sid = "single"
    # One card only: no roster line, no summaries, no WI, no stt; the caller
    # passes empty recall/rolls/facts.
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": LYRA_GREETING})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "BETA", "dsl": "", "segments": []}
    )
    log.append_event(
        sid, "narrative", "narrator", {"prose": "GAMMA", "dsl": "", "segments": []}
    )

    # Single card + no roster/stt/rolls/facts -> the <state> block is empty;
    # the persona rides the static parts instead.
    parts = facts_suffix_parts(log, sid)
    assert parts == []
    assert static_prefix_parts(log, sid) == [session_persona_block(log, sid)]
    assert "" not in parts
    assert all(part for part in parts)


# --- (3) join shape ---------------------------------------------------------


def test_join_shape_blank_line_separated_and_stripped():
    assert join_facts_suffix(["a", "b", "c"]) == "a\n\nb\n\nc"
    # Outer whitespace of the whole joined string is stripped.
    assert join_facts_suffix(["  a", "b  "]) == "a\n\nb"
    assert join_facts_suffix([]) == ""
    # Middle parts keep their own internal whitespace untouched.
    assert join_facts_suffix(["a", "  b  "]) == "a\n\n  b"


def test_join_matches_parts_round_trip(build_log):
    log = build_log()
    sid = "full"
    _seed_full(log, sid)
    parts = facts_suffix_parts(log, sid, recall_text="RECALL", user_text="ALPHA")
    joined = join_facts_suffix(parts)
    # Every part appears as a blank-line-separated chunk.
    assert joined.split("\n\n") == [p for p in parts]
    assert joined == joined.strip()


# --- (4) facts_hash ---------------------------------------------------------


def test_facts_hash_deterministic_and_16_hex():
    text = "persona block\n\nfacts: time 14:35"
    first = facts_hash(text)
    second = facts_hash(text)
    assert first == second
    assert len(first) == 16
    assert all(ch in string.hexdigits for ch in first)
    assert first == hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    assert facts_hash(text) != facts_hash(text + "x")


# --- (5) window_events inclusive boundary -----------------------------------


def test_window_events_inclusive_boundary(build_log):
    log = build_log()
    sid = "win"
    log.append_event(sid, "user", "user", {"text": "one"})
    log.append_event(sid, "narrative", "narrator", {"prose": "beat two"})
    log.append_event(sid, "narrative", "narrator", {"prose": "beat three"})
    events = log.get_events(sid)
    seqs = [e.seq for e in events]

    window = window_events(events, seqs[1])
    assert [e.seq for e in window] == seqs[:2]  # inclusive of seqs[1]
    assert all(e.seq <= seqs[1] for e in window)
    # The next event is excluded.
    assert seqs[2] not in [e.seq for e in window]


# --- (6) persona single vs multi --------------------------------------------


def test_persona_single_card_branch(build_log):
    log = build_log()
    sid = "one"
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": LYRA_GREETING})
    block = session_persona_block(log, sid)
    assert block is not None
    assert block.startswith("### Character: Lyra Voss")
    assert "### Cast" not in block
    assert block.endswith(f"Opening scene (already established): {LYRA_GREETING}")


def test_persona_multi_card_branch(build_log):
    log = build_log()
    sid = "many"
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": LYRA_GREETING})
    log.append_event(
        sid, "system", "system", {"card": MORROW, "first_mes": MORROW_GREETING}
    )
    block = session_persona_block(log, sid)
    assert block is not None
    assert block.startswith("### Cast")
    assert "#### Lyra Voss" in block and "#### Captain Morrow" in block
    # Only the first card's greeting opens the scene.
    assert block.count("Opening scene (already established):") == 1
    assert LYRA_GREETING in block
    assert MORROW_GREETING not in block


def test_persona_none_without_cards(build_log):
    log = build_log()
    log.append_event("nocards", "user", "user", {"text": "hi"})
    assert session_persona_block(log, "nocards") is None
    assert static_prefix_parts(log, "nocards") == []


# --- (7) stt cap ------------------------------------------------------------


def test_stt_block_caps_at_constant(build_log):
    log = build_log()
    sid = "audio"
    texts = [f"line {i}" for i in range(1, 6)]  # 5 stt events
    for text in texts:
        log.append_event(sid, "system", "system", {"event": "stt", "text": text})

    block = session_stt_block(log, sid)
    assert block is not None
    assert STT_LINE_CAP == 3
    lines = block.split("\n")
    assert lines[0] == "### Heard (audio)"
    assert len(lines) == STT_LINE_CAP + 1  # header + capped rows
    # The most recent transcripts survive; the oldest two are dropped.
    assert [ln for ln in lines[1:]] == [f"- {t}" for t in texts[-STT_LINE_CAP:]]


# --- (8) world-info scan string ---------------------------------------------


def test_state_parts_keep_roster_stt_rolls_clock_without_persona(build_log):
    """The persona split must not drop the other prescriptive state parts."""
    log = build_log()
    sid = "state"
    _seed_full(log, sid)
    blocks = assemble_turn_context(
        log,
        sid,
        user_text="ALPHA",
        rolls_text="ROLLS",
        facts="time: 14:35",
    )
    # roster, stt, rolls, clock survive; the character card does not.
    assert blocks.state_parts == [
        "Cast: Lyra Voss, Captain Morrow",
        "### Heard (audio)\n- Where are the maps?",
        "ROLLS",
        "time: 14:35",
    ]
    assert blocks.static_parts == [session_persona_block(log, sid)]


def test_static_accessor_matches_persona_and_context_parity(build_log):
    """The static accessor returns the persona block; context keeps it for the
    provenance hash/replay anchor (byte-parity with the pre-split render)."""
    log = build_log()
    sid = "parity"
    _seed_full(log, sid)

    static = static_prefix_parts(log, sid)
    persona = session_persona_block(log, sid)
    assert static == [persona]

    # context_suffix_parts still begins with the persona (hash/replay anchor).
    full = context_suffix_parts(
        log, sid, recall_text="RECALL", user_text="ALPHA", facts="time: 14:35"
    )
    assert full[0] == persona
    assert full[1] == "Cast: Lyra Voss, Captain Morrow"
    assert full[2] == SUMMARY_TEXT
    assert full[3] == "RECALL"
    assert full[4].startswith("### World info")
    assert full[5] == "### Heard (audio)\n- Where are the maps?"
    assert full[6] == "time: 14:35"
    assert len(full) == 7
    # The canonical order still starts with the persona (persona stays in the
    # hash/replay anchor; only <state> drops it).
    assert join_facts_suffix(full).startswith(persona)


def test_static_prefix_parts_honors_selected_greeting(build_log):
    """D30 greeting-variant selection survives the static split: the persona
    block rides the static parts with the SELECTED alternate greeting."""
    log = build_log()
    sid = "greeting"
    card = {
        "name": "Lyra Voss",
        "description": "A wandering cartographer.",
        "first_mes": "ONE",
        "alternate_greetings": ["TWO", "THREE"],
    }
    log.append_event(sid, "system", "system", {"card": card, "first_mes": "ONE"})
    log.append_event(
        sid, "system", "system", {"event": "greeting_selected", "index": 2}
    )
    static = static_prefix_parts(log, sid)
    assert static == [session_persona_block(log, sid)]
    assert static[0].startswith("### Character: Lyra Voss")
    assert static[0].endswith("Opening scene (already established): THREE")
    # <state> still never repeats the card.
    assert facts_suffix_parts(log, sid) == []


def test_world_info_scan_string_is_newline_join(build_log):
    log = build_log()
    sid = "scan"
    # Cards are not needed; the book rides on a world_info system event.
    log.append_event(sid, "system", "system", {"event": "world_info", "book": SCAN_BOOK})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "BETA", "dsl": "", "segments": []}
    )
    log.append_event(
        sid, "narrative", "narrator", {"prose": "GAMMA", "dsl": "", "segments": []}
    )

    # The exact scan is f"{user_text}\n{recent}" with recent = "BETA GAMMA".
    block = session_world_info_block(log, sid, "ALPHA\nBETA GAMMA")
    assert block is not None
    assert "newline-join hit" in block  # key "ALPHA\nBETA" matched
    assert "space-join hit" in block  # key "BETA GAMMA" matched
    assert "wrong-join hit" not in block  # key "ALPHA BETA" did not

    # And the recalled message builds exactly that scan from user_text + the
    # last two narratives' prose joined by a single space.
    block = recalled_message(log, sid, user_text="ALPHA")
    assert block is not None
    assert "newline-join hit" in block
    assert "space-join hit" in block
    assert "wrong-join hit" not in block


def test_world_info_uses_only_last_two_narratives(build_log):
    log = build_log()
    sid = "tail"
    log.append_event(sid, "system", "system", {"event": "world_info", "book": SCAN_BOOK})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "BETA", "dsl": "", "segments": []}
    )
    # Two newest narratives are DELTA + GAMMA, so recent == "DELTA GAMMA"; the
    # space-join entry ("BETA GAMMA") must not fire off the older BETA.
    log.append_event(
        sid, "narrative", "narrator", {"prose": "DELTA", "dsl": "", "segments": []}
    )
    log.append_event(
        sid, "narrative", "narrator", {"prose": "GAMMA", "dsl": "", "segments": []}
    )

    joined = recalled_message(log, sid, user_text="ALPHA") or ""
    # scan == "ALPHA\nDELTA GAMMA"; neither the space-join key (needs BETA
    # GAMMA) nor the newline-join key (needs ALPHA\nBETA) can fire, proving
    # ``recent`` is the LAST TWO narratives only — not all of them.
    assert "space-join hit" not in joined
    assert "newline-join hit" not in joined
    assert "### World info" not in joined


# --- (9) the ``### World`` block: LIVE definition, not the snapshot ---------
#
# The defect: a card-style world keeps its whole setting in
# the world's ``description`` — the inn IS the ``{{char}}`` — with an empty
# scenario and no cast, and the ``world_ref`` snapshot carries only name +
# scenario. So the block rendered a bare NAME and both the narrator and the
# planner invented the world. These tests pin the live render, its macro
# treatment, its cast-less ``{{char}}`` fallback, and the two byte-identical
# guarantees (no world_ref -> nothing; empty definition -> name only).

INN = "The Crimson Lantern"
INN_DESCRIPTION = (
    "{{char}} is a three story timber inn by the harbour. "
    "{{user}}'s room is on the second floor."
)


def _append_world(log: EventLog, **fields) -> str:
    """Append one world definition to the ``__worlds__`` anchor (D32)."""
    from fabulist.worlds.registry import append_world

    body = {"name": INN}
    body.update(fields)
    return append_world(log, body)


def _attach_world(log: EventLog, sid: str, world_id: str, **ref) -> None:
    """Append the D32 materialization snapshot (name + scenario only)."""
    payload = {"event": "world_ref", "world_id": world_id, "name": INN}
    payload.update(ref)
    log.append_event(sid, "system", "user", payload)


def test_world_block_renders_the_live_description(build_log):
    """The definition's ``description`` IS the setting and must reach the prompt."""
    log = build_log()
    sid = "inn"
    world_id = _append_world(log, description=INN_DESCRIPTION)
    _attach_world(log, sid, world_id)

    block = session_world_definition_block(log, log.get_chain_events(sid))
    assert block is not None
    lines = block.split("\n")
    assert lines[0] == "### World"
    assert lines[1] == INN
    # UNCAPPED and labeled (the cast block's uncapping precedent, be16722).
    # With no map the block still gets the ``{{char}}`` = world-name fallback;
    # every other token stays literal (no map -> nothing to resolve against).
    assert lines[2] == (
        f"Description: {INN} is a three story timber inn by the harbour. "
        "{{user}}'s room is on the second floor."
    )
    # No ``Scenario:`` line at all — the field is empty for this world.
    assert "Scenario:" not in block

    # It rides inside the facts block AHEAD of any world-info entries.
    assert session_world_info_block(log, sid, "hi") == block


def test_world_block_renders_macros(build_log):
    """``{{user}}`` / ``{{char}}`` in the description resolve through D43."""
    log = build_log()
    sid = "inn-macros"
    world_id = _append_world(log, description=INN_DESCRIPTION)
    _attach_world(log, sid, world_id)

    block = session_world_info_block(
        log,
        sid,
        "hi",
        variables={"user": "Wanderer", "char": "[cast: Seraphine]", "persona": "Wanderer"},
    )
    assert block is not None
    assert (
        "Description: [cast: Seraphine] is a three story timber inn by the "
        "harbour. Wanderer's room is on the second floor."
    ) in block
    assert "{{char}}" not in block and "{{user}}" not in block


def test_world_block_char_falls_back_to_the_world_name(build_log):
    """A cast-less world's empty ``{{char}}`` renders as the world's name.

    "The Crimson Lantern" has no cast, so ``{{char}}`` resolves to ``""`` and the
    description would read " is a three story timber inn". The fallback is
    LOCAL to this block: the map's other names are untouched, the caller's map is
    not mutated, and a NON-empty ``char`` still wins.
    """
    log = build_log()
    sid = "inn-fallback"
    world_id = _append_world(log, description=INN_DESCRIPTION)
    _attach_world(log, sid, world_id)
    events = log.get_chain_events(sid)

    variables = {"user": "Wanderer", "char": "", "persona": "Wanderer"}
    block = session_world_definition_block(log, events, variables)
    assert block is not None
    assert f"Description: {INN} is a three story timber inn" in block
    assert "Wanderer's room" in block
    assert variables["char"] == ""  # the caller's map is not mutated

    # A real cast still wins over the fallback.
    with_cast = session_world_definition_block(
        log, events, {"user": "Wanderer", "char": "[cast: Seraphine]"}
    )
    assert with_cast is not None
    assert "Description: [cast: Seraphine] is a three story" in with_cast


def test_world_block_falls_back_to_the_snapshot_for_a_deleted_world(build_log):
    """A tombstoned world renders EXACTLY the pre-live snapshot block."""
    from fabulist.worlds.registry import delete_world

    log = build_log()
    sid = "inn-deleted"
    world_id = _append_world(log, description=INN_DESCRIPTION)
    _attach_world(log, sid, world_id, scenario="It always rains.")
    delete_world(log, world_id)

    events = log.get_chain_events(sid)
    block = session_world_definition_block(log, events)
    assert block == session_world_block(events) == (
        f"### World\n{INN}\nScenario: It always rains."
    )


def test_world_block_is_absent_without_a_world_ref(build_log):
    """A plain session contributes nothing — byte-identically (the D32 rule)."""
    log = build_log()
    sid = "plain"
    log.append_event(sid, "system", "system", {"card": LYRA})
    log.append_event(sid, "user", "user", {"text": "I enter the night market."})

    events = log.get_chain_events(sid)
    assert session_world_definition_block(log, events) is None
    assert session_world_block(events) is None
    assert session_world_info_block(log, sid, "hi") is None


def test_world_block_renders_name_only_when_the_definition_is_empty(build_log):
    """Empty description + empty scenario -> the name-only block, as today."""
    log = build_log()
    sid = "inn-empty"
    world_id = _append_world(log)  # no description, no scenario
    _attach_world(log, sid, world_id)

    events = log.get_chain_events(sid)
    assert session_world_definition_block(log, events) == f"### World\n{INN}"
    assert session_world_definition_block(log, events) == session_world_block(events)


def test_world_block_follows_a_live_studio_edit(build_log):
    """Latest-wins over ``__worlds__``: a studio edit reaches the next build."""
    log = build_log()
    sid = "inn-edited"
    world_id = _append_world(log, description=INN_DESCRIPTION)
    _attach_world(log, sid, world_id)

    events = log.get_chain_events(sid)
    first = session_world_definition_block(log, events)
    assert first is not None and "three story timber inn" in first

    # A studio edit re-appends the definition under the SAME id (append-only D9).
    from fabulist.worlds.registry import append_world

    append_world(
        log,
        {
            "world_id": world_id,
            "name": INN,
            "description": "{{char}} is now a converted lighthouse.",
        },
    )
    edited = session_world_definition_block(log, events)
    assert edited is not None
    assert "converted lighthouse" in edited
    assert "three story timber inn" not in edited


# --- (10) the BTS section reads the registry (W9.29 §8) ---------------------
#
# The narrator's `### Behind the scenes` section was the last reader of the
# free-form `world_state` payload. It now renders the identity-stable registry
# (canonical paths, meters by value, threads, edges, items) and keeps the legacy
# render BENEATH it for sessions with no registry objects (forward-only).


def _registry_room(log: EventLog, sid: str, name: str, *, parent: str | None = None) -> str:
    from fabulist.state import entities

    return entities.establish_location(log, sid, name, parent_id=parent)


def _registry_member(log: EventLog, sid: str, name: str) -> str:
    from fabulist.state.cast import admit_member

    return admit_member(log, sid, name, provenance="card", admitted_by="test")


def test_bts_section_renders_the_registry_objects(build_log):
    """Beings, locations, meters, threads, edges and items, as facts (D22)."""
    from fabulist.context.facts import session_bts_section
    from fabulist.state import entities

    log = build_log()
    sid = "s1"
    seraphine = _registry_member(log, sid, "Seraphine")
    hall = _registry_room(log, sid, "upstairs hall")
    bath = _registry_room(log, sid, "bathroom", parent=hall)
    entities.move_being(log, sid, entities.PLAYER, bath)
    entities.move_being(log, sid, seraphine, bath)
    entities.set_attribute(log, sid, seraphine, key="hostility", value=42, subject=entities.PLAYER)
    entities.set_attribute(log, sid, entities.SCENE, key="tension", value=4)
    entities.establish_thread(log, sid, "the slow courtship", detail="Seraphine tests her")
    entities.establish_relationship(log, sid, seraphine, entities.PLAYER, note="a project")
    entities.establish_item(log, sid, "brass key", holder=entities.PLAYER)

    section = session_bts_section(log, sid)

    assert section is not None
    # Canonical path, not the leaf alone.
    assert "- The scene is at upstairs hall > bathroom." in section
    assert "- Seraphine is at upstairs hall > bathroom." in section
    assert "- Seraphine's hostility toward the player is 42." in section
    assert "- The scene's tension is 4." in section
    assert "- Thread the slow courtship is active — Seraphine tests her." in section
    assert "- Relationship Seraphine→the player: a project." in section
    assert "- Item brass key is held by the player." in section
    # Never instructions, never ids the narrator cannot use.
    assert seraphine not in section
    assert "loc_" not in section


def test_bts_section_renders_the_off_screen_bucket_with_its_reference(build_log):
    from fabulist.context.facts import session_bts_section
    from fabulist.state import entities

    log = build_log()
    sid = "s1"
    yara = _registry_member(log, sid, "Yara")
    bedroom = _registry_room(log, sid, "master bedroom")
    entities.move_being(log, sid, entities.PLAYER, bedroom)
    entities.move_being(log, sid, yara, entities.OFF_SCREEN, at=bedroom)

    section = session_bts_section(log, sid)

    assert section is not None
    assert "- Yara is off-screen at master bedroom." in section


def test_bts_section_falls_back_to_the_legacy_render(build_log):
    """No registry objects (a pre-regime chat) ⇒ today's payload render, verbatim."""
    from fabulist.context.facts import session_bts_section
    from fabulist.state.bts import session_bts_block

    log = build_log()
    sid = "s1"
    log.append_event(
        sid,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {"Yara": {"emotional": {"mood": "wary"}}},
            "venue": {"Yara": {"venue": "tavern", "area": "bar"}},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )

    assert session_bts_section(log, sid) == session_bts_block(log, sid)


def test_bts_section_is_none_without_any_world_state(build_log):
    from fabulist.context.facts import session_bts_section

    log = build_log()

    assert session_bts_section(log, "s1") is None


def test_bts_section_mentions_a_legacy_fact_the_registry_does_not_hold(build_log):
    """D87: a mid-migration session keeps its pre-regime facts (name-only, once)."""
    from fabulist.context.facts import session_bts_section
    from fabulist.state import entities

    log = build_log()
    sid = "s1"
    yara = _registry_member(log, sid, "Yara")
    bedroom = _registry_room(log, sid, "master bedroom")
    entities.move_being(log, sid, entities.PLAYER, bedroom)
    entities.move_being(log, sid, yara, bedroom)
    entities.set_attribute(log, sid, yara, key="stress", value=55)
    log.append_event(
        sid,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {
                "Yara": {
                    "emotional": {"stress": "55", "mood": "wary"},
                    "physical": {"wound": "sprained wrist"},
                }
            },
            "venue": {"Yara": {"venue": "bedroom"}},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )

    section = session_bts_section(log, sid)

    assert section is not None
    # Covered by the registry (a meter with identity) ⇒ not repeated...
    assert section.count("stress") == 1
    # ...while the pre-regime facts with no object behind them survive.
    assert "- Yara's mood is wary." in section
    assert "- Yara's wound is sprained wrist." in section


def test_bts_section_respects_the_section_budget(build_log):
    """The clip is the payload renderer's own (BTS_SECTION_BUDGET)."""
    from fabulist.context.facts import registry_bts_section
    from fabulist.state import entities

    log = build_log()
    sid = "s1"
    for index in range(40):
        entities.establish_thread(log, sid, f"thread number {index}", detail="detail")

    section = registry_bts_section(entities.registry(log, sid), {}, budget=200)

    assert section is not None
    assert len(section) <= 200 + len("…(continuity notes truncated)") + 1
    assert section.endswith("…(continuity notes truncated)")


# --- W9.26 §2.4: the recap ABSORBS its predecessor ---------------------------
#
# A summary v2 row may carry ``absorbs: [session_id, seq]`` naming the summary it
# folded. The PROMPT recap must render that predecessor exactly once (the newer
# text already contains it): paying to re-read it every turn is the whole cost
# the era model exists to remove. ``summaries_payload`` stays the ALL-rows reader
# the RecapPane uses, so both readers are pinned here side by side.


def _absorbing_era(log: EventLog, sid: str) -> tuple[int, int]:
    """Two summaries; the second ABSORBS the first. Returns their seqs."""
    log.append_event(sid, "user", "user", {"text": "I enter the night market."})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "Lanterns.", "dsl": "", "segments": []}
    )
    first = log.append_event(
        sid,
        "summary",
        "summarizer",
        {"text": "ERA ONE TEXT", "covers_seqs": [1, 2], "trigger": "manual"},
    )
    second = log.append_event(
        sid,
        "summary",
        "summarizer",
        {
            "text": "ERA TWO TEXT",
            "covers_seqs": [1, 4],
            "trigger": "manual",
            "absorbs": [sid, first.seq],
        },
    )
    return first.seq, second.seq


def test_the_recalled_recap_drops_an_absorbed_summary(build_log):
    log = build_log()
    sid = "eras"
    _absorbing_era(log, sid)

    block = recalled_message(log, sid, user_text="I wait.")

    assert block is not None
    assert RECALLED_RECAP_HEADER in block
    assert "ERA TWO TEXT" in block
    assert "ERA ONE TEXT" not in block  # absorbed: rendered once, at the fold


def test_summaries_payload_still_lists_every_era_for_the_recap_pane(build_log):
    """The RecapPane reader is ALL rows; only the PROMPT recap absorbs (D-F)."""
    from fabulist.graph.compaction import summaries_payload

    log = build_log()
    sid = "eras"
    _absorbing_era(log, sid)

    assert summaries_payload(log.get_chain_events(sid)) == [
        "ERA ONE TEXT",
        "ERA TWO TEXT",
    ]


def test_a_legacy_summary_without_absorbs_is_never_dropped(build_log):
    """Absence of the key is not evidence of absorption (legacy rows survive)."""
    log = build_log()
    sid = "legacy-eras"
    log.append_event(sid, "user", "user", {"text": "hi"})
    log.append_event(sid, "summary", "summarizer", {"text": "OLD ONE", "covers_seqs": [1]})
    log.append_event(sid, "summary", "summarizer", {"text": "OLD TWO", "covers_seqs": [1]})

    block = recalled_message(log, sid, user_text="hi")

    assert block is not None
    assert "OLD ONE" in block and "OLD TWO" in block


def test_the_context_suffix_parts_carry_the_absorbing_recap_too(build_log):
    """Both call sites (the canonical suffix AND <recalled>) use the same reader."""
    log = build_log()
    sid = "eras"
    _absorbing_era(log, sid)

    parts = context_suffix_parts(
        log, sid, recall_text="", user_text="I wait.", facts="time: 14:35"
    )

    joined = "\n".join(parts)
    assert "ERA TWO TEXT" in joined
    assert "ERA ONE TEXT" not in joined
