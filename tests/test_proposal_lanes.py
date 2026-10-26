"""W9.22 §3 — the proposal lanes: double gate, three Send lanes, per-item judgment.

The stage is the tail's most expensive surface if it is wrong, so these tests pin
the RULES (each test name is the rule it defends) at the seams the engine reads:

* the DOUBLE GATE (plan finding 9): a turn that touched no entity spends NOTHING
  — no ``Send``, no generator call, not even the should-we-propose ask — and a
  turn whose verdict is "no" spends one cheap decision call and ZERO generator
  calls. Both are asserted on the fakes' own call logs AND on the log's rows, so
  "quiet" means byte-identical;
* three lanes, PARALLEL (doctrine 5 / req. 18): one ``Send`` per data type, in
  table order (world / actors / books), each with its OWN type-scoped context;
* PER-ITEM judgment (doctrine 4): every authored row is judged by the decision
  model before it can become an event, and a judged-out row is dropped silently;
* the events are Wave 2's PINNED ``asset_proposal`` shape (validated through
  :func:`fabulist.log.schema_gate.validate_event`, the append door's own gate),
  one per kept item, plus ONE ``asset_proposals`` summary row with per-type counts;
* best-effort: a lane that raises records its reason in the summary and the turn
  survives; the merge keeps every lane's rows and appends them in DISPATCHER
  order, never completion order;
* inspection scope: a lane sees a DIGEST (id + name + one line), never a full
  card, and it may only amend an asset it was shown;
* gate configurability (req. 20): both questions are ordinary rows of the ONE
  decision-query bank — an owner threshold/disable wins over the code default.

Fake models only (a fake generator and a fake decision caller); the live model is
the orchestrator's run, not this file's business.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.graph.nodes import make_asset_proposals_node
from fabulist.decision_queries import PROPOSAL_SECTION
from fabulist.log.schema_gate import validate_event
from fabulist.state.proposal_lanes import (
    GATE_HISTORY_CHARS,
    GATE_HISTORY_MIN_LINE_CHARS,
    PROPOSAL_LANES,
    SHOULD_THIS_PROPOSE_QUESTION_ID,
    SHOULD_WE_PROPOSE_QUESTION_ID,
    SUMMARY_EVENT,
    Evidence,
    gate_questions,
    gate_state,
)

SID = "s1"


def run(coro):
    return asyncio.run(coro)


# -- fixtures ----------------------------------------------------------------


def _narrative(log, prose="Rain drums on the mill roof."):
    """The turn's committed narrative — the turn boundary the roster diff uses."""
    return log.append_event(
        SID,
        "narrative",
        "narrator",
        {"prose": prose, "segments": [{"kind": "prose", "text": prose}]},
    )


def _world_state_payload(*, venue=(), state=None, off_screen=(), kind="delta"):
    """One BTS world-state payload (the shape ``_run_world_state`` commits)."""
    return {
        "event": "world_state",
        "kind": kind,
        "state": dict(state or {}),
        "venue": {name: {"venue": "Miller's Bend", "area": "the mill"} for name in venue},
        "off_screen": [{"name": name, "location": "the road"} for name in off_screen],
        "threads": [],
        "tension": {},
    }


def _turn_state(log, *, actor="Elara", narrative=None):
    """The state the dispatcher reads: the committed turn + this turn's BTS row."""
    event = narrative if narrative is not None else _narrative(log)
    payload = _world_state_payload(venue=(actor,))
    log.append_event(SID, "system", "bts", payload)
    return {
        "session_id": SID,
        "user_content": "I ask about the mill.",
        "narrative_parts": ["Elara says the mill has stood a hundred years."],
        "facts": "time: 09:15",
        "narrative_event": {"id": event.id, "seq": event.seq, "payload": event.payload},
        "world_state_event": {"id": 0, "seq": event.seq + 1, "payload": payload},
        "proposal_plan": [],
        "proposal_rows": {},
        "proposal_meta": {},
    }


class FakeGenerator:
    """The lane's generator: one canned JSON batch per <lane data_type="...">.

    ``finish_reason`` stamps the transport's ``error`` frame on every lane (the frame
    shape ``RegistryModelCaller`` and the lane callers already emit), so a test can prove
    what the structured boundary does with an end reason it does not expect.
    """

    def __init__(self, batches=None, *, delay=0.0, finish_reason=None):
        self.batches = dict(batches or {})
        self.delay = delay
        self.finish_reason = finish_reason
        self.calls: list[list[dict]] = []

    @staticmethod
    def lane_of(messages) -> str:
        for message in messages:
            text = str(message.get("content") or "")
            if "<lane data_type=" in text:
                return text.split('<lane data_type="', 1)[1].split('"', 1)[0]
        return ""

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls.append(messages)
        await asyncio.sleep(self.delay)
        reply = self.batches.get(self.lane_of(messages))
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            reply = {"proposals": []}
        text = reply if isinstance(reply, str) else json.dumps(reply)
        yield {"type": "content", "delta": text}
        if self.finish_reason:
            yield {"type": "error", "delta": "", "meta": {"finish_reason": self.finish_reason}}


class FakeDecision:
    """The duck-typed d1-3b seam: one choice answer per asked question.

    The gate question answers :attr:`gate`; every per-item question answers
    ``items`` (a float, a callable over the state text, or a list consumed in
    order). :attr:`confidence` is the ``confidence`` a real d1-3b ``choice`` answer
    carries (P2's floor reads it); ``None`` omits the key, which is the
    no-confidence shape. Records every (state, questions) pair so a test can
    assert WHICH questions were asked rather than only what came back.
    """

    def __init__(self, *, gate=0.9, items=0.9, confidence=1.0):
        self.gate = gate
        self.items = items
        self.confidence = confidence
        self.calls: list[tuple[str, dict]] = []

    def _item_probability(self, state: str):
        if callable(self.items):
            return self.items(state)
        if isinstance(self.items, list):
            return self.items.pop(0) if self.items else None
        return self.items

    async def ask(self, state, questions):
        self.calls.append((state, dict(questions)))
        question_id = next(iter(questions))
        if question_id == SHOULD_WE_PROPOSE_QUESTION_ID:
            probability = self.gate
        elif question_id == SHOULD_THIS_PROPOSE_QUESTION_ID:
            probability = self._item_probability(state)
        else:  # pragma: no cover - the stage asks only its two questions
            return None
        if probability is None:
            return None
        answer = {
            "choice": "propose",
            "probabilities": {"propose": probability, "skip": 1.0 - probability},
        }
        if self.confidence is not None:
            answer["confidence"] = self.confidence
        return {question_id: answer}

    @property
    def gate_calls(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if SHOULD_WE_PROPOSE_QUESTION_ID in c[1]]

    @property
    def item_calls(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if SHOULD_THIS_PROPOSE_QUESTION_ID in c[1]]


def _fanout(log, *, generator=None, decision=None, lane_messages=None):
    return make_asset_proposals_node(
        log=log,
        caller=generator,
        decision=decision,
        lane_messages=lane_messages,
    )


async def drive(fanout, state, *, order=None):
    """Drive the fan-out the way the compiled graph does.

    Applies the dispatcher's update through the SAME reducers the graph uses,
    runs one ``lane`` per ``Send`` (optionally re-sequenced so a test can prove
    the emitted rows do NOT follow completion order), then runs the fan-in
    ``join``.
    """
    from fabulist.graph.types import merge_proposal_meta, merge_proposal_rows

    def apply(merged, update):
        if "proposal_rows" in update:
            merged["proposal_rows"] = merge_proposal_rows(
                merged.get("proposal_rows"), update["proposal_rows"]
            )
        if "proposal_meta" in update:
            merged["proposal_meta"] = merge_proposal_meta(
                merged.get("proposal_meta"), update["proposal_meta"]
            )
        if "proposal_plan" in update:
            merged["proposal_plan"] = update["proposal_plan"]
        return merged

    merged = apply(dict(state), await fanout(state))
    sends = list(await fanout.route(merged))
    payloads = [send.arg for send in sends]
    if order is not None:
        payloads = [payloads[index] for index in order]
    for result in await asyncio.gather(*(fanout.lane(payload) for payload in payloads)):
        apply(merged, result)
    merged["_join_update"] = fanout.join(merged)
    return merged


def _sends(fanout, state):
    """The dispatcher's plan applied to ``state``, then the router's ``Send`` list.

    Mirrors what the graph does between the two halves: the dispatcher's update
    rides the state forward, so the router sees ``proposal_plan`` AND the turn's
    own channels (``session_id``, ``narrative_parts``).
    """
    update = run(fanout(state))
    merged = {
        **state,
        "proposal_plan": update.get("proposal_plan") or [],
        "proposal_meta": update.get("proposal_meta") or {},
    }
    return run(fanout.route(merged))


def _system_rows(log, variant):
    return [
        event.payload
        for event in log.get_events(SID)
        if event.type == "system" and (event.payload or {}).get("event") == variant
    ]


def _proposal_rows(log):
    return _system_rows(log, "asset_proposal")


def _summary(log):
    rows = _system_rows(log, SUMMARY_EVENT)
    assert len(rows) == 1
    return rows[0]


#: The CURRENT seed definition ``_seed_world`` writes — the values a lane is
#: shown in its slice and therefore the only ``old`` it can honestly author.
_WORLD_SEED = {"name": "Miller's Bend", "description": "A river town."}


def _world_row(**overrides):
    """One lane row in the OPERATION contract (W9.23 §3.4.4), not a rewrite.

    The default amend changes ``description`` with the ``old`` value the seed
    actually holds — a whole-value ``patch`` would be dropped by the parser, and
    that refusal is pinned by its own test below.
    """
    row = {
        "kind": "world",
        "action": "amend",
        "target": "w_mill",
        "ops": [
            {
                "op": "modify",
                "path": "description",
                "old": _WORLD_SEED["description"],
                "new": "A mill town on the bend of the river.",
            }
        ],
        "evidence": "Elara says the mill has stood a hundred years.",
    }
    row.update(overrides)
    return row


def _seed_world(log, world_id="w_mill", name="Miller's Bend"):
    from fabulist.worlds.registry import append_world

    return append_world(
        log,
        {"world_id": world_id, "name": name, "description": "A river town."},
    )


# -- the double gate (plan finding 9) ---------------------------------------


def test_a_turn_that_touched_nothing_spends_no_call_and_appends_nothing(log):
    """Gate (a): no touched entity ⇒ no Send, no decision ask, no generator call."""
    state = {
        "session_id": SID,
        "narrative_parts": ["Rain keeps falling."],
        "narrative_event": {"id": 1, "seq": 1, "payload": {}},
        "world_state_event": {"payload": _world_state_payload()},
        "proposal_plan": [],
    }
    before = len(log.get_events(SID))
    generator = FakeGenerator()
    decision = FakeDecision()
    merged = run(drive(_fanout(log, generator=generator, decision=decision), state))

    assert merged["proposal_meta"]["gate"] == "no_evidence"
    assert merged["proposal_meta"]["lanes"] == []
    assert generator.calls == []  # zero generator calls
    assert decision.calls == []  # not even the verdict ask
    assert len(log.get_events(SID)) == before  # nothing appended
    assert merged["_join_update"] == {}


