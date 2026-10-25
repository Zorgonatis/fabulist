"""PART 1/2: an explicit persona clear means NO PERSONA (not re-inherit the pin).

Owner ruling: "clear = no persona". Before this, clearing a chat while a global
default was pinned re-inherited the pin, so a local/sandbox chat was impossible.

The clear is recorded as a ``chat_persona`` row carrying ``persona_id: null``
**plus** ``{"cleared": true}``:

* same event **kind** (``chat_persona``) so a fork's ``FORK_CONFIG_EVENTS``
  carry keeps the clear on the branch, and
* same **null value** so every existing null-aware reader (chat-config, memory
  scope, the session-list fold) stays byte-identical.

The resolution folds (:func:`persona_for_chat`, the ``{{user}}``/``{{persona}}``
resolver) treat the marker as authoritative: explicitly none, no pin fallback —
while a chat that *never* recorded a choice still inherits the pin.

PART 2: when no persona is in effect, ``{{user}}`` resolves to the literal word
``user`` (not an empty string and not the raw token), in card/cast/WI text and
in preset/module surfaces.

PART 3: the pin fold moved to the neutral :mod:`fabulist.state.settings`; the
API keeps its route + payload shape and re-exports the names.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.settings import DEFAULT_PERSONA_EVENT, SETTINGS_SESSION_ID
from fabulist.context.facts import _render_value, session_world_info_block
from fabulist.context.roster import _render
from fabulist.log import EventLog
from fabulist.memory.scope import collection_for, resolve_scope
from fabulist.personas.registry import (
    CHAT_PERSONA_CLEARED_KEY,
    CHAT_PERSONA_EVENT,
    assigned_persona_id,
    chat_config_for,
    chat_persona_state,
    clear_persona,
    persona_for_chat,
)
from fabulist.prompts.variables import resolve_variables, render_template

PERSONAS_ANCHOR = "__personas__"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
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


async def _create_persona(client: httpx.AsyncClient, name: str) -> str:
    resp = await client.post("/api/personas", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["persona"]["persona_id"]


async def _pin(client: httpx.AsyncClient, persona_id: str | None):
    return await client.patch("/api/settings", json={"default_persona_id": persona_id})


def _chat_persona_rows(log: EventLog, session_id: str) -> list:
    return [
        event
        for event in log.get_events(session_id)
        if event.type == "system"
        and event.payload.get("event") == CHAT_PERSONA_EVENT
    ]


# -- the fold: explicit-none vs never-set ------------------------------------


def test_state_never_set_is_not_explicit(log: EventLog):
    log.append_event("s", "system", "system", {"event": "session_created"})
    assert chat_persona_state(log, "s") == (False, None)


def test_state_assignment_is_explicit(log: EventLog):
    log.append_event(
        "s", "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": "p1"}
    )
    assert chat_persona_state(log, "s") == (True, "p1")


def test_state_clear_is_explicit_none(log: EventLog):
    clear_persona(log, "s")
    assert chat_persona_state(log, "s") == (True, None)


def test_state_legacy_null_row_is_explicit_none(log: EventLog):
    """A bare ``persona_id: null`` (pre-marker clear) is still an explicit clear."""
    log.append_event(
        "s", "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": None}
    )
    assert chat_persona_state(log, "s") == (True, None)


def test_state_clear_then_reassign_wins(log: EventLog):
    log.append_event(
        "s", "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": "p1"}
    )
    clear_persona(log, "s")
    log.append_event(
        "s", "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": "p2"}
    )
    assert chat_persona_state(log, "s") == (True, "p2")


def test_clear_row_payload_shape(log: EventLog):
    clear_persona(log, "s")
    rows = _chat_persona_rows(log, "s")
    assert len(rows) == 1
    assert rows[0].payload["persona_id"] is None
    assert rows[0].payload[CHAT_PERSONA_CLEARED_KEY] is True
    # The raw fold every existing reader uses is unchanged: None.
    assert assigned_persona_id(log, "s") is None


# -- API: clear means no persona, even with a pin ----------------------------


def test_clear_means_no_persona_not_default(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            # Created while pinned -> create-time materialization writes Alice.
            sid = (await client.post("/api/sessions", json={})).json()["session_id"]
            assert persona_for_chat(app.state.log, sid) == pid
            deleted = await client.delete(f"/api/sessions/{sid}/persona")
            assert deleted.status_code == 200
            assert deleted.json() == {"session_id": sid, "persona_id": None}
            return pid, sid

    pid, sid = run(scenario())
    log = app.state.log
    # The clear supersedes the materialized pin row (latest wins).
    assert persona_for_chat(log, sid) is None
    assert chat_config_for(log, sid)["persona_id"] is None
    # {{user}} falls back to the literal word, not the pin's name.
    values = resolve_variables(log, sid, "hi")
    assert values["user"] == "user"
    assert values["persona"] == ""
    # MEMORY is not a resolution site: the chat keeps its chat-local collection.
    assert resolve_scope(log, sid).persona_id is None
    assert resolve_scope(log, sid).collection == collection_for(sid)
    # Log shape: [Alice, explicit-clear].
    rows = _chat_persona_rows(log, sid)
    assert [r.payload["persona_id"] for r in rows] == [pid, None]
    assert rows[-1].payload[CHAT_PERSONA_CLEARED_KEY] is True


def test_clear_survives_a_later_pin(build_app):
    """Clearing a never-set chat, then pinning, must not re-pin it."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = (await client.post("/api/sessions", json={})).json()["session_id"]
            # Explicit clear BEFORE any pin exists.
            assert (await client.delete(f"/api/sessions/{sid}/persona")).status_code == 200
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            return pid, sid

    pid, sid = run(scenario())
    log = app.state.log
    assert assigned_persona_id(log, sid) is None
    assert persona_for_chat(log, sid) is None  # NOT re-pinned
    assert resolve_variables(log, sid, "hi")["user"] == "user"


