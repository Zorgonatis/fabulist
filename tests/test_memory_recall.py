"""Tests for the recall prompt-block renderer + recall policy (W3.2, D25 C2/C4)."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from fabulist.memory import indexer, rerank
from fabulist.memory.recall import (
    DEFAULT_BLOCK_CHARS,
    DEFAULT_OVERFETCH,
    FRAMING,
    HEADER,
    RecallConfig,
    RecallResult,
    _coerce_int,
    build_memory_block,
    gather_candidates,
    layer_labels,
    queries_for,
    recall_block,
    scene_query_from_parts,
    visible_turn_seqs,
)
from fabulist.memory.store import MemoryStore

DIM = 4

def run(coro):
    return asyncio.run(coro)


def row(seq, kind, text, distance=None):
    return {"seq": seq, "kind": kind, "text": text, "distance": distance}


def test_empty_rows_returns_none():
    assert build_memory_block([]) is None


def test_ordering_and_unknown_user_row_is_not_attributed_to_the_user():
    # Rows with no collection / own-session info (legacy callers): a narrative row
    # keeps the plain role tag, but a user row with no recorded WRITER is never
    # attributed to the current user (D55) — it says so plainly instead.
    rows = [
        row(10, "narrative", "the tower fell"),
        row(4, "user", "I draw my sword"),
    ]
    block = build_memory_block(rows)
    assert block is not None
    lines = block.splitlines()
    assert lines[0] == HEADER
    assert FRAMING in block
    bullets = [line for line in lines if line.startswith("- ")]
    assert bullets == [
        "- [narrative] the tower fell",
        "- [another player] I draw my sword",
    ]


def test_k_cap():
    rows = [row(i, "narrative", f"line {i}") for i in range(10)]
    block = build_memory_block(rows, k=3)
    assert block is not None
    body = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(body) == 3
    assert body == [
        "- [narrative] line 0",
        "- [narrative] line 1",
        "- [narrative] line 2",
    ]


def test_dedupe_exact_and_whitespace_equivalent():
    rows = [
        row(1, "narrative", "the  dragon\twakes"),
        row(2, "narrative", "the dragon wakes"),  # near-identical (collapses equal)
        row(3, "narrative", "a different beat"),
    ]
    block = build_memory_block(rows)
    assert block is not None
    body = [line for line in block.splitlines() if line.startswith("- ")]
    assert body == [
        "- [narrative] the dragon wakes",
        "- [narrative] a different beat",
    ]


def test_char_cap_limits_bullets():
    rows = [row(i, "narrative", "x" * 60) for i in range(10)]
    block = build_memory_block(rows, k=10, max_chars=700)
    assert block is not None
    assert len(block) <= 700
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert 1 <= len(bullets) < 10


def test_tiny_char_cap_degrades_without_overflow():
    rows = [row(i, "narrative", "x" * 60) for i in range(10)]
    block = build_memory_block(rows, k=10, max_chars=130)
    assert block is not None
    assert len(block) <= 130
    assert not [line for line in block.splitlines() if line.startswith("- ")]


def test_bullet_text_truncated_to_limit():
    rows = [row(7, "narrative", "y" * 900)]
    block = build_memory_block(rows)
    assert block is not None
    bullet = [line for line in block.splitlines() if line.startswith("- ")][0]
    # text portion is capped at the per-entry limit (clip trims then adds …)
    assert bullet == "- [narrative] " + ("y" * 600) + "…"
    assert len(bullet) - len("- [narrative] ") == 601


def test_visible_turn_seqs_covers_only_the_trailing_turns():
    projection = {
        "user_messages": [{"seq": seq} for seq in (1, 3, 5, 7)],
        "narratives": [{"seq": seq} for seq in (2, 4, 6, 8)],
    }
    # The last two narrative turns (6, 8); older turns (2, 4) stay recallable.
    assert visible_turn_seqs(projection) == {6, 8}


def test_visible_turn_seqs_empty_when_nothing_is_visible():
    assert visible_turn_seqs({}) == set()
    assert visible_turn_seqs({"user_messages": [{"seq": 1}]}) == set()
    assert visible_turn_seqs({"narratives": [{"seq": 2}]}, turns=0) == set()
    # A chat's single committed turn is the whole visible window.
    assert visible_turn_seqs({"narratives": [{"seq": 2}]}) == {2}


# -- D25 C2: RecallConfig coercion -----------------------------------------


def test_recall_config_defaults():
    cfg = RecallConfig()
    assert cfg.as_dict() == {
        "enabled": True,
        "k": 6,
        "block_chars": DEFAULT_BLOCK_CHARS,
        "rerank_enabled": True,
        "query_mode": "message",
        "overfetch": DEFAULT_OVERFETCH,
    }


def test_recall_config_from_payload_clamps_and_ignores_bad_types():
    cfg = RecallConfig.from_payload(
        {"k": 999, "block_chars": 1, "overfetch": 0, "query_mode": "nope",
         "rerank_enabled": "yes", "enabled": "x"}
    )
    assert cfg.k == 50
    assert cfg.block_chars == 100
    assert cfg.overfetch == 1
    assert cfg.query_mode == "message"  # unknown mode falls back
    assert cfg.rerank_enabled is True  # non-bool ignored; default stands
    assert cfg.enabled is True


def test_recall_config_partial_payload_keeps_base():
    base = RecallConfig.from_payload({"k": 9, "rerank_enabled": True})
    merged = RecallConfig.from_payload({"query_mode": "message+scene"}, base=base)
    assert merged.k == 9
    assert merged.rerank_enabled is True
    assert merged.query_mode == "message+scene"


def test_with_knobs_rejects_unknown():
    with pytest.raises(ValueError):
        RecallConfig().with_knobs(bogus=1)


def test_coerce_int_rejects_bool():
    assert _coerce_int(True, minimum=1, maximum=5, default=3) == 3


# -- D25 C4: query construction -------------------------------------------


def test_queries_for_message_is_just_the_user_message():
    assert queries_for("find the cloak", query_mode="message") == ["find the cloak"]


def test_queries_for_message_plus_scene_appends_scene():
    assert queries_for(
        "find the cloak", scene_query="scene with Alice | at market",
        query_mode="message+scene",
    ) == ["find the cloak", "scene with Alice | at market"]


def test_queries_for_drops_blank_scene():
    assert queries_for("hi", scene_query="  ", query_mode="message+scene") == ["hi"]


def test_scene_query_from_parts():
    assert scene_query_from_parts() is None
    assert scene_query_from_parts(persona_name="Alice", location="market") == (
        "scene with Alice | at market"
    )
    assert scene_query_from_parts(cast=["a", "b", "c", "d", "e"]) == (
        "cast: a, b, c, d"
    )


# -- retrieval policy over a real store + faked embed/rerank transports ---


def _seed_store(store: MemoryStore) -> None:
    store.upsert("c_s", 1, "narrative", "one", [1.0, 0.0, 0.0, 0.0])
    store.upsert("c_s", 2, "narrative", "two", [0.9, 0.1, 0.0, 0.0])
    store.upsert("c_s", 3, "narrative", "three", [0.0, 1.0, 0.0, 0.0])


def _embed_stub(monkeypatch, vectors: dict[int, list[float]]):
    """Fake the embeddings transport: map input index -> a vector."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        inputs = payload["input"]
        data = [
            {"object": "embedding", "index": i, "embedding": vectors[i]}
            for i in range(len(inputs))
        ]
        return httpx.Response(200, json={"object": "list", "data": data})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(indexer.httpx, "AsyncClient", fake_client)


