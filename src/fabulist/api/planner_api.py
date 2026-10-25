"""Planner router (W9.20 §7): the steering plane's config, plan and regenerate door.

The planner is an **infrequent turn step** (W9.20 §1) whose output is one ``plan``
event in the log (D9 — :mod:`fabulist.graph.plans`). This router is the Studio's
read/write surface over that data and the one place a plan can be produced
OUTSIDE a turn:

Endpoints
---------
  GET  /api/prompts/planner              -> {"planner": config}
  PUT  /api/prompts/planner              -> {"planner": config} | 422 {"detail": {...}}
  GET  /api/sessions/{id}/plan           -> {"plan", "render", "due_in", ...}
  PUT  /api/sessions/{id}/plan           -> {"plan", "render", "due_in", ...} | 422 | 404
  POST /api/sessions/{id}/plan/run       -> the same envelope; never an opening | 409 | 404
  POST /api/sessions/{id}/plan/regenerate-> {"plan", "opening", ...} | 409 | 404

Config (cadence / steer / field list) is preset data (W9.20 §6). ``GET`` reads the
ACTIVE preset's block through :func:`~fabulist.prompts.preset_store.planner_config`,
which fills the defaults for an absent row — so an unedited install needs no
migration. ``PUT`` validates through
:func:`~fabulist.prompts.preset_store.normalize_planner_config` (the one write-door
contract) and persists with the same preset-append discipline the topology door
uses: the ACTIVE preset's whole row is re-appended with the new ``planner`` block
(the prior value stays queryable, latest wins), or the persisted DEFAULT row is
seeded through :func:`~fabulist.prompts.preset_store.set_default_planner` when no
preset is active. The body may be the bare config or wrapped as
``{"planner": {...}}`` (the topology door's nesting) — a config never carries a
``planner`` key, so the two shapes are unambiguous.

Plan reads are fork-aware: :func:`~fabulist.graph.plans.load_last_plan` and
:func:`~fabulist.graph.nodes.committed_turn_count` both walk the CHAIN, so a
branch renders (and is due against) its parent's plan — the §2/M2 rule.

The two off-turn planner runs — turn-slot policy is ``POST /turns``'s for both
---------------------------------------------------------------------------
Both doors run the WIRED planner node off-turn: no user turn is faked, the real
node (the caller/directive/config seams wiring built) is invoked with a
``{"session_id"}`` state, so the plan event, the state seeding and the replacement
semantics are byte-identical to a due in-graph run. Both CLAIM the session turn
slot exactly like ``POST /turns``: ``409`` while a turn is still streaming
(pre-barrier), and they QUEUE behind a committed tail so the next context assembly
sees a finished log.

``POST .../plan/run`` (W9.23 §3.6.7) is the plain RUN-NOW door: a plan refresh on
an existing session, nothing else — no opening write, on any session.

``POST .../plan/regenerate`` is Regenerate Scenario (§7 / M5): the same refresh
PLUS, on a **zero-turn session** (session-LOCAL committed narratives === 0 — a fork
does not count its ancestor's turns, mirroring ``_latest_narrative``'s read), the
opening message: :func:`_opening_messages` is a dedicated builder (narrator system
contract + persona/world + the rendered plan, with NO user message), and the reply
is committed with the SAME narrative payload shape the ``commit`` node writes
(``{"prose", "dsl", "segments"}`` + ``meta.config``), so the transcript renders it
unchanged. Writing the opening stays Regenerate's exclusively — there is ONE
opening write path, so a run-now can never mint a second one.

Wiring (orchestrator-owned, one line)::

    from fabulist.api.planner_api import router as planner_router
    app.include_router(planner_router)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fabulist.graph.nodes import committed_turn_count
from fabulist.graph.plans import load_last_plan, render_plan_for_prompt, save_plan
from fabulist.graph.planner_schema import validate_planner_payload
from fabulist.graph.provider_errors import PhaseCallError
from fabulist.prompts.preset_store import (
    active_preset,
    append_preset,
    get_preset,
    normalize_planner_config,
    planner_config,
    preset_to_json,
    set_default_planner,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

router = APIRouter(tags=["planner"])

#: Conventional alias (the topology/prompts routers export one too).
planner_router = router

#: The stage name the planner node runs under (its compiled-graph node id) and
#: the label the off-turn run reports in ``plan_refresh_failed`` event text.
PLANNER_NODE_ID = "planner"

#: How long a regenerate holds the session turn slot on top of the planner call.
#: The slot itself is the lock; this cap only bounds the request (a hang would
#: otherwise hold the session forever). The planner is ONE model call.
REGENERATE_TIMEOUT_SECONDS = 300.0


class PlannerConfigInput(BaseModel):
    """Body of ``PUT /api/prompts/planner`` — the planner config to persist.

    ``planner`` is the stored shape the preset carries
    (``{"cadence": int, "steer": str, "fields": [field rows]}``). A bare config
    (no ``planner`` wrapper) is accepted too; see the module docstring.
    """

    planner: dict[str, Any] | None = None


# -- plumbing ----------------------------------------------------------------


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _require_session(log: "EventLog", session_id: str) -> None:
    """404 unless the log knows the session (a fresh fork has a row, no events)."""
    try:
        known = log.get_session(session_id) or log.get_events(session_id)
    except Exception:  # noqa: BLE001 - unknown/unreadable reads as absent
        known = None
    if not known:
        raise HTTPException(status_code=404, detail="unknown session")


def _unwrap_planner(body: Any) -> Any:
    """The config from a request body: ``{"planner": {...}}`` or the bare dict."""
    if isinstance(body, Mapping):
        nested = body.get("planner")
        if isinstance(nested, Mapping):
            return dict(nested)
        return dict(body)
    return body


def _unwrap_plan(body: Any) -> Any:
    """The payload from a plan-edit body: ``{"plan": {...}}`` or the bare dict."""
    if isinstance(body, Mapping) and isinstance(body.get("plan"), Mapping):
        return dict(body["plan"])
    return body


async def _json_body(request: Request) -> Any:
    """Parse the request body as JSON, 422 when it is not."""
    import json

    raw = await request.body()
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail={"reason": f"body is not JSON: {exc}"}) from exc


def _planner_cfg(log: "EventLog") -> dict:
    """The ACTIVE preset's planner config (defaults merged in) — the ONE read."""
    try:
        preset = active_preset(log)
    except Exception:  # noqa: BLE001 - a bad store must not break the API
        preset = None
    return planner_config(preset)


