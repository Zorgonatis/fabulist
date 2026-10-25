"""Memory lifecycle API (D25): destructive reset + background re-embed.

The embedder is a per-connection setting the user can rebind live (D25 C1). A
rebind to a DIFFERENT embedder changes the vector dimensionality, so a single
index file serving one store forever must be wiped and re-created at the new
dims — there is one store, and no data is retained across an embedder switch
(user ruling). This router owns that lifecycle surface:

  POST /api/memory/reset         -> wipe every vector collection (D5 rebuild)
  GET  /api/memory/collections   -> the store's known collection ids (verify empty)
  POST /api/memory/reembed       -> start the in-process re-embed batch job
  GET  /api/memory/reembed       -> the job's status snapshot

Reset is what the Connections drawer's confirm modal calls after rebinding the
``embedder`` agent: the next write re-initializes the store at the new embedder's
dims. Re-embed is a SEPARATE, explicit action (user ruling): it walks every
visible chat with memory enabled and re-embeds its events into the resolved D16
scope (persona-aware), absorbing the enumeration the CLI script also uses
(:mod:`fabulist.memory.reembed`).

Job state is deliberately IN-PROCESS (user ruling: persist nothing): a restart
mid-job loses the job and the "Re-embed all" button simply re-runs it. Re-runs
are idempotent (upserts are seq-keyed, D5). During the job recall uses whatever
has been re-embedded so far — there is no read gating.

Wiring (orchestrator-owned, one line in ``app.py``)::

    from fabulist.api.memory_api import router as memory_router
    app.include_router(memory_router)

The router reads ``app.state.memorizer.store`` (the live index) and
``app.state.registry`` (the live embedder binding). With no store it returns
``503``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from fabulist.memory.reembed import reembed_all, resolve_embedder

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog
    from fabulist.memory.store import MemoryStore

router = APIRouter(tags=["memory"])

#: Job lifecycle states surfaced by ``GET /api/memory/reembed``.
_STATE_IDLE = "idle"
_STATE_RUNNING = "running"
_STATE_DONE = "done"
_STATE_FAILED = "failed"


class _ReembedJob:
    """In-process state for one re-embed batch (never persisted, user ruling)."""

    def __init__(self) -> None:
        self.state: str = _STATE_IDLE
        self.done: int = 0
        self.total: int = 0
        self.error: str | None = None
        self.task: asyncio.Task[None] | None = None

    def snapshot(self) -> dict[str, Any]:
        """Wire view: ``{state, done, total}`` (+ ``error`` when failed)."""
        out: dict[str, Any] = {
            "state": self.state,
            "done": self.done,
            "total": self.total,
        }
        if self.error is not None:
            out["error"] = self.error
        return out


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _store_for(request: Request) -> "MemoryStore":
    """Return the live vector store from the memorizer, or 503 when unwired."""
    memorizer = getattr(request.app.state, "memorizer", None)
    store = getattr(memorizer, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="memory unavailable")
    return store


def _job_for(request: Request) -> _ReembedJob:
    """The app-scoped re-embed job, created lazily on first access."""
    job = getattr(request.app.state, "memory_reembed", None)
    if job is None:
        job = _ReembedJob()
        request.app.state.memory_reembed = job
    return job


@router.post("/api/memory/reset")
async def reset_memory(request: Request) -> dict:
    """Wipe every vector collection (destructive; the confirm-modal target).

    The store is DERIVED state (D5) — the event log is untouched, so the index
    is fully rebuildable via ``POST /api/memory/reembed``. The next write
    re-creates a collection at the current embedder's dims, so the store
    re-initializes at the new embedder after a rebind.
    """
    store = _store_for(request)
    previous = store.collections()
    store.wipe()
    return {"reset": True, "collections": store.collections(), "previous": previous}


@router.get("/api/memory/collections")
async def list_collections(request: Request) -> dict:
    """The store's known collection ids (empty right after a reset)."""
    store = _store_for(request)
    return {"collections": store.collections()}


@router.get("/api/memory/reembed")
async def reembed_status(request: Request) -> dict:
    """Return the in-process re-embed job's status snapshot."""
    return _job_for(request).snapshot()


@router.post("/api/memory/reembed")
async def start_reembed(request: Request) -> dict:
    """Start (or report) the background re-embed of every visible memory chat.

    Idempotent + restart-safe: re-running re-embeds cleanly. A job already
    running is NOT restarted — the current status is returned with
    ``started: false``. With no embedder/connection the request is ``503``.
    """
    log = _log_for(request)
    store = _store_for(request)
    registry = getattr(request.app.state, "registry", None)
    try:
        target = resolve_embedder(registry)
    except Exception:  # noqa: BLE001 - no embedder binding = nothing to embed with
        raise HTTPException(status_code=503, detail="embedder unavailable") from None

    job = _job_for(request)
    if job.state == _STATE_RUNNING:
        return {"started": False, **job.snapshot()}

    job.state = _STATE_RUNNING
    job.done = 0
    job.total = 0
    job.error = None

    def _progress(done: int, total: int) -> None:
        job.done = done
        job.total = total

    async def _run() -> None:
        try:
            stats = await reembed_all(log, store, target, progress=_progress)
            if stats.get("errors"):
                job.error = f"{len(stats['errors'])} row(s) failed to re-embed"
            job.state = _STATE_DONE
        except Exception as exc:  # noqa: BLE001 - the job reports, never crashes
            job.error = str(exc)
            job.state = _STATE_FAILED

    job.task = asyncio.create_task(_run())
    return {"started": True, **job.snapshot()}
