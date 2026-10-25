"""Tests for per-interaction module flows (D18 completion).

Every model-facing interaction carries its own ordered module list:
:data:`fabulist.prompts.modules.DEFAULT_FLOWS` mirrors each landed call site
(narrate / ooc / extract / summarize / caption / assistant), and
:func:`~fabulist.prompts.modules.compile_flow` renders one flow from
engine-resolved ``parts``. The preset store keeps both ``modules`` (the
canonical narrate flow) and ``flows`` so the landed UI/API clients never break.
The ``prompt_overrides`` shim is RETIRED (W6.3): a legacy payload still folds on
read, but :func:`compile_preset_flow` never consults it.

The per-flow "call-site shape" tests build the SAME request the production
helper builds and assert the compiled flow equals it — parity by construction.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.prompts_api import router
from fabulist.graph.compaction import (
    SUMMARIZER_SYSTEM_PROMPT,
    build_summarizer_messages,
)
from fabulist.log import EventLog
from fabulist.media.images import CAPTION_SYSTEM, build_caption_messages
from fabulist.prompts import preset_store
from fabulist.prompts.agent_sets import INTERACTIONS as BINDING_INTERACTIONS
from fabulist.prompts.modules import (
    DEFAULT_FLOWS,
    FLOW_STRUCTURAL_TYPES,
    INTERACTIONS,
    PromptModule,
    compile_flow,
    compile_preset_messages,
    flow_is_default,
    from_default_flow,
)
from fabulist.prompts.preset_store import (
    compile_preset_flow,
    preset_from_json,
    preset_to_json,
    seed_from_default,
)
from fabulist.assistants.manager import (
    AssistantContext,
    AssistantDefinition,
    build_assistant_messages,
    render_scene_digest,
    render_assistant_system,
)
from fabulist.state.extractor import EXTRACTOR_SYSTEM_PROMPT, _turn_block
from fabulist.theme.tokens import CURATED_PALETTE

# Reuse the extractor test's register parser instead of duplicating the regex:
# both tests must read the register line the SAME way (tests/ is on sys.path).
from test_extractor import _TONE_REGISTER_RE


def run(coro):
    return asyncio.run(coro)


# --- contract ---------------------------------------------------------------


def test_interactions_mirror_agent_sets():
    """The flow key set never drifts from the D19 agent-binding key set."""
    assert INTERACTIONS == BINDING_INTERACTIONS


def test_default_flows_cover_every_interaction_with_valid_types():
    assert set(DEFAULT_FLOWS) == set(INTERACTIONS)
    for interaction, rows in DEFAULT_FLOWS.items():
        valid = FLOW_STRUCTURAL_TYPES[interaction] | {"text"}
        assert {module.type for module in rows} <= valid
        assert all(module.enabled for module in rows)
    # The narrator flow is today's DEFAULT_MODULES (incl. the D22 recalled row).
    assert [m.type for m in DEFAULT_FLOWS["narrate"]] == [
        "narrator_system",
        "library",
        "history",
        "facts",
        "user",
        "recalled",
    ]


def test_proposals_flow_is_a_brief_plus_its_type_payload():
    """W9.22 §3: one proposal lane = a system brief + its type-scoped payload.

    The shape mirrors ``bts``/``critic`` (single-shot, one call): the node
    composes the payload from the turn's touched entities of ONE data type, and
    the gate that decides WHETHER the lane runs is the dispatcher's, not this
    flow's.
    """
    assert FLOW_STRUCTURAL_TYPES["proposals"] == frozenset({"system", "payload"})
    assert [module.type for module in DEFAULT_FLOWS["proposals"]] == [
        "system",
        "payload",
    ]


def test_proposals_interaction_is_described_in_the_catalog():
    """The W6 step list needs title/blurb/trigger for every interaction.

    The catalog's import-time guard already fails a missing row; this pins the
    proposal step's own wording so the Studio cannot show a nameless step.
    """
    from fabulist.prompts.catalog import INTERACTION_CATALOG

    meta = INTERACTION_CATALOG["proposals"]
    assert meta.title
    assert meta.blurb
    # W9.23 §3.6.4: the trigger is the HISTORIAN's truth — off-turn, on the
    # preset's cadence (or on demand), never in the turn flow.
    assert "OFF-TURN" in meta.trigger
    assert "historian" in meta.trigger
    assert "cadence" in meta.trigger
    assert "asset_proposals" not in meta.trigger


def test_flow_is_default_ignores_ids_and_names():
    default = DEFAULT_FLOWS["ooc"]
    copied = [
        PromptModule("fresh-id", "renamed", m.type, role=m.role, content=m.content)
        for m in default
    ]
    assert flow_is_default("ooc", copied)
    edited = copied + [PromptModule("extra", "Extra", "text", content="note")]
    assert not flow_is_default("ooc", edited)
    assert not flow_is_default("ooc", copied[:-1])  # reorder/drop = customized
    assert not flow_is_default("nonsense", copied)


def test_from_default_flow_unknown_interaction_raises():
    with pytest.raises(ValueError, match="unknown interaction"):
        from_default_flow("bogus")


# --- compile_flow round-trip per call-site shape ----------------------------


def test_narrate_flow_round_trips_and_aliases_compile_preset_messages():
    parts = {
        "narrator_system": "SYSTEM",
        "library": "LIBRARY",
        "history": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        "facts": "FACTS",
        "user_content": "CURRENT",
    }
    # Issue #44: ONE system message in the prefix (the library spec folds into
    # the narrator system block behind the renderer's section divider).
    expected = [
        {"role": "system", "content": "SYSTEM\n\nLIBRARY"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "FACTS"},
        {"role": "user", "content": "CURRENT"},
    ]
    assert compile_flow("narrate", DEFAULT_FLOWS["narrate"], parts) == expected
    # Back-compat alias: the narrator entry point IS compile_flow("narrate", ...).
    assert compile_preset_messages(DEFAULT_FLOWS["narrate"], parts) == expected


def test_ooc_flow_round_trips():
    """Call site: wiring.make_ooc_message_builder -> assemble_messages(kind=ooc)."""
    parts = {
        "system": "OOC_SYSTEM",
        "history": [{"role": "user", "content": "earlier"}],
        "facts": "time: dawn",
        "user_content": "answer me",
    }
    assert compile_flow("ooc", DEFAULT_FLOWS["ooc"], parts) == [
        {"role": "system", "content": "OOC_SYSTEM"},
        {"role": "user", "content": "earlier"},
        {"role": "user", "content": "time: dawn"},
        {"role": "user", "content": "answer me"},
    ]


def test_extract_flow_matches_extractor_request():
    """Call site: state/extractor.LLMExtractor.__call__ base request (repair
    appends assistant + REPAIR after — outside the base flow)."""
    expected = [
        {"role": "system", "content": EXTRACTOR_SYSTEM_PROMPT},
        {"role": "user", "content": _turn_block("user said", "the prose")},
    ]
    parts = {
        "system": EXTRACTOR_SYSTEM_PROMPT,
        "turn_payload": _turn_block("user said", "the prose"),
    }
    assert compile_flow("extract", DEFAULT_FLOWS["extract"], parts) == expected


# --- the SERVED extract prompt: the committed factory artifact ----------------
#
# ``LLMExtractor._messages`` serves the preset flow's ``system`` row, not the
# constant (``self._system_prompt or EXTRACTOR_SYSTEM_PROMPT``), and on a fresh
# install that row is the COMMITTED factory image
# ``prompts/factory-defaults.json`` (AGENTS.md: the served prompt comes from the
# committed factory artifact — editing the Python constant is not enough).
# The tests above all parse the CONSTANT, so adding a palette key keeps them
# green while the shipped artifact teaches the stale register. This test reads
# the artifact itself.

REPO_ROOT = Path(__file__).resolve().parents[1]
FACTORY_ARTIFACT = REPO_ROOT / "prompts" / "factory-defaults.json"

#: The extract flow's system-row id (mirrors
#: ``scripts/seed_prompt_defaults.py:FLOW_SYSTEM_MODULE_ID["extract"]``).
EXTRACT_SYSTEM_ROW_ID = "extract_system"

#: Actionable remedy for every failure below.
_ARTIFACT_DRIFT_FIX = (
    "the SHIPPED extract prompt in prompts/factory-defaults.json no longer "
    "matches the engine: run `uv run python scripts/seed_prompt_defaults.py "
    "--force` to re-seed it from EXTRACTOR_SYSTEM_PROMPT, then re-check the "
    "prompt-flow row ORDER (the seed write path can reorder rows)."
)


def _artifact_extract_system_row() -> dict:
    """The committed artifact's ``flows.extract`` system row (fail if renamed)."""
    artifact = json.loads(FACTORY_ARTIFACT.read_text(encoding="utf-8"))
    rows = (artifact.get("flows") or {}).get("extract") or []
    matches = [row for row in rows if str(row.get("id") or "") == EXTRACT_SYSTEM_ROW_ID]
    # Guard against a silent pass: a renamed/removed row must fail LOUDLY here,
    # never leave the register assertion to be skipped.
    assert len(matches) == 1, (
        f"prompts/factory-defaults.json has no unique flows.extract row with id "
        f"{EXTRACT_SYSTEM_ROW_ID!r} (found {len(matches)}; rows: "
        f"{[str(row.get('id')) for row in rows]}). The served extract prompt row "
        f"was renamed — restore the id or update this test; {_ARTIFACT_DRIFT_FIX}"
    )
    return matches[0]


