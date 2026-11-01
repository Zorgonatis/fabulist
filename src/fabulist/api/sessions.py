"""Session management router (W2.13a).

The log owns everything (D9): this router never mutates history. It *derives*
the session list from the append-only event log and expresses every user
intent as new rows.

  GET    /api/sessions          list visible sessions (id, title, updated_at, turns,
                                snippet) plus the ``persona_id`` /
                                ``persona_cleared`` / ``world_id`` /
                                ``memory_enabled`` / ``actor_id`` / ``actor_ids``
                                rail keys and the derived ``chat_id`` /
                                ``chat_size`` fold keys (issue #107), with optional
                                ``?persona_id=`` / ``?world_id=`` / ``?actor_id=``
                                scoping
  PATCH  /api/sessions/{id}     rename -> revision event superseding the title (D10)
  DELETE /api/sessions/{id}     soft-hide -> ``system`` event ``session_hidden``

Component-set selection (issues #16/#24) — PER-SESSION, a log fact:

  GET    /api/component-sets?session_id={id}
                                -> {active_set_id, default_set_id, sets: [{id, name,
                                    version, kind, loadable, reason}]}
  POST   /api/sessions/{id}/component-set {"set_id": str}
                                -> {session_id, set_id, name, version,
                                    previous_set_id, changed, applies}
                                The append is a ``set_swapped`` system row; the
                                narrator builder resolves it on the NEXT turn.
                                422 names the components the client cannot
                                render (``detail.unrenderable``).

Renames are revision events (D10): a newer payload supersedes the old title and
the original row stays queryable. Deletes are soft: they append a
``session_hidden`` system event; listing filters hidden sessions but every log
row survives (no destructive deletes, D9). The chat's derived memory-index rows
ARE removed from the shared layers (D5, issue #85).

Wiring (orchestrator-owned, one line)::

    from fabulist.api.sessions import router
    app.include_router(router)

The router reads the live :class:`~fabulist.log.EventLog` from
``request.app.state.log`` (the app factory stores it there), so no signature
change is needed in ``app.py``.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from fabulist.cards.v2 import is_import_seed_text
from fabulist.dsl import sets as dsl_sets
from fabulist.dsl.store import ComponentSetError, library_store
from fabulist.graph.compaction import has_compaction_marker
from fabulist.log.schema_gate import SET_SWAP_EVENT
from fabulist.memory.actor import fold_actors

from .memory_cleanup import delete_session_memory

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["sessions"])

#: System-event kinds that carry a session title anchor.
_TITLE_EVENTS = frozenset({"session_created", "session_renamed"})


class RenameRequest(BaseModel):
    """Body of ``PATCH /api/sessions/{id}``."""

    title: str


class ComponentSetSelectionRequest(BaseModel):
    """Body of ``POST /api/sessions/{id}/component-set``."""

    set_id: str


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _session_ids(log: "EventLog") -> list[str]:
    """All session ids known to the log, via the fork-bookkeeping table.

    ``EventLog`` exposes no public enumerator, so read the ``sessions`` table
    (which ``_ensure_session`` writes on a session's first event) through a
    short-lived secondary connection. WAL makes this a safe concurrent read.
    """
    conn = sqlite3.connect(log.path)
    try:
        rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    # Reserved ids (double-underscore prefix, e.g. the settings anchor session)
    # are internal bookkeeping, never user-visible chats.
    return [row[0] for row in rows if not row[0].startswith("__")]


def _summarize(log: "EventLog", session_id: str) -> dict | None:
    """Derive one session's title/visibility/turn-count/updated_at from rows.

    Returns ``None`` for an id with no events of its own (e.g. a fork row before
    its first append); those are not shown by the list.

    ``persona_cleared`` distinguishes an explicit clear from a never-set chat
    (both carry ``persona_id: None``): it is True only when the latest
    ``chat_persona`` row carries the ``cleared: true`` marker written by
    :func:`fabulist.personas.registry.clear_persona`.

    TWO reads, by design (issue #86): the session-CONSTANT config half (persona,
    world, memory toggle) folds over the fork-aware chain, so a branch lists the
    values its parent had at the cut -- the same values the engine already
    resolves (:func:`fabulist.personas.registry.chat_persona_state`,
    :mod:`fabulist.context.facts`), so the rail badge and the prompt agree. The
    title/visibility/turn-count half stays OWN-row: ``_title_event`` is the
    rename revision's TARGET, and a rename on a branch must supersede the
    branch's own title carry row, never the parent's (#86 constraint).
    """
    events = log.get_events(session_id)
    if not events:
        return None
    # The config half is session-constant context: chain-walked like the engine.
    # Reserved-anchor rows (``__personas__`` etc.) never appear here -- the walk
    # is over this session's fork ancestry only.
    chain = log.get_chain_events(session_id)

    title = ""
    hidden = False
    title_event = None
    title_payload: dict | None = None
    updated_at: str | None = None
    turns = 0
    # Issue #107: the reply this session's OWN rows carry, for the chat's
    # versions dropdown. Same rule as the branch snippet
    # (:func:`fabulist.api.branches._first_prose`): inherited narrative is
    # ignored, so the snippet says what THIS line answered. Free here -- the
    # own-row read below already visits every event.
    snippet = ""
    # D16 derived config (latest-wins folds over the chat's system events):
    # parent persona + per-chat embedding opt-out ride the list row so the
    # drawer groups without per-session round-trips. D32's ``world_ref``
    # snapshot rides along for the same reason (the world filter reads it).
    persona_id: str | None = None
    #: Explicit "no persona" marker (``chat_persona`` with ``cleared: true``).
    #: ``persona_id`` stays ``None`` for both a clear and a never-set chat; this
    #: boolean is what lets the rail's persona picker distinguish them
    #: (owner ruling: clear = no persona, not "use the default again").
    persona_cleared = False
    world_id: str | None = None
    memory_enabled = True
    # D53: the chat's ACTOR(s) are a separate record from the user persona
    # (``chat_persona``). Folded chain-aware so a branch inherits its parent's
    # actor record with a branch-local ``chat_actor`` row superseding it. The
    # first id is the row's primary actor (badge/grouping); the full tuple is
    # kept for the ``?actor_id=`` membership scope.
    actor_ids = fold_actors(log, session_id)
    # W9.26 §2.6 (⚑B): ``compacted`` marks the SUPERSEDED era -- the chat whose
    # history was folded into a later one. It is branches-derived (a child whose
    # OWN rows carry the era marker), NOT the session's own marker: the era child
    # that continues the story is the LIVE row and stays a normal chat in the
    # rail. A parent with no marker-bearing child is not compacted, and the era
    # child itself stops being "live" the day it is compacted in turn.
    compacted = any(
        has_compaction_marker(log, child) for child in log.list_branches(session_id)
    )

    for event in events:
        updated_at = event.ts
        if event.type == "user" and not event.ephemeral:
            # A legacy card-import seed is not a player turn. New imports write
            # no ``user`` row at all (cards.to_session_seed); the old ones stay
            # in the log (D9) but must not inflate the rail's turn count.
            payload = event.payload if isinstance(event.payload, dict) else {}
            if not is_import_seed_text(payload.get("text")):
                turns += 1
        elif event.type == "system":
            kind = event.payload.get("event")
            if kind == "session_hidden":
                hidden = True
            elif kind in _TITLE_EVENTS and isinstance(event.payload.get("title"), str):
                title = event.payload["title"]
                title_event = event
                title_payload = dict(event.payload)
        elif event.type == "revision":
            revised = event.payload.get("revised")
            if isinstance(revised, dict) and isinstance(revised.get("title"), str):
                title = revised["title"]
                title_event = event
                title_payload = dict(revised)
        if event.type == "narrative" and not event.ephemeral and not snippet:
            payload = event.payload if isinstance(event.payload, dict) else {}
            prose = payload.get("prose") or payload.get("text") or ""
            if isinstance(prose, str) and prose.strip():
                snippet = prose.strip()[:140]

    # Chain-walked config: latest-wins across the inherited rows (at/below the
    # fork cut) and the branch's own rows (a carried row or a branch-local
    # write), in seq order.
    for event in chain:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        kind = event.payload.get("event")
        if kind == "chat_persona":
            # A real id sets the assignment; an explicit clear
            # (``persona_id: null`` + ``cleared: true``) leaves ``persona_id``
            # ``None`` (same as never-set) and flags ``persona_cleared``.
            persona_cleared = event.payload.get("cleared") is True
            value = event.payload.get("persona_id")
            persona_id = value if isinstance(value, str) else None
        elif kind == "world_ref":
            value = event.payload.get("world_id")
            world_id = value if isinstance(value, str) and value else None
        elif kind == "chat_memory":
            enabled = event.payload.get("enabled")
            if isinstance(enabled, bool):
                memory_enabled = enabled

    return {
        "session_id": session_id,
        "title": title,
        "updated_at": updated_at,
        "turns": turns,
        "hidden": hidden,
        "persona_id": persona_id,
        "persona_cleared": persona_cleared,
        "world_id": world_id,
        "memory_enabled": memory_enabled,
        "actor_id": actor_ids[0] if actor_ids else None,
        "actor_ids": list(actor_ids),
        "compacted": compacted,
        # Issue #107: the versions dropdown's one-line answer for this member.
        "snippet": snippet,
        "_title_event": title_event,
        "_title_payload": title_payload,
    }


def _public(summary: dict) -> dict:
    """Project a summary to the wire shape (drops internal anchor fields)."""
    return {
        "session_id": summary["session_id"],
        "title": summary["title"],
        "updated_at": summary["updated_at"],
        "turns": summary["turns"],
        # D16/D32/D40: grouping, world layer and toggle state ride the list row
        # (drawer hierarchy) so the rail needs no per-session round-trips.
        "persona_id": summary.get("persona_id"),
        # Explicit-clear marker (always present): true only for a chat whose
        # latest ``chat_persona`` row carries ``cleared: true``. Backward
        # compatible: absent/False means "never set or assigned".
        "persona_cleared": summary.get("persona_cleared", False),
        "world_id": summary.get("world_id"),
        "memory_enabled": summary.get("memory_enabled", True),
        # D53: the chat's actor record rides the list row so the rail's ACTORS
        # layer groups/badges by actor (not by the user persona). ``actor_id``
        # is the primary/first actor (or None); ``actor_ids`` is the full set.
        "actor_id": summary.get("actor_id"),
        "actor_ids": summary.get("actor_ids", []),
        # W9.26 §2.6: True when this chat's history continues in a compaction era
        # (i.e. it is superseded). The drawer badges the row so a reader knows the
        # story lives in the continuation; a fresh engine/legacy log reads False.
        "compacted": summary.get("compacted", False),
        # Issue #107 (Option A): a chat is a READ-ONLY PROJECTION -- the root of
        # this session's fork chain plus every session that climbs to it. No
        # table, no event, no backfill; the rail folds by ``chat_id`` and marks
        # the group with ``chat_size``. A root (or a log with no forks) is its
        # own chat, so legacy logs group correctly as they are.
        "chat_id": summary.get("chat_id") or summary["session_id"],
        "chat_size": summary.get("chat_size", 1),
        # The chat's versions dropdown reads the reply each member owns.
        "snippet": summary.get("snippet", ""),
    }


def _chat_groups(log: "EventLog") -> tuple[dict[str, str], dict[str, int]]:
    """Derive every session's chat root, plus each group's size (issue #107).

    A chat is ONE READ-ONLY PROJECTION, never a stored unit: one root session
    plus every session that climbs to it through ``sessions.base_session``. No
    table, no event kind, no backfill (the approved Option A). The climb is the
    one the engine's cut canonicalisation already walks
    (:meth:`EventLog._fork_pivot`, repo.py) continued to the root, and the map
    is the engine's own single-read bookkeeping read
    (:meth:`EventLog._session_map`) -- so the rail and the engine agree on what
    one story is.

    Why the root and not the fork cut: a reroll forks the session the reader is
    ON, so three rerolls form a chain ``A -> B -> C -> D``. Every one of them
    descends from ``A``, which is the chat; the cut-level pivot is what the
    SWIPE group needs, not what the chat needs. A delete-tail fork also lands in
    the same group, because it forks a session of the same chain.

    Returns ``(roots, sizes)``: ``session_id -> chat_id`` and
    ``chat_id -> member count``. Hidden members are counted (the group is the
    log's, not the rail's view); the rail lists visible members and keeps the
    count honest with the log.
    """
    try:
        sessions = log._session_map()
    except Exception:  # pragma: no cover - defensive: no bookkeeping table
        return {}, {}
    roots: dict[str, str] = {}
    for session_id in sessions:
        anchor = session_id
        seen = {session_id}
        while True:
            info = sessions.get(anchor)
            parent = info.get("base_session") if info else None
            # Unknown parent = a dangling pointer; stop at the last known row so
            # a corrupt pointer can never hang the list.
            if not parent or parent in seen or parent not in sessions:
                break
            seen.add(parent)
            anchor = parent
        roots[session_id] = anchor
    sizes: dict[str, int] = {}
    for root in roots.values():
        sizes[root] = sizes.get(root, 0) + 1
    return roots, sizes


def _sync_registry(request: Request, session_id: str, title: str) -> None:
    """Best-effort mirror a rename into the app's in-memory registry.

    ``GET /events`` still serves the title from that registry, so updating it
    keeps the two views consistent within a process (the log stays truth).
    """
    registry = getattr(request.app.state, "sessions", None)
    if isinstance(registry, dict):
        session = registry.get(session_id)
        if session is not None:
            session.title = title


@router.get("/api/sessions")
async def list_sessions(
    request: Request,
    persona_id: str | None = Query(default=None),
    world_id: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
) -> dict:
    """List visible sessions, newest activity first, derived from the log.

    Optional ``?actor_id=`` / ``?world_id=`` / ``?persona_id=`` scope the list
    server-side (D39/D40) so the frontend stops fetching each chat's config to
    group. D53 splits the two records: ``?actor_id=`` is the **rail scope** — a
    session matches when the id is one of its folded ``chat_actor`` actors (a
    chat with several actors belongs to each); ``?world_id=`` matches the
    latest ``world_ref`` snapshot (D32); ``?persona_id=`` matches the latest
    ``chat_persona`` record, which D53 keeps as the **user** persona and no
    longer conflates with the cast. Each row also carries those keys (D40) so
    one call scopes and layers the rail. A present filter excludes sessions
    with no matching event; with no parameters the list stays unfiltered.
    """
    log = _log_for(request)
    # Issue #107: ONE bookkeeping read per list call serves the whole fold.
    chat_roots, chat_sizes = _chat_groups(log)
    summaries: list[dict] = []
    for session_id in _session_ids(log):
        summary = _summarize(log, session_id)
        if summary is None or summary["hidden"]:
            continue
        chat_id = chat_roots.get(session_id, session_id)
        summary["chat_id"] = chat_id
        summary["chat_size"] = chat_sizes.get(chat_id, 1)
        if persona_id is not None and summary.get("persona_id") != persona_id:
            continue
        if world_id is not None and summary.get("world_id") != world_id:
            continue
        if actor_id is not None and actor_id not in summary.get("actor_ids", ()):
            continue
        summaries.append(_public(summary))
    summaries.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    return {"sessions": summaries}


@router.patch("/api/sessions/{session_id}")
async def rename_session(
    session_id: str, body: RenameRequest, request: Request
) -> dict:
    """Rename a session by appending a non-destructive revision (D10)."""
    log = _log_for(request)
    summary = _summarize(log, session_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="unknown session")

    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title must be non-empty")

    target = summary["_title_event"]
    payload = summary["_title_payload"]
    if target is None or payload is None:
        # No title anchor yet (should not happen for a created session).
        log.append_event(
            session_id,
            "system",
            "user",
            {"event": "session_renamed", "title": title},
        )
    else:
        revised = {**payload, "title": title}
        revised.setdefault("event", "session_renamed")
        log.append_revision(
            session_id,
            target.id,
            "user",
            {"revised": revised, "reason": "rename"},
        )

    _sync_registry(request, session_id, title)
    return {"session_id": session_id, "title": title}


@router.delete("/api/sessions/{session_id}")
async def hide_session(session_id: str, request: Request) -> dict:
    """Soft-delete: hide from the list and drop its derived memory rows.

    The event log keeps every row (D9). The vector index is derived and
    droppable (D5), so this chat's rows are deleted from the shared layers
    (issue #85) — best-effort, never failing the hide.
    """
    log = _log_for(request)
    if _summarize(log, session_id) is None:
        raise HTTPException(status_code=404, detail="unknown session")
    log.append_event(
        session_id, "system", "user", {"event": "session_hidden"}
    )
    # Issue #85: the hide is the delete for the user, so the chat's derived
    # memory rows go with it. Best-effort: a missing store (memory off) is a
    # no-op and a store failure never fails the delete.
    delete_session_memory(request.app, session_id)
    return {"session_id": session_id, "hidden": True}

# --- component-set selection (issues #16/#24) -------------------------------
#
# Selection is PER-SESSION and the swap is a LOG FACT (owner ruling): this door
# validates the set, appends one ``set_swapped`` row and returns. It never
# mutates the running vocabulary — the narrator builder resolves the row at its
# NEXT ``build()`` (wiring.resolve_component_set → sets.session_snapshot), so the
# turn boundary stays the only place a prompt's vocabulary changes and a swap
# can never retarget another session.
#
# Cache consequence (ADR-025, "The cost, stated plainly"): the taught doc sits
# in the stable prefix, so a swap re-prefills everything after the library block
# ONCE (persona/cast, the frozen {{char}} snapshot, history, facts and the
# player's message; the doc is ≈12.6 KB ≈ 3.2k tokens). The per-set doc is
# byte-stable turn to turn, so this is a one-time cost per swap, not per turn.


def _component_set_rows() -> list[dict]:
    """The picker's catalog: every declared set with its render verdict."""
    rows: list[dict] = []
    for entry in dsl_sets.component_sets():
        verdict = dsl_sets.set_loadability(entry)
        rows.append(
            {
                "id": entry.id,
                "name": entry.name,
                "version": entry.version,
                "kind": entry.kind,
                "loadable": verdict.ok,
                "reason": verdict.reason,
            }
        )
    return rows


@router.get("/api/component-sets")
async def list_component_sets(
    request: Request, session_id: str | None = Query(default=None)
) -> dict:
    """The component-set catalog, plus the set ``session_id`` currently uses.

    ``loadable`` is the ADR-025 seam (components ⊆ the client's union), the same
    verdict the POST enforces; a non-loadable row is shown disabled by the
    picker, never selected.
    """
    log = _log_for(request)
    if session_id is not None and _summarize(log, session_id) is None:
        raise HTTPException(status_code=404, detail="unknown session")
    active = (
        dsl_sets.session_set_id(log, session_id)
        if session_id is not None
        else dsl_sets.active_set_id()
    )
    return {
        "active_set_id": active,
        "default_set_id": dsl_sets.default_set_id(),
        "sets": _component_set_rows(),
    }


@router.post("/api/sessions/{session_id}/component-set")
async def select_component_set(
    session_id: str, body: ComponentSetSelectionRequest, request: Request
) -> dict:
    """Select this session's component set; it takes effect on the NEXT turn.

    Refusals are deliberate (ADR-025): a set the client cannot render is 422
    with the offending component names, and a set whose artifacts are unreadable
    is 422 with the readable reason — a swap that would strand the next turn
    never becomes a log row. Selecting the set already in effect is a no-op
    (``changed: false``) so a redundant click cannot cost a prefix re-prefill.
    """
    log = _log_for(request)
    if _summarize(log, session_id) is None:
        raise HTTPException(status_code=404, detail="unknown session")
    set_id = body.set_id.strip()
    if not set_id:
        raise HTTPException(status_code=422, detail="set_id must be non-empty")
    entry = dsl_sets.component_set(set_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown component set: {set_id}")

    verdict = dsl_sets.set_loadability(entry)
    if not verdict.ok:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": verdict.reason,
                "set_id": entry.id,
                "unrenderable": list(verdict.unrenderable),
            },
        )
    try:
        snapshot = library_store().require_snapshot(entry.directory)
    except ComponentSetError as exc:
        raise HTTPException(
            status_code=422,
            detail={"reason": str(exc), "set_id": entry.id, "unrenderable": []},
        ) from exc

    previous_set_id = dsl_sets.session_set_id(log, session_id)
    if snapshot.set_id == previous_set_id:
        return {
            "session_id": session_id,
            "set_id": snapshot.set_id,
            "name": snapshot.name,
            "version": entry.version,
            "previous_set_id": previous_set_id,
            "changed": False,
            "applies": "next_turn",
        }

    fact = dsl_sets.swap_fact(previous_set_id, snapshot)
    log.append_event(
        session_id,
        "system",
        "user",
        {
            "event": SET_SWAP_EVENT,
            "previous_set_id": fact.previous_set_id,
            "set_id": fact.set_id,
            "name": fact.name,
            "version": fact.version,
            # Frozen now: a later rewording cannot rewrite what this turn's
            # prompt carried (facts.set_swap_notice reads THIS text).
            "notice": dsl_sets.swap_notice_text(snapshot),
        },
    )
    return {
        "session_id": session_id,
        "set_id": fact.set_id,
        "name": fact.name,
        "version": entry.version,
        "previous_set_id": fact.previous_set_id,
        "changed": True,
        "applies": "next_turn",
    }
