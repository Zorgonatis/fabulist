"""The turn topology (W9.6): canonical graph, custom topologies, critic routing.

``CANONICAL_TOPOLOGY`` is authored as data and ``build_graph`` builds from it, so
these tests pin the *shape* of the turn — the stage nodes and their edges. Since
W9.24 §3.3.7 the critic fans out to ``ok``/``diffs`` only: the ``fail -> narrate``
regeneration edge is retired, ``critic_route`` never returns ``fail``, and a
stored topology that still carries the dead edge must BOOT (tolerated, not
stripped — no node is orphaned). The routing rule is asserted directly and then
exercised through the real compiled graph, so the fan-out is proven live.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.graph import CANONICAL_TOPOLOGY, NODE_NAMES, build_graph
from fabulist.graph.nodes import critic_route
from fabulist.graph.types import initial_turn_state
from fabulist.log import EventLog
from fabulist.prompts import preset_store


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    return [event async for event in agen]


# -- fakes -------------------------------------------------------------------


class ChannelModel:
    """A ``ModelCaller`` yielding one canned content delta per pass."""

    def __init__(self, text: str = "The rain keeps falling."):
        self.text = text
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        yield {"type": "content", "delta": self.text}


class ScriptedCritic:
    """A critic seam replaying a canned verdict per call, recording evidence."""

    def __init__(self, verdicts: list[str]):
        self.verdicts = list(verdicts)
        self.evidence: list[dict] = []

    async def __call__(self, evidence):
        self.evidence.append(evidence)
        verdict = self.verdicts[min(len(self.evidence), len(self.verdicts)) - 1]
        return verdict


def _verdict(verdict: str, **extra) -> str:
    payload = {
        "verdict": verdict,
        "reason": extra.pop("reason", "because"),
        "edits": extra.pop("edits", []),
    }
    payload.update(extra)
    return json.dumps(payload)


def _extractor(user_msg, prose):
    return {"deltas": {}, "action_class": "dialogue"}


def _graph(log, **kwargs):
    return build_graph(model=kwargs.pop("model", ChannelModel()), extractor=_extractor, log=log, **kwargs)


def _stage_frames(events):
    return [e for e in events if e["type"] == "stage"]


# -- the canonical topology, as data ----------------------------------------


def test_canonical_topology_is_the_expected_node_and_edge_set():
    """The authored graph: linear spine + the critic's verdict fan-out."""
    assert [node["id"] for node in CANONICAL_TOPOLOGY["nodes"]] == [
        "pre_turn",
        # W9.20: the planner is the plan junction's ``plan`` arm — a steering
        # plane that runs BEFORE the lanes and the narration, never per turn.
        "planner",
        # W9.13/W9.17: the NPC fan-out sits between the pre-turn bucket and the
        # narrator as a REAL ``Send`` fan-out: ``npc_lanes`` dispatches, one
        # ``npc_lane`` node runs per ACTIVE member.
        "npc_lanes",
        "npc_lane",
        "narrate",
        "commit",
        "validate",
        "decision_gate",
        "critic",
        "bts",
        "env_hud",
        "attribution",
        # W9.23 §3.6: the tail closes on the terminal again — the W9.22 proposal
        # stage left the turn (the historian runs those lanes between turns).
        "done",
    ]
    # ``regenerate`` is NOT a node (R23) — the fail edge goes back to ``narrate``.
    assert "regenerate" not in [node["id"] for node in CANONICAL_TOPOLOGY["nodes"]]

    edges = [
        (edge["from"], edge["to"], edge.get("condition"))
        for edge in CANONICAL_TOPOLOGY["edges"]
    ]
    assert edges == [
        # W9.20: the plan junction — the router's return values ARE the
        # conditions, and the planner rejoins the spine at the lanes.
        ("pre_turn", "planner", "plan"),
        ("pre_turn", "npc_lanes", "skip"),
        ("planner", "npc_lanes", None),
        # The fan-out edge, then its zero-member route (the plain edge the
        # builder turns into the fan-out's empty branch), then the join.
        ("npc_lanes", "npc_lane", None),
        ("npc_lanes", "narrate", None),
        ("npc_lane", "narrate", None),
        ("narrate", "commit", None),
        ("commit", "validate", None),
        ("validate", "decision_gate", None),
        ("decision_gate", "critic", "critic"),
        # W9.19's clean route keeps its router VALUE ("planner") and lands on the
        # world tracker now that the planner is pre-narration; the critic's keep
        # verdicts land there too.
        ("decision_gate", "bts", "planner"),
        ("critic", "bts", "ok"),
        ("critic", "bts", "diffs"),
        ("bts", "env_hud", None),
        ("env_hud", "attribution", None),
        # W9.23 §3.6: no proposal stage in the turn; the tail ends here.
        ("attribution", "done", None),
    ]
    # W9.17: exactly ONE fan-out edge is left in the turn — the NPC lanes. The
    # proposal fan-out object still exists; it is the historian's now.
    fanout = [
        (edge["from"], edge["to"])
        for edge in CANONICAL_TOPOLOGY["edges"]
        if edge.get("fanout")
    ]
    assert fanout == [("npc_lanes", "npc_lane")]


