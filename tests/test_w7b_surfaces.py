"""W7-B: D27 token-surface datafication tests.

The five formerly code-only token surfaces (library doc, ``<recalled>`` framing
+ headers, regex scripts, history projection knobs, directive default text) are
preset-carried data with ``{{expression}}`` support; the code constants remain
as the fallback. This suite proves:

* each surface reads the preset override when present and the constant when not;
* an "unedited" preset -- one whose surfaces are seeded from
  :func:`fabulist.prompts.surfaces.factory_surfaces` -- compiles BYTE-IDENTICAL
  to the pre-D27 path (compiled system prompt, full compile, recalled message);
* a regex surface is a full-list replace (no partial merges);
* the history depth / DSL-strip knobs apply;
* ``{{expression}}`` resolves inside ``library_doc`` (and recalled copy);
* the schema normalizer is tolerant + idempotent and ``preset_surfaces`` reads a
  mapping, a ``Preset``-shaped object, or ``None``.

Every assertion runs against the real loaders; the recalled fixtures run against
a real tmp-path :class:`~fabulist.log.EventLog` (the pattern in
``tests/test_facts_suffix.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.context.facts import (
    RECALLED_BTS_HEADER,
    RECALLED_CLOSE,
    RECALLED_FRAMING,
    RECALLED_OPEN,
    RECALLED_RECALL_HEADER,
    RECALLED_RECAP_HEADER,
    RECALLED_WORLD_INFO_HEADER,
    recalled_copy,
    recalled_message,
)
from fabulist.context.regex_stage import (
    PRESET_REGEXES,
    apply_regex_stage,
    regexes_from_surfaces,
)
from fabulist.log import EventLog
from fabulist.prompts import factory
from fabulist.prompts.compiler import (
    DEFAULT_HISTORY_DEPTH,
    DEFAULT_HISTORY_STRIP_DSL,
    compile,
    render_system_prompt,
    stripped_history,
)
from fabulist.prompts.library_mini import render_library_doc, resolve_library_doc
from fabulist.prompts.surfaces import (
    LIBRARY_DOC_EXPRESSION,
    SURFACES_KEY,
    factory_surfaces,
    normalize_surfaces,
    preset_surfaces,
)

FACTS = "time: 14:35 | loc: night market | weather: light rain | tone: warm"
HISTORY = [
    {"role": "user", "content": "I enter the market."},
    {"role": "assistant", "content": "The stalls crowd close."},
]

LYRA = {"name": "Lyra Voss", "description": "A cartographer.", "personality": "Wry."}
SUMMARY_TEXT = "They met at the night market."


@pytest.fixture
def build_log(tmp_path: Path):
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


def _seed(log: EventLog, sid: str = "s") -> str:
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": "Hello."})
    log.append_event(sid, "user", "user", {"text": "I enter the night market."})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "The stalls crowd close.", "dsl": "", "segments": []}
    )
    log.append_event(sid, "summary", "summarizer", {"text": SUMMARY_TEXT, "covers_seqs": [1]})
    log.append_event(sid, "state-delta", "extractor", {"time": "14:35"})
    return sid


# ---------------------------------------------------------------------------
# schema: normalizer / accessor / factory seed
# ---------------------------------------------------------------------------


def test_normalize_surfaces_is_tolerant_and_idempotent():
    assert normalize_surfaces(None) == {}
    assert normalize_surfaces("nope") == {}
    assert normalize_surfaces({"unknown": 1}) == {}
    raw = {
        "library_doc": "DOC",
        "recalled": {"framing": "F", "headers": {"recall": "H", "bogus": "x"}},
        "regexes": [{"id": "r", "name": "n", "find": "a", "replace": "b", "flags": "g"}],
        "history": {"depth": 3, "strip_dsl": False, "junk": 1},
        "directives": {"director": "D"},
    }
    once = normalize_surfaces(raw)
    assert once == normalize_surfaces(once)  # idempotent
    assert once["recalled"]["headers"] == {"recall": "H"}
    assert once["history"] == {"depth": 3, "strip_dsl": False}


def test_normalize_surfaces_keeps_empty_string_library_doc():
    # An explicit empty doc is meaningful (blank the library), distinct from absent.
    assert normalize_surfaces({"library_doc": ""}) == {"library_doc": ""}


def test_preset_surfaces_reads_mapping_object_and_none():
    assert preset_surfaces(None) == {}
    assert preset_surfaces({SURFACES_KEY: {"history": {"depth": 5}}}) == {
        "history": {"depth": 5}
    }

    class _Fake:
        surfaces = {"library_doc": "X"}

    assert preset_surfaces(_Fake()) == {"library_doc": "X"}


def test_factory_surfaces_captures_every_constant():
    surfaces = factory_surfaces()
    # D35: the library-doc surface seeds the EXPRESSION, not baked text, so a
    # regenerated doc reaches live DBs without a manual owner PUT. It still
    # resolves to the generated doc through the loader.
    assert surfaces["library_doc"] == LIBRARY_DOC_EXPRESSION
    assert surfaces["library_doc"] == "{{library_doc}}"
    assert resolve_library_doc(surfaces) == render_library_doc()
    assert surfaces["recalled"]["framing"] == RECALLED_FRAMING
    assert surfaces["recalled"]["open"] == RECALLED_OPEN
    assert surfaces["recalled"]["close"] == RECALLED_CLOSE
    assert surfaces["recalled"]["headers"] == {
        "recall": RECALLED_RECALL_HEADER,
        "world_info": RECALLED_WORLD_INFO_HEADER,
        "recap": RECALLED_RECAP_HEADER,
        "bts": RECALLED_BTS_HEADER,
    }
    assert surfaces["history"] == {
        "depth": DEFAULT_HISTORY_DEPTH,
        "strip_dsl": DEFAULT_HISTORY_STRIP_DSL,
    }
    # Directives INHERIT from the code catalog (absent/empty -> constant); the
    # seed must NOT bake copies, or a parts.py edit can be shadowed silently.
    # The normalizer drops the empty mapping, so the key is absent entirely.
    assert not surfaces.get("directives")
    assert len(surfaces["regexes"]) == len(PRESET_REGEXES)


# ---------------------------------------------------------------------------
# byte-parity: unedited preset seed == pre-D27 constants
# ---------------------------------------------------------------------------


def test_unedited_surfaces_render_byte_identical_system_prompt():
    assert render_system_prompt(budget=10**9, surfaces=factory_surfaces()) == (
        render_system_prompt(budget=10**9)
    )


def test_unedited_surfaces_compile_byte_identical_messages(build_log):
    log = build_log()
    sid = _seed(log)
    recalled = recalled_message(log, sid, recall_text="R", user_text="I enter the night market.")
    kwargs = dict(
        facts=FACTS,
        history=HISTORY,
        user_content="I enter the night market.",
        plan="PLAN\n- beat",
        active_panels="status",
        recalled=recalled,
    )
    assert compile(None, None, **kwargs, surfaces=factory_surfaces()) == compile(
        None, None, **kwargs
    )


def test_unedited_surfaces_recalled_message_byte_identical(build_log):
    log = build_log()
    sid = _seed(log)
    baseline = recalled_message(log, sid, recall_text="R", user_text="go")
    seeded = recalled_message(
        log, sid, recall_text="R", user_text="go", surfaces=factory_surfaces()
    )
    assert baseline == seeded


# ---------------------------------------------------------------------------
# surface 1: library doc (override + {{expression}})
# ---------------------------------------------------------------------------


def test_library_doc_override_wins_over_constant():
    assert render_library_doc("CUSTOM DOC") == "CUSTOM DOC"
    assert resolve_library_doc({"library_doc": "CUSTOM DOC"}) == "CUSTOM DOC"
    # No surface -> the constant.
    assert resolve_library_doc({}) == render_library_doc()


def test_library_doc_expression_self_resolves_to_generated_doc():
    # D35: `{{library_doc}}` (the factory seed) resolves to the generated doc
    # even with NO variables map — the reserved self-expression is expanded
    # before the unknown-var-literal pass, so a literal token can never reach
    # the model.
    assert render_library_doc("{{library_doc}}") == render_library_doc()
    assert resolve_library_doc({"library_doc": "{{library_doc}}"}) == render_library_doc()
    assert "{{library_doc}}" not in render_library_doc("{{library_doc}}")


def test_library_doc_top_tail_around_the_expression():
    # The owner's top/tail pattern: their framing text survives around the doc.
    content = "Extra framing line\n{{library_doc}}\nAnother line"
    rendered = render_library_doc(content)
    assert rendered == f"Extra framing line\n{render_library_doc()}\nAnother line"
    # Also composes with other {{vars}} in the same surface.
    rendered2 = render_library_doc("{{char}}:\n{{library_doc}}", {"char": "Lyra"})
    assert rendered2.startswith("Lyra:\n")
    assert rendered2.endswith(render_library_doc())


def test_library_doc_without_expression_is_a_pure_override():
    # No `{{library_doc}}` token = the owner replaced the surface entirely;
    # the generated doc is NOT injected.
    rendered = render_library_doc("My own hand-written component guide.")
    assert rendered == "My own hand-written component guide."
    assert render_library_doc() not in rendered


def test_library_doc_expression_resolves_against_variables():
    rendered = render_library_doc("Doc for {{char}} / {{missing}}", {"char": "Lyra"})
    assert rendered == "Doc for Lyra / {{missing}}"
    assert resolve_library_doc({"library_doc": "Hi {{char}}"}, {"char": "Lyra"}) == "Hi Lyra"


def test_library_doc_surface_reaches_the_compiled_system_prompt():
    system = render_system_prompt(
        budget=10**9,
        surfaces={"library_doc": "PRESET-DOC-FOR-{{char}}"},
        variables={"char": "Lyra"},
    )
    assert "PRESET-DOC-FOR-Lyra" in system
    assert "#### Scene flavors" not in system  # constant doc replaced


# ---------------------------------------------------------------------------
# surface 2: recalled framing + headers
# ---------------------------------------------------------------------------


def test_recalled_headers_override_with_constants_falling_back():
    copy = recalled_copy({"recalled": {"headers": {"recall": "### Mine"}}})
    assert copy["recall"] == "### Mine"
    assert copy["world_info"] == RECALLED_WORLD_INFO_HEADER
    assert copy["framing"] == RECALLED_FRAMING


def test_recalled_message_uses_surface_copy(build_log):
    log = build_log()
    sid = _seed(log)
    block = recalled_message(
        log,
        sid,
        recall_text="R",
        user_text="go",
        surfaces={
            "recalled": {
                "framing": "CUSTOM-FRAME {{char}}",
                "open": "<rec>",
                "close": "</rec>",
                "headers": {"recall": "### Custom recall"},
            }
        },
        variables={"char": "Lyra"},
    )
    assert block is not None
    # The envelope owns the tag pair (contract-fixed); the preset owns the
    # framing/header copy.
    assert block.startswith("CUSTOM-FRAME Lyra")
    assert "<rec>" not in block and "</rec>" not in block
    assert "### Custom recall" in block
    assert RECALLED_RECALL_HEADER not in block


def test_recalled_copy_supports_expressions():
    copy = recalled_copy(
        {"recalled": {"framing": "Hello {{char}}", "headers": {"bts": "BTS {{char}}"}}},
        {"char": "Lyra"},
    )
    assert copy["framing"] == "Hello Lyra"
    assert copy["bts"] == "BTS Lyra"


# ---------------------------------------------------------------------------
# surface 3: regex scripts (full-list replace)
# ---------------------------------------------------------------------------


def test_regex_surface_replaces_the_whole_list():
    surfaces = {
        "regexes": [
            {"id": "x", "name": "Custom", "find": "FOO", "replace": "BAR", "flags": "g"}
        ]
    }
    stages = regexes_from_surfaces(surfaces)
    assert len(stages) == 1 and stages[0].name == "Custom"
    assert apply_regex_stage(["FOO FOO"], depth_index=0, prompt_only=True, surfaces=surfaces) == [
        "BAR BAR"
    ]
    # The constant VTK strip is GONE (full replace, not a merge).
    vtk = ["<!-- VTK_START -->x<!-- VTK_END -->"]
    assert apply_regex_stage(vtk, depth_index=5, prompt_only=True, surfaces=surfaces) == vtk


def test_regex_surface_falls_back_to_constant():
    assert regexes_from_surfaces(None) == PRESET_REGEXES
    assert regexes_from_surfaces({}) == PRESET_REGEXES
    assert regexes_from_surfaces(factory_surfaces()) == PRESET_REGEXES
    vtk = ["<!-- VTK_START -->x<!-- VTK_END -->"]
    assert apply_regex_stage(vtk, depth_index=5, prompt_only=True) == [""]


def test_regex_surface_accepts_st_scope_and_literal():
    stages = regexes_from_surfaces(
        {"regexes": [{"id": "1", "name": "n", "findRegex": "/a/b/i", "replaceString": "z", "scope": "display"}]}
    )
    assert stages[0].find == "a/b" and stages[0].flags == "i"
    assert stages[0].prompt_only is False
    assert stages[0].replace == "z"


# ---------------------------------------------------------------------------
# surface 4: history projection knobs
# ---------------------------------------------------------------------------


def test_history_depth_knob_limits_prior_turns():
    history = [{"role": "user", "content": str(i)} for i in range(5)]
    messages = compile(None, None, None, history, "u", surfaces={"history": {"depth": 2}})
    assert [m["content"] for m in messages if m["role"] == "user"] == ["3", "4", "u"]


def test_history_depth_knob_absent_keeps_all_turns():
    history = [{"role": "user", "content": str(i)} for i in range(5)]
    messages = compile(None, None, None, history, "u")
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        "0",
        "1",
        "2",
        "3",
        "4",
        "u",
    ]


def test_history_strip_dsl_knob():
    items = [{"segments": [{"kind": "prose", "text": "prose"}, {"kind": "dsl", "text": "dsl"}]}]
    assert stripped_history(items) == [{"role": "assistant", "content": "prose"}]
    assert stripped_history(items, strip=False) == [
        {"role": "assistant", "content": "prose\n\ndsl"}
    ]
    messages = compile(None, None, None, items, "u", surfaces={"history": {"strip_dsl": False}})
    assert any("dsl" in str(m["content"]) for m in messages if m["role"] == "assistant")


# ---------------------------------------------------------------------------
# surface 5: directive default text
# ---------------------------------------------------------------------------


def test_directive_text_surface_overrides_constant():
    system = render_system_prompt(
        budget=10**9, surfaces={"directives": {"director": "DIRECTOR-OVERRIDE"}}
    )
    assert "DIRECTOR-OVERRIDE" in system
    assert "immersive experience director" not in system


def test_directive_text_surface_can_add_library_placeholder():
    system = render_system_prompt(
        budget=10**9,
        surfaces={
            "directives": {"director": "before <<library>> after"},
            "library_doc": "THE-DOC",
        },
    )
    assert "before THE-DOC after" in system


def test_factory_seed_does_not_bake_directive_text():
    # A `parts.py` directive edit must reach live DBs by inheritance; the seed
    # and the committed artifact must carry NO baked directive copies (the
    # normalizer drops an empty mapping, so the key is absent entirely).
    assert not factory_surfaces().get("directives")
    artifact = factory.load_artifact()
    assert artifact is not None  # the committed repo artifact is present
    assert not (artifact["surfaces"].get("directives") or {})


def test_empty_directive_surface_renders_code_catalog():
    # The inherit contract in behaviour: an EMPTY directives surface renders
    # the code catalog byte-for-byte (schema docstring: absent/empty -> constant).
    with_empty = render_system_prompt(budget=10**9, surfaces={"directives": {}})
    plain = render_system_prompt(budget=10**9)
    assert with_empty == plain
    assert "## Scene images" in with_empty  # a code-catalog directive present
