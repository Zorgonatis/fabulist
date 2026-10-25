"""Issue #50 — ONE canonical anti-slop list, THREE derived consumers.

The list's home is the factory artifact (``prompts/factory-defaults.json``,
``anti_slop`` section). These tests pin the acceptance criteria:

* all 56 owner-preset entries load, verbatim, from the artifact;
* the validator's patterns DERIVE from it (literal by default; the optional
  per-entry ``pattern`` escape hatch carries the 34 legacy regexes);
* a slop phrase in prose is caught and reaches the narrator repair flags;
* the ``anti_slop`` directive renders the SAME list (Consumer B);
* the d1 bank derives from it and honours ``skip_d1`` (Consumer C);
* a factory commit / preset save round-trips the section without dropping it;
* a user can extend the list with zero code changes.
"""

from __future__ import annotations

import json
from pathlib import Path

from fabulist.antislop import (
    ANTI_SLOP_SECTION,
    ANTI_SLOP_TOKEN,
    load_anti_slop,
    normalize_anti_slop,
    render_prompt_list,
    reset_cache,
)
from fabulist.decision_queries import default_questions, slop_phrases
from fabulist.graph.validator import (
    DEFAULT_SLOP_PATTERNS,
    derive_slop_patterns,
    validate_narrative,
)
from fabulist.prompts import factory
from fabulist.prompts import preset_store as store
from fabulist.prompts.compiler import count_tokens, render_system_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "prompts" / "factory-defaults.json"

#: Every phrase the owner's ST preset (prompts[15]) carries, verbatim. The
#: investigation counted 56 by splitting "vague hazes (fuzzy warm haze | soft
#: blur)" into its two engine rules.
EXPECTED_PHRASES = [
    "Shivers down spine", "hit like [force/blow]", "arousal pooling",
    "torn between", "world narrowing", "air [charging/thick/heavy]",
    "breath catching",
    "Face a [mask/canvas/medium] of [confusion/terror/emotion]", "Pupils blown",
    "circling like [animal]", "predatory [grin/smirk]", "analytical gaze",
    "line of [mouth/jaw] [tightens/hardens/stays flat]",
    "expression [neutral/blank/unreadable]",
    "Voice as texture (velvety/silky/rough/gravelly/husky/breathy/throaty/guttural)",
    "voice as animal (purr/growl/rumble)", "barely whisper", "words are [objects]",
    "voice/tone is [adjective]",
    "Deliberate [movement/slowness]", "economical movements",
    "[fluid/smooth/measured] and [precise/efficient/controlled]",
    "pregnant pause", "silence stretched", "let X hang", "time [slowed/stopped]",
    "[beat/moment] passed", "for a long moment", "in [this/that] moment",
    "did not look back",
    "ozone",
    "I X, really X", "[Action], then [describe it]", "X escapes me",
    "More than X, [intensified]", "Not X, but Y",
    "real/genuine/true [emotion/gesture]", "I'm aware of", "I can feel [obvious]",
    "contrasting statements",
    "The world/universe/reality [verb]ed (tilted/narrowed/fell away)",
    "everything else [faded/ceased]", "fuzzy warm haze", "soft blur",
    "Contradictory adjectives (terrible, beautiful X | cruel, kind Z)",
    "2+ comma-separated adjectives (reckless, stupid, wonderful X | dark, dangerous, intoxicating X)",
    "Playful challenge", "stark contrast", "night is young", "uniquely [pronoun]",
    "toaster", "crimson beads", "it's not a question/request", "choice is yours",
    "words are flat", "[noun] was/became a [adj] thing",
]


# --- the list lives in the artifact ----------------------------------------


def test_artifact_carries_all_56_owner_entries_verbatim():
    listing = load_anti_slop()
    assert [entry.phrase for entry in listing.entries] == EXPECTED_PHRASES
    assert len(listing.entries) == 56
    assert listing.source.startswith("Stabs-GLM5.1-Directives-v4.0.0.json")
    assert [(name, len(rows)) for name, rows in listing.sections] == [
        ("Emotion", 7),
        ("Physical", 7),
        ("Voice", 5),
        ("Movement", 11),
        ("Smells/Scents", 1),
        ("Patterns", 13),
        ("Modifier Abuse", 2),
        ("Misc", 10),
    ]


