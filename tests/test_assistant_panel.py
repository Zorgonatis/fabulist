"""D38 lane 2c — assistant panel parser, per-turn hook, enablement + panel routes.

Covers the three backend pieces the panel lane adds:

* :mod:`fabulist.assistants.panel` — the tolerant ``asst = AssistantCard(...)``
  back-parser (fences, duplicate-last-wins, definition override, kwarg/garbage
  tolerance, absent options) plus the fork-aware log folds.
* :mod:`fabulist.assistants.auto` — the per-turn consult hook (disabled ->
  silent, enabled -> reply + panel events, model failure -> non-fatal).
* The enablement routes (``chat_config_api``) and the panel route
  (``assistants_api``) against a real FastAPI app + tmp event log.

No pytest-asyncio in this repo, so async scenarios run via ``asyncio.run`` in
sync tests (matching ``tests/test_assistants_api.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.assistants_api import (
    ASSISTANTS_EVENT,
    ASSISTANTS_SESSION_ID,
    ASSISTANT_DEFINED_EVENT,
    ASSISTANT_DELETED_EVENT,
    USER_ACTOR,
    assistant_definitions,
)
from fabulist.api.fake import FakeTurnEngine
from fabulist.assistants.auto import ASSISTANT_FAILED_EVENT, make_assistant_turn_hook
from fabulist.assistants.manager import (
    _CARD_OUTPUT_DIRECTIVE,
    AssistantDefinition,
    render_assistant_system,
)
from fabulist.assistants.panel import (
    ASSISTANT_CARD_ROLE,
    ASSISTANT_PANEL_EVENT,
    CHAT_ASSISTANT_EVENT,
    latest_assistant_panel,
    latest_chat_assistant,
    parse_assistant_panel,
    render_assistant_panel_statement,
    split_assistant_reply,
)
from fabulist.log import EventLog
from fabulist.log.schema_gate import ASSISTANT_REPLY_EVENT
from fabulist.personas.registry import append_persona


def run(coro):
    return asyncio.run(coro)


DEF = AssistantDefinition(
    id="segfault",
    name="SEGFAULT",
    persona="A hacked-in assistant that monitors for CRITICAL ERRORS.",
    style_notes="Fun facts and emoji, always at the wrong moment.",
)

#: The canonical card, exactly as the output directive's example renders it.
CANONICAL = (
    'asst = AssistantCard("", "Sam", "Guide", "🙂", "One line on the turn.", '
    '[{text: "I head for the door", tag: "leave", note: "momentum", emphasis: true}, '
    '{text: "I ask what they meant", tag: "ask", note: "clarity", emphasis: false}, '
    '{text: "I wait and see", tag: "wait", note: "low risk", emphasis: false}], "Later.")'
)


# -- fakes -------------------------------------------------------------------


class FakeCaller:
    """A ``ModelCaller`` yielding content chunks, optionally raising."""

    def __init__(self, chunks=("prose line.\n\n" + CANONICAL,), error: Exception | None = None):
        self.chunks = list(chunks)
        self.error = error
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield {"type": "content", "delta": chunk}


class FakeFactory:
    """A ``model_caller_factory`` recording the agent tiers it was called with."""

    def __init__(self, caller: FakeCaller):
        self.caller = caller
        self.agents: list[str] = []

    def __call__(self, agent: str) -> FakeCaller:
        self.agents.append(agent)
        return self.caller


# -- seeding helpers ---------------------------------------------------------


def _seed_definition(log: EventLog, definition: AssistantDefinition = DEF) -> None:
    log.append_event(
        ASSISTANTS_SESSION_ID,
        "system",
        "user",
        {
            "event": ASSISTANTS_EVENT,
            "definitions": [
                {
                    "id": definition.id,
                    "name": definition.name,
                    "persona": definition.persona,
                    "style_notes": definition.style_notes,
                    "agent": definition.agent,
                }
            ],
        },
    )


def _enable(log: EventLog, session_id: str, assistant_id: str | None) -> None:
    log.append_event(
        session_id,
        "system",
        "user",
        {"event": CHAT_ASSISTANT_EVENT, "assistant_id": assistant_id},
    )


def _seed_panel(log: EventLog, session_id: str, statement: str) -> None:
    log.append_event(
        session_id,
        "system",
        "assistant",
        {"event": ASSISTANT_PANEL_EVENT, "assistant": "segfault", "statement": statement},
    )


# ==========================================================================
# Parser
# ==========================================================================


def test_render_assistant_system_appends_directive_verbatim():
    system = render_assistant_system(DEF)
    assert system.endswith(_CARD_OUTPUT_DIRECTIVE)
    assert "## Card output" in system
    assert "You are SEGFAULT" in system


def test_parse_happy_path_applies_definition_overrides():
    card = parse_assistant_panel("Some in-voice prose.\n\n" + CANONICAL, DEF)
    assert card is not None
    # name comes from the definition, role is the fixed subtitle — the model's
    # placeholder "Sam"/"Guide" is deliberately discarded.
    assert card.name == "SEGFAULT"
    assert card.role == ASSISTANT_CARD_ROLE
    assert card.avatar == ""
    assert card.mood == "🙂"
    assert card.commentary == "One line on the turn."
    assert card.footer == "Later."
    assert [option["tag"] for option in card.options] == ["leave", "ask", "wait"]
    assert [option["emphasis"] for option in card.options] == [True, False, False]
    assert card.options[0]["note"] == "momentum"


def test_parse_fenced_statement_is_lifted():
    reply = "prose\n```openui\n" + CANONICAL + "\n```\ntrailing note\n"
    card = parse_assistant_panel(reply, DEF)
    assert card is not None and card.name == "SEGFAULT"


def test_parse_duplicate_statement_last_wins():
    first = CANONICAL.replace('"Later."', '"FIRST."')
    second = CANONICAL.replace('"Later."', '"SECOND."')
    card = parse_assistant_panel(first + "\n" + second, DEF)
    assert card is not None and card.footer == "SECOND."


def test_parse_kwarg_form_returns_none():
    # Named args are unsupported by the positional contract — no coercion.
    kwarg = 'asst = AssistantCard(avatar: "", name: "Sam", role: "Guide", mood: "🙂", commentary: "x", options: [])'
    assert parse_assistant_panel(kwarg, DEF) is None


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        "",
        "   ",
        "just prose, no statement",
        'root = ScenePane("title", [])',
        'asst = AssistantCard("", "n", "r")',  # too few args
        'asst = AssistantCard("", "n", "r", "m",',  # unterminated
        'asst = AssistantCard(avatar: "", name: "n")',  # kwarg form
    ],
)
def test_parse_garbage_returns_none(garbage):
    assert parse_assistant_panel(garbage, DEF) is None


def test_parse_footer_is_optional():
    card = parse_assistant_panel('asst = AssistantCard("", "n", "r", "m", "c", [])', DEF)
    assert card is not None and card.options == [] and card.footer == ""


def test_parse_absent_options_is_empty_list():
    card = parse_assistant_panel('asst = AssistantCard("", "n", "r", "🙂", "c")', DEF)
    assert card is not None and card.options == []
    assert card.footer == ""


def test_parse_garbage_options_degrades_to_empty_list():
    card = parse_assistant_panel(
        'asst = AssistantCard("", "n", "r", "🙂", "c", "not-a-list", "f")', DEF
    )
    assert card is not None and card.options == []
    assert card.footer == "f"


def test_parse_empty_avatar_is_preserved():
    card = parse_assistant_panel(
        'asst = AssistantCard("", "n", "r", "🙂", "c", [], "f")', DEF
    )
    assert card is not None and card.avatar == ""


# -- avatar normalization ----------------------------------------------------
# Live drift: the model fills the positional avatar slot with its NAME
# (``AssistantCard("The Oracle", ...)``), which makes the frontend request the
# name as a URL (404 + broken image). Only an addressable value survives.


def test_avatar_normalization_table():
    cases = [
        # emitted avatar -> expected avatar on the canonical card
        ("The Oracle", ""),  # name drift -> initials fallback
        ("Dr. Smith", ""),  # a dot that is not an extension stays a name
        ("  ", ""),  # whitespace -> blank
        ("Oracle", ""),  # bare word -> blank
        ("https://cdn.example/av.png", "https://cdn.example/av.png"),  # URL kept
        ("http://localhost:4444/avatar", "http://localhost:4444/avatar"),
        ("data:image/png;base64,AAAA", "data:image/png;base64,AAAA"),  # data URI kept
        ("/static/avatars/oracle.png", "/static/avatars/oracle.png"),  # path kept
        ("avatars/oracle", "avatars/oracle"),  # path segment kept (no extension)
        ("oracle.png", "oracle.png"),  # bare filename kept
    ]
    for emitted, expected in cases:
        card = parse_assistant_panel(
            f'asst = AssistantCard("{emitted}", "N", "r", "🙂", "c", [], "f")', DEF
        )
        assert card is not None, emitted
        assert card.avatar == expected, emitted
        # The normalization is carried into the emitted statement too.
        statement = card.to_statement()
        assert parse_assistant_panel(statement, DEF).avatar == expected, emitted


def test_avatar_normalization_kept_url_survives_roundtrip():
    url = "https://cdn.example/a.png?v=2"
    statement = f'asst = AssistantCard("{url}", "n", "r", "m", "c", [], "f")'
    card = parse_assistant_panel(statement, DEF)
    assert card is not None and card.avatar == url
    assert f'"{url}"' in render_assistant_panel_statement(card)


def test_reserialized_statement_reparses_with_corrected_name():
    card = parse_assistant_panel(CANONICAL, DEF)
    assert card is not None
    statement = render_assistant_panel_statement(card)
    assert statement.startswith("asst = AssistantCard(")
    roundtripped = parse_assistant_panel(statement, DEF)
    assert roundtripped is not None
    assert roundtripped.name == "SEGFAULT"
    assert roundtripped.role == ASSISTANT_CARD_ROLE
    assert roundtripped.options[0]["emphasis"] is True


# ==========================================================================
# Prose / statement splitting
# ==========================================================================


def test_split_prose_and_statement():
    prose, statement = split_assistant_reply("Some in-voice prose.\n\n" + CANONICAL)
    assert prose == "Some in-voice prose."
    assert statement is not None and statement.startswith("asst = AssistantCard(")
    assert "AssistantCard(" not in prose


def test_split_fenced_statement_strips_fences_and_leaves_prose():
    reply = "prose\n```openui\n" + CANONICAL + "\n```\ntrailing note\n"
    prose, statement = split_assistant_reply(reply)
    assert prose == "prose\n\ntrailing note"
    assert statement is not None and "```" not in statement
    assert "AssistantCard(" not in prose


def test_split_no_statement_returns_reply_unchanged():
    reply = "just prose, no statement"
    assert split_assistant_reply(reply) == (reply, None)


def test_split_statement_only_yields_empty_prose():
    prose, statement = split_assistant_reply(CANONICAL)
    assert prose == ""
    assert statement == CANONICAL


def test_split_trailing_dangling_fragment_is_dropped():
    for reply in ("Some prose.\n\nasst =", "Some prose.\n\nasst = AssistantCard("):
        prose, statement = split_assistant_reply(reply)
        assert prose == "Some prose."
        assert statement is None


def test_split_collapses_blank_lines_left_behind():
    reply = "one\n\n\n\n" + CANONICAL + "\n\n\n\ntwo"
    prose, _ = split_assistant_reply(reply)
    assert prose == "one\n\ntwo"


# ==========================================================================
# Fork-aware log folds
# ==========================================================================


def test_latest_panel_and_enablement_fork_aware(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        stmt_early = 'asst = AssistantCard("", "E", "assistant", "🙂", "early", [], "e")'
        stmt_child = 'asst = AssistantCard("", "C", "assistant", "🙂", "child", [], "c")'
        stmt_parent_late = 'asst = AssistantCard("", "P", "assistant", "🙂", "late", [], "p")'

        log.append_event("s1", "user", "user", {"text": "hi"})
        _seed_panel(log, "s1", stmt_early)
        _enable(log, "s1", "segfault")
        log.fork_session("s1", at_seq=3, new_session_id="s1b")

        # The branch inherits the parent's panel + enablement up to the fork.
        assert latest_assistant_panel(log, "s1b") == stmt_early
        assert latest_chat_assistant(log, "s1b") == "segfault"

        # A later parent write must not leak into the branch.
        _seed_panel(log, "s1", stmt_parent_late)
        _enable(log, "s1", None)
        assert latest_assistant_panel(log, "s1b") == stmt_early
        assert latest_chat_assistant(log, "s1b") == "segfault"
        assert latest_assistant_panel(log, "s1") == stmt_parent_late
        assert latest_chat_assistant(log, "s1") is None

        # The branch's own write is latest for the branch only.
        _seed_panel(log, "s1b", stmt_child)
        assert latest_assistant_panel(log, "s1b") == stmt_child
        assert latest_assistant_panel(log, "s1") == stmt_parent_late
    finally:
        log.close()


def test_latest_enablement_absent_is_none(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "user", "user", {"text": "hi"})
        assert latest_chat_assistant(log, "s1") is None
        assert latest_assistant_panel(log, "s1") is None
    finally:
        log.close()


# ==========================================================================
# Per-turn hook
# ==========================================================================


def test_hook_disabled_makes_no_model_call(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        _seed_definition(log)
        log.append_event("s1", "user", "user", {"text": "hi"})  # never enabled
        caller = FakeCaller()
        factory = FakeFactory(caller)
        hook = make_assistant_turn_hook(log, factory)

        run(hook("s1"))

        assert factory.agents == []  # no model call
        assert caller.calls == []
        kinds = [e.payload.get("event") for e in log.get_events("s1")]
        assert ASSISTANT_REPLY_EVENT not in kinds
        assert ASSISTANT_PANEL_EVENT not in kinds
    finally:
        log.close()


def test_hook_enabled_appends_reply_and_panel(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        _seed_definition(log)
        log.append_event("s1", "user", "user", {"text": "what now?"})
        _enable(log, "s1", "segfault")
        caller = FakeCaller()
        factory = FakeFactory(caller)
        hook = make_assistant_turn_hook(log, factory)

        run(hook("s1"))

        assert factory.agents == ["narrator"]  # the definition's agent tier
        # Context built from the log (facts + recent prose + latest user message).
        (messages,) = caller.calls
        assert messages[0]["role"] == "system" and "SEGFAULT" in messages[0]["content"]
        assert "what now?" in messages[1]["content"]

        events = [e for e in log.get_events("s1") if e.type == "system"]
        reply_events = [e for e in events if e.payload.get("event") == ASSISTANT_REPLY_EVENT]
        panel_events = [e for e in events if e.payload.get("event") == ASSISTANT_PANEL_EVENT]
        assert len(reply_events) == 1 and len(panel_events) == 1

        reply = reply_events[0]
        assert reply.actor == "assistant"
        assert reply.payload["assistant"] == "segfault"
        assert reply.payload["agent"] == "narrator"
        # The logged text is PROSE ONLY: the DSL statement never rides along.
        assert reply.payload["text"] == "prose line."
        assert "AssistantCard(" not in reply.payload["text"]

        # The panel statement is the corrected one (real name, fixed role).
        statement = panel_events[0].payload["statement"]
        assert statement.startswith("asst = AssistantCard(")
        parsed = parse_assistant_panel(statement, DEF)
        assert parsed is not None and parsed.name == "SEGFAULT"
        assert "SEGFAULT" in statement and '"Guide"' not in statement
        assert latest_assistant_panel(log, "s1") == statement
    finally:
        log.close()


def test_hook_logs_prose_only_without_raw_dsl(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        _seed_definition(log)
        log.append_event("s1", "user", "user", {"text": "hi"})
        _enable(log, "s1", "segfault")
        # Default chunks end in the raw ``asst = AssistantCard(...)`` statement.
        hook = make_assistant_turn_hook(log, FakeFactory(FakeCaller()))

        run(hook("s1"))

        events = [e for e in log.get_events("s1") if e.type == "system"]
        replies = [e for e in events if e.payload.get("event") == ASSISTANT_REPLY_EVENT]
        panels = [e for e in events if e.payload.get("event") == ASSISTANT_PANEL_EVENT]
        assert len(replies) == 1 and len(panels) == 1
        # Clean prose, no raw DSL anywhere on the logged reply text.
        assert replies[0].payload["text"].strip() == "prose line."
        assert "AssistantCard(" not in replies[0].payload["text"]
        # The panel event still carries the (definition-corrected) statement.
        assert panels[0].payload["statement"].startswith("asst = AssistantCard(")
        assert "SEGFAULT" in panels[0].payload["statement"]
    finally:
        log.close()


def test_hook_panel_event_blanks_name_avatar(tmp_path: Path):
    """The live drift: the model's NAME in the avatar slot never reaches the log."""
    log = EventLog(tmp_path / "events.db")
    try:
        _seed_definition(log)
        log.append_event("s1", "user", "user", {"text": "hi"})
        _enable(log, "s1", "segfault")
        drifted = CANONICAL.replace('AssistantCard(""', 'AssistantCard("The Oracle"')
        hook = make_assistant_turn_hook(log, FakeFactory(FakeCaller(chunks=(drifted,))))

        run(hook("s1"))

        panels = [
            e
            for e in log.get_events("s1")
            if e.type == "system" and e.payload.get("event") == ASSISTANT_PANEL_EVENT
        ]
        assert len(panels) == 1
        assert '"The Oracle"' not in panels[0].payload["statement"]
        assert panels[0].payload["statement"].startswith('asst = AssistantCard("", ')
    finally:
        log.close()