def test_a_quiet_turn_is_byte_identical_to_a_turn_without_the_stage(log):
    """The zero-cost claim stated as the turn's shape: no rows, no summary."""
    state = _turn_state(log, actor="Elara")
    # ...and then the tracker reports nothing at all for the turn.
    state["world_state_event"] = {"payload": _world_state_payload()}
    before = [event.type for event in log.get_events(SID)]
    run(drive(_fanout(log, generator=FakeGenerator(), decision=FakeDecision()), state))
    assert [event.type for event in log.get_events(SID)] == before
    assert _proposal_rows(log) == []
    assert _system_rows(log, SUMMARY_EVENT) == []


def test_a_no_verdict_dispatches_no_lane_and_spends_no_generator_call(log):
    """Gate (b): one cheap question; "no" costs ZERO generator calls (finding 9)."""
    state = _turn_state(log)
    generator = FakeGenerator()
    decision = FakeDecision(gate=0.1)
    merged = run(drive(_fanout(log, generator=generator, decision=decision), state))

    assert merged["proposal_meta"]["gate"] == "verdict_no"
    assert len(decision.gate_calls) == 1  # exactly one should-we-propose ask
    assert decision.item_calls == []
    assert generator.calls == []  # zero lanes
    assert _proposal_rows(log) == []
    assert _system_rows(log, SUMMARY_EVENT) == []


def test_a_low_confidence_verdict_reads_as_unsure_and_runs_no_lane(log):
    """P2: an answer below the confidence floor was never really judged.

    ``0.9`` is well over the threshold, but the answer's own confidence says the
    model was guessing, so it unlocks nothing — and the run reports the distinct
    ``unsure`` gate value, not the ``verdict_no`` a judged-quiet window reports.
    """
    from fabulist.state.proposal_lanes import GATE_CONFIDENCE_FLOOR

    state = _turn_state(log)
    generator = FakeGenerator()
    decision = FakeDecision(gate=0.9, confidence=0.1)
    merged = run(drive(_fanout(log, generator=generator, decision=decision), state))

    assert 0.1 < GATE_CONFIDENCE_FLOOR <= 0.9
    assert merged["proposal_meta"]["gate"] == "unsure"
    assert len(decision.gate_calls) == 1  # the one cheap ask still happens
    assert decision.item_calls == []
    assert generator.calls == []  # zero lanes
    assert _proposal_rows(log) == []
    assert _system_rows(log, SUMMARY_EVENT) == []


def test_unsure_is_distinct_from_a_judged_no(log):
    """The floor is not the threshold: a CONFIDENT low P is still a judged "no"."""
    state = _turn_state(log)
    sure_no = run(
        drive(
            _fanout(
                log,
                generator=FakeGenerator(),
                decision=FakeDecision(gate=0.1, confidence=0.9),
            ),
            state,
        )
    )
    assert sure_no["proposal_meta"]["gate"] == "verdict_no"

    guessing = run(
        drive(
            _fanout(
                log,
                generator=FakeGenerator(),
                decision=FakeDecision(gate=0.1, confidence=0.01),
            ),
            state,
        )
    )
    assert guessing["proposal_meta"]["gate"] == "unsure"


def test_a_confident_answer_takes_the_normal_verdict_path(log):
    """0.3 confidence clears the floor: the threshold decides and the lanes run."""
    state = _turn_state(log)
    _seed_world(log)
    generator = FakeGenerator({"world": {"proposals": [_world_row()]}})
    merged = run(
        drive(
            _fanout(
                log,
                generator=generator,
                decision=FakeDecision(gate=0.9, confidence=0.3),
            ),
            state,
        )
    )

    assert merged["proposal_meta"]["gate"] == "proceeded"
    assert generator.calls  # the world lane really ran
    assert len(_proposal_rows(log)) == 1


def test_an_answer_carrying_no_confidence_takes_the_probability_path(log):
    """No confidence is not zero confidence: the floor only cuts on a number.

    A ``noul``-shaped or hand-built answer carries no ``confidence`` key; treating
    its ABSENCE as a guessing answer would gate the stage off everywhere the
    field is not sent, so absence leaves the threshold as the whole verdict.
    """
    state = _turn_state(log)
    decision = FakeDecision(gate=0.9, confidence=None)
    merged = run(
        drive(_fanout(log, generator=FakeGenerator(), decision=decision), state)
    )
    assert merged["proposal_meta"]["gate"] == "proceeded"


def test_a_missing_generator_or_gate_spends_no_call_at_all(log):
    """An unwired stage (no binding / no decision model) proposes nothing."""
    state = _turn_state(log)
    decision = FakeDecision()
    merged = run(drive(_fanout(log, generator=None, decision=decision), state))
    assert merged["proposal_meta"]["gate"] == "no_generator"
    assert decision.calls == []

    generator = FakeGenerator()
    merged = run(drive(_fanout(log, generator=generator, decision=None), state))
    assert merged["proposal_meta"]["gate"] == "gate_off"
    assert generator.calls == []


# -- three lanes, in table order (doctrine 5 / req. 18) ---------------------


def test_three_lanes_dispatch_one_send_each_in_table_order(log):
    """One ``Send`` per data type, in the table's order — world, actors, books."""
    state = _turn_state(log)
    _seed_world(log)
    fanout = _fanout(log, generator=FakeGenerator(), decision=FakeDecision())
    sends = _sends(fanout, state)

    assert [send.node for send in sends] == ["asset_proposal_lane"] * 3
    assert [send.arg["lane"] for send in sends] == [
        spec.lane for spec in PROPOSAL_LANES
    ] == ["world", "actors", "books"]
    assert [send.arg["kind"] for send in sends] == ["world", "actor", "book"]


def test_each_lane_payload_is_scoped_to_its_own_type(log):
    """The world lane sees places, the actors lane the people, the books both."""
    state = _turn_state(log)
    state["world_state_event"] = {
        "payload": _world_state_payload(venue=("Elara",), off_screen=("Bram",))
    }
    fanout = _fanout(log, generator=FakeGenerator(), decision=FakeDecision())
    sends = _sends(fanout, state)
    payloads = {
        send.arg["lane"]: str(send.arg["messages"][-1]["content"]) for send in sends
    }

    assert "the mill" in payloads["world"]  # the place the tracker named
    assert "Bram" in payloads["actors"]  # both named people
    assert "Elara" in payloads["actors"]
    assert "the mill" in payloads["books"]  # lore may be about either
    # A lane is shown only what it needs: the actors lane is not told the places.
    assert "the mill" not in payloads["actors"].split("<turn>", 1)[0]


def test_a_lane_sees_a_digest_line_plus_a_bounded_slice(log):
    """W9.23 §3.4.4: the digest stays ONE clipped line; the slice is the diff's basis.

    The lane is never handed the catalog, but it IS handed the current value of
    the assets the turn is about — that is the only honest source for an ``old``.
    The two blocks stay disjoint: the digest line is for picking a target, the
    slice is for copying a value, and both are capped at their source.
    """
    from fabulist.personas.registry import append_persona
    from fabulist.state.proposal_lanes import MAX_SLICE_FIELD_CHARS

    long_description = "A retired soldier. " * 700  # ~12k chars, over the cap
    append_persona(
        log,
        {"persona_id": "p_elara", "name": "Elara", "description": long_description},
    )
    state = _turn_state(log)
    fanout = _fanout(log, generator=FakeGenerator(), decision=FakeDecision())
    sends = _sends(fanout, state)
    actors = [send for send in sends if send.arg["lane"] == "actors"][0]
    payload = str(actors.arg["messages"][-1]["content"])

    assert "id: p_elara" in payload and "Elara" in payload
    line = [row for row in payload.splitlines() if row.startswith("- id: p_elara")][0]
    assert len(line) < 160  # one clipped digest line, never a whole definition

    # The slice carries the CURRENT value, bounded, and marked truncated.
    current = payload.split("<current_values>", 1)[1].split("</current_values>", 1)[0]
    assert "description" in current
    assert len(current) < MAX_SLICE_FIELD_CHARS + 500
    assert "truncated" in current
    assert long_description not in payload  # the unclipped value never ships


# -- per-item judgment (doctrine 4) -----------------------------------------


def test_per_item_judgment_keeps_the_supported_row_and_drops_the_rest(log):
    """Every authored row is judged; a judged-out row leaves NO event behind."""
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {
            "world": {
                "proposals": [
                    _world_row(evidence="Elara says the mill is a hundred years old."),
                    _world_row(
                        target="w_mill",
                        ops=[
                            {
                                "op": "modify",
                                "path": "scenario",
                                "old": "",
                                "new": "The mill burned down last winter.",
                            }
                        ],
                        evidence="invented: nobody said this",
                    ),
                ]
            }
        }
    )
    decision = FakeDecision(items=lambda state: 0.1 if "invented" in state else 0.9)
    merged = run(drive(_fanout(log, generator=generator, decision=decision), state))

    rows = _proposal_rows(log)
    assert len(rows) == 1
    assert rows[0]["patch"] == {}
    assert rows[0]["ops"] == [
        {
            "op": "modify",
            "path": "description",
            "old": _WORLD_SEED["description"],
            "new": "A mill town on the bend of the river.",
        }
    ]
    assert rows[0]["signal"] == pytest.approx(0.9)
    assert len(decision.item_calls) == 2  # BOTH rows were judged
    assert merged["proposal_meta"]["dropped"] == [
        {"lane": "world", "reason": "verdict_no"}
    ]


def test_an_unjuged_row_is_never_kept(log):
    """An unanswerable judgment drops the row (fail-closed: no unchecked patch)."""
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator({"world": {"proposals": [_world_row()]}})
    merged = run(
        drive(_fanout(log, generator=generator, decision=FakeDecision(items=None)), state)
    )

    assert _proposal_rows(log) == []
    assert merged["proposal_meta"]["dropped"] == [
        {"lane": "world", "reason": "no_verdict"}
    ]


def test_a_lane_may_only_amend_an_asset_it_was_shown(log):
    """The digest is also the write scope: an invented target names nothing."""
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {"world": {"proposals": [_world_row(target="w_invented")]}}
    )
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    assert _proposal_rows(log) == []
    assert merged["proposal_meta"]["dropped"][0]["reason"] == "target"


def test_a_second_kind_in_one_batch_is_dropped_not_retyped(log):
    """``kind`` is fixed per lane: a mis-routed row is refused, never relabelled."""
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {"world": {"proposals": [_world_row(kind="actor"), _world_row()]}}
    )
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    assert len(_proposal_rows(log)) == 1
    assert merged["proposal_meta"]["dropped"][0]["reason"] == "kind"


# -- targeting the RIGHT kind (L4: the live log's defects) ------------------


def _seed_book(log, book_id="b_lore", name="Vale lore"):
    from fabulist.context.books import append_book

    return append_book(log, {"book_id": book_id, "name": name, "entries": []})


