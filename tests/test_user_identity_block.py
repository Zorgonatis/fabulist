"""D54 — the played user persona's identity block reaches the narrator.

DEFECT: the narrator only ever saw the CHARACTER card's idea of "you", so it
recast the played user persona as an NPC (the dnd-guild live defect: "the
white-bearded mage" against a fresh-faced newcomer). The fix renders the chat's
effective persona — name plus (when present) description — as a static
pre-history block IMMEDIATELY after the character/cast block, so the model is
told who the player is before the card's embedded "you" can take over.

This module pins the contract:

* :func:`persona_identity` folds the SAME registry resolution the ``{{user}}`` /
  ``{{persona}}`` macros use (chat assignment -> pinned global default; an
  explicit clear suppresses the pin; an unknown or tombstoned id resolves to
  nothing), returning ``("", "")`` when no persona is in effect.
* :func:`session_user_identity_block` renders ``### User (the player)`` +
  ``Name:``, and a ``Description:`` line only when the description is non-empty;
  ``None`` when no persona is in effect.
* :func:`static_prefix_parts` / :func:`assemble_turn_context` place the block
  immediately after the character block, and the built prompt carries it exactly
  once (never in the per-turn ``<state>`` suffix).
* ``variables._persona_name`` delegates to :func:`persona_identity`, so the name
  the narrator is told and the name the macros bind to cannot diverge.

Seeded with a real tmp-path :class:`~fabulist.log.EventLog` (the house
convention: ``tests/test_facts_suffix.py`` / ``tests/test_card_macro_rendering.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.context.facts import (
    USER_IDENTITY_HEADER,
    assemble_turn_context,
    persona_identity,
    session_persona_block,
    session_user_identity_block,
    static_prefix_parts,
)
from fabulist.graph.types import initial_turn_state
from fabulist.log import EventLog
from fabulist.personas.registry import PERSONAS_ANCHOR, delete_persona
from fabulist.prompts.variables import resolve_variables
from fabulist.state.settings import DEFAULT_PERSONA_EVENT, SETTINGS_SESSION_ID
from fabulist.wiring import make_narrator_message_builder

CARD = {
    "name": "Rex",
    "description": "A ship's captain.",
    "first_mes": "Welcome aboard.",
}

IDENTITY = "### User (the player)"
ZERO = "435c4d4bc7cd4987803f1df8cfde42ea"
ZERO_DESCRIPTION = (
    "An actual time traveler with amnesia. Has heightened perception due to an "
    "ancient implant [source unknown]."
)


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for a tmp-path :class:`EventLog`; closes every log after."""

    built: list[EventLog] = []

    def _build(name: str = "identity.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


def _seed_card(log: EventLog, sid: str, card: dict | None = None) -> None:
    card = card or CARD
    log.append_event(sid, "system", "system", {"card": card, "first_mes": card["first_mes"]})


def _seed_persona(
    log: EventLog, persona_id: str, name: str, description: str = ""
) -> None:
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {
            "event": "persona",
            "persona_id": persona_id,
            "name": name,
            "description": description,
        },
    )


def _assign(log: EventLog, sid: str, persona_id: str) -> None:
    log.append_event(sid, "system", "user", {"event": "chat_persona", "persona_id": persona_id})


# --- persona_identity: the shared registry fold ------------------------------


def test_persona_identity_returns_name_and_description(build_log):
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    _assign(log, "s", "p1")

    assert persona_identity(log, "s") == ("Wanderer", ZERO_DESCRIPTION)


def test_persona_identity_is_empty_without_assignment_or_pin(build_log):
    """A sandbox chat (never set, no pin) resolves to no persona."""
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)

    assert persona_identity(log, "s") == ("", "")
    assert session_user_identity_block(log, "s") is None
    # The card block survives; the identity block contributes nothing.
    assert static_prefix_parts(log, "s") == [session_persona_block(log, "s")]


def test_persona_identity_honors_pinned_default(build_log):
    """A never-set chat inherits the pinned global default (D16 fallback)."""
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    log.append_event(
        SETTINGS_SESSION_ID,
        "system",
        "user",
        {"event": DEFAULT_PERSONA_EVENT, "persona_id": "p1"},
    )

    assert persona_identity(log, "s") == ("Wanderer", ZERO_DESCRIPTION)


def test_persona_identity_explicit_clear_suppresses_pin(build_log):
    """A deliberate clear stays persona-less even with a pin set."""
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    log.append_event(
        SETTINGS_SESSION_ID,
        "system",
        "user",
        {"event": DEFAULT_PERSONA_EVENT, "persona_id": "p1"},
    )
    _assign(log, "s", "p1")
    log.append_event(
        "s",
        "system",
        "user",
        {"event": "chat_persona", "persona_id": None, "cleared": True},
    )

    assert persona_identity(log, "s") == ("", "")
    assert session_user_identity_block(log, "s") is None


def test_persona_identity_unknown_assigned_id_is_empty(build_log):
    log = build_log()
    _seed_card(log, "s")
    _assign(log, "s", "ghost")

    assert persona_identity(log, "s") == ("", "")


def test_persona_identity_tombstoned_persona_is_empty(build_log):
    """A ``persona_deleted`` tombstone drops the id from the catalog."""
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    _assign(log, "s", "p1")
    delete_persona(log, "p1")

    assert persona_identity(log, "s") == ("", "")


