"""The swipe group is symmetric at the fork pivot (issue #59).

Regenerating a reply forks the session at the user message directly below the
reply being replaced (FORK-SEMANTICS F2): the old line and the new branch are
two replies *at the same position*. The group used to be enumerated from the
asking session's own ``(base_session, base_seq)`` pointer only, so it was one
sided -- the NEW branch listed the old line (swiping back worked) while the
ORIGINAL line listed nothing at all, and its arrow to the freshly regenerated
reply was gone (the reported bug).

These tests pin the group from BOTH sides of the pivot: the same set of replies
at that position, in one stable order, whichever session asks. The pivot is a
position in the shared chain, so a fork of a fork at the SAME cut (regenerate
twice) belongs to the one group, while a fork at a LATER cut is a different
position and must stay out.

The owner-approved fix is server-side only: the frontend already re-reads
``/branches`` after a fork (``refreshBranches``), so nothing there changes.
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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _user(log: EventLog, sid: str, text: str):
    return log.append_event(sid, "user", "user", {"text": text})


def _reply(log: EventLog, sid: str, prose: str):
    return log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": "", "segments": [{"kind": "prose", "text": prose}]},
    )


def _regenerate(log: EventLog, session_id: str, user_seq: int, new_id: str) -> str:
    """Fork the way the regenerate path does: cut at the user message below the reply.

    Mirrors ``forkTurn``: ``POST /fork {at_seq: lastUserSeq}`` then a
    ``regenerate:true`` turn that appends a narrative WITHOUT re-appending the
    inherited user row (F2). History is not copied -- the fork row is O(1).
    """
    log.fork_session(session_id, user_seq, new_id)
    return new_id


def _mark_era(log: EventLog, parent: str, child: str, *, at: int) -> None:
    """Append the era marker the compact service writes to the SUCCESSOR."""
    log.append_event(
        child,
        "system",
        "compaction",
        {
            "event": "compaction",
            "source_session": parent,
            "summary_ref": [parent, at],
            "covers_seqs": [1, at],
            "trigger": "manual",
            "stats": {"calls": 1, "prompt_tokens": 10, "model": "m"},
        },
    )


# --------------------------------------------------------------------------
# Repo level: the group, both directions
# --------------------------------------------------------------------------


def test_swipe_group_is_symmetric_after_a_regenerate_fork(log):
    """The reported bug, one regenerate: BOTH sides list both replies."""
    _user(log, "base", "one")
    _reply(log, "base", "first reply")
    pivot = _user(log, "base", "two")
    _reply(log, "base", "second reply")

    _regenerate(log, "base", pivot.seq, "regen")
    _reply(log, "regen", "second reply, again")

    # From the NEW branch (this worked before: the old line is its parent).
    assert log.last_swipe_target("regen") == ["base", "regen"]
    # From the ORIGINAL (this used to be ["base"] -- no way back to the fork).
    assert log.last_swipe_target("base") == ["base", "regen"]
    # Same set, same stable order, whichever side asks.
    assert log.last_swipe_target("base") == log.last_swipe_target("regen")


def test_swipe_group_is_symmetric_across_a_chain_of_regenerate_forks(log):
    """A fork of a fork AT THE SAME CUT is one group -- across both chains."""
    _user(log, "base", "one")
    _reply(log, "base", "first reply")
    pivot = _user(log, "base", "two")
    _reply(log, "base", "second reply")

    _regenerate(log, "base", pivot.seq, "regen-1")
    _reply(log, "regen-1", "second reply, again")
    # Regenerate again while standing on the first fork: same user message cut.
    _regenerate(log, "regen-1", pivot.seq, "regen-2")
    _reply(log, "regen-2", "second reply, a third time")

    group = ["base", "regen-1", "regen-2"]
    for sid in group:
        assert log.last_swipe_target(sid) == group, sid


def test_swipe_group_follows_the_latest_pivot_on_a_branch(log):
    """A later turn on a branch pivots later: that later group is the one shown."""
    _user(log, "base", "one")
    _reply(log, "base", "first reply")
    pivot = _user(log, "base", "two")
    _reply(log, "base", "second reply")

    _regenerate(log, "base", pivot.seq, "alt")
    _reply(log, "alt", "second reply, again")
    later = _user(log, "alt", "three")
    _reply(log, "alt", "third on alt")

    # Regenerating the LATEST reply on the branch pivots at the "three" message.
    _regenerate(log, "alt", later.seq, "alt-regen")
    _reply(log, "alt-regen", "third, again")

    assert log.last_swipe_target("alt") == ["alt", "alt-regen"]
    assert log.last_swipe_target("alt-regen") == ["alt", "alt-regen"]
    # The earlier pivot's group is unchanged and still symmetric at its own cut.
    assert log.last_swipe_target("base") == ["base", "alt"]


def test_swipe_group_keeps_a_lone_session_single(log):
    """No fork anywhere: the session is its own one-line group; unknown is empty."""
    _user(log, "solo", "hello")
    assert log.last_swipe_target("solo") == ["solo"]
    assert log.last_swipe_target("missing") == []


# --------------------------------------------------------------------------
# Router level: the payload the frontend actually reads
# --------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps (branches router mounted) sharing one tmp log."""
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


