"""Unit tests for the derived vector memory store (W3.1, design decision D5).

All vectors are dim 4 to keep the index cheap. Two well-separated clusters are
used: "A" near ``[1, 0, 0, 0]`` and "B" near ``[0, 1, 0, 0]``.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from fabulist.memory import MemoryStore, MemoryStoreUnavailable

DIM = 4
CLUSTER_A = [1.0, 0.0, 0.0, 0.0]
CLUSTER_B = [0.0, 1.0, 0.0, 0.0]


def _a(jitter: float = 0.0) -> list[float]:
    return [1.0, jitter, 0.0, 0.0]


def _b(jitter: float = 0.0) -> list[float]:
    return [jitter, 1.0, 0.0, 0.0]


def _seed(store: MemoryStore, collection: str = "char:alice") -> None:
    """Fill a collection with 10 near-A rows (seq 1..10) and 10 near-B (seq 11..20)."""
    for i in range(1, 11):
        store.upsert(
            collection, i, "narrative", f"A-{i}", _a((i - 1) * 0.001), ts=f"t{i}"
        )
    for i in range(11, 21):
        store.upsert(
            collection, i, "user", f"B-{i}", _b((i - 11) * 0.001), ts=f"t{i}"
        )


def test_ensure_collection_is_idempotent_and_validates_dim(tmp_path):
    with MemoryStore(tmp_path / "memory.db") as store:
        table = store.ensure_collection("char:alice", DIM)
        assert table == store.ensure_collection("char:alice", DIM)
        assert store.collections() == ["char:alice"]
        with pytest.raises(ValueError):
            store.ensure_collection("char:alice", 8)
        with pytest.raises(ValueError):
            store.ensure_collection("char:bob", 0)


def test_query_returns_cluster_members_first(tmp_path):
    with MemoryStore(tmp_path / "memory.db") as store:
        _seed(store)

        results = store.query("char:alice", CLUSTER_A, k=5)
        assert [r["seq"] for r in results] == [1, 2, 3, 4, 5]
        assert all(r["seq"] <= 10 for r in results)
        assert [r["text"] for r in results] == ["A-1", "A-2", "A-3", "A-4", "A-5"]
        assert results[0]["kind"] == "narrative"
        assert results[0]["distance"] == pytest.approx(0.0)

        # Distances are sorted ascending.
        distances = [r["distance"] for r in results]
        assert distances == sorted(distances)

        # The other cluster is clearly farther away.
        far = store.query("char:alice", CLUSTER_B, k=5)
        assert [r["seq"] for r in far] == [11, 12, 13, 14, 15]


def test_exclude_seqs_filters_after_fetch(tmp_path):
    with MemoryStore(tmp_path / "memory.db") as store:
        _seed(store)

        excluded = {1, 2, 3}
        results = store.query("char:alice", CLUSTER_A, k=10, exclude_seqs=excluded)

        # Fetching k + len(exclude) lets the far cluster backfill the cap, so we
        # still return k rows even though three top cluster-A rows were filtered.
        assert len(results) == 10
        assert excluded.isdisjoint({r["seq"] for r in results})
        assert [r["seq"] for r in results][:7] == [4, 5, 6, 7, 8, 9, 10]

        # A smaller k also filters without shrinking the cluster-A-only result.
        small = store.query("char:alice", CLUSTER_A, k=5, exclude_seqs={1, 2, 3})
        assert [r["seq"] for r in small] == [4, 5, 6, 7, 8]

        # An empty/None exclusion set is a no-op.
        assert store.query("char:alice", CLUSTER_A, k=10) == store.query(
            "char:alice", CLUSTER_A, k=10, exclude_seqs=set()
        )


def test_upsert_same_seq_replaces_vector_and_metadata(tmp_path):
    with MemoryStore(tmp_path / "memory.db") as store:
        _seed(store)

        store.upsert("char:alice", 11, "system", "A-11-now", _a(0.0), ts="revised")

        # Still 20 rows total — replacement, not duplication.
        assert len(store.query("char:alice", CLUSTER_A, k=100)) == 20

        top = store.query("char:alice", CLUSTER_A, k=20)
        moved = next(r for r in top if r["seq"] == 11)
        assert moved["text"] == "A-11-now"
        assert moved["kind"] == "system"
        assert moved["distance"] == pytest.approx(0.0)

        # It no longer sits in cluster B.
        b_results = store.query("char:alice", CLUSTER_B, k=5)
        assert 11 not in {r["seq"] for r in b_results}


def test_wipe_clears_everything(tmp_path):
    with MemoryStore(tmp_path / "memory.db") as store:
        _seed(store, "char:alice")
        _seed(store, "char:bob")
        assert store.collections() == ["char:alice", "char:bob"]

        store.wipe()

        assert store.collections() == []
        assert store.query("char:alice", CLUSTER_A, k=5) == []
        assert store.query("char:bob", CLUSTER_B, k=5) == []

        # Wiped store is reusable: reindexing from scratch works (D5 rebuild).
        _seed(store, "char:alice")
        assert len(store.query("char:alice", CLUSTER_A, k=100)) == 20


def test_drop_collection_leaves_siblings_intact(tmp_path):
    with MemoryStore(tmp_path / "memory.db") as store:
        _seed(store, "char:alice")
        _seed(store, "char:bob")

        store.drop_collection("char:alice")

        assert store.collections() == ["char:bob"]
        assert store.query("char:alice", CLUSTER_A, k=5) == []
        assert len(store.query("char:bob", CLUSTER_B, k=100)) == 20
        # Dropping a missing collection is a harmless no-op.
        store.drop_collection("char:missing")


def test_one_file_serves_many_collections(tmp_path):
    path = tmp_path / "memory.db"
    with MemoryStore(path) as store:
        _seed(store, "char:alice")
        _seed(store, "char:bob")
        # Per-character collections are isolated even in a shared file.
        assert [r["seq"] for r in store.query("char:alice", CLUSTER_A, k=3)] == [1, 2, 3]
        assert [r["seq"] for r in store.query("char:bob", CLUSTER_A, k=3)] == [1, 2, 3]


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "memory.db"
    with MemoryStore(path) as store:
        _seed(store)

    with MemoryStore(path) as store:
        results = store.query("char:alice", CLUSTER_A, k=3)
        assert [r["seq"] for r in results] == [1, 2, 3]
        assert store.collections() == ["char:alice"]


def test_query_dim_mismatch_and_edges(tmp_path):
    with MemoryStore(tmp_path / "memory.db") as store:
        _seed(store)
        with pytest.raises(ValueError):
            store.query("char:alice", [1.0, 0.0], k=3)
        assert store.query("char:alice", CLUSTER_A, k=0) == []
        # Unknown collection yields nothing rather than raising.
        assert store.query("char:ghost", CLUSTER_A, k=3) == []


def test_upsert_autocreates_collection_and_rejects_dim_drift(tmp_path):
    with MemoryStore(tmp_path / "memory.db") as store:
        store.upsert("char:alice", 1, "narrative", "hello", _a())
        assert store.collections() == ["char:alice"]
        with pytest.raises(ValueError):
            store.upsert("char:alice", 2, "narrative", "bad", [0.0, 1.0])
        with pytest.raises(ValueError):
            store.upsert("char:alice", 2, "narrative", "empty", [])


def test_close_is_idempotent_and_unavailable_is_runtime_error():
    assert issubclass(MemoryStoreUnavailable, RuntimeError)


def test_missing_extension_raises_unavailable(tmp_path, monkeypatch):
    def _boom(_conn):
        raise sqlite3.OperationalError("no such module: vec0")

    monkeypatch.setattr("fabulist.memory.store.sqlite_vec.load", _boom)
    with pytest.raises(MemoryStoreUnavailable) as excinfo:
        MemoryStore(tmp_path / "memory.db")
    assert "sqlite-vec" in str(excinfo.value)


def test_cross_session_same_seq_never_collides_in_shared_collection(tmp_path):
    """D16 persona fence: two chats sharing ONE collection must not overwrite.

    Persona collections carry rows from many chats; per-chat seqs collide
    (chat A seq 3 vs chat B seq 3). Rows key by the composite
    ``(slot, seq)`` — re-embedding a chat's own seq replaces ITS row, never
    the other chat's. Found live by the D16 re-index (15 rows attempted,
    11 landed).
    """
    import random

    rng = random.Random(7)
    dim = 4
    with MemoryStore(tmp_path / "memory.db") as store:
        for sid, tag in (("chatA", "A"), ("chatB", "B")):
            for seq in (1, 2, 3):
                vec = [rng.random() for _ in range(dim)]
                store.upsert(
                    "p_persona", seq, "user", f"{tag}-{seq}", vec, session_id=sid
                )
        # both chats' rows survive at their own true seqs
        rows = store.query("p_persona", [0.5] * dim, k=100)
        texts = sorted(r["text"] for r in rows)
        assert texts == sorted(
            [f"{t}-{s}" for t in ("A", "B") for s in (1, 2, 3)]
        )
        # re-embedding chat A seq 1 replaces ONLY its own row
        store.upsert(
            "p_persona", 1, "user", "A-1-revised", [0.9] * dim, session_id="chatA"
        )
        rows = store.query("p_persona", [0.5] * dim, k=100)
        texts = sorted(r["text"] for r in rows)
        assert "A-1-revised" in texts
        assert "B-1" in texts  # chat B untouched
        assert texts.count("A-1") == 0


# -- D53 fixes: session-scoped exclusion + multi-collection reads (U2/U5) --


def _seed_two_chats(store: MemoryStore, collection: str, seqs=range(1, 6)) -> None:
    """One collection carrying two chats whose per-chat seqs collide (1..5)."""
    for seq in seqs:
        store.upsert(
            collection, seq, "user", f"A-{seq}", _a(seq * 0.001), session_id="chatA"
        )
        store.upsert(
            collection, seq, "user", f"B-{seq}", _a(seq * 0.001), session_id="chatB"
        )


def test_exclude_seqs_is_session_scoped(tmp_path):
    """D53 bug: excluding seq 5 for chat A must NOT drop chat B's seq 5."""
    with MemoryStore(tmp_path / "memory.db") as store:
        _seed_two_chats(store, "p_persona")

        rows = store.query(
            "p_persona",
            CLUSTER_A,
            k=10,
            exclude_seqs={5},
            exclude_session_id="chatA",
        )
        texts = {r["text"] for r in rows}
        assert "A-5" not in texts  # the recalling chat's own seq is dropped
        assert "B-5" in texts  # the other chat's same-seq row survives
        # Every other row (both chats) is untouched.
        assert {"A-1", "B-1", "A-2", "B-2", "A-3", "B-3", "A-4", "B-4"} <= texts


