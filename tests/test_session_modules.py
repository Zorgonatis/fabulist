"""Tests for Wave B: session-module splicing (B1) + greeting selection (B3).

Covers the pure splicer :func:`fabulist.prompts.modules.splice_session_modules`
(anchors, fallback, tombstones, disable, order, input immutability, compile
integration), the greeting-resolution helpers on
:mod:`fabulist.context.roster`, and the D30 byte-parity guarantee for a legacy
session's persona block. No log writes happen for the splicer tests — it is
pure data in, data out.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cards_api import router as cards_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.context.facts import session_persona_block
from fabulist.context.roster import (
    greeting_variants,
    selected_greeting,
)
from fabulist.log import EventLog
from fabulist.prompts.modules import (
    PromptModule,
    compile_flow,
    from_default_flow,
    splice_session_modules,
)

# --- fixtures / helpers -----------------------------------------------------


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for an :class:`EventLog` on a tmp path; closes logs after."""
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


def _session_module(
    module_id: str,
    content: str,
    *,
    anchor: str = "top",
    role: str = "system",
    name: str | None = None,
    enabled: bool = True,
    origin: str = "card:Lyra",
) -> dict:
    return {
        "event": "session_module",
        "module_id": module_id,
        "name": name or module_id,
        "anchor": anchor,
        "role": role,
        "content": content,
        "enabled": enabled,
        "origin": origin,
    }


def _types(rows: list) -> list[str]:
    return [str(m.type) for m in rows]


def _evt(payload_dict: dict):
    """Minimal duck-typed event for the pure greeting-resolution tests."""
    return SimpleNamespace(**payload_dict)


NARRATE_PARTS = {
    "narrator_system": "SYS",
    "library": "LIB",
    "history": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}],
    "facts": "FACTS",
    "user_content": "USER",
    "recalled": "RECALLED",
}


# --- (B1) splicing: anchors -------------------------------------------------


def test_top_module_prepended_before_first_flow_row():
    flow = from_default_flow("narrate")
    home = _session_module("card:x:system_prompt", "CARD SYSTEM", anchor="top")
    spliced = splice_session_modules(flow, [home])

    assert spliced[0].type == "text"
    assert spliced[0].content == "CARD SYSTEM"
    assert spliced[0].role == "system"
    assert _types(spliced)[1:] == _types(flow)


def test_post_history_lands_after_history_and_before_facts_user():
    flow = from_default_flow("narrate")
    home = _session_module("card:x:post_history", "POST", anchor="post_history")
    spliced = splice_session_modules(flow, [home])

    types = _types(spliced)
    history_at = types.index("history")
    post_at = types.index("text")
    assert post_at == history_at + 1
    # ...and before the first user/facts row.
    assert post_at < types.index("facts")
    assert post_at < types.index("user")
    assert spliced[post_at].content == "POST"


def test_post_history_falls_back_before_first_user_when_no_history_row():
    flow = [
        PromptModule("system", "System", "narrator_system"),
        PromptModule("usr", "User", "user"),
        PromptModule("rec", "Recalled", "recalled"),
    ]
    home = _session_module("post", "POST", anchor="post_history")
    spliced = splice_session_modules(flow, [home])
    assert _types(spliced) == ["narrator_system", "text", "user", "recalled"]


def test_post_history_falls_back_before_first_facts_row():
    flow = [
        PromptModule("system", "System", "narrator_system"),
        PromptModule("facts", "Facts", "facts"),
        PromptModule("user", "User", "user"),
    ]
    home = _session_module("post", "POST", anchor="post_history")
    spliced = splice_session_modules(flow, [home])
    assert _types(spliced) == ["narrator_system", "text", "facts", "user"]


def test_post_history_appends_last_when_no_anchor_row_exists():
    flow = [
        PromptModule("system", "System", "narrator_system"),
        PromptModule("lib", "Library", "library"),
    ]
    home = _session_module("post", "POST", anchor="post_history")
    spliced = splice_session_modules(flow, [home])
    assert _types(spliced) == ["narrator_system", "library", "text"]


def test_top_and_post_history_combine_in_one_splice():
    flow = from_default_flow("narrate")
    spliced = splice_session_modules(
        flow,
        [
            _session_module("a", "TOP-A", anchor="top"),
            _session_module("b", "POST", anchor="post_history"),
        ],
    )
    assert spliced[0].content == "TOP-A"
    contents = [m.content for m in spliced]
    types = _types(spliced)
    assert contents.index("POST") == types.index("history") + 1


# --- (B1) ordering / immutability -------------------------------------------


