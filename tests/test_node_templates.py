"""Node templates (W9.10, R19/R21): the palette library and template nodes.

A node template is a session-independent ASSET (AGENTS.md NN7) that every
canonical node seeds once; the Studio palette lists them, and a topology node
row that references one executes the behaviour that template ``implements``.
These tests pin the four seams that make that real:

* the store (seed / CRUD / delete guard) on the ``__node_templates__`` anchor,
* the router (``GET/POST/PATCH/DELETE /api/node-templates``) and the palette
  payload the editor groups by ``klass``,
* the builder + contracts resolving a template node to its ``implements``
  behaviour — including a FROM-SCRATCH four-node topology assembled entirely
  from templates, which must compile AND run one turn,
* the canonical topology, which must still build exactly as before.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.node_templates_api import router as node_templates_router
from fabulist.api.prompts_api import router as prompts_router
from fabulist.api.topology_api import router as topology_router
from fabulist.graph import NODE_NAMES, build_graph
from fabulist.graph.contracts import NODE_CONTRACTS, validate_topology
from fabulist.graph.graph import CANONICAL_TOPOLOGY
from fabulist.graph.node_templates import (
    NODE_TEMPLATES_ANCHOR,
    NODE_TEMPLATE_EVENT,
    ensure_node_templates,
    list_node_templates,
    template_behaviours,
)
from fabulist.log import EventLog
from fabulist.prompts import preset_store

#: The from-scratch topology every execution test builds: four nodes, every one
#: of them a TEMPLATE reference with an owner-chosen id.
FROM_SCRATCH: dict = {
    "nodes": [
        {"id": "open", "kind": "stage", "template": "tpl_pre_turn"},
        {"id": "speak", "kind": "stage", "template": "tpl_narrate"},
        {"id": "keep", "kind": "stage", "template": "tpl_commit"},
        {"id": "end", "kind": "terminal", "template": "tpl_done"},
    ],
    "edges": [
        {"from": "open", "to": "speak"},
        {"from": "speak", "to": "keep"},
        {"from": "keep", "to": "end"},
    ],
}


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


def _extractor(user_msg, prose):
    return {"deltas": {}, "action_class": "dialogue"}


@pytest.fixture
def log(tmp_path: Path):
    repo = EventLog(tmp_path / "events.db")
    yield repo
    repo.close()


@pytest.fixture
def build_app(tmp_path: Path):
    """Apps sharing one tmp event-log path; logs closed after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(**kwargs):
        app = create_app(FakeTurnEngine(), log_path, **kwargs)
        app.include_router(prompts_router)
        app.include_router(topology_router)
        app.include_router(node_templates_router)
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# -- the store: seeding ------------------------------------------------------


def test_seed_library_covers_every_canonical_node(log):
    """Every canonical node has a built-in template, derived from the code."""
    templates = list_node_templates(log)
    assert [t.implements for t in templates] == [
        node["id"] for node in CANONICAL_TOPOLOGY["nodes"]
    ]
    assert [t.template_id for t in templates] == [
        f"tpl_{node['id']}" for node in CANONICAL_TOPOLOGY["nodes"]
    ]
    by_id = {t.template_id: t for t in templates}
    narrate = by_id["tpl_narrate"]
    assert narrate.klass == NODE_CONTRACTS["narrate"].klass == "model_pass"
    assert narrate.step_id == "narrate"  # the W6 prompt door
    assert narrate.interaction == "narrate"
    assert by_id["tpl_done"].klass == "terminal"
    assert by_id["tpl_pre_turn"].klass == "deterministic"
    # Node data rides along as template config (the fan-out's lane cap).
    assert by_id["tpl_npc_lanes"].config == {"max_lanes": 4}
    assert all(t.builtin for t in templates)
    # W9.23 §3.6.4: the W9.22 proposal stage's seeds are GONE with the stage (its
    # behaviour ids left NODE_CONTRACTS), so the library seeds exactly the
    # canonical turn's nodes.
    assert "tpl_asset_proposals" not in by_id
    assert "tpl_asset_proposal_lane" not in by_id


