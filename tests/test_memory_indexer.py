"""Tests for the memory indexer (W3.3, D5 rebuild).

The embeddings endpoint is faked with ``httpx.MockTransport`` so the *exact*
prefixed strings and multimodal content parts are asserted at the wire seam
(house convention from ``tests/test_memory_embedder.py``). The vector store runs
for real on ``tmp_path`` with fake vectors of matching dim 4.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fabulist.log import EventLog
from fabulist.memory import indexer
from fabulist.memory.embedder import EmbedderError
from fabulist.memory.indexer import (
    IndexerError,
    collection_for,
    doc_text,
    embed_inputs,
    index_media,
    index_turn,
    is_import_seed_text,
    query_text,
    rebuild,
    recall,
    resolve_writer,
)
from fabulist.memory.scope import MemoryScope
from fabulist.memory.store import MemoryStore

DIM = 4
VEC = [1.0, 0.0, 0.0, 0.0]


def run(coro):
    """Drive a coroutine to completion (suite has no pytest-asyncio)."""
    return asyncio.run(coro)


def _make_scope(write_collection, *, layer="chat", enabled=True):
    """Build a scope carrying the D53 frozen field names.

    Uses the real :class:`MemoryScope` once the U3 sibling reshape has landed;
    otherwise falls back to a stand-in with those exact fields so the write-path
    contract is testable independently of the scope lane.
    """
    fields = dict(
        layer=layer,
        write_collection=write_collection,
        read_collections=(write_collection,),
        world_id=None,
        actor_ids=(),
        user_persona_id=None,
        enabled=enabled,
    )
    try:
        return MemoryScope(**fields)
    except TypeError:
        from types import SimpleNamespace

        return SimpleNamespace(**fields)


def _ok_body(vectors):
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": vec}
            for i, vec in enumerate(vectors)
        ],
        "model": "fake",
    }


@pytest.fixture
def transport_factory(monkeypatch):
    """Install a MockTransport-backed ``httpx.AsyncClient`` factory for indexer."""

    def install(handler):
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def fake_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(indexer.httpx, "AsyncClient", fake_client)

    return install


# -- pure formatting -------------------------------------------------------


def test_doc_text_and_query_text_prefixes():
    assert doc_text("hello") == "title: none | text: hello"
    assert doc_text("hello", "Chapter 1") == "title: Chapter 1 | text: hello"
    assert query_text("a dragon") == "task: search result | query: a dragon"


def test_is_import_seed_text_recognises_only_the_seed_marker():
    assert is_import_seed_text("[Imported card: Epoch 27]")
    assert is_import_seed_text("[Imported card: Guild RPG]")
    assert not is_import_seed_text("hello")
    assert not is_import_seed_text("I open the door")
    assert not is_import_seed_text("")
    assert not is_import_seed_text(None)


def test_collection_for_sanitizes_and_caps():
    assert collection_for("abc-123") == "c_abc_123"
    assert collection_for("session/42") == "c_session_42"
    capped = collection_for("x" * 200)
    assert len(capped) == 64
    assert capped.startswith("c_")
    # A run of non-alphanumerics folds to a single underscore.
    assert collection_for("a---b") == "c_a_b"


# -- transport -------------------------------------------------------------


def test_embed_inputs_posts_strings_verbatim(transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        seen["content_type"] = request.headers.get("Content-Type")
        return httpx.Response(200, json=_ok_body([[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]))

    transport_factory(handler)
    out = run(embed_inputs("http://host/", "m", ["a", "b"]))

    assert seen["method"] == "POST"
    assert seen["url"] == "http://host/v1/embeddings"
    assert seen["body"] == {"model": "m", "input": ["a", "b"]}
    assert seen["content_type"] == "application/json"
    assert out == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]


def test_embed_inputs_posts_content_blocks_verbatim(transport_factory):
    seen: dict = {}
    item = {
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    out = run(embed_inputs("http://host", "m", [item]))

    assert seen["body"] == {"model": "m", "input": [item]}
    assert out == [VEC]


def test_embed_inputs_sends_bearer_when_keyed(transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    run(embed_inputs("http://host", "m", ["x"], api_key="secret"))
    assert seen["auth"] == "Bearer secret"


def test_embed_inputs_non_200_raises_embedder_error(transport_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model not loaded")

    transport_factory(handler)
    with pytest.raises(EmbedderError) as excinfo:
        run(embed_inputs("http://host", "m", ["x"]))

    assert excinfo.value.status_code == 503
    assert "model not loaded" in excinfo.value.body


# -- index_turn ------------------------------------------------------------


def test_index_turn_embeds_doc_prefix_and_upserts(tmp_path, transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        run(index_turn(store, "http://host", "m", "c_s1", 5, "user", "hello"))
        rows = store.query("c_s1", VEC, k=1)

    assert seen["body"]["input"] == ["title: none | text: hello"]
    assert rows[0]["seq"] == 5
    assert rows[0]["kind"] == "user"
    assert rows[0]["text"] == "hello"


def test_index_turn_truncates_at_6000_before_prefix(tmp_path, transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        run(index_turn(store, "http://host", "m", "c_s1", 1, "narrative", "x" * 7000))
        rows = store.query("c_s1", VEC, k=1)

    assert seen["body"]["input"] == ["title: none | text: " + "x" * 6000]
    assert len(rows[0]["text"]) == 6000


def test_index_turn_skips_card_import_seed(tmp_path, transport_factory):
    """A card-import seed is NOT a turn: never indexed as a ``[user]`` row.

    The seed rides as a synthetic ``user`` event (``cards.to_session_seed``) and
    was fabricated into recall as the player's own words. It must return
    :data:`INDEX_SKIPPED` without touching the embedder or the store.
    """
    from fabulist.memory.indexer import INDEX_SKIPPED

    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        result = run(
            index_turn(
                store, "http://host", "m", "c_s1", 2, "user",
                "[Imported card: Epoch 27]",
            )
        )
        rows = store.query("c_s1", VEC, k=10)

    assert result is INDEX_SKIPPED
    assert calls == []  # nothing reached the wire
    assert rows == []  # no fabricated row in the derived index


# -- index_media -----------------------------------------------------------


@pytest.mark.parametrize(
    "mime,data_b64,expected_part",
    [
        (
            "image/png",
            "AAAA",
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ),
        (
            "image/jpeg",
            "ZZZZ",
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,ZZZZ"}},
        ),
        (
            "audio/wav",
            "BBBB",
            {"type": "input_audio", "input_audio": {"data": "BBBB", "format": "wav"}},
        ),
        (
            "audio/mpeg",
            "CCCC",
            {"type": "input_audio", "input_audio": {"data": "CCCC", "format": "mpeg"}},
        ),
        (
            "audio/ogg",
            "DDDD",
            {"type": "input_audio", "input_audio": {"data": "DDDD", "format": "ogg"}},
        ),
        (
            "video/mp4",
            "EEEE",
            {"type": "input_video", "input_video": {"url": "data:video/mp4;base64,EEEE"}},
        ),
    ],
)
def test_index_media_content_part_shape(
    tmp_path, transport_factory, mime, data_b64, expected_part
):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        run(index_media(store, "http://host", "m", "c_s1", 3, "image", mime, data_b64))
        rows = store.query("c_s1", VEC, k=1)

    assert seen["body"]["input"] == [{"content": [expected_part]}]
    assert rows[0]["text"] == f"[image {mime}]"
    assert rows[0]["kind"] == "image"
    assert rows[0]["seq"] == 3


def test_index_media_wraps_embedder_error(tmp_path, transport_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        with pytest.raises(IndexerError) as excinfo:
            run(index_media(store, "http://host", "m", "c_s1", 1, "image", "image/png", "AAAA"))
    assert "boom" in str(excinfo.value)


def test_index_media_unsupported_mime_skips(tmp_path):
    """Unsupported media mimes SKIP (INDEX_SKIPPED), never raise.

    The index is derived (D5): the log keeps the asset row regardless, and a
    garbage/unembeddable mime must not kill the whole index_tail pass for its
    session on every turn (found by the live D16 re-index over fuzz artifacts).
    """
    from fabulist.memory.indexer import INDEX_SKIPPED

    with MemoryStore(tmp_path / "mem.db") as store:
        result = run(
            index_media(store, "http://host", "m", "c_s1", 1, "doc", "application/pdf", "AAAA")
        )
    assert result is INDEX_SKIPPED


# -- recall ----------------------------------------------------------------


def test_recall_query_prefixes_and_forwards_filters(tmp_path, transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body([VEC]))

    transport_factory(handler)
    with MemoryStore(tmp_path / "mem.db") as store:
        for seq in range(1, 6):
            store.upsert("c_s1", seq, "narrative", f"n{seq}", VEC)
        rows = run(
            recall(store, "http://host", "m", "c_s1", "the dragon", k=2, exclude_seqs={1})
        )

    assert seen["body"]["input"] == ["task: search result | query: the dragon"]
    seqs = [r["seq"] for r in rows]
    assert len(seqs) == 2
    assert 1 not in seqs


# -- rebuild ---------------------------------------------------------------


def _indexing_handler(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["input"])
        return httpx.Response(200, json=_ok_body([VEC for _ in body["input"]]))

    return handler


def test_rebuild_indexes_text_and_media_across_sessions(tmp_path, transport_factory):
    calls: list = []
    transport_factory(_indexing_handler(calls))

    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "user", "user", {"text": "hello"})
        log.append_event("s1", "narrative", "narrator", {"prose": "the dragon woke"})
        log.append_event(
            "s1",
            "asset",
            "media",
            {
                "asset_id": "a1",
                "kind": "image",
                "mime": "image/png",
                "bytes_b64": "AAAA",
                "sha256": "a1",
                "note": "",
            },
        )
        log.append_event("s2", "user", "user", {"text": "second session"})

        with MemoryStore(tmp_path / "mem.db") as store:
            report = run(rebuild(log, store, "http://host", "m"))
            s1 = store.query(collection_for("s1"), VEC, k=10)
            s2 = store.query(collection_for("s2"), VEC, k=10)
    finally:
        log.close()

    assert report == {"sessions": 2, "text_rows": 3, "media_rows": 1, "errors": []}
    assert {r["seq"] for r in s1} == {1, 2, 3}
    assert {r["kind"] for r in s1} == {"user", "narrative", "image"}
    media = next(r for r in s1 if r["kind"] == "image")
    assert media["text"] == "[image image/png]"
    assert [r["kind"] for r in s2] == ["user"]
    # The doc prefix and the media content part both reached the wire.
    flat = [item for call in calls for item in call]
    assert "title: none | text: hello" in flat
    assert "title: none | text: the dragon woke" in flat
    assert {"content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]} in flat


def test_rebuild_skips_card_import_seed(tmp_path, transport_factory):
    """A rebuild drops the fabricated ``[Imported card: ...]`` user rows.

    The D52 repair path (``POST /api/memory/reembed`` -> ``reembed_all``) funnels
    every ``user`` event through :func:`index_turn`; the seed must never be
    re-embedded, so an existing junk row does not survive a rebuild.
    """
    calls: list = []
    transport_factory(_indexing_handler(calls))

    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "user", "user", {"text": "[Imported card: Epoch 27]"})
        log.append_event("s1", "user", "user", {"text": "hi"})

        with MemoryStore(tmp_path / "mem.db") as store:
            report = run(rebuild(log, store, "http://host", "m"))
            rows = store.query(collection_for("s1"), VEC, k=10)
    finally:
        log.close()

    assert report["text_rows"] == 1
    assert [r["text"] for r in rows] == ["hi"]
    flat = [item for call in calls for item in call]
    assert "title: none | text: hi" in flat
    assert not any("Imported card" in str(item) for item in flat)


def test_rebuild_skips_ephemeral_events(tmp_path, transport_factory):
    transport_factory(_indexing_handler([]))

    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "user", "user", {"text": "kept"})
        log.append_event("s1", "user", "user", {"text": "abandoned"}, ephemeral=True)

        with MemoryStore(tmp_path / "mem.db") as store:
            report = run(rebuild(log, store, "http://host", "m"))
            rows = store.query(collection_for("s1"), VEC, k=10)
    finally:
        log.close()

    assert report["text_rows"] == 1
    assert [r["text"] for r in rows] == ["kept"]


def test_rebuild_skips_reserved_anchor_sessions(tmp_path, transport_factory):
    transport_factory(_indexing_handler([]))

    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("__settings__", "system", "user", {"event": "settings"})
        log.append_event("s1", "user", "user", {"text": "kept"})

        with MemoryStore(tmp_path / "mem.db") as store:
            report = run(rebuild(log, store, "http://host", "m"))
            collections = store.collections()
    finally:
        log.close()

    assert report["sessions"] == 1
    assert report["text_rows"] == 1
    assert collections == [collection_for("s1")]


def test_rebuild_collects_per_row_errors(tmp_path, transport_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport_factory(handler)

    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "user", "user", {"text": "one"})
        log.append_event("s1", "narrative", "narrator", {"prose": "two"})

        with MemoryStore(tmp_path / "mem.db") as store:
            report = run(rebuild(log, store, "http://host", "m"))
    finally:
        log.close()

    assert report["text_rows"] == 0
    assert len(report["errors"]) == 2
    assert all("s1#" in err for err in report["errors"])


def test_rebuild_writes_layer_collections_from_scope(
    tmp_path, transport_factory, monkeypatch
):
    """``rebuild`` writes each session's RESOLVED write collection (D53).

    The collection id comes straight from the scope (``write_collection``) and is
    never recomputed from the session id: a world chat lands in ``w_*``, a
    single-actor chat in ``a_*`` and a plain chat in ``c_*``.
    """
    transport_factory(_indexing_handler([]))

    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s-world", "user", "user", {"text": "w"})
        log.append_event("s-actor", "user", "user", {"text": "a"})
        log.append_event("s-chat", "user", "user", {"text": "c"})

        scopes = {
            "s-world": ("world", "w_w1"),
            "s-actor": ("actor", "a_alice"),
            "s-chat": ("chat", "c_s_chat"),
        }
        monkeypatch.setattr(
            indexer,
            "_resolve_scope",
            lambda lg, sid: _make_scope(scopes[sid][1], layer=scopes[sid][0]),
        )

        with MemoryStore(tmp_path / "mem.db") as store:
            report = run(rebuild(log, store, "http://host", "m"))
            collections = set(store.collections())
    finally:
        log.close()

    assert report["text_rows"] == 3
    assert collections == {"w_w1", "a_alice", "c_s_chat"}


# -- writer stamp ----------------------------------------------------------


def _persona(log: EventLog, name: str) -> str:
    from fabulist.personas.registry import append_persona

    return append_persona(log, {"name": name})


def _clear_persona(log: EventLog, session_id: str) -> None:
    log.append_event(
        session_id,
        "system",
        "user",
        {"event": "chat_persona", "persona_id": None, "cleared": True},
    )


def _user_seqs(log: EventLog, session_id: str) -> dict[str, int]:
    return {
        e.payload.get("text"): e.seq
        for e in log.get_events(session_id)
        if e.type == "user"
    }


def test_resolve_writer_tracks_persona_in_effect_at_each_seq(tmp_path):
    """A row is stamped with the persona in effect at ITS OWN seq."""
    log = EventLog(tmp_path / "events.db")
    try:
        alice = _persona(log, "Alice")
        bob = _persona(log, "Bob")
        sid = "s1"
        log.append_event(sid, "user", "user", {"text": "one"})
        log.append_event(sid, "system", "user", {"event": "chat_persona", "persona_id": alice})
        log.append_event(sid, "user", "user", {"text": "two"})
        log.append_event(sid, "system", "user", {"event": "chat_persona", "persona_id": bob})
        log.append_event(sid, "user", "user", {"text": "three"})

        seqs = _user_seqs(log, sid)
        assert resolve_writer(log, sid, seqs["one"]) == ""  # no assignment yet, no pin
        assert resolve_writer(log, sid, seqs["two"]) == "Alice"
        assert resolve_writer(log, sid, seqs["three"]) == "Bob"
    finally:
        log.close()


def test_resolve_writer_explicit_clear_is_empty(tmp_path):
    """An explicit clear suppresses the writer and the pinned default."""
    log = EventLog(tmp_path / "events.db")
    try:
        alice = _persona(log, "Alice")
        log.append_event(
            "__settings__",
            "system",
            "user",
            {"event": "default_persona_changed", "persona_id": alice},
        )
        sid = "s1"
        log.append_event(sid, "system", "user", {"event": "chat_persona", "persona_id": alice})
        _clear_persona(log, sid)
        seq = log.get_events(sid)[-1].seq + 1
        assert resolve_writer(log, sid, seq) == ""
    finally:
        log.close()


def test_resolve_writer_falls_back_to_pinned_default(tmp_path):
    """A chat that never recorded a choice inherits the pinned default."""
    log = EventLog(tmp_path / "events.db")
    try:
        alice = _persona(log, "Alice")
        log.append_event(
            "__settings__",
            "system",
            "user",
            {"event": "default_persona_changed", "persona_id": alice},
        )
        sid = "s1"
        log.append_event(sid, "user", "user", {"text": "hi"})
        seq = _user_seqs(log, sid)["hi"]
        assert resolve_writer(log, sid, seq) == "Alice"
    finally:
        log.close()


def test_resolve_writer_inherits_parent_chain_across_fork(tmp_path):
    """A branch inherits its parent's assignment (fork-aware chain read)."""
    log = EventLog(tmp_path / "events.db")
    try:
        alice = _persona(log, "Alice")
        parent = "s1"
        log.append_event(parent, "system", "user", {"event": "chat_persona", "persona_id": alice})
        log.append_event(parent, "user", "user", {"text": "hi"})
        child = log.fork_session(parent, log.get_events(parent)[-1].seq, "s2")
        log.append_event(child, "user", "user", {"text": "branch"})
        after = _user_seqs(log, child)["branch"]
        assert resolve_writer(log, child, after) == "Alice"
    finally:
        log.close()


