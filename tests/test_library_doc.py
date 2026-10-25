"""Focused tests for the narrator library doc (lang-core generation pipeline).

``render_library_doc`` is embedded verbatim in the narrator's cache-stable
system prefix. Its content is the narrator-doc-v2 craft doc (draft:
``the maintainer's private notes (unshipped)``) assembled by
``frontend/scripts/generate-library-spec.mjs``: the authored craft sections
(§1 emission framing + density, §2 permission to be loud — compressed to
principles, §5 ONE exemplar turn, §6 hard rules) followed by the lang-core-
derived ``## Component Signatures`` reference block, whose hint lines carry the
per-component when-to-use, shape and slot-menu guidance (compression lane:
the reference dominates; the glyph-menu and before/after example prose was
cut with the context relocated into the hints). The artifact is the ONE source:
``library_mini`` carries no hand-written mirror to fall back to.

The generated doc must:

  * stay under the prompt budget while documenting all 14 components
    (four infrastructure defaults + ten flavors);
  * demote the generics ("Infrastructure is a fallback, not a habit.") and lead
    into the flavor list with the trigger-lookup line that live-observed your-model-id
    needed before it would emit any flavor component;
  * teach the worked examples, each using the sanctioned trailing options object
    (never a bare named arg), anchored mid-prose and closed by the single
    structural root line (children declared first);
  * teach the restricted dialect only — positional args, the root ScenePane rule,
    the options-object rule and declare-before-root composition; no
    hoisting/root-first, no bindings, no tools, no ``$variables``, no edit mode;
  * never wrap a DSL statement in backticks (the model echoes the doc's
    formatting — live defect 2026-10-09) and keep internal pipeline vocabulary
    out (FORBIDDEN_IN_PROMPT).
"""

from __future__ import annotations

import json
import re

import pytest

from fabulist.dsl.store import FLAVORS_ENV_VAR, ComponentSetError
from fabulist.prompts import library_mini
from fabulist.prompts.library_mini import (
    LIBRARY_ARTIFACT_ENV_VAR,
    MINI_COMPONENTS,
    _load_flavors,
    component_names,
    render_library_doc,
    reset_library_artifact_cache,
)

#: The ten VTK flavor components, in the order the doc lists them.
FLAVOR_NAMES = (
    "Mindscape",
    "Interface",
    "Document",
    "Artifact",
    "DialogueSpotlight",
    "ReactionSpotlight",
    "FailureAchievement",
    "Memory",
    "Entrance",
    "Clash",
)

#: §4 per-component when-to-use fragments now carried by the reference block
#: (the terse schema `bestFor` nudges were replaced by the draft's §4 lines).
FLAVOR_USAGE = {
    "Mindscape": "a mind cracking loud enough to hear",
    "Interface": "any other screen the fiction owns",
    "Document": "a paper artifact in full",
    "Artifact": "an item examined",
    "DialogueSpotlight": "a line of dialogue that has earned the spotlight",
    "ReactionSpotlight": "one knee-jerk reaction held for a comedic beat",
    "FailureAchievement": "a comedic backfire trophy-styled",
    "Memory": "a flashback in shards",
    "Entrance": "a character's first appearance",
    "Clash": "one exchange of physical force",
    "Custom": "hand-built html + css for the one-off artifact",
    "Phone": "a group chat: senders, bubbles, read ticks, reaction pills",
    "SMS": "Phone's simpler sibling",
    "Monitor": "the machine's own screen",
    "PostIt": "one to three sticky notes on a wall",
    "SocialPost": "one feed post",
}

#: The surviving exemplar turn (compression lane): Interface with the full
#: options object (art + glyph) and Mindscape's six slots, declared before the
#: single structural root close. The four before/after pairs and the second
#: hybrid turn were cut with the craft compression — this is the few-shot anchor.
WORKED_EXAMPLES = (
    (
        'term = Interface("terminal", "DAEDALUS CORE // FAULT LOG"',
        'root = ScenePane("Daedalus — deck 4, listing", [term, mind])',
    ),
    (
        'mind = Mindscape("Forty Seconds of Calm"',
        'root = ScenePane("Daedalus — deck 4, listing", [term, mind])',
    ),
)

#: The trigger-lookup persuasion line, verbatim (whitespace-normalized at the
#: assertion seam). Re-anchored from the pre-D24 `_FLAVOR_NUDGE`.
FLAVOR_NUDGE = (
    "Infrastructure is a fallback, not a habit. Match the moment: "
    "group chat→Phone, text→SMS, feed post→SocialPost, error/crash screen→Monitor, "
    "sticky note→PostIt, any other screen/machine readout→Interface(terminal|hologram), "
    "letter→Document, inspection→Artifact, "
    "signature line→DialogueSpotlight, "
    "knee-jerk reaction→ReactionSpotlight, comedic backfire→FailureAchievement, "
    "memory→Memory, first entrance→Entrance, "
    "blow→Clash, cracking mind→Mindscape (biological NPCs only); "
    "nothing adequate or specific enough→Custom."
)

