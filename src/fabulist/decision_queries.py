"""Owner-editable decision-query bank (R25): the d1-3b gate's questions as data.

R25 (owner 2026-10-17): the slop bank "is actually missing a lot" and
the other question types (today only narrative movement) must be
**user-definable** — the decision model becomes extensible without code changes.
So the per-turn gate's question bank is an ASSET, not a constant: the event log
owns everything (D9), definitions are plain CRUD with no chat open and no
session required (non-negotiable 7).

Event contract — one ``system`` event on :data:`DECISION_QUERIES_ANCHOR`::

    {"event": "decision_queries", "questions": [question, ...]}

Replace semantics, latest wins: the newest row IS the bank (the earlier rows
stay queryable, D9). An absent anchor / absent row reads as **empty**, and an
empty asset means "the built-in seeded bank" (:data:`DEFAULT_QUESTIONS`): its
slop rows derive from the canonical anti-slop list in the factory artifact
(issue #50) and it also seeds the two W9.22 proposal rows, but those are a
different gate's ``section``, so the per-turn payload is unaffected (see below).

A question is a plain dict (the API/UI wire shape == the stored shape)::

    {
      "id": str,                  # payload key, unique; slop rows = slugify(phrase)
      "type": "noul" | "choice",  # the /v1/systemone question type
      "instructions": str,        # the question text sent to the model
      "criteria": dict,           # noul: {"true": str, "false": str} (optional,
                                  #   see the payload note below); choice:
                                  #   {label: description} (>= 1 entry)
      "threshold": float,         # 0..1, default 0.5
      "enabled": bool,            # disabled rows are neither asked nor flagged
      "source": "slop" | "custom",# provenance: a phrase check vs a rubric check
      "polarity": "true_is_defect" | "false_is_defect",
      "phrase": str,              # slop rows only
      "section": "slop" | "proposal" | "dispatch",  # OPTIONAL, see below
      "v": int,                  # OPTIONAL: the row-shape version, see below
    }

**Which gate owns a row** (``section``, W9.22 doctrine 6 / req. 20). Three gates
share this ONE bank: the per-turn critic gate (:data:`PER_TURN_SECTION`), the
W9.22 proposal stage (:data:`PROPOSAL_SECTION`, the should-we-propose and
should-this-propose rows seeded by :data:`DEFAULT_QUESTIONS`) and the W9.24 NPC
dispatch policy (:data:`DISPATCH_SECTION`, the who-acts-this-beat row).
``section`` names the single consumer of a row; a row WITHOUT it is a per-turn
row, which is what makes a bank written before W9.22 fold byte-identically (no
migration, doctrine 7). Each gate reads its own slice through
:func:`questions_for_section` — a per-turn batched ask must never pay for, or
flag from, a row another gate owns.

**The dispatch row's criteria are ENGINE-COMPOSED** (W9.24 §3.5, the one pinned
difference from every other ``choice`` row). A choice question's labels are
normally static owner text (``propose``/``skip``), but the dispatch question
chooses among CHARACTERS, and the roster changes every turn — so
:mod:`fabulist.state.presence` composes the ``{display name: description}``
label set at ask time from the turn's tier-eligible candidates. The seeded row
therefore carries EMPTY ``criteria`` and the owner edits its
``instructions``/``threshold``; a ``choice`` row with no criteria is valid only
in that section (:func:`_validate_criteria`), because every other choice row's
labels ARE the question.

**Fire rule** (verbatim from the R25 contract, do not re-derive it elsewhere): on
the answer's P(true), ``true_is_defect`` fires when ``P >= threshold`` and
``false_is_defect`` fires when ``P < threshold``. With the seeded 0.5 threshold
(the per-turn rows; the proposal gate's should-we-propose row carries its own
measured cut point, :data:`SHOULD_WE_PROPOSE_THRESHOLD`) that is exactly the
pre-R25 behaviour: a slop phrase "present" flags at ``P >= 0.5``, and movement
flags as stalled below ``0.5`` (movement is the default bank's only
``false_is_defect`` row).

**Seeded rows carry a row-shape version** (``v``, :data:`PROPOSAL_ROW_VERSION`).
The bank is seeded ONLY when the log holds none, so a fix to a seeded row's
wording would never reach an existing log — the log keeps the row an older build
wrote (D9: nothing is rewritten). ``v`` is what makes such a fix deliverable: a
stored row whose ``v`` is absent/older is superseded AT READ TIME by
:func:`_refresh_proposal_rows`, unless the owner changed it (a row that no longer
matches the superseded seed on the fields its gate reads is the owner's and is
left alone). ``source`` cannot tell the two apart — every seeded proposal row is
``source: "custom"`` — so the row's own text is the test.

Deliberate, boring constraints:

* **The seeded bank is the fallback, byte-identical.** ``criteria`` is EMITTED in
  the decision-call payload only when a row carries a non-empty mapping. The
  seeded slop rows carry none, so the payload for them stays exactly
  ``{"type", "instructions"}`` — the shape the re-spike measured
  (``docs/the maintainer's private notes (unshipped)``; identical input -> identical P). That is also why
  ``criteria`` is optional for ``noul`` rows here: the seeded bank round-trips
  through ``GET`` -> ``PUT`` unchanged.
* **Reads never raise.** A log without ``get_events`` (the ``LogPort`` test
  seam), a malformed anchor row, or a row that no longer validates all read as
  "absent asset" -> the seeded bank. The gate is advisory and never fatal.
* **Writes validate, reads tolerate.** :func:`replace_questions` raises
  ``ValueError`` with a human message (the API turns it into a 400); the fold
  uses the same rules defensively and drops an asset it cannot read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


__all__ = [
    "DEFAULT_QUESTIONS",
    "DEFAULT_THRESHOLD",
    "DECISION_QUERIES_ANCHOR",
    "DECISION_QUERIES_EVENT",
    "DISPATCH_QUESTION",
    "DISPATCH_QUESTION_ID",
    "DISPATCH_ROW_VERSION",
    "DISPATCH_SECTION",
    "DISPATCH_THRESHOLD",
    "FALSE_IS_DEFECT",
    "MOVEMENT_QUESTION",
    "MOVEMENT_QUESTION_ID",
    "PER_TURN_SECTION",
    "PROPOSAL_ROW_VERSION",
    "PROPOSAL_SECTION",
    "SLOP_PHRASES",
    "SHOULD_WE_PROPOSE_THRESHOLD",
    "TRUE_IS_DEFECT",
    "TURN_GATE_CHUNK_SIZE",
    "chunk_questions",
    "effective_questions",
    "flag_issue",
    "list_questions",
    "question_fires",
    "question_payloads",
    "question_section",
    "questions_for_section",
    "replace_questions",
    "slop_phrase_map",
    "slop_phrases",
    "slop_questions",
    "slugify",
    "validate_questions",
]

#: Reserved session id anchoring the bank (like ``__personas__`` /
#: ``__worlds__``); the ``__`` prefix keeps it out of the session list.
DECISION_QUERIES_ANCHOR = "__decision_queries__"

#: System-event kind carrying the whole bank (latest wins, replace semantics).
DECISION_QUERIES_EVENT = "decision_queries"

#: Actor stamped on human edits. Mirrors the persona/world appends (``"user"``).
USER_ACTOR = "user"

#: Threshold a row gets when it does not carry one.
DEFAULT_THRESHOLD = 0.5

#: Fire when P(true) >= threshold (a DEFECT is asserted: "the text contains
#: 'pregnant pause'"). Today's slop bank.
TRUE_IS_DEFECT = "true_is_defect"

#: Fire when P(true) < threshold (a REQUIREMENT is asserted: "this turn drives
#: the narrative forward"). Today's movement question.
FALSE_IS_DEFECT = "false_is_defect"

_POLARITIES = (TRUE_IS_DEFECT, FALSE_IS_DEFECT)
_TYPES = ("noul", "choice")
_SOURCES = ("slop", "custom")

#: The per-turn critic gate's section: every row that does not name another
#: section, i.e. the whole pre-W9.22 bank (back-compat, no migration).
PER_TURN_SECTION = "slop"

#: The W9.22 proposal stage's section: the two rows the proposal gates consume
#: (``fabulist.state.proposal_lanes``) and the per-turn gate must not ask.
PROPOSAL_SECTION = "proposal"

#: The W9.24 NPC dispatch policy's section: the ONE row the lane dispatcher asks
#: (``fabulist.state.presence.select_dispatch_members``) when the session's
#: strategy is ``decision``. Its criteria are engine-composed per turn (the
#: roster is dynamic), which is the pinned difference from every other row.
DISPATCH_SECTION = "dispatch"

#: The closed ``section -> consumer`` vocabulary; a row naming anything else is
#: refused at the write door (the consumer is typed code, not owner prose).
_SECTIONS = (PER_TURN_SECTION, PROPOSAL_SECTION, DISPATCH_SECTION)

#: The id of the built-in movement question; it keeps the pre-asset flag label
#: (``d1-movement``) and evidence wording for parity.
MOVEMENT_QUESTION_ID = "movement"


# ---------------------------------------------------------------------------
# the seeded default bank (slop rows derive from the factory artifact)
# ---------------------------------------------------------------------------
#
# The question WORDING below is validated: ``scripts/spike/d1_respike.py``
# measured these exact formulations on d1-3b and found bit-identical
# probabilities across runs (the model emits no tokens, so identical input ->
# identical P). Rewording them invalidates the measurements. The slop phrase
# bank is the re-spike's, which is itself re-used from the engine's
# ``validator.DEFAULT_SLOP_PATTERNS``.


def slugify(phrase: str) -> str:
    """A stable question id for a phrase: lowercase, non-alphanumerics -> ``_``.

    Mirrors ``scripts/spike/d1_respike.py:slugify`` verbatim — the ids the
    validated batched map used (e.g. ``shivers down her spine`` ->
    ``shivers_down_her_spine``).
    """
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


#: The d1 semantic net derives from the SAME canonical list the validator uses
#: (issue #50): every enabled entry that does not carry ``skip_d1``. The phrase
#: wording is what the model is asked about; ``id`` is the stable source id.
#:
#: The 18-phrase code list this module used to hard-code is RETIRED. A fresh bank
#: is seeded from the artifact; a session that already stored a bank keeps it
#: (latest-wins, D9) — see :func:`effective_questions`.
def slop_phrases() -> tuple[tuple[str, str], ...]:
    """``(source_id, phrase)`` for the entries the model layer should ask."""
    from fabulist.antislop import load_anti_slop

    return tuple(
        (entry.id, entry.phrase) for entry in load_anti_slop().d1_entries()
    )


#: Import-time snapshot for callers that read a constant. Fresh seeds use
#: :func:`slop_phrases` (mtime-cached artifact read), so this never becomes a
#: second source of truth.
SLOP_PHRASES: tuple[tuple[str, str], ...] = slop_phrases()


#: The narrative-movement question, verbatim from ``d1_respike.MOVEMENT_QUESTION``.
#: Built from the critic's own "Lack of movement" definition; validated at
#: advancing 0.6721 vs stalled 0.1382 / mood 0.1240, spread 0.0000 over 9 runs.
MOVEMENT_QUESTION = {
    "type": "noul",
    "instructions": "Does this turn drive the narrative forward?",
    "criteria": {
        "true": (
            "Something changes: new information surfaces, a consequence lands, a "
            "decision point opens, or the situation materially shifts."
        ),
        "false": (
            "The turn only restates what is known, replays an emotion, or holds "
            "the same standoff — nothing changes."
        ),
    },
}


def _slop_instructions(phrase: str) -> str:
    """The validated slop question wording for one phrase (verbatim)."""
    return (
        f'Does the text contain the exact phrase "{phrase}" or a trivial '
        "variant of it?"
    )


def slop_questions() -> dict[str, dict]:
    """One ``noul`` per d1-active entry; id = the canonical entry id.

    The instruction string is the validated ``d1_respike.build_slop_map_questions``
    formulation, verbatim. Returns a fresh mapping each call.
    """
    return {
        rule_id: {
            "type": "noul",
            "instructions": _slop_instructions(phrase),
        }
        for rule_id, phrase in slop_phrases()
    }


def slop_phrase_map() -> dict[str, str]:
    """``entry_id -> phrase`` — map a batched answer back to its text."""
    return {rule_id: phrase for rule_id, phrase in slop_phrases()}


def _slop_row(rule_id: str, phrase: str) -> dict:
    """One seeded slop row: a phrase check that fires when the phrase is present."""
    return {
        "id": rule_id,
        "type": "noul",
        "instructions": _slop_instructions(phrase),
        # Deliberately empty: the measured payload carries no rubric text.
        "criteria": {},
        "threshold": DEFAULT_THRESHOLD,
        "enabled": True,
        "source": "slop",
        "polarity": TRUE_IS_DEFECT,
        "phrase": phrase,
    }


def _movement_row() -> dict:
    """The seeded movement row: a requirement check that fires when it fails."""
    return {
        "id": MOVEMENT_QUESTION_ID,
        "type": MOVEMENT_QUESTION["type"],
        "instructions": MOVEMENT_QUESTION["instructions"],
        "criteria": dict(MOVEMENT_QUESTION["criteria"]),
        "threshold": DEFAULT_THRESHOLD,
        "enabled": True,
        "source": "custom",
        "polarity": FALSE_IS_DEFECT,
    }


#: The row-shape version every SEEDED proposal row carries (``"v"``).
#:
#: v2 (D81): should-we-propose is asked ONCE PER HISTORIAN WINDOW over a
#: ``<history>`` block of per-turn lines, not per turn over one ``<turn>`` blob —
#: its wording was rewritten and re-measured against that shape. Bumping it is
#: what lets the read-time refresh (:func:`_refresh_proposal_rows`) deliver a
#: seeded-row fix to a log that already stored the previous one.
PROPOSAL_ROW_VERSION = 2

#: The should-we-propose cut point, measured live against d1-3b with this exact
#: wording and the per-turn ``<history>`` shape (4 real windows + 3 synthetic
#: anchors): propose windows answered 0.57-0.85, skip windows 0.47-0.55, so 0.55
#: is the cluster midpoint. NOT the framework default (0.5) — a row that falls
#: back to :data:`DEFAULT_THRESHOLD` is a stale copy, which is why
#: :mod:`fabulist.state.proposal_lanes` imports this constant.
SHOULD_WE_PROPOSE_THRESHOLD = 0.55

#: The two W9.22 proposal-gate questions as ordinary bank rows, so the Studio's
#: question-bank pane lists and edits them like any other row (req. 20 parity:
#: ONE surface, typed sections — doctrine 6). ``section: "proposal"`` is what
#: keeps them OUT of the per-turn batched ask: their only consumer is
#: :mod:`fabulist.state.proposal_lanes`.
#:
#: The wording is the proposal stage's, duplicated here ONCE because importing
#: that module would be circular (it imports this one).
#: ``tests/test_decision_queries.py::test_the_seeded_proposal_rows_mirror_the_lane_code_defaults``
#: pins id/wording/threshold parity, so the two copies cannot drift.
#:
#: Thresholds: should-we-propose carries its OWN measured cut point
#: (:data:`SHOULD_WE_PROPOSE_THRESHOLD`); should-this-propose keeps the framework
#: default 0.5, which is where the Wave-3 per-item probes separated — a grounded
#: patch scored 0.88 against 0.10 for an invented one.
_SHOULD_WE_PROPOSE_ROW = {
    "id": "should_we_propose",
    "type": "choice",
    "instructions": (
        "This history holds the roleplay turns since the last check. Judge the "
        "whole history as one unit. One question covers every turn in it: did any "
        "turn in this history establish or change a durable fact? A durable fact "
        "stays true after the history ends. Answer with exactly one word, propose "
        "or skip."
    ),
    "criteria": {
        "propose": (
            "At least one turn in this history establishes or changes a durable "
            "fact. Durable facts are: a named place, a person's identity, a "
            "person's situation, a relationship, a plan or goal, an injury, a "
            "death, a reveal about the past, or an event later turns will rely on. "
            "The history text shows the fact. One qualifying turn is enough."
        ),
        "skip": (
            "Every turn in this history only continues the scene. The turns carry "
            "talk, movement, mood, or restatements of what is already known. No "
            "turn adds a new name, place, event, plan, relationship, or change in "
            "a person's situation. Nothing new stays true after the history ends."
        ),
    },
    "threshold": SHOULD_WE_PROPOSE_THRESHOLD,
    "enabled": True,
    "source": "custom",
    # A choice row has no defect side; the bank's polarity vocabulary is
    # defect-only and the proposal gates deliberately do not consume it.
    "polarity": TRUE_IS_DEFECT,
    "section": PROPOSAL_SECTION,
    "v": PROPOSAL_ROW_VERSION,
}

_SHOULD_THIS_PROPOSE_ROW = {
    "id": "should_this_propose",
    "type": "choice",
    "instructions": (
        "An engine pass has authored ONE proposed amendment to a stored asset (a "
        "world, a character definition, or a book of lore). Decide whether the "
        "owner should be asked to accept it. Answer 'propose' only when the patch "
        "reflects what the story established and would leave the stored asset "
        "consistent; answer 'skip' otherwise."
    ),
    "criteria": {
        "propose": (
            "The patch names only what the turn and the evidence establish, edits "
            "only the fields it is allowed to edit, and would leave the stored "
            "asset coherent (no contradiction, no invented history)."
        ),
        "skip": (
            "The patch invents facts the turn does not establish, contradicts what "
            "the stored asset already says, or would corrupt the asset (wrong "
            "shape, duplicated text, an empty or nonsensical change)."
        ),
    },
    "threshold": DEFAULT_THRESHOLD,
    "enabled": True,
    "source": "custom",
    "polarity": TRUE_IS_DEFECT,
    "section": PROPOSAL_SECTION,
    "v": PROPOSAL_ROW_VERSION,
}

#: The seeded proposal rows an EARLIER build wrote, verbatim (v1, W9.22):
#: should-we-propose was then a PER-TURN question whose state was one ``<turn>``
#: blob, which the historian's window-shaped ask miscalibrated (D81).
#:
#: This record exists for ONE reason — the read-time refresh must be able to tell
#: "the old seed, untouched" from "a row the owner edited" WITHOUT writing to the
#: log. A stored row identical to its entry here on every field its gate reads was
#: never touched by a human, so superseding it loses nothing; a row that differs
#: anywhere is the owner's and is preserved. should-this-propose is listed too
#: (its v1 and v2 wording are identical): the refresh then stamps ``v`` on it, so a
#: LATER fix to that row would need no new special case.
_SUPERSEDED_PROPOSAL_ROWS: dict[str, dict] = {
    "should_we_propose": {
        "type": "choice",
        "instructions": (
            "A roleplay turn has been recorded by the engine's world tracker. Decide "
            "whether the turn established or changed anything durable enough to be "
            "filed as a proposed amendment to the world, a character definition, or "
            "the story's lore. Answer 'propose' only when the turn itself supplies "
            "the evidence; answer 'skip' when it only plays out the moment."
        ),
        "criteria": {
            "propose": (
                "The turn establishes or changes a durable fact: a named place, a "
                "character's identity or situation, or lore the story will rely on "
                "later. The entities or the tracked state show it."
            ),
            "skip": (
                "The turn only continues the moment: dialogue, movement, mood, or "
                "a restatement of what was already known."
            ),
        },
        "threshold": DEFAULT_THRESHOLD,
        "enabled": True,
        "source": "custom",
        "section": PROPOSAL_SECTION,
    },
    "should_this_propose": {
        "type": "choice",
        "instructions": _SHOULD_THIS_PROPOSE_ROW["instructions"],
        "criteria": dict(_SHOULD_THIS_PROPOSE_ROW["criteria"]),
        "threshold": DEFAULT_THRESHOLD,
        "enabled": True,
        "source": "custom",
        "section": PROPOSAL_SECTION,
    },
}

#: The seeded proposal rows, in gate order (stage gate, then per-item judgment).
_PROPOSAL_ROWS: tuple[dict, ...] = (
    _SHOULD_WE_PROPOSE_ROW,
    _SHOULD_THIS_PROPOSE_ROW,
)

#: The W9.24 dispatch row's id (the payload key of the ONE ask the dispatcher
#: makes; one question, one consumer).
DISPATCH_QUESTION_ID = "who_dispatches"

#: The dispatch row's cut point, PROVISIONAL at 0.25 (W9.24 R7): a character
#: whose answer probability reaches it dispatches. The live W5 pass measures it
#: against real multi-cast turns and re-pins the seed if the cluster sits
#: elsewhere — the row is owner-editable, so the seeded value is a default, not
#: a law. Zero-lane over-dispatch is the failure mode to watch.
DISPATCH_THRESHOLD = 0.25

#: The dispatch row's row-shape version. The row is NEW with W9.24, so no log
#: carries an earlier seed to supersede yet; the marker exists so a later
#: wording/threshold fix is deliverable through the same read-time supersede the
#: proposal rows use (:func:`_refresh_proposal_rows`), with no new field.
DISPATCH_ROW_VERSION = 1

#: The dispatch question: WHICH characters act in this beat. The labels are NOT
#: here — the engine composes ``{display name: description}`` from the turn's
#: tier-eligible candidates at ask time (:data:`DISPATCH_SECTION` above), because
#: the roster changes every turn and a static label set would ask the wrong
#: question. The owner edits this wording and the threshold in the bank pane.
_DISPATCH_ROW = {
    "id": DISPATCH_QUESTION_ID,
    "type": "choice",
    "instructions": (
        "This is one beat of a roleplay scene. The criteria name the characters "
        "who are present in it. Answer with the character who most needs to act, "
        "speak, or visibly react in THIS beat; a character who stays quiet is not "
        "the answer."
    ),
    # Deliberately empty: the label set is the turn's roster (see above).
    "criteria": {},
    "threshold": DISPATCH_THRESHOLD,
    "enabled": True,
    "source": "custom",
    # A choice row has no defect side; the bank's polarity axis is defect-only
    # and the dispatcher does not consume it.
    "polarity": TRUE_IS_DEFECT,
    "section": DISPATCH_SECTION,
    "v": DISPATCH_ROW_VERSION,
}

#: The seeded dispatch row (a copy, never the module's own dict).
DISPATCH_QUESTION: dict = dict(_DISPATCH_ROW)


def _copy_question(question: dict) -> dict:
    """A shallow-but-safe copy: the criteria mapping is duplicated too.

    A row's ``section`` (a string) rides the plain key copy, so a sectioned
    seeded row keeps its gate on every read.
    """
    copied = dict(question)
    criteria = copied.get("criteria")
    copied["criteria"] = dict(criteria) if isinstance(criteria, dict) else {}
    return copied


def _default_questions() -> list[dict]:
    """The seeded default bank: d1 slop rows + movement + 2 proposal + 1 dispatch."""
    rows = [_slop_row(rule_id, phrase) for rule_id, phrase in slop_phrases()]
    rows.append(_movement_row())
    rows.extend(_copy_question(row) for row in _PROPOSAL_ROWS)
    rows.append(_copy_question(_DISPATCH_ROW))
    return rows


#: The seeded bank as asset rows: the d1 slop nouls + the movement noul (every
#: threshold 0.5, no ``section`` — the per-turn gate's slice), plus the two
#: proposal ``choice`` rows (``section: "proposal"``, ``v`` set; should-we-propose
#: carries its own measured :data:`SHOULD_WE_PROPOSE_THRESHOLD`). Served whenever
#: the log's asset is absent or empty.
DEFAULT_QUESTIONS: tuple[dict, ...] = tuple(_default_questions())


def default_questions() -> list[dict]:
    """A fresh, independently mutable copy of the current seeded bank.

    Rebuilt on every call from the artifact's canonical phrase list, so a fresh
    session seeded after an owner edit sees the new list. A session that already
    stored a bank is untouched (``effective_questions`` prefers the stored asset,
    D9); the d1 wording/fire rules are unchanged.
    """
    return [_copy_question(row) for row in _default_questions()]


# ---------------------------------------------------------------------------
# validation (writes raise; reads tolerate)
# ---------------------------------------------------------------------------


def _require_text(raw: dict, name: str, where: str) -> str:
    """A required non-empty string field, stripped; ``ValueError`` otherwise."""
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: '{name}' must be a non-empty string")
    return value.strip()


def _validate_criteria(
    raw: dict, question_type: str, section: str | None, where: str
) -> dict:
    """Validate/coerce ``criteria`` for the question type.

    ``noul`` rows MAY omit criteria (the measured slop payload carries no
    rubric text — see the module docstring); when supplied, both ``true`` and
    ``false`` must be non-empty strings. ``choice`` rows need at least one
    ``label -> description`` entry with a non-empty description — EXCEPT a
    :data:`DISPATCH_SECTION` row, whose labels are the turn's candidate
    characters and are composed by the engine at ask time (a static label set
    there would ask about a roster that no longer exists).
    """
    criteria = raw.get("criteria")
    if criteria is None:
        criteria = {}
    if not isinstance(criteria, dict):
        raise ValueError(f"{where}: 'criteria' must be an object")
    if question_type == "choice":
        if not criteria:
            if section == DISPATCH_SECTION:
                return {}
            raise ValueError(
                f"{where}: a choice question needs at least one label"
            )
        cleaned: dict[str, str] = {}
        for label, description in criteria.items():
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"{where}: choice labels must be non-empty strings")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(
                    f"{where}: choice '{label}' needs a non-empty description"
                )
            cleaned[label.strip()] = description.strip()
        return cleaned
    if not criteria:
        return {}
    cleaned = {}
    for side in ("true", "false"):
        description = criteria.get(side)
        if not isinstance(description, str) or not description.strip():
            raise ValueError(
                f"{where}: noul criteria need non-empty 'true' and 'false' text"
            )
        cleaned[side] = description.strip()
    return cleaned


def _validate_threshold(raw: dict, where: str) -> float:
    """``threshold`` as a float in ``0..1`` (default :data:`DEFAULT_THRESHOLD`)."""
    value = raw.get("threshold", DEFAULT_THRESHOLD)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: 'threshold' must be a number in 0..1")
    threshold = float(value)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"{where}: 'threshold' must be a number in 0..1")
    return threshold


def _validate_enabled(raw: dict, where: str) -> bool:
    """``enabled`` as a bool (default ``True``)."""
    value = raw.get("enabled", True)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: 'enabled' must be a boolean")
    return value


def _validate_section(raw: dict, where: str) -> str | None:
    """``section`` as a gate id, or ``None`` when the row omits it.

    Absence is preserved as absence (not folded to :data:`PER_TURN_SECTION`) so a
    pre-W9.22 bank round-trips byte-identically through ``GET`` -> ``PUT`` — the
    same rule the seeded slop payload follows. The value is a CLOSED vocabulary
    (:data:`_SECTIONS`) because a section selects TYPED consumer code; an owner
    writes questions, never routing.
    """
    value = raw.get("section")
    if value is None:
        return None
    if not isinstance(value, str) or value not in _SECTIONS:
        raise ValueError(f"{where}: 'section' must be one of {', '.join(_SECTIONS)}")
    return value


def _validate_version(raw: dict, where: str) -> int | None:
    """``v`` as a positive int, or ``None`` when the row carries no marker.

    Absence is preserved as absence (an unversioned row is DATA the refresh must
    judge, not an invalid row): only a row written through
    :func:`replace_questions` from a seeded row carries ``v``.
    """
    value = raw.get("v")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{where}: 'v' must be a positive integer")
    return value


def _normalize_question(raw: Any, where: str) -> dict:
    """Validate one question and return its canonical row (``ValueError``)."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: each question must be an object")
    question_id = _require_text(raw, "id", where)
    question_type = raw.get("type")
    if question_type not in _TYPES:
        raise ValueError(f"{where}: 'type' must be one of {', '.join(_TYPES)}")
    instructions = _require_text(raw, "instructions", where)
    # The section is folded FIRST: it decides whether a ``choice`` row may carry
    # no static labels (the dispatch row's are composed per turn).
    section = _validate_section(raw, where)
    criteria = _validate_criteria(raw, question_type, section, where)
    threshold = _validate_threshold(raw, where)
    enabled = _validate_enabled(raw, where)
    version = _validate_version(raw, where)
    source = raw.get("source", "custom")
    if source not in _SOURCES:
        raise ValueError(f"{where}: 'source' must be one of {', '.join(_SOURCES)}")
    polarity = raw.get("polarity", TRUE_IS_DEFECT)
    if polarity not in _POLARITIES:
        raise ValueError(
            f"{where}: 'polarity' must be one of {', '.join(_POLARITIES)}"
        )
    row = {
        "id": question_id,
        "type": question_type,
        "instructions": instructions,
        "criteria": criteria,
        "threshold": threshold,
        "enabled": enabled,
        "source": source,
        "polarity": polarity,
    }
    if source == "slop":
        # A slop row is a phrase check: the phrase is what the flag names.
        row["phrase"] = _require_text(raw, "phrase", where)
    if section is not None:
        row["section"] = section
    if version is not None:
        row["v"] = version
    return row


