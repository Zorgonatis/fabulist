"""Issue #108 — a fork of a BRANCH keeps the persona, the actor and the name.

The reporter's line: "it also often loses chart Name/Player persona in the
created forks". The root cause is one rule applied one generation too
narrowly:

* ``_carry_config`` read the SOURCE's OWN rows, but a branch inherits its
  config row from an ancestor and owns none, so a generation-2 fork carried
  nothing; the new branch's chain window then truncated that ancestor at the
  new cut and the row fell out of the window. Every ``FORK_CONFIG_EVENTS`` key
  (``chat_persona``, ``chat_actor``, ``world_ref``, books, ...) lost the same
  way.
* the title carry read the source's own rows too, and a source that owns no
  title row (a pre-#86 branch) pinned ``""`` onto itself and every descendant.
* the client opened the new branch BEFORE refreshing the session list, so the
  header read a name it had not fetched yet ("New chat" until a reload).

The approved fix (Option B):

1. ``_carry_config`` folds the source's CHAIN latest-wins per whitelisted key
   and re-appends one row for each key whose winning row sits above the new
   cut. ``seq`` restarts per segment, so the "above the cut" test applies to
   the winning row's own ``seq`` — never a numeric filter over the chain.
2. the title carry reads the source's CHAIN at both call sites.
3. the LIST read treats an empty own title as absent on display and walks the
   chain; the rename revision TARGET stays the branch's own row (#86).
4. the client refreshes the session list before ``openSession`` on the fork
   and delete-tail paths.

The rail row, the chat-config view and the narrator identity block must agree
on every case — they share one fold.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.chat_config_api import _chat_config_view
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.sessions import _summarize
from fabulist.context.facts import persona_identity
from fabulist.log import EventLog
from fabulist.log.repo import is_state_snapshot_event
from fabulist.memory.actor import fold_actors
from fabulist.personas.registry import (
    append_persona,
    chat_persona_state,
    persona_for_chat,
)

PERSONA = {"persona_id": "p1", "name": "Wanderer", "description": "A traveller."}
ACTOR = "a1"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps over one tmp log (the factory mounts every router)."""
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


async def _create(client: httpx.AsyncClient, title: str = "A chat") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _user(log: EventLog, sid: str, text: str = "I enter the market."):
    return log.append_event(sid, "user", "user", {"text": text})


def _reply(log: EventLog, sid: str, prose: str = "Lanterns sway."):
    return log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": "", "segments": [{"kind": "prose", "text": prose}]},
    )


def _turn(log: EventLog, sid: str, text: str = "I enter the market.") -> int:
    """Append one user turn + reply; return the user event's ``seq`` (the cut)."""
    user = _user(log, sid, text)
    _reply(log, sid)
    return user.seq


def _own(log: EventLog, sid: str, kind: str) -> list:
    return [
        event
        for event in log.get_events(sid)
        if event.type == "system"
        and isinstance(event.payload, dict)
        and event.payload.get("event") == kind
    ]


def _titles(app) -> dict[str, str]:
    async def _list():
        async with client_for(app) as client:
            body = (await client.get("/api/sessions")).json()
            return {row["session_id"]: row["title"] for row in body["sessions"]}

    return run(_list())


def _delete_tail(app, sid: str, pivot_id: int):
    async def _call():
        async with client_for(app) as client:
            return await client.post(
                f"/api/sessions/{sid}/delete-tail", json={"pivot_event_id": pivot_id}
            )

    return run(_call())


def _fork(app, sid: str, at_seq: int):
    async def _call():
        async with client_for(app) as client:
            return await client.post(
                f"/api/sessions/{sid}/fork", json={"at_seq": at_seq}
            )

    return run(_call())


def _assert_every_reader_agrees(log: EventLog, sid: str) -> None:
    """One fold, three surfaces: the rail row, the config view, the narrator."""
    summary = _summarize(log, sid)
    assert summary is not None
    config = _chat_config_view(log, sid)
    assert summary["persona_id"] == "p1"
    assert config["persona_id"] == "p1"
    assert persona_for_chat(log, sid) == "p1"
    assert chat_persona_state(log, sid) == (True, "p1")
    assert persona_identity(log, sid) == ("Wanderer", "A traveller.")


# --------------------------------------------------------------------------
# (1) generation 2 — the persona row lives in an ancestor, above the new cut
# --------------------------------------------------------------------------


