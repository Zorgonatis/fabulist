"""Tests for the W9.25 agent taxonomy: types x tasks over the flat agents map.

The load-bearing pins live here:

* the vocabulary is exactly the plan's (§3.2), including ``embedder`` DECLARED in
  ``ITEM_SLOTS`` as ``("embedding", None)``;
* the bidirectional twin rule — resolving ``narrator`` and resolving
  ``completion.narration`` are ONE lookup, pinned both ways (R2 fold P1-1);
* zero migration — a config with only the seven legacy keys resolves every key,
  unchanged;
* precedence item > task > type, typed spelling beats legacy twin;
* authored-but-undeclared custom keys resolve as per-item slots and render;
* ``slot_view`` (the drawer/API payload) reports the SAME resolution the registry
  performs, with provenance.
"""

from __future__ import annotations

import pytest

from fabulist.connections import taxonomy
from fabulist.connections.registry import ConnectionConfigError, Registry
from fabulist.prompts.agent_sets import DEFAULT_BINDINGS, INTERACTIONS

#: The seven keys a live/legacy ``connections.json`` authors today.
LEGACY_CONFIG = {
    "narrator": "your-model-id",
    "extractor": "qwen35-4b",
    "summarizer": "qwen35-4b",
    "captioner": "your-model-id",
    "embedder": "embedder",
    "decision": "d1-3b",
    "imagegen": "qwen35-4b",
}


def _registry(agents: dict[str, str]) -> Registry:
    """A registry with one connection per distinct value in ``agents``."""
    names = sorted(set(agents.values()))
    return Registry.from_dict(
        {
            "connections": {
                name: {"base_url": "http://host/v1", "model": name} for name in names
            },
            "agents": dict(agents),
        }
    )


# --- vocabulary -------------------------------------------------------------------


def test_vocabulary_is_the_declared_shape():
    assert taxonomy.TYPES == ("completion", "embedding", "decision")
    assert taxonomy.TASKS == {"completion": ("narration", "extraction", "state")}
    assert taxonomy.TYPED_SLOTS == (
        "completion",
        "completion.narration",
        "completion.extraction",
        "completion.state",
        "embedding",
    )
    assert taxonomy.LEGACY_TWINS == {
        "narrator": "completion.narration",
        "extractor": "completion.extraction",
    }


def test_embedder_is_a_declared_item_slot():
    """R2 fold P2-7: the live embedder key resolves through the taxonomy."""
    assert taxonomy.ITEM_SLOTS["embedder"] == ("embedding", None)
    assert set(taxonomy.ITEM_SLOTS) == {
        "summarizer",
        "captioner",
        "imagegen",
        "embedder",
    }


def test_declared_spellings_covers_typed_legacy_and_item_slots():
    assert taxonomy.declared_spellings() == frozenset(
        {
            "completion",
            "completion.narration",
            "completion.extraction",
            "completion.state",
            "embedding",
            "decision",
            "narrator",
            "extractor",
            "summarizer",
            "captioner",
            "imagegen",
            "embedder",
        }
    )


def test_canonical_maps_twins_to_their_typed_spelling():
    assert taxonomy.canonical("narrator") == "completion.narration"
    assert taxonomy.canonical("extractor") == "completion.extraction"
    for name in ("completion", "completion.state", "embedding", "decision", "summarizer", "x"):
        assert taxonomy.canonical(name) == name


def test_every_interaction_declares_a_legal_task():
    assert set(taxonomy.INTERACTION_TASKS) == set(INTERACTIONS)
    for interaction, (type_name, task_name) in taxonomy.INTERACTION_TASKS.items():
        assert type_name in taxonomy.TYPES, interaction
        assert task_name in taxonomy.TASKS.get(type_name, ()), interaction


# --- the bidirectional twin rule (R2 fold P1-1) -----------------------------------


@pytest.mark.parametrize("legacy, typed", sorted(taxonomy.LEGACY_TWINS.items()))
def test_twin_pair_resolves_both_directions(legacy, typed):
    """Resolving either spelling finds the other — ONE rule, both directions."""
    agents = {typed: "typed-conn"}
    assert taxonomy.resolve_slot(agents, legacy) == "typed-conn"
    assert taxonomy.resolve_slot(agents, typed) == "typed-conn"

    agents = {legacy: "legacy-conn"}
    assert taxonomy.resolve_slot(agents, legacy) == "legacy-conn"
    assert taxonomy.resolve_slot(agents, typed) == "legacy-conn"


def test_both_spellings_share_one_candidate_list():
    assert taxonomy.candidate_slots("narrator") == taxonomy.candidate_slots(
        "completion.narration"
    )
    assert taxonomy.candidate_slots("extractor") == taxonomy.candidate_slots(
        "completion.extraction"
    )