def test_branches_siblings_are_symmetric_at_the_pivot(build_app):
    """``GET /branches`` from BOTH sessions: same siblings, self located by index."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "first reply")
            pivot = _user(log, base, "two")
            _reply(log, base, "second reply")

            fork = await client.post(
                f"/api/sessions/{base}/fork", json={"at_seq": pivot.seq}
            )
            assert fork.status_code == 200, fork.text
            regen = fork.json()["session_id"]
            _reply(log, regen, "second reply, again")

            from_base = await client.get(f"/api/sessions/{base}/branches")
            from_regen = await client.get(f"/api/sessions/{regen}/branches")
            return base, regen, from_base, from_regen

    base, regen, from_base, from_regen = run(scenario())

    expected = [base, regen]
    for response, self_id in ((from_base, base), (from_regen, regen)):
        body = response.json()
        ids = [entry["session_id"] for entry in body["siblings"]]
        assert ids == expected
        # The index/count pair must locate SELF in the very same group.
        assert body["count"] == len(expected)
        assert body["index"] == ids.index(self_id) + 1
        assert [entry["is_self"] for entry in body["siblings"]] == [
            sid == self_id for sid in expected
        ]

    # The regenerated reply is the snippet the arrow promises.
    assert from_base.json()["siblings"][1]["snippet"] == "second reply, again"


def test_branches_siblings_keep_continuation_exclusions_both_ways(build_app):
    """A compaction era never joins the symmetric group, from either side."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            _user(log, base, "enter")
            _reply(log, base, "Lanterns sway.")

            alt = (
                await client.post(
                    f"/api/sessions/{base}/fork", json={"at_seq": 2}
                )
            ).json()["session_id"]
            _reply(log, alt, "A different reply.")
            era = (
                await client.post(
                    f"/api/sessions/{base}/fork", json={"at_seq": 2}
                )
            ).json()["session_id"]
            _mark_era(log, base, era, at=1)

            from_base = await client.get(f"/api/sessions/{base}/branches")
            from_alt = await client.get(f"/api/sessions/{alt}/branches")
            from_era = await client.get(f"/api/sessions/{era}/branches")
            return base, alt, era, from_base, from_alt, from_era

    base, alt, era, from_base, from_alt, from_era = run(scenario())

    assert [s["session_id"] for s in from_base.json()["siblings"]] == [base, alt]
    assert [s["session_id"] for s in from_alt.json()["siblings"]] == [base, alt]
    # A continuation is its own one-line group: no arrows across eras.
    assert [s["session_id"] for s in from_era.json()["siblings"]] == [era]
    assert from_era.json()["index"] == 1
    assert from_era.json()["count"] == 1