def validate_questions(questions: Any) -> list[dict]:
    """Validate a whole bank; return the canonical rows or raise ``ValueError``.

    Rules: ids unique and non-empty, ``type`` one of ``noul``/``choice``,
    non-empty ``instructions``, criteria matching the type, ``threshold`` a
    float in ``0..1``, ``enabled`` a bool, ``polarity`` one of the two strings,
    ``source`` one of ``slop``/``custom`` (slop rows also need a ``phrase``),
    (optional) ``section`` one of :data:`PER_TURN_SECTION` /
    :data:`PROPOSAL_SECTION` / :data:`DISPATCH_SECTION` (a dispatch row's
    ``choice`` criteria may be empty — composed per turn), and (optional) ``v`` a
    positive int — the row-shape version a SEEDED row carries
    (:data:`PROPOSAL_ROW_VERSION` / :data:`DISPATCH_ROW_VERSION`).
    """
    if not isinstance(questions, (list, tuple)):
        raise ValueError("'questions' must be a list")
    rows: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(questions):
        row = _normalize_question(raw, f"questions[{index}]")
        if row["id"] in seen:
            raise ValueError(f"questions[{index}]: duplicate id '{row['id']}'")
        seen.add(row["id"])
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# storage (log-owned, session-independent)
# ---------------------------------------------------------------------------


