"""Tests for the turn inspector router (W3.6, SURFACE #23).

Exercises the real FastAPI app (httpx ASGI transport) with the inspector router
mounted over a synthetic tmp SQLite event log. The log stays the source of
truth: the router only walks ``get_events`` and derives the trace.

Provenance lives in the narrative row's ``meta["config"]`` column — verified
live: ``EventLog.append_event(...)`` accepts a ``meta=`` kwarg and
``EventLog.get_events`` re-surfaces it on ``Event.meta`` (``_row_to_event``),
so the tests seed the snapshot through ``meta=`` rather than a payload key.

Asserted contracts: per-turn pairing and latency math, config-snapshot
provenance, state-delta / validation-flag / revision counts, usage aggregation
by model and actor, and ``404`` for an unknown session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.inspector_api import router
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
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


def _seed(log_path: Path) -> None:
    """Write the synthetic log: two turns, a state-delta, a flag, a revision."""
    with EventLog(log_path) as log:
        # Turn 1: user -> narrative (config alpha), latency 2.5s.
        log.append_event(
            "s1",
            "user",
            "user",
            {"text": "hi"},
            ts="2026-01-01T00:00:00+00:00",
        )
        narrative1 = log.append_event(
            "s1",
            "narrative",
            "narrator",
            {"prose": "hello there"},
            ts="2026-01-01T00:00:02.500000+00:00",
            meta={
                "config": {
                    "model": "alpha",
                    "sampler": {"temperature": 0.8},
                    "prompt_version": 3,
                    "facts_hash": "abc123",
                }
            },
        )

        # Turn 2: user -> (state-delta + validation flag) -> narrative (beta).
        log.append_event(
            "s1",
            "user",
            "user",
            {"text": "again"},
            ts="2026-01-01T00:01:00+00:00",
        )
        log.append_event("s1", "state-delta", "extractor", {"mood": "calm"})
        log.append_event(
            "s1",
            "system",
            "validator",
            {"event": "validation_flag", "flags": [{"kind": "tell"}]},
        )
        log.append_event(
            "s1",
            "narrative",
            "narrator",
            {"prose": "welcome back"},
            ts="2026-01-01T00:01:03+00:00",
            meta={
                "config": {
                    "model": "beta",
                    "sampler": {"temperature": 0.5},
                    "prompt_version": 4,
                    "facts_hash": "def456",
                }
            },
        )

        # A revision superseding turn 1's narrative (appended after it).
        log.append_revision(
            "s1",
            narrative1.id,
            actor="user",
            payload={"revised": {"prose": "hello, edited"}, "reason": "user edit"},
        )


def test_turns_pairing_latency_and_provenance(build_app):
    build, log_path = build_app
    _seed(log_path)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/inspect/turns")).json()

    body = run(scenario())
    turns = body["turns"]
    assert len(turns) == 2

    first, second = turns

    # Latency math: narrative.ts - user.ts as float seconds.
    assert first["user_seq"] == 1
    assert first["narrative_seq"] == 2
    assert first["latency_s"] == pytest.approx(2.5)
    assert first["user_ts"] == "2026-01-01T00:00:00+00:00"
    assert first["narrative_ts"] == "2026-01-01T00:00:02.500000+00:00"

    # Config-snapshot provenance from the narrative's meta["config"].
    assert first["model"] == "alpha"
    assert first["sampler"] == {"temperature": 0.8}
    assert first["prompt_version"] == 3
    assert first["facts_hash"] == "abc123"

    # Turn 1's only activity is the superseding revision (appended later).
    assert first["delta_count"] == 0
    assert first["validation_flags"] == 0
    assert first["revision_count"] == 1

    # Turn 2: state-delta + validation flag sit inside [user_seq, narrative_seq].
    assert second["user_seq"] == 3
    assert second["narrative_seq"] == 6
    assert second["latency_s"] == pytest.approx(3.0)
    assert second["model"] == "beta"
    assert second["delta_count"] == 1
    assert second["validation_flags"] == 1
    assert second["revision_count"] == 0


def test_usage_aggregates_models_and_actors(build_app):
    build, log_path = build_app
    _seed(log_path)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/inspect/turns")).json()

    usage = run(scenario())["usage"]

    # One turn per model, keyed by the config snapshot's model name.
    assert usage["models"] == {"alpha": 1, "beta": 1}

    # Agents are the distinct actor strings across every event in the session.
    assert usage["agents"] == {
        "user": 3,  # user x2 + the revision's user actor
        "narrator": 2,
        "extractor": 1,
        "validator": 1,
    }


def test_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/nope/inspect/turns")

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


def test_user_without_narrative_is_skipped(build_app):
    """A trailing user event (no narrative yet) yields no turn."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("s2", "user", "user", {"text": "orphan"})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s2/inspect/turns")).json()

    body = run(scenario())
    assert body["turns"] == []
    assert body["usage"]["models"] == {}
    assert body["usage"]["agents"] == {"user": 1}