def test_a_template_for_a_retired_behaviour_leaves_the_palette(log):
    """W9.23 §3.6.4: a stored dead row stays in the log, not in the palette.

    Seeding is seed-only-if-missing and nothing deletes stored rows, so a log
    written while the W9.22 proposal stage existed still carries its two seeded
    templates. The library READS filter on ``implements in NODE_CONTRACTS``, so
    the palette never offers a node the builder cannot place — and the row stays
    addressable and deletable so it can still be cleaned up.
    """
    from fabulist.graph.node_templates import (
        delete_node_template,
        get_node_template,
        library_rows,
    )

    log.append_event(
        NODE_TEMPLATES_ANCHOR,
        "system",
        "user",
        {
            "event": NODE_TEMPLATE_EVENT,
            "id": "tpl_asset_proposals",
            "name": "Asset proposals",
            "klass": "deterministic",
            "implements": "asset_proposals",
            "interaction": "proposals",
            "step_id": "proposals",
            "config": {"max_lanes": 3},
            "description": "retired in W9.23",
            "builtin": True,
        },
    )

    assert "tpl_asset_proposals" not in [
        template.template_id for template in list_node_templates(log)
    ]
    assert "tpl_asset_proposals" not in template_behaviours(log)
    rows = library_rows(
        {template.template_id: template for template in ensure_node_templates(log)}
    )
    assert "tpl_asset_proposals" not in [row["id"] for row in rows]
    # ...and it is still readable by id and deletable, so an owner can clean up.
    assert get_node_template(log, "tpl_asset_proposals") is not None
    delete_node_template(log, "tpl_asset_proposals")
    assert get_node_template(log, "tpl_asset_proposals") is None


def test_seed_is_idempotent(log):
    """Seeding twice writes nothing the second time (seed-only-if-missing)."""

    def _writes() -> list:
        return [
            event
            for event in log.get_events(NODE_TEMPLATES_ANCHOR)
            if (event.payload or {}).get("event") == NODE_TEMPLATE_EVENT
        ]

    first = ensure_node_templates(log)
    written = _writes()
    second = ensure_node_templates(log)
    assert [t.template_id for t in second] == [t.template_id for t in first]
    assert len(_writes()) == len(written) == len(first)


# -- the router: CRUD + palette payload --------------------------------------


def test_node_template_crud_round_trip(build_app):
    """POST -> GET -> PATCH -> DELETE, all sessionless (no chat open)."""

    async def _run():
        async with client_for(build_app()) as client:
            created = await client.post(
                "/api/node-templates",
                json={
                    "name": "Second narrator",
                    "klass": "model_pass",
                    "implements": "narrate",
                    "interaction": "narrate",
                    "step_id": "narrate",
                    "config": {"temperature": 0.4},
                    "description": "A colder narrator pass.",
                },
            )
            assert created.status_code == 200
            template = created.json()["template"]
            assert template["id"].startswith("tpl_")
            assert template["implements"] == "narrate"
            assert template["builtin"] is False

            fetched = await client.get(f"/api/node-templates/{template['id']}")
            assert fetched.status_code == 200
            assert fetched.json()["template"] == template

            patched = await client.patch(
                f"/api/node-templates/{template['id']}",
                json={"name": "Cold narrator", "config": {"temperature": 0.2}},
            )
            assert patched.status_code == 200
            updated = patched.json()["template"]
            assert updated["name"] == "Cold narrator"
            assert updated["config"] == {"temperature": 0.2}
            # An omitted field keeps its stored value (partial merge).
            assert updated["implements"] == "narrate"

            deleted = await client.delete(f"/api/node-templates/{template['id']}")
            assert deleted.status_code == 200
            assert deleted.json() == {"deleted": template["id"]}
            assert (
                await client.get(f"/api/node-templates/{template['id']}")
            ).status_code == 404

    run(_run())


