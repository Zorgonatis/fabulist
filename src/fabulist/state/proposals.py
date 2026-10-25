"""Proposal store (W9.22 §4): the review home's fold, decisions, and UNDO.

Why this module exists
----------------------
W9.22 doctrine 3 is "agents propose; the owner disposes". The promotion gate
(Wave 1, :mod:`fabulist.state.promotion`) files the first proposals and the
BTS proposal stage (Wave 3) will file them from three type-scoped lanes, but
**no lane writes an asset** — a proposal is a *suggestion* that the owner
accepts, rejects, or dismisses. This module is the other half of that contract:
the live proposal set the two Wave-4 surfaces render, and the ONLY place an
asset write happens on the owner's behalf.

Everything is an append (D9/D10)
--------------------------------
"Apply" is not a rewrite: it goes through the asset's EXISTING append API
(:func:`fabulist.worlds.registry.append_world`,
:func:`fabulist.context.books.append_book`,
:func:`fabulist.personas.registry.append_persona`) — the same latest-wins fold
the studio writes — and the decision itself is a ``proposal_decision`` row that
records what happened. "Undo" is the plan's compensating append (finding 13):
the accept captures the PRE-IMAGE of the definition it overwrote, and the undo
re-appends that pre-image or tombstones what the accept created. The asset's
own history therefore stays intact and readable; nothing is destroyed, and a
mis-accept never requires a database edit.

Why the pre-image is captured at ACCEPT time, not at proposal time
-----------------------------------------------------------------
Worlds/books/personas fold WHOLE definitions latest-wins, so an undo needs
exactly the definition the accept replaced. Capturing it when the proposal is
FILED would restore a definition the owner may have edited in the studio
meanwhile — undoing more than the accept did. The decision row is therefore a
snapshot (``kind``/``action``/``target``/``pre_image``/``applied``), not a
pointer into whatever the log says later.

The ONE accept that creates its own parent (W9.30)
--------------------------------------------------
A books-lane amend may name a book that does not exist yet: the lane parser marks
that row ``target_new`` (W9.30 §3.1), and the accept MATERIALIZES the book, applies
the filed entry adds to it, and attaches it to the session's book registration
(§4.1). Its decision records ``pre_image=None`` (nothing existed) and its undo is
therefore a tombstone PLUS the exact detach (§4.2). The marker is what separates
that shape from the ordinary amend-against-a-missing-target, which still REFUSES
("does not exist"): a book deleted between filing and accept must never be
silently re-created.

Supersede (finding 11)
----------------------
The same asset proposed twice is one decision, not two. Live proposals are
grouped by the identity they act on — ``target`` for amend/remove, the proposed
NAME for ``add`` (which has no target by contract) — and the newest applicable
item wins while the older ones collapse into it. A collapsed item stays in the
log (D9) and is reported on the survivor's ``supersedes``, so the surface can
say "3 proposals, 2 collapsed" and never offers a stale id to accept.

Two scopes, deliberately different
----------------------------------
* :func:`pending` / :func:`proposals` are SESSION-scoped and fork-aware (chain
  read, the AGENTS.md rule): a decision taken on a branch must NOT hide the same
  proposal on its parent — the parent is a different storyline.
* :func:`inbox` is ASSET-scoped: an accepted proposal wrote a CATALOG asset,
  which every session shares, so a decision recorded anywhere retires the item
  everywhere (finding 11's cross-session dedupe).

``kind="actor"`` and the persona catalog
---------------------------------------
A proposal names an actor, and the durable home for a reusable cast member is a
PERSONA (:mod:`fabulist.personas.registry`) — that is the "persona/book
equivalent" the wave's write rule names. This is the mirror image of finding 8:
a HIGH-signal promotion mints a *session* actor whose stub rides
``session_actor`` (no asset, nothing to review), while accepting a proposal
mints the reusable definition the owner can put in a world later.

``kind="cast"`` is the ONE kind with no asset class (W9.22 L4)
---------------------------------------------------------------
A cast member is not a definition file: membership is ``cast_admit`` /
``cast_alias`` rows on a session (:mod:`fabulist.state.cast`), so there is no
append API for this module to write. The pinned seam is the cast layer's own two
helpers — ``admit_from_proposal(log, session_id, name, aliases, origin)`` on
accept and ``retire_from_proposal(log, session_id, name)`` on undo — which is
why :data:`_KINDS` deliberately does NOT carry ``cast``: routing it through the
asset seam would invent a fourth append path. The kind exists so a *candidate*
character (someone the turn met) is a proposal the owner gates, instead of an
automatic admit; ``aliases`` rides its patch because aliases are a CAST-layer
fact (see ``schema_gate.ASSET_PROPOSAL_PATCH_FIELDS``), never an asset field.
The pre-image of a cast accept is ``None`` (nothing existed) and its ``applied``
records the member it admitted, so the same one-undo-per-decision rule holds.

Readable targets
----------------
``target`` is an opaque asset id, so :func:`target_name` resolves the readable
label from the catalog (and :class:`Proposal` records the emitter's own
``target_name`` when a producer supplied one). This is a READ-side concern only:
the label never reaches an apply.

Not this module's job: the API routes and the two surfaces (Wave 4 UI), the
proposal lanes that author patches (Wave 3), and any redo/repromote path — an
undo undo is not modelled, deliberately (one compensating append per decision).
"""

from __future__ import annotations

import copy
import logging
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from fabulist.context.books import (
    ENTRY_ID_KEY,
    Book,
    append_book,
    book_by_id,
    delete_book,
    list_books,
)
from fabulist.log.schema_gate import (
    ASSET_PROPOSAL_EVENT,
    ASSET_PROPOSAL_OP_ACTIONS,
    PROPOSAL_DECISION_EVENT,
    PROPOSAL_UNDO_EVENT,
    entry_projection,
    list_item_path,
    writable_field,
)
from fabulist.personas.registry import (
    Persona,
    append_persona,
    delete_persona,
    list_personas,
)
from fabulist.worlds.registry import (
    SESSION_BOOKS_EVENT,
    World,
    append_world,
    delete_world,
    list_worlds,
    set_session_books,
    world_by_id,
)

from .cast import canonical_key
from .presence import _chain_events

logger = logging.getLogger(__name__)

try:  # Lane L3's pinned cast helpers (W9.22 cast-as-proposal); landing in parallel.
    from .cast import admit_from_proposal, retire_from_proposal
except ImportError:  # pragma: no cover - the seam is pinned, the helpers are pending
    admit_from_proposal = None  # type: ignore[assignment]
    retire_from_proposal = None  # type: ignore[assignment]

__all__ = [
    "ARCHIVE_AFTER_TURNS",
    "Decision",
    "Proposal",
    "ProposalError",
    "ReviewSet",
    "accept_many",
    "accept_proposal",
    "archived_ids",
    "catalog_names",
    "committed",
    "dismiss_proposal",
    "display_ops",
    "emit_cast_proposal",
    "inbox",
    "inbox_review",
    "pending",
    "proposals",
    "reject_proposal",
    "review",
    "target_name",
    "undo_decision",
]

#: How many committed turns a live proposal survives before it moves to the
#: archive drawer (W9.23 §3.2). A module constant on purpose: archived-ness is a
#: DERIVED partition of the fold, so changing this re-partitions instantly and
#: needs no event, no migration and no config surface (promote only on request).
ARCHIVE_AFTER_TURNS = 10

#: Actor stamped on an owner decision. Mirrors the asset lanes' ``USER_ACTOR``
#: (:mod:`fabulist.worlds.registry`): a decision is a human intent.
USER_ACTOR = "user"


class ProposalError(ValueError):
    """A proposal or decision cannot be acted on (unknown, decided, superseded).

    A ``ValueError`` subclass so the Wave-4 routes can map it to a 4xx while the
    store itself stays API-free: the message is written for the owner, because it
    is what the surface shows when a stale card is clicked.
    """


