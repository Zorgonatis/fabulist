"""Issue #106: the planner reads the lore, and fixed canon outranks the state.

The defect (RCA, `.pi/issue-106-rca/`): ``_planner_context`` had five reads and no
lore read, so the pass that WRITES the tracked state (the seed loop's
``entity_establish`` rows and the plan's ``state-delta``) never saw an authored
lorebook entry and could plan straight through it. The narrator then read both —
and the prompt's own tier order called the planner state "ground truth" and the
lore "reference material".

The approved fix, pinned here:

* the planner context gains a ``LORE (fixed canon)`` section holding EVERY
  enabled entry — no activation scan (owner constraint 2026-11-01) and no
  world-definition prefix (the planner already renders the definition in WORLD);
* an entry marked ``constant`` is fixed canon: it moves OUT of the
  ``<engine_memory>`` reference block and INTO the ``<engine_state>`` truth
  block, whose header now states that the entry wins over the tracked state;
* the section headings stay editable preset surfaces;
* nothing moves in the stable prefix (ADR-005) and the planner section is
  byte-stable inside one run (the seed loop's cached prefix).
"""

from __future__ import annotations

from fabulist.context.facts import (
    CANON_HEADER,
    LORE_PLANNER_HEADER,
    assemble_turn_context,
    join_facts_suffix,
    lore_copy,
    session_canon_block,
    session_lore_block,
    session_lore_entries,
    session_world_info_block,
    static_prefix_parts,
)
from fabulist.graph.nodes import _planner_context
from fabulist.log import EventLog
from fabulist.prompts.envelope import STATE_HEADER, render_engine_envelope
from fabulist.prompts.narrator import render_state_block
from fabulist.prompts.surfaces import (
    factory_surfaces,
    normalize_surfaces,
    preset_surfaces,
)

SID = "s106"

#: Always-on canon: the entry's key appears NOWHERE in the turn-1 scan text.
HARBOUR = "The harbour froze in the Long Winter and has never thawed."
#: A keyed entry the turn's own words activate (the narrator's memory block).
AWNING = "The awning over the fish stall leaks in any rain."
#: A keyed entry that is dark on turn 1 — the case the scan cannot rescue.
ARCHIVIST = "The archivist cannot read; she remembers by touch."
#: Authored but switched off in the Books tab: never rendered anywhere.
SEALED_MINE = "The mine was sealed by the guild and stays sealed."

ENTRIES = [
    {
        "keys": ["harbour"],
        "content": HARBOUR,
        "constant": True,
        "insertion_order": 1,
        "comment": "harbour",
    },
    {
        "keys": ["awning"],
        "content": AWNING,
        "insertion_order": 2,
        "comment": "awning",
    },
    {
        "keys": ["archivist"],
        "content": ARCHIVIST,
        "insertion_order": 3,
        "comment": "archivist",
    },
    {
        "keys": ["mine"],
        "content": SEALED_MINE,
        "constant": True,
        "enabled": False,
        "insertion_order": 4,
        "comment": "mine",
    },
]

CARD = {
    "name": "Seraphine",
    "description": "A harbour clerk with a long memory.",
    "character_book": {"name": "Canon", "entries": ENTRIES},
}

WORLD_NAME = "The Crimson Lantern"
WORLD_DESCRIPTION = "{{char}} is a three story timber inn by the frozen harbour."

USER_TEXT = "I wait under the awning."


def _card_session(log: EventLog) -> None:
    log.append_event(SID, "system", "system", {"card": CARD})


def _card_and_world_session(log: EventLog) -> None:
    from fabulist.worlds.registry import append_world

    world_id = append_world(
        log, {"name": WORLD_NAME, "description": WORLD_DESCRIPTION}
    )
    log.append_event(
        SID,
        "system",
        "user",
        {"event": "world_ref", "world_id": world_id, "name": WORLD_NAME, "scenario": ""},
    )
    _card_session(log)


