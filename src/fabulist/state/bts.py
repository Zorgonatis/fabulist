"""Behind-the-Scenes (BTS) world-state interaction (PLAN D21 / Wave 6 lane W6-A).

BTS is the single world-state owner (D21/B4). It runs as a **post-turn
interaction** — its own D18 module flow (``bts``) and D19 agent binding — and
reads the turn's user message + narrator prose plus the entity registry, then
records what changed through the WRITE-ACTION tool loop (W9.29 §6).

W9.29 requirement 9 replaced the payload with actions
-----------------------------------------------------
The free-form ``[BTS]`` notation is RETIRED as a write path. The tracker's
protocol is a tool loop: the model reads (``get_roster``/``get_folded_state``/
``get_recent_deltas``), then changes the world with ONE validated call per change
(:func:`fabulist.state.entity_tools.entity_action_tools` — establish / move /
set / update / retire / rename / flag), reads the answer, and closes the turn
with a completion that calls no tool. The engine appends; the model never writes
the log (D9). The old notation parsers stay importable as read-side legacy
helpers (D38 discipline) — the sidebar's raw-notation reconstruction and archived
``raw`` fields still need them — but nothing parses a write out of prose any more.

What this module owns
---------------------
* :data:`BTS_SYSTEM_PROMPT` — the (notation-free) tracker directive: the names
  contract, the write-action protocol, the rules.
* :data:`BTS_ACTION_PROTOCOL` + the entity palette
  (:func:`fabulist.prompts.entity_palette.render_entity_palette`) — the tail
  message appended RUNNER-SIDE after the flow's own messages, so a stored preset
  customisation cannot drop the registry or the protocol (AGENTS 6: the seeded
  prefix stays byte-stable; the dynamic content rides the end).
* :class:`BtsRunner` — the loop driver: the per-turn
  :class:`~fabulist.state.entity_tools.EntityTurn` ledger, the loop's natural
  stop as the turn's close-out, and the failure doctrine (a timed-out or empty
  run raises :class:`BtsExtractionError`; actions already appended STAND).
* :func:`parse_bts` / :func:`parse_bts_structured` / :func:`build_bts_request_profile`
  / :func:`bts_tool` — READ-SIDE legacy helpers (§11.2), no longer a write path.
* :func:`should_checkpoint` — the retired delta/checkpoint cadence (D21). The
  registry is current by construction, so nothing calls it on the live path; it
  stays importable for archived logs and tests.
* :func:`session_bts_block` / :func:`latest_world_state` — fork-aware log reads
  that feed the narrator's ``### Behind the scenes`` section (B2).
* :func:`render_bts_state` / :func:`render_bts_section` — declarative renderers.

The **fold keys** (D21/B4): the projector folds ``state-delta`` payloads
latest-wins per key, and the frontend/facts/<state> consumers read the literal
keys ``loc``/``weather``/``tone``/``inv`` (time stays the rules clock's, D3).
BTS therefore writes a flat ``deltas`` projection alongside its structured
``world_state`` event, so no downstream consumer changes. ``time`` is NEVER set
by BTS (the clock is authoritative; BTS only reads it).

W9.29 §6: the ``world_state`` event is now a COMPAT RENDER
---------------------------------------------------------
The registry (:mod:`fabulist.state.entities`) is the source of truth; the node
folds it into today's ``world_state`` payload shape each turn so every reader
that has not been flipped yet (the presence walk, the facts section, the flat
fold, the sidebar) keeps working. That render retires in W4.

The **typed reference contract** (W8.6, W7 ADDENDUM 1-3)
------------------------------------------------------
BTS is the producer side of W7's cast identity. The report path expressed it as a
``references[]`` payload; W9.29 §6 replaced the payload with actions, so the
defence now arrives TWICE, per person:

* a real action (``move_being``/``set_attr``/``establish_relationship``/
  ``establish_item``) resolves the identity at the being door — a roster
  ``member_id`` or a name, an unknown surface filed as a ``cast`` PROPOSAL
  (:func:`_emit_cast_proposal`) and never assigned;
* the node still calls :func:`apply_bts_cast_proposals` with the turn's
  MENTIONED-NAME SET (:func:`fabulist.state.entity_tools.mentioned_names`), so
  the identity/descriptor hygiene below keeps its single producer path (review
  R1), and ``rename_being`` replaces the retired ``references[].rename_to``
  channel.

Each surfaced person carries a reference *kind*:

* ``identity`` — a specific person the reader can pick out ("Zahra"); it
  RESOLVES to an accepted cast member when one already carries the name, and
  otherwise becomes a ``cast`` PROPOSAL (:func:`_emit_cast_proposal`) — cast
  admission is USER-GATED, so nothing is admitted at this door any more. Once
  accepted, the member is renameable in place
  (:func:`fabulist.state.cast.patch_member`, colour and ``member_id`` preserved).
* ``descriptor`` — a role phrase that can point at somebody else next time
  ("the woman"); it enters through the scene/presence-scoped descriptor path and
  is **never** auto-aliased (ADDENDUM 2 A9/A13). A leading article is the one
  spelling signal the engine reads for itself: "the soldier" is a descriptor
  even when the model typed ``identity`` (W7 ADDENDUM 3 Q1, owner: "anything
  that is not a specific identity"), unless the roster already holds that exact
  surface as a display name or alias (see
  :func:`_keep_roster_surfaces_as_identities`).

The payload shape itself is unchanged (the plan's non-goal): the
``world_state``/``state-delta`` events keep exactly the keys they had, and the
reference list is consumed engine-side by :func:`apply_bts_cast_proposals` at the
single admit path — it is not persisted inside the world-state payload.

The **admit hygiene** rules (live RCA)
--------------------------------------------------------
That session forked a card chat (cards "Hobbs" and "Tessa", player persona
"Wanderer") and, over five turns, left BOTH card characters retired by the cast
cap while seven members play never created stayed active. Everything the narrator
saw as "cast" was junk. The four rules this module now enforces at the door:

* **One scene entity, one member.** The model reported the same person twice per
  turn — as a ``state``/``venue`` header spelled run-together (``TheHeron``)
  and as a ``descriptor`` reference (``the heron``). The spelling rule that
  makes those one surface lives in :func:`fabulist.state.cast.canonical_key`;
  here, a descriptor whose phrase the engine ALREADY linked counts as live for
  :data:`~fabulist.state.presence.PRESENCE_WINDOW` narratives (a delta does not
  re-report an unchanged character), and a surface echoing the engine's own
  ``(N)`` disambiguation suffix re-links instead of minting ``(N+1)``.
* **Identity surfaces are roster-aware, and the PLAYER is never cast.** A surface
  that resolves to a live member is that member (:func:`_resolve_or_propose_identity`),
  and a surface naming nobody — a role word or the chat's persona
  (:func:`_player_keys`) — is refused, retiring the member the engine itself
  minted for it (:func:`_retire_non_character_member`).
* **Seed members are pinned** from cap eviction: ``card``/``world`` provenance is
  never the eviction candidate (:func:`fabulist.state.cast.enforce_cast_cap`).
* **The cap's recency clock counts speaker evidence** as well as world-state
  presence (:func:`_cap_last_seen`): attribution rows name the character who
  spoke, with its ``member_id``, on every turn.

Every one of these is best-effort and append-only: a refused proposal is logged
(the W8.9 tombstone, the ambiguity row) and never fails the turn (D9/D10).

The **User-as-Actor doors** (W9.22 §2)
--------------------------------------
The player is REFUSED at the admit door by default — a header spelling the
persona is not a cast member (the W9.21 rule above). The session setting
``user_as_actor`` (default OFF, folded by the chat-config router and read here by
:func:`user_as_actor`) is the one key that reverses it: ON makes the player an
ACTOR — admitted as the ONE ``user`` member keyed on the persona
(:func:`_admit_user_member`), never retired as a mistake
(:func:`_retire_non_character_member` is not even consulted for a player surface)
and coloured by ROLE (the near-white ``_assign_speaker_colors`` reserves for the
persona's name) rather than by the palette. OFF is the pre-W9.22 behaviour event
for event: refused at the door, tombstoned if the engine had minted it, absent
from the lanes.

The **cast door is USER-GATED** (owner ruling, reversing D76's auto-promotion)
------------------------------------------------------------------------------
A person the tracker learns in play is NOT a cast member. BTS keeps DETECTING
identities — the typed ``references[]`` and, with no references, the state
headers — and every identity with no ACCEPTED member behind it is filed as a
``cast`` proposal for the review home (:func:`_emit_cast_proposal` -> Lane L4's
``state.proposals.emit_cast_proposal``). Nothing is admitted, so an unaccepted
person has no ``cast_admit`` row, no ``member_id``, no colour and no lane; the
owner's accept is the one admission (:func:`fabulist.state.cast.admit_from_proposal`,
provenance ``actor``). ``state.promotion``'s gate is gone with the auto-promotion
it existed to perform — nothing judges a presence into actorhood any more.

Why the door still RESOLVES before it proposes: an accepted member, a card
character and a world seed must keep working exactly as before — a sighting of an
existing member only re-links the spelling (an alias) or resurrects a
cap-tombstoned member, and never files a proposal for a person the owner already
has.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, Sequence

from ..prompts.envelope import (
    assert_envelope_not_in_system_scope,
    assert_envelope_not_in_user_scope,
    insert_engine_envelope,
)
from .cast import CastMember, REFERENCE_DESCRIPTOR, REFERENCE_IDENTITY

logger = logging.getLogger(__name__)

__all__ = [
    "BTS_CHECKPOINT_TURNS",
    "BTS_DELTA_CLIP_CHARS",
    "BTS_RECENT_DELTAS_DEFAULT",
    "BTS_RECENT_DELTAS_MAX",
    "BTS_SYSTEM_PROMPT",
    "BTS_REPAIR_INSTRUCTION",
    "BTS_STRUCTURED_REPAIR_INSTRUCTION",
    "BTS_TOOL_NAME",
    "FOLDED_STATE_TOOL",
    "RECENT_DELTAS_TOOL",
    "ROLE_SURFACES",
    "ROSTER_TOOL",
    "BTS_ACTION_PROTOCOL",
    "BtsExtractionError",
    "BtsParseError",
    "BtsReference",
    "BtsResult",
    "BtsRunner",
    "CHECKPOINT_TURNS",
    "RECONCILE_CADENCE",
    "RECONCILE_DIRECTIVE",
    "last_reconcile_turn",
    "reconcile_is_due",
    "bts_loop_tools",
    "apply_bts_cast_proposals",
    "build_bts_messages",
    "build_bts_request_profile",
    "bts_name_set",
    "bts_references",
    "bts_tool",
    "classify_reference_kind",
    "is_determiner_phrase",
    "is_role_surface",
    "parse_bts_structured",
    "parse_references",
    "fold_flat",
    "latest_world_state",
    "parse_bts",
    "render_bts_section",
    "render_bts_state",
    "checkpoint_due",
    "checkpoint_requested",
    "session_bts_block",
    "should_checkpoint",
    "turns_since_checkpoint",
    "user_as_actor",
    "WorldStateFn",
]

#: Delta checkpoint cadence (D21): a checkpoint is due after this many turns
#: without one (the BTS directive says "~8", the lane spec "~10" — 10 is the
#: blessed cadence here).
BTS_CHECKPOINT_TURNS = 10

#: Back-compat alias used by callers/tests.
CHECKPOINT_TURNS = BTS_CHECKPOINT_TURNS

#: System prompt (W9.29 §6): the tracker's directive, notation-FREE.
#:
#: The retired text taught a ``[BTS]`` notation DSL; a prompt that teaches a
#: format the engine no longer parses is worse than no prompt at all (G7: the
#: step catalog says what the surface IS). What survives is the *names* contract
#: (W7 ADDENDUM 1-3, identity vs descriptor — it never described the envelope),
#: the rules the tracker always had (no invention, no teleporting, carry values
#: forward), and the new write-action protocol. The registry itself is not here:
#: it is dynamic content and rides the tail (:data:`BTS_ACTION_PROTOCOL` + the
#: entity palette) so the seeded prefix stays byte-stable turn to turn (AGENTS 6).
BTS_SYSTEM_PROMPT = """You track continuity for one roleplay turn. You never write prose: you read USER MESSAGE, NARRATIVE and the ENTITY REGISTRY, record what the prose put on the page by CALLING TOOLS (one call per change), then close with one sentence.

Sentence by sentence: who is where, placed, moved, named, changed — one action per answer. The registry says what is recorded, not what the prose showed. A registry listing only off-screen means nothing is recorded yet, not that nothing happened.

## Actions (checklist)
Run this every turn.
- A new place the prose puts anyone in -> establish_location {"name": "master bedroom"}. Keep its returned id.
- Every being the prose places -> move_being {"who": "Seraphine", "to": "<that loc id>"}. The player is who="player". Establish the place first.
- Every being the prose stops showing -> move_being {"who": ..., "to": "off_screen", "at": "<place they are>"}.
- Every attribute the prose changes -> set_attr {"host": "Seraphine", "key": "hostility", "value": "58"}; relational keys add "subject": "player". Only keys the registry lists (defaults: hostility/trust/affection/fear/respect, 0-100 with subject; stress/arousal/energy, 0-100; mood; tension, 0-10; weather/tone/forecast/irony).
- New named item -> establish_item {"name": "brass key", "holder": "player"}.
- New enduring situation -> establish_thread {"name": "the debt", "detail": "..."}.
- Existing object changed -> update_object {"id": ...}.
- A retraction, a thing gone for good -> retire_object {"id": ...}.
- A new person the prose names -> get_roster {"name": "<name>"}; the engine files them for the owner. Never record a non-member.
- Prose that contradicts a recorded object -> flag_contradiction {"anchor": "..."}.

A turn that places, moves, names or changes anything MUST have at least one action. "No changes recorded" is right only when the prose changed nothing.

## Worked example
NARRATIVE: "The master bedroom is dim. Seraphine waits in the doorway. 'You're late,' she says, her jaw tight."
Calls, one at a time, each id reused from the result before it:
1. establish_location {"name": "master bedroom"} -> id loc_9f2c
2. move_being {"who": "player", "to": "loc_9f2c"}
3. move_being {"who": "Seraphine", "to": "loc_9f2c"}
4. set_attr {"host": "Seraphine", "subject": "player", "key": "hostility", "value": "58"} -> id mt_7a01
5. set_attr {"host": "Seraphine", "key": "mood", "value": "tight-jawed"}
Close: "Bedroom established, both placed, hostility 58."

## References
- A name that picks out one specific person (Zahra, SEGFAULT) is an identity.
- A role phrase that could be somebody else next turn (the woman, a guard) is a descriptor: a scene handle, never a name.
- Narrative, narrator, scene, story, off-screen, you are never people.
- A new name for somebody already cast -> rename_being {"who": "Seraphine", "to": "Sera"}: same person, same id, old spelling kept as an alias. One spelling is one person; two people are never merged.
- A name the registry does not carry is not yours to invent: call get_roster; the engine files it for the owner. Record nothing for a non-member.