def test_node_names_follow_the_canonical_topology():
    assert NODE_NAMES == tuple(node["id"] for node in CANONICAL_TOPOLOGY["nodes"])


def test_initial_state_carries_the_turn_transaction_defaults():
    state = initial_turn_state("s1", "hello", "in_character")
    for key, default in (
        ("narrative_event", {}),
        ("critic_verdict", {}),
        ("critic_flags", []),
        ("gate_flags", []),
        ("bts_result", {}),
        ("world_state_event", {}),
        ("attribution_rows", []),
        ("validation", {}),
    ):
        assert state[key] == default, key


# -- the compiled graph ------------------------------------------------------


def test_compiled_graph_has_the_canonical_nodes_and_edges(log):
    drawable = _graph(log).get_graph()
    nodes = [node for node in drawable.nodes if not node.startswith("__")]
    assert nodes == list(NODE_NAMES)

    plain = {
        (edge.source, edge.target) for edge in drawable.edges if not edge.conditional
    }
    assert (("__start__", "pre_turn")) in plain
    assert ("done", "__end__") in plain
    for source, target in zip(NODE_NAMES, NODE_NAMES[1:]):
        # the linear spine, minus the conditional sources (the critic's verdict
        # branch, the W9.19 gate, the two Send fan-outs and the W9.20 plan
        # junction on pre_turn)
        if source in (
            "critic",
            "npc_lanes",
            "decision_gate",
            "pre_turn",
        ):
            continue
        assert (source, target) in plain
    # W9.24 §3.3.7: the critic's fail edge is GONE from the canonical graph.
    conditional = {
        (edge.source, edge.target, edge.data)
        for edge in drawable.edges
        if edge.conditional
    }
    assert ("critic", "narrate", "fail") not in conditional
    assert ("critic", "bts", "diffs") in conditional
    # W9.19 (B): the critic is conditional — a clean turn routes past it.
    # (The drawable's `data` label is langgraph-internal and renders None for
    # this router; membership is what the editor and the run both consume.)
    routed = {(e.source, e.target) for e in drawable.edges if e.conditional}
    assert ("decision_gate", "critic") in routed
    assert ("decision_gate", "bts") in routed
    # W9.20: the plan junction — both arms are conditional edges out of pre_turn.
    assert ("pre_turn", "planner") in routed
    assert ("pre_turn", "npc_lanes") in routed
    # W9.17: the Send fan-out is a conditional edge with two targets — the
    # per-member node and the zero-member route to the narrator.
    assert ("npc_lanes", "npc_lane", None) in conditional
    assert ("npc_lanes", "narrate", None) in conditional
    # the per-member node still merges straight into the narrator
    assert ("npc_lane", "narrate") in plain
    # W9.23 §3.6: the proposal stage is not in the compiled turn at all.
    assert ("attribution", "done") in plain
    assert "asset_proposals" not in nodes
    assert "asset_proposal_lane" not in nodes


def test_custom_topology_builds(tmp_path):
    """A preset topology can drop stages; the builder follows the data."""
    topology = {
        "nodes": [
            {"id": "pre_turn", "kind": "stage"},
            {"id": "narrate", "kind": "stage"},
            {"id": "done", "kind": "terminal"},
        ],
        "edges": [
            {"from": "pre_turn", "to": "narrate"},
            {"from": "narrate", "to": "done"},
        ],
    }
    log = EventLog(tmp_path / "custom.db")
    try:
        drawable = build_graph(
            model=ChannelModel(), extractor=_extractor, log=log, topology=topology
        ).get_graph()
        nodes = [node for node in drawable.nodes if not node.startswith("__")]
        assert nodes == ["pre_turn", "narrate", "done"]
    finally:
        log.close()


