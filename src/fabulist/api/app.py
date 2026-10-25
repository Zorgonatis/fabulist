"""FastAPI application factory + endpoints for the Fabulist turn API (W1.4).

The turn engine is injected (``create_app(engine, log_path)``); the real graph
is wired by the orchestrator later. This module owns the HTTP surface, the
per-session turn lock, the SSE response, and the append to the event log that
gives the ``done`` event its log references.

Turn lifecycle
--------------
A successful ``POST /turns`` starts a producer task that pumps the engine's
``StreamEvent`` iterator into the session's :class:`ReplayBuffer`. The response
stream reads that buffer, so a client may disconnect and reconnect via
``GET /stream?lastEventId=N`` without interrupting the turn. Exactly one turn
may be active per session: a second ``POST /turns`` returns ``409``.
``POST /stop`` sets a cooperative stop flag; the producer cancels the pending
engine pull, emits a terminal ``error`` event (``reason: "interrupted"``), and
finishes. The user message is logged at turn start; the kept narrative is
logged when the engine yields ``done`` (partial/interrupted turns are not
committed).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable, Literal, Protocol, Required, TypedDict
from typing_extensions import Awaitable

import logging

logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
# Starlette's HTTPException, not FastAPI's subclass: ``StaticFiles`` raises the
# BASE class for a missing file, so catching the subclass would miss it.
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from fabulist.log import EventLog
from fabulist.log.degradation import (
    HOOK_DEGRADED_EVENT,
    INDEX_DEGRADED_EVENT,
    append_degradation,
    degradation_reason,
)
from fabulist.media.images import find_asset, image_content_part

from .sse import HEARTBEAT_SECONDS, ReplayBuffer, event_stream


class StreamEvent(TypedDict, total=False):
    """One streamed engine event (shared contract, W1.4)."""

    type: Required[str]  # "reasoning" | "content" | "done" | "error"
    delta: str
    meta: dict


class TurnEngine(Protocol):
    """Injected engine seam.

    Implementations are async-generator functions (``async def ...: yield``),
    declared here as returning an :class:`AsyncIterator` — the contract's
    ``async def run_turn(...) -> AsyncIterator[StreamEvent]``.
    """

    def run_turn(
        self, session_id: str, content: str, kind: str, *, image_url: str | None = None
    ) -> AsyncIterator[StreamEvent]: ...


class CreateSessionRequest(BaseModel):
    """Body of ``POST /api/sessions`` — a bare chat, with no world behind it.

    Unknown fields are REJECTED (422, naming the field) instead of silently
    dropped: a seeding that sent ``{"title", "world_id"}`` once got a 200 and a
    castless session (no roster, no lanes), which reads as a wiring bug rather
    than a wrong endpoint. A world chat is created by
    ``POST /api/worlds/{world_id}/chats``.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = ""
    #: D16: optional parent persona + per-chat embedding opt-out. Both ride
    #: create as their canonical system events (latest-wins folds read them).
    persona_id: str | None = None
    memory_enabled: bool = True


class TurnRequest(BaseModel):
    content: str
    kind: Literal["in_character", "ooc"] = "in_character"
    regenerate: bool = False  # fork replay: branch inherited its user message
    # Native vision path: an attached image asset referenced by id; the engine
    # receives it as an image_url content part (VLM narrator sees it directly).
    image_asset_id: str | None = None


class _Turn:
    """Live state for one in-flight turn."""

    def __init__(self) -> None:
        self.updated = asyncio.Event()
        self.done = asyncio.Event()
        self.stop = asyncio.Event()
        # W9.19 (A): set when the engine's commit barrier passed — the turn is
        # KEPT, the user may act again, and only the background tail remains.
        self.committed = asyncio.Event()
        self.task: asyncio.Task | None = None


class _Session:
    """App-level session registry entry (title + replay buffer + turn slot)."""

    def __init__(self, session_id: str, title: str) -> None:
        self.session_id = session_id
        self.title = title
        self.buffer = ReplayBuffer()
        self.turn: _Turn | None = None


async def _cancel(task: asyncio.Task) -> None:
    """Cancel a child task and swallow its terminal exception."""
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _emit(turn: _Turn, session: _Session, event: StreamEvent) -> None:
    """Append one engine event to the replay buffer and wake any readers."""
    session.buffer.append(event["type"], dict(event))
    turn.updated.set()


#: Explicit post-turn hook -> ``system`` DEGRADATION variant (W9.26 §2.8,
#: issue #34).
#:
#: The old rule was "``memory`` in the name ⇒ ``index_failure``, everyone else ⇒
#: ``compaction_failed``", which told lies: the assistant/historian hooks never
#: compacted anything, and ``compaction_failed`` is the name of a REAL pipeline
#: failure (the ⚑R3a forced-attempt counter reads those rows, so a mislabelled
#: hook could spend a compaction's retry budget). The monitor is pinned hardest
#: (§2.9): a crash in the GAUGE must never look like a failed compaction.
#:
#: Issue #34 renamed the two classes into the degradation vocabulary the
#: Inspector renders: the memory hook degrades INDEXING (``index_degraded``),
#: every other hook degrades HOUSEKEEPING (``hook_degraded``). The legacy
#: ``INDEX_FAILURE_EVENT`` / ``POST_TURN_FAILED_EVENT`` names stay valid in the
#: schema gate for rows already in a log; no producer writes them from here.
POST_TURN_FAILURE_EVENTS: dict[str, str] = {
    "memory_hook": INDEX_DEGRADED_EVENT,
    "assistant_turn_hook": HOOK_DEGRADED_EVENT,
    "historian_hook": HOOK_DEGRADED_EVENT,
    "context_monitor": HOOK_DEGRADED_EVENT,
}

