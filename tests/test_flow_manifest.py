"""The flow manifest (W9.1) — schema, recursion, activation coverage, ``--check``.

The manifest is GENERATED from the code through one seam
(:func:`fabulist.flow.manifest.build_manifest`): the committed artifact
(``prompts/generated/flow-manifest.json``) and ``GET /api/flow/manifest`` both
come from that builder, so they cannot drift. These tests pin the artifact to
the builder (the ``tests/test_agent_map.py`` pattern), assert the schema is
recursive (R13: ``narrate.composition`` is a real ``SubGraph``), assert the
branch topology the plan models (the critic's ok/diffs fan-out — the retired
``fail -> narrate`` regeneration edge is asserted GONE, W9.24 §3.3.7), and assert
the data coverage the UI depends on (every emitted stage name maps to a real turn
node id).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # scripts/ is a namespace package, not installed
    sys.path.insert(0, str(_ROOT))

from scripts import generate_flow_manifest as flow_gen  # noqa: E402

from fabulist.api import create_app  # noqa: E402
from fabulist.api.fake import FakeTurnEngine  # noqa: E402
from fabulist.api.flow_api import router as flow_router  # noqa: E402
from fabulist.flow import manifest as flow_manifest  # noqa: E402
from fabulist.prompts.agent_sets import (  # noqa: E402
    DEFAULT_BINDINGS,
    INTERACTIONS,
    set_bindings_provider,
)
from fabulist.prompts.catalog import INTERACTION_CATALOG  # noqa: E402

FLOW_MANIFEST_PATH = _ROOT / "prompts" / "generated" / "flow-manifest.json"

_GRAPH_KEYS = {"id", "kind", "title", "attached_to", "entry", "nodes", "edges"}
_NODE_KEYS = {
    "id",
    "title",
    "kind",
    "runs",
    "agent",
    "prompt_surface",
    "inspector",
    "activation",
    "composition",
    "fanout",
    "meta",
}
_EDGE_KEYS = {"id", "from", "to", "kind", "condition", "label", "cap"}


def _committed() -> dict:
    return json.loads(FLOW_MANIFEST_PATH.read_text(encoding="utf-8"))


def _turn_graph(manifest: dict) -> dict:
    return manifest["graphs"][0]


def _node(manifest: dict, node_id: str) -> dict:
    for graph in manifest["graphs"]:
        for node in graph["nodes"]:
            if node["id"] == node_id:
                return node
    raise AssertionError(f"no node {node_id!r} in any graph")


# -- artifact / generator parity --------------------------------------------


REGEN = "uv run python scripts/generate_flow_manifest.py"


def test_generator_runs_and_matches_committed_artifact():
    generated = flow_manifest.build_manifest()
    assert generated["graphs"]
    assert flow_manifest.render(generated) == FLOW_MANIFEST_PATH.read_text(encoding="utf-8"), (
        f"prompts/generated/flow-manifest.json is stale — regenerate it: {REGEN}"
    )


def test_check_passes_against_the_committed_file():
    assert flow_manifest.check(FLOW_MANIFEST_PATH) is True


def test_check_fails_when_the_file_is_mutated(tmp_path: Path):
    text = flow_manifest.render(flow_manifest.build_manifest())
    mutated = tmp_path / "flow-manifest.json"
    mutated.write_text(text.replace("turn_flow", "turn_flowX", 1), encoding="utf-8")
    assert mutated.read_text(encoding="utf-8") != text
    assert flow_manifest.check(mutated) is False


def test_check_fails_when_the_file_is_missing(tmp_path: Path):
    assert flow_manifest.check(tmp_path / "absent.json") is False


def test_main_check_mode_returns_zero_for_committed_file(monkeypatch):
    monkeypatch.setattr(flow_gen, "FLOW_MANIFEST_PATH", FLOW_MANIFEST_PATH)
    assert flow_gen.main(["--check"]) == 0


def test_main_check_mode_returns_nonzero_on_drift(tmp_path: Path, monkeypatch):
    drifted = tmp_path / "flow-manifest.json"
    drifted.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(flow_gen, "FLOW_MANIFEST_PATH", drifted)
    assert flow_gen.main(["--check"]) == 1


# -- schema validity ---------------------------------------------------------


def test_manifest_top_level_schema():
    manifest = _committed()
    assert set(manifest) == {"schema_version", "generated_from", "activation_map", "graphs"}
    assert manifest["schema_version"] == 1
    assert manifest["generated_from"]["stage_names"] == "fabulist.graph.nodes.STAGE_NAMES"
    assert manifest["generated_from"]["node_set"] == "fabulist.flow.manifest.TURN_NODES"


def test_every_graph_and_node_and_edge_has_the_schema_keys():
    manifest = _committed()
    assert manifest["graphs"][0]["id"] == "turn_flow"
    assert manifest["graphs"][0]["kind"] == "turn"
    for graph in manifest["graphs"]:
        assert set(graph) == _GRAPH_KEYS, graph["id"]
        ids = [node["id"] for node in graph["nodes"]]
        assert len(ids) == len(set(ids)), graph["id"]
        assert graph["entry"] in ids
        for node in graph["nodes"]:
            assert set(node) == _NODE_KEYS, node["id"]
            assert set(node["runs"]) == {"kind", "interaction", "call_site", "description"}
            assert node["inspector"]["kind"] in {"model_pass", "fixed_stage", "gate", "terminal"}
            assert node["prompt_surface"]["door"] in {"step_editor", "fixed", "none"}
            if node["prompt_surface"]["door"] == "step_editor":
                assert node["prompt_surface"]["step_id"]
        for edge in graph["edges"]:
            assert set(edge) == _EDGE_KEYS, edge["id"]
            # W9.17 adds "fanout": the Send fan-out edge (the frontend
            # normalizes it to "flow", so it draws like any other edge).
            assert edge["kind"] in {"flow", "branch", "loop_back", "attach", "fanout"}
            assert edge["from"] in ids, edge["id"]


# -- recursion proof (R13) ---------------------------------------------------


def test_narrate_composition_is_a_recursive_subgraph():
    narrate = _node(_committed(), "narrate")
    comp = narrate["composition"]
    assert comp is not None
    assert comp["kind"] == "composition"
    assert set(comp) == {"kind", "title", "regions", "entry", "sink", "nodes", "edges"}
    ids = [node["id"] for node in comp["nodes"]]
    assert comp["entry"] in ids
    assert comp["sink"] in ids
    for node in comp["nodes"]:
        assert set(node) == _NODE_KEYS  # the SAME Node shape inside
    for edge in comp["edges"]:
        assert set(edge) == _EDGE_KEYS
        assert edge["from"] in ids and edge["to"] in ids


def test_composition_declares_cache_contract_regions():
    comp = _node(_committed(), "narrate")["composition"]
    regions = {region["id"]: region for region in comp["regions"]}
    assert regions["stable_prefix"]["kind"] == "stable_prefix"
    assert regions["dynamic_tail"]["kind"] == "dynamic_tail"
    assert regions["stable_prefix"]["order"] < regions["dynamic_tail"]["order"]
    # every composition node names a declared region (via its meta description)
    declared = set(regions)
    for node in comp["nodes"]:
        if node["id"] == comp["sink"]:
            continue
        region = node["meta"]["description"].removeprefix("Region: ")
        assert region in declared, node["id"]


def test_only_the_narrate_node_carries_a_composition_in_wave_1():
    manifest = _committed()
    composers = [
        node["id"]
        for graph in manifest["graphs"]
        for node in graph["nodes"]
        if node["composition"] is not None
    ]
    assert composers == ["narrate"]


# -- activation map coverage -------------------------------------------------


def test_activation_map_covers_every_emitted_stage_name():
    manifest = _committed()
    names = set(flow_manifest.stage_names())
    assert names  # the emitter has stages
    assert set(manifest["activation_map"]) == names


def test_every_activation_value_is_a_real_turn_node():
    manifest = _committed()
    turn_ids = {node["id"] for node in _turn_graph(manifest)["nodes"]}
    for stage, node_id in manifest["activation_map"].items():
        assert node_id in turn_ids, stage


def test_stage_name_node_id_gaps_are_carried_as_data():
    """The emitter's names are not the node ids; the table carries the mapping."""
    activation = _committed()["activation_map"]
    assert activation["dsl"] == "validate"
    assert activation["world_state"] == "bts"
    # W9.24 §3.3.7: the ``regen`` stage is retired — the narrator's repair
    # iterations re-emit the ``narrate`` slot's label instead of a stage of their
    # own, so the activation map (and the flow panel) carries no such row.
    assert "regen" not in activation


