"""W9.20 C1: the planner API (config, the folded plan, manual edit, regenerate).

The router is exercised through the real FastAPI app (httpx ASGI transport) over
a real :class:`EventLog`, and the regenerate door runs the REAL planner node: the
tests build a compiled graph with
:func:`fabulist.graph.nodes.make_planner_node` and hand it to the app as the
engine's graph, so the off-turn path is the same code a turn runs (a fake engine
object exposing no graph is the 503 case).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api import planner_api
from fabulist.api.app import _Turn
from fabulist.api.fake import FakeTurnEngine
from fabulist.graph import build_graph
from fabulist.graph.nodes import make_planner_node
from fabulist.graph.plans import Plan, load_last_plan, save_plan
from fabulist.log import EventLog

SID = "s-planner-api"

#: A schema-valid planner reply (content mode: the model answers with JSON).
PLAN_JSON = json.dumps(
    {
        "strings": [
            "The storm traps them in the lodge",
            "Mira hides the letter",
            "The generator fails at dusk",
            "A knock at the door",
        ],
        "datetime": "1994-11-02 17:40",
        "weather": "sleet",
        "forecast": "heavy snow by midnight",
        "established": ["Mira is the caretaker", "the road is closed"],
    }
)


def run(coro):
    return asyncio.run(coro)


class FakePlannerModel:
    """A model caller that answers the planner's ask with ``PLAN_JSON``."""

    def __init__(self, reply: str = PLAN_JSON) -> None:
        self.reply = reply
        self.calls = 0

    def __call__(self, messages, stream=True, request_profile=None):
        self.calls += 1

        async def _gen():
            yield {"type": "content", "delta": self.reply}

        return _gen()


class _RateLimitedModel:
    """A model caller that fails at the provider seam: a real 429."""

    def __call__(self, messages, stream=True, request_profile=None):
        raise _rate_limited()


def _rate_limited():
    """The object the SDK raises for a 429 (status + body + Retry-After)."""
    request = httpx.Request("POST", "http://provider.example/v1/chat/completions")
    body = {"error": {"message": "Rate limit reached for requests"}}
    response = httpx.Response(
        429, request=request, headers={"retry-after": "20"}, json=body
    )
    import openai

    return openai.RateLimitError(
        f"Error code: 429 - {body}", response=response, body=body
    )


class FakeNarratorModel:
    """The opening narrator's caller: prose + one openui statement."""

    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    def __call__(self, messages, stream=True, request_profile=None):
        self.messages.append(messages)

        async def _gen():
            yield {"type": "content", "delta": "The lodge door bangs open.\n"}
            yield {
                "type": "content",
                "delta": "```openui\nroot = Card([Text('sleet')])\n```\n",
            }

        return _gen()


