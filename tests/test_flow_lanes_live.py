"""Issue #56.1: the live preview must draw the CURRENT turn's NPC lanes.

The drawer (`ChatFlowPanel` → `FlowGraph` arms) reads one endpoint,
``GET /api/sessions/{id}/lanes``. This module pins what that endpoint serves
while a turn is IN FLIGHT — the reported bug is that it served the PREVIOUS
turn's members (or nothing) for the whole of the current turn, because the
engine wrote no lane fact until the fan-in join, minutes after the lanes were
planned.

The engine's answer (issue #56.1) is the ``npc_lanes_plan`` row: appended at
DISPATCH time, in dispatcher order, naming the members the turn is about to
lane. A plan newer than the newest ``npc_lanes`` summary means "in flight", and
the endpoint serves those members marked ``pending``; once the join writes the
summary, the same read serves the completed set (intents and outcomes) exactly
as before.

Fake models only — no live inference.
"""

from __future__ import annotations

import asyncio

import pytest

from fabulist.graph.nodes import make_npc_lanes_node
from fabulist.state.cast import admit_member

SID = "s-live"


class _GatedLaneCaller:
    """A lane caller that HOLDS every lane open until the test releases it.

    This is the mid-turn window the bug lived in: the lanes are planned and
    running, and nothing has been written yet.
    """

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    @staticmethod
    def _name(messages: list[dict]) -> str:
        # Issue #73: the lane brief rides the ASSISTANT carrier, so the PRIVATE
        # marker is found by content, not by role.
        for message in reversed(messages):
            for line in str(message.get("content") or "").splitlines():
                if line.startswith("## PRIVATE — you are "):
                    return line.split("## PRIVATE — you are ", 1)[1].strip()
        return ""

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls.append(messages)
        name = self._name(messages)
        self.started.set()
        await self.release.wait()
        yield {
            "type": "content",
            "delta": (
                '{"intent": "' + name + ' wants the ledger", '
                '"narrative": "' + name + ' steps forward", "lines": []}'
            ),
        }


class _Narrator:
    def __init__(self):
        self.calls = 0

    async def __call__(self, messages, stream=True):
        self.calls += 1
        yield {"type": "content", "delta": "Rain drums on the shutters."}


class _Extractor:
    async def __call__(self, user_msg, prose):
        return {"deltas": {}, "action_class": "dialogue"}


class _App:
    """The minimum ``Request`` surface the lanes route reads."""

    def __init__(self, log):
        self.state = type("S", (), {"log": log})()


class _Req:
    def __init__(self, log):
        self.app = _App(log)


class _Decision:
    """A d1-3b double whose verdict clears no threshold (a zero-lane answer)."""

    def __init__(self, probabilities):
        self.probabilities = dict(probabilities)
        self.calls: list[dict] = []

    async def ask(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        question_id = next(iter(questions))
        return {
            question_id: {
                "choice": next(iter(self.probabilities), ""),
                "probabilities": dict(self.probabilities),
                "confidence": 0.9,
            }
        }


def _admit(log, name):
    return admit_member(
        log,
        SID,
        name,
        provenance="card",
        admitted_by="test",
        reference_kind="identity",
    )


def _world_state(log, names):
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {},
            "venue": {name: {"venue": "tavern", "area": "bar"} for name in names},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )


def _plan_rows(log):
    return [
        event.payload
        for event in log.get_events(SID)
        if isinstance(event.payload, dict)
        and event.payload.get("event") == "npc_lanes_plan"
    ]


def test_the_turn_in_flight_serves_its_planned_members_then_its_answers(log):
    """The repro: a lane planned mid-turn is what the drawer must draw."""
    from fabulist.api import flow_api
    from fabulist.graph.engine import GraphTurnEngine
    from fabulist.graph.graph import CANONICAL_TOPOLOGY, build_graph

    kael = _admit(log, "Kael")
    mara = _admit(log, "Mara")
    _world_state(log, ["Kael", "Mara"])

    caller = _GatedLaneCaller()
    lane_node = make_npc_lanes_node(
        log=log,
        build_messages=lambda state: [
            {"role": "system", "content": "You are the narrator."},
            {"role": "user", "content": state["user_content"]},
        ],
        caller=caller,
    )
    graph = build_graph(
        model=_Narrator(),
        extractor=_Extractor(),
        log=log,
        build_messages=lambda state: [
            {"role": "system", "content": "You are the narrator."},
            {"role": "user", "content": state["user_content"]},
        ],
        topology=CANONICAL_TOPOLOGY,
        npc_lanes=lane_node,
    )
    engine = GraphTurnEngine(graph=graph, log=log, model=_Narrator())

    async def scenario():
        frames: list[dict] = []

        async def drive():
            async for frame in engine.run_turn(SID, "I wait."):
                frames.append(frame)

        task = asyncio.create_task(drive())
        # Wait until the first lane is actually blocked inside its model call:
        # the plan is written, the summary is not.
        await asyncio.wait_for(caller.started.wait(), timeout=10)
        mid = await flow_api.get_lanes_route(SID, _Req(log))
        caller.release.set()
        await asyncio.wait_for(task, timeout=20)
        done = await flow_api.get_lanes_route(SID, _Req(log))
        return mid, done

    mid, done = asyncio.run(scenario())

    # MID-TURN: both members are drawn, marked pending, with no intent yet —
    # this is what the bug could not do (it served an empty/stale set).
    assert [lane["name"] for lane in mid["lanes"]] == ["Kael", "Mara"]
    assert all(lane["pending"] is True for lane in mid["lanes"])
    assert mid["in_flight"] is True
    assert {lane["member_id"] for lane in mid["lanes"]} == {kael, mara}
    assert mid["source"] == "presence"

    # AFTER THE JOIN: the same read serves the completed set, the members'
    # answers replacing the placeholders, in dispatcher order.
    assert [lane["name"] for lane in done["lanes"]] == ["Kael", "Mara"]
    assert all(lane["pending"] is False for lane in done["lanes"])
    assert done["in_flight"] is False
    assert all(lane["intent"] for lane in done["lanes"])
    assert done["turn_seq"] > 0


