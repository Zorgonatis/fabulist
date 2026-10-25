"""``POST /api/sessions/{id}/delete-tail`` — message deletion as a fork (#35).

"Delete messages N..end" is a branch-pointer move, not a rewrite (ADR-001): the
endpoint forks the session just below the selected message and returns the new
branch id, leaving the tail's rows untouched on the old line (which stays
reachable through the existing swipe switcher). These tests pin the contract
and the two load-bearing rules:

* the CUT is the largest chain seq strictly below the pivot, so the kept turn's
  post-turn writes (``state-delta``) survive and the deleted turns' state rows
  leave with them -- reading at the previous MESSAGE would silently rewind
  tracked state one turn;
* the pivot is found on the fork-AWARE chain (``get_chain_events``), and the new
  branch inherits the source's visible prefix. That last one is the regression
  case: forking a branch at a cut BELOW the branch's own ``base_seq`` must not
  leak ancestor rows above the tighter cut -- ``_session_chain`` propagates the
  tightest (minimum) bound down the chain, not just each child's stored
  ``base_seq``.

Fixture pattern mirrors ``tests/test_branches_kind.py``: a real tmp event log
under a ``create_app(FakeTurnEngine(), ...)`` app, driven through
``httpx.ASGITransport``. The branches router is already mounted by
``create_app`` (no duplicate include here -- a second registration would shadow
the first route).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.cards.v2 import IMPORT_SEED_PREFIX
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
    return log.append_event(
        sid, "user", "user", {"text": text}, ephemeral=ephemeral
    )


def _reply(log: EventLog, sid: str, prose: str):
    return log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": "", "segments": [{"kind": "prose", "text": prose}]},
    )


def _delta(log: EventLog, sid: str, payload: dict):
    return log.append_event(sid, "state-delta", "extractor", payload)


def _roll(log: EventLog, sid: str):
    return log.append_event(sid, "roll", "clock", {"dice": "1d20", "result": 12})


async def _delete_tail(
    client: httpx.AsyncClient,
    sid: str,
    pivot_id: int,
    *,
    new_session_id: str | None = None,
) -> httpx.Response:
    body: dict = {"pivot_event_id": pivot_id}
    if new_session_id is not None:
        body["new_session_id"] = new_session_id
    return await client.post(f"/api/sessions/{sid}/delete-tail", json=body)


def _texts(projection: dict) -> tuple[list[str], list[str]]:
    return (
        [item["payload"]["text"] for item in projection["user_messages"]],
        [item["payload"]["prose"] for item in projection["narratives"]],
    )


# --------------------------------------------------------------------------
# Happy path: the fork lands just below the pivot's turn
# --------------------------------------------------------------------------


def test_delete_tail_forks_below_pivot_and_returns_the_new_branch(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Market")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            pivot = _user(log, base, "two")
            _reply(log, base, "reply two")

            resp = await _delete_tail(client, base, pivot.id)
            listed = await client.get(f"/api/sessions/{base}/branches")
            return base, pivot, resp, listed

    base, pivot, resp, listed = run(scenario())
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    new_id = payload["session_id"]
    assert new_id != base
    assert payload["base_session"] == base
    # Cut = the largest chain seq below the pivot => the kept reply survives.
    assert payload["base_seq"] == pivot.seq - 1
    assert payload["deleted_count"] == 2  # pivot + its reply

    branch = app.state.log.get_session(new_id)
    assert branch["base_session"] == base
    assert branch["base_seq"] == pivot.seq - 1

    # The new line shows everything before the pivot and nothing after it.
    assert _texts(app.state.log.get_projection(new_id)) == (
        ["one"],
        ["reply one"],
    )
    # The old line is intact and unchanged (append-only; nothing was rewritten).
    assert _texts(app.state.log.get_projection(base)) == (
        ["one", "two"],
        ["reply one", "reply two"],
    )

    # A plain ``kind:"user"`` child: it sits in the swipe group of the old line.
    entries = listed.json()["branches"]
    assert [entry["session_id"] for entry in entries] == [new_id]
    assert entries[0]["kind"] == "user"
    assert entries[0]["base_seq"] == payload["base_seq"]


def test_delete_tail_cut_keeps_previous_turn_state_and_drops_deleted_deltas(
    build_app,
):
    """The cut is the last row of the kept turn, so tracked state does not rewind."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="State")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            kept_delta = _delta(log, base, {"time": "08:00", "loc": "camp"})
            pivot = _user(log, base, "two")
            _reply(log, base, "reply two")
            _delta(log, base, {"time": "09:00", "weather": "rain"})

            resp = await _delete_tail(client, base, pivot.id)
            return base, kept_delta, resp

    base, kept_delta, resp = run(scenario())
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    # Cutting AT the previous message (seq 3 here) would drop the kept turn's
    # state row; the fork must land ON it.
    assert payload["base_seq"] == kept_delta.seq

    projection = app.state.log.get_projection(payload["session_id"])
    assert _texts(projection) == (["one"], ["reply one"])
    # Kept turn's delta survives; the deleted turn's delta left with the tail.
    assert projection["state"] == {"time": "08:00", "loc": "camp"}