def test_a_venue_is_never_a_world_add(log):
    """A shop the turn walked into is not a world: the world lane cannot add.

    The live defect: an actor and a venue were both filed as ``kind="world"``
    adds, which would have minted catalog worlds out of one turn's scenery.
    """
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {"world": {"proposals": [_world_row(action="add", target=None,
                                            patch={"name": "Vale & Co.",
                                                   "description": "A jeweller's."})]}}
    )
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    assert _proposal_rows(log) == []
    assert merged["proposal_meta"]["dropped"][0]["reason"] == "action"


def test_a_world_rename_is_not_the_lanes_to_take(log):
    """The live rename ("The Jewellery Store" -> "Vale & Co.") never files.

    A world's NAME is the owner's (an asset definition is edited offline); a
    proposal that renames one is a venue being promoted into a world, and the
    lane reserves ``name`` so the rest of the patch cannot smuggle it through.
    """
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {"world": {"proposals": [_world_row(
            ops=[
                {
                    "op": "modify",
                    "path": "name",
                    "old": _WORLD_SEED["name"],
                    "new": "Vale & Co.",
                },
                {"op": "add", "path": "cast", "new": ["Marisol"]},
            ],
            evidence="The shop is named Vale & Co. now.",
        )]}}
    )
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    assert _proposal_rows(log) == []
    assert merged["proposal_meta"]["dropped"][0]["reason"] == "reserved"


def test_an_actor_patch_in_the_world_lane_is_a_kind_drift(log):
    """An actor written as a world is caught by the field vocabulary."""
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {"world": {"proposals": [_world_row(
            ops=[
                {
                    "op": "add",
                    "path": "personality",
                    "new": "Wry and watchful.",
                }
            ]
        )]}}
    )
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    assert _proposal_rows(log) == []
    assert merged["proposal_meta"]["dropped"][0]["reason"] == "kind"


def test_a_book_add_must_carry_entry_objects(log):
    """Prose stuffed into ``entries`` as a bare string is dropped, not accepted.

    The live defect: the owner accepted a book whose ``entries`` was the turn's
    prose, and the fold returned an EMPTY book (append_book keeps dicts only).
    """
    _seed_book(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {"books": {"proposals": [
            {"action": "add",
             "patch": {"name": "Vale & Co.", "entries": "A jeweller's shop."},
             "evidence": "The shop is described in the scene."},
            {"action": "add",
             "patch": {"name": "Vale lore",
                       "entries": [{"keys": ["Vale"],
                                    "content": "A jeweller's shop on the square.",
                                    "comment": "Vale & Co."}]},
             "evidence": "The shop is described in the scene."},
        ]}}
    )
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    (row,) = _proposal_rows(log)
    assert row["action"] == "add"
    assert row["patch"]["entries"] == [
        {"keys": ["Vale"], "content": "A jeweller's shop on the square.",
         "comment": "Vale & Co."}
    ]
    assert merged["proposal_meta"]["dropped"][0]["reason"] == "schema"


def test_a_quoted_target_resolves_and_carries_its_readable_name(log):
    """A quoted target names the shown asset, and the card gets a NAME with the id.

    Two live complaints at once: models wrap ids in quotes/backticks (so a
    correct amendment was being dropped over punctuation) and the owner saw only
    the bare GUID.
    """
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {"world": {"proposals": [_world_row(target="'w_mill'")]}}
    )
    run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    (row,) = _proposal_rows(log)
    assert row["target"] == "w_mill"
    assert row["target_name"] == "Miller's Bend"
    assert validate_event("system", row) == []


def test_an_invented_target_still_names_nothing(log):
    """The readable-name lookup never makes an invented id resolvable."""
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {"world": {"proposals": [_world_row(target="`w_nope`")]}}
    )
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    assert _proposal_rows(log) == []
    assert merged["proposal_meta"]["dropped"][0]["reason"] == "target"


# -- the events (Wave 2's pinned shape) + the summary row -------------------


def test_every_kept_row_is_one_pinned_asset_proposal_event(log):
    """One ``system``/``asset_proposal`` per kept item, exactly the pinned shape.

    W9.22 follow-up: the row ALSO carries the generator's bounded ``evidence``
    line — the review home's judgment needs the WHY, not just the diff.
    """
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {
            "world": {"proposals": [_world_row()]},
            "actors": {
                "proposals": [
                    {
                        "action": "add",
                        "patch": {"name": "Bram", "description": "The miller."},
                        "evidence": "Bram was named in the scene.",
                    }
                ]
            },
        }
    )
    run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    rows = _proposal_rows(log)
    assert [row["kind"] for row in rows] == ["world", "actor"]
    for row in rows:
        assert validate_event("system", row) == []  # the append door's own gate
        # The pinned shape, plus ``target_name`` on rows that NAME a target (L4:
        # the readable label rides the amend/remove card; an add names its asset
        # in the patch already).
        pinned = {
            "event",
            "kind",
            "action",
            "target",
            "patch",
            "origin",
            "signal",
            "evidence",
        }
        # An op row additionally carries ``ops`` (W9.23 §3.4.1).
        assert set(row) - pinned <= {"target_name", "ops"}
        assert pinned <= set(row)
        assert row["origin"] == "bts"
        assert isinstance(row["signal"], float)
    assert rows[0]["action"] == "amend" and rows[0]["target"] == "w_mill"
    assert rows[0]["target_name"] == "Miller's Bend"
    assert rows[1]["action"] == "add" and rows[1]["target"] is None
    assert "target_name" not in rows[1]
    assert rows[1]["evidence"] == "Bram was named in the scene."


def test_the_summary_counts_each_data_type_and_its_outcome(log):
    """ONE ``asset_proposals`` row: per-type kept/dropped counts, in lane order."""
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {
            "world": {"proposals": [_world_row()]},
            "actors": {
                "proposals": [
                    {
                        "action": "add",
                        "patch": {"name": "Bram"},
                        "evidence": "Bram was named.",
                    }
                ]
            },
            "books": {"proposals": []},
        }
    )
    run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    summary = _summary(log)
    assert summary["gate"] == "proceeded"
    assert [row["lane"] for row in summary["lanes"]] == ["world", "actors", "books"]
    assert [row["kept"] for row in summary["lanes"]] == [1, 1, 0]
    assert summary["kept"] == 2
    assert summary["touched"] >= 1


def test_the_merge_keeps_every_lane_and_appends_in_dispatcher_order(log):
    """Reducers merge the lanes; the flush follows DISPATCH order, not completion."""
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {
            "world": {"proposals": [_world_row()],},
            "actors": {
                "proposals": [
                    {
                        "action": "add",
                        "patch": {"name": "Bram"},
                        "evidence": "Bram was named.",
                    }
                ]
            },
            "books": {
                "proposals": [
                    {
                        "target": "b_lore",
                        "action": "amend",
                        "ops": [
                            {
                                "op": "add",
                                "path": "entries",
                                "new": {
                                    "keys": ["mill"],
                                    "content": "A mill stands on the bend.",
                                },
                            }
                        ],
                        "evidence": "The mill is the scene's anchor.",
                    }
                ]
            },
        }
    )
    from fabulist.context.books import append_book

    append_book(log, {"book_id": "b_lore", "name": "Mill lore", "entries": []})
    merged = run(
        drive(
            _fanout(log, generator=generator, decision=FakeDecision()),
            state,
            order=[2, 0, 1],  # books finish first, world second: order must not leak
        )
    )

    assert set(merged["proposal_rows"]) == {"world", "actors", "books"}
    assert len(merged["proposal_rows"]["actors"]) == 1
    assert [row["kind"] for row in _proposal_rows(log)] == ["world", "actor", "book"]


def test_a_failed_lane_records_its_reason_and_the_turn_survives(log):
    """Best-effort: a raising lane costs its rows, never the turn or its siblings."""
    _seed_world(log)
    state = _turn_state(log)
    generator = FakeGenerator(
        {"world": {"proposals": [_world_row()]}, "actors": RuntimeError("router down")}
    )
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    assert [row["kind"] for row in _proposal_rows(log)] == ["world"]
    summary = _summary(log)
    actors = [row for row in summary["lanes"] if row["lane"] == "actors"][0]
    assert actors["failed"] == ["router down"]
    assert actors["kept"] == 0
    assert merged["proposal_meta"]["failed"][0]["lane"] == "actors"


def test_a_lane_that_returns_prose_instead_of_json_is_recorded_as_a_failure(log):
    """The structured boundary refuses a non-JSON reply; the lane is a failure."""
    state = _turn_state(log)
    generator = FakeGenerator({"world": "I think the mill is lovely."})
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    assert _proposal_rows(log) == []
    assert merged["proposal_meta"]["failed"][0]["lane"] == "world"


def test_a_lane_with_an_unexpected_finish_reason_files_nothing(log):
    """The finish-reason gate is an ALLOWLIST: a parsing reply is still discarded.

    The payload below is a valid proposal batch — what sinks it is the transport's end
    reason (``content_filter``), which the boundary refuses with its own reason code.
    """
    state = _turn_state(log)
    generator = FakeGenerator(
        {"world": {"proposals": [_world_row()]}}, finish_reason="content_filter"
    )
    merged = run(drive(_fanout(log, generator=generator, decision=FakeDecision()), state))

    assert _proposal_rows(log) == []
    world = [row for row in _summary(log)["lanes"] if row["lane"] == "world"][0]
    assert world["kept"] == 0
    assert world["failed"] == ["structured: finish_reason"]
    assert "world" in [row["lane"] for row in merged["proposal_meta"]["failed"]]


# -- issue #82: the lane's CALL+PARSE rides the bounded retry ----------------
#
# The proposal lane held the call and its structured parse together, but parsed
# exactly ONCE: one malformed batch cost the lane on the first bad reply. It now
# rides the ONE mechanism (``agents.loop.run_structured_call``), like the plan
# refresh. It declares PARSE only — the provider class is retried at the client
# seam this lane's caller already rides (one owner per class, never a product).


def _rate_limited():
    """A REAL ``openai.RateLimitError`` (429) as a registry caller sees it."""
    import httpx
    import openai

    request = httpx.Request("POST", "http://provider.example/v1/chat/completions")
    body = {"error": {"message": "Rate limit reached for requests"}}
    response = httpx.Response(429, request=request, json=body)
    return openai.RateLimitError(
        f"Error code: 429 - {body}", response=response, body=body
    )


class _SequencedGenerator:
    """A generator that serves a QUEUE of replies per lane, repeating the last.

    ``AssetProposalFanout.lane`` holds the call and the parse together, so a
    retried PARSE attempt is a second CALL: a test says "bad, then good" and
    pins the attempt count off :attr:`calls`.
    """

    def __init__(self, sequences):
        self.sequences = {
            lane: list(replies) for lane, replies in dict(sequences).items()
        }
        self.calls: list[str] = []

    async def __call__(self, messages, stream=True, **kwargs):
        lane = FakeGenerator.lane_of(messages)
        self.calls.append(lane)
        queue = self.sequences.get(lane) or [{"proposals": []}]
        index = min(self.calls.count(lane) - 1, len(queue) - 1)
        reply = queue[index]
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        yield {"type": "content", "delta": text}