def test_committed_factory_extract_prompt_teaches_curated_palette():
    """The SHIPPED extract prompt teaches exactly ``sorted(CURATED_PALETTE)``.

    Artifact, not constant: a palette key added to ``theme/tokens.py`` reaches
    existing installs only after a re-seed, so the committed image is pinned
    here — otherwise the constant-derived tests above pass while the served
    prompt teaches a register ``style_tokens`` can no longer resolve.
    """
    row = _artifact_extract_system_row()
    content = str(row.get("content") or "")
    assert content.strip(), (
        f"flows.extract/{EXTRACT_SYSTEM_ROW_ID} carries no content — the factory "
        f"image was never seeded; {_ARTIFACT_DRIFT_FIX}"
    )
    match = _TONE_REGISTER_RE.search(content)
    assert match is not None, (
        f"flows.extract/{EXTRACT_SYSTEM_ROW_ID} no longer teaches a tone "
        f"register; {_ARTIFACT_DRIFT_FIX}"
    )
    served = match.group(1)
    expected = " | ".join(sorted(CURATED_PALETTE))
    assert served == expected, (
        f"tone register drift — {_ARTIFACT_DRIFT_FIX}\n"
        f"  served : {served}\n"
        f"  palette: {expected}"
    )


def test_summarize_flow_matches_compactor_request():
    """Call site: graph/compaction.build_summarizer_messages."""
    expected = build_summarizer_messages("the transcript")
    parts = {"system": SUMMARIZER_SYSTEM_PROMPT, "payload": expected[1]["content"]}
    assert compile_flow("summarize", DEFAULT_FLOWS["summarize"], parts) == expected