@dataclass(frozen=True)
class Proposal:
    """One LIVE ``asset_proposal`` as the review home renders it.

    ``proposal_id`` is opaque: it is the log row id rendered as a string, which
    makes it unique across sessions (the ``events`` table is
    ``INTEGER PRIMARY KEY AUTOINCREMENT``, so an id is never reused) and stable
    across forks — a branch inherits the very same row, so the same proposal has
    the same handle on parent and branch. ``kind``/``action``/``origin``/``signal``
    are the grouping axes the plan's surface spec names (data type, what it does,
    AUTO vs USER-triggered, and the judgment probability that filed it);
    ``supersedes`` lists the older ids that collapsed into this one (finding 11).

    The W9.23 diff-first additions: ``ops`` is the filed operation list (empty
    for a legacy whole-value amend, an add, a remove or a cast row),
    ``age_turns``/``archived`` are the DERIVED age partition (a pure function of
    the chain's committed narratives and the row's own ``seq`` — never an
    event), and ``stale_paths`` names the op paths whose current value no longer
    matches the filed ``old`` (computed at read; empty for a legacy row, which
    anchored nothing).
    """

    proposal_id: str
    session_id: str
    kind: str
    action: str
    target: str | None
    patch: dict
    origin: str
    signal: float | None
    seq: int
    ts: str
    supersedes: tuple[str, ...] = ()
    #: The generator's justification line (W9.22 follow-up). Empty for rows
    #: written before the evidence key existed — the review home must treat an
    #: empty string as "no reason recorded", never as "no reason".
    evidence: str = ""
    #: The READABLE label the emitter recorded for ``target`` (L4), or ``""``.
    #: Empty for rows written before the key existed and for ``add`` rows (whose
    #: patch already names what it creates); :func:`target_name` resolves the
    #: fallback from the catalog, so a surface never has to show a bare id.
    target_name: str = ""
    #: The diff-first operation list (W9.23 §3.4.1), exactly as filed. A TUPLE so
    #: a caller cannot append to the fold; the op dicts themselves are fresh
    #: copies of the logged JSON (the log owns the values, never this fold).
    ops: tuple[dict, ...] = ()
    #: Derived age partition (W9.23 §3.2): committed turns after this row's own
    #: ``seq``, and whether that age crossed :data:`ARCHIVE_AFTER_TURNS`.
    age_turns: int = 0
    archived: bool = False
    #: Op paths whose current value no longer matches the filed ``old`` (read-time
    #: staleness, W9.23 §4). Empty for a legacy whole-value row: nothing was
    #: anchored, so nothing can be stale.
    stale_paths: tuple[str, ...] = ()
    #: The BORN-PARENTED marker (W9.30 §3.1): this books-lane amend names a book
    #: that does not exist yet, so its ACCEPT creates it (§4.1) instead of
    #: amending it. Set by the lane parser only — the flag is what separates
    #: "the lane knew the book was missing" from "the book was deleted before
    #: accept", and the second case keeps the stale-refusal contract.
    target_new: bool = False


@dataclass(frozen=True)
class Decision:
    """One owner decision on a proposal — the row UNDO compensates.

    ``decision_id`` is the undo handle (:func:`undo_decision`), ``pre_image`` the
    definition the accept overwrote (``None`` for an accepted ``add`` — nothing
    existed) and ``applied`` the definition the accept wrote (``None`` for an
    accepted ``remove``). Both are FOLD shapes: the same dict the asset's append
    API re-appends, which is what makes an undo exact rather than approximate.
    ``seq``/``ts``/``actor`` say who decided and when.
    """

    decision_id: str
    proposal_id: str
    decision: str
    kind: str
    action: str
    target: str | None
    pre_image: dict | None
    applied: dict | None
    seq: int
    ts: str
    actor: str
    #: The BORN-PARENTED marker, copied from the proposal it decided (W9.30
    #: §4.1): a parented accept has NO pre-image (nothing existed), so this flag
    #: is what licenses the null and what :func:`_restore` reads to compensate
    #: exactly (tombstone + detach instead of re-appending a pre-image).
    target_new: bool = False


# -- the asset seam (the ONE write path, per kind) ---------------------------


@dataclass(frozen=True)
class _AssetKind:
    """One asset class's real append / read / tombstone APIs.

    ``write`` takes a FOLD shape — the very dict
    :func:`fabulist.worlds.registry.append_world`,
    :func:`fabulist.context.books.append_book` and
    :func:`fabulist.personas.registry.append_persona` persist — so applying a
    patch and restoring a pre-image are the same operation on the same path. No
    second write path exists (PLAN §2 non-goals: every new surface earns itself).
    """

    id_key: str
    write: Callable[[Any, dict], str]
    read: Callable[[Any, str], dict | None]
    tombstone: Callable[[Any, str], None]


def _world_definition(world: World) -> dict:
    """The world FOLD shape (``append_world`` re-appends this dict verbatim).

    Carries the world's OPENING (``first_mes`` / ``alternate_greetings``, issue
    #51) alongside every other persisted field: this dict is the pre-image an
    undo re-appends, so dropping them here would silently erase a world's
    greeting on any proposal revert.
    """
    return {
        "world_id": world.world_id,
        "name": world.name,
        "description": world.description,
        "scenario": world.scenario,
        "first_mes": world.first_mes,
        "alternate_greetings": list(world.alternate_greetings),
        "cast": list(world.cast),
        "book_ids": list(world.book_ids),
        "provenance": dict(world.provenance),
        "image_asset": world.image_asset,
    }


def _book_definition(book: Book) -> dict:
    """The book FOLD shape (``append_book`` re-appends this dict verbatim)."""
    return {
        "book_id": book.book_id,
        "name": book.name,
        "entries": [dict(entry) for entry in book.entries],
        "provenance": dict(book.provenance),
    }


def _persona_definition(persona: Persona) -> dict:
    """The persona FOLD shape (``append_persona`` re-appends this dict verbatim)."""
    return {
        "persona_id": persona.persona_id,
        "name": persona.name,
        "description": persona.description,
        "personality": persona.personality,
        "scenario": persona.scenario,
        "first_mes": persona.first_mes,
        "book_ids": list(persona.book_ids),
        "alternate_greetings": list(persona.alternate_greetings),
        "extensions": dict(persona.extensions),
        "provenance": dict(persona.provenance),
        "image_asset": persona.image_asset,
    }


def _read_world(log: Any, world_id: str) -> dict | None:
    world = world_by_id(log, world_id)
    return None if world is None else _world_definition(world)


def _read_book(log: Any, book_id: str) -> dict | None:
    book = book_by_id(log, book_id)
    return None if book is None else _book_definition(book)


def _read_persona(log: Any, persona_id: str) -> dict | None:
    # The persona registry exposes no by-id read (the catalog is small and folded
    # once), so this is the same fold the studio lists from.
    for persona in list_personas(log):
        if persona.persona_id == persona_id:
            return _persona_definition(persona)
    return None


#: ``kind`` -> the asset class it writes. ``actor`` is the persona catalog: the
#: reusable cast-member definition (see the module docstring).
_KINDS: dict[str, _AssetKind] = {
    "world": _AssetKind("world_id", append_world, _read_world, delete_world),
    "book": _AssetKind("book_id", append_book, _read_book, delete_book),
    "actor": _AssetKind("persona_id", append_persona, _read_persona, delete_persona),
}


def _kind(name: str) -> _AssetKind:
    asset = _KINDS.get(name)
    if asset is None:
        raise ProposalError(f"unknown asset kind {name!r}")
    return asset


def _cast_admit() -> Callable[..., Any]:
    """The pinned cast admit helper, or a refusal naming the pending seam.

    ``admit_from_proposal`` belongs to Lane L3 (:mod:`fabulist.state.cast`): a
    cast member is log rows, not a definition, so this module must not grow a
    fourth append path for it. The import is guarded because the two lanes land
    in parallel — a build without the helper refuses the cast ACCEPT with a
    readable message instead of writing membership behind the owner's back.
    """
    if admit_from_proposal is None:  # pragma: no cover - integration pending
        raise ProposalError(
            "the cast admit seam is not available in this build "
            "(fabulist.state.cast.admit_from_proposal)"
        )
    return admit_from_proposal


def _cast_retire() -> Callable[..., Any]:
    """The pinned cast retire helper, or a refusal naming the pending seam."""
    if retire_from_proposal is None:  # pragma: no cover - integration pending
        raise ProposalError(
            "the cast undo seam is not available in this build "
            "(fabulist.state.cast.retire_from_proposal)"
        )
    return retire_from_proposal


# -- readable targets (a read-side label, never an apply input) --------------

_CATALOGS: dict[str, Callable[[Any], dict[str, str]]] = {
    "world": lambda log: {world.world_id: world.name for world in list_worlds(log)},
    "book": lambda log: {book.book_id: book.name for book in list_books(log)},
    "actor": lambda log: {
        persona.persona_id: persona.name for persona in list_personas(log)
    },
}


def catalog_names(log: Any, kind: str) -> dict[str, str]:
    """``{asset id: display name}`` for one kind's catalog (``{}`` for cast).

    The readable label a review card needs for an ``amend``/``remove`` target:
    the wire carries an opaque id, and the owner cannot read
    ``amend 4f1c9a2b…``. A catalog read failure yields ``{}`` — a label is
    presentation, and losing it must never take the queue down.
    """
    reader = _CATALOGS.get(kind)
    if reader is None:
        return {}
    try:
        return reader(log)
    except Exception:  # noqa: BLE001 - a label is presentation, never fatal
        return {}


