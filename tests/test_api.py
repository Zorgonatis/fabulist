"""Tests for the HTTP API + SSE streaming lane (W1.4).

Uses the real FastAPI app via httpx's ASGI transport plus a scripted
:class:`FakeTurnEngine`. Note that httpx's ASGITransport buffers a streaming
response until the ASGI call returns, so tests that need a turn to stay in
flight (turn lock, stop, Last-Event-ID) start the ``POST /turns`` request as a
background task and synchronise on ``app.state.sessions`` before issuing the
second request.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.sse import ReplayBuffer
from fabulist.log import EventLog


# --- harness -----------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine, **kwargs):
        app = create_app(engine, log_path, **kwargs)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def parse_sse(text: str) -> tuple[list[dict], list[str]]:
    """Split SSE text into parsed data events and comment lines."""
    events: list[dict] = []
    comments: list[str] = []
    block: dict = {}
    for line in text.split("\n"):
        if line == "":
            if block:
                comments.extend(block.pop("comments", []))
                if block:
                    events.append(block)
                block = {}
            continue
        if line.startswith(":"):
            block.setdefault("comments", []).append(line)
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            block["id"] = int(value)
        elif field == "event":
            block["event"] = value
        elif field == "data":
            block["data"] = json.loads(value)
    if block:
        comments.extend(block.pop("comments", []))
        if block:
            events.append(block)
    return events, comments


async def _create_session(client: httpx.AsyncClient, title: str = "Test") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


async def _await_active_turn(app, session_id: str, *, timeout: float = 5.0) -> None:
    """Block until the session has a live turn (producer task scheduled)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        turn = app.state.sessions[session_id].turn
        if turn is not None and not turn.done.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn never became active")


# --- tests -------------------------------------------------------------------------


def test_turn_stream_named_events_with_ordered_ids(build_app):
    build, _ = build_app
    app = build(FakeTurnEngine())

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            resp = await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "hi", "kind": "in_character"},
            )
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            projection = (await client.get(f"/api/sessions/{sid}/events")).json()
            return sid, resp.text, projection

    sid, text, projection = run(scenario())
    events, comments = parse_sse(text)

    assert comments == []
    assert [e["event"] for e in events] == [
        "reasoning",
        "content",
        "reasoning",
        "content",
        "done",
    ]
    assert [e["id"] for e in events] == [1, 2, 3, 4, 5]

    reasoning = "".join(
        e["data"]["delta"] for e in events if e["event"] == "reasoning"
    )
    content = "".join(
        e["data"]["delta"] for e in events if e["event"] == "content"
    )
    assert reasoning == "Weighing the scene and the last beat. Keeping the tone warm. "
    assert content == "Rain hammers the shutters."

    done = events[-1]["data"]
    assert done["type"] == "done"
    assert done["meta"]["session_id"] == sid
    assert done["meta"]["finish_reason"] == "stop"

    # Engines own narrative commits (post_turn for graph engines, the OOC
    # short-circuit for ooc); this fake commits none. The API is transport
    # plus exactly one user event at turn start — never a narrative duplicate.
    assert projection["narratives"] == []
    assert [m["payload"]["text"] for m in projection["user_messages"]] == ["hi"]


def test_stage_and_revision_frames_forward_as_named_sse_events(build_app):
    """D42/W8.2: the pump forwards ANY frame type as its own SSE event name.

    The engine's custom stream already carries ``stage`` (per-step progress)
    and ``revision`` (D41 critic patches); this proves they reach the browser as
    ``event: stage`` / ``event: revision`` lines before ``done`` — not just that
    the graph produced them.
    """
    build, _ = build_app
    script = [
        {
            "type": "stage",
            "meta": {
                "stage": "narrate",
                "label": "Writing the scene",
                "state": "start",
                "index": 1,
                "total": 6,
            },
        },
        {"type": "content", "delta": "Rain hammers "},
        {
            "type": "stage",
            "meta": {
                "stage": "narrate",
                "label": "Writing the scene",
                "state": "done",
                "index": 1,
                "total": 6,
            },
        },
        {
            "type": "revision",
            "meta": {
                "target_id": 3,
                "revision_id": 4,
                "verdict": "diffs",
                "reason": "repeats the closing image",
                "prose": "Rain still drums.",
                "dsl": "",
                "segments": [{"kind": "prose", "text": "Rain still drums."}],
            },
        },
        {"type": "done", "meta": {}},
    ]
    app = build(FakeTurnEngine(script))

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            resp = await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "hi", "kind": "in_character"},
            )
            assert resp.status_code == 200
            return resp.text

    text = run(scenario())
    events, _ = parse_sse(text)
    names = [e["event"] for e in events]
    assert names == ["stage", "content", "stage", "revision", "done"]
    assert names.index("stage") < names.index("done")
    assert names.index("revision") < names.index("done")
    assert events[0]["data"]["meta"]["stage"] == "narrate"
    assert events[0]["data"]["meta"]["state"] == "start"
    assert events[3]["data"]["meta"]["verdict"] == "diffs"


