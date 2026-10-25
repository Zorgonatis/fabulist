"""Issue #56.2/#56.3: the lane width is topology data, and the off switch is real.

Owner spec (issue #56):

* ``max_lanes`` bounds CONCURRENCY and must be overridable — the stored topology
  node's ``data.max_lanes`` is the value the running graph honours, and the flow
  surface serves THAT value (it used to serve the code default of 4 whatever the
  owner saved).
* NPC lanes must be switchable OFF as a STAGE: a disabled stage leaves the
  compiled graph (no dispatcher run, no ``Send``, no lane node) and the turn
  runs the narrator's single forward pass — the documented zero-lane path.
  Re-enabling brings the lanes back.
* The switch is topology data written through the existing validated door
  (:mod:`fabulist.api.topology_api`): an edit the compile cannot honour is
  refused with its violation named, never stored.

Fake models only — no live inference.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.flow_api import router as flow_router
from fabulist.api.prompts_api import router as prompts_router
from fabulist.api.topology_api import router as topology_router
from fabulist.graph.contracts import validate_topology
from fabulist.graph.graph import CANONICAL_TOPOLOGY
from fabulist.graph.node_templates import lane_config
from fabulist.prompts import factory
from fabulist.prompts.preset_store import (
    DEFAULT_PRESET_ID,
    get_preset,
    preset_topology,
    set_default_topology,
    stored_topology,
)
from fabulist.state.cast import admit_member

SID = "s-switch"


# -- topology builders -------------------------------------------------------


def _topology(*, max_lanes: int | None = 4, enabled: bool | None = None) -> dict:
    """The canonical wiring with the fan-out node's ``data`` set as asked."""
    topology = copy.deepcopy(CANONICAL_TOPOLOGY)
    for node in topology["nodes"]:
        if node["id"] != "npc_lanes":
            continue
        data: dict = {}
        if max_lanes is not None:
            data["max_lanes"] = max_lanes
        if enabled is not None:
            data["enabled"] = enabled
        node["data"] = data
    return topology


class _Narrator:
    def __init__(self):
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append([dict(message) for message in messages])
        yield {"type": "content", "delta": "Rain drums on the shutters."}


class _Extractor:
    async def __call__(self, user_msg, prose):
        return {"deltas": {}, "action_class": "dialogue"}


class _LaneCaller:
    """One JSON lane reply per call; records the peak concurrency."""

    def __init__(self, delay=0.02):
        self.delay = delay
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            yield {
                "type": "content",
                "delta": '{"intent": "hold the door", "narrative": "waits", "lines": []}',
            }
        finally:
            self.in_flight -= 1


def _admit_world(log, names):
    for name in names:
        admit_member(
            log,
            SID,
            name,
            provenance="card",
            admitted_by="test",
            reference_kind="identity",
        )
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


def _build(state):
    return [
        {"role": "system", "content": "You are the narrator."},
        {"role": "user", "content": state["user_content"]},
    ]


def _drive(log, topology, *, lane_caller=None, narrator=None):
    """Compile ``topology`` and run one full turn; returns the compiled graph."""
    from fabulist.graph.engine import GraphTurnEngine
    from fabulist.graph.graph import build_graph
    from fabulist.graph.nodes import make_npc_lanes_node

    library = {}  # no template library in these builds: canonical rows only
    nodes_config = lane_config(topology, library)
    lane_node = make_npc_lanes_node(
        log=log, build_messages=_build, caller=lane_caller or _LaneCaller(),
        config=nodes_config,
    )
    narrator = narrator or _Narrator()
    graph = build_graph(
        model=narrator,
        extractor=_Extractor(),
        log=log,
        build_messages=_build,
        topology=topology,
        npc_lanes=lane_node,
    )
    engine = GraphTurnEngine(graph=graph, log=log, model=narrator)

    async def go():
        return [frame async for frame in engine.run_turn(SID, "I wait.")]

    asyncio.run(go())
    return graph


def _lane_rows(log):
    return [
        event.payload
        for event in log.get_events(SID)
        if isinstance(event.payload, dict) and event.payload.get("event") == "npc_lane"
    ]


# -- #56.3, the off switch ---------------------------------------------------


def test_a_disabled_topology_compiles_without_the_lane_nodes():
    """The switch removes the STAGE, not just its output."""
    from fabulist.graph.graph import build_graph

    graph = build_graph(
        model=_Narrator(), extractor=_Extractor(), log=_NullLog(),
        build_messages=_build, topology=_topology(enabled=False),
    )
    nodes = set(graph.get_graph().nodes)
    assert "npc_lanes" not in nodes and "npc_lane" not in nodes
    assert {"pre_turn", "planner", "narrate", "commit", "done"} <= nodes