def _cadence(config: Mapping) -> int:
    cadence = config.get("cadence")
    return cadence if isinstance(cadence, int) and not isinstance(cadence, bool) else 1


def _due_in(plan: Any, committed_turns: int, cadence: int) -> int | None:
    """Turns until the planner runs again: ``cadence - (committed - plan.turn_seq)``.

    Floored at 0 (an overdue plan reads ``0``, never negative) and ``None`` when
    no plan exists yet — the caller then knows the next turn plans unconditionally
    ([OWNER] #2: turn 1 is mandatory).
    """
    if plan is None:
        return None
    return max(0, cadence - (committed_turns - int(plan.turn_seq)))


def _plan_view(log: "EventLog", session_id: str) -> dict:
    """The folded plan + its due-in budget (the GET/PUT response body)."""
    plan = load_last_plan(log, session_id)
    committed = committed_turn_count(log, session_id)
    cadence = _cadence(_planner_cfg(log))
    return {
        "plan": plan.to_dict() if plan is not None else None,
        "render": render_plan_for_prompt(plan) if plan is not None else None,
        "committed_turns": committed,
        "cadence": cadence,
        "due_in": _due_in(plan, committed, cadence),
    }


def _planner_node(engine: Any) -> Callable[..., Awaitable[dict]] | None:
    """The planner node the engine's COMPILED graph runs, or ``None``.

    The off-turn run must use the node wiring built (its narrator-class caller,
    the preset's directive reader, the live config/profile seams), not a copy —
    a second construction in this module would drift from the turn. The compiled
    LangGraph keeps each node's callable on its ``PregelNode.bound``; the graph is
    re-read per call because the engine swaps it when a topology/template edit
    changes the digest.
    """
    graph = getattr(engine, "_graph", None)
    nodes = getattr(graph, "nodes", None)
    if not isinstance(nodes, Mapping):
        return None
    pregel = nodes.get(PLANNER_NODE_ID)
    bound = getattr(pregel, "bound", None)
    for attr in ("afunc", "func"):
        fn = getattr(bound, attr, None)
        if callable(fn):
            return fn
    return None


