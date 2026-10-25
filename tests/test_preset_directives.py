"""Preset-carried directives (Item 2 W1): the ordered list replaces the catalog.

Covers the resolution seam (``resolve_directives(base=...)`` +
``render_system_prompt/compile(directives=...)``), the store field
(``Preset.directives`` + ``preset_directives``), factory normalization
(``normalize_preset_payload`` / artifact round-trip), and the four contract
invariants: absent list inherits the code catalog byte-identically, stored
order is authoritative, consecutive renders are byte-stable (cache contract),
and global toggles still override per-id ``enabled``.
"""

from __future__ import annotations

import re

from fabulist.log import EventLog
from fabulist.prompts import factory, preset_store as store
from fabulist.prompts.compiler import render_system_prompt, resolve_directives
from fabulist.prompts.parts import (
    DEFAULT_DIRECTIVES,
    Directive,
    directive_to_row,
    normalize_directive_rows,
)

#: Budget that disables trimming entirely (pure parity/order assertions).
HUGE_BUDGET = 10**9


def _names(rendered: str) -> list[str]:
    """Directive names in rendered order (the ``## Name`` block headers)."""
    return [
        line[3:]
        for line in rendered.splitlines()
        if line.startswith("## ") and not line.startswith("### ")
    ]


def _seed_base() -> list[Directive]:
    """The materialized preset-carried seed (all rows, declared order)."""
    payload = {"preset_id": "p", "name": "p", "directives": [
        directive_to_row(d) for d in DEFAULT_DIRECTIVES
    ]}
    base = store.preset_directives(store.preset_from_json(payload))
    assert base is not None  # non-empty list -> carried, not inherited
    return base


def test_absent_list_inherits_code_catalog_and_seed_materializes_identical():
    # None base resolves exactly like no argument at all (the default path).
    assert resolve_directives(base=None) == resolve_directives()
    inherited = render_system_prompt(budget=HUGE_BUDGET)
    explicit_none = render_system_prompt(budget=HUGE_BUDGET, directives=None)
    assert inherited == explicit_none
    # A preset list equal to the seed renders byte-identically (materialize
    # parity): unedited presets see zero diff after adopting the list.
    assert render_system_prompt(budget=HUGE_BUDGET, directives=_seed_base()) == inherited


def test_base_list_order_drives_render_order():
    # library_doc="" suppresses the inline library (its markdown ``## `` headers
    # would pollute name extraction) — the same thing the wiring preset path
    # passes when the library rides its own module.
    base = list(_seed_base())
    base.reverse()
    # Issue #39: the engine_context row renders only with Anti Injection ON;
    # this pin is about stored ROW order, so it asks for the ON state.
    rendered = render_system_prompt(
        budget=HUGE_BUDGET, library_doc="", directives=base, anti_injection=True
    )
    assert _names(rendered) == [d.name for d in base]
    # And a single moved row reorders around the rest.
    base = list(DEFAULT_DIRECTIVES)
    moved = [d for d in base if d.id == "ooc"] + [d for d in base if d.id != "ooc"]
    rendered = render_system_prompt(
        budget=HUGE_BUDGET, library_doc="", directives=moved, anti_injection=True
    )
    names = _names(rendered)
    assert names[0] == "Out of character (OOC)"
    assert names[1:] == [d.name for d in DEFAULT_DIRECTIVES if d.id != "ooc"]


def test_renders_are_stable_across_consecutive_calls():
    base = _seed_base()
    first = render_system_prompt(budget=HUGE_BUDGET, directives=base)
    second = render_system_prompt(budget=HUGE_BUDGET, directives=base)
    assert first == second


def test_global_toggles_still_override_base_enabled():
    from dataclasses import replace

    director_marker = "You are the Content Engine's entire roster"
    # Base row disabled, global toggle re-enables it (global wins).
    base = [replace(d, enabled=False) if d.id == "director" else d for d in _seed_base()]
    rendered = render_system_prompt(
        budget=HUGE_BUDGET, toggles={"director": True}, directives=base
    )
    assert director_marker in rendered
    # Base row enabled, global toggle disables it.
    rendered = render_system_prompt(
        budget=HUGE_BUDGET, toggles={"director": False}, directives=_seed_base()
    )
    assert director_marker not in rendered
    assert "## The Content Engine" not in rendered