class _NullLog:
    """A log that records appends (the compile touches it for the template read)."""

    def __init__(self):
        self.events: list[tuple] = []

    def append_event(self, session_id, event_type, source, payload):
        seq = len(self.events) + 1
        self.events.append((session_id, event_type, source, payload))
        return type("E", (), {"seq": seq, "type": event_type, "payload": payload})()

    def get_events(self, session_id):
        return []

    def get_chain_events(self, session_id):
        return []


def test_a_disabled_turn_runs_the_single_narrator_pass_and_commits(log):
    """Disabled: no lane call, one narrator pass, and the turn is kept."""
    _admit_world(log, ["Kael", "Mara"])
    lane = _LaneCaller()
    narrator = _Narrator()

    _drive(log, _topology(enabled=False), lane_caller=lane, narrator=narrator)

    assert lane.calls == 0  # no lane model call at all
    assert len(narrator.calls) == 1  # the single forward pass
    assert _lane_rows(log) == []
    assert not [
        event for event in log.get_events(SID)
        if isinstance(event.payload, dict)
        and event.payload.get("event") in ("npc_lane", "npc_lanes", "npc_lanes_plan")
    ]
    committed = [
        event for event in log.get_events(SID) if event.type == "narrative"
    ]
    assert committed, "the turn committed normally with zero lanes"


def test_reenabling_the_switch_brings_the_lanes_back(log):
    """Same session, two builds: off writes nothing, on lanes the cast."""
    _admit_world(log, ["Kael"])
    off = _LaneCaller()
    _drive(log, _topology(enabled=False), lane_caller=off)
    assert off.calls == 0 and _lane_rows(log) == []

    on = _LaneCaller()
    _drive(log, _topology(enabled=True), lane_caller=on)
    assert on.calls == 1
    assert [row["name"] for row in _lane_rows(log)] == ["Kael"]


def test_the_switch_is_row_data_and_rides_a_string_false(log):
    """A JSON round trip may hand back ``"false"``; the switch still reads OFF."""
    from fabulist.graph.contracts import stage_disabled

    assert stage_disabled({"enabled": False}) is True
    assert stage_disabled({"enabled": "false"}) is True
    assert stage_disabled({"enabled": 0}) is True
    assert stage_disabled({"enabled": True}) is False
    assert stage_disabled({}) is False
    assert stage_disabled(None) is False


# -- #56.2, the concurrency override ----------------------------------------


def test_the_stored_max_lanes_bounds_concurrency_and_drops_nobody(log):
    """A saved width of 2 runs all five members, two at a time."""
    names = ["Kael", "Mara", "Vess", "Ilse", "Tobin"]
    _admit_world(log, names)
    lane = _LaneCaller(delay=0.02)

    _drive(log, _topology(max_lanes=2), lane_caller=lane)

    assert lane.calls == len(names)  # the SET is never capped
    assert lane.max_in_flight == 2  # the stored width bounds CONCURRENCY
    assert [row["name"] for row in _lane_rows(log)] == names


def test_lane_config_reads_the_row_width_and_a_templates_config_wins():
    """The fan-out's node data is the width; a template's config overrides it."""
    from fabulist.graph.node_templates import NodeTemplate

    topology = _topology(max_lanes=2)
    assert lane_config(topology, {})["max_lanes"] == 2
    assert lane_config(None, {}) == {}

    template = NodeTemplate(
        template_id="tpl_lanes",
        implements="npc_lanes",
        klass="model_pass",
        interaction="npc",
        step_id=None,
        config={"max_lanes": 6},
        name="Cast lanes",
        description="",
    )
    placed = {
        "nodes": [
            {"id": "my_lanes", "kind": "stage", "template": "tpl_lanes"},
            {"id": "narrate", "kind": "stage"},
        ],
        "edges": [{"from": "my_lanes", "to": "narrate"}],
    }
    assert lane_config(placed, {"tpl_lanes": template})["max_lanes"] == 6


# -- the flow surface serves the EFFECTIVE settings --------------------------


def test_lane_settings_read_the_stored_topology(log):
    """#56.2/#56.3: the panel's width and off state are the STORED ones."""
    from fabulist.api.flow_api import lane_settings

    assert lane_settings(log)["max_parallel"] == 4  # canonical default
    assert lane_settings(log)["enabled"] is True

    set_default_topology(log, _topology(max_lanes=2))
    assert lane_settings(log) == {"max_parallel": 2, "enabled": True}

    set_default_topology(log, _topology(max_lanes=2, enabled=False))
    assert lane_settings(log) == {"max_parallel": 2, "enabled": False}


