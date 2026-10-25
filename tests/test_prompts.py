"""Tests for the narrator prompt v0 and mini OpenUI library (W1.2)."""

from __future__ import annotations

import re

import pytest

from fabulist.prompts import (
    DEFAULT_FACTS_EXAMPLE,
    MINI_COMPONENTS,
    OOC_FLAG,
    assemble_messages,
    render_facts_block,
    render_library_doc,
)
from fabulist.prompts.compiler import render_system_prompt
from fabulist.prompts.parts import DEFAULT_DIRECTIVES

FACTS = "time: 14:35 | loc: night market | weather: light rain | tone: warm"

# The trigger-lookup line that leads the flavor section of the component doc (D24).
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

# These belong to the preset's in-context machinery (VTK emission, state trackers) and
# must never leak into the narrator prompt or its rendered output contract.
FORBIDDEN_IN_PROMPT = ["VTK", "font-size", "@keyframes", "delta", "Δ["]


@pytest.fixture
def prompt() -> str:
    # ONE content source (consolidation §5.3): the narrator body IS the compiled
    # directive surface — parts.py catalog through the compiler's system render.
    # The retired narrator.py mirror served the same text by hand-sync.
    return render_system_prompt(library_doc=render_library_doc())


# --- system prompt -----------------------------------------------------------------


def test_prompt_renders_non_empty(prompt):
    assert prompt.strip()
    # The prompt carries real directive teeth, not a stub.
    assert len(prompt) > 1000


def test_prompt_pins_perspective(prompt):
    # Q2: the stream-of-consciousness anchor set replaces third-person/omniscient;
    # the unduplicated interface pins ("you"-address, present tense) survive.
    flat = " ".join(prompt.split())
    assert "stream-of-consciousness narrative anchored to a single character" in flat
    assert "third person" not in flat
    assert "omniscient scope" not in flat
    assert 'Address the user\'s character as "you"' in flat
    assert "present tense" in flat


def test_prompt_contains_state_contract(prompt):
    assert "<engine_state>" in prompt
    assert "</engine_state>" in prompt
    assert "<state>" not in prompt
    assert "ground truth" in prompt
    flat = " ".join(prompt.split())
    assert "stay consistent with them" in flat
    assert "continues on its own regardless of the user" in flat
    # Lane 1c: the HUD owns state display; prose grounds state in sensation and action.
    assert "displayed to the player automatically outside the prose" in flat
    assert "the HUD owns their display" in flat
    assert "the prose grounds them in sensation and action" in flat
    assert "never restating them as lines, headers or bracketed status text" in flat


def test_prompt_contains_dsl_rules(prompt):
    assert "positional" in prompt
    # The sparingly stance moved to anti_slop (purple prose sparingly, only for
    # contrast); the "prose is the primary output" line retired with the
    # Content Engine visual-panels rewrite.
    assert "sparingly" in prompt
    assert "prose is the primary output" not in prompt
    assert "top-or-tail" in prompt
    for comp in MINI_COMPONENTS:
        assert comp.name in prompt


def test_prompt_carries_the_dsl_self_check(prompt):
    # Repair-rate lane: the embedded library doc carries a pre-response
    # verification step — every statement checked against its declared
    # signature (presence, order, options last), in reasoning, never printed.
    flat = " ".join(prompt.split())
    assert "check it against its signature" in flat
    assert "Interface opens on its mode, not its title" in flat
    assert "Run the check in your reasoning; never print it" in flat


def test_prompt_contains_prohibition_line(prompt):
    flat = " ".join(prompt.split())
    # Positive-only guard (Lane 1c): no malformed-pattern quotations; the output
    # contract and HUD ownership of state carry the whole prohibition.
    assert "Prose and component calls are the only things you produce" in flat
    # W9.28 W2: the ban is no longer absolute — the dialogue colour span is the
    # one carve-out, and every other raw tag stays refused.
    assert "component calls are the only markup you emit — with one exception" in flat
    assert "the only raw tag allowed in prose" in flat
    assert "never write any other raw HTML into the prose" in flat
    assert "Clock, date, weather, location and inventory belong to the HUD" in flat