def test_index_turn_stamps_writer_on_user_rows_only(tmp_path, transport_factory):
    transport_factory(_indexing_handler([]))
    log = EventLog(tmp_path / "events.db")
    try:
        alice = _persona(log, "Alice")
        sid = "s1"
        log.append_event(sid, "system", "user", {"event": "chat_persona", "persona_id": alice})
        log.append_event(sid, "user", "user", {"text": "hello"})
        log.append_event(sid, "narrative", "narrator", {"prose": "prose"})
        seqs = _user_seqs(log, sid)
        nseq = next(e.seq for e in log.get_events(sid) if e.type == "narrative")
        with MemoryStore(tmp_path / "mem.db") as store:
            run(
                index_turn(
                    store, "http://host", "m", "c_s1", seqs["hello"], "user", "hello",
                    session_id=sid, log=log,
                )
            )
            run(
                index_turn(
                    store, "http://host", "m", "c_s1", nseq, "narrative", "prose",
                    session_id=sid, log=log,
                )
            )
            rows = {r["kind"]: r for r in store.query("c_s1", VEC, k=10)}
        assert rows["user"]["writer"] == "Alice"
        assert rows["narrative"]["writer"] == ""
    finally:
        log.close()


def test_rebuild_stamps_writer_from_log(tmp_path, transport_factory):
    transport_factory(_indexing_handler([]))
    log = EventLog(tmp_path / "events.db")
    try:
        alice = _persona(log, "Alice")
        log.append_event("s1", "system", "user", {"event": "chat_persona", "persona_id": alice})
        log.append_event("s1", "user", "user", {"text": "hello"})
        log.append_event("s1", "narrative", "narrator", {"prose": "prose"})
        with MemoryStore(tmp_path / "mem.db") as store:
            run(rebuild(log, store, "http://host", "m"))
            rows = {r["kind"]: r for r in store.query(collection_for("s1"), VEC, k=10)}
        assert rows["user"]["writer"] == "Alice"
        assert rows["narrative"]["writer"] == ""
    finally:
        log.close()


