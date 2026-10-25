"""Persisted per-turn step traces (W3B follow-up): the turn-progress header
must survive a reload AND a swipe/regenerate branch switch.

The ordered trace used to live ONLY in the client ``turnSteps`` store, folded
from live ``stage`` frames, and was wiped the moment ``openSession(branchId)``
ran ``clearSessionView`` -> ``resetTurnSteps``. ``_drive_turn`` now folds the
turn's stage frames and appends ONE ``turn_stages`` system event bound to the
narrative seq; ``EventLog.get_projection`` re-attaches it to that narrative so
the hydrate path re-seeds the header.

These use the REAL ``GraphTurnEngine`` over canned model/extractor seams (no
model call) so the real ``stage`` frames and the real post-turn narrative commit
are exercised end to end through the HTTP API.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.app import _fold_stage_frame
from fabulist.api.fake import FakeTurnEngine
from fabulist.graph import GraphTurnEngine, build_graph
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


class FakeModel:
    """A ``ModelCaller``: one reasoning delta then canned content chunks."""

    def __init__(self, chunks=("Rain hammers ", "the shutters."), reasoning="weighing"):
        self.chunks = list(chunks)
        self.reasoning = reasoning

    async def __call__(self, messages, stream=True):
        self.calls = getattr(self, "calls", [])
        self.calls.append(messages)
        if self.reasoning is not None:
            yield {"type": "reasoning", "delta": self.reasoning}
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield {"type": "content", "delta": chunk}


class FakeExtractor:
    async def __call__(self, user_msg, prose):
        return {"deltas": {"tone": "warm"}, "action_class": "dialogue"}


def _parse_sse(text: str) -> list[dict]:
    """Split SSE text into ``{event, data}`` blocks (id/comments ignored)."""
    events: list[dict] = []
    block: dict | None = None
    for line in text.split("\n"):
        if line == "":
            if block is not None and block:
                events.append(block)
            block = None
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if block is None:
            block = {}
        if field == "event":
            block["event"] = value
        elif field == "data":
            block["data"] = json.loads(value)
    return events


def _make_graph_app(tmp_path: Path):
    """An app driven by a real GraphTurnEngine over one log path.

    Returns ``(app, engine_log)``; the caller closes both logs.
    """
    log_path = tmp_path / "events.db"
    engine_log = EventLog(log_path)
    model = FakeModel()
    graph = build_graph(model=model, extractor=FakeExtractor(), log=engine_log)
    engine = GraphTurnEngine(graph=graph, log=engine_log, model=model)
    app = create_app(engine, log_path)
    return app, engine_log


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# -- unit: the fold mirrors the client's ``foldTurnStep`` --------------------


def test_fold_stage_frame_mirrors_client_semantics():
    steps: list[dict] = []
    _fold_stage_frame(steps, {"stage": "narrate", "label": "Writing", "state": "start",
                              "index": 1, "total": 6, "ts": "t1"})
    _fold_stage_frame(steps, {"stage": "narrate", "label": "Writing", "state": "done",
                              "index": 1, "total": 6, "ts": "t2"})
    _fold_stage_frame(steps, {"stage": "dsl", "label": "Checking", "state": "start",
                              "index": 2, "total": 6, "ts": "t3"})
    _fold_stage_frame(steps, {"stage": "dsl", "label": "Checking", "state": "done",
                              "index": 2, "total": 6, "ts": "t4",
                              "notes": ["dangling ref dropped"]})
    assert [s["name"] for s in steps] == ["narrate", "dsl"]
    assert [s["state"] for s in steps] == ["done", "done"]
    assert steps[0]["ts"] == "t2"  # the done frame's ts wins on the closed step
    assert steps[1]["notes"] == ["dangling ref dropped"]

    # A new turn's FIRST frame replaces the prior trace (W9.20 §5.6: ``plan`` or a
    # plan-less ``lanes`` — the two ways a turn opens now); a frame naming no step
    # is ignored.
    _fold_stage_frame(steps, {"stage": "lanes", "label": "Gathering", "state": "start",
                              "index": 2, "total": 8, "ts": "t5"})
    assert [s["name"] for s in steps] == ["lanes"]
    _fold_stage_frame(steps, {"label": "", "stage": "", "state": "start"})
    assert [s["name"] for s in steps] == ["lanes"]
    # W9.24 §3.3.7: a defect turn's repair iterations re-emit the NARRATE slot
    # with a "(repair N)" label — the SAME index, so the re-opened start REPLACES
    # that slot in place (one row per slot): the persisted trace can never hold
    # two index-3 rows with the second below index 4.
    _fold_stage_frame(steps, {"stage": "narrate", "label": "Writing the scene",
                              "state": "start", "index": 3, "total": 8, "ts": "t6"})
    _fold_stage_frame(steps, {"stage": "narrate", "label": "Writing the scene",
                              "state": "done", "index": 3, "total": 8, "ts": "t7"})
    _fold_stage_frame(steps, {"stage": "narrate", "label": "Writing the scene (repair 1)",
                              "state": "start", "index": 3, "total": 8, "ts": "t8"})
    _fold_stage_frame(steps, {"stage": "narrate", "label": "Writing the scene (repair 2)",
                              "state": "done", "index": 3, "total": 8, "ts": "t9"})
    assert [s["name"] for s in steps] == ["lanes", "narrate"]
    assert steps[-1]["label"] == "Writing the scene (repair 1)"  # the start's label
    assert steps[-1]["state"] == "done"
    assert {s["index"] for s in steps if s["name"] == "narrate"} == {3}
    # A trace persisted BEFORE the retirement still folds: the ``regen`` stage
    # name is read-side legacy, never emitted again.
    _fold_stage_frame(steps, {"stage": "regen", "label": "Reworking the scene",
                              "state": "start", "index": 5, "total": 8, "ts": "t10"})
    _fold_stage_frame(steps, {"stage": "regen", "label": "Reworking the scene",
                              "state": "done", "index": 5, "total": 8, "ts": "t11"})
    assert steps[-1] == {"name": "regen", "label": "Reworking the scene",
                         "state": "done", "index": 5, "total": 8, "ts": "t11"}


def test_fold_stage_frame_reopened_narrate_yields_eight_unique_indexes():
    """A repaired turn's re-opened narrate replaces its slot: 1..8, no duplicate.

    The repair loop re-emits ``narrate start`` (index 3) AFTER the ``dsl`` step
    has closed. Appending would persist nine rows with index 3 twice and the
    second one below index 4 — the duplicate/out-of-order trace the header
    rehydrated from. Keyed by index, the re-opened row overwrites index 3 in
    place, so a planner turn persists exactly the 8 slots in order.
    """
    steps: list[dict] = []
    names = (
        "plan", "lanes", "narrate", "dsl",
        "critic", "world_state", "env_hud", "attribution",
    )
    for index, name in enumerate(names, start=1):
        _fold_stage_frame(
            steps,
            {"stage": name, "label": name, "state": "start", "index": index, "total": 8},
        )
        _fold_stage_frame(
            steps,
            {"stage": name, "label": name, "state": "done", "index": index, "total": 8},
        )
    assert [s["index"] for s in steps] == [1, 2, 3, 4, 5, 6, 7, 8]

    # The narrator's repair loop re-opens the narrate slot (index 3) after the
    # dsl step already closed; a done frame then closes it again.
    _fold_stage_frame(
        steps,
        {"stage": "narrate", "label": "Writing the scene (repair 1)",
         "state": "start", "index": 3, "total": 8},
    )
    _fold_stage_frame(
        steps,
        {"stage": "narrate", "label": "Writing the scene (repair 1)",
         "state": "done", "index": 3, "total": 8},
    )

    assert [s["name"] for s in steps] == list(names)
    assert [s["index"] for s in steps] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert len({s["index"] for s in steps}) == 8
    assert steps[2]["label"] == "Writing the scene (repair 1)"
    assert steps[2]["state"] == "done"


# -- integration: a driven turn persists + surfaces its trace ----------------


def test_driven_turn_appends_turn_stages_bound_to_narrative_seq(tmp_path):
    app, engine_log = _make_graph_app(tmp_path)
    try:

        async def scenario():
            async with _client(app) as client:
                created = await client.post("/api/sessions", json={"title": "T"})
                sid = created.json()["session_id"]
                resp = await client.post(
                    f"/api/sessions/{sid}/turns",
                    json={"content": "I greet the merchant", "kind": "in_character"},
                )
                assert resp.status_code == 200
                projection = (await client.get(f"/api/sessions/{sid}/events")).json()
                return sid, resp.text, projection

        sid, text, projection = run(scenario())

        # One narrative, one persisted trace, bound to that narrative's seq.
        narrative = projection["narratives"][-1]
        steps = narrative["payload"]["steps"]
        assert steps, "the projection surfaced no turn steps"

        log = app.state.log
        stage_events = [
            e
            for e in log.get_events(sid)
            if e.type == "system" and e.payload.get("event") == "turn_stages"
        ]
        assert len(stage_events) == 1, "expected exactly one turn_stages event"
        assert stage_events[0].payload["seq"] == narrative["seq"]
        assert stage_events[0].payload["steps"] == steps

        # The persisted trace mirrors the live stage frames the browser saw.
        live_names: list[str] = []
        for event in _parse_sse(text):
            if event["event"] != "stage":
                continue
            name = event["data"]["meta"]["stage"]
            if name not in live_names:
                live_names.append(name)
        assert [s["name"] for s in steps] == live_names
        assert all(s["state"] == "done" for s in steps)
        assert steps[0]["name"] == "lanes"  # W9.16: the fan-out is a real stage
    finally:
        app.state.log.close()
        engine_log.close()


def test_each_turn_gets_its_own_trace_in_the_projection(tmp_path):
    """Two driven turns -> two events, each attached to its own narrative."""
    app, engine_log = _make_graph_app(tmp_path)
    try:

        async def scenario():
            async with _client(app) as client:
                sid = (
                    await client.post("/api/sessions", json={"title": "T"})
                ).json()["session_id"]
                for content in ("first", "second"):
                    resp = await client.post(
                        f"/api/sessions/{sid}/turns",
                        json={"content": content, "kind": "in_character"},
                    )
                    assert resp.status_code == 200
                return sid, (await client.get(f"/api/sessions/{sid}/events")).json()

        sid, projection = run(scenario())
        narratives = projection["narratives"]
        assert len(narratives) == 2
        for narrative in narratives:
            assert narrative["payload"]["steps"], "a turn lost its trace"

        stage_events = [
            e
            for e in app.state.log.get_events(sid)
            if e.type == "system" and e.payload.get("event") == "turn_stages"
        ]
        assert len(stage_events) == 2
        assert {e.payload["seq"] for e in stage_events} == {
            n["seq"] for n in narratives
        }
    finally:
        app.state.log.close()
        engine_log.close()


def test_projection_uses_newest_trace_per_seq(tmp_path):
    """Latest-wins: a second trace for the same narrative seq supersedes."""
    app, engine_log = _make_graph_app(tmp_path)
    try:

        async def scenario():
            async with _client(app) as client:
                sid = (
                    await client.post("/api/sessions", json={"title": "T"})
                ).json()["session_id"]
                await client.post(
                    f"/api/sessions/{sid}/turns",
                    json={"content": "hi", "kind": "in_character"},
                )
                return sid

        sid = run(scenario())
        log = app.state.log
        narrative = log.get_projection(sid)["narratives"][-1]
        # A later trace for the SAME seq wins on read (superseding is unneeded).
        log.append_event(
            sid,
            "system",
            "system",
            {
                "event": "turn_stages",
                "steps": [{"name": "later", "label": "Later", "state": "done",
                           "index": 1, "total": 1, "ts": "z"}],
                "seq": narrative["seq"],
            },
        )
        steps = log.get_projection(sid)["narratives"][-1]["payload"]["steps"]
        assert [s["name"] for s in steps] == ["later"]
    finally:
        app.state.log.close()
        engine_log.close()


def test_turn_without_stage_frames_persists_nothing(tmp_path):
    """A scripted engine that emits no stage frames records no trace."""
    log_path = tmp_path / "events2.db"
    app = create_app(FakeTurnEngine(), log_path)
    try:

        async def scenario():
            async with _client(app) as client:
                sid = (
                    await client.post("/api/sessions", json={"title": "T"})
                ).json()["session_id"]
                resp = await client.post(
                    f"/api/sessions/{sid}/turns",
                    json={"content": "hi", "kind": "in_character"},
                )
                assert resp.status_code == 200
                return sid

        sid = run(scenario())
        assert not [
            e
            for e in app.state.log.get_events(sid)
            if e.type == "system" and e.payload.get("event") == "turn_stages"
        ]
    finally:
        app.state.log.close()