def target_name(
    log: Any, item: Proposal, names: Mapping[str, str] | None = None
) -> str | None:
    """The readable label for a proposal's target, or ``None`` when it has none.

    The emitter's own ``target_name`` wins (it is what the proposal MEANT when it
    was filed); otherwise the catalog is read for the current definition's name.
    An ``add`` has no target by contract and therefore no label — its patch
    already names what it creates. ``names`` is an already-folded
    ``{id: name}`` map (:func:`catalog_names`), so a queue read can fold once per
    kind instead of once per row.
    """
    if item.target_name:
        return item.target_name
    if not item.target:
        return None
    catalog = catalog_names(log, item.kind) if names is None else names
    return catalog.get(item.target) or None


# -- the fold ----------------------------------------------------------------


@dataclass(frozen=True)
class _Scan:
    """Everything one pass over the rows knows about proposals and decisions."""

    proposals: dict[str, Proposal]
    decisions: dict[str, Decision]
    by_decision: dict[str, Decision]
    undone: frozenset[str]
    live: list[Proposal]
    #: The DERIVED archive partition of the same fold (W9.23 §3.2): survivors
    #: whose ``age_turns`` reached :data:`ARCHIVE_AFTER_TURNS`. A partition, never
    #: a second read — a decided row appears in neither list.
    archived: list[Proposal]


@dataclass(frozen=True)
class ReviewSet:
    """One read's whole review home: the queue, the archive, and the committed."""

    pending: list[Proposal]
    archived: list[Proposal]
    committed: list[Decision]


def _text(value: Any) -> str:
    """Coerce an optional string field (``None`` -> ``""``)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _handle(event: Any) -> str:
    """The opaque handle for a log row (``str(id)``; see :class:`Proposal`)."""
    return str(event.id)


def _proposal(event: Any) -> Proposal:
    """Build a :class:`Proposal` from one ``asset_proposal`` row."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    target = payload.get("target")
    patch = payload.get("patch")
    signal = payload.get("signal")
    ops = payload.get("ops")
    return Proposal(
        proposal_id=_handle(event),
        session_id=event.session_id,
        kind=_text(payload.get("kind")),
        action=_text(payload.get("action")),
        target=target if isinstance(target, str) and target.strip() else None,
        patch=dict(patch) if isinstance(patch, dict) else {},
        origin=_text(payload.get("origin")),
        signal=(
            float(signal)
            if isinstance(signal, (int, float)) and not isinstance(signal, bool)
            else None
        ),
        seq=event.seq,
        ts=event.ts,
        evidence=_text(payload.get("evidence")).strip(),
        target_name=_text(payload.get("target_name")).strip(),
        ops=(
            tuple(dict(op) for op in ops if isinstance(op, dict))
            if isinstance(ops, list)
            else ()
        ),
        target_new=payload.get("target_new") is True,
    )