def _rerank_fn_stub(monkeypatch, results, *, fail=False):
    """Patch ``rerank.rerank_documents`` (avoids the shared httpx-module seam)."""

    async def stub(endpoint, model, query, documents, *, top_n=None, api_key=None, timeout_s=30.0):
        if fail:
            raise rerank.RerankError(500, "nope", endpoint)
        return list(results)

    monkeypatch.setattr(rerank, "rerank_documents", stub)


def test_gather_candidates_unions_and_dedupes_by_seq(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    _seed_store(store)
    # two queries -> query A pulls 1,2,3; query B pulls 3 (see vectors above)
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0], 1: [0.0, 1.0, 0.0, 0.0]})
    rows = run(
        gather_candidates(
            store, "http://e", "m", "c_s", ["qa", "qb"], fetch_k=3
        )
    )
    seqs = [r["seq"] for r in rows]
    assert set(seqs) == {1, 2, 3}  # unioned
    assert len(seqs) == len(set(seqs))  # deduped by seq
    # distances ascending
    assert [r["distance"] for r in rows] == sorted(r["distance"] for r in rows)
    store.close()


def test_recall_block_rerank_off_uses_vector_order(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    _seed_store(store)
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})
    result = run(
        recall_block(
            store, "http://e", "m", "c_s", "one",
            config=RecallConfig(k=2, rerank_enabled=False),
        )
    )
    assert result.queries == ("one",)
    assert result.config.rerank_enabled is False
    assert "one" in result.block and "two" in result.block
    assert "three" not in result.block
    store.close()