## Closing rule
- One call per message, then wait. Reuse exact ids from results and the registry: never invent one, never establish what exists.
- A rejected call wrote nothing: read the error, fix that argument, call again.
- Ten write actions per turn; the three reads are free.
- Done: one short sentence, no tool call. No notation blocks, no registry recap.
- Record only what the prose shows; a being moves only when the prose moves them. A meter you do not set keeps its value. Off-screen people still live. Never record time."""

#: Legacy (D38 read-side shim): the corrective turn the retired notation path
#: appended after an unparseable reply. W9.29 §11.2 keeps the parsers importable
#: for archived logs, so the instruction that goes with them stays too; nothing on
#: the live path appends it any more (a rejected ACTION is corrected in-loop by the
#: tool result, not by a re-ask).
BTS_REPAIR_INSTRUCTION = (
    "Your previous reply did not contain a parseable BTS block. Reply again with "
    "exactly one block that starts with [BTS] or [BTS|CP] and ends with [/BTS], "
    "using the notation given. No prose outside the block."
)

#: Legacy (D38 read-side shim): the same corrective turn for the retired
#: ``report_world_state`` path. Kept importable with :func:`bts_tool` so an
#: archived trace/replay still resolves its symbols (W9.29 §11.2).
BTS_STRUCTURED_REPAIR_INSTRUCTION = (
    "Your previous reply was not a usable structured BTS payload. Call the "
    "provided function again with a single valid JSON argument object carrying "
    "the same fields (kind, state, deltas, venue, off_screen, threads, tension, "
    "forecast, references). No prose."
)


class BtsParseError(ValueError):
    """No parseable ``[BTS]...[/BTS]`` block was found in the model output."""


class BtsExtractionError(RuntimeError):
    """The turn's BTS pass produced nothing usable; carries what it did produce.

    ``stage`` is the ``world_state_failed`` stage the node logs, so an operator
    can tell the three failure classes apart without reading the message: the
    registry loop produced nothing usable (``"registry"``, the default), the
    connection cannot call functions at all (``"tier_d"`` — W9.29 §11.2 retired
    the notation write path, so such a connection writes no state), or a legacy
    notation row failed to parse (``"parse"``).
    """

    def __init__(self, message: str, *, raw: str, stage: str = "registry") -> None:
        super().__init__(message)
        self.raw = raw
        self.stage = stage


# -- structured result -------------------------------------------------------


@dataclass(frozen=True)
class BtsResult:
    """One parsed BTS block.

    ``state`` is nested ``{category: {character: {key: value}}}`` for the GUI
    dashboard (B3); ``deltas`` is the flat fold projection (``loc``/``weather``/
    ``tone``/``inv``) the existing consumers read. ``venue`` maps character ->
    ``{venue, area}``; ``off_screen``/``threads``/``tension`` carry the sim,
    arc and pressure lanes. ``raw`` keeps the notation for the inspector.
    """

    kind: str
    state: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    deltas: dict[str, str] = field(default_factory=dict)
    venue: dict[str, dict[str, str]] = field(default_factory=dict)
    off_screen: list[dict[str, str]] = field(default_factory=list)
    threads: list[dict[str, str]] = field(default_factory=list)
    tension: dict[str, Any] = field(default_factory=dict)
    #: Optional ``forecast:`` notation line (plausible +6h weather outlook).
    #: Empty when absent/malformed — the HUD falls back, never fails (D21).
    forecast: str = ""
    raw: str = ""
    confidence: float = 1.0
    #: Typed cast references (W8.6/W7 ADDENDUM 1-3). Empty on the notation path
    #: when the model wrote no ``references:`` line — :func:`apply_bts_cast_proposals`
    #: then falls back to the legacy :func:`bts_name_set` identity admit. W9.29 §6:
    #: the action loop fills this with the turn's MENTIONED-NAME SET instead (see
    #: :attr:`mentioned`), so the cast door keeps its one producer path.
    references: tuple["BtsReference", ...] = ()
    #: W9.29 §6 action ledger — what the loop's write tools did this turn
    #: (``entity_summary``'s counts and the names the cast door needs). Zero on the
    #: legacy/read-only paths.
    actions: int = 0
    rejected: int = 0
    flags: int = 0
    #: The being names the turn's actions used (the cast door's input, §6 R1).
    mentioned: tuple[str, ...] = ()


class WorldStateFn(Protocol):
    """The post-turn world-state seam: turn text + prior state -> :class:`BtsResult`.

    ``clock`` is THIS turn's ``(time, date)`` (W9.32 §3.5) — the instant the live
    write tools judge an expiry or a time proposal against. It is a keyword with a
    default so an implementation outside the live loop (a double, the planner's
    seed) needs nothing new, and ``None`` means "no live clock": the ledger falls
    back to the folded projection.
    """

    async def __call__(
        self,
        user_msg: str,
        narrative: str,
        prior_state: dict | None = None,
        *,
        checkpoint: bool = False,
        clock: tuple[str, str] | None = None,
    ) -> BtsResult: ...


# -- W8.6: the typed reference contract --------------------------------------

#: The function name the retired structured report declared (``bts_tool``'s
#: schema). W9.29 §6 swapped the report for write actions; the name and the
#: declaration stay importable as the §11.2 read-side legacy pair
#: (``build_bts_request_profile`` builds it), so an archived profile still resolves.
BTS_TOOL_NAME = "report_world_state"

#: Narrator-class role words: they describe the telling, not a person, so they are
#: ALWAYS descriptors and never identities. Deliberately a SHORT, closed list —
#: W7 ADDENDUM 3 A16 withdrew the general role lexicon (the model classifies
#: "is this a specific person?"), and this guard only kills the observed failure
#: class where a section/lane word ("Narrative") was admitted as a cast member.
#: Entries are written in :func:`fabulist.state.cast.canonical_key` form (the
#: comparison form), so a hyphenated surface is spelled with a space.
ROLE_SURFACES = frozenset(
    {
        "narrative",
        "narrator",
        "narration",
        "scene",
        "story",
        "storyline",
        "off",
        "off screen",
        "offscreen",
        "director",
        # pronouns: a state header may say "you"; the player is not a cast member
        "you",
        "your",
        "yours",
        "player",
    }
)


#: Leading articles. A surface that opens with one names a ROLE, not a name
#: (W7 ADDENDUM 3 A16: "anything that is not a specific identity" is a
#: descriptor), which is the one spelling signal the owner's semantic rule
#: always honours.
DETERMINERS = ("the ", "a ", "an ")


def is_role_surface(name: object) -> bool:
    """True when ``name`` is a narrator-class role word, not a person."""
    from .cast import canonical_key

    return canonical_key(name) in ROLE_SURFACES


def is_determiner_phrase(name: object) -> bool:
    """True when a surface opens with an article (``the soldier``, ``a guard``).

    Spelling only — the classification consequence (a descriptor) is
    :func:`classify_reference_kind`'s, and the roster override that lets a real
    name like "The Nameless God" keep its identity is the apply path's
    (:func:`_keep_roster_surfaces_as_identities`). Separators are normalised
    first, so ``the_courier`` is a determiner phrase too.
    """
    from .cast import normalised_surface

    text = normalised_surface(name)
    return any(
        text.startswith(article) and len(text) > len(article)
        for article in DETERMINERS
    )


def classify_reference_kind(name: object, kind: object = None) -> str:
    """Resolve a reference kind, defaulting only where the model left it open.

    Two engine-side coercions, both narrow:

    * a narrator-class role word (:data:`ROLE_SURFACES`) is ALWAYS a
      ``descriptor`` — a section/lane word must never become a cast member;
    * a DETERMINER PHRASE (:func:`is_determiner_phrase`) is a ``descriptor``
      whatever the model typed, because the owner's rule is "anything that is
      not a specific identity is a descriptor" and ``the soldier`` is the
      canonical failure (the live roster carried ``the soldier``,
      ``the soldier (2)``).

    A BARE common noun is NOT reclassified ("mage" stays whatever the model
    said): a character can legitimately be named Clerk or Mage, and the roster
    carries the naming as an identity.

    This function is pure — it never reads a roster. The one escape from the
    determiner coercion is therefore applied by the caller that HAS the roster:
    :func:`_keep_roster_surfaces_as_identities`, so an existing member whose
    display name or alias is literally "the soldier" keeps its identity rather
    than being re-read as a scene handle.
    """
    if is_role_surface(name):
        return REFERENCE_DESCRIPTOR
    if is_determiner_phrase(name):
        return REFERENCE_DESCRIPTOR
    if isinstance(kind, str):
        stated = kind.strip().lower()
        if stated in (REFERENCE_IDENTITY, REFERENCE_DESCRIPTOR):
            return stated
    return REFERENCE_IDENTITY


@dataclass(frozen=True)
class BtsReference:
    """One typed cast reference from a BTS reply (W8.6).

    ``name`` is the surface form the turn used; ``rename_to`` is the W7 ADDENDUM 1
    rename proposal (an existing person given a new name); ``member_id`` is an
    optional explicit target, preferred over name resolution when it is a known
    member. ``conflict`` is the typed "this is a different person" signal of
    ADDENDUM 3 Q2 rule 5. ``reason`` rides the ``cast_patch`` event for the
    inspector.
    """

    kind: str
    name: str
    rename_to: str = ""
    member_id: str = ""
    reason: str = ""
    conflict: bool = False


def parse_references(value: object) -> list[BtsReference]:
    """Parse ``references[]`` into typed refs, tolerantly (it is model output).

    The canonical shape is ``{kind, name, rename_to, member_id, reason, conflict}``;
    the plan's spike shape (``surface``/``reference``/``display_name``) is accepted
    as a documented alias so a prompt or provider that states either spelling
    still produces typed references. Rows without a usable name are dropped; an
    absent/unknown kind is classified by :func:`classify_reference_kind`.
    """
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    refs: list[BtsReference] = []
    for row in value:
        if isinstance(row, BtsReference):
            refs.append(row)
            continue
        if not isinstance(row, dict):
            continue
        name = _first_text(row.get("name"), row.get("surface"))
        if not name:
            continue
        kind = row.get("kind", row.get("reference"))
        refs.append(
            BtsReference(
                kind=classify_reference_kind(name, kind),
                name=name,
                rename_to=_first_text(row.get("rename_to"), row.get("display_name")),
                member_id=_first_text(row.get("member_id")),
                reason=_first_text(row.get("reason")),
                conflict=bool(row.get("conflict")),
            )
        )
    return refs


def _first_text(*values: object) -> str:
    """First non-empty string among ``values`` (tolerant field aliases)."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def bts_references(result: object) -> list[BtsReference]:
    """Typed references carried by a BTS result (:class:`BtsResult` or its dict)."""
    if isinstance(result, BtsResult):
        return list(result.references)
    if isinstance(result, dict):
        return parse_references(result.get("references"))
    return []


def bts_tool() -> dict[str, Any]:
    """The Tier A tool declaration for the BTS pass (``report_world_state``).

    The arguments mirror the notation payload field for field — ``state`` is the
    ``{<Character>: {<category>: {<key>: <value>}}}`` nesting the parser already
    emits, and ``deltas`` is the flat fold slice (``loc``/``weather``/``tone``/
    ``inv``) — plus ``references``, the typed cast signal of W8.6.
    """
    string = {"type": "string"}
    return {
        "type": "function",
        "function": {
            "name": BTS_TOOL_NAME,
            "description": (
                "Report this turn's behind-the-scenes world state: only what changed "
                "for a delta turn, the full state for a checkpoint."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["delta", "checkpoint"],
                        "description": "delta = only what changed; checkpoint = full state.",
                    },
                    "state": {
                        "type": "object",
                        "description": (
                            "Character -> category -> attribute -> value. Categories: "
                            "physical, emotional, appearance, relationships, inventory, "
                            "stats, narrative."
                        ),
                        "additionalProperties": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "object",
                                "additionalProperties": string,
                            },
                        },
                    },
                    "deltas": {
                        "type": "object",
                        "description": (
                            "Flat fold slice. loc/weather/tone are strings; inv is a "
                            "space-separated signed list ('+rope -torch'). Never time."
                        ),
                        "properties": {
                            "loc": string,
                            "weather": string,
                            "tone": string,
                            "inv": string,
                        },
                    },
                    "venue": {
                        "type": "object",
                        "description": "Character -> {venue, area} for every present character.",
                        "additionalProperties": {
                            "type": "object",
                            "properties": {"venue": string, "area": string},
                        },
                    },
                    "off_screen": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": string,
                                "location": string,
                                "activity": string,
                                "mood": string,
                                "note": string,
                            },
                        },
                    },
                    "threads": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "status": {
                                    "type": "string",
                                    "enum": ["new", "active", "paused", "resolved"],
                                },
                                "name": string,
                                "detail": string,
                            },
                        },
                    },
                    "tension": {
                        "type": "object",
                        "properties": {"level": {"type": "integer"}, "irony": string},
                    },
                    "forecast": string,
                    "references": {
                        "type": "array",
                        "description": (
                            "One entry per person this turn introduces, names or renames. "
                            "kind=identity is a specific person the reader can pick out by "
                            "name; kind=descriptor is a role phrase that could point at "
                            "somebody else next time (never a name). Role words that are "
                            "not people (narrative, narrator, scene, story) are always "
                            "descriptors. rename_to = the person already known by name is "
                            "named differently this turn. Omit the array when nobody is "
                            "introduced or renamed."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": [REFERENCE_IDENTITY, REFERENCE_DESCRIPTOR],
                                },
                                "name": string,
                                "rename_to": string,
                                "member_id": string,
                                "reason": string,
                                "conflict": {"type": "boolean"},
                            },
                            "required": ["kind", "name"],
                        },
                    },
                },
                "required": ["kind"],
            },
        },
    }


def build_bts_request_profile(capabilities: dict[str, bool]) -> dict[str, Any] | None:
    """Tier A request profile for the BTS pass, or ``None`` (= notation path).

    ``None`` is returned when the connection cannot do function-calling: the
    runner then keeps today's free-form notation behaviour, which is the
    permanent Tier D floor (D69). Where the provider supports a FORCED
    ``tool_choice`` (measured on the local llama.cpp deployment) the tool is
    pinned with the ``"required"`` form, the only shape this build enforces as
    a grammar (W9.14); the reply is then the structured payload, never prose.
    """
    if not capabilities.get("function_calling"):
        return None
    profile: dict[str, Any] = {"tools": [bts_tool()]}
    # W9.14 (T1b measured): this llama.cpp build ENFORCES "required" as a
    # grammar, but treats {"type":"function","function":{"name":...}} as a
    # hint — the model complied only ~1/3 of the time (9 live turns, identical
    # prompt: 6 notation / 3 tool calls). The profile declares exactly ONE
    # tool, so "required" is unambiguous and costs nothing.
    if capabilities.get("tool_choice_forced"):
        profile["tool_choice"] = "required"
    else:
        profile["tool_choice"] = "auto"
    return profile


def parse_bts_structured(payload: dict[str, Any]) -> BtsResult:
    """Map a validated structured BTS payload into the canonical :class:`BtsResult`.

    This is the JSON schema authority (the boundary in :mod:`fabulist.state.structured`
    only guarantees "a usable JSON object"). Tolerant by contract, like
    :func:`parse_bts`: unknown categories bucket under ``other``, malformed lanes
    are dropped, and a payload carrying no recognised field at all raises
    :class:`BtsParseError` so the caller's repair ladder sees it as malformed.

    ``deltas`` is restricted to the four fold keys the notation path can write
    (``loc``/``weather``/``tone``/``inv``) and ``time`` is dropped — the clock owns
    time (D3) — so the ``state-delta`` payload stays byte-compatible with the
    notation path.
    """
    known = {
        "kind",
        "state",
        "deltas",
        "venue",
        "off_screen",
        "threads",
        "tension",
        "forecast",
        "references",
    }
    if not isinstance(payload, dict) or not (known & set(payload)):
        raise BtsParseError(
            f"structured BTS payload carries no recognised field: {payload!r}"
        )
    state = _structured_state(payload.get("state"))
    venue = _structured_venue(payload.get("venue"))
    deltas = _structured_deltas(payload.get("deltas"))
    for key, value in fold_flat(state, "", venue).items():
        deltas.setdefault(key, value)
    return BtsResult(
        kind="checkpoint" if str(payload.get("kind") or "").strip().lower() == "checkpoint" else "delta",
        state=state,
        deltas=deltas,
        venue=venue,
        off_screen=_structured_off_screen(payload.get("off_screen")),
        threads=_structured_threads(payload.get("threads")),
        tension=_structured_tension(payload.get("tension")),
        forecast=_first_text(payload.get("forecast")),
        raw=json.dumps(payload, ensure_ascii=False),
        references=tuple(parse_references(payload.get("references"))),
    )