def _decision(event: Any) -> Decision:
    """Build a :class:`Decision` from one ``proposal_decision`` row."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    target = payload.get("target")
    pre_image = payload.get("pre_image")
    applied = payload.get("applied")
    return Decision(
        decision_id=_handle(event),
        proposal_id=_text(payload.get("proposal_id")),
        decision=_text(payload.get("decision")),
        kind=_text(payload.get("kind")),
        action=_text(payload.get("action")),
        target=target if isinstance(target, str) and target.strip() else None,
        pre_image=dict(pre_image) if isinstance(pre_image, dict) else None,
        applied=dict(applied) if isinstance(applied, dict) else None,
        seq=event.seq,
        ts=event.ts,
        actor=event.actor,
        target_new=payload.get("target_new") is True,
    )


def _row_order(handle: str) -> int:
    """Is a handle older or newer? The log row id IS insertion order.

    ``events.id`` is ``INTEGER PRIMARY KEY AUTOINCREMENT``, so a higher id was
    appended later — the ordering that works across sessions (a fork's own rows
    always land after the rows it inherits), which ``seq`` alone cannot give for
    the cross-session inbox.
    """
    return int(handle)


def _applicable_key(proposal: Proposal) -> tuple[str, str, str] | None:
    """The identity a live proposal acts on — the supersede key (finding 11).

    ``amend`` / ``remove`` name an existing asset in ``target``; an ``add`` has
    no target by contract, so its identity is the NAME it proposes, canonicalised
    with the cast layer's write-time key (:func:`fabulist.state.cast.canonical_key`
    — the same fold that makes ``Millie``/``millie`` one surface). A proposal with
    neither is unique and can never supersede or be superseded.

    For an AMEND this is only the outer identity (kind + action + target); the
    rows inside one such group are further clustered by their op-path sets
    (:func:`_amend_clusters`), which is what lets two amends on one target with
    DISJOINT paths coexist (W9.23 §3.4.8).

    A BORN-PARENTED row (W9.30 §3.2.4) names a book that does not exist, so its
    ``target`` is a NAME: the key is that name through the cast layer's
    canonicalisation, and every same-name row therefore lands in ONE group.
    """
    if proposal.target:
        if proposal.target_new:
            return (
                proposal.kind,
                proposal.action,
                "name:" + canonical_key(proposal.target),
            )
        return (proposal.kind, proposal.action, "target:" + proposal.target)
    name = proposal.patch.get("name")
    if isinstance(name, str) and name.strip():
        return (proposal.kind, proposal.action, "name:" + canonical_key(name))
    return None


def _op_paths(proposal: Proposal) -> frozenset[str]:
    """The path set an amend row anchors on — the supersede identity (§3.4.8).

    A filed op list contributes its paths; a legacy whole-value amend row
    normalizes to its WRITABLE patch keys, so an old row's identity is the set of
    fields it claims to change and a new op row only collides with it where they
    actually overlap.
    """
    if proposal.ops:
        return frozenset(
            str(op.get("path"))
            for op in proposal.ops
            if isinstance(op, dict) and str(op.get("path") or "").strip()
        )
    return frozenset(
        str(key) for key in proposal.patch if writable_field(proposal.kind, key)
    )


def _amend_clusters(group: Sequence[Proposal]) -> list[list[Proposal]]:
    """Cluster amend rows on one target by op-path intersection (transitive).

    Oldest-to-newest, a row joins EVERY cluster it intersects and those clusters
    merge, so a legacy row bridging two disjoint op rows collapses all three
    (§8.4's transitive-chain pin). A row that intersects nothing starts its own
    cluster; disjoint path sets therefore coexist as separate live rows.

    BORN-PARENTED rows are the ONE exception (W9.30 §3.2.4): every entry add
    mints a FRESH ``entries.<uuid>`` path (:func:`fabulist.state.proposal_lanes.
    _entry_op`), so path intersection is meaningless between two rows that will
    each create a book of the same name — they would coexist as two live rows and
    both materialize. Same-name parented rows therefore form ONE cluster whatever
    their paths, and they never join an ordinary row's cluster (a parented target
    is a name, an ordinary one is an id, so the two cannot even share a group).
    """
    parented = [item for item in group if item.target_new]
    clusters: list[list[Proposal]] = [parented] if parented else []
    for item in group:
        if item.target_new:
            continue
        paths = _op_paths(item)
        hits = [
            cluster
            for cluster in clusters
            if any(_op_paths(member) & paths for member in cluster)
        ]
        if not hits:
            clusters.append([item])
            continue
        merged = [item]
        for cluster in hits:
            merged.extend(cluster)
            clusters.remove(cluster)
        merged.sort(key=lambda p: _row_order(p.proposal_id))
        clusters.append(merged)
    return clusters


def _live(
    decisions: dict[str, Decision], candidates: Sequence[Proposal]
) -> list[Proposal]:
    """Fold candidates into the live set: terminal decisions out, newest wins.

    Oldest-to-newest so a group's winner is the LAST item appended (the plan's
    "newest applicable item"), then newest-first for the surface. Adds / removes /
    cast rows keep their single identity key (name/target); amend rows are
    clustered by op-path intersection first (§3.4.8).
    """
    groups: dict[tuple[str, str, str], list[Proposal]] = {}
    loose: list[Proposal] = []
    for item in sorted(candidates, key=lambda p: _row_order(p.proposal_id)):
        if item.proposal_id in decisions:
            continue
        key = _applicable_key(item)
        if key is None:
            loose.append(item)
            continue
        groups.setdefault(key, []).append(item)

    kept = list(loose)
    for group in groups.values():
        clusters = (
            _amend_clusters(group)
            if group[0].action == "amend"
            else [group]
        )
        for cluster in clusters:
            winner = cluster[-1]
            if len(cluster) > 1:
                winner = replace(
                    winner,
                    supersedes=tuple(item.proposal_id for item in cluster[:-1]),
                )
            kept.append(winner)
    return sorted(kept, key=lambda p: _row_order(p.proposal_id), reverse=True)


def _chain_ages(events: Iterable[Any], *, filed_here: str | None = None) -> dict[str, int]:
    """``{proposal handle: committed turns after it}`` for ONE chain walk.

    ``age_turns(p)`` is exactly :func:`fabulist.graph.nodes.committed_turn_count`'s
    predicate applied to the rows AFTER the proposal's own ``seq``: a count of
    non-ephemeral ``narrative`` events on the walk. Revisions are their own event
    type, so a revised turn counts once. ``filed_here`` restricts the map to rows
    the session actually FILED (the inbox's per-filing-walk rule, §3.2) — the
    inherited copies a fork carries are not re-aged by the fork's own walk.
    """
    rows = list(events)
    seqs = sorted(
        event.seq
        for event in rows
        if getattr(event, "type", None) == "narrative"
        and not getattr(event, "ephemeral", False)
    )
    ages: dict[str, int] = {}
    for event in rows:
        if getattr(event, "type", None) != "system" or not isinstance(
            getattr(event, "payload", None), dict
        ):
            continue
        if event.payload.get("event") != ASSET_PROPOSAL_EVENT:
            continue
        if filed_here is not None and event.session_id != filed_here:
            continue
        ages[_handle(event)] = len(seqs) - bisect_right(seqs, event.seq)
    return ages


def _scan(events: Iterable[Any], *, ages: Mapping[str, int] | None = None) -> _Scan:
    """One pass over a row stream into proposals, decisions, undos, live set.

    ``ages`` is the read's own turn-age map (see :func:`_chain_ages`); when it is
    omitted the rows ARE one chain and their own age is derived from them, which
    is the session-scoped rule. The archive partition runs AFTER supersede, so a
    fresh same-path amend filed past the boundary pulls a previously archived row
    out of the drawer (the collapsed pair lands in neither set — §3.2's pinned
    archive-mutability corollary).
    """
    rows = list(events)
    if ages is None:
        ages = _chain_ages(rows)
    filed: dict[str, Proposal] = {}
    decisions: dict[str, Decision] = {}
    by_decision: dict[str, Decision] = {}
    undone: set[str] = set()
    for event in rows:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        variant = event.payload.get("event")
        if variant == ASSET_PROPOSAL_EVENT:
            item = _proposal(event)
            filed[item.proposal_id] = item
        elif variant == PROPOSAL_DECISION_EVENT:
            row = _decision(event)
            by_decision[row.decision_id] = row
            prior = decisions.get(row.proposal_id)
            # The terminal event wins: one decision per proposal, and a duplicated
            # or racing verdict must not resurrect an older one.
            if prior is None or _row_order(row.decision_id) > _row_order(
                prior.decision_id
            ):
                decisions[row.proposal_id] = row
        elif variant == PROPOSAL_UNDO_EVENT:
            decision_id = event.payload.get("decision_id")
            if isinstance(decision_id, str) and decision_id:
                undone.add(decision_id)
    live: list[Proposal] = []
    archived: list[Proposal] = []
    for item in _live(decisions, list(filed.values())):
        age = int(ages.get(item.proposal_id, 0))
        item = replace(
            item, age_turns=age, archived=age >= ARCHIVE_AFTER_TURNS
        )
        (archived if item.archived else live).append(item)
    return _Scan(
        proposals=filed,
        decisions=decisions,
        by_decision=by_decision,
        undone=frozenset(undone),
        live=live,
        archived=archived,
    )


def _visible_session_ids(log: Any) -> list[str]:
    """Every non-anchor session id known to the log.

    ``EventLog`` exposes no public enumerator (and no all-sessions event read),
    so this reads the fork-bookkeeping ``sessions`` table through a short-lived
    secondary connection — WAL makes the concurrent read safe. Exactly the
    helper :mod:`fabulist.api.sessions`, :mod:`fabulist.personas.registry`,
    :mod:`fabulist.context.books` and :mod:`fabulist.memory.reembed` each carry;
    reserved ``__``-prefixed anchors are internal, never chats.
    """
    conn = sqlite3.connect(log.path)
    try:
        rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [row[0] for row in rows if not str(row[0]).startswith("__")]


# -- reads for the two surfaces ----------------------------------------------


def _definition(log: Any, kind: str, target: str) -> dict | None:
    """The CURRENT folded definition a proposal targets (``None`` when gone)."""
    asset = _KINDS.get(kind)
    if asset is None:
        return None
    try:
        return asset.read(log, target)
    except Exception:  # noqa: BLE001 - a read failure must never take a queue down
        return None


def _path_value(kind: str, definition: Mapping, path: str) -> tuple[bool, Any]:
    """``(present, value)`` for one addressable path in a folded definition.

    The three address classes of W9.23 §3.4.2, one implementation: a scalar field
    (``description``), a nested key (``extensions.mood``) and a list item by its
    stable key (``entries.<entry_id>``). An item resolves to the RAW entry — the
    comparison pushes both sides through
    :func:`~fabulist.log.schema_gate.entry_projection` so a modify can merge over
    keys the slice never showed (``constant``, ``order``, ST extras).
    """
    item = list_item_path(kind, path)
    if item is not None:
        entries = definition.get(item[0])
        if not isinstance(entries, list):
            return False, None
        for entry in entries:
            if isinstance(entry, Mapping) and entry.get(ENTRY_ID_KEY) == item[1]:
                return True, entry
        return False, None
    root, separator, rest = path.partition(".")
    if not separator:
        return (path in definition), definition.get(path)
    cursor: Any = definition.get(root)
    for segment in rest.split("."):
        if not isinstance(cursor, Mapping) or segment not in cursor:
            return False, None
        cursor = cursor[segment]
    return True, cursor


def _same(kind: str, path: str, current: Any, old: Any) -> bool:
    """Is the filed ``old`` the value the fold holds at ``path`` right now?

    Plain ``==`` on JSON-decoded values, except for list items: both sides go
    through :func:`~fabulist.log.schema_gate.entry_projection` (the shown key
    set), so an ``old`` echoed from the slice — which also carries the entry's
    ``id`` and its ``truncated`` marker — compares equal to the stored entry's
    projected view. A field the slice never showed therefore cannot make an op
    look stale, and cannot be changed by one either.
    """
    if list_item_path(kind, path) is not None:
        return entry_projection(current) == entry_projection(old)
    return current == old


def _stale_paths(
    kind: str,
    definition: Mapping | None,
    ops: Sequence[Mapping],
    *,
    target_new: bool = False,
) -> tuple[str, ...]:
    """The op paths whose current value no longer matches the filed ``old``.

    Read-time staleness (W9.23 §4): the wire shows a stale badge BEFORE the owner
    clicks, using exactly the comparison the accept will run. Paths are returned
    as the filed ``op.path`` strings, byte-identical — the surface matches them
    against the ops it renders, so a normalized spelling would silently un-badge
    a stale row. A vanished target makes every path stale: nothing can be applied
    to it.

    An ``add`` is stale when its path now EXISTS (someone got there first); a
    ``modify``/``delete`` is stale when its path is missing or holds a different
    value. Legacy whole-value rows carry no ops and therefore nothing anchored.

    A BORN-PARENTED row is exempt (W9.30 §3.2.5): it targets a book that does not
    exist by design, so there is no definition to anchor an ``old`` against and
    every op is an add. Nothing can be stale, and the surface badges it as a
    CREATE instead.
    """
    if target_new:
        return ()
    if definition is None:
        return tuple(str(op.get("path") or "") for op in ops)
    stale: list[str] = []
    for op in ops:
        if not isinstance(op, Mapping):
            continue
        path = str(op.get("path") or "")
        action = str(op.get("op") or "")
        if not path:
            continue
        present, current = _path_value(kind, definition, path)
        if action == "add":
            if present:
                stale.append(path)
            continue
        if not present or not _same(kind, path, current, op.get("old")):
            stale.append(path)
    return tuple(stale)


def _with_staleness(log: Any, items: Sequence[Proposal]) -> list[Proposal]:
    """Attach read-time ``stale_paths`` to the op rows of one read.

    One definition read per (kind, target) however many rows name it, and only for
    rows that carry ops: a legacy whole-value row anchored nothing, so nothing can
    be stale on it. A born-parented row's target is a NAME, so the read finds
    nothing and :func:`_stale_paths` exempts it.
    """
    definitions: dict[tuple[str, str], dict | None] = {}
    rows: list[Proposal] = []
    for item in items:
        if not item.ops or not item.target or item.kind not in _KINDS:
            rows.append(item)
            continue
        cache_key = (item.kind, item.target)
        if cache_key not in definitions:
            definitions[cache_key] = _definition(log, item.kind, item.target)
        stale = _stale_paths(
            item.kind,
            definitions[cache_key],
            item.ops,
            target_new=item.target_new,
        )
        rows.append(replace(item, stale_paths=stale) if stale else item)
    return rows


def display_ops(
    log: Any,
    item: Proposal,
    *,
    definitions: dict[tuple[str, str], dict | None] | None = None,
) -> list[dict]:
    """The operation list the WIRE serves for one proposal (W9.23 §4).

    Two cases, one shape, so a surface never has to branch:

    * an op row serves its filed list VERBATIM (paths byte-identical to the ones
      ``stale_paths`` names);
    * a LEGACY whole-value amend (a row filed before the diff-first contract, or
      by a promotion path that still writes one) is hydrated server-side into one
      whole-field ``modify`` row per writable patch key, with ``old`` read from
      the current fold and ``legacy: true``. The row carries no anchored old — it
      applies newest-wins exactly as it always did, and its ``stale_paths`` stay
      empty — but the OWNER still sees ``old -> new`` instead of an unexplained
      blob.

    ``definitions`` is an optional caller-owned cache (one read per target for a
    whole queue read).
    """
    if item.ops:
        return [dict(op) for op in item.ops]
    if item.action != "amend" or not item.patch or not item.target:
        return []
    cache = definitions if definitions is not None else {}
    cache_key = (item.kind, item.target)
    if cache_key not in cache:
        cache[cache_key] = _definition(log, item.kind, item.target)
    definition = cache[cache_key] or {}
    rows: list[dict] = []
    for key, value in item.patch.items():
        if not writable_field(item.kind, str(key)):
            continue
        present, current = _path_value(item.kind, definition, str(key))
        rows.append(
            {
                "op": "modify",
                "path": str(key),
                "old": current if present else None,
                "new": value,
                "legacy": True,
            }
        )
    return rows


def proposals(log: Any, session_id: str) -> list[Proposal]:
    """The session's live proposal set, newest first (fork-aware chain read).

    Decided proposals leave the set (their terminal row wins) and same-identity
    ones collapse into the newest (finding 11); the survivors carry the collapsed
    ids on ``supersedes``. Rows carrying ops are aged against the same chain
    (:func:`_chain_ages`) and carry read-time ``stale_paths``; the ones past
    :data:`ARCHIVE_AFTER_TURNS` are in :func:`review`'s archive partition, not
    here.
    """
    return _with_staleness(log, _scan(_chain_events(log, session_id)).live)


def review(log: Any, session_id: str) -> ReviewSet:
    """One session's WHOLE review home in ONE fold: queue, archive, committed.

    The session-scoped read of W9.23 §3.2: ``age_turns`` is derived for every live
    row from THIS chain's committed narratives (a branch derives its own age — the
    two-scopes doctrine), the survivors are partitioned at
    :data:`ARCHIVE_AFTER_TURNS`, and each op row carries its read-time
    ``stale_paths``. The archive is a partition of the same fold, never a second
    read and never a deletion (Q4/D9).
    """
    scan = _scan(_chain_events(log, session_id))
    return ReviewSet(
        pending=_with_staleness(log, scan.live),
        archived=_with_staleness(log, scan.archived),
        committed=_committed_rows(scan),
    )


def archived_ids(log: Any, session_id: str) -> frozenset[str]:
    """The ids in one session's archive partition (the mass-op guard's input).

    W9.23 §3.3: archived rows are never mass-selected, so a MULTI-id decide names
    none of them; a SINGLE-id decide may (the owner opened the drawer and meant
    THAT row). The rule needs this set at the request seam, and nothing else —
    hence a read that answers exactly one question.
    """
    return frozenset(item.proposal_id for item in _scan(_chain_events(log, session_id)).archived)


def pending(log: Any, session_id: str) -> list[Proposal]:
    """The review home's QUEUE read — the same fold under the plan's name.

    Plan §4 calls the queue ``pending``; :func:`proposals` is the same list. One
    implementation, two names the plan and the surfaces both use, so the drawer
    and the studio panel can never drift apart. Rows past
    :data:`ARCHIVE_AFTER_TURNS` are NOT here — they are :func:`review`'s archive
    partition (W9.23 §3.2), which the surfaces render as their own drawer.
    """
    return proposals(log, session_id)


def _committed_rows(scan: _Scan) -> list[Decision]:
    """The accepted, not-undone decisions of one fold, newest first."""
    rows = [
        row
        for row in scan.by_decision.values()
        if row.decision == "accept" and row.decision_id not in scan.undone
    ]
    return sorted(rows, key=lambda d: _row_order(d.decision_id), reverse=True)


def committed(log: Any, session_id: str) -> list[Decision]:
    """Accepted decisions, newest first, each with its undo handle + pre-image.

    This is the plan's "committed items on TOP, each with UNDO" read: an undone
    decision leaves the list (the compensating ``proposal_undo`` row wins) and
    reject/dismiss rows never enter it — they applied nothing.
    """
    return _committed_rows(_scan(_chain_events(log, session_id)))


def inbox_review(log: Any) -> tuple[list[Proposal], list[Proposal]]:
    """Every session's ``(live, archived)`` in ONE fold — the inbox partition.

    ASSET-scoped on purpose (see the module docstring): an accepted proposal
    writes a catalog asset that every session shares, so a decision recorded in
    ANY session retires the item — which is also what makes the fork-inherited
    duplicate rows harmless, since a proposal row (and a decision row) has one id
    however many chains carry it.

    The cost is one chain read per visible session with no per-session fold: the
    log exposes no all-sessions event read, and its ``sessions`` table is the only
    enumerator, so a single :func:`_scan` over the union dedupes by handle and
    applies supersede globally. AGE, however, comes only from the FILING session's
    own walk (W9.23 §3.2): time passing in one chat must not archive another
    chat's queue, and an inherited copy is not re-aged by the branch that carries
    it. A soft-hidden session still contributes — its proposal names a real
    catalog asset, not a transcript row.
    """
    events: list[Any] = []
    ages: dict[str, int] = {}
    for session_id in _visible_session_ids(log):
        rows = _chain_events(log, session_id)
        events.extend(rows)
        ages.update(_chain_ages(rows, filed_here=session_id))
    scan = _scan(events, ages=ages)
    return _with_staleness(log, scan.live), _with_staleness(log, scan.archived)


def inbox(log: Any) -> list[Proposal]:
    """Every session's LIVE proposals in ONE fold (the cross-session queue).

    The live half of :func:`inbox_review`: one implementation, so the inbox can
    never disagree with the drawer about what is archived.
    """
    return inbox_review(log)[0]


# -- producers ---------------------------------------------------------------


def emit_cast_proposal(
    log: Any, session_id: str, name: str, aliases: Sequence[str], origin: str
) -> Proposal:
    """File ONE cast candidate as a ``kind="cast"`` proposal (L4 pinned shape).

    The user-gated door for a newly met character: a candidate is a proposal the
    owner ADMITS, never an automatic admit (W9.22 L4 reconciliation — the roster
    diff is now only what an engine path auto-admitted). The row is the pinned
    event::

        kind="cast", action="add", target=None,
        patch={"name": str, "aliases": [str], "origin": str}, origin=<origin>

    ``aliases`` ride the PATCH because they are a CAST-layer fact, not an asset
    field (``schema_gate.ASSET_PROPOSAL_PATCH_FIELDS``), and ``origin`` is the
    admitting origin the cast layer records (``bts`` for this producer). The
    payload goes through the append door's schema gate like every other
    producer's, so a nameless candidate is refused here rather than queued as a
    dead item. Returns the folded :class:`Proposal` (its handle is the row id).
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ProposalError("a cast proposal requires a non-empty member name")
    surfaces: list[str] = []
    for alias in aliases:
        text = str(alias or "").strip()
        if text and text != cleaned and text not in surfaces:
            surfaces.append(text)
    event = log.append_event(
        session_id,
        "system",
        origin,
        {
            "event": ASSET_PROPOSAL_EVENT,
            "kind": "cast",
            "action": "add",
            "target": None,
            "patch": {"name": cleaned, "aliases": surfaces, "origin": origin},
            "origin": origin,
            "signal": None,
        },
    )
    return _proposal(event)


