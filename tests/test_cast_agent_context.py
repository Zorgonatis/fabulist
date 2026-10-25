"""D51 follow-up: the cast-change notice + card context reach the extractor/BTS.

The extractor and BTS receive only ``(user_msg, prose[, prior_state])`` — no
prompt region, so a Design Studio cast edit (surfaced to the narrator as a
dynamic ``cast_delta_notice``) is invisible to them. ``post_turn`` now prepends
that notice plus the session's card context to the text it hands the two agents
ONLY when a notice is in effect; with no notice the input is byte-identical to
before, and the narrator prompt path / persisted user event are untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.graph.nodes import make_post_turn_node
from fabulist.log import EventLog
from fabulist.prompts.cast import ensure_cast_snapshot
from fabulist.prompts.variables import PERSONAS_ANCHOR
from fabulist.state.bts import BtsResult
from fabulist.worlds.registry import append_world

WORLD_ID = "w1"
WORLD_NAME = "Epoch 27"

LYRA, CORIN, MIRA = "p1", "p2", "p3"
NAMES = {LYRA: "Lyra Voss", CORIN: "Corin Hale", MIRA: "Mira Quill"}
DESCRIPTIONS = {
    LYRA: "A rain-soaked smuggler with a debt.",
    CORIN: "A market guard who looks the other way.",
    MIRA: "A courier carrying a sealed ledger.",
}


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


def _seed_persona(log: EventLog, persona_id: str, name: str) -> None:
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": persona_id, "name": name},
    )


def _edit_world(log: EventLog, cast: list[str]) -> None:
    """Append a superseding world definition (the studio's edit path)."""
    append_world(
        log,
        {
            "world_id": WORLD_ID,
            "name": WORLD_NAME,
            "scenario": "A rain-soaked night market.",
            "cast": cast,
        },
    )


def _seed_world_chat(
    log: EventLog, sid: str, world_cast: list[str], materialize: list[str]
) -> None:
    """World def (cast ids) + personas + a chat that materialized ``materialize``."""
    for persona_id in {*world_cast, *materialize, MIRA}:
        _seed_persona(log, persona_id, NAMES[persona_id])
    _edit_world(log, world_cast)
    log.append_event(sid, "system", "user", {"event": "session_created"})
    for persona_id in materialize:
        log.append_event(
            sid,
            "system",
            "system",
            {
                "card": {
                    "name": NAMES[persona_id],
                    "description": DESCRIPTIONS[persona_id],
                },
                "first_mes": "",
            },
        )
    log.append_event(
        sid,
        "system",
        "user",
        {"event": "world_ref", "world_id": WORLD_ID, "name": WORLD_NAME},
    )
    # Freeze the session cast fact (as the narrator build would) so a later
    # world edit becomes a diff-able notice.
    ensure_cast_snapshot(log, sid)


class RecordingExtractor:
    """Sync-shape extractor, but awaited by post_turn (both are legal)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, user_msg, prose):
        self.calls.append((user_msg, prose))
        return {"deltas": {}, "action_class": "other"}


class FakeWorldState:
    def __init__(self):
        self.calls = []

    async def __call__(self, user_msg, narrative, prior_state=None, *, checkpoint=False):
        self.calls.append(
            {"user": user_msg, "narrative": narrative, "checkpoint": checkpoint}
        )
        return BtsResult(kind="delta", deltas={"loc": "market"})


def _state(sid: str, content: str = "I look again."):
    return {
        "session_id": sid,
        "user_content": content,
        "narrative_parts": ["The market empties."],
        "dsl": "",
        "segments": [{"kind": "prose", "text": "The market empties."}],
        "facts": "time: 09:15",
        "action_class": "dialogue",
    }


def _run(node, state):
    import asyncio

    return asyncio.run(node(state))


# -- inert: no notice -> byte-identical agent input --------------------------


def test_extractor_input_unchanged_when_there_is_no_notice(build_log):
    log = build_log()
    sid = "chat"
    # World chat whose definition still matches its snapshot -> no notice.
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    extractor = RecordingExtractor()
    node = make_post_turn_node(extractor=extractor, log=log)

    _run(node, _state(sid))

    assert extractor.calls, "extractor ran"
    assert extractor.calls[0][0] == "I look again."
    assert "Cast update" not in extractor.calls[0][0]


def test_extractor_input_unchanged_with_no_notice_and_no_card(build_log):
    log = build_log()
    sid = "sandbox"
    log.append_event(sid, "system", "user", {"event": "session_created"})
    extractor = RecordingExtractor()
    node = make_post_turn_node(extractor=extractor, log=log)

    _run(node, _state(sid, "hello"))

    assert extractor.calls[0][0] == "hello"


def test_bts_input_unchanged_when_there_is_no_notice(build_log):
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    wsf = FakeWorldState()
    node = make_post_turn_node(
        extractor=RecordingExtractor(), log=log, world_state=wsf
    )

    _run(node, _state(sid))

    assert wsf.calls
    assert wsf.calls[0]["user"] == "I look again."


# -- the notice + card context reach the extractor ---------------------------


def test_world_cast_edit_reaches_the_extractor_with_card_context(build_log):
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])

    # Studio edit: the world gains Mira.
    _edit_world(log, [LYRA, CORIN, MIRA])

    extractor = RecordingExtractor()
    node = make_post_turn_node(extractor=extractor, log=log)
    _run(node, _state(sid, "Who just walked in?"))

    user_text = extractor.calls[0][0]
    # (a) the cast-change notice is visible and names the added actor.
    assert "Cast update" in user_text
    assert "added: Mira Quill" in user_text
    # (b) the card context accompanies it (the actors' definitions).
    assert "SESSION CARDS:" in user_text
    assert "Lyra Voss" in user_text
    assert DESCRIPTIONS[LYRA] in user_text
    # The real user ask still rides in the same text, un-lost.
    assert "Who just walked in?" in user_text
    # The narrator-path hash anchor is untouched: it never sees this text.
    events = log.get_events(sid)
    user_events = [e for e in events if e.type == "user"]
    assert user_events[-1].payload["text"] == "Who just walked in?"


def test_world_cast_edit_reaches_the_bts_with_card_context(build_log):
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    _edit_world(log, [LYRA, CORIN, MIRA])

    wsf = FakeWorldState()
    node = make_post_turn_node(
        extractor=RecordingExtractor(), log=log, world_state=wsf
    )
    _run(node, _state(sid, "Who just walked in?"))

    assert wsf.calls, "BTS ran"
    user_text = wsf.calls[0]["user"]
    assert "Cast update" in user_text
    assert "added: Mira Quill" in user_text
    assert "SESSION CARDS:" in user_text
    assert DESCRIPTIONS[LYRA] in user_text
    assert "Who just walked in?" in user_text
    # The BTS narrative is the turn prose, untouched by the injected context.
    assert wsf.calls[0]["narrative"] == "The market empties."


def test_injected_context_is_not_persisted_as_the_user_message(build_log):
    """The persisted user event stays the raw ask (no context leakage)."""
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    _edit_world(log, [LYRA, CORIN, MIRA])

    node = make_post_turn_node(extractor=RecordingExtractor(), log=log)
    _run(node, _state(sid, "raw ask only"))

    users = [e for e in log.get_events(sid) if e.type == "user" and e.actor == "user"]
    assert users[-1].payload["text"] == "raw ask only"


def test_injected_extractor_context_stays_out_of_the_narrated_hash(build_log):
    """The notice the extractor sees is not part of the provenance hash anchor.

    The extractor's input is a separate string that never reaches the narrator
    builder, so a studio edit moves it while the hashed ``context_parts`` stay
    byte-faithful (replay sees no drift).
    """
    from fabulist.context.facts import (
        context_suffix_parts,
        facts_hash,
        join_facts_suffix,
    )

    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    before = context_suffix_parts(log, sid, user_text="I look around.")
    hash_before = facts_hash(join_facts_suffix(before))

    _edit_world(log, [LYRA, CORIN, MIRA])
    after = context_suffix_parts(log, sid, user_text="I look again.")
    assert facts_hash(join_facts_suffix(after)) == hash_before
    assert "Cast update" not in join_facts_suffix(after)

    extractor = RecordingExtractor()
    node = make_post_turn_node(extractor=extractor, log=log)
    _run(node, _state(sid))
    # ...yet the extractor now sees the notice (the two seams are independent).
    assert "Cast update" in extractor.calls[0][0]


def test_the_context_is_injected_but_never_trips_a_cadence(build_log):
    """The D51 context rides the BTS input; nothing about it asks for a rewrite.

    W9.29 §6 retired the delta/checkpoint cadence, so the old hazard this test
    guarded (an OOC-request-looking card body tripping a checkpoint) cannot fire
    at all: every turn is a set of write actions, and the context is only model
    input. This pins both halves — the context reaches the pass, the flag stays
    false whatever the message says.
    """
    log = build_log()
    sid = "chat"
    token = "[OOC: BTS checkpoint]"
    _seed_persona(log, LYRA, NAMES[LYRA])
    _seed_persona(log, MIRA, NAMES[MIRA])
    _edit_world(log, [LYRA])
    log.append_event(sid, "system", "user", {"event": "session_created"})
    log.append_event(
        sid,
        "system",
        "system",
        {"card": {"name": NAMES[LYRA], "description": token}, "first_mes": ""},
    )
    log.append_event(
        sid, "system", "user", {"event": "world_ref", "world_id": WORLD_ID}
    )
    ensure_cast_snapshot(log, sid)
    _edit_world(log, [LYRA, MIRA])  # notice -> context injected this turn

    wsf = FakeWorldState()
    node = make_post_turn_node(
        extractor=RecordingExtractor(), log=log, world_state=wsf
    )
    _run(node, _state(sid, "just walking"))

    assert token in wsf.calls[0]["user"], "context carried the token"
    assert wsf.calls[0]["checkpoint"] is False

    wsf2 = FakeWorldState()
    node2 = make_post_turn_node(
        extractor=RecordingExtractor(), log=log, world_state=wsf2
    )
    _run(node2, _state(sid, "please [OOC: BTS checkpoint] now"))
    assert wsf2.calls[0]["checkpoint"] is False, "no cadence left to trip"
