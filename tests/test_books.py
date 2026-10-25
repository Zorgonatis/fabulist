"""Tests for the D31 book entity registry + book catalog router.

Two layers, mirroring :mod:`tests.test_personas_api`:

* the pure registry (:mod:`fabulist.context.books`) against a tmp SQLite log —
  append/fold/tombstone, wire conversions, canonical signatures, and the
  ``backfill_books`` migration (+ the ``scripts/backfill_books.py`` CLI);
* the real FastAPI app (httpx ASGI transport) with the book routes mounted from
  :mod:`fabulist.api.worldinfo_api`. The log owns everything (D9): books fold
  the ``__books__`` anchor session and the legacy session-scoped import route is
  preserved verbatim.

No pytest-asyncio in this repo, so async scenarios run via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.personas_api import router as personas_router
from fabulist.api.worldinfo_api import router as worldinfo_router
from fabulist.context.books import (
    BOOKS_ANCHOR,
    ENTRY_ID_KEY,
    append_book,
    backfill_books,
    book_by_id,
    book_signature,
    book_to_wire,
    entry_signature,
    list_books,
    mint_entry_ids,
    wire_to_book,
)
from fabulist.context.facts import (
    _entity_book_ids,
    recalled_message,
    session_world_info_block,
)
from fabulist.context.world_info import parse_book
from fabulist.log import EventLog
from fabulist.personas.registry import PERSONAS_ANCHOR, append_persona, list_personas

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKFILL_SCRIPT = REPO_ROOT / "scripts" / "backfill_books.py"


def _unkeyed(entries):
    """Entries as written, minus the stable key the writers mint (§3.4.3)."""
    return [
        {key: value for key, value in entry.items() if key != ENTRY_ID_KEY}
        for entry in entries
    ]

#: One ST entry list (the exact shape ``world_info.parse_book`` consumes).
ENTRIES = [
    {
        "keys": ["lantern", "night market"],
        "content": "The night market opens only after the second bell.",
        "insertion_order": 1,
        "comment": "market",
    },
    {
        "keys": ["vermin"],
        "content": "Vermin are unwelcome here.",
        "insertion_order": 2,
    },
]

CARD = {
    "name": "Alice",
    "description": "A wry detective.",
    "character_book": {"name": "Night Market lore", "entries": ENTRIES},
}


def run(coro):
    return asyncio.run(coro)


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


@pytest.fixture
def build_app(tmp_path: Path):
    """App over a tmp log with the book catalog + persona routers mounted."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(worldinfo_router)
        app.include_router(personas_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create_session(client: httpx.AsyncClient, title: str = "A chat") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


async def _create_book(client: httpx.AsyncClient, **fields) -> dict:
    body = {"name": "Market lore", "entries": ENTRIES}
    body.update(fields)
    resp = await client.post("/api/books", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["book"]


# -- registry: append / fold / tombstone -------------------------------------


def test_append_and_fold_book(build_log):
    log = build_log()
    book_id = append_book(log, {"name": "Market lore", "entries": ENTRIES})
    books = list_books(log)
    assert [b.book_id for b in books] == [book_id]
    assert books[0].name == "Market lore"
    assert _unkeyed(books[0].entries) == ENTRIES
    assert books[0].provenance == {}


def test_append_book_keys_every_entry_once(build_log):
    """W9.23 §3.4.3: the first write keys the book; later writes keep the keys.

    ``entries.<entry_id>`` is the addressing a book proposal uses, so a key has
    to exist after ANY write path and must survive a re-write:
    re-addressing an entry would silently invalidate every filed op against it.
    """
    log = build_log()
    book_id = append_book(log, {"name": "Market lore", "entries": ENTRIES})
    (first,) = list_books(log)
    keys = [entry[ENTRY_ID_KEY] for entry in first.entries]
    assert all(isinstance(key, str) and key for key in keys)
    assert len(set(keys)) == len(keys)

    append_book(log, {"book_id": book_id, "name": "Renamed", "entries": first.entries})
    (second,) = list_books(log)
    assert [entry[ENTRY_ID_KEY] for entry in second.entries] == keys
    assert _unkeyed(second.entries) == ENTRIES


def test_mint_entry_ids_fills_only_the_missing_keys(build_log):
    """A hand-authored key survives; a non-dict item keeps its place."""
    entries = mint_entry_ids([{"content": "a", ENTRY_ID_KEY: "given"}, 42])
    assert entries == [{"content": "a", ENTRY_ID_KEY: "given"}, 42]
    assert mint_entry_ids("not a list") == []
    assert mint_entry_ids(None) == []


def test_fold_is_latest_wins_per_book_id(build_log):
    log = build_log()
    book_id = append_book(log, {"name": "Market lore", "entries": ENTRIES})
    append_book(log, {"book_id": book_id, "name": "Market lore v2", "entries": []})
    books = list_books(log)
    assert len(books) == 1
    assert books[0].name == "Market lore v2"
    assert books[0].entries == []


def test_tombstone_removes_book_from_fold(build_log):
    log = build_log()
    book_id = append_book(log, {"name": "Market lore", "entries": ENTRIES})
    from fabulist.context.books import delete_book

    delete_book(log, book_id)
    assert list_books(log) == []
    assert book_by_id(log, book_id) is None


def test_fold_ignores_nameless_and_malformed_rows(build_log):
    log = build_log()
    log.append_event(BOOKS_ANCHOR, "system", "user", {"event": "book", "book_id": "x"})
    log.append_event(BOOKS_ANCHOR, "system", "user", {"event": "book", "name": "n"})
    log.append_event(BOOKS_ANCHOR, "user", "user", {"text": "not a system row"})
    assert list_books(log) == []


# -- wire conversions --------------------------------------------------------


def test_wire_round_trip(build_log):
    log = build_log()
    book_id = append_book(log, {"name": "Market lore", "entries": ENTRIES})
    book = book_by_id(log, book_id)
    assert book is not None
    wire = book_to_wire(book)
    assert wire_to_book(wire) == book


def test_wire_rejects_bad_shapes():
    with pytest.raises(ValueError, match="non-empty 'name'"):
        wire_to_book({"name": "  "})
    with pytest.raises(ValueError, match="'entries' must be a list"):
        wire_to_book({"name": "x", "entries": "nope"})
    with pytest.raises(ValueError, match="'provenance' must be an object"):
        wire_to_book({"name": "x", "provenance": []})


# -- canonical signatures ----------------------------------------------------


def test_entry_signature_is_canonical_and_content_sensitive():
    (first, second) = parse_book({"entries": ENTRIES})
    assert entry_signature(first) == entry_signature(first)
    assert entry_signature(first) != entry_signature(second)
    # Re-parsing the same entry yields the same signature (dedupe key).
    (again,) = parse_book({"entries": [ENTRIES[0]]})
    assert entry_signature(again) == entry_signature(first)


def test_book_signature_is_order_sensitive():
    entries = parse_book({"entries": ENTRIES})
    assert book_signature(entries) == book_signature(entries)
    assert book_signature(entries) != book_signature(list(reversed(entries)))


# -- API: book catalog -------------------------------------------------------


def test_create_list_and_get_book(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await _create_book(client, provenance={"origin": "test"})
            listed = (await client.get("/api/books")).json()
            fetched = await client.get(f"/api/books/{created['book_id']}")
            return created, listed, fetched

    created, listed, fetched = run(scenario())
    assert created["name"] == "Market lore"
    assert _unkeyed(created["entries"]) == ENTRIES
    assert all(entry[ENTRY_ID_KEY] for entry in created["entries"])
    assert created["provenance"] == {"origin": "test"}
    assert [b["book_id"] for b in listed["books"]] == [created["book_id"]]
    assert fetched.status_code == 200
    assert fetched.json()["book"] == created


def test_put_updates_book_latest_wins(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = await _create_book(client)
            updated = await client.put(
                f"/api/books/{first['book_id']}",
                json={"name": "Market lore v2", "entries": ENTRIES[:1]},
            )
            listed = (await client.get("/api/books")).json()
            return first, updated, listed

    first, updated, listed = run(scenario())
    assert updated.status_code == 200
    assert updated.json()["book"]["book_id"] == first["book_id"]
    assert updated.json()["book"]["name"] == "Market lore v2"
    assert len(listed["books"]) == 1
    assert listed["books"][0]["name"] == "Market lore v2"


def test_delete_book_tombstones(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            book = await _create_book(client)
            deleted = await client.delete(f"/api/books/{book['book_id']}")
            fetched = await client.get(f"/api/books/{book['book_id']}")
            listed = (await client.get("/api/books")).json()
            return book["book_id"], deleted, fetched, listed

    book_id, deleted, fetched, listed = run(scenario())
    assert deleted.json() == {"book_id": book_id, "deleted": True}
    assert fetched.status_code == 404
    assert listed["books"] == []


def test_unknown_book_routes_are_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (
                await client.get("/api/books/ghost"),
                await client.put("/api/books/ghost", json={"name": "x"}),
                await client.delete("/api/books/ghost"),
            )

    get_resp, put_resp, delete_resp = run(scenario())
    assert (get_resp.status_code, put_resp.status_code, delete_resp.status_code) == (
        404,
        404,
        404,
    )


def test_create_book_without_name_is_422(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/books", json={"name": "  ", "entries": []})

    resp = run(scenario())
    assert resp.status_code == 422
    log = EventLog(log_path)
    assert log.get_events(BOOKS_ANCHOR) == []
    log.close()


def test_create_book_with_invalid_entries_is_422(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/books",
                json={"name": "bad", "entries": [{"keys": {"a": 1}, "content": "c"}]},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert "invalid world info" in resp.json()["detail"]
    log = EventLog(log_path)
    assert log.get_events(BOOKS_ANCHOR) == []
    log.close()


def test_create_book_with_non_object_entry_is_422(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/books", json={"name": "bad", "entries": [42]}
            )

    resp = run(scenario())
    assert resp.status_code == 422
    log = EventLog(log_path)
    assert log.get_events(BOOKS_ANCHOR) == []
    log.close()


# -- API: legacy session-scoped import route ---------------------------------


def test_legacy_session_worldinfo_route_still_works(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            imported = await client.post(
                f"/api/sessions/{sid}/worldinfo",
                json={"book": {"entries": ENTRIES}},
            )
            listed = await client.get(f"/api/sessions/{sid}/worldinfo")
            return sid, imported, listed

    sid, imported, listed = run(scenario())
    assert imported.status_code == 200
    assert imported.json() == {"session_id": sid, "entries": 2}
    assert listed.json()["imports"] == [{"seq": 2, "entry_count": 2}]


def test_legacy_route_writes_world_info_not_session_books(build_app):
    """The new attach route must not have altered the legacy import's events."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            await client.post(
                f"/api/sessions/{sid}/worldinfo",
                json={"book": {"entries": ENTRIES}},
            )
            return sid

    sid = run(scenario())
    log = EventLog(log_path)
    try:
        kinds = [
            e.payload.get("event")
            for e in log.get_events(sid)
            if e.type == "system"
        ]
    finally:
        log.close()
    assert kinds.count("world_info") == 1
    assert "session_books" not in kinds


# -- API: attach a catalog book to an existing chat (D40 gap) ----------------


def test_attach_one_book_to_existing_chat(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            book = await _create_book(client)
            attached = await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [book["book_id"]]}
            )
            world = await client.get(f"/api/sessions/{sid}/world")
            return sid, book, attached, world

    sid, book, attached, world = run(scenario())
    assert attached.status_code == 200
    assert attached.json() == {"session_id": sid, "book_ids": [book["book_id"]]}
    assert world.status_code == 200
    assert [b["book_id"] for b in world.json()["books"]] == [book["book_id"]]


def test_attach_several_books(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            first = await _create_book(client, name="First")
            second = await _create_book(client, name="Second")
            attached = await client.post(
                f"/api/sessions/{sid}/books",
                json={"book_ids": [first["book_id"], second["book_id"]]},
            )
            world = await client.get(f"/api/sessions/{sid}/world")
            return first, second, attached, world

    first, second, attached, world = run(scenario())
    assert attached.status_code == 200
    assert [b["book_id"] for b in world.json()["books"]] == [
        first["book_id"],
        second["book_id"],
    ]


def test_attach_is_latest_wins(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            first = await _create_book(client, name="First")
            second = await _create_book(client, name="Second")
            await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [first["book_id"]]}
            )
            await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [second["book_id"]]}
            )
            world = await client.get(f"/api/sessions/{sid}/world")
            return second, world

    second, world = run(scenario())
    assert [b["book_id"] for b in world.json()["books"]] == [second["book_id"]]


def test_empty_list_clears_attachment(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            book = await _create_book(client)
            await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [book["book_id"]]}
            )
            cleared = await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": []}
            )
            world = await client.get(f"/api/sessions/{sid}/world")
            return cleared, world

    cleared, world = run(scenario())
    assert cleared.status_code == 200
    assert cleared.json()["book_ids"] == []
    assert world.json()["books"] == []


def test_omitting_book_ids_is_422(build_app):
    """Absent != empty (D33): omitting the key is malformed, not a clear."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            return await client.post(f"/api/sessions/{sid}/books", json={})

    assert run(scenario()).status_code == 422


def test_unknown_book_id_is_422_and_writes_nothing(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            book = await _create_book(client)
            # A known id alongside a dangling one: the whole request is refused.
            resp = await client.post(
                f"/api/sessions/{sid}/books",
                json={"book_ids": [book["book_id"], "ghost"]},
            )
            return sid, resp

    sid, resp = run(scenario())
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]
    log = EventLog(log_path)
    try:
        kinds = [
            e.payload.get("event")
            for e in log.get_events(sid)
            if e.type == "system"
        ]
    finally:
        log.close()
    assert "session_books" not in kinds


def test_attach_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/ghost/books", json={"book_ids": []}
            )

    assert run(scenario()).status_code == 404


def test_attached_book_renders_in_world_info(build_app):
    """The WI/facts render folds the attachment with no reader-side change."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            book = await _create_book(client)
            await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [book["book_id"]]}
            )
            return sid

    sid = run(scenario())
    log = EventLog(log_path)
    try:
        block = session_world_info_block(log, sid, "I raise the lantern.")
    finally:
        log.close()
    assert block is not None
    assert "night market opens" in block


# -- persona extension (D31) -------------------------------------------------


def test_persona_carries_d31_fields(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/personas",
                json={
                    "name": "Alice",
                    "book_ids": ["b1", "b2"],
                    "alternate_greetings": ["alt one"],
                    "extensions": {"depth_prompt": "x"},
                    "provenance": {"creator": "me", "origin": "import"},
                },
            )
            return resp.json()["persona"]

    persona = run(scenario())
    assert persona["book_ids"] == ["b1", "b2"]
    assert persona["alternate_greetings"] == ["alt one"]
    assert persona["extensions"] == {"depth_prompt": "x"}
    assert persona["provenance"] == {"creator": "me", "origin": "import"}


def test_old_log_persona_folds_with_empty_d31_defaults(build_log):
    log = build_log()
    # A pre-D31 persona row has none of the new keys; the fold must default them.
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": "p1", "name": "Legacy"},
    )
    (persona,) = list_personas(log)
    assert persona.book_ids == []
    assert persona.alternate_greetings == []
    assert persona.extensions == {}
    assert persona.provenance == {}


# -- backfill migration ------------------------------------------------------


def _seed_legacy_card_session(
    log: EventLog, session_id: str, card: dict | None = None
) -> None:
    """Seed a chat the way ``cards_api._seed_session`` does (card event)."""
    card = dict(card or CARD)
    log.append_event(
        session_id,
        "system",
        "system",
        {"title": "Card chat", "event": "session_created"},
    )
    log.append_event(
        session_id,
        "system",
        "system",
        {"card": card, "first_mes": ""},
    )


def test_backfill_creates_books_and_attaches_to_persona(build_log):
    log = build_log()
    _seed_legacy_card_session(log, "s1")
    persona_id = append_persona(log, {"name": "Alice"})
    log.append_event(
        "s1", "system", "user", {"event": "chat_persona", "persona_id": persona_id}
    )

    result = backfill_books(log)
    assert result == {
        "sessions": 1,
        "books_created": 1,
        "attached": 1,
        "left_session_scoped": 0,
    }
    (book,) = list_books(log)
    assert book.name == "Night Market lore"
    assert _unkeyed(book.entries) == ENTRIES
    assert all(entry[ENTRY_ID_KEY] for entry in book.entries)
    (persona,) = list_personas(log)
    assert persona.book_ids == [book.book_id]


def test_backfill_leaves_session_scoped_without_persona(build_log):
    log = build_log()
    _seed_legacy_card_session(log, "sandbox")
    result = backfill_books(log)
    assert result["attached"] == 0
    assert result["left_session_scoped"] == 1
    # The book still exists for reuse; no persona was invented.
    assert len(list_books(log)) == 1
    assert list_personas(log) == []


def test_backfill_is_idempotent(build_log):
    log = build_log()
    _seed_legacy_card_session(log, "s1")
    persona_id = append_persona(log, {"name": "Alice"})
    log.append_event(
        "s1", "system", "user", {"event": "chat_persona", "persona_id": persona_id}
    )

    first = backfill_books(log)
    assert first == {
        "sessions": 1,
        "books_created": 1,
        "attached": 1,
        "left_session_scoped": 0,
    }
    books_before = len(log.get_events(BOOKS_ANCHOR))
    personas_before = len(log.get_events(PERSONAS_ANCHOR))

    second = backfill_books(log)
    assert second == {
        "sessions": 1,
        "books_created": 0,
        "attached": 0,
        "left_session_scoped": 0,
    }
    # No new rows: the second pass found the book by signature and the persona
    # already carried the id.
    assert len(log.get_events(BOOKS_ANCHOR)) == books_before
    assert len(log.get_events(PERSONAS_ANCHOR)) == personas_before


def test_backfill_dedupe_by_signature_across_sessions(build_log):
    log = build_log()
    persona_id = append_persona(log, {"name": "Alice"})
    for sid in ("s1", "s2"):
        _seed_legacy_card_session(log, sid)
        log.append_event(
            sid, "system", "user", {"event": "chat_persona", "persona_id": persona_id}
        )
    result = backfill_books(log)
    # One book entity (same content), attached once to the single persona.
    assert result["books_created"] == 1
    assert result["attached"] == 1
    assert len(list_books(log)) == 1


def test_backfill_dry_run_writes_nothing(build_log):
    log = build_log()
    _seed_legacy_card_session(log, "s1")
    persona_id = append_persona(log, {"name": "Alice"})
    log.append_event(
        "s1", "system", "user", {"event": "chat_persona", "persona_id": persona_id}
    )

    result = backfill_books(log, dry_run=True)
    assert result == {
        "sessions": 1,
        "books_created": 1,
        "attached": 1,
        "left_session_scoped": 0,
    }
    assert list_books(log) == []
    (persona,) = list_personas(log)
    assert persona.book_ids == []


# -- CLI script --------------------------------------------------------------


def test_backfill_script_defaults_to_dry_run(tmp_path: Path):
    log_path = tmp_path / "cli.db"
    seed = EventLog(log_path)
    _seed_legacy_card_session(seed, "s1")
    seed.close()

    proc = subprocess.run(
        [sys.executable, str(BACKFILL_SCRIPT), "--log", str(log_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "DRY RUN" in proc.stdout
    log = EventLog(log_path)
    try:
        assert list_books(log) == []
    finally:
        log.close()


def test_backfill_script_apply_writes_books(tmp_path: Path):
    log_path = tmp_path / "cli_apply.db"
    seed = EventLog(log_path)
    _seed_legacy_card_session(seed, "s1")
    seed.close()

    proc = subprocess.run(
        [sys.executable, str(BACKFILL_SCRIPT), "--log", str(log_path), "--apply"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "APPLIED" in proc.stdout
    log = EventLog(log_path)
    try:
        assert [b.name for b in list_books(log)] == ["Night Market lore"]
    finally:
        log.close()


# -- D31 render fold: latest-wins per source, union across sources ------------


def _lantern_entries(content: str) -> list[dict]:
    """One ST entry that activates on the ``lantern`` scan keyword."""
    return [{"keys": ["lantern"], "content": content, "insertion_order": 1}]


async def _world_chat(
    client: httpx.AsyncClient, book_ids: list[str], name: str = "Aurora"
) -> tuple[str, str]:
    """Create a world carrying ``book_ids`` and materialize a chat (D32).

    Returns ``(session_id, world_id)``.
    """
    created = await client.post(
        "/api/worlds", json={"name": name, "book_ids": book_ids}
    )
    assert created.status_code == 200, created.text
    world_id = created.json()["world"]["world_id"]
    chat = await client.post(f"/api/worlds/{world_id}/chats", json={})
    assert chat.status_code == 200, chat.text
    return chat.json()["session_id"], world_id


def test_render_uses_latest_session_book_set_after_reattach(build_app):
    """A re-attach supersedes the previous session book set in the WI render.

    The defect: the render accumulated every ``session_books`` event, so the
    first set kept contributing after the second replaced it in ``/world``.
    """
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            first = await _create_book(
                client, name="First", entries=_lantern_entries("FIRST lore")
            )
            second = await _create_book(
                client, name="Second", entries=_lantern_entries("SECOND lore")
            )
            await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [first["book_id"]]}
            )
            await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [second["book_id"]]}
            )
            return sid

    sid = run(scenario())
    log = EventLog(log_path)
    try:
        block = session_world_info_block(log, sid, "I raise the lantern.")
    finally:
        log.close()
    assert block is not None
    assert "SECOND lore" in block
    assert "FIRST lore" not in block


def test_empty_replacement_stops_book_rendering(build_app):
    """The detached-book case: an explicit ``[]`` clear drops the render."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            book = await _create_book(
                client, name="Lore", entries=_lantern_entries("DETACHED lore")
            )
            await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [book["book_id"]]}
            )
            await client.post(f"/api/sessions/{sid}/books", json={"book_ids": []})
            return sid

    sid = run(scenario())
    log = EventLog(log_path)
    try:
        block = session_world_info_block(log, sid, "I raise the lantern.")
    finally:
        log.close()
    assert block is None


def test_session_and_world_books_both_render(build_app):
    """Pooling across sources is intended (D31): a session re-attach must not
    evict the world books reached through the ``world_ref`` snapshot."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world_book = await _create_book(
                client, name="World lore", entries=_lantern_entries("WORLD lore line")
            )
            sid, _ = await _world_chat(client, [world_book["book_id"]])
            session_book = await _create_book(
                client,
                name="Session lore",
                entries=_lantern_entries("SESSION lore line"),
            )
            await client.post(
                f"/api/sessions/{sid}/books",
                json={"book_ids": [session_book["book_id"]]},
            )
            return sid

    sid = run(scenario())
    log = EventLog(log_path)
    try:
        block = session_world_info_block(log, sid, "I raise the lantern.")
    finally:
        log.close()
    assert block is not None
    assert "WORLD lore line" in block
    assert "SESSION lore line" in block


def test_session_and_world_books_are_signature_deduped(build_app):
    """A session book with byte-identical entries to a world book still renders
    once (the existing signature dedupe survives the per-source split)."""
    build, log_path = build_app
    app = build()
    shared = _lantern_entries("SHARED lore line")

    async def scenario():
        async with client_for(app) as client:
            world_book = await _create_book(client, name="World lore", entries=shared)
            sid, _ = await _world_chat(client, [world_book["book_id"]])
            duplicate = await _create_book(
                client, name="Duplicate lore", entries=shared
            )
            assert duplicate["book_id"] != world_book["book_id"]
            await client.post(
                f"/api/sessions/{sid}/books",
                json={"book_ids": [duplicate["book_id"]]},
            )
            return sid

    sid = run(scenario())
    log = EventLog(log_path)
    try:
        block = session_world_info_block(log, sid, "I raise the lantern.")
    finally:
        log.close()
    assert block is not None
    assert block.count("SHARED lore line") == 1


def test_per_source_fold_survives_interleaved_events(build_log):
    """Separate accumulators: a ``session_books`` row between ``world_ref`` rows
    must not clobber the accumulated world books (the naive single-list bug)."""
    from fabulist.worlds.registry import append_world

    log = build_log()
    sid = "interleaved"
    session_first = append_book(
        log, {"name": "S1", "entries": _lantern_entries("S1 lore")}
    )
    session_second = append_book(
        log, {"name": "S2", "entries": _lantern_entries("S2 lore")}
    )
    world_book = append_book(log, {"name": "W", "entries": _lantern_entries("W lore")})
    world_id = append_world(log, {"name": "Aurora", "book_ids": [world_book]})

    log.append_event(
        sid, "system", "user", {"event": "session_books", "book_ids": [session_first]}
    )
    ref = {"event": "world_ref", "world_id": world_id, "name": "Aurora", "scenario": ""}
    log.append_event(sid, "system", "user", dict(ref))
    log.append_event(
        sid, "system", "user", {"event": "session_books", "book_ids": [session_second]}
    )
    log.append_event(sid, "system", "user", dict(ref))

    events = log.get_events(sid)
    assert _entity_book_ids(log, sid, events) == [session_second, world_book]
    block = session_world_info_block(log, sid, "I raise the lantern.")
    assert block is not None
    assert "S2 lore" in block and "W lore" in block
    assert "S1 lore" not in block


def test_world_chat_materialisation_render_unchanged(build_app):
    """A world chat writes ``session_books`` mirroring ``world_ref``'s books;
    the latest-wins fold still renders the world book exactly once."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world_book = await _create_book(
                client, name="World lore", entries=_lantern_entries("WORLD lore line")
            )
            sid, _ = await _world_chat(client, [world_book["book_id"]])
            return sid, world_book["book_id"]

    sid, book_id = run(scenario())
    log = EventLog(log_path)
    try:
        block = session_world_info_block(log, sid, "I raise the lantern.")
        render_ids = _entity_book_ids(log, sid, log.get_events(sid))
    finally:
        log.close()
    assert block is not None
    assert block.startswith("### World\nAurora")
    assert block.count("WORLD lore line") == 1
    assert render_ids == [book_id]


def test_world_chat_renders_the_live_world_description(build_app):
    """A card-style world's setting lives in ``description``.

    "The Crimson Lantern"-shaped worlds carry the whole scenario in the
    definition's ``description`` — the inn IS the ``{{char}}`` — with an empty
    scenario and no cast, while the session's ``world_ref`` snapshot holds only
    name + scenario. The render must read the LIVE definition, so the material
    reaches the prompt instead of a bare name the model then invents around.
    """
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/worlds",
                json={
                    "name": "The Crimson Lantern",
                    "description": "{{char}} is a three story timber inn.",
                },
            )
            assert created.status_code == 200, created.text
            world_id = created.json()["world"]["world_id"]
            chat = await client.post(f"/api/worlds/{world_id}/chats", json={})
            assert chat.status_code == 200, chat.text
            return chat.json()["session_id"]

    sid = run(scenario())
    log = app.state.log
    # No variables map: the cast-less ``{{char}}`` falls back to the world name.
    block = session_world_info_block(log, sid, "I knock.")
    assert block == (
        "### World\nThe Crimson Lantern\nDescription: The Crimson Lantern is a "
        "three story timber inn."
    )
    # The session still stores only the D32 snapshot — live material comes from
    # the ``__worlds__`` fold, so a studio edit is live without rewriting it.
    refs = [e for e in log.get_events(sid) if e.payload.get("event") == "world_ref"]
    assert refs and "description" not in refs[-1].payload
    # And it reaches the narrator's recalled message.
    recalled = recalled_message(log, sid, user_text="I knock.")
    assert recalled is not None and "three story timber inn" in recalled


# -- cross-reader agreement (render vs GET /sessions/{sid}/world) ------------


def test_render_and_world_route_agree_on_session_books(build_app):
    """Differential: the WI render's book ids == ``GET /world``'s ids.

    Both readers fold the same ``session_books`` event latest-wins; this pins
    them together so they cannot drift again (the original defect: the render
    accumulated while the route replaced). No persona events, so the entity
    fold's persona source is empty and the id lists compare exactly.
    """
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            first = await _create_book(
                client, name="First", entries=_lantern_entries("FIRST lore")
            )
            second = await _create_book(
                client, name="Second", entries=_lantern_entries("SECOND lore")
            )
            await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [first["book_id"]]}
            )
            await client.post(
                f"/api/sessions/{sid}/books", json={"book_ids": [second["book_id"]]}
            )
            world = await client.get(f"/api/sessions/{sid}/world")
            assert world.status_code == 200
            return sid, second["book_id"], world.json()

    sid, second_id, world = run(scenario())
    log = EventLog(log_path)
    try:
        render_ids = _entity_book_ids(log, sid, log.get_events(sid))
    finally:
        log.close()
    world_ids = [b["book_id"] for b in world["books"]]
    assert render_ids == world_ids == [second_id]


