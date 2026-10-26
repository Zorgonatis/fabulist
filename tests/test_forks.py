"""Fork/branch mechanics tests (W2.2).

Covers the four deliverables layered on the event log (D9/D10):

* ``keep_turn`` explicit ``turn_kept`` marker (F5).
* ``prune_branch`` hard delete of a branch's own rows + bookkeeping row (F4),
  refusing sessions that other branches fork from.
* F7 revision carryover: a late edit to an inherited turn inside the base must
  still show in a branch projection.
* Session-constant config carry: a fork re-appends the parent's persona/world/
  assistant/memory rows assigned *after* the cut (see
  ``tests/test_fork_config_carry.py``) while still copying no *history*.
* ``last_swipe_target`` sibling ordering for the swipe UI.
* The ``branches`` router endpoints against a real tmp ``EventLog``.

NOTE on the prune pins below: ``removed == 1`` / ``events_deleted == 1`` used to
encode "a fork copies nothing". Two rules moved them since. A fork copies no
history but DOES carry post-cut config rows (these fixtures carry none), and
since issue #62 every fork also writes ONE ``session_state_snapshot`` row into
the new chat -- so the count is the branch's own appended rows, snapshot
included. The pin is about "the parent contributes no carried history", never
about a blanket no-copy rule (deliberate pin evolution: the old rule starved
swipes of config, and a fork with no state at all starved it of everything).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.branches import router as branches_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.log.repo import PruneError, is_state_snapshot_event
from fabulist.prompts.variables import PERSONAS_ANCHOR


# --------------------------------------------------------------------------
# Repo-level mechanics
# --------------------------------------------------------------------------


def test_keep_turn_appends_marker(log):
    log.append_event("s", "user", "user", {"text": "hi"})
    log.append_event("s", "narrative", "narrator", {"prose": "hello"})

    marker = log.keep_turn("s", at_seq=2)

    assert marker.type == "system"
    assert marker.actor == "user"
    assert marker.payload == {"event": "turn_kept", "seq": 2}

    kept = [
        e
        for e in log.get_events("s")
        if e.type == "system" and e.payload.get("event") == "turn_kept"
    ]
    assert [e.payload["seq"] for e in kept] == [2]
    # append-only: earlier rows survive
    assert any(e.payload.get("text") == "hi" for e in log.get_events("s"))


def test_keep_turn_rejects_negative_seq(log):
    with pytest.raises(ValueError):
        log.keep_turn("s", at_seq=-1)


def test_prune_branch_removes_own_rows_and_session_row(log):
    log.append_event("base", "user", "user", {"text": "hi"})
    log.append_event("base", "narrative", "narrator", {"prose": "hello"})
    log.fork_session("base", 1, "branch")
    # New semantics, explicit: the parent carries no config and no history is
    # copied (F2) -- the fork's only own row is the issue #62 state snapshot.
    own = log.get_events("branch")
    assert [e.type for e in own] == ["system"]
    assert is_state_snapshot_event(own[0])
    log.append_event("branch", "narrative", "narrator", {"prose": "branch-only"})
    base_ids = [e.id for e in log.get_events("base")]

    assert log.list_branches("base") == ["branch"]
    assert log.get_session("branch") is not None

    removed = log.prune_branch("branch")

    assert removed == 2  # the snapshot row + the branch-only narrative
    assert log.get_events("branch") == []
    assert log.get_session("branch") is None
    # The ghost row is gone: list_branches no longer advertises it.
    assert log.list_branches("base") == []
    # The base session's rows are never touched by a prune.
    assert [e.id for e in log.get_events("base")] == base_ids
    assert log.get_projection("base")["narratives"][0]["payload"]["prose"] == "hello"


def test_prune_branch_refuses_parent_with_children(log):
    log.append_event("base", "user", "user", {"text": "hi"})
    log.fork_session("base", 1, "branch")

    with pytest.raises(PruneError):
        log.prune_branch("base")

    # Nothing was deleted: both sessions and their rows survive.
    assert log.get_session("base") is not None
    assert log.get_session("branch") is not None
    assert log.get_events("base")
    assert log.list_branches("base") == ["branch"]


def test_prune_branch_clears_dangling_supersede_links(log):
    original = log.append_event("base", "narrative", "narrator", {"prose": "v1"})
    log.fork_session("base", 1, "branch")
    revision = log.append_revision(
        "branch",
        original.id,
        "human",
        {"revised": {"prose": "edit"}, "reason": "fix"},
    )
    assert log.get_event(original.id).superseded_by == revision.id

    log.prune_branch("branch")

    # The surviving base row no longer points at a pruned revision.
    assert log.get_event(original.id).superseded_by is None
    assert log.get_projection("base")["narratives"][0]["payload"]["prose"] == "v1"


def test_prune_branch_unknown_session_is_noop(log):
    assert log.prune_branch("missing") == 0


def test_fork_carries_late_revision_of_inherited_turn(log):
    """F7: edit an early turn in the base, then read the branch projection."""
    log.append_event("base", "user", "user", {"text": "I slip into the market."})
    early = log.append_event("base", "narrative", "narrator", {"prose": "v1"})
    log.fork_session("base", 2, "branch")

    # The edit lands at the base's tail *after* the fork (seq 3 > cut 2).
    log.append_revision(
        "base", early.id, "human", {"revised": {"prose": "v1-edited"}, "reason": "fix"}
    )

    branch = log.get_projection("branch")
    assert [n["payload"]["prose"] for n in branch["narratives"]] == ["v1-edited"]
    # The base sees its own edit too.
    base = log.get_projection("base")
    assert [n["payload"]["prose"] for n in base["narratives"]] == ["v1-edited"]


def test_fork_ignores_revision_targeting_events_after_the_cut(log):
    log.append_event("base", "user", "user", {"text": "hi"})
    log.fork_session("base", 1, "branch")
    later = log.append_event("base", "narrative", "narrator", {"prose": "reply1"})
    log.append_revision(
        "base", later.id, "human", {"revised": {"prose": "edited"}, "reason": "x"}
    )

    # Cut at seq 1 inherits only the user message; reply1's edit must not leak in.
    branch = log.get_projection("branch")
    assert branch["narratives"] == []
    assert [u["payload"]["text"] for u in branch["user_messages"]] == ["hi"]


def test_fork_carried_revision_resolves_latest_wins(log):
    log.append_event("base", "user", "user", {"text": "hi"})
    early = log.append_event("base", "narrative", "narrator", {"prose": "v1"})
    log.fork_session("base", 2, "branch")

    log.append_revision(
        "base", early.id, "human", {"revised": {"prose": "edit-1"}, "reason": "a"}
    )
    log.append_revision(
        "base", early.id, "human", {"revised": {"prose": "edit-2"}, "reason": "b"}
    )

    branch = log.get_projection("branch")
    assert [n["payload"]["prose"] for n in branch["narratives"]] == ["edit-2"]


def test_nested_fork_carries_ancestor_revision(log):
    log.append_event("root", "user", "user", {"text": "hi"})
    early = log.append_event("root", "narrative", "narrator", {"prose": "v1"})
    log.append_event("root", "user", "user", {"text": "next"})
    log.fork_session("root", 3, "mid")
    log.append_event("mid", "narrative", "narrator", {"prose": "mid reply"})
    # Cut at mid's LAST row: since issue #62 the branch's first own seq is its
    # state snapshot, so the reply sits one seq higher than it used to.
    log.fork_session("mid", log.get_events("mid")[-1].seq, "leaf")

    # Edit the root's early turn after the leaf exists.
    log.append_revision(
        "root", early.id, "human", {"revised": {"prose": "v1-edit"}, "reason": "x"}
    )

    leaf = log.get_projection("leaf")
    assert [n["payload"]["prose"] for n in leaf["narratives"]] == [
        "v1-edit",
        "mid reply",
    ]


def test_get_session_bookkeeping(log):
    log.append_event("base", "user", "user", {"text": "hi"})
    log.fork_session("base", 7, "b")

    root = log.get_session("base")
    assert root["session_id"] == "base"
    assert root["base_session"] is None
    assert root["base_seq"] is None
    assert root["created_ts"]

    info = log.get_session("b")
    assert info["base_session"] == "base"
    assert info["base_seq"] == 7
    assert info["created_ts"]
    assert log.get_session("missing") is None


def test_last_swipe_target_orders_siblings_by_creation(log):
    log.append_event("base", "user", "user", {"text": "hi"})
    log.append_event("base", "narrative", "narrator", {"prose": "r1"})
    log.fork_session("base", 1, "b1")
    log.fork_session("base", 1, "b2")
    # A fork at a *different* cut is not a sibling (different turn point).
    log.fork_session("base", 2, "other")

    assert log.last_swipe_target("b2") == ["base", "b1", "b2"]
    assert log.last_swipe_target("b1") == ["base", "b1", "b2"]
    # The original line reads the group at its LATEST pivot (issue #59): the
    # origin line now lists the reply regenerated from it (it used to list only
    # itself, which is what made the swipe back one-way). The earlier cut
    # ("b1"/"b2" at seq 1) is a different position and stays out of it -- but
    # it is still the same group from either side of that pivot.
    assert log.last_swipe_target("base") == ["base", "other"]
    assert log.last_swipe_target("other") == ["base", "other"]
    assert log.last_swipe_target("missing") == []


# --------------------------------------------------------------------------
# Router endpoints against a real tmp EventLog
# --------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps (with the branches router mounted) sharing one log."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(branches_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create(client: httpx.AsyncClient, title: str = "") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def _seed_reply(log: EventLog, sid: str, prose: str) -> None:
    log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": "", "segments": [{"kind": "prose", "text": prose}]},
    )


def test_router_fork_and_list_branches(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            app.state.log.append_event(base, "user", "user", {"text": "enter"})
            _seed_reply(app.state.log, base, "Welcome to the market.")

            fork = await client.post(
                f"/api/sessions/{base}/fork", json={"at_seq": 2}
            )
            assert fork.status_code == 200, fork.text
            new_id = fork.json()["session_id"]
            _seed_reply(app.state.log, new_id, "The lantern flickers.")

            listed = await client.get(f"/api/sessions/{base}/branches")
            return base, new_id, listed

    base, new_id, listed = run(scenario())
    assert listed.status_code == 200
    body = listed.json()
    assert body["session_id"] == base
    assert len(body["branches"]) == 1
    entry = body["branches"][0]
    assert entry["session_id"] == new_id
    assert entry["base_session"] == base
    assert entry["base_seq"] == 2
    assert entry["created_at"]
    assert entry["snippet"] == "The lantern flickers."
    assert entry["visible"] is True
    assert entry["hidden"] is False


def test_router_list_branches_empty_and_unknown(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            known = await client.get(f"/api/sessions/{base}/branches")
            unknown = await client.get("/api/sessions/nope/branches")
            return known, unknown

    known, unknown = run(scenario())
    assert known.status_code == 200
    assert known.json()["branches"] == []
    assert unknown.status_code == 404


def test_router_prune_branch_hard_deletes(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            app.state.log.append_event(base, "user", "user", {"text": "enter"})
            new_id = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": 2, "new_session_id": "child-1"},
                )
            ).json()["session_id"]
            _seed_reply(app.state.log, new_id, "Hello from the branch.")

            pruned = await client.delete(f"/api/sessions/{new_id}/branch")
            after = await client.get(f"/api/sessions/{base}/branches")
            # The soft-hide list still shows the base session.
            sessions = await client.get("/api/sessions")
            return base, new_id, pruned, after, sessions

    base, new_id, pruned, after, sessions = run(scenario())
    assert pruned.status_code == 200
    assert pruned.json() == {
        "session_id": new_id,
        "pruned": True,
        # the issue #62 state snapshot row + the issue #86 title carry
        # + the branch's own reply
        "events_deleted": 3,
    }
    assert after.json()["branches"] == []
    assert [s["session_id"] for s in sessions.json()["sessions"]] == [base]

    # Hard delete really removed rows + bookkeeping (no ghost row).
    log = EventLog(log_path)
    assert log.get_session(new_id) is None
    assert log.get_events(new_id) == []
    assert log.get_session(base) is not None
    log.close()


def test_router_prune_refuses_parent_with_children(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            app.state.log.append_event(base, "user", "user", {"text": "enter"})
            await client.post(f"/api/sessions/{base}/fork", json={"at_seq": 2})
            resp = await client.delete(f"/api/sessions/{base}/branch")
            return base, resp

    base, resp = run(scenario())
    assert resp.status_code == 409

    # Refusal is non-destructive: both sessions and their rows survive.
    log = EventLog(log_path)
    assert log.get_session(base) is not None
    assert log.get_events(base)
    assert log.list_branches(base)
    log.close()


def test_router_fork_unknown_and_bad_body(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            unknown = await client.post(
                "/api/sessions/nope/fork", json={"at_seq": 2}
            )
            base = await _create(client, title="Base")
            negative = await client.post(
                f"/api/sessions/{base}/fork", json={"at_seq": -1}
            )
            pruned_unknown = await client.delete("/api/sessions/nope/branch")
            return unknown, negative, pruned_unknown

    unknown, negative, pruned_unknown = run(scenario())
    assert unknown.status_code == 404
    assert negative.status_code == 422
    assert pruned_unknown.status_code == 404


def test_router_fork_carries_persona_assigned_after_the_turn(build_app):
    """The reported bug, end-to-end: PATCH persona after the turn, then swipe."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            log.append_event(
                PERSONAS_ANCHOR,
                "system",
                "user",
                {"event": "persona", "persona_id": "p1", "name": "Wanderer"},
            )
            user = log.append_event(base, "user", "user", {"text": "I enter."})
            _seed_reply(log, base, "Lanterns sway.")
            # Assign the persona AFTER the turn, through the live route.
            assigned = await client.post(
                f"/api/sessions/{base}/persona", json={"persona_id": "p1"}
            )
            assert assigned.status_code == 200, assigned.text
            fork = await client.post(
                f"/api/sessions/{base}/fork", json={"at_seq": user.seq}
            )
            branch = fork.json()["session_id"]
            config = await client.get(f"/api/sessions/{branch}/chat-config")
            return base, branch, config

    base, branch, config = run(scenario())
    assert config.status_code == 200
    # Before the fix this echoed persona_id None on the branch.
    assert config.json()["persona_id"] == "p1"
