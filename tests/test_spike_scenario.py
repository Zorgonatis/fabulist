"""Scenario loader tests: strictness + relative card path resolution.

Fakes only — no network, no model. The loader is a hard contract for the lane
that authors scenario files, so every rejection path is asserted explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # scripts/ is a namespace package, not installed
    sys.path.insert(0, str(_ROOT))

from scripts.spike.scenario import (  # noqa: E402
    Scenario,
    ScenarioError,
    load_scenario,
)

CARD_JSON = {"spec": "chara_card_v2", "data": {"name": "Guild RPG", "first_mes": "Hi"}}


def _write_card(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"name": "Guild RPG"}', encoding="utf-8")
    return path


def _base_scenario(card: str = "card.json") -> dict:
    return {
        "name": "dnd-guild",
        "title": "Spike — Guild RPG",
        "flavor": "dnd-dungeonmaster",
        "card": card,
        "turns": 5,
        "player_model": "glm-api",
        "player": {"name": "Ryn", "persona": "A curious rogue."},
        "beats": [{"kind": "in_character", "content": "I enter the guild."}],
        "ooc_probes": [{"at": 3, "content": "OOC: recap?"}],
        "worldinfo": [{"keys": ["guild"], "content": "The guild is old."}],
    }


def _dump(tmp_path: Path, data: dict, *, card: str = "card.json", name: str = "scenario.yaml") -> Path:
    _write_card(tmp_path / card)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_loads_valid_scenario(tmp_path: Path) -> None:
    path = _dump(tmp_path, _base_scenario())
    scenario = load_scenario(path)

    assert isinstance(scenario, Scenario)
    assert scenario.name == "dnd-guild"
    assert scenario.title == "Spike — Guild RPG"
    assert scenario.flavor == "dnd-dungeonmaster"
    assert scenario.turns == 5
    assert scenario.player_model == "glm-api"
    assert scenario.player.name == "Ryn"
    assert scenario.player.persona == "A curious rogue."
    assert scenario.card == (tmp_path / "card.json").resolve()
    assert scenario.source_path == path.resolve()
    assert len(scenario.beats) == 1
    assert scenario.beats[0].kind == "in_character"
    assert scenario.ooc_probes[0].at == 3
    assert scenario.worldinfo[0].keys == ("guild",)
    assert scenario.worldinfo_book() == [
        {"keys": ["guild"], "content": "The guild is old."}
    ]


def test_relative_card_path_resolves_against_scenario_dir(tmp_path: Path) -> None:
    nested = tmp_path / "bundle"
    path = _dump(nested, _base_scenario(card="cards/GuildRPG.json"), card="cards/GuildRPG.json")
    scenario = load_scenario(path)
    assert scenario.card == (nested / "cards" / "GuildRPG.json").resolve()
    assert scenario.card.is_file()


def test_optional_fields_default(tmp_path: Path) -> None:
    data = _base_scenario()
    data.pop("title")
    data.pop("flavor")
    data.pop("ooc_probes")
    data.pop("worldinfo")
    path = _dump(tmp_path, data)
    scenario = load_scenario(path)
    assert scenario.title == "dnd-guild"  # title falls back to name
    assert scenario.flavor == ""
    assert scenario.ooc_probes == ()
    assert scenario.worldinfo == ()


def test_relative_scenario_path_resolves_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _dump(tmp_path, _base_scenario())
    monkeypatch.chdir(tmp_path)
    scenario = load_scenario("scenario.yaml")
    assert scenario.source_path == path.resolve()


@pytest.mark.parametrize("bad_key", ["nmae", "turn", "unknown"])
def test_unknown_top_level_key_errors(tmp_path: Path, bad_key: str) -> None:
    data = _base_scenario()
    data[bad_key] = "x"
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match="unknown key"):
        load_scenario(path)


def test_unknown_player_key_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["player"]["personality"] = "nope"
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match=r"player: unknown key"):
        load_scenario(path)


def test_unknown_beat_key_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["beats"] = [{"kind": "in_character", "content": "x", "delay": 3}]
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match=r"beats\[0\]: unknown key"):
        load_scenario(path)


def test_unknown_probe_key_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["ooc_probes"] = [{"at": 2, "content": "x", "kind": "ooc"}]
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match=r"ooc_probes\[0\]: unknown key"):
        load_scenario(path)


def test_unknown_directive_key_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["player"]["directives"] = [{"at": 2, "note": "x", "weight": 0.5}]
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match=r"directives\[0\]: unknown key"):
        load_scenario(path)


def test_unknown_worldinfo_key_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["worldinfo"] = [{"keys": ["a"], "content": "b", "priority": 9}]
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match=r"worldinfo\[0\]: unknown key"):
        load_scenario(path)


def test_bad_beat_kind_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["beats"] = [{"kind": "narration", "content": "x"}]
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match="kind"):
        load_scenario(path)


def test_missing_required_top_key_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data.pop("player_model")
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match="missing required key"):
        load_scenario(path)


def test_missing_card_file_errors(tmp_path: Path) -> None:
    data = _base_scenario(card="cards/does_not_exist.png")
    path = _dump(tmp_path, data, card="card.json")  # card.json exists, but not the referenced one
    with pytest.raises(ScenarioError, match="card: file not found"):
        load_scenario(path)


@pytest.mark.parametrize("bad_turns", [0, -1])
def test_turns_must_be_positive(tmp_path: Path, bad_turns: int) -> None:
    data = _base_scenario()
    data["turns"] = bad_turns
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match="turns"):
        load_scenario(path)


def test_turns_must_be_int(tmp_path: Path) -> None:
    data = _base_scenario()
    data["turns"] = "5"
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match="turns"):
        load_scenario(path)


def test_beats_longer_than_turns_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["turns"] = 1
    data["beats"] = [
        {"kind": "in_character", "content": "a"},
        {"kind": "in_character", "content": "b"},
    ]
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match="beats"):
        load_scenario(path)


def test_probe_past_turns_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["turns"] = 3
    data["ooc_probes"] = [{"at": 4, "content": "x"}]
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match="ooc_probes"):
        load_scenario(path)


def test_duplicate_probe_turn_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["ooc_probes"] = [
        {"at": 2, "content": "a"},
        {"at": 2, "content": "b"},
    ]
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match="duplicate probe"):
        load_scenario(path)


def test_directive_past_turns_errors(tmp_path: Path) -> None:
    data = _base_scenario()
    data["turns"] = 3
    data["player"]["directives"] = [{"at": 9, "note": "x"}]
    path = _dump(tmp_path, data)
    with pytest.raises(ScenarioError, match="directives"):
        load_scenario(path)


def test_invalid_yaml_errors(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text("name: [unclosed", encoding="utf-8")
    with pytest.raises(ScenarioError, match="invalid YAML"):
        load_scenario(path)


def test_non_mapping_root_errors(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="expected a mapping"):
        load_scenario(path)


def test_missing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="not found"):
        load_scenario(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# schedule helpers (turn-planning contract)
# ---------------------------------------------------------------------------


def _scenario_with_schedule(tmp_path: Path) -> Scenario:
    data = _base_scenario()
    data["turns"] = 6
    data["beats"] = [
        {"kind": "in_character", "content": "beat one"},
        {"kind": "ooc", "content": "beat two"},
    ]
    data["ooc_probes"] = [{"at": 4, "content": "OOC: probe"}]
    data["player"]["directives"] = [
        {"at": 3, "note": "note three"},
        {"at": 4, "note": "note four (probe turn)"},
        {"at": 9, "note": "outside"},
    ]
    data["turns"] = 9
    path = _dump(tmp_path, data)
    return load_scenario(path)


def test_beat_for(tmp_path: Path) -> None:
    scenario = _scenario_with_schedule(tmp_path)
    assert scenario.beat_for(1).content == "beat one"
    assert scenario.beat_for(2).kind == "ooc"
    assert scenario.beat_for(3) is None
    assert scenario.beat_for(0) is None


def test_probe_for(tmp_path: Path) -> None:
    scenario = _scenario_with_schedule(tmp_path)
    assert scenario.probe_for(4).content == "OOC: probe"
    assert scenario.probe_for(3) is None


def test_directive_for_only_at_its_turn(tmp_path: Path) -> None:
    scenario = _scenario_with_schedule(tmp_path)
    assert scenario.directive_for(3) == "note three"
    assert scenario.directive_for(4) == "note four (probe turn)"
    assert scenario.directive_for(1) is None
    assert scenario.directive_for(2) is None