def _row_version(row: Mapping) -> int:
    """A stored row's ``v``, or ``0`` when it carries none (older than v1)."""
    value = row.get("v")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _refresh_proposal_rows(rows: list[dict]) -> list[dict]:
    """Supersede a STALE SEEDED proposal row in the READ (the v1 -> v2 migration).

    The bank is seeded only when the log holds none, so an existing log keeps the
    row an older build wrote and a fix to the seeded wording could never reach it.
    The log is never rewritten here (D9): the row is superseded in the read, the
    same way a revision supersedes a narrative, and the stored one stays
    queryable.

    Two facts make that safe:

    * ``v`` says whether the row predates the current seed at all — a row at
      :data:`PROPOSAL_ROW_VERSION` (or newer) is returned untouched;
    * a stale row is replaced ONLY when it still matches its
      :data:`_SUPERSEDED_PROPOSAL_ROWS` entry on every field its gate reads, i.e.
      nobody has edited it. ``source`` cannot tell the two apart (a seeded
      proposal row is ``source: "custom"``, like an owner's), so the row's own
      text is the test: any difference anywhere makes the row the OWNER's, and a
      customized row always wins.
    """
    current = {row["id"]: row for row in _PROPOSAL_ROWS}
    refreshed: list[dict] = []
    for row in rows:
        target = current.get(str(row.get("id")))
        stale = _row_version(row) < PROPOSAL_ROW_VERSION
        superseded = _SUPERSEDED_PROPOSAL_ROWS.get(str(row.get("id")))
        if not stale or target is None or superseded is None:
            refreshed.append(row)
            continue
        if any(row.get(field) != value for field, value in superseded.items()):
            refreshed.append(row)  # the owner changed it: the row is theirs
            continue
        refreshed.append(_copy_question(target))
    return refreshed