def test_caption_flow_matches_caption_request():
    """Call site: media/images.build_caption_messages (system + image part)."""
    asset = {"payload": {"mime": "image/png", "bytes_b64": "QUJD"}}
    expected = build_caption_messages(asset)
    parts = {"system": CAPTION_SYSTEM, "payload": expected[1]["content"]}
    assert compile_flow("caption", DEFAULT_FLOWS["caption"], parts) == expected
    # The payload really is the multipart content list, not flattened text.
    assert isinstance(expected[1]["content"], list)


def test_assistant_flow_matches_assistant_request():
    """Call site: assistants/manager.build_assistant_messages (no base history)."""
    definition = AssistantDefinition(id="sk", name="Sk", persona="in-voice")
    context = AssistantContext(facts="f", recent_prose="r", user_msg="m")
    expected = build_assistant_messages(definition, context)
    parts = {
        "system": render_assistant_system(definition),
        "history": [],
        "user_content": render_scene_digest(context),
    }
    assert compile_flow("assistant", DEFAULT_FLOWS["assistant"], parts) == expected
    # AUDIT (current-user duplication): the flow declares a ``history`` row but
    # its call sites always pass ``[]`` (wiring._assistant_flow_messages and the
    # prompts preview both hardcode it), and the ``user`` row is the scene
    # digest, which carries the latest user message exactly once. So there is
    # NO projection-history + live-user duplication here.
    assert "<user_message>\nm\n</user_message>" in expected[-1]["content"]
    assert expected[-1]["content"].count("<user_message>") == 1