#: The environment slice the flat fold consumes; the JSON path accepts no key the
#: notation path could not write (payload byte-compatibility).
_FOLD_KEYS = ("loc", "weather", "tone", "inv")


def _attribute_text(value: object) -> str:
    """Attribute value as the notation parser would have stored it (string)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _structured_state(value: object) -> dict[str, dict[str, dict[str, str]]]:
    """``{character: {category: {key: value}}}``; a flat body is bucketed by key."""
    state: dict[str, dict[str, dict[str, str]]] = {}
    if not isinstance(value, dict):
        return state
    for raw_name, body in value.items():
        name = str(raw_name).strip()
        if not name or not isinstance(body, dict):
            continue
        entry = state.setdefault(name, {})
        for raw_key, raw_value in body.items():
            if isinstance(raw_value, dict):
                category = str(raw_key).strip() or "other"
                attrs = entry.setdefault(category, {})
                for key, val in raw_value.items():
                    text = _attribute_text(val)
                    if text:
                        attrs[str(key).strip()] = text
                continue
            text = _attribute_text(raw_value)
            key = str(raw_key).strip()
            if key and text:
                entry.setdefault(_category_for(key), {})[key] = text
        if not entry:
            state.pop(name, None)
    return state


def _structured_venue(value: object) -> dict[str, dict[str, str]]:
    """``{character: {venue, area}}``; a plain string or ``@a > b`` form is accepted."""
    venue: dict[str, dict[str, str]] = {}
    if not isinstance(value, dict):
        return venue
    for raw_name, body in value.items():
        name = str(raw_name).strip()
        if not name:
            continue
        if isinstance(body, str):
            body = {"venue": body}
        if not isinstance(body, dict):
            continue
        raw = _first_text(body.get("venue"))
        area = _first_text(body.get("area"))
        if not raw:
            raw, area = area, ""
        place, _, tail = raw.partition(">")
        place = place.lstrip("@").strip()
        area = tail.lstrip("@").strip() or area.lstrip("@").strip()
        if not place:
            continue
        row = {"venue": place}
        if area:
            row["area"] = area
        venue[name] = row
    return venue


def _structured_off_screen(value: object) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(value, (list, tuple)):
        return rows
    for row in value:
        if not isinstance(row, dict):
            continue
        name = _first_text(row.get("name"))
        if not name:
            continue
        entry: dict[str, str] = {"name": name}
        for key in ("location", "activity", "mood", "note"):
            text = _first_text(row.get(key))
            if text:
                entry[key] = text.lstrip("@").strip() if key == "location" else text
        rows.append(entry)
    return rows


def _structured_threads(value: object) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(value, (list, tuple)):
        return rows
    for row in value:
        if not isinstance(row, dict):
            continue
        name = _first_text(row.get("name"))
        if not name:
            continue
        status = _first_text(row.get("status")).lower()
        rows.append(
            {
                "status": status if status in _THREAD_STATUS.values() else "active",
                "name": name,
                "detail": _first_text(row.get("detail")),
            }
        )
    return rows


def _structured_tension(value: object) -> dict[str, Any]:
    tension: dict[str, Any] = {}
    if not isinstance(value, dict):
        return tension
    level = value.get("level")
    if isinstance(level, bool):
        level = None
    if isinstance(level, int):
        tension["level"] = level
    elif isinstance(level, str) and level.strip():
        try:
            tension["level"] = int(level.strip().split("→")[-1])
        except ValueError:
            tension["level"] = level.strip()
    irony = _first_text(value.get("irony"))
    if irony:
        tension["irony"] = irony
    return tension


def _structured_deltas(value: object) -> dict[str, str]:
    """The flat fold slice, restricted to :data:`_FOLD_KEYS`; ``time`` never passes."""
    deltas: dict[str, str] = {}
    if isinstance(value, dict):
        for key in _FOLD_KEYS:
            text = _first_text(value.get(key))
            if text:
                deltas[key] = text
    return deltas


# -- parsing -----------------------------------------------------------------

_BLOCK_RE = re.compile(r"\[BTS(\s*\|\s*CP)?\s*\](.*?)\[/BTS\]", re.DOTALL | re.IGNORECASE)
_HEADER_RE = re.compile(
    r"^\s*::\s*(?P<name>.+?)\s*::\s*(?P<reentry>\[RE-ENTRY\])?\s*$"
)
# A fenced line (``` or ```yaml) is stripped so a fenced block parses like a bare one.
_FENCE_LINE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*$", re.MULTILINE)
_WEATHER_RE = re.compile(r"\bweather\s*:\s*([^|\n]+)", re.IGNORECASE)
_TONE_RE = re.compile(r"\btone\s*:\s*([^|\n]+)", re.IGNORECASE)
#: Optional ``forecast: <descriptor>`` (plausible +6h weather outlook) beside
#: ``weather:``. Tolerant by contract: the value must open on the SAME line as
#: the key (``[ \t]*``, not ``\s*`` — a newline never bleeds into the value),
#: so absent, empty, or continued-onto-the-next-line forms all yield ``""`` and
#: the HUD's fallback ladder simply sees no forecast this turn.
_FORECAST_RE = re.compile(r"\bforecast\s*:[ \t]*([^\n|]+)", re.IGNORECASE)
_LOC_ATTR_RE = re.compile(r"\bloc\s*:\s*([^|\n]+)", re.IGNORECASE)
_MOVE_RE = re.compile(r"@\s*([^@|>\n]+?)\s*→\s*@\s*([^@|>\n]+)")
_VENUE_RE = re.compile(r"@\s*([A-Za-z0-9_'’\- ]+?)(?:\s*>\s*([A-Za-z0-9_'’\- ]+?))?\s*(?:[|(]|$)")
_INV_ATTR_RE = re.compile(r"\binv(?:entory)?\s*:\s*([^|\n]+)", re.IGNORECASE)
_INV_TOKEN_RE = re.compile(r"(?<![\w])([+-])([A-Za-z][\w'\-]*)")
_INV_ITEM_RE = re.compile(r"^\s*([+-])\s*(.+?)\s*$")
_INV_PAREN_RE = re.compile(r"\s*\([^)]*\)")
#: A bare ``+name | detail`` line is a plot thread written without the
#: ``Threads:`` label (live-observed model drift); route it to threads.
_BARE_THREAD_RE = re.compile(r"^\s*([+~=\-])\s*([A-Za-z0-9_'’\-]+)\s*\|\s*(.+)$")
_THREAD_TOKEN_RE = re.compile(r"^\s*([+~=\-])\s*([A-Za-z0-9_'’\- ]+?)\s*(?:\((.*?)\))?\s*$")
_TENSION_RE = re.compile(r"\btension\s*:\s*([0-9]+(?:\s*→\s*[0-9]+)?)\s*/\s*10", re.IGNORECASE)
_IRONY_RE = re.compile(r"\birony\s*:\s*(.+)", re.IGNORECASE)
_MOODLET_RE = re.compile(r"\[([^\]]*)\]")

#: Turn-level environment keys excluded from the per-character nested state.
_ENV_KEYS = frozenset({"loc", "location", "weather", "tone", "forecast"})

#: key -> category for the dashboard's nested state (best-effort; unknown keys
#: land under ``other`` so nothing is silently dropped).
_CATEGORY_KEYS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "physical",
        frozenset(
            {
                "hp", "health", "wound", "wounds", "injury", "injuries", "status",
                "fatigue", "energy", "hygiene", "hunger", "satiety", "thirst",
                "pose", "position", "comfort", "condition_physical",
            }
        ),
    ),
    (
        "emotional",
        frozenset(
            {
                "mood", "stress", "focus", "clarity", "fear", "anxiety", "arousal",
                "moodlets", "moodlet", "interest",
            }
        ),
    ),
    (
        "appearance",
        frozenset(
            {
                "outfit", "outfit_cat", "appearance", "clothing", "hair", "makeup",
                "scent", "condition", "wearing", "accessories",
            }
        ),
    ),
    (
        "inventory",
        frozenset(
            {
                "carrying", "inventory", "inv", "currency", "gold", "items",
                "equipment", "weapon", "consumables",
            }
        ),
    ),
    (
        "stats",
        frozenset(
            {
                "str", "dex", "con", "int", "wis", "cha", "skill", "skills", "level",
                "xp", "mana", "stamina", "ability", "abilities", "status_effects",
            }
        ),
    ),
    (
        "narrative",
        frozenset(
            {
                "thread", "threads", "quest", "flag", "flags", "tension", "irony",
                "goal", "scene", "arc", "foreshadow",
            }
        ),
    ),
)


def _category_for(key: str) -> str:
    """Category bucket for an attribute key (``other`` when unknown)."""
    lowered = key.strip().lower()
    if lowered.startswith("rel") or "→" in key or "->" in key:
        return "relationships"
    for category, keys in _CATEGORY_KEYS:
        if lowered in keys:
            return category
    return "other"


def _split_attrs(line: str) -> list[str]:
    """Split a character body line on ``|`` while keeping bare fragments attached.

    Checkpoint relationships are written ``rel→User: trust 38 | friend 65`` —
    the ``| friend 65`` fragment has no ``:`` and belongs to the relationship it
    follows, not a new attribute. Fragments without ``:`` are therefore merged
    back onto the preceding attribute.
    """
    parts: list[str] = []
    for raw in line.split("|"):
        chunk = raw.strip()
        if not chunk:
            continue
        # A bare signed token is a discrete inventory delta; a colon-bearing
        # chunk is a discrete attribute. Only a plain fragment (a relationship
        # metric like ``trust 72``) merges onto the preceding attribute.
        if ":" in chunk or not parts or _INV_ITEM_RE.match(chunk):
            parts.append(chunk)
        else:
            parts[-1] = f"{parts[-1]} | {chunk}"
    return parts


def _key_value(chunk: str) -> tuple[str, str] | None:
    """Split one attribute chunk into ``(key, value)`` on the first ``:``."""
    if ":" not in chunk:
        return None
    key, value = chunk.split(":", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    return key, value


def parse_bts(text: str) -> BtsResult:
    """Parse one BTS notation block into a :class:`BtsResult`.

    Raises :class:`BtsParseError` when no ``[BTS]``/``[BTS|CP]`` … ``[/BTS]``
    block is present. Within a block the parser is tolerant: unknown keys are
    preserved under the ``other`` category, malformed relationship fragments are
    folded onto their parent, and missing optional sections simply yield empty
    structures. This mirrors the extractor's tolerant-parse contract so the
    caller's repair retry handles drift uniformly.
    """
    cleaned = _FENCE_LINE.sub("", text)
    match = _BLOCK_RE.search(cleaned)
    if match is None:
        raise BtsParseError(f"no [BTS] block found in output: {text!r}")
    kind = "checkpoint" if match.group(1) else "delta"
    body = match.group(2)

    state: dict[str, dict[str, dict[str, str]]] = {}
    venue: dict[str, dict[str, str]] = {}
    off_screen: list[dict[str, str]] = []
    threads: list[dict[str, str]] = []
    tension: dict[str, Any] = {}
    reference_rows: list[dict[str, Any]] = []

    current: str | None = None
    in_off = False
    inv_tokens: dict[str, list[str]] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header = _HEADER_RE.match(line)
        if header:
            name = header.group("name").strip()
            if name.upper() == "OFF":
                current = None
                in_off = True
            else:
                if header.group("reentry") and name in state:
                    state.setdefault(name, {})
                current = name
                in_off = False
                state.setdefault(name, {})
            continue
        lowered = line.lower()
        if lowered.startswith("threads:"):
            threads.extend(_parse_threads(line.split(":", 1)[1]))
            continue
        if lowered.startswith("references:"):
            row = _parse_reference_line(line.split(":", 1)[1])
            if row is not None:
                reference_rows.append(row)
            continue
        if lowered.startswith("tension:"):
            _apply_tension(line, tension)
            continue
        if in_off:
            entry = _parse_off_screen(line)
            if entry:
                off_screen.append(entry)
            continue
        if current is None:
            # Top-level environment lines outside a character block.
            continue
        for chunk in _split_attrs(line):
            kv = _key_value(chunk)
            if kv is None:
                # A bare ``+name | detail`` is a label-less plot thread; a bare
                # signed token is an inventory delta (+item / -item).
                bare_thread = _BARE_THREAD_RE.match(chunk)
                if bare_thread:
                    threads.append(
                        {
                            "status": _THREAD_STATUS.get(bare_thread.group(1), "active"),
                            "name": bare_thread.group(2),
                            "detail": bare_thread.group(3).strip(),
                        }
                    )
                    continue
                item = _INV_ITEM_RE.match(chunk)
                if item:
                    token = _INV_PAREN_RE.sub("", f"{item.group(1)}{item.group(2)}").strip()
                    inv_tokens.setdefault(current, []).append(token)
                continue
            key, value = kv
            if key.lower() in _ENV_KEYS:
                # Environment slice (loc/weather/tone) rides the flat fold only;
                # ``forecast`` rides its own result field. All are turn-level,
                # not per-character attributes.
                continue
            category = _category_for(key)
            state[current].setdefault(category, {})[key] = value

    venue = _collect_venues(body, state)
    flat = fold_flat(state, body, venue, inv_tokens=inv_tokens)
    forecast_match = _FORECAST_RE.search(body)
    forecast = forecast_match.group(1).strip() if forecast_match else ""
    return BtsResult(
        kind=kind,
        state=state,
        deltas=flat,
        venue=venue,
        off_screen=off_screen,
        threads=threads,
        tension=tension,
        forecast=forecast,
        raw=match.group(0),
        references=tuple(parse_references(reference_rows)),
    )


#: Thread status symbol -> name (shared by the labeled and bare forms).
_THREAD_STATUS = {"+": "new", "~": "active", "=": "paused", "-": "resolved"}

#: ``references:`` line fields beyond ``kind | name``.
_REFERENCE_FIELD_RE = re.compile(
    r"^(rename_to|reason|member_id|conflict)\s*:\s*(.*)$", re.IGNORECASE
)


def _parse_reference_line(text: str) -> dict[str, Any] | None:
    """One ``references: kind | name | rename_to: X | reason: Y`` notation line.

    The notation transport carries the same typed signal as the structured
    contract, so a Tier D reply is not silently discarded; the row is handed to
    :func:`parse_references`, which owns the field aliases and classification.
    """
    parts = [part.strip() for part in text.split("|") if part.strip()]
    if len(parts) < 2:
        return None
    row: dict[str, Any] = {"kind": parts[0], "name": parts[1]}
    for part in parts[2:]:
        match = _REFERENCE_FIELD_RE.match(part)
        if match is None:
            continue
        field, value = match.group(1).lower(), match.group(2).strip()
        row[field] = value.lower() in ("yes", "true", "1") if field == "conflict" else value
    return row


def _parse_threads(text: str) -> list[dict[str, str]]:
    """Parse ``Threads: +a | ~b(40%) | =c | -d`` into structured rows."""
    status_map = _THREAD_STATUS
    rows: list[dict[str, str]] = []
    for chunk in text.split("|"):
        token = chunk.strip()
        if not token:
            continue
        match = _THREAD_TOKEN_RE.match(token)
        if match is None:
            continue
        symbol, name, detail = match.groups()
        rows.append(
            {
                "status": status_map.get(symbol, "active"),
                "name": name.strip(),
                "detail": (detail or "").strip(),
            }
        )
    return rows


def _apply_tension(line: str, tension: dict[str, Any]) -> None:
    """Fill ``tension`` from a ``Tension: N/10 | irony: ...`` line."""
    match = _TENSION_RE.search(line)
    if match:
        level = match.group(1)
        try:
            tension["level"] = int(level.split("→")[-1].strip())
        except ValueError:
            tension["level"] = level
    irony = _IRONY_RE.search(line.split("|", 1)[-1] if "|" in line else line)
    if irony:
        tension["irony"] = irony.group(1).strip()


def _parse_off_screen(line: str) -> dict[str, str] | None:
    """Parse one ``Name | @loc(activity) | mood | note`` off-screen row."""
    parts = [part.strip() for part in line.split("|")]
    if not parts or not parts[0]:
        return None
    entry: dict[str, str] = {"name": parts[0]}
    for part in parts[1:]:
        venue = _VENUE_RE.search(part)
        if venue and "location" not in entry:
            entry["location"] = venue.group(1).strip()
            paren = re.search(r"\(([^)]*)\)", part)
            if paren:
                entry["activity"] = paren.group(1).strip()
        elif part.lower().startswith("mood:"):
            entry["mood"] = part.split(":", 1)[1].strip()
        elif part:
            entry.setdefault("note", part)
    return entry


def _collect_venues(
    body: str, state: dict[str, dict[str, dict[str, str]]]
) -> dict[str, dict[str, str]]:
    """Map character -> ``{venue, area}`` from their ``@...`` lines."""
    venue: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        header = _HEADER_RE.match(line)
        if header:
            name = header.group("name").strip()
            current = None if name.upper() == "OFF" else name
            continue
        if current is None:
            continue
        found = _VENUE_RE.search(line)
        if not found:
            continue
        place = found.group(1).strip()
        if place in ("", "@"):
            continue
        row = {"venue": place}
        if found.group(2):
            row["area"] = found.group(2).strip()
        venue[current] = row
    return venue


def _scene_location(body: str, venue: dict[str, dict[str, str]]) -> str | None:
    """Best-effort scene location: movement target, else the most common venue."""
    moves = _MOVE_RE.findall(body)
    if moves:
        return moves[-1][1].strip()
    if not venue:
        return None
    counts: dict[str, int] = {}
    for row in venue.values():
        place = row.get("venue") or ""
        if place:
            counts[place] = counts.get(place, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def fold_flat(
    state: dict[str, dict[str, dict[str, str]]],
    body: str = "",
    venue: dict[str, dict[str, str]] | None = None,
    inv_tokens: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """Derive the flat fold projection (``loc``/``weather``/``tone``/``inv``).

    These are the literal keys the existing projector/facts/theme consumers read
    (D21/B4); BTS writes them so nothing downstream changes. ``time`` is
    deliberately never emitted — the rules clock owns it (D3).
    """
    flat: dict[str, str] = {}

    loc_attr = _LOC_ATTR_RE.search(body)
    if loc_attr:
        flat["loc"] = loc_attr.group(1).strip()
    else:
        scene = _scene_location(body, venue or {})
        if scene:
            flat["loc"] = scene

    weather = _WEATHER_RE.search(body)
    if weather:
        flat["weather"] = weather.group(1).strip()

    tone = _TONE_RE.search(body)
    if tone:
        flat["tone"] = tone.group(1).strip()

    inv: list[str] = []
    inv_attr = _INV_ATTR_RE.search(body)
    if inv_attr:
        inv.extend(inv_attr.group(1).split())
    else:
        for tokens in (inv_tokens or {}).values():
            inv.extend(tokens)
        for character in state.values():
            for key, value in character.get("inventory", {}).items():
                if key in ("carrying", "inv", "inventory", "items"):
                    for match in _INV_TOKEN_RE.finditer(value):
                        inv.append(f"{match.group(1)}{match.group(2)}")
    if inv:
        # De-dup while preserving order; never emit a bare '+'/'−'.
        flat["inv"] = " ".join(dict.fromkeys(token for token in inv if len(token) > 1))
    return flat


# -- prompt rendering --------------------------------------------------------


def _render_state_pairs(state: dict[str, Any]) -> str:
    """Declarative prior-state text for the BTS prompt (structured -> lines)."""
    lines: list[str] = []
    for character, categories in (state or {}).items():
        if not isinstance(categories, dict):
            continue
        lines.append(f":: {character} ::")
        for category, attrs in categories.items():
            if not isinstance(attrs, dict):
                continue
            for key, value in attrs.items():
                lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def build_bts_messages(
    user_msg: str,
    narrative: str,
    prior_state: dict | None = None,
    *,
    checkpoint: bool = False,
) -> list[dict[str, str]]:
    """Default two-message BTS request (the ``bts`` flow's byte shape).

    W9.29 §6: the node pushes no prior state any more — the entity registry IS the
    prior state and it rides the runner's tail (so it can never go stale and a
    preset flow cannot drop it). The ``PRIOR WORLD-STATE`` block therefore renders
    only when a caller DOES hand one in (a legacy producer or the notation-era
    read shims); ``prior_state=None`` omits the block instead of claiming there is
    nothing recorded, which the registry beside it would contradict.
    """
    prior = _render_state_pairs(prior_state or {})
    mode = "checkpoint" if checkpoint else "delta"
    sections = [f"MODE: {mode}"]
    if prior:
        sections.append(f"PRIOR WORLD-STATE:\n{prior}")
    sections.append(f"USER MESSAGE:\n{user_msg}")
    sections.append(f"NARRATIVE:\n{narrative}")
    payload = "\n\n".join(sections)
    return [
        {"role": "system", "content": BTS_SYSTEM_PROMPT},
        {"role": "user", "content": payload},
    ]


def render_bts_state(state: dict[str, Any]) -> str:
    """Render a structured world-state dict back to compact notation-ish text."""
    return _render_state_pairs(state)


# -- checkpoint cadence ------------------------------------------------------


def should_checkpoint(
    *,
    turns_since_checkpoint: int,
    scene_changed: bool = False,
    requested: bool = False,
    threshold: int = BTS_CHECKPOINT_TURNS,
) -> bool:
    """D21 cadence: checkpoint on ~N turns, a scene change, or an explicit ask."""
    return bool(requested or scene_changed or turns_since_checkpoint >= threshold)


#: Explicit OOC checkpoint request (``[OOC: BTS checkpoint]``).
_CHECKPOINT_REQUEST_RE = re.compile(
    r"\[\s*OOC\s*:\s*BTS\s+checkpoint\s*\]", re.IGNORECASE
)


def checkpoint_requested(user_msg: str) -> bool:
    """True when the user explicitly asked for a BTS checkpoint."""
    return bool(_CHECKPOINT_REQUEST_RE.search(user_msg or ""))


# -- fork-aware log reads ----------------------------------------------------


def _resolve_events(log: Any, session_id: str) -> list[Any]:
    """Fork-resolved raw events for ``session_id`` (mirrors cast_api's walk).

    A branch copies no events, so ``get_events`` is session-local; walking the
    ``base_session`` chain root-first (each ancestor bounded by its child's
    ``base_seq``) replays inherited world-state byte-for-byte.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = session_id
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        info = log.get_session(current)
        current = info["base_session"] if info else None
    chain.reverse()

    events: list[Any] = []
    for index, sid in enumerate(chain):
        bound: int | None = None
        if index + 1 < len(chain):
            child = log.get_session(chain[index + 1])
            bound = child["base_seq"] if child else None
        for event in log.get_events(sid):
            if bound is not None and event.seq > bound:
                continue
            events.append(event)
    return events


def bts_name_set(result: dict[str, Any] | None) -> list[str]:
    """Every character name a BTS result mentions, in first-appearance order.

    The admit loop (W7) consumes this: the ``:: Name ::`` headers key ``state``,
    a present character carries a ``venue`` entry, and off-screen characters are
    the ``:: OFF ::`` lane's ``name`` field. All three are the *same* name space
    the model writes, so admitting them through ``admit_member`` is what gives a
    BTS-learned character a canonical id (and a colour) through the card path
    rather than a fresh string every turn. Malformed lanes are skipped rather
    than raising — BTS output is model text.
    """
    names: list[str] = []

    def add(value: object) -> None:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in names:
            names.append(cleaned)

    if not isinstance(result, dict):
        return names
    state = result.get("state")
    if isinstance(state, dict):
        for name in state:
            add(name)
    venue = result.get("venue")
    if isinstance(venue, dict):
        for name in venue:
            add(name)
    off_screen = result.get("off_screen")
    if isinstance(off_screen, list):
        for row in off_screen:
            if isinstance(row, dict):
                add(row.get("name"))
    return names


#: Surfaces whose member counts as present/participating this turn: the character
#: headers (``state`` keys) and the ``venue`` holders. Off-screen characters are
#: deliberately excluded — ADDENDUM 3 Q2 makes PRESENCE the descriptor boundary.
def _present_names(result: object) -> list[str]:
    if not isinstance(result, dict):
        return []
    names: list[str] = []
    for key in ("state", "venue"):
        lane = result.get(key)
        if isinstance(lane, dict):
            for name in lane:
                cleaned = str(name).strip()
                if cleaned and cleaned not in names:
                    names.append(cleaned)
    return names


def _player_keys(log: Any, session_id: str) -> frozenset[str]:
    """Canonical keys that name the PLAYER, never a cast member.

    :mod:`fabulist.state.presence` subtracts the chat's persona on the READ side
    (the player never owns a lane); this is the same subtraction at the admit
    DOOR, because the read-side subtraction alone left the member in the roster
    eating a cast-cap slot. A live session admitted "Wanderer" — the
    player's own persona — through a ``references: identity`` entry on turn 1.

    The persona read is :func:`fabulist.state.presence._player_name`, the ONE
    resolution of "who is the player" (imported rather than re-derived; the same
    module already owns the surface list this door extends).
    """
    from .presence import PLAYER_SURFACES, _player_name

    keys = {str(surface) for surface in PLAYER_SURFACES}
    name = _player_name(log, session_id)
    if name:
        key = _canonical(name)
        if key:
            keys.add(key)
    return frozenset(keys)


def _is_player_surface(surface: object, player_keys: frozenset[str]) -> bool:
    """True when a surface is the player (``surface`` is a canonical key there)."""
    return _canonical(str(surface or "")) in player_keys


def user_as_actor(log: Any, session_id: str) -> bool:
    """Is the player a cast ACTOR in this chat (W9.22 §2, default OFF)?

    The one setting that opens the three player doors: what is refused as "not a
    cast member" (the persona name, ``User``/``Player``/``you``) becomes an
    actor of its own, and what the engine used to tombstone as its own mistake
    stays alive. OFF is today's behaviour exactly, so the default direction is
    the safe one.

    The storage and the fold belong to the chat-config router (one latest-wins
    ``system``/``chat_user_as_actor`` row per write, absent = OFF — lane 1c);
    this is the in-engine READ seam, imported lazily for the two reasons the
    module documents for every cross-layer read here: no import cycle
    (``api.chat_config_api`` pulls fastapi + the API package) and one truthful
    source instead of a second fold that could disagree. An unreadable setting
    (or a router that is not loaded at all) reads OFF — impersonation must never
    be enabled by an accident of wiring.
    """
    try:
        from ..api.chat_config_api import resolve_user_as_actor

        return bool(resolve_user_as_actor(log, session_id))
    except Exception:  # noqa: BLE001 - an unreadable setting is OFF (fail-safe)
        return False


def _admit_user_member(log: Any, session_id: str, surface: str) -> str | None:
    """Admit the ONE ``user`` member this session's player surfaces resolve to.

    User-as-Actor ON (W9.22 §2): the player is actor tier — a first-class
    presence and a lane — so the admit door must produce exactly one member for
    them, whatever spelling the tracker used this turn (``User``, ``Player``, the
    persona name). Two decisions make that hold:

    * the member is keyed on the chat's PERSONA name
      (:func:`fabulist.state.presence._player_name`, the one resolution of "who
      is the player"), falling back to the reported surface when the chat has no
      persona — and, with no persona, to the member already bearing the ``user``
      provenance. The surface is re-resolved every turn and never becomes the
      identity, so ``User`` on turn 1 and ``Player`` on turn 2 are one member
      rather than two (the generic player words are synonyms for one person);
    * the display name is that same persona name, which is what keeps the
      read-side ROLE TINT intact: ``_assign_speaker_colors`` (graph/nodes.py)
      reserves the near-white for the persona by name and skips that speaker's
      own rows, so the user actor never takes a palette hue. A member carrying a
      generic ``User`` label would have been coloured like an NPC.

    The tier is set as a PROVENANCE PATCH (the cast layer's in-place path, the
    same event the promotion gate writes) rather than a second admit: a member
    the engine already minted for the player — the W9.21 ``Wanderer`` junk member,
    or one the pre-ON retire door tombstoned — is the SAME person, so it is
    re-tiered and resurrected instead of duplicated. Best-effort like every other
    door here: ``None`` when the admit is refused.
    """
    from .cast import admit_member, member_by_id, member_by_name, patch_member, roster
    from .presence import _player_name

    persona_name = (_player_name(log, session_id) or "").strip()
    name = (persona_name or str(surface or "")).strip()
    if not name:
        return None
    try:
        members = roster(log, session_id)
        existing = member_by_name(members, name)
        if existing is None and not persona_name:
            # No persona to name the actor, so the ONE ``user`` member is the
            # anchor: whichever generic player word arrives next (User/Player/
            # you) resolves to it instead of minting its own member.
            existing = next(
                (member for member in members if member.provenance == "user"), None
            )
        member_id = (
            existing.member_id
            if existing is not None
            else admit_member(
                log, session_id, name, provenance="user", admitted_by="user"
            )
        )
        member = member_by_id(roster(log, session_id), member_id)
        if member is not None and member.provenance != "user":
            patch_member(
                log,
                session_id,
                member_id,
                provenance="user",
                reason="User-as-Actor is ON for this session",
            )
    except Exception:  # noqa: BLE001 - admit is best-effort (D9)
        return None
    return member_id


def _emit_cast_proposal(
    log: Any, session_id: str, name: str, aliases: Sequence[str] = (), origin: str = "bts"
) -> bool:
    """File ONE ``cast`` proposal for a person the owner has not accepted.

    Cast admission is USER-GATED (owner ruling, reversing D76's auto-promotion):
    a person the tracker detects is NOT a cast member until the owner accepts the
    proposal the review home shows her. This is the whole door — the detection
    that used to end in :func:`fabulist.state.cast.admit_member` now ends here.

    The emitter is Lane L4's pinned ``state.proposals.emit_cast_proposal``
    (``session_id, name, aliases, origin``), imported lazily for the two reasons
    this module imports every cross-layer seam lazily (no import cycle, one
    truthful implementation). Until that lane lands the import fails and the
    door degrades to "detected, filed nothing" rather than failing the turn —
    the same best-effort contract as every other door here (D9).
    """
    try:
        from .proposals import emit_cast_proposal
    except ImportError:  # pragma: no cover - the L4 lane owns this symbol
        logger.warning(
            "cast proposals are unavailable: %r was not filed (%s)", name, origin
        )
        return False
    try:
        emit_cast_proposal(log, session_id, name, list(aliases), origin)
    except Exception:  # noqa: BLE001 - a refused filing is not the turn's problem
        logger.warning("could not file a cast proposal for %r", name, exc_info=True)
        return False
    return True


def apply_bts_cast_proposals(
    log: Any, session_id: str, result: object
) -> list[BtsReference]:
    """Wire a BTS result's typed references into :mod:`fabulist.state.cast` (W8.6).

    The single producer path for cast identity from BTS, best-effort by contract
    (a refused proposal never fails the turn):

    * ``reference_kind=identity`` -> :func:`_resolve_or_propose_identity`: an
      ACCEPTED member the surface resolves to is used as-is (and, when the entry
      carries ``rename_to``, renamed by
      :func:`~fabulist.state.cast.patch_member`, so a person introduced as "the
      woman" and later named keeps one ``member_id``, one colour and its whole
      attribution history, ADDENDUM 1 A2); a person nobody has accepted yet is
      filed as a ``cast`` proposal and NOT admitted — cast admission is
      user-gated.
    * ``reference_kind=descriptor`` -> the scene/presence-scoped path
      (``presence`` = this turn's present members, ``conflict`` from the typed
      "a different woman" signal). It is never auto-aliased; ambiguity SPLITS
      (ADDENDUM 2/3). A ``rename_to`` on a descriptor entry patches the member
      that admit resolved to, exactly as on an identity entry (ADDENDUM 1 A12).
    * A determiner phrase is typed ``descriptor`` by
      :func:`classify_reference_kind` whatever the model claimed, EXCEPT when
      exactly one live member already carries that surface as its display name
      or an alias: then it is admitted as that identity
      (:func:`_keep_roster_surfaces_as_identities`), so a real name like "The
      Nameless God" and a rename-demoted alias keep resolving to their member.
    * Every name the result mentions is still SEEN — the typed entries select the
      path, they do not shrink the roster. A name with no reference entry takes
      the same identity door (resolve to an accepted member, else propose).
    * A role word that reaches the descriptor path retires the member the engine
      itself minted for it (:func:`_retire_non_character_member`) and is not
      re-admitted, so a section word can never sit in the cast as a person. The
      same door refuses the PLAYER's persona name (:func:`_player_keys`) — a
      state header of ``Wanderer`` (the user's character) is not a cast member,
      and neither is a phrase whose ``rename_to`` is the player.
      A card-captured or renamed character is never touched.
    * **User-as-Actor ON** (:func:`user_as_actor`, default OFF) reopens exactly
      that player door: a player surface is admitted as the ONE ``user`` member
      (:func:`_admit_user_member`, provenance ``user`` = actor tier) instead of
      being refused, and it never reaches the retire-tombstone path — the member
      carrying the persona's name IS the player, so there is no engine mistake to
      undo. OFF is the pre-W9.22 behaviour event for event.
    * ``references`` absent or empty -> the legacy :func:`bts_name_set` identity
      pass through the same door (:func:`_admit_legacy_names`); the payload
      differences of the pre-W8.6 contract are preserved, the ADMISSION policy is
      not forked.

    Returns the references that were applied (empty on the legacy path).
    """
    user_actor = user_as_actor(log, session_id)
    if user_actor:
        # Requirement 4 ("first-class presence (cast bar entry, world-state
        # bucket, colour)"): with User-as-Actor ON the user's actor exists even
        # on a turn where the tracker never names the player — the toggle alone
        # must populate the cast. Lazy admission would leave the setting
        # looking dead in any session whose BTS never spells the persona out.
        try:
            _admit_user_member(log, session_id, "User")
        except Exception:  # noqa: BLE001 - the door is best-effort like the rest
            pass
    refs = bts_references(result)
    if not refs:
        _admit_legacy_names(log, session_id, result, user_actor=user_actor)
        _enforce_cap(log, session_id)
        return []
    from .cast import admit_member, member_by_id, patch_member, roster

    refs = _keep_roster_surfaces_as_identities(
        log, session_id, refs, roster(log, session_id)
    )
    player_keys = _player_keys(log, session_id)

    by_key: dict[str, BtsReference] = {}
    for ref in refs:
        by_key.setdefault(_canonical(ref.name), ref)

    surfaces = list(bts_name_set(result))
    for ref in refs:
        if ref.name not in surfaces:
            surfaces.append(ref.name)

    admitted: dict[str, str | None] = {}
    for surface in surfaces:
        if user_actor and _is_player_surface(surface, player_keys):
            # User-as-Actor ON: the player is ACTOR tier, so the door admits
            # (ONE member, keyed on the persona) instead of refusing. It must
            # short-circuit BEFORE the retire door: that helper exists to undo
            # the engine's own junk player member, and with the setting ON that
            # member is the actor we want to keep.
            admitted[surface] = _admit_user_member(log, session_id, surface)
            continue
        ref = by_key.get(_canonical(surface))
        if ref is not None and ref.kind == REFERENCE_DESCRIPTOR:
            continue  # descriptors resolve after presence is known
        if _retire_non_character_member(log, session_id, surface, player_keys):
            # A role word or the player: not a person, so never admitted.
            continue
        admitted[surface] = _resolve_or_propose_identity(
            log,
            session_id,
            surface,
            aliases=_sibling_surfaces(surface, surfaces),
            rename_to=_proposed_rename(ref, player_keys),
        )

    present = _present_ids(log, session_id, result, admitted)
    descriptor_keys = [
        _canonical(ref.name) for ref in refs if ref.kind == REFERENCE_DESCRIPTOR
    ]
    presence_by_key = _descriptor_presence(
        log, session_id, present, descriptor_keys, roster(log, session_id)
    )
    descriptors: dict[str, str] = {}
    seen_descriptors: set[str] = set()
    for surface in surfaces:
        ref = by_key.get(_canonical(surface))
        if ref is None or ref.kind != REFERENCE_DESCRIPTOR:
            continue
        # Several surfaces can carry one descriptor (a ``Woman`` state header and
        # the ``the woman`` reference name share a canonical key). The admit is
        # keyed by ``ref.name``, so one pass per reference is the whole job — a
        # second pass would mint a disambiguated duplicate of the member the
        # first pass just linked (it is not in ``present`` yet).
        if ref.name in seen_descriptors:
            continue
        seen_descriptors.add(ref.name)
        if ref.rename_to and _is_player_surface(ref.rename_to, player_keys):
            # The model states this phrase refers to the PLAYER: the phrase names
            # nobody in the cast, so nothing is minted and nothing is renamed.
            continue
        if _retire_non_character_member(log, session_id, ref.name, player_keys):
            # A section/lane word: the member it produced was the engine's own
            # mistake (see the helper). Retired, and NOT re-minted this turn.
            continue
        try:
            descriptors[surface] = admit_member(
                log,
                session_id,
                ref.name,
                provenance="bts",
                admitted_by="bts",
                reference_kind=REFERENCE_DESCRIPTOR,
                presence=presence_by_key.get(_canonical(ref.name), present),
                conflict=ref.conflict,
            )
        except Exception:  # noqa: BLE001 - admit is best-effort (D9)
            continue

    # A descriptor entry carries ``rename_to`` exactly like an identity entry
    # (ADDENDUM 1 A12: "the woman is Mara" confirms the handle into a name). The
    # admit above returns the RESOLVED member when the phrase already has a live
    # link or names an identity member, so the patch lands on that member — never
    # on a stray provisional the turn happened to mint. An engine-minted handle
    # is the one exception (user-gated admission: naming a handle is an admit).
    # Handles whose ``rename_to`` names a person the owner has not accepted: the
    # name is filed as ONE proposal per person, carrying every phrase the turn
    # used for them (see the gate below).
    proposed_renames: dict[str, tuple[str, list[str]]] = {}
    for surface, member_id in {**admitted, **descriptors}.items():
        ref = by_key.get(_canonical(surface))
        if member_id is None or ref is None or not ref.rename_to:
            continue
        if _canonical(ref.rename_to) == _canonical(surface):
            continue
        if _is_player_surface(ref.rename_to, player_keys):
            continue  # a member never takes the player's name (see the door above)
        if user_actor and _is_player_surface(surface, player_keys):
            # User-as-Actor ON: the player's name is the PERSONA's, never the
            # tracker's proposal — renaming the user actor because a reference
            # said "User is really Zahra" would silently rename the player.
            continue
        target = member_id
        if ref.member_id and member_by_id(roster(log, session_id), ref.member_id) is not None:
            target = ref.member_id
        if _is_engine_churn(member_by_id(roster(log, session_id), target)):
            # Cast admission is USER-GATED (owner ruling): an engine-minted
            # scene handle is not a person the owner accepted, so naming it
            # would be an admission through the rename door — live turn: the
            # ``clerk`` handle was renamed "Marisol" one row after the identity
            # door had correctly refused to admit her, which put an unaccepted
            # member (and a colour) in the cast pane. The handle keeps the scene
            # state under the phrase the prose used; the NAME goes to the owner.
            name, phrases = proposed_renames.setdefault(
                _canonical(ref.rename_to), (str(ref.rename_to).strip(), [])
            )
            if surface not in phrases:
                phrases.append(surface)
            continue
        try:
            patch_member(
                log,
                session_id,
                target,
                display_name=ref.rename_to,
                reason=ref.reason,
            )
        except Exception:  # noqa: BLE001 - a refused rename is logged ambiguous, never fatal
            continue
    for name, phrases in proposed_renames.values():
        _emit_cast_proposal(log, session_id, name, phrases)
    _enforce_cap(log, session_id)
    return refs


def _keep_roster_surfaces_as_identities(
    log: Any, session_id: str, refs: list[BtsReference], members: Sequence[CastMember]
) -> list[BtsReference]:
    """Undo the determiner coercion for a surface the roster already carries.

    :func:`classify_reference_kind` is pure, so it reads every determiner phrase
    as a descriptor. That is right for ``the soldier`` the narrator invented and
    wrong for a member the roster ALREADY holds under exactly that surface:
    "The Nameless God" is a real name, and a member demoted to an alias by a
    rename must keep resolving backward to that member (ADDENDUM 2 A9) instead
    of being re-minted as a scene handle beside it.

    The override is deliberately narrow — it is the engine MERGING where its
    default is to split, so it fires only when nothing is in doubt:

    * the reference is typed ``descriptor`` *and* determiner-shaped (a bare
      ``Zahra`` never reaches here) and is not a role word;
    * the entry carries no ``conflict`` (the typed "a different soldier"
      outranks the roster match, exactly as it outranks the identity bridge);
    * the phrase is not already spoken for: NO active member is linked to its
      descriptor key, so the descriptor path would mint this turn — a live link
      is :func:`~fabulist.state.cast.resolve_descriptor`'s case (reuse, or
      refuse-and-split), never this override's;
    * exactly ONE active member carries the surface as its display name or an
      alias — an ambiguous surface still SPLITS.

    Retired members do NOT qualify: their descriptor link is not live either
    (ADDENDUM 3 Q2 presence, plus the W9.15 cap), so resurrecting one from a
    determiner phrase would re-absorb a new person into the one the engine
    deliberately tombstoned.
    """
    from .cast import descriptor_candidates

    carriers: dict[str, list[str]] = {}
    for member in members:
        if not member.is_active:
            continue
        for surface in member.surface_forms():
            key = str(surface).strip().casefold()
            if key:
                ids = carriers.setdefault(key, [])
                if member.member_id not in ids:
                    ids.append(member.member_id)
    out: list[BtsReference] = []
    for ref in refs:
        if ref.kind != REFERENCE_DESCRIPTOR or ref.conflict:
            out.append(ref)
            continue
        if not is_determiner_phrase(ref.name) or is_role_surface(ref.name):
            out.append(ref)
            continue
        if len(carriers.get(ref.name.strip().casefold(), ())) != 1:
            out.append(ref)
            continue
        if descriptor_candidates(log, session_id, ref.name):
            out.append(ref)
            continue
        out.append(replace(ref, kind=REFERENCE_IDENTITY))
    return out


def _cap_last_seen(log: Any, session_id: str, members: Sequence[CastMember]) -> dict[str, int]:
    """The cap's recency clock: world-state presence PLUS speaker evidence.

    W9.15 ranks members by "last seen", but the only clock was the BTS presence
    walk — and BTS is delta-first, so a card character whose header the model
    stopped reproducing has NO presence evidence at all and ranks as never seen.
    A live session used that clock to retire the two CARD characters
    ("Hobbs", "Tessa") mid-scene at seq 86/117 while seven runtime members
    stayed: their names were the turn's own attribution speakers
    (``speaker``/``speaker_id`` on every ``system``/``attribution`` row), which
    is the strongest "this character is in the scene" evidence the log carries
    and which the clock ignored.

    So a member's recency is the newest of: a presence sighting of any of its
    surface forms, and any attribution row that names it — by ``member_id`` when
    the row carries one (spelling-independent), else by surface. No window: the
    cap only ORDERS evidence, so an old row simply ranks below a new one. The
    payload shape is read through :func:`fabulist.state.presence._attribution_rows`
    (the one reader of that shape, including its legacy ``rows`` shim).
    """
    from .cast import member_by_id, member_by_name
    from .presence import _attribution_rows, presence_walk

    walk = presence_walk(log, session_id)
    last_seen = {
        member.member_id: max(
            [walk.present_at.get(surface, -1) for surface in member.surface_forms()]
        )
        for member in members
    }
    for event in _resolve_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != "attribution":
            continue
        for row in _attribution_rows(event.payload):
            if not isinstance(row, dict):
                continue
            speaker_id = str(row.get("speaker_id") or "").strip()
            member = member_by_id(members, speaker_id) if speaker_id else None
            if member is None:
                member = member_by_name(members, row.get("speaker"))
            if member is None:
                continue
            if event.seq > last_seen.get(member.member_id, 0):
                last_seen[member.member_id] = event.seq
    return last_seen


def _enforce_cap(log: Any, session_id: str) -> None:
    """W9.15/W9.16: cap the ACTIVE cast at 8, LRU by appearance.

    Called on BOTH admit paths — the legacy (no references) branch returned
    early, so the cap never ran on turns whose BTS wrote no references. The
    seed-provenance pin lives with the cap itself
    (:func:`fabulist.state.cast.enforce_cast_cap`), so both callers get it.
    """
    try:
        from .cast import enforce_cast_cap, roster

        members = roster(log, session_id)
        enforce_cast_cap(
            log, session_id, last_seen=_cap_last_seen(log, session_id, members)
        )
    except Exception:  # noqa: BLE001 - housekeeping is best-effort (D9)
        pass


def _canonical(name: str) -> str:
    from .cast import canonical_key

    return canonical_key(name)


def _retire_non_character_member(
    log: Any, session_id: str, name: str, player_keys: frozenset[str]
) -> bool:
    """Refuse a surface that names nobody, retiring the engine's own mistakes.

    Two surface classes reach here — a narrator-class role word
    (:data:`ROLE_SURFACES`: "Narrative", "scene") and the chat's PLAYER persona
    (plus the generic player words of
    :data:`fabulist.state.presence.PLAYER_SURFACES`). :func:`classify_reference_kind`
    keeps a role word from entering as an IDENTITY, but the descriptor path still
    minted a scene handle for it, and every session that ran before the typing
    admitted one as an identity outright; the player was minted the same way
    (a live session admitted "Wanderer" on turn 1, and the read-side
    subtraction in ``presence`` then left a junk member eating a cap slot).

    So when a reference arrives for such a surface and a member carrying that
    canonical key was admitted by the engine DURING PLAY (``bts``/``extractor``/
    ``attribution``), the member is the engine's own mistake and is tombstoned.

    What is never touched (the guard refuses to guess):

    * a card/world seed — a deliberately created character;
    * a member any reference has NAMED (``former_names`` non-empty — the W7
      ADDENDUM 1 rename demoted the surface the role word had): being named is
      the "this is a person" signal, so a named role-word handle survives;
      the provisional flag alone is not enough, because the legacy identity
      admits this guard exists for never set it.
    * a member that is not the engine's (``admitted_by`` outside
      :data:`~fabulist.state.cast.RUNTIME_ADMITTERS`), and never a member the
      OWNER accepted from a cast proposal (``admitted_by="owner"``);

    Returns ``True`` when the surface must not be admitted this turn: the mistake
    member is now retired (or already was), so minting a fresh handle for the same
    word would only put the junk back. The two classes differ in ONE respect: a
    role word that no member carries still enters as a scene handle (a descriptor
    provisional, so the state its header carries has somewhere to sit), while the
    PLAYER must not sit in the cast at all and is refused outright.
    """
    from .cast import (
        RUNTIME_ADMITTERS,
        canonical_key,
        member_by_id,
        member_index,
        retire_member,
        roster,
    )
    from .presence import SEED_PROVENANCE

    key = canonical_key(name)
    is_role_word = key in ROLE_SURFACES
    is_player = key in player_keys
    if not (is_role_word or is_player):
        return False
    members = roster(log, session_id)
    carriers = member_index(members).get(key, [])
    if not carriers:
        return is_player
    for member_id in carriers:
        member = member_by_id(members, member_id)
        if member is None:
            continue
        if not member.is_active:
            return True  # already retired for this reason: keep it gone
        if member.provenance in SEED_PROVENANCE:
            continue  # card/world: a deliberately created character
        if member.admitted_by == "owner":
            continue  # the owner accepted this member: a decision, not a mistake
        if member.admitted_by not in RUNTIME_ADMITTERS:
            continue
        if member.former_names:
            continue  # a reference named it: never rewritten back
        reason = (
            f'"{member.display_name}" is the player, not a cast member'
            if is_player
            else f'role word "{member.display_name}" is not a character'
        )
        try:
            retire_member(log, session_id, member_id=member_id, reason=reason)
        except Exception:  # noqa: BLE001 - best-effort, never fails the turn
            continue
        return True
    return False


def _present_ids(
    log: Any,
    session_id: str,
    result: object,
    admitted: dict[str, str | None],
) -> list[str]:
    """Member ids the turn reports as present — the strict descriptor liveness set.

    A name the turn reports present (a ``state`` header or a ``venue`` holder) is
    resolved to its member: first through this turn's admits, then through the
    roster. The descriptor surface itself counts — the phrase appearing in the
    turn is evidence that *somebody* is present under it, which is what makes an
    unambiguous live link reusable (ADDENDUM 3 Q2 rule 1), while a second live
    candidate reported present makes the phrase ambiguous and the engine SPLITS
    (rule 3). Off-screen names are excluded: presence, not existence.

    This is the set the engine TRUSTS as this turn's evidence. What a delta does
    not re-report is handled separately, per phrase, by
    :func:`_descriptor_presence`.
    """
    from .cast import member_by_name, roster

    members = roster(log, session_id)
    ids: list[str] = []
    for name in _present_names(result):
        member_id = admitted.get(name)
        if member_id is None:
            member = member_by_name(members, name)
            member_id = member.member_id if member is not None else None
        if member_id is not None and member_id not in ids:
            ids.append(member_id)
    return ids


def _is_engine_churn(member: CastMember | None) -> bool:
    """True when the member is the engine's own provisional mint for a phrase."""
    from .cast import RUNTIME_ADMITTERS

    return (
        member is not None
        and member.provisional
        and not member.former_names
        and member.admitted_by in RUNTIME_ADMITTERS
    )


def _descriptor_presence(
    log: Any,
    session_id: str,
    strict: Sequence[str],
    descriptor_keys: Sequence[str],
    members: Sequence[CastMember],
) -> dict[str, list[str]]:
    """Per-phrase liveness set: this turn's presence PLUS the engine's own links.

    Two jobs, on ONE rule — a delta reports change, not attendance, so a turn
    that does not re-report a character is not evidence they left:

    * a phrase whose ``cast_link`` is fresh
      (:func:`fabulist.state.cast.last_descriptor_links`, live for
      :data:`~fabulist.state.presence.PRESENCE_WINDOW` narratives like every
      other presence sighting) is still live when this turn's headers name the
      entity under some other spelling. Without it the drifted headers of a live
      session split a fresh provisional every single turn;
    * when the ONLY thing making the phrase ambiguous is that churn — several
      provisional members the ENGINE minted for the same phrase on consecutive
      turns, none of them named, none of them headered this turn — the newest
      link is the current binding, so the phrase RE-LINKS to it instead of
      splitting yet another ``(N)``. Candidates that ARE headered this turn keep
      the ADDENDUM 3 rule 3 split: two names, two people, never a merge.
    """
    from .cast import descriptor_candidates, last_descriptor_links, member_by_id
    from .presence import PRESENCE_WINDOW, presence_walk

    if not descriptor_keys:
        return {}
    links = last_descriptor_links(log, session_id)
    walk = presence_walk(log, session_id)
    out: dict[str, list[str]] = {}
    for key in descriptor_keys:
        if key in out:
            continue
        present = list(strict)
        carried: str | None = None
        link = links.get(key)
        if link is not None:
            member = member_by_id(members, link[0])
            if (
                member is not None
                and member.is_active
                and walk.narratives_after(link[1]) < PRESENCE_WINDOW
            ):
                carried = link[0]
                if carried not in present:
                    present.append(carried)
        live = descriptor_candidates(log, session_id, key, presence=present)
        if len(live) > 1 and carried is not None:
            headered = descriptor_candidates(log, session_id, key, presence=strict)
            if len(headered) <= 1 and all(
                _is_engine_churn(member_by_id(members, member_id))
                for member_id in live
            ):
                present = [carried]
        out[key] = present
    return out


def _sibling_surfaces(surface: str, surfaces: Sequence[str]) -> tuple[str, ...]:
    """The other spellings this turn reported for the SAME person.

    A ``cast`` proposal carries the surface forms the tracker itself wrote (the
    old promotion gate's rule, finding 12: no invented nicknames), so the alias
    list is taken from the turn's own header set: every surface that shares this
    one's canonical key (``Marisol`` / ``marisol`` / ``Marisol.``) is one person
    spelled differently, and anything with a different key is somebody else.
    """
    key = _canonical(surface)
    return tuple(
        other for other in surfaces if other != surface and _canonical(other) == key
    )


def _resolve_or_propose_identity(
    log: Any,
    session_id: str,
    surface: str,
    *,
    aliases: Sequence[str] = (),
    rename_to: str | None = None,
) -> str | None:
    """Resolve an identity surface to an ACCEPTED member, else file a proposal.

    The one identity door (W8.6 door, owner-ruled policy):

    * an accepted member already carries the surface — a card/world seed, the
      player's ``user`` actor, or a member the OWNER accepted from a proposal —
      so it is used as-is. The admit re-links a novel spelling as an alias and
      resurrects a member the cap tombstoned (W9.15: the cap is LRU, not a ban);
      the fold keeps its original provenance, so an accepted actor stays
      actor-tier and keeps its lane.
    * nobody carries it: the person is NOT admitted (cast admission is
      user-gated) and surfaces as a ``cast`` proposal
      (:func:`_emit_cast_proposal`). ``None`` is returned, exactly the old
      refused-admit signal, so the caller's rename/presence bookkeeping treats an
      unaccepted person as absent from the roster.

    ``rename_to`` is the tracker's "this person is named X this turn" signal, so
    an unaccepted person is proposed under the name the story just gave it and
    the surface it was known by rides the alias list (the rename the owner will
    see is the same one an accepted member's ``patch_member`` would perform).

    Whether the surface names a person at all is decided before this call by
    :func:`_retire_non_character_member`.
    """
    from .cast import admit_member, member_by_name, roster

    if member_by_name(roster(log, session_id), surface) is None:
        proposed = str(rename_to or surface).strip() or surface
        filed_aliases = list(aliases)
        if proposed != surface and surface not in filed_aliases:
            filed_aliases.insert(0, surface)
        _emit_cast_proposal(log, session_id, proposed, filed_aliases)
        return None
    try:
        return admit_member(log, session_id, surface, provenance="bts", admitted_by="bts")
    except Exception:  # noqa: BLE001 - the link is best-effort (D9)
        return None


def _proposed_rename(
    ref: "BtsReference | None", player_keys: frozenset[str]
) -> str | None:
    """The name a reference entry says this person now goes by, when usable.

    A ``rename_to`` that is itself the player or a narrator-class word names
    nobody, so it is not a proposal name either (the same doors the rename patch
    uses).
    """
    if ref is None or not ref.rename_to:
        return None
    proposed = str(ref.rename_to).strip()
    if not proposed or is_role_surface(proposed):
        return None
    if _is_player_surface(proposed, player_keys):
        return None
    return proposed


def _admit_legacy_names(
    log: Any, session_id: str, result: object, *, user_actor: bool = False
) -> None:
    """The pre-W8.6 identity pass (parity bridge for ``references`` absent).

    The payload handling is unchanged — the name set comes from the state headers
    — but the ADMISSION policy is not forked: every name goes through the same
    user-gated door (:func:`_resolve_or_propose_identity`), so a new person is a
    ``cast`` proposal here too rather than an admit that only the referenced path
    gates.

    W9.16: role words and pronouns are SKIPPED. The blind loop re-admitted
    'Narrative' after W8.9 retired it, and admitted 'you' as a cast member.
    The PLAYER is skipped for the same reason (:func:`_player_keys`): this path
    has no references to classify, so the header itself is all the evidence
    there is, and a header spelling the user's persona is not a character.

    ``user_actor`` (User-as-Actor ON, W9.22 §2) is the one exception, and it
    carries NO references: it admits the ONE ``user`` member for the player
    surface (:func:`_admit_user_member`) exactly as the referenced path does, so
    a weaker transport tier gets the same actor instead of silently losing it.
    """
    player_keys = _player_keys(log, session_id)
    names = bts_name_set(result)
    for name in names:
        if _is_player_surface(name, player_keys):
            if user_actor:
                _admit_user_member(log, session_id, name)
            continue
        if is_role_surface(name):
            continue
        _resolve_or_propose_identity(
            log, session_id, name, aliases=_sibling_surfaces(name, names)
        )


def latest_world_state(log: Any, session_id: str) -> dict[str, Any]:
    """Latest ``system``/``world_state`` payload (fork-aware), or ``{}``.

    Returns the whole payload (``kind``/``state``/``venue``/``threads``/...).
    """
    latest: dict[str, Any] = {}
    for event in _resolve_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") == "world_state":
            latest = event.payload
    return latest


def turns_since_checkpoint(log: Any, session_id: str) -> int:
    """Count narratives appended after the last checkpoint world_state event."""
    events = _resolve_events(log, session_id)
    last_cp_seq = -1
    for event in events:
        if (
            event.type == "system"
            and isinstance(event.payload, dict)
            and event.payload.get("event") == "world_state"
            and event.payload.get("kind") == "checkpoint"
        ):
            last_cp_seq = event.seq
    return sum(
        1
        for event in events
        if event.type == "narrative" and not getattr(event, "ephemeral", False)
        and event.seq > last_cp_seq
    )


def checkpoint_due(log: Any, session_id: str) -> bool:
    """Turn-count half of the cadence (scene change / request are turn-local)."""
    return turns_since_checkpoint(log, session_id) >= BTS_CHECKPOINT_TURNS


# -- narrator section (B2) ---------------------------------------------------

#: Character budget for the declarative narrator section (~300 tokens). The
#: P1-P4 ladder cuts from the bottom when the render exceeds it.
BTS_SECTION_BUDGET = 1200

_MOODLET_STRIP_RE = re.compile(r"\s*\[([^\]]*)\]\s*")


def render_bts_section(
    payload: dict[str, Any], *, budget: int = BTS_SECTION_BUDGET
) -> str | None:
    """Render a ``world_state`` payload as declarative continuity notes (B2).

    Declarative phrasing only (D22): the narrator receives facts about the
    world, never instructions. Returns ``None`` when there is nothing to say.
    Priority ladder (DROP from the bottom when over ``budget``): P1 present
    relationships/wounds/venue, P2 threads/tension, P3 off-screen, P4 the rest.
    """
    if not isinstance(payload, dict):
        return None
    state = payload.get("state") or {}
    venue = payload.get("venue") or {}
    threads = payload.get("threads") or []
    tension = payload.get("tension") or {}
    off_screen = payload.get("off_screen") or []

    present: list[str] = []
    p3: list[str] = []
    p4: list[str] = []
    for character, categories in state.items():
        if not isinstance(categories, dict):
            continue
        facts: list[str] = []
        rels = categories.get("relationships") or {}
        for key, value in rels.items():
            facts.append(f"{key} stands at {value}")
        physical = categories.get("physical") or {}
        for key, value in physical.items():
            facts.append(f"{character}'s {key} is {value}")
        where = venue.get(character)
        if isinstance(where, dict) and where.get("venue"):
            area = f" ({where['area']})" if where.get("area") else ""
            facts.append(f"{character} is at @{where['venue']}{area}")
        # P4: the remaining (appearance/stats/emotional) detail.
        for category, attrs in categories.items():
            if category in ("relationships", "physical") or not isinstance(attrs, dict):
                continue
            for key, value in attrs.items():
                if category == "narrative" or key in ("moodlets",):
                    continue
                p4.append(f"{character}'s {key} is {value}")
        if facts:
            present.append(f"- " + "; ".join(facts) + ".")

    narrative = payload.get("state", {})
    for character, categories in state.items():
        narr = (categories or {}).get("narrative") or {}
        for key, value in narr.items():
            p3.append(f"- {character}'s {key}: {value}.")
    for thread in threads:
        detail_text = thread.get("detail") or ""
        if detail_text and detail_text.lower() == (thread.get("status") or ""):
            detail_text = ""
        detail = f" — {detail_text}" if detail_text else ""
        p3.append(f"- Thread {thread['name']} is {thread['status']}{detail}.")
    if isinstance(tension, dict) and tension.get("level") is not None:
        p3.append(f"- Narrative tension is {tension['level']}/10.")
    for entry in off_screen:
        place = f" at @{entry['location']}" if entry.get("location") else ""
        mood = f", mood {entry['mood']}" if entry.get("mood") else ""
        p3.append(f"- Off-screen, {entry['name']} is{place}{mood}.")

    blocks: list[str] = list(present)
    # P2/P3 join the body, then trim P4 first.
    body = "\n".join(blocks + p3)
    if not body.strip():
        body = ""
    if p4:
        candidate = body + ("\n" if body else "") + "\n".join(p4)
        if len(candidate) <= budget:
            body = candidate

    if not body.strip():
        return None
    if len(body) > budget:
        body = body[:budget].rsplit("\n", 1)[0] + "\n…(continuity notes truncated)"
    return body


def session_bts_block(
    log: Any, session_id: str, *, budget: int = BTS_SECTION_BUDGET
) -> str | None:
    """Assemble the narrator BTS section from the latest world-state event."""
    payload = latest_world_state(log, session_id)
    if not payload:
        return None
    return render_bts_section(payload, budget=budget)


# -- the action loop (W9.29 §6) ----------------------------------------------

#: The three READ tools the tracker may call. Each is a thin wrapper over a read
#: this module (or ``state.cast``) already owns — no new truth, no new write path:
#: the reads are FREE (they never charge the action budget) and they widen what
#: the model has in front of it while it works.
FOLDED_STATE_TOOL = "get_folded_state"
ROSTER_TOOL = "get_roster"
RECENT_DELTAS_TOOL = "get_recent_deltas"

#: ``get_recent_deltas`` reads this many TURNS of entity actions when the model
#: names no count, and clamps whatever it does name to the ceiling (a read is
#: context for one turn, not a dump of the ledger).
BTS_RECENT_DELTAS_DEFAULT = 3
BTS_RECENT_DELTAS_MAX = 10

#: One ``get_recent_deltas`` group is clipped here (chars of JSON): a full turn of
#: actions would otherwise re-send the ledger through a tool result, which rides
#: the very context the read exists to inform.
BTS_DELTA_CLIP_CHARS = 1200

#: The tail message that carries the ACTION PROTOCOL. It is appended RUNNER-SIDE
#: after the flow's own messages — the same tail position the retired notation
#: directive used (AGENTS 6: the seeded prefix must stay byte-identical turn to
#: turn, so nothing dynamic may live there) — and the entity palette
#: (:func:`fabulist.prompts.entity_palette.render_entity_palette`) is appended
#: after it, because a stored preset customisation of the ``bts`` surface must not
#: be able to drop either one (W9.29 §7, review N5).
#:
#: It names each case where an action or a read pays for itself: live, a model told
#: only "read when needed" never read, and one told to confirm the roster before
#: naming a person did.
BTS_ACTION_PROTOCOL = """ACTION PROTOCOL — you record state by CALLING TOOLS, one call per message. The ENTITY REGISTRY below is the ONLY id source: cite its ids exactly, never invent one, and establish nothing it already lists. A registry that lists only off-screen means nothing is recorded yet, not that nothing happened.

PLACES (follow in order):
1. Look first. Read the LOCATIONS block. If the place is already listed — or is the same thing under another word ("dorm" for "Dormitory", "bathroom" for "Bathroom") — USE it by citing its loc_ id. Never re-create a listed place.
2. Nest, never re-root. A room inside a listed place is establish_location with parent_id = that place's id. Leave parent_id OUT when the room belongs to the tree on screen: an unparented place nests under the session's existing place automatically. Pass root:true only for a genuinely separate building or area. The world is not a location; no world roots.
3. Use the returned id — to nest under a just-created parent, cite the id the PREVIOUS call returned.

Then act: person named in prose → get_roster first; being placed or moved → move_being (player is who:"player"); attribute or meter changed → set_attr; named item → establish_item; new situation → establish_thread; existing object changed → update_object; gone for good → retire_object; a state that wears off → set_condition (an expiry is the CLOCK's job: give a future instant or none, never a turn count); how someone stands, dresses or what shows on them → set_body; money earned or spent → adjust_economy (an amount only, never the denomination); the prose really moved the clock → propose_time, quoting that prose as the evidence — the engine refuses a move backwards or one longer than six hours.

RULE: who:"player" is the USER's own persona, already in the scene — never establish them as a new person, and never go looking for them in get_roster (it lists cast members only, and the player is not one).

One call per message, wait for the result. A rejected call recorded nothing: read the error, correct that argument, call again. Reads are free; writes cost one of fifteen. Anything the prose placed or changed gets an action; nothing changed = close with one sentence now."""


def _last_action_count(log: Any, session_id: str) -> int:
    """The newest ``entity_summary`` row's accepted-action count (palette footer).

    One row per turn (§5), so the newest one describes the last turn — which is
    what the footer's "(N objects · M actions last turn)" reports.
    """
    from .entities import ENTITY_SUMMARY_EVENT

    count = 0
    for event in _resolve_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != ENTITY_SUMMARY_EVENT:
            continue
        actions = payload.get("actions")
        if isinstance(actions, int) and not isinstance(actions, bool):
            count = actions
    return count


def registry_palette(log: Any, session_id: str, *, actions: int | None = None) -> str:
    """The turn's entity palette: the folded registry rendered for the prompt tail.

    The ONE render both the tail message and ``get_folded_state`` use, so what the
    model reads at the top of the turn and what it re-reads mid-loop can never
    disagree. ``actions`` overrides the footer's count (the node knows this turn's;
    the default reads the last committed ``entity_summary``).

    The player's assignment renders as ``Player (<persona>)``: the persona read is
    :func:`fabulist.state.presence._player_name` — the ONE resolution of "who is
    the player" (best-effort, exactly as the cast door uses it), because the roster
    never carries the user's own persona.
    """
    from ..prompts.entity_palette import render_entity_palette
    from .cast import roster
    from .entities import Registry, registry
    from .presence import _player_name

    if log is None:
        # No log, no registry: the honest render is the empty one (the off-screen
        # bucket is always a destination, req. 2) — a caller that was handed no
        # log still gets a prompt tail instead of a crash.
        return render_entity_palette(Registry(), (), actions=actions or 0)
    counted = _last_action_count(log, session_id) if actions is None else actions
    return render_entity_palette(
        registry(log, session_id),
        roster(log, session_id),
        actions=counted,
        player_display=_player_name(log, session_id),
    )


#: Committed turns between RECONCILIATION passes (W9.29 §12, decision point §14.7
#: — INCLUDE). 10 is the value the retired ``BTS_CHECKPOINT_TURNS`` had: the pass
#: inherits the checkpoint's slot, which the registry left vacant (a fold is current
#: by construction, so there is nothing to checkpoint). No session setting and no
#: graph state: the cadence is this constant, and the clock is a log read.
RECONCILE_CADENCE = 10

#: The one sentence a reconciliation turn adds to the dynamic tail (AGENTS 6: the
#: seeded prefix stays byte-stable; the instruction that changes per turn belongs at
#: the END of the prompt). It names the tool FIRST, because the pass is worthless if
#: the model acts before it dumps what it believes.
RECONCILE_DIRECTIVE = (
    "RECONCILE TURN: first call reconcile with everything you believe, then act on "
    "the prose."
)


def last_reconcile_turn(log: Any, session_id: str) -> int:
    """The committed-turn index of the newest completed reconciliation pass, or 0.

    The clock is the ``agent_loop`` trace the loop already writes: a reconcile call
    is a ``tool_call`` row naming :data:`fabulist.state.entity_tools.RECONCILE_TOOL`
    on the ``bts`` surface. Reading it (rather than storing a stamp) is what keeps
    the cadence free of new schema, new events and graph state — and it gives D86's
    missed-window rule for free: a turn whose pass never ran (a failed BTS pass, or
    a model that ignored the directive) leaves the clock where it was, so the next
    committed turn is due again.

    Turns are counted the only way they are defined: non-ephemeral narratives on the
    fork-aware chain.
    """
    from .entity_tools import RECONCILE_TOOL

    latest_seq = 0
    try:
        events = _cadence_events(log, session_id)
    except Exception:  # noqa: BLE001 - the cadence is a read, never fatal
        logger.warning("could not read the reconcile clock", exc_info=True)
        return 0
    for event in events:
        payload = getattr(event, "payload", None)
        if getattr(event, "type", None) != "system" or not isinstance(payload, dict):
            continue
        if payload.get("event") != "agent_loop" or payload.get("tool") != RECONCILE_TOOL:
            continue
        latest_seq = max(latest_seq, int(getattr(event, "seq", 0) or 0))
    if not latest_seq:
        return 0
    return sum(
        1
        for event in events
        if getattr(event, "type", None) == "narrative"
        and not getattr(event, "ephemeral", False)
        and int(getattr(event, "seq", 0) or 0) <= latest_seq
    )


def reconcile_is_due(
    *,
    committed_turns: int,
    last_reconcile: int,
    cadence: int = RECONCILE_CADENCE,
) -> bool:
    """Is THIS turn a reconciliation turn? (§12's cadence rule)

    ``committed_turns`` counts the chain's non-ephemeral narratives and includes the
    turn being tracked (BTS runs post-turn), so the first pass lands on turn 10; the
    clock resets to the turn a pass actually ran on
    (:func:`last_reconcile_turn`). ``cadence`` is floored at 1 so a corrupt value can
    never make every turn a reconcile turn.
    """
    try:
        turn = int(committed_turns)
        last = int(last_reconcile)
    except (TypeError, ValueError):
        return False
    return turn - last >= max(1, int(cadence))


def _cadence_events(log: Any, session_id: str) -> list[Any]:
    """The cadence's log view: the fork-aware CHAIN, in the graph's own read rule.

    :func:`fabulist.graph.nodes.committed_turn_count` (the plan and historian
    cadence) reads ``get_chain_events``; the reconciliation clock must count turns
    the SAME way or the two cadences would disagree about what a turn is. The
    module-local :func:`_resolve_events` walk is the fallback for a log seam that
    has no chain read.
    """
    get_chain = getattr(log, "get_chain_events", None)
    if callable(get_chain):
        return list(get_chain(session_id))
    return _resolve_events(log, session_id)


def _committed_turns(log: Any, session_id: str) -> int:
    """Committed narratives on the fork-aware chain (the cadence's left side).

    Same definition as :func:`fabulist.graph.nodes.committed_turn_count` and the
    historian's due rule, read locally: this module must not import the graph layer.
    """
    return sum(
        1
        for event in _cadence_events(log, session_id)
        if getattr(event, "type", None) == "narrative"
        and not getattr(event, "ephemeral", False)
    )


def _folded_state_read(log: Any, session_id: str) -> dict[str, Any]:
    """``get_folded_state``: the registry render (fork-aware), ids and all.

    W9.29 §6 replaced the "last committed world_state payload" read with this one:
    the world is the registry, and the model needs the same view mid-loop (after
    its own actions) that the tail handed it at the start of the turn.
    """
    return {"ok": True, "registry": registry_palette(log, session_id)}


def _roster_read(log: Any, session_id: str) -> dict[str, Any]:
    """``get_roster``: the folded cast — ids, display names, provenance.

    ``member_id`` is the stable join key every write action resolves against; the
    display name is what the model spells when it names a person.
    """
    from .cast import roster

    return {
        "ok": True,
        "members": [
            {
                "id": member.member_id,
                "name": member.display_name,
                "provenance": member.provenance,
                "active": member.is_active,
            }
            for member in roster(log, session_id)
        ],
    }


def _recent_deltas_read(log: Any, session_id: str, count: Any) -> dict[str, Any]:
    """``get_recent_deltas``: the last N turns' ENTITY ACTIONS, clipped (§6).

    One group per committed ``entity_summary`` row (one row per turn, §5): the
    actions the tracker appended that turn plus that row's counts. The retired
    ``world_state`` payloads are not re-sent — ``get_folded_state`` is the current
    view, and history is exactly what these groups answer.
    """
    from .entities import ENTITY_EVENT_KINDS, ENTITY_SUMMARY_EVENT

    groups: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for event in _resolve_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        variant = payload.get("event")
        if variant in ENTITY_EVENT_KINDS and variant != ENTITY_SUMMARY_EVENT:
            current.append(dict(payload))
            continue
        if variant == ENTITY_SUMMARY_EVENT:
            groups.append({"summary": dict(payload), "actions": current})
            current = []
    if current:
        # Actions with no summary yet: the turn in flight (the runner appends the
        # summary after the loop), so this group is the freshest one.
        groups.append({"summary": {}, "actions": current})
    limit = BTS_RECENT_DELTAS_DEFAULT
    if isinstance(count, int) and not isinstance(count, bool):
        limit = count
    limit = max(1, min(BTS_RECENT_DELTAS_MAX, limit))
    recent: list[dict[str, Any]] = []
    for group in groups[-limit:]:
        serialized = json.dumps(group, ensure_ascii=False, default=str)
        recent.append(
            group
            if len(serialized) <= BTS_DELTA_CLIP_CHARS
            else {"clipped": serialized[:BTS_DELTA_CLIP_CHARS]}
        )
    return {"ok": True, "count": len(recent), "turns": recent}


def bts_loop_tools(
    log: Any, session_id: str, turn: Any = None
) -> list[Any]:
    """The action loop's tool set: the three free reads plus the fifteen write actions.

    W9.29 §6: ``report_world_state`` is gone from the served set (its declaration
    stays importable as the §11.2 read-side legacy shim) and the loop's natural
    stop — a completion that calls no tool — is the turn's close-out, so no
    adapter is needed to terminate anything.

    ``turn`` is the per-turn :class:`~fabulist.state.entity_tools.EntityTurn`
    ledger every write tool charges; the runner creates it (and reads its counts
    back for the ``entity_summary`` row). Attribution comes from the transport's
    tool NAME (``meta.name``), never from position.
    """
    from ..agents.loop import LoopTool
    from .entity_tools import EntityTurn, entity_action_tools

    ledger = turn if turn is not None else EntityTurn(log, session_id)

    # ASYNC on purpose: a sync ``fn`` runs in a worker thread (the runtime's
    # execution guard), and the EventLog's sqlite connection is bound to the
    # thread that created it — the reads must stay on the loop thread, exactly
    # like the extractor's usage recording.
    async def folded_state(args: dict[str, Any]) -> dict[str, Any]:
        return _folded_state_read(log, session_id)

    async def roster_read(args: dict[str, Any]) -> dict[str, Any]:
        return _roster_read(log, session_id)

    async def recent_deltas(args: dict[str, Any]) -> dict[str, Any]:
        return _recent_deltas_read(log, session_id, args.get("count"))

    return [
        LoopTool(
            name=FOLDED_STATE_TOOL,
            description=(
                "Read the entity registry as it stands right now (every location, "
                "meter, trait, relationship, thread and item, with its id). Free. "
                "Call it again after your own actions to see their effect."
            ),
            parameters={"type": "object", "properties": {}},
            fn=folded_state,
        ),
        LoopTool(
            name=ROSTER_TOOL,
            description=(
                "Read the session's cast roster: every member's id, display name "
                "and provenance. Free. Call this before you name, move, set a "
                "value for or rename a person, so an existing member is reused."
            ),
            parameters={"type": "object", "properties": {}},
            fn=roster_read,
        ),
        LoopTool(
            name=RECENT_DELTAS_TOOL,
            description=(
                "Read the last few turns of recorded entity actions (most recent "
                "last), each clipped. Free. Call this when the turn continues "
                "something an earlier turn recorded."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": BTS_RECENT_DELTAS_MAX,
                        "description": (
                            "How many recent turns to read "
                            f"(default {BTS_RECENT_DELTAS_DEFAULT})."
                        ),
                    }
                },
            },
            fn=recent_deltas,
        ),
        *entity_action_tools(ledger),
    ]