def test_delete_is_refused_while_a_stored_topology_references_it(build_app):
    """409 (naming the preset) instead of a dangling reference; nothing written."""

    async def _run():
        app = build_app()
        preset_store.set_default_topology(app.state.log, FROM_SCRATCH)
        async with client_for(app) as client:
            refused = await client.delete("/api/node-templates/tpl_narrate")
            assert refused.status_code == 409
            detail = refused.json()["detail"]
            assert "referenced" in detail["reason"]
            assert detail["presets"] == [preset_store.DEFAULT_PRESET_ID]
            # The template is still there (a refusal writes nothing).
            assert (
                await client.get("/api/node-templates/tpl_narrate")
            ).status_code == 200

            # Unreferenced built-ins remain deletable, and the delete STICKS.
            assert (
                await client.delete("/api/node-templates/tpl_env_hud")
            ).status_code == 200
            listed = (await client.get("/api/node-templates")).json()
            ids = [row["id"] for row in listed["templates"]]
            assert "tpl_env_hud" not in ids

    run(_run())


def test_palette_payload_groups_by_klass(build_app):
    """The palette's payload: ready rows + the klass vocabulary, never empty."""

    async def _run():
        async with client_for(build_app()) as client:
            response = await client.get("/api/node-templates")
            assert response.status_code == 200
            body = response.json()
            assert body["klasses"] == [
                "model_pass",
                "gate",
                "deterministic",
                "terminal",
            ]
            templates = body["templates"]
            assert len(templates) == len(CANONICAL_TOPOLOGY["nodes"])
            for row in templates:
                assert set(row) == {
                    "id",
                    "name",
                    "klass",
                    "implements",
                    "interaction",
                    "step_id",
                    "config",
                    "description",
                    "builtin",
                }
                assert row["klass"] in body["klasses"]
                assert row["implements"] in NODE_CONTRACTS
                assert row["name"]
            grouped = {
                klass: [row["id"] for row in templates if row["klass"] == klass]
                for klass in body["klasses"]
            }
            assert grouped["terminal"] == ["tpl_done"]
            assert "tpl_narrate" in grouped["model_pass"]

    run(_run())


def test_create_refuses_a_template_that_implements_nothing_runnable(build_app):
    async def _run():
        async with client_for(build_app()) as client:
            response = await client.post(
                "/api/node-templates",
                json={"name": "Ghost", "klass": "model_pass", "implements": "ghost"},
            )
            assert response.status_code == 422
            assert response.json()["detail"]["known_implements"] == list(
                NODE_CONTRACTS
            )

    run(_run())


# -- the builder -------------------------------------------------------------


def test_builder_resolves_a_template_node_to_its_implements_behaviour(tmp_path):
    """A template node executes its template's factory under the row's own id."""
    log = EventLog(tmp_path / "resolve.db")
    try:
        topology = {
            "nodes": [
                {"id": "open", "template": "tpl_pre_turn"},
                {"id": "speak", "template": "tpl_narrate"},
                {"id": "end", "template": "tpl_done"},
            ],
            "edges": [
                {"from": "open", "to": "speak"},
                {"from": "speak", "to": "end"},
            ],
        }
        graph = build_graph(
            model=ChannelModel(),
            extractor=_extractor,
            log=log,
            topology=topology,
            templates=template_behaviours(log),
        )
        drawable = graph.get_graph()
        assert [n for n in drawable.nodes if not n.startswith("__")] == [
            "open",
            "speak",
            "end",
        ]
        assert ("open", "speak") in {(e.source, e.target) for e in drawable.edges}
    finally:
        log.close()


def test_unknown_template_is_rejected_naming_both(tmp_path):
    log = EventLog(tmp_path / "unknown.db")
    try:
        topology = {
            "nodes": [
                {"id": "open", "template": "tpl_pre_turn"},
                {"id": "speak", "template": "tpl_nowhere"},
            ],
            "edges": [{"from": "open", "to": "speak"}],
        }
        with pytest.raises(ValueError) as excinfo:
            build_graph(
                model=ChannelModel(),
                extractor=_extractor,
                log=log,
                topology=topology,
                templates=template_behaviours(log),
            )
        message = str(excinfo.value)
        assert "tpl_nowhere" in message and "speak" in message
    finally:
        log.close()


