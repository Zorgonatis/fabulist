"""Presence projection (W9.13-A): the ONE read of "who is in the scene" today.

Why this module exists
----------------------
Presence was never a first-class read. :mod:`fabulist.state.cast` owns identity
and states explicitly that presence is an external, read-side boundary; the two
world-state reads in the tree disagree in opposite directions:

* :func:`fabulist.state.bts.latest_world_state` returns the **newest** payload
  only — BTS is delta-first by contract, so a quiet turn reports only what
  changed and the newest payload **under-reports** who is present;
* ``api/state_api._fold_world_state`` merges deltas into the fold but never
  removes a ``venue`` entry, so a character who left keeps their venue forever
  and the fold **over-reports**.

Neither is a presence answer. This module is the third read and the only one:
a **seq-ordered presence walk** over the fork-aware chain.

The rule (documented, deterministic)
------------------------------------
Walk every ``system``/``world_state`` event in seq order:

* a name under ``venue`` **or** ``state`` was present at that event's seq (a
  changed header is a present character);
* a row in ``off_screen`` was **absent** at that event's seq — the only removal
  signal the BTS contract produces;
* a ``kind: "checkpoint"`` payload **replaces** the accumulated maps, so a
  stale venue carried by an earlier delta cannot resurrect after a checkpoint.

A name is present iff its last presence evidence is **newer** than its last
absence evidence (``last_present > last_absent``) and that evidence is fresh
(fewer than ``presence_window`` narratives exist after it). Every present name
then resolves through :func:`fabulist.state.cast.member_by_name`, which returns
``None`` on ambiguity instead of merging — so an ambiguous name contributes no
lane. Members that are provisional (a descriptor such as "the woman" is a role
word, not a person) and the session persona (the player) are subtracted.

Nothing here writes to the log and nothing here imports the API or graph layers
(the same hygiene :mod:`fabulist.state.bts` keeps): it is a pure projection,
like :func:`fabulist.state.cast.roster`.

The lane ladder: presence -> attribution -> seed floor
-----------------------------------------------------
:func:`active_members` answers "who gets a lane this turn" with three rungs, in
order:

1. **presence** — the REGISTRY answer first, the walk above beneath it,
   stamped ``source = "presence"`` (see the next section);
2. **attribution** — with no present names, the ``system``/``attribution``
   rows (speaker -> member) of the last ``presence_window`` narrative turns,
   stamped ``source = "attribution"``. It is a heuristic and says so. Never
   prose substring matching — a name mentioned in narration is not a present
   character;
3. **seed floor** — a brand-new chat which has its cards/world actors on the
   roster but no evidence yet, stamped ``source = "seed"`` (below).

Registry-first presence (W9.29 §8, req. 13)
------------------------------------------
The BTS tracker now assigns beings to *identity-stable* locations
(:mod:`fabulist.state.entities`: ``assignment[who] = loc_id | "off_screen"``),
so presence has an exact answer and it outranks the delta walk:

* for a member the registry has assigned, **present iff
  ``assignment[member_id] == assignment[player]``** — object equality, no
  freshness window (an assignment is exact where a ``world_state`` delta is
  only "what changed"). Any other location is the deterministic presence EXIT,
  and ``off_screen`` is just one more other location (the absence bucket);
* the registry answers only when it holds an assignment for the PLAYER too —
  without a player room there is nothing to compare against, and the member
  falls through to the walk;
* a member WITHOUT a registry assignment keeps the walk (:func:`_member_present`),
  unchanged — pre-regime turns and attribution-rung members have no rows, and
  this is a forward-only migration (req. 15), not a backfill.

The checkpoint-replaces rule is the walk's alone: the registry fold has no
checkpoints. The walk still runs (the venue texts, the seed floor's evidence
gate, the attribution rung's freshness clock all read it), it just does not
decide a being the registry has placed.

The seed floor and its evidence gate
------------------------------------
Both evidence rungs are BTS-shaped: BTS writes ``world_state`` POST-turn and the
attribution pass also runs post-turn, so TURN 1 of a fresh chat carries
neither — and a live world chat that had both attached actors admitted to the
roster planned ZERO lanes on turn 1 even though the seed roster WAS the scene.
So when both rungs yield no member, the floor returns every eligible member
whose provenance is a seed provenance (:data:`SEED_PROVENANCE`: ``card`` /
``world``).

The gate is "does ANY evidence EXIST in the chain", not "did evidence yield
members": the floor applies only while the chain holds NO
``system``/``world_state`` and NO ``system``/``attribution`` event at all. Once
either kind exists — even stale, even naming only the player — BTS ran and
reported the scene, so the empty lane set IS the answer and the floor must not
resurrect it. That keeps the floor a turn-1/fresh-session rule rather than a
second, shadowing presence rule.

**Tiers** (W9.22 §2, tightened by the owner's user-gated cast ruling)
------------------------------------------------------------------------
Who may own a lane at all is a TIER question, decided by provenance
(:func:`fabulist.state.cast.member_tier`):

* **actor tier** (:data:`ACTOR_PROVENANCE` — ``card`` / ``world`` / ``actor`` /
  ``user``) lanes. These are the owner's attached cast, the cast member she
  ACCEPTED from a proposal, plus the player when User-as-Actor is ON;
* **presence tier** (everything outside :data:`ACTOR_PROVENANCE` — in a real log
  the ``bts`` / ``extractor`` identities learned in play) never lanes. D76's
  transitional bridge ("lane until the promotion gate judges you") is GONE with
  the gate: cast admission is user-gated, so an unaccepted presence is tracked
  and visible but is never role-played. A member only lanes after the owner
  accepts it, which re-tiers it (:func:`fabulist.state.cast.admit_from_proposal`).

The three rungs below run over the TIER-ELIGIBLE set, so a presence is
invisible to presence, attribution and the seed floor alike.

The dispatch policy (W9.24 §3.5)
--------------------------------
The ladder answers WHO IS ELIGIBLE; it does not answer who gets a lane THIS
turn. That second question is a per-session setting — ``strategy``
(``decision`` | ``random`` | ``lru``) plus a SET bound ``max_dispatch`` — folded
latest-wins from ``system``/``chat_npc_dispatch`` rows on the chat session
(:func:`npc_dispatch_policy`) and applied by :func:`select_dispatch_members`
over the ladder's answer. It is a SETTING, never an asset rewrite: eligibility
stays a tier fact, and no strategy can lane a presence-tier member.

What the decision model is SHOWN, and the direct-address floor
--------------------------------------------------------------
Two owner-ruled changes (2026-10-21) came out of one live defect: a session
where the ``decision`` strategy declined, every turn, a character
the player had just ADDRESSED BY NAME. A probe matrix over the real d1-3b
(three stable runs each) found the cause in what the ask put IN FRONT of the
model, not in the model:

* the candidate DESCRIPTIONS were ``last_intents`` — each character's own last
  lane intent — so a timid intent ("I want to disappear from the conversation")
  permanently argued against her dispatch (0.085 against a 0.25 threshold). The
  turn after her FIRST lane she had no intent yet and the old default ("X is in
  the scene.") cleared; after any self-effacing intent she could never clear
  again. That is the RATCHET, and :func:`_dispatch_labels` now describes
  presence/venue facts instead (same candidate, same probe: 0.455);
* adding the player's message to the state line did NOT help (0.070) while the
  descriptions still carried the intents — but it is the one line that makes
  "who did the player just speak to" answerable, so :func:`_dispatch_state`
  carries it once the labels are facts;
* the tail caps were not the binding constraint on the choice (context quality,
  not the verdict) — raised anyway, see :data:`DISPATCH_STATE_CHARS`.

The FLOOR that backs this up: a tier-eligible candidate whose display name or
any alias appears in the turn's player message is dispatched whatever the
model answered, for EVERY strategy (:func:`direct_address_members`). It is
applied AFTER the strategy narrows, as a union — it never widens eligibility
(a presence-tier member named in the message still does not lane), and it is
not re-bounded by ``max_dispatch``.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .cast import (
    TIER_ACTOR,
    CastMember,
    member_by_id,
    member_by_name,
    member_tier,
    roster,
)

__all__ = [
    "ACTOR_PROVENANCE",
    "DEFAULT_MAX_LANES",
    "DEFAULT_NPC_MAX_DISPATCH",
    "DEFAULT_NPC_STRATEGY",
    "NPC_DISPATCH_EVENT",
    "NPC_LANE_EVENT",
    "NPC_LANES_EVENT",
    "NPC_STRATEGIES",
    "PRESENCE_WINDOW",
    "SEED_PROVENANCE",
    "LaneConfig",
    "NpcDispatchPolicy",
    "PresenceWalk",
    "active_members",
    "direct_address_members",
    "lane_history",
    "last_intents",
    "member_categories",
    "npc_dispatch_policy",
    "presence_walk",
    "select_dispatch_members",
    "witnessed_turns",
]

#: Freshness window: presence evidence is trusted while fewer than this many
#: narratives exist after it (data knob, mirrors the topology node's
#: ``presence_window``).
PRESENCE_WINDOW = 3

#: The PARALLELISM POOL: at most this many lane requests in flight at once
#: (owner spec W9.13-A: max_lanes = 4). It is a concurrency bound, NOT a cap on
#: the lane SET — every member the DISPATCH POLICY selects gets a lane, and the
#: pool slides over the queue (worker-pool semantics: 9 selected on a pool of 4
#: run in ceil(9/4) batches). The topology node's ``max_lanes`` data overrides
#: it; the SET is bounded by the session's ``max_dispatch`` instead
#: (:data:`NPC_DISPATCH_EVENT`) — the two knobs are orthogonal.
DEFAULT_MAX_LANES = 4

#: The world-state variant this walk reads (``system``/``world_state``).
_WORLD_STATE_EVENT = "world_state"

#: The attribution variant (:data:`fabulist.log.schema_gate.ATTRIBUTION_EVENT`,
#: written by ``graph/nodes.py`` ``_run_attribution``); read here for the
#: attribution rung AND as the seed floor's second evidence kind.
_ATTRIBUTION_EVENT = "attribution"

#: Provenance values the SEED FLOOR trusts: a card-captured character (the
#: session's cards, materialised as ``card``/``system`` events) or a world seed.
#: Runtime admits (``bts`` / ``extractor``) are deliberately NOT seed — they are
#: learned in play and only exist because some evidence already named them in
#: the scene. A SUBSET of :data:`ACTOR_PROVENANCE` (both are seed-tier actors).
SEED_PROVENANCE = frozenset({"card", "world"})

#: The ACTOR tier (W9.22 §2, the cross-lane contract): provenance values whose
#: members are the owner's attached cast — a card-captured character, a world
#: seed, an ``actor`` the OWNER admitted from a cast proposal
#: (:func:`fabulist.state.cast.admit_from_proposal`) and the ``user`` when
#: User-as-Actor is ON (that member is admitted by the player seams; the SETTING
#: is read there, never here — this module stays a pure projection). Everything
#: else is the presence tier: learned in play, never lanes, and the only tier the
#: cast cap may retire.
ACTOR_PROVENANCE = frozenset({"card", "world", "actor", "user"})

#: Lane record kinds (W9.13-B appends them; this module reads them back).
NPC_LANE_EVENT = "npc_lane"
NPC_LANES_EVENT = "npc_lanes"

#: The per-session dispatch POLICY (W9.24 §3.5): a ``system`` event on the chat
#: session, folded latest-wins by :func:`npc_dispatch_policy`::
#:
#:     {"event": "chat_npc_dispatch", "strategy": "decision"|"random"|"lru",
#:      "max_dispatch": int >= 0}
#:
#: Absent means :data:`DEFAULT_NPC_STRATEGY` / :data:`DEFAULT_NPC_MAX_DISPATCH`.
NPC_DISPATCH_EVENT = "chat_npc_dispatch"

#: The closed strategy vocabulary. ``decision`` (the owner's default) asks d1-3b
#: which characters act this beat; ``random`` samples them; ``lru`` keeps the
#: ladder's own order. Anything else in a stored row is ignored (reads tolerate
#: data), so a hand-edited log cannot select a strategy that does not exist.
NPC_STRATEGIES = ("decision", "random", "lru")

#: The strategy a session runs when it never wrote a policy row: the decision
#: model picks. This CHANGES lane behaviour at upgrade for every existing
#: session (all-active dispatch becomes threshold-gated subset dispatch, and a
#: zero-lane turn is legal) — intended, and stated in the plan's migration note.
DEFAULT_NPC_STRATEGY = "decision"

#: The default SET bound: ``0`` means no bound (every candidate the strategy
#: keeps dispatches), i.e. today's lane set.
DEFAULT_NPC_MAX_DISPATCH = 0

#: How much recent prose the dispatch ask carries as its state line (the
#: decision model is a single forward pass; the scene tail is the context that
#: makes "who acts now" answerable). Raised 800 -> 4000 together with the
#: narrative window: the 2026-10-21 probe matrix showed the tail was never the
#: binding constraint on the CHOICE (the candidate descriptions were, see
#: :func:`_dispatch_labels`), so this buys context QUALITY — a beat judged
#: against five turns instead of two — not a flipped verdict.
DISPATCH_STATE_CHARS = 4000

#: How many of the most recent narratives the state line keeps (2 -> 5 in the
#: same change, same reason: two turns are not enough context to judge "who acts
#: now" against).
DISPATCH_STATE_NARRATIVES = 5


@dataclass(frozen=True)
class PresenceWalk:
    """One fold of the presence evidence for a session (fork-aware).

    ``present_at``/``absent_at`` are the last seq each name was seen present /
    absent; ``categories`` is the per-name world-state bucket (the member's own
    private state), ``venues`` the last venue row per name (the BTS ``venue``
    header — the fact the dispatch ask describes a candidate with),
    ``off_screen`` the latest off-screen rows, and
    ``narrative_seqs`` every narrative seq in chain order (the witnessed-turn
    index and the freshness clock). ``saw_world_state``/``saw_attribution`` are
    the seed floor's evidence gate: whether the chain holds ANY event of that
    kind (the walk visits every event anyway, so this is free).
    """

    present_at: dict[str, int] = field(default_factory=dict)
    absent_at: dict[str, int] = field(default_factory=dict)
    categories: dict[str, dict] = field(default_factory=dict)
    venues: dict[str, dict] = field(default_factory=dict)
    off_screen: list[dict] = field(default_factory=list)
    narrative_seqs: tuple[int, ...] = ()
    saw_world_state: bool = False
    saw_attribution: bool = False

    def is_present(self, name: str, window: int = PRESENCE_WINDOW) -> bool:
        """Present iff the last presence evidence is newer than the last absence."""
        last_present = self.present_at.get(name)
        if last_present is None:
            return False
        if self.absent_at.get(name, -1) >= last_present:
            return False
        return self.narratives_after(last_present) < window

    def narratives_after(self, seq: int) -> int:
        """How many narratives were appended after ``seq``."""
        return sum(1 for narrative_seq in self.narrative_seqs if narrative_seq > seq)

    def witnessed(self, seq: int, window: int = PRESENCE_WINDOW) -> tuple[int, ...]:
        """1-based turn numbers (of the last ``window`` narratives) since ``seq``."""
        turns = self.narrative_seqs[-window:]
        return tuple(
            index + 1 for index, narrative_seq in enumerate(turns) if narrative_seq >= seq
        )


def _chain_events(log: Any, session_id: str) -> list[Any]:
    """The fork-aware chain, preferring the public walk (no private re-walk)."""
    get_chain = getattr(log, "get_chain_events", None)
    if callable(get_chain):
        return list(get_chain(session_id))
    return list(log.get_events(session_id))


def presence_walk(log: Any, session_id: str) -> PresenceWalk:
    """Fold the session's presence evidence in seq order (see the module rule)."""
    present_at: dict[str, int] = {}
    absent_at: dict[str, int] = {}
    categories: dict[str, dict] = {}
    venues: dict[str, dict] = {}
    off_screen: list[dict] = []
    narrative_seqs: list[int] = []
    saw_world_state = False
    saw_attribution = False

    events = sorted(_chain_events(log, session_id), key=lambda event: event.seq)
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type == "narrative":
            narrative_seqs.append(event.seq)
            continue
        if event.type != "system":
            continue
        variant = payload.get("event")
        if variant == _ATTRIBUTION_EVENT:
            saw_attribution = True
            continue
        if variant != _WORLD_STATE_EVENT:
            continue
        saw_world_state = True
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        venue = payload.get("venue") if isinstance(payload.get("venue"), dict) else {}
        rows = payload.get("off_screen") if isinstance(payload.get("off_screen"), list) else []
        if payload.get("kind") == "checkpoint":
            # A checkpoint REPLACES: drop accumulated evidence so a stale venue
            # carried by an earlier delta cannot survive it.
            present_at.clear()
            absent_at.clear()
            categories.clear()
            venues.clear()
            off_screen = []
        for name, row in venue.items():
            key = _name_key(name)
            present_at[key] = event.seq
            if isinstance(row, dict):
                venues[key] = dict(row)
        for name, bucket in state.items():
            key = _name_key(name)
            present_at[key] = event.seq
            if isinstance(bucket, dict):
                merged = categories.setdefault(key, {})
                for category, attrs in bucket.items():
                    slot = merged.setdefault(category, {})
                    if isinstance(attrs, dict):
                        slot.update(attrs)
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = _name_key(row.get("name"))
            if not key:
                continue
            absent_at[key] = event.seq
            off_screen = [
                existing
                for existing in off_screen
                if _name_key(existing.get("name")) != key
            ]
            off_screen.append(dict(row))

    return PresenceWalk(
        present_at=present_at,
        absent_at=absent_at,
        categories=categories,
        venues=venues,
        off_screen=off_screen,
        narrative_seqs=tuple(narrative_seqs),
        saw_world_state=saw_world_state,
        saw_attribution=saw_attribution,
    )