def _stored_rows(log: Any) -> list[dict] | None:
    """The newest stored bank, or ``None`` when the log holds no usable row.

    Tolerates everything the read path can meet: a log without ``get_events``
    (the ``LogPort`` seam), a non-``system`` row, a malformed payload, or a row
    that no longer validates (edited by hand / by an older build). A validated
    bank then passes through :func:`_refresh_proposal_rows`, which supersedes a
    seeded row an older build wrote (:data:`PROPOSAL_ROW_VERSION`).
    """
    getter = getattr(log, "get_events", None)
    if not callable(getter):
        return None
    try:
        events = getter(DECISION_QUERIES_ANCHOR)
    except Exception:  # noqa: BLE001 - a read failure means "no asset"
        return None
    latest: Any = None
    for event in events:
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != DECISION_QUERIES_EVENT:
            continue
        latest = payload.get("questions")
    if not isinstance(latest, list):
        return None
    try:
        return _refresh_proposal_rows(validate_questions(latest))
    except ValueError:
        return None


def list_questions(log: "EventLog") -> list[dict]:
    """The stored bank as the engine reads it; ``[]`` when nothing usable is stored.

    An empty list is meaningful: it is what "the seeded bank applies" reads as. A
    stored row an older build seeded comes back superseded
    (:func:`_refresh_proposal_rows`) — the log itself is never rewritten.
    """
    return _stored_rows(log) or []