def _narrator_caller(log: "EventLog", app: Any) -> Callable:
    """The narrator's model caller for the opening message (the real agent).

    Same chokepoint as the engine's narrator (``make_agent_caller``), resolved
    from the ACTIVE preset's ``narrate`` binding so a re-bound narrator is
    honoured. A module-level function so a harness can inject a scripted caller
    without a model server (the opening path is the only model call this router
    makes itself).
    """
    from fabulist.prompts.agent_sets import bindings_for, resolve_agent
    from fabulist.wiring import make_agent_caller

    registry = getattr(app.state, "registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503, detail="narrator registry unavailable (engine not wired)"
        )
    agent = "narrator"
    try:
        agent = resolve_agent(bindings_for(log), "narrate", fallback=agent)
    except Exception:  # noqa: BLE001 - preset lookup must never break the call
        pass
    return make_agent_caller(agent, registry, log)


def _config_snapshot(log: "EventLog", app: Any) -> dict:
    """The narrative meta ``commit`` stamps (model/sampler/prompt provenance).

    Mirrors ``build_engine``'s snapshot: the narrator connection's model and
    sampler plus the prompt version. Best-effort — an unresolvable connection
    falls back to the placeholder snapshot rather than failing an opening.
    """
    from fabulist.graph.nodes import DEFAULT_CONFIG_SNAPSHOT

    snapshot = dict(DEFAULT_CONFIG_SNAPSHOT)
    registry = getattr(app.state, "registry", None)
    try:
        from fabulist.prompts.agent_sets import bindings_for, resolve_agent

        agent = resolve_agent(bindings_for(log), "narrate", fallback="narrator")
        connection = registry.connection_for(agent)
        snapshot["model"] = connection.model
        snapshot["sampler"] = dict(connection.sampling or {})
        snapshot["prompt_version"] = 2
    except Exception:  # noqa: BLE001 - provenance is advisory, never fatal
        pass
    return snapshot


def _session_turn_count(log: "EventLog", session_id: str) -> int:
    """Committed narratives this session OWNS (session-local, not chain-aware).

    §7/M5: chain-inherited narratives do NOT count — a fork whose base already
    has turns must not get a second opening. Same read rule as
    ``app._latest_narrative`` (raw own rows).
    """
    try:
        events = log.get_events(session_id)
    except Exception:  # noqa: BLE001 - unreadable reads as zero turns
        return 0
    return sum(
        1
        for event in events
        if getattr(event, "type", None) == "narrative"
        and not getattr(event, "ephemeral", False)
    )


# -- config ------------------------------------------------------------------


@router.get("/api/prompts/planner")
async def get_planner_route(request: Request) -> dict:
    """The active preset's planner config (defaults merged in)."""
    return {"planner": _planner_cfg(_log_for(request))}


@router.put("/api/prompts/planner")
async def put_planner_route(request: Request) -> dict:
    """Validate + persist the planner config; ``422`` with the reasons when bad.

    Nothing is written on a rejection, so the stored config stays the last legal
    one (the topology door's discipline). On success the response is what a later
    GET serves — the store normalizes on write, so a client never sees a value it
    cannot read back.
    """
    log = _log_for(request)
    body = await _json_body(request)
    config, errors = normalize_planner_config(_unwrap_planner(body))
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"reason": "planner config rejected", "errors": errors},
        )
    return {"planner": _write_planner(log, config)}