def _name_key(name: Any) -> str:
    return str(name or "").strip()


@dataclass(frozen=True)
class LaneConfig:
    """Data knobs for the lane set (topology node data; defaults are the code's).

    ``max_lanes`` bounds CONCURRENCY only (see :data:`DEFAULT_MAX_LANES`).
    """

    max_lanes: int = DEFAULT_MAX_LANES
    presence_window: int = PRESENCE_WINDOW


def _attribution_rows(payload: dict) -> list:
    """The speaker rows of one ``system``/``attribution`` payload.

    The ONE writer (``graph/nodes.py`` ``_run_attribution``, shape pinned by
    ``schema_gate.ATTRIBUTION_SCHEMA``) emits ``{"event": "attribution",
    "attributions": [...]}``. An earlier reader here looked for ``rows`` only,
    so the attribution rung could never fire on a real log; ``rows`` stays
    tolerated as a read-side legacy shim (D38 discipline) for a pre-fix payload
    that carries only that key.
    """
    rows = payload.get("attributions")
    if not isinstance(rows, list):
        rows = payload.get("rows")
    return rows if isinstance(rows, list) else []


def _attribution_names(log: Any, session_id: str, window: int) -> list[str]:
    """Speakers of the last ``window`` narrative turns, newest speech first."""
    events = sorted(_chain_events(log, session_id), key=lambda event: event.seq)
    narrative_seqs = [event.seq for event in events if event.type == "narrative"]
    if not narrative_seqs:
        return []
    cutoff = narrative_seqs[-window:][0]
    names: list[str] = []
    for event in reversed(events):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type != "system" or payload.get("event") != _ATTRIBUTION_EVENT:
            continue
        if event.seq < cutoff:
            continue
        for row in _attribution_rows(payload):
            if not isinstance(row, dict):
                continue
            name = _name_key(row.get("speaker"))
            if name and name not in names:
                names.append(name)
    return names


