"""Issue #86: a branch carries its config AND reads it through the chain.

Two distinct gaps, pinned together because they share one symptom ("the fork
lost my books/persona"):

* WRITE -- ``_carry_config`` re-appends only whitelisted kinds above the cut
  (:data:`fabulist.log.repo.FORK_CONFIG_EVENTS`). ``session_books``, the legacy
  ``world_info`` import and ``set_swapped`` were not whitelisted, so an attach or
  a component-set swap made after the last message never reached a regenerate
  branch. They are CONFIG (selections over a catalogue, the class ``world_ref``
  is in), so they now join the carry.
* READ -- the summary/config/drawer/opening readers used ``get_events`` (own
  rows) for session-CONSTANT context, so a pre-cut persona/world/book read as
  unset on a branch while the engine (chain-aware) used the true value. Those
  readers now walk the chain, exactly as the narrator already did.

Deliberately NOT changed (recorded rulings, from :mod:`fabulist.log.repo`):
``session_module``, ``greeting_selected``, ``cast_snapshot`` and
``chat_agent_debug`` stay out of the carry, and ``chat_agent_debug``'s fold stays
own-row.
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
from fabulist.context.facts import _entity_book_ids, session_world_info_block
from fabulist.dsl.sets import session_set_id
from fabulist.log import EventLog
from fabulist.log.repo import FORK_CONFIG_EVENTS
from fabulist.personas.registry import (
    PERSONAS_ANCHOR,
    append_persona,
    chat_persona_state,
)
from fabulist.state.bts import user_as_actor
from fabulist.worlds.registry import append_world, create_world_chat

ENTRIES = [
    {
        "keys": ["lantern", "night market"],
        "content": "The night market opens only after the second bell.",
        "insertion_order": 1,
    }
]


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


def _turn(log: EventLog, sid: str, text: str = "I enter the market.") -> int:
    """Append one user turn + reply; return the user event's ``seq`` (the cut)."""
    user = log.append_event(sid, "user", "user", {"text": text})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "Lanterns sway.", "segments": []}
    )
    return user.seq


def _own(log: EventLog, sid: str, kind: str) -> list:
    return [
        event
        for event in log.get_events(sid)
        if event.type == "system" and event.payload.get("event") == kind
    ]


# --- the whitelist classification (owner ruling 2026-10-26) -----------------


def test_fork_carry_whitelist_classifies_config_not_content():
    """Pins the membership ruling: selections are carried, content is not."""
    # CONFIG (issue #86 adds the three book/set rows).
    assert {
        "session_books",
        "world_info",
        "set_swapped",
        "world_ref",
        "chat_persona",
        "chat_memory",
    } <= FORK_CONFIG_EVENTS
    # Recorded exclusions stay excluded.
    assert FORK_CONFIG_EVENTS.isdisjoint(
        {"session_module", "session_module_deleted", "greeting_selected",
         "cast_snapshot", "chat_agent_debug"}
    )


# --- (a) a book attached ABOVE the cut survives and renders -----------------