def test_exclude_without_session_id_preserves_legacy_global_filter(tmp_path):
    """``exclude_session_id=None`` keeps the pre-D53 global-seq behavior."""
    with MemoryStore(tmp_path / "memory.db") as store:
        _seed_two_chats(store, "p_persona")

        scoped = store.query("p_persona", CLUSTER_A, k=10, exclude_seqs={5})
        explicit_none = store.query(
            "p_persona", CLUSTER_A, k=10, exclude_seqs={5}, exclude_session_id=None
        )
        # Byte-for-byte identical to the legacy single-argument call.
        assert scoped == explicit_none
        texts = {r["text"] for r in scoped}
        assert "A-5" not in texts and "B-5" not in texts  # legacy global drop


def test_query_many_unions_collections_and_sorts_globally(tmp_path):
    with MemoryStore(tmp_path / "memory.db") as store:
        store.upsert("w_world", 1, "narrative", "world-near", _a(0.0), session_id="chatW")
        store.upsert("w_world", 2, "narrative", "world-far", _b(0.0), session_id="chatW")
        store.upsert("a_actor", 1, "narrative", "actor-mid", _a(0.5), session_id="chatA")

        rows = store.query_many(["w_world", "a_actor"], [CLUSTER_A], k=10)
        texts = [r["text"] for r in rows]
        assert "world-near" in texts and "actor-mid" in texts  # both layers read
        assert "world-far" in texts  # other cluster still fetched when k allows
        assert texts[0] == "world-near"  # global best-first ordering
        distances = [r["distance"] for r in rows]
        assert distances == sorted(distances)  # GLOBAL sort across collections


