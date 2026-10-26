"""Revision (message-edit) router (SURFACE #6).

The event log owns everything (D9) and corrections are appends (D10): editing a
message never mutates the original row. This router appends a ``revision`` event
whose ``revised`` payload fully replaces the target's payload; the projection's
latest-wins resolver (:meth:`EventLog._resolve_payloads`) then renders the
edited text. The target row stays queryable forever.

  POST /api/sessions/{id}/revisions   edit one user/narrative event
  GET  /api/sessions/{id}/revisions   list the session's revision rows

Editable types are the two that carry user-visible text:

* ``user``      -> ``{"text": new_text, **<other original keys>}``
* ``narrative`` -> ``{"prose": new_text, **<other original keys>}``

A modern ``narrative`` payload also carries ``segments`` (the ordered
prose/DSL interleave), and the transcript renders THE SEGMENTS, not the
joined ``prose`` — so rewriting only ``prose`` was an invisible no-op. The
revision therefore rewrites ``segments`` too, by this deterministic rule
(:func:`_revise_segments`): the FIRST ``kind == "prose"`` segment takes the
new text and every later prose segment is dropped (the user edited the whole
prose as one block); ``dsl`` segments are preserved verbatim and in order
(panels are not the prose channel); a payload with no prose segment at all
gets a prose segment prepended. A narrative payload WITHOUT ``segments`` is
rewritten exactly as before (no ``segments`` key is invented), and ``user``
rows are untouched by this rule.

Any other event type (``asset``, ``system``, ``state-delta``, ...) is refused
with a ``422`` and a machine-readable reason; those rows are not free text.

Fork views (issue #90)
----------------------
A branch renders its ancestors' pre-cut events through the chain
(:meth:`EventLog.get_projection` -> ``_stream_events``), so those messages carry
an edit control. The target is therefore resolved through
:meth:`EventLog.get_chain_events` — the same fork-aware walk — not through the
session-LOCAL :meth:`EventLog.get_events`. A fork editing an inherited event
appends the revision to the FORK (the URL's session): the edit is branch-local,
so the parent's own row, and the parent's own projection, keep the original
text. ``EventLog.append_revision`` resolves the target id globally, so a branch
revision superseding a parent row appends without a repo change, and
``_resolve_payloads`` folds it only in the stream that carries the revision row
(the fork's own).

The listing is chain-aware for the same reason: an inherited revision row is
what marks an inherited message as edited in a branch view.

The router reads the live :class:`~fabulist.log.EventLog` from
``request.app.state.log`` (the app factory stores it there), matching the other
routers, so wiring is one line in ``app.py``::

    from fabulist.api.revisions import router
    app.include_router(router)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fabulist.api.attachments import _session_exists

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


logger = logging.getLogger(__name__)

router = APIRouter(tags=["revisions"])

#: Event type -> the payload key that holds its editable text. Only these types
#: accept an edit; everything else is a 422.
EDITABLE_TEXT_KEYS: dict[str, str] = {
    "user": "text",
    "narrative": "prose",
}


class RevisionRequest(BaseModel):
    """Body of ``POST /api/sessions/{id}/revisions``."""

    target_id: int
    new_text: str
    reason: str = "user edit"


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _revise_segments(segments: list, new_text: str) -> list:
    """Rewrite the prose channel of a segmented narrative payload.

    The transcript renders ``segments``, so the edit has to land there. The user
    edits the whole prose as ONE block: the first prose segment takes the new
    text, later prose segments are dropped (their text is part of that block),
    and every ``dsl`` segment survives verbatim and in order — panels are a
    separate channel and are never merged into or reordered by a prose edit.
    A payload with no prose segment gets one prepended.
    """
    revised: list = []
    wrote_prose = False
    for segment in segments:
        kind = segment.get("kind") if isinstance(segment, dict) else None
        if kind == "prose":
            if wrote_prose:
                continue
            revised.append({**segment, "text": new_text})
            wrote_prose = True
            continue
        revised.append(segment)
    if not wrote_prose:
        revised.insert(0, {"kind": "prose", "text": new_text})
    return revised


def _replacement(text_key: str, original: dict, new_text: str) -> dict:
    """Build the full replacement payload: new text plus every other key.

    The revision carries the WHOLE payload (not a patch) so the projection swap
    is unconditional; all original keys except the edited one are preserved
    (e.g. a ``user`` row keeps its ``kind``/``image_asset_id``).

    A ``narrative`` row with ``segments`` additionally gets its prose segments
    rewritten (:func:`_revise_segments`) — the render path is segment-based. The
    joined ``dsl`` string is left as-is (it is the flat mirror of the dsl
    segments, and those are preserved).
    """
    revised = {key: value for key, value in original.items() if key != text_key}
    revised[text_key] = new_text
    if text_key == EDITABLE_TEXT_KEYS["narrative"]:
        segments = original.get("segments")
        if isinstance(segments, list):
            revised["segments"] = _revise_segments(segments, new_text)
    return revised


#: Strong refs for fire-and-forget reindex tasks — asyncio keeps only weak
#: task references, so an unreferenced one can be GC'd mid-flight silently.
#: Mirrors :data:`fabulist.api.attachments._STT_TASKS`.
_REINDEX_TASKS: set = set()


def _memory_target(request: Request, log: "EventLog"):
    """Resolve ``(store, endpoint, model)`` for reindexing, or ``None``.

    Mirrors :func:`fabulist.wiring.make_memorizer`: the embedder endpoint comes
    from ``registry.connection_for("embedder")`` (``base_url`` stripped of a
    trailing ``/v1``) and the index is the store beside the event log
    (``MemoryStore(log_path.parent / "memory.db")``). Memory is an optional
    enhancement, so a missing ``embedder`` entry (or no registry at all) yields
    ``None`` rather than raising.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return None
    try:
        conn = registry.connection_for("embedder")
    except Exception:  # noqa: BLE001 - missing entry = memory disabled
        return None
    from fabulist.memory.store import MemoryStore

    endpoint = conn.base_url.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[: -len("/v1")]
    store = MemoryStore(Path(log.path).parent / "memory.db")
    return store, endpoint, conn.model