def _planner_state(**overrides):
    state = {
        "session_id": SID,
        "user_content": USER_TEXT,
        "narrative_parts": [],
        "facts": "<state>\ntime: 08:00 | date: Monday, June 02, 2025 | day: 1\n</state>",
        "plan": "",
        "action_class": "dialogue",
    }
    state.update(overrides)
    return state


def _section(context: str, header: str) -> str:
    """The text from ``header`` to the next top-level section (or the end)."""
    start = context.index(header)
    rest = context[start + len(header) :]
    cut = rest.find("\n\nRECENT TURNS:")
    if cut == -1:
        cut = rest.find("\n\nWORLD OBJECTS:")
    if cut == -1:
        cut = len(rest)
    return rest[:cut]


# -- the planner's LORE section ---------------------------------------------


def test_planner_context_carries_every_enabled_lore_entry(log):
    """No scan: a canon entry dark on turn 1 still reaches the planner."""
    _card_session(log)

    context = _planner_context(log, SID, _planner_state())

    lore = _section(context, LORE_PLANNER_HEADER)
    assert HARBOUR in lore
    # "archivist" occurs nowhere in the user message, the card or the world —
    # keyword activation would leave it dark, which is the reported defect.
    assert ARCHIVIST in lore
    # The turned-off entry is not authored canon any more.
    assert SEALED_MINE not in context
    assert LORE_PLANNER_HEADER in context


def test_planner_lore_section_has_no_world_definition_prefix(log):
    """The planner already renders WORLD; the lore section must not repeat it."""
    _card_and_world_session(log)

    context = _planner_context(log, SID, _planner_state())

    assert "WORLD:\n### World" in context
    lore = _section(context, LORE_PLANNER_HEADER)
    assert HARBOUR in lore
    assert "### World" not in lore
    assert WORLD_DESCRIPTION.replace("{{char}}", WORLD_NAME) not in lore
    # The definition renders exactly once, in the WORLD block.
    assert context.count("### World") == 1


def test_planner_lore_section_is_byte_stable_across_one_run(log):
    """The seed phase and the plan phase read the same authored bytes (ADR-005)."""
    _card_and_world_session(log)

    seed = _section(_planner_context(log, SID, _planner_state()), LORE_PLANNER_HEADER)
    plan = _section(_planner_context(log, SID, _planner_state()), LORE_PLANNER_HEADER)

    assert seed == plan
    # ...and identical to the one seam both phases call.
    assert session_lore_block(log, SID) == f"{LORE_PLANNER_HEADER}\n" + "\n".join(
        f"- {text}" for text in (HARBOUR, AWNING, ARCHIVIST)
    )


def test_planner_context_without_lore_is_unchanged(log):
    """A session with no books grows no empty LORE section (empty means absent)."""
    log.append_event(SID, "system", "system", {"card": {"name": "Lyra Voss"}})

    context = _planner_context(log, SID, _planner_state())

    assert LORE_PLANNER_HEADER not in context
    assert "CAST AND WORLD:" in context


def test_session_lore_entries_pool_and_dedupe(log):
    """The pooled read is the same entry set the narrator's assembler pools."""
    _card_session(log)

    pooled = session_lore_entries(log, SID)

    assert [entry.content for entry in pooled] == [HARBOUR, AWNING, ARCHIVIST]
    block = session_world_info_block(log, SID, USER_TEXT)
    assert block is not None
    assert HARBOUR in block  # the pre-#106 default still renders every activation
    assert ARCHIVIST not in block  # activation still filters


# -- the narrator's truth tier ----------------------------------------------


def _blocks(log):
    return assemble_turn_context(log, SID, user_text=USER_TEXT)