# --- session_user_identity_block: render shape -------------------------------


def test_identity_block_renders_name_and_description(build_log):
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    _assign(log, "s", "p1")

    assert session_user_identity_block(log, "s") == (
        "### User (the player)\n"
        "Name: Wanderer\n"
        f"Description: {ZERO_DESCRIPTION}"
    )


def test_identity_block_omits_empty_description(build_log):
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer")
    _assign(log, "s", "p1")

    block = session_user_identity_block(log, "s")
    assert block == "### User (the player)\nName: Wanderer"
    assert "Description:" not in block


def test_identity_block_header_constant_matches_render(build_log):
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer", "desc")
    _assign(log, "s", "p1")

    block = session_user_identity_block(log, "s") or ""
    assert block.splitlines()[0] == USER_IDENTITY_HEADER


# --- macro binding -----------------------------------------------------------


def test_identity_block_binds_description_macros(build_log):
    """The description renders the fixed vocabulary exactly like card text."""
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(
        log, "p1", "Wanderer", "A traveller in {{world}}. {{user}} knows it."
    )
    _assign(log, "s", "p1")
    log.append_event(
        "s", "system", "system", {"event": "world_ref", "world_id": "w1", "name": "Epoch 27"}
    )
    variables = resolve_variables(log, "s", "hi")
    assert variables["world"] == "Epoch 27"
    assert variables["user"] == "Wanderer"

    rendered = session_user_identity_block(log, "s", variables=variables)
    assert "A traveller in Epoch 27." in rendered
    assert "Wanderer knows it." in rendered
    assert "{{" not in rendered

    # No variables -> byte-identical raw path, literal tokens preserved.
    raw = session_user_identity_block(log, "s")
    assert "A traveller in {{world}}. {{user}} knows it." in raw


def test_persona_name_macro_matches_identity_name(build_log):
    """``_persona_name`` delegates to ``persona_identity`` — one fold, one name."""
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    _assign(log, "s", "p1")

    variables = resolve_variables(log, "s", "hi")
    assert variables["persona"] == "Wanderer"
    assert variables["user"] == "Wanderer"
    assert persona_identity(log, "s")[0] == variables["persona"]


# --- fork inheritance --------------------------------------------------------


def test_branch_inherits_persona_identity(build_log):
    log = build_log()
    _seed_card(log, "parent")
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    _assign(log, "parent", "p1")
    log.append_event("parent", "user", "user", {"text": "hello"})
    fork_seq = log.get_events("parent")[-1].seq
    log.fork_session("parent", at_seq=fork_seq, new_session_id="branch")

    assert persona_identity(log, "branch") == ("Wanderer", ZERO_DESCRIPTION)
    block = session_user_identity_block(log, "branch")
    assert block is not None
    assert "Name: Wanderer" in block
    assert "Description:" in block


# --- placement: world / multi-card chat + ordering ---------------------------


def test_world_multi_card_places_identity_after_cast(build_log):
    """A world (multi-card) chat renders ``### Cast`` first, identity second."""
    log = build_log()
    sid = "world"
    card_a = {"name": "Rex", "description": "Captain.", "first_mes": "A"}
    card_b = {"name": "Nova", "description": "Engineer.", "first_mes": "B"}
    _seed_card(log, sid, card_a)
    _seed_card(log, sid, card_b)
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    _assign(log, sid, "p1")

    parts = static_prefix_parts(log, sid)
    assert len(parts) == 2
    assert parts[0].startswith("### Cast")
    assert parts[1].startswith(IDENTITY)

    # Canonical context order: card, identity, then the dynamic parts.
    context = assemble_turn_context(log, sid, user_text="hi").context_parts
    assert context[0].startswith("### Cast")
    assert context[1].startswith(IDENTITY)


def test_static_prefix_is_card_then_identity(build_log):
    """Single-card ordering is exactly [character, identity]."""
    log = build_log()
    _seed_card(log, "s")
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    _assign(log, "s", "p1")

    parts = static_prefix_parts(log, "s")
    assert len(parts) == 2
    assert parts[0].startswith("### Character: Rex")
    assert parts[1] == (
        "### User (the player)\n"
        "Name: Wanderer\n"
        f"Description: {ZERO_DESCRIPTION}"
    )


def test_no_card_no_persona_static_prefix_is_empty(build_log):
    log = build_log()
    log.append_event("s", "user", "user", {"text": "hi"})

    assert static_prefix_parts(log, "s") == []
    assert session_user_identity_block(log, "s") is None


# --- the built prompt carries the block exactly once -------------------------


def test_built_prompt_carries_identity_exactly_once(build_log):
    log = build_log()
    sid = "s"
    _seed_card(log, sid)
    _seed_persona(log, "p1", "Wanderer", ZERO_DESCRIPTION)
    _assign(log, sid, "p1")

    build = make_narrator_message_builder(log)
    messages = build(initial_turn_state(sid, "I look around.", "narrate"))
    joined = "\n".join(str(message.get("content", "")) for message in messages)

    assert joined.count(IDENTITY) == 1
    assert "Name: Wanderer" in joined
    # It rides the static pre-history block, NOT <state> — since issue #44 that
    # block is folded into the ONE prefix system message.
    assert IDENTITY in str(messages[0].get("content", ""))
