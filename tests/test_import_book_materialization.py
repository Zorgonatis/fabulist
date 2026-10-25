"""Card-import lorebook materialization (D31 import-loss fix).

A card's ``character_book`` must become a reusable Book entity on the
``__books__`` catalog on BOTH import modes — persona (``?persona=1``) and seed
(``?seed=1``) — and the card's persona must reference it. This file pins the
materialization, its idempotence (signature dedupe + ``book_ids`` contains
check), the clean no-op for a bookless card, and verbatim preservation of
unknown SillyTavern entry keys.

Fixtures are synthetic. The real FastAPI app runs over a tmp SQLite log with
httpx's ASGI transport (no pytest-asyncio in this repo, so async scenarios run
via ``asyncio.run``) plus direct registry calls for the unit-level cases.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cards_api import router as cards_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.context.books import (
    BOOKS_ANCHOR,
    attach_book_to_persona,
    list_books,
    materialize_card_book,
)
from fabulist.log import EventLog
from fabulist.personas.registry import append_persona, list_personas

#: A synthetic card whose character_book carries unknown ST keys alongside the
#: canonical ones (they must survive verbatim).
CARD_WITH_BOOK = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Lyra Voss",
        "description": "A wandering cartographer.",
        "first_mes": "Lyra looks up from her maps.",
        "character_book": {
            "name": "Lyra's atlas",
            "entries": [
                {
                    "keys": ["atlas", "drowned road"],
                    "content": "The atlas marks a drowned road.",
                    "insertion_order": 1,
                    "comment": "atlas",
                    "vectorized": True,
                    "probability": 80,
                    "sticky": 3,
                }
            ],
        },
    },
}

#: A card with no character_book at all (the clean no-op).
CARD_NO_BOOK = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {"name": "Bookless", "description": "No lore here.", "first_mes": "Hi."},
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Real app over a tmp log; the cards router mounted, fake turn engine."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(cards_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


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


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _import(client: httpx.AsyncClient, payload: dict, query: str = ""):
    return await client.post(f"/api/cards/import{query}", json=payload)


# -- helper level ------------------------------------------------------------


def test_materialize_card_book_creates_book_named_from_character_book(build_log):
    log = build_log()
    card = CARD_WITH_BOOK["data"]
    book_id = materialize_card_book(log, card)
    assert book_id is not None
    (book,) = list_books(log)
    assert book.book_id == book_id
    assert book.name == "Lyra's atlas"
    assert book.provenance == {"origin": "card_import"}


def test_materialize_card_book_falls_back_to_card_name(build_log):
    log = build_log()
    card = {
        "name": "Nameless Lore",
        "character_book": {"entries": [{"keys": ["x"], "content": "body"}]},
    }
    materialize_card_book(log, card)
    (book,) = list_books(log)
    assert book.name == "Nameless Lore lore"


def test_materialize_card_book_is_idempotent_by_signature(build_log):
    log = build_log()
    card = CARD_WITH_BOOK["data"]
    first = materialize_card_book(log, card)
    books_before = len(log.get_events(BOOKS_ANCHOR))
    second = materialize_card_book(log, card)
    assert first == second
    assert len(list_books(log)) == 1
    # No new book row: the signature matched an existing entity.
    assert len(log.get_events(BOOKS_ANCHOR)) == books_before


@pytest.mark.parametrize(
    "card",
    [
        {"name": "No book"},
        {"name": "Null book", "character_book": None},
        {"name": "Bad book", "character_book": "not a mapping"},
        {"name": "Empty book", "character_book": {"entries": []}},
    ],
)
def test_materialize_card_book_noop_on_bookless_card(build_log, card):
    log = build_log()
    assert materialize_card_book(log, card) is None
    assert list_books(log) == []
    assert log.get_events(BOOKS_ANCHOR) == []


def test_materialize_preserves_unknown_st_keys_verbatim(build_log):
    log = build_log()
    materialize_card_book(log, CARD_WITH_BOOK["data"])
    (book,) = list_books(log)
    (entry,) = book.entries
    assert entry["keys"] == ["atlas", "drowned road"]
    assert entry["content"] == "The atlas marks a drowned road."
    assert entry["insertion_order"] == 1
    assert entry["comment"] == "atlas"
    assert entry["vectorized"] is True
    assert entry["probability"] == 80
    assert entry["sticky"] == 3


def test_attach_book_to_persona_is_idempotent(build_log):
    log = build_log()
    persona_id = append_persona(log, {"name": "Lyra Voss"})
    assert attach_book_to_persona(log, persona_id, "b1") is True
    (persona,) = list_personas(log)
    assert persona.book_ids == ["b1"]
    # Second attach: the contains check makes it a no-op.
    assert attach_book_to_persona(log, persona_id, "b1") is False
    (persona,) = list_personas(log)
    assert persona.book_ids == ["b1"]


def test_attach_book_to_persona_unknown_id_is_noop(build_log):
    log = build_log()
    assert attach_book_to_persona(log, "ghost", "b1") is False
    assert list_personas(log) == []


# -- persona mode (?persona=1) -----------------------------------------------


def test_persona_import_materializes_book_and_references_it(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await _import(client, CARD_WITH_BOOK, "?persona=1")

    resp = run(scenario())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "session_id" not in body

    log = EventLog(log_path)
    try:
        books = list_books(log)
        (persona,) = list_personas(log)
    finally:
        log.close()

    assert len(books) == 1
    assert books[0].name == "Lyra's atlas"
    assert persona.name == "Lyra Voss"
    assert persona.book_ids == [books[0].book_id]
    # The returned persona wire carries the reference too.
    assert body["persona"]["book_ids"] == [books[0].book_id]


def test_persona_reimport_creates_no_duplicate(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = await _import(client, CARD_WITH_BOOK, "?persona=1")
            second = await _import(client, CARD_WITH_BOOK, "?persona=1")
            return first, second

    first, second = run(scenario())
    assert first.status_code == 200 and second.status_code == 200
    assert (
        first.json()["persona"]["persona_id"]
        == second.json()["persona"]["persona_id"]
    )

    log = EventLog(log_path)
    try:
        books = list_books(log)
        (persona,) = list_personas(log)
    finally:
        log.close()

    # One Book entity, one folded persona, and no duplicate book id.
    assert len(books) == 1
    assert persona.book_ids == [books[0].book_id]


def test_persona_import_bookless_card_is_clean_noop(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await _import(client, CARD_NO_BOOK, "?persona=1")

    resp = run(scenario())
    assert resp.status_code == 200, resp.text

    log = EventLog(log_path)
    try:
        assert list_books(log) == []
        (persona,) = list_personas(log)
    finally:
        log.close()

    assert persona.name == "Bookless"
    assert persona.book_ids == []
    assert resp.json()["persona"]["book_ids"] == []


# -- seed mode (?seed=1) -----------------------------------------------------


def test_seed_import_materializes_book(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await _import(client, CARD_WITH_BOOK, "?seed=1")

    resp = run(scenario())
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_id"]

    log = EventLog(log_path)
    try:
        books = list_books(log)
        (persona,) = list_personas(log)
    finally:
        log.close()

    assert len(books) == 1
    assert books[0].name == "Lyra's atlas"
    # The seed path also attaches the materialized book to its created persona.
    assert persona.book_ids == [books[0].book_id]


def test_seed_import_bookless_card_is_clean_noop(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await _import(client, CARD_NO_BOOK, "?seed=1")

    resp = run(scenario())
    assert resp.status_code == 200, resp.text

    log = EventLog(log_path)
    try:
        assert list_books(log) == []
    finally:
        log.close()


def test_cross_mode_reimport_shares_one_materialized_book(build_app):
    """A persona import then a seed import produce one deduped Book entity."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            persona_resp = await _import(client, CARD_WITH_BOOK, "?persona=1")
            seed_resp = await _import(client, CARD_WITH_BOOK, "?seed=1")
            return persona_resp, seed_resp

    persona_resp, seed_resp = run(scenario())
    assert persona_resp.status_code == 200 and seed_resp.status_code == 200

    log = EventLog(log_path)
    try:
        books = list_books(log)
        assert len(books) == 1
        # The stored entity keeps its extension keys across both paths.
        (entry,) = books[0].entries
        assert entry["vectorized"] is True
    finally:
        log.close()