#: Budget for the generated doc in the stable prefix. narrator-doc-v2 integrates
#: the craft sections + worked examples + the trailing component reference block,
#: so it is larger than the D24 hand copy — this guards against runaway growth.
MAX_DOC_BYTES = 16000

FORBIDDEN_IN_PROMPT = ["VTK", "font-size", "@keyframes", "delta", "Δ["]

#: A component signature line: lang-core emits bare `Name(sig) — hint` lines.
_COMPONENT_LINE_RE = re.compile(r"^(?P<name>[A-Za-z]\w*)\((?P<sig>[^)]*)\) — (?P<hint>.+)$")


@pytest.fixture(autouse=True)
def _clean_artifact_cache():
    """The generated-doc cache is process-global; reset around every test."""
    reset_library_artifact_cache()
    yield
    reset_library_artifact_cache()


def _components(doc: str) -> list[tuple[str, str, str]]:
    """Every `Name(sig) — hint` line in the Component Signatures section."""
    section = doc.split("## Component Signatures", 1)[-1]
    out: list[tuple[str, str, str]] = []
    for line in section.splitlines():
        match = _COMPONENT_LINE_RE.match(line)
        if match:
            out.append((match["name"], match["sig"], match["hint"]))
    return out


def _flavor_bullets(doc: str) -> list[tuple[str, str, str]]:
    """Flavor lines only — infrastructure rows above and the artifact
    reproductions section below are excluded (bounded at the next heading)."""
    after = doc.split("### Scene flavors", 1)[-1]
    after = after.split("### ", 1)[0]
    out: list[tuple[str, str, str]] = []
    for line in after.splitlines():
        match = _COMPONENT_LINE_RE.match(line)
        if match:
            out.append((match["name"], match["sig"], match["hint"]))
    return out


# --- size + stability ---------------------------------------------------------------


def test_doc_stays_within_prompt_budget():
    size = len(render_library_doc().encode("utf-8"))
    assert size < MAX_DOC_BYTES, f"library doc is {size} bytes (>= {MAX_DOC_BYTES})"


def test_reference_block_dominates_the_doc():
    # Owner verdict (compression lane): the Component Signatures reference is
    # where the guidance lives; craft is invariants + one exemplar only.
    doc = render_library_doc()
    craft = doc.split("## Component Signatures", 1)[0]
    reference = doc[len(craft):]
    assert len(reference) > len(craft)


def test_doc_is_byte_stable_across_renders():
    # The doc is part of the cache-stable prefix; rendering must be deterministic.
    assert render_library_doc() == render_library_doc()


def test_render_matches_the_committed_generated_artifact():
    from pathlib import Path

    artifact = (
        Path(library_mini.__file__).resolve().parents[3]
        / "prompts"
        / "generated"
        / "library.doc.txt"
    )
    if not artifact.is_file():
        pytest.skip("generated library doc artifact not present")
    assert render_library_doc() == artifact.read_text(encoding="utf-8")


# --- demotion + nudge -----------------------------------------------------------------


def test_generics_are_demoted_to_defaults():
    doc = render_library_doc()
    assert "### Infrastructure (the defaults)" in doc
    assert "Infrastructure is a fallback, not a habit." in doc


def test_custom_group_leads_the_doc():
    # D62: Custom is FIRST-LISTED for salience (owner steering, variant b) —
    # its group precedes Infrastructure in the Component Signatures block.
    doc = render_library_doc()
    sigs = doc.index("## Component Signatures")
    assert sigs < doc.index("### Custom (the escape hatch)") < doc.index(
        "### Infrastructure (the defaults)"
    )


def test_flavor_nudge_leads_into_the_flavor_section():
    doc = render_library_doc()
    infra = doc.index("### Infrastructure (the defaults)")
    nudge = doc.index(" ".join(FLAVOR_NUDGE.split()))
    header = doc.index("### Scene flavors (10)")
    first_flavor = doc.index("Mindscape(", header)
    # The nudge sits after the infrastructure defaults and before the flavor list.
    assert infra < nudge < header < first_flavor


# --- dialect restriction ------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_section",
    [
        "Hoisting & Streaming",
        "## Important Rules",
        "## Final Verification",
        "## Edit Mode",
        "## Built-in Functions",
        "## Mutation",
        "## Query",
    ],
)
def test_non_dialect_sections_are_absent(forbidden_section):
    assert forbidden_section not in render_library_doc()


def test_dialect_teaches_positional_and_root_close():
    doc = render_library_doc()
    assert "strictly positional" in doc
    assert "root = ScenePane(...)" in doc


