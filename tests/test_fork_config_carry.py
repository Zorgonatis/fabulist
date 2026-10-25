"""A fork carries session-constant config assigned *after* the swiped turn.

DEFECT (bug-wave): a reroll/swipe forks the parent at the last user turn
(``fork_session(parent, at_seq=user_seq, new_id)``). Session-constant config the
user assigned AFTER that turn -- a persona, world, assistant or memory toggle --
sat at ``seq > at_seq``, outside the branch's inherited chain window, so the
branch silently lost it. Live symptom: the narrator no longer knew the user's
name after a reroll because ``chat_persona`` was invisible on the branch.

The fix is in ``EventLog.fork_session``: history still cuts at the turn point
(FORK-SEMANTICS F2 -- no prose/narrative/user rows are copied), but the parent's
whitelisted config rows with ``seq > at_seq`` are re-appended onto the branch.
Rows at/before the cut already arrive through the chain and are NOT copied (that
would duplicate them); the readers' latest-wins folds pick the newest value per
key, and a branch-local write (higher seq) still wins.
"""

from __future__ import annotations

from fabulist.api.chat_config_api import resolve_recall_config
from fabulist.assistants.panel import latest_chat_assistant
from fabulist.log import EventLog
from fabulist.memory.scope import LAYER_WORLD, collection_for_world, resolve_scope
from fabulist.personas.registry import persona_for_chat
from fabulist.prompts.variables import PERSONAS_ANCHOR, resolve_variables

#: A persona definition on the reserved anchor (so ``{{user}}`` resolves a name).
PERSONA_P1 = {"event": "persona", "persona_id": "p1", "name": "Wanderer"}
PERSONA_P2 = {"event": "persona", "persona_id": "p2", "name": "Bob"}


def _seed_anchor(log: EventLog) -> None:
    log.append_event(PERSONAS_ANCHOR, "system", "user", PERSONA_P1)
    log.append_event(PERSONAS_ANCHOR, "system", "user", PERSONA_P2)


def _turn(log: EventLog, sid: str, text: str = "I enter the market.") -> int:
    """Append one user turn + reply; return the user event's ``seq`` (the cut)."""
    user = log.append_event(sid, "user", "user", {"text": text})
    log.append_event(sid, "narrative", "narrator", {"prose": "Lanterns sway.", "segments": []})
    return user.seq


def _own_kinds(log: EventLog, sid: str) -> list[str]:
    return [
        e.payload.get("event")
        for e in log.get_events(sid)
        if e.type == "system" and isinstance(e.payload, dict)
    ]


# --- the bug: config assigned after the turn is carried ---------------------


def test_fork_carries_config_assigned_after_the_turn(log):
    _seed_anchor(log)
    parent, branch = "base", "branch"
    cut = _turn(log, parent)  # user seq 1, narrative seq 2

    # Exactly what the UI PATCHes AFTER the turn: config at seq 3+.
    log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    log.append_event(
        parent, "system", "system", {"event": "world_ref", "world_id": "w1", "name": "Rain-World"}
    )
    log.append_event(parent, "system", "user", {"event": "chat_assistant", "assistant_id": "a1"})
    log.append_event(parent, "system", "user", {"event": "chat_memory", "enabled": False})

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    # The reporter's scenario: the branch now knows the user's persona.
    assert persona_for_chat(log, branch) == "p1"
    values = resolve_variables(log, branch, "I look around.")
    assert values["user"] == "Wanderer"
    assert values["persona"] == "Wanderer"
    assert values["world"] == "Rain-World"

    scope = resolve_scope(log, branch)
    # D53 supersedes D16's `p_` persona fence: the carried `world_ref` puts this
    # chat at the WORLD layer, and the branch inherits it (never `p_p1`).
    assert scope.persona_id == "p1"  # user persona is still recorded (not a fence)
    assert scope.layer == LAYER_WORLD
    assert scope.world_id == "w1"
    assert scope.collection == collection_for_world("w1")
    assert scope.enabled is False

    assert latest_chat_assistant(log, branch) == "a1"


