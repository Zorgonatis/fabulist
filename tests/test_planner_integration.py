"""W9.20 B1: the planner wired into the engine (junction, upgrades, seams).

Wave A landed the plan object, the planner node and the junction router; this
wave puts them on the graph. The tests here pin the integration edges that no
unit test can reach:

* the THREE compile-upgrades composed on one pre-W9.17 stored topology
  (Send fan-out + conditional critic + the planner REPOSITION), idempotent and
  leaving the stored record untouched (§15.1);
* the default preset's stored (pre-W9.20 canonical) topology compiling to the
  new junction (§15.2);
* ``plan_due_route``'s return values being exactly the junction's path-map keys,
  invoked through the COMPILED graph's conditional edge on a due and a skip
  turn (§15.3b — catches a router returning a non-edge value before live);
* a refused planner upgrade raising at the graph layer and the wiring's
  ``_compile`` falling back to the canonical turn (§15.3c);
* the wiring seams the planner node is built with (§6): the narrator-class
  caller, the planner step's directive, the live preset config;
* the A2 seam: ``set_default_planner`` and the factory's payload normalizer
  keeping the owner's planner block through a default commit/reset.
"""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import fabulist.wiring as wiring
from fabulist.connections.registry import Registry
from fabulist.graph import CANONICAL_TOPOLOGY, build_graph
from fabulist.graph.graph import (
    _upgrade_legacy_critic_gate,
    _upgrade_legacy_fanout,
    _upgrade_legacy_planner,
)
from fabulist.graph.nodes import DEFAULT_PLANNER_DIRECTIVE
from fabulist.graph.plans import Plan, load_last_plan, save_plan
from fabulist.log import EventLog
from fabulist.prompts import preset_store
from fabulist.prompts import factory

SID = "s-plan"

#: The topology a pre-W9.20 install STORED: the canonical copy of the day — the
#: pre-W9.17 lane spine (``npc_lanes -> narrate`` plain), the pre-W9.19
#: unconditional critic and the POST-COMMIT planner (critic/decision_gate ->
#: planner -> bts). This is the primary REPOSITION case (§10).
PRE_W9_17_STORED: dict = {
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
        {"from": "critic", "to": "planner", "kind": "branch", "condition": "ok"},
        {"from": "critic", "to": "planner", "kind": "branch", "condition": "diffs"},
        {"from": "critic", "to": "narrate", "kind": "branch", "condition": "fail"},
        {"from": "planner", "to": "bts"},
        {"from": "bts", "to": "env_hud"},
        {"from": "env_hud", "to": "attribution"},
        {"from": "attribution", "to": "done"},
    ],
}

#: The W9.19 canonical copy: the same post-commit planner, but the critic is
#: already conditional (the shape ``preset_store.canonical_topology()`` returned
#: between W9.19 and W9.20). The commonest stored topology in the wild.
PRE_W9_20_STORED: dict = copy.deepcopy(PRE_W9_17_STORED)
PRE_W9_20_STORED["nodes"].insert(2, {"id": "npc_lane", "kind": "stage"})
PRE_W9_20_STORED["edges"] = [
    {"from": "pre_turn", "to": "npc_lanes"},
    {"from": "npc_lanes", "to": "npc_lane", "fanout": True},
    {"from": "npc_lanes", "to": "narrate"},
    {"from": "npc_lane", "to": "narrate"},
    {"from": "narrate", "to": "commit"},
    {"from": "commit", "to": "validate"},
    {"from": "validate", "to": "decision_gate"},
    {"from": "decision_gate", "to": "critic", "kind": "branch", "condition": "critic"},
    {"from": "decision_gate", "to": "planner", "kind": "branch", "condition": "planner"},
    {"from": "critic", "to": "planner", "kind": "branch", "condition": "ok"},
    {"from": "critic", "to": "planner", "kind": "branch", "condition": "diffs"},
    {"from": "critic", "to": "narrate", "kind": "branch", "condition": "fail"},
    {"from": "planner", "to": "bts"},
    {"from": "bts", "to": "env_hud"},
    {"from": "env_hud", "to": "attribution"},
    {"from": "attribution", "to": "done"},
]


