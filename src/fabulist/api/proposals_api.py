"""Proposals router (W9.22 §4): the review home's queue, decisions, and UNDO.

Thin transport over :mod:`fabulist.state.proposals`. The fold, the decision
rows and the ONE asset write (``accept``) all live there — this module adds no
rule of its own. It validates the request, calls the store, and projects the
store's dataclasses onto the wire shape both Wave-4 surfaces share (plan §4: the
chat drawer and the studio home are one component over one API).

Endpoints
---------
  GET  /api/proposals?session={sid}
      -> {"pending": [Proposal], "archived": [Proposal], "committed": [Decision]}
  GET  /api/proposals/inbox
      -> {"by_session": {sid: {"title", "pending": [Proposal], "archived": [...]}}}
  POST /api/proposals/{sid}/decide  {"ids": [str], "decision": "accept"|"reject"|"dismiss"}
      -> {"decisions": [Decision], "rejected": [{"id": str, "error": str}]}
  POST /api/proposals/{sid}/undo    {"decision_id": str}
      -> {"decision": Decision}

The wire shapes are the pinned cross-lane contract (plan §1 doctrine 3: agents
propose, the owner disposes):

  Proposal = {"id","kind","action","target","target_name","patch","ops","origin",
              "signal","evidence","supersedes","archived","age_turns","stale_paths",
              "target_new"}
  Decision = {"decision_id","proposal_id","decision","kind","action","pre_image",
              "applied","applied_summary","target_new"}

W9.23 §4 amendments (all additive):
  * ``ops`` — the diff-first operation list. An op row serves its filed list
    verbatim; a LEGACY whole-value amend is hydrated SERVER-SIDE into one
    whole-field ``modify`` row per writable patch key, ``old`` read from the
    current fold and ``legacy: true``, so the surface never has to branch and the
    owner always sees a delta (:func:`fabulist.state.proposals.display_ops`).
  * ``stale_paths`` — the op paths whose current value no longer matches the filed
    ``old``. Byte-identical to ``ops[].path``: the surface badges a row by
    matching strings, so a normalized spelling would silently un-badge it.
  * ``archived`` / ``age_turns`` — the DERIVED turn-aged partition (§3.2), served
    as its own list AND as a per-row flag (the surface partitions over both).
  * ``kind`` / ``action`` on a Decision — the committed list could not name what
    an applied item did to what without them (closes the D77 residual).

W9.30 §6 amendment (additive, both shapes):
  * ``target_new`` — the BORN-PARENTED marker. ``true`` on a books-lane amend
    whose accept CREATES the book it names (the row's ``target_name`` is that
    book's name, so the surface labels the row without a catalog read); ``true``
    on its decision too, which is what licenses the null ``pre_image``. Every row
    written before the marker existed folds as ``false``.

L4 contract amendments (both ADDITIVE, so an existing surface keeps working):

* ``kind`` may be ``"cast"`` — a candidate cast member the owner gates. It is
  not an asset class, so its ``target`` is always null and its patch carries
  ``name``/``aliases``/``origin``; an accept admits through the cast layer
  (:func:`fabulist.state.proposals.emit_cast_proposal` is the producer) and its
  ``applied`` records the admitted member.
* ``target_name`` is the READABLE label for ``target``, resolved by the store
  from the catalog (never an apply input). ``target`` stays the opaque asset id
  the decide/undo doors take, so a surface can SHOW a name and still ACT on an
  id. Old rows and ``add`` rows serve ``null``.

Two scopes, read straight from the store
----------------------------------------
``GET /api/proposals?session=`` is SESSION-scoped and fork-aware (the chain
read): a decision taken on a branch must not hide the parent's identical
proposal, because a branch is a different storyline. ``GET /api/proposals/inbox``
is ASSET-scoped: an accepted proposal writes a catalog asset every session
shares, so a decision recorded anywhere retires the item everywhere (plan §4
finding 11's cross-session dedupe). Both reads are the store's; the router never
re-derives either.

This router never touches a registry
------------------------------------
Non-negotiable doctrine 3 / plan §1: the ONLY asset write on the owner's behalf
is inside the store's ``accept`` (:func:`fabulist.state.proposals.accept_proposal`),
which appends through the asset's own append API and captures the pre-image for
UNDO. Nothing here imports a world/book/persona writer, so there is no second
write path to drift.

Wiring (the ``app.state`` seam needs no signature change)::

    from fabulist.api.proposals_api import proposals_router
    app.include_router(proposals_router)

Like :mod:`fabulist.api.decision_queries_api`, the router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from fabulist.api.attachments import _session_exists
from fabulist.api.sessions import _summarize
from fabulist.state import proposals as store
from fabulist.state.proposals import Decision, Proposal, ProposalError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


__all__ = ["proposals_router", "router"]


proposals_router = APIRouter(tags=["proposals"])

#: Conventional alias (some mounting code imports ``router``).
router = proposals_router

#: The three decisions the owner can take: wire name -> store call. A closed map
#: (not free text), because the decision WORD is the row the store appends and
#: the schema gate refuses anything else (:data:`fabulist.log.schema_gate.
#: PROPOSAL_DECISIONS`); rejecting it here keeps the 400 a readable message
#: rather than a log-schema error surfacing as a 500.
_DECISIONS: dict[str, Callable[..., Decision]] = {
    "accept": store.accept_proposal,
    "reject": store.reject_proposal,
    "dismiss": store.dismiss_proposal,
}

#: How an ACCEPT reads in the committed list, by the action it applied.
_ACCEPT_VERBS = {"add": "Created", "amend": "Amended", "remove": "Removed"}

#: How a decision that applied nothing reads (it never reaches the committed
#: list, but the decide response carries the same shape).
_DECLINED_VERBS = {"reject": "Rejected", "dismiss": "Dismissed"}


# -- the wire (the pinned cross-lane shapes) ---------------------------------


def _proposal_wire(item: Proposal, name: str | None, ops: list[dict]) -> dict:
    """One live proposal on the wire — the pinned cross-lane shape.

    ``evidence`` carries the generator's justification line (W9.22 follow-up:
    the schema key is optional at the append door, both writers supply it —
    proposal_lanes for bts rows, the promotion gate for auto-promotions — and
    the store projects it). Rows written before the follow-up, and any row whose
    writer sent nothing, expose ``None``: the review home renders the slot but
    must never read absence as a verdict.

    ``target_name`` is the readable label for ``target`` (L4 amendment):
    ``target`` is an opaque id because the decide/undo doors take one, and a
    card that reads ``amend 4f1c9a2b…`` tells the owner nothing. The caller
    resolves it (:func:`fabulist.state.proposals.target_name`) with ONE catalog
    fold per kind, so a queue read never folds a catalog per row.

    ``ops`` always has content for an amend (filed list, or the server-hydrated
    legacy rows) and is empty for whole-value add/remove/cast rows, which is what
    lets the surface render ONE row vocabulary over both eras of the log.

    ``target_new`` (W9.30 §6) marks a row whose accept CREATES its target, so the
    review body can badge it as a create and replace a stale badge with the hint
    "accepting creates this book and adds N entries" — a born-parented row can
    never be stale (:func:`fabulist.state.proposals._stale_paths` exempts it).
    """
    return {
        "id": item.proposal_id,
        "kind": item.kind,
        "action": item.action,
        "target": item.target,
        "target_name": name,
        "patch": dict(item.patch),
        "ops": ops,
        "origin": item.origin,
        "signal": item.signal,
        "evidence": item.evidence or None,
        "supersedes": list(item.supersedes),
        "archived": item.archived,
        "age_turns": item.age_turns,
        "stale_paths": list(item.stale_paths),
        "target_new": item.target_new,
    }


def _wired(log: "EventLog", items: list[Proposal]) -> list[dict]:
    """Project a queue, resolving each target's readable label once per kind.

    The definition cache is shared across the whole queue, so hydrating legacy
    rows into ops costs one catalog read per TARGET however many rows name it.
    """
    catalogs: dict[str, dict[str, str]] = {}
    definitions: dict[tuple[str, str], dict | None] = {}
    rows: list[dict] = []
    for item in items:
        if item.target and not item.target_name and item.kind not in catalogs:
            catalogs[item.kind] = store.catalog_names(log, item.kind)
        rows.append(
            _proposal_wire(
                item,
                store.target_name(log, item, catalogs.get(item.kind)),
                store.display_ops(log, item, definitions=definitions),
            )
        )
    return rows


def _asset_name(row: Decision) -> str:
    """The asset's display name from whichever side of the write carries it.

    An accepted ``add`` has only ``applied``, an accepted ``remove`` only
    ``pre_image``, an amend both; ``target`` is the id the store resolved. The
    first name found wins, and an id-only asset degrades to the target.
    """
    for side in (row.applied, row.pre_image):
        if isinstance(side, dict):
            name = side.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return row.target or ""


def _applied_summary(row: Decision) -> str:
    """One human line for the committed list — the pinned ``applied_summary``.

    Composed here because the store's decision row is a machine record and the
    pinned Decision wire carries no ``kind``/``action`` of its own: the surface
    cannot name the asset class from the two fold shapes alone, so the readable
    line has to exist at this seam or nowhere (plan §4 req. 21: a committed item
    reads as what it did to what). It stays purely factual — the
    scope-of-effect hint of finding 7 ("affects NEW chats only") is the
    surface's copy on top of this, not the transport's.

    A ``cast`` accept reads "Added to the cast 'X'": the verb is membership, not
    creation (nothing was minted in a catalog; the member joined this chat).
    A BORN-PARENTED books accept reads "Created book 'X' (N entries)" (W9.30 §6):
    the amend verb would be a lie — nothing was amended, a container was made —
    and the entry count is what the owner is actually approving.
    """
    if row.decision == "accept" and row.kind == "book" and row.target_new:
        name = _asset_name(row)
        entries = (row.applied or {}).get("entries")
        count = len(entries) if isinstance(entries, list) else 0
        noun = "entry" if count == 1 else "entries"
        return f"Created book '{name}' ({count} {noun})"
    if row.kind == "cast":
        line = (
            "Added to the cast"
            if row.decision == "accept"
            else _DECLINED_VERBS.get(row.decision, row.decision.title() or "Decided")
        )
        name = _asset_name(row)
        return f"{line} '{name}'" if name else line
    verb = (
        _ACCEPT_VERBS.get(row.action, "Applied")
        if row.decision == "accept"
        else _DECLINED_VERBS.get(row.decision, row.decision.title() or "Decided")
    )
    line = f"{verb} {row.kind}".strip()
    name = _asset_name(row)
    return f"{line} '{name}'" if name else line


def _decision_wire(row: Decision) -> dict:
    """One decision on the wire — the pinned cross-lane shape, exactly.

    ``pre_image`` / ``applied`` are the asset FOLD definitions on both sides of
    an accept (``None`` when that side does not exist), which is what lets a
    surface label what an applied item affected (plan §4 finding 7) and what an
    UNDO would restore; ``applied_summary`` is the one readable line on top of
    them (:func:`_applied_summary`). ``kind``/``action`` ARE served (W9.23 §4):
    the committed list has to say what an applied item did to what, and deriving
    the asset class from two fold shapes alone was never possible. ``target``,
    ``actor`` and ``ts`` stay off the wire — the pinned contract is the interface
    the frontend builds against, and a surface that wants another axis needs an
    amended contract, not a local extra key. ``target_new`` (W9.30 §6) rides the
    wire ADDITIVE: it is true only for a born-parented books accept, whose
    ``pre_image`` is null because the book it created did not exist.
    """
    return {
        "decision_id": row.decision_id,
        "proposal_id": row.proposal_id,
        "decision": row.decision,
        "kind": row.kind,
        "action": row.action,
        "pre_image": row.pre_image,
        "applied": row.applied,
        "applied_summary": _applied_summary(row),
        "target_new": row.target_new,
    }


# -- request plumbing --------------------------------------------------------


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _require_session(log: "EventLog", session_id: str) -> None:
    """404 an id the log has never seen.

    ``_session_exists`` is the router family's one implementation (it also
    counts a fork that has a ``sessions`` row and no own events yet) — a
    proposal read on an unknown session is a client bug, never an empty queue.
    """
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")


def _handles(body: dict) -> list[str]:
    """The de-duplicated proposal ids of a ``/decide`` body, in request order.

    Duplicates collapse instead of being decided twice: the store gives every
    id ONE terminal decision, so replaying one would only manufacture an
    "already accept" error for a request the owner made once — the same
    de-duplication :func:`fabulist.state.proposals.accept_many` performs.
    Shape problems (not a list, a non-id element, an empty set) are 400 with a
    readable ``detail``, per the pinned error shape.
    """
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="'ids' must be a list")
    ordered: list[str] = []
    for value in ids:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise HTTPException(
                status_code=400, detail="'ids' must hold proposal id strings"
            )
        handle = str(value).strip()
        if handle and handle not in ordered:
            ordered.append(handle)
    if not ordered:
        raise HTTPException(
            status_code=400, detail="'ids' must name at least one proposal"
        )
    return ordered


# -- reads -------------------------------------------------------------------


@proposals_router.get("/api/proposals/inbox")
async def get_proposals_inbox(request: Request) -> dict:
    """Every session's pending proposals, grouped by session, in ONE fold.

    The cross-session queue of plan §4: the store's :func:`fabulist.state.
    proposals.inbox` walk IS the fold (one chain read per visible session, then
    one ``_scan`` over the union, which is what applies supersede across
    sessions), so this handler folds nothing twice. Supersede means a session's
    group can shrink to nothing while another session keeps the survivor — the
    grouping is therefore built from the fold's OUTPUT, and a session with no
    pending proposal never appears at all (the surface must not render empty
    sections).

    Both partitions are served (W9.23 §3.2): ``pending`` is the drawer's queue and
    ``archived`` its drawer, and age comes from the FILING session's own walk, so
    turns passing in one chat never archive another chat's queue.

    The title is reused from the sessions router's fold
    (:func:`fabulist.api.sessions._summarize`, the same call the rail and
    :mod:`fabulist.api.global_search_api` make) so the inbox can never spell a
    chat differently than the drawer does, and it falls back to the session id
    for a session with no title anchor. That fold runs ONLY for sessions that
    actually hold a pending item — the bounded per-request memo the plan asks
    for, with no cache to invalidate.

    Soft-hidden sessions still appear: the store's inbox is asset-scoped, and a
    hidden chat can still propose a change to a shared catalog asset.
    """
    log = _log_for(request)
    live, archived = store.inbox_review(log)
    grouped: dict[str, list[Proposal]] = {}
    for item in [*live, *archived]:
        grouped.setdefault(item.session_id, []).append(item)
    by_session: dict[str, dict] = {}
    for session_id, items in grouped.items():
        summary = _summarize(log, session_id)
        by_session[session_id] = {
            "title": (summary or {}).get("title") or session_id,
            "pending": _wired(log, [row for row in items if not row.archived]),
            "archived": _wired(log, [row for row in items if row.archived]),
        }
    return {"by_session": by_session}


@proposals_router.get("/api/proposals")
async def get_proposals(request: Request, session: str = Query(...)) -> dict:
    """One session's review home: the pending queue and the committed items.

    ONE fold (:func:`fabulist.state.proposals.review`) serves all three lists:
    ``pending`` is the live queue (decided proposals leave it, same-identity ones
    collapse into the newest with the collapsed ids on ``supersedes``),
    ``archived`` is the turn-aged partition (W9.23 §3.2 — never deleted, never
    mass-selected, and single-id decides on it stay legal), and ``committed`` is
    the accepted decisions, newest first, each carrying the ``decision_id`` its
    UNDO takes and the pre-image that undo restores. All three are fork-aware
    chain reads, so a branch sees what its storyline sees.
    """
    log = _log_for(request)
    _require_session(log, session)
    found = store.review(log, session)
    return {
        "pending": _wired(log, found.pending),
        "archived": _wired(log, found.archived),
        "committed": [_decision_wire(row) for row in found.committed],
    }


# -- decisions ---------------------------------------------------------------


@proposals_router.post("/api/proposals/{session_id}/decide")
async def decide_proposals(session_id: str, body: dict, request: Request) -> dict:
    """Accept / reject / dismiss a SET of proposal ids — each one independently.

    Granular accept, mass approval and a mixed set are the same request: the
    owner may tick five cards, one of which a branch already settled. Ids are
    therefore decided ONE AT A TIME and a failing id lands in ``rejected``
    instead of sinking the batch (the pinned contract). The store's
    ``accept_many`` is deliberately NOT used: it pre-checks the whole batch and
    refuses all-or-nothing, which is right for its callers and wrong for a
    mixed set. Deciding one id at a time is the same log outcome — one
    ``proposal_decision`` row per item, so a granular UNDO survives a mass
    accept (plan §4 finding 13).

    Status codes follow the pinned contract: ``200`` whenever at least one id
    was decided, carrying ``decisions`` and ``rejected`` together (partial
    success is not an error); ``400`` with ``detail`` + the per-id ``rejected``
    list only when NOTHING could be decided — a single stale id is a hard
    failure the surface must report, not a silent no-op.

    An ARCHIVED id inside a multi-id body is refused for that id with a readable
    error (W9.23 §3.3: the drawer is never mass-selected); a single-id body on an
    archived id is decided normally.
    """
    log = _log_for(request)
    _require_session(log, session_id)
    decision = body.get("decision")
    if not isinstance(decision, str) or decision not in _DECISIONS:
        raise HTTPException(
            status_code=400, detail="'decision' must be one of accept|reject|dismiss"
        )
    decide = _DECISIONS[decision]
    handles = _handles(body)
    # W9.23 §3.3: the archive drawer is never mass-selected. A MULTI-id body that
    # names an archived id is refused FOR THAT ID (the rest still apply); a
    # single-id body on an archived row stays legal, because the owner opened the
    # drawer and meant exactly that row.
    archived = store.archived_ids(log, session_id) if len(handles) > 1 else frozenset()
    decisions: list[dict] = []
    rejected: list[dict] = []
    for handle in handles:
        if handle in archived:
            rejected.append(
                {
                    "id": handle,
                    "error": (
                        f"proposal {handle} is archived: archived proposals can "
                        "only be decided one at a time"
                    ),
                }
            )
            continue
        try:
            decisions.append(_decision_wire(decide(log, session_id, handle)))
        except ProposalError as exc:
            rejected.append({"id": handle, "error": str(exc)})
    if not decisions:
        # Nothing applied: the request failed as a whole. A single id's message
        # is the useful detail; for a wider set, the summary plus the per-id list.
        detail = (
            rejected[0]["error"]
            if len(rejected) == 1
            else f"none of the {len(rejected)} proposals could be decided"
        )
        return JSONResponse(
            status_code=400, content={"detail": detail, "rejected": rejected}
        )
    return {"decisions": decisions, "rejected": rejected}


@proposals_router.post("/api/proposals/{session_id}/undo")
async def undo_proposal_decision(session_id: str, body: dict, request: Request) -> dict:
    """Undo one accepted decision — a compensating append, never a rewrite.

    The store re-appends the ``pre_image`` the accept captured (or tombstones
    what an accepted ``add`` created) through the asset's own API, then appends
    the ``proposal_undo`` row; the asset's history stays readable (D9/D10, plan
    §4 finding 13). The returned ``decision`` is the ACCEPT decision being
    compensated — the row the surface just took off its committed list, whose
    ``pre_image``/``applied`` say exactly what moved. (There is no Decision
    shape for the compensating row itself: a ``proposal_undo`` payload carries
    only the id it compensates, so inventing one here would be a second wire
    shape for one concept.)

    Refusals are the store's own: an unknown decision, an already-undone one,
    and a reject/dismiss (which applied nothing) all arrive as
    :class:`~fabulist.state.proposals.ProposalError` -> ``400 {"detail"}``.
    """
    log = _log_for(request)
    _require_session(log, session_id)
    decision_id = body.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise HTTPException(
            status_code=400, detail="'decision_id' must be a non-empty string"
        )
    try:
        row = store.undo_decision(log, session_id, decision_id)
    except ProposalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"decision": _decision_wire(row)}