def replace_questions(log: "EventLog", questions: Any) -> list[dict]:
    """Replace the whole bank with a validated question list (latest wins).

    Append-only (D9): the definitions are one ``system`` event on
    :data:`DECISION_QUERIES_ANCHOR`; the previous bank stays queryable and the
    newest row wins. ``ValueError`` (with a human message) on any invalid row —
    the API maps it to a 400.
    """
    rows = validate_questions(questions)
    log.append_event(
        DECISION_QUERIES_ANCHOR,
        "system",
        USER_ACTOR,
        {"event": DECISION_QUERIES_EVENT, "questions": rows},
    )
    return rows


def effective_questions(log: Any | None = None) -> list[dict]:
    """The bank the engine asks: the stored asset, or the artifact-seeded bank.

    A session with a stored bank keeps it (latest-wins, D9 — the back-compat
    contract). ``log=None`` (and an absent/unreadable/empty asset) yields a fresh
    copy of the current factory seed, whose slop rows derive from the canonical
    artifact list (issue #50).
    """
    stored = _stored_rows(log) if log is not None else None
    return stored if stored else default_questions()


def question_section(question: Mapping) -> str:
    """The gate a row feeds: :data:`PER_TURN_SECTION` unless it names another.

    The ONE fold of "absence means per-turn": a row written before W9.22 carries
    no ``section`` and belongs to the per-turn gate (no migration, doctrine 7).
    Anything written through :func:`replace_questions` is already validated to the
    closed vocabulary; a value outside it can only come from code, and reads
    tolerate data (they never raise), so it folds to per-turn as well.
    """
    value = question.get("section")
    return value if isinstance(value, str) and value in _SECTIONS else PER_TURN_SECTION