# -- decisions ---------------------------------------------------------------


def accept_proposal(
    log: Any, session_id: str, proposal_id: Any, *, actor: str = USER_ACTOR
) -> Decision:
    """ACCEPT a proposal: write the asset, then record the decision (finding 13).

    The ONLY place a proposal's asset write happens. The patch is applied through
    the asset's existing append API, the pre-image of what it replaced is captured
    in the same breath, and the resulting definition is read BACK from the fold so
    ``applied`` is what the log actually says rather than what we meant.
    """
    return _decide(log, session_id, proposal_id, "accept", actor=actor)


def reject_proposal(
    log: Any, session_id: str, proposal_id: Any, *, actor: str = USER_ACTOR
) -> Decision:
    """REJECT a proposal — "no, do not do that". Terminal; no asset write."""
    return _decide(log, session_id, proposal_id, "reject", actor=actor)


def dismiss_proposal(
    log: Any, session_id: str, proposal_id: Any, *, actor: str = USER_ACTOR
) -> Decision:
    """DISMISS a proposal — "not now". Terminal; no asset write.

    Distinct from reject on purpose: the owner may want to see "I ignored this"
    rows in the history without them reading as a judgment on the content.
    """
    return _decide(log, session_id, proposal_id, "dismiss", actor=actor)


