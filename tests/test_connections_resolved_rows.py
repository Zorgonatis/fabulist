"""The connections read model: ONE resolved row per agent ROLE (issue #48).

The drawer used to list every authored SPELLING as its own row: ``embedder`` and
``embedding`` were two rows (one of them able to read "unset" while the engine
happily used the other), and a legacy ``narrator`` was a second row next to
``completion.narration``. Three times in one night a bound role SHOWED unbound
under its alias (sandbox: ``embedder`` -> ``sidecar-embed`` with 48 usage rows
while ``embedding`` displayed unset).

The read model now renders one row per ROLE:

* ``slot`` is the role's canonical spelling (``embedding``, ``completion.narration``);
* ``connection`` is the EFFECTIVE binding — the connection the engine's own call
  path resolves (``connection_for("embedder")`` for the embedding role);
* ``write_slot`` is the declared spelling a rebind writes, chosen so it always
  takes effect (``embedder`` for the embedding role: the call sites ask for that
  name and a name's own key is tried before its type);
* ``inert`` names every authored spelling of the role that LOST precedence, with
  its own connection — a conflict is surfaced, never hidden.

What must NOT change: ``resolve_slot`` (the load-bearing call-side rule, incl.
the task-less type fall-down fixed in 1e38bff) and every PATCH address that works
today. Those are pinned here too, because a read-side refactor is exactly where
they would regress.
"""

from __future__ import annotations

import pytest

from fabulist.connections import taxonomy
from fabulist.connections.registry import Registry

#: The seven keys a live/legacy ``connections.json`` authors today.
LEGACY_CONFIG = {
    "narrator": "your-model-id",
    "extractor": "qwen35-4b",
    "summarizer": "qwen35-4b",
    "captioner": "your-model-id",
    "embedder": "sidecar-embed",
    "decision": "d1-3b",
    "imagegen": "qwen35-4b",
}


def _rows(agents: dict[str, str]) -> dict[str, dict]:
    """The payload as ``{slot: row}`` (one entry per role — that is the claim)."""
    return {row["slot"]: row for row in taxonomy.slot_view(agents)}


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


# --- criterion: one resolved row for an item-slot-authored role -------------------


def test_embedder_only_authors_one_resolved_embedding_row():
    """The incident case: ``embedder`` authored, ``embedding`` must not read unset."""
    rows = _rows({"embedder": "sidecar-embed"})

    # exactly one embedding-type row, and it is resolved
    embedding_rows = [row for row in rows.values() if row["type"] == "embedding"]
    assert len(embedding_rows) == 1
    assert embedding_rows[0]["slot"] == "embedding"
    assert embedding_rows[0]["connection"] == "sidecar-embed"
    assert embedding_rows[0]["via"] == "item:embedder"
    # the alias spelling has no row of its own
    assert "embedder" not in rows
    # ... it is the row's alias and its rebind key
    assert rows["embedding"]["aliases"] == ["embedder"]
    assert rows["embedding"]["write_slot"] == "embedder"
    assert rows["embedding"]["inert"] == []
    # and the read model agrees with the call side, both ways
    assert taxonomy.resolve_slot({"embedder": "sidecar-embed"}, "embedder") == "sidecar-embed"
    assert taxonomy.resolve_slot({"embedder": "sidecar-embed"}, "embedding") == "sidecar-embed"


def test_typed_embedding_only_authors_one_resolved_embedding_row():
    """The other spelling of the same role: still ONE row, still resolved."""
    rows = _rows({"embedding": "typed-emb"})

    embedding_rows = [row for row in rows.values() if row["type"] == "embedding"]
    assert len(embedding_rows) == 1
    assert rows["embedding"]["connection"] == "typed-emb"
    assert rows["embedding"]["via"] == "authored"
    # a rebind still writes the key the embedding call path resolves first
    assert rows["embedding"]["write_slot"] == "embedder"
    assert rows["embedding"]["inert"] == []


# --- criterion: one resolved row for a legacy-twin-authored role ------------------


def test_narrator_only_authors_one_resolved_narration_row():
    rows = _rows({"narrator": "legacy-n"})

    narration = [row for row in rows.values() if row.get("task") == "narration"]
    assert len(narration) == 1
    assert narration[0]["slot"] == "completion.narration"
    assert narration[0]["connection"] == "legacy-n"
    assert narration[0]["via"] == "legacy:narrator"
    assert narration[0]["aliases"] == ["narrator"]
    assert narration[0]["write_slot"] == "completion.narration"
    assert narration[0]["inert"] == []
    # the legacy spelling itself never becomes a row
    assert "narrator" not in rows


def test_both_twin_spellings_authored_render_one_row_typed_effective_legacy_inert():
    """Typed wins; the legacy key is marked INERT with the connection it names."""
    rows = _rows({"narrator": "legacy-n", "completion.narration": "typed-n"})

    narration = [row for row in rows.values() if row.get("task") == "narration"]
    assert len(narration) == 1
    assert rows["completion.narration"]["connection"] == "typed-n"
    assert rows["completion.narration"]["via"] == "authored"
    assert rows["completion.narration"]["inert"] == [
        {"slot": "narrator", "connection": "legacy-n"}
    ]
    assert "narrator" not in rows


def test_a_conflicting_typo_of_the_twin_rule_never_hides_the_loser():
    """Any authored spelling that loses precedence is reported, not dropped."""
    rows = _rows({"extractor": "legacy-e", "completion.extraction": "typed-e"})
    assert rows["completion.extraction"]["connection"] == "typed-e"
    assert rows["completion.extraction"]["inert"] == [
        {"slot": "extractor", "connection": "legacy-e"}
    ]