def _player_name(log: Any, session_id: str) -> str | None:
    """The session persona's display name (the player never gets a lane)."""
    try:
        from fabulist.personas.registry import list_personas, persona_for_chat

        persona_id = persona_for_chat(log, session_id)
        for persona in list_personas(log):
            if persona.persona_id == persona_id:
                return persona.name
    except Exception:  # noqa: BLE001 - presence is best-effort, never fatal
        return None
    return None


#: Surfaces that are the PLAYER, never a lane owner (a BTS header may say
#: "User"/"Player"/"You" — the live session admitted "User" as cast and laned it).
#: ``protagonist`` is the same door for a persona named by its ROLE: a live session
#: admitted "Protagonist" (the user's own persona) as a ``bts`` member
#: and then laned it, so the engine role-played the player with User-as-Actor OFF.
PLAYER_SURFACES = frozenset(
    {
        "user", "player", "you", "your", "yours", "protagonist",
        # First-person surfaces (live e2e, wave close): the user's spoken "I"
        # was BTS-detected as a cast candidate. The player is never a candidate.
        "i", "me", "my", "mine", "myself",
    }
)


def _lane_eligible(member: CastMember) -> bool:
    """Does this member's TIER let it own a lane this turn?

    Actor tier only. Cast admission is USER-GATED (owner ruling): BTS detection
    files a ``cast`` proposal and admits nothing, so a presence-tier member has
    not been accepted by anyone and must never be role-played. This used to be a
    "lane until the promotion gate assesses you" bridge; the gate is gone, and a
    strict tier test is now the whole rule — a member starts laning when the
    owner accepts its proposal (:func:`fabulist.state.cast.admit_from_proposal`
    re-tiers it to ``actor``), never before.
    """
    return member_tier(member) == TIER_ACTOR