def accept_many(
    log: Any, session_id: str, proposal_ids: Iterable[Any], *, actor: str = USER_ACTOR
) -> list[Decision]:
    """Mass approval (mask batching): accept a list in one call, ONE row each.

    The UI batches, the log does not: every item gets its own ``proposal_decision``
    row so a granular UNDO survives a mass accept (finding 13 — one compensating
    append per accept). Ids are de-duplicated first and every id is checked
    against the live set BEFORE anything is written, so a stale id in a batch
    fails the call instead of half-applying it. A later failure (an asset the
    studio deleted meanwhile) leaves the earlier decisions landed — they are
    individually undoable, which is exactly why the rows are granular.
    """
    ordered: list[str] = []
    for value in proposal_ids:
        handle = _handle_value(value)
        if handle not in ordered:
            ordered.append(handle)

    scan = _scan(_chain_events(log, session_id))
    live = {item.proposal_id for item in scan.live}
    missing = [handle for handle in ordered if handle not in live]
    if missing:
        raise ProposalError("not a live proposal: " + ", ".join(missing))
    return [
        _decide(log, session_id, handle, "accept", actor=actor) for handle in ordered
    ]


def undo_decision(
    log: Any, session_id: str, decision_id: Any, *, actor: str = USER_ACTOR
) -> Decision:
    """UNDO one accepted decision by appending its pre-image back (finding 13).

    A compensating APPEND, never a rewrite (D9/D10): an accepted ``add`` is undone
    by tombstoning what it created (the definition rows stay queryable), an
    accepted ``amend``/``remove`` by re-appending the captured pre-image through
    the same asset API — which resurrects a removed asset latest-wins. Returns the
    decision that was compensated. Undoing twice, or undoing a reject/dismiss, is
    refused: neither has an applied write to take back.

    Scope note for the surface: the pre-image is what the ACCEPT replaced, so an
    undo re-appends it latest-wins even if the owner edited the same asset in the
    studio afterwards. That is the plan's compensating-append semantic (the edit
    rows stay readable); a surface that wants to warn about it can compare the
    current definition with ``applied`` before offering the button.
    """
    scan = _scan(_chain_events(log, session_id))
    handle = _handle_value(decision_id)
    row = scan.by_decision.get(handle)
    if row is None:
        raise ProposalError(
            f"unknown decision {decision_id!r} in session {session_id!r}"
        )
    if row.decision != "accept":
        raise ProposalError(
            f"decision {handle} was a {row.decision}: it applied nothing to undo"
        )
    if handle in scan.undone:
        raise ProposalError(f"decision {handle} was already undone")
    _restore(log, session_id, row)
    log.append_event(
        session_id,
        "system",
        actor,
        {"event": PROPOSAL_UNDO_EVENT, "decision_id": handle},
    )
    return row


# -- internals ---------------------------------------------------------------


def _handle_value(value: Any) -> str:
    """Normalise a caller's handle (string or int id) to its opaque form."""
    if value is None:
        return ""
    return str(value).strip()


def _decide(
    log: Any, session_id: str, proposal_id: Any, decision: str, *, actor: str
) -> Decision:
    """Shared body of accept/reject/dismiss: resolve, maybe apply, append ONE row."""
    scan = _scan(_chain_events(log, session_id))
    handle = _handle_value(proposal_id)
    item = scan.proposals.get(handle)
    if item is None:
        raise ProposalError(
            f"unknown proposal {proposal_id!r} in session {session_id!r}"
        )
    terminal = scan.decisions.get(handle)
    if terminal is not None:
        raise ProposalError(
            f"proposal {handle} was already {terminal.decision} "
            f"(decision {terminal.decision_id})"
        )
    if handle not in {live.proposal_id for live in scan.live} and handle not in {
        archived.proposal_id for archived in scan.archived
    }:
        winner = next(
            (
                row
                for row in [*scan.live, *scan.archived]
                if handle in row.supersedes
            ),
            None,
        )
        raise ProposalError(
            f"proposal {handle} was superseded by "
            f"{winner.proposal_id if winner is not None else 'a newer proposal'}"
        )

    pre_image: dict | None = None
    applied: dict | None = None
    if decision == "accept":
        # Note the order: the asset write happens BEFORE the decision row because
        # the row must record what was written, and the pre-image is read in the
        # same step. The payload's key set is the gate's own contract, pinned by
        # tests/test_proposals.py, so a shape drift fails a test rather than
        # leaving an unrecorded apply.
        pre_image, applied = _apply(log, session_id, item)

    event = log.append_event(
        session_id,
        "system",
        actor,
        {
            "event": PROPOSAL_DECISION_EVENT,
            "proposal_id": item.proposal_id,
            "decision": decision,
            "kind": item.kind,
            "action": item.action,
            "target": item.target,
            "pre_image": pre_image,
            "applied": applied,
            # W9.30 §4.1: a parented accept is the only accepted amend without a
            # pre-image, and this marker is its license (the append door refuses
            # a null pre-image on an ordinary amend).
            "target_new": item.target_new,
        },
    )
    return _decision(event)


def _apply(
    log: Any, session_id: str, item: Proposal
) -> tuple[dict | None, dict | None]:
    """Write the proposal's patch through the asset API; return (pre, applied).

    An ``amend`` is applied as an OPERATION LIST when the row carries one
    (:func:`_apply_ops`, W9.23 §3.4.5) and as a whole-value merge when it is a
    legacy row. The write itself is wrapped: an append API that refuses the
    definition is a REFUSAL the owner can read, never a 500 — nothing landed, the
    asset is untouched and no decision row is written.

    A BORN-PARENTED books amend (``target_new``, W9.30 §4.1) takes the
    materialize branch BEFORE the pre-image read: its target does not exist by
    design, so there is nothing to read and nothing to refuse.
    """
    if item.kind == "cast":
        return _apply_cast(log, session_id, item)
    asset = _kind(item.kind)
    if item.action == "add":
        written = _write(log, item, asset, dict(item.patch))
        created = asset.read(log, written)
        if created is None:
            raise ProposalError(f"the {item.kind} write did not land (id {written!r})")
        if item.kind == "book":
            # W9.30 live gate: a proposal-created book must REACH THE PROMPT, not
            # just the catalog. The live books lane emits the plain ``add`` shape
            # (patch with name + entries) at least as readily as the parented
            # amend — both shapes mean "new book for THIS chat's lore", so both
            # attach (the same contained :func:`_attach_book` the parented path
            # uses). Without this, an accepted add is inert permanence: the book
            # exists, renders nowhere, and the owner wonders why nothing changed.
            try:
                _attach_book(log, session_id, written)
            except Exception:  # noqa: BLE001 - an unattached book is a studio fix (D8)
                logger.warning(
                    "could not attach book %r to session %r after accepting add "
                    "proposal %r",
                    written,
                    session_id,
                    item.proposal_id,
                    exc_info=True,
                )
        return None, created

    if item.kind == "book" and item.target_new:
        # W9.30 §4.1: the parent does not exist YET — the accept materializes it
        # and then applies the filed entry adds to it (the schema gate licenses
        # this shape only for a books amend).
        return _apply_parented_book(log, session_id, item)

    if not item.target:
        # The schema gate refuses a target-less amend/remove at the append door,
        # so this is a pre-gate row: refuse rather than guess an asset.
        raise ProposalError(
            f"{item.kind} proposal {item.proposal_id} names no target to {item.action}"
        )
    pre_image = asset.read(log, item.target)
    if pre_image is None:
        raise ProposalError(f"{item.kind} {item.target!r} does not exist")

    if item.action == "amend":
        merged = (
            _apply_ops(item, pre_image)
            if item.ops
            else _amended(item.kind, asset, pre_image, item.patch)
        )
        # Neither shape may re-home the asset: identity stays the target's.
        merged[asset.id_key] = item.target
        _write(log, item, asset, merged)
        amended = asset.read(log, item.target)
        return pre_image, amended if amended is not None else merged

    if item.action == "remove":
        asset.tombstone(log, item.target)
        return pre_image, None

    raise ProposalError(f"unknown proposal action {item.action!r}")