def test_recall_block_rerank_on_reorders(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    _seed_store(store)
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})
    # candidates in vector order [seq1, seq2, seq3]; rerank favors 3 then 2
    _rerank_fn_stub(
        monkeypatch,
        [
            {"index": 2, "score": 0.9},
            {"index": 1, "score": 0.5},
            {"index": 0, "score": 0.1},
        ],
    )
    result = run(
        recall_block(
            store, "http://e", "m", "c_s", "one",
            config=RecallConfig(k=2, rerank_enabled=True, overfetch=3),
            rerank_endpoint="http://r", rerank_model="qwen3-reranker",
        )
    )
    lines = [line for line in result.block.splitlines() if line.startswith("- ")]
    assert "three" in lines[0]
    assert "two" in lines[1]
    assert "rerank_score" in result.rows[0]
    store.close()


def test_recall_block_rerank_failure_falls_back_to_vector_order(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    _seed_store(store)
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})
    _rerank_fn_stub(monkeypatch, [], fail=True)
    result = run(
        recall_block(
            store, "http://e", "m", "c_s", "one",
            config=RecallConfig(k=2, rerank_enabled=True, overfetch=3),
            rerank_endpoint="http://r", rerank_model="qwen3-reranker",
        )
    )
    lines = [line for line in result.block.splitlines() if line.startswith("- ")]
    assert "one" in lines[0]  # vector order preserved
    assert "two" in lines[1]
    store.close()


def test_recall_block_query_mode_message_plus_scene_embeds_both(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    _seed_store(store)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        calls.append(len(payload["input"]))
        data = [
            {"object": "embedding", "index": i, "embedding": [1.0, 0.0, 0.0, 0.0]}
            for i in range(len(payload["input"]))
        ]
        return httpx.Response(200, json={"object": "list", "data": data})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        indexer.httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": transport}),
    )
    result = run(
        recall_block(
            store, "http://e", "m", "c_s", "one",
            config=RecallConfig(query_mode="message+scene"),
            scene_query="scene with Alice | at market",
        )
    )
    assert calls == [2]  # both queries embedded in ONE request
    assert result.queries == ("one", "scene with Alice | at market")
    store.close()


def test_recall_block_trim_skips_visible_turns_and_keeps_older_rows(tmp_path, monkeypatch):
    """#84: the wired exclusion drops the visible tail; older rows still return."""
    store = MemoryStore(tmp_path / "m.db")
    for seq in range(1, 9):
        store.upsert(
            "c_s", seq, "narrative", f"turn-{seq}", [1.0, 0.0, 0.0, 0.0],
            session_id="chatA",
        )
    # Another chat's row collides with a visible seq: session scoping keeps it.
    store.upsert(
        "c_s", 6, "narrative", "other-chat-6", [1.0, 0.0, 0.0, 0.0],
        session_id="chatB",
    )
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})
    # The narrator already carries the last two turns (narratives 6 and 8).
    projection = {
        "user_messages": [{"seq": seq} for seq in (5, 7)],
        "narratives": [{"seq": seq} for seq in (6, 8)],
    }
    result = run(
        recall_block(
            store, "http://e", "m", "c_s", "anything",
            config=RecallConfig(k=10, rerank_enabled=False),
            exclude_seqs=visible_turn_seqs(projection),
            exclude_session_id="chatA",
        )
    )
    texts = " | ".join(str(row["text"]) for row in result.rows)
    for seq in (6, 8):
        assert f"turn-{seq}" not in texts  # prose already visible in the prompt
    for seq in (1, 2, 3, 4, 5, 7):
        assert f"turn-{seq}" in texts  # older rows still recallable
    assert "other-chat-6" in texts  # D53: a shared layer keeps another chat's seq
    store.close()