# -- the write door (ADR-009: invalid edits are refused, never stored) -------


@pytest.fixture
def build_app(tmp_path: Path):
    """Apps sharing one tmp event-log path; logs closed after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(**kwargs):
        app = create_app(FakeTurnEngine(), log_path, **kwargs)
        app.include_router(prompts_router)
        app.include_router(topology_router)
        app.include_router(flow_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


@pytest.fixture(autouse=True)
def _isolate_factory_artifact(tmp_path: Path, monkeypatch) -> Path:
    """Keep the write door off the committed artifact (test hermeticity)."""
    path = tmp_path / "prompts" / "factory-defaults.json"
    monkeypatch.setattr(factory, "DEFAULT_ARTIFACT_RELPATH", path)
    return path


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_the_door_persists_the_switch_and_the_engine_reads_it(build_app):
    """An accepted off switch is the graph the engine will compile."""
    build, _ = build_app

    async def scenario():
        app = build()
        async with _client(app) as client:
            put = await client.put(
                "/api/prompts/topology", json={"topology": _topology(max_lanes=2, enabled=False)}
            )
            get = await client.get("/api/prompts/topology")
            manifest = await client.get("/api/flow/manifest")
        return app, put, get, manifest

    app, put, get, manifest = asyncio.run(scenario())
    assert put.status_code == 200, put.text
    body = get.json()
    assert body["violations"] == []
    lanes_node = next(n for n in body["topology"]["nodes"] if n["id"] == "npc_lanes")
    assert lanes_node["data"] == {"max_lanes": 2, "enabled": False}
    # The engine's validated read (what a turn compiles through) carries it too:
    # a disabled switch must not be read as "fall back to canonical".
    carried = preset_topology(
        get_preset(app.state.log, DEFAULT_PRESET_ID),
        lambda _template: None,
    )
    assert next(n for n in carried["nodes"] if n["id"] == "npc_lanes")["data"]["enabled"] is False
    # The served manifest describes what the next turn will run.
    fanout = next(n for n in manifest.json()["graphs"][0]["nodes"] if n["id"] == "npc_lanes")[
        "fanout"
    ]
    assert fanout["max_parallel"] == 2
    assert fanout["enabled"] is False


def test_the_door_refuses_an_off_switch_it_cannot_prune(build_app):
    """#56.4: a disabling edit that would build a broken graph is refused."""
    build, _ = build_app
    broken = _topology(enabled=False)
    # Drop the fan-out edge: the compile could not tell which node the stage
    # fans into, so the prune refuses and the door must too.
    broken["edges"] = [
        edge for edge in broken["edges"] if not (edge["from"] == "npc_lanes" and edge.get("fanout"))
    ]

    async def scenario():
        app = build()
        async with _client(app) as client:
            put = await client.put("/api/prompts/topology", json={"topology": broken})
            get = await client.get("/api/prompts/topology")
        return app, put, get

    app, put, get = asyncio.run(scenario())
    assert put.status_code == 400, put.text
    codes = {violation["code"] for violation in put.json()["violations"]}
    assert "disabled_stage_unprunable" in codes
    # Nothing was written: the stored topology is still the canonical one.
    stored = stored_topology(get_preset(app.state.log, DEFAULT_PRESET_ID))
    assert stored in (None, {}) or not any(
        node.get("data", {}).get("enabled") is False
        for node in stored.get("nodes") or []
    )
    assert get.json()["violations"] == []


def test_validation_judges_the_pruned_graph_not_the_declaration():
    """The pruned wiring is what the invariants are checked against."""
    assert validate_topology(_topology(enabled=False)) == []
    # A disabled stage whose removal would strand the narrator is refused: here
    # the lane node exits somewhere else entirely, so nothing fans back in.
    broken = _topology(enabled=False)
    for edge in broken["edges"]:
        if edge["from"] == "npc_lane":
            edge["to"] = "done"
    codes = {violation["code"] for violation in validate_topology(broken)}
    assert "disabled_stage_unprunable" in codes


def test_the_canonical_topology_json_round_trip_still_validates():
    """A switch written through JSON keeps the topology legal."""
    carried = json.loads(json.dumps(_topology(max_lanes=6, enabled=False)))
    assert validate_topology(carried) == []
