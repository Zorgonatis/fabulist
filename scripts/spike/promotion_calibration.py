#!/usr/bin/env python
"""W9.22 Wave 0a — promotion-gate calibration spike against the LIVE d1-3b.

WHY this script exists
----------------------
``the maintainer's private notes (unshipped)`` §1 doctrine 3 lets ONE agent output
skip the owner: a high-signal promotion auto-creates a session actor
(``actor_promoted``). §2 puts the promotion gate in Wave 1 and §5 Wave 0a makes
the threshold a MEASURED number before the gate is built; reviewer finding 14
records the reason this cannot be harvested from existing data — recorded turns
carry no promotion labels, so there is no ground truth to calibrate against.
This spike manufactures that ground truth: four owner-specified scenarios whose
answer is known by construction, built through the REAL event shapes the engine
writes, then judged by the REAL decision model through the REAL client.

What is measured
----------------
The gate asks d1-3b ONE ``choice`` question (``promote`` vs ``presence``); its
probability for ``promoted`` is the ``signal`` float of the ``actor_assessed``
event in the cross-lane contract, so this spike measures exactly that number.
Four cases span the two criteria the gate must combine (§2: persistence across
turns + speaking directly to the user, versus ambient scene furniture):

* ``persistent-direct`` — introduced, speaks DIRECTLY to the player, persists
  into the next turn and speaks again. Ground truth: promotion-worthy.
* ``ambient-bartender`` — described behind the bar wiping a glass, no speech,
  no direct address, while another character is the turn's focus. Ground truth:
  NOT promotion-worthy (the owner's "furniture" case).
* ``one-shot-direct`` (gradient C) — speaks directly to the player once, then
  leaves. Address without persistence.
* ``silent-persistent`` (gradient D) — present and described for three turns,
  never speaks. Persistence without address.

The two gradients are what turn a bare threshold into a boundary: they sit on
opposite axes, so the report shows HOW the model reaches its answer, not just
that the two extremes separate.

Two judgment modes (steady state + cold start, §6 "Cold-start promotion")
-----------------------------------------------------------------------
1. ``evidence`` — the steady-state gate input: the candidate's evidence summary
   (turns present, the last world-state bucket, attributed quotes). It carries
   NO narration, so the verdict is a bucket judgment rather than prose reading.
2. ``cold-start`` — the fallback §5 Wave 0a asks for: when a candidate has one
   turn of evidence the bucket features are ~zero, so the state is the
   candidate's FIRST turn text (the player's line + that turn's narration).
   Both modes use the SAME question and the SAME threshold; the cold-start rule
   the report recommends is the one that keeps a turn-1 verdict provisional.

Honesty rules (this is calibration, not a demo)
-----------------------------------------------
* Every session is written to a TEMP ``EventLog`` through the writers the
  engine itself uses — ``system``/``card`` + ``ensure_roster`` for the seed
  cast, ``user``/``narrative`` per turn, the exact ``system``/``world_state``
  payload ``_run_world_state`` appends, ``apply_bts_cast_proposals`` for the
  BTS admit path (not a hand-minted member), and the ``system``/``attribution``
  row shape ``_run_attribution`` writes.
* The evidence summary is computed from the ENGINE'S OWN reads
  (``presence_walk`` / ``member_categories`` / ``attribution`` rows), and the
  per-turn presence fold is cross-checked against ``PresenceWalk.is_present``;
  a disagreement ABORTS the run, because a wrong evidence read invalidates
  every number below it.
* The endpoint is read from the same runtime config the engine reads
  (``configs/connections.json`` via ``RegistryStore`` — the exact object
  ``scripts/serve.py`` hands to ``build_app``). No key, host or model is
  hardcoded here.
* If the endpoint is unreachable, or any case yields no usable answer, the run
  REPORTS the failure and refuses to recommend a threshold. It never fills in
  numbers it did not observe.

What this script does NOT do
----------------------------
It applies no threshold, writes no event, and mutates no asset: it reads a
config, writes a temp SQLite log, posts questions, and prints a report. The
gate itself (Wave 1) owns the decision to promote.

Usage::

    uv run python scripts/spike/promotion_calibration.py [--reps 5]
        [--config configs/connections.json] [--timeout 120] [--states]

Exit codes: ``0`` report printed with a recommended threshold; ``1`` the model
gave no usable answers (no threshold recommended); ``2`` the config/binding
could not be resolved (the spike cannot run at all).
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import math
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fabulist.connections.registry import ConnectionConfigError, RegistryStore
from fabulist.decision import DecisionCaller
from fabulist.log import EventLog
from fabulist.state.bts import apply_bts_cast_proposals
from fabulist.state.cast import (
    CastMember,
    admit_member,
    canonical_key,
    ensure_roster,
    member_by_name,
    roster,
)
from fabulist.state.presence import PresenceWalk, member_categories, presence_walk

__all__ = [
    "CASES",
    "DEFAULT_CONFIG",
    "DEFAULT_REPS",
    "DEFAULT_TIMEOUT",
    "EXISTING_CAST",
    "PROMOTION_QUESTION",
    "SIGNAL_CHOICE",
    "Case",
    "CaseResult",
    "Evidence",
    "RepResult",
    "Turn",
    "collect_evidence",
    "main",
    "measure_case",
    "recommend",
    "render_cold_start_state",
    "render_evidence_state",
    "seed_session",
]

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The runtime config the engine loads (``scripts/serve.py --config``). Read at
#: run time, NEVER parsed here: the decision binding lives in the user's
#: gitignored state and may point anywhere.
DEFAULT_CONFIG = REPO_ROOT / "configs" / "connections.json"

#: Repetitions per case (plan §5 Wave 0a: sampling stability). d1-3b emits no
#: tokens, so identical input SHOULD be identical — a non-zero spread is a
#: finding about the router/model, not noise the report may average away.
DEFAULT_REPS = 5

#: Per-request ceiling. The first request to a sleeping router child pays that
#: child's model load (``scripts/spike/d1_systemone.py`` notes this), so the
#: spike is deliberately more generous than a turn's 5s gate timeout.
DEFAULT_TIMEOUT = 120.0

#: The class name whose probability IS the ``actor_assessed.signal`` float. It
#: matches the ``actor_assessed`` verdict vocabulary in the W9.22 cross-lane
#: contract (``"promoted"`` | ``"presence"``), so the gate passes the choice
#: through without a translation table.
SIGNAL_CHOICE = "promoted"

#: The ambient/scene-furniture class (the abstain side of the gate).
PRESENCE_CHOICE = "presence"

#: The promotion gate's question — the ONE the spike calibrates.
#:
#: Wording follows the ``GENRE_QUESTION`` precedent in ``fabulist/decision.py``:
#: d1-3b answers a typed choice in one forward pass and the CRITERIA carry the
#: concrete sign-lists, so the judgment is the model's and the signs are ours.
#: The two criteria are the plan's two §2 promotion criteria and nothing else —
#: persistence across turns AND speaking directly to the player (an addressed
#: quote) versus ambient description. Rewording this invalidates every number
#: in the report (identical input -> identical probabilities), so the gate must
#: ship this payload byte-identically or re-run the spike.
PROMOTION_QUESTION: dict[str, Any] = {
    "type": "choice",
    "instructions": (
        "A character tracked by the engine is being considered for promotion "
        "into the recurring cast. Decide from the evidence whether this is a "
        "character the story is building or ambient scene furniture. Answer "
        "'promoted' only when the evidence shows BOTH persistence across turns "
        "AND the candidate speaking directly to the player. Answer 'presence' "
        "otherwise."
    ),
    "criteria": {
        "promoted": (
            "The candidate is present in more than one turn AND at least one "
            "attributed quote speaks directly to the player (addresses 'you', "
            "answers the player's line, or offers/gives something to the "
            "player)."
        ),
        "presence": (
            "Ambient scene furniture: described in passing, present in only one "
            "turn, or never speaks to the player."
        ),
    },
}


# ---------------------------------------------------------------------------
# the ground-truth scenarios (owner-specified; prose is load-bearing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One synthetic turn, in the order the engine writes its events.

    ``venue``/``state``/``off_screen`` are exactly the BTS world-state fields
    ``_run_world_state`` persists (``state`` is the per-character category
    bucket the gate reads through :func:`member_categories`). ``speakers`` is
    the attribution rows for the turn — ``(speaker, quote)`` — so a candidate
    either has attributed quotes or provably does not.
    """

    user: str
    prose: str
    venue: dict[str, dict[str, str]]
    state: dict[str, dict[str, dict[str, str]]]
    speakers: tuple[tuple[str, str], ...] = ()
    off_screen: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Case:
    """One labelled scenario: a session whose promotion answer is known."""

    case_id: str
    ground_truth: str  # SIGNAL_CHOICE or PRESENCE_CHOICE
    rationale: str
    candidate: str
    turns: tuple[Turn, ...]


