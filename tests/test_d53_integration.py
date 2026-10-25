"""W1 integration tests — the D53 layer wiring, end to end (PLAN §6 D53).

These cover the call sites that make "memory lives at the layer it is USED"
real:

* a fork/swipe CARRIES the chat's ``chat_actor`` owner record (repo whitelist) —
  exactly as ``chat_persona`` / ``world_ref`` are carried;
* ``POST /api/sessions`` writes ``chat_actor`` (the ACTOR) for an explicit
  ``persona_id`` and ``chat_persona`` (the USER persona) for the pinned-default
  path;
* a card import writes ``chat_actor`` for the imported card's persona;
* the memorizer WRITES ``scope.write_collection`` and READS
  ``scope.read_collections`` (world -> ``w_*`` + nested actor layers,
  single-actor -> ``a_*``, plain -> ``c_*``), and a live world-cast edit is
  reflected with no event backfill.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cards_api import router as cards_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.connections.registry import RegistryStore
from fabulist.log import EventLog
from fabulist.memory import indexer
from fabulist.memory.actor import append_chat_actor, fold_actors
from fabulist.memory.recall import RecallConfig, RecallResult
from fabulist.memory.scope import (
    LAYER_ACTOR,
    LAYER_CHAT,
    LAYER_WORLD,
    collection_for,
    collection_for_actor,
    collection_for_world,
    resolve_scope,
)
from fabulist.personas.registry import list_personas
from fabulist.wiring import make_memorizer
from fabulist.worlds.registry import append_world

VEC = [1.0, 0.0, 0.0, 0.0]

CARD_JSON = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Lyra Voss",
        "description": "A wandering cartographer.",
        "first_mes": "Lyra looks up from her maps. 'You lost?'",
        "scenario": "A rain-soaked night market.",
    },
}


#: Captured before any test patches ``httpx.AsyncClient`` on the shared module.
_RealAsyncClient = httpx.AsyncClient


def run(coro):
    return asyncio.run(coro)


def client_for(app) -> httpx.AsyncClient:
    return _RealAsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# -- api app fixture ---------------------------------------------------------


@pytest.fixture
def api(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build(**kwargs):
        app = create_app(FakeTurnEngine(), log_path, **kwargs)
        app.include_router(cards_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


# -- embedder recorder fixture ----------------------------------------------


@pytest.fixture
def recorder(monkeypatch):
    """Fake the embeddings endpoint; record the requests it receives."""

    calls: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        inputs = payload.get("input") or []
        calls.append({"model": payload.get("model"), "count": len(inputs)})
        body = {
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": VEC}
                for i in range(len(inputs))
            ],
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(indexer.httpx, "AsyncClient", fake_client)
    return calls


def _write_registry(path: Path) -> Path:
    data = {
        "connections": {"emb": {"base_url": "http://host:4445/v1", "model": "emb"}},
        "agents": {"embedder": "emb"},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- 1. fork carry -----------------------------------------------------------


def test_fork_carries_chat_actor_currently_assigned(log: EventLog):
    """The owner record assigned after the swiped turn rides the branch."""
    parent, branch = "base", "branch"
    user = log.append_event(parent, "user", "user", {"text": "I enter."})
    log.append_event(parent, "narrative", "narrator", {"prose": "Lanterns.", "segments": []})
    # The chat's owner is recorded AFTER the cut -> only the fork carry saves it.
    append_chat_actor(log, parent, ["a1"])

    log.fork_session(parent, at_seq=user.seq, new_session_id=branch)

    assert fold_actors(log, branch) == ("a1",)
    scope = resolve_scope(log, branch)
    assert scope.layer == LAYER_ACTOR
    assert scope.write_collection == collection_for_actor("a1")


def test_fork_does_not_duplicate_chat_actor_before_the_cut(log: EventLog):
    parent, branch = "base", "branch"
    append_chat_actor(log, parent, ["a1"])  # seq 1, before the cut
    user = log.append_event(parent, "user", "user", {"text": "I enter."})
    log.append_event(parent, "narrative", "narrator", {"prose": "Lanterns.", "segments": []})

    log.fork_session(parent, at_seq=user.seq, new_session_id=branch)

    own = [
        e
        for e in log.get_events(branch)
        if isinstance(e.payload, dict) and e.payload.get("event") == "chat_actor"
    ]
    assert own == []  # inherited through the chain, never copied
    assert fold_actors(log, branch) == ("a1",)


# -- 2. create_session: explicit actor vs pinned user persona ----------------


def test_create_session_explicit_persona_writes_chat_actor(api):
    build, _ = api
    app = build()

    async def scenario():
        async with client_for(app) as client:
            pid = (
                await client.post("/api/personas", json={"name": "Bob"})
            ).json()["persona"]["persona_id"]
            resp = await client.post("/api/sessions", json={"persona_id": pid})
            assert resp.status_code == 200, resp.text
            return pid, resp.json()["session_id"]

    pid, sid = run(scenario())
    log = app.state.log

    assert fold_actors(log, sid) == (pid,)
    scope = resolve_scope(log, sid)
    assert scope.layer == LAYER_ACTOR
    assert scope.actor_ids == (pid,)
    assert scope.write_collection == collection_for_actor(pid)

    # The persona is still seeded as the chat's card (unchanged behaviour)...
    seeds = [e for e in log.get_events(sid) if isinstance(e.payload.get("card"), dict)]
    assert seeds, "explicit persona chat must still seed the card"
    # ...but it is NOT the USER persona record.
    persona_rows = [
        e for e in log.get_events(sid) if e.payload.get("event") == "chat_persona"
    ]
    assert persona_rows == []


def test_create_session_pin_writes_chat_persona_as_user(api):
    build, _ = api
    app = build()

    async def scenario():
        async with client_for(app) as client:
            pid = (
                await client.post("/api/personas", json={"name": "Alice"})
            ).json()["persona"]["persona_id"]
            pin = await client.patch("/api/settings", json={"default_persona_id": pid})
            assert pin.status_code == 200, pin.text
            resp = await client.post("/api/sessions", json={"title": "Pinned"})
            return pid, resp.json()["session_id"]

    pid, sid = run(scenario())
    log = app.state.log

    persona_rows = [
        e.payload["persona_id"]
        for e in log.get_events(sid)
        if e.payload.get("event") == "chat_persona"
    ]
    assert persona_rows == [pid]
    # The pin is the USER persona, NOT a chat owner: no actor record, chat layer.
    assert fold_actors(log, sid) == ()
    scope = resolve_scope(log, sid)
    assert scope.layer == LAYER_CHAT
    assert scope.user_persona_id == pid


# -- 3. card import ----------------------------------------------------------


def test_card_import_seed_writes_chat_actor(api):
    build, _ = api
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/cards/import", json={**CARD_JSON, "seed": True})
            assert resp.status_code == 200, resp.text
            return resp.json()["session_id"]

    sid = run(scenario())
    log = app.state.log

    lyra = next(p for p in list_personas(log) if p.name == "Lyra Voss")
    assert fold_actors(log, sid) == (lyra.persona_id,)
    scope = resolve_scope(log, sid)
    assert scope.layer == LAYER_ACTOR
    assert scope.write_collection == collection_for_actor(lyra.persona_id)


# -- 4. memorizer reads/writes the resolved layers ---------------------------


def _seed_turn(log: EventLog, sid: str, text: str) -> None:
    log.append_event(sid, "user", "user", {"text": text})
    log.append_event(sid, "narrative", "narrator", {"prose": f"reply to {text}", "segments": []})


def test_memorizer_reads_and_writes_the_resolved_layers(tmp_path, recorder):
    config = _write_registry(tmp_path / "connections.json")
    log_path = tmp_path / "events.db"

    world_sid, actor_sid, plain_sid = "world-chat", "actor-chat", "plain-chat"
    with EventLog(log_path) as log:
        # A world whose cast lives on __worlds__ (D32 -> D53 live derivation).
        append_world(log, {"world_id": "w1", "name": "Rain-World", "cast": ["a1", "a2"]})
        log.append_event(
            world_sid, "system", "system",
            {"event": "world_ref", "world_id": "w1", "name": "Rain-World"},
        )
        _seed_turn(log, world_sid, "world memory alpha")

        append_chat_actor(log, actor_sid, ["a1"])
        _seed_turn(log, actor_sid, "actor memory beta")

        _seed_turn(log, plain_sid, "plain memory gamma")

    memorizer = make_memorizer(RegistryStore(config), log_path)
    assert memorizer is not None

    # -- writes land at each chat's OWN layer (D53 write rule) --
    run(memorizer.index_tail(world_sid))
    run(memorizer.index_tail(actor_sid))
    run(memorizer.index_tail(plain_sid))
    written = set(memorizer.store.collections())
    assert collection_for_world("w1") in written
    assert collection_for_actor("a1") in written
    assert collection_for(plain_sid) in written

    # -- reads span the chat's own layer + nested layers --
    world = run(memorizer.recall_fn(world_sid, "what happened?"))
    assert isinstance(world, RecallResult)
    assert world.collections == (
        collection_for_world("w1"),
        collection_for_actor("a1"),
        collection_for_actor("a2"),
    )
    assert world.layers == (LAYER_WORLD, LAYER_ACTOR, LAYER_ACTOR)
    texts = " | ".join(str(row.get("text", "")) for row in world.rows)
    assert "world memory alpha" in texts  # own world layer
    assert "actor memory beta" in texts  # nested actor layer

    # An actor-only chat does NOT reach into the world.
    actor = run(memorizer.recall_fn(actor_sid, "what happened?"))
    assert actor.collections == (collection_for_actor("a1"),)
    assert actor.layers == (LAYER_ACTOR,)
    actor_texts = " | ".join(str(row.get("text", "")) for row in actor.rows)
    assert "actor memory beta" in actor_texts
    assert "world memory alpha" not in actor_texts

    # A plain chat reads only itself.
    plain = run(memorizer.recall_fn(plain_sid, "what happened?"))
    assert plain.collections == (collection_for(plain_sid),)
    assert plain.layers == (LAYER_CHAT,)

    # search uses the same read set (multi-collection).
    rows = run(memorizer.search(world_sid, "what happened?"))
    found = " | ".join(str(row.get("text", "")) for row in rows)
    assert "actor memory beta" in found  # nested actor layer reached by search too

    # -- a live world-cast edit is reflected with NO event backfill (task 4) --
    with EventLog(log_path) as log:
        append_world(log, {"world_id": "w1", "name": "Rain-World", "cast": ["a1", "a2", "a3"]})
        scope = resolve_scope(log, world_sid)
    assert scope.read_collections == (
        collection_for_world("w1"),
        collection_for_actor("a1"),
        collection_for_actor("a2"),
        collection_for_actor("a3"),
    )

    assert recorder  # the embedder was actually called (not a fake-out)


def test_memorizer_passes_the_read_set_and_session_scoping(tmp_path, monkeypatch):
    """The memorizer hands the resolved read set + session scoping to memory."""
    import fabulist.memory.recall as recall_mod

    config = _write_registry(tmp_path / "connections.json")
    log_path = tmp_path / "events.db"
    with EventLog(log_path) as log:
        append_world(log, {"world_id": "w1", "name": "Rain", "cast": ["a1"]})
        log.append_event(
            "wchat", "system", "system",
            {"event": "world_ref", "world_id": "w1", "name": "Rain"},
        )
        _seed_turn(log, "wchat", "hi")

    captured: dict = {}

    async def fake_recall_block(store, endpoint, model, collections, user_text, **kw):
        captured["recall"] = (tuple(collections), kw.get("layers"), kw.get("exclude_session_id"))
        return RecallResult(
            block=None,
            queries=(),
            config=RecallConfig(),
            collections=tuple(collections),
            layers=tuple(kw.get("layers") or ()),
        )

    async def fake_gather(store, endpoint, model, collections, queries, **kw):
        captured["search"] = (tuple(collections), kw.get("exclude_session_id"))
        return []

    monkeypatch.setattr(recall_mod, "recall_block", fake_recall_block)
    monkeypatch.setattr(recall_mod, "gather_candidates", fake_gather)

    memorizer = make_memorizer(RegistryStore(config), log_path)
    assert memorizer is not None
    run(memorizer.recall_fn("wchat", "q"))
    run(memorizer.search("wchat", "q", k=3))

    read_set = (collection_for_world("w1"), collection_for_actor("a1"))
    assert captured["recall"][0] == read_set
    assert captured["recall"][1] == (LAYER_WORLD, LAYER_ACTOR)
    assert captured["recall"][2] == "wchat"
    assert captured["search"][0] == read_set
    assert captured["search"][1] == "wchat"


# -- 5. revision re-embed lands in the chat's OWN layer (task 6) --------------


def test_revision_reembed_lands_in_the_actors_own_layer(tmp_path, monkeypatch):
    from fabulist.api import revisions
    from fabulist.api.revisions import router as revisions_router
    from fabulist.memory.store import MemoryStore

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": VEC}],
                "model": "emb",
            },
        )

    transport = httpx.MockTransport(_handler)

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr(indexer.httpx, "AsyncClient", fake_client)

    config = _write_registry(tmp_path / "connections.json")
    log_path = tmp_path / "events.db"
    app = create_app(FakeTurnEngine(), log_path)
    app.include_router(revisions_router)
    app.state.registry = RegistryStore(config)
    try:
        log = app.state.log
        append_chat_actor(log, "s-actor", ["a1"])
        user = log.append_event("s-actor", "user", "user", {"text": "original text"})

        async def scenario():
            async with client_for(app) as client:
                resp = await client.post(
                    "/api/sessions/s-actor/revisions",
                    json={"target_id": user.id, "new_text": "revised text"},
                )
                assert resp.status_code == 200, resp.text
                # Drain the fire-and-forget reindex task (deterministic).
                while revisions._REINDEX_TASKS:
                    await asyncio.gather(*list(revisions._REINDEX_TASKS))

        run(scenario())

        with MemoryStore(tmp_path / "memory.db") as store:
            assert collection_for_actor("a1") in store.collections()
            rows = store.query(collection_for_actor("a1"), VEC, k=5)
        assert any("revised text" in str(r["text"]) for r in rows)
    finally:
        app.state.log.close()


# -- 6. inspector RAG panel surfaces the read layers (extends D25) ------------


def test_inspector_rag_panel_shows_the_layers_read(api):
    build, _ = api
    app = build()
    log = app.state.log

    append_world(log, {"world_id": "w1", "name": "Rain-World", "cast": ["a1"]})
    sid = "insp-world"
    log.append_event(
        sid, "system", "system",
        {"event": "world_ref", "world_id": "w1", "name": "Rain-World"},
    )
    _seed_turn(log, sid, "look around")

    async def scenario():
        async with client_for(app) as client:
            return (await client.get(f"/api/sessions/{sid}/inspect/turns")).json()

    body = run(scenario())
    rag = body["turns"][0]["rag"]
    # D53 extension: additive keys, existing RAG keys untouched.
    assert rag["collections"] == [
        collection_for_world("w1"),
        collection_for_actor("a1"),
    ]
    assert rag["layers"] == [LAYER_WORLD, LAYER_ACTOR]
    assert "query" in rag and "k" in rag and "results" in rag


def test_inspector_rag_panel_prefers_snapshot_collections(api):
    """A future engine-stamped read set wins over the live-resolved one."""
    build, _ = api
    app = build()
    log = app.state.log

    log.append_event("snap", "user", "user", {"text": "hi"})
    log.append_event(
        "snap", "narrative", "narrator", {"prose": "hello"},
        meta={"config": {
            "model": "m", "sampler": {}, "prompt_version": 1, "facts_hash": "h",
            "recall_collections": ["w_w1", "a_a1"],
            "recall_layers": ["world", "actor"],
        }},
    )

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/snap/inspect/turns")).json()

    rag = run(scenario())["turns"][0]["rag"]
    assert rag["collections"] == ["w_w1", "a_a1"]
    assert rag["layers"] == ["world", "actor"]
