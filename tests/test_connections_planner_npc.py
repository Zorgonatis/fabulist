"""The planner and the NPC lane group are mappable from the connections pane (#55).

Both are D19 INTERACTIONS that resolve their agent per call through
:func:`fabulist.connections.taxonomy.resolve_interaction_agent` — the planner
lane (``PresetAgentCaller("planner", ...)``) and the NPC fan-out
(``PresetAgentCaller("npc", ...)``). Issue #55 asks for them to be mappable with
"granular settings across multiple providers and models", so each is declared as
an ordinary per-item ROLE in the taxonomy:

* ``planner`` -> ``("completion", "narration")``,
* ``npc``     -> ``("completion", "narration")``,

which gives the drawer ONE resolved row per role (the #48 read model, unchanged),
one ``write_slot``, and the ordinary PATCH address. The affinity is what keeps
every existing config serving both unchanged: unauthored, the row resolves
through the narration task (a legacy ``narrator`` key included), which IS the
shipped ``planner -> narrator`` default. Authored, the key wins because
:func:`resolve_interaction_agent` consults the interaction's own slot before the
task leg.

What must NOT change — pinned here: a legacy/typed narration binding keeps
serving the planner and the lanes; a preset binding still outranks the document
(the D19 layer sits above it); and the row model still renders exactly one row
per role.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.connections_api import router
from fabulist.api.fake import FakeTurnEngine
from fabulist.connections import taxonomy
from fabulist.connections.registry import Registry, RegistryStore
from fabulist.prompts.agent_sets import DEFAULT_BINDINGS


def run(coro):
    return asyncio.run(coro)


#: A legacy document: only the historical keys, one connection each.
LEGACY = {"narrator": "writer", "extractor": "cheap"}


def _registry(agents: dict[str, str]) -> Registry:
    names = sorted(set(agents.values()))
    return Registry.from_dict(
        {
            "connections": {
                name: {"base_url": "http://host/v1", "model": name} for name in names
            },
            "agents": dict(agents),
        }
    )


def _rows(agents: dict[str, str]) -> dict[str, dict]:
    return {row["slot"]: row for row in taxonomy.slot_view(agents)}


# --- declared roles -------------------------------------------------------


def test_planner_and_npc_are_declared_item_roles():
    """Both are ordinary declared spellings: PATCH accepts them, no new concept."""
    declared = taxonomy.declared_spellings()
    assert "planner" in declared
    assert "npc" in declared
    assert taxonomy.ITEM_SLOTS["planner"] == ("completion", "narration")
    assert taxonomy.ITEM_SLOTS["npc"] == ("completion", "narration")
    # the labels the drawer shows
    assert taxonomy.SLOT_LABELS["planner"] == "Planner"
    assert taxonomy.SLOT_LABELS["npc"] == "Npc lanes"


def test_each_role_is_one_row_in_the_completion_group():
    rows = _rows(LEGACY)

    for slot, label in (("planner", "Planner"), ("npc", "Npc lanes")):
        row = rows[slot]
        assert row["type"] == "completion"
        assert row["task"] is None
        assert row["label"] == label
        assert row["aliases"] == []
        assert row["write_slot"] == slot
    # exactly one row each — the #48 read model is untouched
    slots = [row["slot"] for row in taxonomy.slot_view(LEGACY)]
    assert slots.count("planner") == 1
    assert slots.count("npc") == 1


def test_unauthored_rows_resolve_through_the_narration_task():
    """Backward compat: a legacy ``narrator`` document keeps serving both."""
    rows = _rows(LEGACY)
    assert rows["planner"]["connection"] == "writer"
    assert rows["planner"]["via"] == "legacy:narrator"
    assert rows["npc"]["connection"] == "writer"
    assert rows["npc"]["via"] == "legacy:narrator"

    typed = _rows({"completion.narration": "typed-writer"})
    assert typed["planner"]["connection"] == "typed-writer"
    assert typed["planner"]["via"] == "type:completion.narration"
    assert typed["npc"]["connection"] == "typed-writer"


def test_the_npc_group_renders_in_its_own_section():
    """Issue #55c: the lane group is a GROUP in the pane, not a completion extra."""
    rows = _rows(LEGACY)
    assert rows["npc"]["group"] == "Npc lanes"
    # the planner is mappable like the others: no section of its own
    assert rows["planner"]["group"] == "completion"
    assert rows["completion.narration"]["group"] == "completion"


def test_nothing_authored_leaves_both_rows_unbound():
    rows = _rows({})
    assert rows["planner"]["connection"] is None
    assert rows["planner"]["via"] is None
    assert rows["npc"]["connection"] is None


# --- the call side: resolution honours the authored key --------------------


def test_authored_planner_key_resolves_the_planner_lane():
    """The planner lane asks for the interaction; the authored key answers."""
    agents = {"narrator": "writer", "planner": "thinker"}
    assert (
        taxonomy.resolve_interaction_agent(None, agents, "planner", "narrator")
        == "planner"
    )
    assert _registry(agents).connection_for("planner").name == "thinker"
    # and the row is the same answer, from the other direction
    assert _rows(agents)["planner"]["connection"] == "thinker"
    assert _rows(agents)["planner"]["via"] == "authored"