def test_hard_rules_carry_the_dialogue_colour_exception():
    # W9.28 W2: the doc's own absolute ban ("no HTML outside Custom's html")
    # carries exactly one carve-out — the colour span around a spoken line — so
    # the doc never contradicts the prompt's Dialogue colour section.
    doc = render_library_doc()
    assert "no HTML outside Custom's html" in doc
    assert "the one exception: the colour span around a spoken line" in doc
    assert '<span style="color:#e06c75">"You\'re late."</span>' in doc


def test_doc_teaches_pre_response_signature_check():
    # Repair-rate lane: pass-1 calls drop or reorder positional args (live:
    # Interface called without its leading mode), forcing the expensive
    # re-narrate pass. The doc must make the model verify each statement
    # against the declared signature BEFORE the reply — the check runs in
    # reasoning (thinking is enabled), and is never printed.
    doc = render_library_doc()
    flat = " ".join(doc.split())
    assert "check it against its signature" in flat
    # The three checkable properties: presence, order, options last.
    assert "every argument not marked ? present" in flat
    assert "in the declared order" in flat
    assert "the options object last" in flat
    # The measured top failure named at its root: Interface leads with mode.
    assert "Interface opens on its mode, not its title" in flat
    # Thinking-model phrasing: the check is reasoning-only, never output.
    assert "Run the check in your reasoning; never print it" in flat


# --- worked examples ---------------------------------------------------------------


def test_examples_section_and_loudness_teaching_present():
    doc = render_library_doc()
    assert "## Examples" in doc
    assert "## Permission to be loud" in doc
    # The compressed anti-report teaching (was: four before/after contrast pairs).
    assert "A panel is not a report" in doc
    assert "captions are the death of this craft" in doc


def test_worked_examples_present_with_options_object_and_root_close():
    doc = render_library_doc()
    for stmt, root in WORKED_EXAMPLES:
        # The statement is a real sanctioned call (trailing options object only)...
        assert stmt in doc, stmt
        component = stmt.split("=", 1)[1].strip().split("(", 1)[0]
        assert component in FLAVOR_NAMES + ("MeterBar",), (component, stmt)
        # ...and every example closes with the single structural root line.
        assert root in doc, root
        assert root.startswith("root = ScenePane(") and root.endswith("])"), root


def test_each_example_root_references_a_declared_child():
    doc = render_library_doc()
    for stmt, root in WORKED_EXAMPLES:
        variable = stmt.split("=", 1)[0].strip()
        assert variable in root, (variable, root)


def test_examples_use_the_sanctioned_options_object_only():
    doc = render_library_doc()
    # The verified expressive form is ONE trailing options object; rule 2 and
    # the exemplar both demonstrate it (art + glyph inside braces).
    for token in ('{glyph: "💢"}', '{art: "', 'glyph: "📡"'):
        assert token in doc, token
    # Every glyph:/art: key sits INSIDE an options object (brace depth > 0); a
    # key at brace depth 0 would be a bare named arg, which must never appear.
    depth = 0
    for match in re.finditer(r"[{}]|(?<![\w{])(?:glyph|art):", doc):
        token = match.group(0)
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        else:
            assert depth > 0, f"bare named arg {token!r} at offset {match.start()}"


def test_no_dsl_statement_is_wrapped_in_backticks():
    # Live defect 2026-10-09: the model echoed backticked statements from the doc.
    # The narrator doc must be entirely backtick-free.
    assert "`" not in render_library_doc()


# --- component coverage -------------------------------------------------------------


def test_all_components_documented():
    doc = render_library_doc()
    documented = {name for name, _sig, _hint in _components(doc)}
    for comp in MINI_COMPONENTS:
        assert comp.name in documented, comp.name
    flavors = _load_flavors()
    assert flavors, "flavors.json must resolve for the narrator doc"
    assert {f["component"] for f in flavors} == set(FLAVOR_NAMES) | {
        "Custom",
        "Phone",
        "SMS",
        "Monitor",
        "PostIt",
        "SocialPost",
    }
    for flavor in flavors:
        assert flavor["component"] in documented, flavor["component"]


def test_component_count_is_20():
    assert len(_components(render_library_doc())) == 20


def test_flavor_bullets_are_in_mirror_order():
    doc_names = [name for name, _sig, _hint in _flavor_bullets(render_library_doc())]
    assert tuple(doc_names) == FLAVOR_NAMES


