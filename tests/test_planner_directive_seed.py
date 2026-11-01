"""Issue #105 (Option 2C): the planner directive becomes preset data.

The planner was the last model-facing prompt that lived only in a Python file:
``DEFAULT_PLANNER_DIRECTIVE`` behind an EMPTY factory ``planner_system`` row.
ADR-006 rules that every text contributing tokens is editable data carried by
the active preset. This module pins the closing move, end to end:

* the COMMITTED factory artifact carries the directive byte for byte (so the
  sent prompt does not change — only its home does), and its flow row ORDER is
  unchanged (the seed write path can reorder rows);
* the code constant stays the fallback for a call with no preset surface;
* the factory version was bumped, so existing installs get the one-time
  "factory defaults changed — review/reset" notice instead of a silent
  overwrite;
* ``GET /api/prompts/steps`` serves ``system_default`` so the block editor can
  pre-fill an empty row (no install keeps a blind box);
* the seeding script covers the planner row and fails loudly on a reorder.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.prompts_api import router
from fabulist.graph.nodes import DEFAULT_PLANNER_DIRECTIVE
from fabulist.prompts import factory, preset_store
from fabulist.prompts.modules import DEFAULT_FLOWS

REPO_ROOT = Path(__file__).resolve().parents[1]
FACTORY_ARTIFACT = REPO_ROOT / "prompts" / "factory-defaults.json"
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_prompt_defaults.py"

#: The planner flow's one authored row (mirrors
#: ``scripts/seed_prompt_defaults.py:FLOW_SYSTEM_MODULE_ID["planner"]``).
PLANNER_ROW_ID = "planner_system"

_ARTIFACT_DRIFT_FIX = (
    "the SHIPPED planner directive in prompts/factory-defaults.json no longer "
    "matches the engine: run `uv run python scripts/seed_prompt_defaults.py "
    "--force` to re-seed it from DEFAULT_PLANNER_DIRECTIVE, then re-check the "
    "prompt-flow row ORDER (the seed write path can reorder rows)."
)


def run(coro):
    return asyncio.run(coro)


def _artifact() -> dict:
    return json.loads(FACTORY_ARTIFACT.read_text(encoding="utf-8"))


def _planner_rows() -> list[dict]:
    rows = (_artifact().get("flows") or {}).get("planner") or []
    matches = [row for row in rows if str(row.get("id") or "") == PLANNER_ROW_ID]
    assert len(matches) == 1, (
        f"prompts/factory-defaults.json has no unique flows.planner row with id "
        f"{PLANNER_ROW_ID!r} (found {len(matches)}; rows: "
        f"{[str(row.get('id')) for row in rows]})"
    )
    return rows


def _load_seed_module() -> ModuleType:
    """Load the seed script by path (``scripts/`` is not an importable package).

    Registered in ``sys.modules`` before execution: the script declares a
    dataclass, and ``@dataclass`` resolves string annotations through
    ``sys.modules[cls.__module__]`` at class creation.
    """
    import sys

    spec: ModuleSpec | None = importlib.util.spec_from_file_location(
        "seed_prompt_defaults", SEED_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- the committed artifact ---------------------------------------------------


def test_committed_factory_planner_row_is_the_engine_directive_byte_for_byte():
    """The shipped text IS the constant: the sent prompt does not change.

    Byte equality is the whole safety argument for the factory bump — a single
    differing character re-prefills every install's planner call once.
    """
    rows = _planner_rows()
    content = str(rows[0].get("content") or "")
    assert content.strip(), f"flows.planner/{PLANNER_ROW_ID} was never seeded; {_ARTIFACT_DRIFT_FIX}"
    assert content == DEFAULT_PLANNER_DIRECTIVE, _ARTIFACT_DRIFT_FIX


def test_planner_flow_is_still_its_single_directive_row():
    """Seeding replaced the empty BODY, never added/reordered a row."""
    rows = _planner_rows()
    assert [row["type"] for row in rows] == [m.type for m in DEFAULT_FLOWS["planner"]]
    assert rows[0]["type"] == "system"
    assert rows[0]["role"] == "system"
    assert rows[0]["enabled"] is True


def test_committed_factory_flow_row_order_is_unchanged():
    """Every flow's row-type ORDER matches the code default flow.

    The seed write path can reorder rows (AGENTS.md); the artifact is the served
    surface, so a reorder is a real regression, not a cosmetic diff.
    """
    flows = _artifact().get("flows") or {}
    for interaction, default_rows in DEFAULT_FLOWS.items():
        assert [row["type"] for row in flows.get(interaction) or []] == [
            module.type for module in default_rows
        ], f"{interaction}: row order/content drifted from DEFAULT_FLOWS"


def test_factory_version_was_bumped_for_the_planner_directive():
    """ADR-006: factory content changed -> the committed image and the code agree.

    A mismatch is exactly the state where existing installs would never see the
    "factory defaults changed — review/reset" notice.
    """
    assert _artifact()["factory_version"] == factory.FACTORY_VERSION
    # The bump carried this change: v11 predates the seeded planner directive.
    assert factory.FACTORY_VERSION >= 12


# -- the code fallback stays --------------------------------------------------


def test_the_code_default_flow_keeps_the_empty_fallback_row():
    """``DEFAULT_FLOWS`` is the no-preset path: the constant still renders."""
    row = DEFAULT_FLOWS["planner"][0]
    assert row.content == ""
    assert DEFAULT_PLANNER_DIRECTIVE.strip()


# -- the seeding script -------------------------------------------------------


def test_seed_script_covers_the_planner_row():
    module = _load_seed_module()
    assert module.FLOW_SYSTEM_MODULE_ID["planner"] == PLANNER_ROW_ID
    assert module.resolve_system_prompts()["planner"] == DEFAULT_PLANNER_DIRECTIVE


def test_seed_plan_seeds_an_empty_planner_row_and_is_idempotent():
    module = _load_seed_module()
    constants = module.resolve_system_prompts()
    preset = {"flows": {"planner": [{"id": PLANNER_ROW_ID, "type": "system", "content": ""}]}}

    plan = module.build_plan(preset, constants, force=False)
    planner = next(entry for entry in plan if entry.flow == "planner")
    assert planner.action == "set"

    seeded = module.apply_plan(preset["flows"], [planner], constants)
    assert seeded["planner"][0]["content"] == DEFAULT_PLANNER_DIRECTIVE
    # The source preset is untouched (apply_plan deep-copies).
    assert preset["flows"]["planner"][0]["content"] == ""

    # Re-planning the seeded result is a true no-op.
    again = module.build_plan({"flows": seeded}, constants, force=False)
    assert next(e for e in again if e.flow == "planner").action == "already"
    # The planner row verifies (the other flows are not in this fixture).
    planner_row = module._find_system_row(seeded["planner"], PLANNER_ROW_ID)
    assert planner_row is not None
    assert planner_row["content"] == constants["planner"]


def test_seed_row_order_guard_flags_a_reorder():
    """The post-write check that AGENTS.md demands (and the script now runs)."""
    module = _load_seed_module()
    before = {"planner": [{"id": "planner_system"}, {"id": "planner_extra"}]}
    same = {"planner": [{"id": "planner_system"}, {"id": "planner_extra"}]}
    reordered = {"planner": [{"id": "planner_extra"}, {"id": "planner_system"}]}
    assert module.verify_row_order(before, same) == []
    problems = module.verify_row_order(before, reordered)
    assert problems and "row order changed" in problems[0]


# -- the steps read seam (the editor's pre-fill source) -----------------------


@pytest.fixture
def app_and_log(tmp_path: Path):
    log_path = tmp_path / "events.db"
    app = create_app(FakeTurnEngine(), log_path)
    app.include_router(router)
    yield app, app.state.log
    app.state.log.close()


def _steps(app) -> dict[str, dict]:
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return (await client.get("/api/prompts/steps")).json()

    body = run(scenario())
    return {step["id"]: step for step in body["steps"]}


def test_steps_serves_the_planner_engine_default(app_and_log):
    """An install seeded from the factory image reports the authored row AND
    still names the engine default (the pre-fill source)."""
    app, log = app_and_log
    preset_store.ensure_default_preset(log)  # the committed factory image
    steps = _steps(app)
    assert steps["planner"]["system_source"] == "authored"
    assert steps["planner"]["system_default"] == DEFAULT_PLANNER_DIRECTIVE
    # Flows with no single engine default say so rather than serving a guess.
    assert steps["narrate"]["system_default"] is None
    assert steps["assistant"]["system_default"] is None


def test_steps_serves_the_default_for_a_row_that_is_still_empty(app_and_log):
    """The pre-#105 shape: an empty planner row -> engine source + the text.

    This is the state every existing install keeps until it resets, and it is
    what the block editor pre-fills so the box is never blind.
    """
    app, log = app_and_log
    preset_store.ensure_default_preset(log)
    preset = preset_store.get_preset(log, preset_store.DEFAULT_PRESET_ID)
    assert preset is not None

    async def clear():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.patch(
                "/api/prompts/presets/default",
                json={
                    "flows": {
                        **{k: [dict(row) for row in v] for k, v in preset.flows.items()},
                        "planner": [
                            {
                                "id": PLANNER_ROW_ID,
                                "name": "Planner directive",
                                "type": "system",
                                "role": "system",
                                "content": "",
                                "enabled": True,
                            }
                        ],
                    }
                },
            )

    resp = run(clear())
    assert resp.status_code == 200, resp.text
    step = _steps(app)["planner"]
    assert step["system_source"] == "engine"
    assert step["system_default"] == DEFAULT_PLANNER_DIRECTIVE
    assert step["block_count"] == 1