def test_the_plan_row_is_written_before_the_first_lane_row(log):
    """The plan is a fact of DISPATCH, not of the fan-in (the bug's root cause)."""
    from fabulist.graph.engine import GraphTurnEngine
    from fabulist.graph.graph import CANONICAL_TOPOLOGY, build_graph

    _admit(log, "Kael")
    _world_state(log, ["Kael"])
    caller = _GatedLaneCaller()

    def build(state):
        return [
            {"role": "system", "content": "You are the narrator."},
            {"role": "user", "content": state["user_content"]},
        ]

    lane_node = make_npc_lanes_node(log=log, build_messages=build, caller=caller)
    graph = build_graph(
        model=_Narrator(),
        extractor=_Extractor(),
        log=log,
        build_messages=build,
        topology=CANONICAL_TOPOLOGY,
        npc_lanes=lane_node,
    )
    engine = GraphTurnEngine(graph=graph, log=log, model=_Narrator())

    async def scenario():
        async def drive():
            return [frame async for frame in engine.run_turn(SID, "I wait.")]

        task = asyncio.create_task(drive())
        await asyncio.wait_for(caller.started.wait(), timeout=10)
        plan_seq = [
            event.seq for event in log.get_events(SID)
            if isinstance(event.payload, dict)
            and event.payload.get("event") == "npc_lanes_plan"
        ]
        caller.release.set()
        await asyncio.wait_for(task, timeout=20)
        return plan_seq

    plan_seq = asyncio.run(scenario())

    lane_seqs = [
        event.seq
        for event in log.get_events(SID)
        if isinstance(event.payload, dict) and event.payload.get("event") == "npc_lane"
    ]
    assert plan_seq and lane_seqs
    assert max(plan_seq) < min(lane_seqs)


def test_a_zero_lane_turn_records_an_empty_plan(log):
    """A policy-emptied turn is an ANSWER: the preview must show zero, not stale."""
    from fabulist.graph.engine import GraphTurnEngine
    from fabulist.graph.graph import CANONICAL_TOPOLOGY, build_graph

    _admit(log, "Kael")
    _admit(log, "Mara")
    _world_state(log, ["Kael", "Mara"])
    caller = _GatedLaneCaller()

    def build(state):
        return [
            {"role": "system", "content": "You are the narrator."},
            {"role": "user", "content": state["user_content"]},
        ]

    # The dispatch verdict rates both members below the clearing threshold: a
    # legal zero-lane turn (W9.24 §3.5), which the preview must report as such.
    lane_node = make_npc_lanes_node(
        log=log,
        build_messages=build,
        caller=caller,
        decision=_Decision({"Kael": 0.02, "Mara": 0.01}),
    )
    graph = build_graph(
        model=_Narrator(),
        extractor=_Extractor(),
        log=log,
        build_messages=build,
        topology=CANONICAL_TOPOLOGY,
        npc_lanes=lane_node,
    )
    engine = GraphTurnEngine(graph=graph, log=log, model=_Narrator())

    async def drive():
        return [frame async for frame in engine.run_turn(SID, "I wait.")]

    asyncio.run(drive())

    plans = _plan_rows(log)
    assert plans, "a zero-lane turn must record its empty plan"
    assert plans[-1]["lanes"] == []
    assert plans[-1]["dispatched"] == 0
    assert caller.calls == []  # nothing ran


def test_the_lanes_read_ignores_a_plan_the_join_already_settled(log):
    """A settled plan must never shadow the completed set (idempotent read)."""
    from fabulist.api import flow_api
    from fabulist.graph.engine import GraphTurnEngine
    from fabulist.graph.graph import CANONICAL_TOPOLOGY, build_graph

    _admit(log, "Kael")
    _world_state(log, ["Kael"])
    caller = _GatedLaneCaller()
    caller.release.set()  # never blocks: the turn runs straight through

    def build(state):
        return [
            {"role": "system", "content": "You are the narrator."},
            {"role": "user", "content": state["user_content"]},
        ]

    lane_node = make_npc_lanes_node(log=log, build_messages=build, caller=caller)
    graph = build_graph(
        model=_Narrator(),
        extractor=_Extractor(),
        log=log,
        build_messages=build,
        topology=CANONICAL_TOPOLOGY,
        npc_lanes=lane_node,
    )
    engine = GraphTurnEngine(graph=graph, log=log, model=_Narrator())

    async def scenario():
        async def drive():
            return [frame async for frame in engine.run_turn(SID, "I wait.")]

        await drive()
        return await flow_api.get_lanes_route(SID, _Req(log))

    body = asyncio.run(scenario())
    assert body["in_flight"] is False
    assert [lane["name"] for lane in body["lanes"]] == ["Kael"]
    assert body["lanes"][0]["intent"]