def _signature_props(sig: str) -> dict[str, str]:
    """Split a rendered signature into ``{key: type}`` at brace depth 0.

    ``sig`` is the argument list between the parentheses; a nested shape
    (``messages: {from: string, reactions?: {emoji: string}[]}[]``) carries
    commas of its own, so a naive ``split(", ")`` would shred it.
    """
    parts: list[str] = []
    depth = 0
    token = ""
    for char in sig:
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(token)
            token = ""
        else:
            token += char
    if token.strip():
        parts.append(token)
    out: dict[str, str] = {}
    for part in parts:
        key, _sep, type_str = part.partition(":")
        out[key.strip()] = type_str.strip()
    return out


def test_flavor_signatures_include_every_prop_with_array_and_optional_marks():
    doc = render_library_doc()
    by_name = {name: sig for name, sig, _hint in _components(doc)}
    for flavor in _load_flavors():
        component = flavor["component"]
        sig = by_name[component]
        rendered = _signature_props(sig)
        for prop in flavor["props"]:
            # Every prop is rendered, and an optional one carries the `?` on the
            # KEY (the reference block's stated convention).
            key = prop["name"] if prop.get("required", True) else f"{prop['name']}?"
            assert key in rendered, (component, prop, sig)
            # An array prop is marked `[]` — the item shape may nest (braces and
            # spaces inside), so assert the suffix, not one whitespace-free token.
            if prop["type"].endswith("[]"):
                assert rendered[key].endswith("[]"), (component, prop, sig)


def test_flavor_usage_hints_are_documented():
    doc = render_library_doc()
    for name, fragment in FLAVOR_USAGE.items():
        # The reference block carries the draft's §4 when-to-use line, not the
        # terse schema `bestFor` nudge.
        assert fragment in doc, name


def test_infrastructure_signatures_are_positional():
    doc = render_library_doc()
    assert "ScenePane(title: string, children: component[], options?: object)" in doc
    assert "MeterBar(label: string, value: number, max: number, options?: object)" in doc
    assert "BeatCard(text: string, options?: object)" in doc


# --- forbidden vocabulary -----------------------------------------------------------


@pytest.mark.parametrize("bad", FORBIDDEN_IN_PROMPT)
def test_forbidden_vocabulary_absent_from_doc(bad):
    assert bad not in render_library_doc()


# --- missing artifact is a loud error, never a hand-written doc ---------------------


def _force_missing_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv(LIBRARY_ARTIFACT_ENV_VAR, str(tmp_path / "absent"))
    reset_library_artifact_cache()


def test_missing_artifact_raises_naming_the_artifact_and_generator(monkeypatch, tmp_path):
    # The generated artifact is the ONE source of the taught doc. A checkout
    # without it must fail loudly and say how to fix it — serving a stale
    # hand-authored mirror would silently teach a vocabulary nobody regenerated.
    _force_missing_artifact(monkeypatch, tmp_path)
    with pytest.raises(ComponentSetError) as excinfo:
        render_library_doc()
    message = str(excinfo.value)
    assert "library.doc.txt" in message
    assert str(tmp_path / "absent") in message
    assert "node scripts/generate-library-spec.mjs" in message


# --- hot reload (W9.27): the doc is data, not an import-time snapshot ----------------


def test_render_follows_a_hot_reloaded_doc_artifact(monkeypatch, tmp_path):
    """Regenerating the artifact changes the taught doc on the NEXT render — no
    restart, no reset call. This is the prefix side of the one-vocabulary seam."""
    monkeypatch.setenv(LIBRARY_ARTIFACT_ENV_VAR, str(tmp_path))
    (tmp_path / "library.doc.txt").write_text("## Component Signatures\nv1\n")
    reset_library_artifact_cache()
    assert render_library_doc() == "## Component Signatures\nv1\n"

    (tmp_path / "library.doc.txt").write_text("## Component Signatures\nv2, longer\n")
    assert render_library_doc() == "## Component Signatures\nv2, longer\n"


def test_reset_clears_the_flavors_mirror_cache(monkeypatch, tmp_path):
    """P2-4: the mirror feeds ``component_names``' fallback list, so a repointed
    ``FABULIST_FLAVORS_JSON`` must not keep reading the old one."""
    _force_missing_artifact(monkeypatch, tmp_path)
    first = tmp_path / "flavors-a.json"
    first.write_text(
        json.dumps({"flavors": [{"component": "Mindscape", "props": [{"name": "title"}]}]}),
        encoding="utf-8",
    )
    second = tmp_path / "flavors-b.json"
    second.write_text(
        json.dumps({"flavors": [{"component": "Entrance", "props": [{"name": "who"}]}]}),
        encoding="utf-8",
    )

    monkeypatch.setenv(FLAVORS_ENV_VAR, str(first))
    reset_library_artifact_cache()
    assert "Mindscape" in component_names()

    monkeypatch.setenv(FLAVORS_ENV_VAR, str(second))
    assert "Mindscape" in component_names()  # still the warm cache
    reset_library_artifact_cache()
    assert "Entrance" in component_names()
    assert "Mindscape" not in component_names()