def questions_for_section(log: Any | None, section: str) -> list[dict]:
    """The effective bank's rows belonging to ONE gate, in bank order.

    WHY this exists (W9.22 doctrine 6 / req. 20): one owner-editable bank now feeds
    two gates — the per-turn critic gate and the proposal stage — so each gate must
    read ONLY its own rows. The per-turn gate pays for every row it asks in its one
    batched call, and an answer to a row whose consumer is another gate would mint a
    flag nobody owns; the proposal stage likewise must not gate on a slop phrase.
    Rows without a ``section`` are per-turn rows (see :func:`question_section`).

    An unknown ``section`` raises ``ValueError``: unlike a stored row (data, which
    reads tolerate), the section is a CODE-side consumer name, so asking for one
    that does not exist is a bug that should be loud.
    """
    if section not in _SECTIONS:
        raise ValueError(
            f"unknown section {section!r}; expected one of {', '.join(_SECTIONS)}"
        )
    return [row for row in effective_questions(log) if question_section(row) == section]


#: Maximum questions per ``/v1/systemone`` POST (issue #50). d1-3b runs at an
#: 8192 ctx and a question costs ~170 input tokens, so a full owner list is
#: split into batches of at most this many; the re-spike's validated batch was
#: 18, and 30 leaves headroom for the prose state.
TURN_GATE_CHUNK_SIZE = 30