def _hand_wired_planner() -> dict:
    """A topology that VALIDATES but wires the planner non-canonically.

    The pre-narration placement an owner can hand-edit: ``pre_turn -> planner``
    as a plain edge (no junction condition). The plan junction cannot move it,
    so the compile must refuse it (and the engine fall back to canonical).
    """
    topology = copy.deepcopy(PRE_W9_20_STORED)
    kept = []
    for edge in topology["edges"]:
        if edge.get("to") == "planner":
            # the post-commit nodes keep routing to the world tracker
            kept.append({**edge, "to": "bts"})
            continue
        kept.append(edge)
    kept.insert(0, {"from": "pre_turn", "to": "planner"})
    kept.insert(1, {"from": "planner", "to": "npc_lanes"})
    topology["edges"] = kept
    return topology


# -- fakes -------------------------------------------------------------------


class ChannelModel:
    """A ``ModelCaller`` yielding one canned content delta per pass."""

    def __init__(self, text: str = "The rain keeps falling."):
        self.text = text
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append(messages)
        yield {"type": "content", "delta": self.text}


def _extractor(user_msg, prose):
    return {"deltas": {}, "action_class": "dialogue"}


def _registry() -> Registry:
    return Registry.from_dict(
        {
            "connections": {
                "c": {"base_url": "http://localhost:4444/v1", "model": "your-model-id"}
            },
            "agents": {"narrator": "c", "extractor": "c"},
        }
    )


def _plain_edges(graph) -> set[tuple[str, str]]:
    drawable = graph.get_graph()
    return {(e.source, e.target) for e in drawable.edges if not e.conditional}


def _branch(graph, source: str):
    branches = graph.builder.branches[source]
    assert len(branches) == 1, list(branches)
    return next(iter(branches.values()))


@pytest.fixture()
def log(tmp_path):
    log = EventLog(tmp_path / "planner.db")
    try:
        yield log
    finally:
        log.close()


def _compiled(log, topology, *, model=None, plan_cadence=5):
    return build_graph(
        model=model or ChannelModel(),
        extractor=_extractor,
        log=log,
        topology=topology,
        plan_cadence=plan_cadence,
    )


# -- §15.1 the three upgrades, composed on one stored topology ---------------


def test_three_upgrades_compose_on_one_pre_w9_17_topology(log):
    """Fan-out + conditional critic + planner reposition, all at compile time."""
    stored = copy.deepcopy(PRE_W9_17_STORED)
    graph = _compiled(log, stored, plan_cadence=5)

    # 1. the Send fan-out was inserted (pre-W9.17 "npc_lanes -> narrate" plain)
    drawable = graph.get_graph()
    conditional = {(e.source, e.target) for e in drawable.edges if e.conditional}
    assert ("npc_lanes", "npc_lane") in conditional
    assert "npc_lane" in drawable.nodes
    # 2. the critic became conditional (the gate + the critic's keep verdicts)
    assert ("decision_gate", "critic") in conditional
    assert ("critic", "bts") in conditional
    # 3. the planner was REPOSITIONED onto the junction...
    assert ("pre_turn", "planner") in conditional
    assert ("pre_turn", "npc_lanes") in conditional
    assert {"plan": "planner", "skip": "npc_lanes"} == _branch(graph, "pre_turn").ends
    # ...and nothing routes into it post-commit any more.
    assert not [e for e in conditional if e[1] == "planner" and e[0] != "pre_turn"]
    assert ("planner", "npc_lanes") in _plain_edges(graph)
    assert ("planner", "bts") not in _plain_edges(graph)
    # the stored record itself is untouched (the upgrade is compile-side)
    assert stored == PRE_W9_17_STORED


