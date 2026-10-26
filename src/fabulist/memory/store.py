"""Vector memory store (W3.1) — a *derived*, droppable side index.

Design decision **D5**: embeddings are derived state, never authoritative. The
append-only event log (:mod:`fabulist.log`) owns all truth; this store is a
disposable index that can be wiped and rebuilt from the log at any time. It
therefore lives in its **own SQLite file**, *not* inside the event-log database —
dropping the index must never risk the log.

One database file serves **many collections** (per-character RAG, one collection
per character/session). Each collection gets:

* a ``vec0`` virtual table (sqlite-vec) holding ``(seq, embedding)`` rows; the
  table name is a sanitized, hashed form of the caller's collection id so
  arbitrary ids (``char:alice``, ``session/42``, …) are safe identifiers;
* rows in a single shared sidecar metadata table ``memory_meta`` keyed
  ``(collection, seq)`` carrying the human-readable ``kind``, ``text`` and
  ``ts`` for each embedded event.

A small ``memory_collections`` catalog maps a collection id to its vector table
and dimensionality so ``ensure_collection``/``upsert`` are idempotent.

Graceful degradation
--------------------
If the ``sqlite-vec`` extension cannot be loaded, the constructor raises
:class:`MemoryStoreUnavailable` with a clear message. It does **not** silently
fall back to a no-op store — the caller decides the fallback (skip RAG, surface
an error, etc.).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Sequence

import sqlite_vec

logger = logging.getLogger(__name__)

__all__ = ["MemoryStore", "MemoryStoreUnavailable"]

#: First-run log line for the derived vector index (DEBUG: rebuildable, D5).
NEW_LOG_DEBUG: str = (
    "memory index not found - creating a NEW EMPTY index; it is derived and "
    "rebuildable from the event log"
)

_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS memory_collections (
    collection TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    dim        INTEGER NOT NULL
)
"""