def test_template_implements_without_a_factory_is_rejected_naming_both(log):
    """A template may name a behaviour the code has no stage for — refuse it."""
    topology = {
        "nodes": [{"id": "open", "template": "tpl_custom"}],
        "edges": [],
    }
    with pytest.raises(ValueError) as excinfo:
        build_graph(
            model=ChannelModel(),
            extractor=_extractor,
            log=log,
            topology=topology,
            templates={"tpl_custom": {"implements": "ghost_stage"}},
        )
    message = str(excinfo.value)
    assert "tpl_custom" in message and "ghost_stage" in message and "open" in message


# -- contracts ---------------------------------------------------------------


def test_contracts_judge_a_template_node_by_its_implements_behaviour(log):
    """The same invariants as the canonical ids, under the owner's own labels."""
    library = template_behaviours(log)
    assert validate_topology(FROM_SCRATCH, library) == []

    # narrate requires ``facts``; a template graph without a pre-turn producer
    # violates exactly the invariant the canonical node would.
    no_pre_turn = {
        "nodes": [
            {"id": "speak", "template": "tpl_narrate"},
            {"id": "end", "template": "tpl_done"},
        ],
        "edges": [{"from": "speak", "to": "end"}],
    }
    violations = validate_topology(no_pre_turn, library)
    assert [(v["code"], v["node"]) for v in violations] == [
        ("unsatisfied_requirement", "speak")
    ]
    assert "facts" in violations[0]["message"]


def test_contracts_reject_an_unresolved_template_naming_both(log):
    topology = {
        "nodes": [
            {"id": "open", "template": "tpl_pre_turn"},
            {"id": "speak", "template": "tpl_ghost"},
            {"id": "end", "template": "tpl_done"},
        ],
        "edges": [{"from": "open", "to": "speak"}, {"from": "speak", "to": "end"}],
    }
    violations = validate_topology(topology, template_behaviours(log))
    # The unresolved node drops out like any unknown id (so its edges orphan and
    # it is unreachable); the code that owns the reason is ``unknown_node``.
    assert violations[0]["code"] == "unknown_node"
    assert violations[0]["node"] == "speak"
    assert "tpl_ghost" in violations[0]["message"]


# -- execution ---------------------------------------------------------------


def test_from_scratch_template_topology_compiles_and_runs_a_turn(tmp_path):
    """Four nodes, four templates, owner-chosen ids: it compiles AND runs."""
    from fabulist.graph import GraphTurnEngine

    log = EventLog(tmp_path / "scratch.db")
    try:
        library = template_behaviours(log)
        model = ChannelModel("The lamp gutters.")
        graph = build_graph(
            model=model,
            extractor=_extractor,
            log=log,
            topology=FROM_SCRATCH,
            templates=library,
        )

        async def _turn():
            engine = GraphTurnEngine(graph=graph, log=log, model=model)
            return await collect(engine.run_turn("s1", "I wait.", "in_character"))

        events = run(_turn())
        assert events[-1]["type"] == "done"
        # The narrator pass ran (the template executed ``narrate``'s factory)...
        assert model.calls, "the narrate template never called the model"
        # ...and the commit template wrote the turn to the log.
        narratives = [e for e in log.get_events("s1") if e.type == "narrative"]
        assert len(narratives) == 1
        assert narratives[0].payload["prose"] == "The lamp gutters."
    finally:
        log.close()