def test_unknown_node_id_raises(log):
    topology = {
        "nodes": [{"id": "pre_turn"}, {"id": "wishful_thinking"}],
        "edges": [{"from": "pre_turn", "to": "wishful_thinking"}],
    }
    with pytest.raises(ValueError, match="unknown node id: 'wishful_thinking'"):
        build_graph(model=ChannelModel(), extractor=_extractor, log=log, topology=topology)


def test_unknown_edge_target_raises(log):
    topology = {
        "nodes": [{"id": "pre_turn"}],
        "edges": [{"from": "pre_turn", "to": "nowhere"}],
    }
    with pytest.raises(ValueError, match="unknown node id: 'nowhere'"):
        build_graph(model=ChannelModel(), extractor=_extractor, log=log, topology=topology)


# -- the critic's routing rule ----------------------------------------------


def test_critic_route_ok_and_diffs_keep_the_turn():
    assert critic_route({"critic_verdict": {"verdict": "ok"}}) == "ok"
    assert critic_route({"critic_verdict": {"verdict": "diffs"}}) == "diffs"
    # No verdict at all (a critic that raised or returned garbage) keeps it too.
    assert critic_route({"critic_verdict": {}}) == "ok"


def test_critic_route_maps_a_legacy_fail_to_the_keep_route():
    """W9.24 §3.3.7: ``fail`` never routes anywhere but the keep route.

    The regression this pins: a parser (or a stored state) still reporting
    ``fail`` must not resurrect the retired regeneration — there is no such edge
    and no such stage any more.
    """
    assert critic_route({"critic_verdict": {"verdict": "fail"}}) == "ok"
    # Even with the retired retry keys present in state (an old checkpoint).
    assert (
        critic_route(
            {
                "critic_verdict": {"verdict": "fail"},
                "regen_attempt": 1,
                "regen_feedback": "advance it",
            }
        )
        == "ok"
    )


def test_stored_topology_carrying_the_dead_fail_edge_still_boots(tmp_path):
    """A preset saved before W9.24 must BOOT (tolerated, never stripped).

    The edge is unreachable — ``critic_route`` never returns ``fail`` — so the
    graph compiles, runs and commits exactly one narrative, and no node id is
    orphaned (nothing here needs the W9.22-style strip pass).
    """
    legacy = {
        "nodes": [dict(node) for node in CANONICAL_TOPOLOGY["nodes"]],
        "edges": [
            dict(edge)
            for edge in CANONICAL_TOPOLOGY["edges"]
        ]
        + [{"from": "critic", "to": "narrate", "kind": "branch", "condition": "fail"}],
    }
    log = EventLog(tmp_path / "legacy.db")
    try:
        graph = build_graph(
            model=ChannelModel(),
            extractor=_extractor,
            log=log,
            topology=legacy,
            critic=ScriptedCritic([_verdict("fail", reason="stalls")]),
            decision=FlaggingGate(),
        )
        events = run(collect(_graph_events(graph, log)))
        narratives = [e for e in log.get_events("s1") if e.type == "narrative"]
        assert len(narratives) == 1
        assert not [e for e in log.get_events("s1") if e.type == "revision"]
        assert events[-1]["type"] == "done"
    finally:
        log.close()


# -- the fan-out, live through the graph ------------------------------------


async def _graph_events(graph, log):
    from fabulist.graph import GraphTurnEngine

    engine = GraphTurnEngine(graph=graph, log=log, model=ChannelModel())
    async for event in engine.run_turn("s1", "I wait.", "in_character"):
        yield event


# -- preset-carried topology (seed-only-if-missing) -------------------------


def test_default_preset_seeds_the_canonical_topology(tmp_path):
    log = EventLog(tmp_path / "seed.db")
    try:
        preset_store.ensure_default_preset(log)
        row = preset_store.default_preset(log)
        assert preset_store.preset_topology(row) == preset_store.canonical_topology()
        # ...and it round-trips through the export shape.
        assert preset_store.preset_from_json(
            preset_store.preset_to_json(row)
        ).topology == row.topology
    finally:
        log.close()


def test_preset_topology_falls_back_to_the_canonical_one():
    """A preset written before W9.6 inherits the code topology (no rewrite)."""
    legacy = preset_store.preset_from_json({"preset_id": "old", "name": "Old"})
    assert legacy.topology == {}
    assert "topology" not in preset_store.preset_to_json(legacy)
    assert preset_store.preset_topology(legacy) == preset_store.canonical_topology()
    assert preset_store.preset_topology(None) == preset_store.canonical_topology()


