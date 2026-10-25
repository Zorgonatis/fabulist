"""SQLite DDL for the Fabulist event log.

The log is a single SQLite file; derived indexes (RAG, checkpoints) are
rebuildable and never authoritative (D5, D11). `events` is append-only in
practice — `prune_ephemeral` is the sole hard-delete path and only touches rows
explicitly flagged ephemeral (abandoned swipe branches, PLAN.md W2.2).
"""

from __future__ import annotations

from .events import EVENT_TYPES

_TYPE_CHECK = ", ".join(f"'{t}'" for t in sorted(EVENT_TYPES))

DDL = f"""
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    session_id     TEXT    NOT NULL,
    seq            INTEGER NOT NULL,
    type           TEXT    NOT NULL CHECK (type IN ({_TYPE_CHECK})),
    actor          TEXT    NOT NULL,
    payload        TEXT    NOT NULL DEFAULT '{{}}',
    supersedes     INTEGER,
    superseded_by  INTEGER,
    ephemeral      INTEGER NOT NULL DEFAULT 0,
    meta           TEXT    NOT NULL DEFAULT '{{}}',
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS assets (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{{}}',
    created_ts TEXT NOT NULL
);

-- Fork bookkeeping. A branch row names the session it forked from and the last
-- source seq it inherits; the branch's own events continue numbering from there
-- so the combined stream stays ordered by seq (see repo.EventLog docstring).
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    base_session TEXT,
    base_seq     INTEGER,
    created_ts   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type        ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_supersedes  ON events(supersedes);
CREATE INDEX IF NOT EXISTS idx_sessions_base      ON sessions(base_session);
"""