def test_latest_turn_window_extends_to_end_of_log(build_app):
    """Post-narrative deltas/flags count toward the LATEST turn's window.

    ``post_turn`` appends state-deltas and validator flags AFTER the narrative;
    with no next user event the window must run to the end of the log, else the
    latest turn always reports zero (flavor-probe finding).
    """
    build, log_path = build_app
    app = build()
    log: EventLog = app.state.log
    sid = "s-late"
    log.append_event(sid, "user", "user", {"text": "go"})
    log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": "ok"},
        meta={"config": {"model": "m", "sampler": {}, "prompt_version": 1, "facts_hash": "x"}},
    )
    log.append_event(sid, "state-delta", "extractor", {"time": "09:00"})
    log.append_event(
        sid, "system", "validator", {"event": "validator_flags", "flags": ["probe"]}
    )
    async def scenario():
        async with client_for(app) as client:
            return await client.get(f"/api/sessions/{sid}/inspect/turns")

    resp = run(scenario())
    assert resp.status_code == 200
    last = resp.json()["turns"][-1]
    assert last["delta_count"] == 1
    assert last["validation_flags"] == 1


# -- I2/I3: the v2 per-turn payload (text, tokens, regions, RAG) -------------


def test_turn_payload_exposes_text_tokens_regions_and_rag(build_app):
    from fabulist.usage.track import usage_payload

    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("v2", "user", "user", {"text": "Who are you?"})
        # narrator usage lands between user and narrative (the call happens then)
        log.append_event(
            "v2", "system", "narrator",
            usage_payload(agent="narrator", model="your-model-id", session_id="v2",
                          prompt_tokens=3138, completion_tokens=980, latency_s=20.75),
        )
        log.append_event(
            "v2", "system", "validator",
            {"event": "validator_flags", "flags": [{"kind": "tell"}]},
        )
        log.append_event("v2", "state-delta", "extractor", {"tone": "dry"})
        narrative = log.append_event(
            "v2", "narrative", "narrator",
            {"prose": "A gear slips. I am Vex.", "dsl": "x = ScenePane()"},
            meta={"config": {
                "model": "your-model-id", "sampler": {"temperature": 0.85},
                "prompt_version": 2, "facts_hash": "abc",
                "recall": "<recall>\nold turn\n</recall>",
                "facts_text": "time: 08:10 | tone: dry",
                "regions": {"system": 4000, "library": 900, "history": 1200,
                            "facts": 300, "user": 16, "plan": 40, "panels": 0},
            }},
        )
        # extractor usage lands after the narrative (post_turn) -> same window
        log.append_event(
            "v2", "system", "extractor",
            usage_payload(agent="extractor", model="qwen", session_id="v2",
                          prompt_tokens=40, completion_tokens=10, latency_s=0.5),
        )
        log.append_revision(
            "v2", narrative.id, actor="user",
            payload={"revised": {"prose": "edited"}, "reason": "user fix"},
        )
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/v2/inspect/turns")).json()

    body = run(scenario())
    turn = body["turns"][0]

    # Full text, not snippets.
    assert turn["user_text"] == "Who are you?"
    assert turn["narrative_text"] == "A gear slips. I am Vex."
    assert turn["narrative_dsl"] == "x = ScenePane()"

    # Token counts summed from the usage events in the window.
    assert turn["tokens"] == {
        "prompt_tokens": 3178, "completion_tokens": 990, "total_tokens": 4168,
    }
    assert {c["agent"] for c in turn["calls"]} == {"narrator", "extractor"}

    # Prompt-region breakdown + injected facts text from the config snapshot.
    assert turn["regions"]["facts"] == 300
    assert turn["facts_text"] == "time: 08:10 | tone: dry"

    # Recall text + the RAG panel (query defaults to the user post).
    assert turn["recall_text"] == "<recall>\nold turn\n</recall>"
    assert turn["rag"]["query"] == "Who are you?"
    assert turn["rag"]["queries"] == ["Who are you?"]
    assert turn["rag"]["k"] == 6
    assert turn["rag"]["rerank_enabled"] is False
    assert turn["rag"]["memory_enabled"] is True

    # Validation detail + revision refs (not just counts).
    assert turn["validation_flags"] == 1
    assert turn["validation_details"][0]["flags"] == [{"kind": "tell"}]
    assert turn["revision_count"] == 1
    assert turn["revision_refs"][0]["reason"] == "user fix"
    assert turn["window"]["low"] == 1

    # Session usage carries the log-backed aggregate.
    usage = body["usage"]
    assert usage["calls"]["calls"] == 2
    assert usage["calls"]["total_tokens"] == 4168
    assert usage["by_agent"]["narrator"]["total_tokens"] == 4118
    assert usage["by_model"]["your-model-id"]["calls"] == 1


