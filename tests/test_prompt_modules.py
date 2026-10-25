"""Tests for the D18 prompt-module engine (:mod:`fabulist.prompts.modules`).

The pipeline is data: :data:`DEFAULT_MODULES` reproduces today's order
(``narrator_system, library, history, facts, user, recalled``),
:func:`compile_preset_messages` renders it against engine-resolved ``parts``,
and :func:`from_default` yields an editable copy with unique ids.
"""

from __future__ import annotations

import pytest

from fabulist.prompts.modules import (
    DEFAULT_MODULES,
    STRUCTURAL_TYPES,
    PromptModule,
    compile_preset_messages,
    from_default,
)
from fabulist.prompts.variables import render_template


def _parts(**overrides) -> dict:
    """Engine-resolved structural content, the shape the wiring builder emits."""
    base = {
        "narrator_system": "SYSTEM",
        "library": "LIBRARY",
        "history": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        "facts": "FACTS",
        "user_content": "CURRENT",
        # D22: the post-user <recalled> system message (optional; omitted -> no
        # empty system block).
        "recalled": "RECALLED",
    }
    base.update(overrides)
    return base


# --- DEFAULT_MODULES --------------------------------------------------------


def test_default_modules_describe_todays_pipeline():
    assert [m.type for m in DEFAULT_MODULES] == [
        "narrator_system",
        "library",
        "history",
        "facts",
        "user",
        "recalled",
    ]
    assert all(m.enabled for m in DEFAULT_MODULES)
    assert {m.type for m in DEFAULT_MODULES} <= STRUCTURAL_TYPES | {"text"}


def test_default_modules_round_trip_order():
    messages = compile_preset_messages(DEFAULT_MODULES, _parts())
    # Issue #44: the narrator system row and the library row are TWO flow rows
    # but ONE served system message — the fold runs at the end of the flow
    # compile (stock Jinja templates reject any non-first system message). The
    # library spec rides behind the renderer's own section divider, in order.
    assert messages == [
        {"role": "system", "content": "SYSTEM\n\nLIBRARY"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "FACTS"},
        # Issue #44: the per-turn envelope rides ONE ephemeral ASSISTANT message
        # inserted immediately BEFORE the final user message — the ROW only
        # decides that it ships, its position is the contract's.
        {"role": "assistant", "content": "RECALLED"},
        {"role": "user", "content": "CURRENT"},
    ]


def test_history_is_spliced_verbatim():
    history = [{"role": "user", "content": "raw", "extra": 1}]
    messages = compile_preset_messages(
        [PromptModule("h", "H", "history")], _parts(history=history)
    )
    # Not copied or re-shaped — the supplied dicts ride through as-is.
    assert messages == history
    assert messages[0] is history[0]


# --- ordering, disabled modules, text modules -------------------------------


def test_compile_preserves_module_order():
    modules = [
        PromptModule("u", "U", "user"),
        PromptModule("n", "N", "narrator_system"),
        PromptModule("f", "F", "facts"),
    ]
    messages = compile_preset_messages(modules, _parts())
    assert [m["content"] for m in messages] == ["CURRENT", "SYSTEM", "FACTS"]


def test_disabled_modules_are_skipped():
    modules = [
        PromptModule("n", "N", "narrator_system"),
        PromptModule("l", "L", "library", enabled=False),
        PromptModule("u", "U", "user"),
    ]
    messages = compile_preset_messages(modules, _parts())
    assert messages == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "CURRENT"},
    ]


def test_text_module_renders_template_and_role():
    modules = [
        PromptModule(
            "greet",
            "Greeting",
            "text",
            role="assistant",
            content="Hello {{char}}, welcome to {{ loc }}!",
        )
    ]
    messages = compile_preset_messages(
        modules, _parts(variables={"char": "Lyra", "loc": "the market"})
    )
    assert messages == [
        {"role": "assistant", "content": "Hello Lyra, welcome to the market!"}
    ]


def test_text_module_unknown_var_stays_literal():
    modules = [
        PromptModule("t", "T", "text", content="keep {{mystery}} as-is")
    ]
    messages = compile_preset_messages(modules, _parts(variables={}))
    assert messages[0]["content"] == "keep {{mystery}} as-is"


def test_text_module_variables_default_to_empty_mapping():
    # No "variables" key in parts must not crash and must leave tokens literal.
    modules = [PromptModule("t", "T", "text", content="{{char}}")]
    messages = compile_preset_messages(modules, _parts())
    assert messages[0]["content"] == "{{char}}"


def test_unknown_module_type_raises():
    modules = [PromptModule("x", "X", "bogus")]  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown module type"):
        compile_preset_messages(modules, _parts())


# --- from_default -----------------------------------------------------------


def test_from_default_ids_are_unique_and_fresh():
    first = from_default()
    second = from_default()
    ids = [m.id for m in first]
    assert len(ids) == len(set(ids))
    assert set(ids).isdisjoint({m.id for m in second})
    # The default singleton is untouched (its ids are the stable literals).
    assert [m.id for m in DEFAULT_MODULES] == [
        "narrator_system",
        "library",
        "history",
        "facts",
        "user",
        "recalled",
    ]


def test_from_default_preserves_shape_and_is_mutable_copy():
    copy = from_default()
    assert [m.type for m in copy] == [m.type for m in DEFAULT_MODULES]
    copy[0].content = "edited"
    assert DEFAULT_MODULES[0].content == ""


# --- render_template contract ----------------------------------------------


def test_render_template_known_and_unknown():
    out = render_template(
        "{{char}} meets {{unknown}}", {"char": "Lyra"}
    )
    assert out == "Lyra meets {{unknown}}"


def test_render_template_is_whitespace_insensitive():
    variables = {"char": "Lyra"}
    assert render_template("{{char}}", variables) == "Lyra"
    assert render_template("{{ char }}", variables) == "Lyra"
    assert render_template("{{   char   }}", variables) == "Lyra"


def test_render_template_does_not_evaluate():
    # No code execution: an expression-looking token is an unknown variable and
    # stays literal rather than being evaluated.
    assert render_template("{{1+1}}", {}) == "{{1+1}}"
    assert render_template("{{a.b()}}", {}) == "{{a.b()}}"
