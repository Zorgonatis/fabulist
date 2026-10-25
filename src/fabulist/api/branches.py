"""Branch / swipe router (W2.2).

Forks are O(1) ``sessions`` rows (they copy no *history*; the repo carries the
parent's session-constant config rows after the cut -- see
``EventLog.fork_session``); this router exposes the four operations the chat UI
needs to drive swipes, "branch from here" and deleting a message tail:

  GET    /api/sessions/{id}/branches     children of ``id`` (fork point, snippet,
                                          created_at, visible/hidden, ``kind``)
  POST   /api/sessions/{id}/fork          fork a new branch at ``at_seq``
  POST   /api/sessions/{id}/delete-tail   fork just below a message and return the
                                          branch (issue #35: "delete messages
                                          N..end" as a branch-pointer move)
  DELETE /api/sessions/{id}/branch        hard-prune a branch (own events + row)

Deleting a message tail is NOT a rewrite (ADR-001/002): the tail's rows stay in
append-only history on the old line, and the operation is expressed as a fork
just below the pivot whose id the client switches to. It writes only the fork
bookkeeping row plus one ``system``/``session_renamed`` title-carry row on the
new branch (the pattern ``CompactService._start_era`` uses) -- no compaction
marker, so the old line stays in the same swipe group and is one swipe away.

W9.26 §2.6 (context management) adds the era distinction: a child whose OWN
rows carry a ``system``/``compaction`` marker is a CONTINUATION era, everything
else is a user swipe alternate. ``kind`` is derived with Lane A's
:func:`fabulist.graph.compaction.has_compaction_marker` (own rows -- a chain
read would label every fork of an era "compaction") and the ``branches`` list
KEEPS both kinds, labeled (a compaction child gets ``label``/
"compacted continuation" instead of a blank snippet). The ``siblings`` swipe
group, by contrast, EXCLUDES continuations: a swipe across eras would silently
move the reader into a different log.

All state lives in the event log (D9): the GET reads derived bookkeeping, the
POST appends one fork row, and the DELETE removes a branch's own rows and its
bookkeeping row. The soft ``session_hidden`` path in
:mod:`fabulist.api.sessions` still owns ordinary session deletion; this
``DELETE .../branch`` is the hard prune the fork doc requires (F4 — the ghost
row defect).

Wiring (orchestrator-owned, one line)::

    from fabulist.api.branches import router
    app.include_router(router)

Like the other routers, it reads the live :class:`~fabulist.log.EventLog` from
``request.app.state.log``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Iterable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from fabulist.cards.v2 import is_import_seed_text
from fabulist.graph.compaction import has_compaction_marker, session_title
from fabulist.log.repo import PruneError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog
    from fabulist.log.events import Event


router = APIRouter(tags=["branches"])

#: Characters of the branch's first prose kept for the UI list.
SNIPPET_LIMIT = 140

#: UI label for a ``kind:"compaction"`` child (W9.26 §2.6): a continuation era
#: is never a swipe alternative, so it is named instead of snippet-blank.
COMPACTION_LABEL = "compacted continuation"

#: ``kind`` values the branches payload reports per child.
KIND_USER = "user"
KIND_COMPACTION = "compaction"

#: Event types the transcript renders as a message (delete-tail §2.1 step 3).
#: ``opening:true`` narratives and generated-image ``narrative`` canon rows are
#: ``narrative`` rows and COUNT: both render as transcript content (the pinned
#: opening block, the canon lane), so they keep/leave like any other message.
MESSAGE_TYPES = frozenset({"user", "narrative"})

#: ``409`` ``reason`` values of ``POST .../delete-tail`` (§2.1 rejection set).
REASON_FIRST_MESSAGE = "first_message"
REASON_PIVOT_MISSING = "pivot_missing"
REASON_NOT_A_MESSAGE = "not_a_message"


class ForkRequest(BaseModel):
    """Body of ``POST /api/sessions/{id}/fork``."""

    at_seq: int = Field(ge=0)
    #: Optional caller-chosen id (tests/replay); fresh uuid4 otherwise.
    new_session_id: str | None = None


class DeleteTailRequest(BaseModel):
    """Body of ``POST /api/sessions/{id}/delete-tail``.

    ``pivot_event_id`` is the log row id of the message the reader selected: it
    and every message after it leave the (new) branch.
    """

    pivot_event_id: int
    #: Optional caller-chosen id (tests/replay); fresh uuid4 otherwise.
    new_session_id: str | None = None


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _known(log: "EventLog", session_id: str) -> None:
    """404 unless the session has fork bookkeeping."""
    if log.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="unknown session")


# -- delete-tail (issue #35) ------------------------------------------------
def is_message_event(event: "Event") -> bool:
    """Whether ``event`` renders as a transcript message (delete-tail pivot rule).

    §2.1 step 3: a message is a non-ephemeral ``user``/``narrative`` row. The
    legacy card-import seed is a ``user`` row the player never sent and never
    surfaced as a bubble (``EventLog.get_projection``), so it is not a message
    and cannot be a pivot. Everything else counts -- ``opening:true`` and
    generated-image canon narratives included: they are content the reader sees
    in the transcript, so they must leave with the tail like any other message.
    """
    if getattr(event, "ephemeral", False):
        return False
    if event.type not in MESSAGE_TYPES:
        return False
    if event.type == "user":
        payload = event.payload if isinstance(event.payload, dict) else {}
        if is_import_seed_text(payload.get("text")):
            return False
    return True


def chain_messages(chain: Iterable["Event"]) -> list["Event"]:
    """The messages of a fork-aware chain, in chain (seq) order."""
    return [event for event in chain if is_message_event(event)]


def delete_tail_cut(chain: Iterable["Event"], pivot: "Event") -> int | None:
    """The fork cut for deleting ``pivot`` and everything after it (§2.2).

    ``cut`` is the LARGEST chain seq strictly below the pivot -- not literally
    the previous message's seq. Rows between two messages are the kept turn's
    post-turn writes (``state-delta`` for the tracker/entity registry,
    ``turn_stages``, ``validation_notice``), and ``get_projection`` folds a
    ``state-delta`` in by seq (last wins): cutting AT the previous message
    would drop the kept turn's state rows and silently rewind tracked state by
    one turn. Cutting at the largest seq below the pivot keeps the kept turn
    whole and lets the DELETED turns' state rows leave with them.

    Returns ``None`` when the pivot has nothing below it on the chain.
    """
    candidates = [event.seq for event in chain if event.seq < pivot.seq]
    return max(candidates) if candidates else None


def _first_prose(log: "EventLog", session_id: str) -> str:
    """First non-empty prose the branch itself owns (its generated reply).

    Inherited narrative is deliberately ignored — the snippet answers "what did
    *this* sibling say", which is what the swipe UI contrasts.
    """
    for event in log.get_events(session_id):
        if event.type != "narrative" or event.ephemeral:
            continue
        prose = event.payload.get("prose") or event.payload.get("text") or ""
        prose = prose.strip()
        if prose:
            return prose[:SNIPPET_LIMIT]
    return ""


def _is_hidden(log: "EventLog", session_id: str) -> bool:
    """True once a ``session_hidden`` system event exists in the branch."""
    return any(
        event.type == "system"
        and event.payload.get("event") == "session_hidden"
        for event in log.get_events(session_id)
    )


def _child_kind(log: "EventLog", child_id: str) -> str:
    """``"compaction"`` iff the child's OWN rows carry an era marker, else ``"user"``.

    Own rows, never the chain: ``fork_session`` re-appends the parent's config
    rows after the cut, so a compaction child's FIRST row is carried config and
    the marker lands later -- ``has_compaction_marker`` scans the whole own-row
    set, which is why it (and not a "first row is a marker" test) is the rule.
    """
    return KIND_COMPACTION if has_compaction_marker(log, child_id) else KIND_USER


def _branch_view(log: "EventLog", child_id: str) -> dict:
    info = log.get_session(child_id) or {}
    hidden = _is_hidden(log, child_id)
    kind = _child_kind(log, child_id)
    label = COMPACTION_LABEL if kind == KIND_COMPACTION else ""
    return {
        "session_id": child_id,
        "base_session": info.get("base_session"),
        "base_seq": info.get("base_seq"),
        "created_at": info.get("created_ts"),
        "kind": kind,
        "label": label,
        # A compaction continuation has no reply of its own until it takes a
        # turn, so it would list snippet-blank; the label stands in.
        "snippet": _first_prose(log, child_id) or label,
        "visible": not hidden,
        "hidden": hidden,
    }


def _swipe_group(log: "EventLog", session_id: str) -> list[str]:
    """``last_swipe_target`` minus compaction continuations (W9.26 §2.6).

    The group is PIVOT-based and symmetric (issue #59): the anchor line
    (position 0) is kept unconditionally -- it is the group's base reply, not a
    child of this cut -- and the forks listed are the replies at that same
    position, so the original line lists the regenerate branch exactly as the
    branch lists the original. Every compaction child drops out. When the
    requested session is ITSELF a continuation it forms a one-line group of its
    own (one sibling => no arrows): a continuation has no alternates, and it
    must never be reachable by swiping out of the era it starts.
    """
    if _child_kind(log, session_id) == KIND_COMPACTION:
        return [session_id]
    # ``or [session_id]`` keeps the group self-inclusive by construction: the
    # payload's ``index`` locates SELF in this list, so an (impossible today)
    # empty read must degrade to a one-line group, never an empty one.
    ids = list(log.last_swipe_target(session_id)) or [session_id]
    return ids[:1] + [
        sid for sid in ids[1:] if _child_kind(log, sid) != KIND_COMPACTION
    ]


@router.get("/api/sessions/{session_id}/branches")
async def list_child_branches(session_id: str, request: Request) -> dict:
    """Full swipe context for the session (orchestrator contract, W2.2 UI).

    ``parent``: the session this one was forked from (null at a root).
    ``siblings``: pivot-first ordered swipe group INCLUDING self (``is_self``);
    position 1 is the anchor line's own reply, and the group is the same set
    from either side of the fork (issue #59). ``index``/``count`` are the
    1-based position of ``session_id`` in ``siblings`` and the group size, so
    the client's arrows are derivable without re-deriving them from the list.
    ``branches``: direct children, oldest first (branch-chip/fork-point list).

    W9.26 §2.6: every ``branches`` entry carries ``kind`` (``"user"`` vs
    ``"compaction"``) and a ``label`` (empty for user alternates); compaction
    continuations are KEPT there and EXCLUDED from ``siblings``.
    """
    log = _log_for(request)
    _known(log, session_id)
    branches = [_branch_view(log, child) for child in log.list_branches(session_id)]
    branches.sort(key=lambda b: (b["created_at"] or "", b["session_id"]))

    info = log.get_session(session_id) or {}
    parent_id = info.get("base_session")

    def _snippet(sid: str) -> str:
        for event in log.get_events(sid):
            if event.type == "narrative":
                return str(event.payload.get("prose", ""))[:80]
        # Same rule as the branches list: a continuation is labelled, never blank.
        return COMPACTION_LABEL if _child_kind(log, sid) == KIND_COMPACTION else ""

    # ONE group read for every session, roots included (issue #59): the origin
    # line of a fork owns forks of its own, so "no parent pointer" no longer
    # means "no siblings" -- a root lists the reply regenerated from it.
    siblings = [
        {
            "session_id": sid,
            "base_seq": (
                None if sid == parent_id
                else (log.get_session(sid) or {}).get("base_seq")
            ),
            "created_at": (log.get_session(sid) or {}).get("created_ts"),
            "snippet": _snippet(sid),
            "is_self": sid == session_id,
        }
        for sid in _swipe_group(log, session_id)
    ]
    parent_view = (
        {
            "session_id": parent_id,
            "base_seq": (log.get_session(parent_id) or {}).get("base_seq"),
            "snippet": _snippet(parent_id),
        }
        if parent_id
        else None
    )

    # ``index`` locates SELF in the very group that is being returned: the page
    # out of ``len(siblings)`` is what the UI's prev/next arrows read.
    index = next(
        (
            position
            for position, entry in enumerate(siblings, start=1)
            if entry["is_self"]
        ),
        1,
    )
    return {
        "session_id": session_id,
        "parent": parent_view,
        "siblings": siblings,
        "index": index,
        "count": len(siblings),
        "branches": branches,
    }


@router.post("/api/sessions/{session_id}/fork")
async def fork_branch(
    session_id: str, body: ForkRequest, request: Request
) -> dict:
    """Fork ``session_id`` at ``at_seq``; returns the new branch id."""
    log = _log_for(request)
    _known(log, session_id)
    new_session_id = body.new_session_id or uuid.uuid4().hex
    try:
        log.fork_session(session_id, body.at_seq, new_session_id)
    except ValueError as exc:  # target exists / forks onto itself
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    info = log.get_session(new_session_id) or {}
    return {
        "session_id": new_session_id,
        "base_session": session_id,
        "base_seq": body.at_seq,
        "created_at": info.get("created_ts"),
    }


@router.post("/api/sessions/{session_id}/delete-tail")
async def delete_tail(
    session_id: str, body: DeleteTailRequest, request: Request
) -> dict:
    """Delete a message tail by forking just below the pivot (issue #35).

    The operation is a branch-pointer move, not a rewrite: the tail's rows stay
    in the append-only log on the old line (ADR-001), which remains reachable
    through the existing swipe/fork switcher. The new branch is a plain
    ``kind:"user"`` child, so it sits in the same swipe group as the line it
    replaced.

    Returns ``{session_id, base_session, base_seq, deleted_count}`` where
    ``deleted_count`` counts the chain MESSAGES at or after the pivot (the
    engine is authoritative for the confirm dialog's N).

    No turn-lock, mirroring ``POST /fork``: the fork writes only to the NEW
    session, so a concurrent turn on the source cannot corrupt it.
    """
    log = _log_for(request)
    _known(log, session_id)
    # Fork-aware read (MANDATORY): a branch's visible messages are mostly
    # INHERITED rows, so ``get_events`` would miss the pivot entirely -- the
    # 4e59afd chain-read starvation bug (AGENTS.md "Choosing an event-log read").
    chain = log.get_chain_events(session_id)

    pivot = next(
        (event for event in chain if event.id == body.pivot_event_id), None
    )
    if pivot is None:
        raise HTTPException(status_code=409, detail={"reason": REASON_PIVOT_MISSING})
    if not is_message_event(pivot):
        raise HTTPException(status_code=409, detail={"reason": REASON_NOT_A_MESSAGE})

    messages = chain_messages(chain)
    # Deleting the FIRST message would delete the session's whole history (and
    # leave nothing to fork below); refuse it instead of forking an empty line.
    if not any(message.seq < pivot.seq for message in messages):
        raise HTTPException(
            status_code=409, detail={"reason": REASON_FIRST_MESSAGE}
        )

    cut = delete_tail_cut(chain, pivot)
    if cut is None:  # pragma: no cover - guaranteed by the first-message gate
        raise HTTPException(
            status_code=409, detail={"reason": REASON_FIRST_MESSAGE}
        )

    new_session_id = body.new_session_id or uuid.uuid4().hex
    try:
        log.fork_session(session_id, cut, new_session_id)
    except ValueError as exc:  # target exists / forks onto itself
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Title carry (compaction's rule, title only): ``GET /api/sessions``
    # summarizes OWN rows, so a lazily forked branch would list title "" -- and
    # the rail must show the line the reader just switched to. Deliberately NO
    # compaction marker: a marker would make the branch a continuation era,
    # drop it from the swipe group (``_swipe_group``) and hide the old line.
    #
    # ALWAYS append it, empty title included: ``_summarize`` returns ``None`` for
    # a branch with zero own rows and the list DROPS such a session, so a
    # delete-tail on an untitled chat (the default new-chat path) would make the
    # line the reader just switched to vanish from the rail until its next turn.
    # Compaction reaches the same guarantee through its marker row; this fork
    # has no marker, so the carry row IS the branch's own-row anchor. An
    # empty-title rename row is harmless downstream: ``_summarize`` folds it to
    # ``title: ""`` (what an untitled chat already reads) and
    # ``context_monitor.session_title`` ignores blank titles and falls back to
    # the parent, exactly as it does for the unanchored child today.
    title = session_title(log.get_events(session_id))
    log.append_event(
        new_session_id,
        "system",
        "system",
        {"event": "session_renamed", "title": title},
    )

    return {
        "session_id": new_session_id,
        "base_session": session_id,
        "base_seq": cut,
        "deleted_count": sum(1 for message in messages if message.seq >= pivot.seq),
    }


@router.delete("/api/sessions/{session_id}/branch")
async def prune_branch(session_id: str, request: Request) -> dict:
    """Hard-prune a branch: its own events + its bookkeeping row (F4).

    Refuses with 409 when other sessions fork from this one (they would end up
    with a dangling base pointer). This is distinct from the soft
    ``DELETE /api/sessions/{id}`` hide path.
    """
    log = _log_for(request)
    _known(log, session_id)
    try:
        removed = log.prune_branch(session_id)
    except PruneError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"session_id": session_id, "pruned": True, "events_deleted": removed}