def _eligible(
    members: list[CastMember],
    player: str | None,
) -> list[CastMember]:
    """Active roster members that may own a lane, in roster order.

    The shared filters first — retired, provisional (a descriptor is a role word,
    not a person), the player and the generic player surfaces — then the TIER
    gate (:func:`_lane_eligible`).
    """
    from .cast import canonical_key

    player_key = canonical_key(player) if player else ""
    out: list[CastMember] = []
    for member in members:
        if not getattr(member, "is_active", True):
            continue
        if getattr(member, "provisional", False):
            continue
        # W9.22 User-as-Actor: the ADMIT DOOR owns the ON/OFF decision (a
        # ``user``-provenance member exists only when the setting is ON), so the
        # read side must not subtract it again by name — that would grant the
        # actor tier and then mute it. Every other member keeps today's
        # subtraction exactly.
        if getattr(member, "provenance", "") != "user":
            if player_key and canonical_key(member.display_name) == player_key:
                continue
            if canonical_key(member.display_name) in PLAYER_SURFACES:
                continue
        if not _lane_eligible(member):
            continue
        out.append(member)
    return out


def _registry_presence(
    log: Any, session_id: str, keys: list[str]
) -> dict[str, bool]:
    """Registry-first presence answers for ``keys`` (W9.29 §8, req. 13).

    A being the registry has assigned is in the scene iff its assignment is
    OBJECT-EQUAL to the player's; the absence bucket is just another location, so
    a move to it (or to any other room) is the deterministic exit. Only beings
    the registry can fully answer for appear in the mapping: a being with no
    assignment, or a session whose player has no room yet, is ABSENT from it and
    the caller falls through to the walk beneath (:func:`_member_present`).
    """
    from . import entities

    try:
        reg = entities.registry(log, session_id)
    except Exception:  # noqa: BLE001 - presence is best-effort, never fatal
        return {}
    player_where = reg.assignments.get(entities.PLAYER)
    if player_where is None:
        return {}
    answers: dict[str, bool] = {}
    for key in keys:
        where = reg.assignments.get(str(key))
        if where is not None:
            answers[str(key)] = where == player_where
    return answers