def test_the_artifact_section_is_the_only_list_and_the_prompt_row_has_the_token():
    data = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert ANTI_SLOP_SECTION in data
    assert data["factory_version"] == factory.FACTORY_VERSION
    row = next(row for row in data["directives"] if row["id"] == "anti_slop")
    assert ANTI_SLOP_TOKEN in row["content"]
    # The list is not duplicated into the directive row (it renders from the list).
    assert "Shivers down spine" not in row["content"]


# --- Consumer A: the validator derives its patterns ------------------------


def test_validator_patterns_derive_from_the_list():
    patterns = derive_slop_patterns()
    assert len(patterns) == 56
    assert len(DEFAULT_SLOP_PATTERNS) == 56
    by_id = {pattern.rule_id: pattern for pattern in patterns}
    # A legacy regex survives through the optional ``pattern`` escape hatch.
    assert by_id["shivers_down"].regex == r"\bshivers? down\b"
    assert by_id["toaster"].severity == "info"
    assert "Anti-Slop / Misc" in by_id["toaster"].source
    # A new literal entry is escaped, not treated as a regex.
    assert "circling like \\[animal\\]" not in by_id["circling_like"].regex  # literal text is escaped
    assert "circling\\ like" in by_id["circling_like"].regex


def test_literal_entries_escape_regex_metacharacters(tmp_path):
    """A user phrase is DATA: dots/brackets/parens never become regex syntax."""
    raw = {
        "sections": [
            {"name": "Custom", "entries": ["a.b (literally) [x]"]},
        ]
    }
    listing = load_anti_slop(_write_artifact(tmp_path / "artifact.json", raw))
    pattern = derive_slop_patterns(listing)[0]
    assert pattern.compiled.search("a.b (literally) [x]") is not None
    # The dot is escaped, so it cannot match an arbitrary character.
    assert pattern.compiled.search("axb (literally) [x]") is None
    # An explicit pattern keeps regex semantics.
    raw2 = {
        "sections": [
            {"name": "Custom", "entries": [
                {"phrase": "shiver down", "pattern": r"\bshivers? down\b"},
            ]},
        ]
    }
    listing2 = load_anti_slop(_write_artifact(tmp_path / "artifact2.json", raw2))
    assert derive_slop_patterns(listing2)[0].compiled.search("shivers down") is not None


def _write_artifact(path: Path, anti_slop: dict) -> Path:
    path.write_text(json.dumps({"anti_slop": anti_slop}), encoding="utf-8")
    reset_cache()
    return path


def test_a_slop_phrase_is_caught_and_reaches_the_repair_flag(log):
    """Catch -> repair: the deterministic flag bundle the narrator loop repairs."""
    from fabulist.graph.nodes import _narrate_flags

    prose = [{"kind": "prose", "text": "A pregnant pause. The room said nothing."}]
    report = validate_narrative("x", prose)
    assert [flag.rule_id for flag in report.flags] == ["pregnant_pause"]

    flags = _narrate_flags(
        user_content="x",
        segments=prose,
        validation_errors=[],
        prior_first_words=None,
    )
    slop = [flag for flag in flags if flag["check"] == "slop"]
    assert [flag["rule_id"] for flag in slop] == ["pregnant_pause"]
    assert slop[0]["fix"], "the repair loop needs a corrective action"


# --- Consumer B: the prompt renders the SAME list --------------------------


def test_factory_anti_slop_directive_renders_the_full_list():
    system = render_system_prompt()
    rendered = render_prompt_list()
    assert system.count(rendered) == 1
    for phrase in EXPECTED_PHRASES:
        assert phrase in system
    # The list rides the stable prefix (counted for the report).
    assert count_tokens(rendered) > 0


def test_a_preset_carried_directive_row_renders_the_list_too():
    data = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    rows = data["directives"]
    from fabulist.prompts.preset_store import preset_directives
    from fabulist.prompts.preset_store import preset_from_json

    preset = preset_from_json({"directives": rows})
    system = render_system_prompt(directives=preset_directives(preset))
    assert render_prompt_list() in system


# --- Consumer C: the d1 bank derives and skips -----------------------------


def test_d1_bank_derives_from_the_list_and_honours_skip_flags():
    asked = {entry.id for entry in load_anti_slop().d1_entries()}
    skipped = {
        entry.id
        for entry in load_anti_slop().entries
        if entry.skip_d1
    }
    assert asked and skipped
    assert {rule_id for rule_id, _phrase in slop_phrases()} == asked
    slop_rows = {row["id"] for row in default_questions() if row["source"] == "slop"}
    assert slop_rows == asked
    assert slop_rows.isdisjoint(skipped)
    # The regex-caught legacy phrases do not pay for a model question.
    assert "pregnant_pause" in skipped
    assert "circling_like" in asked