def test_the_upgraded_shape_is_idempotent(log):
    """Running the upgrades twice changes nothing (no doubled junction edges)."""
    node_ids, edges = _upgrade_legacy_fanout(
        list(PRE_W9_17_STORED["nodes"][i]["id"] for i in range(
            len(PRE_W9_17_STORED["nodes"])
        )),
        copy.deepcopy(PRE_W9_17_STORED["edges"]),
    )
    edges = _upgrade_legacy_critic_gate(edges)
    rows = {node["id"]: dict(node) for node in PRE_W9_17_STORED["nodes"]}
    once = _upgrade_legacy_planner(node_ids, edges, rows, None)
    twice = _upgrade_legacy_planner(node_ids, once, rows, None)
    assert twice == once
    junction = [e for e in once if e.get("from") == "pre_turn"]
    assert len(junction) == 2  # exactly one arm per branch value

    upgraded = {
        "nodes": [
            *copy.deepcopy(PRE_W9_17_STORED["nodes"]),
            {"id": "npc_lane", "kind": "stage"},
        ],
        "edges": once,
    }
    graph = _compiled(log, upgraded)
    assert {"plan": "planner", "skip": "npc_lanes"} == _branch(graph, "pre_turn").ends


def test_the_default_presets_stored_topology_compiles_to_the_junction(log):
    """§15.2: the seeded default carries a stored canonical copy — reproject it."""
    preset_store.ensure_default_preset(log)
    # Simulate the seeded row of a pre-W9.20 install: the canonical copy of the
    # day, stored by the owner's topology Save.
    preset_store.set_default_topology(log, PRE_W9_20_STORED)
    stored = preset_store.preset_topology(preset_store.default_preset(log))
    assert stored == PRE_W9_20_STORED

    graph = _compiled(log, stored)
    assert {"plan": "planner", "skip": "npc_lanes"} == _branch(graph, "pre_turn").ends
    assert ("planner", "npc_lanes") in _plain_edges(graph)
    assert ("planner", "bts") not in _plain_edges(graph)


# -- §15.3b the router's domain IS the junction's path map -------------------


def test_plan_due_route_values_are_the_junction_path_map_keys(log):
    """Due -> "plan", skip -> "skip", both resolved THROUGH the compiled edge."""
    graph = _compiled(log, dict(CANONICAL_TOPOLOGY))
    branch = _branch(graph, "pre_turn")
    assert branch.ends == {"plan": "planner", "skip": "npc_lanes"}

    async def route(state):
        return await branch.path.ainvoke(state)

    # turn 1: no plan exists -> mandatory
    due = asyncio.run(route({"session_id": SID}))
    assert due == "plan"
    assert due in branch.ends

    # a committed turn WITH a plan inside the cadence -> skip
    log.append_event(SID, "narrative", "model", {"prose": "One."})
    save_plan(
        log, SID, Plan(turn_seq=1, created_from="manual", strings=["a", "b", "c", "d"])
    )
    skip = asyncio.run(route({"session_id": SID}))
    assert skip == "skip"
    assert skip in branch.ends


# -- §15.3c a refused upgrade falls back to the canonical turn ---------------


def test_a_non_canonical_planner_wiring_is_refused_at_the_graph_layer(log):
    """A hand-made pre-narration planner (no junction condition) is refused."""
    with pytest.raises(ValueError, match="compile refused"):
        _compiled(log, _hand_wired_planner())


def test_a_template_planner_elsewhere_is_refused(log):
    """W9.10 template node implementing the planner under another id: refuse."""
    topology = copy.deepcopy(PRE_W9_20_STORED)
    topology["nodes"] = [
        node for node in topology["nodes"] if node["id"] != "planner"
    ] + [{"id": "my_plan", "template": "tpl_plan"}]
    topology["edges"] = [
        {**edge, "to": "my_plan" if edge["to"] == "planner" else edge["to"]}
        for edge in topology["edges"]
        if edge["from"] != "planner"
    ] + [{"from": "my_plan", "to": "bts"}]
    with pytest.raises(ValueError, match="planner behaviour"):
        build_graph(
            model=ChannelModel(),
            extractor=_extractor,
            log=log,
            topology=topology,
            templates={"tpl_plan": "planner"},
        )


