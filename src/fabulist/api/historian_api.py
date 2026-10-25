"""Historian router (W9.23 §3.6.7): the off-turn proposal lanes' control plane.

The historian runs the world/actor/book lane system OUTSIDE the turn flow — on
the preset's cadence, or on the owner's demand. This router is the Studio's
read/write surface over that schedule and the manual door:

Endpoints
---------
  GET  /api/prompts/historian              -> {"historian": {"cadence": int}}
  PUT  /api/prompts/historian              -> the stored read | 422 {"detail": {...}}
  GET  /api/sessions/{id}/historian        -> {"running", "last_run",
                                               "committed_turns", "cadence", "due_in"}
  POST /api/sessions/{id}/historian/run    -> {"gate", "filed", "dropped",
                                               "failed_lanes", "turn_seq",
                                               "duration_ms"}
                                               | 404 | 409 | 503

Config (``{"cadence": int}``) is preset data — the planner's contract exactly
(W9.20 §6): ``GET`` reads the ACTIVE preset's block through
:func:`~fabulist.prompts.preset_store.historian_config`, which fills the default
for an absent row, so an unedited install needs no migration; ``PUT`` validates
through :func:`~fabulist.prompts.preset_store.normalize_historian_config` (the
one write-door contract) and persists with the same append discipline — the
ACTIVE preset's whole row is re-appended with the new ``historian`` block, or the
persisted DEFAULT row is seeded through
:func:`~fabulist.prompts.preset_store.set_default_historian` when none is
active. The body may be the bare config or wrapped as ``{"historian": {...}}``
(the planner door's nesting); a config never carries a ``historian`` key, so the
two shapes are unambiguous.

The run door does NOT claim the session turn slot (decided, §3.6.7)
------------------------------------------------------------------
The planner's runs hold the slot because a plan feeds the NEXT turn's context
(steering — no turn may interleave between the plan read and its append).
Proposals feed NOTHING in the turn flow: they are user-gated post-turn artifacts,
so making a send wait three lane calls would invert the owner's "no need to block
on this task" ruling. A send issued while the historian works therefore nears
neither a 409 nor a wait; the two guards that matter are elsewhere:

* the STARTED row's ``turn_seq`` (the cadence clock — the next turn's due check
  sees a fresh stamp), and
* a process-local in-flight set, which is what this door's ``409`` reports
  (``{"detail": "historian already running"}``) and the post-turn hook reads to
  skip a second spawn.

Concurrent filing is safe by construction (§3.6.8): the log's append path is one
sqlite connection with ``BEGIN IMMEDIATE`` per append and no awaits inside, and
no turn-flow read consumes ``asset_proposal`` rows (the one fold that hides
proposals — ``unaccepted_candidate_names`` — concerns ``kind=cast``, which the
historian never files).

``503`` when the engine wiring supplied no runner (a bare ``create_app`` in a
test or a CLI), ``404`` for an unknown session. The run has NO run-total time
bound: each lane call is bounded by its CONNECTION's ``timeout`` field, and a
call that hits it is a per-lane failure the run reports
(``failed_lanes`` + the counts of the lanes that did file), not an error answer
from this door. Only a runner-level failure (the dispatcher/fan-in/log raised)
surfaces as ``error`` on the run result, with the runner's ``failed`` row
already stamped and the in-flight guard already released.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping

from fastapi import APIRouter, HTTPException, Request

from fabulist.prompts.preset_store import (
    active_preset,
    append_preset,
    get_preset,
    historian_config,
    normalize_historian_config,
    preset_to_json,
    set_default_historian,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog
    from fabulist.state.historian import HistorianRunner

router = APIRouter(tags=["historian"])

#: Conventional alias (the planner/prompts routers export one too).
historian_router = router


# -- plumbing ----------------------------------------------------------------


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _runner_for(request: Request) -> "HistorianRunner":
    """The wired historian runner, or ``503`` when this app has none."""
    runner = getattr(request.app.state, "historian", None)
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail="historian unavailable: the engine wiring supplied no runner",
        )
    return runner


def _require_session(log: "EventLog", session_id: str) -> None:
    """404 unless the log knows the session (a fresh fork has a row, no events)."""
    try:
        known = log.get_session(session_id) or log.get_events(session_id)
    except Exception:  # noqa: BLE001 - unknown/unreadable reads as absent
        known = None
    if not known:
        raise HTTPException(status_code=404, detail="unknown session")


def _unwrap_historian(body: Any) -> Any:
    """The config from a request body: ``{"historian": {...}}`` or the bare dict."""
    if isinstance(body, Mapping):
        nested = body.get("historian")
        if isinstance(nested, Mapping):
            return dict(nested)
        return dict(body)
    return body


async def _json_body(request: Request) -> Any:
    """Parse the request body as JSON, 422 when it is not."""
    raw = await request.body()
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422, detail={"reason": f"body is not JSON: {exc}"}
        ) from exc


def _config(log: "EventLog") -> dict:
    """The ACTIVE preset's historian config (defaults merged in) — the ONE read."""
    try:
        preset = active_preset(log)
    except Exception:  # noqa: BLE001 - a bad store must not break the API
        preset = None
    return historian_config(preset)


# -- config ------------------------------------------------------------------


@router.get("/api/prompts/historian")
async def get_historian_route(request: Request) -> dict:
    """The active preset's historian config (defaults merged in)."""
    return {"historian": _config(_log_for(request))}