# -- D53: multi-collection retrieval + per-chat seq collision (U2/U5) ------


def test_as_collections_normalizes_and_dedupes():
    from fabulist.memory.recall import _as_collections

    assert _as_collections("c_s") == ["c_s"]  # legacy bare id
    assert _as_collections(["w_1", "a_1", "w_1", "", None]) == ["w_1", "a_1"]
    assert _as_collections(None) == []


def test_gather_candidates_two_sessions_same_seq_both_survive(tmp_path, monkeypatch):
    """D53 bug: one collection, two chats, colliding seq 3 — both must survive."""
    store = MemoryStore(tmp_path / "m.db")
    store.upsert("c_shared", 3, "narrative", "A-three", [1.0, 0, 0, 0], session_id="chatA")
    store.upsert("c_shared", 3, "narrative", "B-three", [1.0, 0, 0, 0], session_id="chatB")
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})

    rows = run(
        gather_candidates(store, "http://e", "m", "c_shared", ["q"], fetch_k=10)
    )
    assert sorted(r["text"] for r in rows) == ["A-three", "B-three"]
    assert {r["seq"] for r in rows} == {3}
    assert {r["session_id"] for r in rows} == {"chatA", "chatB"}
    store.close()


def test_gather_candidates_exclude_seqs_is_session_scoped(tmp_path, monkeypatch):
    """D53 bug: excluding seq 5 for chat A must not drop chat B's seq 5."""
    store = MemoryStore(tmp_path / "m.db")
    for seq in (1, 2, 3, 4, 5):
        store.upsert(
            "p_shared", seq, "narrative", f"A-{seq}", [1.0, 0, 0, 0], session_id="chatA"
        )
        store.upsert(
            "p_shared", seq, "narrative", f"B-{seq}", [1.0, 0, 0, 0], session_id="chatB"
        )
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})

    rows = run(
        gather_candidates(
            store,
            "http://e",
            "m",
            "p_shared",
            ["q"],
            fetch_k=10,
            exclude_seqs={5},
            exclude_session_id="chatA",
        )
    )
    texts = {r["text"] for r in rows}
    assert "A-5" not in texts  # recalling chat's own seq excluded
    assert "B-5" in texts  # other chat's same-seq row survives
    store.close()


def test_gather_candidates_multi_collection_union_sorts_globally(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    store.upsert("w_world", 1, "narrative", "world-near", [1.0, 0, 0, 0], session_id="chatW")
    store.upsert("w_world", 2, "narrative", "world-far", [0.0, 1, 0, 0], session_id="chatW")
    store.upsert("a_actor", 1, "narrative", "actor-mid", [0.9, 0.1, 0, 0], session_id="chatA")
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})

    rows = run(
        gather_candidates(
            store, "http://e", "m", ["w_world", "a_actor"], ["q"], fetch_k=10
        )
    )
    texts = [r["text"] for r in rows]
    assert "world-near" in texts and "actor-mid" in texts  # both collections read
    assert texts[0] == "world-near"  # global best-first
    distances = [r["distance"] for r in rows]
    assert distances == sorted(distances)
    store.close()


def test_gather_candidates_skips_dim_mismatched_collection(tmp_path, monkeypatch, caplog):
    """U5: a stale-dim collection is skipped with a log line, never raising."""
    store = MemoryStore(tmp_path / "m.db")
    store.upsert("c_good", 1, "narrative", "good", [1.0, 0, 0, 0], session_id="chatG")
    store.upsert(
        "c_bad", 1, "narrative", "bad", [0.5] * 8, session_id="chatB"
    )
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})

    with caplog.at_level(logging.WARNING, logger="fabulist.memory.store"):
        rows = run(
            gather_candidates(
                store, "http://e", "m", ["c_bad", "c_good"], ["q"], fetch_k=10
            )
        )
    assert [r["text"] for r in rows] == ["good"]  # sibling still returns
    assert "c_bad" in caplog.text and "skipping" in caplog.text
    store.close()