# --- criterion: conflicting item/typed spellings for the embedding role -----------


def test_conflicting_embedding_and_embedder_spellings_surface_the_effective_binding():
    """The row shows what RUNS and marks the shadowed key (never a silent pick)."""
    agents = {"embedding": "typed-emb", "embedder": "item-emb"}
    rows = _rows(agents)

    embedding_rows = [row for row in rows.values() if row["type"] == "embedding"]
    assert len(embedding_rows) == 1
    assert rows["embedding"]["connection"] == "item-emb"
    assert rows["embedding"]["via"] == "item:embedder"
    assert rows["embedding"]["write_slot"] == "embedder"
    assert rows["embedding"]["inert"] == [{"slot": "embedding", "connection": "typed-emb"}]
    # the effective binding is the connection the engine's embedding path dials
    assert _registry(agents).connection_for("embedder").name == "item-emb"


# --- criterion: an unbound role is ONE unbound row --------------------------------


def test_unbound_role_renders_one_unbound_row():
    rows = _rows({})

    unbound = [row["slot"] for row in rows.values() if row["connection"] is None]
    assert len(unbound) == len(rows)  # every role, listed once
    assert unbound.count("embedding") == 1
    assert "embedder" not in rows
    assert rows["embedding"]["connection"] is None
    assert rows["embedding"]["via"] is None
    assert rows["embedding"]["write_slot"] == "embedder"


# --- the invariant: one row per role, for a live config ---------------------------


def test_one_row_per_role_for_a_live_legacy_config():
    rows = taxonomy.slot_view(LEGACY_CONFIG)
    slots = [row["slot"] for row in rows]

    # no duplicated role, and no alias spelling sneaking in as a row
    assert len(slots) == len(set(slots))
    for alias in ("narrator", "extractor", "embedder"):
        assert alias not in slots
    # every declared role is present exactly once, in type/task/item order
    assert slots == [
        "completion",
        "completion.narration",
        "completion.extraction",
        "completion.state",
        "summarizer",
        "captioner",
        "imagegen",
        "embedding",
        "decision",
    ]
    # the embedding role carries the document's embedder binding
    assert rows[7]["connection"] == "sidecar-embed"
    assert rows[7]["via"] == "item:embedder"


# --- criterion: PATCH semantics unchanged ----------------------------------------


@pytest.mark.parametrize("name", sorted(taxonomy.declared_spellings()))
def test_every_declared_spelling_still_resolves_exactly_as_before(name):
    """A write path unchanged pin: the call-side rule is untouched by #48."""
    agents = dict(LEGACY_CONFIG)
    before = taxonomy.resolve_slot(agents, name)
    # the read model may not perturb resolution
    taxonomy.slot_view(agents)
    assert taxonomy.resolve_slot(agents, name) == before
    # and canonicalization (what PATCH writes) is unchanged too
    assert taxonomy.canonical(name) in taxonomy.declared_spellings()


@pytest.mark.parametrize("row_slot", [row["slot"] for row in taxonomy.slot_view(LEGACY_CONFIG)])
def test_a_row_write_slot_is_an_accepted_patch_address_and_resolves_the_row(row_slot):
    """``write_slot`` is always a declared spelling -> PATCH accepts it (no 404)."""
    agents = dict(LEGACY_CONFIG)
    row = _rows(agents)[row_slot]
    assert row["write_slot"] in taxonomy.declared_spellings()
    registry = _registry(agents)
    if row["connection"] is None:
        with pytest.raises(Exception):
            registry.connection_for(row["write_slot"])
    else:
        assert registry.connection_for(row["write_slot"]).name == row["connection"]


def test_rebinding_a_row_through_its_write_slot_takes_effect():
    """The drawer's rebind writes ``write_slot``; that must move the role.

    Only ``embedding`` authored (no item key): writing the canonical spelling
    from the drawer would leave the role on the OLD connection, because the
    embedding call path asks for ``embedder`` and tries it first.
    """
    agents = {"embedding": "old-emb"}
    row = _rows(agents)["embedding"]
    assert row["connection"] == "old-emb"

    # the rebind writes the row's write_slot against a new connection
    rebound = {**agents, row["write_slot"]: "new-emb"}
    after = _rows(rebound)["embedding"]
    assert after["connection"] == "new-emb"
    # the superseded typed key stays visible as an inert conflict, not hidden
    assert after["inert"] == [{"slot": "embedding", "connection": "old-emb"}]
    assert _registry(rebound).connection_for("embedder").name == "new-emb"


def test_the_task_less_type_fall_down_is_not_regressed():
    """1e38bff: ``embedding`` resolves through ``embedder``; ``completion`` does not."""
    assert taxonomy.candidate_slots("embedding") == ("embedding", "embedder")
    assert taxonomy.resolve_slot({"embedder": "sidecar-embed"}, "embedding") == "sidecar-embed"
    # an authored typed key still wins for the TYPE's own name
    assert taxonomy.resolve_slot({"embedder": "e", "embedding": "t"}, "embedding") == "t"
    # a type WITH a task layer keeps its single-purpose items as separate roles
    assert taxonomy.candidate_slots("completion") == ("completion",)
    assert taxonomy.folded_item_slots("completion") == ()
    assert "imagegen" in _rows({"imagegen": "img"})