def test_modules_within_an_anchor_keep_event_order():
    flow = from_default_flow("narrate")
    spliced = splice_session_modules(
        flow,
        [
            _session_module("one", "FIRST", anchor="top"),
            _session_module("two", "SECOND", anchor="top"),
        ],
    )
    tops = [m.content for m in spliced if m.type == "text"]
    assert tops == ["FIRST", "SECOND"]


def test_splice_does_not_mutate_its_inputs():
    flow = from_default_flow("narrate")
    home = _session_module("a", "TOP", anchor="top")
    flow_snapshot = [(m.id, m.type, m.content, m.enabled) for m in flow]
    home_snapshot = dict(home)

    spliced = splice_session_modules(flow, [home])

    assert [(m.id, m.type, m.content, m.enabled) for m in flow] == flow_snapshot
    assert home == home_snapshot
    assert len(spliced) == len(flow) + 1
    # The returned rows are copies, not aliases of the flow rows.
    assert all(a is not b for a, b in zip(spliced[1:], flow))


# --- (B1) disable / tombstone / latest-wins ---------------------------------


def test_disabled_module_is_dropped():
    flow = from_default_flow("narrate")
    spliced = splice_session_modules(
        flow, [_session_module("a", "TOP", anchor="top", enabled=False)]
    )
    assert _types(spliced) == _types(flow)


def test_tombstone_removes_and_latest_wins():
    flow = from_default_flow("narrate")
    entries = [
        _session_module("a", "TOP", anchor="top"),
        {"event": "session_module_deleted", "module_id": "a"},
    ]
    assert _types(splice_session_modules(flow, entries)) == _types(flow)

    # Re-append after the tombstone restores it (latest-wins per module_id).
    restored = splice_session_modules(
        flow, entries + [_session_module("a", "BACK", anchor="top")]
    )
    assert restored[0].content == "BACK"


# --- (B3) greeting route + opening endpoint ---------------------------------

GREETING_CARD = {
    "spec": "chara_card_v2",
    "data": {
        "name": "Lyra Voss",
        "description": "A wandering cartographer.",
        "first_mes": "Lyra looks up.",
        "alternate_greetings": ["Lyra is already packing.", "Lyra waves you over."],
    },
}


@pytest.fixture
def build_app(tmp_path: Path):
    built = []

    def _build():
        app = create_app(FakeTurnEngine(), tmp_path / "events.db")
        app.include_router(cards_router)
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


def test_opening_endpoint_defaults_and_cycle(build_app):
    app = build_app()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post("/api/cards/import", json={**GREETING_CARD, "seed": True})
            sid = created.json()["session_id"]
            opening = await client.get(f"/api/sessions/{sid}/opening")
            return sid, opening

    sid, opening = asyncio.run(scenario())
    body = opening.json()
    assert opening.status_code == 200
    assert body["greetings"] == ["Lyra looks up.", "Lyra is already packing.", "Lyra waves you over."]
    assert body["index"] == 0
    assert body["card"]["name"] == "Lyra Voss"


def test_greeting_route_appends_and_persists_selection(build_app):
    app = build_app()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post("/api/cards/import", json={**GREETING_CARD, "seed": True})
            sid = created.json()["session_id"]
            selected = await client.post(f"/api/sessions/{sid}/greeting", json={"index": 2})
            opening = await client.get(f"/api/sessions/{sid}/opening")
            return sid, selected, opening

    sid, selected, opening = asyncio.run(scenario())
    assert selected.status_code == 200
    assert selected.json()["index"] == 2
    assert opening.json()["index"] == 2

    # The selection is an appended system event (latest-wins), not a mutation.
    events = app.state.log.get_events(sid)
    selections = [
        e.payload for e in events
        if e.type == "system" and e.payload.get("event") == "greeting_selected"
    ]
    assert selections == [{"event": "greeting_selected", "index": 2}]


def test_greeting_route_rejects_out_of_range_index(build_app):
    app = build_app()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post("/api/cards/import", json={**GREETING_CARD, "seed": True})
            sid = created.json()["session_id"]
            return await client.post(f"/api/sessions/{sid}/greeting", json={"index": 9})

    assert asyncio.run(scenario()).status_code == 422


def test_reappend_updates_content_latest_wins():
    flow = from_default_flow("narrate")
    entries = [
        _session_module("a", "OLD", anchor="top"),
        _session_module("a", "NEW", anchor="top"),
    ]
    spliced = splice_session_modules(flow, entries)
    tops = [m.content for m in spliced if m.type == "text"]
    assert tops == ["NEW"]


def test_unknown_anchor_is_ignored():
    flow = from_default_flow("narrate")
    spliced = splice_session_modules(
        flow, [_session_module("a", "X", anchor="sideways")]
    )
    assert _types(spliced) == _types(flow)


# --- (B1) compile integration -----------------------------------------------