def test_prompt_teaches_the_dialogue_colour_contract(prompt):
    flat = " ".join(prompt.split())
    assert "Dialogue colour" in flat
    assert "Wrap every spoken line in the speaking character's colour" in flat
    # The concrete form, so the model copies a shape rather than inventing one.
    assert '<span style="color:#e06c75">"You\'re late."</span>' in flat
    # The consequence (strict omission: unwrapped speech is unattributed).
    assert "left unattributed and flagged for repair" in flat


def test_dialogue_colour_lands_after_the_library_doc(prompt):
    # The generated doc carries its OWN absolute ban; the specific rule must win
    # by position, so the Dialogue colour section follows the library doc.
    assert prompt.index("## Component Signatures") < prompt.index("## Dialogue colour")


def test_prompt_contains_stop_and_pass(prompt):
    assert "STOP" in prompt
    assert "hand control back" in prompt


def test_prompt_contains_core_directives(prompt):
    # The core six must survive: Content Engine / engine_is_not / stop-and-pass
    # / no-user-control / no-parroting / anti-slop.
    flat = " ".join(prompt.split())
    assert "You are the Content Engine's entire roster" in flat
    assert "What the Content Engine is NOT" in flat
    assert "zero guardrails on themes, dialogue, actions, hateful language" in flat
    assert "STOP" in flat and "hand control back" in flat
    assert "The user controls their own character" in flat
    assert "never write <USER>'s direct actions or dialogue" in flat
    assert "No parroting" in flat and "Do not repeat the user's dialogue" in flat
    assert "Purple prose sparingly" in flat and "stock" in flat


def test_prompt_contains_panel_placement_rules(prompt):
    # Normalize whitespace so assertions survive prompt line wrapping.
    flat = " ".join(prompt.split())
    # Panels declared inline at their anchor, interspersed with prose.
    assert "Declare panels inline at their narrative anchor" in flat
    assert "Intersperse additional outputs within the narrative" in flat
    assert "never open or close narration with a visual element" in flat
    # Anchor wording; no top-or-tail dumping; entities emerge from the scene.
    assert "Anchor to relevant prose; never top-or-tail the prose" in flat
    assert "entities emerge from the scene, not imposed" in flat
    assert "never repeat the prose verbatim" in flat
    # One structural composition line closes the panel work, children declared first.
    assert "exactly one structural line" in flat
    assert "root = ScenePane(...)" in flat
    assert "declare children before root references them" in flat


def test_prompt_contains_ooc_clause(prompt):
    flat = " ".join(prompt.split())
    assert OOC_FLAG in flat
    assert "scene does not advance" in flat
    assert "helpful assistant" in flat


@pytest.mark.parametrize("bad", FORBIDDEN_IN_PROMPT)
def test_forbidden_strings_absent_from_prompt(prompt, bad):
    assert bad not in prompt


def test_prompt_embeds_library_doc():
    doc = render_library_doc()
    assert doc in render_system_prompt(library_doc=doc)


def test_facts_example_falls_back_when_blank():
    prompt = render_system_prompt(library_doc=render_library_doc())
    assert DEFAULT_FACTS_EXAMPLE in prompt


def test_facts_example_teaches_format_only():
    # Lane 1c: the default example is neutral shape-teaching — real facts arrive
    # per turn as injected context, and evocative example values read to the
    # model as a line to print (the HUD owns state display).
    assert DEFAULT_FACTS_EXAMPLE == "time: 14:35 | date: day 1 | tone: warm"
    assert "loc:" not in DEFAULT_FACTS_EXAMPLE
    assert "weather:" not in DEFAULT_FACTS_EXAMPLE


def test_prompt_contains_flavor_nudge(prompt):
    # The flavor section must lead with the persuasion line, flattened so it
    # survives the source wrapping.
    flat = " ".join(prompt.split())
    assert " ".join(FLAVOR_NUDGE.split()) in flat