@pytest.fixture
def harness(tmp_path: Path):
    """Apps sharing one log path; closes the logs afterwards."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _planner_app(log: EventLog, model: FakePlannerModel, **node_kwargs):
    """Attach a COMPILED graph carrying a real planner node to ``app.state.engine``."""
    graph = build_graph(
        model=model,
        extractor=lambda *args: {},
        log=log,
        planner=make_planner_node(log=log, caller=model, **node_kwargs),
    )
    return SimpleNamespace(_graph=graph)


async def _new_session(client: httpx.AsyncClient, title: str = "Plan") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def _events(log: EventLog, sid: str) -> list:
    return log.get_events(sid)


def _plan_events(log: EventLog, sid: str) -> list:
    return [
        event
        for event in _events(log, sid)
        if event.type == "system" and (event.payload or {}).get("event") == "plan"
    ]


# -- config (GET/PUT /api/prompts/planner) -----------------------------------


def test_planner_config_defaults_and_round_trip(harness):
    build, _ = harness
    app = build()

    async def scenario():
        async with client_for(app) as client:
            defaults = (await client.get("/api/prompts/planner")).json()
            assert defaults["planner"] == {"cadence": 5, "steer": "", "fields": []}

            saved = await client.put(
                "/api/prompts/planner",
                json={
                    "planner": {
                        "cadence": 3,
                        "steer": "keep it tense",
                        "fields": [
                            {"id": "gold", "title": "Gold", "type": "number"},
                            {"id": "reputation", "title": "Rep", "type": "string"},
                        ],
                    }
                },
            )
            assert saved.status_code == 200
            stored = saved.json()["planner"]
            assert stored["cadence"] == 3
            assert stored["steer"] == "keep it tense"
            assert [row["id"] for row in stored["fields"]] == ["gold", "reputation"]

            # The write is what a later GET serves (preset append, latest wins).
            assert (await client.get("/api/prompts/planner")).json()["planner"] == stored

            # A BARE config body is accepted too (no ``planner`` wrapper).
            bare = await client.put("/api/prompts/planner", json={"cadence": 7})
            assert bare.status_code == 200
            assert bare.json()["planner"]["cadence"] == 7
            # ... and the omitted keys keep their defaults rather than vanishing.
            assert bare.json()["planner"]["steer"] == ""
            assert bare.json()["planner"]["fields"] == []

    run(scenario())


def test_planner_config_rejects_bad_rows_with_reasons(harness):
    build, _ = harness
    app = build()

    async def scenario():
        async with client_for(app) as client:
            cadence = await client.put("/api/prompts/planner", json={"cadence": 0})
            assert cadence.status_code == 422
            assert "cadence must be >= 1" in cadence.json()["detail"]["errors"][0]

            duplicate = await client.put(
                "/api/prompts/planner",
                json={
                    "fields": [
                        {"id": "gold", "type": "number"},
                        {"id": "gold", "type": "number"},
                    ]
                },
            )
            assert duplicate.status_code == 422
            assert "duplicates 'gold'" in duplicate.json()["detail"]["errors"][0]

            bad_type = await client.put(
                "/api/prompts/planner",
                json={"fields": [{"id": "gold", "type": "currency"}]},
            )
            assert bad_type.status_code == 422
            assert "must be one of" in bad_type.json()["detail"]["errors"][0]
            assert "'currency'" in bad_type.json()["detail"]["errors"][0]

            # Nothing was written: the config is still the untouched default.
            assert (await client.get("/api/prompts/planner")).json()["planner"] == {
                "cadence": 5,
                "steer": "",
                "fields": [],
            }

    run(scenario())


# -- the session's plan (GET/PUT /api/sessions/{id}/plan) --------------------


def test_plan_get_is_null_before_any_plan_and_due_in_after(harness):
    build, _ = harness
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            empty = await client.get(f"/api/sessions/{sid}/plan")
            assert empty.status_code == 200
            body = empty.json()
            assert body["plan"] is None
            assert body["due_in"] is None  # no plan -> turn 1 plans unconditionally
            assert body["cadence"] == 5
            assert body["committed_turns"] == 0

            await client.put("/api/prompts/planner", json={"cadence": 4})
            plan = Plan(turn_seq=0, created_from="graph", strings=["a", "b", "c", "d"])
            save_plan(app.state.log, sid, plan)

            due = (await client.get(f"/api/sessions/{sid}/plan")).json()
            assert due["plan"]["strings"] == ["a", "b", "c", "d"]
            assert due["render"].startswith("PLAN (turn 0)")
            assert due["due_in"] == 4
            assert due["cadence"] == 4

    run(scenario())


def test_manual_plan_edit_folds_as_the_latest_plan(harness):
    build, _ = harness
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            await client.put(
                "/api/prompts/planner",
                json={"fields": [{"id": "gold", "title": "Gold", "type": "number"}]},
            )
            save_plan(
                app.state.log,
                sid,
                Plan(turn_seq=0, created_from="graph", strings=["x", "y", "z", "w"]),
            )

            resp = await client.put(
                f"/api/sessions/{sid}/plan",
                json={
                    "plan": {
                        "strings": ["one", "two", "three", "four"],
                        "datetime": "1994-11-02 17:40",
                        "established": ["the road is closed"],
                        "gold": 9,
                    }
                },
            )
            assert resp.status_code == 200
            assert resp.json()["plan"]["created_from"] == "manual"
            assert resp.json()["plan"]["extras"] == {"gold": 9}

            # D9 append-only: both plan events are in the log, the fold takes the
            # newest, and the narrator's seam sees the manual edit.
            assert len(_plan_events(app.state.log, sid)) == 2
            folded = load_last_plan(app.state.log, sid)
            assert folded is not None
            assert folded.strings == ["one", "two", "three", "four"]
            assert folded.created_from == "manual"
            assert folded.extras == {"gold": 9}
            assert (await client.get(f"/api/sessions/{sid}/plan")).json()["plan"][
                "strings"
            ] == ["one", "two", "three", "four"]

    run(scenario())


def test_manual_plan_edit_validates_against_the_active_field_list(harness):
    build, _ = harness
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            await client.put(
                "/api/prompts/planner",
                json={
                    "fields": [
                        {"id": "gold", "title": "Gold", "type": "number", "required": True}
                    ]
                },
            )
            # Required extra missing -> 422 with the reason, nothing appended.
            missing = await client.put(
                f"/api/sessions/{sid}/plan",
                json={"strings": ["a", "b", "c", "d"]},
            )
            assert missing.status_code == 422
            assert "required planner field 'gold'" in missing.json()["detail"]["errors"][0]
            assert _plan_events(app.state.log, sid) == []

            # Wrongly typed extra -> 422.
            wrong = await client.put(
                f"/api/sessions/{sid}/plan",
                json={"strings": ["a", "b", "c", "d"], "gold": "lots"},
            )
            assert wrong.status_code == 422
            assert "must be a number" in wrong.json()["detail"]["errors"][0]

            # Unknown key -> 422 (a typo must never be persisted as preset data).
            unknown = await client.put(
                f"/api/sessions/{sid}/plan",
                json={"strings": ["a", "b", "c", "d"], "gold": 3, "silver": 1},
            )
            assert unknown.status_code == 422
            assert "unknown planner fields: silver" in unknown.json()["detail"]["errors"][0]

            ok = await client.put(
                f"/api/sessions/{sid}/plan",
                json={"strings": ["a", "b", "c", "d"], "gold": 3},
            )
            assert ok.status_code == 200
            assert ok.json()["plan"]["extras"] == {"gold": 3}

    run(scenario())


# -- regenerate --------------------------------------------------------------


def test_regenerate_409_while_a_turn_streams_pre_barrier(harness):
    build, log_path = harness
    log = EventLog(log_path)
    model = FakePlannerModel()
    app = build()
    app.state.engine = _planner_app(log, model)
    app.state.log = log

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            # A turn that has NOT passed the commit barrier (W9.19 A): refused.
            streaming = _Turn()
            app.state.sessions[sid].turn = streaming
            resp = await client.post(f"/api/sessions/{sid}/plan/regenerate")
            assert resp.status_code == 409
            assert model.calls == 0
            assert _plan_events(log, sid) == []
            streaming.done.set()

    try:
        run(scenario())
    finally:
        log.close()


def test_regenerate_queues_behind_a_committed_tail(harness, monkeypatch):
    build, log_path = harness
    log = EventLog(log_path)
    model = FakePlannerModel()
    app = build()
    app.state.engine = _planner_app(log, model)
    app.state.log = log
    narrator = FakeNarratorModel()
    monkeypatch.setattr(planner_api, "_narrator_caller", lambda log, app: narrator)
    order: list[str] = []

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            committed_turn = _Turn()
            committed_turn.committed.set()  # prose kept, background tail running
            app.state.sessions[sid].turn = committed_turn

            async def _tail_finishes():
                await asyncio.sleep(0.05)
                order.append("tail-done")
                committed_turn.done.set()

            tail = asyncio.ensure_future(_tail_finishes())
            resp = await client.post(f"/api/sessions/{sid}/plan/regenerate")
            order.append("regenerated")
            await tail
            assert resp.status_code == 200
            # The planner ran AFTER the committed turn's tail released the slot.
            assert order == ["tail-done", "regenerated"]
            assert model.calls == 1
            assert resp.json()["plan"]["created_from"] == "graph"
            # A zero-turn session also got its opening message (session-local 0).
            assert resp.json()["opening"]["prose"].startswith("The lodge door")

    try:
        run(scenario())
    finally:
        log.close()


def test_regenerate_opening_message_payload_shape_on_zero_turn_session(
    harness, monkeypatch
):
    build, log_path = harness
    log = EventLog(log_path)
    planner_model = FakePlannerModel()
    narrator_model = FakeNarratorModel()
    app = build()
    app.state.engine = _planner_app(log, planner_model)
    app.state.log = log
    monkeypatch.setattr(
        planner_api, "_narrator_caller", lambda log, app: narrator_model
    )

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            resp = await client.post(f"/api/sessions/{sid}/plan/regenerate")
            assert resp.status_code == 200
            body = resp.json()
            assert body["plan"]["created_from"] == "graph"
            assert body["plan"]["strings"][0] == "The storm traps them in the lodge"

            opening = body["opening"]
            # The commit node's payload shape, unchanged for segment.ts.
            assert set(opening) >= {"prose", "dsl", "segments", "seq"}
            assert "The lodge door bangs open." in opening["prose"]
            assert "root = Card" in opening["dsl"]
            assert [seg["kind"] for seg in opening["segments"]] == ["prose", "dsl"]

            narratives = [
                event for event in _events(log, sid) if event.type == "narrative"
            ]
            assert len(narratives) == 1
            payload = narratives[0].payload
            assert set(payload) == {"prose", "dsl", "segments"}
            assert payload["segments"] == opening["segments"]
            # Provenance meta like a committed turn's (config snapshot).
            config = narratives[0].meta["config"]
            assert set(config) >= {"model", "sampler", "prompt_version", "facts_hash"}

            # The opening ask is ONE system message: the plan rides it and there
            # is no player message (§7: the user_content seam is not faked).
            messages = narrator_model.messages[0]
            assert [m["role"] for m in messages] == ["system"]
            assert "PLAN (turn 0)" in messages[0]["content"]
            assert "The storm traps them in the lodge" in messages[0]["content"]
            assert "Write the OPENING" in messages[0]["content"]

    try:
        run(scenario())
    finally:
        log.close()


def test_regenerate_on_a_started_session_replaces_the_plan_only(harness, monkeypatch):
    build, log_path = harness
    log = EventLog(log_path)
    planner_model = FakePlannerModel()
    narrator_model = FakeNarratorModel()
    app = build()
    app.state.engine = _planner_app(log, planner_model)
    app.state.log = log
    monkeypatch.setattr(
        planner_api, "_narrator_caller", lambda log, app: narrator_model
    )

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            # A session that already played a turn (session-LOCAL narrative).
            log.append_event(sid, "narrative", "narrator", {"prose": "Earlier.", "dsl": ""})
            save_plan(
                log,
                sid,
                Plan(turn_seq=0, created_from="graph", strings=["old1", "old2", "old3", "old4"]),
            )

            resp = await client.post(f"/api/sessions/{sid}/plan/regenerate")
            assert resp.status_code == 200
            body = resp.json()
            assert body["opening"] is None
            assert body["plan"]["strings"][0] == "The storm traps them in the lodge"
            assert body["plan"]["turn_seq"] == 1  # the committed-turn count now
            # Strings only: no second narrative, no opening narrator call.
            assert len([e for e in _events(log, sid) if e.type == "narrative"]) == 1
            assert narrator_model.messages == []

    try:
        run(scenario())
    finally:
        log.close()


def test_regenerate_without_a_wired_planner_node_is_503(harness):
    build, _ = harness
    app = build()  # FakeTurnEngine: no compiled graph on the engine

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            resp = await client.post(f"/api/sessions/{sid}/plan/regenerate")
            assert resp.status_code == 503
            assert "no compiled planner node" in resp.json()["detail"]

    run(scenario())


def test_regenerate_reports_a_planner_failure(harness, monkeypatch):
    """A model reply the schema rejects is a 502, and the previous plan survives."""
    build, log_path = harness
    log = EventLog(log_path)
    broken = FakePlannerModel(reply=json.dumps({"strings": ["only one"]}))
    app = build()
    app.state.engine = _planner_app(log, broken)
    app.state.log = log
    monkeypatch.setattr(planner_api, "_narrator_caller", lambda log, app: FakeNarratorModel())

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            previous = Plan(
                turn_seq=0, created_from="graph", strings=["a", "b", "c", "d"]
            )
            save_plan(log, sid, previous)
            resp = await client.post(f"/api/sessions/{sid}/plan/regenerate")
            assert resp.status_code == 502
            assert "needs 4-6 strings" in resp.json()["detail"]["error"]
            # Best-effort rule: the PREVIOUS plan keeps steering.
            folded = load_last_plan(log, sid)
            assert folded is not None and folded.strings == ["a", "b", "c", "d"]
            failures = [
                event
                for event in _events(log, sid)
                if (event.payload or {}).get("event") == "plan_refresh_failed"
            ]
            assert len(failures) == 1

    try:
        run(scenario())
    finally:
        log.close()


def test_regenerate_reports_a_provider_failure_without_a_stale_plan(harness, monkeypatch):
    """A 429 aborts the run (it is NOT contained) and the door reports its text."""
    build, log_path = harness
    log = EventLog(log_path)
    app = build()
    app.state.engine = _planner_app(
        log, _RateLimitedModel(), provider_label_fn=lambda: "glm-4.6"
    )
    app.state.log = log
    monkeypatch.setattr(planner_api, "_narrator_caller", lambda log, app: FakeNarratorModel())

    async def scenario():
        async with client_for(app) as client:
            sid = await _new_session(client)
            previous = Plan(
                turn_seq=0, created_from="graph", strings=["a", "b", "c", "d"]
            )
            save_plan(log, sid, previous)
            resp = await client.post(f"/api/sessions/{sid}/plan/regenerate")
            assert resp.status_code == 502
            detail = resp.json()["detail"]
            assert detail["reason"] == "planner provider failure"
            assert detail["error"].startswith("Planning failed: 429 from glm-4.6")
            # The old plan is untouched and NO "kept the previous plan" row claims
            # the failure was contained.
            folded = load_last_plan(log, sid)
            assert folded is not None and folded.strings == ["a", "b", "c", "d"]
            assert not [
                event
                for event in _events(log, sid)
                if (event.payload or {}).get("event") == "plan_refresh_failed"
            ]

    try:
        run(scenario())
    finally:
        log.close()


# -- the preset seam (PresetInput.planner, W9.20/A2) -------------------------


def test_preset_planner_field_round_trips_and_keeps_on_omitted(harness):
    """A provided ``planner`` block round-trips; an omitted one KEEPS the stored."""
    build, _ = harness
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post("/api/prompts/presets", json={"name": "GM"})
            assert created.status_code == 200
            preset_id = created.json()["preset"]["preset_id"]

            saved = await client.put(
                f"/api/prompts/presets/{preset_id}",
                json={
                    "name": "GM",
                    "planner": {
                        "cadence": 2,
                        "steer": "grim",
                        "fields": [{"id": "gold", "title": "Gold", "type": "number"}],
                    },
                },
            )
            assert saved.status_code == 200
            stored = saved.json()["preset"]["planner"]
            assert stored["cadence"] == 2
            assert [row["id"] for row in stored["fields"]] == ["gold"]

            # An unrelated replace (no ``planner`` key) must not reset the block.
            kept = await client.put(
                f"/api/prompts/presets/{preset_id}", json={"name": "GM renamed"}
            )
            assert kept.status_code == 200
            assert kept.json()["preset"]["planner"] == stored

            # PATCH preserves it too (its planner door is PUT /api/prompts/planner).
            patched = await client.patch(
                f"/api/prompts/presets/{preset_id}", json={"name": "GM"}
            )
            assert patched.status_code == 200
            assert patched.json()["preset"]["planner"] == stored

            # A broken block is 422 with the reasons, and nothing changes.
            broken = await client.put(
                f"/api/prompts/presets/{preset_id}",
                json={"name": "GM", "planner": {"cadence": 0}},
            )
            assert broken.status_code == 422
            assert "cadence must be >= 1" in broken.json()["detail"]["errors"][0]
            after = await client.get(f"/api/prompts/presets/{preset_id}")
            assert after.json()["preset"]["planner"] == stored

    run(scenario())