def test_render_and_world_route_agree_on_world_chat(build_app):
    """Differential for a materialized world chat: both readers resolve the
    world's books through the mirrored ``session_books`` registration."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world_book = await _create_book(
                client, name="World lore", entries=_lantern_entries("WORLD lore line")
            )
            sid, _ = await _world_chat(client, [world_book["book_id"]])
            world = await client.get(f"/api/sessions/{sid}/world")
            assert world.status_code == 200
            return sid, world_book["book_id"], world.json()

    sid, book_id, world = run(scenario())
    log = EventLog(log_path)
    try:
        render_ids = _entity_book_ids(log, sid, log.get_events(sid))
    finally:
        log.close()
    world_ids = [b["book_id"] for b in world["books"]]
    assert render_ids == world_ids == [book_id]


def test_render_and_world_route_agree_after_world_chat_reattach(build_app):
    """Differential case (c): a world chat whose session book set is REPLACED.

    The trio pinned in this section: (a) a plain chat's attached set —
    ``test_render_and_world_route_agree_on_session_books``; (b) a world chat
    with no override — ``test_render_and_world_route_agree_on_world_chat``;
    (c) this test, the case the route got wrong.

    The render pools the replacement session set with the books RE-RESOLVED from
    the ``world_ref`` world (D31, separate per-source folds). ``GET /world`` must
    report the same union: the original defect folded only ``session_books``, so
    after a re-attach the UI listed a different book set than the one the model
    was actually given. Order is the render's — session set first, then world.

    The scenario has no persona events, so the entity fold's persona source is
    empty and the two id lists compare exactly (``/world`` reads the session's
    ``session_books``/``world_ref`` events; persona ``book_ids`` are a separate
    D31 source it does not report).
    """
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            world_book = await _create_book(
                client, name="World lore", entries=_lantern_entries("WORLD lore line")
            )
            sid, _ = await _world_chat(client, [world_book["book_id"]])
            replacement = await _create_book(
                client,
                name="Replacement lore",
                entries=_lantern_entries("REPLACEMENT lore line"),
            )
            await client.post(
                f"/api/sessions/{sid}/books",
                json={"book_ids": [replacement["book_id"]]},
            )
            world = await client.get(f"/api/sessions/{sid}/world")
            assert world.status_code == 200
            return sid, world_book["book_id"], replacement["book_id"], world.json()

    sid, world_book_id, replacement_id, world = run(scenario())

    log = EventLog(log_path)
    try:
        render_ids = _entity_book_ids(log, sid, log.get_events(sid))
        block = session_world_info_block(log, sid, "I raise the lantern.")
    finally:
        log.close()

    world_ids = [b["book_id"] for b in world["books"]]
    assert render_ids == world_ids == [replacement_id, world_book_id]
    # ...and the render really does carry both, so the ids are not vacuous.
    assert block is not None
    assert "REPLACEMENT lore line" in block and "WORLD lore line" in block


def test_render_and_world_route_agree_on_persona_books(build_app):
    """Differential case (d): the chat's assigned persona carries books.

    The original defect: ``GET /world`` resolved only the session + world
    sources, so it under-reported a persona's ``book_ids`` that the
    model-visible render (``context.facts._entity_book_ids``) already pooled.

    Both readers now share that helper, so the id lists compare exactly, in the
    render's order: the persona's books first, then the session's attached set.
    """
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            persona_book = await _create_book(
                client,
                name="Persona lore",
                entries=_lantern_entries("PERSONA lore line"),
            )
            session_book = await _create_book(
                client,
                name="Session lore",
                entries=_lantern_entries("SESSION lore line"),
            )
            sid = await _create_session(client)
            persona = (
                await client.post(
                    "/api/personas",
                    json={"name": "Rin", "book_ids": [persona_book["book_id"]]},
                )
            ).json()["persona"]
            assigned = await client.post(
                f"/api/sessions/{sid}/persona",
                json={"persona_id": persona["persona_id"]},
            )
            assert assigned.status_code == 200, assigned.text
            await client.post(
                f"/api/sessions/{sid}/books",
                json={"book_ids": [session_book["book_id"]]},
            )
            world = await client.get(f"/api/sessions/{sid}/world")
            assert world.status_code == 200
            return sid, persona_book["book_id"], session_book["book_id"], world.json()

    sid, persona_book_id, session_book_id, world = run(scenario())
    log = EventLog(log_path)
    try:
        render_ids = _entity_book_ids(log, sid, log.get_events(sid))
    finally:
        log.close()
    world_ids = [b["book_id"] for b in world["books"]]
    assert render_ids == world_ids == [persona_book_id, session_book_id]


def test_tombstoned_persona_books_drop_from_both_readers(build_app):
    """A persona tombstone drops its books from BOTH readers.

    The assigned persona's ``book_ids`` are a D31 source. Tombstoning the
    persona (``persona_deleted`` on the anchor) removes it from the persona fold,
    so both the model-visible render and ``GET /world`` must stop listing its
    books while the ``chat_persona`` assignment row survives (D9).
    """
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            persona_book = await _create_book(
                client,
                name="Persona lore",
                entries=_lantern_entries("PERSONA lore line"),
            )
            sid = await _create_session(client)
            persona = (
                await client.post(
                    "/api/personas",
                    json={"name": "Rin", "book_ids": [persona_book["book_id"]]},
                )
            ).json()["persona"]
            await client.post(
                f"/api/sessions/{sid}/persona",
                json={"persona_id": persona["persona_id"]},
            )
            before = await client.get(f"/api/sessions/{sid}/world")
            deleted = await client.delete(f"/api/personas/{persona['persona_id']}")
            after = await client.get(f"/api/sessions/{sid}/world")
            return sid, persona_book["book_id"], before.json(), deleted, after.json()

    sid, book_id, before, deleted, after = run(scenario())
    assert deleted.status_code == 200, deleted.text
    assert [b["book_id"] for b in before["books"]] == [book_id]
    assert after["books"] == []

    log = EventLog(log_path)
    try:
        render_ids = _entity_book_ids(log, sid, log.get_events(sid))
    finally:
        log.close()
    assert render_ids == []


def test_entry_signature_ignores_the_minted_key(build_log):
    """W9.23 §3.4.3: the stable key is addressing, never content.

    ``entry_signature`` is the D31 dedupe key (``backfill_books`` find-or-create,
    card materialization). It covers every field ``parse_book`` keeps, and the
    minted ``entry_id`` is deliberately NOT one of them: keying a book must not
    make it look like new lore.
    """
    log = build_log()
    append_book(log, {"book_id": "b1", "name": "Market lore", "entries": ENTRIES})
    (keyed,) = list_books(log)

    plain_a, plain_b = parse_book({"entries": _unkeyed(ENTRIES)})
    written_a, written_b = parse_book({"entries": keyed.entries})

    assert entry_signature(written_a) == entry_signature(plain_a)
    assert entry_signature(written_b) == entry_signature(plain_b)
    assert book_signature([written_a, written_b]) == book_signature([plain_a, plain_b])