@router.put("/api/prompts/historian")
async def put_historian_route(request: Request) -> dict:
    """Validate + persist the historian config; ``422`` with the reasons when bad.

    Nothing is written on a rejection, so the stored config stays the last legal
    one (the planner/topology doors' discipline). On success the response is what
    a later GET serves — the store normalizes on write, so a client never sees a
    value it cannot read back.
    """
    log = _log_for(request)
    config, errors = normalize_historian_config(_unwrap_historian(await _json_body(request)))
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"reason": "historian config rejected", "errors": errors},
        )
    return {"historian": _write_config(log, config)}


def _write_config(log: "EventLog", config: Mapping[str, Any]) -> dict:
    """Persist ``config`` on the ACTIVE preset (or the default row) → stored read.

    Same append-only discipline as every other preset write (D9): the whole
    preset row is re-appended with the new ``historian`` block. The active preset
    is the row the engine's live cadence reader reads, so editing it is what
    changes behaviour; with no active pointer the persisted DEFAULT row is the
    engine's preset and :func:`set_default_historian` seeds it when missing.
    """
    target = None
    try:
        target = active_preset(log)
    except Exception:  # noqa: BLE001 - treat an unreadable store as "none active"
        target = None
    if target is None:
        return historian_config(set_default_historian(log, config))
    payload = preset_to_json(target)
    payload["preset_id"] = target.preset_id
    payload["name"] = target.name
    payload["historian"] = dict(config)
    append_preset(log, payload)
    stored = get_preset(log, target.preset_id)
    return historian_config(stored)


# -- the session's run state -------------------------------------------------


def _status_view(request: Request, log: "EventLog", session_id: str) -> dict:
    """The drawer's read: in flight? last run? how many turns until due?

    ``last_run`` is the TRACE (the latest row of any status — the run the owner
    last saw happen), while ``due_in`` reads the CLOCK (the last run that
    CONSUMED its window): a missed run advances no clock, so the drawer says
    "due now" on the turn that retries it, never "due in N turns" over a window
    nobody judged (§3.6.3).
    """
    runner = getattr(request.app.state, "historian", None)
    last_run = (
        runner.last_run(session_id)
        if runner is not None
        else _last_run_from_log(log, session_id)
    )
    cadence = _config(log)["cadence"]
    committed = (
        runner.committed_turns(session_id)
        if runner is not None
        else _committed_turns(log, session_id)
    )
    completed = (
        runner.last_completed_run(session_id)
        if runner is not None
        else _last_completed_run_from_log(log, session_id)
    )
    last = int((completed or {}).get("turn_seq") or 0)
    return {
        "running": bool(runner is not None and runner.inflight(session_id)),
        "last_run": last_run,
        "committed_turns": committed,
        "cadence": cadence,
        "due_in": max(0, cadence - (committed - last)),
    }


def _last_run_from_log(log: "EventLog", session_id: str) -> dict | None:
    """The latest ``historian_run`` on the chain (the read without a runner)."""
    from fabulist.state.historian import load_last_historian_run

    return load_last_historian_run(log, session_id)


def _last_completed_run_from_log(log: "EventLog", session_id: str) -> dict | None:
    """The last window-consuming run on the chain (the read without a runner)."""
    from fabulist.state.historian import load_last_completed_historian_run

    return load_last_completed_historian_run(log, session_id)


def _committed_turns(log: "EventLog", session_id: str) -> int:
    """Committed narratives on the chain (the due rule's left side)."""
    from fabulist.graph.nodes import committed_turn_count

    return committed_turn_count(log, session_id)


@router.get("/api/sessions/{session_id}/historian")
async def get_historian_status_route(session_id: str, request: Request) -> dict:
    """The session's historian state — the drawer's last-run line + button state.

    ``last_run`` is the latest ``historian_run`` row on the chain (fork-aware, the
    planner's ``load_last_plan`` read rule); ``due_in`` counts committed turns
    until the next automatic run and reads 0 when the historian is overdue.
    ``running`` is process-local truth: a run is in flight HERE.
    """
    log = _log_for(request)
    _require_session(log, session_id)
    return _status_view(request, log, session_id)


# -- the run door ------------------------------------------------------------


@router.post("/api/sessions/{session_id}/historian/run")
async def run_historian_route(session_id: str, request: Request) -> dict:
    """Run the historian NOW (synchronously), regardless of due-ness.

    The owner asked for it now, so the cadence clock is not consulted — a run
    always files, and the ``started`` row it appends becomes the new clock.
    ``409`` while another run for this session is in flight here; it is the
    DOUBLE-RUN guard, not a turn lock: the turn slot is deliberately not claimed,
    so a send issued meanwhile neither waits nor 409s (§3.6.7).

    The response is the run's own outcome (the runner's dict), which is the same
    shape the ``finished`` row carries. There is no run-total bound to map to an
    error status: a lane call that outlives its CONNECTION's ``timeout`` is a
    per-lane failure (``failed_lanes``) reported with the counts the other lanes
    filed, and only a runner-level failure carries ``error``.
    """
    log = _log_for(request)
    runner = _runner_for(request)
    _require_session(log, session_id)
    if runner.inflight(session_id):
        raise HTTPException(status_code=409, detail="historian already running")
    return await runner.run(session_id, trigger="manual")