def schedule_reindex(
    request: Request,
    log: "EventLog",
    session_id: str,
    target_seq: int,
    kind: str,
    revised_text: str,
) -> None:
    """Fire-and-forget re-embed of an edited event at its original seq.

    Mirrors :func:`fabulist.api.attachments.schedule_transcription` exactly:
    ``asyncio.create_task`` plus a module-level strong-ref set (asyncio keeps
    only weak task references), failures logged never raised, and the task
    discarded from the set in ``finally``. The revision event has already
    landed, so a failed reindex leaves the log correct — only the derived
    index is briefly stale (it is rebuildable, D5).
    """

    async def _run() -> None:
        store = None
        try:
            target = _memory_target(request, log)
            if target is None:
                return
            store, endpoint, model = target
            from fabulist.memory.indexer import reindex_revision
            from fabulist.memory.scope import resolve_scope

            # D53 write rule: the reindex lands at the chat's OWN layer
            # (``w_<world>`` / ``a_<actor>`` / ``c_<session>``); opted-out chats
            # never re-embed at all.
            scope = resolve_scope(log, session_id)
            if not scope.enabled:
                return
            await reindex_revision(
                store,
                endpoint,
                model,
                session_id,
                target_seq,
                kind,
                revised_text,
                collection=scope.write_collection,
                # D55: an edited USER row must keep its writer stamp, so the
                # revision path resolves it from the log like the live path.
                log=log,
            )
        except Exception:  # noqa: BLE001 - housekeeping must never surface
            logger.warning(
                "revision reindex failed for %s#%s", session_id, target_seq, exc_info=True
            )
        finally:
            if store is not None:
                store.close()
            _REINDEX_TASKS.discard(task)

    task = asyncio.create_task(_run())
    _REINDEX_TASKS.add(task)


@router.post("/api/sessions/{session_id}/revisions")
async def create_revision(
    session_id: str, body: RevisionRequest, request: Request
) -> dict:
    """Append one revision superseding ``target_id``; return the new row id.

    The target is resolved over the session's CHAIN
    (:meth:`EventLog.get_chain_events`), not its own rows: a fork view renders
    the inherited pre-cut events, so editing one of those must work from the
    branch. The revision is appended to the URL's session (the fork) — the edit
    is branch-local (issue #90).

    ``404`` for an unknown session or a target id absent from the session's
    chain; ``422`` for a non-editable event type (with the type in the detail).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    target = next(
        (
            event
            for event in log.get_chain_events(session_id)
            if event.id == body.target_id
        ),
        None,
    )
    if target is None:
        raise HTTPException(
            status_code=404, detail=f"unknown event id {body.target_id}"
        )

    text_key = EDITABLE_TEXT_KEYS.get(target.type)
    if text_key is None:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": f"event type {target.type!r} is not editable",
                "target_id": body.target_id,
                "type": target.type,
            },
        )

    revised = _replacement(text_key, target.payload, body.new_text)
    revision = log.append_revision(
        session_id,
        body.target_id,
        actor="user",
        payload={"revised": revised, "reason": body.reason},
    )
    # A ``user``/``narrative`` target (the only types with a text key above)
    # re-enters the derived index at its ORIGINAL seq so recall/search stop
    # returning the stale text (D5). Fire-and-forget: the revision is already
    # committed, and indexing is a rebuildable enhancement (D11).
    if target.type in EDITABLE_TEXT_KEYS:
        schedule_reindex(
            request, log, session_id, target.seq, target.type, body.new_text
        )
    return {
        "session_id": session_id,
        "revision_id": revision.id,
        "target_id": body.target_id,
    }


@router.get("/api/sessions/{session_id}/revisions")
async def list_revisions(session_id: str, request: Request) -> dict:
    """List the session's chain-visible ``revision`` rows in append order.

    Projection-independent: reads raw :meth:`EventLog.get_chain_events` (so a
    fork also lists the ancestors' revision rows for the events it inherits —
    that is the edited marker on an inherited message) and filters
    ``type == "revision"``. The superseded target id is the row's
    ``Event.supersedes`` field (the dataclass attribute / ``to_dict`` key), not
    a payload key — :meth:`EventLog.append_revision` stores it in the row's
    ``supersedes`` column.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    revisions = [
        {
            "id": event.id,
            "supersedes": event.supersedes,
            "reason": event.payload.get("reason", ""),
            "ts": event.ts,
        }
        for event in log.get_chain_events(session_id)
        if event.type == "revision"
    ]
    return {"session_id": session_id, "revisions": revisions}
