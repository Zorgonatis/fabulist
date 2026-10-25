"""W5-S2: the runtime agent-debug toggle + capture chokepoint predicate.

Lane under test: the per-session toggle in :mod:`fabulist.api.chat_config_api`
(folded from ``chat_agent_debug`` system events, default supplied by the boot
flag) and the toggle-gated :class:`fabulist.agents.debug.DebugCapture`
(``enabled`` predicate -> pure pass-through when off).

The chokepoint factory itself lives in ``wiring`` (a prepared patch, applied by
the orchestrator); its predicate test skips until that patch lands, so this file
stays green against the owned modules alone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from fabulist.agents.debug import (
    AGENT_CALL_EVENT,
    CURRENT_SESSION,
    DebugCapture,
    append_call_event,
    calls_for,
)
from fabulist.api import create_app
from fabulist.api.chat_config_api import (
    CHAT_AGENT_DEBUG_EVENT,
    resolve_agent_debug,
    router,
)
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


async def collect(agen: AsyncIterator[dict]) -> list[dict]:
    return [event async for event in agen]


class FakeCaller:
    """Minimal ModelCaller with the RegistryModelCaller event shapes."""

    def __init__(self, events: list[dict], *, agent: str = "narrator") -> None:
        self._events = [dict(event) for event in events]
        self.agent = agent

    async def __call__(
        self, messages: list[dict], stream: bool = True
    ) -> AsyncIterator[dict]:
        for event in self._events:
            yield dict(event)


SCRIPT = [
    {"type": "reasoning", "delta": "think "},
    {"type": "content", "delta": "prose", "meta": {"channel": "prose"}},
]
MESSAGES = [{"role": "user", "content": "hi"}]


# -- DebugCapture toggle predicate (zero-overhead when off) ------------------


def test_capture_off_is_pure_passthrough_and_never_sinks():
    """Predicate false: events forwarded byte-for-byte, sink never called."""
    raw = run(collect(FakeCaller(SCRIPT)(MESSAGES)))
    captured: list[dict] = []
    wrapped = run(
        collect(
            DebugCapture(FakeCaller(SCRIPT), captured.append, enabled=lambda: False)(
                MESSAGES
            )
        )
    )
    assert wrapped == raw == SCRIPT
    assert captured == []


def test_capture_on_sinks_once():
    captured: list[dict] = []
    run(
        collect(
            DebugCapture(FakeCaller(SCRIPT), captured.append, enabled=lambda: True)(
                MESSAGES
            )
        )
    )
    assert len(captured) == 1
    assert captured[0]["event"] == AGENT_CALL_EVENT
    assert captured[0]["response_text"] == "prose"


def test_capture_predicate_failure_is_swallowed_as_off(caplog):
    """A raising predicate is treated as off; the stream still flows intact."""
    def boom() -> bool:
        raise RuntimeError("fold exploded")

    raw = run(collect(FakeCaller(SCRIPT)(MESSAGES)))
    captured: list[dict] = []
    wrapped = run(
        collect(DebugCapture(FakeCaller(SCRIPT), captured.append, enabled=boom)(MESSAGES))
    )
    assert wrapped == raw
    assert captured == []


def test_no_predicate_preserves_historical_always_capture():
    captured: list[dict] = []
    run(collect(DebugCapture(FakeCaller(SCRIPT), captured.append)(MESSAGES)))
    assert len(captured) == 1


def test_capture_row_emission_follows_session_toggle(tmp_path: Path):
    """Rows land only while the session's toggle is on (per real log fold)."""
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "system", "user", {"event": "session_created"})

        def enabled() -> bool:
            return resolve_agent_debug(log, CURRENT_SESSION.get(""), default=False)

        token = CURRENT_SESSION.set("s1")
        try:
            # OFF (no toggle written; boot default False) -> no rows.
            run(collect(DebugCapture(FakeCaller(SCRIPT), lambda w: append_call_event(log, "s1", w), enabled=enabled)(MESSAGES)))
            assert calls_for(log, "s1") == []

            # Flip the session ON -> the next call emits exactly one row.
            log.append_event(
                "s1", "system", "user",
                {"event": CHAT_AGENT_DEBUG_EVENT, "enabled": True},
            )
            run(collect(DebugCapture(FakeCaller(SCRIPT), lambda w: append_call_event(log, "s1", w), enabled=enabled)(MESSAGES)))
            assert len(calls_for(log, "s1")) == 1
        finally:
            CURRENT_SESSION.reset(token)
    finally:
        log.close()


# -- the resolve helper fold -------------------------------------------------


def test_resolve_absent_returns_default(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "system", "user", {"event": "session_created"})
        assert resolve_agent_debug(log, "s1", default=True) is True
        assert resolve_agent_debug(log, "s1", default=False) is False
    finally:
        log.close()


def test_resolve_blank_session_is_default(tmp_path: Path):
    """No active session on the call path must never invent a capture."""
    log = EventLog(tmp_path / "events.db")
    try:
        assert resolve_agent_debug(log, "", default=True) is True
        assert resolve_agent_debug(log, "", default=False) is False
    finally:
        log.close()


