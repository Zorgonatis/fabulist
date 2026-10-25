"""Tests for the assistant consult router (W2.5 wiring + UI).

Exercises the real FastAPI app (httpx ASGI transport) with the assistants router
mounted, against a tmp SQLite event log. The log owns everything (D9):
definitions fold the ``__assistants__`` anchor session and consult replies are
appended as ``assistant_reply`` system events. Model callers are fakes injected
through ``app.state.assistant_model_caller_factory`` — no network.

No pytest-asyncio dependency in this repo, so async scenarios run via
``asyncio.run`` inside sync tests (matching ``tests/test_assistants.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.assistants_api import (
    ASSISTANTS_EVENT,
    ASSISTANTS_SESSION_ID,
    ASSISTANT_REPLY_EVENT,
    _caller_factory,
    router,
)
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


# -- fakes -------------------------------------------------------------------


class FakeCaller:
    """A ``ModelCaller``: yields content chunks, recording the messages."""

    def __init__(self, chunks=("SEGFAULT ", "online.")):
        self.chunks = list(chunks)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield {"type": "content", "delta": chunk}


class RecordingFactory:
    """A ``model_caller_factory`` recording agent tiers."""

    def __init__(self, caller: FakeCaller | None = None):
        self.caller = caller or FakeCaller()
        self.agents: list[str] = []

    def __call__(self, agent):
        self.agents.append(agent)
        return self.caller


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built: list = []

    def _build(factory=None, registry=None):
        app = create_app(FakeTurnEngine(), log_path)
        app.include_router(router)
        if factory is not None:
            app.state.assistant_model_caller_factory = factory
        if registry is not None:
            app.state.registry = registry
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create_session(client: httpx.AsyncClient, title: str = "Test") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


# -- catalog -----------------------------------------------------------------


def test_get_returns_demo_placeholders(build_app):
    build, _ = build_app
    app = build(factory=RecordingFactory())

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/assistants")).json()

    body = run(scenario())
    assert body["meta"]["source"] == "builtin-demo"
    assert body["meta"]["demo"] is True
    assert body["meta"]["session"] == ASSISTANTS_SESSION_ID
    assert body["meta"]["count"] == 2
    ids = [s["id"] for s in body["assistants"]]
    assert ids == ["demo_oracle", "demo_gremlin"]
    # Every placeholder is clearly marked demo in both row and persona text.
    assert all(s["demo"] is True for s in body["assistants"])
    assert all("DEMO PERSONA" in s["persona"] for s in body["assistants"])


def test_get_folds_log_definitions_latest_wins(build_app):
    build, _ = build_app
    app = build(factory=RecordingFactory())

    async def scenario():
        async with client_for(app) as client:
            app.state.log.append_event(
                ASSISTANTS_SESSION_ID,
                "system",
                "user",
                {
                    "event": ASSISTANTS_EVENT,
                    "definitions": [
                        {"id": "old", "name": "Old", "persona": "old persona"},
                    ],
                },
            )
            app.state.log.append_event(
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
                            "style_notes": "emoji at the wrong moment",
                            "agent": "narrator",
                            "trigger": "keyword",
                            "keywords": ["segfault", "error"],
                        }
                    ],
                },
            )
            return (await client.get("/api/assistants")).json()

    body = run(scenario())
    assert body["meta"]["source"] == "log"
    assert body["meta"]["demo"] is False
    assert [s["id"] for s in body["assistants"]] == ["segfault"]
    segfault = body["assistants"][0]
    assert segfault["name"] == "SEGFAULT"
    assert segfault["trigger"] == "keyword"
    assert segfault["keywords"] == ["segfault", "error"]
    assert segfault["demo"] is False


def test_get_accepts_bare_list_payload_and_skips_malformed(build_app):
    build, _ = build_app
    app = build(factory=RecordingFactory())

    async def scenario():
        async with client_for(app) as client:
            app.state.log.append_event(
                ASSISTANTS_SESSION_ID,
                "system",
                "user",
                [
                    {"id": "muse", "name": "Muse", "persona": "A quiet muse."},
                    {"name": "missing id", "persona": "dropped"},
                    {"id": "bad", "name": "Bad", "persona": "x", "trigger": "sometimes"},
                    "not a dict",
                ],
            )
            return (await client.get("/api/assistants")).json()

    body = run(scenario())
    assert body["meta"]["source"] == "log"
    assert [s["id"] for s in body["assistants"]] == ["muse"]


def test_get_accepts_definitions_key_without_event_kind(build_app):
    build, _ = build_app
    app = build(factory=RecordingFactory())

    async def scenario():
        async with client_for(app) as client:
            app.state.log.append_event(
                ASSISTANTS_SESSION_ID,
                "system",
                "user",
                {"definitions": [{"id": "x", "name": "X", "persona": "p"}]},
            )
            return (await client.get("/api/assistants")).json()

    body = run(scenario())
    assert body["meta"]["source"] == "log"
    assert [s["id"] for s in body["assistants"]] == ["x"]


# -- consult -----------------------------------------------------------------


def test_consult_returns_reply_and_appends_system_event(build_app):
    build, log_path = build_app
    caller = FakeCaller(chunks=("Hello ", "from ", "the oracle."))
    factory = RecordingFactory(caller)
    app = build(factory=factory)

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            resp = await client.post(
                f"/api/sessions/{sid}/assistants/demo_oracle/consult",
                json={"user_msg": "What now?"},
            )
            projection = (await client.get(f"/api/sessions/{sid}/events")).json()
            return sid, resp, projection

    sid, resp, projection = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {
        "assistant": "demo_oracle",
        "text": "Hello from the oracle.",
        "agent": "narrator",
    }
    assert factory.agents == ["narrator"]

    # A consult never advances state/history: no narrative, no state-delta.
    assert projection["narratives"] == []
    assert projection["state"] == {}

    # The reply is an appended assistant_reply system event (never a narrative).
    log = EventLog(log_path)
    events = log.get_events(sid)
    log.close()
    replies = [
        e
        for e in events
        if e.type == "system" and e.payload.get("event") == ASSISTANT_REPLY_EVENT
    ]
    assert len(replies) == 1
    assert replies[0].payload["assistant"] == "demo_oracle"
    assert replies[0].payload["text"] == "Hello from the oracle."
    assert replies[0].meta["agent"] == "narrator"
    assert replies[0].meta["user_msg"] == "What now?"


def test_consult_passes_session_context_to_assistant(build_app):
    build, _ = build_app
    caller = FakeCaller(chunks=("ack",))
    factory = RecordingFactory(caller)
    app = build(factory=factory)

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            app.state.log.append_event(
                sid, "state-delta", "extractor", {"time": "14:45", "loc": "night market"}
            )
            app.state.log.append_event(
                sid,
                "narrative",
                "narrator",
                {
                    "prose": "The market hums under lamplight.",
                    "dsl": "root = ScenePane(\"night market\", [])",
                },
            )
            resp = await client.post(
                f"/api/sessions/{sid}/assistants/demo_gremlin/consult",
                json={"user_msg": "Are we safe?"},
            )
            return resp

    resp = run(scenario())
    assert resp.status_code == 200
    (messages,) = caller.calls
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "Demo Gremlin" in system
    assert "time: 14:45" in user and "loc: night market" in user
    assert "The market hums under lamplight." in user
    assert "Are we safe?" in user
    # DSL never leaks into the prose digest.
    assert "root = ScenePane" not in user


def test_consult_unknown_assistant_is_404(build_app):
    build, _ = build_app
    app = build(factory=RecordingFactory())

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            return await client.post(
                f"/api/sessions/{sid}/assistants/nope/consult",
                json={"user_msg": "hi"},
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


def test_consult_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build(factory=RecordingFactory())

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/ghost/assistants/demo_oracle/consult",
                json={"user_msg": "hi"},
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


def test_consult_without_caller_seam_is_500(build_app):
    build, _ = build_app
    app = build()  # neither factory nor registry attached

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            return await client.post(
                f"/api/sessions/{sid}/assistants/demo_oracle/consult",
                json={"user_msg": "hi"},
            )

    resp = run(scenario())
    assert resp.status_code == 500
    assert "factory unavailable" in resp.json()["detail"]


# -- replies hydration -------------------------------------------------------


def test_replies_endpoint_reads_back_system_events(build_app):
    build, _ = build_app
    app = build(factory=RecordingFactory(FakeCaller(chunks=("one",))))

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            await client.post(
                f"/api/sessions/{sid}/assistants/demo_oracle/consult",
                json={"user_msg": "first"},
            )
            return sid, (await client.get(f"/api/sessions/{sid}/assistants/replies")).json()

    sid, body = run(scenario())
    assert body["session_id"] == sid
    assert len(body["replies"]) == 1
    reply = body["replies"][0]
    assert reply["assistant"] == "demo_oracle"
    assert reply["text"] == "one"
    assert reply["agent"] == "narrator"
    assert reply["user_msg"] == "first"


def test_replies_endpoint_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build(factory=RecordingFactory())

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/ghost/assistants/replies")

    resp = run(scenario())
    assert resp.status_code == 404


# -- caller-factory seam -----------------------------------------------------


def test_caller_factory_prefers_injected_then_registry():
    injected = RecordingFactory()

    class InjectedState:
        assistant_model_caller_factory = injected
        registry = object()

    class RegistryState:
        registry = object()

    injected_request = SimpleNamespace(app=SimpleNamespace(state=InjectedState()))
    registry_request = SimpleNamespace(app=SimpleNamespace(state=RegistryState()))

    assert _caller_factory(injected_request) is injected
    # A registry-only state yields a callable that builds a RegistryModelCaller
    # (construction does no network I/O).
    factory = _caller_factory(registry_request)
    assert callable(factory)
    caller = factory("narrator")
    assert caller.agent == "narrator"