def test_hook_model_failure_is_non_fatal(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        _seed_definition(log)
        log.append_event("s1", "user", "user", {"text": "hi"})
        _enable(log, "s1", "segfault")
        caller = FakeCaller(error=RuntimeError("model down"))
        factory = FakeFactory(caller)
        hook = make_assistant_turn_hook(log, factory)

        run(hook("s1"))  # must not raise

        events = [e for e in log.get_events("s1") if e.type == "system"]
        kinds = [e.payload.get("event") for e in events]
        assert ASSISTANT_FAILED_EVENT in kinds
        assert ASSISTANT_REPLY_EVENT not in kinds
        assert ASSISTANT_PANEL_EVENT not in kinds
    finally:
        log.close()


def test_hook_reply_without_card_appends_reply_only(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        _seed_definition(log)
        log.append_event("s1", "user", "user", {"text": "hi"})
        _enable(log, "s1", "segfault")
        caller = FakeCaller(chunks=("Just prose, no card statement.",))
        hook = make_assistant_turn_hook(log, FakeFactory(caller))

        run(hook("s1"))

        kinds = [e.payload.get("event") for e in log.get_events("s1") if e.type == "system"]
        assert ASSISTANT_REPLY_EVENT in kinds
        assert ASSISTANT_PANEL_EVENT not in kinds
    finally:
        log.close()


def test_hook_enabled_id_without_definition_appends_failed(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "user", "user", {"text": "hi"})
        _enable(log, "s1", "ghost")  # enabled but no such definition
        caller = FakeCaller()
        hook = make_assistant_turn_hook(log, FakeFactory(caller))

        run(hook("s1"))

        kinds = [e.payload.get("event") for e in log.get_events("s1") if e.type == "system"]
        assert ASSISTANT_FAILED_EVENT in kinds
        assert ASSISTANT_PANEL_EVENT not in kinds
    finally:
        log.close()


def test_hook_persona_id_used_directly_as_assistant(tmp_path: Path):
    """Bug 4: a persona id in the enabled slot runs as the assistant (no clone)."""
    log = EventLog(tmp_path / "events.db")
    try:
        persona_id = append_persona(
            log,
            {
                "name": "SEGFAULT",
                "description": "A hacked-in assistant monitoring for CRITICAL ERRORS.",
            },
        )
        log.append_event("s1", "user", "user", {"text": "what now?"})
        _enable(log, "s1", persona_id)  # a PERSONA id, not an assistant id
        caller = FakeCaller()
        factory = FakeFactory(caller)
        hook = make_assistant_turn_hook(log, factory)

        run(hook("s1"))

        assert factory.agents == ["narrator"]  # synthesized default tier
        (messages,) = caller.calls
        # The persona's own text is the system block; nothing was cloned.
        assert messages[0]["role"] == "system"
        assert "SEGFAULT" in messages[0]["content"]
        assert "CRITICAL ERRORS" in messages[0]["content"]

        events = [e for e in log.get_events("s1") if e.type == "system"]
        replies = [e for e in events if e.payload.get("event") == ASSISTANT_REPLY_EVENT]
        panels = [e for e in events if e.payload.get("event") == ASSISTANT_PANEL_EVENT]
        assert len(replies) == 1 and len(panels) == 1
        assert replies[0].payload["assistant"] == persona_id
        assert ASSISTANT_FAILED_EVENT not in [e.payload.get("event") for e in events]
        # The card's name is corrected from the synthesized persona definition.
        assert "SEGFAULT" in panels[0].payload["statement"]
    finally:
        log.close()


# ==========================================================================
# HTTP routes
# ==========================================================================


@pytest.fixture
def app(tmp_path: Path):
    application = create_app(FakeTurnEngine(), tmp_path / "events.db")
    yield application
    application.state.log.close()


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    )


def test_enablement_roundtrip(app):
    log = app.state.log
    _seed_definition(log)
    log.append_event("s1", "system", "user", {"title": "Chat", "event": "session_created"})

    async def scenario():
        async with client_for(app) as client:
            before = await client.get("/api/sessions/s1/assistant")
            on = await client.patch(
                "/api/sessions/s1/assistant", json={"assistant_id": "segfault"}
            )
            after_on = await client.get("/api/sessions/s1/assistant")
            off = await client.patch(
                "/api/sessions/s1/assistant", json={"assistant_id": None}
            )
            after_off = await client.get("/api/sessions/s1/assistant")
            return before, on, after_on, off, after_off

    before, on, after_on, off, after_off = run(scenario())
    assert before.status_code == 200 and before.json() == {"assistant_id": None}
    assert on.status_code == 200 and on.json() == {"assistant_id": "segfault"}
    assert after_on.json() == {"assistant_id": "segfault"}
    assert off.status_code == 200 and off.json() == {"assistant_id": None}
    assert after_off.json() == {"assistant_id": None}

    rows = [
        e
        for e in log.get_events("s1")
        if e.type == "system" and e.payload.get("event") == CHAT_ASSISTANT_EVENT
    ]
    assert [r.payload["assistant_id"] for r in rows] == ["segfault", None]
    assert all(r.actor == "user" for r in rows)


def test_enablement_unknown_id_is_404(app):
    log = app.state.log
    _seed_definition(log)
    log.append_event("s1", "system", "user", {"title": "Chat", "event": "session_created"})

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/s1/assistant", json={"assistant_id": "ghost"}
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]
    # Nothing appended on a rejected write.
    assert not [
        e
        for e in log.get_events("s1")
        if e.type == "system" and e.payload.get("event") == CHAT_ASSISTANT_EVENT
    ]