def test_resolve_latest_write_wins_and_ignores_malformed(tmp_path: Path):
    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "system", "user", {"event": "session_created"})
        log.append_event("s1", "system", "user", {"event": CHAT_AGENT_DEBUG_EVENT, "enabled": True})
        # malformed (non-bool) row must not flip the fold
        log.append_event("s1", "system", "user", {"event": CHAT_AGENT_DEBUG_EVENT, "enabled": "yes"})
        assert resolve_agent_debug(log, "s1", default=False) is True
        log.append_event("s1", "system", "user", {"event": CHAT_AGENT_DEBUG_EVENT, "enabled": False})
        assert resolve_agent_debug(log, "s1", default=True) is False
    finally:
        log.close()


# -- the router endpoints ----------------------------------------------------


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build(agent_debug_default: bool | None = None):
        app = create_app(FakeTurnEngine(), log_path)
        app.include_router(router)
        if agent_debug_default is not None:
            app.state.agent_debug_default = agent_debug_default
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _seed(log: EventLog, session_id: str = "s1") -> None:
    log.append_event(session_id, "system", "user", {"event": "session_created"})


def test_defaults_off_without_boot_flag(build_app):
    build, _ = build_app
    app = build()
    _seed(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/s1/agent-debug")

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"session_id": "s1", "enabled": False}


def test_boot_flag_default_inheritance(build_app):
    """``agent_debug_default=True`` makes a never-toggled session read ON."""
    build, _ = build_app
    app = build(agent_debug_default=True)
    _seed(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            before = await client.get("/api/sessions/s1/agent-debug")
            await client.patch("/api/sessions/s1/agent-debug", json={"enabled": False})
            after = await client.get("/api/sessions/s1/agent-debug")
            return before, after

    before, after = run(scenario())
    assert before.json()["enabled"] is True
    assert after.json()["enabled"] is False  # per-session write overrides default


def test_patch_true_persists_one_user_row(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed(log)

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/sessions/s1/agent-debug", json={"enabled": True}
            )
            fetched = await client.get("/api/sessions/s1/agent-debug")
            return patched, fetched

    patched, fetched = run(scenario())
    assert patched.status_code == 200
    assert patched.json() == {"session_id": "s1", "enabled": True}
    assert fetched.json()["enabled"] is True

    rows = [
        e for e in log.get_events("s1")
        if e.payload.get("event") == CHAT_AGENT_DEBUG_EVENT
    ]
    assert len(rows) == 1
    assert rows[0].actor == "user"
    assert rows[0].payload == {"event": CHAT_AGENT_DEBUG_EVENT, "enabled": True}


@pytest.mark.parametrize("verb", ["get", "patch"])
def test_unknown_session_is_404(build_app, verb):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            if verb == "get":
                return await client.get("/api/sessions/ghost/agent-debug")
            return await client.patch(
                "/api/sessions/ghost/agent-debug", json={"enabled": True}
            )

    assert run(scenario()).status_code == 404


@pytest.mark.parametrize("body", [{}, {"enabled": "nope"}, {"enabled": 5}, {"enabled": None}])
def test_non_bool_or_missing_is_422_and_writes_nothing(build_app, body):
    build, _ = build_app
    app = build()
    log = app.state.log
    _seed(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.patch("/api/sessions/s1/agent-debug", json=body)

    assert run(scenario()).status_code == 422
    rows = [
        e for e in log.get_events("s1")
        if e.payload.get("event") == CHAT_AGENT_DEBUG_EVENT
    ]
    assert rows == []


# -- chokepoint factory (prepared wiring patch) ------------------------------


def test_make_agent_caller_predicate_follows_toggle(tmp_path: Path):
    """The wiring chokepoint wraps every caller and gates on the session toggle.

    Skips until the prepared ``wiring.py`` patch is applied (the orchestrator
    applies it before the live run).
    """
    from fabulist import wiring

    make = getattr(wiring, "make_agent_caller", None)
    if make is None:
        pytest.skip("wiring chokepoint not applied yet")

    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "system", "user", {"event": "session_created"})
        caller = make("narrator", registry=object(), log=log)
        assert isinstance(caller, DebugCapture)
        assert caller.enabled is not None

        token = CURRENT_SESSION.set("s1")
        try:
            assert caller.enabled() is False
            log.append_event(
                "s1", "system", "user",
                {"event": CHAT_AGENT_DEBUG_EVENT, "enabled": True},
            )
            assert caller.enabled() is True
        finally:
            CURRENT_SESSION.reset(token)
    finally:
        log.close()


def test_make_agent_caller_emits_rows_only_when_on(monkeypatch, tmp_path: Path):
    """Drive the real factory: off -> no rows, on -> exactly one capture row."""
    from fabulist import wiring

    make = getattr(wiring, "make_agent_caller", None)
    if make is None:
        pytest.skip("wiring chokepoint not applied yet")
    monkeypatch.setattr(wiring, "RegistryModelCaller", lambda *a, **k: FakeCaller(SCRIPT))

    log = EventLog(tmp_path / "events.db")
    try:
        log.append_event("s1", "system", "user", {"event": "session_created"})
        caller = make("narrator", registry=object(), log=log)
        token = CURRENT_SESSION.set("s1")
        try:
            run(collect(caller(MESSAGES)))
            assert calls_for(log, "s1") == []
            log.append_event(
                "s1", "system", "user",
                {"event": CHAT_AGENT_DEBUG_EVENT, "enabled": True},
            )
            run(collect(caller(MESSAGES)))
            rows = calls_for(log, "s1")
            assert len(rows) == 1
            assert rows[0].agent == "narrator"
            assert rows[0].response_text == "prose"
        finally:
            CURRENT_SESSION.reset(token)
    finally:
        log.close()