def test_the_engine_compiles_canonical_when_the_stored_topology_is_refused(
    log, monkeypatch
):
    """§10/N3: the refusal never boots a per-turn post-commit planner."""
    preset_store.ensure_default_preset(log)
    refused = _hand_wired_planner()
    preset_store.set_default_topology(log, refused)

    seen: list[dict | None] = []
    real_build = wiring.build_graph

    def spy(**kwargs):
        seen.append(kwargs.get("topology"))
        return real_build(**kwargs)

    monkeypatch.setattr(wiring, "build_graph", spy)
    engine = wiring.build_engine(
        registry=_registry(),
        log=log,
        bts_enabled=False,
        attribution_enabled=False,
    )
    assert engine is not None
    # Building the engine compiles canonical (no stored topology yet at boot);
    # the turn-start rebuild is where the stored topology is tried. The private
    # rebuild hook is the only door to it without running a network turn.
    _revision, compiled = engine._graph_source()
    assert compiled is not None
    assert refused in seen  # the refused topology DID reach the builder...
    assert seen[-1] is None  # ...and the compile fell back to canonical


# -- §6 the wiring seams the planner node is built with ---------------------


class _FakeDelta:
    def __init__(self, content: str | None = None) -> None:
        self.content = content
        self.reasoning_content = None
        self.tool_calls = None


class _FakeChoice:
    def __init__(self, delta: _FakeDelta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, choices: list) -> None:
        self.choices = choices
        self.usage = None


class _FakeCompletions:
    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    async def create(self, **kwargs):
        async def _stream():
            for chunk in self._chunks:
                yield chunk

        return _stream()


class _FakeAsyncClient:
    def __init__(self, chunks: list) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(chunks))


PLAN_PAYLOAD = {
    "strings": ["the guild asks", "the ledger turns", "the alley waits", "the debt calls"],
    "datetime": "1492-03-04 07:40",
    "weather": "cold drizzle",
    "forecast": "clearing by noon",
    "established": ["the guild owes the ledger"],
}


def _engine_planner(log, monkeypatch, preset_payload: dict):
    """The planner node ``build_engine`` registers, with the transport faked.

    The retired narrate-regen label test pattern: intercept ``build_graph`` to reach
    the real wiring closures without running a graph, and fake the streaming
    transport at ``wiring._async_client``.
    """
    captured: dict = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(wiring, "build_graph", capture)
    chunks = [
        _FakeChunk([_FakeChoice(_FakeDelta(json.dumps(PLAN_PAYLOAD)))]),
        _FakeChunk([_FakeChoice(_FakeDelta(), finish_reason="stop")]),
    ]
    monkeypatch.setattr(
        wiring, "_async_client", lambda agent, registry: _FakeAsyncClient(chunks)
    )
    preset = preset_store.preset_from_json(preset_payload)
    preset_store.append_preset(log, preset)
    preset_store.set_active(log, preset.preset_id)
    wiring.build_engine(
        registry=_registry(),
        log=log,
        agent_debug=True,
        bts_enabled=False,
        attribution_enabled=False,
    )
    return captured