#: The card-captured cast member every case already has. He is the "DIFFERENT
#: character ... that turn's main focus" of the furniture scenario, and he is a
#: card seed (provenance ``card``) so nothing about him is learned in play.
EXISTING_CAST = "Elias"

_SALTED_GULL = "the Salted Gull"

_ELIAS_VENUE = {"venue": _SALTED_GULL, "area": "corner table by the hearth"}
_ELIAS_STATE = {
    "social": {"role": "guide", "standing": "paid by the week"},
    "emotional": {"mood": "wary"},
}

CASES: tuple[Case, ...] = (
    # --- the positive -----------------------------------------------------
    Case(
        case_id="persistent-direct",
        ground_truth=SIGNAL_CHOICE,
        rationale=(
            "introduced in turn 2 speaking directly to the player, persists into "
            "turn 3 and speaks to the player again"
        ),
        candidate="Marlow",
        turns=(
            Turn(
                user=(
                    "I shoulder the door open and stand in it until my eyes "
                    "adjust to the lamplight."
                ),
                prose=(
                    "Rain runs off the hem of your coat and pools on the boards. "
                    "Elias has the table nearest the hearth, a cup at his elbow "
                    "and a map he is not reading. He looks up at the draught you "
                    "have let in, and does not look away."
                ),
                venue={EXISTING_CAST: _ELIAS_VENUE},
                state={EXISTING_CAST: _ELIAS_STATE},
                speakers=(
                    (
                        EXISTING_CAST,
                        "You're late. The river road is a bad road after dark.",
                    ),
                ),
            ),
            Turn(
                user=(
                    "I cross to the bar and ask whether they have anything hot "
                    "that isn't already spoken for."
                ),
                prose=(
                    "The innkeeper — Marlow, by the name chalked over the taps — "
                    "sets down the cup she was drying before you finish the "
                    "question. She pours without asking what you want and slides "
                    "the cup across the boards to your hand. \"You look like "
                    "you've been walking all night,\" she says, and she does not "
                    "look at Elias while she says it."
                ),
                venue={
                    "Marlow": {"venue": _SALTED_GULL, "area": "behind the bar"},
                    EXISTING_CAST: _ELIAS_VENUE,
                },
                state={
                    "Marlow": {
                        "social": {"role": "innkeeper", "station": "behind the bar"},
                        "appearance": {"hair": "dark, tied back"},
                        "emotional": {"mood": "tired but direct"},
                    },
                    EXISTING_CAST: _ELIAS_STATE,
                },
                speakers=(
                    ("Marlow", "You look like you've been walking all night."),
                ),
            ),
            Turn(
                user="I take the cup to Elias's table and sit down across from him.",
                prose=(
                    "Elias folds the map away and waits, and it is a while before "
                    "either of you says anything worth the name. Marlow comes out "
                    "from behind the bar with a bowl of stew and sets it in front "
                    "of you herself, which is not work a keeper usually does. "
                    "\"I'll bring the bread when it's out of the oven,\" she says, "
                    "and goes back to the bar."
                ),
                venue={
                    "Marlow": {"venue": _SALTED_GULL, "area": "common room"},
                    EXISTING_CAST: _ELIAS_VENUE,
                },
                state={
                    "Marlow": {
                        "social": {"role": "innkeeper", "station": "serving the room"},
                        "emotional": {"mood": "attentive"},
                    },
                    EXISTING_CAST: _ELIAS_STATE,
                },
                speakers=(
                    ("Marlow", "I'll bring the bread when it's out of the oven."),
                ),
            ),
        ),
    ),
    # --- the negative: the owner's "bartender cleaning a glass" ------------
    Case(
        case_id="ambient-bartender",
        ground_truth=PRESENCE_CHOICE,
        rationale=(
            "described behind the bar wiping a glass in turn 2 with no speech "
            "and no direct address, while Elias is the turn's focus; gone in "
            "turn 3"
        ),
        candidate="Orrin",
        turns=(
            Turn(
                user=(
                    "I come in out of the rain and stand by the fire until the "
                    "water has stopped running off my sleeve."
                ),
                prose=(
                    "Elias is where he always is, at the corner table, with his "
                    "back to the room's one good wall. He lifts two fingers from "
                    "his cup in greeting and pushes the other chair out with his "
                    "boot. The room smells of wet wool and old smoke."
                ),
                venue={EXISTING_CAST: _ELIAS_VENUE},
                state={EXISTING_CAST: _ELIAS_STATE},
                speakers=(
                    (EXISTING_CAST, "Sit down. You look half-drowned and twice as stubborn."),
                ),
            ),
            Turn(
                user="I sit and ask Elias what he has been digging at all week.",
                prose=(
                    "Elias talks the way he digs: steadily, in one long trench, "
                    "and it is a while before you notice he has told you nothing. "
                    "Behind the bar, the bartender works a cloth around the same "
                    "glass he has been holding since you came in, back and forth, "
                    "his eyes on the door and not on either of you. He does not "
                    "say a word."
                ),
                venue={
                    "Orrin": {"venue": _SALTED_GULL, "area": "behind the bar"},
                    EXISTING_CAST: _ELIAS_VENUE,
                },
                state={
                    "Orrin": {
                        "social": {"role": "bartender", "station": "behind the bar"},
                        "emotional": {"mood": "inattentive"},
                    },
                    EXISTING_CAST: _ELIAS_STATE,
                },
                speakers=(
                    (
                        EXISTING_CAST,
                        "The surveyors came in spring. They left in autumn. That is the whole of it.",
                    ),
                ),
            ),
            Turn(
                user="I lean in and ask Elias to show me the map.",
                prose=(
                    "Elias looks at the room, then at the bar, then unrolls the "
                    "map on the table between your cups. The bartender has gone "
                    "through to the cellar with an armful of bottles and pulled "
                    "the door to behind him, and nobody comes back out. Whatever "
                    "Elias is working through, he works through it quietly, and "
                    "the two of you are the only people still talking when the "
                    "lamps are trimmed."
                ),
                venue={EXISTING_CAST: _ELIAS_VENUE},
                state={EXISTING_CAST: _ELIAS_STATE},
                speakers=((EXISTING_CAST, "Keep your voice down."),),
                off_screen=(("Orrin", "the cellar"),),
            ),
        ),
    ),
    # --- gradient C: direct address, no persistence -----------------------
    Case(
        case_id="one-shot-direct",
        ground_truth=PRESENCE_CHOICE,
        rationale=(
            "speaks directly to the player once in turn 2 and is gone in turn 3 "
            "(address without persistence)"
        ),
        candidate="Wren",
        turns=(
            Turn(
                user="I wait by the town gate with the lantern turned low.",
                prose=(
                    "Elias is already there, sitting on the mounting block with "
                    "his elbows on his knees. The gate stands open because nobody "
                    "has thrown a lock on it in twenty years, and both of you are "
                    "listening to the road."
                ),
                venue={"Elias": {"venue": "the town gate", "area": "mounting block"}},
                state={EXISTING_CAST: _ELIAS_STATE},
                speakers=(
                    (EXISTING_CAST, "If it were coming by the road we would have heard it by now."),
                ),
            ),
            Turn(
                user="I hear boots on the gravel and lift the lantern.",
                prose=(
                    "A courier in a grey coat walks out of the dark without "
                    "hurrying and stops at the edge of your light. Her name is "
                    "Wren, and she does not offer her hand. She holds out a "
                    "folded paper, rain-spotted at the corner. \"You'd better "
                    "read it before the bell,\" she says, and then she is past "
                    "you, and past the gate, and gone down the road she came by."
                ),
                venue={
                    "Wren": {"venue": "the town gate", "area": "the lantern light"},
                    "Elias": {"venue": "the town gate", "area": "mounting block"},
                },
                state={
                    "Wren": {
                        "social": {"role": "courier", "station": "on the road"},
                        "appearance": {"coat": "grey, rain-spotted"},
                    },
                    EXISTING_CAST: _ELIAS_STATE,
                },
                speakers=(("Wren", "You'd better read it before the bell."),),
            ),
            Turn(
                user="I break the seal and read it twice.",
                prose=(
                    "Elias lights his own lantern and reads it over your shoulder, "
                    "swearing softly at the second line. Down the road the sound "
                    "of boots is gone, swallowed by the rain. The gate still "
                    "stands open, and nobody comes through it."
                ),
                venue={"Elias": {"venue": "the town gate", "area": "mounting block"}},
                state={EXISTING_CAST: _ELIAS_STATE},
                speakers=((EXISTING_CAST, "That is the mayor's seal."),),
                off_screen=(("Wren", "the road north"),),
            ),
        ),
    ),
    # --- gradient D: persistence, no address ------------------------------
    Case(
        case_id="silent-persistent",
        ground_truth=PRESENCE_CHOICE,
        rationale=(
            "present and described in every turn from turn 2 to the end, never "
            "speaks (persistence without address)"
        ),
        candidate="Sable",
        turns=(
            Turn(
                user="I take my usual place at the back of the hall.",
                prose=(
                    "The hall is full and the speaker at the front is proud of his "
                    "arithmetic. Elias is up near the front where the guild likes "
                    "him to be seen, and he does not look back at you."
                ),
                venue={
                    "Elias": {"venue": "the guild hall", "area": "near the front"},
                },
                state={EXISTING_CAST: _ELIAS_STATE},
                speakers=((EXISTING_CAST, "Sit where you like. I'll find you after."),),
            ),
            Turn(
                user="I watch the room instead of the speaker.",
                prose=(
                    "Sable is at the back wall, where the lamps do not reach, with "
                    "her arms folded and her shoulders against the plaster. She "
                    "has been there since the reading began and has not moved to "
                    "sit. When your eyes cross the room she is watching the room — "
                    "not you, not the speaker — and she says nothing."
                ),
                venue={
                    "Elias": {"venue": "the guild hall", "area": "near the front"},
                    "Sable": {"venue": "the guild hall", "area": "the back wall"},
                },
                state={
                    "Sable": {
                        "appearance": {"coat": "grey, worn at the cuffs"},
                        "emotional": {"mood": "watchful", "patience": "unhurried"},
                    },
                    EXISTING_CAST: _ELIAS_STATE,
                },
            ),
            Turn(
                user="I shift my chair back to hear better.",
                prose=(
                    "The speaker is reading the accounts now, and the numbers are "
                    "the kind that get a guild clerk hanged. Sable has not shifted "
                    "with the crowd. Her arms are still folded; her eyes still go "
                    "from the door to the speaker and back again, measuring "
                    "something she does not say out loud."
                ),
                venue={
                    "Elias": {"venue": "the guild hall", "area": "near the front"},
                    "Sable": {"venue": "the guild hall", "area": "the back wall"},
                },
                state={
                    "Sable": {
                        "appearance": {"coat": "grey, worn at the cuffs"},
                        "emotional": {"mood": "watchful"},
                    },
                    EXISTING_CAST: _ELIAS_STATE,
                },
            ),
            Turn(
                user="The hall empties and I stand up to go.",
                prose=(
                    "Sable leaves the wall at last and walks the length of the "
                    "hall, pulling the shutters to one by one while the last of "
                    "the guild men file out. She passes you on her way back to the "
                    "door, close enough that you could have put a hand on her "
                    "sleeve, and says nothing at all. The lamps are already being "
                    "put out behind her."
                ),
                venue={
                    "Elias": {"venue": "the guild hall", "area": "near the front"},
                    "Sable": {"venue": "the guild hall", "area": "by the door"},
                },
                state={
                    "Sable": {
                        "appearance": {"coat": "grey, worn at the cuffs"},
                        "emotional": {"mood": "watchful"},
                    },
                    EXISTING_CAST: _ELIAS_STATE,
                },
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# building the sessions through the engine's own writers
# ---------------------------------------------------------------------------


def _chain(log: EventLog, session_id: str) -> list[Any]:
    """The session's fork-aware event chain, in seq order (the engine's read)."""
    return sorted(log.get_chain_events(session_id), key=lambda event: event.seq)


def _append_user(log: EventLog, session_id: str, text: str) -> None:
    """The ``user`` event ``graph/nodes.py`` writes when it commits the turn."""
    log.append_event(session_id, "user", "user", {"text": text})


def _append_narrative(log: EventLog, session_id: str, prose: str) -> None:
    """The ``narrative`` event the narrator commit appends (prose + segments)."""
    log.append_event(
        session_id,
        "narrative",
        "narrator",
        {"prose": prose, "segments": [{"kind": "prose", "text": prose}]},
    )


def _append_world_state(log: EventLog, session_id: str, turn: Turn) -> dict[str, Any]:
    """Append one BTS ``system``/``world_state`` payload, then run its admit pass.

    Mirrors ``_run_world_state`` field for field (``kind``/``state``/``venue``/
    ``off_screen``/``threads``/``tension``/``raw``) and then calls the REAL
    producer ``apply_bts_cast_proposals`` over the same result dict, so the
    candidate enters the roster through the BTS admit path — not a hand-minted
    member — exactly as a live turn would admit it.
    """
    result: dict[str, Any] = {
        "kind": "delta",
        "state": dict(turn.state),
        "venue": dict(turn.venue),
        "off_screen": [
            {"name": name, "location": location} for name, location in turn.off_screen
        ],
        "threads": [],
        "tension": {},
        "raw": "",
    }
    log.append_event(
        session_id,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": result["kind"],
            "state": result["state"],
            "venue": result["venue"],
            "off_screen": result["off_screen"],
            "threads": result["threads"],
            "tension": result["tension"],
            "raw": result["raw"],
        },
    )
    apply_bts_cast_proposals(log, session_id, result)
    return result


def _append_attribution(
    log: EventLog, session_id: str, speakers: tuple[tuple[str, str], ...]
) -> None:
    """Append one ``system``/``attribution`` row set the way ``_run_attribution`` does.

    A speaker with no resolved id is admitted here through the same
    ``admit_member`` call the live writer makes (``provenance="extractor"``,
    ``admitted_by="attribution"``), and the row carries ``speaker_id`` — so the
    spike's rows join to members exactly like production rows. Turns where
    nobody speaks still append the empty event: presence.py treats the event's
    existence as evidence that the pass ran.
    """
    rows: list[dict[str, str]] = []
    for speaker, quote in speakers:
        speaker_id = admit_member(
            log,
            session_id,
            speaker,
            provenance="extractor",
            admitted_by="attribution",
        )
        rows.append({"speaker": speaker, "quote": quote, "speaker_id": speaker_id})
    log.append_event(
        session_id,
        "system",
        "attribution",
        {"event": "attribution", "attributions": rows},
    )


def seed_session(log: EventLog, session_id: str, case: Case) -> None:
    """Write one case as a session, turn by turn, in the engine's write order.

    ``system``/``card`` + ``ensure_roster`` seed the existing cast (the same
    lazy bootstrap the dispatcher performs), then each turn appends
    ``user`` -> ``narrative`` -> ``system``/``world_state`` (+ BTS admit) ->
    ``system``/``attribution``. That is the post-turn order W9.21 established
    (BTS before attribution), so an attribution row's admit resolves against
    the member BTS already created instead of minting a second one.
    """
    log.append_event(
        session_id,
        "system",
        "system",
        {
            "card": {
                "name": EXISTING_CAST,
                "description": f"{EXISTING_CAST} is a river-town guide, already in the cast.",
            },
            "first_mes": "",
        },
    )
    ensure_roster(log, session_id)
    for turn in case.turns:
        _append_user(log, session_id, turn.user)
        _append_narrative(log, session_id, turn.prose)
        _append_world_state(log, session_id, turn)
        _append_attribution(log, session_id, turn.speakers)


# ---------------------------------------------------------------------------
# the evidence summary (the gate's state), from the engine's own reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One candidate's promotion evidence, as the gate would read it.

    ``present_turns``/``absent_turns`` are 1-based turn numbers (a turn is the
    count of narratives at or before an event — the convention
    ``presence.lane_history`` already uses). ``bucket`` is
    :func:`member_categories`' answer (the last world-state bucket), ``quotes``
    are the attributed quotes with their turn numbers, and ``present_now`` /
    ``fold_present_last`` are the two independent presence answers the run
    cross-checks (``PresenceWalk.is_present`` vs the per-turn fold).
    """

    candidate: str
    member_id: str
    display_name: str
    aliases: tuple[str, ...]
    turn_count: int
    first_turn: int | None
    present_turns: tuple[int, ...]
    absent_turns: tuple[int, ...]
    bucket: dict[str, dict[str, Any]]
    quotes: tuple[tuple[int, str], ...]
    present_now: bool
    fold_present_last: bool


def _turn_of(event_seq: int, narrative_seqs: list[int]) -> int:
    """1-based turn number of an event: narratives appended at or before it."""
    return bisect.bisect_right(narrative_seqs, event_seq)


def _per_turn_presence(
    log: EventLog, session_id: str, member: CastMember
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """``(present_turns, absent_turns)`` folded with the presence-walk rule.

    The engine's public read (:func:`fabulist.state.presence.witnessed_turns`)
    answers only for the freshness WINDOW and only from the member's LAST
    presence evidence, but the gate must count persistence over the whole
    session ("2 of 3 turns", "absent in turn 3"). So this folds the same
    evidence the walk folds — a name under ``venue``/``state`` is presence, a
    row in ``off_screen`` is absence, a ``checkpoint`` replaces the accumulated
    maps — and keeps the walk's last-evidence-wins rule WITHIN a turn (the max
    seq of each kind), which is why both event classes carry their seq here.
    :func:`collect_evidence` asserts the fold's answer for the last turn equals
    ``PresenceWalk.is_present`` so a drift between this read and the engine's is
    loud instead of silent.
    """
    keys = {canonical_key(surface) for surface in member.surface_forms()}
    events = _chain(log, session_id)
    narrative_seqs = [event.seq for event in events if event.type == "narrative"]
    present_seq: dict[int, dict[str, int]] = {}
    absent_seq: dict[int, dict[str, int]] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type != "system" or payload.get("event") != "world_state":
            continue
        turn = _turn_of(event.seq, narrative_seqs)
        if payload.get("kind") == "checkpoint":
            present_seq.clear()
            absent_seq.clear()
        venue = payload.get("venue") if isinstance(payload.get("venue"), dict) else {}
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        for name in (*venue, *state):
            key = canonical_key(name)
            if key in keys:
                present_seq.setdefault(turn, {})[key] = event.seq
        rows = payload.get("off_screen") if isinstance(payload.get("off_screen"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = canonical_key(row.get("name"))
            if key in keys:
                absent_seq.setdefault(turn, {})[key] = event.seq
    present_turns: list[int] = []
    absent_turns: list[int] = []
    for turn in sorted(set(present_seq) | set(absent_seq)):
        for key in keys:
            last_present = present_seq.get(turn, {}).get(key, -1)
            last_absent = absent_seq.get(turn, {}).get(key, -1)
            if last_absent >= 0 and last_absent >= last_present:
                if turn not in absent_turns:
                    absent_turns.append(turn)
            elif last_present >= 0:
                if turn not in present_turns:
                    present_turns.append(turn)
    return tuple(present_turns), tuple(absent_turns)


def _attributed_quotes(
    log: EventLog, session_id: str, member: CastMember
) -> tuple[tuple[int, str], ...]:
    """``(turn, quote)`` for every ``system``/``attribution`` row this member spoke.

    Rows are matched by ``speaker_id`` when the writer persisted it (the real
    shape carries it) and by canonical speaker name otherwise, so an
    unresolved-id row still joins. Quotes stay in log order.
    """
    keys = {canonical_key(surface) for surface in member.surface_forms()}
    events = _chain(log, session_id)
    narrative_seqs = [event.seq for event in events if event.type == "narrative"]
    quotes: list[tuple[int, str]] = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type != "system" or payload.get("event") != "attribution":
            continue
        rows = payload.get("attributions")
        if not isinstance(rows, list):
            continue
        turn = _turn_of(event.seq, narrative_seqs)
        for row in rows:
            if not isinstance(row, dict):
                continue
            speaker_id = row.get("speaker_id")
            if isinstance(speaker_id, str) and speaker_id:
                matches = speaker_id == member.member_id
            else:
                matches = canonical_key(row.get("speaker")) in keys
            quote = row.get("quote")
            if matches and isinstance(quote, str) and quote.strip():
                quotes.append((turn, quote.strip()))
    return tuple(quotes)


def collect_evidence(log: EventLog, session_id: str, case: Case) -> Evidence:
    """Read the candidate's promotion evidence out of the log (engine reads only)."""
    members = roster(log, session_id)
    member = member_by_name(members, case.candidate)
    if member is None:
        raise RuntimeError(
            f"{case.case_id}: candidate {case.candidate!r} never reached the roster — "
            "the BTS admit path did not run, so the evidence read would be empty"
        )
    walk: PresenceWalk = presence_walk(log, session_id)
    present_turns, absent_turns = _per_turn_presence(log, session_id, member)
    turn_count = len(walk.narrative_seqs)
    present_now = any(
        walk.is_present(surface) for surface in member.surface_forms()
    )
    fold_present_last = turn_count in present_turns
    return Evidence(
        candidate=case.candidate,
        member_id=member.member_id,
        display_name=member.display_name,
        aliases=tuple(member.aliases),
        turn_count=turn_count,
        first_turn=present_turns[0] if present_turns else None,
        present_turns=present_turns,
        absent_turns=absent_turns,
        bucket=member_categories(log, session_id, member.member_id),
        quotes=_attributed_quotes(log, session_id, member),
        present_now=present_now,
        fold_present_last=fold_present_last,
    )


def _render_bucket(bucket: dict[str, dict[str, Any]]) -> list[str]:
    """Render the accumulated world-state bucket as ``category: key=value`` lines."""
    lines: list[str] = []
    for category in sorted(bucket):
        attrs = bucket[category]
        if not isinstance(attrs, dict) or not attrs:
            continue
        pairs = "; ".join(f"{key}={value}" for key, value in sorted(attrs.items()))
        lines.append(f"  {category}: {pairs}")
    return lines or ["  (no attributes recorded)"]


def render_evidence_state(evidence: Evidence) -> str:
    """The steady-state gate input: bucket facts + attributed quotes, no prose.

    Deliberately narration-free: the gate runs post-turn on a candidate the BTS
    bucket already describes, and the spike measures a BUCKET judgment. Nothing
    here labels the answer — the words "promote"/"presence" appear only in the
    question's criteria, never in its state (a leaked label would calibrate
    nothing).
    """
    present = ", ".join(str(turn) for turn in evidence.present_turns) or "none"
    absent = ", ".join(str(turn) for turn in evidence.absent_turns) or "none"
    aliases = ", ".join(evidence.aliases) or "none"
    quotes = (
        "\n".join(f'turn {turn}: "{quote}"' for turn, quote in evidence.quotes)
        if evidence.quotes
        else "none"
    )
    lines = [
        "<candidate>",
        f"name: {evidence.display_name}",
        f"aliases: {aliases}",
        f"scene turns so far: {evidence.turn_count}",
        f"first seen: turn {evidence.first_turn}",
        f"turns present: {present} ({len(evidence.present_turns)} of {evidence.turn_count} scene turns)",
        f"turns absent: {absent}",
        "world state bucket:",
        *_render_bucket(evidence.bucket),
        "</candidate>",
        "",
        "<attributed quotes>",
        quotes,
        "</attributed quotes>",
    ]
    return "\n".join(lines)


def render_cold_start_state(log: EventLog, session_id: str, evidence: Evidence) -> str:
    """The cold-start gate input: the candidate's FIRST turn text, read from the log.

    Plan §5 Wave 0a asks for "a cold-start rule (turn-text judgment when bucket
    features are ~zero)". A first-sighting candidate has no persistence to
    count, so the only honest evidence is the turn itself: the player's line
    and the narration of the candidate's first present turn. Both are read back
    out of the log (not out of the case fixture), so the fallback is measured on
    the same artifact the gate would see.
    """
    if evidence.first_turn is None:
        raise RuntimeError(
            f"{evidence.candidate}: no presence evidence, so there is no first turn "
            "to judge at cold start"
        )
    events = _chain(log, session_id)
    narrative_seqs = [event.seq for event in events if event.type == "narrative"]
    prose = ""
    narrative_seq: int | None = None
    for event in events:
        if event.type != "narrative":
            continue
        if _turn_of(event.seq, narrative_seqs) == evidence.first_turn:
            payload = event.payload if isinstance(event.payload, dict) else {}
            prose = str(payload.get("prose") or "")
            narrative_seq = event.seq
            break
    user_line = ""
    if narrative_seq is not None:
        for event in events:
            if event.type == "user" and event.seq < narrative_seq:
                payload = event.payload if isinstance(event.payload, dict) else {}
                user_line = str(payload.get("text") or "")
    return "\n".join(
        [
            "<candidate>",
            f"name: {evidence.display_name}",
            f"first seen: turn {evidence.first_turn} of {evidence.turn_count}",
            "</candidate>",
            "",
            "<turn text>",
            f"the player wrote: {user_line}",
            "the turn read:",
            prose,
            "</turn text>",
        ]
    )


# ---------------------------------------------------------------------------
# the judgment, N repetitions, through the real client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepResult:
    """One decision call: the answer, the signal, and how long it took."""

    choice: str | None
    signal: float | None
    probabilities: dict[str, float]
    latency_s: float | None
    error: str | None


@dataclass(frozen=True)
class CaseResult:
    """One case in one judgment mode: its state text and every repetition."""

    case: Case
    evidence: Evidence
    mode: str
    state: str
    reps: tuple[RepResult, ...] = field(default_factory=tuple)

    def signals(self) -> list[float]:
        return [rep.signal for rep in self.reps if rep.signal is not None]

    def mean_signal(self) -> float | None:
        values = self.signals()
        return sum(values) / len(values) if values else None

    def spread(self) -> float | None:
        values = self.signals()
        return (max(values) - min(values)) if values else None


def _signal_from_answer(answer: Any) -> tuple[str | None, float | None, dict[str, float]]:
    """Pull ``(choice, P(signal), probabilities)`` out of one choice answer.

    Strict on shape, like ``DecisionCaller.classify_genre``: a non-float
    probability is "no answer", never a coerced 0/1. The signal is the
    probability of :data:`SIGNAL_CHOICE`; a response that omits it is unusable
    for calibration even when the model's argmax choice is ``promoted``.
    """
    if not isinstance(answer, dict):
        return None, None, {}
    choice = answer.get("choice")
    probabilities_raw = answer.get("probabilities")
    probabilities: dict[str, float] = {}
    if isinstance(probabilities_raw, dict):
        for name, value in probabilities_raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            probabilities[str(name)] = float(value)
    signal = probabilities.get(SIGNAL_CHOICE)
    return (
        choice if isinstance(choice, str) else None,
        signal,
        probabilities,
    )


async def measure_case(
    registry: Any,
    case: Case,
    evidence: Evidence,
    mode: str,
    state: str,
    reps: int,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> CaseResult:
    """Ask the promotion question ``reps`` times over one state text.

    The caller is built the way ``wiring.build_app`` builds the engine's: from
    the registry, with an observer — here the observer collects one record per
    call, which is where latency and errors come from (the same seam the engine
    logs decision usage rows through). One question per call (the gate's own
    shape). Repeated identical calls are the sampling-stability probe: d1-3b
    generates no tokens, so a spread is a router/model finding.
    """
    records: list[dict[str, Any]] = []
    caller = DecisionCaller(registry, timeout=timeout, observer=records.append)
    results: list[RepResult] = []
    for _ in range(reps):
        before = len(records)
        answers = await caller.ask(state, {"promotion": PROMOTION_QUESTION})
        record = records[before] if len(records) > before else None
        if record is None:
            record = {"ok": False, "error": "no observer record for this call"}
        answer = answers.get("promotion") if isinstance(answers, dict) else None
        choice, signal, probabilities = _signal_from_answer(answer)
        results.append(
            RepResult(
                choice=choice,
                signal=signal,
                probabilities=probabilities,
                latency_s=record.get("latency_s"),
                error=record.get("error") if not record.get("ok") else None,
            )
        )
    return CaseResult(
        case=case,
        evidence=evidence,
        mode=mode,
        state=state,
        reps=tuple(results),
    )


@dataclass(frozen=True)
class Recommendation:
    """The threshold the report recommends (or the reason it has none).

    ``cold_rule`` spells out what the gate does on a first-sighting candidate: the
    cold-start measurement decides whether the turn-text read may promote at all,
    so the rule is derived from the numbers rather than asserted.
    """

    threshold: float | None
    min_positive: float | None
    max_negative: float | None
    margin_below: float | None
    margin_above: float | None
    separated: bool
    reason: str
    cold_rule: str
    cold_start: dict[str, Any]


def _midpoint_threshold(low: float, high: float) -> float:
    """The separating threshold: midpoint of the highest negative and lowest positive.

    Rounded UP to two decimals (``floor(x*100 + 0.5)``) so the printed number is
    a value a gate can compare against without re-deriving it, and so rounding
    never moves the threshold INTO the positive cluster.
    """
    midpoint = (low + high) / 2.0
    return math.floor(midpoint * 100 + 0.5) / 100


def _separation(results: list[CaseResult]) -> dict[str, Any]:
    """Highest non-promotion mean vs lowest promotion mean for one mode."""
    positive = [
        result.mean_signal()
        for result in results
        if result.case.ground_truth == SIGNAL_CHOICE
    ]
    negative = [
        result.mean_signal()
        for result in results
        if result.case.ground_truth != SIGNAL_CHOICE
    ]
    positive = [value for value in positive if value is not None]
    negative = [value for value in negative if value is not None]
    if not positive or not negative:
        return {
            "usable": False,
            "min_positive": min(positive) if positive else None,
            "max_negative": max(negative) if negative else None,
        }
    low = max(negative)
    high = min(positive)
    threshold = _midpoint_threshold(low, high) if high > low else None
    return {
        "usable": True,
        "min_positive": high,
        "max_negative": low,
        "threshold": threshold,
        "margin_below": (high - threshold) if threshold is not None else None,
        "margin_above": (threshold - low) if threshold is not None else None,
        "top_negative_case": max(
            (
                result
                for result in results
                if result.case.ground_truth != SIGNAL_CHOICE
                and result.mean_signal() is not None
            ),
            key=lambda result: result.mean_signal(),
        ).case.case_id,
        "bottom_positive_case": min(
            (
                result
                for result in results
                if result.case.ground_truth == SIGNAL_CHOICE
                and result.mean_signal() is not None
            ),
            key=lambda result: result.mean_signal(),
        ).case.case_id,
    }


def _cold_start_rule(cold: dict[str, Any], threshold: float | None) -> str:
    """State what the gate does at cold start, from the measured cold-start split.

    The rule the plan asks for ("turn-text judgment when bucket features are
    ~zero") is only safe if the turn-text read separates on its own. The spike
    measures exactly that, so this function reports the measured answer: either
    the same threshold holds and the verdict is merely provisional (persistence
    unobservable), or the turn-text read does NOT separate and must never
    promote — the candidate waits for its second present turn.
    """
    if cold.get("threshold") is not None and threshold is not None:
        return (
            "turn-text read SEPARATES (lowest promotion "
            f"{_fmt(cold.get('min_positive'))} > highest non-promotion "
            f"{_fmt(cold.get('max_negative'))}): ask the same question on the "
            "candidate's first turn text and apply the same threshold "
            f"({threshold:.2f}), but mark the verdict PROVISIONAL — persistence is "
            "unobservable at one turn, so a provisional verdict never emits "
            "actor_promoted and the candidate is re-judged on its second present turn."
        )
    return (
        "turn-text read does NOT separate (lowest promotion "
        f"{_fmt(cold.get('min_positive'))} <= highest non-promotion "
        f"{_fmt(cold.get('max_negative'))}), so a first-sighting candidate must "
        "NEVER be promoted: hold it in the presence tier and re-judge with the "
        "evidence-state question on its second present turn, when persistence AND "
        "direct address are both observable. The turn-text read is a deferral, not "
        "a promotion signal"
        + (
            "; at the evidence threshold "
            f"({threshold:.2f}) the promotion case itself would be withheld"
            if threshold is not None
            else ""
        )
        + "."
    )


def recommend(
    evidence_results: list[CaseResult], cold_results: list[CaseResult]
) -> Recommendation:
    """Turn the measured means into a threshold + a cold-start rule (or refuse).

    The steady-state (evidence-mode) separation is the number the gate plans to
    apply, so it owns the threshold. The cold-start numbers ride along: they
    decide whether the turn-text fallback may promote or only defer, because a
    first-sighting candidate has no persistence to count and a second threshold
    would only hide that.
    """
    main = _separation(evidence_results)
    cold = _separation(cold_results)
    if not main.get("usable"):
        return Recommendation(
            threshold=None,
            min_positive=main.get("min_positive"),
            max_negative=main.get("max_negative"),
            margin_below=None,
            margin_above=None,
            separated=False,
            reason=(
                "no usable signal for at least one ground-truth class — the model "
                "returned no probabilities"
            ),
            cold_rule=_cold_start_rule(cold, None),
            cold_start=cold,
        )
    if main.get("threshold") is None:
        return Recommendation(
            threshold=None,
            min_positive=main["min_positive"],
            max_negative=main["max_negative"],
            margin_below=None,
            margin_above=None,
            separated=False,
            reason=(
                "the lowest promotion-worthy case scores at or below the highest "
                "non-promotion case — the measured cases do NOT separate, so no "
                "threshold can be recommended; the gate needs the structural guard "
                "(turns_present >= 2 AND at least one direct-address quote) as its "
                "rule and the model only as a tie-break"
            ),
            cold_rule=_cold_start_rule(cold, None),
            cold_start=cold,
        )
    return Recommendation(
        threshold=main["threshold"],
        min_positive=main["min_positive"],
        max_negative=main["max_negative"],
        margin_below=main["margin_below"],
        margin_above=main["margin_above"],
        separated=True,
        reason=(
            f"{main['bottom_positive_case']} (lowest promotion mean) clears "
            f"{main['top_negative_case']} (highest non-promotion mean)"
        ),
        cold_rule=_cold_start_rule(cold, main["threshold"]),
        cold_start=cold,
    )


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def _fmt(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _print_case_header(case: Case, evidence: Evidence) -> None:
    print(f"[{case.case_id}] candidate={case.candidate} ground truth={case.ground_truth}")
    print(f"  why: {case.rationale}")
    print(
        "  evidence: present turns "
        f"{list(evidence.present_turns)} of {evidence.turn_count}; absent "
        f"{list(evidence.absent_turns)}; quotes={len(evidence.quotes)}"
    )


def _print_mode(results: list[CaseResult], title: str, states: bool) -> None:
    print()
    print(f"--- {title} ---")
    for result in results:
        _print_case_header(result.case, result.evidence)
        if states:
            print("  state:")
            for line in result.state.splitlines():
                print(f"    {line}")
        for index, rep in enumerate(result.reps, start=1):
            if rep.signal is None:
                print(
                    f"  rep {index}: no usable answer (error={rep.error or 'none'})"
                )
                continue
            probabilities = " ".join(
                f"P({name})={_fmt(value)}"
                for name, value in sorted(rep.probabilities.items())
            )
            print(
                f"  rep {index}: choice={rep.choice} {probabilities} "
                f"latency={_fmt(rep.latency_s, 3)}s"
            )
        print(
            f"  mean {SIGNAL_CHOICE}={_fmt(result.mean_signal())} "
            f"spread={_fmt(result.spread(), 6)} "
            f"reps_usable={len(result.signals())}/{len(result.reps)}"
        )


def _print_recommendation(recommendation: Recommendation) -> None:
    print()
    print("=== RECOMMENDATION ===")
    print(f"min promotion mean    : {_fmt(recommendation.min_positive)}")
    print(f"max non-promotion mean: {_fmt(recommendation.max_negative)}")
    if recommendation.threshold is None:
        print("RECOMMENDED THRESHOLD  : NONE")
        print(f"reason                 : {recommendation.reason}")
    else:
        print(
            f"RECOMMENDED THRESHOLD  : signal >= {recommendation.threshold:.2f} "
            f"(P({SIGNAL_CHOICE}) from the promotion choice question)"
        )
        print(
            f"margin below the lowest promotion case: {_fmt(recommendation.margin_below)}"
        )
        print(
            f"margin above the highest non-promotion case: "
            f"{_fmt(recommendation.margin_above)}"
        )
        print(f"separation             : {recommendation.reason}")
    cold = recommendation.cold_start
    print()
    print("COLD-START RULE (turns_present < 2: the bucket has one turn of evidence)")
    print(
        "  state     : the candidate's FIRST turn text (the player's line + that "
        "turn's narration), not bucket features"
    )
    print("  question  : the SAME promotion question")
    print(f"  measured  : {recommendation.cold_rule}")
    print(
        "  measured split: min promotion="
        f"{_fmt(cold.get('min_positive'))} max non-promotion="
        f"{_fmt(cold.get('max_negative'))} "
        f"(separated={'yes' if cold.get('threshold') is not None else 'no'})"
    )
    print(
        "  why a first sighting cannot promote: 'one-shot-direct' has address "
        "without persistence, so a turn-1 read cannot tell it from the promotion "
        "case; only the second present turn can withdraw it"
    )


def _print_self_checks(evidence: list[Evidence]) -> None:
    print()
    print("SELF-CHECKS (evidence reads agree with the engine's presence walk)")
    for item in evidence:
        verdict = "ok" if item.present_now == item.fold_present_last else "MISMATCH"
        print(
            f"  [{item.candidate}] fold_present_last={item.fold_present_last} "
            f"presence_walk.is_present={item.present_now} -> {verdict}"
        )


async def _run(args: argparse.Namespace) -> int:
    try:
        registry = RegistryStore(args.config)
    except ConnectionConfigError as exc:
        print(f"ERROR: cannot load the connections config {args.config}: {exc}", file=sys.stderr)
        return 2
    caller = DecisionCaller(registry, timeout=args.timeout)
    if not caller.enabled:
        print(
            "ERROR: the 'decision' agent binding is missing (or carries no "
            f"base_url/model) in {args.config}; the promotion gate's model is "
            "feature-off, so there is nothing to calibrate.",
            file=sys.stderr,
        )
        return 2

    print("=== W9.22 Wave 0a — promotion calibration (live d1-3b) ===")
    print(f"config    : {args.config}")
    print(f"model     : {registry.connection_for('decision').model}")
    print(f"base_url  : {registry.connection_for('decision').base_url}")
    print(f"reps/case : {args.reps}")
    print(f"signal    : P({SIGNAL_CHOICE}) from one 'choice' question")
    print(f"question  : {PROMOTION_QUESTION['instructions']}")
    for name, criterion in PROMOTION_QUESTION["criteria"].items():
        print(f"  {name}: {criterion}")
    print("ground truth is the owner's four W9.22 scenarios (see --help)")

    evidence_results: list[CaseResult] = []
    cold_results: list[CaseResult] = []
    with tempfile.TemporaryDirectory(prefix="w922-promotion-") as tmp:
        log = EventLog(Path(tmp) / "spike.db")
        try:
            print()
            print(f"sessions written to a temp log: {log.path}")
            for case in CASES:
                session_id = f"spike-{case.case_id}"
                seed_session(log, session_id, case)
                evidence = collect_evidence(log, session_id, case)
                if evidence.present_now != evidence.fold_present_last:
                    print(
                        "ERROR: the per-turn presence fold disagrees with "
                        f"PresenceWalk.is_present for {case.candidate} "
                        f"({evidence.fold_present_last} vs {evidence.present_now}); "
                        "the evidence read is untrustworthy, so no numbers are "
                        "reported.",
                        file=sys.stderr,
                    )
                    return 1
                print(
                    f"  {case.case_id}: session={session_id} turns="
                    f"{evidence.turn_count} candidate={case.candidate} "
                    f"member={evidence.member_id} quotes={len(evidence.quotes)}"
                )
                evidence_results.append(
                    await measure_case(
                        registry,
                        case,
                        evidence,
                        "evidence",
                        render_evidence_state(evidence),
                        args.reps,
                        timeout=args.timeout,
                    )
                )
                cold_results.append(
                    await measure_case(
                        registry,
                        case,
                        evidence,
                        "cold-start",
                        render_cold_start_state(log, session_id, evidence),
                        args.reps,
                        timeout=args.timeout,
                    )
                )
        finally:
            log.close()

        _print_mode(evidence_results, "EVIDENCE MODE (bucket + attributed quotes)", args.states)
        _print_mode(cold_results, "COLD-START MODE (first turn text only)", args.states)
        _print_self_checks([result.evidence for result in evidence_results])

        missing = [
            result.case.case_id
            for result in (*evidence_results, *cold_results)
            if not result.signals()
        ]
        if missing:
            print()
            print("CALIBRATION FAILED — no threshold recommended.", file=sys.stderr)
            print(
                f"  cases with no usable answer: {', '.join(missing)}; "
                "the decision endpoint returned errors (see the rep lines above).",
                file=sys.stderr,
            )
            return 1

        _print_recommendation(recommend(evidence_results, cold_results))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "W9.22 Wave 0a promotion-gate calibration spike: measure d1-3b's "
            "P(promoted) on the owner's four ground-truth scenarios and print the "
            "recommended gate threshold plus the cold-start rule."
        ),
        epilog=(
            "Scenarios: persistent+direct (promote), ambient bartender (not), "
            "one-shot direct speech (gradient), silent persistence (gradient). "
            "Sessions are built in a temp EventLog through the engine's own "
            "writers and judged through the live decision endpoint configured in "
            "--config; nothing is written outside the temp dir."
        ),
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="connections config the engine reads (default: configs/connections.json)",
    )
    parser.add_argument(
        "--reps",
        type=int,
        default=DEFAULT_REPS,
        help=f"repetitions per case (default: {DEFAULT_REPS})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT:.0f})",
    )
    parser.add_argument(
        "--states",
        action="store_true",
        help="print the exact state text sent to the model for every case",
    )
    args = parser.parse_args(argv)
    if args.reps < 1:
        parser.error("--reps must be at least 1")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