def test_preset_topology_reads_a_custom_one():
    custom = {
        "nodes": [{"id": "pre_turn"}, {"id": "narrate"}, {"id": "done"}],
        "edges": [{"from": "pre_turn", "to": "narrate"}, {"from": "narrate", "to": "done"}],
    }
    preset = preset_store.preset_from_json(
        {"preset_id": "custom", "name": "Custom", "topology": custom}
    )
    assert preset_store.preset_topology(preset) == custom
    assert preset_store.preset_topology(preset_store.preset_to_json(preset)) == custom


def test_ensure_default_preset_never_overwrites_an_existing_row(tmp_path):
    """Seed-only-if-missing: a persisted default keeps its own topology."""
    log = EventLog(tmp_path / "keep.db")
    try:
        preset_store.ensure_default_preset(log)
        row = preset_store.default_preset(log)
        custom = {"nodes": [{"id": "pre_turn"}, {"id": "done"}], "edges": []}
        preset_store.append_preset(
            log, {**preset_store.preset_to_json(row), "topology": custom}
        )
        preset_store.ensure_default_preset(log)
        assert preset_store.default_preset(log).topology == custom
    finally:
        log.close()


class FlaggingGate:
    """Decision seam that always fires the bank's first question (W9.19 B).

    The critic is conditional now: it runs only when the validator or the
    decision gate produced a flag. Tests that exercise the critic path need a
    real flag from a real seam, not a state hack.
    """

    enabled = True

    def __init__(self):
        self.ask_calls: list[tuple[dict, list[dict]]] = []

    async def ask(self, state, questions):
        self.ask_calls.append((state, questions))
        # ``questions`` is the payload map {id: row}; the wire shape back is
        # {question id: {"noul": P(true)}}. Answer EVERY question in its own
        # defect polarity so at least one flag fires.
        # The payload map's KEY is the question id (the rows carry only
        # type/instructions); answer every noul row with P(true)=1.0, which
        # fires every true_is_defect question in the bank.
        items = questions.items() if isinstance(questions, dict) else ()
        return {qid: {"noul": 1.0} for qid, row in items
                if isinstance(row, dict) and row.get("type") == "noul"}


def test_a_clean_turn_skips_the_critic_and_a_flagged_turn_runs_it(tmp_path):
    """W9.19 (B): the critic runs only when a flag demands it.

    A clean turn reaches the planner with a ``critic_skipped`` row in the log
    (the trace must explain the gap, not hide it); a flagged turn runs the
    critic and records its verdict.
    """
    log = EventLog(tmp_path / "conditional.db")
    try:
        # clean: no validator flags, a gate that answers clean
        class CleanGate:
            enabled = True

            async def ask(self, state, questions):
                return {}

        critic = ScriptedCritic([_verdict("ok")])
        graph = _graph(log, critic=critic, decision=CleanGate())
        events = run(collect(_graph_events(graph, log)))
        assert len(critic.evidence) == 0  # never called
        skips = [
            e
            for e in log.get_events("s1")
            if e.payload.get("event") == "critic_skipped"
        ]
        assert len(skips) == 1
        # the tail still ran to completion (no critic verdict row anywhere)
        assert not any(
            e.payload.get("event") == "critic_verdict" for e in log.get_events("s1")
        )
        assert events[-1]["type"] == "done"
    finally:
        log.close()

    log = EventLog(tmp_path / "conditional2.db")
    try:
        critic = ScriptedCritic([_verdict("ok")])
        graph = _graph(log, critic=critic, decision=FlaggingGate())
        events = run(collect(_graph_events(graph, log)))
        assert len(critic.evidence) == 1  # flagged -> judged
        assert not any(
            e.payload.get("event") == "critic_skipped" for e in log.get_events("s1")
        )
    finally:
        log.close()


# -- W9.23 §3.6: the proposal stage left the turn ----------------------------