class _RegistryStyleGenerator:
    """A generator whose OPEN rides the REAL client retry seam (the caller's).

    ``PresetAgentCaller`` -> ``RegistryModelCaller`` opens every request through
    ``connections.client.call_with_retry``, which owns the PROVIDER class. This
    fake does the same, so a test can prove the lane does NOT retry that class a
    second time (which would be nine calls for one rate-limited lane).
    """

    def __init__(self, errors, *, batch=None):
        self.errors = list(errors)
        self.batch = batch if batch is not None else {"proposals": []}
        self.calls = 0

    async def __call__(self, messages, stream=True, **kwargs):
        from fabulist.connections.client import call_with_retry

        async def _open() -> str:
            index = self.calls
            self.calls += 1
            if index < len(self.errors):
                raise self.errors[index]
            return json.dumps(self.batch)

        text = await call_with_retry(_open, stage="model call (proposals)")
        yield {"type": "content", "delta": text}


def test_a_malformed_proposal_batch_retries_then_the_lane_files_its_rows(log):
    """Issue #82: a batch that clears on a re-send is a RECOVERED lane.

    Before the mechanism was wired here, one non-JSON reply made the lane
    ``failed`` on the first attempt — the asset update the model did produce on
    the next try was never asked for.
    """
    _seed_world(log)
    state = _turn_state(log)
    generator = _SequencedGenerator(
        {"world": ["I think the mill is lovely.", {"proposals": [_world_row()]}]}
    )
    fanout = _fanout(log, generator=generator, decision=FakeDecision())
    merged = run(drive(fanout, state))

    assert generator.calls.count("world") == 2  # one retry, then the good batch
    assert [row["kind"] for row in _proposal_rows(log)] == ["world"]
    assert merged["proposal_meta"].get("failed") is None
    assert [row["lane"] for row in _summary(log)["lanes"] if row["failed"]] == []


def test_a_batch_that_never_parses_retries_the_bound_then_degrades_its_lane(log):
    """Issue #82: exhaustion keeps the lane's OWN policy — the failed row.

    The stage runs off-turn (historian), so the degrade row IS its contract: no
    raise, no turn abort, no new event kind. Three attempts, then one
    ``proposal_meta["failed"]`` reason, exactly as a single malformed reply used
    to read.
    """
    _seed_world(log)
    state = _turn_state(log)
    generator = _SequencedGenerator({"world": ["I think the mill is lovely."]})
    fanout = _fanout(log, generator=generator, decision=FakeDecision())
    merged = run(drive(fanout, state))

    assert generator.calls.count("world") == 3  # the first try plus two retries
    assert _proposal_rows(log) == []
    world = [row for row in _summary(log)["lanes"] if row["lane"] == "world"][0]
    assert world["kept"] == 0
    assert world["failed"] == ["structured: malformed"]
    assert merged["proposal_meta"]["failed"] == [
        {"lane": "world", "reason": "structured: malformed"}
    ]


def test_a_provider_failure_at_the_lane_seam_is_retried_once_class_wide(log):
    """The scope rule: the lane retries PARSE, the caller seam retries PROVIDER.

    If the lane ALSO claimed the provider class, its scope would suppress the
    client's own loop and own the count itself — or, with no scope at all, the
    two loops would multiply to nine calls for one rate-limited lane. Three is
    the one-class-wide count.
    """
    _seed_world(log)
    state = _turn_state(log)
    generator = _RegistryStyleGenerator([_rate_limited() for _ in range(3)])
    fanout = _fanout(log, generator=generator, decision=FakeDecision())
    payload = next(
        send.arg for send in _sends(fanout, state) if send.arg["lane"] == "world"
    )

    update = run(fanout.lane(payload))

    assert generator.calls == 3  # the client's bounded count, not 3 x 3
    failed = update["proposal_meta"]["failed"]
    assert failed[0]["lane"] == "world"
    assert "429" in failed[0]["reason"]


# -- gate configurability (req. 20: the ONE question bank) ------------------


def _gate_row(
    question_id,
    instructions,
    criteria,
    threshold=0.5,
    *,
    enabled=True,
    section=PROPOSAL_SECTION,
):
    """One proposal-gate bank row, sectioned to THIS stage (W9.22 §4a)."""
    row = {
        "id": question_id,
        "type": "choice",
        "instructions": instructions,
        "criteria": criteria,
        "threshold": threshold,
        "enabled": enabled,
        "source": "custom",
    }
    if section is not None:
        row["section"] = section
    return row


def test_the_gate_questions_resolve_on_a_fresh_log(log):
    """A fresh log behaves: both questions resolve, at their MEASURED cut points.

    The seeded bank CARRIES both rows (``section: "proposal"``), so the
    resolution path is the bank, not the code fallback — the thresholds are the
    same either way, which is why the seed changes no behaviour. The stage gate's
    0.55 is D81's measured midpoint (the per-item judgment keeps the framework
    default).
    """
    questions = gate_questions(log)
    assert questions is not None
    assert SHOULD_WE_PROPOSE_QUESTION_ID in questions.gate
    assert SHOULD_THIS_PROPOSE_QUESTION_ID in questions.item
    from fabulist.decision_queries import (
        DEFAULT_THRESHOLD,
        SHOULD_WE_PROPOSE_THRESHOLD,
    )

    assert questions.gate_threshold == SHOULD_WE_PROPOSE_THRESHOLD == 0.55
    assert questions.item_threshold == DEFAULT_THRESHOLD


def test_an_owner_bank_row_overrides_the_code_default(log):
    """An owner threshold in the SAME bank changes the verdict with no code change."""
    from fabulist.decision_queries import replace_questions
    from fabulist.state.proposal_lanes import (
        SHOULD_THIS_PROPOSE_CRITERIA,
        SHOULD_THIS_PROPOSE_INSTRUCTIONS,
        SHOULD_WE_PROPOSE_CRITERIA,
        SHOULD_WE_PROPOSE_INSTRUCTIONS,
    )

    replace_questions(
        log,
        [
            _gate_row(
                SHOULD_WE_PROPOSE_QUESTION_ID,
                SHOULD_WE_PROPOSE_INSTRUCTIONS,
                SHOULD_WE_PROPOSE_CRITERIA,
                0.95,  # the owner raised the bar
            ),
            _gate_row(
                SHOULD_THIS_PROPOSE_QUESTION_ID,
                SHOULD_THIS_PROPOSE_INSTRUCTIONS,
                SHOULD_THIS_PROPOSE_CRITERIA,
            ),
        ],
    )
    state = _turn_state(log)
    generator = FakeGenerator()
    merged = run(
        drive(_fanout(log, generator=generator, decision=FakeDecision(gate=0.9)), state)
    )
    assert merged["proposal_meta"]["gate"] == "verdict_no"  # 0.9 < 0.95
    assert generator.calls == []


def test_a_disabled_question_turns_the_stage_off_with_no_call(log):
    """``enabled: false`` on the gate question ⇒ zero calls, zero events."""
    from fabulist.decision_queries import replace_questions
    from fabulist.state.proposal_lanes import (
        SHOULD_THIS_PROPOSE_CRITERIA,
        SHOULD_THIS_PROPOSE_INSTRUCTIONS,
        SHOULD_WE_PROPOSE_CRITERIA,
        SHOULD_WE_PROPOSE_INSTRUCTIONS,
    )

    replace_questions(
        log,
        [
            _gate_row(
                SHOULD_WE_PROPOSE_QUESTION_ID,
                SHOULD_WE_PROPOSE_INSTRUCTIONS,
                SHOULD_WE_PROPOSE_CRITERIA,
                enabled=False,  # switched off in the Studio
            ),
            _gate_row(
                SHOULD_THIS_PROPOSE_QUESTION_ID,
                SHOULD_THIS_PROPOSE_INSTRUCTIONS,
                SHOULD_THIS_PROPOSE_CRITERIA,
            ),
        ],
    )
    state = _turn_state(log)
    generator = FakeGenerator()
    decision = FakeDecision()
    merged = run(drive(_fanout(log, generator=generator, decision=decision), state))
    assert merged["proposal_meta"]["gate"] == "gate_off"
    assert decision.calls == []
    assert generator.calls == []


def test_a_bank_row_without_the_propose_verdict_fails_closed(log):
    """The rubric MUST keep the ``propose`` label: renaming it turns the gate off."""
    from fabulist.decision_queries import replace_questions
    from fabulist.state.proposal_lanes import (
        SHOULD_THIS_PROPOSE_CRITERIA,
        SHOULD_THIS_PROPOSE_INSTRUCTIONS,
        SHOULD_WE_PROPOSE_INSTRUCTIONS,
    )

    replace_questions(
        log,
        [
            _gate_row(
                SHOULD_WE_PROPOSE_QUESTION_ID,
                SHOULD_WE_PROPOSE_INSTRUCTIONS,
                {"yes": "a durable change", "no": "only the moment"},
            ),
            _gate_row(
                SHOULD_THIS_PROPOSE_QUESTION_ID,
                SHOULD_THIS_PROPOSE_INSTRUCTIONS,
                SHOULD_THIS_PROPOSE_CRITERIA,
            ),
        ],
    )
    assert gate_questions(log) is None


def test_an_older_bank_without_the_proposal_rows_falls_back_per_id(log):
    """A bank written before these rows existed still runs the stage (fallback)."""
    from fabulist.decision_queries import (
        SHOULD_WE_PROPOSE_THRESHOLD,
        replace_questions,
    )

    pre_w922_bank = [
        {
            "id": "movement",
            "type": "noul",
            "instructions": "Does this turn drive the narrative forward?",
            "criteria": {"true": "Something changes.", "false": "Nothing changes."},
            "threshold": 0.5,
            "enabled": True,
            "source": "custom",
            "polarity": "false_is_defect",
        }
    ]
    replace_questions(log, pre_w922_bank)
    questions = gate_questions(log)
    assert questions is not None
    assert questions.gate_threshold == SHOULD_WE_PROPOSE_THRESHOLD
    assert SHOULD_WE_PROPOSE_QUESTION_ID in questions.gate


def test_a_proposal_row_without_the_section_is_not_an_override(log):
    """Absent ``section`` means the PER-TURN gate, so this stage ignores the row.

    The rule is what keeps old rows byte-identical (no migration): a row that does
    not name a section is asked by the per-turn gate, and this gate falls back to
    its code default instead of adopting a row another gate also consumes.
    """
    from fabulist.decision_queries import (
        SHOULD_WE_PROPOSE_THRESHOLD,
        replace_questions,
    )
    from fabulist.state.proposal_lanes import (
        SHOULD_THIS_PROPOSE_CRITERIA,
        SHOULD_THIS_PROPOSE_INSTRUCTIONS,
        SHOULD_WE_PROPOSE_CRITERIA,
        SHOULD_WE_PROPOSE_INSTRUCTIONS,
    )

    replace_questions(
        log,
        [
            _gate_row(
                SHOULD_WE_PROPOSE_QUESTION_ID,
                SHOULD_WE_PROPOSE_INSTRUCTIONS,
                SHOULD_WE_PROPOSE_CRITERIA,
                0.95,  # would turn the gate off if this stage adopted the row
                enabled=False,
                section=None,
            ),
            _gate_row(
                SHOULD_THIS_PROPOSE_QUESTION_ID,
                SHOULD_THIS_PROPOSE_INSTRUCTIONS,
                SHOULD_THIS_PROPOSE_CRITERIA,
                section=None,
            ),
        ],
    )
    questions = gate_questions(log)
    assert questions is not None
    assert questions.gate_threshold == SHOULD_WE_PROPOSE_THRESHOLD