def test_authored_npc_key_resolves_the_lane_group():
    """All lanes ride the group key — the fan-out's one caller reads it."""
    agents = {"narrator": "writer", "npc": "cheap-lanes"}
    assert taxonomy.resolve_interaction_agent(None, agents, "npc", "extractor") == "npc"
    assert _registry(agents).connection_for("npc").name == "cheap-lanes"


def test_unauthored_resolution_is_unchanged_for_both_interactions():
    """The legacy document resolves exactly as it did before #55."""
    assert (
        taxonomy.resolve_interaction_agent(None, LEGACY, "planner", "narrator")
        == "narrator"
    )
    assert (
        taxonomy.resolve_interaction_agent(None, LEGACY, "npc", "extractor")
        == "narrator"
    )
    # the shipped D19 defaults still name the narration task / the narrator
    assert DEFAULT_BINDINGS["npc"]["agent"] == "completion.narration"
    assert DEFAULT_BINDINGS["planner"]["agent"] == "narrator"


def test_a_preset_binding_still_outranks_the_document():
    """D19 layer above the taxonomy: an explicit preset choice wins."""
    agents = {"narrator": "writer", "planner": "thinker", "extractor": "cheap"}
    bindings = {"planner": {"agent": "extractor", "overrides": {}}}
    assert (
        taxonomy.resolve_interaction_agent(bindings, agents, "planner", "narrator")
        == "extractor"
    )
    bindings = {"npc": {"agent": "extractor", "overrides": {}}}
    assert taxonomy.resolve_interaction_agent(bindings, agents, "npc", "extractor") == (
        "extractor"
    )


def test_an_authored_item_key_beats_the_task_leg_but_not_a_preset_binding():
    """The precedence the pane promises: preset > authored key > task > type."""
    agents = {
        "narrator": "writer",
        "planner": "thinker",
        "completion.narration": "typed-writer",
    }
    assert (
        taxonomy.resolve_interaction_agent(None, agents, "planner", "narrator")
        == "planner"
    )
    defaults_only = {"narrator": "writer", "completion.narration": "typed-writer"}
    assert (
        taxonomy.resolve_interaction_agent(None, defaults_only, "planner", "narrator")
        == "completion.narration"
    )


# --- the write side: PATCH accepts both, and it takes effect --------------


def _write_config(path: Path) -> Path:
    data = {
        "connections": {
            "writer": {"base_url": "http://localhost:4444/v1", "model": "writer-model"},
            "thinker": {"base_url": "http://localhost:5555/v1", "model": "thinker-model"},
        },
        "agents": {"narrator": "writer", "extractor": "writer"},
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def build_app(tmp_path: Path):
    config_path = _write_config(tmp_path / "connections.json")
    log_path = tmp_path / "events.db"
    store = RegistryStore(config_path, env_path=tmp_path / ".env")
    built = []

    def _build():
        app = create_app(FakeTurnEngine(), log_path)
        app.include_router(router)
        app.state.registry = store
        built.append(app)
        return app

    yield _build, store, config_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.parametrize("slot", ["planner", "npc"])
def test_patch_rebinds_the_role_and_persists_the_key(build_app, slot):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                f"/api/connections/agents/{slot}", json={"connection": "thinker"}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == slot
    assert body["agents"][slot] == "thinker"
    # the LIVE store (the object callers hold) resolves the new binding …
    assert store.connection_for(slot).name == "thinker"
    # … and the document carries the authored key (survives a restart)
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["agents"][slot] == "thinker"
    # the legacy keys are untouched — nothing was renamed or removed
    assert written["agents"]["narrator"] == "writer"


@pytest.mark.parametrize("slot", ["planner", "npc"])
def test_a_rebind_moves_the_resolved_row(build_app, slot):
    build, store, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections")

    before = run(scenario()).json()
    rows = {row["slot"]: row for row in before["slots"]}
    assert rows[slot]["connection"] == "writer"
    assert rows[slot]["via"] == "legacy:narrator"

    async def patch():
        async with client_for(app) as client:
            return await client.patch(
                f"/api/connections/agents/{slot}", json={"connection": "thinker"}
            )

    assert run(patch()).status_code == 200

    after = run(scenario()).json()
    rows = {row["slot"]: row for row in after["slots"]}
    assert rows[slot]["connection"] == "thinker"
    assert rows[slot]["via"] == "authored"
    # the narration row is untouched: mapping the planner/lanes never moves the
    # narrator's own binding
    assert rows["completion.narration"]["connection"] == "writer"


def test_the_write_slot_of_both_rows_is_the_patch_address(build_app):
    """``write_slot`` is what the drawer posts — always a declared spelling."""
    build, _, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections")

    rows = {row["slot"]: row for row in run(scenario()).json()["slots"]}
    for slot in ("planner", "npc"):
        assert rows[slot]["write_slot"] == slot
        assert rows[slot]["write_slot"] in taxonomy.declared_spellings()


def test_a_typed_narration_document_needs_no_migration(build_app):
    """A config that authored ONLY typed keys still serves both roles."""
    rows = _rows({"completion.narration": "typed-writer", "extractor": "cheap"})
    assert rows["planner"]["connection"] == "typed-writer"
    assert rows["npc"]["connection"] == "typed-writer"
    assert _registry({"completion.narration": "typed-writer"}).connection_for(
        "completion.narration"
    ).name == "typed-writer"
