"""The cast ladder's promotion rung (W9.30 §5): session cast -> actor persona card.

The ladder has two rungs and this module owns the upper one. A person the engine
detected enters the cast only when the OWNER accepts their cast proposal
(D79, ``cast.admit_from_proposal`` — untouched here, req. 4); that member carries
``provenance: "actor"``. The rung above it turns such a member into a real studio
asset — an actor **persona card** that outlives the chat (req. 5) — through two
doors that share ONE assembly function and ONE suffix algorithm:

* the **engine door** — :func:`promotion_pass`, a deterministic step the historian
  cadence runs (req. 6a, §5.2). It ranks candidate members by their distinct
  registry facts and files at most :data:`PROMOTION_MAX_PER_RUN` ordinary
  ``kind="actor" action="add"`` proposal row (``origin="promotion"``), which the
  owner accepts exactly like any other actor add — no new accept machinery;
* the **manual door** — :func:`apply_promotion`, called by the cast API's promote
  route (req. 6b, §5.3). It writes VERBATIM through
  :func:`fabulist.personas.registry.append_persona`, the same asset-write path as
  the studio CRUD (AGENTS 7), and files no proposal row.

A REUSED NAME, not a returned gate
----------------------------------
``fabulist.state.promotion`` was D79's promotion *gate* — the code that decided
whether the engine could auto-admit a detected person into the cast. The owner
deleted that gate (cast admission is user-gated; nothing a model detects becomes a
member) and W9.30 reuses the module NAME for the ladder rung it created in its
place. Nothing here admits a cast member: this module only reads the roster and
writes a persona definition.

Deterministic, bounded, zero model calls (D4/D7)
------------------------------------------------
The card is ASSEMBLED, never generated: names, aliases, relationships and items
come from the event log's own folds (the roster and the entity registry) in
ESTABLISH order, so a replay or a fork assembles the identical description. What
travels is identity (aliases, relationship edges, held items, provenance); what
does not is turn state — transient meters/traits stay out of the card (D7: a
persona is a durable definition, a stress value is turn state). Authored fields
(``personality`` / ``scenario`` / ``first_mes`` / greetings / ``book_ids`` /
``image_asset``) are the owner's in the studio and stay empty.

One promotion per member per session, by EITHER door (§5.1, review S2)
---------------------------------------------------------------------
A member with a prior promotion is not a candidate again: any ``asset_proposal``
on the chain with ``origin="promotion"`` whose ``patch.provenance.member_id``
matches (live or decided) AND any live persona whose ``provenance.member_id``
matches both block the pass. A proposal that never became a persona (rejected or
dismissed) still blocks — silence after a refusal is the point (§5.2).

The ONE way back onto the cadence is the re-arm (O2): a member whose single prior
row was REJECTED re-enters once, and only once, when their current fact count has
at least DOUBLED since the count recorded on that row. A dismiss is the owner's
"not now" — the same non-accept terminal ruling that wrote nothing — and re-arms
under the identical rule. The count is recorded where the re-arm read can find
it: ``patch.provenance.fact_count`` on the filed row (the row's patch IS the card,
and provenance is already the linkage block the dedupe and the badge read; the
evidence line stays human prose). At most two rows per member can ever exist. The
manual door stays open at ANY time regardless (§5.3) — it is the change-of-heart
path, which is why the re-arm can be this stingy.

Undo asymmetry (documented behaviour, §5.5 + the review note)
------------------------------------------------------------
An ACCEPTED engine promotion is undone by the ordinary decision undo
(:func:`fabulist.state.proposals.undo_decision`) — the accept is a normal
``kind=actor`` add decision, so the undo tombstones the persona. A MANUAL
promotion has no decision row (it is an owner action through the registry API, the
studio's door), so its reverse is the studio's persona delete
(:func:`fabulist.personas.registry.delete_persona`) and nothing else. Both leave
the log append-only; neither re-opens the cadence for that member (a tombstoned
persona no longer blocks the persona fold, but the filed promotion ROW still
does — the manual door is the re-promote path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from fabulist.graph.nodes import committed_turn_count
from fabulist.log.schema_gate import (
    ASSET_PROPOSAL_EVENT,
    PROPOSAL_DECISION_EVENT,
    validate_event,
)
from fabulist.personas.registry import Persona, append_persona, list_personas

from . import entities
from .cast import CastMember, canonical_key, roster
from .proposal_lanes import MAX_ITEM_EVIDENCE_CHARS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ITEM_LINES",
    "MAX_RELATIONSHIP_LINES",
    "PROMOTION_ACTOR",
    "PROMOTION_MAX_PER_RUN",
    "PROMOTION_MIN_FACTS",
    "PROMOTION_ORIGIN",
    "PromotionError",
    "apply_promotion",
    "candidates",
    "fact_count",
    "member_definition",
    "promoted_persona_ids",
    "promotion_eligibility",
    "promotion_pass",
    "suffixed_name",
]

#: The proposal ``kind`` a promotion files: an actor persona (the persona
#: catalog is the reusable cast-member definition, ``proposals._KINDS["actor"]``).
PROMOTION_KIND = "actor"

#: The proposal ``origin`` for an engine promotion — already in
#: ``schema_gate.ASSET_PROPOSAL_ORIGINS`` (the vocabulary outlived the D79 gate)
#: and the axis the review home groups AUTO proposals by (req. 7/§6).
PROMOTION_ORIGIN = "promotion"

#: The actor stamped on the proposal row. Its own value (not ``bts``): the row is
#: minted by the deterministic pass, not by a lane's model call.
PROMOTION_ACTOR = "promotion"

#: Distinct registry facts a member needs before a card is worth minting (§5.2).
#: Below three the card is an empty shell the owner would rewrite anyway.
PROMOTION_MIN_FACTS = 3

#: Proposals one cadence run may file (§5.2). One: the pass is a review-queue
#: trickle, not a batch, and the owner's attention is the scarce resource.
PROMOTION_MAX_PER_RUN = 1

#: Lines one card's description spends on relationship edges (§5.5). A being with
#: a hundred edges must not produce a hundred-line card.
MAX_RELATIONSHIP_LINES = 6

#: Lines one card's description spends on held items. Same reason as
#: :data:`MAX_RELATIONSHIP_LINES` (the plan bounds the relationship list; an
#: unbounded item list would break the same bound from the other end).
MAX_ITEM_LINES = 6

#: System-event kinds carrying a session title anchor (the fold
#: :mod:`fabulist.api.sessions` uses; a revision's ``revised.title`` supersedes).
_TITLE_EVENTS = frozenset({"session_created", "session_renamed"})

#: How one registry key reads when it is not a roster member. The registry's
#: reserved being keys (``entities.PLAYER`` / ``SCENE``) and an off-screen bucket
#: are real subjects of a fact and need a readable surface in a card.
_RESERVED_SURFACES = {
    entities.PLAYER: "the player",
    entities.SCENE: "the scene",
}


class PromotionError(ValueError):
    """A member cannot be promoted through the door that asked.

    A ``ValueError`` subclass so the cast route maps it to a 400 with the message
    intact: the text is written for the owner (req. 7's never-refuse is about NAME
    COLLISIONS, not about eligibility), and the same text is what the promotion
    GET serves as ``reason``.
    """


# -- the collision rule (req. 7, §5.4) ---------------------------------------


def suffixed_name(base: object, taken: Iterable[object]) -> str:
    """``base`` if free, else ``base1`` / ``base2`` / … — NEVER a refusal.

    ``taken`` is the live catalog's display names; comparison runs through the
    cast layer's :func:`~fabulist.state.cast.canonical_key`, the same fold that
    makes ``Millie``/``millie``/``the millie`` one surface — so a promotion cannot
    mint a name the roster would read as a second spelling of an existing card.
    The suffix is the plan's deterministic one: the first free ``base<n>`` in
    order, which is why the owner's pre-filled suggestion is usually ``Tessa1``.
    """
    root = str(base or "").strip()
    if not root:
        # A member always has a display name; an empty one would mint an unnamed
        # card, which ``append_persona`` refuses. Name the failure honestly.
        raise PromotionError("a promotion needs a non-empty member name")
    keys = {
        canonical_key(name)
        for name in taken
        if isinstance(name, str) and name.strip()
    }
    if canonical_key(root) not in keys:
        return root
    index = 1
    while canonical_key(f"{root}{index}") in keys:
        index += 1
    return f"{root}{index}"


# -- the evidence the card is assembled from (the entity registry) -----------
#
# One fact = one identity-stable registry object the member is part of: a meter
# they host, a relationship edge they are an end of (either direction) and an item
# they hold. Presence (a location assignment) is deliberately NOT a fact: being in
# a room is scene state, and a member who merely stood somewhere has no card yet.


def _hosted_meters(reg: entities.Registry, member_id: str) -> list[entities.Meter]:
    """The meters ``member_id`` hosts, in establish order (self/ambient included)."""
    return [meter for meter in reg.meters.values() if meter.host == member_id]


def _involving_relationships(
    reg: entities.Registry, member_id: str
) -> list[entities.Relationship]:
    """The relationship edges ``member_id`` is an end of, in establish order."""
    return [
        edge
        for edge in reg.relationships.values()
        if member_id in (edge.source, edge.target)
    ]


def _held_items(reg: entities.Registry, member_id: str) -> list[entities.Item]:
    """The items ``member_id`` holds, in establish order."""
    return [item for item in reg.items.values() if item.holder == member_id]


def fact_count(reg: entities.Registry, member_id: object) -> int:
    """How many distinct registry facts involve this member (the §5.2 bar's unit).

    Meters hosted + relationship edges (either direction) + items held. This is
    the whole ranking signal of the pass: deterministic, already folded, and one
    cheap integer per candidate.
    """
    key = str(member_id or "")
    if not key:
        return 0
    return (
        len(_hosted_meters(reg, key))
        + len(_involving_relationships(reg, key))
        + len(_held_items(reg, key))
    )


def _surface(key: object, names: Mapping[str, str]) -> str:
    """A readable surface for a registry being key (roster name, reserved, or raw)."""
    text = str(key or "").strip()
    if not text:
        return ""
    return names.get(text) or _RESERVED_SURFACES.get(text) or text


def _edge_detail(edge: object) -> str:
    """One relationship edge's prose, without its MECHANICAL default name.

    An edge established without a label carries ``name == "from→to"`` (the
    registry's own default, ``entities.establish_relationship``) — a raw id
    arrow that adds nothing a reader wants (live gate: the filed card read
    ``Towards Yara: cm_…→cm_… — neighbors``). When the name is that default
    (or empty), the note alone is the detail; a NAME the model actually gave
    stays.
    """
    default_arrow = f"{edge.source}\u2192{edge.target}"
    name = str(getattr(edge, "name", "") or "").strip()
    if not name or name == default_arrow:
        return str(getattr(edge, "note", "") or "").strip()
    return _detail(name, getattr(edge, "note", ""))


def _detail(*parts: object) -> str:
    """Join the non-empty prose parts of one fact with an em dash."""
    return " — ".join(str(part).strip() for part in parts if str(part or "").strip())


def _fact_lines(
    reg: entities.Registry, member_id: str, names: Mapping[str, str]
) -> list[str]:
    """The deterministic fact summary lines the evidence row carries.

    One line per fact, in registry establish order, in the form the owner reads in
    the proposal card ("relationship with Marcus (mentor)", "hostility toward
    Marcus = 4", "holds the brass key").
    """
    lines: list[str] = []
    for edge in _involving_relationships(reg, member_id):
        other = edge.target if edge.source == member_id else edge.source
        detail = _detail(edge.name, edge.note)
        label = f"relationship with {_surface(other, names)}"
        lines.append(f"{label} ({detail})" if detail else label)
    for meter in _hosted_meters(reg, member_id):
        if meter.subject:
            label = f"{meter.key} toward {_surface(meter.subject, names)}"
        else:
            label = str(meter.key)
        lines.append(f"{label} = {meter.value}")
    for item in _held_items(reg, member_id):
        detail = _detail(item.name, item.note)
        lines.append(f"holds {detail}")
    return lines


def _one_line(text: str) -> str:
    """Collapse whitespace and bound the evidence line (§ proposal_lanes' rule)."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= MAX_ITEM_EVIDENCE_CHARS:
        return collapsed
    return collapsed[: MAX_ITEM_EVIDENCE_CHARS - 1].rstrip() + "…"


# -- the card (one assembly function, both doors, §5.5) ----------------------


def _session_title(log: "EventLog", session_id: str) -> str:
    """The chat's title from its own anchor rows (latest wins), or ``""``.

    Mirrors the fold :mod:`fabulist.api.sessions` derives its list row from:
    ``session_created``/``session_renamed`` carry ``title`` and a ``revision``'s
    ``revised.title`` supersedes it. Read from the CHAIN, like every other context
    read here, so a branch's card names the chat the story is actually in.
    """
    title = ""
    for event in _chain_events(log, session_id):
        payload = event.payload if isinstance(event.payload, dict) else {}
        kind = getattr(event, "type", None)
        if kind == "system":
            if payload.get("event") in _TITLE_EVENTS and isinstance(
                payload.get("title"), str
            ):
                title = payload["title"].strip()
        elif kind == "revision":
            revised = payload.get("revised")
            if isinstance(revised, dict) and isinstance(revised.get("title"), str):
                title = revised["title"].strip()
    return title


def _chain_seq_now(log: "EventLog", session_id: str) -> int:
    """The highest seq on the chain — "where the log stood" when the card was made.

    Recorded as ``provenance.at_seq``: the promotion happened at this point of the
    story, and a fork that carries the card can trace it back to the same place.
    """
    seq = 0
    for event in _chain_events(log, session_id):
        seq = max(seq, int(getattr(event, "seq", 0) or 0))
    return seq


def member_definition(
    log: "EventLog",
    session_id: str,
    member: CastMember,
    reg: entities.Registry,
) -> dict:
    """Assemble the persona card a promotion would mint (§5.5) — no model, no writes.

    The description is a bounded, deterministic block:

    * ``Also known as …`` when the member carries aliases (its surface forms);
    * one line per relationship edge involving the member (``Towards Marcus:
      mentor`` / ``From Marcus: mentor``), at most
      :data:`MAX_RELATIONSHIP_LINES`;
    * one line per held item (``Holds the brass key``), at most
      :data:`MAX_ITEM_LINES`;
    * the closing line ``Promoted from chat "<title>" at turn <n>.``.

    ``extensions.cast_aliases`` carries the surface forms machine-readably (a
    card's prose is for a reader; the alias list is for a re-import), and
    ``provenance`` is the linkage the dedupe (§5.1) and the cast pane's badge
    (§5.3) read: :data:`{"origin": "promotion", "member_id", "session_id",
    "at_seq"}`.

    ``name`` is returned as the member's ``display_name``: suffixing needs the live
    persona catalog, which is the DOOR's read — both callers overwrite this key
    with :func:`suffixed_name`'s answer (or the owner's typed name).
    """
    names = {row.member_id: row.display_name for row in roster(log, session_id)}
    lines: list[str] = []
    aliases = [str(alias).strip() for alias in member.aliases if str(alias).strip()]
    if aliases:
        lines.append(f"Also known as {', '.join(aliases)}.")
    edges = _involving_relationships(reg, member.member_id)
    for edge in edges[:MAX_RELATIONSHIP_LINES]:
        other = edge.target if edge.source == member.member_id else edge.source
        detail = _edge_detail(edge)
        head = (
            f"Towards {_surface(other, names)}"
            if edge.source == member.member_id
            else f"From {_surface(other, names)}"
        )
        lines.append(f"{head}: {detail}." if detail else f"{head}.")
    for item in _held_items(reg, member.member_id)[:MAX_ITEM_LINES]:
        detail = _detail(item.name, item.note)
        lines.append(f"Holds {detail}.")
    title = _session_title(log, session_id)
    turn = committed_turn_count(log, session_id)
    origin_line = (
        f'Promoted from chat "{title}" at turn {turn}.'
        if title
        else f"Promoted from an untitled chat at turn {turn}."
    )
    lines.append(origin_line)
    return {
        "name": member.display_name,
        "description": "\n".join(lines),
        # Authored fields stay EMPTY: the engine writes only what the session
        # actually established (§5.5), and a greeting is the owner's voice.
        "personality": "",
        "scenario": "",
        "first_mes": "",
        "book_ids": [],
        "alternate_greetings": [],
        "image_asset": None,
        "extensions": {"cast_aliases": aliases},
        "provenance": {
            "origin": PROMOTION_ORIGIN,
            "member_id": member.member_id,
            "session_id": session_id,
            "at_seq": _chain_seq_now(log, session_id),
        },
    }


# -- who may be promoted, and by which door ---------------------------------


def promoted_persona_ids(log: "EventLog") -> dict[str, str]:
    """``{member_id: persona_id}`` from the LIVE persona catalog (one fold).

    The reverse index of the promotion linkage: a persona whose
    ``provenance.member_id`` names a cast member is that member's card. Both the
    cadence's dedupe (§5.1) and the cast pane's ``promoted_persona_id`` badge
    (§5.3) read it — one fold, one answer. The FIRST card wins when a member was
    promoted twice through the manual door (duplicates are legal, req. 7).
    """
    found: dict[str, str] = {}
    for persona in list_personas(log):
        member_id = persona.provenance.get("member_id")
        if isinstance(member_id, str) and member_id:
            found.setdefault(member_id, persona.persona_id)
    return found


def promotion_eligibility(member: CastMember) -> tuple[bool, str]:
    """``(eligible, reason)`` for the MANUAL door (§5.3) — the owner's rule.

    Only the rung BELOW counts: the member must be an active cast member admitted
    from an owner-accepted cast proposal (``provenance == "actor"``). Card/world
    members are already backed by a studio asset and the player is the user, so
    they have nothing to promote. A PRIOR promotion never closes this door (req.
    7's never-refuse, D5/D6): a duplicate card is a deliberate owner choice, which
    is why the reason only mentions it.
    """
    if not member.is_active:
        return False, (
            f'"{member.display_name}" is retired from this chat, so it cannot be '
            "promoted"
        )
    if member.provenance != "actor":
        return False, (
            f'"{member.display_name}" was not admitted from an owner-accepted cast '
            f'proposal (provenance "{member.provenance}"), so it is not on the '
            "promotion rung"
        )
    return True, (
        f'"{member.display_name}" was admitted from an owner-accepted cast '
        "proposal and can be promoted to an actor persona card"
    )


@dataclass(frozen=True)
class _PriorPromotion:
    """One ``origin="promotion"`` proposal row about one member (the dedupe's unit).

    ``fact_count`` is the count recorded on the row (``None`` for a row that
    predates the key — impossible since this module is the only emitter, but the
    re-arm must not invent a threshold out of a missing value). ``decision`` is the
    row's latest terminal decision (``""`` while it is live).
    """

    proposal_id: str
    member_id: str
    fact_count: int | None
    decision: str


def _chain_events(log: "EventLog", session_id: str) -> list[Any]:
    """The session's fork-aware chain of events, in seq order."""
    get_chain = getattr(log, "get_chain_events", None)
    events = (
        get_chain(session_id) if callable(get_chain) else log.get_events(session_id)
    )
    return sorted(events, key=lambda event: int(getattr(event, "seq", 0) or 0))


def _prior_promotions(
    log: "EventLog", session_id: str
) -> dict[str, list[_PriorPromotion]]:
    """Every promotion row on the chain, grouped by member, in filing order.

    Live OR decided rows count (§5.1, review S2): the point of the dedupe is that
    one member is promoted once, not that it is promoted once per queue state.
    """
    rows: list[tuple[int, str, str, int | None]] = []
    decisions: dict[str, str] = {}
    for event in _chain_events(log, session_id):
        if getattr(event, "type", None) != "system":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        variant = payload.get("event")
        if variant == ASSET_PROPOSAL_EVENT:
            if payload.get("origin") != PROMOTION_ORIGIN:
                continue
            patch = payload.get("patch")
            provenance = patch.get("provenance") if isinstance(patch, dict) else None
            if not isinstance(provenance, dict):
                continue
            member_id = provenance.get("member_id")
            if not isinstance(member_id, str) or not member_id:
                continue
            recorded = provenance.get("fact_count")
            count = (
                recorded
                if isinstance(recorded, int) and not isinstance(recorded, bool)
                else None
            )
            rows.append(
                (
                    int(getattr(event, "id", 0) or 0),
                    str(getattr(event, "id", "")),
                    member_id,
                    count,
                )
            )
        elif variant == PROPOSAL_DECISION_EVENT:
            proposal_id = payload.get("proposal_id")
            decision = payload.get("decision")
            if isinstance(proposal_id, str) and isinstance(decision, str):
                decisions[proposal_id] = decision
    grouped: dict[str, list[_PriorPromotion]] = {}
    for _, proposal_id, member_id, count in sorted(rows):
        grouped.setdefault(member_id, []).append(
            _PriorPromotion(
                proposal_id=proposal_id,
                member_id=member_id,
                fact_count=count,
                decision=decisions.get(proposal_id, ""),
            )
        )
    return grouped


def _rearmed(prior: Sequence[_PriorPromotion], current_facts: int) -> bool:
    """May the cadence speak again about this member? (O2, the ONE re-arm)

    Exactly two ways back: no row at all, or a SINGLE prior row the owner rejected
    (or dismissed — the same non-accept terminal ruling, which wrote nothing) whose
    recorded fact count the member has since at least DOUBLED. Anything else —
    a live row awaiting the owner, an accepted row, a second row already filed, or
    a row whose recorded count is missing — stays silent: the manual door is
    always there for a change of heart.
    """
    if not prior:
        return True
    if len(prior) > 1:
        return False
    row = prior[0]
    if row.decision not in ("reject", "dismiss"):
        return False
    recorded = row.fact_count
    if not isinstance(recorded, int) or recorded < 1:
        return False
    return current_facts >= 2 * recorded


def candidates(log: "EventLog", session_id: str) -> list[CastMember]:
    """Roster members the cadence may still promote, in roster order (§5.1).

    The dedupe half of the rule: active, ``provenance == "actor"``, and no prior
    promotion by EITHER door (a filed row, live or decided — with the O2 re-arm —
    or a live persona carrying the member's ``provenance.member_id``). The fact
    BAR is :func:`promotion_pass`'s, not this read's, so a caller can inspect the
    rung without re-deriving the count.
    """
    members = roster(log, session_id)
    promoted = promoted_persona_ids(log)
    prior = _prior_promotions(log, session_id)
    reg = entities.registry(log, session_id)
    chosen: list[CastMember] = []
    for member in members:
        if not member.is_active or member.provenance != "actor":
            continue
        if member.member_id in promoted:
            continue
        count = fact_count(reg, member.member_id)
        if not _rearmed(prior.get(member.member_id, ()), count):
            continue
        chosen.append(member)
    return chosen


# -- the engine door: the historian cadence's deterministic pass (§5.2) ------


def _promotion_payload(definition: Mapping[str, Any], evidence: str) -> dict:
    """The pinned ``asset_proposal`` payload for one promotion row.

    ``kind="actor"``/``action="add"``/``target=null`` with the assembled card as
    the patch, ``origin="promotion"`` and ``signal=None``: the row was never model-
    judged (§5.2 — the pass is ungated by ``should_we_propose``, req. 10), and the
    accept path is the ordinary actor add.
    """
    return {
        "event": ASSET_PROPOSAL_EVENT,
        "kind": PROMOTION_KIND,
        "action": "add",
        "target": None,
        "patch": dict(definition),
        "origin": PROMOTION_ORIGIN,
        "signal": None,
        "evidence": _one_line(evidence),
    }


def promotion_pass(log: "EventLog", session_id: str) -> int:
    """File at most :data:`PROMOTION_MAX_PER_RUN` proposal(s); return how many.

    The engine trigger of req. 6a, run by the historian cadence (§5.2). Zero model
    calls: fold the roster + the entity registry, rank the
    :func:`candidates` by their distinct :func:`fact_count` (highest first, roster
    order as the tiebreak), drop anyone under :data:`PROMOTION_MIN_FACTS`, and file
    ONE row for the best of them.

    The row is append-ONLY and self-contained (review S3): this pass builds the
    payload, runs it through the schema gate and appends it here — nothing lands in
    :mod:`fabulist.state.proposals`, which owns the accept side. The name is
    suffixed at FILE time against the live persona catalog (§5.4), so the review
    row shows the exact name the accept will write.

    A row that fails the gate raises :class:`PromotionError`; the historian wraps
    this call and counts ``promotions: 0`` (§5.2 containment).
    """
    members = roster(log, session_id)
    if not members:
        return 0
    personas = list_personas(log)
    promoted = promoted_persona_ids(log)
    prior = _prior_promotions(log, session_id)
    reg = entities.registry(log, session_id)
    names = {member.member_id: member.display_name for member in members}
    taken = [persona.name for persona in personas]
    ranked: list[tuple[int, int, CastMember, int]] = []
    for index, member in enumerate(members):
        if not member.is_active or member.provenance != "actor":
            continue
        if member.member_id in promoted:
            continue
        count = fact_count(reg, member.member_id)
        if count < PROMOTION_MIN_FACTS:
            continue
        if not _rearmed(prior.get(member.member_id, ()), count):
            continue
        ranked.append((-count, index, member, count))
    ranked.sort(key=lambda row: (row[0], row[1]))
    filed = 0
    for _, _, member, count in ranked[:PROMOTION_MAX_PER_RUN]:
        definition = member_definition(log, session_id, member, reg)
        definition["name"] = suffixed_name(member.display_name, taken)
        taken.append(definition["name"])
        # The re-arm's recorded count (O2). It rides the row's patch because the
        # patch IS the card and provenance is already its linkage block; the
        # evidence line stays prose the owner reads.
        definition["provenance"]["fact_count"] = count
        facts = _fact_lines(reg, member.member_id, names)
        evidence = (
            f"cast member {member.display_name} has {count} tracked facts: "
            + "; ".join(facts)
        )
        payload = _promotion_payload(definition, evidence)
        errors = validate_event("system", payload)
        if errors:
            raise PromotionError(
                "the promotion proposal does not pass the schema gate: "
                + "; ".join(errors)
            )
        log.append_event(session_id, "system", PROMOTION_ACTOR, payload)
        filed += 1
    return filed


# -- the manual door (§5.3) -------------------------------------------------


def apply_promotion(
    log: "EventLog",
    session_id: str,
    member: CastMember,
    *,
    name: str | None = None,
) -> Persona:
    """Promote ``member`` through the MANUAL door; return the created persona.

    ``name`` OMITTED (or blank — a blank name is not a name) → the collision
    suffix of §5.4 against the live persona catalog. ``name`` GIVEN → written
    VERBATIM: never refused, never merged, never re-suffixed (req. 7 / D5 — the
    catalog has always allowed duplicate names, the dialog pre-fills a distinct
    suggestion, and an owner who types a duplicate means it).

    The write is :func:`fabulist.personas.registry.append_persona` — the studio's
    own path (AGENTS 7) — so no proposal row and no decision row is filed: a manual
    promotion is a studio-class owner write, and its reverse is the studio's
    persona delete (see the module docstring's undo asymmetry). The card carries no
    ``provenance.fact_count``: that key is the filed ROW's re-arm record, and this
    door files no row (nothing to re-arm).

    Raises :class:`PromotionError` when the member is not on the rung
    (:func:`promotion_eligibility`): the route maps it to a 400 with this text.
    """
    eligible, reason = promotion_eligibility(member)
    if not eligible:
        raise PromotionError(reason)
    reg = entities.registry(log, session_id)
    definition = member_definition(log, session_id, member, reg)
    typed = str(name or "").strip()
    if typed:
        definition["name"] = typed
    else:
        definition["name"] = suffixed_name(
            member.display_name, [persona.name for persona in list_personas(log)]
        )
    persona_id = append_persona(log, definition)
    created = next(
        (persona for persona in list_personas(log) if persona.persona_id == persona_id),
        None,
    )
    if created is None:  # pragma: no cover - append_persona just wrote it
        raise PromotionError(
            f"the persona anchor fold lost the definition for {persona_id!r}"
        )
    logger.info(
        "cast member %s promoted to persona %s (manual door)",
        member.member_id,
        persona_id,
    )
    return created
