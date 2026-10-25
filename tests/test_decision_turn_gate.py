"""Per-turn d1-3b decision gate (``graph/nodes.py`` + ``decision.py``).

One batched ``/v1/systemone`` call per turn carries the 18 validated slop-phrase
questions and the narrative-movement question (``docs/the maintainer's private notes (unshipped)``). It
carries NOTHING ELSE: the same owner bank now also holds the two W9.22 proposal
rows (``section: "proposal"``), and this gate is one batched call, so asking them
would pay for a judgment another gate consumes — :func:`questions_for_section`
keeps the per-turn slice at 19 (W9.22 §4a / req. 20). Answers become ADVISORY
critic flags — they only add evidence; the killed critic SKIP pre-gate is not
resurrected. There is no memory-worthiness gate: every turn is persisted (owner
ruling 2026-10-14).

No live model: the wire seam is exercised with an ``httpx.MockTransport``; the
node is exercised with a fake caller that records its one ``ask``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fabulist.decision import (
    MOVEMENT_QUESTION,
    SLOP_PHRASES,
    DecisionCaller,
    slop_questions,
    slugify,
    turn_gate_questions,
    turn_gate_state,
)
from fabulist.decision_queries import (
    TURN_GATE_CHUNK_SIZE,
    chunk_questions,
    default_questions,
    replace_questions,
    slop_phrases,
)
from fabulist.graph.nodes import (
    _run_turn_gate,
    make_post_turn_node,
    turn_gate_flags,
)


def run(coro):
    return asyncio.run(coro)


# --- builders: validated shapes, verbatim ----------------------------------

#: The d1-active entry ids (issue #50: derived from the canonical artifact list,
#: not a hand-maintained 18-phrase code constant). ``skip_d1`` entries stay in the
#: validator and out of this model layer.
_EXPECTED_SLOP_IDS = {rule_id for rule_id, _phrase in slop_phrases()}


def test_slop_questions_shape_is_the_derived_map():
    questions = slop_questions()
    assert len(questions) == len(slop_phrases())
    assert set(questions) == _EXPECTED_SLOP_IDS
    assert all(q["type"] == "noul" for q in questions.values())
    # EXACT validated instruction string (pinned: rewording invalidates the spike).
    assert (
        questions["circling_like"]["instructions"]
        == 'Does the text contain the exact phrase "circling like [animal]" or a '
        "trivial variant of it?"
    )
    # A fresh, independent mapping each call (no shared mutable state).
    assert slop_questions() is not questions


def test_the_18_phrase_code_list_is_retired_and_skip_is_honoured():
    """The bank derives from the artifact list; regex-caught entries are skipped."""
    from fabulist.antislop import load_anti_slop

    listing = load_anti_slop()
    assert len(listing.entries) == 56
    skipped = {entry.id for entry in listing.entries if entry.skip_d1}
    asked = {entry.id for entry in listing.entries if not entry.skip_d1}
    assert skipped
    assert asked
    # Every skipped legacy rule the old 18-phrase bank used is now regex-only.
    assert "pregnant_pause" in skipped
    assert "circling_like" in asked
    assert {rule_id for rule_id, _ in slop_phrases()} == asked
    assert "pregnant_pause" not in slop_questions()
    # The import-time constant is the same derived view (no second list).
    assert {rule_id for rule_id, _ in SLOP_PHRASES} == asked
    assert slugify("didn't look back") == "didn_t_look_back"
    assert slugify("for a long moment") == "for_a_long_moment"


def test_movement_question_is_the_validated_noul():
    assert MOVEMENT_QUESTION["type"] == "noul"
    assert MOVEMENT_QUESTION["instructions"] == "Does this turn drive the narrative forward?"
    assert (
        MOVEMENT_QUESTION["criteria"]["false"]
        == "The turn only restates what is known, replays an emotion, or holds "
        "the same standoff — nothing changes."
    )
    assert "new information surfaces" in MOVEMENT_QUESTION["criteria"]["true"]


def test_turn_gate_questions_merges_slop_and_movement():
    questions = turn_gate_questions()
    assert len(questions) == len(slop_phrases()) + 1  # d1 slop + movement
    assert questions["movement"] == MOVEMENT_QUESTION
    assert "worthiness" not in questions
    assert set(_EXPECTED_SLOP_IDS) <= set(questions)
    # The bank also seeds the two proposal rows; this seam must not ask them.
    assert "should_we_propose" not in questions
    assert "should_this_propose" not in questions


def test_turn_gate_state_is_prose_driven_like_the_respike():
    """The validated fixtures posted RAW prose — no scene line, no user message."""
    assert turn_gate_state("user asks", "the prose body", "time: 09:15 | loc: x") == "the prose body"
    assert turn_gate_state("", "", "") == ""


# --- mapping thresholds ------------------------------------------------------


def test_slop_flag_fires_at_half_but_not_below():
    at_half = turn_gate_flags({"circling_like": {"noul": 0.5}})
    # R25: the pre-asset keys are unchanged; ``question_id`` / ``issue`` are added
    # so the critic reads a precise issue instead of a slug.
    assert at_half == [
        {
            "check": "d1-slop",
            "severity": "warn",
            "evidence": "phrase 'circling like [animal]' P=0.5",
            "question_id": "circling_like",
            "issue": "the prose contains the phrase 'circling like [animal]'",
        }
    ]
    assert turn_gate_flags({"circling_like": {"noul": 0.499}}) == []


def test_movement_flag_fires_below_half_but_not_at_half():
    assert turn_gate_flags({"movement": {"noul": 0.499}}) == [
        {
            "check": "d1-movement",
            "severity": "warn",
            "evidence": "stalled turn: P(advancing)=0.499",
            "question_id": "movement",
            "issue": MOVEMENT_QUESTION["criteria"]["false"],
        }
    ]
    assert turn_gate_flags({"movement": {"noul": 0.5}}) == []


def test_movement_high_probability_is_not_flagged():
    assert turn_gate_flags({"movement": {"noul": 0.6721}}) == []


def test_a_proposal_section_row_is_never_flagged_by_the_per_turn_gate():
    """Section routing holds for the flag path too, not just the payload."""
    proposal_row = {
        "id": "propose_something",
        "type": "noul",
        "instructions": "Should this be proposed?",
        "criteria": {"true": "durable fact", "false": "only the moment"},
        "threshold": 0.5,
        "enabled": True,
        "source": "custom",
        "section": "proposal",
    }
    answers = {"propose_something": {"noul": 0.99}}
    assert turn_gate_flags(answers, [proposal_row]) == []


def test_flags_skip_ill_typed_and_extra_answers():
    assert turn_gate_flags({}) == []
    assert turn_gate_flags({"pregnant_pause": {"noul": "high"}}) == []
    assert turn_gate_flags({"genre": {"choice": "fantasy"}}) == []


# --- node integration: one batched call, advisory flags ---------------------


PROSE = "The rain kept falling on the empty street."


class FakeGate:
    """Decision-caller stand-in: records its one ``ask``; optional raise/answers."""

    def __init__(self, answers=None, *, enabled=True, raises=None):
        self.enabled = enabled
        self.answers = answers
        self.raises = raises
        self.ask_calls: list[tuple[str, dict]] = []

    async def ask(self, state, questions):
        self.ask_calls.append((state, questions))
        if self.raises is not None:
            raise self.raises
        return self.answers

    async def classify_genre(self, state):
        return None  # the genre path is not under test here


class FakeCritic:
    def __init__(self):
        self.calls = 0
        self.evidence: list[dict] = []

    async def __call__(self, evidence):
        self.calls += 1
        self.evidence.append(evidence)
        return json.dumps({"verdict": "ok", "reason": "fine", "edits": []})


def _state(**overrides):
    state = {
        "session_id": "s1",
        "user_content": "I wait under the awning.",
        "narrative_parts": [PROSE],
        "dsl": "",
        "segments": [{"kind": "prose", "text": PROSE}],
        "facts": "time: 09:15",
        "action_class": "dialogue",
    }
    state.update(overrides)
    return state


def _node(log, decision, critic):
    return make_post_turn_node(
        extractor=lambda u, p: {"deltas": {}, "action_class": "dialogue"},
        log=log,
        critic=critic,
        decision=decision,
    )


def _d1_checks(flags):
    return [f for f in flags if str(f.get("check", "")).startswith("d1-")]


def test_gate_makes_one_batched_ask_and_maps_flags(log):
    gate = FakeGate(
        {
            "circling_like": {"noul": 0.9688},
            "voice_tone_adjective": {"noul": 0.9693},
            "x_escapes_me": {"noul": 0.06},
            "movement": {"noul": 0.1382},
        }
    )
    critic = FakeCritic()
    state = _state()
    run(_node(log, gate, critic)(state))

    # Exactly ONE ask (the seeded bank fits one chunk), on the raw prose.
    assert len(gate.ask_calls) == 1
    seen_state, seen_questions = gate.ask_calls[0]
    assert seen_state == PROSE
    assert len(seen_questions) == len(slop_phrases()) + 1
    assert "worthiness" not in seen_questions and "movement" in seen_questions
    # Section routing: a proposal row is another gate's row, never asked here.
    assert not set(seen_questions) & {
        "should_we_propose",
        "should_this_propose",
    }
    # The critic still runs — the skip pre-gate stays dead.
    assert critic.calls == 1

    flags = critic.evidence[0]["flags"]
    slop = [f for f in flags if f["check"] == "d1-slop"]
    assert {f["evidence"] for f in slop} == {
        "phrase 'circling like [animal]' P=0.9688",
        "phrase 'voice/tone is [adjective]' P=0.9693",
    }
    movement = [f for f in flags if f["check"] == "d1-movement"]
    assert movement == [
        {
            "check": "d1-movement",
            "severity": "warn",
            "evidence": "stalled turn: P(advancing)=0.1382",
            "question_id": "movement",
            "issue": MOVEMENT_QUESTION["criteria"]["false"],
        }
    ]
    # The gate never stashes a memory verdict — every turn is persisted.
    assert "memory_worthiness" not in state


def test_disabled_caller_is_zero_behavior_change(log):
    # Separate sessions: a second turn on one session sees the first as prior
    # prose, which perturbs the deterministic echo flag and would mask the point.
    baseline_critic = FakeCritic()
    run(_node(log, None, baseline_critic)(_state(session_id="base")))

    disabled = FakeGate({}, enabled=False)
    disabled_critic = FakeCritic()
    state = _state(session_id="dis")
    run(_node(log, disabled, disabled_critic)(state))

    assert disabled.ask_calls == []
    assert disabled_critic.evidence[0]["flags"] == baseline_critic.evidence[0]["flags"]
    assert "memory_worthiness" not in state


def test_raising_ask_adds_no_flags(log):
    gate = FakeGate(enabled=True, raises=RuntimeError("router down"))
    critic = FakeCritic()
    state = _state()
    run(_node(log, gate, critic)(state))

    assert gate.ask_calls and len(gate.ask_calls) == 1
    assert _d1_checks(critic.evidence[0]["flags"]) == []
    assert "memory_worthiness" not in state
    assert critic.calls == 1


def test_movement_flag_kept_when_other_answers_absent(log):
    gate = FakeGate({"movement": {"noul": 0.13}})  # only the movement answer landed
    critic = FakeCritic()
    state = _state()
    run(_node(log, gate, critic)(state))

    assert [f["check"] for f in _d1_checks(critic.evidence[0]["flags"])] == ["d1-movement"]
    assert "memory_worthiness" not in state


def test_the_gate_asks_only_its_own_section_from_a_stored_bank(log):
    """A stored bank mixing sections: the one ask carries the per-turn rows only."""
    from fabulist.decision_queries import replace_questions

    per_turn = {
        "id": "no_bananas",
        "type": "noul",
        "instructions": "Are there no bananas in the scene?",
        "criteria": {"true": "No bananas.", "false": "A banana is present."},
        "threshold": 0.5,
        "enabled": True,
        "source": "custom",
        "polarity": "true_is_defect",
    }
    other_gate = {**per_turn, "id": "propose_something", "section": "proposal"}
    replace_questions(log, [per_turn, other_gate])

    gate = FakeGate({"propose_something": {"noul": 0.99}})
    critic = FakeCritic()
    state = _state()
    run(_node(log, gate, critic)(state))

    assert len(gate.ask_calls) == 1
    assert list(gate.ask_calls[0][1]) == ["no_bananas"]
    assert [
        f for f in critic.evidence[0]["flags"]
        if f.get("question_id") == "propose_something"
    ] == []


def test_decision_none_is_todays_behavior(log):
    critic = FakeCritic()
    state = _state()
    run(_node(log, None, critic)(state))
    assert _d1_checks(critic.evidence[0]["flags"]) == []
    assert "memory_worthiness" not in state


def test_real_client_posts_exactly_one_gate_request(log):
    """The wired ``DecisionCaller`` issues ONE POST carrying all 19 questions."""
    requests: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        qids = list(body["questions"])
        if "movement" not in qids:  # the separate genre ask is not our concern
            return httpx.Response(200, json={"answers": {}})
        answers = {q: {"noul": 0.9} for q in qids}
        answers["movement"] = {"noul": 0.2}
        return httpx.Response(200, json={"answers": answers})

    class _Registry:
        def connection_for(self, name):
            return SimpleNamespace(base_url="http://d1.test/v1", model="d1-3b")

    caller = DecisionCaller(
        _Registry(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    critic = FakeCritic()
    state = _state()
    run(_node(log, caller, critic)(state))

    gate_requests = [b for b in requests if "movement" in b["questions"]]
    assert len(gate_requests) == 1
    assert len(gate_requests[0]["questions"]) == len(slop_phrases()) + 1
    assert "worthiness" not in gate_requests[0]["questions"]
    assert gate_requests[0]["state"] == PROSE
    slop = [f for f in critic.evidence[0]["flags"] if f["check"] == "d1-slop"]
    assert len(slop) == len(slop_phrases())  # every d1 row answered 0.9
    assert "memory_worthiness" not in state


# --- issue #50: batching at <=30 questions per POST -------------------------


def _slop_rows(count: int) -> list[dict]:
    """``count`` valid per-turn slop rows (no skip concept: a stored bank is data)."""
    return [
        {
            "id": f"phrase_{index}",
            "type": "noul",
            "instructions": (
                f'Does the text contain the exact phrase "phrase {index}" or a '
                "trivial variant of it?"
            ),
            "criteria": {},
            "threshold": 0.5,
            "enabled": True,
            "source": "slop",
            "polarity": "true_is_defect",
            "phrase": f"phrase {index}",
        }
        for index in range(count)
    ]


def test_chunk_questions_is_ordered_and_bounded():
    rows = _slop_rows(56)
    batches = chunk_questions(rows, 30)
    assert [len(b) for b in batches] == [30, 26]
    assert [row["id"] for b in batches for row in b] == [r["id"] for r in rows]
    assert chunk_questions([], 30) == []
    with pytest.raises(ValueError):
        chunk_questions(rows, 0)


def test_fifty_six_questions_post_as_two_batches_of_30_and_26(log):
    """Fake transport: 56 per-turn rows -> exactly 2 POSTs, 30 + 26 questions."""
    replace_questions(log, _slop_rows(56))
    requests: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={"answers": {qid: {"noul": 0.9} for qid in body["questions"]}},
        )

    class _Registry:
        def connection_for(self, name):
            return SimpleNamespace(base_url="http://d1.test/v1", model="d1-3b")

    caller = DecisionCaller(
        _Registry(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    flags = run(_run_turn_gate(caller, "user", PROSE, "", log))

    assert len(requests) == 2
    assert [len(body["questions"]) for body in requests] == [
        30,
        26,
    ]
    assert all(len(body["questions"]) <= TURN_GATE_CHUNK_SIZE for body in requests)
    assert len(flags) == 56
    assert [flag["question_id"] for flag in flags] == [
        row["id"] for row in _slop_rows(56)
    ]


def test_a_stored_pre_50_bank_keeps_working(log):
    """Back-compat: an old stored bank is still asked verbatim (one 19-row POST)."""
    old_rows = [
        {
            "id": slugify(phrase),
            "type": "noul",
            "instructions": (
                f'Does the text contain the exact phrase "{phrase}" or a trivial '
                "variant of it?"
            ),
            "criteria": {},
            "threshold": 0.5,
            "enabled": True,
            "source": "slop",
            "polarity": "true_is_defect",
            "phrase": phrase,
        }
        for phrase in (
            "shivers down her spine",
            "the air was thick with",
            "breath catching",
            "torn between",
            "the world narrowed",
            "predatory grin",
            "analytical gaze",
            "barely a whisper",
            "pregnant pause",
            "the silence stretched",
            "for a long moment",
            "didn't look back",
            "economical movements",
            "stark contrast",
            "the night is young",
            "playful challenge",
            "everything else faded",
            "a soft blur",
        )
    ]
    old_rows.append(dict(MOVEMENT_QUESTION, id="movement", threshold=0.5,
                         enabled=True, source="custom",
                         polarity="false_is_defect"))
    replace_questions(log, old_rows)

    requests: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"answers": {}})

    class _Registry:
        def connection_for(self, name):
            return SimpleNamespace(base_url="http://d1.test/v1", model="d1-3b")

    caller = DecisionCaller(
        _Registry(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    run(_run_turn_gate(caller, "user", PROSE, "", log))

    assert len(requests) == 1
    assert len(requests[0]["questions"]) == 19
    assert "pregnant_pause" in requests[0]["questions"]

    # A fresh log (no stored bank) seeds from the artifact instead.
    assert len(default_questions()) == len(slop_phrases()) + 4  # + movement/proposal×2/dispatch


def test_all_56_owner_entries_unskipped_post_as_30_and_26(tmp_path, monkeypatch, log):
    """The literal acceptance case: the full 56-entry list, no skips -> 2 POSTs."""
    from fabulist.antislop import reset_cache
    from fabulist.prompts import factory

    real = json.loads(
        (Path(__file__).resolve().parents[1] / "prompts" / "factory-defaults.json")
        .read_text(encoding="utf-8")
    )
    for section in real["anti_slop"]["sections"]:
        for entry in section["entries"]:
            if isinstance(entry, dict):
                entry.pop("skip_d1", None)
    artifact = tmp_path / "factory-defaults.json"
    artifact.write_text(json.dumps(real), encoding="utf-8")
    monkeypatch.setattr(factory, "DEFAULT_ARTIFACT_RELPATH", artifact)
    reset_cache()

    rows = [row for row in default_questions() if row.get("source") == "slop"]
    assert len(rows) == 56
    replace_questions(log, rows)

    requests: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"answers": {}})

    class _Registry:
        def connection_for(self, name):
            return SimpleNamespace(base_url="http://d1.test/v1", model="d1-3b")

    caller = DecisionCaller(
        _Registry(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    run(_run_turn_gate(caller, "user", PROSE, "", log))

    assert [len(body["questions"]) for body in requests] == [30, 26]
