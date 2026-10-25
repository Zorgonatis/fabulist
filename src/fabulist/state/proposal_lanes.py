"""The W9.22 §3 proposal lanes: the evidence gate, the judgments, the row shape.

Why this module exists
----------------------
The BTS decomposition (plan §3) turns the amendment stage into a parallel
``Send`` fan-out of three type-scoped lanes (world / actors / books). Doctrine 4
splits every lane into two halves that never mix, and this module owns both:

* **the JUDGMENTS** — the decision model (d1-3b) answers typed choice questions
  in one forward pass and generates no tokens, so it decides *whether* to
  propose and *whether each authored row survives*;
* **the SCOPE + SHAPE** — what the turn touched, what already exists (a digest
  with a bounded CURRENT-VALUE slice, never a full audit), and the exact row a
  lane's reply may become.

The lanes author OPERATIONS, never regenerated content (W9.23 §3.4)
-------------------------------------------------------------------
An ``amend`` row is a list of ``add``/``modify``/``delete`` operations over
addressable paths (``description``, ``extensions.mood``, ``entries.<entry_id>``),
and every ``modify``/``delete`` carries the value it replaces. The owner approved
a DELTA, so the lane must never be asked to rewrite what it is not changing — that
is the whole reason a whole-value ``patch`` on an amend is refused here. The lane
can only author ``old`` for what it was SHOWN, which is why a digest row carries a
``current`` slice (:func:`stored_digest`) and why :func:`parse_proposal_batch`
narrows it to the assets the turn actually touched (:func:`sliced_rows`): a
hallucinated ``old`` is born-stale — flagged at review and refused at accept
(:func:`fabulist.state.proposals.accept_proposal`), never destructive. A BOOK
row therefore matches on its entry KEYS as well as its name: a book's name is the
owner's label ("Vale & Co. Ledger") and need never echo its contents, and the
live defect was exactly that — the entries the turn touched went unsliced and the
lane authored their ``old`` from memory. Both sides of every comparison are also
stripped of a leading article ("the greenhouse" is "greenhouse").

The lanes themselves (the generator calls) are the extractor-class passes in
:mod:`fabulist.graph.nodes`; nothing here calls a content model, and nothing
here writes an asset (the review home, Wave 4, disposes — doctrine 3).

The double gate, defined precisely (plan §3, reviewer finding 9)
----------------------------------------------------------------
The stage sits on the turn's tail, so it must cost NOTHING on a quiet turn.
:func:`proposal_evidence` is the first half, and its definition is deliberately
narrow — "an entity the turn named" means exactly one of:

* a **state header** in this turn's ``world_state`` delta (``state`` keys), i.e.
  a character the tracker reported attributes for THIS turn;
* a **venue header** (``venue`` keys) or a **venue/area value** (a place name the
  tracker placed someone at);
* an **off-screen name** or its **location**.

plus the **roster diff**: members an ENGINE path admitted during this turn
(``admitted_at_seq`` after the turn's committed narrative) — a character the
engine itself admitted is an asset event even before BTS says anything about it.

A cast CANDIDATE is not evidence (L4 reconciliation)
----------------------------------------------------
The roster diff is only what an engine path actually admitted (card seeds, the
promotion gate's high-signal auto-admits). A newly met character is no longer
admitted automatically: it is filed as a ``kind="cast"``
:func:`fabulist.state.proposals.emit_cast_proposal` for the owner to gate, and an
UNACCEPTED proposal changes no roster and therefore unlocks nothing here. That is
why nothing in this module admits a member: the user-gated flow is the only
door for a candidate, and the three lanes author ASSET patches.

Deliberately NOT evidence: plot ``threads`` (a thread is narrative state, not an
asset entity, and every eventful turn has one — counting it would defeat the
zero-cost gate) and the flat ``deltas`` env keys (weather/tone are not names).
When the set is empty the dispatcher emits no ``Send`` and asks the decision
model NOTHING; when the set is non-empty, one cheap should-we-propose question
decides whether the three lanes run at all (``gate_questions``). Both halves must
say yes — that is the whole cost bound on this stage.

W9.29 §9.1 WIDENS that narrowness for REGISTRY objects only (``Evidence.objects``)
-------------------------------------------------------------------------------
The ``world_state`` payload stops being written (W9.29 §6 retired the payload, and
W4 retires the compat render), so on a registry-era session the state/venue
headers above are EMPTY. :func:`proposal_evidence` therefore walks the ``entity_*``
events between the window's seq bounds itself and adds two things:

* **the object lines** (``Evidence.objects``): locations established, threads
  established/updated, relationships established, meters whose value moved — one
  bounded line each (``MAX_TOUCHED_ENTITIES`` rows, one digest line per row);
* **their SURFACES** — the being names and place names those events carry, which
  join ``names``/``places`` so a lane's CURRENT-VALUE slice still matches the asset
  the turn is about (without them a registry-era session would send every lane a
  digest it can never slice, and the lane would author ``old`` from memory).

Why the D77 "threads and the flat deltas are excluded" narrowness is deliberately
lifted here for objects: with identity-stable objects, a thread that survives N
turns and a meter that keeps moving ARE signal (that is what req. 6 means by "the
MODEL decides what is important") — the lane digest shows the objects, and the
per-item judgment still gates every row. The flat ``deltas`` env keys stay out
(weather/tone are not asset entities and would fire on every turn). The walk
lives HERE, not in the historian's synthesized payload union (review N7): the
historian's state stays unchanged and still yields an empty payload for a
registry-era window, which is fine — registry facts no longer come from it.

The CONFIDENCE floor on that verdict (P2, owner ruling)
------------------------------------------------------
A d1-3b ``choice`` answer carries the model's own ``confidence`` (0 = every
option equally likely), and real windows often answer at 0.02–0.27 — the guessing
regime, where P(``propose``) is noise a threshold reads as signal. Below
:data:`GATE_CONFIDENCE_FLOOR` the verdict is UNUSABLE: no lane runs, at the same
zero generator cost as a "no" — but the run reports gate ``unsure`` rather than
``verdict_no``, because a window that was never really judged is not a window
judged quiet. :meth:`ProposalGate.should_propose` returns the whole
:class:`GateVerdict` for that reason (``proceed`` folds in both cuts). The MANUAL
door asks the same gate and obeys it, so an unsure manual run reports ``unsure``
too.

Per-item judgment (doctrine 4)
-----------------------------
Every row a lane authors is judged before it can become an event
(:meth:`ProposalGate.judge_item`): the state is the row (kind/action/target/patch),
its own evidence line and the turn; the rubric asks the two questions the plan
names — does this patch reflect what the story established, and would it corrupt
the owner's asset? A judged-out row is dropped SILENTLY (no event, no log row);
only the per-type counts reach the fan-in summary.

Gate configurability (req. 20 / doctrine 6) — the ONE question bank
------------------------------------------------------------------
Both questions are rows of the existing owner-editable decision-query asset
(:mod:`fabulist.decision_queries`: ``questions_for_section`` over
``__decision_queries__``; edited in the Studio's question-bank pane; per-row
``instructions``/``criteria``/``threshold``/``enabled``). They are SEEDED into
that bank (``DEFAULT_QUESTIONS``) carrying ``section: "proposal"``, which is what
routes them here instead of into the per-turn gate's batched ask. :func:`gate_questions`
reads that bank FIRST and falls back to :data:`CODE_DEFAULT_QUESTIONS` only for a
row the bank does not carry, so a fresh log behaves and an owner override always
wins. The consumed axes are:

* ``enabled`` — a disabled row turns the gate OFF: zero model calls, zero events
  (the same meaning the critic's rows have);
* ``threshold`` — the P(propose) a verdict must reach (the framework's
  ``DEFAULT_THRESHOLD`` when unset). This is the honest reading of a scalar cut
  point on a ``choice`` row: the answer's probability of the ``propose`` label;
* ``instructions`` + ``criteria`` — the wording and the rubric, i.e. the
  measurement. The rubric MUST keep the ``propose`` label: the probability read
  is keyed on it, so a row that renames it turns the gate off rather than
  guessing (fail-closed).

Known parity gaps (reported, not papered over): ``polarity`` is a DEFECT
vocabulary ("which answer is the defect") and has no meaning for a
should-we-do-it question, so it is not consumed; and the gate-level axes the
parity inventory's §5.2 names (per-data-type ``enabled``, ``run_condition``) do
not exist yet — they ride the bank's ``gates`` block, which is the shared
framework's (Wave 2/P4) work, and inventing a second config surface here is
exactly what req. 20 forbids. Until it lands, disabling either question turns the
whole stage off, and a quiet turn is the only per-type saving.

No lane ever writes an asset, and no judgment is fatal: every fan-out half in
:mod:`fabulist.graph.nodes` swallows its own failure into the summary row.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fabulist.context.books import ENTRY_ID_KEY
from fabulist.decision_queries import (
    DEFAULT_THRESHOLD,
    PROPOSAL_SECTION,
    SHOULD_WE_PROPOSE_THRESHOLD,
    question_payloads,
    questions_for_section,
)
from fabulist.log.schema_gate import (
    ASSET_PROPOSAL_EVENT,
    ASSET_PROPOSAL_FIELD_TYPES,
    ASSET_PROPOSAL_LIST_FIELDS,
    ASSET_PROPOSAL_NESTED_FIELDS,
    ASSET_PROPOSAL_PATCH_FIELDS,
    ENTRY_SHOWN_KEYS,
    list_item_path,
    validate_event,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CODE_DEFAULT_QUESTIONS",
    "Evidence",
    "GATE_CONFIDENCE_FLOOR",
    "GateVerdict",
    "LaneSpec",
    "MAX_DIGEST_ROWS",
    "MAX_DIGEST_SUMMARY_CHARS",
    "MAX_ITEM_EVIDENCE_CHARS",
    "MAX_ITEMS",
    "MAX_OPS_PER_ROW",
    "MAX_PARENT_NAME_CHARS",
    "MAX_SLICED_ASSETS",
    "MAX_SLICE_ENTRIES",
    "MAX_SLICE_ENTRY_CHARS",
    "MAX_SLICE_EXTENSION_CHARS",
    "MAX_SLICE_EXTENSION_KEYS",
    "MAX_SLICE_FIELD_CHARS",
    "MAX_TOUCHED_ENTITIES",
    "PROPOSAL_LANES",
    "PROPOSE_CRITERION",
    "ProposalGate",
    "ProposalQuestions",
    "SHOULD_THIS_PROPOSE_QUESTION_ID",
    "SHOULD_WE_PROPOSE_QUESTION_ID",
    "SKIP_CRITERION",
    "SUMMARY_EVENT",
    "gate_questions",
    "gate_state",
    "judgment_state",
    "lane_spec",
    "list_fields",
    "nested_fields",
    "parse_proposal_batch",
    "patch_fields",
    "permanence_targets",
    "proposal_evidence",
    "proposal_event_payload",
    "sliced_rows",
    "stored_digest",
    "summary_payload",
    "touched_for",
]


@dataclass(frozen=True)
class LaneSpec:
    """One proposal lane: its id, the asset kind it may touch, and its scope.

    ``lane`` is the fan-out's per-lane id (also the JSON key the lane reports its
    batch under and the ``asset_proposals`` summary's row label); ``kind`` is the
    SCHEMA's asset kind (:data:`fabulist.log.schema_gate.ASSET_PROPOSAL_KINDS`),
    fixed per lane so a model can never route a world patch into a persona;
    ``label`` is the human phrase the system brief uses; ``slices`` names which
    :class:`Evidence` fields the lane is shown.

    ``actions`` and ``reserved`` are the lane's fixed CONTRACT, not advice
    (L4): a row whose action the lane does not declare, or whose patch names a
    field the lane may never write, is DROPPED with its reason — the fixed kind
    already stops cross-kind routing, and these extend the same rule inside a
    kind. ``reserved`` is what an asset's identity/name costs: the world lane may
    never write the world's ``name``, because a venue or a shop being renamed
    into the world was one of the two live mis-targeting defects.

    ``scope`` is the brief's one-paragraph boundary (what belongs here, what does
    not). It is prompt text and lives here so a lane's routing rule and its
    wording cannot drift apart.
    """

    lane: str
    kind: str
    label: str
    slices: tuple[str, ...]
    actions: tuple[str, ...]
    scope: str
    reserved: frozenset[str] = frozenset()


#: The three lanes, in DISPATCH order. The order is load-bearing three times: it
#: is the order the fan-out sends, the order the fan-in reduces and appends the
#: events (never completion order — the W9.17 rule), and the order the summary
#: row lists its per-type outcomes. ``slices`` scopes the touched evidence: a
#: world amendment is about places and the people in them, an actor amendment
#: about named people, and a lore entry can be about either. ``objects`` (the
#: registry's one-line object rows, W9.29 §9.1) is the ONE slice every lane sees:
#: each line carries its own kind, so a lane scopes by reading it, and req. 6
#: says the MODEL decides which established fact matters enough to propose.
#:
#: The world lane is AMEND-ONLY (L4): a chat's world is a reusable definition the
#: owner made offline (AGENTS non-negotiable 7), so a turn never creates one —
#: and a turn that "creates a world" is always really describing a VENUE, which
#: belongs in a book (one entry per place) or in the actor lane (one persona per
#: person). That is the defect this lane contract kills, not a cost saving.
PROPOSAL_LANES: tuple[LaneSpec, ...] = (
    LaneSpec(
        "world",
        "world",
        "the world",
        ("places", "names", "objects"),
        ("amend",),
        "Amend the ONE campaign container this chat runs in (its description, "
        "scenario, cast or books). A venue, shop, street, room or region is NOT "
        "a world: a place is a lore entry in the books lane. A person is NOT a "
        "world: a person is an actor. Never rename the world, never add one. A "
        "durable fact about the setting ITSELF that the play established (a "
        "custom, a rule, a piece of history) belongs here as an amendment to "
        "`description` or `scenario`.",
        frozenset({"name"}),
    ),
    LaneSpec(
        "actors",
        "actor",
        "its people",
        ("names", "admitted", "objects"),
        ("add", "amend"),
        "Write or amend ONE person's reusable definition (name, description, "
        "personality, scenario, greeting, books, extensions). A person is a "
        "character, never a place and never the world. Do not propose someone "
        "the turn did not name. A person fact the play established belongs here "
        "— their `description`/`personality` amended, or a structured fact under "
        "`extensions.<key>` — and in a chat with NO world the char card IS the "
        "source, so every established fact (a place, a custom, an item) belongs "
        "here too, under `extensions.<key>` when it is structured.",
    ),
    LaneSpec(
        "books",
        "book",
        "the lore",
        ("names", "places", "objects"),
        ("add", "amend", "remove"),
        "Write lore as BOOK ENTRIES — the placeholder for a place (a venue, "
        "shop, street, region, building), an institution, a custom or a piece "
        "of history. Every entry is an OBJECT with `keys` (the words that "
        "should trigger it) and `content` (what it establishes), plus an "
        "optional `comment` naming it; `entries` is a LIST of those objects, "
        "never prose. A place is not a world and not a person. A place the play "
        "established (a room, a street, a shop) belongs here as an entry add or "
        "modify whose `keys` name it — `keys: [\"<the place>\"]` — with the "
        "established facts in `content`.",
    ),
)

_BY_LANE: dict[str, LaneSpec] = {spec.lane: spec for spec in PROPOSAL_LANES}
_BY_KIND: dict[str, LaneSpec] = {spec.kind: spec for spec in PROPOSAL_LANES}


def lane_spec(lane: object) -> LaneSpec | None:
    """The :class:`LaneSpec` for a lane (or kind) id, or ``None`` when unknown.

    Accepts the SCHEMA kind too (``actor`` for the ``actors`` lane) because the
    lane's own reply is allowed to echo its ``kind``; an unknown id means a stale
    ``Send`` payload and dispatches nothing.
    """
    key = str(lane or "").strip()
    return _BY_LANE.get(key) or _BY_KIND.get(key)


def patch_fields(kind: str) -> tuple[str, ...]:
    """The writable fields a patch may name for ``kind`` (the schema gate's list).

    Shared with the append door on purpose: a lane must never be taught a field
    that :func:`fabulist.log.schema_gate.validate_asset_proposal_payload` would
    refuse, so the prompt and the validator read ONE list. When the kind has a
    lane (:data:`PROPOSAL_LANES`), the lane's ``reserved`` fields are subtracted:
    the world lane is not taught ``name``, because a row renaming the world is
    refused by :func:`parse_proposal_batch` anyway and a brief that offers the
    field would only bait the model into it.
    """
    vocabulary = set(ASSET_PROPOSAL_PATCH_FIELDS.get(kind, frozenset()))
    spec = _BY_KIND.get(kind)
    if spec is not None:
        vocabulary -= spec.reserved
    return tuple(sorted(vocabulary))


def nested_fields(kind: str) -> tuple[str, ...]:
    """The writable mapping fields a patch may address NESTED for ``kind``.

    The prompt teaches these as ``<field>.<key>`` (L4 coverage widening): one
    custom persona extension is proposable without replacing every sibling key.
    They ride the SAME schema list the append door validates against.
    """
    return tuple(sorted(ASSET_PROPOSAL_NESTED_FIELDS.get(kind, frozenset())))


def list_fields(kind: str) -> tuple[str, ...]:
    """The writable LIST fields a lane may address ONE ITEM at a time.

    W9.23 §3.4.2/§3.4.3: a book's ``entries`` carry a stable key, so a lane can
    name one entry (``entries.<entry_id>``) instead of resending the whole list —
    which is what makes "delete this place" a diff rather than a regenerate. Every
    other list field changes whole-field as an explicit ``old -> new`` pair.
    """
    return tuple(sorted(ASSET_PROPOSAL_LIST_FIELDS.get(kind, frozenset())))


#: Caps that bound the lane's INSPECTION SCOPE (and therefore its prompt):
#: the digest read stops here rather than in the renderer, so what the prompt
#: shows and what a target may name are the same set (see
#: :func:`parse_proposal_batch`). 20 rows x ~150 chars is the ~750-token digest
#: :mod:`fabulist.prompts.proposals` budgets for.
MAX_DIGEST_ROWS = 20
MAX_DIGEST_SUMMARY_CHARS = 120
MAX_TOUCHED_ENTITIES = 20

#: Proposals a lane may file per turn, and the length of one row's evidence line.
#: 3 is the cost bound: 1 should-we-propose + 3 lanes x 3 items = 10 cheap
#: decision calls and 3 generator calls is the absolute worst case for the stage.
MAX_ITEMS = 3
MAX_ITEM_EVIDENCE_CHARS = 400

#: Operations one amend row may carry (W9.23 §3.4.1). Four is the readability
#: bound on a review row — beyond that the owner is reading a regenerate with
#: extra steps — and an over-cap row is DROPPED by the parser, never truncated:
#: a silently shortened op list would apply LESS than the owner approved.
MAX_OPS_PER_ROW = 4

#: The CURRENT-VALUE slice caps (W9.23 §3.4.4). ``old`` is authored by the lane
#: from what it was shown, so the slice is the fidelity budget: ~2k chars per
#: text field, 12 entries of 600 chars each, 8 extension keys of 200 — a bounded
#: worst case that buys an honest diff. Field/entry content beyond the cap is
#: shown clipped and MARKED truncated, and the prompt forbids modifying a
#: truncated field (the store would refuse the op anyway: its ``old`` could
#: never match).
MAX_SLICE_FIELD_CHARS = 2000
MAX_SLICE_ENTRY_CHARS = 600
MAX_SLICE_ENTRIES = 12
MAX_SLICE_EXTENSION_CHARS = 200
MAX_SLICE_EXTENSION_KEYS = 8

#: How many digest rows get a full slice per lane. The slice exists for the
#: asset a turn is actually about; four is the cost bound (a lane's worst case
#: stays a few thousand tokens) and the rest keep the one-line digest.
MAX_SLICED_ASSETS = 4

#: The longest a born-parented book NAME may be (W9.30 §3.2.1). A "name" past
#: this is a sentence, and a sentence is prose the books lane may not file as a
#: container's identity.
MAX_PARENT_NAME_CHARS = 120

#: What a made-up asset id looks like (``b_lore``, ``bk_9f3a1c``). A model that
#: answers a lookup miss with an id it invented must not CREATE a book whose name
#: is that id — the same class of guard the D89 cast-proposal door gained for
#: invented ``cm_kell`` handles.
_ID_SHAPED_TARGET = re.compile(r"^[a-z]{2,4}_[a-z0-9_]+$")

#: The fan-in summary row's ``event`` variant (one per proposing turn). Deliberately
#: NOT a schema-gated variant: it carries per-type OUTCOMES (counts, drops,
#: failures), and the typed shapes Wave 2 pinned are the review home's
#: ``asset_proposal``/``proposal_decision``/``proposal_undo`` rows.
SUMMARY_EVENT = "asset_proposals"


# -- the evidence (the double gate's first half) -----------------------------


@dataclass(frozen=True)
class Evidence:
    """What this turn TOUCHED — the only thing that may unlock the stage.

    Built by :func:`proposal_evidence` from this turn's ``world_state`` payload,
    the registry's ``entity_*`` events inside the window, and the roster diff.
    ``names`` are character surfaces, ``places`` are place surfaces (venue/area/
    location values), ``admitted`` are the members the cast admitted during this
    turn, and ``objects`` are the REGISTRY's own one-line object rows (§9.1):
    locations established, threads established/updated, relationships
    established, meters whose value moved. The slices exist because a lane is
    scoped: see :data:`PROPOSAL_LANES`.
    """

    names: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    admitted: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()

    @property
    def touched(self) -> bool:
        """True when the turn named anything an asset could be amended from."""
        return bool(self.names or self.places or self.admitted or self.objects)

    def count(self) -> int:
        """How many touched surfaces the gate saw (the summary's ``touched``)."""
        return (
            len(self.names)
            + len(self.places)
            + len(self.admitted)
            + len(self.objects)
        )


def _dedupe(values: Sequence[object], limit: int) -> tuple[str, ...]:
    """Stripped, de-duplicated surfaces in first-seen order, capped at ``limit``."""
    seen: list[str] = []
    for value in values:
        name = str(value or "").strip()
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= limit:
            break
    return tuple(seen)


def _mapping_keys(value: object) -> list[object]:
    """The keys of a payload mapping, or ``[]`` (tolerant: BTS payloads drift)."""
    return list(value) if isinstance(value, Mapping) else []


def _venue_places(value: object) -> list[object]:
    """The ``venue``/``area`` VALUES of a ``venue`` mapping (place surfaces)."""
    places: list[object] = []
    if not isinstance(value, Mapping):
        return places
    for bucket in value.values():
        if not isinstance(bucket, Mapping):
            continue
        for key in ("venue", "area"):
            place = bucket.get(key)
            if isinstance(place, str) and place.strip():
                places.append(place)
    return places


def _off_screen(payload: Mapping) -> tuple[list[object], list[object]]:
    """``(names, locations)`` of an ``off_screen`` list (the tracker's own rows)."""
    names: list[object] = []
    places: list[object] = []
    rows = payload.get("off_screen")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return names, places
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        names.append(row.get("name"))
        places.append(row.get("location"))
    return names, places


def proposal_evidence(log: Any, session_id: str, state: Mapping) -> Evidence:
    """What this turn touched: the ``world_state`` delta, the registry, the roster diff.

    The module docstring defines "touched" exactly; this function is that
    definition and nothing more:

    * the turn's OWN ``world_state`` payload (``state`` carries it, never a log
      read: the ``bts`` node put it in ``world_state_event``) — state headers,
      venue headers, venue/area values, off-screen names and locations;
    * the REGISTRY's ``entity_*`` events between the window's seq bounds
      (:func:`_registry_evidence`, W9.29 §9.1) — the object rows a lane is shown,
      plus the being/place surfaces they carry. On a registry-era session this is
      the ONLY source of both, because the payload above is no longer written;
    * the roster diff — members admitted after the turn's committed narrative
      (``admitted_at_seq``), i.e. identities this turn introduced.

    A failed BTS pass (no ``world_state_event``) therefore leaves the registry rows
    and the roster diff as evidence: the stage proposes nothing off a turn whose
    state the tracker could not read. Everything is capped
    (:data:`MAX_TOUCHED_ENTITIES`) and best-effort — an unreadable log costs that
    slice, not the turn.
    """
    event = state.get("world_state_event")
    payload = event.get("payload") if isinstance(event, Mapping) else None
    payload = payload if isinstance(payload, Mapping) else {}
    off_names, off_places = _off_screen(payload)
    objects, registry_names, registry_places = _registry_evidence(
        log, session_id, state
    )
    names = _dedupe(
        [
            *_mapping_keys(payload.get("state")),
            *_mapping_keys(payload.get("venue")),
            *off_names,
            *registry_names,
        ],
        MAX_TOUCHED_ENTITIES,
    )
    places = _dedupe(
        [*_venue_places(payload.get("venue")), *off_places, *registry_places],
        MAX_TOUCHED_ENTITIES,
    )
    return Evidence(
        names=names,
        places=places,
        admitted=_roster_diff(log, session_id, state),
        objects=objects,
    )


def _being_surface(who: object, label: Callable[[str], str]) -> str:
    """A being key as the NAME a lane can match, or ``""`` for a non-being.

    ``player`` and ``scene`` are reserved literals, not characters an asset may be
    amended from: a meter toward the player is evidence about its HOST, and a
    scene trait is about the world (the object line still names it).
    """
    from .entities import PLAYER, SCENE

    key = str(who or "").strip()
    if not key or key in (PLAYER, SCENE):
        return ""
    return label(key)


def _registry_evidence(
    log: Any, session_id: str, state: Mapping
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(objects, names, places)`` the registry recorded inside the window (§9.1).

    The walk lives HERE (review N7), not in the historian's synthesized payload
    union: the entity events carry their own per-turn ``seq``, so the function reads
    the log directly between the window's bounds — events AFTER the window's first
    narrative (``narrative_event.seq``). On the per-turn path that is the turn's own
    actions; on the historian's window it is every turn the run is judging.

    One line per OBJECT IDENTITY (a meter set three times inside a window is one
    line carrying its latest value, a thread patched twice is one line), in the
    order each identity first appeared; the row and surface caps are
    :data:`MAX_TOUCHED_ENTITIES`, exactly like every other evidence slice. Being
    keys render their display name through the roster so a lane can match them
    against its digest; an unreadable roster renders the key as-is (evidence is
    never fatal).

    A RETIREMENT is deliberately not evidence (§9.1 names establish/update/set/
    move): a tombstoned object is nothing to amend a card from, and its history
    stays in the log for the reviewer who wants it.
    """
    from .entities import (
        ENTITY_ESTABLISH_EVENT,
        ENTITY_MOVE_EVENT,
        ENTITY_SET_EVENT,
        ENTITY_UPDATE_EVENT,
    )

    narrative = state.get("narrative_event")
    start = narrative.get("seq") if isinstance(narrative, Mapping) else 0
    try:
        start = int(start or 0)
    except (TypeError, ValueError):
        start = 0
    try:
        events = log.get_chain_events(session_id)
    except Exception:  # noqa: BLE001 - the walk is evidence, never fatal
        logger.warning("proposal evidence could not read the chain", exc_info=True)
        return (), (), ()
    try:
        from .cast import member_by_id, roster

        members = roster(log, session_id)

        def label(who: str) -> str:
            member = member_by_id(members, who)
            return member.display_name if member is not None else who

    except Exception:  # noqa: BLE001 - names fall back to the keys
        logger.warning("proposal evidence could not read the roster", exc_info=True)

        def label(who: str) -> str:
            return who

    lines: dict[str, str] = {}
    names: list[str] = []
    places: list[str] = []

    def note(value: object, bucket: list[str]) -> None:
        text = str(value or "").strip()
        if text and text not in bucket:
            bucket.append(text)

    def line(object_id: str, text: str) -> None:
        if object_id in lines or len(lines) < MAX_TOUCHED_ENTITIES:
            lines[object_id] = _one_line(text)

    def being(who: object) -> None:
        note(_being_surface(who, label), names)

    for event in events:
        if getattr(event, "type", None) != "system" or getattr(event, "ephemeral", False):
            continue
        if int(getattr(event, "seq", 0) or 0) <= start:
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            continue
        variant = payload.get("event")
        if variant == ENTITY_ESTABLISH_EVENT:
            kind = str(payload.get("kind") or "")
            object_id = str(payload.get("id") or "")
            name = str(payload.get("name") or "").strip()
            if not object_id:
                continue
            if kind == "location":
                note(name, places)
                line(object_id, f'place "{name}" ({object_id}) established')
            elif kind == "thread":
                line(object_id, f'thread "{name}" ({object_id}) established')
            elif kind == "relationship":
                origin = label(str(payload.get("from") or ""))
                target = label(str(payload.get("to") or ""))
                being(payload.get("from"))
                being(payload.get("to"))
                line(
                    object_id,
                    f"relationship {origin}->{target} ({object_id}) established",
                )
            elif kind == "item":
                being(payload.get("holder"))
                line(object_id, f'item "{name}" ({object_id}) established')
            continue
        if variant == ENTITY_UPDATE_EVENT:
            object_id = str(payload.get("id") or "")
            patch = payload.get("patch")
            patch = dict(patch) if isinstance(patch, Mapping) else {}
            name = str(patch.get("name") or "").strip()
            status = str(patch.get("status") or "").strip()
            if name and object_id.startswith("loc_"):
                note(name, places)
            if object_id:
                described = [object_id]
                if name:
                    described.append(f'name "{name}"')
                if status:
                    described.append(status)
                line(object_id, " ".join(described) + " updated")
            continue
        if variant == ENTITY_SET_EVENT:
            object_id = str(payload.get("id") or "")
            host = str(payload.get("host") or "")
            subject = str(payload.get("subject") or "")
            key = str(payload.get("key") or "")
            value = payload.get("value")
            if not object_id or not key:
                continue
            being(host)
            being(subject)
            owner = label(host)
            toward = f"->{label(subject)}" if subject else ""
            line(object_id, f"{key} {owner}{toward} = {value} ({object_id})")
            continue
        if variant == ENTITY_MOVE_EVENT:
            being(payload.get("who"))

    return (
        tuple(lines.values())[:MAX_TOUCHED_ENTITIES],
        _dedupe(names, MAX_TOUCHED_ENTITIES),
        _dedupe(places, MAX_TOUCHED_ENTITIES),
    )


def _roster_diff(log: Any, session_id: str, state: Mapping) -> tuple[str, ...]:
    """Members an ENGINE path admitted DURING this turn (the cast rows after the
    turn's commit).

    The turn boundary is the committed narrative's ``seq``: every ``cast_admit``
    the engine wrote this turn sits after it, and the seed admits of an earlier
    turn sit before it. A newly met character is NOT here — it waits as a
    ``kind="cast"`` proposal the owner gates (L4) — so this is evidence of an
    ADMITTED member only. A log hiccup means "no diff": a quiet addition, never
    a failed turn.
    """
    from .cast import roster

    narrative = state.get("narrative_event")
    turn_seq = narrative.get("seq") if isinstance(narrative, Mapping) else 0
    try:
        turn_seq = int(turn_seq or 0)
    except (TypeError, ValueError):
        turn_seq = 0
    try:
        members = roster(log, session_id)
    except Exception:  # noqa: BLE001 - the roster is evidence, never fatal
        logger.warning("proposal evidence could not read the roster", exc_info=True)
        return ()
    return _dedupe(
        [member.display_name for member in members if member.admitted_at_seq > turn_seq],
        MAX_TOUCHED_ENTITIES,
    )


def permanence_targets(log: Any, session_id: str) -> dict[str, Any]:
    """Where this session's established facts belong (W9.29 §9.2, req. 7).

    Ownership is LAYER-OWNED, never per-turn: a chat materialized from a world has
    that world as its source, and a chat that is only cards has the char card as its
    source ("the char card IS the world"). The answer therefore comes from the
    chain, not from the turn:

    * a ``world_ref`` snapshot anywhere on the chain -> ``{"source": "world",
      "world_id": <id>, "book_ids": [...]}``, where ``book_ids`` is the SAME set the
      narrator's world-info render resolves
      (:func:`fabulist.context.facts._entity_book_ids`: the assigned persona's
      books, ``session_books`` and the world's own books) — the lane must name a
      book the render actually carries;
    * otherwise -> ``{"source": "card", "persona_ids": [...]}``: the chat's actors
      (D53, the char card) plus any persona whose name matches one of the session's
      cards (the D31 card-name fallback a multi-cast world chat relies on).

    Latest ``world_ref`` wins (a D32 re-attach appends a new snapshot), and every
    read is best-effort: an unreadable log reads as a card-only chat with no ids,
    never as a failure of the run that asked.
    """
    try:
        events = list(log.get_chain_events(session_id))
    except Exception:  # noqa: BLE001 - targeting is a read, never fatal
        logger.warning("permanence targets could not read the chain", exc_info=True)
        events = []

    world_id = ""
    for event in events:
        if getattr(event, "type", None) != "system" or getattr(event, "ephemeral", False):
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping) or payload.get("event") != "world_ref":
            continue
        value = payload.get("world_id")
        world_id = value if isinstance(value, str) and value else world_id

    if world_id:
        book_ids: list[str] = []
        try:
            from fabulist.context.facts import _entity_book_ids

            book_ids = [str(book) for book in _entity_book_ids(log, session_id, events)]
        except Exception:  # noqa: BLE001 - a world with no readable books is still a world
            logger.warning("permanence targets could not read the books", exc_info=True)
        return {"source": "world", "world_id": world_id, "book_ids": book_ids}

    persona_ids: list[str] = []

    def add(persona_id: object) -> None:
        value = str(persona_id or "").strip()
        if value and value not in persona_ids:
            persona_ids.append(value)

    try:
        from fabulist.memory.actor import fold_actors

        for persona_id in fold_actors(log, session_id):
            add(persona_id)
    except Exception:  # noqa: BLE001 - the card list is evidence, not the answer
        logger.warning("permanence targets could not read the actors", exc_info=True)
    card_names = {
        str(event.payload["card"].get("name") or "").strip()
        for event in events
        if getattr(event, "type", None) == "system"
        and isinstance(getattr(event, "payload", None), Mapping)
        and isinstance(event.payload.get("card"), Mapping)
    } - {""}
    if card_names:
        try:
            from fabulist.personas.registry import list_personas

            for persona in list_personas(log):
                if persona.name in card_names:
                    add(persona.persona_id)
        except Exception:  # noqa: BLE001 - same rule
            logger.warning("permanence targets could not read the personas", exc_info=True)
    return {"source": "card", "persona_ids": persona_ids}


def touched_for(spec: LaneSpec, evidence: Evidence) -> tuple[str, ...]:
    """The lane's slice of the touched evidence, in the spec's declared order."""
    values: list[object] = []
    for name in spec.slices:
        values.extend(getattr(evidence, name, ()) or ())
    return _dedupe(values, MAX_TOUCHED_ENTITIES)


# -- the digest (cheap: names + one line, never a full audit) ----------------


def _one_line(value: object, limit: int = MAX_DIGEST_SUMMARY_CHARS) -> str:
    """Collapse whitespace and clip to the digest summary bound."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _clip(value: object, limit: int) -> tuple[str, bool]:
    """Collapse + clip one shown value; ``(text, was_clipped)``."""
    text = " ".join(str(value or "").split())
    if limit > 0 and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…", True
    return text, False


def _clip_tail(value: object, limit: int) -> str:
    """Collapse + keep the TAIL of one shown value, marked ``…`` when clipped.

    The head-kept :func:`_clip` is the digest shape (a lane echoes back the
    values it was SHOWN). A history line keeps the turn's END instead: the beat
    a gate asks about ("the duke is dead") lands late in a turn, and D81's
    measured cut point was calibrated on tail-kept lines.
    """
    text = " ".join(str(value or "").split())
    if limit > 0 and len(text) > limit:
        return "…" + text[-(max(0, limit - 1)) :].lstrip()
    return text


def _json_text(value: object, limit: int) -> tuple[str, bool]:
    """One nested value as the JSON text a lane can echo back as ``old``."""
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - non-JSON value
        rendered = str(value)
    return _clip(rendered, limit)


def _slice_entries(entries: Sequence[object]) -> tuple[list[dict], int]:
    """The entry slice: id + the shown keys, per entry (W9.23 §3.4.4).

    ``id`` is ``None`` for an entry a pre-W9.23 write left unkeyed: the prompt
    teaches that such an entry can be neither modified nor deleted, and the
    parser enforces it, so the model is told the truth rather than a path it
    could never resolve. ``truncated`` marks an entry whose content was clipped.
    """
    shown: list[dict] = []
    items = [entry for entry in entries if isinstance(entry, Mapping)]
    for entry in items[:MAX_SLICE_ENTRIES]:
        entry_id = entry.get(ENTRY_ID_KEY)
        row: dict = {
            "id": entry_id if isinstance(entry_id, str) and entry_id else None
        }
        for key in ENTRY_SHOWN_KEYS:
            if key not in entry:
                continue
            if key == "content":
                text, clipped = _clip(entry[key], MAX_SLICE_ENTRY_CHARS)
                row[key] = text
                if clipped:
                    row["truncated"] = True
            else:
                row[key] = entry[key]
        shown.append(row)
    return shown, len(items)


def _current_slice(kind: str, definition: Mapping[str, Any]) -> dict:
    """The CURRENT values a lane may author ``old`` from (W9.23 §3.4.4).

    One block per digest row, built server-side from the folded definition:

    * every writable field of the kind that the definition HAS (absence is
      information: it is what tells the lane a path is an ``add`` target);
    * a mapping field is expanded one key deep (``extensions.mood``), which is
      exactly the addressing the gate accepts, so the path the lane copies is
      the path the store resolves;
    * a list field with stable keys (``entries``) becomes the entry slice plus
      its true total, so two entries added to a twelve-entry slice still read as
      "the book grew" rather than "the book is twelve entries";
    * text and entry content are clipped at their caps and EVERY clipped path is
      listed in ``truncated`` — a field the lane may not modify, because its
      ``old`` could never match what the store compares against.
    """
    fields: dict[str, Any] = {}
    truncated: list[str] = []
    entries: list[dict] | None = None
    total = 0
    listed = list_fields(kind)
    for name in patch_fields(kind):
        if name not in definition:
            continue
        value = definition[name]
        if name in ASSET_PROPOSAL_NESTED_FIELDS.get(kind, frozenset()) and isinstance(
            value, Mapping
        ):
            if name == "provenance":
                fields[name] = dict(value)
                continue
            for index, key in enumerate(sorted(value, key=str)):
                if index >= MAX_SLICE_EXTENSION_KEYS:
                    truncated.append(f"{name}.<more>")
                    break
                path = f"{name}.{key}"
                shown, clipped = _json_text(value[key], MAX_SLICE_EXTENSION_CHARS)
                fields[path] = shown
                if clipped:
                    truncated.append(path)
            continue
        if name in listed and isinstance(value, list):
            entries, total = _slice_entries(value)
            continue
        shape = ASSET_PROPOSAL_FIELD_TYPES.get(kind, {}).get(name)
        if shape is str:
            shown, clipped = _clip(value, MAX_SLICE_FIELD_CHARS)
            fields[name] = shown
            if clipped:
                truncated.append(name)
            continue
        fields[name] = value
    current: dict = {"fields": fields}
    if entries is not None:
        current["entries"] = entries
        current["entries_total"] = total
    if truncated:
        current["truncated"] = truncated
    return current


def _world_rows(log: Any) -> list[dict]:
    from fabulist.worlds.registry import list_worlds, world_to_wire

    return [
        {
            "id": world.world_id,
            "name": world.name,
            "summary": _one_line(world.description)
            or (f"{len(world.cast)} cast member(s)" if world.cast else "no description"),
            "current": _current_slice("world", world_to_wire(world)),
        }
        for world in list_worlds(log)
    ]


def _persona_definition(persona: Any) -> dict:
    """The persona FOLD shape the CURRENT slice is read from.

    Local on purpose (the same shape :mod:`fabulist.state.proposals` builds):
    the ``actor`` kind's writable fields are the persona's own, and the profile
    envelope's wire adds greeting keys this lane may no longer write (Q1) — the
    slice iterates the gate's field list, so extra keys cost nothing.
    """
    return {
        "persona_id": persona.persona_id,
        "name": persona.name,
        "description": persona.description,
        "personality": persona.personality,
        "scenario": persona.scenario,
        "book_ids": list(persona.book_ids),
        "extensions": dict(persona.extensions),
        "provenance": dict(persona.provenance),
        "image_asset": persona.image_asset,
    }


def _persona_rows(log: Any) -> list[dict]:
    from fabulist.personas.registry import list_personas

    return [
        {
            "id": persona.persona_id,
            "name": persona.name,
            "summary": _one_line(persona.description)
            or (_one_line(persona.personality) or "no description"),
            "current": _current_slice("actor", _persona_definition(persona)),
        }
        for persona in list_personas(log)
    ]


def _book_rows(log: Any) -> list[dict]:
    from fabulist.context.books import book_to_wire, list_books

    return [
        {
            "id": book.book_id,
            "name": book.name,
            "summary": f"{len(book.entries)} entr"
            + ("y" if len(book.entries) == 1 else "ies"),
            "current": _current_slice("book", book_to_wire(book)),
        }
        for book in list_books(log)
    ]


_DIGEST_READERS = {
    "world": _world_rows,
    "actor": _persona_rows,
    "book": _book_rows,
}


def stored_digest(log: Any, spec: LaneSpec) -> list[dict]:
    """``[{"id","name","summary","current"}]`` for the lane's stored assets.

    The lane's inspection AND write scope: one line per asset to pick a target
    from, plus the bounded ``current`` slice (:func:`_current_slice`) it authors
    ``old`` from, capped at :data:`MAX_DIGEST_ROWS`. It doubles as the write
    scope — :func:`parse_proposal_batch` resolves an ``amend``/``remove`` target
    against it, so a lane can only amend what it was shown. A catalog read
    failure yields ``[]`` (a lane with no targets can still ``add``), never a
    failed turn.
    """
    reader = _DIGEST_READERS.get(spec.kind)
    if reader is None:
        return []
    try:
        rows = reader(log)
    except Exception:  # noqa: BLE001 - the digest is context, never fatal
        logger.warning("proposal digest read failed for %s", spec.kind, exc_info=True)
        return []
    return [
        {
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or ""),
            "summary": _one_line(row.get("summary")),
            "current": row.get("current") or {"fields": {}},
        }
        for row in rows[:MAX_DIGEST_ROWS]
        if str(row.get("id") or "").strip()
    ]


#: Leading articles stripped from BOTH sides of every slice comparison (P1).
#: An entry key is authored as a bare noun (``greenhouse``) while the turn names
#: it the way prose does (``the greenhouse``); the article carries no identity, so
#: it is removed before the substring rule runs. Longest first, so ``an `` is
#: tried before ``a ``.
_LEADING_ARTICLES: tuple[str, ...] = ("the ", "an ", "a ")


def _strip_article(text: object) -> str:
    """Casefolded, trimmed text with ONE leading article removed."""
    folded = str(text or "").casefold().strip()
    for article in _LEADING_ARTICLES:
        if folded.startswith(article):
            return folded[len(article) :].strip()
    return folded


def _matches_touched(name: object, touched: Sequence[str]) -> bool:
    """Does ONE surface (a row's name or an entry key) name what the turn touched?

    The pinned match rule (W9.23 §3.4.4): casefold containment in EITHER
    direction — the turn says "the mill" and the row is "Miller's Bend" (row
    contains surface), the turn says "Miller's Bend courtyard" and the row is
    "Miller's Bend" (surface contains row) — with a leading article stripped from
    both sides first (:func:`_strip_article`). Selecting the wrong asset for a
    slice only costs prompt tokens: the sliced value is what the store compares
    the lane's ``old`` against, so an unsliced asset simply yields stale ops
    that are refused, never a bad write.
    """
    folded = _strip_article(name)
    if not folded:
        return False
    for value in touched:
        surface = _strip_article(value)
        if surface and (surface in folded or folded in surface):
            return True
    return False


def _book_entry_keys(row: Mapping) -> list[object]:
    """The ``keys`` of every entry a BOOK row's slice shows (books only, P1).

    A book's name is the owner's label for the collection ("Vale & Co. Ledger")
    and need never echo what is inside it, so name matching alone left a real
    book unsliced: the lane then never saw the entry it was amending and authored
    ``old`` from memory (born-stale). The slice is where the entry keys live, and
    :data:`MAX_SLICE_ENTRIES` is also the honest matching bound — an entry beyond
    the slice is an entry the lane was never shown and could not author a usable
    op against.
    """
    current = row.get("current")
    entries = current.get("entries") if isinstance(current, Mapping) else None
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return []
    keys: list[object] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        entry_keys = entry.get("keys")
        if isinstance(entry_keys, Sequence) and not isinstance(
            entry_keys, (str, bytes)
        ):
            keys.extend(entry_keys)
    return keys


def _digest_surfaces(targets: Sequence[Mapping]) -> list[str]:
    """Every stored surface a born-parented NAME could be a near-duplicate of.

    A digest row's own NAME, plus — for a book row that carries its ``current``
    slice — each entry's KEYS: a book's name is the owner's label for the
    collection and need never echo its contents, so a parented row naming a place
    an existing book already documents has to read as a near-match of THAT book
    (W9.30 §3.2.2). The dispatcher's per-lane ``Send`` payload carries only the
    digest's ``id``/``name`` today, so the entry half binds wherever the caller
    passes the full digest row.
    """
    surfaces: list[str] = []
    for row in targets:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            surfaces.append(name)
        for key in _book_entry_keys(row):
            text = str(key or "").strip()
            if text:
                surfaces.append(text)
    return surfaces


def _near_match(name: str, targets: Sequence[Mapping]) -> str | None:
    """The stored surface a born-parented ``name`` reads as, or ``None``.

    The same match rule the digest slicer uses (:func:`_matches_touched`: leading
    article stripped, casefold containment in either direction), because the
    article/containment pair ("The Vale Ledger" beside a stored "Vale Ledger") is
    the SAME conflation the slice rule already answers — one rule, so what the
    lane may not re-create and what it may amend cannot drift apart.
    """
    for surface in _digest_surfaces(targets):
        if _matches_touched(name, (surface,)):
            return surface
    return None


def sliced_rows(
    touched: Sequence[str], digest: Sequence[Mapping], kind: str
) -> list[dict]:
    """The digest rows that get a rendered ``current`` slice, in digest order.

    A row is selected when its NAME matches (:func:`_matches_touched`), and — for
    ``kind="book"`` only — also when any of its entry KEYS matches (:func:
    `_book_entry_keys`): a book whose name never echoes its contents is exactly
    the asset the books lane kept authoring stale ops against. The worlds and
    actors lanes keep the name rule alone; a world's or persona's name is the
    entity the turn names.

    Bounded by :data:`MAX_SLICED_ASSETS` so a turn that names ten people does not
    ship ten cards; everything else keeps the one-line digest. Deterministic: the
    digest order is the catalog order, so the same turn always slices the same
    rows.
    """
    sliced: list[dict] = []
    for row in digest:
        if len(sliced) >= MAX_SLICED_ASSETS:
            break
        surfaces: list[object] = [row.get("name")]
        if kind == "book":
            surfaces.extend(_book_entry_keys(row))
        if not any(_matches_touched(surface, touched) for surface in surfaces):
            continue
        current = row.get("current")
        if not isinstance(current, Mapping) or not current.get("fields"):
            continue
        sliced.append(dict(row))
    return sliced


# -- the question bank (req. 20: the ONE owner-editable surface) -------------

#: The two question ids. They are ordinary rows of the decision-query bank, so
#: they are edited (and enabled/disabled/re-thresholded) in the SAME Studio pane
#: the slop phrases are.
SHOULD_WE_PROPOSE_QUESTION_ID = "should_we_propose"
SHOULD_THIS_PROPOSE_QUESTION_ID = "should_this_propose"

#: The verdict labels. ``propose`` is the probability the gate reads (and the
#: threshold cuts); ``skip`` is the alternative. Both are the CROSS-LANE
#: vocabulary of a should-question on purpose — no defect wording, because
#: nothing here is a defect (parity gap G4: the bank's ``polarity`` axis is
#: defect-only and is therefore not consumed).
PROPOSE_CRITERION = "propose"
SKIP_CRITERION = "skip"

#: The should-we-propose question, RE-MEASURED for the historian's window (D81).
#: The W9.22 wording was written for a per-turn gate and then ran per window, where
#: one ``<turn>`` blob clipped at 800 chars hid all but the last turn of a real
#: window (~600+ chars per turn). This text plus the per-turn ``<history>`` block
#: (:func:`gate_state`) was calibrated live against d1-3b on 4 real windows and 3
#: synthetic anchors; the wording IS the measurement, so do not reword it —
#: tests/test_decision_queries.py pins it byte-for-byte.
SHOULD_WE_PROPOSE_INSTRUCTIONS = (
    "This history holds the roleplay turns since the last check. Judge the "
    "whole history as one unit. One question covers every turn in it: did any "
    "turn in this history establish or change a durable fact? A durable fact "
    "stays true after the history ends. Answer with exactly one word, propose "
    "or skip."
)

SHOULD_WE_PROPOSE_CRITERIA = {
    PROPOSE_CRITERION: (
        "At least one turn in this history establishes or changes a durable "
        "fact. Durable facts are: a named place, a person's identity, a "
        "person's situation, a relationship, a plan or goal, an injury, a "
        "death, a reveal about the past, or an event later turns will rely on. "
        "The history text shows the fact. One qualifying turn is enough."
    ),
    SKIP_CRITERION: (
        "Every turn in this history only continues the scene. The turns carry "
        "talk, movement, mood, or restatements of what is already known. No "
        "turn adds a new name, place, event, plan, relationship, or change in "
        "a person's situation. Nothing new stays true after the history ends."
    ),
}

SHOULD_THIS_PROPOSE_INSTRUCTIONS = (
    "An engine pass has authored ONE proposed amendment to a stored asset (a "
    "world, a character definition, or a book of lore). Decide whether the "
    "owner should be asked to accept it. Answer 'propose' only when the patch "
    "reflects what the story established and would leave the stored asset "
    "consistent; answer 'skip' otherwise."
)

SHOULD_THIS_PROPOSE_CRITERIA = {
    PROPOSE_CRITERION: (
        "The patch names only what the turn and the evidence establish, edits "
        "only the fields it is allowed to edit, and would leave the stored "
        "asset coherent (no contradiction, no invented history)."
    ),
    SKIP_CRITERION: (
        "The patch invents facts the turn does not establish, contradicts what "
        "the stored asset already says, or would corrupt the asset (wrong "
        "shape, duplicated text, an empty or nonsensical change)."
    ),
}


def _default_row(
    question_id: str,
    instructions: str,
    criteria: Mapping[str, str],
    threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """One code-default bank ROW (the shape ``validate_questions`` accepts).

    A row, not a bare payload, so it is byte-shaped like an owner row: if the
    owner later saves the bank, the pane's round trip and the gate read see the
    same fields (id/type/instructions/criteria/threshold/enabled/source). The
    ``section`` names this stage as the row's only consumer (W9.22 §4a): the
    per-turn gate filters the same field out.

    ``threshold`` defaults to the framework's :data:`DEFAULT_THRESHOLD`; the
    stage gate passes its own MEASURED cut point instead
    (:data:`SHOULD_WE_PROPOSE_THRESHOLD`), because a row that fell back to 0.5
    would ask a different question than the bank's seeded row does.
    """
    return {
        "id": question_id,
        "type": "choice",
        "instructions": instructions,
        "criteria": dict(criteria),
        "threshold": threshold,
        "enabled": True,
        "source": "custom",
        "section": PROPOSAL_SECTION,
    }


#: The code defaults for a fresh log — the behaviour of every session that never
#: touched the bank. NOT a second config surface: :func:`gate_questions` reads
#: the owner bank first and uses a row from here only when the bank has none.
CODE_DEFAULT_QUESTIONS: dict[str, dict] = {
    SHOULD_WE_PROPOSE_QUESTION_ID: _default_row(
        SHOULD_WE_PROPOSE_QUESTION_ID,
        SHOULD_WE_PROPOSE_INSTRUCTIONS,
        SHOULD_WE_PROPOSE_CRITERIA,
        SHOULD_WE_PROPOSE_THRESHOLD,
    ),
    SHOULD_THIS_PROPOSE_QUESTION_ID: _default_row(
        SHOULD_THIS_PROPOSE_QUESTION_ID,
        SHOULD_THIS_PROPOSE_INSTRUCTIONS,
        SHOULD_THIS_PROPOSE_CRITERIA,
    ),
}


@dataclass(frozen=True)
class ProposalQuestions:
    """The resolved questions + their P(propose) cut points, for one turn.

    ``gate``/``item`` are ``/v1/systemone`` question maps (``{id: payload}``),
    resolved ONCE per turn by :func:`gate_questions` so the fan-out's three lanes
    judge with exactly the wording the dispatcher gated on — an owner edit that
    lands mid-turn therefore cannot split one turn's rubric in two.
    """

    gate: dict[str, dict]
    gate_threshold: float
    item: dict[str, dict]
    item_threshold: float


def _resolved(
    log: Any, question_id: str
) -> tuple[dict[str, dict], float] | None:
    """One question as ``(payload map, threshold)``; ``None`` when it is off.

    Only rows in THIS stage's section are candidates
    (:func:`questions_for_section`), so a row another gate owns can never decide
    this one; the bank's row wins and :data:`CODE_DEFAULT_QUESTIONS` covers an id
    the bank does not carry at all (an older bank). ``None`` means "this gate must
    not run": a disabled row, a row of a type that cannot answer into a
    probability, or a rubric without the ``propose`` verdict — all fail CLOSED,
    because a gate that cannot read its own answer would otherwise spend generator
    calls on a judgment that never happened.
    """
    stored = {
        str(row.get("id")): row
        for row in questions_for_section(log, PROPOSAL_SECTION)
        if isinstance(row, Mapping)
    }
    row = stored.get(question_id) or CODE_DEFAULT_QUESTIONS[question_id]
    if not row.get("enabled", True):
        return None
    if row.get("type") != "choice":
        return None
    payloads = question_payloads([dict(row)])
    payload = payloads.get(question_id)
    if not payload:
        return None
    if PROPOSE_CRITERION not in (payload.get("criteria") or {}):
        return None
    try:
        threshold = float(row.get("threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return None
    return {question_id: payload}, threshold


def gate_questions(log: Any) -> ProposalQuestions | None:
    """Both gate questions, owner bank first, code defaults for a fresh log.

    ``None`` when either question is off (disabled, mis-typed, or missing the
    ``propose`` verdict): the stage then makes ZERO model calls and appends
    nothing, exactly like a turn that touched no entity.
    """
    gate = _resolved(log, SHOULD_WE_PROPOSE_QUESTION_ID)
    item = _resolved(log, SHOULD_THIS_PROPOSE_QUESTION_ID)
    if gate is None or item is None:
        return None
    return ProposalQuestions(
        gate=gate[0],
        gate_threshold=gate[1],
        item=item[0],
        item_threshold=item[1],
    )


def _propose_probability(answer: Any) -> float | None:
    """P(``propose``) of one choice answer, or ``None`` when it is unusable.

    Strict on shape, the same discipline :func:`fabulist.state.promotion.
    promotion_signal` uses: the route answers a ``choice`` question with
    ``{"choice", "probabilities"}``, and anything else is "no answer" rather than
    a coerced 0/1 — the threshold is a probability and a fabricated one would
    either spend calls or drop a real proposal.
    """
    if not isinstance(answer, Mapping):
        return None
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, Mapping):
        return None
    value = probabilities.get(PROPOSE_CRITERION)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _answer_confidence(answer: Any) -> float | None:
    """The ``confidence`` a ``choice`` answer carries, or ``None`` (P2).

    d1-3b answers a ``choice`` question with ``{"choice", "probabilities",
    "confidence"}`` (``0`` = every option equally likely; see
    ``docs/the maintainer's private notes (unshipped)``). Absent or ill-shaped ⇒ ``None``, which is NOT a
    zero: an answer that carries no confidence is not the guessing regime the
    floor targets, so the probability path stands as the whole verdict. A
    ``bool`` is not a number here — the same strictness the probability read has.
    """
    if not isinstance(answer, Mapping):
        return None
    value = answer.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


#: The confidence floor on the should-we-propose answer (P2, owner ruling). A
#: real window's answer often lands at 0.02–0.27 confidence — the model's
#: guessing regime, where P(``propose``) is noise that a threshold reads as
#: signal. Below this floor the verdict is UNUSABLE: no lane runs (no generator
#: cost), and the run reports gate ``unsure`` rather than ``verdict_no`` so the
#: owner can tell a window that was never really judged from one judged quiet.
#: This is a FLOOR, not a dial: it is deliberately not in the question bank.
GATE_CONFIDENCE_FLOOR = 0.25


@dataclass(frozen=True)
class GateVerdict:
    """What the should-we-propose answer was, as the gate read it (P2).

    ``probability`` is P(``propose``) (``None`` = no usable answer arrived);
    ``confidence`` is the choice answer's own confidence (``None`` when the answer
    carried none); ``threshold`` is the cut point the gate question carried.

    ``proceed`` is the ONE thing a caller must read to know whether a lane may
    run — the floor AND the threshold — and ``unsure`` is the distinct outcome the
    dispatcher reports as its own gate value: an answer below the floor is not a
    "no", it is an unanswered question, and reporting it as ``verdict_no`` would
    tell the owner the window was judged and found quiet.
    """

    probability: float | None = None
    confidence: float | None = None
    threshold: float = DEFAULT_THRESHOLD

    @property
    def unsure(self) -> bool:
        """True when the answer's confidence is below :data:`GATE_CONFIDENCE_FLOOR`."""
        return self.confidence is not None and self.confidence < GATE_CONFIDENCE_FLOOR

    @property
    def proceed(self) -> bool:
        """True only for a usable answer whose P(``propose``) reaches the threshold."""
        if self.unsure or self.probability is None:
            return False
        return self.probability >= self.threshold

    def __bool__(self) -> bool:
        """Bool context IS the verdict, so a bool-only caller stays correct.

        ``if not await gate.should_propose(...)`` was the whole contract before this
        result type existed; a frozen dataclass is otherwise always truthy, which
        would turn every verdict into a "yes".
        """
        return self.proceed


class ProposalGate:
    """The stage's two judgments over ONE d1-3b caller (doctrine 4).

    ``decision`` is the duck-typed seam :class:`fabulist.decision.DecisionCaller`
    satisfies (``await ask(state, questions)``, plus an ``enabled`` flag), so a
    test fake is a few lines and this module never imports the decision client.
    ``None`` (no binding wired) or a DISABLED caller makes every judgment
    ``None``: :meth:`should_propose` then answers "no", so an unwired decision
    model proposes NOTHING — the safe direction for a stage whose only effect is
    to file amendments (the opposite of the promotion gate, where silence would
    mute a character).
    """

    def __init__(self, decision: Any | None) -> None:
        self._decision = decision

    @property
    def enabled(self) -> bool:
        """True when a decision caller is wired AND usable (the gate CAN be asked).

        ``DecisionCaller`` reports its own ``enabled`` (no binding / no usable
        connection ⇒ every ask returns ``None``), so a config-less engine reads as
        "gate off" BEFORE a call is spent on an answer that cannot arrive. A
        duck-typed fake without the attribute counts as usable.
        """
        if self._decision is None:
            return False
        return bool(getattr(self._decision, "enabled", True))

    async def should_propose(
        self, questions: ProposalQuestions, state: str
    ) -> GateVerdict:
        """The stage-level verdict: may any lane run at all?

        ONE cheap choice question over the touched evidence and the turn. A
        missing/ill-shaped answer is no verdict at all (``proceed`` False: no
        lanes, no cost) — the plan's zero-cost rule, not an error — and an answer
        whose confidence falls below :data:`GATE_CONFIDENCE_FLOOR` is ``unsure``
        rather than a "no" (P2).
        """
        answer = await self._ask_answer(
            questions.gate, SHOULD_WE_PROPOSE_QUESTION_ID, state
        )
        return GateVerdict(
            probability=_propose_probability(answer),
            confidence=_answer_confidence(answer),
            threshold=questions.gate_threshold,
        )

    async def judge_item(
        self, question: Mapping[str, dict], state: str
    ) -> float | None:
        """The per-item verdict's P(propose); ``None`` means "no judgment".

        ``question`` is the item question map the dispatcher resolved (a Send
        payload field), so the lane judges with the wording its gate used.
        """
        answer = await self._ask_answer(
            question, SHOULD_THIS_PROPOSE_QUESTION_ID, state
        )
        return _propose_probability(answer)

    async def _ask_answer(
        self, question: Mapping[str, dict], question_id: str, state: str
    ) -> Any:
        """One ``ask`` + that question's raw answer; never raises, never fatal.

        The RAW answer is returned (not just its probability) so the gate can read
        the confidence the choice answer carries as well as P(``propose``).
        """
        if self._decision is None or not question:
            return None
        try:
            answers = await self._decision.ask(state, dict(question))
        except Exception:  # noqa: BLE001 - a judgment is advisory, never fatal
            logger.warning("proposal judgment failed", exc_info=True)
            return None
        return answers.get(question_id) if isinstance(answers, Mapping) else None


#: The ``<history>`` block's TOTAL prose budget, shared across the window's turns.
#: The gate is asked once per historian window (D81), so the budget must not grow
#: with the window: a real turn is ~600 chars, and the pre-D81 800-char single
#: ``<turn>`` clip showed the model roughly one turn of a ten-turn window.
GATE_HISTORY_CHARS = 1200

#: The FLOOR of one turn line's budget (``GATE_HISTORY_CHARS // <turns>`` shrinks
#: as the window grows): below this a line shows too little of a turn to hold the
#: durable fact the rubric asks about, so a long window overruns the total budget
#: rather than effacing its turns.
GATE_HISTORY_MIN_LINE_CHARS = 80


def _history_turns(prose: str | Sequence[str]) -> list[str]:
    """The window's turns as ONE string per turn.

    The seam is deliberately tolerant: the historian's state carries
    ``narrative_parts`` (one entry per turn), and a caller that joined them first
    hands over a single string — which is one turn, not a window that may be
    guessed apart (nothing in the join marks a boundary).
    """
    if isinstance(prose, str):
        return [prose]
    if isinstance(prose, Sequence):
        return [str(part or "") for part in prose]
    return [str(prose or "")]


def _history_block(prose: str | Sequence[str]) -> list[str]:
    """The ``<history>`` block: ``T<n>: <collapse+clip>`` per turn, in order.

    Every turn in the window gets a LINE — a dropped turn is a turn the gate was
    never asked about, which is exactly the miscalibration D81 fixed. The budget
    is therefore split across the turns (``GATE_HISTORY_CHARS // <turns>``, never
    under :data:`GATE_HISTORY_MIN_LINE_CHARS`), so a narrow window shows each turn
    in full while a wide one shows every turn briefly. A clipped line keeps its
    turn's TAIL (:func:`_clip_tail`): the revealing beat lands at the end.
    """
    turns = _history_turns(prose)
    if not turns:
        return []
    line_cap = max(GATE_HISTORY_MIN_LINE_CHARS, GATE_HISTORY_CHARS // len(turns))
    lines: list[str] = []
    for index, turn in enumerate(turns, start=1):
        text = _clip_tail(turn, line_cap)
        if text:
            lines.append(f"T{index}: {text}")
    if not lines:
        return []
    return ["<history>", *lines, "</history>"]


def gate_state(evidence: Evidence, prose: str | Sequence[str]) -> str:
    """The decision model's view for should-we-propose — ONCE PER WINDOW (D81).

    The touched evidence (the window's), then the window's turns as a
    ``<history>`` block of one line per turn. The prose is included because a
    durable fact is often established in a line that names nothing new ("the duke
    is dead") — judging a window by its entity list alone would answer "no" to
    exactly the amendments worth filing — and it is per TURN because the question
    covers the whole history: one clipped blob hid every turn but the last.

    ``prose`` is therefore one string per turn (``narrative_parts``); a single
    joined string is read as one turn (see :func:`_history_turns`).

    The registry's object lines ride as ``<tracked>`` (§9.1) — that is what makes
    a registry-era window legible to the gate at all, since the payload blocks
    above are empty there. One line per object, capped by the evidence itself.
    """
    lines: list[str] = []
    if evidence.names:
        lines.append("<named>" + ", ".join(evidence.names) + "</named>")
    if evidence.places:
        lines.append("<places>" + ", ".join(evidence.places) + "</places>")
    if evidence.admitted:
        lines.append(
            "<newly_admitted>" + ", ".join(evidence.admitted) + "</newly_admitted>"
        )
    if evidence.objects:
        lines.append("<tracked>" + " | ".join(evidence.objects) + "</tracked>")
    lines.extend(_history_block(prose))
    return "\n".join(lines)


#: The turn text the per-ITEM judgment sees. Small on purpose: the decision route
#: is one forward pass and the rubric is about the patch, not the prose. (The
#: stage gate's own budget is :data:`GATE_HISTORY_CHARS`, split over the turns.)
JUDGE_PROSE_CHARS = 600
MAX_PATCH_CHARS = 1200


def _ops_view(ops: Sequence[Mapping]) -> str:
    """The decision model's view of an op list: sorted paths, ``old -> new``.

    Sorted so the same row always renders the same state (and therefore the same
    probability); clipped at :data:`MAX_PATCH_CHARS` because the rubric is about
    what would change, not the whole value.
    """
    lines: list[str] = []
    for op in sorted(ops, key=lambda row: str(row.get("path") or "")):
        action = str(op.get("op") or "")
        path = str(op.get("path") or "")
        if action == "add":
            lines.append(f"add {path} -> {_json_text(op.get('new'), 400)[0]}")
        elif action == "delete":
            lines.append(f"delete {path} (was {_json_text(op.get('old'), 400)[0]})")
        else:
            lines.append(
                f"modify {path}: {_json_text(op.get('old'), 400)[0]} -> "
                f"{_json_text(op.get('new'), 400)[0]}"
            )
    return "\n".join(lines)[:MAX_PATCH_CHARS]


def judgment_state(row: Mapping, prose: str) -> str:
    """The decision model's view for ONE proposal row.

    The row ITSELF (kind/action/target, then what it would change) plus the
    lane's evidence line and the turn — exactly the material the two rubric
    questions need ("does this patch reflect what the story established?",
    "would it corrupt the owner's asset?"). An op row renders its OPERATIONS
    (sorted paths, ``old -> new``) rather than a patch blob, because a whole-value
    dict is no longer what the accept writes and a rubric shown the wrong shape
    would be judging a row that does not exist (W9.23 §3.4.4). Deterministic
    ordering so the same row always produces the same state and therefore the
    same probability.
    """
    patch = row.get("patch") if isinstance(row.get("patch"), Mapping) else {}
    ops = row.get("ops") if isinstance(row.get("ops"), Sequence) else ()
    if ops:
        body = f"<ops>{_ops_view([op for op in ops if isinstance(op, Mapping)])}</ops>"
    else:
        try:
            rendered = json.dumps(dict(patch), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):  # pragma: no cover - non-JSON patch is dropped earlier
            rendered = str(patch)
        body = f"<patch>{rendered[:MAX_PATCH_CHARS]}</patch>"
    lines = [
        f'<proposal kind="{row.get("kind") or ""}" '
        f'action="{row.get("action") or ""}" '
        f'target="{row.get("target") or ""}">',
        body,
        f"<evidence>{str(row.get('evidence') or '')[:JUDGE_PROSE_CHARS]}</evidence>",
    ]
    text = " ".join(str(prose or "").split())
    if text:
        lines.append(f"<turn>{text[:JUDGE_PROSE_CHARS]}</turn>")
    lines.append("</proposal>")
    return "\n".join(lines)


# -- the lane's reply: one batch -> judged-shape rows ------------------------

_ACTIONS = ("add", "amend", "remove")


def _target_index(targets: Sequence[Mapping]) -> dict[str, tuple[str, str]]:
    """``{surface: (asset id, readable name)}`` for the digest the lane was shown.

    Both spellings resolve: the exact id (what the brief asks for) and the
    asset's NAME (casefolded), because a model that answers ``target: "Miller's
    Bend"`` plainly meant the asset it was shown and dropping that row would
    throw away the turn's only amendment. Ids also match casefolded, and the
    readable NAME comes back with the id so the filed event can label its target
    instead of showing the owner a bare GUID. Anything else is an invented target
    and is refused (see :func:`parse_proposal_batch`).
    """
    index: dict[str, tuple[str, str]] = {}
    for row in targets:
        asset_id = str(row.get("id") or "").strip()
        if not asset_id:
            continue
        name = str(row.get("name") or "").strip()
        cell = (asset_id, name)
        index.setdefault(asset_id, cell)
        index.setdefault(asset_id.casefold(), cell)
        if name:
            index.setdefault(name.casefold(), cell)
    return index


#: Punctuation a model wraps an id in when it answers from a digest line
#: (``"w_mill"``, ``<w_mill>``, ``id: w_mill``). Stripped before resolution: a
#: quoted id names the very asset the lane was shown, and refusing it would drop
#: an otherwise-correct amendment over punctuation.
_TARGET_TRIM = " \t\r\n\"'`<>[](){}*_."


def _clean_target(value: object) -> str:
    """A row's ``target`` without the wrappers/quotes a model likes to add."""
    return str(value or "").strip().strip(_TARGET_TRIM)


def _resolve_target(
    value: object, index: Mapping[str, tuple[str, str]]
) -> tuple[str, str] | None:
    """The ``(id, name)`` a row's ``target`` names, or ``None`` when it names none."""
    text = _clean_target(value)
    if not text:
        return None
    return index.get(text) or index.get(text.casefold())


def _parented_target(
    value: object,
    spec: LaneSpec,
    targets: Sequence[Mapping],
    ops: Sequence[Mapping],
    action: str,
    position: int,
) -> tuple[str | None, dict | None]:
    """The NEW book NAME a lane row may create, or the reason it may not (W9.30 §3.2).

    A books-lane ``amend`` may name a book the digest does not hold: that is the
    D90 defect's answer, because a world with ``book_ids: []`` gives a place no
    entry target and the lane then had nothing to amend at all. The absence is
    expressed by the TARGET (W9.30 req. 3) — same event, same action, same ops
    grammar — so this function's whole job is to keep that door honest:

    * only ``kind="book"`` + ``action="amend"``: the world lane never adds a
      world (L4) and a person's card is never invented by a lookup miss, so an
      invented target stays REFUSED there (today's ``target`` drop);
    * a NAME, not an id: non-empty, at most :data:`MAX_PARENT_NAME_CHARS`, and
      not id-shaped (:data:`_ID_SHAPED_TARGET`) — a model answering a miss with
      a made-up handle must not mint a container named after it;
    * not a near-duplicate: a name that casefold-reads as an existing book's
      name or entry key (:func:`_matches_touched`) is ``parent_ambiguous``,
      because a model writing "The Vale Ledger" beside a stored "Vale Ledger"
      meant THAT book, and silently creating a near-twin is the conflation
      W9.29 refused everywhere. Non-destructive: the lane may spell it right
      next window (req. 11 — nothing is written or remembered);
    * adds only: ``modify``/``delete`` against a book that does not exist can
      never resolve, so every op must be an entry ``add`` (``parent_ops``).

    Returns ``(name, None)`` for a filable parented row, else ``(None, drop)``.
    """
    miss = {
        "reason": "target",
        "detail": f"row {position} names no asset from the digest",
    }
    if spec.kind != "book" or action != "amend":
        return None, miss
    name = _clean_target(value)
    if not name:
        return None, miss
    if len(name) > MAX_PARENT_NAME_CHARS:
        return None, {
            "reason": "parent_name",
            "detail": (
                f"row {position} names a target longer than "
                f"{MAX_PARENT_NAME_CHARS} characters — a book name, not prose"
            ),
        }
    if _ID_SHAPED_TARGET.match(name.casefold()):
        return None, {
            "reason": "parent_name",
            "detail": (
                f"row {position} targets {name!r}, which is shaped like an asset "
                "id — a new book is created by NAME"
            ),
        }
    near = _near_match(name, targets)
    if near is not None:
        return None, {
            "reason": "parent_ambiguous",
            "detail": (
                f"row {position} introduces {name!r}, which reads as the stored "
                f"{near!r} — amend that asset instead of creating a near-duplicate"
            ),
        }
    for index, op in enumerate(ops):
        item = list_item_path(spec.kind, str(op.get("path") or ""))
        if str(op.get("op") or "") != "add" or item is None or item[0] != "entries":
            return None, {
                "reason": "parent_ops",
                "detail": (
                    f"row {position} ops[{index}] touches a book that does not exist "
                    "— only entry adds can create it"
                ),
            }
    return name, None


def _kind_agrees(value: object, spec: LaneSpec) -> bool:
    """Does a row's optional ``kind`` agree with the lane it came from?

    ``kind`` is FIXED per lane (plan §3), so the event always carries the lane's
    kind; a row that states a different one is a mis-routed batch and is dropped
    rather than silently retyped.
    """
    if value is None or not str(value).strip():
        return True
    return str(value).strip() in (spec.kind, spec.lane)


def _path_drift(path: str, spec: LaneSpec) -> str:
    """Why one PATH is not this lane's to write (``""`` when it is).

    The same three rules the patch check always had (L4), applied to an op path
    (W9.23 §3.4.4), plus the list-item address:

    * a nested key under a mapping field the kind may address (``extensions.mood``)
      — fine;
    * a list ITEM (``entries.<entry_id>``) — fine, and only for a kind whose list
      field has stable keys, which is what keeps ``entries.*`` in the books lane;
    * a field the lane RESERVES (the world's ``name``) — reason ``reserved``;
    * anything else — the row speaks another data type ⇒ reason ``kind``.
    """
    text = str(path or "")
    root, separator, _ = text.partition(".")
    if separator and root in nested_fields(spec.kind):
        return ""
    if list_item_path(spec.kind, text) is not None:
        return ""
    if text in spec.reserved:
        return "reserved"
    return "" if text in patch_fields(spec.kind) else "kind"


def _drift(paths: Sequence[str], spec: LaneSpec) -> tuple[str, tuple[str, ...]]:
    """``(reason, keys)`` for a whole row's paths — ``kind`` before ``reserved``.

    A row that names another kind's field is a mis-routed row whatever else it
    names, so the more fundamental refusal wins the reason slot (the reviewer's
    reading of the live defects: an actor written as a world was never a rename
    problem).
    """
    foreign: list[str] = []
    reserved: list[str] = []
    for path in paths:
        reason = _path_drift(path, spec)
        if reason == "kind":
            foreign.append(path)
        elif reason == "reserved":
            reserved.append(path)
    if foreign:
        return "kind", tuple(sorted(foreign))
    return ("reserved", tuple(sorted(reserved))) if reserved else ("", ())


#: The path tails a model writes for an entry the slice showed as ``id: null``
#: (an entry a pre-W9.23 write left unkeyed). Such an op is DROPPED (reason
#: ``unkeyed``): the path could never resolve, and an ``old`` against nothing is
#: not a diff. The prompt teaches the same rule.
_UNKEYED_TAILS = frozenset({"", "null", "none", "nil", "unkeyed"})


def _entry_op(
    kind: str, op: Mapping, index: int
) -> tuple[dict | None, dict | None]:
    """One op normalized for a list field, or the reason it cannot be filed.

    W9.23 §3.4.3: the model never invents an entry key. An ``add`` names the list
    (``entries``) or a fresh tail and the parser MINTS the ``entry_id``, embedding
    it in the path AND the new entry — so the filed row carries the key the entry
    will actually have. A ``modify``/``delete`` on an entry the slice showed
    without an id is dropped: an unkeyed book is add-only until its first re-write.
    """
    fields = list_fields(kind)
    path = str(op.get("path") or "")
    action = str(op.get("op") or "")
    item = list_item_path(kind, path)
    if item is None and path not in fields:
        return dict(op), None
    tail = item[1] if item is not None else ""
    if action == "add":
        new = op.get("new")
        if not isinstance(new, Mapping):
            return None, {
                "reason": "ops",
                "detail": f"ops[{index}] adds an entry that is not an entry object",
            }
        entry_id = tail if tail.casefold() not in _UNKEYED_TAILS else uuid.uuid4().hex
        field = item[0] if item is not None else path
        keyed = {**dict(new), ENTRY_ID_KEY: entry_id}
        return {**dict(op), "path": f"{field}.{entry_id}", "new": keyed}, None
    if tail.casefold() in _UNKEYED_TAILS:
        return None, {
            "reason": "unkeyed",
            "detail": f"ops[{index}] names an entry the digest showed without an id",
        }
    return dict(op), None


def _row_ops(
    kind: str, ops: Sequence[Any]
) -> tuple[list[dict] | None, dict | None]:
    """A row's op list, normalized and capped; ``(ops, None)`` or ``(None, drop)``.

    Two refusals live here rather than in the gate: the CAP
    (:data:`MAX_OPS_PER_ROW` — a silently truncated op list would apply less than
    the owner approved, so an over-cap row is dropped whole, reason
    ``over_cap_ops``) and the entry-key handling (:func:`_entry_op`, which mints
    for adds and refuses unkeyed targets). Everything else — the per-verb key
    contract, unique paths, entry-object values — is the append door's own rule
    (:func:`fabulist.log.schema_gate.validate_asset_proposal_payload`), applied
    to every row below through ``validate_event``.
    """
    if len(ops) > MAX_OPS_PER_ROW:
        return None, {
            "reason": "over_cap_ops",
            "detail": f"more than {MAX_OPS_PER_ROW} operations in one row",
        }
    cleaned: list[dict] = []
    for index, raw in enumerate(ops):
        if not isinstance(raw, Mapping):
            return None, {"reason": "ops", "detail": f"ops[{index}] is not an object"}
        op, drop = _entry_op(kind, raw, index)
        if drop is not None:
            return None, drop
        assert op is not None  # _entry_op returns one or the other
        cleaned.append(op)
    return cleaned, None


def parse_proposal_batch(
    payload: Any, spec: LaneSpec, targets: Sequence[Mapping]
) -> tuple[list[dict], list[dict]]:
    """One lane reply -> ``(rows, dropped)``; ``rows`` are event-shaped.

    The reply is ``{"proposals": [ … ]}`` (``kind`` fixed by the lane), where a
    row is an ``amend`` carrying ``ops``, or an ``add``/``remove`` carrying a
    whole-value ``patch`` — the diff-first contract of W9.23 §3.4.4, never a
    regenerated value on an amend. Each row is: type/path-checked, action-checked,
    target-RESOLVED against the digest the lane was shown (an invented target
    names nothing, and a card that cannot be applied is not a proposal — with ONE
    exception, the born-parented books amend of W9.30 :func:`_parented_target`),
    evidence-required, then handed to the SAME validator the append door uses
    (:func:`fabulist.log.schema_gate.validate_event` on the pinned
    ``asset_proposal`` payload) with a ``None`` signal placeholder — the judgment
    fills the signal in later. A row that fails any of that is DROPPED with its
    reason (never an exception, never an event).

    Returns event-shaped rows (``event``/``kind``/``action``/``target``/``patch``/
    ``ops``/``origin``/``signal``) plus the lane's own ``evidence`` line, which the
    per-item judgment reads and the event does not carry (the pinned row shape has
    no evidence field).
    """
    if not isinstance(payload, Mapping):
        return [], [{"reason": "shape", "detail": "the reply was not a JSON object"}]
    raw = payload.get("proposals")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return [], [
            {"reason": "shape", "detail": "the reply carried no 'proposals' list"}
        ]
    index = _target_index(targets)
    rows: list[dict] = []
    dropped: list[dict] = []
    for position, item in enumerate(raw):
        if len(rows) >= MAX_ITEMS:
            dropped.append(
                {"reason": "over_cap", "detail": f"more than {MAX_ITEMS} proposals"}
            )
            break
        if not isinstance(item, Mapping):
            dropped.append({"reason": "shape", "detail": f"row {position} is not an object"})
            continue
        if not _kind_agrees(item.get("kind"), spec):
            dropped.append({"reason": "kind", "detail": f"row {position} kind drifted"})
            continue
        action = str(item.get("action") or "").strip().lower()
        if action not in _ACTIONS:
            dropped.append(
                {"reason": "action", "detail": f"row {position} action {action!r}"}
            )
            continue
        if action not in spec.actions:
            # The lane's fixed contract: a world is never ADDED from a turn (a
            # place the turn describes is a book entry), and the actor lane has
            # no remove (a person's definition is not deleted by play).
            dropped.append(
                {
                    "reason": "action",
                    "detail": f"row {position} action {action!r} is not the "
                    f"{spec.lane} lane's to take",
                }
            )
            continue
        patch: dict = {}
        ops: list[dict] | None = None
        if action == "amend":
            # W9.23 §3.4.4: an amend is an OPERATION LIST, never a regenerated
            # value. A whole-value patch here would be the very shape the owner
            # reversed, and the gate refuses a row carrying both.
            raw_ops = item.get("ops")
            if (
                not isinstance(raw_ops, Sequence)
                or isinstance(raw_ops, (str, bytes))
                or not raw_ops
            ):
                dropped.append(
                    {
                        "reason": "ops",
                        "detail": f"row {position} amends without operations",
                    }
                )
                continue
            ops, drop = _row_ops(spec.kind, raw_ops)
            if drop is not None:
                dropped.append({**drop, "detail": f"row {position} {drop['detail']}"})
                continue
            paths = [str(op.get("path") or "") for op in ops or ()]
        else:
            raw_patch = item.get("patch")
            if not isinstance(raw_patch, Mapping):
                dropped.append(
                    {"reason": "patch", "detail": f"row {position} has no patch"}
                )
                continue
            patch = dict(raw_patch)
            paths = list(patch)
        drift, keys = _drift(paths, spec)
        if drift:
            # A path outside this lane's vocabulary means the row speaks another
            # data type (an actor written as a world); a reserved path means it
            # wants the owner's own (a world rename). Either way: not this row.
            dropped.append(
                {
                    "reason": drift,
                    "detail": f"row {position} names {', '.join(keys)}",
                }
            )
            continue
        evidence = " ".join(str(item.get("evidence") or "").split())
        if not evidence:
            dropped.append(
                {"reason": "evidence", "detail": f"row {position} carries no evidence"}
            )
            continue
        target: str | None = None
        target_name: str | None = None
        target_new = False
        if action != "add":
            resolved = _resolve_target(item.get("target"), index)
            if resolved is None:
                # W9.30 §3.2: a books-lane amend whose target names no stored
                # asset may be BORN-PARENTED — the row creates the book it names
                # (see _parented_target for the guards and the accept path).
                parented, drop = _parented_target(
                    item.get("target"), spec, targets, ops or (), action, position
                )
                if drop is not None:
                    dropped.append(drop)
                    continue
                assert parented is not None  # one or the other
                target = parented
                target_name = parented
                target_new = True
            else:
                target, target_name = resolved
        event = {
            "event": ASSET_PROPOSAL_EVENT,
            "kind": spec.kind,
            "action": action,
            "target": target,
            "patch": patch,
            "origin": "bts",
            # The judgment's probability lands here after the item is judged; the
            # validator must see the key (it is required, and nullable).
            "signal": None,
        }
        if ops is not None:
            event["ops"] = ops
        if target_name:
            event["target_name"] = target_name
        if target_new:
            # The ONE additive marker a born-parented row carries (§3.1): set by
            # the parser, never by the model, and legal only for a book amend.
            event["target_new"] = True
        errors = validate_event("system", event)
        if errors:
            dropped.append({"reason": "schema", "detail": "; ".join(errors)})
            continue
        rows.append({**event, "evidence": evidence[:MAX_ITEM_EVIDENCE_CHARS]})
    return rows, dropped


def proposal_event_payload(row: Mapping) -> dict:
    """The pinned ``asset_proposal`` payload for one judged row.

    The event shape is Wave 2's (``schema_gate.ASSET_PROPOSAL_SCHEMA``) plus the
    lane's ``evidence`` line, bounded by :data:`MAX_ITEM_EVIDENCE_CHARS`. The
    line is recorded, not stripped: the review home's whole job is the owner's
    judgment ("does this patch reflect what the story established?"), and
    without the justification the surface can only show what would change, not
    why. A row that never carried evidence still writes the key (empty string).

    An op row carries its ``ops`` list EXACTLY as parsed (paths already resolve,
    entry keys already minted): the filed row is what the store compares against
    the fold at accept, so the parser's normalization has to be what lands.
    """
    evidence = _one_line(row.get("evidence"), MAX_ITEM_EVIDENCE_CHARS)
    payload = {
        "event": ASSET_PROPOSAL_EVENT,
        "kind": str(row.get("kind") or ""),
        "action": str(row.get("action") or ""),
        "target": row.get("target"),
        "patch": dict(row.get("patch") or {}),
        "origin": "bts",
        "signal": row.get("signal"),
        "evidence": evidence,
    }
    ops = row.get("ops")
    if isinstance(ops, Sequence) and not isinstance(ops, (str, bytes)) and ops:
        payload["ops"] = [dict(op) for op in ops if isinstance(op, Mapping)]
    name = row.get("target_name")
    if isinstance(name, str) and name.strip():
        payload["target_name"] = name.strip()
    if row.get("target_new"):
        # W9.30 §3.1: the born-parented marker reaches the log exactly as parsed
        # (the accept path reads it to decide whether to materialize the target).
        payload["target_new"] = True
    return payload


def summary_payload(meta: Mapping, rows: Mapping) -> dict:
    """The ONE ``asset_proposals`` row the fan-in appends — per-type outcomes.

    The stage's tail is otherwise invisible: kept rows land as ``asset_proposal``
    events and a judged-out or failed row lands nowhere (dropping silently is the
    contract, and a failed lane must never fail the turn). This row is therefore
    the only place the turn records what the stage DID — one entry per dispatched
    lane, in DISPATCH order, with its kept/dropped counts and any failure — so
    the inspector can tell "proposed nothing" from "never ran".
    """
    dropped = [row for row in (meta.get("dropped") or []) if isinstance(row, Mapping)]
    failed = [row for row in (meta.get("failed") or []) if isinstance(row, Mapping)]
    outcomes: list[dict] = []
    for spec in PROPOSAL_LANES:
        if spec.lane not in (meta.get("lanes") or []):
            continue
        outcomes.append(
            {
                "lane": spec.lane,
                "kind": spec.kind,
                "kept": len((rows.get(spec.lane) or [])),
                "dropped": len(
                    [row for row in dropped if str(row.get("lane")) == spec.lane]
                ),
                "failed": [
                    str(row.get("reason") or "")[:200]
                    for row in failed
                    if str(row.get("lane")) == spec.lane
                ],
            }
        )
    return {
        "event": SUMMARY_EVENT,
        "gate": str(meta.get("gate") or ""),
        "touched": int(meta.get("touched") or 0),
        "lanes": outcomes,
        "kept": sum(row["kept"] for row in outcomes),
        "dropped": [
            {"lane": str(row.get("lane") or ""), "reason": str(row.get("reason") or "")}
            for row in dropped
        ],
    }