#: v2 (D16 persona fence): rows key by (collection, row_key) where
#: ``row_key = (slot << 32) | seq`` — a persona collection carries rows from
#: MANY chats, and per-chat seqs collide (chat A seq 3 vs chat B seq 3 would
#: overwrite). ``slot`` is a per-session int from memory_sessions; ``seq``
#: stays each row's TRUE chat-local seq for display/filtering.
_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS memory_sessions (
    session_id TEXT PRIMARY KEY,
    slot       INTEGER NOT NULL UNIQUE
)
"""

_META_DDL = """
CREATE TABLE IF NOT EXISTS memory_meta (
    collection TEXT NOT NULL,
    row_key    INTEGER NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    ts         TEXT NOT NULL DEFAULT '',
    writer     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (collection, row_key)
)
"""


class MemoryStoreUnavailable(RuntimeError):
    """Raised when the sqlite-vec extension cannot be loaded/used.

    The caller chooses the fallback; this store never degrades silently.
    """


def _sanitize_collection(collection: str) -> str:
    """Return a safe, stable SQLite table name for an arbitrary collection id.

    Non-alphanumeric runs collapse to ``_``; a short hash of the original id is
    appended so distinct ids cannot collide after sanitization.
    """
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", collection).strip("_").lower() or "c"
    digest = hashlib.sha1(collection.encode("utf-8")).hexdigest()[:8]
    return f"vec_{slug[:40]}_{digest}"


class MemoryStore:
    """A disposable sqlite-vec index holding one or more named collections.

    Parameters
    ----------
    db_path:
        Path to the *separate* index SQLite file. Parent directories are created
        if needed. ``":memory:"`` is permitted for tests.
    """

    def __init__(self, db_path: Path | str) -> None:
        if str(db_path) != ":memory:":
            # A missing index file is a first run: it is DERIVED, droppable state
            # (D5) rebuildable from the log, so this is DEBUG, not a warning.
            if not Path(db_path).exists():
                logger.debug("%s: %s", db_path, NEW_LOG_DEBUG)
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        try:
            # The sqlite-vec gotcha: enable extension loading BEFORE load().
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
        except Exception as exc:  # pragma: no cover - environment dependent
            self._conn.close()
            raise MemoryStoreUnavailable(
                "sqlite-vec extension could not be loaded; vector memory is "
                f"unavailable ({exc!r}). The caller decides the fallback."
            ) from exc
        finally:
            try:
                self._conn.enable_load_extension(False)
            except Exception:  # pragma: no cover - connection already closed
                pass
        # v2 (D16): session-scoped row keys. Migrate-or-drop FIRST (legacy
        # meta lacks row_key), then ensure the full v2 schema.
        self._conn.execute(_SESSIONS_DDL)
        self._conn.execute(_CATALOG_DDL)
        self._conn.execute(_META_DDL)
        self._conn.commit()
        self._migrate_meta_if_legacy()
        self._ensure_writer_column()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- collections -------------------------------------------------------

    def ensure_collection(self, collection: str, dim: int) -> str:
        """Create the collection's ``vec0`` table if absent; return its table name.

        Raises :class:`ValueError` if the collection exists with a different
        dimensionality (mixing dims in one vec0 table is not supported).
        """
        if dim < 1:
            raise ValueError(f"dim must be >= 1, got {dim!r}")
        existing = self._conn.execute(
            "SELECT table_name, dim FROM memory_collections WHERE collection = ?",
            (collection,),
        ).fetchone()
        if existing is not None:
            table_name, existing_dim = existing
            if existing_dim != dim:
                raise ValueError(
                    f"collection {collection!r} already exists with dim "
                    f"{existing_dim}, refusing to reuse it with dim {dim}"
                )
            return table_name
        table_name = _sanitize_collection(collection)
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} "
            f"USING vec0(seq integer primary key, embedding float[{dim}])"
        )
        self._conn.execute(
            "INSERT INTO memory_collections (collection, table_name, dim) VALUES (?, ?, ?)",
            (collection, table_name, dim),
        )
        self._conn.commit()
        return table_name

    def _table_for(self, collection: str) -> tuple[str, int] | None:
        row = self._conn.execute(
            "SELECT table_name, dim FROM memory_collections WHERE collection = ?",
            (collection,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def collections(self) -> list[str]:
        """Return the known collection ids, sorted for stable output."""
        rows = self._conn.execute(
            "SELECT collection FROM memory_collections ORDER BY collection"
        ).fetchall()
        return [r[0] for r in rows]

    # -- writes ------------------------------------------------------------

    def _slot_for(self, session_id: str) -> int:
        """Return the session's stable slot (assigning the next one on first use)."""
        with self._conn:
            row = self._conn.execute(
                "SELECT slot FROM memory_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                return int(row[0])
            nxt = (
                self._conn.execute("SELECT MAX(slot) FROM memory_sessions").fetchone()[0]
                or 0
            ) + 1
            self._conn.execute(
                "INSERT INTO memory_sessions (session_id, slot) VALUES (?, ?)",
                (session_id, nxt),
            )
            return int(nxt)

    def _row_key(self, session_id: str, seq: int) -> int:
        """Composite row key: ``(slot << 32) | seq`` — cross-chat seq-unique."""
        return (self._slot_for(session_id) << 32) | (int(seq) & 0xFFFFFFFF)

    def _migrate_meta_if_legacy(self) -> None:
        """Drop a pre-v2 meta schema (no ``row_key`` column).

        The store is DERIVED state (D5): the event log owns every row it holds.
        A legacy DB is dropped wholesale and recreated; callers rebuild with the
        reindex script (scripts/reindex_memory.py).
        """
        import logging

        cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(memory_meta)").fetchall()
        }
        if cols and "row_key" not in cols:
            logging.getLogger(__name__).warning(
                "memory_meta predates v2 (no row_key/session scoping) — dropping "
                "the derived index for a clean rebuild (D5: log is truth)"
            )
            with self._conn:
                tables = [
                    r[0]
                    for r in self._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name LIKE 'vec_%'"
                    ).fetchall()
                ]
                for t in tables:
                    self._conn.execute(f'DROP TABLE IF EXISTS "{t}"')
                self._conn.execute("DROP TABLE IF EXISTS memory_meta")
                self._conn.execute("DROP TABLE IF EXISTS memory_collections")
                self._conn.execute("DROP TABLE IF EXISTS memory_sessions")
                self._conn.execute(_META_DDL)
                self._conn.execute(_CATALOG_DDL)
                self._conn.execute(_SESSIONS_DDL)

    def _ensure_writer_column(self) -> None:
        """Add the ``writer`` stamp column to a pre-existing ``memory_meta``.

        Unlike the ``row_key`` migration this is NON-destructive: the column is
        purely additive and defaults to ``''``, so every existing row reads as
        "no writer recorded" without forcing a rebuild. An older v2 index DB
        (already row_key-scoped) therefore gains the column in place; a legacy
        DB dropped by :meth:`_migrate_meta_if_legacy` already has it via
        :data:`_META_DDL`.
        """
        cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(memory_meta)").fetchall()
        }
        if cols and "writer" not in cols:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE memory_meta ADD COLUMN writer TEXT NOT NULL DEFAULT ''"
                )

    def upsert(
        self,
        collection: str,
        seq: int,
        kind: str,
        text: str,
        embedding: list[float],
        ts: str = "",
        *,
        session_id: str = "",
        writer: str = "",
    ) -> None:
        """Insert-or-replace one embedded row plus its sidecar metadata.

        ``seq`` is the event-log sequence number; ``session_id`` scopes the row
        key (D16): a persona collection carries rows from many chats, so the
        stored key is the composite ``(slot, seq)`` — re-embedding the same
        chat's ``seq`` replaces its own row (idempotent reindex), never another
        chat's. ``query`` returns each row's TRUE per-chat ``seq``.

        ``writer`` is the persona name in effect when a ``user`` row was
        authored (``''`` for narrative/asset rows or a persona-less chat). It
        travels in the shared sidecar so a persona collection pooled from many
        chats keeps the writing persona's identity even though the chat's own
        ``session_id`` is the only other provenance on the row.
        """
        dim = len(embedding)
        if dim < 1:
            raise ValueError("embedding must not be empty")
        table_name, stored_dim = self._resolve(collection, dim)
        row_key = self._row_key(session_id, seq)
        blob = sqlite_vec.serialize_float32(list(embedding))
        with self._conn:
            # vec0 has no UPSERT; delete-then-insert is the replace.
            self._conn.execute(f"DELETE FROM {table_name} WHERE seq = ?", (row_key,))
            self._conn.execute(
                f"INSERT INTO {table_name} (seq, embedding) VALUES (?, ?)",
                (row_key, blob),
            )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO memory_meta
                (collection, row_key, session_id, seq, kind, text, ts, writer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (collection, row_key, session_id, seq, kind, text, ts, writer),
            )

    def _resolve(self, collection: str, dim: int) -> tuple[str, int]:
        found = self._table_for(collection)
        if found is None:
            return self.ensure_collection(collection, dim), dim
        table_name, stored_dim = found
        if stored_dim != dim:
            raise ValueError(
                f"collection {collection!r} is dim {stored_dim}, got embedding "
                f"of dim {dim}"
            )
        return table_name, stored_dim

    # -- reads -------------------------------------------------------------

    def query(
        self,
        collection: str,
        embedding: list[float],
        k: int = 8,
        exclude_seqs: set[int] | None = None,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the top-``k`` nearest rows as ``{seq, kind, text, distance}``.

        ``exclude_seqs`` is applied *after* fetch: we fetch ``k + len(exclude)``
        neighbours so that filtering can still yield up to ``k`` results.

        ``exclude_session_id`` scopes the exclusion (D53 bug fix): a collection
        may carry rows from MANY chats whose per-chat ``seq`` values collide, so
        an unconditional seq filter would drop another chat's same-seq row. When
        a session id is given, a row is excluded only when its own
        ``session_id`` matches it. ``None`` keeps the legacy global-seq behaviour
        byte-for-byte (the single-chat live path).
        """
        if k <= 0:
            return []
        found = self._table_for(collection)
        if found is None:
            return []
        table_name, stored_dim = found
        if len(embedding) != stored_dim:
            raise ValueError(
                f"query embedding dim {len(embedding)} != collection dim {stored_dim}"
            )
        excluded = exclude_seqs or set()
        # Exclude applies to each row's TRUE per-chat seq (not the composite
        # row_key): fetch k + len(exclude) neighbours, join meta, then filter.
        fetch_limit = k + len(excluded)
        blob = sqlite_vec.serialize_float32(list(embedding))
        rows = self._conn.execute(
            f"SELECT seq, distance FROM {table_name} "
            f"WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, fetch_limit),
        ).fetchall()
        if not rows:
            return []
        row_keys = [r[0] for r in rows]
        placeholders = ",".join("?" for _ in row_keys)
        meta = {
            key: (kind, text, sid, seq, writer)
            for key, kind, text, sid, seq, writer in self._conn.execute(
                f"SELECT row_key, kind, text, session_id, seq, writer FROM memory_meta "
                f"WHERE collection = ? AND row_key IN ({placeholders})",
                (collection, *row_keys),
            ).fetchall()
        }
        _MISSING = ("", "", "", 0, "")

        def _excluded(row_key: int) -> bool:
            # Missing meta mirrors the legacy filter default (row_key as seq).
            seq = meta.get(row_key, ("", "", "", row_key, ""))[3]
            if seq not in excluded:
                return False
            if exclude_session_id is None:
                return True
            return meta.get(row_key, _MISSING)[2] == exclude_session_id

        rows = [r for r in rows if not _excluded(r[0])][:k]
        if not rows:
            return []
        return [
            {
                "row_key": row_key,
                "seq": meta.get(row_key, _MISSING)[3],
                "session_id": meta.get(row_key, _MISSING)[2],
                "kind": meta.get(row_key, _MISSING)[0],
                "text": meta.get(row_key, _MISSING)[1],
                "writer": meta.get(row_key, _MISSING)[4],
                "distance": float(distance),
            }
            for row_key, distance in rows
        ]

    def query_many(
        self,
        collections: Sequence[str],
        vectors: Sequence[list[float]],
        k: int = 8,
        *,
        exclude_seqs: set[int] | None = None,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Union the top-``k`` neighbours across ``collections`` × ``vectors``.

        The D53 read set (own layer + nested layers) spans several collections,
        each queried with the SAME query vectors. Rows are keyed by
        ``(collection, row_key)`` — two chats' colliding ``seq`` values (or the
        same session's rows in two layers) are distinct rows — keeping the best
        (lowest) distance per key, then the whole union is sorted GLOBALLY by
        distance (all collections share one embedder/metric/dim, so distances
        are comparable).

        **Dim tolerance (U5):** a collection whose stored dim differs from the
        query vectors is SKIPPED with a log line, never raised — an orphaned
        table left by an aborted re-embed must not kill a turn. ``exclude_seqs``
        is forwarded session-scoped via :meth:`query`.
        """
        if k <= 0:
            return []
        live_vectors = [v for v in vectors if v]
        if not live_vectors:
            return []
        best: dict[tuple[str, int], dict[str, Any]] = {}
        for collection in collections:
            found = self._table_for(collection)
            if found is None:
                continue
            _, stored_dim = found
            compatible = [v for v in live_vectors if len(v) == stored_dim]
            if not compatible:
                logger.warning(
                    "memory collection %r dim %d != query dim %d; skipping "
                    "(orphaned/aborted re-embed)",
                    collection,
                    stored_dim,
                    len(live_vectors[0]),
                )
                continue
            for vector in compatible:
                for row in self.query(
                    collection,
                    vector,
                    k=k,
                    exclude_seqs=exclude_seqs,
                    exclude_session_id=exclude_session_id,
                ):
                    key = (collection, int(row["row_key"]))
                    previous = best.get(key)
                    if previous is None or float(row["distance"]) < float(
                        previous["distance"]
                    ):
                        best[key] = row
        return sorted(best.values(), key=lambda r: float(r["distance"]))

    # -- per-session delete (issue #85) ------------------------------------

    def session_ids(self) -> list[str]:
        """Distinct non-empty session ids carrying rows in any collection.

        The meta table's ``session_id`` column is the authoritative index: a
        shared layer (``w_<world>`` / ``a_<actor>``) carries rows from many
        chats, and this is how a cleanup finds every chat that put rows there.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT session_id FROM memory_meta "
            "WHERE session_id != '' ORDER BY session_id"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def count_session_rows(self, session_id: str) -> int:
        """Count a session's rows across ALL collections (0 for an empty id)."""
        if not session_id:
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) FROM memory_meta WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def delete_session_rows(self, session_id: str) -> int:
        """Delete ONE session's meta + vector rows across ALL its collections.

        The rows live in shared layers, so this is a per-row delete, never a
        collection drop: only row keys whose meta row carries ``session_id``
        are removed, leaving a sibling chat's rows (even at the same per-chat
        ``seq``) untouched. The composite ``row_key`` is per-collection, so the
        meta index is read first and the vector rows are then deleted by that
        key in each collection's own table.

        ``memory_sessions`` slot rows are deliberately LEFT alone: they are
        monotone bookkeeping (``wipe`` leaves them too), not orphans.

        Returns the number of rows deleted (0 for a missing/empty session).
        """
        if not session_id:
            return 0
        rows = self._conn.execute(
            "SELECT collection, row_key FROM memory_meta WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        if not rows:
            return 0
        by_collection: dict[str, list[int]] = {}
        for collection, row_key in rows:
            by_collection.setdefault(str(collection), []).append(int(row_key))
        with self._conn:
            for collection, row_keys in by_collection.items():
                found = self._table_for(collection)
                if found is not None:
                    table_name, _ = found
                    placeholders = ",".join("?" for _ in row_keys)
                    self._conn.execute(
                        f"DELETE FROM {table_name} WHERE seq IN ({placeholders})",
                        row_keys,
                    )
                placeholders = ",".join("?" for _ in row_keys)
                self._conn.execute(
                    f"DELETE FROM memory_meta WHERE collection = ? "
                    f"AND row_key IN ({placeholders})",
                    (collection, *row_keys),
                )
        return len(rows)

    # -- teardown / rebuild (D5) -------------------------------------------

    def drop_collection(self, collection: str) -> None:
        """Drop one collection's vector table and its metadata (no-op if absent)."""
        found = self._table_for(collection)
        if found is None:
            return
        table_name, _ = found
        with self._conn:
            self._conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            self._conn.execute("DELETE FROM memory_meta WHERE collection = ?", (collection,))
            self._conn.execute(
                "DELETE FROM memory_collections WHERE collection = ?", (collection,)
            )

    def wipe(self) -> None:
        """Drop every collection, metadata and catalog entry (D5 rebuild path)."""
        names = [
            r[0]
            for r in self._conn.execute(
                "SELECT table_name FROM memory_collections"
            ).fetchall()
        ]
        with self._conn:
            for table_name in names:
                self._conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            self._conn.execute("DELETE FROM memory_meta")
            self._conn.execute("DELETE FROM memory_collections")