def test_attribution_node_title_matches_the_step_bars_label():
    """The flow board/inspector title and the step bar name the step identically.

    Two surfaces show the ``attribution`` step: the flow panel node (its
    inspector header too) renders the manifest ``title``, while the turn's step
    bar renders the emitted ``stage`` frame's label from
    ``nodes._STAGE_SPECS``. A rename must land on both, so pin them together.
    """
    from fabulist.graph.nodes import _STAGE_SPECS

    assert _node(_committed(), "attribution")["title"] == "Attributing speakers"
    assert _STAGE_SPECS["attribution"][0] == "Attributing speakers"


# -- topology: branches, loop-back and ordering ------------------------------


def test_critic_fans_out_ok_and_diffs_only():
    edges = _turn_graph(_committed())["edges"]
    branches = {edge["label"]: edge for edge in edges if edge["from"] == "critic"}
    assert set(branches) == {"ok", "diffs"}
    # W9.20: the planner runs BEFORE the narration, so a keep verdict goes
    # straight to the world tracker.
    assert branches["ok"]["to"] == "bts"
    assert branches["diffs"]["to"] == "bts"
    for edge in branches.values():
        assert edge["kind"] == "branch"
        assert edge["condition"] == f"verdict == {edge['label']}"


def test_the_retired_fail_edge_is_gone_from_the_manifest():
    """W9.24 §3.3.7: no critic -> narrate edge, no cap, no loop-back node.

    The narrator repairs in its own node before the commit, so the flow panel
    must not render a regeneration loop the engine no longer has.
    """
    edges = _turn_graph(_committed())["edges"]
    assert not [edge for edge in edges if edge["kind"] == "loop_back"]
    assert not [
        edge
        for edge in edges
        if edge["from"] == "critic" and edge["to"] == "narrate"
    ]
    assert not [edge for edge in edges if edge.get("cap")]
    # The narrator still feeds the commit node, which feeds the validator: the
    # spine the retired loop used to re-enter is intact.
    assert any(edge["from"] == "narrate" and edge["to"] == "commit" for edge in edges)
    assert any(edge["from"] == "commit" and edge["to"] == "validate" for edge in edges)