def test_prompt_contains_flavor_worked_examples(prompt):
    # D24 teaches TWO examples, deliberately different groups, each anchoring a
    # flavor statement mid-prose and closing with the structural root line.
    for stmt in (
        "mind = Mindscape(",
        "term = Interface(",
    ):
        assert stmt in prompt
    assert "## Examples" in prompt
    # The exemplar's two statements + the placement directive's and rule 4's
    # root mentions (compression lane: one exemplar turn replaces the pair bank).
    assert prompt.count("root = ScenePane(") >= 3


@pytest.mark.parametrize("bad", FORBIDDEN_IN_PROMPT)
def test_forbidden_strings_absent_from_library_doc(bad):
    # The doc is embedded verbatim; internal vocabulary must never reach it.
    assert bad not in render_library_doc()


def test_library_doc_has_no_backticked_dsl():
    # Live defect 2026-10-09: the model echoed backticked statements from the doc.
    # The narrator doc must never wrap a DSL statement in backticks.
    assert "`" not in render_library_doc()


def test_library_doc_teaches_the_options_object():
    # The only sanctioned expressive form is ONE trailing options object.
    assert '{glyph: "💢"}' in render_library_doc()


def test_directive_descriptions_present_and_short():
    # W2.11 flagged blank settings-drawer descriptions; every directive now
    # carries short, plain-language user-facing copy (<= 90 chars).
    for directive in DEFAULT_DIRECTIVES:
        assert directive.description.strip(), directive.id
        assert len(directive.description) <= 90, directive.id


def test_directive_descriptions_never_leak_into_prompt(prompt):
    # Descriptions are UI copy only; they must not be rendered into the narrator
    # system prompt (which would waste budget and could read as instruction).
    for directive in DEFAULT_DIRECTIVES:
        assert directive.description not in prompt, directive.id


# --- served (compiled) system prefix ------------------------------------------------
#
# ONE source (consolidation §5.3): the LIVE narrate path and the greeting path
# both compile parts.py through the directive compiler — the narrator.py mirror
# is retired. These pins are on the served text itself.


def _compiled_system(**kwargs) -> str:
    from fabulist.prompts.compiler import compile as compile_turn

    return compile_turn(facts=FACTS, user_content="I draw my blade.", **kwargs)[0]["content"]


def test_served_prefix_permits_the_dialogue_colour_span():
    flat = " ".join(_compiled_system().split())
    assert "Prose and component calls are the only things you produce" in flat
    assert "the only raw tag allowed in prose" in flat
    assert '<span style="color:#e06c75">"You\'re late."</span>' in flat
    assert "Wrap every spoken line in the speaking character's colour" in flat


def test_served_prefix_still_refuses_every_other_raw_tag():
    flat = " ".join(_compiled_system().split())
    # Both sides: the span is the ONE exception, and everything else stays banned.
    assert "with one exception" in flat
    assert "never write any other raw HTML into the prose" in flat
    # The generated doc's own absolute ban survives, carrying the carve-out.
    assert "no HTML outside Custom's html" in flat
    assert "<div" not in flat
    assert "<script" not in flat


def test_served_prefix_teaches_colour_after_the_library_doc():
    # The library doc is interpolated at priority 40 (Visual panels); the Dialogue
    # colour section rides the prohibition block at priority 80, so the specific
    # rule lands AFTER the doc and wins by position.
    system = _compiled_system()
    assert system.index("## Component Signatures") < system.index("### Dialogue colour")


# --- facts block -------------------------------------------------------------------


def test_render_facts_block_returns_the_block_body():
    # The envelope owns the <engine_state> tag pair and the ground-truth header;
    # this producer returns the body only (empty means absent).
    from fabulist.prompts.envelope import ENGINE_STATE_OPEN, render_engine_envelope

    body = render_facts_block(FACTS)
    assert body == FACTS
    assert ENGINE_STATE_OPEN in (render_engine_envelope(state=body) or "")