def test_model_resolved_from_usage_when_config_is_unknown(build_app):
    from fabulist.usage.track import usage_payload

    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("m2", "user", "user", {"text": "hi"})
        log.append_event(
            "m2", "system", "narrator",
            usage_payload(agent="narrator", model="your-model-id", session_id="m2",
                          prompt_tokens=10, completion_tokens=5, latency_s=1.0),
        )
        log.append_event(
            "m2", "narrative", "narrator", {"prose": "hello"},
            meta={"config": {"model": "unknown", "sampler": {},
                             "prompt_version": 0, "facts_hash": None}},
        )
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/m2/inspect/turns")).json()

    turn = run(scenario())["turns"][0]
    assert turn["model"] == "your-model-id"      # repaired from usage
    assert turn["model_raw"] == "unknown"  # raw value kept


def test_recall_settings_folded_into_rag_panel(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("rag1", "user", "user", {"text": "look around"})
        log.append_event(
            "rag1", "system", "user",
            {"event": "chat_memory_config", "k": 9, "rerank_enabled": True,
             "query_mode": "message+scene", "block_chars": 1500, "overfetch": 20},
        )
        log.append_event(
            "rag1", "narrative", "narrator", {"prose": "You see a market."},
            meta={"config": {"model": "m", "sampler": {},
                             "prompt_version": 1, "facts_hash": "h"}},
        )
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/rag1/inspect/turns")).json()

    rag = run(scenario())["turns"][0]["rag"]
    assert rag["k"] == 9
    assert rag["rerank_enabled"] is True
    assert rag["query_mode"] == "message+scene"


def test_usage_endpoint_reads_log_and_404s_unknown_session(build_app):
    from fabulist.usage.track import usage_payload

    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("u1", "user", "user", {"text": "hi"})
        log.append_event(
            "u1", "system", "narrator",
            usage_payload(agent="narrator", model="your-model-id", session_id="u1",
                          prompt_tokens=100, completion_tokens=50, latency_s=2.0),
        )
    app = build()

    async def scenario():
        async with client_for(app) as client:
            ok = await client.get("/api/usage?session_id=u1")
            missing = await client.get("/api/usage?session_id=nope")
            allsessions = await client.get("/api/usage")
        return ok, missing, allsessions

    ok, missing, allsessions = run(scenario())
    assert ok.status_code == 200
    body = ok.json()
    assert body["source"] == "log"
    assert body["session_id"] == "u1"
    assert body["calls"] == 1
    assert body["total_tokens"] == 150
    assert body["agents"]["narrator"]["total_tokens"] == 150
    assert missing.status_code == 404
    # Cross-session view folds every non-reserved session.
    assert allsessions.status_code == 200
    assert allsessions.json()["calls"] >= 1
