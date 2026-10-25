"""Tests for the D18 variable registry/resolver (:mod:`fabulist.prompts.variables`).

Seeded the way ``tests/test_personas_api.py`` / ``tests/test_facts_suffix.py``
seed a chat: a tmp-path :class:`~fabulist.log.EventLog` closed in teardown, with
``append_event`` rows — a ``system`` card payload (``payload["card"]``), a
``system`` ``chat_persona`` assignment plus a ``persona`` definition on the
``__personas__`` anchor, ``user`` turns, a ``narrative`` with ``prose``, and a
``state-delta`` fold. The resolver reads the log only (D9).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.prompts.library_mini import render_library_doc
from fabulist.prompts.variables import (
    PERSONAS_ANCHOR,
    VARIABLE_SPECS,
    VariableSpec,
    render_template,
    resolve_variables,
)

CARD = {
    "name": "Lyra Voss",
    "description": "A wandering cartographer.",
    "personality": "Curious and wry.",
    "scenario": "A rain-soaked night market.",
    "first_mes": "Lyra looks up from her maps.",
}

STATE = {
    "time": "14:35",
    "loc": "night market",
    "weather": "light rain",
    "tone": "warm",
    "inv": "brass compass",
}


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for an :class:`EventLog` on a tmp path; closes logs after."""
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


def _seed(log: EventLog) -> str:
    """Seed one chat: card, persona assignment, two user turns, prose, state."""
    sid = "chat1"
    log.append_event(sid, "system", "user", {"event": "session_created"})
    log.append_event(
        sid, "system", "system", {"card": CARD, "first_mes": CARD["first_mes"]}
    )
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": "p1", "name": "Alice"},
    )
    log.append_event(sid, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    log.append_event(sid, "user", "user", {"text": "I knock twice."})
    log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": "The door creaks open.", "dsl": "", "segments": []},
    )
    log.append_event(sid, "user", "user", {"text": "I open the door."})
    log.append_event(sid, "state-delta", "system", dict(STATE))
    return sid


# --- registry ---------------------------------------------------------------


def test_variable_specs_cover_the_required_names():
    required = {
        "char",
        "persona",
        "user",
        "last_user_turn",
        "last_narrative",
        "state",
        "facts",
        "recall",
        "wi",
        "library",
        # D35: the factory library-doc surface expression target.
        "library_doc",
        "time",
        "loc",
        "weather",
        "tone",
        "inv",
        "history_count",
        "now",
        "turns",
    }
    names = {spec.name for spec in VARIABLE_SPECS}
    assert required <= names
    assert all(isinstance(spec, VariableSpec) for spec in VARIABLE_SPECS)
    assert all(spec.description and isinstance(spec.example, str) for spec in VARIABLE_SPECS)


def test_resolve_variables_returns_every_spec(build_log):
    log = build_log()
    sid = _seed(log)
    values = resolve_variables(log, sid, "I open the door.")
    assert set(values) == {spec.name for spec in VARIABLE_SPECS}
    assert all(isinstance(v, str) for v in values.values())


# --- log-derived values -----------------------------------------------------


def test_resolve_char_persona_and_user(build_log):
    log = build_log()
    sid = _seed(log)
    values = resolve_variables(log, sid, "I open the door.")
    # D43: {{char}} is the frozen cast form (single seeded card); {{user}} is
    # the selected persona NAME and the message text moved to {{message}}.
    assert values["char"] == "[cast: Lyra Voss]"
    assert values["persona"] == "Alice"
    assert values["user"] == "Alice"
    assert values["message"] == "I open the door."


def test_resolve_previous_turn_values(build_log):
    log = build_log()
    sid = _seed(log)
    values = resolve_variables(log, sid, "I open the door.")
    # The current user message is already logged; the previous one is returned.
    assert values["last_user_turn"] == "I knock twice."
    assert values["last_narrative"] == "The door creaks open."


def test_resolve_state_scalars(build_log):
    log = build_log()
    sid = _seed(log)
    values = resolve_variables(log, sid, "I open the door.")
    for key, expected in STATE.items():
        assert values[key] == expected
    # state is the merged pipe-YAML line.
    assert values["state"] == (
        "time: 14:35 | loc: night market | weather: light rain | tone: warm "
        "| inv: brass compass"
    )