def test_query_many_skips_dim_mismatched_collection(tmp_path, caplog):
    """U5: an orphaned collection (aborted re-embed) is skipped, not fatal."""
    with MemoryStore(tmp_path / "memory.db") as store:
        store.upsert("c_good", 1, "narrative", "good", _a(0.0), session_id="chatG")
        # A collection left at the OLD embedder dim (8) after an aborted re-embed.
        store.upsert("c_bad", 1, "narrative", "bad", [0.5] * 8, session_id="chatB")

        with caplog.at_level(logging.WARNING, logger="fabulist.memory.store"):
            rows = store.query_many(["c_bad", "c_good"], [CLUSTER_A], k=10)

        assert [r["text"] for r in rows] == ["good"]  # sibling still returns
        assert "c_bad" in caplog.text
        assert "skipping" in caplog.text


# -- per-session delete (issue #85) ----------------------------------------


def test_delete_session_rows_spares_a_sibling_in_every_shared_collection(tmp_path):
    """Per-row delete: one chat's rows go, its sibling's same-seq rows stay."""
    with MemoryStore(tmp_path / "memory.db") as store:
        store.upsert("w_world", 1, "user", "A-1", _a(0.0), session_id="chatA")
        store.upsert("w_world", 2, "narrative", "A-2", _a(0.1), session_id="chatA")
        store.upsert("w_world", 1, "user", "B-1", _a(0.0), session_id="chatB")
        store.upsert("w_world", 2, "narrative", "B-2", _a(0.1), session_id="chatB")
        store.upsert("a_alice", 7, "user", "A-7", _b(0.0), session_id="chatA")
        store.upsert("a_alice", 7, "user", "B-7", _b(0.0), session_id="chatB")

        assert store.session_ids() == ["chatA", "chatB"]
        assert store.count_session_rows("chatA") == 3

        assert store.delete_session_rows("chatA") == 3
        assert store.count_session_rows("chatA") == 0
        assert store.session_ids() == ["chatB"]

        # The sibling's rows survive in BOTH shared collections, at the same
        # per-chat seqs the deleted chat used (row keys are per-session).
        world = {r["text"] for r in store.query("w_world", CLUSTER_A, k=100)}
        assert world == {"B-1", "B-2"}
        actor = {r["text"] for r in store.query("a_alice", CLUSTER_B, k=100)}
        assert actor == {"B-7"}

        # Idempotent: a second delete (or an empty id) is a no-op.
        assert store.delete_session_rows("chatA") == 0
        assert store.delete_session_rows("") == 0

        # Slot rows are monotone bookkeeping, not orphans: they stay.
        slots = store._conn.execute(
            "SELECT session_id FROM memory_sessions WHERE session_id = 'chatA'"
        ).fetchall()
        assert slots == [("chatA",)]
