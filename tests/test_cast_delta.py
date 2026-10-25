"""D51 studio cast deltas: the static cast prefix stays frozen, the change is
surfaced as a DYNAMIC notice (D43 ``{{char}}`` snapshot + live world definition).

The bug class: ``{{char}}`` / the card block sit in the STABLE prefix and are
snapshotted into the session as fact (prompt-cache protection, D43). A Design
Studio (D51) edit to a world's cast is expected to be *live* for an active
session, but D32's ``world_ref`` is snapshot-at-creation, so the session's
materialized cards never follow the edit. The fix is not to thaw the static
prefix (that breaks the provider prompt cache every turn) but to diff the
session's snapshotted cast fact against the CURRENT world definition and emit a
dynamic notice in the hash-neutral post-user ``<engine_notice>`` region —
the D22/B4 injection seam — so the narrator sees the change that turn while the
prefix stays byte-identical and replay stays byte-faithful.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.context.facts import (
    assemble_turn_context,
    context_suffix_parts,
    facts_hash,
    join_facts_suffix,
    static_prefix_parts,
)
from fabulist.graph.types import initial_turn_state
from fabulist.log import EventLog
from fabulist.prompts.cast import cast_delta_notice, ensure_cast_snapshot
from fabulist.prompts.variables import PERSONAS_ANCHOR, resolve_variables
from fabulist.wiring import make_narrator_message_builder
from fabulist.worlds.registry import append_world

WORLD_ID = "w1"
WORLD_NAME = "Epoch 27"

# persona_id -> name. The session materializes cards for the world's cast.
LYRA, CORIN, MIRA = "p1", "p2", "p3"
NAMES = {LYRA: "Lyra Voss", CORIN: "Corin Hale", MIRA: "Mira Quill"}


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
            {"card": {"name": NAMES[persona_id]}, "first_mes": ""},
        )
    log.append_event(
        sid,
        "system",
        "user",
        {"event": "world_ref", "world_id": WORLD_ID, "name": WORLD_NAME},
    )


# -- cast_delta_notice: the diff seam ----------------------------------------


def test_no_notice_for_a_plain_card_chat(build_log):
    """No ``world_ref`` -> the session's own cards ARE its definition (no diff)."""
    log = build_log()
    sid = "plain"
    log.append_event(sid, "system", "system", {"card": {"name": "Lyra Voss"}})
    assert ensure_cast_snapshot(log, sid) == "[cast: Lyra Voss]"
    assert cast_delta_notice(log, sid) is None


def test_no_notice_before_the_session_cast_is_snapshotted(build_log):
    """No snapshot yet -> there is no session fact to be stale against."""
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    assert cast_delta_notice(log, sid) is None


def test_no_notice_when_the_world_matches_the_snapshot(build_log):
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    assert resolve_variables(log, sid, "hi")["char"] == "[cast: Lyra Voss, Corin Hale]"
    assert cast_delta_notice(log, sid) is None


def test_notice_names_an_actor_added_to_the_world(build_log):
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    assert resolve_variables(log, sid, "hi")["char"] == "[cast: Lyra Voss, Corin Hale]"

    # Studio edit: the world definition gains Mira.
    _edit_world(log, [LYRA, CORIN, MIRA])
    notice = cast_delta_notice(log, sid)
    assert notice is not None
    assert "added: Mira Quill" in notice
    assert "[cast: Lyra Voss, Corin Hale, Mira Quill]" in notice
    # The session's ``{{char}}`` fact is UNCHANGED (static prefix cache safety).
    assert resolve_variables(log, sid, "hi again")["char"] == (
        "[cast: Lyra Voss, Corin Hale]"
    )


def test_notice_names_an_actor_removed_from_the_world(build_log):
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    assert resolve_variables(log, sid, "hi")["char"] == "[cast: Lyra Voss, Corin Hale]"

    _edit_world(log, [LYRA])
    notice = cast_delta_notice(log, sid)
    assert notice is not None
    assert "removed: Corin Hale" in notice


def test_no_notice_for_a_tombstoned_world(build_log):
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    resolve_variables(log, sid, "hi")
    log.append_event(
        "__worlds__", "system", "user", {"event": "world_deleted", "world_id": WORLD_ID}
    )
    assert cast_delta_notice(log, sid) is None


# -- byte-stability: static prefix frozen, notice dynamic --------------------


def test_static_prefix_is_byte_identical_while_the_notice_is_dynamic(build_log):
    """(c) build -> studio edit -> rebuild: prefix unchanged, notice present."""
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    resolve_variables(log, sid, "I look around.")  # freezes the session cast fact

    before = assemble_turn_context(log, sid, user_text="I look around.")
    assert before.static_parts
    assert "Cast update" not in (before.notice or "")
    prefix_before = static_prefix_parts(log, sid)

    _edit_world(log, [LYRA, CORIN, MIRA])

    after = assemble_turn_context(log, sid, user_text="I look again.")
    # Static prefix (the card/cast block the prompt cache covers) is unchanged.
    assert after.static_parts == before.static_parts
    assert static_prefix_parts(log, sid) == prefix_before
    # The dynamic post-user region carries the change.
    assert after.notice is not None
    assert "Cast update" in after.notice
    assert "added: Mira Quill" in after.notice


def test_notice_is_hash_neutral_so_replay_stays_byte_faithful(build_log):
    """The notice rides ``<engine_notice>`` (not ``context_parts``) by design.

    The provenance hash anchors ``context_parts``. A notice derived from the
    CURRENT world definition cannot be reconstructed for pre-edit turns, so
    hashing it would drift their replay; the D22/B4 injection seam keeps it out.
    """
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    resolve_variables(log, sid, "I look around.")

    before = context_suffix_parts(log, sid, user_text="I look around.")
    hash_before = facts_hash(join_facts_suffix(before))

    _edit_world(log, [LYRA, CORIN, MIRA])

    after = context_suffix_parts(log, sid, user_text="I look again.")
    hash_after = facts_hash(join_facts_suffix(after))
    # The hash anchor is unaffected by the studio edit: no drift for prior turns.
    assert after == before
    assert hash_after == hash_before
    assert "Cast update" not in join_facts_suffix(after)


# -- end to end through the real narrator builder ----------------------------


def test_builder_prompt_carries_the_notice_and_a_stable_prefix(build_log):
    log = build_log()
    sid = "chat"
    _seed_world_chat(log, sid, [LYRA, CORIN], [LYRA, CORIN])
    build = make_narrator_message_builder(log)

    first = build(initial_turn_state(sid, "I look around.", "narrate"))
    joined_first = "\n".join(str(m.get("content", "")) for m in first)
    assert "Cast update" not in joined_first
    prefix_first = static_prefix_parts(log, sid)

    _edit_world(log, [LYRA, CORIN, MIRA])

    second = build(initial_turn_state(sid, "I look again.", "narrate"))
    joined_second = "\n".join(str(m.get("content", "")) for m in second)
    assert "Cast update" in joined_second
    assert "added: Mira Quill" in joined_second
    # The static card/cast prefix did not change between the two builds.
    assert static_prefix_parts(log, sid) == prefix_first
