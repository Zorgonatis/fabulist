"""Vector memory package (W3.1).

A derived, droppable sqlite-vec index (design decision **D5**): embeddings are
never authoritative — the event log owns all truth, and this index can be wiped
and rebuilt at any time. It lives in its own SQLite file, separate from the
event-log database, and serves many collections (per-character RAG) from one
file.

Public surface:

* :class:`MemoryStore` — open/ensure/upsert/query/drop/wipe/close; context
  manager supported.
* :class:`MemoryStoreUnavailable` — raised when sqlite-vec cannot load, so the
  caller can choose a fallback (never a silent no-op).
"""

from __future__ import annotations

from .store import MemoryStore, MemoryStoreUnavailable

__all__ = ["MemoryStore", "MemoryStoreUnavailable"]