def test_enablement_allows_demo_placeholder_ids(app):
    log = app.state.log
    log.append_event("s1", "system", "user", {"title": "Chat", "event": "session_created"})

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/s1/assistant", json={"assistant_id": "demo_oracle"}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"assistant_id": "demo_oracle"}


def test_enablement_allows_persona_id(app):
    """Bug 4: the enabled slot accepts a persona id (used directly as assistant)."""
    log = app.state.log
    persona_id = append_persona(log, {"name": "Elara", "description": "A flower girl."})
    log.append_event("s1", "system", "user", {"title": "Chat", "event": "session_created"})

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/s1/assistant", json={"assistant_id": persona_id}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"assistant_id": persona_id}
    rows = [
        e
        for e in log.get_events("s1")
        if e.type == "system" and e.payload.get("event") == CHAT_ASSISTANT_EVENT
    ]
    assert [r.payload["assistant_id"] for r in rows] == [persona_id]


def test_consult_route_accepts_persona_id(app):
    """Bug 4: ``POST .../consult`` resolves a persona id through the same seam."""
    log = app.state.log
    persona_id = append_persona(
        log, {"name": "SEGFAULT", "description": "Hacked-in assistant."}
    )
    log.append_event("s1", "system", "user", {"title": "Chat", "event": "session_created"})
    app.state.assistant_model_caller_factory = FakeFactory(FakeCaller())

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                f"/api/sessions/s1/assistants/{persona_id}/consult",
                json={"user_msg": "hello"},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["assistant"] == persona_id