def _book_by_name(log: Any, name: str) -> Book | None:
    """The live catalog book whose NAME is ``name`` (:func:`canonical_key` fold).

    The duplicate-name guard's read (W9.30 §4.1/B1b). It compares through the
    cast layer's write-time key — the same fold that makes ``The Vale Ledger``
    and ``vale ledger`` one surface — because two books whose names collapse to
    one key are the near-duplicate case, not two names.
    """
    key = canonical_key(name)
    if not key:
        return None
    for book in list_books(log):
        if canonical_key(book.name) == key:
            return book
    return None


def _session_books_registration(log: Any, session_id: str) -> list[str]:
    """The session's OWN book registration — the ``session_books`` fold ALONE.

    The latest ``session_books`` row on the fork-aware chain (D31's latest-wins
    fold) and nothing else. Deliberately NOT
    :func:`fabulist.context.facts._entity_book_ids`, which POOLS this fold with
    the chat persona's books and every ``world_ref`` world's books for the render
    (W9.30 review S1): writing that pool back into the registration would copy
    those ids into the session row permanently, defeating the fold's clear/
    detach contract — and the undo below could no longer compensate exactly,
    because it would have to guess which ids it had added.
    """
    registered: list[str] = []
    for event in _chain_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != SESSION_BOOKS_EVENT:
            continue
        value = event.payload.get("book_ids")
        registered = (
            [item for item in value if isinstance(item, str)]
            if isinstance(value, list)
            else []
        )
    return registered


def _attach_book(log: Any, session_id: str, book_id: str) -> None:
    """Add one book to the session's registration, preserving what is there.

    Seeding from the REGISTRATION alone ∪ the created id is the minimal write
    that makes the new book reach this chat's prompt next turn (W9.30 D3); an id
    the owner attached meanwhile is kept because the fold is read at write time.
    """
    registered = _session_books_registration(log, session_id)
    if book_id in registered:
        return
    set_session_books(log, session_id, [*registered, book_id])


def _detach_book(log: Any, session_id: str, book_id: str) -> None:
    """Remove one book from the session's registration — exactly that id.

    The exact-compensation twin of :func:`_attach_book`: the fold is read at
    UNDO time, so ids the owner attached meanwhile survive and a book the owner
    already detached is not re-registered. Nothing is written when the id is not
    registered at all (an attach that failed at accept time leaves nothing to
    take back).
    """
    registered = _session_books_registration(log, session_id)
    if book_id in registered:
        set_session_books(
            log, session_id, [book for book in registered if book != book_id]
        )


def _apply_parented_book(
    log: Any, session_id: str, item: Proposal
) -> tuple[dict | None, dict | None]:
    """Materialize the book a born-parented amend targets, then apply its ops.

    W9.30 §4.1, in this order and for these reasons:

    1. **Duplicate-name refusal** (review B1b): a live catalog book whose name is
       the target's own under :func:`canonical_key` is the very conflation the
       parse-side near-match guard refuses, one window later — the owner
       re-points or renames instead of getting a twin. Nothing is written.
    2. **Seed** the parent deterministically: the name, an empty entry list, and
       provenance back to this proposal (``append_book`` mints the id).
    3. **Apply the ops** through the SAME verifier every amend uses
       (:func:`_apply_ops`) over the seed, so a malformed op refuses exactly as
       it would anywhere else — and nothing has landed yet.
    4. **Write** through ``append_book`` (the studio's own API) and read the
       created definition back, so ``applied`` is what the log actually says.
    5. **Attach** it to THIS chat (§4.1/§9 D3: session-level, never the world's
       ``book_ids``, which every chat of that world shares) so the lore reaches
       the prompt next turn. An attach failure is CONTAINED (§9 D8): it is
       logged, the decision still records, and the owner attaches in the studio —
       the alternative, a live proposal over an already-created book, risks a
       duplicate on re-accept.
    """
    name = (item.target_name or item.target or "").strip()
    if not name:
        raise ProposalError(f"book proposal {item.proposal_id} names no book to create")
    if not item.ops:
        raise ProposalError(
            f"book proposal {item.proposal_id} would create '{name}' but carries "
            "no entry operations to apply"
        )
    existing = _book_by_name(log, name)
    if existing is not None:
        raise ProposalError(
            f"proposal {item.proposal_id} would create a second book named "
            f"'{name}': '{existing.name}' ({existing.book_id}) already exists — "
            "amend that book, or re-propose under a different name"
        )
    asset = _kind("book")
    seed = {
        "name": name,
        "entries": [],
        "provenance": {"origin": "proposal", "proposal_id": item.proposal_id},
    }
    written = _write(log, item, asset, _apply_ops(item, seed))
    created = asset.read(log, written)
    if created is None:
        raise ProposalError(f"the book write did not land (id {written!r})")
    try:
        _attach_book(log, session_id, written)
    except Exception:  # noqa: BLE001 - an unattached book is a studio fix (D8)
        logger.warning(
            "could not attach book %r to session %r after accepting proposal %r",
            written,
            session_id,
            item.proposal_id,
            exc_info=True,
        )
    return None, created


def _write(log: Any, item: Proposal, asset: _AssetKind, definition: dict) -> str:
    """One append through the asset's API, with its refusals made readable.

    ``append_book`` (and its siblings) raise ``ValueError`` on a definition they
    will not persist. The accept has already captured nothing at that point, so
    the refusal must reach the owner as a :class:`ProposalError` — the decide
    door's 4xx / per-id ``rejected`` slot — rather than as a 500 the surface
    cannot explain.
    """
    try:
        return asset.write(log, definition)
    except ProposalError:
        raise
    except ValueError as exc:
        raise ProposalError(
            f"proposal {item.proposal_id} could not be applied: {exc}"
        ) from exc


def _stale(item: Proposal, path: str) -> ProposalError:
    """The readable refusal for an op that no longer matches the fold (§3.4.5)."""
    return ProposalError(
        f"proposal {item.proposal_id} is stale: '{path}' changed since it was "
        "filed (the card was edited, or a newer proposal landed) — reject it, "
        "or re-propose against the current value"
    )


def _apply_ops(item: Proposal, pre_image: Mapping) -> dict:
    """Fold an op list over the CURRENT definition (W9.23 §3.4.5).

    The pre-image is read fresh at accept and every op is verified against it, so
    a stale op REFUSES before anything is written: the accept lands nothing, the
    asset stays byte-identical and no decision row appears.

    The owner's core invariant lives here: a ``delete`` removes EXACTLY its path —
    one nested key (siblings untouched), one entry by its stable id (the others
    keep their order and their bytes), or one scalar field (the rest of the
    definition is the pre-image). A ``modify`` on an entry MERGES ``new`` over the
    raw entry, so the flags the slice never showed (``constant``, ``order``,
    ``case_sensitive``, ST extras) survive; on a scalar or nested path it replaces
    the value whole.
    """
    merged = copy.deepcopy(dict(pre_image))
    kind = item.kind
    for op in item.ops:
        if not isinstance(op, Mapping):
            continue
        path = str(op.get("path") or "")
        action = str(op.get("op") or "")
        if action not in ASSET_PROPOSAL_OP_ACTIONS:
            # The append door refuses an unknown verb, so this is a hand-written
            # row: refuse loudly rather than "applying" nothing after the owner
            # clicked accept.
            raise ProposalError(
                f"proposal {item.proposal_id} carries an unknown operation {action!r}"
            )
        if not path or not writable_field(kind, path):
            # An extra the gate tolerates (D4) is not a write instruction: the
            # apply ignores it, exactly as the legacy merge ignores an unknown
            # patch key.
            continue
        present, current = _path_value(kind, merged, path)
        if action == "add":
            if present:
                raise _stale(item, path)
            _write_path(kind, merged, path, op.get("new"))
            continue
        if not present or not _same(kind, path, current, op.get("old")):
            raise _stale(item, path)
        if action == "delete":
            _drop_path(kind, merged, path)
        elif action == "modify":
            _replace_path(kind, merged, path, current, op.get("new"))
    return merged


