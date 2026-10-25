"""The Anti Injection toggle (issue #39).

Owner ruling (2026-10-24, the issue comment): the trailing-system
``<preset_runtime>`` CARRIER is permanent — where and how the tail renders is
untouchable. The toggle removes ONLY the ``## Engine context`` contract framing
(the clause and its firewall semantics) wholesale, in one click:

* ON  — today's prompt byte-for-byte (clause present, ``protected`` = never
  budget-trimmed);
* OFF (the shipped default) — the clause is absent from the assembled prompt on
  BOTH prompt paths (the code catalog AND the preset/factory-artifact directive
  rows), the tagged ``<preset_runtime>`` tail still renders as a system message
  after the player's, and the diff between the two states is EXACTLY the clause.

The content home never moves: the clause text stays in ``parts.py`` and in the
committed ``prompts/factory-defaults.json`` directive row; the product setting
gates RENDERING only (content home vs runtime gate).

Everything here runs against real tmp-path event logs and the real builder /
compiler seams — no duck-typed stand-ins.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.settings import FEATURE_FLAGS, SETTINGS_SESSION_ID, router
from fabulist.log import EventLog
from fabulist.prompts.compiler import compile as compile_turn, render_system_prompt
from fabulist.prompts.parts import (
    ENGINE_CONTEXT_BODY,
    ENGINE_CONTEXT_CLAUSE,
    ENGINE_CONTEXT_DIRECTIVE_ID,
)
from fabulist.prompts.preset_store import (
    active_preset,
    ensure_default_preset,
    preset_directives,
)
from fabulist.prompts.wrapper import RUNTIME_OPEN
from fabulist.state.settings import (
    ANTI_INJECTION_DEFAULT,
    ANTI_INJECTION_FLAG,
    SETTINGS_EVENT,
    anti_injection_enabled,
)
from fabulist.wiring import make_narrator_message_builder, narrator_system_prompt

FACTS = "time: 09:00 | loc: the dock | weather: light rain"


def _turn_state(sid: str) -> dict:
    return {
        "session_id": sid,
        "user_content": "look around",
        "kind": "in_character",
        "facts": FACTS,
        "recall": "",
        "rolls": "",
    }


def _session(log: EventLog, sid: str = "ai39") -> str:
    """A session with one user row (the builder expects the turn's own row)."""
    log.append_event(sid, "user", "user", {"text": "look around"})
    return sid


def _set_anti_injection(log: EventLog, enabled: bool) -> None:
    """Persist the setting the way the settings door does (one log event)."""
    log.append_event(
        SETTINGS_SESSION_ID,
        "system",
        "user",
        {"event": SETTINGS_EVENT, "features": {ANTI_INJECTION_FLAG: enabled}},
    )


def _system_prefix(messages: list[dict]) -> str:
    return str(messages[0].get("content", ""))


# ---------------------------------------------------------------------------
# 1. the shipped default is OFF
# ---------------------------------------------------------------------------


def test_default_is_off_on_a_fresh_log(log: EventLog):
    assert ANTI_INJECTION_DEFAULT is False
    assert anti_injection_enabled(log) is False
    # No settings event was written just by reading.
    assert log.get_events(SETTINGS_SESSION_ID) == []


def test_settings_surface_names_the_flag_anti_injection():
    # The settings surface label is the issue's name; the wire id is stable.
    assert FEATURE_FLAGS[ANTI_INJECTION_FLAG] == "Anti Injection"


# ---------------------------------------------------------------------------
# 2. prompt build: OFF omits the clause, ON is today's prompt, delta = clause
# ---------------------------------------------------------------------------


def test_code_catalog_path_off_omits_the_clause_and_keeps_the_carrier(
    log: EventLog,
):
    """Path 1: no persisted preset -> the compiler renders the CODE catalog."""
    sid = _session(log)
    build = make_narrator_message_builder(log)  # default OFF
    messages = build(_turn_state(sid))

    prefix = _system_prefix(messages)
    assert "## Engine context" not in prefix
    assert ENGINE_CONTEXT_BODY not in prefix
    # The prompt still renders the rest of the definition (same framing lead-in).
    assert "You are the Content Engine of a live, single-user roleplay." in prefix
    assert "## The Content Engine" in prefix

    # The ENVELOPE is untouched (issue #44: the injection is NOT gated): it is
    # still the ONE ephemeral assistant message immediately before the ask.
    assert messages[-1]["role"] == "user"
    assert messages[-2]["role"] == "assistant"
    assert RUNTIME_OPEN in str(messages[-2]["content"])


def test_on_and_off_differ_exactly_by_the_clause(log: EventLog):
    sid = _session(log)
    build = make_narrator_message_builder(log)

    off = build(_turn_state(sid))  # shipped default
    _set_anti_injection(log, True)
    on = build(_turn_state(sid))

    on_prefix, off_prefix = _system_prefix(on), _system_prefix(off)
    # ON = today's prompt: the whole inclause clause, heading + body, once.
    assert on_prefix.count(ENGINE_CONTEXT_CLAUSE) == 1
    # OFF = ON with exactly that clause block removed (its own leading
    # separator included): nothing else moves.
    assert on_prefix.replace("\n\n" + ENGINE_CONTEXT_CLAUSE, "", 1) == off_prefix
    # And the clause is the ONLY difference in the whole message list.
    assert on[1:] == off[1:]
    assert len(on) == len(off)
    # The envelope is byte-identical across the two states.
    assert RUNTIME_OPEN in str(on[-2]["content"])
    assert RUNTIME_OPEN in str(off[-2]["content"])
    assert on[-2] == off[-2]


def test_artifact_row_path_is_gated_too(log: EventLog):
    """Path 2: the seeded default preset carries the clause ROW (content home).

    The build renders from those rows — so the gate has to suppress the ROW
    read, not just the code catalog. The row itself is never deleted.
    """
    sid = _session(log)
    ensure_default_preset(log)
    rows = preset_directives(active_preset(log))
    assert rows is not None  # the preset carries its own rows
    row = next(r for r in rows if r.id == ENGINE_CONTEXT_DIRECTIVE_ID)
    # Content home intact: the artifact row still carries the clause verbatim,
    # enabled — the shipped default is OURS (the setting), not the row's.
    assert row.content == ENGINE_CONTEXT_BODY
    assert row.enabled is True

    build = make_narrator_message_builder(log)
    off = build(_turn_state(sid))
    assert "## Engine context" not in _system_prefix(off)

    _set_anti_injection(log, True)
    on = build(_turn_state(sid))
    assert "## Engine context" in _system_prefix(on)
    # Same clause, same place: the row text renders verbatim when ON.
    assert ENGINE_CONTEXT_BODY in _system_prefix(on)


def test_per_turn_read_applies_without_a_restart(log: EventLog):
    """ONE builder instance (the engine's) sees the flip on the next build."""
    sid = _session(log)
    build = make_narrator_message_builder(log)

    first = build(_turn_state(sid))
    assert "## Engine context" not in _system_prefix(first)

    _set_anti_injection(log, True)
    second = build(_turn_state(sid))
    assert "## Engine context" in _system_prefix(second)

    _set_anti_injection(log, False)
    third = build(_turn_state(sid))
    assert "## Engine context" not in _system_prefix(third)
    # Flipping back is byte-identical to the first build (no drift accrues).
    assert third == first


def test_both_prompt_paths_agree_when_off(log: EventLog):
    """The catalog path and the artifact-row path render the same OFF prefix."""
    sid = _session(log)
    catalog_off = _system_prefix(make_narrator_message_builder(log)(_turn_state(sid)))
    ensure_default_preset(log)
    row_off = _system_prefix(make_narrator_message_builder(log)(_turn_state(sid)))
    # The preset path carries the library as its own module, so the prefixes
    # differ in the <<library>> region — but neither carries the clause.
    assert "## Engine context" not in catalog_off
    assert "## Engine context" not in row_off
    assert ENGINE_CONTEXT_BODY not in catalog_off
    assert ENGINE_CONTEXT_BODY not in row_off


def test_compiler_default_is_off_and_on_is_opt_in():
    # The pure render seam: default OFF (shipped), ON on request.
    assert "## Engine context" not in render_system_prompt(budget=10**9)
    assert ENGINE_CONTEXT_CLAUSE in render_system_prompt(
        budget=10**9, anti_injection=True
    )
    # compile() threads the same flag (both entry points share one assembly).
    off = compile_turn(None, None, FACTS, [], "hi")
    on = compile_turn(None, None, FACTS, [], "hi", anti_injection=True)
    assert "## Engine context" not in str(off[0]["content"])
    assert ENGINE_CONTEXT_CLAUSE in str(on[0]["content"])
    assert on[1:] == off[1:]


def test_opening_prompt_obeys_the_same_default():
    """The greeting/opening is a built prose prompt: same shipped default."""
    assert "## Engine context" not in narrator_system_prompt()
    assert ENGINE_CONTEXT_CLAUSE in narrator_system_prompt(anti_injection=True)


# ---------------------------------------------------------------------------
# 3. the persisted product setting (settings surface round-trip)
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None):
        app = create_app(engine or FakeTurnEngine(), log_path)
        app.include_router(router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_settings_payload_exposes_the_flag_off_by_default(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/settings")).json()

    body = run(scenario())
    flag = body["features"][ANTI_INJECTION_FLAG]
    assert flag == {"id": ANTI_INJECTION_FLAG, "name": "Anti Injection", "enabled": False}


def test_patch_features_persists_and_survives_a_rebuild(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/settings", json={"features": {ANTI_INJECTION_FLAG: True}}
            )
            assert patched.status_code == 200
            return patched.json()

    body = run(scenario())
    assert body["features"][ANTI_INJECTION_FLAG]["enabled"] is True

    # The write rode ONE settings_changed system event on the reserved anchor.
    log = EventLog(log_path)
    try:
        events = [
            e
            for e in log.get_events(SETTINGS_SESSION_ID)
            if e.type == "system" and e.payload.get("event") == SETTINGS_EVENT
        ]
        assert len(events) == 1
        assert events[0].payload["features"] == {ANTI_INJECTION_FLAG: True}
        # The prompt build's fold sees it (no restart, no side table).
        assert anti_injection_enabled(log) is True
    finally:
        log.close()

    # A fresh app over the same log folds the same value.
    async def read():
        async with client_for(build()) as client:
            return (await client.get("/api/settings")).json()

    assert run(read())["features"][ANTI_INJECTION_FLAG]["enabled"] is True