def test_concurrent_turn_returns_409(build_app):
    build, _ = build_app
    app = build(FakeTurnEngine(delay=0.2))

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            url = f"/api/sessions/{sid}/turns"
            first = asyncio.create_task(
                client.post(url, json={"content": "a", "kind": "in_character"})
            )
            await _await_active_turn(app, sid)
            second = await client.post(
                url, json={"content": "b", "kind": "in_character"}
            )
            first_resp = await first
            return second, first_resp.text

    second, first_text = run(scenario())

    assert second.status_code == 409
    assert second.json() == {"detail": "turn in progress"}
    # The rejected request never started a second turn; the first still finishes.
    events, _ = parse_sse(first_text)
    assert events[-1]["event"] == "done"


def test_stop_returns_202_and_terminates_with_interrupted(build_app):
    build, _ = build_app
    app = build(FakeTurnEngine(delay=0.3))

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            turn = asyncio.create_task(
                client.post(
                    f"/api/sessions/{sid}/turns",
                    json={"content": "a", "kind": "in_character"},
                )
            )
            await _await_active_turn(app, sid)
            stop = await client.post(f"/api/sessions/{sid}/stop")
            turn_resp = await turn
            return sid, stop, turn_resp.text

    sid, stop, text = run(scenario())
    events, _ = parse_sse(text)

    assert stop.status_code == 202
    assert [e["event"] for e in events][-1] == "error"
    assert events[-1]["data"]["meta"]["reason"] == "interrupted"
    assert all(e["event"] != "done" for e in events)
    # Interrupted turns are not committed as narrative (partial event policy).
    assert app.state.log.get_projection(sid)["narratives"] == []


def test_reconnect_replays_missed_suffix_then_continues_live(build_app):
    build, _ = build_app
    app = build(FakeTurnEngine(delay=0.4))

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            turn = asyncio.create_task(
                client.post(
                    f"/api/sessions/{sid}/turns",
                    json={"content": "a", "kind": "in_character"},
                )
            )
            # Wait until at least two chunks are buffered, then reconnect with
            # lastEventId=1: id 2 is a replayed suffix, ids 3+ arrive live.
            deadline = asyncio.get_running_loop().time() + 5
            while app.state.sessions[sid].buffer.last_id < 2:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            reconnect = await client.get(
                f"/api/sessions/{sid}/stream", params={"lastEventId": 1}
            )
            await turn
            return reconnect.text

    text = run(scenario())
    events, _ = parse_sse(text)

    assert [e["id"] for e in events] == [2, 3, 4, 5]
    assert [e["event"] for e in events] == [
        "content",
        "reasoning",
        "content",
        "done",
    ]


def test_projection_reflects_latest_wins_and_state_fold(build_app):
    build, log_path = build_app
    app = build(FakeTurnEngine())

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            # The log is truth: write directly on a second connection.
            other = EventLog(log_path)
            first = other.append_event(
                sid, "narrative", "narrator", {"text": "v1"}
            )
            other.append_revision(
                sid,
                first.id,
                "human",
                {"revised": {"text": "v2"}, "reason": "typo"},
            )
            other.append_event(
                sid, "state-delta", "extractor", {"time": "08:00", "loc": "camp"}
            )
            other.append_event(
                sid, "state-delta", "extractor", {"time": "09:00", "weather": "rain"}
            )
            other.close()
            resp = await client.get(f"/api/sessions/{sid}/events")
            return sid, resp.json()

    sid, body = run(scenario())

    assert body["session_id"] == sid
    assert [n["payload"]["text"] for n in body["narratives"]] == ["v2"]
    assert body["state"] == {"time": "09:00", "loc": "camp", "weather": "rain"}


def test_projection_endpoint_exposes_a_validation_notice_with_its_anchor(build_app):
    """D-F: the chat read carries each notice's anchor seq.

    The transcript renders the notice after the narrative that seq names, so the
    anchor must survive the HTTP shape — not just the Python projection.
    """
    build, log_path = build_app
    app = build(FakeTurnEngine())

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            # The log is truth: write directly on a second connection.
            other = EventLog(log_path)
            narrative = other.append_event(
                sid, "narrative", "narrator", {"text": "flagged"}
            )
            other.append_event(
                sid,
                "system",
                "validator",
                {
                    "event": "validation_notice",
                    "seq": narrative.seq,
                    "flags": [
                        {
                            "rule_id": "prose_markup",
                            "check": "format",
                            "severity": "warn",
                            "message": "raw HTML detected in prose",
                            "fix": "remove the raw markup from prose",
                        }
                    ],
                    "score": 75,
                },
            )
            other.close()
            resp = await client.get(f"/api/sessions/{sid}/events")
            return sid, narrative, resp.json()

    sid, narrative, body = run(scenario())

    assert body["session_id"] == sid
    assert body["notices"] == [
        {
            "seq": narrative.seq,
            "flags": [
                {
                    "rule_id": "prose_markup",
                    "check": "format",
                    "severity": "warn",
                    "message": "raw HTML detected in prose",
                    "fix": "remove the raw markup from prose",
                }
            ],
            "score": 75,
        }
    ]