def test_only_narrate_ooc_and_assistant_declare_history_and_user_rows():
    """Audit: the projection-plus-live-ask shape exists only in three flows.

    ``history`` is the projection block and ``user`` is the live final user
    turn. Only narrate/ooc/assistant declare BOTH; extract/summarize/caption/
    bts/critic use self-contained payloads (their turn text rides one
    ``payload``/``turn_payload`` row), so they cannot render the current user row
    twice. Of the three, only narrate/ooc actually feed the projection into
    ``history`` (assistant passes ``[]``).
    """
    for interaction, modules in DEFAULT_FLOWS.items():
        types = [m.type for m in modules]
        if interaction in ("narrate", "ooc", "assistant"):
            assert "history" in types and "user" in types, interaction
        else:
            assert "history" not in types and "user" not in types, interaction


def test_critic_flow_is_system_plus_payload():
    """D41/D49: the critic is a post-turn single-shot flow (system + payload).

    Its call site lives in ``graph/quality.py`` (wired by a separate lane); the
    flow SHAPE mirrors ``bts`` — one system prompt plus one user payload carrying
    the turn's prose, the validator flags and the state line. The stored
    ``system`` body overrides the engine part (D34 seam), so a preset author can
    edit the critic prompt without touching code.
    """
    assert [m.type for m in DEFAULT_FLOWS["critic"]] == ["system", "payload"]
    assert FLOW_STRUCTURAL_TYPES["critic"] == frozenset({"system", "payload"})
    parts = {"system": "CRITIC_SYSTEM", "payload": "TURN + FLAGS"}
    assert compile_flow("critic", DEFAULT_FLOWS["critic"], parts) == [
        {"role": "system", "content": "CRITIC_SYSTEM"},
        {"role": "user", "content": "TURN + FLAGS"},
    ]
    authored = [
        PromptModule("s", "S", "system", content="AUTHORED CRITIC"),
        PromptModule("p", "P", "payload"),
    ]
    assert compile_flow("critic", authored, parts)[0] == {
        "role": "system",
        "content": "AUTHORED CRITIC",
    }


def test_disabled_and_text_modules_apply_to_any_flow():
    modules = [
        PromptModule("sys", "S", "system", enabled=False),
        PromptModule("t", "T", "text", role="assistant", content="note {{user}}"),
        PromptModule("u", "U", "user"),
    ]
    messages = compile_flow(
        "ooc", modules, {"user_content": "go", "variables": {"user": "Bo"}}
    )
    assert messages == [
        {"role": "assistant", "content": "note Bo"},
        {"role": "user", "content": "go"},
    ]


# --- system module content override (SEAM) ---------------------------------


def test_system_module_content_wins_over_parts_system():
    """A non-empty stored ``system`` body is the flow's system message.

    The plain ``system`` inserter is the one structural type whose row body
    overrides the engine part — preset-authored system text rides the flow as
    data instead of a code constant.
    """
    modules = [
        PromptModule("sys", "S", "system", content="PRESET AUTHORED"),
        PromptModule("u", "U", "user"),
    ]
    messages = compile_flow(
        "ooc", modules, {"system": "ENGINE DEFAULT", "user_content": "go"}
    )
    assert messages == [
        {"role": "system", "content": "PRESET AUTHORED"},
        {"role": "user", "content": "go"},
    ]


def test_empty_system_module_keeps_engine_part_byte_identical():
    """An empty/whitespace-only body falls back to ``parts["system"]``.

    This is what keeps the default flows (whose ``system`` rows carry no body)
    compiling exactly as before the seam landed.
    """
    parts = {"system": "ENGINE DEFAULT", "history": [], "facts": "", "user_content": "go"}
    # The shipped default ooc flow's system row is empty -> engine part wins.
    assert compile_flow("ooc", DEFAULT_FLOWS["ooc"], dict(parts)) == [
        {"role": "system", "content": "ENGINE DEFAULT"},
        {"role": "user", "content": "go"},
    ]
    # Both "" and whitespace-only bodies take the fallback (never emit noise).
    for content in ("", "   \n  "):
        modules = [PromptModule("sys", "S", "system", content=content)]
        assert compile_flow("ooc", modules, dict(parts)) == [
            {"role": "system", "content": "ENGINE DEFAULT"}
        ]