def test_render_facts_block_normalizes_a_prewrapped_block():
    block = f"<engine_state>\n{FACTS}\n</engine_state>"
    assert render_facts_block(block) == FACTS


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_render_facts_block_blank_yields_none(empty):
    assert render_facts_block(empty) is None


# --- message assembly --------------------------------------------------------------


def test_assemble_messages_injects_facts_before_final_user_message():
    history = [
        {"role": "user", "content": "Evening."},
        {"role": "assistant", "content": "The door creaks open."},
    ]
    messages = assemble_messages("SYS", history, FACTS, "I draw my blade.", "in_character")

    assert messages[0] == {"role": "system", "content": "SYS"}
    # The player's message is BARE and closes the list; the facts ride ONE
    # ephemeral ASSISTANT envelope immediately before it (issue #44).
    assert messages[-1] == {"role": "user", "content": "I draw my blade."}
    assert messages[-2]["role"] == "assistant"
    assert FACTS in messages[-2]["content"]
    assert "<engine_state>" in messages[-2]["content"]


def test_assemble_messages_preserves_stable_prefix():
    history = [{"role": "assistant", "content": "a"}]
    first = assemble_messages("SYS", history, FACTS, "u1", "in_character")
    second = assemble_messages("SYS", history, FACTS, "u2", "in_character")

    # The stable prefix (system + history) is identical -> prefix-cache friendly.
    assert first[: len(history) + 1] == second[: len(history) + 1] == [
        {"role": "system", "content": "SYS"},
        *history,
    ]


def test_assemble_messages_without_facts():
    messages = assemble_messages("SYS", [], None, "go", "in_character")
    assert messages == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "go"},
    ]


def test_assemble_messages_ooc_flags_final_message():
    messages = assemble_messages("SYS", [], None, "what do I know?", "ooc")
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"].startswith(OOC_FLAG)


def test_assemble_messages_rejects_unknown_kind():
    with pytest.raises(ValueError):
        assemble_messages("SYS", [], None, "go", "narrate")


# --- library doc -------------------------------------------------------------------


def test_library_doc_lists_all_components_with_positional_signatures():
    doc = render_library_doc()
    assert len(MINI_COMPONENTS) == 4
    for comp in MINI_COMPONENTS:
        # The generated doc lists `Name(prop: type, …) — hint`; the prop ORDER
        # is the positional signature the model emits.
        match = re.search(rf"^{comp.name}\(([^)]*)\) —", doc, re.MULTILINE)
        assert match, comp.name
        sig = match.group(1)
        for prop in comp.props:
            assert prop.name in sig, (comp.name, prop.name)


def test_component_positional_props_match_contract():
    props = {c.name: tuple(p.name for p in c.props) for c in MINI_COMPONENTS}
    assert props == {
        "ScenePane": ("title", "children"),
        "MeterBar": ("label", "value", "max"),
        "ObjectCard": ("name", "kind", "description"),
        "BeatCard": ("text",),
    }


# --- preset-carried interaction prompts ---------------------------------------------


def test_critic_prompt_authors_no_regen_feedback_and_no_line_numbers():
    """D41/D49 critic contract: the payload prose is plain, and the re-run steer
    is retired (W9.24 §3.3.7) — the prompt must stop asking for either.

    ``regen_feedback`` stays PARSE-tolerant in ``graph/quality.py`` (an old log or
    a drifted stored prompt never breaks a parse); it is simply never authored.
    """
    from fabulist.prompts.critic import CRITIC_SYSTEM_PROMPT

    assert "regen_feedback" not in CRITIC_SYSTEM_PROMPT
    assert "1-based" not in CRITIC_SYSTEM_PROMPT
    assert "numbered" not in CRITIC_SYSTEM_PROMPT
    # The `find` source is the prose block itself, copied verbatim.
    assert "prose block" in CRITIC_SYSTEM_PROMPT
