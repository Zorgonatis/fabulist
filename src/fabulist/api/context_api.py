"""W9.26 §2.2/§2.7 — the compaction job doors and the per-task context ledger.

Three read/write surfaces, all additive (nothing here mutates history):

  POST /api/sessions/{id}/compact
      -> ``202 {"job_id", "status": "running", "session_id"}``
      -> ``200 {"job_id": None, "status": "done", "session_id", "title"?}`` when a
         compaction successor already exists (idempotent — NEVER a second era)
      -> ``409 {"detail": {"reason": "compacting"}}`` while this session is
         claimed or a job is running (§2.3 step 1's claim protocol)

  GET  /api/sessions/{id}/context/compaction/{job_id}
      -> ``{status: running|done|failed|noop, session_id?, title?, error?,
             stage?, reason?}``  (§2.2's polling DTO)

  GET  /api/sessions/{id}/context
      -> the §2.7 per-task ledger: one row per task surface with model,
         connection, declared window, effective response length
         (``merge_sampling``), usable budget, the last MEASURED
         ``prompt_tokens`` and the resulting ratio.

Why polling and 409s instead of the stream: SSE closes at ``turn.done``, which
app.py sets BEFORE the post-turn hooks run, and the button path has no stream at
all — no frame can deliver a hook result (§1).

Delivery doctrine (why this router constructs nothing): ``app.state`` owns the
seams. ``compact_service`` / ``context_jobs`` / ``compaction_claims`` /
``turn_slot`` are built in ``build_app`` (Lane B) and consumed here; a missing
service is a ``stage:"config"`` job failure, a missing registry just means no job
door (503) — never a boot failure.

Read discipline (AGENTS.md): the ledger's usage rows are OWN-session
``get_events``; only the chain-context questions (does a successor exist?) walk
``get_chain_events``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from fabulist.api.attachments import _session_exists
from fabulist.connections.taxonomy import resolve_interaction_agent
from fabulist.graph import context_monitor as monitor
from fabulist.graph.compaction import RESERVE_TOKENS, prompt_budget_tokens
from fabulist.prompts.agent_sets import bindings_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

logger = logging.getLogger(__name__)

__all__ = ["LEDGER_TASKS", "build_context_ledger", "router"]

router = APIRouter(tags=["context"])

#: The §2.7 ledger's task surfaces, in display order:
#: ``(task label, taxonomy interaction, fallback agent)``.
#:
#: The fallbacks mirror :data:`fabulist.prompts.agent_sets.DEFAULT_BINDINGS`
#: exactly, so an un-authored preset row reports the connection the engine
#: would actually use. ``summarize``/``caption``/``npc`` are the taxonomy's
#: interaction ids; the labels are the human surfaces the Inspector shows.
LEDGER_TASKS: tuple[tuple[str, str, str], ...] = (
    ("narrate", "narrate", "narrator"),
    ("ooc", "ooc", "narrator"),
    ("extract", "extract", "extractor"),
    ("bts", "bts", "extractor"),
    ("critic", "critic", "extractor"),
    ("planner", "planner", "narrator"),
    ("assistant", "assistant", "narrator"),
    ("summarizer", "summarize", "summarizer"),
    ("captioner", "caption", "captioner"),
    ("npc-lane", "npc", "completion.narration"),
)

#: The task whose label union must match the GAUGE's exactly (see
#: :func:`fabulist.graph.context_monitor.narrate_labels`): the Inspector row and
#: the ``context_low`` notice must quote the same measurement, so the narrate row
#: accepts the dialect aliases (``narrate``/``narrator``) the gauge accepts.
NARRATE_TASK = "narrate"

#: The declared-window status the ledger emits for a connection with no Context
#: Size authored (⚑C: every live connection today).
WINDOW_UNDECLARED = "window undeclared"


def _log_for(request: Request) -> "EventLog":
    """The app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _state(request: Request, name: str) -> Any:
    """One optional ``app.state`` seam (None when the build did not set it)."""
    return getattr(request.app.state, name, None)


# --- §2.7 the per-task context ledger --------------------------------------