def test_system_module_content_resolves_variables_like_text():
    """The override body renders ``{{variable}}`` exactly like a text module."""
    modules = [
        PromptModule("sys", "S", "system", content="You are {{char}} at {{loc}}.")
    ]
    messages = compile_flow(
        "ooc",
        modules,
        {"system": "ENGINE", "variables": {"char": "Lyra", "loc": "the market"}},
    )
    assert messages == [
        {"role": "system", "content": "You are Lyra at the market."}
    ]
    # Unknown vars stay literal (same contract as text modules) and a missing
    # ``variables`` mapping is tolerated.
    assert compile_flow(
        "ooc", modules, {"system": "ENGINE", "variables": {"char": "Lyra"}}
    )[0]["content"] == "You are Lyra at {{loc}}."
    assert compile_flow("ooc", modules, {"system": "ENGINE"})[0][
        "content"
    ] == "You are {{char}} at {{loc}}."


@pytest.mark.parametrize(
    "interaction",
    ["ooc", "extract", "summarize", "caption", "bts", "assistant"],
)
def test_system_module_override_applies_to_every_system_flow(interaction):
    """The seam covers every flow whose structural set includes ``system``."""
    modules = [PromptModule("sys", "S", "system", content="AUTHORED")]
    assert compile_flow(interaction, modules, {"system": "ENGINE"}) == [
        {"role": "system", "content": "AUTHORED"}
    ]


def test_unknown_structural_type_for_flow_raises_listing_valid():
    with pytest.raises(ValueError, match="valid types"):
        compile_flow(
            "ooc", [PromptModule("l", "L", "library")], {"system": "s"}
        )
    with pytest.raises(ValueError, match="unknown module type"):
        compile_flow(
            "extract", [PromptModule("h", "H", "history")], {"system": "s"}
        )


def test_compile_flow_unknown_interaction_raises():
    with pytest.raises(ValueError, match="unknown interaction"):
        compile_flow("bogus", [], {})


# --- preset store back-compat ----------------------------------------------


def test_preset_with_only_modules_normalizes_flows_narrate():
    preset = preset_from_json(
        {
            "name": "Legacy",
            "modules": [{"id": "u", "name": "U", "type": "user", "enabled": True}],
        }
    )
    assert [m["type"] for m in preset.flows["narrate"]] == ["user"]
    assert preset.flows["narrate"] == preset.modules


def test_preset_to_json_keeps_both_fields_in_sync():
    preset = preset_from_json(
        {"name": "P", "modules": [{"id": "u", "type": "user", "enabled": True}]}
    )
    payload = preset_to_json(preset)
    assert "modules" in payload and "flows" in payload
    assert payload["flows"]["narrate"] == payload["modules"]


def test_preset_from_json_reads_other_flows():
    preset = preset_from_json(
        {
            "name": "Full",
            "modules": [],
            "flows": {
                "narrate": [{"id": "n", "type": "user", "enabled": True}],
                "ooc": [{"id": "o", "type": "system", "enabled": True}],
            },
        }
    )
    # Empty modules adopt flows["narrate"] (canonicalization).
    assert [m["type"] for m in preset.modules] == ["user"]
    assert [m["type"] for m in preset.flows["ooc"]] == ["system"]


def test_preset_from_json_keeps_a_retired_attribution_flow_as_inert_data():
    """A preset saved before the removal still reads; nothing consumes its rows.

    Speaker attribution is deterministic code now, so the step id is gone from
    INTERACTIONS. Stored presets may still carry the flow (and its old system
    text): the read path keeps it as DATA and no compiler asks for it — the W6
    precedent for retired ids (``legacy_toggles`` / the ``sidekick`` fold) is
    "tolerate on read, never resurrect".
    """
    preset = preset_from_json(
        {
            "name": "Legacy",
            "modules": [{"id": "u", "type": "user", "enabled": True}],
            "flows": {
                "attribution": [
                    {
                        "id": "attribution_system",
                        "name": "Attribution system prompt",
                        "type": "system",
                        "content": "OLD MODEL PROMPT",
                        "enabled": True,
                    }
                ]
            },
        }
    )
    assert "attribution" in preset.flows
    assert "attribution" not in INTERACTIONS
    assert "attribution" not in DEFAULT_FLOWS
    # The live flows still hold; the retired one is never asked for.
    assert [m["type"] for m in preset.flows["narrate"]] == ["user"]


