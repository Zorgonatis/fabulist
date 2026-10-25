"""Tests for the D32 world entity registry + materialization router.

Mirrors :mod:`tests.test_personas_api`: the real FastAPI app (httpx ASGI
transport) over a tmp SQLite log, with :mod:`fabulist.api.worlds_api` mounted
beside the app factory's personas/world-info routers. The log owns everything
(D9): worlds fold the ``__worlds__`` anchor session and starting a chat appends
card-shaped seed events + a ``world_ref`` snapshot + a ``session_books``
registration.

The end-to-end render assertion uses the real facts path
(:func:`fabulist.context.facts.session_persona_block` /
:func:`session_world_info_block` / :func:`recalled_message`) to prove the cast
flows through the EXISTING multi-card roster render and the world prefix rides
the recalled assembly — no new render machinery.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.personas_api import router as personas_router
from fabulist.api.worldinfo_api import router as worldinfo_router
from fabulist.api.worlds_api import worlds_router
from fabulist.context.facts import (
    facts_suffix_parts,
    join_facts_suffix,
    recalled_message,
    session_persona_block,
    static_prefix_parts,
)
from fabulist.log import EventLog
from fabulist.worlds.registry import (
    NARRATOR_ACTOR,
    SESSION_BOOKS_EVENT,
    WORLDS_ANCHOR,
    WORLD_OPENING_FLAG,
    WORLD_REF_EVENT,
    append_world,
    create_world_chat,
    delete_world,
    list_worlds,
    world_by_id,
)

ENTRIES = [
    {
        "keys": ["lantern"],
        "content": "The night market opens only after the second bell.",
        "insertion_order": 1,
    }
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """App over a tmp log with the world + persona + book routers mounted."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(worlds_router)
        app.include_router(personas_router)
        app.include_router(worldinfo_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create_persona(client: httpx.AsyncClient, **fields) -> dict:
    body = {"name": "Alice"}
    body.update(fields)
    resp = await client.post("/api/personas", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["persona"]


async def _create_world(client: httpx.AsyncClient, **fields) -> dict:
    body = {"name": "Aurora Falls"}
    body.update(fields)
    resp = await client.post("/api/worlds", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["world"]


# -- registry ----------------------------------------------------------------


def test_append_fold_and_tombstone(tmp_path: Path):
    log = EventLog(tmp_path / "w.db")
    try:
        world_id = append_world(
            log,
            {
                "name": "Aurora Falls",
                "description": "A neon city.",
                "scenario": "It always rains.",
                "cast": ["p1", "p2"],
                "book_ids": ["b1"],
                "provenance": {"origin": "test"},
            },
        )
        (world,) = list_worlds(log)
        assert world.world_id == world_id
        assert world.cast == ["p1", "p2"]
        assert world.book_ids == ["b1"]
        assert world.provenance == {"origin": "test"}

        append_world(log, {"world_id": world_id, "name": "Aurora Falls v2"})
        assert [w.name for w in list_worlds(log)] == ["Aurora Falls v2"]

        delete_world(log, world_id)
        assert list_worlds(log) == []
        assert world_by_id(log, world_id) is None
    finally:
        log.close()


def test_append_world_requires_name(tmp_path: Path):
    log = EventLog(tmp_path / "w2.db")
    try:
        with pytest.raises(ValueError, match="non-empty 'name'"):
            append_world(log, {"name": "  "})
    finally:
        log.close()


# -- API: world CRUD ---------------------------------------------------------


def test_create_list_and_get_world(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await _create_world(
                client, scenario="Neon rain.", cast=["p1"], book_ids=["b1"]
            )
            listed = (await client.get("/api/worlds")).json()
            fetched = await client.get(f"/api/worlds/{created['world_id']}")
            return created, listed, fetched

    created, listed, fetched = run(scenario())
    assert created["name"] == "Aurora Falls"
    assert created["scenario"] == "Neon rain."
    assert created["cast"] == ["p1"]
    assert listed["worlds"] == [created]
    assert fetched.status_code == 200
    assert fetched.json()["world"] == created


def test_put_updates_world_latest_wins(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = await _create_world(client)
            updated = await client.put(
                f"/api/worlds/{first['world_id']}",
                json={"name": "Aurora Falls v2", "scenario": "Drier."},
            )
            listed = (await client.get("/api/worlds")).json()
            return first, updated, listed

    first, updated, listed = run(scenario())
    assert updated.status_code == 200
    assert updated.json()["world"]["world_id"] == first["world_id"]
    assert updated.json()["world"]["name"] == "Aurora Falls v2"
    assert len(listed["worlds"]) == 1


def test_world_opening_survives_a_studio_style_update(build_app):
    """An update that omits the opening KEEPS it (issue #51).

    The studio's world form predates ``first_mes``/``alternate_greetings`` and
    posts only the editable slots. Saving must never erase the greeting an
    import captured; a caller that DOES supply an opening still replaces it.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world = await _create_world(
                client,
                first_mes="The rain drums on the canvas.",
                alternate_greetings=["You wake to the smell of salt and tar."],
            )
            saved = await client.put(
                f"/api/worlds/{world['world_id']}",
                json={"name": "Aurora Falls v2", "scenario": "Drier."},
            )
            replaced = await client.put(
                f"/api/worlds/{world['world_id']}",
                json={
                    "name": "Aurora Falls v3",
                    "first_mes": "New first message.",
                    "alternate_greetings": ["New alternate."],
                },
            )
            cleared = await client.put(
                f"/api/worlds/{world['world_id']}",
                json={
                    "name": "Aurora Falls v4",
                    "first_mes": "",
                    "alternate_greetings": [],
                },
            )
            return saved, replaced, cleared

    saved, replaced, cleared = run(scenario())
    assert saved.status_code == 200, saved.text
    assert saved.json()["world"]["first_mes"] == "The rain drums on the canvas."
    assert saved.json()["world"]["alternate_greetings"] == [
        "You wake to the smell of salt and tar."
    ]
    # An explicit opening replaces it; an explicit empty one clears it.
    assert replaced.json()["world"]["first_mes"] == "New first message."
    assert replaced.json()["world"]["alternate_greetings"] == ["New alternate."]
    assert cleared.json()["world"]["first_mes"] == ""
    assert cleared.json()["world"]["alternate_greetings"] == []


def test_world_with_malformed_opening_is_422(build_app):
    """The opening fields are typed at the door: a non-list is a precise 422."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/worlds", json={"name": "Aurora", "alternate_greetings": "nope"}
            )

    assert run(scenario()).status_code == 422


def test_delete_world_tombstones(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world = await _create_world(client)
            deleted = await client.delete(f"/api/worlds/{world['world_id']}")
            fetched = await client.get(f"/api/worlds/{world['world_id']}")
            listed = (await client.get("/api/worlds")).json()
            return deleted, fetched, listed

    deleted, fetched, listed = run(scenario())
    assert deleted.json()["deleted"] is True
    assert fetched.status_code == 404
    assert listed["worlds"] == []


def test_unknown_world_routes_are_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (
                await client.get("/api/worlds/ghost"),
                await client.put("/api/worlds/ghost", json={"name": "x"}),
                await client.delete("/api/worlds/ghost"),
                await client.post("/api/worlds/ghost/chats", json={}),
            )

    responses = run(scenario())
    assert {resp.status_code for resp in responses} == {404}


def test_create_world_without_name_is_422(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/worlds", json={"name": "  "})

    assert run(scenario()).status_code == 422
    log = EventLog(log_path)
    assert log.get_events(WORLDS_ANCHOR) == []
    log.close()


# -- materialization ---------------------------------------------------------


def test_world_chat_materializes_cast_events(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            lyra = await _create_persona(
                client,
                name="Lyra Voss",
                description="A wandering cartographer.",
                personality="Curious.",
                scenario="A night market.",
                first_mes="Lyra looks up.",
            )
            morrow = await _create_persona(client, name="Captain Morrow")
            book = (
                await client.post(
                    "/api/books", json={"name": "Market lore", "entries": ENTRIES}
                )
            ).json()["book"]
            world = await _create_world(
                client,
                scenario="Neon rain.",
                cast=[lyra["persona_id"], morrow["persona_id"]],
                book_ids=[book["book_id"]],
            )
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            return lyra, morrow, book, world, chat

    lyra, morrow, book, world, chat = run(scenario())
    assert chat.status_code == 200, chat.text
    payload = chat.json()
    assert payload["world_id"] == world["world_id"]
    assert payload["title"] == "Aurora Falls"
    sid = payload["session_id"]

    events = app.state.log.get_events(sid)
    cards = [e.payload["card"] for e in events if isinstance(e.payload.get("card"), dict)]
    assert [c["name"] for c in cards] == ["Lyra Voss", "Captain Morrow"]
    # Card payload mirrors cards_api._seed_session: persona fields as card dict.
    assert cards[0]["description"] == "A wandering cartographer."
    assert cards[0]["personality"] == "Curious."
    assert cards[0]["scenario"] == "A night market."

    seeds = [e for e in events if isinstance(e.payload.get("card"), dict)]
    # Issue #51 (owner ruling): a world chat's opening is the WORLD'S OWN
    # first message — never a cast member's greeting. Reused personas keep
    # their greetings for their own chats, but a world-seeded chat suppresses
    # them entirely (they poisoned the context), so the cast card events carry
    # NEITHER ``first_mes`` NOR ``alternate_greetings``.
    assert lyra["first_mes"] == "Lyra looks up."
    assert all("first_mes" not in seed.payload for seed in seeds)
    assert all("first_mes" not in card for card in cards)
    assert all("alternate_greetings" not in card for card in cards)

    # ...and the SCENARIO is context, not the opening: no narrative row is
    # seeded at all (the marked scenario opening is the pre-#51 shape, kept
    # only as a read shim for chats created before the fix).
    assert not [e for e in events if e.payload.get(WORLD_OPENING_FLAG) is True]
    assert not [e for e in events if e.type == "narrative"]

    # The opening rides the ``world_ref`` SNAPSHOT: the world's own greeting
    # cycle, which ``GET .../opening`` resolves into the pinned Opening block.
    ref = [e for e in events if e.payload.get("event") == WORLD_REF_EVENT]
    assert len(ref) == 1
    assert ref[0].payload == {
        "event": WORLD_REF_EVENT,
        "world_id": world["world_id"],
        "name": "Aurora Falls",
        "scenario": "Neon rain.",
        "first_mes": "",
        "alternate_greetings": [],
    }
    registered = [
        e.payload["book_ids"]
        for e in events
        if e.payload.get("event") == SESSION_BOOKS_EVENT
    ]
    assert registered == [[book["book_id"]]]

    # The chat is registered so /api/sessions resolves it.
    listed = run(_list_sessions(app))
    assert sid in {row["session_id"] for row in listed["sessions"]}
    # ...and mirrored into the app's in-memory registry (cards_api parity), so
    # the frontend can open it immediately via /chat?s=<sid>.
    assert sid in app.state.sessions
    assert app.state.sessions[sid].title == "Aurora Falls"


def test_world_chat_opens_on_the_worlds_first_message_with_swipes(build_app):
    """Issue #51: a world chat opens on the WORLD's ``first_mes``, with swipes.

    The persona path is the spec: the pinned Opening block renders
    ``[first_mes, *alternate_greetings]`` behind swipe arrows, and the swipe is
    an append-only ``greeting_selected`` event (D30). A world chat gets the SAME
    cycle from its ``world_ref`` snapshot — and the SCENARIO is never the
    opening (owner ruling).
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world = await _create_world(
                client,
                scenario="Neon rain sheets across the market.",
                first_mes="The rain drums on the canvas as you duck under it.",
                alternate_greetings=[
                    "You wake to the smell of salt and tar — the docks, before dawn.",
                    "A lantern swings once, twice: someone is waiting.",
                ],
            )
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            opening = await client.get(f"/api/sessions/{sid}/opening")
            swiped = await client.post(
                f"/api/sessions/{sid}/greeting", json={"index": 1}
            )
            after = await client.get(f"/api/sessions/{sid}/opening")
            past_end = await client.post(
                f"/api/sessions/{sid}/greeting", json={"index": 3}
            )
            return sid, opening, swiped, after, past_end

    sid, opening, swiped, after, past_end = run(scenario())
    assert opening.status_code == 200, opening.text
    body = opening.json()
    # The cycle is the world's own greeting, in card order, index 0 = first_mes.
    assert body["greetings"] == [
        "The rain drums on the canvas as you duck under it.",
        "You wake to the smell of salt and tar — the docks, before dawn.",
        "A lantern swings once, twice: someone is waiting.",
    ]
    assert body["index"] == 0
    assert body["card"]["name"] == "Aurora Falls"
    # A world's image is a sessionless definition asset: no session card asset.
    assert body["card_asset"] is None
    # The scenario is CONTEXT: it is not among the opening variants.
    assert "Neon rain sheets across the market." not in body["greetings"]

    # The swipe is the D30 append (latest-wins), never a rewrite.
    assert swiped.status_code == 200, swiped.text
    assert swiped.json()["index"] == 1
    events = app.state.log.get_events(sid)
    assert [
        e.payload["index"]
        for e in events
        if e.payload.get("event") == "greeting_selected"
    ] == [1]
    assert after.json()["index"] == 1
    # An index past the world's cycle is refused exactly like a card's.
    assert past_end.status_code == 422
    assert "past the 3 available greetings" in past_end.json()["detail"]


def test_world_chat_greeting_swipe_appends_and_drives_the_narrator(build_app):
    """The world swipe persists as ``greeting_selected`` and the narrator follows.

    Mirror of the persona path: the pinned block and the narrator's
    ``Opening scene (already established)`` line read the SAME selected index,
    so a swipe changes what the model is told the chat opened on.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world = await _create_world(
                client,
                scenario="Neon rain.",
                first_mes="The door sticks; the rain gets in.",
                alternate_greetings=["You are already soaked when the bell rings."],
            )
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            before = recalled_message(app.state.log, sid, user_text="x")
            await client.post(f"/api/sessions/{sid}/greeting", json={"index": 1})
            return sid, before, await client.get(f"/api/sessions/{sid}/opening")

    sid, before, after = run(scenario())
    events = app.state.log.get_events(sid)
    assert [
        e.payload["index"]
        for e in events
        if e.payload.get("event") == "greeting_selected"
    ] == [1]
    assert after.json()["index"] == 1

    assert before is not None
    assert "Opening scene (already established): The door sticks; the rain gets in." in before
    assert "Neon rain." in before  # scenario is CONTEXT, still rendered

    recalled = recalled_message(app.state.log, sid, user_text="x")
    assert recalled is not None
    assert (
        "Opening scene (already established): You are already soaked when the bell rings."
        in recalled
    )
    assert "The door sticks" not in recalled


def test_world_chat_cast_personas_never_surface_their_own_greetings(build_app):
    """Owner ruling: persona greetings stay SUPPRESSED in world context.

    A world whose cast personas carry ``first_mes`` AND ``alternate_greetings``
    must never show them in a world-seeded chat: not in the cast card events,
    not in the Opening cycle, not in the roster render — including AFTER a world
    swipe moves the session-wide ``greeting_selected`` index onto a slot the
    persona's alternates would otherwise fill.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            lyra = await _create_persona(
                client,
                name="Lyra Voss",
                first_mes="Lyra looks up from her maps.",
                alternate_greetings=["Lyra does not look up at all."],
            )
            world = await _create_world(
                client,
                scenario="Neon rain.",
                first_mes="The market is empty but for one stall.",
                alternate_greetings=["Rain, and no custom at all."],
                cast=[lyra["persona_id"]],
            )
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            opening = await client.get(f"/api/sessions/{sid}/opening")
            await client.post(f"/api/sessions/{sid}/greeting", json={"index": 1})
            return sid, opening

    sid, opening = run(scenario())
    assert opening.json()["greetings"] == [
        "The market is empty but for one stall.",
        "Rain, and no custom at all.",
    ]

    events = app.state.log.get_events(sid)
    cards = [e.payload["card"] for e in events if isinstance(e.payload.get("card"), dict)]
    assert [c["name"] for c in cards] == ["Lyra Voss"]
    assert "first_mes" not in cards[0]
    assert "alternate_greetings" not in cards[0]

    block = session_persona_block(app.state.log, sid)
    assert block is not None and "Lyra Voss" in block
    assert "Lyra looks up from her maps." not in block
    # Index 1 is inside the WORLD's cycle; the cast card's (absent) cycle must
    # not be indexed into it and leak the persona's alternate greeting.
    assert "Lyra does not look up at all." not in block
    recalled = recalled_message(app.state.log, sid, user_text="x")
    assert recalled is not None
    assert "Lyra looks up from her maps." not in recalled
    assert "Lyra does not look up at all." not in recalled


def test_world_chat_without_a_greeting_seeds_nothing_to_open_on(build_app):
    """An empty/absent greeting degrades exactly like an empty persona greeting.

    No Opening block (the frontend hides an all-blank cycle), no opening-scene
    line for the narrator — and STILL no scenario opening: the scenario is
    context, never the first message.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world = await _create_world(
                client, scenario="   ", first_mes="   ", alternate_greetings=[]
            )
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            return sid, await client.get(f"/api/sessions/{sid}/opening")

    sid, opening = run(scenario())
    assert opening.status_code == 200
    # Exactly the persona degradation: variant 0 is the (blank) ``first_mes``
    # verbatim — the frontend trims to decide whether the block renders, so a
    # whitespace-only greeting hides it instead of inventing text.
    assert [greeting.strip() for greeting in opening.json()["greetings"]] == [""]

    events = app.state.log.get_events(sid)
    assert not [e for e in events if e.type == "narrative"]
    assert not [e for e in events if e.payload.get(WORLD_OPENING_FLAG) is True]
    recalled = recalled_message(app.state.log, sid, user_text="x")
    if recalled is not None:
        assert "Opening scene" not in recalled


def test_world_chat_without_scenario_has_no_opening_event(build_app):
    """No scenario (or greeting) seeds no opening narrative, ever (issue #51)."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world = await _create_world(client, scenario="   ")
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            return chat.json()["session_id"]

    sid = run(scenario())
    events = app.state.log.get_events(sid)
    assert not [e for e in events if e.payload.get(WORLD_OPENING_FLAG) is True]
    assert not [e for e in events if e.type == "narrative"]


async def _list_sessions(app):
    async with client_for(app) as client:
        return (await client.get("/api/sessions")).json()


def test_world_chat_renders_cast_and_world_first_turn(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            lyra = await _create_persona(
                client, name="Lyra Voss", description="A wandering cartographer."
            )
            morrow = await _create_persona(client, name="Captain Morrow")
            book = (
                await client.post(
                    "/api/books", json={"name": "Market lore", "entries": ENTRIES}
                )
            ).json()["book"]
            world = await _create_world(
                client,
                scenario="Neon rain.",
                cast=[lyra["persona_id"], morrow["persona_id"]],
                book_ids=[book["book_id"]],
            )
            return (
                await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            ).json()

    chat = run(scenario())
    sid = chat["session_id"]
    log = app.state.log

    # Cast flows through the EXISTING multi-card roster render (2 cards -> Cast).
    cast_block = session_persona_block(log, sid)
    assert cast_block is not None and cast_block.startswith("### Cast")
    assert "#### Lyra Voss" in cast_block and "#### Captain Morrow" in cast_block

    # The cast is STATIC (pre-history): the static accessor carries it, and the
    # per-turn <state> suffix no longer repeats the character card. The recalled
    # message carries the world prefix + the world's activated lore.
    assert "### Cast" in "\n\n".join(static_prefix_parts(log, sid))
    state_joined = join_facts_suffix(facts_suffix_parts(log, sid, user_text="lantern"))
    assert "### Cast" not in state_joined
    assert "### Character" not in state_joined

    recalled = recalled_message(log, sid, user_text="lantern")
    assert recalled is not None
    assert "### World" in recalled
    assert "Aurora Falls" in recalled
    assert "Scenario: Neon rain." in recalled
    assert "The night market opens only after the second bell." in recalled


def test_world_chat_without_books_omits_session_books(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            lyra = await _create_persona(client, name="Lyra")
            world = await _create_world(client, cast=[lyra["persona_id"]])
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            return chat.json()

    chat = run(scenario())
    events = app.state.log.get_events(chat["session_id"])
    assert not [e for e in events if e.payload.get("event") == SESSION_BOOKS_EVENT]
    # The world prefix still renders with no lore entries.
    recalled = recalled_message(app.state.log, chat["session_id"], user_text="x")
    assert recalled is not None and "### World" in recalled


def test_create_world_chat_unknown_world_raises(tmp_path: Path):
    log = EventLog(tmp_path / "missing.db")
    try:
        with pytest.raises(ValueError, match="unknown world"):
            create_world_chat(log, "nope")
        assert log.get_events("__worlds__") == []
    finally:
        log.close()


def test_create_world_chat_unknown_cast_is_rejected(tmp_path: Path):
    log = EventLog(tmp_path / "cast.db")
    try:
        world_id = append_world(log, {"name": "Aurora", "cast": ["ghost-persona"]})
        with pytest.raises(ValueError, match="unknown cast persona"):
            create_world_chat(log, world_id)
        # No partial chat was created.
        assert log.get_events(world_id) == []
    finally:
        log.close()


def test_session_world_read_endpoint(build_app):
    """GET /api/sessions/{id}/world: snapshot + resolved books (D32 read half)."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            lyra = await _create_persona(client, name="Lyra Voss")
            book = (
                await client.post(
                    "/api/books", json={"name": "Market lore", "entries": ENTRIES}
                )
            ).json()["book"]
            world = await _create_world(
                client,
                cast=[lyra["persona_id"]],
                book_ids=[book["book_id"]],
                scenario="Neon rain.",
            )
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            got = await client.get(f"/api/sessions/{sid}/world")
            deleted = await client.delete(f"/api/books/{book['book_id']}")
            after = await client.get(f"/api/sessions/{sid}/world")
            app.state.log.append_event(
                "plain-sess", "system", "human",
                {"event": "session_created", "title": "plain"},
            )
            plain = await client.get("/api/sessions/plain-sess/world")
            unknown = await client.get("/api/sessions/nope/world")
            return world, got, deleted, after, plain, unknown

    world, got, deleted, after, plain, unknown = run(scenario())
    assert got.status_code == 200
    body = got.json()
    assert body["world"]["world_id"] == world["world_id"]
    assert body["world"]["scenario"] == "Neon rain."
    assert [b["name"] for b in body["books"]] == ["Market lore"]
    # Tombstoning the book drops it from the resolved list; the snapshot stays.
    assert deleted.status_code == 200
    assert after.json()["books"] == []
    assert after.json()["world"] is not None
    # A session with neither event renders conditionally: 200 with nulls.
    assert plain.status_code == 200 and plain.json() == {"world": None, "books": []}
    assert unknown.status_code == 404


def test_session_world_read_pools_world_books_after_reattach(build_app):
    """``GET /world`` reports the same lore set the narrator render resolves.

    A world chat's ``session_books`` mirrors the world's books. Re-attaching a
    different set supersedes the session half (latest-wins) but must not hide the
    books re-resolved from the ``world_ref`` world — the render pools both (D31),
    so ``/world`` does too and the drawer cannot contradict the model-visible set.
    Response shape is unchanged: ``{world, books}`` with the same wire fields.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world_book = (
                await client.post(
                    "/api/books", json={"name": "Market lore", "entries": ENTRIES}
                )
            ).json()["book"]
            replacement = (
                await client.post(
                    "/api/books", json={"name": "New lore", "entries": ENTRIES}
                )
            ).json()["book"]
            world = await _create_world(
                client, scenario="Neon rain.", book_ids=[world_book["book_id"]]
            )
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            reattached = await client.get(f"/api/sessions/{sid}/world")
            await client.post(
                f"/api/sessions/{sid}/books",
                json={"book_ids": [replacement["book_id"]]},
            )
            after = await client.get(f"/api/sessions/{sid}/world")
            await client.post(f"/api/sessions/{sid}/books", json={"book_ids": []})
            cleared = await client.get(f"/api/sessions/{sid}/world")
            return (
                reattached,
                after,
                cleared,
                world_book,
                replacement,
                world["world_id"],
            )

    reattached, after, cleared, world_book, replacement, world_id = run(scenario())
    # No session re-attach yet: the world's registered set (unchanged shape).
    assert reattached.json()["books"] == [world_book]
    # Re-attach: the session half is replaced, the world half is still pooled.
    assert after.json()["books"] == [replacement, world_book]
    # An explicit ``[]`` clear drops the session half only.
    assert cleared.json()["books"] == [world_book]
    body = after.json()
    assert set(body) == {"world", "books"}
    assert body["world"] == {
        "world_id": world_id,
        "name": "Aurora Falls",
        "scenario": "Neon rain.",
        "first_mes": "",
        "alternate_greetings": [],
    }


def test_session_world_read_keys_are_stable(build_app):
    """The response KEY shape is the frontend contract: exactly ``{world, books}``.

    The route now pools persona books too, so membership/order can change, but
    the keys must not — ``getSessionWorld`` in ``frontend/src/lib/api.ts`` reads
    them directly.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            persona_book = (
                await client.post(
                    "/api/books", json={"name": "Persona lore", "entries": ENTRIES}
                )
            ).json()["book"]
            persona = await _create_persona(
                client, book_ids=[persona_book["book_id"]]
            )
            world_book = (
                await client.post(
                    "/api/books", json={"name": "World lore", "entries": ENTRIES}
                )
            ).json()["book"]
            world = await _create_world(client, book_ids=[world_book["book_id"]])
            chat = await client.post(f"/api/worlds/{world['world_id']}/chats", json={})
            sid = chat.json()["session_id"]
            assigned = await client.post(
                f"/api/sessions/{sid}/persona",
                json={"persona_id": persona["persona_id"]},
            )
            assert assigned.status_code == 200, assigned.text
            return await client.get(f"/api/sessions/{sid}/world")

    got = run(scenario())
    assert got.status_code == 200
    body = got.json()
    assert set(body) == {"world", "books"}
    # Persona books join the pool without adding a key.
    assert len(body["books"]) >= 2