def active_members(
    log: Any,
    session_id: str,
    *,
    cfg: LaneConfig | None = None,
) -> tuple[list[str], str]:
    """The active NPC roster members (``member_id``, roster order) + source.

    ``source`` is ``"presence"`` when the registry/walk pair produced the set,
    ``"attribution"`` when it fell back to who spoke recently, ``"seed"`` when
    a fresh chat has no evidence at all and its seed roster stands in for the
    scene, and ``"none"`` when the set is empty (zero lanes). The set is drawn
    from the TIER-ELIGIBLE roster only, so a presence-tier member is invisible to
    all three rungs — BTS still reports it present (it IS in the scene), the
    engine just does not spend a lane on a character the owner has not accepted.
    """
    cfg = cfg or LaneConfig()
    members = _eligible(
        roster(log, session_id),
        _player_name(log, session_id),
    )
    if not members:
        return [], "none"
    walk = presence_walk(log, session_id)
    decided = _registry_presence(
        log, session_id, [member.member_id for member in members]
    )
    present: list[str] = []
    for member in members:
        answer = decided.get(member.member_id)
        if answer is None:
            answer = _member_present(member, walk, cfg.presence_window)
        if answer:
            present.append(member.member_id)
    if present:
        return present, "presence"
    fallback: list[str] = []
    for name in _attribution_names(log, session_id, cfg.presence_window):
        member = member_by_name(members, name)
        if member is not None and member.member_id not in fallback:
            fallback.append(member.member_id)
    if fallback:
        return fallback, "attribution"
    if walk.saw_world_state or walk.saw_attribution or decided:
        # BTS (or the attribution pass, or the registry) RAN: it reported who is
        # present, so an empty set is the answer even when that report is stale
        # or names only the player. The seed floor is for logs with no evidence
        # kind at all. ``decided`` is evidence of the same class (W9.29 §8): a
        # registry assignment is a placement, so a member the tracker moved out
        # of the player's room must not be resurrected by the floor — that would
        # make req. 13's "a move is a deterministic exit" a lie on any session
        # whose compat ``world_state`` render is not written (W4 retires it).
        return [], "none"
    seed = [member.member_id for member in members if member.provenance in SEED_PROVENANCE]
    if seed:
        return seed, "seed"
    return [], "none"


def _member_present(
    member: CastMember, walk: PresenceWalk, window: int
) -> bool:
    """Presence evidence for one member, honouring User-as-Actor synonyms.

    A ``user``-provenance member (W9.22, the setting is ON) is the PLAYER: the
    tracker may spell the same person ``User``, ``Player`` or the persona name
    in different turns, while the member's display name is the persona. Matching
    only the display/aliases would leave the user actor permanently absent and
    lane-less, so the union with :data:`PLAYER_SURFACES` counts for that member
    only — every other member keeps the plain display+alias match.
    """
    surfaces = [member.display_name, *member.aliases]
    if getattr(member, "provenance", "") == "user":
        # The walk's keys preserve the tracker's casing (``User``), so the
        # synonym match is casefold-aware over the walk's OWN keys — the same
        # person spelled ``User``/``user`` is one presence.
        synonyms = {surface.casefold() for surface in surfaces} | {
            synonym.casefold() for synonym in PLAYER_SURFACES
        }
        return any(
            key.casefold() in synonyms and walk.is_present(key, window)
            for key in walk.present_at
        )
    return any(walk.is_present(surface, window) for surface in surfaces)


def lane_history(log: Any, session_id: str) -> dict[str, int]:
    """``member_id -> turn index`` of the LAST ``npc_lanes`` row that laned them.

    The turn index is the number of narratives appended at or before that row,
    so the ordering is monotone in log order and survives forks/replays without
    a stored clock. Diagnostic only (every active member is laned every turn, so
    this is not a rotation clock) — it is what the inspector reads to answer
    "has this NPC had a lane recently".
    """
    events = sorted(_chain_events(log, session_id), key=lambda event: event.seq)
    narrative_seqs = [event.seq for event in events if event.type == "narrative"]
    last: dict[str, int] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type != "system" or payload.get("event") != NPC_LANES_EVENT:
            continue
        turn = sum(1 for seq in narrative_seqs if seq <= event.seq)
        for member_id in payload.get("lanes") or []:
            last[str(member_id)] = turn
    return last


def witnessed_turns(
    log: Any, session_id: str, member_id: str, *, window: int = PRESENCE_WINDOW
) -> tuple[int, ...]:
    """The 1-based turn numbers this member was present for, of the last ``window``."""
    member = member_by_id(roster(log, session_id), member_id)
    if member is None:
        return ()
    walk = presence_walk(log, session_id)
    last_present = -1
    for name in (member.display_name, *member.aliases):
        seq = walk.present_at.get(_name_key(name))
        if seq is not None and seq > last_present:
            last_present = seq
    if last_present < 0:
        return ()
    return walk.witnessed(last_present, window)


def member_categories(log: Any, session_id: str, member_id: str) -> dict:
    """The member's OWN world-state categories (never anyone else's)."""
    member = member_by_id(roster(log, session_id), member_id)
    if member is None:
        return {}
    walk = presence_walk(log, session_id)
    for name in (member.display_name, *member.aliases):
        bucket = walk.categories.get(_name_key(name))
        if bucket:
            return bucket
    return {}


def _member_venue(walk: PresenceWalk, member: CastMember) -> dict:
    """The member's last stated venue row (``{venue, area}``; ``{}`` when none).

    Read from the walk's ``venues`` fold, keyed by the same display+alias match
    the presence rule uses. It is the log's own last statement, not a fresh
    inference: a member who has moved has the newer row and a member the log
    never placed gets ``{}`` (the caller's "In the scene." fallback).
    """
    for name in (member.display_name, *member.aliases):
        row = walk.venues.get(_name_key(name))
        if row:
            return row
    return {}


