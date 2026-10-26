"""Issue #74: a ``/fork`` cut must land ON a message of the source's chain.

The stale-cut rewind: a regenerate forked at an earlier turn because the client
sent a stored SIBLING pivot (the previous fork's own ``base_seq``) instead of
the message the reader acted on. The server guard is the last line of defence:
a fork cut that names a non-message row -- a ``session_created`` bookkeeping
row, a ``state-delta``, a ``roll``, or no row at all -- is refused with ``409``
``cut_not_a_message``. The client-side fix (retry forks the CURRENT session at
``lastUserSeq``) is what keeps this guard from ever firing in practice.

``delete-tail`` is EXEMPT by design: it cuts at the largest seq BELOW its pivot,
which is legitimately a non-message row (the kept turn's ``state-delta``), and
it calls ``fork_session`` directly rather than the ``/fork`` route. That
exemption is pinned below so the guard can never creep onto it.

Fixture pattern mirrors ``tests/test_delete_tail.py``: a real tmp event log under
a ``create_app(FakeTurnEngine(), ...)`` app driven through ``httpx.ASGITransport``.
The branches router is already mounted by ``create_app`` -- do not include it a
second time (a duplicate registration would shadow the first route).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp log; the branches router is mounted."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
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


def _user(log: EventLog, sid: str, text: str, *, ephemeral: bool = False):
    return log.append_event(sid, "user", "user", {"text": text}, ephemeral=ephemeral)


def _reply(log: EventLog, sid: str, prose: str):
    return log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": "", "segments": [{"kind": "prose", "text": prose}]},
    )


def _delta(log: EventLog, sid: str, payload: dict):
    return log.append_event(sid, "state-delta", "extractor", payload)


async def _fork(
    client: httpx.AsyncClient,
    sid: str,
    at_seq: int,
    *,
    new_session_id: str | None = None,
) -> httpx.Response:
    body: dict = {"at_seq": at_seq}
    if new_session_id is not None:
        body["new_session_id"] = new_session_id
    return await client.post(f"/api/sessions/{sid}/fork", json=body)


# --------------------------------------------------------------------------
# The guard: a cut naming no message is refused
# --------------------------------------------------------------------------


def test_fork_rejects_a_cut_that_names_no_message(build_app):
    """A system row, a state-delta and a missing seq are not valid cuts."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            delta = _delta(log, base, {"time": "08:00", "loc": "camp"})
            _user(log, base, "two")
            _reply(log, base, "reply two")
            # seq 1 is session_created (a system row); delta.seq is a state-delta;
            # 9999 names no row at all. None of them is a message.
            responses = [await _fork(client, base, seq) for seq in (1, delta.seq, 9999)]
            return base, responses

    base, responses = run(scenario())
    for resp in responses:
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == {"reason": "cut_not_a_message"}
    # A refused cut writes nothing: no branch was created.
    assert app.state.log.list_branches(base) == []


def test_fork_rejects_an_ephemeral_user_row_as_a_cut(build_app):
    """A streamed-but-not-transcript row (ephemeral) is not a message either."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            thinking = _user(log, base, "thinking", ephemeral=True)
            return await _fork(client, base, thinking.seq)

    resp = run(scenario())
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == {"reason": "cut_not_a_message"}


# --------------------------------------------------------------------------
# Happy paths: a user / narrative message cut is accepted
# --------------------------------------------------------------------------


def test_fork_accepts_a_user_message_cut(build_app):
    """The client's real cut: the seq of the user message acted on."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            user = _user(log, base, "two")
            _reply(log, base, "reply two")
            resp = await _fork(client, base, user.seq, new_session_id="child")
            return user, resp

    user, resp = run(scenario())
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_seq"] == user.seq
    assert app.state.log.get_session("child")["base_seq"] == user.seq


def test_fork_accepts_a_narrative_message_cut(build_app):
    """A narrative row is a message too (a canon/opening turn can be a cut)."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            _user(log, base, "one")
            reply = _reply(log, base, "reply one")
            return reply, await _fork(
                client, base, reply.seq, new_session_id="child"
            )

    reply, resp = run(scenario())
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_seq"] == reply.seq


# --------------------------------------------------------------------------
# Chain awareness: an INHERITED message seq is a valid cut of a branch
# --------------------------------------------------------------------------


def test_fork_cut_walks_the_chain_of_a_branch(build_app):
    """Forking a branch at an inherited message seq works (chain read).

    ``get_events`` would miss this cut -- the row belongs to the parent -- so
    the guard must use the fork-aware chain read (AGENTS.md "Choosing an
    event-log read").
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            user = _user(log, base, "one")
            _reply(log, base, "reply one")
            first = await _fork(client, base, user.seq, new_session_id="first")
            assert first.status_code == 200, first.text
            second = await _fork(client, "first", user.seq, new_session_id="second")
            return user, second

    user, second = run(scenario())
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["base_session"] == "first"
    assert body["base_seq"] == user.seq


# --------------------------------------------------------------------------
# The exemption: delete-tail keeps cutting at a non-message seq
# --------------------------------------------------------------------------


def test_delete_tail_still_cuts_at_a_non_message_seq(build_app):
    """Issue #74 guard is /fork ONLY -- delete-tail's cut is a state-delta row."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="DeleteTail")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            kept = _delta(log, base, {"time": "08:00", "loc": "camp"})
            pivot = _user(log, base, "two")
            _reply(log, base, "reply two")
            resp = await client.post(
                f"/api/sessions/{base}/delete-tail",
                json={"pivot_event_id": pivot.id},
            )
            return kept, pivot, resp

    kept, pivot, resp = run(scenario())
    assert resp.status_code == 200, resp.text
    # Cut = the largest seq below the pivot = the kept turn's state-delta -- a
    # NON-message row. The /fork guard must never reach delete-tail.
    assert resp.json()["base_seq"] == kept.seq
    assert resp.json()["base_seq"] < pivot.seq