# --- factory / preset round-trip -------------------------------------------


def test_factory_save_and_load_preserve_the_list(tmp_path: Path):
    image = factory.factory_image()
    path = tmp_path / "prompts" / "factory-defaults.json"
    factory.save_artifact(image.payload, path=path)

    loaded = factory.load_artifact(path)
    assert loaded is not None
    assert loaded["anti_slop"] == image.payload["anti_slop"]
    assert sum(len(s["entries"]) for s in loaded["anti_slop"]["sections"]) == 56


def test_preset_save_and_factory_commit_round_trip_the_list(tmp_path: Path, monkeypatch):
    artifact = tmp_path / "prompts" / "factory-defaults.json"
    monkeypatch.setattr(factory, "DEFAULT_ARTIFACT_RELPATH", artifact)
    monkeypatch.setattr(factory, "FACTORY_VERSION", factory.FACTORY_VERSION)
    real = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps(real), encoding="utf-8")

    from fabulist.log import EventLog

    log = EventLog(tmp_path / "events.db")
    try:
        store.ensure_default_preset(log)
        row = store.default_preset(log)
        assert row is not None and len(row.anti_slop["sections"]) == 8
        payload = store.preset_to_json(row)
        assert payload["anti_slop"] == row.anti_slop
        # Commit writes the row back to the artifact without dropping the list.
        store.commit_default_to_factory(log)
        committed = json.loads(artifact.read_text(encoding="utf-8"))
        assert sum(len(s["entries"]) for s in committed["anti_slop"]["sections"]) == 56
    finally:
        log.close()


def test_normalize_preserves_strings_for_a_hand_editable_diff():
    raw = {
        "version": 1,
        "source": "owner",
        "sections": [
            {"name": "Misc", "entries": ["a plain phrase", {"phrase": "regexed", "pattern": r"\bregexed\b", "skip_d1": True}]},
        ],
    }
    normalized = normalize_anti_slop(raw)
    assert normalized["sections"][0]["entries"][0] == "a plain phrase"
    assert normalized["sections"][0]["entries"][1] == {
        "phrase": "regexed",
        "pattern": r"\bregexed\b",
        "skip_d1": True,
    }


def test_a_user_extends_the_list_with_zero_code_changes(tmp_path):
    """Add one section entry to the artifact: all three consumers see it."""
    from fabulist.antislop import render_prompt_list

    raw = {
        "version": 1,
        "source": "owner edit",
        "sections": [
            {"name": "House rules", "entries": ["the plot thickened"]},
        ],
    }
    listing = load_anti_slop(_write_artifact(tmp_path / "factory.json", raw))
    assert [entry.phrase for entry in listing.entries] == ["the plot thickened"]
    # Consumer A derives a pattern for the new phrase ...
    pattern = derive_slop_patterns(listing)[0]
    assert pattern.compiled.search("And then the plot thickened.") is not None
    # ... Consumer B renders it ...
    assert "the plot thickened" in render_prompt_list(listing)
    # ... and Consumer C (a fresh bank) would ask it (skip_d1 defaults false).
    assert [entry.id for entry in listing.d1_entries()] == ["the_plot_thickened"]


def test_the_preset_write_door_carries_the_section(tmp_path):
    """The studio/factory path can write the owner list (issue #50)."""
    from fabulist.api.prompts_api import PresetInput, _input_to_payload

    raw = {
        "version": 1,
        "source": "owner",
        "sections": [{"name": "House rules", "entries": ["the plot thickened"]}],
    }
    payload = _input_to_payload(PresetInput(name="mine", anti_slop=raw))
    assert payload["anti_slop"]["sections"][0]["entries"] == ["the plot thickened"]


def test_commit_does_not_drop_an_artifact_owned_list(tmp_path):
    """A commit from an old row (no ``anti_slop``) preserves the artifact's list."""
    path = tmp_path / "prompts" / "factory-defaults.json"
    real = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(real), encoding="utf-8")

    factory.save_artifact(
        {"preset_id": "default", "name": "default", "modules": [], "flows": {}},
        path=path,
    )
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert sum(len(s["entries"]) for s in committed["anti_slop"]["sections"]) == 56