def last_intents(log: Any, session_id: str) -> dict[str, str]:
    """``member_id -> the intent of its most recent ``npc_lane`` row``.

    The lane prompt's "last beat you were trying to" continuity line, and the
    dispatcher's per-row ``last_intent``. It is deliberately NOT the dispatch
    ask's description any more: feeding a member's own last intent back to the
    decision model is the ratchet documented in :func:`_dispatch_labels`.
    """
    last: dict[str, str] = {}
    for event in sorted(_chain_events(log, session_id), key=lambda event: event.seq):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type != "system" or payload.get("event") != NPC_LANE_EVENT:
            continue
        member_id = str(payload.get("member_id") or "")
        intent = str(payload.get("intent") or "").strip()
        if member_id and intent:
            last[member_id] = intent
    return last


# -- the per-session dispatch policy (W9.24 §3.5) ----------------------------


@dataclass(frozen=True)
class NpcDispatchPolicy:
    """Which of the ladder's candidates actually lane this turn (session setting).

    ``max_dispatch`` bounds the SET (``0`` = no bound); ``max_lanes``
    (:class:`LaneConfig`) bounds CONCURRENCY. The two are orthogonal: a turn of
    9 candidates under ``max_dispatch=3`` plans 3 lanes and runs them on a pool
    of up to ``max_lanes``.
    """

    strategy: str = DEFAULT_NPC_STRATEGY
    max_dispatch: int = DEFAULT_NPC_MAX_DISPATCH

    def bound(self, count: int) -> int:
        """``count`` after the SET bound (``0`` = unbounded)."""
        if self.max_dispatch <= 0:
            return count
        return min(count, self.max_dispatch)


def npc_dispatch_policy(log: Any, session_id: str) -> NpcDispatchPolicy:
    """Fold ``chat_npc_dispatch`` system events latest-wins (fork-aware).

    Per KEY, not per row: a malformed field (an unknown strategy, a negative or
    non-int bound) is skipped and the previous valid value stands, so a
    hand-edited row can neither select a strategy that does not exist nor
    invert the bound. Absent event / blank session ⇒ the documented defaults —
    ``decision`` and ``0`` (no bound).

    Fork-carrying is the reason this reads the CHAIN
    (:func:`_chain_events`): a branch inherits its parent's policy up to the
    fork point, exactly like the presence evidence around it.
    """
    strategy = DEFAULT_NPC_STRATEGY
    max_dispatch = DEFAULT_NPC_MAX_DISPATCH
    for event in sorted(_chain_events(log, session_id), key=lambda event: event.seq):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type != "system" or payload.get("event") != NPC_DISPATCH_EVENT:
            continue
        value = payload.get("strategy")
        if isinstance(value, str) and value in NPC_STRATEGIES:
            strategy = value
        value = payload.get("max_dispatch")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            max_dispatch = value
    return NpcDispatchPolicy(strategy=strategy, max_dispatch=max_dispatch)


async def select_dispatch_members(
    log: Any,
    session_id: str,
    candidates: list[str],
    decision: Any = None,
    policy: NpcDispatchPolicy | None = None,
    *,
    beat: str = "",
    user_content: str = "",
) -> list[str]:
    """The policy's lane SET out of this turn's tier-eligible ``candidates``.

    ``candidates`` are the ladder's own answer (:func:`active_members`: presence
    → attribution → seed floor, tier-eligible only), in ladder order — this
    function never widens eligibility, it only narrows the SET. Returns the
    selected ``member_id`` list in CANDIDATE order, so the fan-out's rows stay
    deterministic whatever the strategy did internally. An empty list is a
    legal answer: zero-lane turns are in the contract.

    Strategies:

    * ``decision`` (default): ONE d1-3b ``choice`` ask over the candidate
      display names (:data:`DISPATCH_SECTION` — the engine composes the labels
      per turn, the row owns the wording and the threshold) with the beat and
      the recent prose tail as the state line. Candidates whose answer
      probability reaches the row's threshold dispatch, capped by
      ``max_dispatch`` (highest probability first when the cap bites). Nothing
      clearing the threshold is a legal zero-lane turn.
    * ``random``: a sample of the candidates, seeded from the log itself
      (``session_id`` + the last narrative seq) so a replay of the same log
      selects the same members — the log is the source of truth, never ambient
      entropy.
    * ``lru``: the ladder's own order truncated to ``max_dispatch``. Despite the
      name no rotation clock is consulted: this is the deterministic sibling
      closest to today's behaviour (a real rotation would need per-member lane
      history to outrank the ladder).

    **Fail-open.** A missing/disabled decision caller, an ask that returns
    nothing usable (error, timeout, no probabilities), or a disabled bank row
    all fall back to today's set — the ladder's candidates, still truncated to
    ``max_dispatch`` because that bound is the owner's cost knob. The fallback
    never yields zero lanes on error.

    **The direct-address floor (owner ruling).** Whatever the strategy answered,
    a candidate the player named in ``user_content`` dispatches anyway — see
    :func:`direct_address_members`. It is applied AFTER the strategy narrows,
    as a UNION, and is deliberately not re-bounded by ``max_dispatch``: the
    bound is the owner's cost knob, the floor her ruling, and a character the
    player just spoke to by name is the one case where presence wins. With the
    default ``user_content=""`` (a caller that has not been updated, every test
    double) nothing matches and the answer is the strategy's, untouched.
    """
    ordered = [str(member_id) for member_id in candidates if str(member_id)]
    if not ordered:
        return []
    policy = policy or npc_dispatch_policy(log, session_id)
    if policy.strategy == "random":
        selected = _random_dispatch(log, session_id, ordered, policy)
    elif policy.strategy == "lru":
        selected = ordered[: policy.bound(len(ordered))]
    else:
        decided = await _decision_dispatch(
            log, session_id, ordered, decision, policy, beat, user_content
        )
        selected = ordered[: policy.bound(len(ordered))] if decided is None else decided
    return _with_direct_address(log, session_id, ordered, selected, user_content)


