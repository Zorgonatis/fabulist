"""Unit tests for the shared ``fabulist.dsl`` package (W2.4 defect fix).

The package is the one home for spec-driven statement recognition (positional
**and** named/kwargs forms), the component name/arity lookup and the strip
helper. These tests are hermetic (temp spec/flavor files) plus one real-spec
parity check, and cover the deterministic kwargs→positional repair and the
dangling-child prune that the narrator gate leans on.
"""

from __future__ import annotations

import json

import pytest

from fabulist import dsl
from fabulist.dsl import spec as dsl_spec

#: The four canonical infrastructure components plus the legacy ``ArtifactCard``
#: alias (renamed to ``ObjectCard`` in Wave 3A).
CORE_NAMES = {"ScenePane", "MeterBar", "ObjectCard", "ArtifactCard", "BeatCard"}


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch, tmp_path):
    """Cold cache + no real spec/flavors unless a test opts in."""
    monkeypatch.setenv(dsl_spec.SPEC_ENV_VAR, str(tmp_path / "absent-spec.json"))
    monkeypatch.setenv(dsl_spec.FLAVORS_ENV_VAR, str(tmp_path / "absent-flavors.json"))
    dsl_spec.reset_spec_cache()
    yield
    dsl_spec.reset_spec_cache()


@pytest.fixture
def real_spec(monkeypatch):
    """Load the repo's real generated spec (skip when the checkout lacks it)."""
    monkeypatch.delenv(dsl_spec.SPEC_ENV_VAR, raising=False)
    monkeypatch.delenv(dsl_spec.FLAVORS_ENV_VAR, raising=False)
    dsl_spec.reset_spec_cache()
    if not dsl_spec.spec_path().is_file():
        pytest.skip("generated library.spec.json not present in this checkout")
    return dsl_spec