#: Anything the table does not name (a test's fake, a future hook) files the
#: honest generic row rather than borrowing a name that means something else.
POST_TURN_FAILURE_DEFAULT = HOOK_DEGRADED_EVENT


#: The reader's next move per degradation class — the make-it-actionable half of
#: a notice (issue #34: "a short actionable reason").
_POST_TURN_ADVICE: dict[str, str] = {
    INDEX_DEGRADED_EVENT: (
        "this turn is not in the memory index yet — check the embedding connection"
    ),
    HOOK_DEGRADED_EVENT: "housekeeping for this turn did not finish",
}


def _post_turn_failure_event(name: str) -> str:
    """The failure variant one hook's crash files (see the table above)."""
    mapped = POST_TURN_FAILURE_EVENTS.get(name)
    if mapped is not None:
        return mapped
    # The historic substring rule is kept for the memory hook ONLY, so a
    # differently-named memory/index seam still files ``index_degraded``.
    if "memory" in name:
        return INDEX_DEGRADED_EVENT
    return POST_TURN_FAILURE_DEFAULT


def _append_post_turn_failure(
    log: EventLog,
    session: "_Session",
    hook: Callable[[str], Awaitable[None]],
    exc: BaseException,
) -> None:
    """Record a non-fatal post-turn hook failure as a system event (S4c/#34).

    The server journal already carries the traceback; this makes the failure
    visible in the event log (D9 source of truth) so the inspector can show it,
    instead of the log silently missing an index/compaction degradation.
    Best-effort: recording a failure must never itself break the turn.

    The variant is chosen by an EXPLICIT map (:data:`POST_TURN_FAILURE_EVENTS`,
    W9.26 §2.8 + issue #34): ``memory_hook`` files ``index_degraded``, every other
    hook — the context monitor above all — files ``hook_degraded`` with the
    hook's name in ``stage`` AND ``hook``, so the row says who failed instead of
    blaming a pipeline that never ran. The row is the SAME shape the other
    degradation sites write (``event``/``stage``/``reason``/``turn``), which is
    what lets the Inspector list all four from one read.
    """
    name = getattr(hook, "__name__", "post_turn")
    event = _post_turn_failure_event(name)
    seq = 0
    try:
        events = log.get_events(session.session_id)
        if events:
            seq = int(events[-1].seq)
    except Exception:  # noqa: BLE001 - seq context is optional
        seq = 0
    extra: dict = {"seq": seq}
    if event == POST_TURN_FAILURE_DEFAULT:
        # The typed shape names the culprit in `hook` (§2.9).
        extra["hook"] = name
    if not append_degradation(
        log,
        session.session_id,
        event,
        stage=name,
        reason=degradation_reason(exc, _POST_TURN_ADVICE.get(event, "")),
        turn=seq,
        extra=extra,
    ):
        logger.exception(
            "could not record post-turn failure for %s", session.session_id
        )


async def _post_turn_hook_task(
    hook: Callable[[str], Awaitable[None]], session: _Session, log: EventLog
) -> None:
    """Run ONE housekeeping hook in its own task; never raises.

    R4/G1: the post-turn hooks run CONCURRENTLY, one task each. Three slow
    proposal-lane calls must not delay compaction/memory/assistant housekeeping,
    and no hook may delay another — the hooks read disjoint things (compaction
    summarizes the OLDEST turns, memory embeds the just-settled turn, the
    assistant reads the settled log, the historian reads the window) and every
    append serializes at the single sqlite connection, so nothing here is
    correctness-ordered.

    Failure isolation is per hook: a raise is logged and recorded as that hook's
    own ``post_turn_failed`` row, and cancels nothing else.
    """
    try:
        await hook(session.session_id)
    except Exception as exc:  # housekeeping must never surface to the user
        logger.exception("post-turn hook failed for %s", session.session_id)
        _append_post_turn_failure(log, session, hook, exc)
    finally:
        # Discard THIS task by identity. A single shared slot discarded by
        # position loses the wrong task as soon as two post-turn task sets
        # overlap (two sessions, or several hooks of one turn).
        current = asyncio.current_task()
        if current is not None:
            _BACKGROUND_TASKS.discard(current)


def _schedule_post_turn(
    hooks: list[Callable[[str], Awaitable[None]]], session: _Session, log: EventLog
) -> None:
    """Fire-and-forget post-turn housekeeping; never blocks the SSE close.

    One task PER hook (see :func:`_post_turn_hook_task`). Every task is held in
    ``_BACKGROUND_TASKS`` — ``asyncio`` keeps only weak references to tasks, so
    an unreferenced one can be garbage-collected mid-flight (silent: the
    exception never logs). This is the likely reason the (never-observed)
    compaction hook never fired in production either.
    """
    for hook in hooks:
        task = asyncio.create_task(_post_turn_hook_task(hook, session, log))
        _BACKGROUND_TASKS.add(task)