def test_narrator_moves_constant_entries_into_the_state_block(log):
    """Constant entries leave the memory block and ride beside the state facts."""
    _card_session(log)

    blocks = _blocks(log)
    state_body = join_facts_suffix(blocks.state_parts)

    assert CANON_HEADER in state_body
    assert HARBOUR in state_body
    # A dark entry is not canon (no ``constant`` flag) — it stays recalled lore.
    assert ARCHIVIST not in state_body
    # The memory block keeps the ACTIVATED lore and loses the fixed canon.
    assert blocks.recalled is not None
    assert AWNING in blocks.recalled
    assert HARBOUR not in blocks.recalled


def test_envelope_places_fixed_canon_beside_state_and_not_in_memory(log):
    """The served order is the contract: state (facts, canon) then memory."""
    _card_session(log)

    blocks = _blocks(log)
    envelope = render_engine_envelope(
        state=render_state_block(join_facts_suffix(blocks.state_parts)),
        memory=blocks.recalled,
    )
    assert envelope is not None

    state_open = envelope.index("<engine_state>")
    state_close = envelope.index("</engine_state>")
    memory_open = envelope.index("<engine_memory>")
    canon_at = envelope.index(HARBOUR)
    assert state_open < canon_at < state_close < memory_open
    # The precedence rule is stated by the truth block that carries the canon.
    assert STATE_HEADER in envelope[state_open:state_close]
    assert "the entry wins" in envelope[state_open:state_close]
    # ...and never by the reference block.
    assert "the entry wins" not in envelope[memory_open:]
    assert ARCHIVIST not in envelope[state_open:state_close]


def test_narrator_without_constant_entries_renders_no_canon_section(log):
    """Sessions whose books carry no constant entry are byte-unchanged."""
    log.append_event(
        SID,
        "system",
        "system",
        {"card": {"name": "Lyra Voss", "character_book": {"entries": [ENTRIES[1]]}}},
    )

    blocks = _blocks(log)

    assert CANON_HEADER not in join_facts_suffix(blocks.state_parts)
    assert blocks.recalled is not None and AWNING in blocks.recalled


# -- the editable heading surface -------------------------------------------


def test_lore_headings_are_editable_preset_surfaces(log):
    """Both fixed-canon headings come from ``surfaces.lore`` (ADR-006/D27)."""
    _card_session(log)
    surfaces = normalize_surfaces(
        {
            "lore": {
                "planner_header": "CANON YOU MUST NOT BREAK:",
                "canon_header": "### World canon (entries win)",
            }
        }
    )

    assert lore_copy(surfaces)["planner"] == "CANON YOU MUST NOT BREAK:"
    assert session_lore_block(log, SID, surfaces=surfaces).startswith(
        "CANON YOU MUST NOT BREAK:\n"
    )
    canon = session_canon_block(log, SID, surfaces=surfaces)
    assert canon is not None and canon.startswith("### World canon (entries win)\n")

    blocks = assemble_turn_context(log, SID, user_text=USER_TEXT, surfaces=surfaces)
    assert "### World canon (entries win)" in join_facts_suffix(blocks.state_parts)


def test_the_factory_surface_seeds_the_code_headings():
    """An install that never edited the surface renders the constants exactly."""
    assert factory_surfaces()["lore"] == {
        "planner_header": LORE_PLANNER_HEADER,
        "canon_header": CANON_HEADER,
    }
    assert lore_copy(None) == {
        "planner": LORE_PLANNER_HEADER,
        "canon": CANON_HEADER,
    }
    assert normalize_surfaces({"lore": {"bogus": "x"}}) == {}
    assert preset_surfaces(None) == {}


def test_fixed_canon_never_reaches_the_stable_prefix(log):
    """ADR-005: the canon is tail content — the cached prefix loses no byte."""
    _card_session(log)

    prefix = "\n\n".join(static_prefix_parts(log, SID))

    assert "### Character: Seraphine" in prefix  # the card block really rendered
    assert HARBOUR not in prefix
    assert CANON_HEADER not in prefix
    assert LORE_PLANNER_HEADER not in prefix
    # The rule text rides the tail state block only, never the frozen prefix.
    assert "the entry wins" not in prefix