def reconcile_loop_tools(turn: Any) -> list[Any]:
    """The manual check-state loop's tool set: ``reconcile`` and NOTHING else.

    The free reads of :func:`bts_loop_tools` are deliberately absent: this pass
    compares what the model BELIEVES against the registry, so handing it
    ``get_folded_state`` would let it copy the registry back and report "no drift"
    by construction. The write actions are absent for the reason the cadence's
    pass never writes (req. 12): flags only, the corrective write is a later turn.

    ``turn`` must be a reconciliation :class:`~fabulist.state.entity_tools.EntityTurn`
    (``reconcile=True``) — the only state in which the tool exists at all.
    """
    from .entity_tools import RECONCILE_TOOL, entity_action_tools

    return [tool for tool in entity_action_tools(turn) if tool.name == RECONCILE_TOOL]


def last_narrative_prose(log: Any, session_id: str) -> str:
    """The newest committed narrative's prose on the fork-aware chain (or "").

    The synthetic turn state's evidence: an off-turn pass has no turn of its own,
    so the scene it is asked about is the last one the story committed. Ephemeral
    rows are skipped (they are not the story), and the newest non-empty prose
    wins — the same rule :class:`~fabulist.state.entity_tools.EntityTurn` uses to
    anchor its flags.
    """
    prose = ""
    for event in _cadence_events(log, session_id):
        if getattr(event, "type", None) != "narrative":
            continue
        if getattr(event, "ephemeral", False):
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("prose") or "")
        if text:
            prose = text
    return prose


