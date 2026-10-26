"""Best-effort per-session memory cleanup for the two delete doors (issue #85).

The vector index is derived and droppable (D5), but a deleted chat's rows must
stop being recallable from the live chats that share its layer. Both delete
paths call :func:`delete_session_memory` AFTER their log write:

* ``DELETE /api/sessions/{id}`` (soft hide, ``api.sessions.hide_session``);
* ``DELETE /api/sessions/{id}/branch`` (hard prune, ``api.branches.prune_branch``).

Cleanup is never allowed to fail the delete: the event log owns the truth, so a
missing memory handle (no embedder / memory off) is a no-op and a store failure
is a warning.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["delete_session_memory", "store_for_app"]

logger = logging.getLogger(__name__)


def store_for_app(app: Any) -> Any | None:
    """Return the live ``MemoryStore`` handle, or ``None`` when memory is off.

    The handle is ``app.state.memorizer.store`` (built by
    :func:`fabulist.wiring.make_memorizer`); a test or a memory-disabled app may
    have no memorizer at all.
    """
    memorizer = getattr(app.state, "memorizer", None)
    return getattr(memorizer, "store", None)


def delete_session_memory(app: Any, session_id: str) -> int:
    """Delete ``session_id``'s rows across every collection; return the count.

    Never raises into the delete path. A missing handle, an empty session id, or
    a chat with no rows is a no-op (0). A store failure logs a warning and the
    chat deletion still succeeds.
    """
    store = store_for_app(app)
    if store is None or not session_id:
        return 0
    try:
        return int(store.delete_session_rows(session_id))
    except Exception:  # noqa: BLE001 - cleanup is best-effort (D5)
        logger.warning(
            "memory cleanup for session %s failed; the delete still succeeds",
            session_id,
            exc_info=True,
        )
        return 0