# -- the gate state: one line per WINDOW turn (D81) -------------------------
#
# The historian asks should-we-propose ONCE per window, so the state is a
# ``<history>`` block with a line per turn. The W9.22 state was one ``<turn>``
# clipped at 800 chars, which on a real window (~600+ chars per turn) showed the
# model barely one turn of ten; these tests pin the shape that replaced it.


def test_the_gate_state_renders_one_line_per_window_turn():
    """Evidence lines unchanged, then ``T1..Tn`` inside one ``<history>`` block."""
    state = gate_state(
        Evidence(names=("Elara",), places=("Miller's Bend",), admitted=("Bram",)),
        ["The mill stands.", "Elara mentions her brother.", "Bram dies."],
    )
    assert state.splitlines() == [
        "<named>Elara</named>",
        "<places>Miller's Bend</places>",
        "<newly_admitted>Bram</newly_admitted>",
        "<history>",
        "T1: The mill stands.",
        "T2: Elara mentions her brother.",
        "T3: Bram dies.",
        "</history>",
    ]


def test_a_wide_window_keeps_every_turn_and_splits_the_prose_budget():
    """N=10 x 200 chars: ten lines, each capped at ``1200 // 10 = 120`` chars.

    The per-line budget is DYNAMIC: the block's total prose budget is fixed, so a
    wider window shows each turn more briefly — but never drops one.
    """
    turns = []
    for index in range(1, 11):
        head = f"turn{index} "
        tail = f" the{index}"
        turns.append(head + "x" * (200 - len(head) - len(tail)) + tail)
    assert all(len(turn) == 200 for turn in turns)
    lines = [
        line
        for line in gate_state(Evidence(), turns).splitlines()
        if line.startswith("T")
    ]
    assert [line.split(":", 1)[0] for line in lines] == [
        f"T{index}" for index in range(1, 11)
    ]
    cap = GATE_HISTORY_CHARS // 10
    assert cap == 120
    for line in lines:
        line_ordinal, _, text = line.partition(": ")
        assert len(text) == cap, (line_ordinal, len(text))
        assert text.startswith("…")  # tail-clipped, marked as such
    # Every turn's own ENDING survives: the TAIL is what a line keeps (the beat
    # a gate asks about lands late, and D81's numbers were measured this way).
    assert lines[0] == f"T1: …{turns[0][-(cap - 1) :]}"
    assert lines[0].endswith(" the1")
    assert lines[-1].endswith(" the10")
    assert "turn1 " not in lines[0]  # the head is what the clip spends


def test_a_turn_line_keeps_the_turns_ending_not_its_opening():
    """A clipped line is the turn's TAIL: the reveal beat is the last thing said."""
    turn = "Elara mends the mill wheel. " + "x" * 1200 + " The duke is dead."
    lines = [
        line
        for line in gate_state(Evidence(), [turn]).splitlines()
        if line.startswith("T")
    ]
    assert len(lines) == 1
    text = lines[0].partition(": ")[2]
    assert text.startswith("…")
    assert text.endswith("The duke is dead.")
    assert "Elara mends" not in text


def test_a_narrow_window_keeps_each_turn_in_full():
    """N=2 x 600 chars: ``1200 // 2 = 600`` per line, so nothing is clipped."""
    first, second = "a" * 600, "b" * 600
    lines = gate_state(Evidence(), [first, second]).splitlines()
    assert lines == ["<history>", f"T1: {first}", f"T2: {second}", "</history>"]


def test_a_long_window_stops_at_the_per_line_floor():
    """Past a point the budget share would efface a turn, so the floor takes over.

    ``1200 // 30 = 40`` chars shows too little of a turn to hold a durable fact;
    :data:`GATE_HISTORY_MIN_LINE_CHARS` is the smallest line worth asking about, so
    a very wide window overruns the total rather than hiding its turns.
    """
    turns = ["x" * 200] * 30
    lines = [
        line
        for line in gate_state(Evidence(), turns).splitlines()
        if line.startswith("T")
    ]
    assert len(lines) == 30
    assert all(
        len(line.partition(": ")[2]) == GATE_HISTORY_MIN_LINE_CHARS for line in lines
    )


def test_a_turn_line_is_whitespace_collapsed():
    """One turn is ONE line, whatever newlines/indentation the prose carries."""
    state = gate_state(Evidence(), ["Rain falls.\n\n   Elara   turns away."])
    assert state.splitlines() == [
        "<history>",
        "T1: Rain falls. Elara turns away.",
        "</history>",
    ]


def test_a_joined_string_is_one_turn_not_a_guessed_window():
    """The seam a caller that pre-joins ``narrative_parts`` gets: one turn.

    Nothing in the join marks a turn boundary, so splitting is impossible and
    guessing one is worse — a joined string reads as a single-turn history.
    """
    state = gate_state(Evidence(), "The mill stands. Rain falls.")
    assert state.splitlines() == [
        "<history>",
        "T1: The mill stands. Rain falls.",
        "</history>",
    ]


def test_an_empty_window_adds_no_history_block():
    """No prose ⇒ no block (an empty ``<history>`` would be a lie about a window)."""
    assert gate_state(Evidence(names=("Elara",)), []) == "<named>Elara</named>"
    assert gate_state(Evidence(), "") == ""


# -- the real seam: driven OFF-GRAPH (W9.23 §3.6) ---------------------------
#
# The stage left the turn: ``state.historian`` drives these same four halves over
# the evidence window, and the graph no longer wires a proposal fan-out at all.
# The two tests below used to run a compiled turn with ``build_graph(fanouts=...)``;
# they now drive the fan-out the way the historian does, which is the seam the
# engine actually uses (the turn-side retirement is pinned in
# test_graph_topology.py).


def _window_state(log, *, actor="Elara"):
    """The synthetic state the historian builds: one window, one unioned payload."""
    from fabulist.state.historian import historian_window

    _narrative(log)
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {"Elara": {"physical": {"hp": "fine"}}},
            "venue": {"Elara": {"venue": "Miller\'s Bend", "area": "the mill"}},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )
    window = historian_window(log, SID)
    return {
        "session_id": SID,
        "narrative_parts": window["narrative_parts"],
        "narrative_event": window["narrative_event"],
        "world_state_event": window["world_state_event"],
    }


def test_the_four_halves_driven_off_graph_file_the_judged_rows(log):
    """End to end without the graph: dispatcher → 3 Sends → lanes → join."""
    _seed_world(log)
    generator = FakeGenerator(
        {
            "world": {"proposals": [_world_row()]},
            "actors": {
                "proposals": [
                    {
                        "action": "add",
                        "patch": {"name": "Bram", "description": "The miller."},
                        "evidence": "Bram was named in the scene.",
                    }
                ]
            },
        }
    )
    fanout = _fanout(log, generator=generator, decision=FakeDecision())
    merged = run(drive(fanout, _window_state(log)))

    assert len(generator.calls) == 3  # one per data type
    # The lanes' two kept rows, in dispatcher order; nothing else is ever filed
    # off-turn (cast proposals stay the BTS turn's, §3.6.6).
    assert [row["kind"] for row in _proposal_rows(log)] == ["world", "actor"]
    assert _summary(log)["kept"] == 2
    assert merged["_join_update"]["proposal_meta"]["emitted"] is True


def test_the_window_reaches_the_gate_one_line_per_turn(log):
    """The LIVE shape: the fan-out's ask renders the window's turns as T1/T2.

    The historian hands ``narrative_parts`` one string per turn; the dispatcher
    must pass that list through, because a pre-joined blob renders ONE line — the
    miscalibration D81 exists to end.
    """
    from fabulist.state.historian import historian_window

    _seed_world(log)
    _narrative(log, "Elara mends the mill wheel.")
    _narrative(log, "Bram says the duke is dead.")
    log.append_event(SID, "system", "bts", _world_state_payload(venue=("Elara",)))
    window = historian_window(log, SID)
    assert window["narrative_parts"] == [
        "Elara mends the mill wheel.",
        "Bram says the duke is dead.",
    ]
    decision = FakeDecision(gate=0.1)
    merged = run(
        drive(
            _fanout(log, generator=FakeGenerator(), decision=decision),
            {
                "session_id": SID,
                "narrative_parts": window["narrative_parts"],
                "narrative_event": window["narrative_event"],
                "world_state_event": window["world_state_event"],
            },
        )
    )
    assert merged["proposal_meta"]["gate"] == "verdict_no"  # one ask, no lanes
    assert len(decision.gate_calls) == 1
    asked = decision.gate_calls[0][0].splitlines()
    assert "T1: Elara mends the mill wheel." in asked
    assert "T2: Bram says the duke is dead." in asked


def test_a_quiet_second_run_does_not_inherit_the_first_runs_rows(log):
    """Each drive builds its OWN state: the channels never carry over (§3.6.2)."""
    _seed_world(log)
    generator = FakeGenerator({"world": {"proposals": [_world_row()]}})
    fanout = _fanout(log, generator=generator, decision=FakeDecision())

    first = run(drive(fanout, _window_state(log)))
    assert len(_system_rows(log, SUMMARY_EVENT)) == 1
    calls = len(generator.calls)

    # A second window that touched nothing: the dispatcher gates it off, so no
    # lane runs, no row is appended, and its own channels start EMPTY.
    quiet = {
        "session_id": SID,
        "narrative_parts": ["I say nothing."],
        "narrative_event": {"id": 0, "seq": 0, "payload": {}},
        "world_state_event": {},
    }
    second = run(drive(fanout, quiet))

    assert len(_system_rows(log, SUMMARY_EVENT)) == 1
    assert len(generator.calls) == calls  # no lane dispatched
    assert first["proposal_rows"]  # the first window filed rows
    assert second.get("proposal_rows", {}) == {}
    assert second["proposal_meta"]["lanes"] == []


def test_a_disabled_decision_caller_reads_as_gate_off_without_a_call(log):
    """No usable ``decision`` binding ⇒ gate off BEFORE any call (feature-off rule)."""

    class DisabledCaller:
        enabled = False

        def __init__(self):
            self.calls = 0

        async def ask(self, state, questions):  # pragma: no cover - never reached
            self.calls += 1
            return None

    state = _turn_state(log)
    caller = DisabledCaller()
    generator = FakeGenerator()
    merged = run(drive(_fanout(log, generator=generator, decision=caller), state))

    assert merged["proposal_meta"]["gate"] == "gate_off"
    assert caller.calls == 0
    assert generator.calls == []