def _harbor(log: EventLog) -> tuple[str, str]:
    """The RCA's ``root_harbor`` shape: persona at seq 6, one fork at 7.

    ``U1`` inherits the persona (6 <= its cut); it owns no persona row. A cut
    at 4 on ``U1`` therefore sits BELOW the row and belongs to no segment the
    new branch may read — exactly the four conditions from the RCA.
    """
    root = "root_harbor"
    log.append_event(root, "system", "user", {"event": "session_created", "title": "Harbor"})
    _turn(log, root, "one")
    _turn(log, root, "two")
    log.append_event(root, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    log.append_event(root, "system", "user", {"event": "chat_actor", "actor_ids": [ACTOR]})
    _turn(log, root, "three")
    branch = "U1"
    log.fork_session(root, 7, branch)
    assert persona_for_chat(log, branch) == "p1"  # inherited, not owned
    assert _own(log, branch, "chat_persona") == []
    return root, branch


def test_generation_two_fork_keeps_persona_and_actor(log: EventLog):
    append_persona(log, dict(PERSONA))
    _harbor(log)

    log.fork_session("U1", 4, "U3")

    # WRITE: the branch now OWNS the effective row, so no later cut can drop it.
    carried = _own(log, "U3", "chat_persona")
    assert [row.payload["persona_id"] for row in carried] == ["p1"]
    assert [row.payload["actor_ids"] for row in _own(log, "U3", "chat_actor")] == [
        [ACTOR]
    ]
    _assert_every_reader_agrees(log, "U3")
    assert fold_actors(log, "U3") == (ACTOR,)


def test_generation_two_delete_tail_keeps_persona_and_actor(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    append_persona(log, dict(PERSONA))
    root, branch = _harbor(log)

    pivot = next(
        event for event in log.get_events(root) if event.seq == 4 and event.type == "user"
    )
    resp = _delete_tail(app, branch, pivot.id)
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["session_id"]

    assert [row.payload["persona_id"] for row in _own(log, new_id, "chat_persona")] == [
        "p1"
    ]
    _assert_every_reader_agrees(log, new_id)
    assert fold_actors(log, new_id) == (ACTOR,)


def test_branch_local_row_wins_even_though_its_seq_is_lower(log: EventLog):
    """``seq`` restarts per segment — a numeric cross-segment filter is wrong.

    ``U1`` writes its own persona at a seq BELOW the ancestor's row. The chain
    fold (root-first, latest per segment) makes the branch-local ``p2`` the
    winner, and its own ``seq`` (8) is above the new cut, so it is carried.
    A filter that compared the ancestor's ``seq`` 6 would have carried ``p1``.
    """
    append_persona(log, dict(PERSONA))
    append_persona(log, {"persona_id": "p2", "name": "Bob"})
    _harbor(log)
    log.append_event("U1", "system", "user", {"event": "chat_persona", "persona_id": "p2"})

    log.fork_session("U1", 4, "U3")

    assert [row.payload["persona_id"] for row in _own(log, "U3", "chat_persona")] == [
        "p2"
    ]
    assert persona_for_chat(log, "U3") == "p2"


# --------------------------------------------------------------------------
# (2) the title heal — a source that owns no title row must not pin ""
# --------------------------------------------------------------------------


def test_pointer_only_branch_heals_and_forks_no_longer_pin_empty(build_app):
    """The pre-#86 shape: a fork that wrote no title row.

    ``legacy_fork`` (a direct ``fork_session``, the v0.0.6 shape) owns no title
    row. Before the fix it listed ``''`` and every fork pinned ``''`` forever.
    Now it DISPLAYS the ancestor's name (no log write) and a new fork carries
    that name instead of an empty pin.
    """
    build, _ = build_app
    app = build()
    log = app.state.log
    append_persona(log, dict(PERSONA))
    log.append_event("root_old", "system", "user", {"event": "session_created", "title": "Old Chat"})
    log.append_event("root_old", "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    _turn(log, "root_old")
    log.fork_session("root_old", 3, "legacy_fork")
    # The pre-#86 shape, asserted: a snapshot and no title row of its own.
    assert _own(log, "legacy_fork", "session_renamed") == []

    # READ heal: nothing was written to the log, the name comes from the chain.
    assert _summarize(log, "legacy_fork")["title"] == "Old Chat"

    first = _fork(app, "legacy_fork", 3)
    assert first.status_code == 200, first.text
    a3 = first.json()["session_id"]
    second = _fork(app, a3, 3)
    assert second.status_code == 200, second.text
    a3b = second.json()["session_id"]

    # WRITE heal: the carry reads the chain, so the pin is the real name.
    assert [row.payload["title"] for row in _own(log, a3, "session_renamed")] == [
        "Old Chat"
    ]
    assert [row.payload["title"] for row in _own(log, a3b, "session_renamed")] == [
        "Old Chat"
    ]
    titles = _titles(app)
    assert titles["legacy_fork"] == "Old Chat"
    assert titles[a3] == "Old Chat"
    assert titles[a3b] == "Old Chat"


def test_blank_own_title_falls_back_to_the_chain_on_display(build_app):
    """An already-pinned ``''`` (today's delete-tail of an untitled chat) heals.

    A branch whose own title row is ``''`` reads the ancestor's name again at
    display time. The rename TARGET stays the branch's own row (#86), so a
    rename still supersedes the branch's carry and never the parent's row.
    The branch can also still be renamed — a deliberate blank cannot exist,
    ``rename_session`` rejects one with 422.
    """
    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event("root_named", "system", "user", {"event": "session_created", "title": "Harbor"})
    _turn(log, "root_named")
    log.fork_session("root_named", 1, "pinned_blank")
    # An empty pin of the old shape: exactly what the pre-fix fork handler wrote.
    log.append_event(
        "pinned_blank", "system", "system", {"event": "session_renamed", "title": ""}
    )

    summary = _summarize(log, "pinned_blank")
    assert summary["title"] == "Harbor"  # displayed from the chain
    assert summary["_title_event"].session_id == "pinned_blank"  # rename target

    async def _rename_and_list():
        async with client_for(app) as client:
            renamed = await client.patch(
                "/api/sessions/pinned_blank", json={"title": "My Branch"}
            )
            empty = await client.patch("/api/sessions/pinned_blank", json={"title": "  "})
            listed = (await client.get("/api/sessions")).json()
            return renamed, empty, listed

    renamed, empty, listed = run(_rename_and_list())
    assert renamed.status_code == 200, renamed.text
    assert empty.status_code == 422
    titles = {row["session_id"]: row["title"] for row in listed["sessions"]}
    assert titles["pinned_blank"] == "My Branch"
    assert titles["root_named"] == "Harbor"  # the parent's row is untouched


# --------------------------------------------------------------------------
# (3) the cases that already worked must keep working
# --------------------------------------------------------------------------


def test_fork_of_a_root_keeps_the_persona(log: EventLog):
    append_persona(log, dict(PERSONA))
    log.append_event("tavern", "system", "user", {"event": "session_created", "title": "Tavern Night"})
    log.append_event("tavern", "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    cut = _turn(log, "tavern")

    log.fork_session("tavern", cut, "A1")

    _assert_every_reader_agrees(log, "A1")


def test_generation_one_cut_below_the_persona_row_keeps_it(log: EventLog):
    """The persona row sits ABOVE the cut, but the SOURCE owns it - carry it."""
    append_persona(log, dict(PERSONA))
    log.append_event("lighthouse", "system", "user", {"event": "session_created", "title": "Lighthouse"})
    _turn(log, "lighthouse")
    log.append_event("lighthouse", "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    _turn(log, "lighthouse")

    log.fork_session("lighthouse", 2, "A2")

    assert [row.payload["persona_id"] for row in _own(log, "A2", "chat_persona")] == [
        "p1"
    ]
    _assert_every_reader_agrees(log, "A2")


def test_inherited_config_is_not_duplicated(log: EventLog):
    """A winning row at or below the cut arrives through the chain: no copy."""
    log.append_event("base", "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    cut = _turn(log, "base")

    log.fork_session("base", cut, "branch")

    assert [
        event
        for event in log.get_events("branch")
        if not is_state_snapshot_event(event)
    ] == []
    assert persona_for_chat(log, "branch") == "p1"


def test_carry_writes_one_row_per_key_not_every_post_cut_write(log: EventLog):
    """The fold re-appends the WINNER per key, not the raw row history."""
    log.append_event("base", "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    cut = _turn(log, "base")
    log.append_event("base", "system", "user", {"event": "chat_persona", "persona_id": "p2"})
    log.append_event("base", "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    log.append_event("base", "system", "user", {"event": "world_ref", "world_id": "w1"})

    log.fork_session("base", cut, "branch")

    assert len(_own(log, "branch", "chat_persona")) == 1
    assert [row.payload["persona_id"] for row in _own(log, "branch", "chat_persona")] == [
        "p1"
    ]
    assert len(_own(log, "branch", "world_ref")) == 1


# --------------------------------------------------------------------------
# (4) the client: list first, then open — on BOTH new-branch paths
# --------------------------------------------------------------------------


def test_client_refreshes_the_list_before_opening_the_new_branch():
    """Issue #108 (root cause 3): ``openSession`` reads the title from the
    store, so the fork and delete-tail paths must refresh the list FIRST, as
    the compaction path already documents and does."""
    page = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "src"
        / "routes"
        / "chat"
        / "+page.svelte"
    ).read_text(encoding="utf-8")

    def body(name: str) -> str:
        start = page.index(f"async function {name}(")
        end = page.index("\n\t}", start)
        return page[start:end]

    for name in ("deleteFrom", "retry"):
        code = body(name)
        assert "refreshSessions()" in code and "openSession(" in code, name
        assert code.index("refreshSessions()") < code.index("openSession("), name