def test_delete_tail_narrative_pivot_keeps_its_user_turn_and_rolls(build_app):
    """A narrative pivot drops from there; its user message + roll chips stay."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Rolls")
            log = app.state.log
            _user(log, base, "one")
            roll = _roll(log, base)
            pivot = _reply(log, base, "reply one")
            _user(log, base, "two")
            _reply(log, base, "reply two")

            resp = await _delete_tail(client, base, pivot.id)
            return base, roll, resp

    base, roll, resp = run(scenario())
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["base_seq"] == roll.seq
    # Three MESSAGES leave (the pivot narrative + the whole next turn); the roll
    # is not a message, so it does not inflate ``deleted_count``.
    assert payload["deleted_count"] == 3

    projection = app.state.log.get_projection(payload["session_id"])
    assert _texts(projection) == (["one"], [])
    assert len(projection["rolls"]) == 1


# --------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------


def test_delete_tail_rejects_the_first_message(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="First")
            log = app.state.log
            first = _user(log, base, "one")
            _reply(log, base, "reply one")
            return base, first, await _delete_tail(client, base, first.id)

    base, _first, resp = run(scenario())
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"reason": "first_message"}
    # Nothing was forked: "delete everything" is impossible by construction.
    assert app.state.log.list_branches(base) == []


def test_delete_tail_rejects_non_message_pivots(build_app):
    """System rows, ephemeral rows and legacy import seeds are not messages."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="NotMessages")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            system_row = log.get_events(base)[0]  # session_created
            ephemeral = _user(log, base, "thinking", ephemeral=True)
            seed = log.append_event(
                base, "user", "system", {"text": f"{IMPORT_SEED_PREFIX} card"}
            )
            return base, [
                (await _delete_tail(client, base, event.id)).json()
                for event in (system_row, ephemeral, seed)
            ]

    _base, bodies = run(scenario())
    assert bodies == [
        {"detail": {"reason": "not_a_message"}},
        {"detail": {"reason": "not_a_message"}},
        {"detail": {"reason": "not_a_message"}},
    ]