# -- the diff-first lane contract (W9.23 §3.4.4) ----------------------------
#
# The lane CHOOSES operations against what it was SHOWN; it is never asked to
# regenerate what it is not changing. These pins defend both halves: the parser
# refuses a whole-value amend (and every op it could not apply exactly), and the
# payload carries the bounded CURRENT-VALUE slice the `old` values are copied
# from.


def _parse(payload, spec):
    from fabulist.state.proposal_lanes import parse_proposal_batch

    return parse_proposal_batch(payload, spec, [{"id": "w_mill", "name": "Miller's Bend"}])


def _world_spec():
    from fabulist.state.proposal_lanes import lane_spec

    return lane_spec("world")


def _book_spec():
    from fabulist.state.proposal_lanes import lane_spec

    return lane_spec("books")


def test_an_amend_without_ops_is_dropped(log):
    """§3.4.4: an amend is an OPERATION LIST; a whole-value patch is not legal."""
    rows, dropped = _parse(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": "w_mill",
                    "patch": {"description": "A mill town."},
                    "evidence": "Elara says so.",
                }
            ]
        },
        _world_spec(),
    )

    assert rows == []
    assert dropped[0]["reason"] == "ops"


def test_a_row_over_the_op_cap_is_dropped_whole(log):
    """§3.4.1: a truncated op list would apply LESS than the owner approved."""
    ops = [
        {"op": "add", "path": f"provenance.k{index}", "new": index} for index in range(5)
    ]
    rows, dropped = _parse(
        {"proposals": [{"action": "amend", "target": "w_mill", "ops": ops, "evidence": "x"}]},
        _world_spec(),
    )

    assert rows == []
    assert dropped[0]["reason"] == "over_cap_ops"


def test_an_op_path_from_another_kind_is_dropped(log):
    """Drift runs over op PATH ROOTS, exactly as it did over patch keys."""
    rows, dropped = _parse(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": "w_mill",
                    "ops": [{"op": "add", "path": "personality", "new": "Wry."}],
                    "evidence": "x",
                }
            ]
        },
        _world_spec(),
    )

    assert rows == []
    assert dropped[0]["reason"] == "kind"


def test_a_reserved_op_path_is_dropped(log):
    """The world lane still may not rename the world (§3.4.1's one exception)."""
    rows, dropped = _parse(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": "w_mill",
                    "ops": [
                        {"op": "modify", "path": "name", "old": "Miller's Bend", "new": "Vale"}
                    ],
                    "evidence": "x",
                }
            ]
        },
        _world_spec(),
    )

    assert rows == []
    assert dropped[0]["reason"] == "reserved"


def test_an_entry_path_is_legal_only_in_the_books_lane(log):
    """``entries.<id>`` addresses a book's list; no other lane has that list."""
    rows, dropped = _parse(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": "w_mill",
                    "ops": [
                        {
                            "op": "delete",
                            "path": "entries.abc",
                            "old": {"content": "A mill."},
                        }
                    ],
                    "evidence": "x",
                }
            ]
        },
        _world_spec(),
    )

    assert rows == []
    assert dropped[0]["reason"] == "kind"


def test_an_entry_add_mints_the_key_it_will_have(log):
    """§3.4.3: the parser mints the ``entry_id``, in the PATH and in the entry."""
    from fabulist.state.proposal_lanes import parse_proposal_batch

    rows, dropped = parse_proposal_batch(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": "b_lore",
                    "ops": [
                        {
                            "op": "add",
                            "path": "entries",
                            "new": {"keys": ["mill"], "content": "A mill."},
                        }
                    ],
                    "evidence": "The mill is named.",
                }
            ]
        },
        _book_spec(),
        [{"id": "b_lore", "name": "Vale lore"}],
    )

    assert dropped == []
    (op,) = rows[0]["ops"]
    entry_id = op["new"]["entry_id"]
    assert entry_id
    assert op["path"] == f"entries.{entry_id}"


def test_a_modify_of_an_unkeyed_entry_is_dropped(log):
    """§3.4.3: an unkeyed book is add-only until its first re-write."""
    from fabulist.state.proposal_lanes import parse_proposal_batch

    for op in (
        {"op": "modify", "path": "entries.null", "old": {"content": "A"}, "new": {}},
        {"op": "delete", "path": "entries.null", "old": {"content": "A"}},
    ):
        rows, dropped = parse_proposal_batch(
            {"proposals": [{"action": "amend", "target": "b_lore", "ops": [op], "evidence": "x"}]},
            _book_spec(),
            [{"id": "b_lore", "name": "Vale lore"}],
        )
        assert rows == []
        assert dropped[0]["reason"] == "unkeyed"


def test_the_filed_row_carries_the_parsed_ops_verbatim(log):
    """What the parser normalized is what the store compares against at accept."""
    from fabulist.state.proposal_lanes import proposal_event_payload

    op = {
        "op": "modify",
        "path": "description",
        "old": "A river town.",
        "new": "A mill town.",
    }
    rows, _ = _parse(
        {"proposals": [{"action": "amend", "target": "w_mill", "ops": [op], "evidence": "x"}]},
        _world_spec(),
    )

    payload = proposal_event_payload(rows[0])
    assert payload["ops"] == [op]
    assert payload["patch"] == {}
    assert validate_event("system", payload) == []


# -- W9.30 §3.2: a books row may name a book that does not exist yet --------
#
# The D90 defect: a world with ``book_ids: []`` gives a place no entry target,
# so the books lane authored nothing. The fix is one rule — an amend's target may
# be a book NAME — and these pins are its guards: a NAME (not an invented id, not
# prose), never a near-duplicate of what is stored, and entry adds only.


_ENTRY_ADD = {
    "op": "add",
    "path": "entries",
    "new": {"keys": ["the greenhouse"], "content": "Glass, warm, behind the mill."},
}


def _parented_parse(target, ops=None, *, digest=(), spec=None):
    """Parse ONE books-lane amend whose target names a book the digest lacks."""
    from fabulist.state.proposal_lanes import parse_proposal_batch

    return parse_proposal_batch(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": target,
                    "ops": [dict(op) for op in (ops or [_ENTRY_ADD])],
                    "evidence": "The greenhouse behind the mill is named.",
                }
            ]
        },
        spec or _book_spec(),
        list(digest),
    )


def test_a_books_amend_may_name_a_book_the_digest_does_not_list():
    """§3.1: the absence is expressed by the TARGET — one ordinary amend row."""
    from fabulist.state.proposal_lanes import proposal_event_payload

    rows, dropped = _parented_parse(
        "The Mill Ledger", digest=[{"id": "b_vale", "name": "Vale lore"}]
    )

    assert dropped == []
    (row,) = rows
    assert row["kind"] == "book" and row["action"] == "amend"
    # The filed row names the missing book TWICE: the target is the name (there
    # is no id yet) and target_name is what the surface renders.
    assert row["target"] == "The Mill Ledger"
    assert row["target_name"] == "The Mill Ledger"
    assert row["target_new"] is True
    assert row["patch"] == {}
    # The entry add minted its id in the path AND the entry (the accept writes
    # exactly this), and the row passes the append door with the marker.
    (op,) = row["ops"]
    assert op["path"] == f"entries.{op['new']['entry_id']}"
    payload = proposal_event_payload(row)
    assert payload["target_new"] is True
    assert validate_event("system", payload) == []


def test_a_parented_row_is_the_only_shape_that_carries_the_marker():
    """§3.1: ``target_new`` separates born-parented from gone-stale."""
    from fabulist.state.proposal_lanes import parse_proposal_batch, proposal_event_payload

    rows, _ = parse_proposal_batch(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": "b_lore",
                    "ops": [dict(_ENTRY_ADD)],
                    "evidence": "The mill is named.",
                }
            ]
        },
        _book_spec(),
        [{"id": "b_lore", "name": "Vale lore"}],
    )

    payload = proposal_event_payload(rows[0])
    assert "target_new" not in payload
    assert payload["target"] == "b_lore"


def test_the_world_lane_still_refuses_an_invented_target():
    """L4: a world is never added from play, so a miss stays a drop."""
    rows, dropped = _parse(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": "The Far Reach",
                    "ops": [{"op": "add", "path": "description", "new": "Snow."}],
                    "evidence": "x",
                }
            ]
        },
        _world_spec(),
    )

    assert rows == []
    assert dropped[0]["reason"] == "target"


def test_the_actor_lane_still_refuses_an_invented_target():
    """A person's card is never invented by a lookup miss (only books are)."""
    from fabulist.state.proposal_lanes import lane_spec, parse_proposal_batch

    rows, dropped = parse_proposal_batch(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": "Marcus the Younger",
                    "ops": [
                        {"op": "add", "path": "description", "new": "A ledger keeper."}
                    ],
                    "evidence": "x",
                }
            ]
        },
        lane_spec("actors"),
        [{"id": "p_marcus", "name": "Marcus"}],
    )

    assert rows == []
    assert dropped[0]["reason"] == "target"


def test_a_parented_name_that_reads_as_a_stored_book_is_parent_ambiguous():
    """§3.2.2: "The Vale Ledger" when a book IS "Vale Ledger" is that book."""
    for target, stored in (
        ("The Vale Ledger", "Vale Ledger"),  # one surface once the article goes
        ("Vale Ledger of Co", "Vale Ledger"),  # either containment
        ("vale ledger", "The Vale Ledger"),
    ):
        rows, dropped = _parented_parse(
            target, digest=[{"id": "b_vale", "name": stored}]
        )

        assert rows == [], target
        assert dropped[0]["reason"] == "parent_ambiguous"
        assert stored in dropped[0]["detail"]


def test_a_parented_name_that_reads_as_a_stored_entry_key_is_parent_ambiguous():
    """A book's name need never echo its contents, so its entry keys count too."""
    rows, _ = _parented_parse("the greenhouse", digest=())
    assert rows, "the same name is filable when nothing stored names it"

    # The digest row shape ``stored_digest`` produces: the name PLUS the current
    # slice whose entries carry the keys a parented target could shadow.
    digest = [
        {
            "id": "b_vale",
            "name": "Vale & Co. Ledger",
            "summary": "1 entry",
            "current": {
                "fields": {},
                "entries": [{"id": "e1", "keys": ["greenhouse"]}],
            },
        }
    ]

    rows, dropped = _parented_parse("the greenhouse", digest=digest)

    assert rows == []
    assert dropped[0]["reason"] == "parent_ambiguous"


def test_a_parented_row_may_only_add_entries():
    """§3.2.3: a modify/delete against a book that does not exist cannot resolve."""
    for op in (
        {"op": "modify", "path": "name", "old": "The Mill Ledger", "new": "Mill"},
        {"op": "delete", "path": "provenance.origin", "old": "studio"},
        {"op": "delete", "path": "entries.abc123", "old": {"content": "A mill."}},
        {
            "op": "modify",
            "path": "entries.abc123",
            "old": {"content": "A mill."},
            "new": {"content": "A bigger mill."},
        },
    ):
        rows, dropped = _parented_parse("The Mill Ledger", [op, _ENTRY_ADD])

        assert rows == [], op
        assert dropped[0]["reason"] == "parent_ops"