def test_gather_candidates_single_collection_path_unchanged(tmp_path, monkeypatch):
    """The single-collection/single-session live path is byte-identical."""
    store = MemoryStore(tmp_path / "m.db")
    _seed_store(store)
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})

    rows = run(gather_candidates(store, "http://e", "m", "c_s", ["qa"], fetch_k=2))
    direct = store.query("c_s", [1.0, 0.0, 0.0, 0.0], k=2)
    assert [r["seq"] for r in rows] == [r["seq"] for r in direct]
    assert [r["text"] for r in rows] == [r["text"] for r in direct]
    store.close()


def test_recall_result_reports_collections_and_layers(tmp_path, monkeypatch):
    """D25/D53: the inspector can show WHAT was read."""
    store = MemoryStore(tmp_path / "m.db")
    _seed_store(store)
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})

    result = run(
        recall_block(
            store,
            "http://e",
            "m",
            ["w_world", "a_alice", "c_s"],
            "one",
            config=RecallConfig(k=2),
            layers=("world", "actor", "chat"),
        )
    )
    assert isinstance(result, RecallResult)
    assert result.collections == ("w_world", "a_alice", "c_s")
    assert result.layers == ("world", "actor", "chat")
    store.close()


def test_recall_block_bare_collection_string_still_accepted(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    _seed_store(store)
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})

    result = run(
        recall_block(store, "http://e", "m", "c_s", "one", config=RecallConfig(k=1))
    )
    assert result.collections == ("c_s",)
    assert result.layers == ()
    assert "one" in result.block
    store.close()


# -- D53 per-entry provenance (owner-reported defect) ----------------------


def test_user_row_is_tagged_by_writer_not_layer():
    """D55: a user row belongs to the PERSONA who wrote it, never to the layer.

    A Forest-world chat reads Tessa's actor layer, whose rows were written in
    OTHER chats. The layer says where a row is stored, not who spoke: a row the
    player themselves wrote while a different persona was active must not be
    handed back as this user's memory, and one written under the player's own
    persona must read as theirs even from another scene.
    """
    rows = [
        {
            "seq": 133,
            "kind": "user",
            "writer": "Tessa",
            "text": "The stream is off, the house is quiet, and I lower myself onto the sofa.",
            "session_id": "somewhere-else",
            "collection": "a_tessa",
        },
        {
            "seq": 27,
            "kind": "user",
            "writer": "Wanderer",
            "text": "I settle onto the hammock and think about how quiet this place is.",
            "session_id": "another-chat",
            "collection": "w_forest",
        },
        {
            "seq": 31,
            "kind": "user",
            "text": "a line whose writer was never recorded",
            "session_id": "anonymous-chat",
            "collection": "w_forest",
        },
        {
            "seq": 12,
            "kind": "narrative",
            "text": "the glade is quiet",
            "session_id": "another-chat",
            "collection": "w_forest",
        },
    ]
    block = build_memory_block(
        rows, labels={"a_tessa": "Tessa", "w_forest": "Forest"}
    )
    assert block is not None
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert bullets == [
        "- [Tessa] The stream is off, the house is quiet, and I lower myself onto the sofa.",
        "- [Wanderer] I settle onto the hammock and think about how quiet this place is.",
        "- [another player] a line whose writer was never recorded",
        "- [Forest] the glade is quiet",
    ]
    # No user row may ever borrow the layer's identity again (the defect).
    assert not any(line.startswith("- [user]") for line in block.splitlines())


def test_own_session_entry_keeps_role_tag_but_foreign_does_not():
    rows = [
        {
            "seq": 7,
            "kind": "user",
            "text": "my own earlier line",
            "session_id": "mine",
            "collection": "w_forest",
        },
        {
            "seq": 9,
            "kind": "narrative",
            "text": "another chat's scene",
            "session_id": "other",
            "collection": "w_forest",
        },
    ]
    block = build_memory_block(
        rows, labels={"w_forest": "Forest"}, own_session_id="mine"
    )
    assert block is not None
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert bullets == [
        "- [user] my own earlier line",
        "- [Forest] another chat's scene",
    ]