def test_surfaces_map_overrides_by_id_and_variables_render_in_list_rows():
    base = _seed_base() + [
        Directive(
            id="user_var",
            name="User var",
            content="Address {{char}} by name.",
            priority=50,
            tier=2,
        )
    ]
    rendered = render_system_prompt(
        budget=HUGE_BUDGET,
        variables={"char": "Ada"},
        surfaces={"directives": {"ooc": "OVERRIDE-MARKER"}},
        directives=base,
    )
    # {{var}} tokens render in list-row content.
    assert "Address Ada by name." in rendered
    assert "{{char}}" not in rendered
    # The legacy surfaces map still content-overrides by id after the base.
    assert "OVERRIDE-MARKER" in rendered
    assert "prefixed with [OOC]" not in rendered


def test_delete_and_add_rows_delete_absent_add_renders_and_trims():
    base = [d for d in _seed_base() if d.id != "ooc"]
    rendered = render_system_prompt(budget=HUGE_BUDGET, directives=base)
    assert "## Out of character (OOC)" not in rendered
    assert "prefixed with [OOC]" not in rendered

    # A user row renders, and is trimmable by priority (lowest first).
    base = base + [
        Directive(id="user_rule", name="User rule", content="USER-RULE-MARKER",
                  priority=5, tier=2)
    ]
    assert "USER-RULE-MARKER" in render_system_prompt(budget=HUGE_BUDGET, directives=base)
    assert "USER-RULE-MARKER" not in render_system_prompt(budget=100, directives=base)
    # Core rows always survive — authored or catalog.
    assert "You are the Content Engine's entire roster" in render_system_prompt(
        budget=100, directives=base
    )
    authored_core = base + [
        Directive(id="authored_core", name="Authored core",
                  content="AUTHORED-CORE-MARKER", priority=5, tier=2, core=True)
    ]
    assert "AUTHORED-CORE-MARKER" in render_system_prompt(
        budget=100, directives=authored_core
    )


def test_preset_json_round_trip_absent_empty_and_minted_ids():
    rows = [directive_to_row(d) for d in DEFAULT_DIRECTIVES[:3]]
    preset = store.preset_from_json({"preset_id": "p1", "name": "P1", "directives": rows})
    assert preset.directives == rows
    exported = store.preset_to_json(preset)
    assert exported["directives"] == rows
    assert store.preset_from_json(exported).directives == rows
    # Absent / empty -> [] (inherit); the export never carries the key then.
    assert store.preset_from_json({"preset_id": "p2", "name": "P2"}).directives == []
    assert store.preset_from_json({"preset_id": "p3", "name": "P3", "directives": []}).directives == []
    empty_export = store.preset_to_json(store.preset_from_json({"preset_id": "p3", "name": "P3"}))
    assert "directives" not in empty_export
    # preset_directives: None (never []) is the inherit signal — the sharp edge.
    assert store.preset_directives(None) is None
    assert store.preset_directives(store.preset_from_json({"preset_id": "p4", "name": "P4"})) is None
    resolved = store.preset_directives(preset)
    assert isinstance(resolved, list) and resolved == list(DEFAULT_DIRECTIVES[:3])
    # Id-less rows get a minted uuid4().hex id.
    minted = normalize_directive_rows([{"name": "N", "content": "C"}])
    assert re.fullmatch(r"[0-9a-f]{32}", minted[0]["id"])
    # Tolerance: junk rows drop, scalars coerce, unknown keys survive.
    assert normalize_directive_rows(None) == []
    mixed = normalize_directive_rows(
        ["junk", {"content": "kept", "priority": "7", "extra": 1}]
    )
    assert len(mixed) == 1
    assert mixed[0]["priority"] == 7
    assert mixed[0]["extra"] == 1
    assert mixed[0]["enabled"] is True


def test_factory_normalize_and_artifact_round_trip_carry_directives(tmp_path):
    rows = [directive_to_row(d) for d in DEFAULT_DIRECTIVES[:2]]
    normalized = factory.normalize_preset_payload({"name": "P", "directives": rows})
    assert normalized["directives"] == rows
    # Absent -> the key is NOT emitted (empty means inherit).
    assert "directives" not in factory.normalize_preset_payload({"name": "P"})
    # The commit path (save_artifact -> load_artifact) never drops the list.
    written = factory.save_artifact(
        {"name": "P", "directives": rows}, path=tmp_path / "f.json"
    )
    loaded = factory.load_artifact(written)
    assert loaded is not None and loaded["directives"] == rows
    # The store fold (seed/reset path) carries it too.
    log = EventLog(tmp_path / "events.db")
    try:
        pid = store.append_preset(
            log, {"name": "P", "directives": rows}
        )
        stored = store.get_preset(log, pid)
        assert stored is not None and stored.directives == rows
        assert store.preset_directives(stored) == list(DEFAULT_DIRECTIVES[:2])
    finally:
        log.close()