_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _stage_step(meta: dict) -> dict | None:
    """Normalize one ``stage`` frame's meta into a persisted step record.

    Mirrors the client's ``foldTurnStep`` record (stores.ts): ``name`` is the
    node/agent identity the SSE frame calls ``stage``; ``index``/``total`` ride
    along for the derived percentage; ``notes`` is omitted when empty (the
    frame's own rule, W3B). Returns ``None`` for a frame that names no step
    (empty/foreign payload) — the client ignores those too (D42).
    """
    stage = meta.get("stage") if isinstance(meta.get("stage"), str) else ""
    label = meta.get("label") if isinstance(meta.get("label"), str) else ""
    if not stage and not label:
        return None
    state = "done" if meta.get("state") == "done" else "start"
    raw_index = meta.get("index")
    index = (
        raw_index
        if isinstance(raw_index, int) and not isinstance(raw_index, bool)
        else None
    )
    raw_total = meta.get("total")
    total = (
        raw_total
        if isinstance(raw_total, int) and not isinstance(raw_total, bool)
        else None
    )
    ts = meta.get("ts") if isinstance(meta.get("ts"), str) else ""
    raw_notes = meta.get("notes")
    notes = (
        [note for note in raw_notes if isinstance(note, str)]
        if isinstance(raw_notes, list)
        else []
    )
    step: dict = {
        "name": stage,
        "label": label,
        "state": state,
        "index": index,
        "total": total,
        "ts": ts,
    }
    if notes:
        step["notes"] = notes
    return step


def _fold_stage_frame(steps: list[dict], meta: dict) -> None:
    """Fold one ``stage`` frame into the ordered trace (mirrors ``foldTurnStep``).

    A ``start`` on ``plan`` (the FIRST step of a planner turn, W9.20 §5.6) opens a
    NEW turn, as does a ``start`` on ``lanes`` when this turn's trace holds no
    ``plan`` step yet (a planner-SKIP turn starts at the lanes). The lanes case is
    guarded on the trace because a planner turn emits ``plan start/done`` and THEN
    a mid-turn ``lanes start``: that one must not wipe the just-recorded plan step.
    ``steps`` is the per-turn list (``_drive_turn`` allocates a fresh one), so
    "this turn's trace" is exactly what it holds.

    A ``done`` closes the most recent OPEN step of the same name (a FAIL
    regeneration re-emits the critic's slot, so matching by name keeps the order
    honest). An unmatched ``done`` still records a closed step rather than being
    dropped. A ``start`` for an index the trace already holds REPLACES that slot
    in place — the narrator's repair loop re-opens ``narrate`` at its own index
    after the ``dsl`` step, and one slot keeps one row. Order is preserved.
    """
    step = _stage_step(meta)
    if step is None:
        return
    if step["state"] == "start" and (
        step["name"] == "plan"
        or (
            step["name"] == "lanes"
            and not any(prior["name"] == "plan" for prior in steps)
        )
    ):
        steps[:] = [step]
        return
    if step["state"] == "start":
        # A RE-OPENED stage replaces its slot in place. The narrator's repair
        # loop re-emits ``narrate`` at the SAME index with a "(repair N)" label
        # after the ``dsl`` step; appending would persist a duplicate,
        # out-of-order row (two index-3 steps, the second below index 4). The
        # trace is keyed by stage INDEX — one persisted row per slot — so the
        # re-opened start overwrites that slot and the order stays 1..8.
        if step["index"] is not None:
            for position, prior in enumerate(steps):
                if prior.get("index") == step["index"]:
                    steps[position] = step
                    return
    else:
        for i in range(len(steps) - 1, -1, -1):
            if steps[i]["name"] == step["name"] and steps[i]["state"] == "start":
                closed = dict(steps[i])
                closed["state"] = "done"
                closed["ts"] = step["ts"] or closed["ts"]
                if "notes" in step:
                    closed["notes"] = step["notes"]
                steps[i] = closed
                return
    steps.append(step)


def _latest_narrative(log: EventLog, session_id: str):
    """The session's most recent committed narrative row, or ``None``.

    Session-local (raw own rows): a turn commits its narrative into the session
    it is driving, so a regenerated branch's narrative appears here — never
    misattributed to an inherited ancestor row.
    """
    try:
        events = log.get_events(session_id)
    except Exception:  # noqa: BLE001 - seq context is optional
        return None
    for event in reversed(events):
        if event.type == "narrative" and not event.ephemeral:
            return event
    return None


def _record_turn_stages(
    log: EventLog,
    session_id: str,
    steps: list[dict],
    prior_narrative_id: int | None,
) -> None:
    """Append ONE ``turn_stages`` system event for a completed turn (D9).

    The ordered step trace otherwise lives only in the client store and is wiped
    the moment the view switches to a fork (a swipe/regenerate) — the reported
    "No steps recorded for this turn yet". Persisting it as a system event bound
    to the narrative seq it describes lets the projection rehydrate the header
    on reload and on branch switches. Flat, additive payload; latest-wins on
    read; never fatal to the turn. A turn that committed no NEW narrative
    (engine error after the user event) is skipped so a trace can never be bound
    to a previous turn.
    """
    if not steps:
        return
    latest = _latest_narrative(log, session_id)
    if latest is None or latest.id == prior_narrative_id:
        return
    try:
        log.append_event(
            session_id,
            "system",
            "system",
            {"event": "turn_stages", "steps": steps, "seq": int(latest.seq)},
        )
    except Exception:  # noqa: BLE001 - the trace is diagnostic, never fatal
        logger.exception("could not record turn stages for %s", session_id)


def _emit_done(
    turn: _Turn,
    session: _Session,
    kind: str,
    content_parts: list[str],
    engine_meta: dict,
) -> None:
    """Emit ``done``. The engine/graph owns the log commit — ``post_turn``
    for in-character turns, the OOC short-circuit for ``ooc``. The API layer
    never appends turn content (it previously duplicated every narrative as a
    bare ``{text}`` row alongside the graph's ``{prose, dsl, segments}`` row,
    breaking the interweave contract and making turn targets ambiguous)."""
    _emit(
        turn,
        session,
        {"type": "done", "meta": {**engine_meta, "session_id": session.session_id}},
    )