def test_critic_precedes_bts_along_the_spine():
    """C2: critic-before-BTS is encoded as edges and cannot be reordered."""
    edges = _turn_graph(_committed())["edges"]
    assert any(edge["from"] == "critic" and edge["to"] == "bts" for edge in edges)
    assert not any(
        edge["from"] == "critic" and edge["to"] == "planner" for edge in edges
    )


def test_the_plan_junction_is_authored_as_edges():
    """W9.20 §2/§10: pre_turn -> [plan] planner -> npc_lanes, and [skip] lanes."""
    edges = _turn_graph(_committed())["edges"]
    junction = {
        (edge["from"], edge["to"], edge["kind"], edge["label"], edge["condition"])
        for edge in edges
        if edge["from"] == "pre_turn"
    }
    assert junction == {
        ("pre_turn", "planner", "branch", "due", "plan due"),
        ("pre_turn", "npc_lanes", "branch", "skip", "no plan due"),
    }
    assert any(
        edge["from"] == "planner" and edge["to"] == "npc_lanes" for edge in edges
    )
    # The junction's ``plan`` edge is where the emitted ``plan`` stage lives.
    assert _committed()["activation_map"]["plan"] == "planner"


def test_the_planner_node_is_a_preset_bindable_model_pass():
    """W9.20 §6/§10: model_pass + a step_editor door onto the planner step."""
    planner = _node(_committed(), "planner")
    assert planner["runs"]["kind"] == "model_pass"
    assert planner["runs"]["interaction"] == "planner"
    assert planner["prompt_surface"] == {"door": "step_editor", "step_id": "planner"}
    assert planner["agent"]["default_agent"] == DEFAULT_BINDINGS["planner"]["agent"]
    assert planner["activation"] == ["plan"]
    # W9.31 §3.5 (S1): the seed phase's writes are the tracker's own ``entity_*``
    # rows, so the planner node must declare them — otherwise the per-node
    # inspector hides every object the bootstrap/repair phase recorded.
    declared = set(planner["inspector"]["events"])
    assert {"plan", "plan_refresh_failed"} <= declared
    assert {
        "entity_establish",
        "entity_move",
        "entity_set",
        "entity_update",
        "entity_retire",
        "validation_notice",
    } <= declared