def test_unresolved_collection_falls_back_to_prefix_label():
    rows = [
        {
            "seq": 1,
            "kind": "narrative",
            "text": "t",
            "session_id": "x",
            "collection": "a_unknown",
        }
    ]
    block = build_memory_block(rows)
    assert block is not None
    assert "- [character] t" in block


def test_layer_labels_resolves_world_and_persona_names(tmp_path):
    from fabulist.log import EventLog
    from fabulist.memory.scope import collection_for_actor, collection_for_world
    from fabulist.prompts.variables import PERSONAS_ANCHOR
    from fabulist.worlds.registry import append_world

    log = EventLog(tmp_path / "events.db")
    try:
        append_world(log, {"world_id": "w1", "name": "Forest", "cast": ["a1"]})
        log.append_event(
            PERSONAS_ANCHOR,
            "system",
            "user",
            {"event": "persona", "persona_id": "a1", "name": "Tessa"},
        )
        labels = layer_labels(
            log, [collection_for_world("w1"), collection_for_actor("a1"), "c_nope"]
        )
        assert labels == {
            collection_for_world("w1"): "Forest",
            collection_for_actor("a1"): "Tessa",
        }
    finally:
        log.close()


def test_gather_candidates_annotates_the_source_collection(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    store.upsert("w_world", 1, "narrative", "world-near", [1.0, 0, 0, 0], session_id="cW")
    store.upsert("a_actor", 1, "narrative", "actor-mid", [0.9, 0.1, 0, 0], session_id="cA")
    _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})

    rows = run(
        gather_candidates(
            store, "http://e", "m", ["w_world", "a_actor"], ["q"], fetch_k=5
        )
    )
    assert {r["text"]: r["collection"] for r in rows} == {
        "world-near": "w_world",
        "actor-mid": "a_actor",
    }
    store.close()


def test_recall_block_attributes_cross_layer_rows_by_owner(tmp_path, monkeypatch):
    """End-to-end: a real log resolves the world/actor names for the renderer."""
    from fabulist.log import EventLog
    from fabulist.memory.scope import collection_for_actor, collection_for_world
    from fabulist.prompts.variables import PERSONAS_ANCHOR
    from fabulist.worlds.registry import append_world

    log = EventLog(tmp_path / "events.db")
    store = MemoryStore(tmp_path / "m.db")
    try:
        append_world(log, {"world_id": "w1", "name": "Forest", "cast": ["a1"]})
        log.append_event(
            PERSONAS_ANCHOR,
            "system",
            "user",
            {"event": "persona", "persona_id": "a1", "name": "Tessa"},
        )
        wcol = collection_for_world("w1")
        acol = collection_for_actor("a1")
        # D55: the writer is stamped at index time and the renderer tags by it —
        # the layer only decides where the row LIVES, never who wrote it.
        store.upsert(
            wcol, 5, "user", "I settle into the glade", [1.0, 0, 0, 0],
            session_id="other", writer="Wanderer",
        )
        store.upsert(
            acol,
            133,
            "user",
            "I lower myself onto the sofa beside you",
            [0.95, 0.05, 0, 0],
            session_id="elsewhere",
            writer="Tessa",
        )
        store.upsert(wcol, 7, "user", "my own earlier line", [0.9, 0.1, 0, 0], session_id="mine")
        store.upsert(
            wcol, 9, "narrative", "The glade is quiet.", [0.85, 0.15, 0, 0],
            session_id="other",
        )
        _embed_stub(monkeypatch, {0: [1.0, 0.0, 0.0, 0.0]})

        result = run(
            recall_block(
                store,
                "http://e",
                "m",
                [wcol, acol],
                "sofa",
                config=RecallConfig(k=4),
                log=log,
                own_session_id="mine",
            )
        )
        bullets = [line for line in result.block.splitlines() if line.startswith("- ")]
        # A user row is tagged by its stamped writer (D55) ...
        assert "- [Wanderer] I settle into the glade" in bullets
        assert "- [Tessa] I lower myself onto the sofa beside you" in bullets
        # ... this chat's own row is the user's, by session ...
        assert "- [user] my own earlier line" in bullets
        # ... and a NARRATIVE row still carries the layer that holds it.
        assert "- [Forest] The glade is quiet." in bullets
        assert "[seq" not in result.block
    finally:
        store.close()
        log.close()