def chunk_questions(
    questions: list[dict], size: int = TURN_GATE_CHUNK_SIZE
) -> list[list[dict]]:
    """Split the gate's rows into POST-sized batches, preserving bank order.

    The d1 contract accepts one map per POST, so batching is the ONLY way to ask
    more than one context's worth of questions per turn. An empty list yields an
    empty list (no call); a size below 1 is a code bug, not data.
    """
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [
        list(questions[index : index + size])
        for index in range(0, len(questions), size)
    ]


# ---------------------------------------------------------------------------
# decision-call payload + flag derivation
# ---------------------------------------------------------------------------


def question_payloads(questions: list[dict]) -> dict[str, dict]:
    """The ``/v1/systemone`` question map for the enabled rows.

    Shape per enabled row: ``{"type", "instructions"}`` plus ``"criteria"``
    ONLY when the row carries a non-empty mapping — which is exactly what the
    validated slop map (no criteria) and the movement question (criteria)
    posted before the bank became an asset.
    """
    payloads: dict[str, dict] = {}
    for question in questions:
        if not question.get("enabled", True):
            continue
        entry: dict[str, Any] = {
            "type": question["type"],
            "instructions": question["instructions"],
        }
        criteria = question.get("criteria") or {}
        if criteria:
            entry["criteria"] = dict(criteria)
        payloads[question["id"]] = entry
    return payloads