def test_the_turn_graph_carries_no_proposal_stage():
    """W9.23 §3.6: the proposal lanes left the turn; the tail ends at ``done``.

    The honest picture the owner asked for: proposals arrive BETWEEN turns, so
    the turn diagram must not show a proposal box at all (the historian's
    off-turn graph below is where they live now).
    """
    manifest = _committed()
    turn_ids = [node["id"] for node in _turn_graph(manifest)["nodes"]]
    assert "asset_proposals" not in turn_ids
    assert "asset_proposal_lane" not in turn_ids

    edges = _turn_graph(manifest)["edges"]
    by_label = {(edge["from"], edge["to"]): edge for edge in edges}
    assert ("bts", "env_hud") in by_label
    assert ("env_hud", "attribution") in by_label
    assert ("attribution", "done") in by_label
    assert not [
        (edge["from"], edge["to"])
        for edge in edges
        if "asset_proposal" in edge["from"] or "asset_proposal" in edge["to"]
    ]


def test_the_historian_is_an_off_turn_flow_attached_to_done():
    """W9.23 §3.6.4: the lane system is drawn where it RUNS — between turns.

    The row is the compaction row's shape: an engine trigger feeding ONE model
    pass, attached to the turn node it hangs off, with the run trace and the
    filed rows as its inspector events.
    """
    manifest = _committed()
    historian = next(
        graph for graph in manifest["graphs"] if graph.get("id") == "historian"
    )
    assert historian["kind"] == "off_turn"
    assert historian["attached_to"] == "done"
    assert [node["id"] for node in historian["nodes"]] == [
        "due_check",
        "proposal_lanes",
    ]
    assert historian["nodes"][0]["runs"]["kind"] == "engine"
    assert historian["nodes"][0]["activation"] == []
    lanes = historian["nodes"][1]
    assert lanes["runs"]["kind"] == "model_pass"
    assert lanes["runs"]["interaction"] == "proposals"
    assert lanes["prompt_surface"] == {"door": "step_editor", "step_id": "proposals"}
    assert (
        lanes["agent"]["default_agent"]
        == DEFAULT_BINDINGS["proposals"]["agent"]
        == "extractor"
    )
    assert lanes["inspector"]["agent_filter"] == "proposals"
    # The run's own trace and the rows it files share one inspector row.
    assert set(lanes["inspector"]["events"]) == {"historian_run", "asset_proposal"}
    assert [edge["from"] for edge in historian["edges"]] == ["due_check", "due_check"]
    # The attach edge names the turn node the flow hangs off (rendered as the
    # external stub the FlowGraph draws for a target outside the graph).
    assert historian["edges"][0]["kind"] == "attach"
    assert historian["edges"][0]["to"] == "done"
    assert historian["edges"][1]["to"] == "proposal_lanes"


def test_off_turn_graphs_are_attached_to_real_turn_nodes():
    manifest = _committed()
    turn_ids = {node["id"] for node in _turn_graph(manifest)["nodes"]}
    off_turn = [graph for graph in manifest["graphs"] if graph["kind"] == "off_turn"]
    assert {graph["id"] for graph in off_turn} == {
        "recall",
        "imagegen",
        "compaction",
        "assistant_hooks",
        "historian",
    }
    for graph in off_turn:
        assert graph["attached_to"] in turn_ids, graph["id"]


# -- registry / catalog coverage --------------------------------------------


