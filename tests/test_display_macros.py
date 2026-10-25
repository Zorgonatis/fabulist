"""Issue #63 — known macros render in the transcript, the Opening and the prompt.

Reported defect: typing ``{{user}}`` into a chat message rendered as the literal
string ``{{user}}``; the owner added that the FIRST message / opening message
shows the raw tag too.

The fix is ONE display step, :func:`fabulist.prompts.variables.render_display_text`
(over :func:`~fabulist.prompts.variables.render_template`, the prompt engine's
own substitution), applied at every read seam that shows or sends the text:

* :meth:`fabulist.log.EventLog.get_projection` attaches ``payload["text_rendered"]``
  to each user row — the stored event keeps the RAW bytes (ADR-001: store raw,
  render at read time; a revision edit and ``{{last_user_turn}}`` still match).
* ``GET /api/sessions/{id}/opening`` renders the greeting cycle (the Opening
  block is the chat's first message).
* the prompt path — history rows and the final user message (narrate + OOC),
  plus the opening builder's card block — renders the SAME map, so the model and
  the transcript can never disagree.

Unknown tokens stay literal (never blanked) and a persona-less chat keeps the
documented ``user`` fallback.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cards_api import router as cards_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.graph.plans import Plan
from fabulist.log import EventLog
from fabulist.personas.registry import PERSONAS_ANCHOR
from fabulist.prompts.cast import CAST_SNAPSHOT_EVENT
from fabulist.prompts.variables import (
    display_variables,
    render_display_text,
    resolve_variables,
)
from fabulist.wiring import (
    history_from_log,
    make_narrator_message_builder,
    make_ooc_message_builder,
)


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


#: A card whose greeting carries both display macros — the live bug's shape
#: (SillyTavern cards are authored with ``{{user}}`` / ``{{char}}``).
CARD = {
    "name": "Lyra Voss",
    "description": "A wandering cartographer who owes {{user}} a map.",
    "first_mes": "Welcome aboard, {{user}}. {{char}} salutes.",
    "alternate_greetings": ["You again, {{user}}?"],
}


def _assign_persona(log: EventLog, sid: str, name: str = "Alice") -> None:
    """Seed a persona definition on the anchor + a latest-wins chat assignment."""
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": "p1", "name": name},
    )
    log.append_event(
        sid, "system", "user", {"event": "chat_persona", "persona_id": "p1"}
    )


def _seed(log: EventLog, user_text: str, *, persona: str | None = "Alice") -> str:
    """A one-turn chat: session row, card seed, persona assignment, one user row."""
    sid = "chat1"
    log.append_event(sid, "system", "user", {"event": "session_created"})
    log.append_event(
        sid, "system", "system", {"card": CARD, "first_mes": CARD["first_mes"]}
    )
    if persona is not None:
        _assign_persona(log, sid, persona)
    log.append_event(sid, "user", "user", {"text": user_text})
    return sid


def _user_row(log: EventLog, sid: str) -> dict:
    rows = log.get_projection(sid)["user_messages"]
    assert rows, "the projection dropped the user row"
    return rows[0]


# --- the display step ------------------------------------------------------


def test_display_variables_reads_the_same_folds_as_the_prompt(build_log):
    """ONE map: the display render and the prompt resolve the same names."""
    log = build_log()
    sid = _seed(log, "hi")

    display = display_variables(log, sid)
    resolved = resolve_variables(log, sid, "hi")

    for name in ("user", "persona", "char", "world"):
        assert display[name] == resolved[name], name
    assert display["user"] == "Alice"


def test_the_display_step_is_read_only(build_log):
    """A display read never writes: no ``cast_snapshot`` memoisation (ADR-001)."""
    log = build_log()
    sid = _seed(log, "I greet {{user}}.")
    before = len(log.get_events(sid))

    log.get_projection(sid)
    render_display_text(log, sid, "{{user}} again")

    after = log.get_events(sid)
    assert len(after) == before
    assert not [
        event
        for event in after
        if event.type == "system"
        and isinstance(event.payload, dict)
        and event.payload.get("event") == CAST_SNAPSHOT_EVENT
    ]


def test_render_display_text_leaves_text_without_macros_alone(build_log):
    log = build_log()
    sid = _seed(log, "hi")
    assert render_display_text(log, sid, "plain prose") == "plain prose"


# --- the transcript (projection) -------------------------------------------


def test_a_user_message_renders_the_user_macro_in_the_projection(build_log):
    """(a) ``{{user}}`` in a typed message renders as the persona name."""
    log = build_log()
    sid = _seed(log, "I tell {{user}} that {{char}} sent me.")

    payload = _user_row(log, sid)["payload"]

    # The stored/derived RAW text is untouched — the revision door re-opens it.
    assert payload["text"] == "I tell {{user}} that {{char}} sent me."
    assert payload["text_rendered"] == "I tell Alice that [cast: Lyra Voss] sent me."


def test_an_unknown_macro_stays_literal_in_the_rendered_text(build_log):
    """(d) an unknown token is visible, never blanked."""
    log = build_log()
    sid = _seed(log, "{{nope}} greets {{user}}.")

    rendered = _user_row(log, sid)["payload"]["text_rendered"]

    assert rendered == "{{nope}} greets Alice."


def test_no_persona_keeps_the_documented_literal_user(build_log):
    """(e) persona-less chat -> the word ``user``, never the raw token."""
    log = build_log()
    sid = _seed(log, "{{user}} knocks.", persona=None)

    rendered = _user_row(log, sid)["payload"]["text_rendered"]

    assert rendered == "user knocks."
    assert "{{" not in rendered


def test_a_message_without_macros_carries_its_text_verbatim(build_log):
    """The field is always present on a user row (a client may read it blind)."""
    log = build_log()
    sid = _seed(log, "I open the door.")

    payload = _user_row(log, sid)["payload"]

    assert payload["text_rendered"] == payload["text"] == "I open the door."


def test_a_fork_inherits_the_rendered_user_text(build_log):
    """Fork-aware: an inherited row renders with the BRANCH's persona."""
    log = build_log()
    sid = _seed(log, "{{user}} waits.")
    user_seq = _user_row(log, sid)["seq"]
    log.fork_session(sid, at_seq=user_seq, new_session_id="branch")
    _assign_persona(log, "branch", "Bob")

    rendered = _user_row(log, "branch")["payload"]["text_rendered"]

    assert rendered == "Bob waits."