def test_seed_from_default_seeds_all_flows(tmp_path: Path):
    log = EventLog(tmp_path / "seed.db")
    try:
        preset = preset_store.get_preset(log, seed_from_default(log, "S"))
        assert preset is not None
        assert set(preset.flows) >= set(INTERACTIONS)
        for interaction in INTERACTIONS:
            assert [m["type"] for m in preset.flows[interaction]] == [
                m.type for m in DEFAULT_FLOWS[interaction]
            ]
        assert preset.modules == preset.flows["narrate"]
        # Editable copies: row ids are fresh, never the DEFAULT_FLOWS literals.
        seed_ids = {m["id"] for rows in preset.flows.values() for m in rows}
        assert seed_ids.isdisjoint(
            {m.id for rows in DEFAULT_FLOWS.values() for m in rows}
        )
    finally:
        log.close()


# --- retired prompt_overrides shim (W6.3) ------------------------------------


def _ooc_parts() -> dict:
    return {
        "system": "DEFAULT_OOC",
        "history": [],
        "facts": "",
        "user_content": "hi",
    }


def test_compile_preset_flow_ignores_prompt_overrides():
    """The retired override shim is no longer consulted (W6.3, owner decision).

    A legacy payload still carries ``prompt_overrides``; the engine default (or
    the flow's own ``system`` row) must win — never the shim.
    """
    preset = preset_from_json(
        {"name": "Shim", "modules": [], "prompt_overrides": {"ooc_system": "OVERRIDE"}}
    )
    # flows has no ooc entry -> the all-default ooc flow renders the parts system.
    messages = compile_preset_flow(preset, "ooc", _ooc_parts())
    assert messages[0] == {"role": "system", "content": "DEFAULT_OOC"}
    assert all(m["content"] != "OVERRIDE" for m in messages)


def test_compile_preset_flow_ignores_extractor_overrides():
    preset = preset_from_json(
        {
            "name": "Shim",
            "modules": [],
            "prompt_overrides": {"extractor_system": "EXTRACT_OVERRIDE"},
        }
    )
    messages = compile_preset_flow(
        preset, "extract", {"system": "DEFAULT", "turn_payload": "T"}
    )
    assert messages[0] == {"role": "system", "content": "DEFAULT"}


def test_flow_wins_and_shim_is_inert():
    """A customized flow wins; the legacy override never surfaces."""
    preset = preset_from_json(
        {
            "name": "Edited",
            "modules": [],
            "prompt_overrides": {"ooc_system": "OVERRIDE"},
            "flows": {
                "ooc": [
                    {"id": "sys", "name": "S", "type": "text",
                     "role": "system", "content": "CUSTOM", "enabled": True},
                    {"id": "u", "name": "U", "type": "user",
                     "role": "user", "content": "", "enabled": True},
                ]
            },
        }
    )
    messages = compile_preset_flow(preset, "ooc", _ooc_parts())
    assert messages[0] == {"role": "system", "content": "CUSTOM"}
    assert all(m["content"] != "OVERRIDE" for m in messages)


def test_nonempty_system_module_body_is_the_authoring_seam():
    """A non-empty ``system`` row body is the one system-prompt authoring seam.

    With the shim retired, the row body is what renders; a legacy
    ``prompt_overrides`` value is ignored entirely.
    """
    preset = preset_from_json(
        {
            "name": "Authored",
            "modules": [],
            "prompt_overrides": {"ooc_system": "SHIM"},
            "flows": {
                "ooc": [
                    {"id": "sys", "name": "S", "type": "system",
                     "role": "system", "content": "AUTHORED", "enabled": True},
                    {"id": "u", "name": "U", "type": "user",
                     "role": "user", "content": "", "enabled": True},
                ]
            },
        }
    )
    assert not flow_is_default("ooc", preset.flows["ooc"])
    messages = compile_preset_flow(preset, "ooc", _ooc_parts())
    assert messages[0] == {"role": "system", "content": "AUTHORED"}
    assert all(m["content"] != "SHIM" for m in messages)