def test_heartbeat_comments_do_not_corrupt_framing(build_app):
    build, _ = build_app
    app = build(FakeTurnEngine(delay=0.2), heartbeat=0.02)

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            resp = await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "a", "kind": "in_character"},
            )
            return resp.text

    text = run(scenario())
    events, comments = parse_sse(text)

    assert comments, "expected idle heartbeats between slow chunks"
    assert all(c == ": ping" for c in comments)
    assert [e["id"] for e in events] == [1, 2, 3, 4, 5]
    assert [e["event"] for e in events] == [
        "reasoning",
        "content",
        "reasoning",
        "content",
        "done",
    ]


def test_unknown_session_returns_404(build_app):
    build, _ = build_app
    app = build(FakeTurnEngine())

    async def scenario():
        async with client_for(app) as client:
            events = await client.get("/api/sessions/nope/events")
            turn = await client.post(
                "/api/sessions/nope/turns",
                json={"content": "a", "kind": "in_character"},
            )
            return events, turn

    events, turn = run(scenario())
    assert events.status_code == 404
    assert turn.status_code == 404


def test_create_session_rejects_unknown_body_fields(build_app):
    """An unknown create field is a 422 that names it — never a silent 200.

    Regression (live seeding): ``{"title", "world_id"}`` got a 200, the world id
    was dropped, and the session came back castless (no roster, no lanes) — a
    wrong-endpoint mistake read as a wiring bug. A world chat is
    ``POST /api/worlds/{world_id}/chats``.
    """
    build, _ = build_app
    app = build(FakeTurnEngine())

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions", json={"title": "Seeded", "world_id": "w1"}
            )

    resp = run(scenario())
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert [entry["loc"][-1] for entry in detail] == ["world_id"]
    assert not app.state.sessions


def test_create_session_accepts_the_documented_fields(build_app):
    build, _ = build_app
    app = build(FakeTurnEngine())
    from fabulist.personas.registry import append_persona

    persona_id = append_persona(app.state.log, {"name": "Narrator"})

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions",
                json={
                    "title": "Kept",
                    "persona_id": persona_id,
                    "memory_enabled": False,
                },
            )

    resp = run(scenario())
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]
    assert app.state.sessions[session_id].title == "Kept"


def test_stop_without_active_turn_is_accepted(build_app):
    build, _ = build_app
    app = build(FakeTurnEngine())

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            return await client.post(f"/api/sessions/{sid}/stop")

    resp = run(scenario())
    assert resp.status_code == 202
    assert resp.json()["status"] == "stopping"


def test_replay_buffer_is_bounded_and_ordered():
    buffer = ReplayBuffer(cap=2)
    for i in range(3):
        buffer.append("content", {"i": i})

    assert buffer.last_id == 3
    assert [item.id for item in buffer.after(0)] == [2, 3]
    assert buffer.after(3) == []
    assert buffer.after(1)[0].serialize() == 'id: 2\nevent: content\ndata: {"i":1}\n\n'


def test_second_turn_queues_behind_a_committed_tail(build_app):
    """W9.19 (A): after the commit barrier the next turn QUEUES, not 409s.

    The tail (validate/critic/bts) must finish before the next turn assembles
    its context, but the user already has the prose — refusing their next
    message would charge them waiting time for work they cannot see.
    """
    from fabulist.api.fake import FakeTurnEngine

    script = [
        {"type": "content", "delta": "The pawnshop settles. "},
        {"type": "committed", "meta": {}},
        {"type": "stage", "meta": {"stage": "critic", "state": "start"}},
        {"type": "stage", "meta": {"stage": "critic", "state": "done"}},
        {"type": "done", "meta": {}},
    ]
    build, _ = build_app
    app = build(FakeTurnEngine(script=script, delay=0.05))

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            url = f"/api/sessions/{sid}/turns"
            first = asyncio.create_task(
                client.post(url, json={"content": "a", "kind": "in_character"})
            )
            await _await_barrier(app, sid)
            second = await client.post(
                url, json={"content": "b", "kind": "in_character"}
            )
            first_resp = await first
            return sid, second, first_resp.text

    sid, second, first_text = run(scenario())

    assert second.status_code == 200
    second_events, _ = parse_sse(second.text)
    assert second_events[-1]["event"] == "done"
    # the first turn's stream ended cleanly too
    events, _ = parse_sse(first_text)
    assert events[-1]["event"] == "done"
    assert [e["event"] for e in events] == [
        "content", "committed", "stage", "stage", "done",
    ]