def test_an_id_shaped_parent_name_is_refused():
    """§3.2.1: a model answering a miss with a made-up id must not mint a book."""
    for target in ("bk_lore", "wm_mill", "cm_kell"):
        rows, dropped = _parented_parse(target)

        assert rows == [], target
        assert dropped[0]["reason"] == "parent_name"


def test_a_parent_name_longer_than_the_cap_is_refused():
    """§3.2.1: a "name" past the cap is a sentence, and prose is not a container."""
    from fabulist.state.proposal_lanes import MAX_PARENT_NAME_CHARS

    rows, dropped = _parented_parse("M" * (MAX_PARENT_NAME_CHARS + 1))

    assert rows == []
    assert dropped[0]["reason"] == "parent_name"

    rows, _ = _parented_parse("M" * MAX_PARENT_NAME_CHARS)
    assert rows, "the cap itself is legal"


def test_a_book_remove_still_names_an_existing_book():
    """Only an AMEND may create: a remove of a missing book has nothing to take."""
    from fabulist.state.proposal_lanes import parse_proposal_batch

    rows, dropped = parse_proposal_batch(
        {
            "proposals": [
                {
                    "action": "remove",
                    "target": "The Mill Ledger",
                    "patch": {},
                    "evidence": "x",
                }
            ]
        },
        _book_spec(),
        [],
    )

    assert rows == []
    assert dropped[0]["reason"] == "target"


# -- the current-value slice the lane authors `old` from --------------------