def test_typed_spelling_beats_its_legacy_twin():
    agents = {"narrator": "legacy-conn", "completion.narration": "typed-conn"}
    # Both spellings are the SAME lookup, so both see the typed key win.
    assert taxonomy.resolve_slot(agents, "narrator") == "typed-conn"
    assert taxonomy.resolve_slot(agents, "completion.narration") == "typed-conn"

    registry = _registry(
        {"narrator": "legacy-conn", "completion.narration": "typed-conn"}
    )
    assert registry.connection_for("narrator").name == "typed-conn"
    assert registry.connection_for("completion.narration").name == "typed-conn"


# --- hierarchy: item > task > type ------------------------------------------------


def test_candidate_order_is_item_then_task_then_type():
    assert taxonomy.candidate_slots("narrator") == (
        "completion.narration",
        "narrator",
        "completion",
    )
    assert taxonomy.candidate_slots("summarizer") == (
        "summarizer",
        "completion.extraction",
        "extractor",
        "completion",
    )
    assert taxonomy.candidate_slots("imagegen") == ("imagegen", "completion")
    assert taxonomy.candidate_slots("embedder") == ("embedder", "embedding")
    assert taxonomy.candidate_slots("completion") == ("completion",)
    assert taxonomy.candidate_slots("embedding") == ("embedding",)
    assert taxonomy.candidate_slots("decision") == ("decision",)
    assert taxonomy.candidate_slots("my-own-slot") == ("my-own-slot",)


def test_item_key_wins_over_task_and_type():
    agents = {"summarizer": "item", "completion.extraction": "task", "completion": "type"}
    assert taxonomy.resolve_slot(agents, "summarizer") == "item"


def test_task_key_wins_when_the_item_is_unauthored():
    agents = {"completion.extraction": "task", "completion": "type"}
    assert taxonomy.resolve_slot(agents, "summarizer") == "task"
    # the legacy spelling of the task key counts as the task level too
    assert taxonomy.resolve_slot({"extractor": "legacy"}, "summarizer") == "legacy"


def test_type_key_is_the_last_resort():
    assert taxonomy.resolve_slot({"completion": "type"}, "summarizer") == "type"
    assert taxonomy.resolve_slot({"completion": "type"}, "imagegen") == "type"
    assert taxonomy.resolve_slot({"completion": "type"}, "narrator") == "type"


def test_embedder_resolves_through_the_embedding_type():
    assert taxonomy.resolve_slot({"embedder": "own"}, "embedder") == "own"
    assert taxonomy.resolve_slot({"embedding": "type"}, "embedder") == "type"


def test_undeclared_custom_key_resolves_as_a_per_item_slot():
    assert "my-own-slot" not in taxonomy.declared_spellings()
    assert taxonomy.resolve_slot({"my-own-slot": "custom-conn"}, "my-own-slot") == (
        "custom-conn"
    )
    # an undeclared key gets no hierarchy of its own
    assert taxonomy.resolve_slot({"completion": "type"}, "my-own-slot") is None


def test_unresolvable_slot_is_none():
    assert taxonomy.resolve_slot({}, "narrator") is None
    assert taxonomy.resolve_slot(LEGACY_CONFIG, "ghost") is None


def test_legacy_config_resolves_every_key_unchanged():
    """Zero migration: no typed key authored -> each key resolves to its own value."""
    for name, connection in LEGACY_CONFIG.items():
        assert taxonomy.resolve_slot(LEGACY_CONFIG, name) == connection

    registry = _registry(LEGACY_CONFIG)
    for name, connection in LEGACY_CONFIG.items():
        assert registry.connection_for(name).name == connection


def test_registry_names_the_slot_and_the_vocabulary_on_failure():
    registry = _registry({"embedder": "emb"})
    with pytest.raises(ConnectionConfigError) as excinfo:
        registry.connection_for("summarizer")
    message = str(excinfo.value)
    assert "summarizer" in message
    assert "completion.narration" in message  # the declared vocabulary is in the error


# --- interaction resolution (the preset layer's default) --------------------------


def test_preset_binding_wins_when_its_spelling_resolves():
    agents = {"narrator": "n", "captioner": "c"}
    bindings = {"narrate": {"agent": "captioner", "overrides": {}}}
    assert taxonomy.resolve_interaction_agent(bindings, agents, "narrate", "narrator") == (
        "captioner"
    )


def test_a_binding_naming_an_unresolvable_slot_degrades_to_the_task():
    agents = {"narrator": "n"}
    bindings = {"narrate": {"agent": "ghost", "overrides": {}}}
    assert taxonomy.resolve_interaction_agent(bindings, agents, "narrate", "fallback") == (
        "narrator"
    )