async def _drive_turn(
    turn: _Turn,
    session: _Session,
    engine: TurnEngine,
    log: EventLog,
    content: str,
    kind: str,
    context_monitor_hook: Callable[[str], Awaitable[None]] | None = None,
    memory_hook: Callable[[str], Awaitable[None]] | None = None,
    assistant_hook: Callable[[str], Awaitable[None]] | None = None,
    historian_hook: Callable[[str], Awaitable[None]] | None = None,
    regenerate: bool = False,
    image_url: str | None = None,
    image_kind: str = "image",
) -> None:
    """Pump one engine turn into the buffer; cooperative-stop aware.

    The full body runs under ``try``/``finally`` so ``done`` is always set —
    a failure before the first chunk must not hang connected readers.
    ``regenerate`` replays a turn on a fork whose user message is already
    inherited from the base chain (FORK-SEMANTICS F2): the user event is NOT
    appended again.
    """
    content_parts: list[str] = []
    steps: list[dict] = []
    agen: AsyncIterator[StreamEvent] | None = None
    # Every wired post-turn hook: the W9.26 CONTEXT MONITOR (the retired W2.9
    # threshold compactor's slot), memory, the assistant consult and the W9.23
    # historian. Each runs in its OWN task at the end of the turn
    # (:func:`_schedule_post_turn`), so none of them delays another.
    hooks = [
        hook
        for hook in (
            context_monitor_hook,
            memory_hook,
            assistant_hook,
            historian_hook,
        )
        if hook
    ]
    try:
        prior = _latest_narrative(log, session.session_id)
        prior_narrative_id = prior.id if prior is not None else None
        if not regenerate:
            log.append_event(
                session.session_id, "user", "user", {"text": content, "kind": kind}
            )
        agen = engine.run_turn(
                session.session_id, content, kind,
                image_url=image_url, image_kind=image_kind,
            )
        # D17 capture correctness: the pump drives the engine through per-event
        # ensure_future tasks, each copying THIS context — a contextvar set
        # inside the generator's frame does not survive the task switching
        # (found live: OOC turns silently lost their agent_call captures).
        # Set it here so every task — and any DebugCapture sink downstream —
        # inherits the session id.
        from fabulist.agents.debug import CURRENT_INTERACTION, CURRENT_SESSION

        CURRENT_SESSION.set(session.session_id)
        CURRENT_INTERACTION.set("ooc" if kind == "ooc" else "narrate")
        while True:
            next_task = asyncio.ensure_future(agen.__anext__())
            stop_task = asyncio.ensure_future(turn.stop.wait())
            try:
                finished, _ = await asyncio.wait(
                    {next_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                await _cancel(next_task)
                await _cancel(stop_task)
                raise
            if stop_task in finished and next_task not in finished:
                await _cancel(next_task)
                if turn.committed.is_set():
                    # W9.19 (A): the prose is already kept — stopping now only
                    # abandons background work. Report a quiet done, not an
                    # error on a committed turn.
                    _record_turn_stages(
                        log, session.session_id, steps, prior_narrative_id
                    )
                    _emit_done(
                        turn, session, kind, content_parts,
                        {"stopped_after_commit": True},
                    )
                    _schedule_post_turn(hooks, session, log)
                    return
                _emit(
                    turn,
                    session,
                    {"type": "error", "meta": {"reason": "interrupted"}},
                )
                return
            await _cancel(stop_task)
            try:
                event = next_task.result()
            except StopAsyncIteration:
                break
            if event["type"] == "content" and event.get("delta"):
                content_parts.append(event["delta"])
            if event["type"] == "done":
                _record_turn_stages(
                    log, session.session_id, steps, prior_narrative_id
                )
                _emit_done(turn, session, kind, content_parts, event.get("meta") or {})
                _schedule_post_turn(hooks, session, log)
                return
            if event["type"] == "error":
                _emit(turn, session, event)
                return
            if event["type"] == "committed":
                # The barrier: the turn is kept. The lock policy (post_turn)
                # stops rejecting and starts QUEUING on this event.
                turn.committed.set()
            if event["type"] == "stage":
                meta = event.get("meta")
                if isinstance(meta, dict):
                    _fold_stage_frame(steps, meta)
            _emit(turn, session, event)
        _record_turn_stages(log, session.session_id, steps, prior_narrative_id)
        _emit_done(turn, session, kind, content_parts, {})
        _schedule_post_turn(hooks, session, log)
    except Exception as exc:  # engine failure -> terminal error event
        _emit(turn, session, {"type": "error", "meta": {"reason": str(exc)}})
    finally:
        if agen is not None:
            with contextlib.suppress(Exception):
                await agen.aclose()
        turn.done.set()
        turn.updated.set()


def _sse_response(gen: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _session_or_404(
    sessions: dict[str, _Session], session_id: str, log: EventLog | None = None
) -> _Session:
    session = sessions.get(session_id)
    if session is None and log is not None:
        # Log-known sessions resolve lazily: a fresh fork has a sessions-table
        # row but no own events yet; seeded sessions may have either.
        known = log.get_session(session_id) or log.get_events(session_id)
        if known:
            session = _Session(session_id, "")
            sessions[session_id] = session
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return session


# --- W9.26 §2.3 step 1 / §2.6: the two POST /turns guards ------------------


def _compaction_claimed(app: FastAPI, session_id: str) -> bool:
    """Whether a compaction job holds this session's claim (await-free read).

    Reads the SAME registry ``wiring.build_app`` put on ``app.state`` and the
    monitor/router mutate (``claim_held`` is Lane D's helper, so the /turns guard
    and the claim protocol cannot drift). An app without the seam never claims.
    """
    from fabulist.graph.context_monitor import claim_held

    return claim_held(getattr(app.state, "compaction_claims", None), session_id)


def _compacting_conflict() -> HTTPException:
    """409 ``{detail:{reason:"compacting"}}`` — the pinned body the client parses."""
    return HTTPException(status_code=409, detail={"reason": "compacting"})


def compaction_successor(log: EventLog, session_id: str) -> str | None:
    """The TRANSITIVELY latest compaction successor of ``session_id``, or ``None``.

    W9.26 §2.6 (⚑B): once a session has been compacted its history continues in
    the era child, so a turn on the parent is refused and the client follows the
    successor (and replays its ask there). The chain is walked because an era can
    itself be compacted: ``A -> B -> C`` answers ``C`` for ``A``.

    Derived from the log only (§5 "single-engine-per-log"): the sessions table's
    ``base_session`` links give the children, and Lane A's
    :func:`fabulist.graph.compaction.has_compaction_marker` (OWN-row marker
    presence) separates a compaction era from an ordinary swipe fork — a fork of
    an era inherits the marker on the CHAIN, which is exactly why the test is
    own-rows. Several marker-bearing children (a hand-crafted log) resolve to the
    newest by ``created_ts``, deterministically.
    """
    from fabulist.graph.compaction import has_compaction_marker

    list_branches = getattr(log, "list_branches", None)
    get_session = getattr(log, "get_session", None)
    if not callable(list_branches):
        return None
    current = session_id
    seen = {session_id}
    while True:
        try:
            children = list_branches(current) or ()
        except Exception:  # noqa: BLE001 - an unreadable fork table is "no successor"
            return None if current == session_id else current
        candidates: list[tuple[str, str]] = []
        for candidate in children:
            if candidate in seen:
                continue
            try:
                if not has_compaction_marker(log, candidate):
                    continue
            except Exception:  # noqa: BLE001 - an unreadable child is not an era
                continue
            info = (get_session(candidate) if callable(get_session) else None) or {}
            candidates.append((str(info.get("created_ts") or ""), candidate))
        if not candidates:
            return None if current == session_id else current
        current = max(candidates)[1]
        seen.add(current)


_DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def _dev_origins() -> list[str]:
    """Dev-server CORS origins: the :5173 defaults plus ``FABULIST_DEV_ORIGINS``.

    The env var is a comma-separated list of extra origins (whitespace-trimmed,
    empty entries dropped) so a lane can point the frontend at a spare port
    without patching this module (the old /tmp runpy-wrapper workaround). Read at
    app build; an explicit ``allowed_origins`` argument still wins outright.
    """
    extra = os.environ.get("FABULIST_DEV_ORIGINS", "").split(",")
    return [*_DEV_ORIGINS, *(origin.strip() for origin in extra if origin.strip())]


#: The repository root: this file lives at ``<root>/src/fabulist/api/app.py``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: ``adapter-static``'s SPA output (see ``frontend/vite.config.ts``).
_FRONTEND_BUILD = _REPO_ROOT / "frontend" / "build"

#: The route prefix that must NEVER be answered by the SPA fallback.
_API_PREFIX = "/api"


class _SpaStaticFiles(StaticFiles):
    """``StaticFiles`` + the SPA fallback page ``adapter-static`` wrote.

    A SPA's client router owns every extension-less path (``/chat``,
    ``/chat``'s deep links), but ``StaticFiles(html=True)`` only answers ``/``
    and the files it holds — a hard load of ``/chat`` was a bare
    ``404 {"detail":"Not Found"}``. That matters here: ``/`` client-redirects
    to ``/chat``, so a refresh or a bookmark of the app's own screen died.

    The fallback is deliberately narrow (a wrong answer here would hide real
    failures):

    * only ``GET``/``HEAD``;
    * only a path with NO file extension (a missing ``/_app/.../x.js`` must stay
      a 404, not silently become HTML with the wrong content type);
    * never under ``/api/`` — an unknown API route stays a 404 JSON.

    Otherwise the response is unchanged, including the 404.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not self._answers_with_the_shell(scope):
                raise
            return await super().get_response("index.html", scope)

    @staticmethod
    def _answers_with_the_shell(scope: Scope) -> bool:
        """Whether a missing path is a CLIENT route (the SPA shell answers it)."""
        request_path = scope["path"]
        return (
            scope["method"] in ("GET", "HEAD")
            and request_path != _API_PREFIX
            and not request_path.startswith(_API_PREFIX + "/")
            and not Path(request_path).suffix
        )


def mount_frontend(app: FastAPI, build_dir: Path | None = None) -> Path | None:
    """Serve the built frontend at the SAME ORIGIN, or log and serve API only.

    W3a: the deployable artifact is ``frontend/build`` (adapter-static SPA, see
    ``frontend/vite.config.ts``), and the engine is the only server, so the UI
    and ``/api`` share an origin — which is what lets the client's ``API_BASE``
    default to ``""`` (relative paths) in a production build.

    MUST be called after every router is registered: a ``Mount`` at ``"/"``
    matches every path, so mounting earlier would shadow the whole API. The
    dev workflow is unaffected — with no build present (the ``vite dev`` case)
    this logs one line and the app is API-only.

    Returns the directory being served, or ``None`` when there is no build.
    """
    build = _FRONTEND_BUILD if build_dir is None else Path(build_dir)
    if not build.is_dir():
        # WARNING, not INFO: uvicorn's default logging config filters this
        # package's INFO records, and a silent "API only" boot is exactly the
        # failure this line exists to make visible.
        logger.warning(
            "frontend build not found at %s — serving API only",
            "frontend/build" if build_dir is None else build,
        )
        return None
    app.mount("/", _SpaStaticFiles(directory=str(build), html=True), name="web")
    logger.info("serving the built frontend from %s at /", build)
    return build


def create_app(
    engine: TurnEngine,
    log_path: Path,
    *,
    heartbeat: float = HEARTBEAT_SECONDS,
    allowed_origins: tuple[str, ...] | None = None,
    context_monitor_hook: Callable[[str], Awaitable[None]] | None = None,
    memory_hook: Callable[[str], Awaitable[None]] | None = None,
    assistant_hook: Callable[[str], Awaitable[None]] | None = None,
    historian_hook: Callable[[str], Awaitable[None]] | None = None,
) -> FastAPI:
    """Build the API over an injected turn engine and event-log path.

    ``heartbeat`` is the idle comment cadence in seconds (test/ops knob).
    ``allowed_origins`` defaults to the Vite dev-server origins — the frontend
    runs cross-origin (:5173 -> :2727) and dies without CORS headers.
    ``context_monitor_hook`` (W9.26 §2.1) is the measured context gauge —
    ``fabulist.graph.context_monitor.make_context_monitor_hook`` — run after a
    turn completes (fire-and-forget). It REPLACES the retired W2.9 threshold
    compactor: measuring is always on, and it owns the 80% notice and the 90%
    forced compaction. Its `app.state` seams (``compact_service``,
    ``context_jobs``, ``compaction_claims``, ``turn_slot``) are built by
    ``wiring.build_app``; a missing claims registry simply never claims.
    ``memory_hook`` is the post-turn vector index.
    ``assistant_hook`` is the D38 per-turn Assistant consult (fire-and-forget;
    silently returns when no assistant is enabled).
    ``historian_hook`` is the W9.23 §3.6 historian spawn (fire-and-forget): it
    checks the preset's cadence and runs the off-turn proposal lanes when due.
    The hooks are independent tasks (R4/G1) — a slow one never delays its
    siblings, and a raising one cancels nothing.
    """
    log = EventLog(log_path)
    sessions: dict[str, _Session] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        log.close()

    app = FastAPI(title="Fabulist API", lifespan=lifespan)

    # Session management endpoints (W2.13a) — list/rename/soft-delete over the
    # same log; the log owns everything (D9), this router only derives views.
    from fabulist.api.sessions import router as sessions_router
    from fabulist.api.worldinfo_api import router as worldinfo_router
    from fabulist.api.worlds_api import router as worlds_router
    from fabulist.api.profiles_api import router as profiles_router
    from fabulist.api.revisions import router as revisions_router
    from fabulist.api.search_api import router as search_router
    from fabulist.api.global_search_api import router as global_search_router
    from fabulist.api.imagegen_api import router as imagegen_router
    from fabulist.api.imagegen_defs_api import router as imagegen_defs_router
    from fabulist.api.export_api import router as export_router
    from fabulist.api.state_api import router as state_router
    from fabulist.api.inspector_api import router as inspector_router
    from fabulist.api.stt_api import router as stt_router
    from fabulist.api.personas_api import router as personas_router
    from fabulist.api.chat_config_api import router as chat_config_router
    from fabulist.api.agent_debug_api import router as agent_debug_router
    from fabulist.api.prompts_api import router as prompts_router
    # Wave 4b player-HUD reads: recap (H1) + cast colour registry (H5).
    from fabulist.api.recap_api import router as recap_router
    from fabulist.api.hud_api import router as hud_router
    from fabulist.api.cast_api import router as cast_router
    # R25: the d1-3b gate's question bank as an owner-editable asset.
    from fabulist.api.decision_queries_api import decision_queries_router
    # W9.29 §4: the BTS attribute catalog as an owner-editable asset.
    from fabulist.api.entity_catalog_api import entity_catalog_router
    # W9.22 §4: the review home — the proposal queue, the decide door and UNDO
    # (the ONE place an owner disposition becomes an asset write).
    from fabulist.api.proposals_api import proposals_router
    # W9.26 §2.2/§2.7: the compaction job doors + the per-task context ledger.
    # The seams they read (`compact_service`/`context_jobs`/`compaction_claims`/
    # `turn_slot`) are constructed by `wiring.build_app` onto app.state.
    from fabulist.api.context_api import router as context_router

    app.include_router(sessions_router)

    # Connections read/patch + hot re-configure (D25 C1) — model/sampler
    # changes without hand-editing configs/connections.json + restart.
    from fabulist.api.connections_api import router as connections_router

    app.include_router(connections_router)

    # Memory lifecycle (D25): destructive reset (the embedder-switch confirm
    # modal target) + the explicit background re-embed job.
    from fabulist.api.memory_api import router as memory_router

    app.include_router(memory_router)

    # Settings/toggles surface (W2.11) — directive toggles persisted as log
    # events in the reserved __settings__ session; the compiler reads them.
    from fabulist.api.settings import router as settings_router

    app.include_router(settings_router)

    # Appearance (issue #12, ADR-027) — the theme preset + one pasted-CSS
    # override. USER state, not the event log: a theme is not replayable history
    # and must never fork with a branch, so it lives in data/appearance.json
    # beside the log. The store is created on first request (app.state.appearance).
    from fabulist.api.appearance_api import router as appearance_router

    app.include_router(appearance_router)

    # Swipe/branch surface (W2.2) — fork mechanics per the maintainer's private notes (unshipped).
    from fabulist.api.branches import router as branches_router

    app.include_router(branches_router)

    # Image attachments (W2.8) — captioner path; asset + caption events.
    from fabulist.api.attachments import router as attachments_router

    app.include_router(attachments_router)

    # Assistants (W2.5) — persona consults; advisory system events.
    from fabulist.api.assistants_api import router as assistants_router

    app.include_router(assistants_router)

    # Character card import (W2.10b) — PNG/JSON -> seeded session.
    from fabulist.api.cards_api import router as cards_router

    app.include_router(cards_router)
    app.include_router(worldinfo_router)
    app.include_router(worlds_router)
    app.include_router(profiles_router)
    app.include_router(revisions_router)
    app.include_router(search_router)
    app.include_router(global_search_router)
    app.include_router(imagegen_router)
    app.include_router(imagegen_defs_router)
    app.include_router(export_router)
    app.include_router(state_router)
    app.include_router(inspector_router)
    app.include_router(stt_router)
    # D16/D17: personas (parent-of-chat + memory fence), chat memory toggle,
    # agent-capture interrogation. Routers mount beside the engine routers;
    # the shared /chat-config route lives ONLY in chat_config_api.
    app.include_router(personas_router)
    app.include_router(chat_config_router)
    app.include_router(agent_debug_router)
    from fabulist.api.flow_api import router as flow_router
    app.include_router(flow_router)
    # D18/D19: prompt surface — presets, modules, variables, preview.
    app.include_router(prompts_router)
    # W9.5 (R23): the turn topology's validated write door (the editor's PUT).
    from fabulist.api.topology_api import router as topology_router
    app.include_router(topology_router)
    from fabulist.api.node_templates_api import router as node_templates_router
    app.include_router(node_templates_router)
    # W9.20 §7: the planner (steering plane) — preset config, the folded plan,
    # manual edits and Regenerate Scenario.
    from fabulist.api.planner_api import router as planner_router

    app.include_router(planner_router)
    # W9.23 §3.6.7: the historian's control plane — the preset cadence, the
    # session's run state and the run-now door.
    from fabulist.api.historian_api import router as historian_router

    app.include_router(historian_router)
    app.include_router(hud_router)
    # Wave 4b H1/H5: player-facing recap + cast-colour registry reads.
    app.include_router(recap_router)
    app.include_router(cast_router)
    app.include_router(decision_queries_router)
    app.include_router(entity_catalog_router)
    app.include_router(proposals_router)
    app.include_router(context_router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins) if allowed_origins else _dev_origins(),
        # Personal localhost service; the ORIGIN allowlist is the real gate.
        # A method allowlist bit us three times (PATCH, then PUT — each left
        # browser flows silently dead while curl kept passing), so methods are
        # fully open. Do not reintroduce a method list without a test that
        # preflights every router's method set against it.
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.engine = engine
    app.state.log = log
    app.state.sessions = sessions

    @app.get("/health")
    async def health() -> dict:
        """Liveness plus the configuration a probe needs (issue #4).

        Dependency-free on purpose: no model call, no log write, no config
        reload — one ``stat`` at most — so it is safe to poll. It answers 200
        whenever the API answers, including a FIRST-RUN checkout with no
        configs document, because that is precisely the case it exists for:
        the body reports what resolved (narrator binding, unbound slots, the
        config path and whether it exists) instead of leaving a configuration
        problem to surface mid-turn. Connection NAMES only — never a key.
        """
        from fabulist.connections.registry import configuration_report

        return {
            "status": "ok",
            # ``wiring.build_app`` installs the store on app.state; reading it
            # here (not at construction) is what makes the report LIVE.
            **configuration_report(getattr(app.state, "registry", None)),
        }

    # Endpoints are ``async`` so the single sqlite connection stays on the
    # event-loop thread (EventLog connections are thread-bound).
    @app.post("/api/sessions")
    async def create_session(body: CreateSessionRequest) -> dict:
        session_id = uuid.uuid4().hex
        session = _Session(session_id, body.title)
        sessions[session_id] = session
        log.append_event(
            session_id,
            "system",
            "system",
            {"title": body.title, "event": "session_created"},
        )
        # D53: a chat's OWNER is its actor. An explicit ``persona_id`` names the
        # chat's ACTOR (the character it is *with*), recorded with the
        # ``chat_actor`` event and kept wholly apart from ``chat_persona`` —
        # which is the USER persona ({{user}}, D43) and stops being a memory
        # fence. The pinned-default path (no explicit id) still materializes the
        # SAME ``chat_persona`` (user-persona) row as before; a later explicit
        # clear row (``persona_id: null`` + ``cleared: true``) still supersedes
        # it latest-wins. NOTE: the default path does NOT seed a card — the pin
        # is the user's persona ({{user}}), not a character, so it must not be
        # injected into the roster/Opening.
        explicit_persona = body.persona_id is not None
        persona_id = body.persona_id
        if persona_id is None:
            from fabulist.api.settings import default_persona_id

            persona_id = default_persona_id(log)
        if persona_id is not None:
            from fabulist.personas.registry import list_personas

            personas = {p.persona_id: p for p in list_personas(log)}
            persona = personas.get(persona_id)
            if persona is None:
                if explicit_persona:
                    raise HTTPException(status_code=404, detail="unknown persona_id")
                # A pin left behind by a since-deleted persona is stale: skip
                # materialization (creation still succeeds) rather than 404.
            elif explicit_persona:
                # D53: the explicit persona is the chat's ACTOR/owner — record
                # it in the SEPARATE ``chat_actor`` event, NOT ``chat_persona``
                # (which is the user persona, {{user}}).
                from fabulist.memory.actor import append_chat_actor

                append_chat_actor(log, session_id, [persona_id])
                # Bug B: materialize the persona as a card seed event (the
                # import shape) so the Opening block and the narrator's
                # character block see the greeting + card fields. Seeded for
                # EVERY explicitly-persona'd chat; the frontend hides an
                # empty greeting. Seeding here (not in the worlds registry)
                # is deliberate: a persona-owned chat IS this persona's
                # scene, unlike a world's scenery cast members.
                from fabulist.cards.v2 import persona_to_seed_card

                log.append_event(
                    session_id, "system", "system", persona_to_seed_card(persona)
                )
            else:
                # Pinned-default path: the pin is the USER persona ({{user}}),
                # materialized near seq 2 so a later fork inherits it.
                log.append_event(
                    session_id,
                    "system",
                    "user",
                    {"event": "chat_persona", "persona_id": persona_id},
                )
        if body.memory_enabled is not True:
            log.append_event(
                session_id,
                "system",
                "user",
                {"event": "chat_memory", "enabled": False},
            )
        return {"session_id": session_id, "title": body.title}

    @app.get("/api/sessions/{session_id}/events")
    async def get_events(session_id: str) -> dict:
        session = _session_or_404(sessions, session_id, log)
        projection = log.get_projection(session_id)
        return {"session_id": session_id, "title": session.title, **projection}

    @app.post("/api/sessions/{session_id}/turns")
    async def post_turn(session_id: str, body: TurnRequest) -> StreamingResponse:
        session = _session_or_404(sessions, session_id, log)
        # --- W9.26 §2.3 step 1: CLAIM-FIRST ---------------------------------
        # A session whose compaction job holds the claim cannot take a turn: the
        # job is about to fork it, so this ask belongs to the successor era. The
        # check is the handler's first statement and involves no await, so a job
        # cannot acquire the claim behind it (the done/claim race). The client
        # shows "Compacting…" and retries (§2.2).
        if _compaction_claimed(app, session_id):
            raise _compacting_conflict()
        # --- W9.26 §2.6 (⚑B): a superseded ERA PARENT is read-only for turns --
        # Its history lives on in the compaction successor, so the client follows
        # it (TRANSITIVELY: an era can itself be compacted) and replays the ask
        # through the normal send path. Forking/reading the parent stays legal;
        # only turning it does not.
        successor = compaction_successor(log, session_id)
        if successor is not None:
            raise HTTPException(
                status_code=409,
                detail={"reason": "superseded", "id": successor},
            )
        # W9.19 (A): the turn lock covers only the user-facing phase. While a
        # turn is still STREAMING (pre-barrier) a second turn is refused as
        # before; once it is COMMITTED the next turn QUEUES behind the
        # background tail instead of 409ing — the tail must finish before the
        # next turn's context is assembled (the BTS fold is log-derived), and
        # the wait is bounded by the tail, which overlaps the user's reading.
        while (prior_turn := session.turn) is not None and not prior_turn.done.is_set():
            if not prior_turn.committed.is_set():
                raise HTTPException(status_code=409, detail="turn in progress")
            await prior_turn.done.wait()
        # --- one await-free block: re-check the claim, then claim the slot -----
        # The wait above can last a whole background tail, so the claim is
        # re-read HERE: if a compaction claimed the session while this request
        # waited, the turn is handed to the successor instead of racing the fork.
        if _compaction_claimed(app, session_id):
            raise _compacting_conflict()
        start_id = session.buffer.last_id
        turn = _Turn()
        session.turn = turn
        # Native vision: resolve the attached asset to a data URL the engine
        # forwards as an image content part. Unknown asset -> 404 before the
        # turn starts (no burned turns on bad references).
        image_url: str | None = None
        image_kind: str = "image"
        if body.image_asset_id:
            asset = find_asset(log, session_id, body.image_asset_id)
            if asset is None:
                raise HTTPException(status_code=404, detail="unknown image asset")
            payload = asset.get("payload", {})
            kind = payload.get("kind", "image")
            if kind not in ("image", "video"):
                # Audio is embedded-only (multimodal index); narrators do not
                # listen (STT parsing is W3.8).
                raise HTTPException(415, detail="only image/video assets ride the turn")
            mime = payload.get("mime") or "application/octet-stream"
            image_url = f"data:{mime};base64,{payload.get('bytes_b64', '')}"
            image_kind = kind
        turn.task = asyncio.create_task(
            _drive_turn(
                turn,
                session,
                engine,
                log,
                body.content,
                body.kind,
                context_monitor_hook,
                memory_hook,
                assistant_hook,
                historian_hook,
                body.regenerate,
                image_url,
                image_kind,
            )
        )
        return _sse_response(
            event_stream(
                session.buffer,
                turn.updated,
                turn.done,
                last_id=start_id,
                heartbeat=heartbeat,
            )
        )

    @app.post("/api/sessions/{session_id}/stop", status_code=202)
    async def stop_turn(session_id: str) -> dict:
        session = _session_or_404(sessions, session_id, log)
        if session.turn is not None:
            session.turn.stop.set()
        return {"status": "stopping", "session_id": session_id}

    @app.get("/api/sessions/{session_id}/stream")
    async def stream_session(
        session_id: str,
        last_event_id: int = Query(0, alias="lastEventId"),
    ) -> StreamingResponse:
        session = _session_or_404(sessions, session_id, log)
        if session.turn is None:
            raise HTTPException(status_code=404, detail="no turn to stream")
        turn = session.turn
        return _sse_response(
            event_stream(
                session.buffer,
                turn.updated,
                turn.done,
                last_id=last_event_id,
                heartbeat=heartbeat,
            )
        )

    # W3a: the built frontend LAST, so every route registered above wins the
    # match first (see mount_frontend). One origin, one server, no third process.
    mount_frontend(app)

    return app