def test_clear_survives_a_pin_change_after_create_materialization(build_app):
    """Create (materialize Alice) -> clear -> pin Bob: stays persona-less."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            alice = await _create_persona(client, "Alice")
            await _pin(client, alice)
            sid = (await client.post("/api/sessions", json={})).json()["session_id"]
            await client.delete(f"/api/sessions/{sid}/persona")
            bob = await _create_persona(client, "Bob")
            await _pin(client, bob)  # a later "downgrade"/change of the pin
            return alice, bob, sid

    alice, bob, sid = run(scenario())
    log = app.state.log
    assert persona_for_chat(log, sid) is None
    assert resolve_variables(log, sid, "hi")["user"] == "user"


def test_absent_still_inherits_the_pin(build_app):
    """A chat that NEVER recorded a choice keeps inheriting the pin."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            # Created BEFORE the pin -> no chat_persona row at all.
            sid = (await client.post("/api/sessions", json={})).json()["session_id"]
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            return pid, sid

    pid, sid = run(scenario())
    log = app.state.log
    assert assigned_persona_id(log, sid) is None
    assert persona_for_chat(log, sid) == pid
    assert resolve_variables(log, sid, "hi")["user"] == "Alice"


def test_reassign_after_clear_wins(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            alice = await _create_persona(client, "Alice")
            await _pin(client, alice)
            sid = (await client.post("/api/sessions", json={})).json()["session_id"]
            await client.delete(f"/api/sessions/{sid}/persona")
            bob = await _create_persona(client, "Bob")
            assigned = await client.post(
                f"/api/sessions/{sid}/persona", json={"persona_id": bob}
            )
            assert assigned.status_code == 200
            return bob, sid

    bob, sid = run(scenario())
    log = app.state.log
    assert chat_persona_state(log, sid) == (True, bob)
    assert persona_for_chat(log, sid) == bob
    assert resolve_variables(log, sid, "hi")["user"] == "Bob"


def test_session_row_carries_persona_cleared_flag(build_app):
    """Server truth for the picker: cleared vs never-set vs assigned.

    ``persona_id`` is null for BOTH a clear and a never-set chat; the boolean
    ``persona_cleared`` is the only discriminator (orchestrator owns adding the
    matching ``api.ts`` SessionSummary field; the drawer reads it defensively).
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            # Created BEFORE any pin -> no chat_persona row (never set).
            never_set = (await client.post("/api/sessions", json={})).json()["session_id"]
            pid = await _create_persona(client, "Alice")
            await _pin(client, pid)
            # Created while pinned -> materialized assignment (assigned).
            assigned = (await client.post("/api/sessions", json={})).json()["session_id"]
            cleared = (await client.post("/api/sessions", json={})).json()["session_id"]
            await client.delete(f"/api/sessions/{cleared}/persona")
            rows = {
                row["session_id"]: row
                for row in (await client.get("/api/sessions")).json()["sessions"]
            }
            return pid, never_set, assigned, cleared, rows

    pid, never_set, assigned, cleared, rows = run(scenario())
    assert rows[never_set]["persona_id"] is None
    assert rows[never_set]["persona_cleared"] is False
    assert rows[assigned]["persona_id"] == pid
    assert rows[assigned]["persona_cleared"] is False
    assert rows[cleared]["persona_id"] is None
    assert rows[cleared]["persona_cleared"] is True


def test_clear_rides_a_fork(log: EventLog):
    """The clear rides the same event kind, so a fork's config carry keeps it."""
    parent, branch = "parent", "branch"
    log.append_event(
        parent, "system", "system", {"title": "T", "event": "session_created"}
    )
    log.append_event(
        parent, "system", "user", {"event": CHAT_PERSONA_EVENT, "persona_id": "p1"}
    )
    clear_persona(log, parent)
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": "p1", "name": "Alice"},
    )
    # Fork before the clear: the assignment AND the clear are carried onto the
    # branch; latest wins -> still explicitly no persona.
    cut = log.get_events(parent)[0].seq  # after session_created
    log.fork_session(parent, at_seq=cut, new_session_id=branch)
    carried = [r.payload.get("persona_id") for r in _chat_persona_rows(log, branch)]
    assert carried == ["p1", None]
    assert persona_for_chat(log, branch) is None