def test_fork_does_not_duplicate_config_at_or_before_the_cut(log):
    parent, branch = "base", "branch"
    # Persona assigned BEFORE the cut: inherited via the chain, never copied.
    log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    cut = _turn(log, parent)  # user seq 2, narrative seq 3

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    assert [k for k in _own_kinds(log, branch) if k == "chat_persona"] == []
    assert log.get_events(branch) == []
    # ...yet the branch still resolves it through the inherited chain.
    assert persona_for_chat(log, branch) == "p1"


def test_fork_carries_only_post_cut_rows_and_latest_wins(log):
    parent, branch = "base", "branch"
    log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    cut = _turn(log, parent)  # seq 2/3
    # Reassign after the cut: only this row should be carried, and it wins.
    log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": "p2"})

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    own = [
        e.payload["persona_id"]
        for e in log.get_events(branch)
        if e.payload.get("event") == "chat_persona"
    ]
    assert own == ["p2"]  # pre-cut p1 is inherited, not duplicated
    assert persona_for_chat(log, branch) == "p2"


def test_fork_resolves_latest_when_several_post_cut_writes_exist(log):
    """Latest-wins per config key across the carried rows."""
    _seed_anchor(log)
    parent, branch = "base", "branch"
    cut = _turn(log, parent)
    log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": "p2"})

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    assert persona_for_chat(log, branch) == "p2"
    assert resolve_variables(log, branch, "hi")["user"] == "Bob"


def test_branch_local_config_change_beats_carried_config(log):
    _seed_anchor(log)
    parent, branch = "base", "branch"
    cut = _turn(log, parent)
    log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    log.fork_session(parent, at_seq=cut, new_session_id=branch)
    assert persona_for_chat(log, branch) == "p1"

    # A branch-local write lands above the carried seq and wins.
    log.append_event(branch, "system", "user", {"event": "chat_persona", "persona_id": "p2"})
    assert persona_for_chat(log, branch) == "p2"
    assert resolve_variables(log, branch, "hi")["user"] == "Bob"


# --- F2 is preserved: no history is copied ----------------------------------


def test_fork_still_copies_no_narrative_or_user_rows(log):
    parent, branch = "base", "branch"
    cut = _turn(log, parent)  # user seq 1, narrative seq 2
    log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": "p1"})

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    own = log.get_events(branch)
    assert [e.type for e in own] == ["system"]  # only the carried config row
    assert all(e.type not in ("user", "narrative") for e in own)
    # The reply after the cut is NOT inherited; the user turn at the cut IS.
    projection = log.get_projection(branch)
    assert projection["narratives"] == []
    assert [u["payload"]["text"] for u in projection["user_messages"]] == [
        "I enter the market."
    ]


# --- the rest of the session-constant config family -------------------------


def test_fork_carries_memory_config_knobs(log):
    parent, branch = "base", "branch"
    cut = _turn(log, parent)
    log.append_event(parent, "system", "user", {"event": "chat_memory", "enabled": False})
    log.append_event(
        parent,
        "system",
        "user",
        {
            "event": "chat_memory_config",
            "k": 7,
            "block_chars": 1234,
            "rerank_enabled": True,
            "query_mode": "message",
            "overfetch": 3,
        },
    )

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    cfg = resolve_recall_config(log, branch)
    assert cfg.k == 7
    assert cfg.block_chars == 1234
    assert cfg.enabled is False


def test_fork_does_not_carry_content_events(log):
    """Greeting selection / session modules are content, not live config."""
    parent, branch = "base", "branch"
    cut = _turn(log, parent)
    log.append_event(parent, "system", "user", {"event": "greeting_selected", "index": 2})
    log.append_event(
        parent,
        "system",
        "system",
        {
            "event": "session_module",
            "module_id": "m1",
            "name": "M",
            "anchor": "top",
            "role": "system",
            "content": "text",
            "enabled": True,
        },
    )

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    assert log.get_events(branch) == []


def test_prune_removes_carried_config_with_the_branch(log):
    """Carried rows are the branch's own, so a hard prune takes them too."""
    parent, branch = "base", "branch"
    cut = _turn(log, parent)
    log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": "p1"})

    log.fork_session(parent, at_seq=cut, new_session_id=branch)
    assert len(log.get_events(branch)) == 1

    removed = log.prune_branch(branch)

    assert removed == 1
    assert log.get_events(branch) == []
    # The parent's row is untouched.
    assert persona_for_chat(log, parent) == "p1"