def test_clearing_system_body_renders_the_parts_system_not_the_shim():
    """Emptying the system body returns the flow to all-default -> the parts system.

    The retired shim is not restored: an all-default flow falls back to the
    engine's compiled default, never to ``prompt_overrides``.
    """

    def preset_with(content: str):
        return preset_from_json(
            {
                "name": "Toggle",
                "modules": [],
                "prompt_overrides": {"ooc_system": "SHIM"},
                "flows": {
                    "ooc": [
                        {"id": "sys", "name": "S", "type": "system",
                         "role": "system", "content": content, "enabled": True},
                        {"id": "h", "name": "H", "type": "history",
                         "role": "system", "content": "", "enabled": True},
                        {"id": "f", "name": "F", "type": "facts",
                         "role": "system", "content": "", "enabled": True},
                        {"id": "u", "name": "U", "type": "user",
                         "content": "", "enabled": True},
                    ]
                },
            }
        )

    authored = preset_with("AUTHORED")
    assert compile_preset_flow(authored, "ooc", _ooc_parts())[0] == {
        "role": "system",
        "content": "AUTHORED",
    }

    cleared = preset_with("")
    # Same shape as the shipped default ooc flow -> all-default -> parts system.
    assert flow_is_default("ooc", cleared.flows["ooc"])
    assert compile_preset_flow(cleared, "ooc", _ooc_parts())[0] == {
        "role": "system",
        "content": "DEFAULT_OOC",
    }


def test_default_flow_without_override_uses_parts_system():
    preset = preset_from_json({"name": "Plain", "modules": []})
    messages = compile_preset_flow(preset, "ooc", _ooc_parts())
    assert messages[0] == {"role": "system", "content": "DEFAULT_OOC"}


# --- preview API ------------------------------------------------------------


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _seed_session(log: EventLog, session_id: str = "s1") -> None:
    log.append_event(session_id, "user", "user", {"text": "hello"})


def test_preview_bad_interaction_is_422(build_app):
    build, _ = build_app
    app = build()
    _seed_session(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "hi", "interaction": "bogus"},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"].startswith("unknown interaction")
    assert detail["known_interactions"] == list(INTERACTIONS)


def test_preview_narrate_degraded_mode_preserved(build_app):
    build, _ = build_app
    app = build()
    _seed_session(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "go"},
            )

    body = run(scenario()).json()
    assert body["preview_mode"] == "degraded"


def test_preview_narrate_builder_mode(build_app):
    build, _ = build_app
    app = build()
    _seed_session(app.state.log)

    async def builder(state):
        return [{"role": "system", "content": "BUILT"}]

    app.state.message_builder = builder

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "go"},
            )

    body = run(scenario()).json()
    assert body["preview_mode"] == "builder"
    assert body["messages"] == [{"role": "system", "content": "BUILT"}]


def test_preview_ooc_flow_mode_shape(build_app):
    build, _ = build_app
    app = build()
    _seed_session(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "answer", "interaction": "ooc"},
            )

    body = run(scenario()).json()
    assert body["preview_mode"] == "flow"
    # system (OOC default) + the seeded history + empty facts + the user turn.
    assert body["messages"][0]["role"] == "system"
    assert "out-of-character" in body["messages"][0]["content"]
    assert {"role": "user", "content": "hello"} in body["messages"]
    assert body["messages"][-1] == {"role": "user", "content": "answer"}
    assert body["chars"] == sum(len(str(m["content"])) for m in body["messages"])


def test_preview_flow_ignores_legacy_prompt_overrides(build_app):
    """Preview == reality: the retired override shim never reaches the model."""
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_session(log)
    pid = preset_store.append_preset(
        log, {"name": "WithOverride", "modules": [], "prompt_overrides": {"ooc_system": "SHIMMED"}}
    )
    preset_store.set_active(log, pid)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "answer", "interaction": "ooc"},
            )

    body = run(scenario()).json()
    assert body["preview_mode"] == "flow"
    assert all(m["content"] != "SHIMMED" for m in body["messages"])
    assert "out-of-character" in body["messages"][0]["content"]