def direct_address_members(
    log: Any, session_id: str, candidates: list[str], user_content: str
) -> list[str]:
    """The ``candidates`` the player ADDRESSED BY NAME this turn (candidate order).

    The floor's whole read: a tier-eligible candidate counts when its display
    name or ANY alias appears in the turn's player message as a WORD —
    case-insensitive, ``\\b``-bounded on the casefolded text, so a short name
    such as "Min" cannot match inside a longer word ("Morning"). Only the
    candidates handed in are considered, so this can never widen eligibility: a
    presence-tier member named in the message is still not a lane.
    """
    message = str(user_content or "").casefold()
    if not message.strip():
        return []
    cast = roster(log, session_id)
    addressed: list[str] = []
    for member_id in candidates:
        member = member_by_id(cast, member_id)
        if member is None:
            continue
        surfaces = (member.display_name, *member.aliases)
        if any(_named_in(surface, message) for surface in surfaces):
            addressed.append(str(member_id))
    return addressed


def _named_in(surface: str, message: str) -> bool:
    """Does ``surface`` occur in the casefolded ``message`` as a whole word?"""
    name = " ".join(str(surface or "").casefold().split())
    if not name:
        return False
    return re.search(rf"\b{re.escape(name)}\b", message) is not None


def _with_direct_address(
    log: Any,
    session_id: str,
    ordered: list[str],
    selected: list[str],
    user_content: str,
) -> list[str]:
    """``selected`` ∪ :func:`direct_address_members`, in candidate order.

    A union, never a filter: the floor can only ADD candidates the strategy
    dropped, and it never removes one the strategy kept.
    """
    addressed = direct_address_members(log, session_id, ordered, user_content)
    if not addressed:
        return selected
    keep = set(selected) | set(addressed)
    return [member_id for member_id in ordered if member_id in keep]


async def _decision_dispatch(
    log: Any,
    session_id: str,
    ordered: list[str],
    decision: Any,
    policy: NpcDispatchPolicy,
    beat: str,
    user_content: str = "",
) -> list[str] | None:
    """The ``decision`` strategy's SET, or ``None`` when there is no answer.

    ``None`` is the fail-open signal (no caller, no usable answer, the row is
    off) — distinct from ``[]``, which is a real verdict of "nobody acts".
    """
    if decision is None or getattr(decision, "enabled", True) is False:
        return None
    labels = _dispatch_labels(log, session_id, ordered)
    question = _dispatch_question(log, labels)
    if question is None:
        return None
    payloads, threshold = question
    answers = await decision.ask(
        _dispatch_state(log, session_id, beat, user_content), payloads
    )
    probabilities = _answer_probabilities(answers, next(iter(payloads)))
    if probabilities is None:
        return None
    scored = [
        (probabilities[label], index)
        for index, (_member_id, label, _description) in enumerate(labels)
        if label in probabilities
    ]
    cleared = [
        (probability, index)
        for probability, index in scored
        if probability >= threshold
    ]
    if not cleared:
        return []
    cleared.sort(key=lambda row: (-row[0], row[1]))
    keep = {index for _probability, index in cleared[: policy.bound(len(cleared))]}
    return [
        member_id
        for index, (member_id, _label, _description) in enumerate(labels)
        if index in keep
    ]


def _dispatch_labels(
    log: Any, session_id: str, ordered: list[str]
) -> list[tuple[str, str, str]]:
    """``(member_id, label, description)`` per candidate, in candidate order.

    The choice question's labels ARE the candidate display names (the pinned
    difference from the bank's static-criteria rows): the roster changes every
    turn, so the engine composes them here. A duplicate display name gets a
    stable numeric suffix — ``member_by_name`` refuses ambiguity, and a label
    the answer cannot be read back from would silently drop a candidate.

    The DESCRIPTION is a presence/venue FACT, never the member's last lane
    intent, and that is a probe finding rather than a wording preference. It
    used to be ``last_intents`` (the intent of the member's most recent
    ``npc_lane`` row), which made a candidate's own previous beat argue against
    its next one: a timid intent ("I want to disappear from the conversation")
    rated her 0.085 against the row's 0.25 threshold, three stable runs on the
    real d1-3b. Worse, it RATCHETS — the turn after her FIRST lane she had no
    intent yet and the old default cleared, while every cleared turn writes
    another self-effacing intent, so she can never clear again. The same probe
    rated the same candidate 0.455 on presence facts ("Present in the
    bathroom."), which is what this renders: the log's last stated venue, or
    "In the scene." when the walk holds no venue for her (a seed-floor turn, an
    attribution rung, a member the tracker never placed).
    """
    from .cast import member_by_id, roster

    cast = roster(log, session_id)
    walk = presence_walk(log, session_id)
    registry = _registry_fold(log, session_id)
    seen: dict[str, int] = {}
    rows: list[tuple[str, str, str]] = []
    for member_id in ordered:
        member = member_by_id(cast, member_id)
        name = member.display_name if member is not None else member_id
        count = seen.get(name, 0) + 1
        seen[name] = count
        label = name if count == 1 else f"{name} ({count})"
        description = (
            _presence_fact(walk, member, registry)
            if member is not None
            else "In the scene."
        )
        rows.append((member_id, label, description))
    return rows


def _registry_fold(log: Any, session_id: str) -> Any:
    """The session's folded registry, or ``None`` when it cannot be read.

    Presence stays best-effort: a registry read that raises (a partial log, a
    malformed entity row) must never cost the turn its lanes, so the caller
    falls back to the walk alone — exactly the pre-W9.29 behaviour.
    """
    try:
        from . import entities

        return entities.registry(log, session_id)
    except Exception:  # noqa: BLE001 - presence is best-effort, never fatal
        return None


