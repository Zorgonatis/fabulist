"""Turn inspector router (W3.6, SURFACE #23).

The inspector is a read-only trace view over the append-only log (D9): it walks
raw events with :meth:`EventLog.get_events` and pairs each ``user`` event with
the *next* ``narrative`` event, exposing per-turn provenance (latency, the
narrative's config snapshot) and per-turn activity (state deltas, validator
flags, revisions).

  GET /api/sessions/{id}/inspect/turns
      -> ``{"turns": [...], "usage": {...}, "notices": [...]}``
  GET /api/usage[?session_id=]
      -> log-backed per-session / agent / model token aggregate (I1)

Provenance source
-----------------
``make_post_turn_node`` records every ``narrative`` row with
``meta={"config": snapshot}`` where ``snapshot`` is
``{"model", "sampler", "prompt_version", "facts_hash"}`` plus optional
``{"recall", "facts_text", "regions"}`` (see
``fabulist.graph.nodes.DEFAULT_CONFIG_SNAPSHOT`` / ``wiring`` builder).
``EventLog.get_event`` / ``get_events`` surface that column via the
:class:`~fabulist.log.events.Event` ``meta`` attribute (populated by
``EventLog._row_to_event``), so the inspector reads ``event.meta["config"]``
directly — no payload key is involved. When a narrative carries no (or a
malformed) config snapshot the provenance fields are returned as ``None``; the
resolved ``model`` is then repaired from the ``system``/``usage`` events that
recorded the call (I1) and ``model_raw`` keeps the raw config value.

Range convention
----------------
``delta_count``, ``validation_flags`` and the per-turn ``tokens``/``calls``
count events whose ``seq`` falls in the half-open ``[user_seq, next_user_seq)``
window of the pair (the window is surfaced as ``window: {low, high}`` so the UI
can match agent captures to the same span). ``revision_count`` is global to the
session: revisions are appended *after* the narrative they supersede, so it
counts the rows whose ``Event.supersedes`` points at the narrative's ``id``.

The router reads the live :class:`~fabulist.log.EventLog` from
``request.app.state.log`` (the app factory stores it there), matching the other
routers, so wiring is one line in ``app.py``::

    from fabulist.api.inspector_api import router
    app.include_router(router)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request

from fabulist.api.attachments import _session_exists
from fabulist.api.cast_api import list_speaker_colors
from fabulist.api.recap_api import build_recap
from fabulist.cards.v2 import is_import_seed_text
from fabulist.log.degradation import DEGRADATION_EVENTS, WORLD_INFO_ADVISORY_ENTRY_LIMIT
from fabulist.log.schema_gate import USAGE_EVENT

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog
    from fabulist.log.events import Event


router = APIRouter(tags=["inspector"])

#: Default recall ``k`` the pre-turn autofetch uses. Kept in sync with
#: :func:`fabulist.memory.indexer.recall` (``k: int = 6``), which is the only
#: producer. Chat-config does not expose a per-chat override yet; when it does
#: the RAG panel reads it here instead (D25: query + k + rerank are surfaced).
RECALL_K_DEFAULT = 6

#: ``system`` payload ``event`` name carrying a chat's memory toggle (D16).
#: Folded locally (latest-wins, default on) so the inspector does not import the
#: memory-scope lane; the contract is fixed in ``api/chat_config_api``.
CHAT_MEMORY_EVENT = "chat_memory"

#: ``system`` payload ``event`` name carrying a chat's recall knobs (D25 C2):
#: ``{k, block_chars, rerank_enabled, query_mode, overfetch}``. Appended by the
#: memory lane's chat-config router; folded here for the RAG panel.
CHAT_MEMORY_CONFIG_EVENT = "chat_memory_config"

#: Defaults when no ``chat_memory_config`` row exists (match
#: :mod:`fabulist.memory.recall`: k=6, rerank off, raw-message query).
DEFAULT_RERANK_ENABLED = False
DEFAULT_QUERY_MODE = "message"
_QUERY_MODES = ("message", "message+scene")

#: Per-result text clip for the RAG panel ``results`` rows (W5C-1). Kept in
#: sync with :data:`fabulist.memory.recall._BULLET_TEXT_LIMIT`, so the panel's
#: clipped text matches what the rendered recall block shows inline.
RESULT_TEXT_LIMIT = 240


@router.get("/api/usage")
async def usage_snapshot(
    request: Request,
    session_id: str | None = Query(
        None, description="restrict the aggregate to one session"
    ),
) -> dict:
    """Log-backed per-agent / per-model usage (I1, D23).

    The append-only log is the source of truth (D9): per-call usage lands as
    ``system``/``usage`` events and this endpoint aggregates them per session /
    agent / model. ``?session_id=`` narrows to one session (404 when unknown).
    Without it, every non-reserved session is folded together. The in-RAM
    tracker's live snapshot rides along under ``memory`` as a process-local
    debug view (it resets on restart); the log numbers do not.
    """
    from fabulist.usage.track import get_tracker, usage_from_log

    log = getattr(request.app.state, "log", None)
    if session_id is not None:
        if log is None:
            raise HTTPException(status_code=500, detail="event log unavailable")
        if not _session_exists(log, session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        aggregate = usage_from_log(log, session_id)
        return {"source": "log", "session_id": session_id, **aggregate}
    aggregate = usage_from_log(log, None)
    return {"source": "log", **aggregate, "memory": get_tracker().snapshot()}

#: ``system`` payload ``event`` names that count as a validation flag. The
#: engine currently emits ``validator_flags`` (``graph/nodes.py``); the task's
#: spec names ``validation_flag``. Both spellings are accepted so the inspector
#: stays correct against real logs and against the spec contract.
VALIDATION_FLAG_EVENTS = frozenset({"validation_flag", "validator_flags"})


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _seconds_between(start_iso: str, end_iso: str) -> float:
    """Signed seconds from ``start_iso`` to ``end_iso`` (both ISO-8601).

    Subtracting two ``datetime`` values yields the latency; a naive/aware
    mismatch (one side missing an offset) is normalised by dropping tzinfo from
    both so the diff never raises.
    """
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    if (start.tzinfo is None) != (end.tzinfo is None):
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
    return (end - start).total_seconds()


def _config_snapshot(event: "Event") -> dict:
    """Return the narrative's ``meta["config"]`` snapshot, or ``{}``.

    The config lives in the row's ``meta`` column (not the payload) and is
    exposed through :attr:`Event.meta`; a missing or non-dict value degrades to
    an empty dict so the four provenance fields render as ``None``.
    """
    meta = event.meta if isinstance(event.meta, dict) else {}
    config = meta.get("config")
    return config if isinstance(config, dict) else {}


def _recall_settings(events: list["Event"]) -> dict:
    """Fold ``chat_memory_config`` system events latest-wins onto the defaults.

    Returns ``{k, rerank_enabled, query_mode}``. A malformed value is skipped
    (the contract is fixed in ``api/chat_config_api``); folded locally so the
    inspector never imports the memory lane.
    """
    settings: dict = {
        "k": RECALL_K_DEFAULT,
        "rerank_enabled": DEFAULT_RERANK_ENABLED,
        "query_mode": DEFAULT_QUERY_MODE,
    }
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != CHAT_MEMORY_CONFIG_EVENT:
            continue
        payload = event.payload
        k = payload.get("k")
        if isinstance(k, int) and not isinstance(k, bool) and k > 0:
            settings["k"] = k
        rerank = payload.get("rerank_enabled")
        if isinstance(rerank, bool):
            settings["rerank_enabled"] = rerank
        query_mode = payload.get("query_mode")
        if query_mode in _QUERY_MODES:
            settings["query_mode"] = query_mode
    return settings


def _collapse_ws(text: str) -> str:
    """Collapse all runs of whitespace (including newlines) to single spaces."""
    return " ".join(str(text).split())


def _clip(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars, appending an ellipsis when truncated."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\u2026"


def _recall_results(config: dict) -> list[dict]:
    """W5C-1: the per-result rows the recall stage actually surfaced this turn.

    Read from the narrative snapshot's ``recall_rows`` key (the engine unpacks
    :attr:`~fabulist.memory.recall.RecallResult.rows` into state; post_turn
    stamps it). Rows are projected to the wire shape ``{seq, text, distance}``
    with ``text`` whitespace-collapsed and clipped (:data:`RESULT_TEXT_LIMIT`).
    Legacy turns (pre-W5C) carry no rows -> ``[]`` rather than a guess.
    """
    raw = config.get("recall_rows")
    if not isinstance(raw, list):
        return []
    results: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        seq = row.get("seq")
        distance = row.get("distance")
        results.append(
            {
                "seq": int(seq)
                if isinstance(seq, (int, float)) and not isinstance(seq, bool)
                else None,
                "text": _clip(_collapse_ws(row.get("text") or ""), RESULT_TEXT_LIMIT),
                "distance": float(distance)
                if isinstance(distance, (int, float)) and not isinstance(distance, bool)
                else None,
            }
        )
    return results


def _read_layers(
    log: "EventLog | None", session_id: str | None
) -> tuple[list[str], list[str]]:
    """The D53 read set (collections + layer labels) this chat's recall queries.

    ``resolve_scope`` is a pure projection of the chat's ``chat_actor`` /
    ``world_ref`` / ``chat_memory`` rows (own layer + nested layers), so the RAG
    panel can show WHICH layers were read even on turns whose config snapshot
    predates this panel. Best-effort: no log/scope degrades to empty lists.
    """
    if log is None or not session_id:
        return [], []
    try:
        from fabulist.memory.scope import resolve_scope

        scope = resolve_scope(log, session_id)
    except Exception:  # noqa: BLE001 - the panel is additive, never fatal
        return [], []
    labels = [scope.layer, *(["actor"] * (len(scope.read_collections) - 1))]
    return list(scope.read_collections), labels


def _rag_panel(
    config: dict,
    user_text: str | None,
    events: list["Event"],
    collections: list[str] | None = None,
    layers: list[str] | None = None,
) -> dict:
    """Build the per-turn RAG panel (D25): query + recall block + settings.

    Prefers the values the recall path stamped onto ``meta.config.rag`` (the
    exact query text(s) and resolved config once the wiring lane stamps it),
    and otherwise falls back to the turn's own user post as the query plus the
    folded ``chat_memory_config`` settings. Additive and always present.

    D53 (extends D25, does not replace it): the panel also carries the read set
    the chat's layers resolve to — ``collections`` (own layer + nested layers)
    and their matching ``layers`` labels — preferring explicit snapshot keys
    when present, else the live-resolved values the caller passes in.
    """
    settings = _recall_settings(events)
    stamped = config.get("rag")
    stamped = stamped if isinstance(stamped, dict) else {}
    nested = stamped.get("config")
    nested = nested if isinstance(nested, dict) else {}

    # D25 C4: the exact queries the recall stage embedded land in the config
    # snapshot as ``recall_queries`` (engine unpacks RecallResult → state →
    # post_turn stamp). Older turns (pre-merge) carry none -> user-post
    # fallback keeps the panel populated.
    queries = stamped.get("queries")
    if not isinstance(queries, list) or not queries:
        queries = config.get("recall_queries")
    if not isinstance(queries, list) or not queries:
        queries = [user_text] if user_text else []
    queries = [str(q) for q in queries]

    recall_text = stamped.get("block") or stamped.get("recall_text") or config.get("recall")
    k = stamped.get("k", nested.get("k", settings["k"]))
    rerank = stamped.get("rerank_enabled", nested.get("rerank_enabled", settings["rerank_enabled"]))
    query_mode = stamped.get("query_mode", nested.get("query_mode", settings["query_mode"]))
    # D25 C4: prefer the snapshot's OWN resolved values (what the turn really
    # ran with) over the folded current settings; legacy turns fall back.
    snap_cfg = config.get("recall_config")
    if isinstance(snap_cfg, dict):
        k = snap_cfg.get("k", k)
        rerank = snap_cfg.get("rerank_enabled", rerank)
        query_mode = snap_cfg.get("query_mode", query_mode)

    # D53: prefer the read set stamped on the snapshot (when a future engine
    # pass carries it); otherwise use the live-resolved one the caller passed.
    snap_cols = config.get("recall_collections")
    if isinstance(snap_cols, list) and snap_cols:
        collections = [str(c) for c in snap_cols]
    snap_layers = config.get("recall_layers")
    if isinstance(snap_layers, list) and snap_layers:
        layers = [str(label) for label in snap_layers]

    return {
        "query": queries[0] if queries else (user_text or ""),
        "queries": queries,
        "recall_text": recall_text,
        "k": k,
        "rerank_enabled": rerank if isinstance(rerank, bool) else None,
        "query_mode": query_mode,
        "memory_enabled": _memory_enabled(events),
        # W5C-1: the actual per-result rows (seq/text/distance) recall surfaced.
        "results": _recall_results(config),
        # D53: the layers/collections recall actually read this chat's memory from.
        "collections": list(collections or []),
        "layers": list(layers or []),
    }


def _memory_enabled(events: list["Event"]) -> bool:
    """Fold ``chat_memory`` system events latest-wins; absent -> ``True`` (D16).

    Local to the inspector so it stays free of the memory-scope import; the
    event contract is fixed in :mod:`fabulist.api.chat_config_api`.
    """
    enabled = True
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != CHAT_MEMORY_EVENT:
            continue
        value = event.payload.get("enabled")
        if isinstance(value, bool):
            enabled = value
    return enabled


def _turn_view(
    user: "Event",
    narrative: "Event",
    events: list["Event"],
    collections: list[str] | None = None,
    layers: list[str] | None = None,
) -> dict:
    """Project one paired ``(user, narrative)`` to the wire turn shape (I2).

    The turn window is ``[user_seq, next_user_seq)`` — exclusive of the next
    turn's user event. ``post_turn`` appends state-deltas, validator flags, and
    the extractor/embedder usage AFTER the narrative event, so a window ending
    at ``narrative_seq`` would always read zero (flavor-probe finding,
    live-verified 2026-10-10). The window is surfaced as ``window`` so the UI
    can match agent captures (``agent_call`` system events) to the same span.

    Additive over the v1 shape: full ``user_text``/``narrative_text``,
    ``tokens`` (summed from ``system``/``usage`` events in the window — I1),
    ``calls`` (per-call breakdown), ``regions``/``facts_text`` (from the
    narrative config snapshot when the builder stamps them), ``recall_text``
    (from ``meta.config.recall``), resolved ``model`` (+ raw), validation flag
    detail, revision refs, and the RAG panel.
    """
    config = _config_snapshot(narrative)
    next_user = next(
        (
            event.seq
            for event in events
            if event.type == "user" and event.seq > user.seq
        ),
        None,
    )
    # no next user yet (latest turn): the window runs to the end of the log —
    # post_turn deltas/flags appended after this narrative still belong to THIS
    # turn's window.
    high = next_user if next_user is not None else max((e.seq for e in events), default=narrative.seq) + 1
    low = user.seq
    # high is EXCLUSIVE: next turn's user event is the next turn's window.
    window = [event for event in events if low <= event.seq < high]

    delta_count = sum(1 for event in window if event.type == "state-delta")
    # Wave 4b HB4: rolls persist dice+result+detail now (S4e); surface each
    # roll event in the turn window so the transcript chip has data inline.
    rolls = [
        {
            "seq": event.seq,
            "dice": (event.payload or {}).get("dice")
            if isinstance(event.payload, dict)
            else None,
            "result": (event.payload or {}).get("result")
            if isinstance(event.payload, dict)
            else None,
            "detail": (event.payload or {}).get("detail")
            if isinstance(event.payload, dict)
            else None,
            "ask": (event.payload or {}).get("ask")
            if isinstance(event.payload, dict)
            else None,
        }
        for event in window
        if event.type == "roll"
    ]
    validation_details = [
        dict(event.payload)
        for event in window
        if event.type == "system"
        and isinstance(event.payload, dict)
        and event.payload.get("event") in VALIDATION_FLAG_EVENTS
    ]
    revision_refs = [
        {
            "id": event.id,
            "seq": event.seq,
            "ts": event.ts,
            "actor": event.actor,
            "reason": (event.payload or {}).get("reason")
            if isinstance(event.payload, dict)
            else None,
        }
        for event in events
        if event.type == "revision" and event.supersedes == narrative.id
    ]

    calls: list[dict] = []
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for event in window:
        if (
            event.type != "system"
            or not isinstance(event.payload, dict)
            or event.payload.get("event") != USAGE_EVENT
        ):
            continue
        payload = event.payload
        prompt = int(payload.get("prompt_tokens") or 0)
        completion = int(payload.get("completion_tokens") or 0)
        total = int(payload.get("total_tokens") or (prompt + completion))
        tokens["prompt_tokens"] += prompt
        tokens["completion_tokens"] += completion
        tokens["total_tokens"] += total
        calls.append(
            {
                "agent": str(payload.get("agent") or ""),
                "model": str(payload.get("model") or ""),
                "seq": event.seq,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "latency_s": float(payload.get("latency_s") or 0.0),
            }
        )

    # Model resolution: a narrative config model is authoritative; the legacy
    # placeholder ("unknown" / absent — narratives built without a config
    # snapshot) is repaired from the usage events that recorded the real model.
    config_model = config.get("model")
    usage_model = next((call["model"] for call in calls if call["model"]), None)
    resolved_model = config_model
    if resolved_model in (None, "", "unknown"):
        resolved_model = usage_model

    user_text = user.payload.get("text") if isinstance(user.payload, dict) else None
    narrative_text = (
        narrative.payload.get("prose") if isinstance(narrative.payload, dict) else None
    )
    narrative_dsl = (
        narrative.payload.get("dsl") if isinstance(narrative.payload, dict) else None
    )
    rag = _rag_panel(config, user_text, events, collections, layers)
    recall_text = rag["recall_text"]

    return {
        "user_seq": user.seq,
        "user_ts": user.ts,
        "user_text": user_text,
        "narrative_seq": narrative.seq,
        "narrative_ts": narrative.ts,
        "narrative_text": narrative_text,
        "narrative_dsl": narrative_dsl,
        "latency_s": _seconds_between(user.ts, narrative.ts),
        "model": resolved_model,
        "model_raw": config_model,
        "sampler": config.get("sampler"),
        "prompt_version": config.get("prompt_version"),
        "facts_hash": config.get("facts_hash"),
        "facts_text": config.get("facts_text"),
        "regions": config.get("regions"),
        "recall_text": recall_text,
        "tokens": tokens,
        "calls": calls,
        "rolls": rolls,
        "delta_count": delta_count,
        "validation_flags": len(validation_details),
        "validation_details": validation_details,
        "revision_count": len(revision_refs),
        "revision_refs": revision_refs,
        "window": {"low": low, "high": high},
        "rag": rag,
    }


def _build_turns(
    events: list["Event"],
    log: "EventLog | None" = None,
    session_id: str | None = None,
) -> list[dict]:
    """Pair each ``user`` event with the next ``narrative`` event, in seq order.

    A user event with no following narrative is skipped (it has not produced a
    turn). Each qualifying user event yields one turn. ``log``/``session_id`` are
    optional: when supplied the RAG panel resolves the chat's D53 read layers
    live ONCE for the whole view (inspector display); callers that only pass
    events still get the panel, just without the live-resolved collections.
    """
    collections, layers = _read_layers(log, session_id)
    turns: list[dict] = []
    for index, event in enumerate(events):
        if event.type != "user":
            continue
        # A legacy card-import seed is not a player turn: skipping it keeps it
        # from pairing with the first narrative and inventing a bogus turn.
        payload = event.payload if isinstance(event.payload, dict) else {}
        if is_import_seed_text(payload.get("text")):
            continue
        narrative = next(
            (later for later in events[index + 1 :] if later.type == "narrative"),
            None,
        )
        if narrative is None:
            continue
        turns.append(_turn_view(event, narrative, events, collections, layers))
    return turns


def _usage(turns: list[dict], events: list["Event"]) -> dict:
    """Aggregate session usage: legacy per-turn model/actor counts + log totals.

    * ``models`` — turns per narrative (resolved) model (``None`` skipped);
    * ``agents`` — every event by its ``actor`` string (activity, not tokens);
    * ``calls`` — session token totals from the ``system``/``usage`` events;
    * ``by_agent`` / ``by_model`` — the log-backed aggregates (I1).
    """
    from fabulist.usage.track import aggregate_usage

    models: dict[str, int] = {}
    for turn in turns:
        model = turn["model"]
        if model is None:
            continue
        name = str(model)
        models[name] = models.get(name, 0) + 1

    agents: dict[str, int] = {}
    for event in events:
        agents[event.actor] = agents.get(event.actor, 0) + 1

    log_usage = aggregate_usage(events)
    return {
        "models": models,
        "agents": agents,
        "calls": {
            "calls": log_usage["calls"],
            "prompt_tokens": log_usage["prompt_tokens"],
            "completion_tokens": log_usage["completion_tokens"],
            "total_tokens": log_usage["total_tokens"],
        },
        "by_agent": log_usage["agents"],
        "by_model": log_usage["models"],
    }


#: How many degradation notices the inspector returns (newest last). A notice is
#: a per-failure row; the cap keeps a long-degraded session's payload bounded.
NOTICE_LIMIT = 50


def _notices(events: list["Event"]) -> list[dict]:
    """The session's degradation notices in seq order (issue #34).

    One entry per ``system`` row a swallow-site appended (:mod:`fabulist.log.
    degradation`): ``event`` names the degraded class, ``stage`` the step that
    failed, ``reason`` the short actionable line, ``turn`` the seq context the
    producer knew. ``seq`` is the notice's own seq — that is what the client
    acknowledges, so opening the failures tab can clear the badge without a
    second write to the log (the log stays append-only, ADR-001).
    """
    notices: list[dict] = []
    for event in events:
        if event.type != "system":
            continue
        payload = event.payload or {}
        name = payload.get("event")
        if name not in DEGRADATION_EVENTS:
            continue
        turn = payload.get("turn")
        hook = payload.get("hook")
        notice: dict[str, Any] = {
            "seq": event.seq,
            "ts": event.ts,
            "event": name,
            "stage": str(payload.get("stage") or ""),
            "reason": str(payload.get("reason") or payload.get("error") or ""),
            "turn": turn
            if isinstance(turn, int) and not isinstance(turn, bool)
            else None,
            "hook": hook if isinstance(hook, str) and hook else None,
        }
        # The world-info size advisory (issue #95) also carries its size, the
        # active/all ratio and the first five active entry names, so the
        # notices tab can name what filled the block instead of only its size.
        for key in ("chars", "threshold", "active", "total"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                notice[key] = value
        entries = payload.get("entries")
        if isinstance(entries, list):
            names = [item for item in entries if isinstance(item, str) and item]
            if names:
                notice["entries"] = names[:WORLD_INFO_ADVISORY_ENTRY_LIMIT]
        notices.append(notice)
    return notices[-NOTICE_LIMIT:]


@router.get("/api/sessions/{session_id}/inspect/turns")
async def inspect_turns(session_id: str, request: Request) -> dict:
    """Return the session's turn trace and aggregate usage.

    ``404`` when the session is unknown (no events and no ``sessions`` row).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    events = log.get_events(session_id)
    turns = _build_turns(events, log=log, session_id=session_id)
    # Wave 4b HB2/HB4: session-level player-HUD folds, additive to the payload.
    # ``speaker_colors`` is fork-aware (the registry's latest-wins fold).
    return {
        "turns": turns,
        "usage": _usage(turns, events),
        "recap": build_recap(events),
        "speaker_colors": list_speaker_colors(log, session_id),
        # Issue #34: the swallowed optional-service failures, so the Inspector's
        # notices tab (and the header badge) read ONE endpoint it already calls.
        "notices": _notices(events),
    }