def test_preview_flow_customized_preset_flow_wins(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_session(log)
    pid = preset_store.append_preset(
        log,
        {
            "name": "Custom",
            "modules": [],
            "prompt_overrides": {"ooc_system": "SHIMMED"},
            "flows": {
                "ooc": [
                    {"id": "sys", "name": "S", "type": "text",
                     "role": "system", "content": "CUSTOM", "enabled": True},
                    {"id": "u", "name": "U", "type": "user",
                     "role": "user", "content": "", "enabled": True},
                ]
            },
        },
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={
                    "session_id": "s1",
                    "user_text": "answer",
                    "interaction": "ooc",
                    "preset_id": pid,
                },
            )

    body = run(scenario()).json()
    assert body["preview_mode"] == "flow"
    assert body["messages"][0]["content"] == "CUSTOM"


def test_preview_flow_uses_authored_system_module_content(build_app):
    """Preview == reality: a preset-authored ``system`` body rides the flow.

    The preview endpoint routes through ``compile_preset_flow``, so a
    non-empty system module body must show up (and beat the override shim)
    without any API change.
    """
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_session(log)
    pid = preset_store.append_preset(
        log,
        {
            "name": "Authored",
            "modules": [],
            "prompt_overrides": {"ooc_system": "SHIMMED"},
            "flows": {
                "ooc": [
                    {"id": "sys", "name": "S", "type": "system",
                     "role": "system", "content": "AUTHORED", "enabled": True},
                    {"id": "u", "name": "U", "type": "user",
                     "role": "user", "content": "", "enabled": True},
                ]
            },
        },
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={
                    "session_id": "s1",
                    "user_text": "answer",
                    "interaction": "ooc",
                    "preset_id": pid,
                },
            )

    body = run(scenario()).json()
    assert body["preview_mode"] == "flow"
    assert body["messages"][0] == {"role": "system", "content": "AUTHORED"}
    assert all(m["content"] != "SHIMMED" for m in body["messages"])


def test_create_preset_wire_carries_modules_and_flows(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/prompts/presets", json={"name": "N"})

    preset = run(scenario()).json()["preset"]
    assert "flows" in preset
    assert preset["flows"]["narrate"] == preset["modules"]


def test_compile_preset_flow_with_no_active_preset_renders_default():
    """preset=None is the NORMAL state (no preset active) — never a 500.

    Found live: a photo attachment upload crashed the caption flow renderer
    (preset None reached ``preset.flows``). Every flow call site passes the
    active preset or None; None must render the default flow.
    """
    from fabulist.prompts.preset_store import compile_preset_flow
    from fabulist.prompts.modules import DEFAULT_FLOWS

    parts = {"system": "S", "payload": "P", "variables": {}}
    messages = compile_preset_flow(None, "caption", parts)
    assert messages == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "P"},
    ]
    # and the narrate None path mirrors compile_preset_messages defaults
    narrate = compile_preset_flow(
        None,
        "narrate",
        {"narrator_system": "NS", "library": "L", "history": [], "facts": "", "user_content": "U"},
    )
    # Issue #44: the library row folds into the ONE prefix system message.
    assert narrate[0]["content"] == "NS\n\nL"


# --- default endpoint (D18 inheritance source) ------------------------------


def test_default_endpoint_covers_every_interaction_flow(build_app):
    """GET /api/prompts/default returns one flow per INTERACTION (read-only).

    The D18 UI reads this to inherit an unset flow's modules; missing a flow
    would silently render "No modules" for it. The narrator flow must also carry
    the D22 ``recalled`` row so the UI can render it as a structural block.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/default")

    resp = run(scenario())
    assert resp.status_code == 200
    preset = resp.json()["preset"]
    assert set(preset["flows"]) == set(INTERACTIONS)
    assert preset["modules"] == preset["flows"]["narrate"]
    assert [m["type"] for m in preset["flows"]["narrate"]] == [
        m.type for m in DEFAULT_FLOWS["narrate"]
    ]
    assert "recalled" in {m["type"] for m in preset["flows"]["narrate"]}
    for interaction, rows in DEFAULT_FLOWS.items():
        assert [m["type"] for m in preset["flows"][interaction]] == [
            m.type for m in rows
        ]


def test_default_endpoint_writes_no_preset_event(build_app):
    """The default endpoint is a pure read: it must not seed a preset."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/default")

    run(scenario())
    assert preset_store.list_presets(app.state.log) == []
    # and the wire payload never claims to be an active preset
    assert run(scenario()).json()["preset"]["preset_id"] == "default"