def test_the_digest_slice_carries_current_values_and_truncation(log):
    """§3.4.4: per-field CURRENT values, clipped, with every clipped path named."""
    from fabulist.personas.registry import append_persona
    from fabulist.state.proposal_lanes import (
        MAX_SLICE_ENTRY_CHARS,
        MAX_SLICE_EXTENSION_CHARS,
        MAX_SLICE_FIELD_CHARS,
        MAX_SLICE_EXTENSION_KEYS,
    )

    append_persona(
        log,
        {
            "persona_id": "p_elara",
            "name": "Elara",
            "description": "long " * (MAX_SLICE_FIELD_CHARS // 2),
            "book_ids": ["b1"],
            "extensions": {
                f"k{index}": "v" * (MAX_SLICE_EXTENSION_CHARS + 10) for index in range(10)
            },
        },
    )
    from fabulist.state.proposal_lanes import lane_spec, stored_digest

    (row,) = stored_digest(log, lane_spec("actors"))
    current = row["current"]

    assert len(current["fields"]["description"]) <= MAX_SLICE_FIELD_CHARS
    assert "description" in current["truncated"]
    # Id-lists ride whole; every extension key is shown clipped and named.
    assert current["fields"]["book_ids"] == ["b1"]
    extension_paths = [path for path in current["fields"] if path.startswith("extensions.")]
    assert len(extension_paths) == MAX_SLICE_EXTENSION_KEYS
    assert all(path in current["truncated"] for path in extension_paths)
    assert "extensions.<more>" in current["truncated"]
    # A field the lane may not write is not in the slice at all (Q1: greetings
    # are out of proposal scope), which is what keeps the vocabulary honest.
    assert "first_mes" not in current["fields"]
    assert "alternate_greetings" not in current["fields"]


def test_the_book_slice_shows_entries_with_their_ids_and_a_total(log):
    """§3.4.3/§3.4.4: 12 entries max, the true total, unkeyed entries as null."""
    from fabulist.context.books import append_book
    from fabulist.state.proposal_lanes import (
        MAX_SLICE_ENTRIES,
        lane_spec,
        stored_digest,
    )

    append_book(
        log,
        {
            "book_id": "b_lore",
            "name": "Vale lore",
            "entries": [{"keys": [f"k{index}"], "content": "c"} for index in range(15)],
        },
    )
    (row,) = stored_digest(log, lane_spec("books"))

    current = row["current"]
    assert len(current["entries"]) == MAX_SLICE_ENTRIES
    assert current["entries_total"] == 15
    assert all(entry["id"] for entry in current["entries"])


def test_an_unkeyed_entry_reads_as_a_null_id(log):
    """A pre-W9.23 write leaves an unkeyed entry: the slice says so, not a lie."""
    from fabulist.context.books import BOOK_EVENT, BOOKS_ANCHOR
    from fabulist.state.proposal_lanes import lane_spec, stored_digest

    log.append_event(
        BOOKS_ANCHOR,
        "system",
        "user",
        {
            "event": BOOK_EVENT,
            "book_id": "b_old",
            "name": "Old lore",
            "entries": [{"keys": ["mill"], "content": "A mill."}],
            "provenance": {},
        },
    )

    (row,) = stored_digest(log, lane_spec("books"))
    assert row["current"]["entries"][0]["id"] is None


def test_only_the_assets_the_turn_is_about_get_a_slice(log):
    """§3.4.4 match rule: casefold containment in EITHER direction, capped at 4."""
    from fabulist.state.proposal_lanes import MAX_SLICED_ASSETS, sliced_rows

    digest = [
        {"id": f"w{index}", "name": name, "current": {"fields": {"description": "d"}}}
        for index, name in enumerate(
            ["Miller's Bend", "The Vale", "Far Keep", "Salt Road", "Old Mill"]
        )
    ]

    # Both directions: the turn names the asset plainly ("the vale" -> "vale"
    # matches the row's name), or it names something that CONTAINS the row's name
    # ("Old Mill courtyard" -> "Old Mill"). "the mill" matches Miller's Bend too:
    # the article is stripped BEFORE the substring rule runs, so the surface the
    # rule compares is "mill".
    sliced = sliced_rows(["the vale", "Old Mill courtyard", "the mill"], digest, "world")
    assert [row["id"] for row in sliced] == ["w0", "w1", "w4"]
    # A surface naming nothing in the catalog slices nothing.
    assert sliced_rows(["the river"], digest, "world") == []
    # The cap holds whatever the turn named.
    assert len(sliced_rows([row["name"] for row in digest], digest, "world")) == (
        MAX_SLICED_ASSETS
    )
    assert sliced_rows(["nobody"], digest, "world") == []


def test_a_book_slices_on_an_entry_key_when_its_name_never_echoes_it(log):
    """P1 (the live failure): a book's NAME is a label, not its contents.

    "Vale & Co. Ledger" names none of its entries, so name-only matching left the
    books lane unsliced: the model never saw the entry it was amending and
    authored ``old`` from memory (born-stale). The entry keys are the second
    matching surface, and they are read from the same slice the lane is shown.
    """
    from fabulist.context.books import append_book
    from fabulist.state.proposal_lanes import lane_spec, sliced_rows, stored_digest

    append_book(
        log,
        {
            "book_id": "b_ledger",
            "name": "Vale & Co. Ledger",
            "entries": [
                {"keys": ["ledger"], "content": "Balances, all of them short."},
                {"keys": ["shop"], "content": "The shop on the corner."},
                {"keys": ["greenhouse"], "content": "Glass, and too much steam."},
            ],
        },
    )
    rows = stored_digest(log, lane_spec("books"))

    for touched in (
        ["the greenhouse"],
        ["the shop ledger"],
        ["the greenhouse", "the shop ledger"],
    ):
        sliced = sliced_rows(touched, rows, "book")
        assert [row["id"] for row in sliced] == ["b_ledger"], touched
        # ...and the slice really does carry the entries the model may amend.
        assert [entry["keys"] for entry in sliced[0]["current"]["entries"]] == [
            ["ledger"],
            ["shop"],
            ["greenhouse"],
        ]


def test_a_book_with_no_overlapping_entry_key_is_not_sliced(log):
    """The widened rule is still a MATCH rule: no name, no key, no slice."""
    from fabulist.context.books import append_book
    from fabulist.state.proposal_lanes import lane_spec, sliced_rows, stored_digest

    append_book(
        log,
        {
            "book_id": "b_ledger",
            "name": "Vale & Co.",
            "entries": [{"keys": ["ledger"], "content": "Balances."}],
        },
    )
    rows = stored_digest(log, lane_spec("books"))

    assert sliced_rows(["the ledger"], rows, "book")
    assert sliced_rows(["the mill", "Elara"], rows, "book") == []
    # Worlds and actors keep the NAME rule alone: an entry-key match is books-only.
    assert sliced_rows(["the ledger"], rows, "world") == []
    assert sliced_rows(["the ledger"], rows, "actor") == []


def test_entry_keys_match_with_the_article_stripped_on_both_sides(log):
    """An article is never part of a name: "the greenhouse" == "greenhouse"."""
    from fabulist.state.proposal_lanes import sliced_rows

    digest = [
        {
            "id": "b_shop",
            "name": "The Shop Ledger",
            "current": {
                "fields": {"name": "The Shop Ledger"},
                "entries": [
                    {"id": "e1", "keys": ["a greenhouse"]},
                    {"id": "e2", "keys": ["an old ledger"]},
                ],
            },
        }
    ]

    # The article is stripped on BOTH sides, so every spelling of it matches: the
    # KEY carries one ("a greenhouse"), the turn's surface carries one, or both do.
    assert sliced_rows(["greenhouse"], digest, "book")
    assert sliced_rows(["the greenhouse"], digest, "book")
    assert sliced_rows(["an old ledger"], digest, "book")
    # The NAME surface is stripped too: "The Shop Ledger" matches "shop ledger".
    assert sliced_rows(["shop ledger"], digest, "world")
    # ...and stripping an article never invents a match the rule does not have.
    assert sliced_rows(["a mill"], digest, "book") == []


def test_the_slice_cap_still_holds_for_book_entry_matches(log):
    """P1 widens WHICH rows slice, never HOW MANY (the cost bound stays 4)."""
    from fabulist.state.proposal_lanes import MAX_SLICED_ASSETS, sliced_rows

    digest = [
        {
            "id": f"b{index}",
            "name": f"Book {index}",
            "current": {
                "fields": {"name": f"Book {index}"},
                "entries": [{"id": "e1", "keys": ["greenhouse"]}],
            },
        }
        for index in range(MAX_SLICED_ASSETS + 3)
    ]

    sliced = sliced_rows(["the greenhouse"], digest, "book")
    assert len(sliced) == MAX_SLICED_ASSETS
    assert [row["id"] for row in sliced] == [f"b{index}" for index in range(4)]


def test_the_payload_renders_the_slice_only_for_matched_assets(log):
    """The digest stays one line; the slice is its own bounded block."""
    from fabulist.prompts.proposals import build_lane_payload

    digest = [
        {
            "id": "w_mill",
            "name": "Miller's Bend",
            "summary": "A river town.",
            "current": {"fields": {"description": "A river town."}},
        },
        {
            "id": "w_keep",
            "name": "Far Keep",
            "summary": "A border fort.",
            "current": {"fields": {"description": "A border fort."}},
        },
    ]
    payload = build_lane_payload(
        lane="world",
        kind="world",
        touched=["Miller's Bend"],
        digest=digest,
        prose="Elara says the mill stands.",
    )

    current = payload.split("<current_values>", 1)[1].split("</current_values>", 1)[0]
    assert "w_mill" in current and "A river town." in current
    assert "w_keep" not in current
    # The digest block stays ONE clipped line per asset: the full value rides the
    # slice block only (the digest summary is a separate, shorter text here).
    stored = payload.split("<stored>", 1)[1].split("</stored>", 1)[0]
    assert "<current_values>" not in stored
    assert stored.count("Miller's Bend") == 1


def test_the_payload_renders_a_book_slice_reached_through_an_entry_key(log):
    """P1 end to end: the entry the turn touched really reaches the prompt."""
    from fabulist.prompts.proposals import build_lane_payload

    digest = [
        {
            "id": "b_ledger",
            "name": "Vale & Co. Ledger",
            "summary": "3 entries",
            "current": {
                "fields": {"name": "Vale & Co. Ledger"},
                "entries": [
                    {"id": "e1", "keys": ["greenhouse"], "content": "Glass."},
                ],
                "entries_total": 1,
            },
        }
    ]
    payload = build_lane_payload(
        lane="books",
        kind="book",
        touched=["the greenhouse"],
        digest=digest,
        prose="The greenhouse is out of glass.",
    )

    current = payload.split("<current_values>", 1)[1].split("</current_values>", 1)[0]
    assert "b_ledger" in current  # the book is the sliced asset
    assert "greenhouse" in current and "Glass." in current  # ...with its entry


def test_the_payload_says_so_when_nothing_matches(log):
    from fabulist.prompts.proposals import build_lane_payload

    payload = build_lane_payload(
        lane="world",
        kind="world",
        touched=["the mill"],
        digest=[{"id": "w1", "name": "Far Keep", "summary": "s", "current": {"fields": {}}}],
        prose="Elara says the mill stands.",
    )

    assert "(none of the stored assets matches this turn's entities)" in payload


def test_the_brief_teaches_operations_and_never_omission_deletion(log):
    """§3.4.4: the R1 "anything you omit is deleted" teaching is DEAD."""
    from fabulist.prompts.proposals import build_lane_system

    brief = build_lane_system(
        lane_label="the lore",
        kind="book",
        fields=("entries", "name", "provenance"),
        max_items=3,
    )

    assert "ops" in brief and "`old`" in brief
    assert "entries.<id>" in brief
    assert "omit is deleted" not in brief
    assert "anything you omit" not in brief
    # The entry OBJECT shape is still spelled out (the live bare-string defect).
    assert '"keys"' in brief and '"content"' in brief


def test_the_judgment_reads_the_operations_not_a_patch_blob(log):
    """§3.4.4: the rubric sees exactly what would change."""
    from fabulist.state.proposal_lanes import judgment_state

    state = judgment_state(
        {
            "kind": "world",
            "action": "amend",
            "target": "w_mill",
            "patch": {},
            "ops": [
                {"op": "modify", "path": "scenario", "old": "Rain.", "new": "Snow."},
                {"op": "delete", "path": "provenance.origin", "old": "play"},
            ],
            "evidence": "It snows now.",
        },
        "The turn.",
    )

    assert "<ops>" in state and "<patch>" not in state
    assert "modify scenario: \"Rain.\" -> \"Snow.\"" in state
    assert "delete provenance.origin" in state
    # Sorted paths: deterministic state, therefore a deterministic probability.
    assert state.index("provenance.origin") < state.index("scenario")


# -- W9.29 §9.1: the registry is evidence --------------------------------------
#
# The ``world_state`` payload stops being written (the compat render retires in
# W4), so on a registry-era window these tests are the ONLY proof the stage can
# still see what the play established: the function walks the ``entity_*`` events
# between the window's seq bounds itself.


def _entity_window(log, *, start_seq=0):
    """A synthetic window state starting AFTER ``start_seq`` (the seq bound)."""
    event = _narrative(log, "The registry window opens.")
    return {
        "narrative_event": {"id": event.id, "seq": event.seq, "payload": event.payload},
        "world_state_event": {},
    }


def _establish(log, key, **args):
    from fabulist.state import entities

    if key == "location":
        return entities.establish_location(log, SID, args["name"], note=args.get("note", ""))
    if key == "thread":
        return entities.establish_thread(
            log, SID, args["name"], detail=args.get("detail", "")
        )
    if key == "relationship":
        return entities.establish_relationship(log, SID, args["from"], args["to"])
    raise AssertionError(key)


def test_the_registrys_objects_inside_the_window_are_evidence(log):
    """§9.1: places, threads, relationships and moved meters, one line each."""
    from fabulist.state import entities
    from fabulist.state.proposal_lanes import proposal_evidence

    state = _entity_window(log)
    bath = _establish(log, "location", name="bathroom")
    thread = _establish(log, "thread", name="the slow courtship")
    edge = entities.establish_relationship(log, SID, "player", "scene")
    entities.move_being(log, SID, "player", bath)
    mood_id, _prior = entities.set_attribute(log, SID, "player", key="mood", value="wary")
    meter_id, _prior = entities.set_attribute(
        log, SID, "player", key="hostility", value=42, subject="scene"
    )

    evidence = proposal_evidence(log, SID, state)

    assert f'place "bathroom" ({bath}) established' in evidence.objects
    assert f'thread "the slow courtship" ({thread}) established' in evidence.objects
    assert f"relationship player->scene ({edge}) established" in evidence.objects
    assert f"hostility player->scene = 42 ({meter_id})" in evidence.objects
    assert f"mood player = wary ({mood_id})" in evidence.objects
    # The object rows unlock the gate on their own (a payload may be absent).
    assert evidence.touched is True
    assert evidence.count() >= len(evidence.objects)


def test_objects_before_the_windows_bound_are_not_evidence(log):
    """The window's seq bound is the turn boundary: older objects stay out."""
    from fabulist.state import entities
    from fabulist.state.proposal_lanes import proposal_evidence

    _establish(log, "location", name="upstairs hall")  # BEFORE the window
    state = _entity_window(log)
    bath = _establish(log, "location", name="bathroom")

    evidence = proposal_evidence(log, SID, state)

    assert evidence.objects == (f'place "bathroom" ({bath}) established',)
    assert "upstairs hall" not in " ".join(evidence.objects)


def test_one_object_is_one_line_however_often_the_window_touched_it(log):
    """A meter set three times is one line carrying its LATEST value."""
    from fabulist.state import entities
    from fabulist.state.proposal_lanes import proposal_evidence

    state = _entity_window(log)
    meter_id, _ = entities.set_attribute(log, SID, "player", key="hostility", value=47)
    entities.set_attribute(log, SID, "player", key="hostility", value=42)
    entities.set_attribute(log, SID, "player", key="hostility", value=30)

    evidence = proposal_evidence(log, SID, state)

    lines = [line for line in evidence.objects if line.startswith("hostility")]
    assert lines == [f"hostility player = 30 ({meter_id})"]


def test_registry_objects_supply_the_lane_surfaces_too(log):
    """The names/places join the payload slices, or no lane could ever slice.

    A lane authors ``old`` from the CURRENT-VALUE slice, and ``sliced_rows``
    matches by the touched surfaces: without the registry's own names a
    registry-era window would send every lane a digest it can never slice.
    """
    from fabulist.state import entities
    from fabulist.state.cast import admit_member, member_by_name, roster
    from fabulist.state.proposal_lanes import proposal_evidence

    admit_member(log, SID, "Elara", provenance="card", admitted_by="user")
    elara = member_by_name(roster(log, SID), "Elara").member_id
    state = _entity_window(log)
    _establish(log, "location", name="the greenhouse")
    entities.move_being(log, SID, elara, "off_screen")
    entities.set_attribute(log, SID, elara, key="mood", value="wary")

    evidence = proposal_evidence(log, SID, state)

    assert "Elara" in evidence.names  # the member id renders its display name
    assert "the greenhouse" in evidence.places


def test_the_evidence_carries_at_most_twenty_object_rows(log):
    from fabulist.state import entities
    from fabulist.state.proposal_lanes import MAX_TOUCHED_ENTITIES, proposal_evidence

    state = _entity_window(log)
    for index in range(MAX_TOUCHED_ENTITIES + 5):
        entities.establish_location(log, SID, f"room {index}")

    evidence = proposal_evidence(log, SID, state)

    assert len(evidence.objects) == MAX_TOUCHED_ENTITIES


def test_the_gate_state_shows_the_tracked_objects():
    """The gate's own view carries them, or a registry-era window looks quiet."""
    state = gate_state(Evidence(objects=('place "bathroom" (loc_4b7e) established',)), "")
    assert state == '<tracked>place "bathroom" (loc_4b7e) established</tracked>'


def test_every_lane_sees_the_registry_slice():
    for spec in PROPOSAL_LANES:
        assert "objects" in spec.slices


# -- W9.29 §9.2: permanence targeting is LAYER-OWNED ---------------------------


def test_permanence_targets_resolves_a_world_backed_chat(log):
    """req. 7: a world-backed chat's source IS the world, and its books."""
    from fabulist.context.books import append_book
    from fabulist.state.proposal_lanes import permanence_targets
    from fabulist.worlds.registry import append_world

    append_world(
        log,
        {"world_id": "w_mill", "name": "Miller's Bend", "book_ids": ["b_vale"]},
    )
    log.append_event(
        SID,
        "system",
        "user",
        {"event": "world_ref", "world_id": "w_mill", "name": "Miller's Bend"},
    )

    targets = permanence_targets(log, SID)

    assert targets["source"] == "world"
    assert targets["world_id"] == "w_mill"
    # The SAME book set the narrator's world-info render resolves (D31): the lane
    # must name a book the render actually carries.
    assert targets["book_ids"] == ["b_vale"]


def test_permanence_targets_falls_back_to_the_card_layer(log):
    """req. 7: a card-only chat's char card IS the world (the chat's actors)."""
    from fabulist.memory.actor import append_chat_actor
    from fabulist.state.proposal_lanes import permanence_targets

    append_chat_actor(log, SID, ["p_seraphine"])

    targets = permanence_targets(log, SID)

    assert targets == {"source": "card", "persona_ids": ["p_seraphine"]}


def test_permanence_targets_reads_the_card_personas_the_session_carries(log):
    """A session whose cards match personas names them even with no actor row."""
    from fabulist.personas.registry import append_persona
    from fabulist.state.proposal_lanes import permanence_targets

    persona_id = append_persona(log, {"name": "Seraphine", "description": "The host."})
    log.append_event(
        SID, "system", "cards", {"card": {"name": "Seraphine", "description": "x"}}
    )

    targets = permanence_targets(log, SID)

    assert targets["source"] == "card"
    assert targets["persona_ids"] == [persona_id]


def test_permanence_targets_survives_an_unreadable_log():
    """Best-effort: an unreadable chain reads as a card chat with no ids."""
    from fabulist.state.proposal_lanes import permanence_targets

    class Broken:
        def get_chain_events(self, session_id):
            raise RuntimeError("db gone")

    assert permanence_targets(Broken(), SID) == {"source": "card", "persona_ids": []}