def test_enablement_missing_key_is_422(app):
    log = app.state.log
    _seed_definition(log)
    log.append_event("s1", "system", "user", {"title": "Chat", "event": "session_created"})

    async def scenario():
        async with client_for(app) as client:
            return await client.patch("/api/sessions/s1/assistant", json={})

    assert run(scenario()).status_code == 422


def test_enablement_unknown_session_is_404(app):
    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/ghost/assistant")

    assert run(scenario()).status_code == 404


def test_panel_endpoint_null_before_first_consult(app):
    log = app.state.log
    log.append_event("s1", "system", "user", {"title": "Chat", "event": "session_created"})

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/assistant-panel")

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"statement": None}


def test_panel_endpoint_latest_wins(app):
    log = app.state.log
    stmt1 = 'asst = AssistantCard("", "One", "assistant", "🙂", "first", [], "1")'
    stmt2 = 'asst = AssistantCard("", "Two", "assistant", "🙂", "second", [], "2")'
    _seed_panel(log, "s1", stmt1)
    _seed_panel(log, "s1", stmt2)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/assistant-panel")

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"statement": stmt2}


def test_panel_endpoint_fork_aware(app):
    log = app.state.log
    stmt_early = 'asst = AssistantCard("", "E", "assistant", "🙂", "early", [], "e")'
    stmt_parent_late = 'asst = AssistantCard("", "P", "assistant", "🙂", "late", [], "p")'
    stmt_child = 'asst = AssistantCard("", "C", "assistant", "🙂", "child", [], "c")'

    log.append_event("s1", "user", "user", {"text": "hi"})
    _seed_panel(log, "s1", stmt_early)
    log.fork_session("s1", at_seq=2, new_session_id="s1b")
    _seed_panel(log, "s1", stmt_parent_late)
    _seed_panel(log, "s1b", stmt_child)

    async def scenario():
        async with client_for(app) as client:
            parent = await client.get("/api/sessions/s1/assistant-panel")
            child = await client.get("/api/sessions/s1b/assistant-panel")
            return parent, child

    parent, child = run(scenario())
    assert parent.json() == {"statement": stmt_parent_late}
    assert child.json() == {"statement": stmt_child}