def test_task_slot_beats_the_default_binding():
    """npc/planner/etc. ride the interaction's task slot (W9.25 §3.2 step 2)."""
    agents = {"narrator": "n", "extractor": "e"}
    assert taxonomy.resolve_interaction_agent(None, agents, "npc", "extractor") == (
        "narrator"
    )
    assert taxonomy.resolve_interaction_agent(None, agents, "bts", "extractor") == (
        "extractor"
    )

    # authoring the state task key moves ONLY the state interaction
    with_state = {**agents, "completion.state": "state-conn"}
    assert taxonomy.resolve_interaction_agent(None, with_state, "bts", "extractor") == (
        "completion.state"
    )
    assert taxonomy.resolve_interaction_agent(None, with_state, "critic", "extractor") == (
        "extractor"
    )


def test_type_slot_is_used_when_no_task_key_resolves():
    agents = {"completion": "type-conn"}
    assert taxonomy.resolve_interaction_agent(None, agents, "bts", "fallback") == (
        "completion"
    )


def test_default_binding_applies_when_the_task_layer_does_not_resolve():
    # A config authoring only the extractor: the state task finds nothing, so
    # bts falls back to its shipped default binding (extractor).
    assert taxonomy.resolve_interaction_agent(None, {"extractor": "e"}, "bts", "x") == (
        "extractor"
    )
    # and the default binding's own spelling resolves through the twin rule
    assert taxonomy.resolve_interaction_agent(
        None, {"completion.narration": "typed-n"}, "narrate", "x"
    ) == ("completion.narration")


def test_fallback_is_returned_when_nothing_resolves():
    assert taxonomy.resolve_interaction_agent(None, {}, "npc", "my-fallback") == (
        "my-fallback"
    )
    assert taxonomy.resolve_interaction_agent({}, {}, "narrate", "my-fallback") == (
        "my-fallback"
    )


def test_every_default_binding_is_a_declared_or_legacy_spelling():
    for interaction, binding in DEFAULT_BINDINGS.items():
        assert interaction in INTERACTIONS
        assert binding["agent"] in taxonomy.declared_spellings()


# --- slot_view (GET /api/connections ``slots``) -----------------------------------


def test_slot_view_orders_types_tasks_then_items():
    rows = taxonomy.slot_view(LEGACY_CONFIG | {"my-own-slot": "custom-conn"})
    assert [row["slot"] for row in rows] == [
        "completion",
        "completion.narration",
        "completion.extraction",
        "completion.state",
        "summarizer",
        "captioner",
        "imagegen",
        "embedding",
        "embedder",
        "decision",
        "my-own-slot",
    ]


def test_slot_view_reports_resolution_and_provenance():
    rows = {row["slot"]: row for row in taxonomy.slot_view(LEGACY_CONFIG)}

    narration = rows["completion.narration"]
    assert narration == {
        "slot": "completion.narration",
        "type": "completion",
        "task": "narration",
        "label": "Narration",
        "canonical": "completion.narration",
        "aliases": ["narrator"],
        "connection": "your-model-id",
        "via": "legacy:narrator",
    }
    assert rows["completion"]["connection"] is None  # no type key authored
    assert rows["completion"]["via"] is None
    assert rows["summarizer"]["via"] == "authored"
    assert rows["summarizer"]["connection"] == "qwen35-4b"
    assert rows["captioner"]["via"] == "authored"
    assert rows["captioner"]["connection"] == "your-model-id"
    assert rows["embedder"] == {
        "slot": "embedder",
        "type": "embedding",
        "task": None,
        "label": "Embedder",
        "canonical": "embedder",
        "aliases": [],
        "connection": "embedder",
        "via": "authored",
    }
    assert rows["decision"]["type"] == "decision"
    assert rows["decision"]["task"] is None


def test_slot_view_marks_type_level_fallbacks():
    rows = {row["slot"]: row for row in taxonomy.slot_view({"completion": "type-conn"})}
    assert rows["completion"]["via"] == "authored"
    assert rows["completion.narration"]["via"] == "type:completion"
    assert rows["imagegen"]["via"] == "type:completion"
    assert rows["summarizer"]["connection"] == "type-conn"
    assert rows["embedding"]["connection"] is None


def test_slot_view_renders_authored_custom_keys_as_per_item_rows():
    rows = taxonomy.slot_view({"narrator": "n", "my-own-slot": "custom-conn"})
    custom = [row for row in rows if row["slot"] == "my-own-slot"]
    assert custom == [
        {
            "slot": "my-own-slot",
            "type": None,
            "task": None,
            "label": "my-own-slot",
            "canonical": "my-own-slot",
            "aliases": [],
            "connection": "custom-conn",
            "via": "authored",
        }
    ]


def test_slot_view_agrees_with_registry_resolution():
    """The payload can never claim a resolution the registry would not perform."""
    agents = {
        "extractor": "e",
        "completion.narration": "typed-n",
        "summarizer": "s",
        "embedding": "emb",
    }
    registry = _registry(agents)
    for row in taxonomy.slot_view(agents):
        if row["connection"] is None:
            with pytest.raises(ConnectionConfigError):
                registry.connection_for(row["slot"])
        else:
            assert registry.connection_for(row["slot"]).name == row["connection"]