async def _await_barrier(app, sid, timeout: float = 5.0) -> None:
    """Block until the live turn's commit barrier fired (turn kept)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        turn = app.state.sessions[sid].turn
        if turn is not None and turn.committed.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("commit barrier never fired")


def test_stop_after_commit_is_a_quiet_done(build_app):
    """W9.19 (A): stopping past the barrier abandons background work only.

    The prose is kept; an `interrupted` error on a committed turn would read
    as a failure the user cannot act on. The stream ends with a done whose
    meta says the tail was stopped.
    """
    from fabulist.api.fake import FakeTurnEngine

    script = [
        {"type": "content", "delta": "The deal closes. "},
        {"type": "committed", "meta": {}},
        {"type": "stage", "meta": {"stage": "critic", "state": "start"}},
        {"type": "done", "meta": {}},
    ]
    build, _ = build_app
    app = build(FakeTurnEngine(script=script, delay=0.05))

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            url = f"/api/sessions/{sid}/turns"
            first = asyncio.create_task(
                client.post(url, json={"content": "a", "kind": "in_character"})
            )
            await _await_active_turn(app, sid)
            await _await_barrier(app, sid)
            stop = await client.post(f"/api/sessions/{sid}/stop")
            resp = await first
            return stop, resp.text

    stop, text = run(scenario())

    assert stop.status_code == 202
    events, _ = parse_sse(text)
    assert "error" not in [e["event"] for e in events]
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["meta"].get("stopped_after_commit") is True


# --- W9.20 §5.6: the per-turn stage-trace reset rule --------------------------------


def test_fold_stage_frame_skip_turn_first_frame_resets_the_trace():
    """A plan-SKIP turn opens at ``lanes`` with no plan step: the trace resets."""
    from fabulist.api.app import _fold_stage_frame

    # The per-turn list: ``_drive_turn`` allocates a fresh one every turn, so a
    # new turn starts empty and its first ``lanes start`` replaces nothing but
    # the (empty) trace.
    steps: list[dict] = []
    _fold_stage_frame(
        steps, {"stage": "lanes", "label": "Gathering", "state": "start", "index": 2}
    )
    assert [s["name"] for s in steps] == ["lanes"]

    # A trace with earlier steps and NO plan step is from this turn's own tail;
    # a second skip turn's first frame (a fresh list) behaves identically, and a
    # mid-list ``lanes start`` never fires (it is not a turn opener).
    _fold_stage_frame(
        steps, {"stage": "lanes", "label": "Gathering", "state": "done", "index": 2}
    )
    _fold_stage_frame(
        steps, {"stage": "narrate", "label": "Writing", "state": "start", "index": 3}
    )
    assert [s["name"] for s in steps] == ["lanes", "narrate"]

    fresh: list[dict] = []
    _fold_stage_frame(
        fresh, {"stage": "lanes", "label": "Gathering", "state": "start", "index": 2}
    )
    assert [s["name"] for s in fresh] == ["lanes"]


def test_fold_stage_frame_plan_turn_keeps_the_plan_step():
    """A planner turn's sequence keeps ``plan``; the mid-turn lanes start does NOT reset."""
    from fabulist.api.app import _fold_stage_frame

    steps: list[dict] = []
    for frame in (
        {"stage": "plan", "label": "Planning the story", "state": "start", "index": 1},
        {"stage": "plan", "label": "Planning the story", "state": "done", "index": 1},
        {"stage": "lanes", "label": "Gathering", "state": "start", "index": 2},
        {"stage": "lanes", "label": "Gathering", "state": "done", "index": 2},
        {"stage": "narrate", "label": "Writing", "state": "start", "index": 3},
    ):
        _fold_stage_frame(steps, frame)

    assert [s["name"] for s in steps] == ["plan", "lanes", "narrate"]
    assert steps[0]["state"] == "done"
    assert steps[2]["state"] == "start"


def test_fold_stage_frame_new_plan_turn_still_resets():
    """Back-to-back planner turns: the next ``plan start`` opens a fresh trace."""
    from fabulist.api.app import _fold_stage_frame

    steps: list[dict] = []
    for name, index in (("plan", 1), ("lanes", 2), ("narrate", 3)):
        _fold_stage_frame(
            steps, {"stage": name, "label": name, "state": "start", "index": index}
        )
    assert [s["name"] for s in steps] == ["plan", "lanes", "narrate"]

    _fold_stage_frame(
        steps, {"stage": "plan", "label": "Planning the story", "state": "start", "index": 1}
    )
    assert [s["name"] for s in steps] == ["plan"]
