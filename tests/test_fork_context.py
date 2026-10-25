"""Fork-context coverage: branch turns must inherit the session-constant context.

DEFECT (bug-wave): a fork copies no events (``EventLog.fork_session``); history
was made fork-aware through ``get_projection`` but the session-constant readers
still called the session-local ``get_events``, so a branch/regenerate turn
rendered "as if no personas exist" — an empty character card, empty cast, empty
macros, a sandbox memory scope, and an EMPTY frozen ``cast_snapshot``.

The fix routes those readers through the fork-aware
:meth:`EventLog.get_chain_events` (a sibling of ``get_events`` that walks the
base chain via ``_stream_events``). ``get_events`` itself stays session-local —
pinned by ``tests/test_forks.py`` / ``tests/test_log.py`` and asserted here.

Seeded with a real tmp-path :class:`~fabulist.log.EventLog` (the house
convention: ``tests/test_facts_suffix.py`` / ``tests/test_variables.py``).
"""

from __future__ import annotations

from fabulist.cards.homes import ANCHOR_TOP, session_modules_from_log
from fabulist.context.facts import (
    assemble_turn_context,
    context_suffix_parts,
    facts_hash,
    session_persona_block,
    session_stt_block,
    static_prefix_parts,
)
from fabulist.log import EventLog
from fabulist.memory.scope import LAYER_WORLD, collection_for_world, resolve_scope
from fabulist.personas.registry import persona_for_chat
from fabulist.prompts.cast import CAST_SNAPSHOT_EVENT, ensure_cast_snapshot
from fabulist.prompts.variables import PERSONAS_ANCHOR, resolve_variables

LYRA = {
    "name": "Lyra Voss",
    "description": "A wandering cartographer.",
    "personality": "Curious and wry.",
    "scenario": "A rain-soaked night market.",
}
LYRA_GREETING = "Lyra looks up from her maps. 'You lost?'"
WORLD_NAME = "The Rain-Soaked Market"


def _seed_parent(log: EventLog, sid: str) -> None:
    """Card session + persona assignment + world snapshot + module + a turn."""
    log.append_event(sid, "system", "user", {"event": "session_created"})
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": LYRA_GREETING})
    log.append_event(
        sid, "system", "system", {"event": "stt", "text": "a bell rings"}
    )
    log.append_event(
        sid,
        "system",
        "system",
        {
            "event": "session_module",
            "module_id": "card:Lyra Voss:system_prompt",
            "name": "Card system prompt",
            "anchor": ANCHOR_TOP,
            "role": "system",
            "content": "Stay in Lyra's voice.",
            "enabled": True,
            "origin": "card:Lyra Voss",
        },
    )
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": "p1", "name": "Alice"},
    )
    log.append_event(
        sid, "system", "user", {"event": "chat_persona", "persona_id": "p1"}
    )
    log.append_event(sid, "system", "user", {"event": "chat_memory", "enabled": False})
    log.append_event(
        sid,
        "system",
        "system",
        {"event": "world_ref", "world_id": "w1", "name": WORLD_NAME},
    )
    log.append_event(sid, "user", "user", {"text": "I enter the market."})
    log.append_event(sid, "narrative", "narrator", {"prose": "Lanterns sway.", "segments": []})


def _fork(log: EventLog, parent: str, branch: str, at_seq: int) -> None:
    log.fork_session(parent, at_seq=at_seq, new_session_id=branch)


# --- get_chain_events: the new fork-aware read ------------------------------


def test_chain_events_matches_get_events_for_a_root(log: EventLog):
    sid = "root"
    _seed_parent(log, sid)
    root_ids = [e.id for e in log.get_events(sid)]
    assert [e.id for e in log.get_chain_events(sid)] == root_ids


def test_chain_events_inherits_parent_prefix_on_a_branch(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)
    log.append_event(branch, "user", "user", {"text": "I look around."})

    chain = log.get_chain_events(branch)
    assert [e.payload.get("card", {}).get("name") for e in chain if e.payload.get("card")] == [
        "Lyra Voss"
    ]
    # The branch's own row is included after the inherited prefix.
    assert any(e.session_id == branch for e in chain)
    assert chain[-1].session_id == branch


def test_get_events_stays_session_local_on_a_branch(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)
    log.append_event(branch, "user", "user", {"text": "branch only"})

    # The raw audit view must NOT walk forks (pinned contract).
    own = log.get_events(branch)
    assert [e.session_id for e in own] == [branch]
    assert all(e.session_id != parent for e in own)
    assert own[0].payload == {"text": "branch only"}


def test_chain_events_honours_max_schema_version(log: EventLog):
    sid = "schema"
    log.append_event(sid, "user", "user", {"text": "old"})
    log.append_event(sid, "user", "user", {"text": "new"}, schema_version=999)
    filtered = log.get_chain_events(sid, max_schema_version=1)
    assert [e.payload["text"] for e in filtered] == ["old"]