def _write_planner(log: "EventLog", config: Mapping[str, Any]) -> dict:
    """Persist ``config`` on the ACTIVE preset (or the default row) → stored read.

    Same append-only discipline as every other preset write (D9): the whole
    preset row is re-appended with the new ``planner`` block. The active preset is
    the row the engine reads (``_planner_config`` in wiring), so editing it is
    what changes behaviour; with no active pointer the persisted DEFAULT row is
    the engine's preset and :func:`~fabulist.prompts.preset_store.set_default_planner`
    seeds it when missing.
    """
    target = None
    try:
        target = active_preset(log)
    except Exception:  # noqa: BLE001 - treat an unreadable store as "none active"
        target = None
    if target is None:
        return planner_config(set_default_planner(log, config))
    payload = preset_to_json(target)
    payload["preset_id"] = target.preset_id
    payload["name"] = target.name
    payload["planner"] = dict(config)
    append_preset(log, payload)
    stored = get_preset(log, target.preset_id)
    return planner_config(stored)


# -- the session's plan ------------------------------------------------------


@router.get("/api/sessions/{session_id}/plan")
async def get_plan_route(session_id: str, request: Request) -> dict:
    """The folded current plan (chain) + the turns until the next auto run."""
    log = _log_for(request)
    _require_session(log, session_id)
    return _plan_view(log, session_id)


@router.put("/api/sessions/{session_id}/plan")
async def put_plan_route(session_id: str, request: Request) -> dict:
    """Manual plan edit: validate against the ACTIVE field list, append ``manual``.

    ``plan.turn_seq`` is stamped here (the committed-turn count now), so a manual
    edit RESETS the cadence budget exactly like a planner run — the owner edited
    the steering, the next auto run is N turns away. A payload the active schema
    rejects (a missing/blank string set, an unknown key, a wrongly typed extra)
    is ``422`` with the reason; nothing is appended then.
    """
    log = _log_for(request)
    _require_session(log, session_id)
    body = await _json_body(request)
    config = _planner_cfg(log)
    turn_seq = committed_turn_count(log, session_id)
    try:
        plan = validate_planner_payload(
            _unwrap_plan(body),
            config.get("fields") or [],
            turn_seq=turn_seq,
            created_from="manual",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"reason": "plan rejected", "errors": [str(exc)]}
        ) from exc
    save_plan(log, session_id, plan)
    return _plan_view(log, session_id)


# -- the off-turn runs -------------------------------------------------------


@router.post("/api/sessions/{session_id}/plan/run")
async def run_plan_route(session_id: str, request: Request) -> dict:
    """Run planner now: one planner pass off-turn, and NO opening on any session.

    This is the ordinary plan refresh — the same call a due in-graph run makes,
    with the planning pane's turn-slot discipline (see :func:`_plan_refresh`).
    Regenerate Scenario keeps the zero-turn opening write exclusively, so a chat
    that has never played a turn gets its plan refreshed and its transcript left
    exactly as it was.
    """
    log = _log_for(request)
    await _plan_refresh(request.app, log, session_id, write_opening=False)
    return _plan_view(log, session_id)


@router.post("/api/sessions/{session_id}/plan/regenerate")
async def regenerate_plan_route(session_id: str, request: Request) -> dict:
    """Regenerate Scenario: run the planner off-turn (and the opening on turn 0).

    The plan refresh is :func:`_plan_refresh`'s (run-now's); the difference is the
    zero-turn session, whose fresh plan ALSO writes the opening message.
    """
    log = _log_for(request)
    _plan, opening = await _plan_refresh(
        request.app, log, session_id, write_opening=True
    )
    return {**_plan_view(log, session_id), "opening": opening}