def _ledger_row(
    *,
    task: str,
    interaction: str,
    fallback: str,
    bindings: Mapping[str, Any] | None,
    registry: Any,
    events: list[Any],
) -> dict[str, Any]:
    """One task surface's ledger row (measured-only, ⚑R1a: no estimate column).

    Everything except ``ratio`` is read from configuration or from a usage row:

    * ``agent`` — the slot the interaction resolves to right now;
    * ``model``/``connection`` — the connection serving that slot;
    * ``context_window`` — the owner-declared Context Size (``None`` = undeclared,
      the ⚑C state, which makes every derived number ``None``);
    * ``response_length`` — ``merge_sampling(...).max_tokens``, the ONLY correct
      source (there is no ``conn.response_length``);
    * ``budget_tokens`` — ``window − response − RESERVE_TOKENS``
      (:func:`fabulist.graph.compaction.prompt_budget_tokens`, one formula);
    * ``prompt_tokens`` — the newest OWN usage row for that slot (measured);
    * ``ratio`` — ``prompt_tokens / budget``, only when the budget is positive
      (never a division by a non-positive declaration).
    """
    agents = getattr(registry, "agents", None)
    agents = agents if isinstance(agents, Mapping) else {}
    agent = resolve_interaction_agent(bindings, agents, interaction, fallback)
    labels = (
        monitor.narrate_labels(agent)
        if task == NARRATE_TASK
        else monitor.slot_labels(agent)
    )
    row = monitor.latest_usage_row(events, labels)
    model = str(row.get("model") or "") if row else None
    connection = monitor.serving_connection(registry, model, agent=agent)

    window = getattr(connection, "context_window", None) if connection is not None else None
    response = (
        monitor.effective_response_length(connection) if connection is not None else None
    )
    budget = (
        prompt_budget_tokens(window, response)
        if connection is not None and window is not None
        else None
    )
    prompt_tokens = int(row["prompt_tokens"]) if row else None
    ratio = (
        round(prompt_tokens / budget, 4)
        if prompt_tokens is not None and budget is not None and budget > 0
        else None
    )

    entry: dict[str, Any] = {
        "task": task,
        "interaction": interaction,
        "agent": agent,
        "model": model,
        "connection": getattr(connection, "name", None) if connection is not None else None,
        "context_window": int(window) if window is not None else None,
        "window_declared": window is not None,
        "response_length": response,
        "budget_tokens": budget,
        "prompt_tokens": prompt_tokens,
        "ratio": ratio,
        "basis": "measured",
        "reserve_tokens": RESERVE_TOKENS,
    }
    if window is None:
        # ⚑C: no Context Size authored — the row says so instead of inventing a
        # window (D84 doctrine: never probe, never guess).
        entry["note"] = WINDOW_UNDECLARED
    return entry


def build_context_ledger(
    log: "EventLog",
    registry: Any,
    session_id: str,
    *,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The §2.7 ledger payload: one row per task surface (pure over the log)."""
    events = log.get_events(session_id)  # OWN usage rows (audit discipline)
    resolved = bindings if bindings is not None else bindings_for(log)
    rows = [
        _ledger_row(
            task=task,
            interaction=interaction,
            fallback=fallback,
            bindings=resolved,
            registry=registry,
            events=events,
        )
        for task, interaction, fallback in LEDGER_TASKS
    ]
    return {"session_id": session_id, "tasks": rows}


# --- §2.2 the compact doors -------------------------------------------------


def _conflict(reason: str) -> HTTPException:
    """409 with the pinned body shape ``{detail:{reason}}``."""
    return HTTPException(status_code=409, detail={"reason": reason})


@router.post("/api/sessions/{session_id}/compact")
async def post_compact(session_id: str, request: Request) -> JSONResponse:
    """Start a compaction job (202 + job id) or return the existing successor (200).

    The idempotent check runs FIRST and needs no claim: it is a log read, and a
    successor that already exists is a ``200`` answer, not a second era (§2.6:
    ``POST /compact`` on a superseded parent returns its successor). The claim
    is then checked and taken by the JOB (``run_compact_job``), which also
    re-runs that same check under the claim — the duplicate-era race is closed
    there, not here.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    # Two log reads to answer "is there already an era to hand back?".
    successor = monitor.idempotent_successor(log, session_id)
    if successor is not None:
        title = monitor.session_title(log, successor) or monitor.session_title(log, session_id)
        body: dict[str, Any] = {
            "job_id": None,
            "status": "done",
            "session_id": successor,
        }
        if title:
            body["title"] = title
        return JSONResponse(status_code=200, content=body)

    jobs = _state(request, "context_jobs")
    if jobs is None:
        raise HTTPException(
            status_code=503, detail="context jobs unavailable (app.state.context_jobs)"
        )
    service = _state(request, "compact_service")
    claims = _state(request, "compaction_claims")
    slot = _state(request, "turn_slot")

    # --- one await-free block: claim check + running-job check --------------
    if monitor.claim_held(claims, session_id) or jobs.running_for(session_id) is not None:
        raise _conflict("compacting")
    job = jobs.create(session_id, monitor.MANUAL_TRIGGER)
    if job is None:  # a job won the race between the two lines above
        raise _conflict("compacting")

    # Hold the task on the job record: asyncio keeps only weak references to
    # tasks, so an unreferenced job task can be collected mid-flight (the same
    # lesson app.py's ``_BACKGROUND_TASKS`` records).
    job.task = asyncio.create_task(
        monitor.run_compact_job(
            job, log=log, service=service, claims=claims, slot=slot
        )
    )
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job.job_id,
            "status": monitor.STATUS_RUNNING,
            "session_id": session_id,
        },
    )


@router.get("/api/sessions/{session_id}/context/compaction/{job_id}")
async def get_compaction_job(session_id: str, job_id: str, request: Request) -> dict:
    """Poll one job's state (§2.2's DTO). 404 for unknown/foreign job ids."""
    jobs = _state(request, "context_jobs")
    job = jobs.get(job_id) if jobs is not None else None
    if job is None or job.session_id != session_id:
        raise HTTPException(status_code=404, detail="unknown compaction job")
    return {"session_id": session_id, **job.dto()}


@router.get("/api/sessions/{session_id}/context")
async def get_context_ledger(session_id: str, request: Request) -> dict:
    """The §2.7 per-task context ledger (404 when the session is unknown)."""
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return build_context_ledger(log, _state(request, "registry"), session_id)
