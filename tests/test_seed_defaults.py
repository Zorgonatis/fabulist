"""Bug 4 — the OOTB seed script's pure helpers.

The script's HTTP path (persona import + assistant create + idempotent skip) is
exercised against a live isolated engine by the wave verification script; these
tests pin the pure derivation: the preset prompt -> definition extraction and
the derived card being a valid Character Card V2 the import parser accepts.

No pytest runner imports ``scripts/`` today, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

from fabulist.cards import parse_card_v2

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_defaults.py"


def _load_seed_module() -> ModuleType:
    spec: ModuleSpec | None = importlib.util.spec_from_file_location("seed_defaults", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_find_definition_extracts_block_and_avatar():
    module = _load_seed_module()
    preset = {
        "prompts": [
            {"content": "noise"},
            {
                "content": (
                    "INPUT:\n- AvatarURL: http://example.test/segfault.png\n"
                    "<SEGFAULT_DEFINITION>\nTheme: cool\n</SEGFAULT_DEFINITION>\n"
                )
            },
        ]
    }
    definition, avatar = module.find_definition(preset, Path("preset.json"))
    assert definition == "Theme: cool"
    assert avatar == "http://example.test/segfault.png"


def test_build_card_is_a_valid_chara_card_v2():
    module = _load_seed_module()
    card = module.build_card("A hacked-in assistant.", "", Path("preset.json"))
    assert card["spec"] == "chara_card_v2"
    assert card["data"]["name"] == module.CHARACTER_NAME
    assert card["data"]["description"] == "A hacked-in assistant."
    # The definition-only import path parses exactly this shape.
    parsed = parse_card_v2(card)
    assert parsed.name == "SEGFAULT"
    assert parsed.description == "A hacked-in assistant."