def test_seeded_system_prompt_is_first_system_message():
    flow = from_default_flow("narrate")
    spliced = splice_session_modules(
        flow, [_session_module("card:x:system_prompt", "CARD SYSTEM", anchor="top")]
    )
    messages = compile_flow("narrate", spliced, NARRATE_PARTS)
    assert messages[0]["role"] == "system"
    # Issue #44: the card's top home opens the ONE prefix system message; the
    # narrator system + library rows fold in behind it (in served order).
    assert messages[0]["content"].startswith("CARD SYSTEM")
    assert "SYS" in messages[0]["content"] and "LIB" in messages[0]["content"]


def test_seeded_post_history_renders_after_history_before_user():
    flow = from_default_flow("narrate")
    spliced = splice_session_modules(
        flow, [_session_module("card:x:post_history", "POST", anchor="post_history")]
    )
    messages = compile_flow("narrate", spliced, NARRATE_PARTS)
    roles = [m["role"] for m in messages]
    contents = [m["content"] for m in messages]
    # history renders two messages (user, assistant); the post-history row
    # follows them and precedes the facts/user user-messages. Issue #68: it rides
    # the ASSISTANT carrier — a system row after history is a 500 on a stock
    # Jinja chat template, and its authored position (immediately before the ask)
    # is preserved.
    post_at = contents.index("POST")
    assert roles[post_at] == "assistant"
    assert contents.index("FACTS") > post_at
    assert contents.index("USER") > post_at
    assert contents.index("hey") < post_at


# --- (B3) greeting helpers --------------------------------------------------


def test_selected_greeting_defaults_to_zero_without_event():
    assert selected_greeting([]) == 0
    assert selected_greeting([_evt({"type": "system", "payload": {"event": "other"}})]) == 0


def test_selected_greeting_latest_wins():
    events = [
        _evt({"type": "system", "payload": {"event": "greeting_selected", "index": 1}}),
        _evt({"type": "system", "payload": {"event": "greeting_selected", "index": 3}}),
    ]
    assert selected_greeting(events) == 3


def test_selected_greeting_ignores_bad_values():
    events = [
        _evt({"type": "system", "payload": {"event": "greeting_selected", "index": -1}}),
        _evt({"type": "system", "payload": {"event": "greeting_selected", "index": True}}),
        _evt({"type": "system", "payload": {"event": "greeting_selected", "index": "2"}}),
    ]
    assert selected_greeting(events) == 0


def test_greeting_variants_includes_first_and_alternates():
    card = {"first_mes": "ONE", "alternate_greetings": ["TWO", "THREE"]}
    assert greeting_variants(card) == ["ONE", "TWO", "THREE"]
    assert greeting_variants(card, "PAYLOAD") == ["PAYLOAD", "TWO", "THREE"]
    assert greeting_variants({}) == [""]


# --- (B3) persona-block byte parity + selection -----------------------------

LYRA = {
    "name": "Lyra Voss",
    "description": "A wandering cartographer.",
    "personality": "Curious and wry.",
}
LYRA_GREETING = "Lyra looks up from her maps. 'You lost?'"
LYRA_ALT = "Lyra is already packing her maps when you arrive."

#: Byte-exact pre-D30 single-card block (no greeting_selected event).
LEGACY_BLOCK = (
    "### Character: Lyra Voss\n"
    "Description: A wandering cartographer.\n"
    "Personality: Curious and wry.\n"
    "Opening scene (already established): "
    "Lyra looks up from her maps. 'You lost?'"
)
#: sha256 of :data:`LEGACY_BLOCK` — pinned so a future edit to the render path
#: cannot silently drift the bytes a legacy session sends the narrator.
LEGACY_HASH = hashlib.sha256(LEGACY_BLOCK.encode("utf-8")).hexdigest()


def test_legacy_persona_block_is_byte_identical(build_log):
    log = build_log()
    sid = "legacy"
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": LYRA_GREETING})
    block = session_persona_block(log, sid)
    assert block == LEGACY_BLOCK
    assert hashlib.sha256(block.encode("utf-8")).hexdigest() == LEGACY_HASH


def test_selected_alternate_renders_that_variant(build_log):
    log = build_log()
    sid = "swiped"
    log.append_event(
        sid,
        "system",
        "system",
        {
            "card": {**LYRA, "alternate_greetings": [LYRA_ALT]},
            "first_mes": LYRA_GREETING,
        },
    )
    log.append_event(sid, "system", "user", {"event": "greeting_selected", "index": 1})
    block = session_persona_block(log, sid)
    assert block is not None
    assert block.endswith(f"Opening scene (already established): {LYRA_ALT}")
    # Index 0 still renders the legacy bytes.
    log.append_event(sid, "system", "user", {"event": "greeting_selected", "index": 0})
    assert session_persona_block(log, sid) == LEGACY_BLOCK
