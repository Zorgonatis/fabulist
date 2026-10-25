"""Node contracts + the topology write door (W9.5, R23).

``validate_topology`` is the editor's type system and ``PUT /api/prompts/topology``
is its door: a wiring that breaks an invariant is REJECTED (400, nothing
written), a legal rewire persists as preset data, and a hand-edited/broken
stored topology falls back to the canonical graph at read — the engine never
boots a graph its builder would refuse.

Each violation code gets its own test, so the contract table and the code list
cannot drift apart silently. A few codes legitimately co-fire (an unreachable
node has no upstream producers either); those tests assert the code they own is
present, not that it is alone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.prompts_api import router as prompts_router
from fabulist.api.topology_api import router as topology_router
from fabulist.graph.contracts import (
    NODE_CONTRACTS,
    ROOT_ARTIFACTS,
    VIOLATION_CODES,
    validate_topology,
)
from fabulist.graph.graph import CANONICAL_TOPOLOGY, NODE_NAMES
from fabulist.log import EventLog
from fabulist.prompts import factory
from fabulist.prompts.preset_store import (
    DEFAULT_PRESET_ID,
    PRESETS_ANCHOR,
    append_preset,
    canonical_topology,
    default_preset,
    ensure_default_preset,
    get_preset,
    preset_from_json,
    preset_to_json,
    preset_topology,
    stored_topology,
)


def run(coro):
    return asyncio.run(coro)


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.fixture
def build_app(tmp_path: Path):
    """Apps sharing one tmp event-log path; logs closed after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(prompts_router)
        app.include_router(topology_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


@pytest.fixture(autouse=True)
def _isolate_factory_artifact(tmp_path: Path, monkeypatch) -> Path:
    """Keep the write door off the committed artifact (test hermeticity).

    Same discipline as ``tests/test_default_preset.py``: a real
    ``prompts/factory-defaults.json`` must not become test input when the PUT
    seeds a missing default row.
    """
    path = tmp_path / "prompts" / "factory-defaults.json"
    monkeypatch.setattr(factory, "DEFAULT_ARTIFACT_RELPATH", path)
    return path


# -- builders ----------------------------------------------------------------


def _codes(violations) -> list[str]:
    return [violation["code"] for violation in violations]


#: The canonical spine as ``(from, to)`` pairs, for building legal reorders.
_SPINE: list[tuple[str, str]] = [
    (edge["from"], edge["to"]) for edge in CANONICAL_TOPOLOGY["edges"]
]


def _topology(
    nodes: list[str],
    edges: list[tuple[str, str]],
    extras: dict[tuple[str, str], dict] | None = None,
) -> dict:
    """A topology from node ids + ``(from, to)`` pairs (+ per-pair edge extras)."""
    extra = extras or {}
    return {
        "nodes": [{"id": node_id, "kind": "stage"} for node_id in nodes],
        "edges": [
            {"from": source, "to": target, **extra.get((source, target), {})}
            for source, target in edges
        ],
    }


def _edge_rows(pairs: list[tuple[str, str]]) -> list[dict]:
    """``(from, to)`` tuples in the topology's stored edge shape."""
    return [{"from": source, "to": target} for source, target in pairs]


def _rewired(
    pairs: list[tuple[str, str]],
    drop: list[tuple[str, str]],
    add: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """``pairs`` minus ``drop`` plus ``add`` — one rewire of the canonical spine."""
    kept = [pair for pair in pairs if pair not in drop]
    return [*kept, *add]


#: The canonical wiring with attribution moved ahead of env_hud — legal by the
#: contracts (attribution needs the surviving prose, env_hud the bts state, and
#: bts precedes both), which is exactly the reorder the editor must allow.
_ATTRIBUTION_FIRST = _rewired(
    _SPINE,
    drop=[("bts", "env_hud"), ("env_hud", "attribution"), ("attribution", "done")],
    add=[("bts", "attribution"), ("attribution", "env_hud"), ("env_hud", "done")],
)


# -- the contract table ------------------------------------------------------


def test_every_canonical_node_has_a_contract():
    assert set(NODE_CONTRACTS) == set(NODE_NAMES)
    assert NODE_NAMES == tuple(NODE_CONTRACTS)  # same declaration order
    for contract in NODE_CONTRACTS.values():
        assert contract.klass in ("model_pass", "gate", "deterministic", "terminal")


def test_canonical_topology_validates_clean():
    """The engine's own graph must pass its own type system."""
    assert validate_topology(CANONICAL_TOPOLOGY) == []


def test_artifact_dependencies_are_declared_somewhere():
    """Every required artifact is either a root input or some node's output."""
    provided = {name for c in NODE_CONTRACTS.values() for name in c.provides}
    required = {name for c in NODE_CONTRACTS.values() for name in c.requires}
    assert required <= provided | ROOT_ARTIFACTS
    # ...and the classes say what the nodes are: the critic yields the surviving
    # prose bts folds, the HUD reads the bts state, done only terminates.
    assert "surviving_prose" in NODE_CONTRACTS["critic"].provides
    assert "surviving_prose" in NODE_CONTRACTS["bts"].requires
    assert "bts_state" in NODE_CONTRACTS["env_hud"].requires
    assert NODE_CONTRACTS["done"].klass == "terminal"
    assert NODE_CONTRACTS["done"].requires == ()


def test_the_violation_code_list_is_stable():
    assert set(VIOLATION_CODES) == {
        "unknown_node",
        "duplicate_node",
        # W9.17: one branch value routes to one target; a fan-out edge repeats.
        "duplicate_condition",
        "missing_entry",
        "missing_terminal",
        "unsatisfied_requirement",
        "causal_survivor_broken",
        "unreachable_node",
        "orphan_edge",
        "cap_violation",
    }
    for violation in validate_topology({"nodes": [{"id": "ghost"}], "edges": []}):
        assert set(violation) >= {"code", "message"}
        assert violation["code"] in VIOLATION_CODES


# -- violation codes: one test each ------------------------------------------


def test_unknown_node_code():
    topology = _topology([*NODE_NAMES, "ghost_stage"], _SPINE)
    violations = validate_topology(topology)
    assert _codes(violations) == ["unknown_node"]
    assert violations[0]["node"] == "ghost_stage"


def test_duplicate_node_code():
    nodes = [{"id": node_id, "kind": "stage"} for node_id in NODE_NAMES]
    nodes.append({"id": "narrate", "kind": "stage"})
    violations = validate_topology({"nodes": nodes, "edges": _edge_rows(_SPINE)})
    assert _codes(violations) == ["duplicate_node"]
    assert violations[0]["node"] == "narrate"


def test_missing_entry_code():
    """The node the builder starts from must not be fed by another node."""
    violations = validate_topology(
        _topology(list(NODE_NAMES), [*_SPINE, ("narrate", "pre_turn")])
    )
    assert _codes(violations) == ["missing_entry"]
    assert violations[0]["node"] == "pre_turn"


def test_missing_entry_and_terminal_for_an_empty_topology():
    assert _codes(validate_topology({"nodes": [], "edges": []})) == [
        "missing_entry",
        "missing_terminal",
    ]


def test_missing_terminal_code():
    """The graph must END at a terminal node (``done``), not mid-pipeline."""
    nodes = ["pre_turn", "narrate", "commit", "done", "env_hud"]
    edges = [("pre_turn", "narrate"), ("narrate", "commit"), ("commit", "done")]
    violations = validate_topology(_topology(nodes, edges))
    assert "missing_terminal" in _codes(violations)
    assert any(
        v.get("node") == "env_hud"
        for v in violations
        if v["code"] == "missing_terminal"
    )


def test_unsatisfied_requirement_code():
    """narrate needs ``facts``; with no pre_turn, nothing provides them."""
    violations = validate_topology(_topology(["narrate", "done"], [("narrate", "done")]))
    assert _codes(violations) == ["unsatisfied_requirement"]
    assert violations[0]["node"] == "narrate"
    assert "facts" in violations[0]["message"]


def test_causal_survivor_broken_code():
    """bts folds the SURVIVING prose: it may not run before the critic.

    The data dependency (bts requires ``surviving_prose``) fires alongside, by
    design: the ordering constraint and the artifact lookup are one fact stated
    twice, so a client that ignores one code cannot slip past the other.
    """
    nodes = ["pre_turn", "narrate", "commit", "validate", "bts", "critic", "done"]
    edges = [
        ("pre_turn", "narrate"),
        ("narrate", "commit"),
        ("commit", "validate"),
        ("validate", "bts"),
        ("bts", "critic"),
        ("critic", "done"),
    ]
    codes = _codes(validate_topology(_topology(nodes, edges)))
    assert "causal_survivor_broken" in codes
    assert "unsatisfied_requirement" in codes


def test_unreachable_node_code():
    """A declared node nothing routes into is dead weight, not a stage."""
    nodes = ["pre_turn", "narrate", "commit", "done", "planner"]
    edges = [("pre_turn", "narrate"), ("narrate", "commit"), ("commit", "done")]
    violations = validate_topology(_topology(nodes, edges))
    assert "unreachable_node" in _codes(violations)
    assert any(
        v.get("node") == "planner"
        for v in violations
        if v["code"] == "unreachable_node"
    )


def test_orphan_edge_code():
    violations = validate_topology(
        _topology(list(NODE_NAMES), [*_SPINE, ("done", "ghost_stage")])
    )
    assert _codes(violations) == ["orphan_edge"]
    assert violations[0]["edge"] == {"from": "done", "to": "ghost_stage"}


def test_cap_violation_code():
    """A branch edge whose cap is below 1 can never run.

    (The pair used to be ``critic -> narrate``, the retired regeneration edge;
    W9.24 §3.3.7 removed it, so the pin rides the critic's keep route, which
    appears once in the spine.)
    """
    topology = _topology(
        list(NODE_NAMES), _SPINE, extras={("decision_gate", "critic"): {"cap": 0}}
    )
    violations = validate_topology(topology)
    assert _codes(violations) == ["cap_violation"]
    assert violations[0]["edge"]["cap"] == 0


# -- the write door ----------------------------------------------------------


def test_get_serves_the_carried_topology_and_the_canonical_one(build_app):
    build, _ = build_app
    app = build()
    ensure_default_preset(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/topology")

    response = run(scenario())
    assert response.status_code == 200
    body = response.json()
    assert body["topology"] == canonical_topology()
    assert body["canonical"] == canonical_topology()
    assert body["violations"] == []


def test_put_persists_a_legal_reorder_and_reads_back(build_app):
    """A legal rewire (attribution before env_hud) survives the round trip."""
    build, _ = build_app
    app = build()
    ensure_default_preset(app.state.log)
    reorder = {
        "nodes": [dict(node) for node in CANONICAL_TOPOLOGY["nodes"]],
        "edges": _edge_rows(_ATTRIBUTION_FIRST),
    }

    async def scenario():
        async with client_for(app) as client:
            put = await client.put("/api/prompts/topology", json={"topology": reorder})
            got = await client.get("/api/prompts/topology")
            return put, got

    put, got = run(scenario())
    assert put.status_code == 200
    assert put.json()["violations"] == []
    assert put.json()["topology"] == reorder
    assert got.json()["topology"] == reorder
    assert got.json()["violations"] == []
    # ...and it is the topology the engine would build (validated, no fallback).
    assert preset_topology(default_preset(app.state.log)) == reorder


def test_put_refuses_an_invalid_topology_without_writing(build_app):
    build, _ = build_app
    app = build()
    ensure_default_preset(app.state.log)
    before = len(app.state.log.get_events(PRESETS_ANCHOR))
    broken = _topology(list(NODE_NAMES), [*_SPINE, ("done", "ghost_stage")])

    async def scenario():
        async with client_for(app) as client:
            return await client.put("/api/prompts/topology", json={"topology": broken})

    response = run(scenario())
    assert response.status_code == 400
    body = response.json()
    assert "orphan_edge" in body["detail"]
    assert _codes(body["violations"]) == ["orphan_edge"]
    # Nothing was written: the log's preset events are unchanged.
    assert len(app.state.log.get_events(PRESETS_ANCHOR)) == before


def test_put_creates_the_default_row_when_it_was_never_seeded(build_app):
    """The write door needs no prior boot: it seeds the row it edits."""
    build, _ = build_app
    app = build()
    assert default_preset(app.state.log) is None
    reorder = _topology(list(NODE_NAMES), _ATTRIBUTION_FIRST)

    async def scenario():
        async with client_for(app) as client:
            return await client.put("/api/prompts/topology", json={"topology": reorder})

    response = run(scenario())
    assert response.status_code == 200
    row = get_preset(app.state.log, DEFAULT_PRESET_ID)
    assert row is not None
    assert row.is_default is True
    assert stored_topology(row) == reorder


# -- the read fallback (the engine must still boot) --------------------------


def test_an_invalid_stored_topology_falls_back_to_the_canonical_one():
    """A hand-edited/broken value must never reach ``build_graph``."""
    broken = _topology(
        ["pre_turn", "narrate", "ghost_stage", "done"],
        [("pre_turn", "narrate"), ("narrate", "ghost_stage"), ("ghost_stage", "done")],
    )
    preset = preset_from_json({"preset_id": "hand", "name": "Hand", "topology": broken})
    assert stored_topology(preset) == broken  # the editor still sees it...
    assert preset_topology(preset) == canonical_topology()  # ...the engine does not


def test_a_stored_topology_that_breaks_a_dependency_falls_back(tmp_path: Path):
    """Well-formed but invariant-breaking (bts before critic) also falls back."""
    log = EventLog(tmp_path / "broken.db")
    try:
        nodes = ["pre_turn", "narrate", "commit", "validate", "bts", "critic", "done"]
        edges = [
            ("pre_turn", "narrate"),
            ("narrate", "commit"),
            ("commit", "validate"),
            ("validate", "bts"),
            ("bts", "critic"),
            ("critic", "done"),
        ]
        preset_id = append_preset(
            log,
            {
                "name": "broken",
                "modules": [],
                "topology": _topology(nodes, edges),
            },
        )
        row = get_preset(log, preset_id)
        assert stored_topology(row) == _topology(nodes, edges)
        assert preset_topology(row) == canonical_topology()
    finally:
        log.close()


def test_preset_put_and_patch_keep_the_stored_topology(build_app):
    """A module edit must not silently rewire the engine back to canonical."""
    build, _ = build_app
    app = build()
    ensure_default_preset(app.state.log)
    reorder = _topology(list(NODE_NAMES), _ATTRIBUTION_FIRST)

    async def scenario():
        async with client_for(app) as client:
            put_topology = await client.put(
                "/api/prompts/topology", json={"topology": reorder}
            )
            patched = await client.patch(
                f"/api/prompts/presets/{DEFAULT_PRESET_ID}", json={"name": "Default"}
            )
            after_patch = await client.get("/api/prompts/topology")
            row = preset_to_json(get_preset(app.state.log, DEFAULT_PRESET_ID))
            replaced = await client.put(
                f"/api/prompts/presets/{DEFAULT_PRESET_ID}", json={"name": "Default"}
            )
            after_replace = await client.get("/api/prompts/topology")
            return put_topology, patched, after_patch, row, replaced, after_replace

    put_topology, patched, after_patch, row, replaced, after_replace = run(scenario())
    assert put_topology.status_code == 200
    assert patched.status_code == 200
    assert patched.json()["preset"]["topology"] == reorder
    assert after_patch.json()["topology"] == reorder
    assert row["topology"] == reorder
    assert replaced.status_code == 200
    assert after_replace.json()["topology"] == reorder


def test_preset_routes_refuse_an_illegal_topology(build_app):
    """Every topology write is validated, not just the topology door (R23)."""
    build, _ = build_app
    app = build()
    ensure_default_preset(app.state.log)
    broken = _topology(list(NODE_NAMES), [*_SPINE, ("done", "ghost_stage")])

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                f"/api/prompts/presets/{DEFAULT_PRESET_ID}",
                json={"topology": broken},
            )
            replaced = await client.put(
                f"/api/prompts/presets/{DEFAULT_PRESET_ID}",
                json={"name": "Default", "topology": broken},
            )
            return patched, replaced

    patched, replaced = run(scenario())
    for response in (patched, replaced):
        assert response.status_code == 400
        assert _codes(response.json()["detail"]["violations"]) == ["orphan_edge"]
    # The stored wiring is untouched (still the canonical seed).
    assert stored_topology(default_preset(app.state.log)) == canonical_topology()