def test_the_engine_registers_the_planner_with_real_seams(log, monkeypatch):
    """§6/[OWNER] 11: the node the engine builds runs the preset's planner step.

    Drives the CAPTURED node through the real wiring closures with only the
    transport faked: the call goes out on the planner interaction's caller, the
    directive is the flow's system row, the preset's steer rides the ask, and the
    plan event lands on the log.
    """
    captured = _engine_planner(
        log,
        monkeypatch,
        {
            "preset_id": "gm",
            "name": "GM",
            "planner": {"cadence": 3, "steer": "keep it tense", "fields": []},
        },
    )
    planner = captured["planner"]
    assert callable(planner)
    # the cadence reader is LIVE: the active preset's 3, not the default 5
    assert captured["plan_cadence"]() == 3

    from fabulist.agents.debug import CURRENT_SESSION

    CURRENT_SESSION.set(SID)
    try:
        update = asyncio.run(planner({"session_id": SID}))
    finally:
        CURRENT_SESSION.set("")
    plan = load_last_plan(log, SID)
    assert plan is not None and plan.created_from == "graph"
    assert plan.strings == PLAN_PAYLOAD["strings"]
    assert "STRINGS: " in update["plan"]
    # the scenario seeds landed in the state-delta (the planner's other effect)
    assert log.get_projection(SID)["state"]["weather"] == "cold drizzle"

    asked = log_plan_call(log)
    system, ask = asked[0]["content"], asked[1]["content"]
    assert system.startswith(DEFAULT_PLANNER_DIRECTIVE)
    assert "Steer: keep it tense" in ask


def log_plan_call(log) -> list[dict]:
    """The planner's captured request (the debug capture row's messages)."""
    rows = [e for e in log.get_events(SID) if e.payload.get("event") == "agent_call"]
    assert rows, "the planner call was not captured"
    assert rows[-1].payload["agent"] == "planner"
    return rows[-1].payload["messages"]


def test_the_planner_directive_is_the_flows_system_row(log, monkeypatch):
    """§6: the planner step's authored text IS the directive the node gets.

    The default flow carries ONE row (the directive) with empty content, so the
    code text renders until the owner edits that step — and then the edited text
    is what the planner call opens with.
    """
    from fabulist.prompts.modules import DEFAULT_FLOWS

    row = DEFAULT_FLOWS["planner"]
    assert [module.type for module in row] == ["system"]
    assert row[0].content == ""  # the code default renders until the owner edits

    captured = _engine_planner(
        log,
        monkeypatch,
        {
            "preset_id": "voice",
            "name": "Voice",
            "flows": {
                "planner": [
                    {
                        "id": "planner_system",
                        "name": "Planner directive",
                        "type": "system",
                        "role": "system",
                        "content": "PLAN WITH A COLD VOICE.",
                        "enabled": True,
                    }
                ]
            },
        },
    )
    from fabulist.agents.debug import CURRENT_SESSION

    CURRENT_SESSION.set(SID)
    try:
        asyncio.run(captured["planner"]({"session_id": SID}))
    finally:
        CURRENT_SESSION.set("")
    assert log_plan_call(log)[0]["content"].startswith("PLAN WITH A COLD VOICE.")


# -- the A2 seam: the planner block survives a default commit/reset ----------


def test_set_default_planner_persists_the_block_on_the_default_row(log):
    preset_store.ensure_default_preset(log)
    saved = preset_store.set_default_planner(
        log, {"cadence": 2, "steer": "slow burn", "fields": []}
    )
    assert saved.preset_id == preset_store.DEFAULT_PRESET_ID
    assert saved.is_default is True
    assert preset_store.planner_config(preset_store.default_preset(log)) == {
        "cadence": 2,
        "steer": "slow burn",
        "fields": [],
    }


def test_set_default_planner_creates_the_row_when_it_was_never_seeded(log):
    assert preset_store.default_preset(log) is None
    row = preset_store.set_default_planner(log, {"cadence": 7})
    assert row.is_default is True
    assert preset_store.planner_config(row)["cadence"] == 7


def test_the_factory_payload_keeps_the_planner_block():
    block = {"cadence": 4, "steer": "hold the line", "fields": []}
    normalized = factory.normalize_preset_payload(
        {"preset_id": "p", "name": "P", "planner": block}
    )
    assert normalized["planner"] == block
    # absent (and the default shape) stay absent: absent reads as the defaults
    assert "planner" not in factory.normalize_preset_payload({"name": "P"})
    assert "planner" not in factory.normalize_preset_payload(
        {
            "name": "P",
            "planner": {"cadence": preset_store.PLANNER_DEFAULT_CADENCE, "steer": "", "fields": []},
        }
    )