def test_panel_endpoint_unknown_session_is_404(app):
    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/ghost/assistant-panel")

    assert run(scenario()).status_code == 404


# ==========================================================================
# D47 — actor reference + definition CRUD (create/update/tombstone)
# ==========================================================================


def test_directive_names_elements_and_option_range():
    """D47/D48: the directive names each card element and allows 3-6 options."""
    system = render_assistant_system(DEF)
    assert system.endswith(_CARD_OUTPUT_DIRECTIVE)
    assert "You drive the AssistantCard surface" in _CARD_OUTPUT_DIRECTIVE
    # Each positional element is named with how to populate it (D47), in order.
    for index, element in enumerate(
        ("avatar", "name", "role", "mood", "commentary", "options", "footer"), 1
    ):
        assert f"{index}. {element}" in _CARD_OUTPUT_DIRECTIVE
    # Flexible option count (D48): three to six, the model's choice.
    assert "three to six" in _CARD_OUTPUT_DIRECTIVE
    assert "three or four" not in _CARD_OUTPUT_DIRECTIVE
    # Positive-only (D38(d)): the example is the single canonical statement.
    assert _CARD_OUTPUT_DIRECTIVE.count("asst = AssistantCard(") == 1


def test_definition_actor_roundtrips_through_catalog(app):
    log = app.state.log
    log.append_event(
        ASSISTANTS_SESSION_ID,
        "system",
        "user",
        {
            "event": ASSISTANTS_EVENT,
            "definitions": [
                {
                    "id": "segfault",
                    "name": "SEGFAULT",
                    "persona": "A hacked-in assistant.",
                    "actor": "elara",
                }
            ],
        },
    )

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/assistants")).json()

    body = run(scenario())
    assert body["meta"]["source"] == "log"
    assert body["assistants"][0]["actor"] == "elara"
    # The folded definition carries the same reference (no default invented).
    assert assistant_definitions(log)[0].actor == "elara"