def test_resolve_facts_suffix_is_dynamic_only(build_log):
    # D34: the persona card block moved to the pre-history static region
    # (context.facts.static_prefix_parts, spliced by the narrator builder);
    # {{facts}} carries dynamic per-turn state only.
    log = build_log()
    sid = _seed(log)
    values = resolve_variables(log, sid, "I open the door.")
    assert "### Character: Lyra Voss" not in values["facts"]
    assert "time: 14:35" in values["facts"]


def test_resolve_counts(build_log):
    log = build_log()
    sid = _seed(log)
    values = resolve_variables(log, sid, "I open the door.")
    # 2 user messages + 1 narrative = 3 history rows; 1 completed narrative.
    assert values["history_count"] == "3"
    assert values["turns"] == "1"


def test_image_narrative_does_not_change_turns_or_last_narrative(build_log):
    """A generated-image narrative row is a canon image (no prose), not a turn:
    it must not shift ``{{turns}}`` / ``{{history_count}}`` or become
    ``{{last_narrative}}``."""
    log = build_log()
    sid = _seed(log)
    baseline = resolve_variables(log, sid, "I open the door.")
    assert baseline["turns"] == "1"
    assert baseline["history_count"] == "3"
    assert baseline["last_narrative"] == "The door creaks open."

    # A generated image lands as the NEWEST narrative row (imagegen actor, no
    # prose) — the row shape ``is_image_narrative`` recognizes.
    log.append_event(
        sid,
        "narrative",
        "imagegen",
        {
            "prose": "",
            "dsl": "",
            "kind": "image",
            "image_asset_id": "asset-img-1",
            "prompt": "the tavern",
        },
    )

    after = resolve_variables(log, sid, "I open the door.")
    assert after["turns"] == "1"
    assert after["history_count"] == "3"
    assert after["last_narrative"] == "The door creaks open."


def test_resolve_library_and_now(build_log):
    log = build_log()
    sid = _seed(log)
    values = resolve_variables(log, sid, "I open the door.")
    # The library variable carries the generated component doc verbatim.
    assert values["library"] == render_library_doc()
    assert "root = ScenePane" in values["library"]
    # D35: `library_doc` resolves through the same chain (artifact + fallback)
    # and is what the factory surface expression `{{library_doc}}` expands to.
    assert values["library_doc"] == render_library_doc()
    assert "root = ScenePane" in values["library_doc"]
    # now is a parseable UTC ISO timestamp.
    assert datetime.fromisoformat(values["now"]).tzinfo is not None


def test_resolve_recall_is_empty_by_default(build_log):
    log = build_log()
    sid = _seed(log)
    assert resolve_variables(log, sid, "hi")["recall"] == ""


def test_resolve_extra_overrides_and_adds(build_log):
    log = build_log()
    sid = _seed(log)
    values = resolve_variables(
        log, sid, "I open the door.", extra={"recall": "### Recalled", "char": "Over"}
    )
    assert values["recall"] == "### Recalled"
    assert values["char"] == "Over"


# --- edge cases -------------------------------------------------------------


def test_resolve_without_persona_assignment_is_sandbox(build_log):
    log = build_log()
    sid = "plain"
    log.append_event(sid, "user", "user", {"text": "Hello."})
    values = resolve_variables(log, sid, "Hello.")
    assert values["persona"] == ""
    assert values["char"] == ""
    assert values["last_user_turn"] == ""
    assert values["last_narrative"] == ""
    assert values["turns"] == "0"
    assert values["history_count"] == "1"


def test_resolve_current_message_not_yet_logged(build_log):
    log = build_log()
    sid = "chat1"
    log.append_event(sid, "user", "user", {"text": "First."})
    # Caller passes a fresh message that is not (yet) in the log.
    values = resolve_variables(log, sid, "Second.")
    assert values["last_user_turn"] == "First."


# --- render_template (also exercised via the module engine) ------------------


def test_render_template_unknown_stays_literal_with_spacing(build_log):
    assert render_template("a {{ ghost }} b", {"char": "x"}) == "a {{ ghost }} b"


def test_resolved_values_render_into_a_template(build_log):
    log = build_log()
    sid = _seed(log)
    values = resolve_variables(log, sid, "I open the door.")
    out = render_template("{{char}} at {{loc}}: {{last_narrative}}", values)
    assert out == "[cast: Lyra Voss] at night market: The door creaks open."