class _FinishWatch:
    """Thin transport probe: records the completion's ``finish_reason``, forwards all.

    The loop runtime reads ``finish_reason`` off the transport's ``error`` frame for
    its rows but does not report it on :class:`~fabulist.agents.loop.LoopResult`,
    and D86's rule ("a truncated completion is never trusted, and is never re-tried
    on the same budget") belongs to the pass, not the runtime. This adapter adds
    exactly that one fact and changes nothing else — no withholding, no rewriting:
    the loop sees byte-identical events.
    """

    def __init__(self, caller: Any) -> None:
        self._caller = caller
        #: The transport's stop reason for the last completion (``None`` if unstated).
        self.finish_reason: str | None = None

    async def __call__(
        self,
        messages: list[dict],
        stream: bool = True,
        request_profile: dict | None = None,
    ) -> Any:
        async for event in self._caller(
            messages, stream, request_profile=request_profile
        ):
            if event.get("type") == "error":
                meta = event.get("meta") or {}
                reason = meta.get("finish_reason")
                if reason:
                    self.finish_reason = reason
            yield event


# -- runner ------------------------------------------------------------------


class BtsRunner:
    """Drive the ``bts`` action loop: the turn's write actions, appended as they land.

    ``caller`` is any :data:`fabulist.graph.types.ModelCaller`. ``renderer`` is
    an optional ``(user_msg, narrative, prior_state, checkpoint) -> messages`` the
    preset flow supplies; ``None`` keeps :func:`build_bts_messages`. The action
    protocol and the entity palette are appended AFTER whatever the renderer
    produced — the tail pattern AGENTS 6 requires, so a stored preset flow cannot
    drop the registry or the protocol (W9.29 §7, review N5).

    ``profile_fn`` resolves the connection's transport capabilities per call. A
    resolved profile means the connection does function calling, which is a
    PRECONDITION now: W9.29 §11.2 retired the notation write path, so a connection
    with no function calling writes no state at all (the pass raises
    :class:`BtsExtractionError` with ``stage="tier_d"`` and the next turn retries).
    ``tool_choice`` is never sent from here — the loop asks with ``"auto"``, since a
    forced choice would trap the model inside a tool instead of letting it close
    the turn out.

    ``timeout_fn`` returns the bound each loop call rides, recorded on the runner's
    ``agent_loop`` rows as PROVENANCE only — hang protection is the transport's
    per-connection timeout, never a loop-level bound (owner ruling).

    ``window_fn`` returns the BTS slot's AVAILABLE prompt tokens (declared Context
    Size − Response Length − engine reserve), or ``None`` when the owner declared
    no Context Size. It is handed to the runtime so the loop's ``agent_loop`` rows
    carry passive ``headroom`` (W9.25 §3.5.3); absent, the rows keep today's shape
    and the loop proceeds either way — headroom never stops a run.

    ``return_thinking_fn`` resolves the connection's tri-state "Return thinking"
    setting per call (``None`` = AUTO, the default). It is handed to the runtime
    unchanged: a thinking model's ``reasoning_content`` is then repeated back on
    the assistant ``tool_calls`` message inside the action loop, which DeepSeek's
    thinking mode requires after a tool call.

    The RECONCILIATION cadence (§12) is decided per request
    (:meth:`_reconcile_due`, :data:`RECONCILE_CADENCE`): on such a turn the loop
    additionally carries the ``reconcile`` tool and the tail gains
    :data:`RECONCILE_DIRECTIVE`. Nothing else changes — the pass appends flags, and
    the clock it reads is the loop's own ``agent_loop`` trace, so there is no new
    session setting and no state in the graph/checkpoint.

    The result is a :class:`BtsResult` whose ``state``/``venue``/``threads`` lanes
    are EMPTY by design (there is no report payload any more): the world is the
    registry the loop's actions just wrote, and the node folds it. What the result
    does carry is the turn's ledger — ``actions``/``rejected``/``flags`` (the
    ``entity_summary`` row) and ``mentioned`` (the cast door's input, §6 review R1).
    """

    def __init__(
        self,
        caller: Any,
        *,
        renderer: Any | None = None,
        log: Any | None = None,
        profile_fn: Any | None = None,
        timeout_fn: Any | None = None,
        window_fn: Any | None = None,
        return_thinking_fn: Any | None = None,
    ) -> None:
        self._caller = caller
        self._renderer = renderer
        self._log = log
        self._profile_fn = profile_fn
        self._timeout_fn = timeout_fn
        self._window_fn = window_fn
        self._return_thinking_fn = return_thinking_fn
        #: Rendering-side timing of the last call (unchanged consumer contract).
        self.last_latency_s: float = 0.0
        #: The turn's action ledger (the counts the node commits).
        self.last_turn: Any | None = None
        #: Was the last turn a reconciliation turn? (§12 — the cadence's answer,
        #: recorded for the trace/tests; it changes no other output.)
        self.last_reconcile: bool = False
        #: The turn's closing summary (the last completion that called no tool).
        self.last_summary: str = ""

    def _profile(self) -> dict[str, Any] | None:
        if self._profile_fn is None:
            return None
        return self._profile_fn()

    def _timeout(self) -> float | None:
        if self._timeout_fn is None:
            return None
        try:
            return float(self._timeout_fn())
        except Exception:  # noqa: BLE001 - provenance must never break the pass
            return None

    def _return_thinking(self) -> bool | None:
        """The connection's reasoning-echo setting (``None`` = AUTO).

        A lookup failure answers ``None`` — AUTO is the behaviour every tool-loop
        model wants by default, so a broken settings read must never be the reason
        a thinking model's 400 returns.
        """
        if self._return_thinking_fn is None:
            return None
        try:
            return self._return_thinking_fn()
        except Exception:  # noqa: BLE001 - AUTO is the safe answer
            return None

    def _messages(
        self,
        user_msg: str,
        narrative: str,
        prior_state: dict | None,
        checkpoint: bool,
        session_id: str | None = None,
    ) -> list[dict[str, str]]:
        if self._renderer is not None:
            # Issue #70: the flow builder is session-aware so a failed preset
            # read can leave its ``preset_degraded`` row on the TURN's session.
            # A caller that knows no session passes the four-argument shape.
            if session_id is None:
                return list(
                    self._renderer(user_msg, narrative, prior_state, checkpoint)
                )
            return list(
                self._renderer(
                    user_msg, narrative, prior_state, checkpoint, session_id
                )
            )
        return build_bts_messages(user_msg, narrative, prior_state, checkpoint=checkpoint)

    def _session_id(self) -> str:
        """The turn's session, read the way every post-turn pass reads it.

        ``CURRENT_SESSION`` is installed pump-side for the whole turn (the same
        contextvar the usage rows and the attribution roster read), so the BTS
        pass — which is handed no session — resolves its own reads and trace scope
        from it.
        """
        from ..agents.debug import CURRENT_SESSION

        return CURRENT_SESSION.get("")

    def _reconcile_due(self, session_id: str) -> bool:
        """Is this turn's reconciliation pass due? (§12 — computed per request)

        Two chain reads (the committed-turn count and the reconcile clock) and no
        model call. A read failure answers ``False``: a cadence probe must never be
        the reason a turn's BTS pass did not run.
        """
        if not session_id or self._log is None:
            return False
        try:
            return reconcile_is_due(
                committed_turns=_committed_turns(self._log, session_id),
                last_reconcile=last_reconcile_turn(self._log, session_id),
            )
        except Exception:  # noqa: BLE001 - the cadence is advisory
            logger.warning("could not decide the reconcile cadence", exc_info=True)
            return False

    async def __call__(
        self,
        user_msg: str,
        narrative: str,
        prior_state: dict | None = None,
        *,
        checkpoint: bool = False,
        clock: tuple[str, str] | None = None,
        session_id: str | None = None,
    ) -> BtsResult:
        """Record the turn's world changes through the write-action loop (§6).

        ``checkpoint`` is accepted and IGNORED: the registry is current by
        construction, so there is no delta/checkpoint cadence any more (§6 — the
        cadence question's successor is the reconciliation pass, §12).

        ``clock`` is this turn's ``(time, date)`` from the clock node's facts: it
        rides the ledger (:class:`~fabulist.state.entity_tools.EntityTurn`), so
        ``set_condition``'s expiry and ``propose_time``'s move are judged against
        the instant the turn is really at instead of the projection's previous
        turn (W9.32 §3.5). Omitted, the ledger validates against the fold.
        """
        if self._profile() is None:
            # W9.29 §11.2 (doctrine amendment to D69): the notation write path is
            # retired, so a connection that cannot call functions writes no state.
            # The node logs ``world_state_failed`` (stage ``tier_d``) and the next
            # turn retries — nothing is invented on the way out.
            raise BtsExtractionError(
                "BTS write path unavailable: this connection does not support "
                "function calling, and the notation write path is retired "
                "(W9.29 §11.2). No world state was recorded.",
                raw="",
                stage="tier_d",
            )
        messages = self._messages(
            user_msg, narrative, prior_state, checkpoint, session_id
        )
        return await self._action_call(messages, clock=clock)

    async def _action_call(
        self,
        messages: list[dict[str, Any]],
        *,
        clock: tuple[str, str] | None = None,
    ) -> BtsResult:
        """Tier A: one read+write loop; a completion with no tool call closes it (§6).

        Reads are free, writes are budgeted and each is appended the moment it
        validates (D9: the engine appends, the model never writes the log), so a
        run that is cut off mid-loop keeps every action it already landed (§6).
        Total output failure — nothing landed and nothing usable was said, or the
        transport timed out, or a truncated completion produced no action — raises
        :class:`BtsExtractionError` and the node logs ``world_state_failed``
        (stage ``registry``).
        """
        from ..agents.loop import run_agent_loop
        from .entity_tools import EntityTurn

        session_id = self._session_id()
        # §12: the reconciliation pass is a CADENCE, decided here at request time
        # (no session setting, no graph state). It changes exactly two things: the
        # loop carries the `reconcile` tool, and the tail gains one sentence.
        reconcile = self._reconcile_due(session_id)
        self.last_reconcile = reconcile
        turn = EntityTurn(self._log, session_id, reconcile=reconcile, clock=clock)
        self.last_turn = turn
        # The protocol + palette ride the TAIL, after whatever the flow rendered:
        # the seeded prefix stays byte-stable (AGENTS 6) and a stored preset
        # customisation of this surface cannot drop either of them (§7 N5). The
        # reconcile sentence is the LAST line on its turn — most recent wins.
        #
        # Issue #67: the tail is the NARRATOR's carrier (:mod:`fabulist.prompts.
        # envelope`), not a second system message — ONE ephemeral ``assistant``
        # message inserted immediately before the final user message, because a
        # stock Jinja chat template refuses any system message after index 0
        # ("System message must be at the beginning") and a local GGUF answered
        # the old shape with a 500.
        tail = f"{BTS_ACTION_PROTOCOL}\n\n{registry_palette(self._log, session_id)}"
        if reconcile:
            tail = f"{tail}\n\n{RECONCILE_DIRECTIVE}"
        # ``insert_engine_envelope`` copies: the caller's list is never mutated.
        seeded, _def_end, _tail_at = insert_engine_envelope(
            messages, len(messages), tail
        )
        # The envelope module's own provenance rails: engine-authored content
        # reaches neither the player's message space nor a post-prefix system
        # message (both RAISE, naming the offending index).
        assert_envelope_not_in_user_scope(seeded, tail)
        assert_envelope_not_in_system_scope(seeded, tail)
        watcher = _FinishWatch(self._caller)
        loop_result = await run_agent_loop(
            caller=watcher,
            messages=seeded,
            tools=bts_loop_tools(self._log, session_id, turn),
            surface="bts",
            log=self._log,
            session_id=session_id,
            timeout_s=self._timeout(),
            # W9.25 §3.5.3: the BTS slot's declared Context Size, as the passive
            # headroom the loop's rows carry (None = undeclared = today's rows).
            window_fn=self._window_fn,
            # The profile being resolved IS the ladder's "this connection does
            # function calling"; the runtime then builds the wire request itself
            # (tools + ``tool_choice: "auto"`` — never forced).
            capabilities={"function_calling": True},
            return_thinking=self._return_thinking(),
        )
        self.last_latency_s = sum(item.latency_s for item in loop_result.iterations)
        self.last_summary = loop_result.text
        if loop_result.timed_out:
            # The owner's per-connection doctrine: the transport's timeout raised
            # through the loop, which recorded the failed iteration and returned.
            # Re-trying here would buy a second identical wait, not a turn. The
            # actions that already landed stand (§6).
            raise BtsExtractionError(
                "BTS action loop unusable: the model call timed out "
                "(per-connection transport bound; no repair retry follows)",
                raw=loop_result.text,
            )
        if watcher.finish_reason == "length" and turn.actions == 0:
            # D86: a truncated completion is never trusted, and re-running it on
            # the same budget cannot help. With actions already appended the turn
            # has something true to commit, so only an EMPTY truncated run fails.
            raise BtsExtractionError(
                "BTS action loop unusable: the completion was truncated "
                "(finish_reason=length) before any action was recorded",
                raw=loop_result.text,
            )
        if turn.actions == 0 and not loop_result.text.strip():
            raise BtsExtractionError(
                "BTS action loop unusable: no action was recorded and the model "
                "wrote no closing summary",
                raw="",
            )
        return BtsResult(
            kind="delta",
            raw=loop_result.text,
            actions=turn.actions,
            rejected=turn.rejected,
            flags=turn.flags,
            mentioned=tuple(turn.mentions),
            references=tuple(
                BtsReference(kind=REFERENCE_IDENTITY, name=name)
                for name in turn.mentions
            ),
        )

    async def reconcile_now(
        self, session_id: str, *, user_msg: str = ""
    ) -> dict[str, Any]:
        """Run ONE reconciliation pass NOW, off-turn (the owner's check-state door).

        The cadence's pass (:data:`RECONCILE_CADENCE`) is a per-turn decision taken
        inside :meth:`_action_call`; this is the same engine half driven on demand,
        with no turn behind it. The synthetic turn state is the historian's
        off-turn window pattern in miniature: an EMPTY user message, the newest
        committed narrative's prose as the evidence, ``MODE: delta`` (no
        checkpoint), and no prior state — the registry fold is current by
        construction (W9.29 §6).

        The loop carries ONLY the reconcile tool (:func:`reconcile_loop_tools`), so
        the model states what it believes instead of reading the registry back. The
        tool appends one ``system``/``validation_notice`` row per disagreement
        through the same append the cadence's pass uses, and the registry is never
        written (req. 12).

        Returns ``{"flags": [<drift row>, ...], "checked": <beliefs stated>}``.
        Raises :class:`BtsExtractionError` when the pass cannot say anything (no
        function calling, no committed narrative, a transport timeout, or a
        completion that stated no beliefs) — the API door turns that into its soft
        failure, never a 500.
        """
        from ..agents.loop import run_agent_loop
        from .entity_tools import EntityTurn

        if self._profile() is None:
            raise BtsExtractionError(
                "reconciliation unavailable: this connection does not support "
                "function calling (W9.29 §11.2)",
                raw="",
                stage="tier_d",
            )
        if self._log is None:
            raise BtsExtractionError(
                "reconciliation unavailable: the runner has no event log", raw=""
            )
        narrative = last_narrative_prose(self._log, session_id)
        if not narrative:
            raise BtsExtractionError(
                "reconciliation unavailable: this session has no committed "
                "narrative to check",
                raw="",
            )
        turn = EntityTurn(self._log, session_id, reconcile=True)
        messages = self._messages(user_msg, narrative, None, False, session_id)
        # The directive IS the tail here: the action protocol and the registry
        # palette would both be wrong for this pass (it offers no write actions,
        # and showing the registry would make every belief a copy of it). Issue
        # #67: the tail rides the same assistant carrier as every other engine
        # tail — immediately before the final user message, never a second system.
        seeded, _def_end, _tail_at = insert_engine_envelope(
            messages, len(messages), RECONCILE_DIRECTIVE
        )
        assert_envelope_not_in_user_scope(seeded, RECONCILE_DIRECTIVE)
        assert_envelope_not_in_system_scope(seeded, RECONCILE_DIRECTIVE)
        watcher = _FinishWatch(self._caller)
        loop_result = await run_agent_loop(
            caller=watcher,
            messages=seeded,
            tools=reconcile_loop_tools(turn),
            surface="bts",
            log=self._log,
            session_id=session_id,
            timeout_s=self._timeout(),
            window_fn=self._window_fn,
            capabilities={"function_calling": True},
            return_thinking=self._return_thinking(),
        )
        self.last_latency_s = sum(item.latency_s for item in loop_result.iterations)
        self.last_summary = loop_result.text
        self.last_turn = turn
        self.last_reconcile = True
        if loop_result.timed_out:
            raise BtsExtractionError(
                "reconciliation pass timed out (per-connection transport bound)",
                raw=loop_result.text,
            )
        if turn.beliefs == 0:
            raise BtsExtractionError(
                "reconciliation pass stated no beliefs — nothing was checked",
                raw=loop_result.text,
            )
        return {"flags": list(turn.drift), "checked": turn.beliefs}