def test_store_round_trips_writer(tmp_path):
    with MemoryStore(tmp_path / "mem.db") as store:
        store.upsert("c_s1", 1, "user", "hi", VEC, session_id="s1", writer="Alice")
        rows = store.query("c_s1", VEC, k=1)
    assert rows[0]["writer"] == "Alice"


def test_store_migration_adds_writer_column_in_place(tmp_path):
    """An older v2 meta (no ``writer``) gains the column without a drop."""
    import sqlite3

    db = tmp_path / "mem.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memory_meta ("
        "collection TEXT NOT NULL, row_key INTEGER NOT NULL, "
        "session_id TEXT NOT NULL DEFAULT '', seq INTEGER NOT NULL, "
        "kind TEXT NOT NULL, text TEXT NOT NULL, ts TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (collection, row_key))"
    )
    conn.execute(
        "INSERT INTO memory_meta (collection,row_key,session_id,seq,kind,text,ts) "
        "VALUES ('c_s1',1,'s1',1,'user','hi','')"
    )
    conn.commit()
    conn.close()

    with MemoryStore(db) as store:
        cols = {
            r[1] for r in store._conn.execute("PRAGMA table_info(memory_meta)").fetchall()
        }
        assert "writer" in cols
        preserved = store._conn.execute(
            "SELECT text, writer FROM memory_meta WHERE row_key = 1"
        ).fetchone()
    assert preserved == ("hi", "")