def question_fires(question: dict, probability: float) -> bool:
    """Does this answer fire the question's advisory flag?

    The contract rule: on P(true), ``true_is_defect`` fires when
    ``P >= threshold`` and ``false_is_defect`` fires when ``P < threshold``
    (0.5 by default — today's slop and stalled-turn rules).
    """
    threshold = float(question.get("threshold", DEFAULT_THRESHOLD))
    if question.get("polarity", TRUE_IS_DEFECT) == FALSE_IS_DEFECT:
        return probability < threshold
    return probability >= threshold


def flag_issue(question: dict) -> str:
    """The human-readable issue a fired question gives the critic.

    Slop rows name their phrase; an owner-written rubric row states its DEFECT
    side (``criteria`` for the failing polarity), so a fired
    ``false_is_defect`` question reads as "the turn does not drive the
    narrative forward" rather than as a negated question. Falls back to the
    question's own instruction when no defect text exists.
    """
    phrase = question.get("phrase")
    if isinstance(phrase, str) and phrase:
        return f"the prose contains the phrase '{phrase}'"
    criteria = question.get("criteria") or {}
    defect_side = "false" if question.get("polarity") == FALSE_IS_DEFECT else "true"
    defect = criteria.get(defect_side) if isinstance(criteria, dict) else None
    if isinstance(defect, str) and defect.strip():
        return defect.strip()
    return str(question.get("instructions", "")).strip()