def _write_spec(tmp_path, components):
    path = tmp_path / "library.spec.json"
    path.write_text(json.dumps({"components": components}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# spec table
# ---------------------------------------------------------------------------


def test_core_four_always_known_without_any_files():
    assert set(dsl.component_names()) == CORE_NAMES


def test_real_generated_spec_parses_all_components(monkeypatch):
    monkeypatch.delenv(dsl_spec.SPEC_ENV_VAR, raising=False)
    monkeypatch.delenv(dsl_spec.FLAVORS_ENV_VAR, raising=False)
    dsl_spec.reset_spec_cache()

    path = dsl_spec.spec_path()
    if not path.is_file():
        pytest.skip("generated library.spec.json not present in this checkout")

    names = set(dsl.component_names())
    assert CORE_NAMES <= names
    # 4 infrastructure + 10 flavors + Custom + 5 artifact reproductions + 1 legacy alias.
    assert len(names) == 21, sorted(names)

    # Prop order comes from the spec (kwargs repair depends on it).
    assert dsl.component_schema("ArtifactCard").prop_names == (
        "name",
        "kind",
        "description",
        "options",
    )
    assert dsl.component_schema("DialogueSpotlight").prop_names == (
        "speaker",
        "line",
        "tell",
        "options",
    )
    # Optional trailing props widen the accepted arity range (Document
    # signature? + Wave 2b options {glyph, art}).
    document = dsl.component_schema("Document")
    assert (document.min_props, document.max_props) == (3, 5)


def test_env_spec_override_loads_component_table(monkeypatch, tmp_path):
    path = _write_spec(
        tmp_path,
        {
            "Widget": {
                "props": [
                    {"name": "a", "required": True},
                    {"name": "b", "required": True},
                    {"name": "c", "required": False},
                ]
            }
        },
    )
    monkeypatch.setenv(dsl_spec.SPEC_ENV_VAR, str(path))
    dsl_spec.reset_spec_cache()

    assert {"Widget"} <= set(dsl.component_names())
    schema = dsl.component_schema("Widget")
    assert schema.prop_names == ("a", "b", "c")
    assert schema.accepts(2) and schema.accepts(3) and not schema.accepts(1)


def test_flavors_mirror_merges_at_lower_precedence(monkeypatch, tmp_path):
    mirror = tmp_path / "flavors.json"
    mirror.write_text(
        json.dumps({"flavors": [{"component": "Mindscape", "props": ["title", "thoughts", "mood"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(dsl_spec.FLAVORS_ENV_VAR, str(mirror))
    dsl_spec.reset_spec_cache()

    assert "Mindscape" in dsl.component_names()
    assert dsl.component_schema("Mindscape").prop_names == ("title", "thoughts", "mood")


def test_core_four_win_name_collisions(monkeypatch, tmp_path):
    mirror = tmp_path / "flavors.json"
    mirror.write_text(
        json.dumps({"flavors": [{"component": "BeatCard", "props": ["a", "b", "c"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(dsl_spec.FLAVORS_ENV_VAR, str(mirror))
    dsl_spec.reset_spec_cache()

    assert dsl.component_schema("BeatCard").min_props == 1


def test_name_only_entry_is_unbounded(monkeypatch, tmp_path):
    mirror = tmp_path / "flavors.json"
    mirror.write_text(json.dumps({"flavors": ["Entrance"]}), encoding="utf-8")
    monkeypatch.setenv(dsl_spec.FLAVORS_ENV_VAR, str(mirror))
    dsl_spec.reset_spec_cache()

    schema = dsl.component_schema("Entrance")
    assert schema.bounded is False
    assert schema.accepts(0) and schema.accepts(9)


def test_malformed_spec_falls_back_to_core(monkeypatch, tmp_path):
    bad = tmp_path / "library.spec.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv(dsl_spec.SPEC_ENV_VAR, str(bad))
    dsl_spec.reset_spec_cache()
    assert set(dsl.component_names()) == CORE_NAMES


# ---------------------------------------------------------------------------
# statement recognition
# ---------------------------------------------------------------------------


def test_parse_positional_statement():
    st = dsl.parse_statement('card = ArtifactCard("Tense", "Study", "08:30")')
    assert st is not None
    assert (st.ident, st.component) == ("card", "ArtifactCard")
    assert st.args == ('"Tense"', '"Study"', '"08:30"')
    assert st.has_named_args is False


def test_parse_kwargs_statement():
    st = dsl.parse_statement(
        'card = ArtifactCard(name="Exhausted", kind="Room", description="08:45")'
    )
    assert st is not None
    assert st.has_named_args is True
    assert st.named_args == (
        ("name", '"Exhausted"'),
        ("kind", '"Room"'),
        ("description", '"08:45"'),
    )


def test_parse_ignores_non_statements():
    assert dsl.parse_statement("just prose") is None
    assert dsl.parse_statement("root = 3") is None


def test_statement_re_matches_every_known_component(monkeypatch):
    monkeypatch.delenv(dsl_spec.SPEC_ENV_VAR, raising=False)
    monkeypatch.delenv(dsl_spec.FLAVORS_ENV_VAR, raising=False)
    dsl_spec.reset_spec_cache()
    if not dsl_spec.spec_path().is_file():
        pytest.skip("generated library.spec.json not present in this checkout")

    pattern = dsl.statement_re()
    for name in dsl.component_names():
        assert pattern.match(f"x = {name}("), name
    assert dsl.is_statement("x = Mindscape(")
    assert not dsl.is_statement("x = NotAComponent(")


def test_split_args_respects_nesting_and_quotes():
    args = dsl.split_args('"a, b", MeterBar("x", 1, 2), [a, b]')
    assert args == ['"a, b"', 'MeterBar("x", 1, 2)', "[a, b]"]


# ---------------------------------------------------------------------------
# deterministic repair: kwargs -> positional
# ---------------------------------------------------------------------------


def test_positionalize_pure_kwargs_uses_spec_order():
    schema = dsl.component_schema("ArtifactCard")
    st = dsl.parse_statement(
        'card = ArtifactCard(description="08:45", name="Exhausted", kind="Room")'
    )
    assert dsl.positionalize(st, schema) == 'card = ArtifactCard("Exhausted", "Room", "08:45")'


def test_positionalize_mixed_keeps_positional_prefix(real_spec):
    schema = dsl.component_schema("DialogueSpotlight")
    st = dsl.parse_statement('line = DialogueSpotlight("Tessa", line="Two of \'em.", tell="flat")')
    assert (
        dsl.positionalize(st, schema)
        == 'line = DialogueSpotlight("Tessa", "Two of \'em.", "flat")'
    )


def test_positionalize_unknown_prop_is_unrepairable():
    schema = dsl.component_schema("ArtifactCard")
    st = dsl.parse_statement('card = ArtifactCard(name="x", typo="y", kind="z")')
    assert dsl.positionalize(st, schema) is None


def test_positionalize_missing_required_is_unrepairable():
    schema = dsl.component_schema("ArtifactCard")
    st = dsl.parse_statement('card = ArtifactCard(name="x", kind="y")')
    assert dsl.positionalize(st, schema) is None


def test_positionalize_optional_omitted_at_end(real_spec):
    schema = dsl.component_schema("Document")
    # Document = (docType, title, body, signature?) — three provided is valid.
    assert schema.accepts(3)
    st = dsl.parse_statement('d = Document(docType="Scrap", title="Notes", body="milk")')
    assert dsl.positionalize(st, schema) == 'd = Document("Scrap", "Notes", "milk")'


# ---------------------------------------------------------------------------
# deterministic repair: dangling child refs
# ---------------------------------------------------------------------------


def test_prune_child_refs_drops_only_undeclared_bare_idents():
    text = 'root = ScenePane("Morning", [status, cat_mind, circle_note])'
    new, dropped, empty = dsl.prune_child_refs(text, {"status"})
    assert new == 'root = ScenePane("Morning", [status])'
    assert dropped == ["cat_mind", "circle_note"]
    assert empty is False


def test_prune_child_refs_handles_mixed_inline_calls():
    text = 'root = ScenePane("Stream", [ArtifactCard("a","b","c"), Interface, DialogueSpotlight])'
    new, dropped, empty = dsl.prune_child_refs(text, set())
    assert dropped == ["Interface", "DialogueSpotlight"]
    # The inline component call is preserved verbatim.
    assert 'ArtifactCard("a","b","c")' in new
    assert "Interface" not in new and "DialogueSpotlight" not in new


def test_prune_child_refs_reports_empty_after_all_dropped():
    text = 'root = ScenePane("Alone", [ghost, phantom])'
    new, dropped, empty = dsl.prune_child_refs(text, set())
    assert new == 'root = ScenePane("Alone", [])'
    assert dropped == ["ghost", "phantom"]
    assert empty is True


def test_prune_child_refs_noop_when_non_scenepane_or_all_declared():
    text = 'mind = Mindscape("Cracks", ["a", "b"], "dread")'
    assert dsl.prune_child_refs(text, set()) == (text, [], False)
    text2 = 'root = ScenePane("T", [a, b])'
    assert dsl.prune_child_refs(text2, {"a", "b"}) == (text2, [], False)


# ---------------------------------------------------------------------------
# strip helper
# ---------------------------------------------------------------------------


def test_strip_removes_fenced_and_unfenced_statements():
    raw = (
        "Before.\n"
        "card = ArtifactCard(1, 2, 3)\n"
        "```openui\n"
        'spot = DialogueSpotlight("A", "B", "C")\n'
        "```\n"
        "After."
    )
    cleaned = dsl.strip_dsl(raw)
    assert "ArtifactCard" not in cleaned
    assert "DialogueSpotlight" not in cleaned
    assert "Before." in cleaned and "After." in cleaned


def test_strip_removes_multiline_flavor_statement(real_spec):
    raw = (
        "She waits.\n"
        "mind = Mindscape(\n"
        '  "the storm",\n'
        "  brief=True,\n"
        ")\n"
        "Then silence."
    )
    cleaned = dsl.strip_dsl(raw)
    assert "Mindscape" not in cleaned
    assert cleaned == "She waits.\nThen silence."


def test_strip_keeps_unknown_component_text():
    assert dsl.strip_dsl("x = NotAComponent(1)") == "x = NotAComponent(1)"


def test_is_named_arg_accepts_colon_form() -> None:
    from fabulist.dsl.statements import is_named_arg

    assert is_named_arg('face: "A flicker of discomfort"')
    assert is_named_arg("thought: 'Do they like me?'")
    assert is_named_arg('mood="Exhausted"')
    assert not is_named_arg('"a plain string"')
    assert not is_named_arg("42")


def test_positionalize_repairs_colon_form(real_spec) -> None:
    from fabulist.dsl.spec import component_schema
    from fabulist.dsl.statements import parse_statement, positionalize

    stmt = parse_statement(
        'spot = ReactionSpotlight(face: "Wide eyes", thought: "Do they like me?")'
    )
    assert stmt is not None
    schema = component_schema("ReactionSpotlight")
    repaired = positionalize(stmt, schema)
    assert repaired is not None
    assert repaired == 'spot = ReactionSpotlight("Wide eyes", "Do they like me?")'


def test_strip_removes_unassigned_component_statements(real_spec):
    """A narrator may emit a bare call (`DialogueSpotlight("Hobbs", …)`).

    Live defect 2026-10-17: without a tolerant recogniser such lines stayed in
    the prose channel, so the panel text rendered as a paragraph and the critic
    judged it as narration.
    """
    raw = (
        "He leans closer.\n"
        'DialogueSpotlight("Hobbs", "Tied up?", "The words are slow.")\n'
        "The claw resumes.\n"
        'ObjectCard("Tarnished Silver Teapot", "Antique", "A heavy piece.")'
    )
    cleaned = dsl.strip_dsl(raw)
    assert "DialogueSpotlight" not in cleaned
    assert "ObjectCard" not in cleaned
    assert cleaned == "He leans closer.\nThe claw resumes."