def test_legacy_definition_without_actor_still_loads(app):
    log = app.state.log
    _seed_definition(log)  # the legacy payload carries no actor key

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/assistants")).json()

    body = run(scenario())
    assert body["meta"]["source"] == "log"
    assert body["assistants"][0]["id"] == "segfault"
    assert body["assistants"][0]["actor"] == ""
    assert assistant_definitions(log)[0].actor == ""


def test_definition_crud_latest_wins_and_tombstone(app):
    log = app.state.log

    async def scenario():
        async with client_for(app) as client:
            # Create (id minted) replaces the demo placeholders.
            created = await client.post(
                "/api/assistants",
                json={"name": "Muse", "persona": "A quiet muse.", "actor": "elara"},
            )
            muse_id = created.json()["assistant"]["id"]
            # Create with an explicit id upserts that row.
            await client.post(
                "/api/assistants",
                json={"id": "gremlin", "name": "Gremlin", "persona": "Gleeful."},
            )
            catalog_before = (await client.get("/api/assistants")).json()
            # Update is a latest-wins append, not a row mutation.
            updated = await client.put(
                f"/api/assistants/{muse_id}",
                json={"name": "Muse II", "persona": "Quieter.", "actor": "elara"},
            )
            catalog_after_update = (await client.get("/api/assistants")).json()
            # Delete is a tombstone event; the row survives, the fold drops it.
            deleted = await client.delete("/api/assistants/gremlin")
            catalog_after_delete = (await client.get("/api/assistants")).json()
            # Unknown ids reject with 404 and append nothing.
            missing_put = await client.put(
                "/api/assistants/ghost", json={"name": "X", "persona": "p"}
            )
            missing_delete = await client.delete("/api/assistants/ghost")
            return (
                created,
                muse_id,
                catalog_before,
                updated,
                catalog_after_update,
                deleted,
                catalog_after_delete,
                missing_put,
                missing_delete,
            )

    (
        created,
        muse_id,
        catalog_before,
        updated,
        catalog_after_update,
        deleted,
        catalog_after_delete,
        missing_put,
        missing_delete,
    ) = run(scenario())

    assert created.status_code == 200
    assert created.json()["assistant"]["actor"] == "elara"
    assert catalog_before["meta"]["source"] == "log"
    assert catalog_before["meta"]["demo"] is False
    assert sorted(a["id"] for a in catalog_before["assistants"]) == sorted(
        [muse_id, "gremlin"]
    )

    assert updated.status_code == 200 and updated.json()["assistant"]["name"] == "Muse II"
    renamed = next(a for a in catalog_after_update["assistants"] if a["id"] == muse_id)
    assert renamed["name"] == "Muse II"
    assert len(catalog_after_update["assistants"]) == 2  # update, not add

    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    remaining = [a["id"] for a in catalog_after_delete["assistants"]]
    assert remaining == [muse_id]  # gremlin omitted from the catalog

    assert missing_put.status_code == 404 and missing_delete.status_code == 404

    # The log is append-only: per-id writes + tombstone, never a row mutation.
    kinds = [
        e.payload.get("event")
        for e in log.get_events(ASSISTANTS_SESSION_ID)
        if e.type == "system"
    ]
    assert kinds.count(ASSISTANT_DEFINED_EVENT) == 3  # muse, gremlin, muse II
    assert kinds.count(ASSISTANT_DELETED_EVENT) == 1
    assert all(
        e.actor == USER_ACTOR
        for e in log.get_events(ASSISTANTS_SESSION_ID)
        if e.type == "system"
    )


def test_deleting_last_definition_leaves_empty_catalog(app):
    """A tombstoned-to-empty roster does not fall back to the demos."""
    log = app.state.log

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/assistants", json={"id": "solo", "name": "Solo", "persona": "p"}
            )
            await client.delete("/api/assistants/solo")
            return created, (await client.get("/api/assistants")).json()

    created, catalog = run(scenario())
    assert created.status_code == 200
    assert catalog["meta"]["source"] == "log"
    assert catalog["assistants"] == []
    assert [
        e.payload["event"]
        for e in log.get_events(ASSISTANTS_SESSION_ID)
        if e.type == "system"
    ] == [ASSISTANT_DEFINED_EVENT, ASSISTANT_DELETED_EVENT]