def test_delete_tail_rejects_unknown_and_foreign_pivots(build_app):
    """A pivot must be a row on THIS session's chain (not another session's)."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            other = await _create(client, title="Other")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            foreign = _user(log, other, "elsewhere")
            return base, [
                await _delete_tail(client, base, 999_999),
                await _delete_tail(client, base, foreign.id),
            ]

    base, (unknown, foreign) = run(scenario())
    assert unknown.status_code == 409
    assert unknown.json()["detail"] == {"reason": "pivot_missing"}
    assert foreign.status_code == 409
    assert foreign.json()["detail"] == {"reason": "pivot_missing"}
    assert app.state.log.list_branches(base) == []


def test_delete_tail_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await _delete_tail(client, "nope", 1)

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


# --------------------------------------------------------------------------
# The old line stays reachable; the new one carries the title
# --------------------------------------------------------------------------


def test_delete_tail_old_line_stays_walkable_and_swipeable(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            pivot = _user(log, base, "two")
            _reply(log, base, "reply two")

            new_id = (await _delete_tail(client, base, pivot.id)).json()["session_id"]
            new_side = await client.get(f"/api/sessions/{new_id}/branches")
            return base, new_id, new_side

    base, new_id, new_side = run(scenario())
    log = app.state.log
    # The deleted tail is still in the append-only log on the old line.
    assert [event.type for event in log.get_chain_events(base)] == [
        "system",
        "user",
        "narrative",
        "user",
        "narrative",
    ]
    assert log.get_projection(base)["narratives"]

    # The switcher offers the old line as the swipe-group parent (position 0).
    body = new_side.json()
    assert body["parent"]["session_id"] == base
    assert [s["session_id"] for s in body["siblings"]] == [base, new_id]
    assert body["siblings"][0]["is_self"] is False
    assert body["siblings"][1]["is_self"] is True


def test_delete_tail_carries_the_title_onto_the_new_branch(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Night Market")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            pivot = _user(log, base, "two")

            new_id = (await _delete_tail(client, base, pivot.id)).json()["session_id"]
            listed = await client.get("/api/sessions")
            return new_id, listed

    new_id, listed = run(scenario())
    own = app.state.log.get_events(new_id)
    # Exactly one carry row: a title rename, never a compaction marker (a marker
    # would drop the new branch out of the swipe group).
    assert [event.type for event in own] == ["system"]
    assert own[0].payload == {"event": "session_renamed", "title": "Night Market"}

    # The rail can render the line the reader just switched to.
    entries = {row["session_id"]: row for row in listed.json()["sessions"]}
    assert entries[new_id]["title"] == "Night Market"


def test_delete_tail_untitled_source_still_lists_the_new_branch(build_app):
    """The carry row is UNCONDITIONAL: an untitled chat must stay in the rail.

    ``GET /api/sessions`` summarizes a session's OWN rows and drops an id with
    none (``_summarize`` returns ``None``), so skipping the title carry on an
    untitled source -- the DEFAULT new-chat path -- forked a branch with zero
    own events and the line the reader just switched to vanished from the rail
    until its next turn. Compaction's successor is never in that state (its era
    marker is an own row); a delete-tail branch has no marker by design, so the
    carry row is its only anchor and must be appended with ``title: \"\"``.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client)  # title omitted: default new chat
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            pivot = _user(log, base, "two")

            resp = await _delete_tail(client, base, pivot.id)
            listed = await client.get("/api/sessions")
            branches = await client.get(f"/api/sessions/{base}/branches")
            return base, resp, listed, branches

    base, resp, listed, branches = run(scenario())
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["session_id"]

    # Exactly one own row: the empty-title carry. No marker, so the child stays
    # a plain ``kind:"user"`` swipe alternate of the line it replaced.
    own = app.state.log.get_events(new_id)
    assert len(own) == 1
    assert own[0].type == "system"
    assert own[0].payload == {"event": "session_renamed", "title": ""}
    entries_b = branches.json()["branches"]
    assert [entry["session_id"] for entry in entries_b] == [new_id]
    assert entries_b[0]["kind"] == "user"

    # ... and it is listed IMMEDIATELY, with the empty title an untitled chat
    # already renders (the rail's "Untitled chat" fallback), zero turns.
    entries = {row["session_id"]: row for row in listed.json()["sessions"]}
    assert new_id in entries
    assert entries[new_id]["title"] == ""
    assert entries[new_id]["turns"] == 0


# --------------------------------------------------------------------------
# The defect: delete from a BRANCH session (inherited pivot)
# --------------------------------------------------------------------------


def test_delete_tail_from_branch_session_with_inherited_pivot(build_app):
    """Forking a branch below its own ``base_seq`` must not leak ancestor rows.

    ``base`` forks at seq 5 (``branch``), then the reader deletes from
    ``branch`` with an INHERITED pivot ("two", base's seq 4). The fork cut is 3,
    which is BELOW ``branch``'s stored ``base_seq`` (5). A chain that bounds the
    root by its child's ``base_seq`` alone would still expose base rows 4..5 --
    i.e. show the "deleted" messages. The effective bound is the tightest cut
    any descendant makes (min propagation in ``EventLog._session_chain``).
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            _user(log, base, "one")
            _reply(log, base, "reply one")
            inherited_pivot = _user(log, base, "two")
            _reply(log, base, "reply two")

            branch = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": reply_two_seq(log, base), "new_session_id": "b1"},
                )
            ).json()["session_id"]
            assert log.get_session(branch)["base_seq"] == inherited_pivot.seq + 1

            resp = await _delete_tail(client, branch, inherited_pivot.id)
            return base, branch, inherited_pivot, resp

    base, branch, inherited_pivot, resp = run(scenario())
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    new_id = payload["session_id"]
    assert payload["base_session"] == branch
    assert payload["base_seq"] == inherited_pivot.seq - 1
    assert payload["deleted_count"] == 2

    log = app.state.log
    # The grandchild's chain ends at the tighter cut: no base row above it. The
    # trailing ``system`` row is the new branch's own title carry -- this
    # intermediate fork owns no title anchor, so the carry lands with "".
    assert [event.type for event in log.get_chain_events(new_id)] == [
        "system",
        "user",
        "narrative",
        "system",
    ]
    assert _texts(log.get_projection(new_id)) == (["one"], ["reply one"])

    # The intermediate branch keeps its own (fuller) view: only the new branch
    # was tightened.
    assert _texts(log.get_projection(branch)) == (
        ["one", "two"],
        ["reply one", "reply two"],
    )


def reply_two_seq(log: EventLog, base: str) -> int:
    """Seq of ``base``'s last row (the fork cut that includes both turns)."""
    return log.get_events(base)[-1].seq