# --- the Opening message ---------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_the_opening_greeting_renders_through_the_opening_route(tmp_path):
    """(b) the Opening block (the chat's first message) renders its greeting."""
    log_path = tmp_path / "opening.db"
    app = create_app(FakeTurnEngine(), log_path)
    app.include_router(cards_router)
    try:
        sid = _seed(app.state.log, "hi")
        raw_greetings = [
            event.payload.get("first_mes")
            for event in app.state.log.get_events(sid)
            if event.type == "system" and isinstance(event.payload, dict)
            and isinstance(event.payload.get("card"), dict)
        ]
        # The stored definition stays raw (ADR-001).
        assert raw_greetings == [CARD["first_mes"]]

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.get(f"/api/sessions/{sid}/opening")

        resp = _run(scenario())
        assert resp.status_code == 200
        greetings = resp.json()["greetings"]
        assert greetings[0] == "Welcome aboard, Alice. [cast: Lyra Voss] salutes."
        assert greetings[1] == "You again, Alice?"
        assert all("{{" not in greeting for greeting in greetings)
        # The raw card dict the Details view shows is NOT rewritten.
        assert resp.json()["card"]["first_mes"] == CARD["first_mes"]
    finally:
        app.state.log.close()


def test_the_opening_prompt_renders_the_card_block(tmp_path):
    """The opening builder's card block carries the resolved name, not a token."""
    from fabulist.api.planner_api import _opening_messages

    log = EventLog(tmp_path / "opening-prompt.db")
    try:
        sid = _seed(log, "hi")
        messages = _opening_messages(log, sid, Plan(turn_seq=0))

        content = "\n".join(str(message.get("content", "")) for message in messages)
        assert "Welcome aboard, Alice." in content
        assert "{{user}}" not in content
    finally:
        log.close()


# --- the prompt (assembled context) ---------------------------------------


def _turn_state(sid: str, user_content: str) -> dict:
    return {
        "session_id": sid,
        "user_content": user_content,
        "kind": "in_character",
        "facts": "",
        "recall": "",
        "rolls": "",
    }


def test_the_prompt_history_carries_the_resolved_name(build_log):
    """(c) the history row the model reads matches the transcript's render."""
    log = build_log()
    sid = _seed(log, "I tell {{user}} about {{char}}.")
    # A prior turn gives history a row that is NOT the current ask.
    log.append_event(
        sid, "narrative", "narrator", {"prose": "Lyra nods.", "segments": []}
    )
    log.append_event(sid, "user", "user", {"text": "{{user}} leaves."})

    hist = history_from_log(log, sid)
    user_rows = [m["content"] for m in hist if m["role"] == "user"]

    assert user_rows == ["I tell Alice about [cast: Lyra Voss].", "Alice leaves."]


def test_the_assembled_narrate_prompt_carries_the_resolved_name(build_log):
    """(c) the final user message the model receives is the rendered text."""
    log = build_log()
    sid = _seed(log, "I tell {{user}} the news.")
    build = make_narrator_message_builder(log)

    messages = build(_turn_state(sid, "I tell {{user}} the news."))

    contents = [str(message.get("content", "")) for message in messages]
    assert any("I tell Alice the news." in content for content in contents)
    assert all("{{user}}" not in content for content in contents)


def test_the_ooc_prompt_carries_the_resolved_name(build_log):
    """The OOC path renders the same step (its own final user message)."""
    log = build_log()
    sid = _seed(log, "{{user}} asks out of character.")

    messages = make_ooc_message_builder(log)(sid, "{{user}} asks out of character.")

    contents = [str(message.get("content", "")) for message in messages]
    assert any("Alice asks out of character." in content for content in contents)
    assert all("{{user}}" not in content for content in contents)


def test_the_resolved_message_variable_matches_the_display(build_log):
    """A preset emitting ``{{message}}`` sees the same rendered text."""
    log = build_log()
    sid = _seed(log, "hi")

    variables = resolve_variables(log, sid, "I hail {{user}}.")

    assert variables["message"] == "I hail Alice."
