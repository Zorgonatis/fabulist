"""Tests for D19 agent sets (interaction -> agent bindings as preset data).

The DEFAULT_BINDINGS assertions pin today's wiring, citing each call site in
``wiring.py`` / ``state/extractor.py`` / ``assistants`` (see the module docstring
of :mod:`fabulist.prompts.agent_sets`).

No pytest-asyncio needed — everything here is synchronous. The preset-store
dependency is bypassed with the module-level ``set_bindings_provider`` seam,
restored after every test so the global can never leak between tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.connections.taxonomy import resolve_interaction_agent
from fabulist.prompts.agent_sets import (
    DEFAULT_BINDINGS,
    INTERACTIONS,
    bindings_for,
    resolve_agent,
    resolve_overrides,
    set_bindings_provider,
    validate_bindings,
)


@pytest.fixture(autouse=True)
def _restore_provider():
    """Guarantee the global provider seam is cleared around every test."""

    set_bindings_provider(None)
    yield
    set_bindings_provider(None)


# -- DEFAULT_BINDINGS matches today's wiring ---------------------------------


def test_default_bindings_cover_every_interaction() -> None:
    assert set(DEFAULT_BINDINGS) == set(INTERACTIONS)


def test_critic_joins_the_interaction_set() -> None:
    # D41/D49: the post-turn critic is a first-class interaction with its own
    # binding; preset data (D19) may rebind it in the UI.
    assert "critic" in INTERACTIONS
    assert "critic" in DEFAULT_BINDINGS


def test_default_bindings_match_current_wiring() -> None:
    # narrate + ooc -> narrator: wiring.build_engine builds
    # RegistryModelCaller(narrator_agent, registry) with narrator_agent default
    # "narrator"; GraphTurnEngine(model=caller) serves both narrate and ooc.
    assert resolve_agent(DEFAULT_BINDINGS, "narrate", fallback="?") == "narrator"
    assert resolve_agent(DEFAULT_BINDINGS, "ooc", fallback="?") == "narrator"
    # extract -> extractor: state/extractor.py LLMExtractor build_request("extractor").
    assert resolve_agent(DEFAULT_BINDINGS, "extract", fallback="?") == "extractor"
    # summarize -> summarizer: wiring.build_app RegistryModelCaller("summarizer", ...).
    assert resolve_agent(DEFAULT_BINDINGS, "summarize", fallback="?") == "summarizer"
    # caption -> captioner: wiring.build_app app.state.captioner = RegistryModelCaller("captioner", ...).
    assert resolve_agent(DEFAULT_BINDINGS, "caption", fallback="?") == "captioner"
    # assistant -> per-definition agent, default narrator (assistants/manager.py;
    # api/assistants_api.py _coerce_definition).
    assert resolve_agent(DEFAULT_BINDINGS, "assistant", fallback="?") == "narrator"
    # critic -> extractor: D49 — the critic runs in-graph at the tail of the turn
    # and holds the turn lock until it returns, so it defaults to the same cheap
    # extractor-class agent as bts (the D19 preset may rebind it).
    assert resolve_agent(DEFAULT_BINDINGS, "critic", fallback="?") == "extractor"


def test_proposal_lane_binds_to_the_extractor_class() -> None:
    # W9.22 §3 (doctrine 4): proposal AUTHORING is extractor-class generation —
    # the decision model only JUDGES each item and cannot author content — so the
    # lane joins bts/critic on the cheap extractor agent. One call per data
    # type, and only on a turn the double gate admits: this is the cost knob.
    assert "proposals" in INTERACTIONS
    assert resolve_agent(DEFAULT_BINDINGS, "proposals", fallback="?") == "extractor"


def test_npc_lanes_default_to_the_narration_task() -> None:
    # W9.25 §3.4: the lanes are narrative-voice work, so ``npc`` names the
    # NARRATION task (the taxonomy's canonical spelling) instead of the
    # W9.13 extractor cost classing. On any legacy connections document the
    # narration task resolves through its ``narrator`` twin; lane cost is the
    # dispatch policy's knob (W9.24-W5), never the model class.
    assert DEFAULT_BINDINGS["npc"]["agent"] == "completion.narration"
    assert resolve_agent(DEFAULT_BINDINGS, "npc", fallback="?") == (
        "completion.narration"
    )
    # every OTHER default keeps its legacy spelling (zero display churn)
    legacy_spellings = {
        interaction: binding["agent"]
        for interaction, binding in DEFAULT_BINDINGS.items()
        if interaction != "npc"
    }
    assert "completion.narration" not in legacy_spellings.values()
    assert set(legacy_spellings.values()) == {
        "narrator",
        "extractor",
        "summarizer",
        "captioner",
    }


def test_per_item_defaults_precede_authored_task_keys() -> None:
    """R3 fold (W9.25 §3.2): the per-item rows keep their own connection.

    A literal task-before-default order silently rerouted caption/summarize to
    an authored ``completion.extraction`` — contradicting "exactly one behavior
    change at upgrade". ``bts`` defers to the task because its default is the
    ``extractor`` TWIN, not a per-item slot.
    """
    agents = {
        "completion.extraction": "extraction-conn",
        "summarizer": "summarizer-conn",
        "captioner": "captioner-conn",
        "extractor": "extractor-conn",
        "narrator": "narrator-conn",
    }
    assert resolve_interaction_agent(None, agents, "summarize", "summarizer") == (
        "summarizer"
    )
    assert resolve_interaction_agent(None, agents, "caption", "captioner") == (
        "captioner"
    )
    # bts defers to the authored task key even though its default is ``extractor``
    with_state = {**agents, "completion.state": "state-conn"}
    assert resolve_interaction_agent(None, with_state, "bts", "extractor") == (
        "completion.state"
    )
    # and an EXPLICIT preset binding still beats every default — resolved to the
    # matched slot key, so binding the ``extractor`` twin on a config that
    # authors ``completion.extraction`` reports the typed spelling
    bindings = {"caption": {"agent": "extractor", "overrides": {}}}
    assert resolve_interaction_agent(bindings, agents, "caption", "captioner") == (
        "completion.extraction"
    )
    assert resolve_interaction_agent(
        {"caption": {"agent": "narrator"}}, agents, "caption", "captioner"
    ) == "narrator"


def test_default_bindings_have_empty_overrides() -> None:
    for interaction in INTERACTIONS:
        assert resolve_overrides(DEFAULT_BINDINGS, interaction) == {}


#: The pre-W9.25 wiring, as the D19 defaults named it (``resolve_agent`` over the
#: old table): interaction -> the agent the engine called before typed slots.
_PRE_TAXONOMY_AGENT: dict[str, str] = {
    "narrate": "narrator",
    "ooc": "narrator",
    "extract": "extractor",
    "summarize": "summarizer",
    "caption": "captioner",
    "assistant": "narrator",
    "bts": "extractor",
    "critic": "extractor",
    "npc": "extractor",
    "planner": "narrator",
    "proposals": "extractor",
}

#: A legacy connections document: the owner's seven keys, no typed spelling.
_LEGACY_CONFIG: dict[str, str] = {
    "narrator": "narrator-conn",
    "extractor": "extractor-conn",
    "summarizer": "summarizer-conn",
    "captioner": "captioner-conn",
    "imagegen": "imagegen-conn",
    "embedder": "embedder-conn",
    "decision": "decision-conn",
}


def test_legacy_config_changes_exactly_one_interaction_at_upgrade() -> None:
    """W9.25 §6: ``npc`` is the ONE behavior change at upgrade.

    On a legacy connections document every other interaction resolves to the
    exact slot the engine called before the taxonomy existed, so no other step
    silently moves model class. ``npc`` moves extractor -> narrator, which is
    the point of the wave (lanes are narrative-voice work).
    """
    assert set(_PRE_TAXONOMY_AGENT) == set(INTERACTIONS)
    changed: dict[str, tuple[str, str]] = {}
    for interaction, before in _PRE_TAXONOMY_AGENT.items():
        after = resolve_interaction_agent(
            None, _LEGACY_CONFIG, interaction, before
        )
        if after != before:
            changed[interaction] = (before, after)
    assert changed == {"npc": ("extractor", "narrator")}


# -- validate_bindings -------------------------------------------------------


def test_validate_accepts_defaults_with_known_agents() -> None:
    # ``extractor`` is no longer among the shipped defaults' spellings (npc
    # moved to the narration task), so this set also proves the W9.25 §3.4
    # union: ``completion.narration`` is accepted WITHOUT being a registry key.
    known = {"narrator", "extractor", "summarizer", "captioner"}
    validate_bindings(DEFAULT_BINDINGS, known_agents=known)


def test_validate_accepts_a_declared_spelling_without_an_authored_key() -> None:
    # A preset may bind a typed slot the connections document does not author:
    # resolution falls through the taxonomy hierarchy at call time, so the
    # spelling is legal even when only a legacy key exists.
    validate_bindings(
        {"npc": {"agent": "completion.extraction"}}, known_agents={"narrator"}
    )


def test_validate_rejects_a_spelling_outside_the_declared_vocabulary() -> None:
    # Typo protection: dotted spellings are validated against the taxonomy's
    # vocabulary, so a mistyped task never silently degrades at call time.
    with pytest.raises(ValueError, match="unknown agent"):
        validate_bindings(
            {"npc": {"agent": "completion.bogus"}}, known_agents={"narrator"}
        )


def test_validate_rejects_unknown_interaction() -> None:
    with pytest.raises(ValueError, match="unknown interaction"):
        validate_bindings({"narratte": {"agent": "narrator"}})


def test_validate_rejects_unknown_agent() -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        validate_bindings(
            {"narrate": {"agent": "ghost"}},
            known_agents={"narrator", "extractor"},
        )


def test_validate_reports_both_unknown_interaction_and_agent() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_bindings(
            {"nope": {"agent": "narrator"}, "narrate": {"agent": "ghost"}},
            known_agents={"narrator"},
        )
    message = str(excinfo.value)
    assert "unknown interaction" in message
    assert "unknown agent" in message


def test_validate_rejects_non_mapping_and_missing_agent() -> None:
    with pytest.raises(ValueError, match="must be an object mapping"):
        validate_bindings("not-a-mapping")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="missing required string 'agent'"):
        validate_bindings({"narrate": {"overrides": {}}})


def test_validate_without_known_agents_skips_agent_check() -> None:
    # No known set supplied: interaction shape alone is validated.
    validate_bindings({"narrate": {"agent": "anything-goes"}})


# -- resolve_agent / resolve_overrides ---------------------------------------


def test_resolve_agent_bindings_win_over_fallback() -> None:
    bindings = {"narrate": {"agent": "custom-narrator"}}
    assert resolve_agent(bindings, "narrate", fallback="narrator") == "custom-narrator"


def test_resolve_agent_missing_falls_back() -> None:
    assert resolve_agent({}, "narrate", fallback="narrator") == "narrator"
    assert resolve_agent(None, "narrate", fallback="narrator") == "narrator"
    assert (
        resolve_agent({"narrate": {}}, "narrate", fallback="narrator") == "narrator"
    )


def test_resolve_overrides_returns_dict_or_empty() -> None:
    bindings = {"extract": {"agent": "extractor", "overrides": {"temperature": 0.1}}}
    assert resolve_overrides(bindings, "extract") == {"temperature": 0.1}
    assert resolve_overrides(bindings, "narrate") == {}
    assert resolve_overrides(None, "narrate") == {}


# -- bindings_for (preset store seam) ----------------------------------------


def test_bindings_for_stubbed_provider_returns_preset_bindings() -> None:
    preset = {"narrate": {"agent": "big-model", "overrides": {"temperature": 0.9}}}
    set_bindings_provider(lambda log: preset)
    assert bindings_for(object()) == preset


def test_bindings_for_absent_preset_falls_back_to_defaults() -> None:
    set_bindings_provider(lambda log: None)
    assert bindings_for(object()) == DEFAULT_BINDINGS


def test_bindings_for_empty_preset_falls_back_to_defaults() -> None:
    set_bindings_provider(lambda log: {})
    assert bindings_for(object()) == DEFAULT_BINDINGS


def test_bindings_for_provider_error_falls_back_to_defaults() -> None:
    def boom(log):
        raise RuntimeError("preset store exploded")

    set_bindings_provider(boom)
    assert bindings_for(object()) == DEFAULT_BINDINGS


def test_bindings_for_without_stub_does_not_raise() -> None:
    # preset_store does not exist yet (parallel lane); the live path must still
    # degrade to DEFAULT_BINDINGS rather than import-error.
    set_bindings_provider(None)
    assert bindings_for(object()) == DEFAULT_BINDINGS


# -- the wiring seam (the ONE taxonomy resolution) ----------------------------

#: The seams that resolve an interaction's slot at call time (W9.25 §3.4).
_WIRING_SEAMS = (
    "_caller",  # PresetAgentCaller — every caller-wrapped interaction
    "_bts_request_profile",
    "_bts_loop_timeout",
    "bts_window_budget",  # W25.5: the BTS slot's passive headroom reader
    "_planner_request_profile",
    "_narrate_loop_capabilities",
    "_narrate_loop_timeout",
    # The reasoning-echo readers' ONE slot resolution ("Return thinking"): the
    # narrate/BTS/planner tool loops all resolve their connection through it, so
    # the echo setting cannot drift from the connection the loop actually calls.
    "_loop_connection",
    # W9.26 §2.3 step 2: the ONE slot resolution the W9.26 seams share
    # (``_narrate_connection`` / ``summarizer_budget_tokens`` / the summarize
    # caller all route through it), so the Context Size consumers and the
    # summarizer cannot resolve the same interaction two different ways.
    "_interaction_slot",
)


def _wiring_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "src" / "fabulist" / "wiring.py").read_text(encoding="utf-8")


def test_every_wiring_seam_resolves_through_the_taxonomy() -> None:
    """W9.25 §3.4: ONE resolution function, never a per-seam re-derivation.

    A seam still calling ``agent_sets.resolve_agent`` (which knows nothing about
    the item > task > type hierarchy) would silently ignore typed slots — the
    npc fix included — so this pin fails on any such call site.
    """
    source = _wiring_source()
    assert "resolve_agent(" not in source
    # exactly one resolution call per registered seam — a partial swap (or a
    # second, divergent resolution path) fails the count
    assert source.count("resolve_interaction_agent(") == len(_WIRING_SEAMS)
    for seam in _WIRING_SEAMS:
        assert f"def {seam}" in source, seam
