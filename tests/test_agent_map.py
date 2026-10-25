"""The generated agent map (D46) — derivation, coverage and the ``--check`` gate.

The map is GENERATED from the code (PLAN D46): the registry interactions come
from :data:`fabulist.prompts.agent_sets.INTERACTIONS`, the flows from
``prompts/modules.py``, the message builders from a wire-source parse of
``wiring.py``, and the non-registry model callers are verified present by AST.
These tests pin the committed artifact to the generator and assert the coverage
that makes the map useful: every registry interaction appears and the four
invisible non-registry callers are listed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # scripts/ is a namespace package, not installed
    sys.path.insert(0, str(_ROOT))

from scripts import generate_agent_map as agent_map  # noqa: E402

from fabulist.connections.taxonomy import INTERACTION_TASKS  # noqa: E402
from fabulist.prompts.agent_sets import INTERACTIONS  # noqa: E402

AGENT_MAP_PATH = _ROOT / "prompts" / "generated" / "agent-map.json"

#: The three invisible model callers the audit flagged plus the DSL
#: re-synthesizer (RESUME 2026-10-12: 8 registry interactions + 3 invisible).
NON_REGISTRY_NAMES = frozenset({"dsl_resynthesizer", "reranker", "stt", "embedder"})


def _committed() -> dict:
    return json.loads(AGENT_MAP_PATH.read_text(encoding="utf-8"))


def _agents_by_name() -> dict[str, dict]:
    return {agent["name"]: agent for agent in _committed()["agents"]}


def test_generator_runs_and_matches_committed_artifact():
    generated = agent_map.build_agent_map()
    assert generated["agents"]
    assert agent_map.render(generated) == AGENT_MAP_PATH.read_text(encoding="utf-8")


def test_every_interaction_appears_as_a_registry_agent():
    agents = _agents_by_name()
    for name in INTERACTIONS:
        entry = agents[name]
        assert entry["registry"] is True
        assert entry["default_agent"], name
        assert entry["input_builder"].startswith("fabulist.wiring.")


def test_agent_roster_is_exactly_interactions_plus_non_registry_callers():
    names = set(_agents_by_name())
    assert names == set(INTERACTIONS) | NON_REGISTRY_NAMES
    assert not (NON_REGISTRY_NAMES & set(INTERACTIONS))


def test_registry_agents_carry_their_taxonomy_type_and_task():
    """W9.25 §3.2: the map names the slot (type + task) every step rides.

    The columns are derived from ``INTERACTION_TASKS`` — the same table the
    resolution seam consults — so the artifact cannot disagree with the
    taxonomy about which model class an interaction belongs to.
    """
    agents = _agents_by_name()
    for name in INTERACTIONS:
        entry = agents[name]
        assert (entry["type"], entry["task"]) == INTERACTION_TASKS[name]
    # non-registry callers have no binding layer, hence no slot
    for name in NON_REGISTRY_NAMES:
        assert agents[name]["type"] is None
        assert agents[name]["task"] is None
    assert (
        _committed()["generated_from"]["interaction_tasks"]
        == "fabulist.connections.taxonomy.INTERACTION_TASKS"
    )


def test_non_registry_callers_are_listed_with_a_note():
    agents = _agents_by_name()
    for name in NON_REGISTRY_NAMES:
        entry = agents[name]
        assert entry["registry"] is False
        assert entry["note"]
        assert entry["output_schema"]
        assert entry["disposition"]


def test_resynthesizer_note_flags_the_uncounted_second_call():
    note = _committed()["resynthesizer_note"]
    assert "uncounted" in note
    assert "INTERACTIONS" in note
    assert _agents_by_name()["dsl_resynthesizer"]["registry"] is False


def test_check_passes_against_the_committed_file():
    assert agent_map.check(AGENT_MAP_PATH) is True


def test_check_fails_when_the_file_is_mutated(tmp_path):
    text = agent_map.render(agent_map.build_agent_map())
    mutated = tmp_path / "agent-map.json"
    mutated.write_text(text.replace("narrate", "narrateX", 1), encoding="utf-8")
    assert mutated.read_text(encoding="utf-8") != text
    assert agent_map.check(mutated) is False


def test_check_fails_when_the_file_is_missing(tmp_path):
    assert agent_map.check(tmp_path / "absent.json") is False


def test_main_check_mode_returns_zero_for_committed_file(monkeypatch):
    monkeypatch.setattr(agent_map, "AGENT_MAP_PATH", AGENT_MAP_PATH)
    assert agent_map.main(["--check"]) == 0


def test_main_check_mode_returns_nonzero_on_drift(tmp_path, monkeypatch):
    drifted = tmp_path / "agent-map.json"
    drifted.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(agent_map, "AGENT_MAP_PATH", drifted)
    assert agent_map.main(["--check"]) == 1