def test_model_pass_interactions_are_registry_interactions():
    manifest = _committed()
    for graph in manifest["graphs"]:
        for node in graph["nodes"]:
            runs = node["runs"]
            if runs["kind"] == "model_pass" and runs["interaction"] is not None:
                assert runs["interaction"] in INTERACTIONS, node["id"]


def test_step_editor_doors_point_at_catalog_steps():
    manifest = _committed()
    for graph in manifest["graphs"]:
        for node in graph["nodes"]:
            for inner in (node, *(node["composition"]["nodes"] if node["composition"] else ())):
                surface = inner["prompt_surface"]
                if surface["door"] == "step_editor":
                    assert surface["step_id"] in INTERACTION_CATALOG, inner["id"]


def test_artifact_default_agents_match_default_bindings():
    manifest = _committed()
    for graph in manifest["graphs"]:
        for node in graph["nodes"]:
            agent = node["agent"]
            if agent and agent["interaction"] in DEFAULT_BINDINGS:
                assert agent["default_agent"] == DEFAULT_BINDINGS[agent["interaction"]]["agent"]
            if agent:
                assert agent["bound_agent"] is None  # artifact is preset-independent


# -- endpoint ----------------------------------------------------------------


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_endpoint_serves_the_manifest_with_bound_agents(tmp_path: Path):
    app = create_app(FakeTurnEngine(), tmp_path / "flow.db")
    app.include_router(flow_router)
    try:
        async def _get():
            async with _client(app) as client:
                return await client.get("/api/flow/manifest")

        response = asyncio.run(_get())
        assert response.status_code == 200
        body = response.json()
        assert body["schema_version"] == 1
        assert body["graphs"][0]["id"] == "turn_flow"
        narrate = _node(body, "narrate")["agent"]
        assert narrate["bound_agent"] == "narrator"
    finally:
        app.state.log.close()


def test_endpoint_overlays_the_active_presets_binding(tmp_path: Path):
    app = create_app(FakeTurnEngine(), tmp_path / "flow2.db")
    app.include_router(flow_router)
    set_bindings_provider(lambda log: {"bts": {"agent": "narrator", "overrides": {}}})
    try:
        async def _get():
            async with _client(app) as client:
                return await client.get("/api/flow/manifest")

        body = asyncio.run(_get()).json()
        bts = _node(body, "bts")["agent"]
        assert bts["default_agent"] == "extractor"  # artifact default unchanged
        assert bts["bound_agent"] == "narrator"  # preset overlay wins
    finally:
        set_bindings_provider(None)
        app.state.log.close()


def test_lanes_endpoint_shape_and_width(monkeypatch):
    """W9.8: the panel draws ACTUAL members, so the lanes read must serve the
    last turn's real lane set (and the pool width from the fanout descriptor)."""
    from fabulist.api import flow_api

    class _Evt:
        def __init__(self, seq, payload):
            self.seq, self.type, self.payload = seq, "system", payload

    class _Log:
        def get_chain_events(self, session_id):
            # per-lane rows precede their summary (the real append order)
            return [
                _Evt(10, {"event": "npc_lanes", "source": "presence", "lanes": ["cm_a"]}),
                _Evt(11, {"event": "npc_lane", "member_id": "cm_a", "name": "Kael", "intent": "ask", "ok": True}),
                _Evt(12, {"event": "npc_lane", "member_id": "cm_b", "name": "Mara", "intent": "warn", "ok": False}),
                _Evt(12, {"event": "npc_lane", "member_id": "cm_u", "name": "User", "intent": "player", "ok": True}),
                _Evt(14, {"event": "npc_lanes", "source": "presence", "lanes": ["cm_a", "cm_b"]}),
            ]

    class _App:
        state = type("S", (), {"log": _Log()})()

    class _Req:
        app = _App()

    import asyncio

    body = asyncio.run(flow_api.get_lanes_route("s1", _Req()))
    # the player never draws as a cast arm (W9.16)
    assert [lane["name"] for lane in body["lanes"]] == ["Kael", "Mara"]
    assert body["lanes"][1]["ok"] is False
    assert body["source"] == "presence"
    assert body["max_parallel"] == 4
    assert body["turn_seq"] == 14
