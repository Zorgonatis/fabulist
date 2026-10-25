"""Owner-editable decision-query bank (R25).

The d1-3b per-turn gate's questions are an ASSET: log-owned rows on the
``__decision_queries__`` anchor, editable with no chat open (D9 /
non-negotiable 7). These tests pin the four things the lane must not break:

* **Parity** — an absent/empty asset yields the seeded bank as the PER-TURN
  slice: the artifact-derived slop questions (issue #50: the retired 18-phrase
  code list is replaced by the canonical list's d1-active entries) plus the
  validated movement question, in bank order.
* **Extensibility** — an owner-written question rides the same one-call path and
  its flag reaches the critic with a precise human-readable issue.
* **Storage** — validation refusals, and an appended bank that survives a fresh
  log read.
* **Section routing** (W9.22 §4a / req. 20) — the bank also carries the two
  proposal-gate rows, so the Studio pane lists/edits them; ``section`` routes a
  row to exactly ONE gate, rows without it stay per-turn (no migration), and a
  save round trip is lossless.

No live model: the flag path is exercised with a fake caller, the API with an
ASGI transport.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api.app import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.decision import (
    MOVEMENT_QUESTION,
    turn_gate_questions,
)
from fabulist.decision_queries import (
    DECISION_QUERIES_ANCHOR,
    DECISION_QUERIES_EVENT,
    DEFAULT_QUESTIONS,
    DISPATCH_QUESTION,
    DISPATCH_QUESTION_ID,
    DISPATCH_ROW_VERSION,
    DISPATCH_SECTION,
    DISPATCH_THRESHOLD,
    FALSE_IS_DEFECT,
    PROPOSAL_ROW_VERSION,
    PROPOSAL_SECTION,
    SHOULD_WE_PROPOSE_THRESHOLD,
    TRUE_IS_DEFECT,
    default_questions,
    effective_questions,
    flag_issue,
    list_questions,
    question_fires,
    question_payloads,
    question_section,
    questions_for_section,
    replace_questions,
    slop_phrases,
    slop_questions,
    validate_questions,
)
from fabulist.graph.nodes import make_post_turn_node, turn_gate_flags
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


def _bank_rows() -> list[dict]:
    """The pre-R25 question map rebuilt from the old public seams."""
    expected = dict(slop_questions())
    expected["movement"] = MOVEMENT_QUESTION
    return expected


#: The ids of the two W9.22 proposal rows the seeded bank now also carries.
_PROPOSAL_IDS = ["should_we_propose", "should_this_propose"]

#: The W9.24 NPC dispatch row the seeded bank also carries (its own section).
_DISPATCH_IDS = [DISPATCH_QUESTION_ID]


CUSTOM = {
    "id": "no_bananas",
    "type": "noul",
    "instructions": "Are there no bananas in the scene?",
    "criteria": {
        "true": "The scene contains no bananas.",
        "false": "At least one banana is present in the scene.",
    },
    "threshold": 0.5,
    "enabled": True,
    "source": "custom",
    "polarity": TRUE_IS_DEFECT,
}


# --- (a) seeded default == the code bank, byte-identical --------------------


def test_empty_asset_yields_the_pre_r25_code_bank(log):
    """No stored rows -> today's 19 per-turn questions, same order, same ids.

    The seeded bank also lists the two proposal rows (pane visibility, req. 20),
    but they are a DIFFERENT section: the per-turn slice is exactly the old bank.
    """
    rows = effective_questions(log)
    expected = _bank_rows()
    per_turn = [row for row in rows if question_section(row) == "slop"]
    assert [r["id"] for r in per_turn] == list(expected)
    assert len(per_turn) == len(slop_phrases()) + 1
    assert all(r["threshold"] == 0.5 for r in per_turn)
    # The movement question is the per-turn bank's only requirement-framed row.
    assert [r["id"] for r in per_turn if r["polarity"] == FALSE_IS_DEFECT] == [
        "movement"
    ]
    assert all(r["polarity"] == TRUE_IS_DEFECT for r in per_turn[:-1])
    # And the two proposal rows ride the same asset, routed away from this gate.
    assert [r["id"] for r in rows if question_section(r) == PROPOSAL_SECTION] == (
        _PROPOSAL_IDS
    )
    assert all(
        r["type"] == "choice" for r in rows if question_section(r) == PROPOSAL_SECTION
    )


def test_the_seeded_proposal_rows_mirror_the_lane_code_defaults():
    """ONE wording, two readers: the bank rows and the lane's fallback cannot drift.

    ``fabulist.state.proposal_lanes.CODE_DEFAULT_QUESTIONS`` is the per-id
    fallback for a bank that does not carry the row; the seeded rows are what a
    fresh log actually resolves. The proposal gate must not behave differently
    before and after the first save, so the fields it reads are pinned equal.
    """
    from fabulist.state.proposal_lanes import (
        CODE_DEFAULT_QUESTIONS,
        PROPOSE_CRITERION,
        SHOULD_THIS_PROPOSE_QUESTION_ID,
        SHOULD_WE_PROPOSE_QUESTION_ID,
    )

    assert _PROPOSAL_IDS == [
        SHOULD_WE_PROPOSE_QUESTION_ID,
        SHOULD_THIS_PROPOSE_QUESTION_ID,
    ]
    seeded = {
        row["id"]: row
        for row in DEFAULT_QUESTIONS
        if question_section(row) == PROPOSAL_SECTION
    }
    assert set(seeded) == set(CODE_DEFAULT_QUESTIONS)
    for question_id, code_default in CODE_DEFAULT_QUESTIONS.items():
        row = seeded[question_id]
        for field in (
            "type",
            "instructions",
            "criteria",
            "threshold",
            "enabled",
            "source",
        ):
            assert row[field] == code_default[field], (question_id, field)
        # The gate reads P(propose); a row without that label turns it off.
        assert PROPOSE_CRITERION in row["criteria"]


#: The should-we-propose row the v1 (W9.22) build seeded, verbatim: a PER-TURN
#: question over one ``<turn>`` blob. D81 replaced it (the historian asks per
#: WINDOW), and the read-time refresh must supersede exactly this — so the text is
#: duplicated here ONCE as data, which is what makes an accidental edit to either
#: copy fail a test instead of silently leaving every existing log on the old row.
_SHOULD_WE_PROPOSE_V1 = {
    "id": "should_we_propose",
    "type": "choice",
    "instructions": (
        "A roleplay turn has been recorded by the engine's world tracker. Decide "
        "whether the turn established or changed anything durable enough to be "
        "filed as a proposed amendment to the world, a character definition, or the "
        "story's lore. Answer 'propose' only when the turn itself supplies the "
        "evidence; answer 'skip' when it only plays out the moment."
    ),
    "criteria": {
        "propose": (
            "The turn establishes or changes a durable fact: a named place, a "
            "character's identity or situation, or lore the story will rely on "
            "later. The entities or the tracked state show it."
        ),
        "skip": (
            "The turn only continues the moment: dialogue, movement, mood, or a "
            "restatement of what was already known."
        ),
    },
    "threshold": 0.5,
    "enabled": True,
    "source": "custom",
    "polarity": TRUE_IS_DEFECT,
    "section": PROPOSAL_SECTION,
}


def test_the_seeded_should_we_propose_row_is_the_measured_window_wording():
    """The D81 wording + cut point, pinned character for character.

    The wording IS the measurement: the gate was calibrated live against d1-3b on
    4 real windows and 3 synthetic anchors with exactly this text and the
    per-turn ``<history>`` shape, and 0.55 is the midpoint of the measured
    clusters (propose 0.57-0.85, skip 0.47-0.55). A silent reword invalidates
    that calibration, so it has to fail here first.
    """
    row = [
        r for r in DEFAULT_QUESTIONS if r["id"] == "should_we_propose"
    ][0]
    assert row["instructions"] == (
        "This history holds the roleplay turns since the last check. Judge the "
        "whole history as one unit. One question covers every turn in it: did any "
        "turn in this history establish or change a durable fact? A durable fact "
        "stays true after the history ends. Answer with exactly one word, propose "
        "or skip."
    )
    assert row["criteria"]["propose"] == (
        "At least one turn in this history establishes or changes a durable "
        "fact. Durable facts are: a named place, a person's identity, a "
        "person's situation, a relationship, a plan or goal, an injury, a "
        "death, a reveal about the past, or an event later turns will rely on. "
        "The history text shows the fact. One qualifying turn is enough."
    )
    assert row["criteria"]["skip"] == (
        "Every turn in this history only continues the scene. The turns carry "
        "talk, movement, mood, or restatements of what is already known. No "
        "turn adds a new name, place, event, plan, relationship, or change in "
        "a person's situation. Nothing new stays true after the history ends."
    )
    assert row["threshold"] == SHOULD_WE_PROPOSE_THRESHOLD == 0.55
    assert row["v"] == PROPOSAL_ROW_VERSION == 2
    # The fields that route the row and gate it are unchanged by the rewrite.
    assert (row["type"], row["section"], row["enabled"], row["polarity"]) == (
        "choice",
        PROPOSAL_SECTION,
        True,
        TRUE_IS_DEFECT,
    )


def test_a_bank_an_older_build_seeded_is_superseded_at_read(log):
    """The migration: a stored v1 row is replaced in the READ, never in the log.

    The bank is seeded ONLY when missing, so without this every existing log keeps
    the old per-turn question and the fix reaches nobody (D81). The log stays
    append-only (D9): the superseded row is still there to be read back.
    """
    replace_questions(log, [dict(_SHOULD_WE_PROPOSE_V1)])

    rows = {
        row["id"]: row for row in questions_for_section(log, PROPOSAL_SECTION)
    }
    refreshed = rows["should_we_propose"]
    assert refreshed["instructions"].startswith(
        "This history holds the roleplay turns since the last check."
    )
    assert refreshed["threshold"] == SHOULD_WE_PROPOSE_THRESHOLD
    assert refreshed["v"] == PROPOSAL_ROW_VERSION

    # What the log still holds is the row the old build wrote, untouched.
    stored = [
        event.payload["questions"][0]
        for event in log.get_events(DECISION_QUERIES_ANCHOR)
        if getattr(event, "type", None) == "system"
        and isinstance(event.payload, dict)
        and event.payload.get("event") == DECISION_QUERIES_EVENT
    ]
    assert stored[-1]["instructions"] == _SHOULD_WE_PROPOSE_V1["instructions"]
    assert stored[-1].get("v") is None


def test_a_customized_should_we_propose_row_survives_the_refresh(log):
    """The owner's edits win: only an UNTOUCHED old seed is superseded.

    ``source`` cannot tell the two apart (a seeded proposal row is
    ``source: "custom"``, like the owner's), so the row's own text is the test.
    """
    customized = {
        **_SHOULD_WE_PROPOSE_V1,
        "instructions": "Did this window settle anything worth filing?",
        "threshold": 0.7,
    }
    replace_questions(log, [customized])

    rows = {row["id"]: row for row in questions_for_section(log, PROPOSAL_SECTION)}
    kept = rows["should_we_propose"]
    assert kept["instructions"] == "Did this window settle anything worth filing?"
    assert kept["threshold"] == 0.7
    assert "v" not in kept  # not stamped: the row is the owner's, not the seed's


def test_the_row_version_round_trips_through_a_save(log):
    """A saved bank keeps ``v``, so the refresh fires once — not on every read."""
    replace_questions(log, default_questions())
    assert list_questions(log) == default_questions()
    assert [row["v"] for row in list_questions(log) if row["id"] in _PROPOSAL_IDS] == [
        PROPOSAL_ROW_VERSION,
        PROPOSAL_ROW_VERSION,
    ]


def test_seeded_payload_is_byte_identical_to_the_pre_r25_map(log):
    """Same ids + same criteria text => the same decision-call payload, bytes included.

    The per-turn SEAM (``fabulist.decision.turn_gate_questions``) asks its own
    section only — 19 payloads even though the bank now holds 21 rows.
    """
    expected = _bank_rows()
    assert turn_gate_questions(log) == expected
    assert turn_gate_questions() == expected  # log-less callers ask the code bank
    assert json.dumps(turn_gate_questions(log)) == json.dumps(expected)
    # The bank itself carries the proposal + dispatch rows the seam must not ask
    # (W9.24 §3.5: the dispatch row's consumer is the lane dispatcher).
    assert set(question_payloads(effective_questions(log))) - set(expected) == {
        *_PROPOSAL_IDS,
        *_DISPATCH_IDS,
    }
    # The measured slop payload carries NO criteria key; movement carries its rubric.
    assert "criteria" not in turn_gate_questions(log)["circling_like"]
    assert turn_gate_questions(log)["movement"] == MOVEMENT_QUESTION


def test_default_questions_are_the_validated_wording(log):
    slop = {r["id"]: r for r in DEFAULT_QUESTIONS if r["source"] == "slop"}
    assert len(slop) == len(slop_phrases())
    # Issue #50: the 18 validated code rows are retired; the artifact's
    # d1-active entries are what a fresh seed asks.
    row = slop["circling_like"]
    assert row["instructions"] == (
        'Does the text contain the exact phrase "circling like [animal]" or a '
        "trivial variant of it?"
    )
    assert row["phrase"] == "circling like [animal]"
    # The code bank is never mutated by a read (a fresh copy each call).
    effective_questions(log)[0]["instructions"] = "tampered"
    assert effective_questions(log)[0]["instructions"] != "tampered"
    # A copy keeps a row's gate binding (a dropped section would re-route it).
    assert [
        row["section"] for row in default_questions() if row["id"] in _PROPOSAL_IDS
    ] == [PROPOSAL_SECTION, PROPOSAL_SECTION]


# --- (b) a custom question rides the same call ------------------------------


def test_custom_question_appears_in_the_decision_payload(log):
    replace_questions(log, [CUSTOM])
    payload = turn_gate_questions(log)
    assert list(payload) == ["no_bananas"]
    assert payload["no_bananas"] == {
        "type": "noul",
        "instructions": "Are there no bananas in the scene?",
        "criteria": dict(CUSTOM["criteria"]),
    }


def test_custom_question_extends_the_default_bank(log):
    rows = effective_questions(log)
    assert len(rows) == len(default_questions())  # asset absent -> seeded bank
    replace_questions(log, [*rows, CUSTOM])
    payload = turn_gate_questions(log)
    # The per-turn rows + the custom one (proposal/dispatch rows are other gates').
    assert len(payload) == len(slop_phrases()) + 2
    assert "no_bananas" in payload and "movement" in payload
    assert set(_PROPOSAL_IDS).isdisjoint(payload)
    assert set(_DISPATCH_IDS).isdisjoint(payload)


def test_choice_question_is_asked_but_never_flagged(log):
    choice = {
        "id": "pov",
        "type": "choice",
        "instructions": "Whose point of view carries the turn?",
        "criteria": {"protagonist": "The user's character.", "other": "Anyone else."},
        "source": "custom",
    }
    replace_questions(log, [choice])
    assert turn_gate_questions(log)["pov"]["criteria"] == dict(choice["criteria"])
    # A choice answer has no defect side, so it contributes no flag.
    assert turn_gate_flags({"pov": {"choice": "protagonist"}}, list_questions(log)) == []


# --- (c) disabled questions are skipped ------------------------------------


def test_disabled_question_is_neither_asked_nor_flagged(log):
    rows = effective_questions(log)
    for row in rows:
        if row["id"] == "circling_like":
            row["enabled"] = False
    replace_questions(log, rows)

    payload = turn_gate_questions(log)
    assert "circling_like" not in payload and len(payload) == len(slop_phrases())
    flags = turn_gate_flags({"circling_like": {"noul": 0.99}}, list_questions(log))
    assert flags == []


# --- (d) thresholds are honoured -------------------------------------------


def test_threshold_is_honoured_when_extracting_flags(log):
    strict = {**CUSTOM, "threshold": 0.8}
    replace_questions(log, [strict])
    rows = list_questions(log)

    assert turn_gate_flags({"no_bananas": {"noul": 0.79}}, rows) == []
    flagged = turn_gate_flags({"no_bananas": {"noul": 0.8}}, rows)
    assert [f["question_id"] for f in flagged] == ["no_bananas"]


def test_false_is_defect_fires_below_its_threshold(log):
    lenient = {
        "id": "bananas_present",
        "type": "noul",
        "instructions": "Does the scene contain a banana?",
        "criteria": {"true": "A banana is present.", "false": "No banana appears."},
        "threshold": 0.3,
        "source": "custom",
        "polarity": FALSE_IS_DEFECT,
    }
    replace_questions(log, [lenient])
    rows = list_questions(log)
    assert question_fires(rows[0], 0.29) is True
    assert question_fires(rows[0], 0.3) is False
    # The issue reads as the DEFECT statement, not as a negated question.
    assert flag_issue(rows[0]) == "No banana appears."


# --- (e) PUT validation refusals -------------------------------------------


def test_put_refuses_invalid_banks(log):
    """Every contract rule has a 400-worthy refusal (raised here as ValueError)."""
    cases = {
        "unique ids": [CUSTOM, dict(CUSTOM)],
        "valid type": [{**CUSTOM, "type": "score"}],
        "non-empty instructions": [{**CUSTOM, "instructions": "   "}],
        "noul criteria": [{**CUSTOM, "criteria": {"true": "only one side"}}],
        "choice labels": [{**CUSTOM, "type": "choice", "criteria": {}}],
        "choice descriptions": [
            {**CUSTOM, "type": "choice", "criteria": {"a": "  "}}
        ],
        "threshold range": [{**CUSTOM, "threshold": 1.5}],
        "threshold type": [{**CUSTOM, "threshold": "high"}],
        "enabled bool": [{**CUSTOM, "enabled": "yes"}],
        "polarity": [{**CUSTOM, "polarity": "either_way"}],
        "source": [{**CUSTOM, "source": "owner"}],
        "slop phrase": [{**CUSTOM, "source": "slop"}],
        "section": [{**CUSTOM, "section": "promotion"}],
        "list shape": {"questions": []},
    }
    for label, questions in cases.items():
        with pytest.raises(ValueError):
            replace_questions(log, questions)  # noqa: PT012 - one rule per case
    # Nothing was written by any refusal.
    assert list_questions(log) == []


# --- (f) the asset is log-backed and survives a fresh read -----------------


def test_asset_persists_across_a_fresh_log_read(log, tmp_path: Path):
    replace_questions(log, [CUSTOM])
    events = [e for e in log.get_events(DECISION_QUERIES_ANCHOR)]
    assert [e.payload["event"] for e in events] == [DECISION_QUERIES_EVENT]
    assert events[0].type == "system"

    reopened = EventLog(log.path)
    try:
        rows = list_questions(reopened)
        assert [r["id"] for r in rows] == ["no_bananas"]
        assert turn_gate_questions(reopened) == {
            "no_bananas": {
                "type": "noul",
                "instructions": "Are there no bananas in the scene?",
                "criteria": dict(CUSTOM["criteria"]),
            }
        }
        # Replace semantics: the newest row wins, the old rows stay queryable.
        replace_questions(reopened, [])
        assert list_questions(reopened) == []
        assert len(turn_gate_questions(reopened)) == len(slop_phrases()) + 1
    finally:
        reopened.close()


# --- (h) section routing: which gate owns a row (W9.22 §4a / req. 20) ----


def test_a_row_without_section_is_a_per_turn_row_and_round_trips_unchanged(log):
    """Pre-W9.22 rows keep their exact stored shape AND their gate (no migration)."""
    replace_questions(log, [CUSTOM])
    stored = list_questions(log)
    assert stored == [CUSTOM]  # the field does not grow on the way through
    assert json.dumps(stored) == json.dumps([CUSTOM])
    assert "section" not in stored[0]
    assert question_section(stored[0]) == "slop"
    assert [r["id"] for r in questions_for_section(log, "slop")] == ["no_bananas"]
    assert questions_for_section(log, PROPOSAL_SECTION) == []


def test_a_section_survives_the_stored_round_trip(log):
    """A sectioned row keeps its consumer; the other gate does not see it."""
    replace_questions(log, [{**CUSTOM, "section": PROPOSAL_SECTION}])
    assert list_questions(log)[0]["section"] == PROPOSAL_SECTION
    assert [r["id"] for r in questions_for_section(log, PROPOSAL_SECTION)] == [
        "no_bananas"
    ]
    assert questions_for_section(log, "slop") == []


def test_an_unknown_section_is_refused_at_the_write_door(log):
    """The section vocabulary is CLOSED: an owner picks a gate, never routing."""
    with pytest.raises(ValueError, match="section"):
        replace_questions(log, [{**CUSTOM, "section": "promotion"}])
    assert list_questions(log) == []


def test_questions_for_section_refuses_an_unknown_section(log):
    """A code-side consumer name that does not exist is a bug, not a read miss."""
    with pytest.raises(ValueError, match="unknown section"):
        questions_for_section(log, "promotion")


# --- (i) the W9.24 dispatch row: one row, its own gate, composed labels ------


def test_the_seeded_bank_carries_one_dispatch_row_owned_by_its_own_gate():
    """The dispatch question is ONE row of this bank, and no other gate asks it.

    W9.24 §3.5: the lane dispatcher is a THIRD consumer of the owner-editable
    bank (owner ruling), so the row lives here like the slop/proposal rows and
    is sliced by ``section`` like them — the per-turn ask pays for nothing.
    """
    rows = [row for row in DEFAULT_QUESTIONS if row["id"] == DISPATCH_QUESTION_ID]
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "choice"
    assert row["section"] == DISPATCH_SECTION
    assert row["threshold"] == DISPATCH_THRESHOLD
    assert row["enabled"] is True
    assert row["v"] == DISPATCH_ROW_VERSION
    # The labels are composed per turn from the ROSTER; a static set here would
    # ask about characters who are no longer in the scene.
    assert row["criteria"] == {}
    assert [r["id"] for r in questions_for_section(None, DISPATCH_SECTION)] == [
        DISPATCH_QUESTION_ID
    ]


def test_the_seeded_dispatch_row_is_the_exported_code_default():
    """A log whose stored bank predates the row resolves the same question.

    ``presence`` falls back to :data:`DISPATCH_QUESTION` when the bank carries no
    dispatch row (every bank saved before W9.24), so the two copies must agree on
    everything the dispatcher reads.
    """
    seeded = [r for r in DEFAULT_QUESTIONS if r["id"] == DISPATCH_QUESTION_ID][0]
    for field in ("type", "instructions", "criteria", "threshold", "enabled"):
        assert seeded[field] == DISPATCH_QUESTION[field], field


def test_an_empty_criteria_choice_row_is_refused_outside_the_dispatch_section():
    """Only the dispatch row may omit its labels: every other choice row IS them."""
    with pytest.raises(ValueError, match="at least one label"):
        validate_questions([{**CUSTOM, "type": "choice", "criteria": {}}])
    # The dispatch row's empty criteria ride through the same validation.
    rows = validate_questions([{**DISPATCH_QUESTION}])
    assert rows[0]["criteria"] == {}


def test_the_dispatch_row_is_not_asked_by_the_per_turn_gate(log):
    """One consumer per row: the batched per-turn ask never spends the dispatch question."""
    assert DISPATCH_QUESTION_ID not in turn_gate_questions(log)
    assert DISPATCH_QUESTION_ID in {
        row["id"] for row in effective_questions(log)
    }
    # Disabling it (the owner's switch) still leaves the row in its own slice.
    replace_questions(
        log, [{**row, "enabled": False} for row in default_questions()]
    )
    disabled = [
        row
        for row in questions_for_section(log, DISPATCH_SECTION)
        if row["id"] == DISPATCH_QUESTION_ID
    ]
    assert disabled and disabled[0]["enabled"] is False


# --- (g) a custom flag reaches the critic with its precise issue -----------

PROSE = "The rain kept falling on the empty street."


class FakeGate:
    """Decision-caller stand-in: records its one ``ask``; answers as scripted."""

    def __init__(self, answers):
        self.enabled = True
        self.answers = answers
        self.ask_calls: list[tuple[str, dict]] = []

    async def ask(self, state, questions):
        self.ask_calls.append((state, questions))
        return self.answers

    async def classify_genre(self, state):
        return None


class FakeCritic:
    def __init__(self):
        self.evidence: list[dict] = []

    async def __call__(self, evidence):
        self.evidence.append(evidence)
        return json.dumps({"verdict": "ok", "reason": "fine", "edits": []})


def _turn_node(log, decision, critic):
    return make_post_turn_node(
        extractor=lambda u, p: {"deltas": {}, "action_class": "dialogue"},
        log=log,
        critic=critic,
        decision=decision,
    )


def _turn_state():
    return {
        "session_id": "s1",
        "user_content": "I wait under the awning.",
        "narrative_parts": [PROSE],
        "dsl": "",
        "segments": [{"kind": "prose", "text": PROSE}],
        "facts": "time: 09:15",
        "action_class": "dialogue",
    }


def test_custom_question_flag_carries_the_precise_issue_to_the_critic(log):
    replace_questions(log, [CUSTOM])
    gate = FakeGate({"no_bananas": {"noul": 0.91}})
    critic = FakeCritic()
    run(_turn_node(log, gate, critic)(_turn_state()))

    # ONE batched ask carrying the owner's question (not the code bank).
    assert len(gate.ask_calls) == 1
    seen_state, seen_questions = gate.ask_calls[0]
    assert seen_state == PROSE
    assert list(seen_questions) == ["no_bananas"]

    flags = critic.evidence[0]["flags"]
    d1 = [f for f in flags if str(f.get("check", "")).startswith("d1-")]
    assert d1 == [
        {
            "check": "d1-question",
            "severity": "warn",
            "evidence": "question 'no_bananas' P=0.91",
            "question_id": "no_bananas",
            "issue": "The scene contains no bananas.",
        }
    ]


def test_seeded_default_flag_shape_keeps_the_pre_r25_keys(log):
    """Parity: the default bank's flags keep their labels/evidence and gain id+issue."""
    gate = FakeGate(
        {"circling_like": {"noul": 0.9688}, "movement": {"noul": 0.1382}}
    )
    critic = FakeCritic()
    run(_turn_node(log, gate, critic)(_turn_state()))

    d1 = [f for f in critic.evidence[0]["flags"] if str(f.get("check", "")).startswith("d1-")]
    assert [f["check"] for f in d1] == ["d1-slop", "d1-movement"]
    assert [f["evidence"] for f in d1] == [
        "phrase 'circling like [animal]' P=0.9688",
        "stalled turn: P(advancing)=0.1382",
    ]
    assert [f["question_id"] for f in d1] == ["circling_like", "movement"]
    assert d1[0]["issue"] == "the prose contains the phrase 'circling like [animal]'"
    assert d1[1]["issue"] == MOVEMENT_QUESTION["criteria"]["false"]


# --- router -----------------------------------------------------------------


@pytest.fixture
def build_app(tmp_path: Path):
    """App over a tmp event-log path (the router is mounted by ``create_app``)."""
    built = []

    def _build():
        app = create_app(FakeTurnEngine(), tmp_path / "events.db")
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_get_serves_the_seeded_bank(build_app):
    app = build_app()

    async def go():
        async with _client(app) as client:
            response = await client.get("/api/decision-queries")
        assert response.status_code == 200
        body = response.json()
        assert body["seeded"] is True
        assert [q["id"] for q in body["questions"]] == [
            *_bank_rows(),
            *_PROPOSAL_IDS,
            *_DISPATCH_IDS,
        ]
        return body

    body = run(go())
    per_turn = [
        q
        for q in body["questions"]
        if q.get("section") not in (PROPOSAL_SECTION, DISPATCH_SECTION)
    ]
    assert question_payloads(per_turn) == _bank_rows()


def test_put_then_get_preserves_the_proposal_sections(build_app):
    """The pane saves the whole bank; a save must never re-route a row."""
    app = build_app()
    bank = [dict(row) for row in DEFAULT_QUESTIONS]

    async def go():
        async with _client(app) as client:
            put = await client.put("/api/decision-queries", json={"questions": bank})
            get = await client.get("/api/decision-queries")
        return put, get

    put, get = run(go())
    assert put.status_code == 200
    sections = {q["id"]: q.get("section") for q in get.json()["questions"]}
    assert sections.pop("should_we_propose") == PROPOSAL_SECTION
    assert sections.pop("should_this_propose") == PROPOSAL_SECTION
    assert sections.pop(DISPATCH_QUESTION_ID) == DISPATCH_SECTION
    assert set(sections.values()) == {None}  # every other row stays per-turn


def test_put_replaces_and_get_reflects_it(build_app):
    app = build_app()

    async def go():
        async with _client(app) as client:
            put = await client.put("/api/decision-queries", json={"questions": [CUSTOM]})
            get = await client.get("/api/decision-queries")
        return put, get

    put, get = run(go())
    assert put.status_code == 200
    assert put.json()["seeded"] is False
    assert [q["id"] for q in put.json()["questions"]] == ["no_bananas"]
    assert get.json() == put.json()


def test_put_validation_refusals_are_400(build_app):
    app = build_app()

    async def go(bad_body):
        async with _client(app) as client:
            return await client.put("/api/decision-queries", json=bad_body)

    for bad in (
        {"questions": [{**CUSTOM, "threshold": 3}]},
        {"questions": [CUSTOM, dict(CUSTOM)]},
        {"questions": "nope"},
        {},
    ):
        response = run(go(bad))
        assert response.status_code == 400
        assert response.json()["detail"]
