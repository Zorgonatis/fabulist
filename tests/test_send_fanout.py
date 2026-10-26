"""W9.17: the NPC lane fan-out is REAL LangGraph topology (``Send``).

W9.13 ran the lanes in a semaphore pool INSIDE one node. W9.17 splits that into
the shape the graph can draw, resume and inspect:

* ``npc_lanes`` — the DISPATCHER node (resolve the ACTIVE cast, build the shared
  prefix once, write the fan-out's bookkeeping). It calls no model;
* the fan-out conditional edge — ``list[Send("npc_lane", payload)]``, one per
  ACTIVE member, and the plain-edge target when there are none;
* ``npc_lane`` — ONE member, one model pass, under a MODULE-level pool;
* the join flush — the per-member rows in DISPATCHER order plus the summary,
  written at the fan-in because a fan-out returns no control flow of its own.

What these tests pin, in order: the reducers that make concurrent returns merge,
the dispatcher's Send list, best-effort failures, the concurrency bound, the
deterministic row order, the topology/contracts accept-or-reject rules, and the
zero-lane parity gate (a turn with no cast must stay byte-identical).

Fake models only — no live inference.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.graph.contracts import NODE_CONTRACTS, validate_topology
from fabulist.graph.graph import CANONICAL_TOPOLOGY, NODE_NAMES, build_graph
from fabulist.graph.nodes import make_npc_lanes_node
from fabulist.graph.types import (
    initial_turn_state,
    merge_lane_meta,
    merge_npc_lanes,
)
from fabulist.log import EventLog
from fabulist.state.cast import admit_member

SID = "s1"


# -- fakes -------------------------------------------------------------------


class LaneCaller:
    """One JSON lane reply per call, keyed by the lane's private block.

    Records the peak number of concurrent calls (``max_in_flight``) so the pool
    bound is measured, not assumed, and can stagger per member so completion
    order is the REVERSE of the dispatcher's member order.
    """

    def __init__(self, replies=None, delays=None, finish_reason=None):
        self.replies = dict(replies or {})
        self.delays = dict(delays or {})
        self.finish_reason = finish_reason
        self.calls: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0

    @staticmethod
    def _name(messages):
        # Issue #73: the lane brief rides the ASSISTANT carrier, so the PRIVATE
        # marker is found by content, not by role.
        for message in reversed(messages):
            for line in str(message.get("content") or "").splitlines():
                if line.startswith("## PRIVATE — you are "):
                    return line.split("## PRIVATE — you are ", 1)[1].strip()
        return ""

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            name = self._name(messages)
            await asyncio.sleep(self.delays.get(name, 0.0))
            reply = self.replies.get(name)
            if isinstance(reply, Exception):
                raise reply
            if reply is None:
                reply = {
                    "intent": f"{name} wants the door shut.",
                    "narrative": f"{name} moves to the door.",
                    "lines": [{"text": f"Not yet, {name}."}],
                }
            yield {"type": "content", "delta": json.dumps(reply)}
            if self.finish_reason:
                yield {
                    "type": "error",
                    "delta": "truncated",
                    "meta": {"finish_reason": self.finish_reason},
                }
        finally:
            self.in_flight -= 1


class NarratorModel:
    """A ``ModelCaller`` recording the prompt the narrator actually received."""

    def __init__(self, text="Rain drums."):
        self.text = text
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append([dict(message) for message in messages])
        yield {"type": "content", "delta": self.text}


def _extractor(user_msg, prose):
    return {"deltas": {}, "action_class": "dialogue"}


# -- log fixtures ------------------------------------------------------------


def _venue(log, *names):
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {},
            "venue": {name: {"venue": "bar", "area": "room"} for name in names},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )


def _cast(log, *names):
    """Admit ``names`` and put them all on stage; returns their member ids."""
    ids = [
        admit_member(log, SID, name, provenance="card", admitted_by="test", reference_kind="identity")
        for name in names
    ]
    _venue(log, *names)
    return ids


def _state(**overrides):
    state = {
        "session_id": SID,
        "user_content": "I wait.",
        "facts": "time: 09:15",
        "plan": "",
        "npc_lanes": {},
        "lane_meta": {},
        "lane_plan": [],
    }
    state.update(overrides)
    return state


def _fanout(log, **kwargs):
    kwargs.setdefault("build_messages", lambda state: [{"role": "system", "content": "SHARED"}])
    return make_npc_lanes_node(log=log, **kwargs)


async def _drive(fanout, state, *, reverse_completion=False):
    """Drive the fan-out exactly as the compiled graph does.

    The dispatcher's update goes through the graph's own reducers, one
    ``npc_lane`` runs per ``Send``, each partial return merges, then the join
    flush runs. ``reverse_completion`` starts the lanes in reverse ``Send`` order
    so a test can prove the emitted rows do not follow completion.
    """
    update = await fanout(state)
    merged = {**state, **{k: v for k, v in update.items() if k not in ("npc_lanes", "lane_meta")}}
    merged["npc_lanes"] = merge_npc_lanes(state.get("npc_lanes"), update["npc_lanes"])
    merged["lane_meta"] = merge_lane_meta(state.get("lane_meta"), update["lane_meta"])
    payloads = [send.arg for send in await fanout.route(merged)]
    if reverse_completion:
        payloads = list(reversed(payloads))
    for result in await asyncio.gather(*(fanout.lane(payload) for payload in payloads)):
        if "npc_lanes" in result:
            merged["npc_lanes"] = merge_npc_lanes(merged["npc_lanes"], result["npc_lanes"])
        if "lane_meta" in result:
            merged["lane_meta"] = merge_lane_meta(merged["lane_meta"], result["lane_meta"])
    joined = fanout.join(merged)
    if "lane_meta" in joined:
        merged["lane_meta"] = merge_lane_meta(merged["lane_meta"], joined["lane_meta"])
    return merged


def _payload(content: object) -> str:
    """The message body without the served two-bound envelope (issue #26).

    ``<preset_def>`` wraps the engine definition through the player's latest
    message, ``<preset_runtime>`` the per-turn tail after it; the guard line (on
    the first def message) sits outside both.
    """
    from fabulist.prompts.wrapper import (
        DEF_CLOSE,
        DEF_OPEN,
        GUARD_LINE,
        RUNTIME_CLOSE,
        RUNTIME_OPEN,
    )

    text = str(content)
    if text.startswith(GUARD_LINE):
        text = text[len(GUARD_LINE) :].lstrip("\n")
    for open_tag, close_tag in ((DEF_OPEN, DEF_CLOSE), (RUNTIME_OPEN, RUNTIME_CLOSE)):
        prefix = f"{open_tag}\n"
        if text.startswith(prefix) and text.rstrip().endswith(close_tag):
            body = text[len(prefix) :].rstrip()
            return body[: -len(close_tag)].rstrip("\n")
    return text


def _lane_rows(log):
    return [e.payload for e in log.get_events(SID) if e.payload.get("event") in ("npc_lane", "npc_lanes")]


# -- the reducers -----------------------------------------------------------


def test_merge_npc_lanes_keeps_both_members_and_later_key_wins():
    """Two concurrent partial returns MERGE; a collision resolves last-write."""
    marcus = {"name": "Marcus", "intent": "shut the door"}
    ilse = {"name": "Ilse", "intent": "watch"}

    merged = merge_npc_lanes({"cm_a": marcus}, {"cm_b": ilse})

    assert merged == {"cm_a": marcus, "cm_b": ilse}
    # Later key wins on collision (documented rule: a re-run of one lane).
    assert merge_npc_lanes({"cm_a": marcus}, {"cm_a": {**marcus, "intent": "leave"}}) == {
        "cm_a": {**marcus, "intent": "leave"}
    }
    # Order matters: the update is applied over the current value, not the reverse.
    assert list(merge_npc_lanes({"cm_b": ilse}, {"cm_a": marcus})) == ["cm_b", "cm_a"]


def test_merge_npc_lanes_empty_write_resets_the_channel():
    """An empty write is the TURN BOUNDARY, not a no-op (W9.6 no-inheritance)."""
    assert merge_npc_lanes({"cm_a": {"name": "Marcus"}}, {}) == {}
    assert merge_npc_lanes({}, {}) == {}
    assert merge_npc_lanes(None, None) == {}


def test_merge_lane_meta_sums_ran_and_concatenates_skipped():
    dispatcher = {
        "source": "presence",
        "candidates": 3,
        "ran": 0,
        "max_lanes": 4,
        "batches": 1,
        "skipped": [],
        "members": ["cm_a", "cm_b", "cm_c"],
    }
    first = merge_lane_meta({}, dispatcher)
    second = merge_lane_meta(first, {"ran": 1})
    third = merge_lane_meta(second, {"ran": 0, "skipped": [{"member_id": "cm_b", "reason": "boom"}]})

    assert third["ran"] == 1  # 0 + 1 + 0, summed across concurrent returns
    assert third["skipped"] == [{"member_id": "cm_b", "reason": "boom"}]
    # ...and concatenation keeps both rows, in merge order.
    assert merge_lane_meta(third, {"skipped": [{"member_id": "cm_c"}]})["skipped"] == [
        {"member_id": "cm_b", "reason": "boom"},
        {"member_id": "cm_c"},
    ]
    # The dispatcher's shape keys are NOT movable by a later (per-lane) write.
    assert merge_lane_meta(third, {"source": "fallback", "candidates": 9, "max_lanes": 1}) == third
    # An empty write resets (the turn boundary), like the lanes channel.
    assert merge_lane_meta(third, {}) == {}


# -- the dispatcher + its Send list ----------------------------------------


def test_dispatcher_emits_one_send_per_active_member(log):
    ids = _cast(log, "Marcus", "Ilse", "Zahra")

    fanout = _fanout(log, caller=LaneCaller())
    update = asyncio.run(fanout(_state()))
    sends = asyncio.run(fanout.route({**_state(), **update}))

    assert [send.node for send in sends] == ["npc_lane"] * 3
    # One Send per ACTIVE member, in the dispatcher's own member order.
    assert [send.arg["member"]["member_id"] for send in sends] == ids
    assert update["lane_meta"]["members"] == ids
    # The dispatcher writes no lanes itself: it only plans them.
    assert update["npc_lanes"] == {}
    assert update["lane_meta"]["ran"] == 0
    assert update["lane_meta"]["batches"] == 1


def test_dispatcher_emits_no_send_and_routes_to_the_narrator_without_cast(log):
    fanout = _fanout(log, caller=LaneCaller())

    update = asyncio.run(fanout(_state()))

    assert asyncio.run(fanout.route({**_state(), **update})) == []
    assert update["lane_plan"] == []
    assert update["lane_meta"]["members"] == []
    assert log.get_events(SID) == []  # nothing written, not even a summary


def test_the_shared_prefix_is_built_once_per_turn_and_never_per_lane(log):
    _cast(log, "Marcus", "Ilse", "Zahra")
    builds: list[int] = []

    def build_messages(state):
        builds.append(1)
        return [{"role": "system", "content": "SHARED"}]

    caller = LaneCaller()
    fanout = make_npc_lanes_node(log=log, build_messages=build_messages, caller=caller)
    update = asyncio.run(fanout(_state()))
    merged = {**_state(), **update}
    sends = asyncio.run(fanout.route(merged))

    async def run_all():
        return await asyncio.gather(*(fanout.lane(send.arg) for send in sends))

    results = asyncio.run(run_all())

    assert len(results) == 3

    assert len(builds) == 1  # the cache contract: the stable prefix is built ONCE
    for call in caller.calls:
        assert call["messages"][0] == {"role": "system", "content": "SHARED"}


# -- best-effort lanes -----------------------------------------------------


def test_route_threads_the_anti_injection_setting_into_every_payload(log):
    """Issue #77: the fan-out's Send payload carries the folded setting.

    The lane tail's <preset_runtime> wrap is decided per build from the same
    persisted setting the narrator's wrapper read, so the two carriers can
    never disagree inside one turn. Default OFF on a fresh log.
    """
    from fabulist.api.settings import SETTINGS_SESSION_ID
    from fabulist.state.settings import ANTI_INJECTION_FLAG, SETTINGS_EVENT

    _cast(log, "Marcus", "Ilse")
    fanout = _fanout(log, caller=LaneCaller())
    update = asyncio.run(fanout(_state()))

    off = asyncio.run(fanout.route({**_state(), **update}))
    assert [send.arg["anti_injection"] for send in off] == [False, False]

    log.append_event(
        SETTINGS_SESSION_ID,
        "system",
        "user",
        {"event": SETTINGS_EVENT, "features": {ANTI_INJECTION_FLAG: True}},
    )
    on = asyncio.run(fanout.route({**_state(), **update}))
    assert [send.arg["anti_injection"] for send in on] == [True, True]


def test_a_failed_lane_records_a_skip_and_never_raises(log):
    _cast(log, "Marcus", "Ilse")
    caller = LaneCaller(replies={"Ilse": RuntimeError("boom")})
    fanout = _fanout(log, caller=caller)

    merged = asyncio.run(_drive(fanout, _state()))

    assert merged["lane_meta"]["ran"] == 1
    assert [row["name"] for row in merged["lane_meta"]["skipped"]] == ["Ilse"]
    assert "boom" in merged["lane_meta"]["skipped"][0]["reason"]
    # The failed member has NO lane entry (so it never renders) but does get a
    # row in the log, marked not-ok, in member order.
    rows = _lane_rows(log)
    assert [(r["event"], r.get("ok")) for r in rows] == [
        ("npc_lane", True),
        ("npc_lane", False),
        ("npc_lanes", None),
    ]


def test_a_failed_lane_writes_no_npc_lanes_key_at_all(log):
    """A failed lane must not write ``{}``: an empty write RESETS its siblings."""
    _cast(log, "Marcus")
    fanout = _fanout(log, caller=LaneCaller(replies={"Marcus": RuntimeError("boom")}))
    update = asyncio.run(fanout(_state()))
    payload = asyncio.run(fanout.route({**_state(), **update}))[0].arg

    result = asyncio.run(fanout.lane(payload))

    assert "npc_lanes" not in result
    assert result["lane_meta"]["ran"] == 0


def test_truncated_reply_skips_the_lane(log):
    _cast(log, "Marcus")
    fanout = _fanout(log, caller=LaneCaller(finish_reason="length"))

    merged = asyncio.run(_drive(fanout, _state()))

    assert merged["npc_lanes"] == {}
    assert "truncated" in merged["lane_meta"]["skipped"][0]["reason"]


# -- the concurrency bound -------------------------------------------------


def test_the_pool_never_exceeds_max_lanes_for_six_members(log):
    names = [f"N{index}" for index in range(6)]
    _cast(log, *names)
    caller = LaneCaller(delays={name: 0.02 for name in names})
    fanout = _fanout(log, caller=caller, config={"max_lanes": 4})

    merged = asyncio.run(_drive(fanout, _state()))

    assert len(merged["npc_lanes"]) == 6  # the cap bounds CONCURRENCY, not the set
    assert merged["lane_meta"]["max_lanes"] == 4
    assert merged["lane_meta"]["batches"] == 2  # ceil(6/4)
    assert caller.max_in_flight <= 4


def test_the_pool_bound_holds_across_two_concurrent_turns(log):
    """The pool is MODULE-level: two turns cannot each open their own width."""
    from fabulist.graph import GraphTurnEngine

    other = "s2"

    def cast(session_id, names):
        for name in names:
            admit_member(
                log,
                session_id,
                name,
                provenance="card",
                admitted_by="test",
                reference_kind="identity",
            )
        log.append_event(
            session_id,
            "system",
            "bts",
            {
                "event": "world_state",
                "kind": "delta",
                "state": {},
                "venue": {name: {"venue": "bar", "area": "room"} for name in names},
                "off_screen": [],
                "threads": [],
                "tension": {},
            },
        )

    cast(SID, [f"A{index}" for index in range(4)])
    cast(other, [f"B{index}" for index in range(4)])
    caller = LaneCaller(delays={f"{side}{index}": 0.02 for side in "AB" for index in range(4)})

    async def two_turns():
        graphs = {}
        for session_id in (SID, other):
            graphs[session_id] = GraphTurnEngine(
                graph=build_graph(
                    model=NarratorModel(),
                    extractor=_extractor,
                    log=log,
                    npc_lanes=_fanout(log, caller=caller, config={"max_lanes": 4}),
                ),
                log=log,
                model=NarratorModel(),
            )

        async def drive(session_id):
            return [event async for event in graphs[session_id].run_turn(session_id, "I wait.")]

        await asyncio.gather(drive(SID), drive(other))

    asyncio.run(two_turns())

    assert len(caller.calls) == 8  # every member of both turns still ran
    assert caller.max_in_flight <= 4


# -- deterministic emission ------------------------------------------------


def test_joined_rows_follow_dispatcher_order_not_completion_order(log):
    """Marcus finishes LAST but is emitted FIRST (the panel reads this order)."""
    ids = _cast(log, "Marcus", "Ilse", "Zahra")
    caller = LaneCaller(delays={"Marcus": 0.05, "Ilse": 0.01, "Zahra": 0.0})
    fanout = _fanout(log, caller=caller)

    merged = asyncio.run(_drive(fanout, _state(), reverse_completion=True))

    rows = _lane_rows(log)
    assert [row["member_id"] for row in rows if row["event"] == "npc_lane"] == ids
    assert rows[-1]["event"] == "npc_lanes"  # the summary closes the rows
    assert rows[-1]["lanes"] == ids
    assert rows[-1]["ran"] == 3
    assert merged["lane_meta"]["emitted"] is True


def test_the_join_is_idempotent_within_a_turn(log):
    """The critic's ``fail`` edge re-enters ``narrate``; rows must not repeat."""
    _cast(log, "Marcus")
    fanout = _fanout(log, caller=LaneCaller())
    merged = asyncio.run(_drive(fanout, _state()))
    before = list(_lane_rows(log))

    assert fanout.join(merged) == {}
    assert _lane_rows(log) == before


# -- topology + contracts --------------------------------------------------


def test_canonical_topology_compiles_with_the_fanout_edge(log):
    graph = build_graph(
        model=NarratorModel(), extractor=_extractor, log=log, topology=CANONICAL_TOPOLOGY
    ).get_graph()

    assert "npc_lane" in NODE_NAMES
    conditional = {
        (edge.source, edge.target) for edge in graph.edges if edge.conditional
    }
    assert ("npc_lanes", "npc_lane") in conditional
    assert ("npc_lanes", "narrate") in conditional  # the zero-member route
    plain = {(edge.source, edge.target) for edge in graph.edges if not edge.conditional}
    assert ("npc_lane", "narrate") in plain
    assert validate_topology(CANONICAL_TOPOLOGY) == []


def test_a_topology_without_the_lane_node_still_builds(log):
    """A pre-W9.17 topology (no fan-out edge, no ``npc_lane``) keeps working."""
    topology = {
        "nodes": [
            {"id": "pre_turn"},
            {"id": "npc_lanes"},
            {"id": "narrate"},
            {"id": "done"},
        ],
        "edges": [
            {"from": "pre_turn", "to": "npc_lanes"},
            {"from": "npc_lanes", "to": "narrate"},
            {"from": "narrate", "to": "done"},
        ],
    }
    graph = build_graph(
        model=NarratorModel(), extractor=_extractor, log=log, topology=topology
    ).get_graph()

    # W9.17 upgrade: a pre-fanout shape keeps the cast by inserting npc_lane and
    # making `npc_lanes -> narrate` the zero-member route (see _upgrade_legacy_fanout).
    assert [node for node in graph.nodes if not node.startswith("__")] == [
        "pre_turn",
        "npc_lanes",
        "npc_lane",
        "narrate",
        "done",
    ]
    conditional = {(e.source, e.target) for e in graph.edges if e.conditional}
    assert ("npc_lanes", "npc_lane") in conditional
    assert ("npc_lanes", "narrate") in conditional
    assert validate_topology(topology) == []


def test_contracts_declare_the_lane_node_and_accept_the_fanout_edge():
    contract = NODE_CONTRACTS["npc_lane"]
    assert contract.klass == "model_pass"
    assert contract.requires == ("user", "facts")
    assert contract.provides == ("npc_lane",)
    # The canonical wiring is legal, unknown nodes are not.
    assert validate_topology(CANONICAL_TOPOLOGY) == []
    broken = {
        "nodes": [{"id": "pre_turn"}, {"id": "npc_lanes"}, {"id": "npc_lane"}, {"id": "wishful"}],
        "edges": [{"from": "pre_turn", "to": "npc_lanes"}],
    }
    assert "unknown_node" in {violation["code"] for violation in validate_topology(broken)}


def test_contracts_reject_a_duplicate_condition():
    topology = {
        "nodes": [
            {"id": "pre_turn"},
            {"id": "npc_lanes"},
            {"id": "npc_lane"},
            {"id": "narrate"},
            {"id": "done"},
        ],
        "edges": [
            {"from": "pre_turn", "to": "npc_lanes"},
            {"from": "npc_lanes", "to": "npc_lane", "fanout": True},
            {"from": "npc_lanes", "to": "narrate"},
            {"from": "npc_lane", "to": "narrate"},
            # two targets for one branch value: the builder cannot route it
            {"from": "narrate", "to": "done", "condition": "ok"},
            {"from": "narrate", "to": "npc_lanes", "condition": "ok"},
        ],
    }
    codes = [violation["code"] for violation in validate_topology(topology)]

    assert codes.count("duplicate_condition") == 1


def test_a_topology_with_a_dangling_fanout_edge_falls_back_to_canonical():
    """The API read keeps ``build_graph`` away from a fan-out it cannot wire."""
    from fabulist.prompts.preset_store import preset_from_json, preset_topology

    dangling = {
        "nodes": [
            {"id": "pre_turn"},
            {"id": "npc_lanes"},
            {"id": "narrate"},
            {"id": "done"},
        ],
        "edges": [
            {"from": "pre_turn", "to": "npc_lanes"},
            # the fan-out target is not a declared node
            {"from": "npc_lanes", "to": "npc_lane", "fanout": True},
            {"from": "npc_lanes", "to": "narrate"},
            {"from": "narrate", "to": "done"},
        ],
    }
    preset = preset_from_json({"preset_id": "p", "name": "P", "topology": dangling})

    assert "orphan_edge" in {v["code"] for v in validate_topology(dangling)}
    # ...so the engine reads the canonical wiring instead and still boots.
    assert "npc_lane" in [node["id"] for node in preset_topology(preset)["nodes"]]


def test_the_manifest_shows_the_fanout_and_the_per_member_node():
    from fabulist.flow.manifest import build_manifest

    nodes = {node["id"]: node for node in build_manifest()["graphs"][0]["nodes"]}

    assert nodes["npc_lanes"]["fanout"]["kind"] == "per_member"
    assert nodes["npc_lanes"]["fanout"]["nodes"] == ["npc_lane"]
    assert nodes["npc_lane"]["runs"]["kind"] == "model_pass"
    assert nodes["npc_lane"]["runs"]["interaction"] == "npc"
    edges = {
        (edge["from"], edge["to"]): edge for edge in build_manifest()["graphs"][0]["edges"]
    }
    assert edges[("npc_lanes", "npc_lane")]["kind"] == "fanout"
    assert ("npc_lane", "narrate") in edges


# -- parity ----------------------------------------------------------------


async def _run_turn(log, topology, lane_caller=None, model=None, build_messages=None):
    from fabulist.graph import GraphTurnEngine

    model = model or NarratorModel()
    build_messages = build_messages or (
        lambda state: [{"role": "system", "content": "N"}, {"role": "user", "content": state["user_content"]}]
    )
    graph = build_graph(
        model=model,
        extractor=_extractor,
        log=log,
        topology=topology,
        build_messages=build_messages,
        npc_lanes=(
            make_npc_lanes_node(
                log=log, build_messages=build_messages, caller=lane_caller
            )
            if lane_caller is not None
            else None
        ),
    )
    engine = GraphTurnEngine(graph=graph, log=log, model=model)
    return [event async for event in engine.run_turn(SID, "I wait.")]


def _event_shape(log):
    """The turn's log rows minus identity: what a parity gate compares."""
    return [(event.type, json.dumps(event.payload, sort_keys=True)) for event in log.get_events(SID)]


def _without_lanes(topology):
    """The canonical spine with the fan-out removed (the pre-W9.13 shape)."""
    return {
        "nodes": [
            node
            for node in topology["nodes"]
            if node["id"] not in ("npc_lanes", "npc_lane")
        ],
        "edges": [{"from": "pre_turn", "to": "narrate"}]
        + [
            edge
            for edge in topology["edges"]
            if edge.get("from") not in ("pre_turn", "npc_lanes", "npc_lane")
            and edge.get("to") not in ("npc_lanes", "npc_lane")
        ],
    }


def test_a_zero_cast_turn_is_byte_identical_to_a_turn_without_the_fanout(tmp_path):
    """The parity gate: no active cast ⇒ no lane rows, no prompt change."""
    with_lanes = EventLog(tmp_path / "with.db")
    without = EventLog(tmp_path / "without.db")
    try:
        model = NarratorModel()
        asyncio.run(_run_turn(with_lanes, CANONICAL_TOPOLOGY, lane_caller=LaneCaller(), model=model))
        asyncio.run(
            _run_turn(without, _without_lanes(CANONICAL_TOPOLOGY), model=NarratorModel())
        )

        # ...and the turns are real (not two empty logs).
        assert [row[0] for row in _event_shape(with_lanes)].count("narrative") == 1
        assert _event_shape(with_lanes) == _event_shape(without)
        assert not [row for row in _lane_rows(with_lanes)]
        # ...and the narrator saw the today-shaped prompt: no lane block.
        assert all(
            "intentions this turn" not in str(message.get("content"))
            for message in model.calls[0]
        )
    finally:
        with_lanes.close()
        without.close()


def test_a_second_turn_on_the_same_thread_does_not_inherit_the_lanes(tmp_path):
    """The W9.6 no-inheritance rule, now that the channels are REDUCED."""
    from fabulist.graph import GraphTurnEngine, open_sqlite_checkpointer
    from fabulist.wiring import make_narrator_message_builder

    async def scenario():
        log = EventLog(tmp_path / "events.db")
        try:
            _cast(log, "Marcus")
            model = NarratorModel()
            build = make_narrator_message_builder(log)
            async with open_sqlite_checkpointer(tmp_path / "ckpt.db") as checkpointer:
                graph = build_graph(
                    model=model,
                    extractor=_extractor,
                    log=log,
                    build_messages=build,
                    checkpointer=checkpointer,
                    npc_lanes=_fanout(log, build_messages=build, caller=LaneCaller()),
                )
                engine = GraphTurnEngine(graph=graph, log=log, model=model)
                await _collect(engine.run_turn(SID, "I wait."))
                # Marcus leaves the scene: the next turn has no active cast.
                log.append_event(
                    SID,
                    "system",
                    "bts",
                    {
                        "event": "world_state",
                        "kind": "delta",
                        "state": {},
                        "venue": {},
                        "off_screen": [{"name": "Marcus", "location": "street"}],
                        "threads": [],
                        "tension": {},
                    },
                )
                await _collect(engine.run_turn(SID, "I wait."))
                snapshot = await graph.aget_state({"configurable": {"thread_id": SID}})
                return snapshot.values, model.calls
        finally:
            log.close()

    values, calls = asyncio.run(scenario())

    # The second turn's narrator saw NO lane block: the empty input write reset
    # the reduced channel instead of merging last turn's lanes forward.
    assert values["npc_lanes"] == {}
    lane_prompts = [
        row
        for row in calls
        if any("intentions this turn" in str(message.get("content")) for message in row)
    ]
    assert len(lane_prompts) == 1


async def _collect(agen):
    return [event async for event in agen]


def test_a_live_fanout_turn_writes_rows_then_the_narrator_sees_the_lanes(tmp_path):
    """End to end through the compiled graph: Send → lane → join → narrate."""
    log = EventLog(tmp_path / "live.db")
    try:
        from fabulist.wiring import make_narrator_message_builder

        ids = _cast(log, "Marcus", "Ilse")
        model = NarratorModel()
        asyncio.run(
            _run_turn(
                log,
                CANONICAL_TOPOLOGY,
                # Completion order is the REVERSE of member order: the rows and
                # the narrator's block must still read Marcus first.
                lane_caller=LaneCaller(delays={"Marcus": 0.05, "Ilse": 0.0}),
                model=model,
                # The REAL narrator builder: it is what renders the lane block.
                build_messages=make_narrator_message_builder(log),
            )
        )

        rows = _lane_rows(log)
        assert [row["member_id"] for row in rows if row["event"] == "npc_lane"] == ids
        assert rows[-1]["event"] == "npc_lanes"
        assert rows[-1]["ran"] == 2
        # The narrator's own prompt carries the merged lanes as its own block
        # (W9.28: the SPEAKER PALETTE closes the tail after it), in DISPATCHER
        # member order (the state merge is deterministic too).
        block = str(
            next(
                message
                for message in model.calls[0]
                if "intentions this turn" in str(message.get("content"))
            )["content"]
        )
        assert "intentions this turn" in block
        # Issue #44: the envelope is the last ASSISTANT message, immediately
        # before the player's ask (which closes the served list).
        assert model.calls[0][-1]["role"] == "user"
        served = _payload(model.calls[0][-2]["content"])
        assert "<engine_lane>" in served and "intentions this turn" in served
        assert served.rstrip().endswith("</engine_palette>")
        assert "SPEAKER PALETTE" in served
        lane_body = served.split("<engine_lane>\n", 1)[1].split("</engine_lane>", 1)[0]
        speakers = [
            line.removeprefix("- ").split(" \u2014 ")[0]
            for line in lane_body.splitlines()
            if line.startswith("- ")
        ]
        assert speakers == ["Marcus", "Ilse"]
        # ...and the state the narrator saw had both lanes merged, not replaced.
        assert set(_lane_rows(log)[-1]["lanes"]) == set(ids)
    finally:
        log.close()


def test_legacy_topology_runs_the_fanout_instead_of_zero_lanes(log):
    """A pre-W9.17 stored topology (`npc_lanes -> narrate`) must still lane.

    The preset store holds whatever the owner last saved; every pre-W9.17 save
    is the old single-node pool. Compiling it verbatim silenced the whole cast,
    so build_graph upgrades the COMPILE: npc_lane is inserted, the plain edge
    becomes the zero-member route, and the fan-out edge drives the Sends.
    """
    legacy = {
        "nodes": [
            {"id": "pre_turn", "kind": "stage"},
            {"id": "npc_lanes", "kind": "stage", "data": {"max_lanes": 4}},
            {"id": "narrate", "kind": "stage"},
            {"id": "commit", "kind": "stage"},
            {"id": "validate", "kind": "gate"},
            {"id": "decision_gate", "kind": "gate"},
            {"id": "critic", "kind": "gate"},
            {"id": "planner", "kind": "stage"},
            {"id": "bts", "kind": "stage"},
            {"id": "env_hud", "kind": "stage"},
            {"id": "attribution", "kind": "stage"},
            {"id": "done", "kind": "terminal"},
        ],
        "edges": [
            {"from": "pre_turn", "to": "npc_lanes"},
            {"from": "npc_lanes", "to": "narrate"},
            {"from": "narrate", "to": "commit"},
            {"from": "commit", "to": "validate"},
            {"from": "validate", "to": "decision_gate"},
            {"from": "decision_gate", "to": "critic"},
            {"from": "critic", "to": "planner", "condition": "ok"},
            {"from": "critic", "to": "planner", "condition": "diffs"},
            {"from": "critic", "to": "narrate", "condition": "fail"},
            {"from": "planner", "to": "bts"},
            {"from": "bts", "to": "env_hud"},
            {"from": "env_hud", "to": "attribution"},
            {"from": "attribution", "to": "done"},
        ],
    }
    graph = build_graph(
        model=NarratorModel(), extractor=_extractor, log=log, topology=legacy
    ).get_graph()
    assert "npc_lane" in set(graph.nodes)
    conditional = {(e.source, e.target) for e in graph.edges if e.conditional}
    assert ("npc_lanes", "npc_lane") in conditional
    assert ("npc_lanes", "narrate") in conditional


def test_the_committed_frame_marks_the_barrier(tmp_path):
    """W9.19 (A): the commit barrier frame — after it the turn is KEPT.

    The frame must fire after the last prose delta (the user has the whole
    turn) and before the tail's stage frames (validate/critic/bts run behind
    the barrier), carrying the narrative row it committed.
    """
    log = EventLog(tmp_path / "barrier.db")
    try:
        events = asyncio.run(_run_turn(log, CANONICAL_TOPOLOGY))
        kinds = [e["type"] for e in events]
        assert "committed" in kinds
        barrier = kinds.index("committed")
        # every content delta precedes the barrier; done trails the tail
        last_content = max(i for i, k in enumerate(kinds) if k == "content")
        assert last_content < barrier < kinds.index("done")
        seq = events[barrier]["meta"]["narrative_seq"]
        assert seq == _event_shape_committed_seq(log)
    finally:
        log.close()


def _event_shape_committed_seq(log):
    rows = [e for e in log.get_events(SID) if e.type == "narrative"]
    return rows[-1].seq if rows else None


def test_turn_1_of_a_fresh_card_chat_lanes_the_seed_cast(tmp_path):
    """W9.21 live regression: the DISPATCHER mints the lazy roster first.

    A fresh chat carries card-shaped events and NOTHING else -- no
    ``cast_admit`` exists until :func:`ensure_roster` runs, and its first call
    otherwise lands POST-narrate (attribution/BTS own an ``ensure_roster``).
    The bare ``roster`` fold was therefore empty at dispatch time and turn 1 of
    a live world chat laned ``"no active cast"`` even with
    the seed floor in place: the unit tests had hand-called ``ensure_roster``,
    the graph never did. The dispatcher must mint before the ladder runs.
    """
    from fabulist.graph import GraphTurnEngine, open_sqlite_checkpointer
    from fabulist.wiring import make_narrator_message_builder

    async def scenario():
        log = EventLog(tmp_path / "events.db")
        try:
            # The live world-chat shape: card events, no admits, no
            # world_state, no attribution. Nothing calls ensure_roster first.
            for name in ("Marcus", "Ilse"):
                log.append_event(
                    SID, "system", "system", {"card": {"name": name, "description": "a seed"}}
                )
            model = NarratorModel()
            build = make_narrator_message_builder(log)
            async with open_sqlite_checkpointer(tmp_path / "ckpt.db") as checkpointer:
                graph = build_graph(
                    model=model,
                    extractor=_extractor,
                    log=log,
                    build_messages=build,
                    checkpointer=checkpointer,
                    npc_lanes=_fanout(log, build_messages=build, caller=LaneCaller()),
                )
                engine = GraphTurnEngine(graph=graph, log=log, model=model)
                await _collect(engine.run_turn(SID, "I wait."))
            rows = _lane_rows(log)
            assert rows, "turn 1 of a fresh card chat must lane the seed cast"
            summary = [r for r in rows if r.get("event") == "npc_lanes"]
            assert summary, "the fan-in must write the npc_lanes summary row"
            assert summary[0].get("source") == "seed"
            laned = {r.get("name") for r in rows if r.get("event") == "npc_lane"}
            assert laned == {"Marcus", "Ilse"}
        finally:
            log.close()

    asyncio.run(scenario())


def test_raw_html_in_prose_passes_through_untouched_and_flags():
    """D80: markup in prose is DETECTED, never stripped (owner ruling).

    The W9.22-era strip (ca17f02) restricted a model that confidently emits
    HTML — the owner rejected that: the fix for markup-in-prose is TEACHING
    (the library doc names Custom(html=, css=)/Interface as its home), not
    output policing. The prose must therefore survive BYTE-IDENTICAL apart from
    the whitespace collapse, while the leak list feeds the ``prose_markup``
    drift flag (a transcript marker, item 14) and the critic path (D73).
    """
    from fabulist.graph.nodes import _segment_unfenced

    text = (
        'The shop goes quiet.\n'
        '<details><summary>STATUS</summary>\n'
        'Cash: $4,250 | Inventory: 47 items\n'
        '</details>\n'
        '<div style="font-family: \'Courier New\'; color: #00ff41;">\n'
        '  <div style="font-weight: bold;">TERMINAL</div>\n'
        '</div>\n'
        '<style>.x { color: red; }</style>\n'
        'Hobbs watches you.'
    )
    leaks: list[str] = []
    prose = " ".join(
        seg["text"] for seg in _segment_unfenced(text, leaks) if seg["kind"] == "prose"
    )
    # Detection feeds the warn flag (rule_id prose_markup) — never silent.
    assert leaks, "markup drift must be reported, not silent"
    assert leaks[0].startswith("<")
    # D80: the markup SURVIVES — the model's confident output is not edited.
    assert "<details>" in prose and "</details>" in prose
    assert "font-family" in prose  # attribute text intact
    assert ".x { color: red; }" in prose  # style body intact
    assert "Cash: $4,250" in prose
    assert "Hobbs watches you." in prose