def test_book_attached_above_the_cut_survives_and_renders_on_the_branch(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Night Market")
            log = app.state.log
            cut = _turn(log, base)
            book = (
                await client.post(
                    "/api/books", json={"name": "Market lore", "entries": ENTRIES}
                )
            ).json()["book"]
            # The user attaches the book AFTER the last message, then rerolls.
            attach = await client.post(
                f"/api/sessions/{base}/books", json={"book_ids": [book["book_id"]]}
            )
            assert attach.status_code == 200, attach.text
            fork = await client.post(
                f"/api/sessions/{base}/fork", json={"at_seq": cut}
            )
            assert fork.status_code == 200, fork.text
            branch = fork.json()["session_id"]
            world = await client.get(f"/api/sessions/{branch}/world")
            return base, branch, book["book_id"], world

    base, branch, book_id, world = run(scenario())

    log = EventLog(log_path)
    try:
        # WRITE gap: the attach row was above the cut, so it must be CARRIED --
        # own rows are the only place it can live on the branch.
        assert [row.payload["book_ids"] for row in _own(log, branch, "session_books")] == [
            [book_id]
        ]
        # ...and it resolves through the chain like any attached book.
        chain = log.get_chain_events(branch)
        assert book_id in _entity_book_ids(log, branch, chain)
        # The narrator's render sees the lore on the branch.
        block = session_world_info_block(log, branch, "I raise the lantern.")
        assert block is not None and "night market opens" in block
    finally:
        log.close()

    # The drawer read (chain-aware) agrees with the render.
    assert world.status_code == 200, world.text
    assert [book["book_id"] for book in world.json()["books"]] == [book_id]


def test_legacy_world_info_import_above_the_cut_survives(log: EventLog):
    parent, branch = "base", "branch"
    cut = _turn(log, parent)
    log.append_event(
        parent,
        "system",
        "user",
        {
            "event": "world_info",
            "book": {"entries": ENTRIES},
            "entry_count": len(ENTRIES),
        },
    )

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    assert len(_own(log, branch, "world_info")) == 1
    assert branch


# --- (b) a pre-cut persona reads set from the summary and the config view ---


def test_pre_cut_persona_reads_set_on_the_branch(log: EventLog):
    append_persona(log, {"persona_id": "p1", "name": "Wanderer"})
    parent, branch = "base", "branch"
    log.append_event(parent, "system", "user", {"event": "session_created", "title": "T"})
    # ASSIGNED BELOW the cut: it reaches the branch only through the chain.
    log.append_event(
        parent, "system", "user", {"event": "chat_persona", "persona_id": "p1"}
    )
    cut = _turn(log, parent)

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    summary = _summarize(log, branch)
    assert summary is not None
    assert summary["persona_id"] == "p1"
    assert summary["persona_cleared"] is False
    assert _chat_config_view(log, branch)["persona_id"] == "p1"
    # The badges agree with the ENGINE's own read of the same question.
    assert chat_persona_state(log, branch) == (True, "p1")


def test_pre_cut_world_and_memory_read_set_on_the_branch(log: EventLog):
    parent, branch = "base", "branch"
    log.append_event(parent, "system", "user", {"event": "session_created", "title": "T"})
    log.append_event(
        parent, "system", "system", {"event": "world_ref", "world_id": "w1", "name": "Rain"}
    )
    log.append_event(parent, "system", "user", {"event": "chat_memory", "enabled": False})
    cut = _turn(log, parent)

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    summary = _summarize(log, branch)
    assert summary is not None
    assert summary["world_id"] == "w1"
    assert summary["memory_enabled"] is False
    assert _chat_config_view(log, branch)["memory_enabled"] is False


# --- (c) a component-set swap above the cut survives ------------------------


def test_set_swapped_above_the_cut_survives_the_fork(log: EventLog):
    parent, branch = "base", "branch"
    log.append_event(parent, "system", "user", {"event": "session_created", "title": "T"})
    cut = _turn(log, parent)
    log.append_event(
        parent,
        "system",
        "user",
        {"event": "set_swapped", "set_id": "waves@3", "name": "Waves", "version": 3},
    )

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    assert [row.payload["set_id"] for row in _own(log, branch, "set_swapped")] == [
        "waves@3"
    ]
    assert session_set_id(log, branch) == "waves@3"


# --- (d) user_as_actor keeps its value on a branch --------------------------


def test_user_as_actor_true_on_the_source_stays_true_on_the_branch(log: EventLog):
    parent, branch = "base", "branch"
    log.append_event(parent, "system", "user", {"event": "session_created", "title": "T"})
    # BELOW the cut: the reported defect was the READ, so this row is inherited
    # rather than carried, and the BTS seam must still see it.
    log.append_event(
        parent, "system", "user", {"event": "chat_user_as_actor", "user_as_actor": True}
    )
    cut = _turn(log, parent)

    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    assert user_as_actor(log, parent) is True
    assert user_as_actor(log, branch) is True  # was False before the fix
    assert _chat_config_view(log, branch)["user_as_actor"] is True


def test_branch_local_user_as_actor_write_beats_the_inherited_value(log: EventLog):
    parent, branch = "base", "branch"
    log.append_event(parent, "system", "user", {"event": "session_created", "title": "T"})
    log.append_event(
        parent, "system", "user", {"event": "chat_user_as_actor", "user_as_actor": True}
    )
    cut = _turn(log, parent)
    log.fork_session(parent, at_seq=cut, new_session_id=branch)

    log.append_event(
        branch, "system", "user", {"event": "chat_user_as_actor", "user_as_actor": False}
    )

    assert user_as_actor(log, branch) is False
    assert user_as_actor(log, parent) is True  # the parent is untouched


# --- (e) a branch rename supersedes the branch's own title carry ------------


def test_branch_rename_supersedes_its_own_title_carry(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Night Market")
            log = app.state.log
            cut = _turn(log, base)
            fork = await client.post(f"/api/sessions/{base}/fork", json={"at_seq": cut})
            assert fork.status_code == 200, fork.text
            branch = fork.json()["session_id"]
            renamed = await client.patch(
                f"/api/sessions/{branch}", json={"title": "Branch of the Market"}
            )
            assert renamed.status_code == 200, renamed.text
            listed = await client.get("/api/sessions")
            return base, branch, listed

    base, branch, listed = run(scenario())
    log = EventLog(log_path)
    try:
        carries = _own(log, branch, "session_renamed")
        # /fork wrote the branch-local title carry (issue #86).
        assert [row.payload["title"] for row in carries] == ["Night Market"]
        carry = carries[0]
        assert carry.session_id == branch

        # The rename supersedes the BRANCH's carry, never the parent's row.
        revisions = [e for e in log.get_events(branch) if e.type == "revision"]
        assert len(revisions) == 1
        assert revisions[0].supersedes == carry.id
        parent_title_rows = _own(log, base, "session_created") + _own(
            log, base, "session_renamed"
        )
        assert all(row.session_id == base for row in parent_title_rows)
        assert [
            e for e in log.get_events(base) if e.supersedes in {r.id for r in parent_title_rows}
        ] == []
    finally:
        log.close()

    titles = {row["session_id"]: row["title"] for row in listed.json()["sessions"]}
    assert titles[branch] == "Branch of the Market"
    assert titles[base] == "Night Market"


# --- the opening cycle and the drawer on a world-chat branch ----------------


def test_world_chat_branch_reads_inherited_books_and_opening(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            book = (
                await client.post(
                    "/api/books", json={"name": "Market lore", "entries": ENTRIES}
                )
            ).json()["book"]
            log = app.state.log
            world = append_world(
                log,
                {
                    "name": "Rain-World",
                    "first_mes": "Rain on the awning.",
                    "alternate_greetings": ["The market is flooding."],
                    "book_ids": [book["book_id"]],
                },
            )
            chat = create_world_chat(log, world, title="Rain-World")
            # The world chat's opening/swipe rows sit BELOW the cut.
            swipe = await client.post(
                f"/api/sessions/{chat['session_id']}/greeting", json={"index": 1}
            )
            assert swipe.status_code == 200, swipe.text
            cut = _turn(log, chat["session_id"])
            fork = await client.post(
                f"/api/sessions/{chat['session_id']}/fork", json={"at_seq": cut}
            )
            assert fork.status_code == 200, fork.text
            branch = fork.json()["session_id"]
            opening = await client.get(f"/api/sessions/{branch}/opening")
            session_world = await client.get(f"/api/sessions/{branch}/world")
            return chat["session_id"], branch, book["book_id"], opening, session_world

    source, branch, book_id, opening, session_world = run(scenario())

    body = opening.json()
    # The inherited world opening (own rows on a branch hold none of it).
    assert body["greetings"] == ["Rain on the awning.", "The market is flooding."]
    # The inherited swipe index is the one in force at the cut.
    assert body["index"] == 1
    assert body["card"]["name"] == "Rain-World"
    assert [book["book_id"] for book in session_world.json()["books"]] == [book_id]

    # A branch swipe validates against the INHERITED cycle, not an empty one.
    async def sweep_past_the_end():
        async with client_for(app) as client:
            return await client.post(
                f"/api/sessions/{branch}/greeting", json={"index": 2}
            )

    assert run(sweep_past_the_end()).status_code == 422
    assert source != branch