def _write_path(kind: str, definition: dict, path: str, value: Any) -> None:
    """Create ``path``: one new entry, or a scalar/nested key (parents created)."""
    item = list_item_path(kind, path)
    if item is not None:
        field, entry_id = item
        entries = definition.get(field)
        entries = list(entries) if isinstance(entries, list) else []
        entry = dict(value) if isinstance(value, Mapping) else {}
        entry[ENTRY_ID_KEY] = entry_id
        entries.append(entry)
        definition[field] = entries
        return
    _set_nested(definition, path, value)


def _replace_path(
    kind: str, definition: dict, path: str, current: Any, value: Any
) -> None:
    """Write ``value`` at ``path``: entries MERGE, scalars/nested replace."""
    item = list_item_path(kind, path)
    if item is not None:
        field, entry_id = item
        entries = list(definition.get(field) or [])
        for index, entry in enumerate(entries):
            if isinstance(entry, Mapping) and entry.get(ENTRY_ID_KEY) == entry_id:
                # Merge over the RAW entry: keys the slice never showed stay.
                updated = dict(entry)
                updated.update(entry_projection(value))
                updated[ENTRY_ID_KEY] = entry_id
                entries[index] = updated
                break
        definition[field] = entries
        return
    _set_nested(definition, path, value)


def _drop_path(kind: str, definition: dict, path: str) -> None:
    """Remove EXACTLY one path: one entry, or one scalar/nested key."""
    item = list_item_path(kind, path)
    if item is not None:
        field, entry_id = item
        entries = [
            entry
            for entry in definition.get(field) or []
            if not (isinstance(entry, Mapping) and entry.get(ENTRY_ID_KEY) == entry_id)
        ]
        definition[field] = entries
        return
    root, separator, rest = path.partition(".")
    if not separator:
        definition.pop(root, None)
        return
    cursor: Any = definition.get(root)
    segments = rest.split(".")
    for segment in segments[:-1]:
        if not isinstance(cursor, Mapping):
            return
        cursor = cursor.get(segment)
    if isinstance(cursor, dict):
        cursor.pop(segments[-1], None)


def _set_nested(definition: dict, path: str, value: Any) -> None:
    """Set a scalar field or one nested key, creating the parent mappings."""
    root, separator, rest = path.partition(".")
    if not separator:
        definition[root] = value
        return
    branch = definition.get(root)
    branch = dict(branch) if isinstance(branch, Mapping) else {}
    cursor = branch
    segments = rest.split(".")
    for segment in segments[:-1]:
        child = cursor.get(segment)
        child = dict(child) if isinstance(child, Mapping) else {}
        cursor[segment] = child
        cursor = child
    cursor[segments[-1]] = value
    definition[root] = branch


def _amended(
    kind: str, asset: _AssetKind, pre_image: dict, patch: Mapping
) -> dict:
    """The definition an amend writes: WRITABLE keys only, nested paths merged.

    Two rules, both schema-gated (:func:`fabulist.log.schema_gate.writable_field`):

    * a key that is not writable for this kind is IGNORED — the schema gate
      already refuses an amend with no writable field, so an accepted amend
      always writes something, and an extra the lane sent (a stray label) can
      never leak into the definition;
    * a dotted key writes ONE nested key (``extensions.mood``), leaving the
      siblings of that mapping alone — which is what makes a CUSTOM persona
      extension proposable without replacing every other extension on accept.
    """
    merged = dict(pre_image)
    for key, value in patch.items():
        if not writable_field(kind, key):
            continue
        root, separator, rest = key.partition(".")
        if not separator:
            merged[root] = value
            continue
        branch = merged.get(root)
        branch = dict(branch) if isinstance(branch, dict) else {}
        cursor = branch
        segments = rest.split(".")
        for segment in segments[:-1]:
            child = cursor.get(segment)
            child = dict(child) if isinstance(child, dict) else {}
            cursor[segment] = child
            cursor = child
        cursor[segments[-1]] = value
        merged[root] = branch
    return merged


def _apply_cast(
    log: Any, session_id: str, item: Proposal
) -> tuple[dict | None, dict | None]:
    """Admit a cast CANDIDATE through the cast layer's pinned helper (L3).

    A cast member is log rows, not a stored definition, so there is no pre-image
    and no append API: ``applied`` records the member the accept admitted (the
    same snapshot rule every other kind follows), and the helper returns the
    member id when it has one.
    """
    patch = item.patch
    name = patch.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProposalError(
            f"cast proposal {item.proposal_id} names no member to admit"
        )
    aliases = [
        str(alias).strip()
        for alias in (patch.get("aliases") or [])
        if isinstance(alias, str) and str(alias).strip()
    ]
    origin = str(patch.get("origin") or item.origin or "")
    # The admit path REFUSES a name that is the player's persona (W9.32/R3): the
    # player is a node, never a cast member. That refusal is a ``ValueError``, and
    # the accept has landed nothing at that point, so it must reach the owner as
    # the decide door's readable 4xx — the same translation :func:`_write` makes.
    try:
        member_id = _cast_admit()(log, session_id, name.strip(), aliases, origin)
    except ProposalError:
        raise
    except ValueError as exc:
        raise ProposalError(
            f"cast proposal {item.proposal_id} cannot be accepted: {exc}"
        ) from exc
    applied: dict = {
        "name": name.strip(),
        "aliases": aliases,
        "origin": origin,
    }
    if isinstance(member_id, str) and member_id:
        applied["member_id"] = member_id
    return None, applied


def _restore(log: Any, session_id: str, decision: Decision) -> None:
    """Put back what an accepted decision changed, through the same asset API."""
    if decision.kind == "cast":
        _restore_cast(log, session_id, decision)
        return
    asset = _kind(decision.kind)
    if decision.target_new:
        # A parented accept CREATED its parent (W9.30 §4.2), so its undo is the
        # add-style tombstone PLUS the attachment it made — there is no pre-image
        # to re-append.
        _restore_parented_book(log, session_id, decision, asset)
        return
    if decision.action == "add":
        created = (decision.applied or {}).get(asset.id_key)
        if not isinstance(created, str) or not created:
            raise ProposalError(
                f"decision {decision.decision_id} does not record what it created"
            )
        # Tombstone, not delete (D9): the created definition stays queryable.
        asset.tombstone(log, created)
        if decision.kind == "book":
            # The accept attached a proposal-created book (both the add and the
            # parented shapes do), so the undo detaches it — the same exact
            # registration-minus-id write :func:`_restore_parented_book` makes.
            try:
                _detach_book(log, session_id, created)
            except Exception:  # noqa: BLE001 - a stray registration is a studio fix
                logger.warning(
                    "could not detach book %r from session %r on undo of %r",
                    created,
                    session_id,
                    decision.decision_id,
                    exc_info=True,
                )
        return

    if not isinstance(decision.pre_image, dict):
        raise ProposalError(
            f"decision {decision.decision_id} carries no pre-image to restore"
        )
    asset.write(log, dict(decision.pre_image))


def _restore_parented_book(
    log: Any, session_id: str, decision: Decision, asset: _AssetKind
) -> None:
    """Undo a parented accept: tombstone the created book AND detach it exactly.

    W9.30 §4.2: the accept created a book and registered it on THIS session, so
    the compensation does both — a tombstone (D9: the definition rows stay
    queryable) and a registration write of the current fold MINUS the created id.

    The fold is read at UNDO time, which is what makes the detach exact in both
    directions: ids the owner attached meanwhile survive untouched, and a book
    the owner already detached is not re-registered. Nothing is written when the
    id is not registered at all (an attach that failed at accept time leaves
    nothing to take back).
    """
    created = (decision.applied or {}).get(asset.id_key)
    if not isinstance(created, str) or not created:
        raise ProposalError(
            f"decision {decision.decision_id} does not record the book it created"
        )
    _detach_book(log, session_id, created)
    asset.tombstone(log, created)


def _restore_cast(log: Any, session_id: str, decision: Decision) -> None:
    """Retire the member an accepted cast proposal admitted (a tombstone, D10).

    The name comes from the decision's own snapshot, so an undo reinstates
    exactly the member this accept admitted even if the roster changed meanwhile.
    """
    applied = decision.applied if isinstance(decision.applied, dict) else {}
    name = applied.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProposalError(
            f"decision {decision.decision_id} does not record which member it admitted"
        )
    _cast_retire()(log, session_id, name.strip())