async def _plan_refresh(
    app: Any, log: "EventLog", session_id: str, *, write_opening: bool
) -> tuple[Any, dict | None]:
    """Run the wired planner off-turn under the session turn slot.

    Returns ``(the refreshed plan, the opening message written or None)``. Turn-slot
    policy is ``POST /turns``'s (W9.19 A): ``404`` for an unknown session, ``409``
    while a turn is streaming pre-barrier, otherwise wait for the committed turn's
    background tail, then hold the slot for the duration of the planner call so no
    turn can interleave between the plan read and its append. ``503`` when the
    engine carries no compiled planner node; ``502`` when the planner produced no
    plan (the node logs ``plan_refresh_failed`` and keeps the previous plan — the
    request must not look like a success).

    ``write_opening`` is :func:`_session_turn_count`'s rule — only a session with no
    committed narrative of its OWN can be missing an opening, and only Regenerate
    asks for one (run-now refreshes the plan and touches no prose). When it is set
    the opening write happens INSIDE the slot, exactly as it always has: the plan
    refresh and the prose it steers are one operation from the session's point of
    view, and releasing the lock between them would let a turn interleave.
    """
    import asyncio

    from fabulist.api.app import _session_or_404

    session = _session_or_404(getattr(app.state, "sessions", {}), session_id, log)
    while (prior := session.turn) is not None and not prior.done.is_set():
        if not prior.committed.is_set():
            raise HTTPException(status_code=409, detail="turn in progress")
        await prior.done.wait()

    turn = _claim_turn(app, session)
    opening: dict | None = None
    try:
        try:
            plan, failure = await asyncio.wait_for(
                _run_planner(app, log, session_id), timeout=REGENERATE_TIMEOUT_SECONDS
            )
        except PhaseCallError as exc:
            # The node CONTAINS a bad reply but ABORTS on a provider failure (a
            # stale plan is the lie that rule exists to prevent), so an off-turn
            # run reports it with its own text instead of surfacing as a 500.
            raise HTTPException(
                status_code=502,
                detail={"reason": "planner provider failure", "error": str(exc)},
            ) from exc
        if failure is not None or plan is None:
            # The node is best-effort by contract (it keeps the PREVIOUS plan
            # steering); the request must still not look like a success.
            raise HTTPException(
                status_code=502,
                detail={"reason": "planner produced no plan", "error": failure},
            )
        if write_opening and _session_turn_count(log, session_id) == 0:
            opening = await asyncio.wait_for(
                _write_opening(log, app, session_id, plan),
                timeout=REGENERATE_TIMEOUT_SECONDS,
            )
    finally:
        _release_turn(turn)
    return plan, opening


def _claim_turn(app: Any, session: Any) -> Any:
    """Claim the session's turn slot (a bare ``_Turn``), mirroring ``post_turn``."""
    from fabulist.api.app import _Turn

    turn = _Turn()
    session.turn = turn
    return turn


def _release_turn(turn: Any) -> None:
    """Release the claimed slot: readers see the request as finished."""
    turn.done.set()
    turn.updated.set()


async def _run_planner(
    app: Any, log: "EventLog", session_id: str
) -> tuple[Any, str | None]:
    """Invoke the wired planner node off-turn → ``(folded plan, failure text)``.

    The node is called with a minimal state (``session_id`` only): everything it
    needs it reads from the log through the same seams a turn uses, and its stage
    writer is ``None`` outside a graph run (so no phantom stage frames). A raise
    inside the node is swallowed by the node itself (``plan_refresh_failed``), so
    this reads the outcome from the log: no NEW plan and a failure event means the
    run failed.
    """
    node = _planner_node(getattr(app.state, "engine", None))
    if node is None:
        raise HTTPException(
            status_code=503,
            detail="planner unavailable: the engine has no compiled planner node",
        )
    return await _call_planner_node(node, log, session_id)


async def _call_planner_node(
    node: Callable, log: "EventLog", session_id: str
) -> tuple[Any, str | None]:
    """Run ``node`` → ``(folded plan, the failure THIS run logged)``.

    Only events appended by this run are inspected, so a plan identical to the
    previous one is not mistaken for a failure.
    """
    before = load_last_plan(log, session_id)
    prior_seq = _last_seq(log, session_id)
    await node({"session_id": session_id})
    after = load_last_plan(log, session_id)
    failure = _refresh_failure_after(log, session_id, prior_seq)
    if failure is not None:
        return after if after is not None else before, failure
    return after, None


def _last_seq(log: "EventLog", session_id: str) -> int:
    """The session's newest event seq (0 when it has none / is unreadable)."""
    try:
        events = log.get_events(session_id)
    except Exception:  # noqa: BLE001 - an unreadable log yields no boundary
        return 0
    return int(events[-1].seq) if events else 0