def test_canonical_topology_still_builds_identically(log):
    """No templates in play: the compiled graph is what it always was."""
    drawable = build_graph(model=ChannelModel(), extractor=_extractor, log=log).get_graph()
    nodes = [node for node in drawable.nodes if not node.startswith("__")]
    assert nodes == list(NODE_NAMES)
    plain = {(e.source, e.target) for e in drawable.edges if not e.conditional}
    conditional = {(e.source, e.target, e.data) for e in drawable.edges if e.conditional}
    for source, target in zip(NODE_NAMES, NODE_NAMES[1:]):
        # the conditional sources: the critic's verdicts, the two Send fan-outs
        # (NPC lanes W9.17, asset proposals W9.22) and the W9.20 plan junction
        # on pre_turn
        if source in (
            "critic",
            "npc_lanes",
            "decision_gate",
            "pre_turn",
        ):
            continue
        assert (source, target) in plain
    # W9.24 §3.3.7: the retired ``critic -> narrate [fail]`` regeneration edge is
    # not in the canonical graph; the critic keeps only its ok/diffs fan-out.
    assert ("critic", "narrate", "fail") not in conditional
    assert ("critic", "bts", "diffs") in conditional
    assert ("npc_lanes", "npc_lane", None) in conditional
    assert ("npc_lanes", "narrate", None) in conditional
    assert ("attribution", "done") in plain
    assert ("pre_turn", "planner", "plan") in conditional
    assert ("pre_turn", "npc_lanes", "skip") in conditional


# -- the write door ----------------------------------------------------------


def test_topology_door_accepts_and_serves_a_template_topology(build_app):
    """PUT stores a template graph; GET validates it against the library."""

    async def _run():
        async with client_for(build_app()) as client:
            put = await client.put(
                "/api/prompts/topology", json={"topology": FROM_SCRATCH}
            )
            assert put.status_code == 200, put.text
            assert put.json()["violations"] == []
            stored = await client.get("/api/prompts/topology")
            assert stored.json()["topology"]["nodes"] == FROM_SCRATCH["nodes"]
            assert stored.json()["violations"] == []

            # An unknown template is refused at the door, nothing written.
            broken = {
                "nodes": [{"id": "speak", "template": "tpl_ghost"}],
                "edges": [],
            }
            refused = await client.put(
                "/api/prompts/topology", json={"topology": broken}
            )
            assert refused.status_code == 400
            assert "unknown_node" in refused.json()["detail"]
            # The stored graph is still the legal one.
            again = await client.get("/api/prompts/topology")
            assert again.json()["topology"]["nodes"] == FROM_SCRATCH["nodes"]

    run(_run())


def test_lane_config_follows_the_template_not_the_node_id(log):
    """A from-scratch graph may place the fan-out under any node id (W9.10)."""
    from fabulist.graph.node_templates import lane_config

    tpl = SimpleNamespace(
        template_id="tpl_fan", implements="npc_lanes", config={"max_lanes": 2}
    )
    topology = {
        "nodes": [
            {"id": "pre_turn"},
            {"id": "call_the_cast", "template": "tpl_fan", "data": {"max_lanes": 7}},
            {"id": "narrate"},
        ],
        "edges": [],
    }

    # The template's config is authoritative over the row's data.
    assert lane_config(topology, {"tpl_fan": tpl}) == {"max_lanes": 2}
    # A plain canonical row still reads its own data.
    plain = {"nodes": [{"id": "npc_lanes", "data": {"max_lanes": 5}}], "edges": []}
    assert lane_config(plain, {}) == {"max_lanes": 5}
    # No fan-out node at all: no config is invented.
    assert lane_config({"nodes": [{"id": "narrate"}], "edges": []}, {}) == {}


def test_library_rows_carry_the_fields_that_change_behaviour(log):
    """The rebuild digest must see a template EDIT, not just its existence."""
    from fabulist.graph.node_templates import library_rows, list_node_templates

    library = {tpl.template_id: tpl for tpl in list_node_templates(log)}
    rows = library_rows(library)
    first = rows[0]
    assert set(first) == {"id", "implements", "klass", "interaction", "step_id", "config"}

    target = next(tpl for tpl in list_node_templates(log) if tpl.implements == "narrate")
    before = library_rows({target.template_id: target})
    edited = SimpleNamespace(**{**vars(target), "config": {"note": "changed"}})
    after = library_rows({target.template_id: edited})
    assert json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True)