class ProposalFanout:
    """A stand-in for the proposal lane system: one ``Send`` per data type.

    The turn no longer wires it (the historian drives the real one), so this
    object's only job here is to prove the builder IGNORES a supplied fan-out for
    a stage the table no longer declares.
    """

    fanout_node = "asset_proposal_lane"
    buckets = ("world", "actors", "books")

    def __init__(self):
        self.dispatched = 0
        self.lanes: list[dict] = []
        self.joins = 0

    async def __call__(self, state):
        self.dispatched += 1
        return {}

    async def route(self, state):
        from langgraph.types import Send

        return [
            Send("asset_proposal_lane", {"bucket": bucket}) for bucket in self.buckets
        ]

    async def lane(self, payload):
        self.lanes.append(dict(payload))
        return {}

    def join(self, state):
        self.joins += 1
        return {}


class LaneModel:
    """One canned lane reply per call (the lane's JSON contract)."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls += 1
        yield {
            "type": "content",
            "delta": json.dumps(
                {
                    "intent": "shut the door",
                    "narrative": "Marcus moves to the door.",
                    "lines": [{"text": "Not yet."}],
                }
            ),
        }


def _w9_22_topology() -> dict:
    """The canonical turn as an owner SAVED it while the W9.22 stage existed.

    Exactly the shape the removed inserter produced: the two proposal ids
    inserted in front of the terminal and the ``attribution -> done`` edge
    spliced into the four-edge fan-out.
    """
    nodes = [
        {
            "id": node["id"],
            "kind": node["kind"],
            **({"data": dict(node["data"])} if node.get("data") else {}),
        }
        for node in CANONICAL_TOPOLOGY["nodes"]
    ]
    edges = [dict(edge) for edge in CANONICAL_TOPOLOGY["edges"]]
    at = next(index for index, node in enumerate(nodes) if node["id"] == "done")
    nodes[at:at] = [
        {"id": "asset_proposals", "kind": "stage", "data": {"max_lanes": 3}},
        {"id": "asset_proposal_lane", "kind": "stage"},
    ]
    spliced: list[dict] = []
    for edge in edges:
        if edge["from"] == "attribution" and edge["to"] == "done":
            spliced.append({"from": "attribution", "to": "asset_proposals"})
            spliced.append(
                {"from": "asset_proposals", "to": "asset_proposal_lane", "fanout": True}
            )
            spliced.append({"from": "asset_proposals", "to": "done"})
            spliced.append({"from": "asset_proposal_lane", "to": "done"})
            continue
        spliced.append(edge)
    return {"nodes": nodes, "edges": spliced}


def test_every_canonical_fanout_edge_has_a_table_row():
    """The one place a ``Send`` fan-out is declared (W9.22 §3)."""
    from fabulist.graph.graph import FANOUT_STAGES

    table = {(stage.dispatcher, stage.lane) for stage in FANOUT_STAGES}
    edges = {
        (edge["from"], edge["to"])
        for edge in CANONICAL_TOPOLOGY["edges"]
        if edge.get("fanout")
    }
    assert edges == table == {("npc_lanes", "npc_lane")}


def test_the_npc_fanout_runs_and_a_supplied_proposal_fanout_is_ignored(tmp_path):
    """The table is the turn's fan-out set; a retired stage cannot re-enter it.

    Before W9.23 the proposal stage was a second table row and the wiring handed
    it a real object. The row is gone, so the SAME ``fanouts=`` key now resolves
    to nothing: the turn dispatches no proposal lane and writes no proposal row,
    while the NPC fan-out keeps running through the same machinery.
    """
    from fabulist.graph.nodes import make_npc_lanes_node
    from fabulist.state.cast import admit_member

    log = EventLog(tmp_path / "fanouts.db")
    try:
        admit_member(
            log,
            "s1",
            "Marcus",
            provenance="card",
            admitted_by="test",
            reference_kind="identity",
        )
        log.append_event(
            "s1",
            "system",
            "bts",
            {
                "event": "world_state",
                "kind": "delta",
                "state": {},
                "venue": {"Marcus": {"venue": "bar", "area": "room"}},
                "off_screen": [],
                "threads": [],
                "tension": {},
            },
        )
        proposals = ProposalFanout()
        npc = make_npc_lanes_node(
            log=log,
            build_messages=lambda state: [{"role": "system", "content": "SHARED"}],
            caller=LaneModel(),
        )
        graph = build_graph(
            model=ChannelModel(),
            extractor=_extractor,
            log=log,
            npc_lanes=npc,
            fanouts={"asset_proposals": proposals},
        )
        run(collect(_graph_events(graph, log)))

        # the NPC fan-out: one real lane for the admitted member
        lane_rows = [
            event.payload
            for event in log.get_events("s1")
            if event.payload.get("event") == "npc_lane"
        ]
        assert [row["name"] for row in lane_rows] == ["Marcus"]
        # the retired proposal stage: never dispatched, never joined
        assert proposals.dispatched == 0
        assert proposals.lanes == []
        assert proposals.joins == 0
        assert not [
            event.payload
            for event in log.get_events("s1")
            if event.payload.get("event") == "asset_proposal"
        ]
    finally:
        log.close()


def test_the_turn_itself_proposes_nothing_and_spends_no_lane_call(tmp_path):
    """W9.23 §3.6: the proposal calls are the historian's, never the turn's."""
    log = EventLog(tmp_path / "retired.db")
    try:
        model = ChannelModel()
        graph = build_graph(model=model, extractor=_extractor, log=log)
        events = run(collect(_graph_events(graph, log)))

        assert events[-1]["type"] == "done"
        assert not [
            event.payload
            for event in log.get_events("s1")
            if event.payload.get("event") in ("asset_proposal", "asset_proposals")
        ]
        assert not [
            event.payload
            for event in log.get_events("s1")
            if event.payload.get("event") == "historian_run"
        ]
        assert len(model.calls) == 1  # the narrator only
    finally:
        log.close()


def test_the_retired_stage_is_no_longer_a_node_contract():
    """The coverage pin (W9.23 §3.6.4): ``NODE_CONTRACTS`` shrinks with the ids."""
    from fabulist.graph.contracts import NODE_CONTRACTS

    assert "asset_proposals" not in NODE_CONTRACTS
    assert "asset_proposal_lane" not in NODE_CONTRACTS
    assert "asset_proposals" not in NODE_NAMES
    assert "asset_proposal_lane" not in NODE_NAMES
    assert set(NODE_CONTRACTS) == set(NODE_NAMES)


# -- W9.23 §3.6.4: stored-topology STRIP (D71/D74) --------------------------


def test_a_stored_topology_carrying_the_w9_22_stage_is_stripped_at_compile(tmp_path):
    """An old save compiles to the stripped tail; the stored record is untouched."""
    log = EventLog(tmp_path / "strip.db")
    try:
        stored = _w9_22_topology()
        drawable = build_graph(
            model=ChannelModel(), extractor=_extractor, log=log, topology=stored
        ).get_graph()

        nodes = [node for node in drawable.nodes if not node.startswith("__")]
        assert nodes == list(NODE_NAMES)
        plain = {
            (edge.source, edge.target) for edge in drawable.edges if not edge.conditional
        }
        assert ("attribution", "done") in plain
        # The COMPILE was rewritten; the editor still reads what the owner saved.
        assert "asset_proposals" in [row["id"] for row in stored["nodes"]]
        assert {"from": "attribution", "to": "asset_proposals"} in stored["edges"]
    finally:
        log.close()


def test_the_stripped_topology_is_legal_and_stable():
    """The strip yields a VALID topology, and a second pass changes nothing."""
    from fabulist.graph.contracts import validate_topology
    from fabulist.graph.graph import strip_legacy_topology

    stored = _w9_22_topology()
    stripped = strip_legacy_topology(stored)

    assert validate_topology(stripped) == []
    assert [row["id"] for row in stripped["nodes"]] == list(NODE_NAMES)
    assert {"from": "attribution", "to": "done"} in stripped["edges"]
    assert strip_legacy_topology(stripped) is stripped  # idempotent, no rewrite


def test_the_strip_keeps_the_owners_other_custom_edits(tmp_path):
    """The whole point of stripping instead of falling back (W9.23 §3.6.4).

    The save carries a custom ``npc_lanes`` lane cap; a wholesale canonical
    fallback would silently put the code default back. The strip removes ONLY the
    retired stage, so every other edit survives — and the result still validates.
    """
    from fabulist.graph.contracts import validate_topology
    from fabulist.prompts.preset_store import preset_topology

    stored = _w9_22_topology()
    for row in stored["nodes"]:
        if row["id"] == "npc_lanes":
            row["data"] = {"max_lanes": 2}
    compiled = preset_topology({"topology": stored})

    ids = [row["id"] for row in compiled["nodes"]]
    assert "asset_proposals" not in ids
    assert ids == [node["id"] for node in CANONICAL_TOPOLOGY["nodes"]]
    assert next(row for row in compiled["nodes"] if row["id"] == "npc_lanes")[
        "data"
    ] == {"max_lanes": 2}
    assert validate_topology(compiled) == []


def test_preset_topology_falls_back_to_canonical_when_the_strip_refuses():
    """A hand-edited (non-inserter) shape is refused, and the engine boots canonical."""
    from fabulist.prompts.preset_store import preset_topology

    stored = _w9_22_topology()
    # Move the stage mid-graph: legal pre-R3, but not the shape the inserter wrote.
    stored["nodes"] = [
        {"id": "pre_turn"},
        {"id": "asset_proposals"},
        {"id": "asset_proposal_lane"},
        *[row for row in stored["nodes"] if row["id"] not in ("pre_turn", "done")],
        {"id": "done"},
    ]
    compiled = preset_topology({"topology": stored})
    ids = [row["id"] for row in compiled["nodes"]]
    assert "asset_proposals" not in ids
    assert ids == list(NODE_NAMES)


def test_a_proposal_dispatcher_without_its_fanout_edge_is_refused(log):
    """Never silently zero lanes: a plainly-wired dispatcher would plan nothing.

    The builder refuses the compile (``ValueError``), which is what turns into
    the engine's canonical fallback — a stored save the strip cannot read is
    never booted as if it were the canonical turn.
    """
    stored = _w9_22_topology()
    topology = {
        "nodes": stored["nodes"],
        "edges": [
            edge
            for edge in stored["edges"]
            if not (edge["from"] == "asset_proposals" and edge.get("fanout"))
        ],
    }
    with pytest.raises(ValueError, match="without its fan-out edge"):
        build_graph(
            model=ChannelModel(), extractor=_extractor, log=log, topology=topology
        )
    # ...and the canonical topology (the fallback target) still compiles.
    build_graph(model=ChannelModel(), extractor=_extractor, log=log)


def test_a_half_declared_proposal_stage_is_refused(log):
    """A dispatcher without its lane node is refused, never half-run."""
    stored = _w9_22_topology()
    topology = {
        "nodes": [
            row for row in stored["nodes"] if row["id"] != "asset_proposal_lane"
        ],
        "edges": [edge for edge in stored["edges"] if edge["to"] != "asset_proposal_lane"],
    }
    with pytest.raises(ValueError, match="half-declared"):
        build_graph(
            model=ChannelModel(), extractor=_extractor, log=log, topology=topology
        )


def test_a_legal_but_non_canonical_stage_shape_is_refused(log):
    """Both ids + the fan-out edge, placed mid-graph: savable pre-R3, REFUSED now.

    The strip is the W9.22 inserter's exact inverse. A hand-placed stage is a
    shape the inserter could not have produced, so the strip will not guess —
    compile refuses and the engine falls back to the canonical topology.
    """
    topology = {
        "nodes": [
            {"id": "pre_turn"},
            {"id": "narrate"},
            {"id": "commit"},
            {"id": "validate"},
            {"id": "asset_proposals"},
            {"id": "asset_proposal_lane"},
            {"id": "bts"},
            {"id": "done"},
        ],
        "edges": [
            {"from": "pre_turn", "to": "narrate"},
            {"from": "narrate", "to": "commit"},
            {"from": "commit", "to": "validate"},
            {"from": "validate", "to": "asset_proposals"},
            {"from": "asset_proposals", "to": "asset_proposal_lane", "fanout": True},
            {"from": "asset_proposals", "to": "bts"},
            {"from": "asset_proposal_lane", "to": "bts"},
            {"from": "bts", "to": "done"},
        ],
    }
    with pytest.raises(ValueError, match="cannot strip the retired"):
        build_graph(
            model=ChannelModel(), extractor=_extractor, log=log, topology=topology
        )


def test_a_from_scratch_topology_is_left_alone(log):
    """No proposal ids: the strip is a no-op and the graph builds as authored."""
    topology = {
        "nodes": [
            {"id": "pre_turn", "kind": "stage"},
            {"id": "narrate", "kind": "stage"},
            {"id": "done", "kind": "terminal"},
        ],
        "edges": [
            {"from": "pre_turn", "to": "narrate"},
            {"from": "narrate", "to": "done"},
        ],
    }
    drawable = build_graph(
        model=ChannelModel(), extractor=_extractor, log=log, topology=topology
    ).get_graph()
    nodes = [node for node in drawable.nodes if not node.startswith("__")]
    assert nodes == ["pre_turn", "narrate", "done"]