def _refresh_failure_after(
    log: "EventLog", session_id: str, prior_seq: int
) -> str | None:
    """The ``plan_refresh_failed`` error text appended after ``prior_seq``."""
    try:
        events = log.get_events(session_id)
    except Exception:  # noqa: BLE001 - the failure report is advisory
        return None
    for event in reversed(events):
        if int(getattr(event, "seq", 0)) <= prior_seq:
            break
        payload = getattr(event, "payload", None) or {}
        if payload.get("event") == "plan_refresh_failed":
            return str(payload.get("error") or "")[:500] or "planner refresh failed"
    return None


async def _write_opening(
    log: "EventLog", app: Any, session_id: str, plan: Any
) -> dict:
    """Write the opening message a zero-turn session's fresh plan steers.

    A DEDICATED builder (never the narrator's turn builder): the narrator's
    system contract + the persona/world block + the rendered plan + the opening
    ask, and NO user message (§7: the ``user_content`` seam is not faked). The
    reply goes through the same deterministic DSL gate as a narrator pass, and is
    committed with the ``commit`` node's payload shape so ``segment.ts`` and the
    inspector render it unchanged.
    """
    from fabulist.graph.nodes import segment_narrative
    from fabulist.graph.types import collect_text
    from fabulist.graph.validation import validate_dsl

    caller = _narrator_caller(log, app)
    raw = await collect_text(caller, _opening_messages(log, session_id, plan))
    segments = validate_dsl(segment_narrative(raw)).segments
    prose = "\n".join(s["text"] for s in segments if s["kind"] == "prose")
    dsl = "\n".join(s["text"] for s in segments if s["kind"] == "dsl")
    event = log.append_event(
        session_id,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": dsl, "segments": segments},
        meta={"config": _config_snapshot(log, app), "opening": True},
    )
    return {
        "prose": prose,
        "dsl": dsl,
        "segments": segments,
        "seq": getattr(event, "seq", None),
    }


def _opening_messages(log: "EventLog", session_id: str, plan: Any) -> list[dict]:
    """The opening ask: ONE ``system`` message — no user turn (W9.20 §7).

    The narrator's own system contract, then the persona/world block, then the
    rendered plan, then the opening instruction, all joined and wrapped in the
    ``<preset_def>`` bound with the safety guard line (consolidation §5.1, issue
    #26 — the greeting surface gets the same guard + definition bound as the
    live narrate path). There is no per-turn runtime tail here, so the opening
    prompt is a valid def-only prompt: no ``<preset_runtime>`` block is
    emitted for a turn that does not exist yet.
    There is deliberately NO user message: no player said anything yet, so the
    ``user_content`` seam is not faked and no ``user`` event is written — the
    transcript shows the opening as the narrator's first narrative (exactly
    what a greeting-style card renders).

    Issue #39: the opening is a BUILT narrator prompt, so it reads the persisted
    Anti Injection setting per call (default OFF = no ``## Engine context``
    clause) exactly like the turn builder.
    """
    from fabulist.prompts.wrapper import wrap_preset
    from fabulist.state.settings import anti_injection_enabled
    from fabulist.wiring import narrator_system_prompt

    parts = [narrator_system_prompt(anti_injection=anti_injection_enabled(log))]
    try:
        from fabulist.context.roster import extract_cards, persona_block

        block = persona_block(extract_cards(log.get_chain_events(session_id)))
        if block:
            parts.append(block)
    except Exception:  # noqa: BLE001 - the card block is advisory
        pass
    parts.append(render_plan_for_prompt(plan))
    parts.append(
        "Write the OPENING of this story — no turns have happened yet. Establish "
        "the scene the PLAN describes: where it is, who is present, the weather "
        "and the time, and the tension the first string opens. End on a beat the "
        "player can answer. Prose and openui DSL only; do not speak or act for "
        "the player's character, and do not address the player out of character."
    )
    return [{"role": "system", "content": wrap_preset("\n\n".join(parts), guard=True)}]