# --- session-constant context readers on a branch ---------------------------


def test_branch_persona_block_is_non_empty(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)

    block = session_persona_block(log, branch)
    assert block is not None
    assert block.startswith("### Character: Lyra Voss")
    assert block.endswith(f"Opening scene (already established): {LYRA_GREETING}")
    # The static pre-history prefix carries it too, plus the D54 identity block
    # inherited from the parent's persona assignment (p1 = Alice).
    assert static_prefix_parts(log, branch) == [
        block,
        "### User (the player)\nName: Alice",
    ]


def test_branch_static_context_and_hash_are_non_empty(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)

    # Replay parity: the canonical context (and thus the provenance hash) must
    # now carry the persona, not an empty string.
    context = context_suffix_parts(log, branch, user_text="I look around.")
    assert any(part.startswith("### Character: Lyra Voss") for part in context)
    blocks = assemble_turn_context(log, branch, user_text="I look around.")
    assert blocks.static_parts
    assert facts_hash("\n\n".join(context)) != facts_hash("")

    # The branch still inherits the parent's stt block.
    assert session_stt_block(log, branch) is not None


# --- macros: {{persona}} / {{user}} / {{world}} on a branch -----------------


def test_branch_macros_resolve_parent_persona_and_world(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)

    values = resolve_variables(log, branch, "I look around.")
    assert values["persona"] == "Alice"
    assert values["user"] == "Alice"
    assert values["world"] == WORLD_NAME
    # The branch inherits the parent's card: its cast is non-empty and live.
    assert values["char"] == "[cast: Lyra Voss]"


def test_branch_local_persona_reassignment_overrides_parent(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)
    # A branch-local assignment has a higher seq (base_seq + 1) and wins.
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": "p2", "name": "Bob"},
    )
    log.append_event(branch, "system", "user", {"event": "chat_persona", "persona_id": "p2"})

    assert persona_for_chat(log, branch) == "p2"
    assert resolve_variables(log, branch, "hi")["user"] == "Bob"


# --- cast_snapshot ----------------------------------------------------------


def test_branch_cast_snapshot_is_non_empty_and_frozen(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)

    cast = ensure_cast_snapshot(log, branch)
    assert cast == "[cast: Lyra Voss]"
    snapshots = [
        e
        for e in log.get_events(branch)
        if e.type == "system" and e.payload.get("event") == CAST_SNAPSHOT_EVENT
    ]
    assert [s.payload.get("cast") for s in snapshots] == ["[cast: Lyra Voss]"]
    # Memoised: a second resolve appends nothing.
    assert ensure_cast_snapshot(log, branch) == "[cast: Lyra Voss]"
    assert len([e for e in log.get_events(branch) if e.payload.get("event") == CAST_SNAPSHOT_EVENT]) == 1


def test_branch_inherits_a_parent_frozen_cast_snapshot(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    # Freeze the parent BEFORE the fork so the snapshot is inherited.
    assert ensure_cast_snapshot(log, parent) == "[cast: Lyra Voss]"
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)

    assert ensure_cast_snapshot(log, branch) == "[cast: Lyra Voss]"
    # Nothing extra appended on the branch: it read the inherited snapshot.
    assert [
        e for e in log.get_events(branch) if e.payload.get("event") == CAST_SNAPSHOT_EVENT
    ] == []


# --- modules / persona assignment / memory scope ----------------------------


def test_branch_inherits_session_modules(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)

    modules = session_modules_from_log(log, branch)
    assert [m["module_id"] for m in modules] == ["card:Lyra Voss:system_prompt"]
    assert modules[0]["content"] == "Stay in Lyra's voice."


def test_branch_inherits_persona_assignment(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)
    assert persona_for_chat(log, branch) == "p1"


def test_branch_inherits_memory_scope(log: EventLog):
    parent, branch = "parent", "branch"
    _seed_parent(log, parent)
    fork_seq = log.get_events(parent)[-1].seq
    _fork(log, parent, branch, fork_seq)

    scope = resolve_scope(log, branch)
    # D53 supersedes D16's `p_` persona fence: this chat carries a `world_ref`,
    # so it lives at the WORLD layer and the fork-aware chain makes the branch
    # inherit it (the branch writes/reads `w_w1`, never `p_p1`).
    assert scope.persona_id == "p1"  # user persona is still recorded (not a fence)
    assert scope.layer == LAYER_WORLD
    assert scope.world_id == "w1"
    assert scope.collection == collection_for_world("w1")
    assert scope.read_collections == (collection_for_world("w1"),)
    assert scope.enabled is False


def test_sandbox_branch_still_resolves_to_chat_local_scope(log: EventLog):
    parent, branch = "parent", "branch"
    log.append_event(parent, "user", "user", {"text": "no persona here"})
    _fork(log, parent, branch, at_seq=1)
    scope = resolve_scope(log, branch)
    assert scope.persona_id is None