def _registry_place(registry: Any, member_id: str) -> str:
    """The registry's location NAME for a member, or ``""`` when it has none.

    ``""`` covers three cases on purpose, all of which fall back to the walk's
    venue text: no registry at all, a being the registry never assigned, and a
    being in the ABSENCE bucket (``off_screen`` is not a place anyone is
    "present in" — its name would turn a deterministic exit into a lie).
    """
    if registry is None:
        return ""
    from .entities import OFF_SCREEN

    where = registry.assignments.get(str(member_id))
    if not where or where == OFF_SCREEN:
        return ""
    location = registry.locations.get(where)
    return getattr(location, "name", "") or ""


def _presence_fact(walk: PresenceWalk, member: CastMember, registry: Any = None) -> str:
    """One presence/venue sentence for a candidate (never its last intent).

    Registry first (W9.29 §8): when the registry has assigned the member to a
    room, the sentence names THAT room — the fact behind the D88 probe phrase,
    now backed by an object instead of a delta's free text. Otherwise the walk's
    last stated venue is the answer, as before.
    """
    place = _registry_place(registry, member.member_id)
    if not place:
        venue = _member_venue(walk, member)
        place = str(
            venue.get("venue") or venue.get("location") or venue.get("area") or ""
        ).strip()
    return f"Present in {place}." if place else "In the scene."


def _dispatch_question(
    log: Any, labels: list[tuple[str, str, str]]
) -> tuple[dict[str, dict], float] | None:
    """The ask payload + threshold, or ``None`` when the dispatch row is off.

    Bank row first (owner wording/threshold), the seeded code row
    (:data:`fabulist.decision_queries.DISPATCH_QUESTION`) for a log whose stored
    bank predates it — a fresh log and an upgraded one therefore ask the same
    question. The criteria are REPLACED with this turn's candidate labels: the
    row's stored criteria are deliberately empty.
    """
    from fabulist.decision_queries import (
        DEFAULT_THRESHOLD,
        DISPATCH_QUESTION,
        DISPATCH_QUESTION_ID,
        DISPATCH_SECTION,
        question_payloads,
        questions_for_section,
    )

    rows = questions_for_section(log, DISPATCH_SECTION)
    row = rows[0] if rows else DISPATCH_QUESTION
    if not row.get("enabled", True) or row.get("type") != "choice":
        return None
    question_id = str(row.get("id") or DISPATCH_QUESTION_ID)
    payload = question_payloads([dict(row, id=question_id)]).get(question_id)
    if payload is None or not labels:
        return None
    payload["criteria"] = {label: description for _id, label, description in labels}
    return {question_id: payload}, float(row.get("threshold", DEFAULT_THRESHOLD))


def _dispatch_state(
    log: Any, session_id: str, beat: str = "", user_content: str = ""
) -> str:
    """The decision model's view of the turn: prose tail, player message, beat.

    ``user_content`` is the turn's own player message, plumbed from the turn
    state by the dispatcher node. It belongs HERE, once, rather than folded into
    every candidate's description: the message is one fact about the BEAT, the
    description is one fact about the CANDIDATE. The probe is explicit that the
    message alone does not rescue a bad description (0.070 against 0.085) — it
    is the line that makes "who did the player just speak to" answerable, taken
    together with :func:`_dispatch_labels`' presence facts and the direct-address
    floor. Empty (the back-compat default, and every pre-change caller) appends
    nothing, so the prompt stays what it was.
    """
    prose = _recent_prose(log, session_id)
    sections: list[str] = []
    if prose:
        sections.append(prose)
    message = " ".join(str(user_content or "").split())
    if message:
        sections.append(f"The player's message this turn: {message}")
    beat = " ".join(str(beat or "").split())
    if beat:
        sections.append(f"This beat: {beat}")
    return "\n\n".join(sections) or "A roleplay scene with no narrative yet."


def _recent_prose(
    log: Any, session_id: str, narratives: int = DISPATCH_STATE_NARRATIVES
) -> str:
    """The tail of the last ``narratives`` narrative prose blocks (clipped)."""
    texts: list[str] = []
    for event in sorted(_chain_events(log, session_id), key=lambda event: event.seq):
        if event.type != "narrative":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        prose = payload.get("prose")
        if isinstance(prose, str) and prose.strip():
            texts.append(prose.strip())
    return "\n\n".join(texts[-narratives:])[-DISPATCH_STATE_CHARS:]


def _answer_probabilities(
    answers: Any, question_id: str
) -> dict[str, float] | None:
    """``label -> P`` of the dispatch choice answer, or ``None`` when unusable.

    Strict on shape, the same discipline :mod:`fabulist.state.proposal_lanes`
    uses for P(propose): a choice answer carries ``{"choice",
    "probabilities"}``, and anything else is "no answer" rather than a coerced
    zero — a fabricated probability would either spend lanes on noise or drop
    the scene's real actors.
    """
    if not isinstance(answers, Mapping):
        return None
    answer = answers.get(question_id)
    if not isinstance(answer, Mapping):
        return None
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, Mapping):
        return None
    parsed: dict[str, float] = {}
    for label, value in probabilities.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parsed[str(label)] = float(value)
    return parsed or None


def _random_dispatch(
    log: Any, session_id: str, ordered: list[str], policy: NpcDispatchPolicy
) -> list[str]:
    """A LOG-SEEDED sample of the candidates, returned in candidate order.

    The seed is ``session_id`` + the seq of the chain's newest narrative: a
    replay (or a fork reading the same chain) selects exactly the same members,
    while consecutive turns differ because their narrative seqs differ. Python
    seeds a string deterministically (sha512), so no ambient entropy enters the
    turn. When the bound keeps every candidate there is nothing to sample.
    """
    count = policy.bound(len(ordered))
    if count >= len(ordered):
        return list(ordered)
    seqs = [
        event.seq
        for event in _chain_events(log, session_id)
        if event.type == "narrative"
    ]
    rng = random.Random(f"{session_id}:{seqs[-1] if seqs else 0}")
    picked = set(rng.sample(ordered, count))
    return [member_id for member_id in ordered if member_id in picked]