# -- PART 2: {{user}} -> the word "user" -------------------------------------


def test_user_word_in_module_surface(log: EventLog):
    log.append_event("s", "user", "user", {"text": "hi"})
    values = resolve_variables(log, "s", "hi")
    assert values["user"] == "user"
    assert values["persona"] == ""
    assert render_template("{{user}} acts.", values) == "user acts."


def test_helpers_render_the_user_word():
    assert _render("{{user}} acts", {"user": "user"}) == "user acts"
    assert _render_value("{{user}} acts", {"user": "user"}) == "user acts"


def test_world_info_renders_user_word_without_a_persona(log: EventLog):
    sid = "s"
    book = {
        "name": "Ship lore",
        "entries": [
            {
                "keys": ["ship"],
                "content": "{{user}} serves aboard the ship.",
                "insertion_order": 1,
            }
        ],
    }
    log.append_event(sid, "system", "system", {"event": "world_info", "book": book})
    values = resolve_variables(log, sid, "I look at the ship.")
    assert values["user"] == "user"
    block = session_world_info_block(log, sid, "I look at the ship.", variables=values)
    assert block is not None
    assert "user serves aboard the ship." in block
    assert "{{user}}" not in block


# -- PART 3: the moved fold keeps the settings surface identical -------------


def test_pin_fold_is_reexported_from_the_neutral_module():
    import fabulist.api.settings as api_settings
    import fabulist.state.settings as state_settings

    assert api_settings.default_persona_id is state_settings.default_persona_id
    assert api_settings.DEFAULT_PERSONA_EVENT is state_settings.DEFAULT_PERSONA_EVENT
    assert api_settings.SETTINGS_SESSION_ID is state_settings.SETTINGS_SESSION_ID


def test_settings_get_patch_unchanged_by_the_move(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            pid = await _create_persona(client, "Alice")
            pinned = await _pin(client, pid)
            after_pin = pinned.json()
            unpinned = (await _pin(client, None)).json()
            return pid, after_pin, unpinned

    pid, after_pin, unpinned = run(scenario())
    assert after_pin["default_persona_id"] == pid
    assert after_pin["default_persona_label"] == "Alice"
    assert unpinned["default_persona_id"] is None
    # The anchor still carries the raw fold the neutral module projects.
    log = app.state.log
    from fabulist.state.settings import default_persona_id

    assert default_persona_id(log) is None
