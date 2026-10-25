"""W9.23 §3.6.7 — the historian's control plane (the four endpoints).

The router is exercised through the real FastAPI app (httpx ASGI transport) over
a real :class:`EventLog`; the run door drives a REAL
:class:`~fabulist.state.historian.HistorianRunner` over the REAL
:class:`~fabulist.graph.nodes.AssetProposalFanout`, so the off-turn path is the
code the engine runs (a fake lane generator/decision caller stands in for the
models, exactly like ``test_proposal_lanes``).

The pins:

* the config door is preset data — GET defaults to the code cadence, PUT
  validates (422 with reasons and NOTHING written), and the stored value is what
  later reads serve;
* the status read answers the drawer's questions (in flight? last run? due in how
  many turns?) from the chain, with or without a wired runner;
* the run door: 503 with no runner, 404 for an unknown session, 409 while a run
  is in flight, and the run's own counts otherwise;
* the BOUND is per CONNECTION, never per run: a lane call that hits its
  connection's ``timeout`` is a per-lane failure the door reports as a normal
  run result (``failed_lanes`` + the counts that did file, ``200``) on both
  entry paths — no error status, no failed run row, the guard released, and the
  cadence backed off;
* **no turn-slot claim** — a send issued while the historian works neither 409s
  nor waits (§3.6.7's decided distinction from the planner).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.graph.nodes import make_asset_proposals_node
from fabulist.log import EventLog
from fabulist.prompts.preset_store import active_preset, historian_config
from fabulist.state.historian import make_historian_runner
from fabulist.state.proposal_lanes import (
    SHOULD_THIS_PROPOSE_QUESTION_ID,
    SHOULD_WE_PROPOSE_QUESTION_ID,
)

SID = "s-historian-api"
WORLD_ID = "w_mill"
WORLD_NAME = "Miller's Bend"
WORLD_DESCRIPTION = "A river town."


def run(coro):
    return asyncio.run(coro)


# -- model fakes -------------------------------------------------------------


class FakeGenerator:
    """One canned JSON batch per ``<lane data_type="...">``."""

    def __init__(self, batches=None, *, delay: float = 0.0):
        self.batches = dict(batches or {})
        self.delay = delay
        self.calls = 0

    @staticmethod
    def lane_of(messages) -> str:
        for message in messages:
            text = str(message.get("content") or "")
            if "<lane data_type=" in text:
                return text.split('<lane data_type="', 1)[1].split('"', 1)[0]
        return ""

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls += 1
        await asyncio.sleep(self.delay)
        reply = self.batches.get(self.lane_of(messages)) or {"proposals": []}
        yield {
            "type": "content",
            "delta": reply if isinstance(reply, str) else json.dumps(reply),
        }


class FakeDecision:
    """Every judgment answers ``propose``."""

    async def ask(self, state, questions):
        question_id = next(iter(questions))
        probability = (
            0.9
            if question_id in (SHOULD_WE_PROPOSE_QUESTION_ID, SHOULD_THIS_PROPOSE_QUESTION_ID)
            else None
        )
        if probability is None:  # pragma: no cover - the stage asks only its two
            return None
        return {
            question_id: {
                "choice": "propose",
                "probabilities": {"propose": probability, "skip": 1 - probability},
            }
        }


def _world_amend_row():
    return {
        "kind": "world",
        "action": "amend",
        "target": WORLD_ID,
        "ops": [
            {
                "op": "modify",
                "path": "description",
                "old": WORLD_DESCRIPTION,
                "new": "A mill town on the bend of the river.",
            }
        ],
        "evidence": "The mill has stood a hundred years.",
    }


# -- harness -----------------------------------------------------------------


@pytest.fixture
def harness(tmp_path: Path):
    """Apps sharing one log path; closes the logs afterwards."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(**kwargs):
        app = create_app(FakeTurnEngine(), log_path, **kwargs)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _wire_historian(app, *, generator=None, cadence=10):
    """Wire a runner onto the app exactly as ``wiring.build_engine`` does."""
    runner = make_historian_runner(
        log=app.state.log,
        fanout=make_asset_proposals_node(
            log=app.state.log,
            caller=generator
            if generator is not None
            else FakeGenerator({"world": {"proposals": [_world_amend_row()]}}),
            decision=FakeDecision(),
        ),
        cadence=cadence,
    )
    app.state.historian = runner
    return runner


async def _session(client: httpx.AsyncClient) -> str:
    resp = await client.post("/api/sessions", json={"title": "Historian"})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def _seed_window(log: EventLog, session_id: str) -> None:
    """One committed turn + the world-state row its evidence needs."""
    from fabulist.worlds.registry import append_world

    append_world(
        log,
        {
            "world_id": WORLD_ID,
            "name": WORLD_NAME,
            "description": WORLD_DESCRIPTION,
        },
    )
    prose = "Elara says the mill has stood a hundred years."
    log.append_event(
        session_id,
        "narrative",
        "narrator",
        {"prose": prose, "segments": [{"kind": "prose", "text": prose}]},
    )
    log.append_event(
        session_id,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {},
            "venue": {WORLD_NAME: {"venue": "Miller's Bend", "area": "the mill"}},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )


def _rows(log: EventLog, session_id: str, variant: str) -> list[dict]:
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.type == "system" and (event.payload or {}).get("event") == variant
    ]


# -- the config door ---------------------------------------------------------


def test_get_serves_the_default_cadence(harness):
    build, _path = harness
    app = build()

    async def scenario():
        async with _client(app) as client:
            resp = await client.get("/api/prompts/historian")
            assert resp.status_code == 200
            return resp.json()

    assert run(scenario()) == {"historian": {"cadence": 10}}


def test_put_persists_the_cadence_and_a_later_get_serves_it(harness):
    build, _path = harness
    app = build()

    async def scenario():
        async with _client(app) as client:
            put = await client.put("/api/prompts/historian", json={"cadence": 3})
            get = await client.get("/api/prompts/historian")
            return put.json(), get.json()

    stored, served = run(scenario())
    assert stored == {"historian": {"cadence": 3}}
    assert served == {"historian": {"cadence": 3}}
    # The value lives on the preset — the same read the engine's live cadence
    # reader uses, so a Studio edit changes behaviour with no rebuild.
    assert historian_config(active_preset(app.state.log))["cadence"] == 3


def test_put_accepts_the_nested_envelope(harness):
    build, _path = harness
    app = build()

    async def scenario():
        async with _client(app) as client:
            return await client.put(
                "/api/prompts/historian", json={"historian": {"cadence": 7}}
            )

    assert run(scenario()).json() == {"historian": {"cadence": 7}}


def test_a_rejected_cadence_writes_nothing(harness):
    build, _path = harness
    app = build()

    async def scenario():
        async with _client(app) as client:
            bad = await client.put("/api/prompts/historian", json={"cadence": 0})
            bad_type = await client.put(
                "/api/prompts/historian", json={"cadence": "ten"}
            )
            get = await client.get("/api/prompts/historian")
            return bad, bad_type, get.json()

    bad, bad_type, served = run(scenario())
    assert bad.status_code == 422
    assert bad.json()["detail"]["errors"] == ["cadence must be >= 1, got 0"]
    assert bad_type.status_code == 422
    assert bad_type.json()["detail"]["errors"] == [
        "cadence must be an integer, got 'ten'"
    ]
    assert served == {"historian": {"cadence": 10}}


def test_the_historian_is_spawned_as_a_post_turn_hook(harness):
    """§3.6.8: the engine wires the historian beside the housekeeping hooks."""
    build, _path = harness
    seen: list[str] = []

    async def historian_hook(session_id: str) -> None:
        seen.append(session_id)

    app = build(historian_hook=historian_hook)

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            resp = await client.post(
                f"/api/sessions/{session_id}/turns",
                json={"content": "I ask about the mill.", "kind": "in_character"},
            )
            assert resp.status_code == 200
            # The hook runs in its own task after the turn closes; drain them.
            from fabulist.api.app import _BACKGROUND_TASKS

            deadline = asyncio.get_running_loop().time() + 5.0
            while _BACKGROUND_TASKS and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            return session_id

    assert run(scenario()) in seen


# -- the status read ---------------------------------------------------------


def test_status_without_a_runner_reads_the_log(harness):
    """The read never needs the runner; only the run door does."""
    build, _path = harness
    app = build()

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            resp = await client.get(f"/api/sessions/{session_id}/historian")
            return resp

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {
        "running": False,
        "last_run": None,
        "committed_turns": 0,
        "cadence": 10,
        "due_in": 10,
    }


def test_status_with_a_runner_and_no_runs_at_all_still_reads(harness):
    """A wired runner whose session has ZERO runs is the common case, not an error.

    The live defect this pins: the clock read crashed on ``None`` when a runner
    existed but no run had completed, the status GET 500ed, and the drawer read
    the 500 as "the engine has no historian endpoint". The clock is simply None
    there — the whole chain is uncovered and the first cadence run is ``due_in``
    away.
    """
    build, _path = harness
    app = build()
    _wire_historian(app)

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            return await client.get(f"/api/sessions/{session_id}/historian")

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is False
    assert body["last_run"] is None
    assert body["committed_turns"] == 0
    assert body["cadence"] == 10
    assert body["due_in"] == 10



def test_status_reports_the_last_run_and_the_due_budget(harness):
    build, _path = harness
    app = build()
    _wire_historian(app)
    log = app.state.log

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            for index in range(3):
                log.append_event(
                    session_id,
                    "narrative",
                    "narrator",
                    {"prose": f"Turn {index}."},
                )
            log.append_event(
                session_id,
                "system",
                "bts",
                {
                    "event": "historian_run",
                    "status": "finished",
                    "trigger": "cadence",
                    "turn_seq": 1,
                    "gate": "proceeded",
                    "filed": 3,
                    "dropped": 1,
                    "failed_lanes": [],
                    "duration_ms": 41230,
                },
            )
            return await client.get(f"/api/sessions/{session_id}/historian")

    body = run(scenario()).json()
    assert body["running"] is False
    assert body["committed_turns"] == 3
    assert body["cadence"] == 10
    assert body["due_in"] == 8  # 10 - (3 - 1)
    assert body["last_run"]["gate"] == "proceeded"
    assert (body["last_run"]["filed"], body["last_run"]["dropped"]) == (3, 1)
    assert body["last_run"]["trigger"] == "cadence"
    assert body["last_run"]["ts"]  # the row's own append timestamp


def test_status_reads_due_in_from_the_clock_not_the_missed_run(harness):
    """A missed run advances no clock: the drawer says "due now", never a budget.

    The trace (``last_run``) still shows the failed row — the run the owner last
    saw happen — but ``due_in`` must read the last run that CONSUMED its window
    (§3.6.3): at the boundary (committed 11, clock 1) the retry is due THIS turn,
    where the any-status read would have claimed 9 more turns of quiet.
    """
    build, _path = harness
    app = build()
    _wire_historian(app)
    log = app.state.log

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            log.append_event(
                session_id,
                "system",
                "bts",
                {
                    "event": "historian_run",
                    "status": "finished",
                    "trigger": "cadence",
                    "turn_seq": 1,
                    "gate": "proceeded",
                    "filed": 1,
                    "dropped": 0,
                    "failed_lanes": [],
                    "duration_ms": 9000,
                },
            )
            for index in range(11):
                log.append_event(
                    session_id,
                    "narrative",
                    "narrator",
                    {"prose": f"Turn {index}."},
                )
            log.append_event(
                session_id,
                "system",
                "bts",
                {
                    "event": "historian_run",
                    "status": "failed",
                    "trigger": "cadence",
                    "turn_seq": 10,
                    "error": "connection refused",
                },
            )
            return await client.get(f"/api/sessions/{session_id}/historian")

    body = run(scenario()).json()
    assert body["committed_turns"] == 11
    assert body["last_run"]["status"] == "failed"  # the trace shows the miss
    assert body["last_run"]["error"] == "connection refused"
    assert body["due_in"] == 0  # the clock is the finished run: the retry is due now


def test_status_404s_for_an_unknown_session(harness):
    build, _path = harness
    app = build()

    async def scenario():
        async with _client(app) as client:
            return await client.get("/api/sessions/nope/historian")

    assert run(scenario()).status_code == 404


# -- the run door ------------------------------------------------------------


def test_run_is_503_when_the_engine_wired_no_runner(harness):
    build, _path = harness
    app = build()

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            return await client.post(f"/api/sessions/{session_id}/historian/run")

    resp = run(scenario())
    assert resp.status_code == 503
    assert "no runner" in resp.json()["detail"]


def test_run_404s_for_an_unknown_session(harness):
    build, _path = harness
    app = build()
    _wire_historian(app)

    async def scenario():
        async with _client(app) as client:
            return await client.post("/api/sessions/nope/historian/run")

    assert run(scenario()).status_code == 404


def test_run_files_proposals_and_returns_the_runs_counts(harness):
    build, _path = harness
    app = build()
    _wire_historian(app)
    log = app.state.log

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            _seed_window(log, session_id)
            resp = await client.post(f"/api/sessions/{session_id}/historian/run")
            return session_id, resp

    session_id, resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["gate"] == "proceeded"
    assert (body["filed"], body["dropped"]) == (1, 0)
    assert body["failed_lanes"] == []
    assert body["turn_seq"] == 1
    assert isinstance(body["duration_ms"], int)
    assert [row["kind"] for row in _rows(log, session_id, "asset_proposal")] == [
        "world"
    ]
    assert [row["status"] for row in _rows(log, session_id, "historian_run")] == [
        "started",
        "finished",
    ]


def test_a_second_run_409s_while_one_is_in_flight(harness):
    """The double-run guard (§3.6.8) — not a turn lock."""
    build, _path = harness
    app = build()
    _wire_historian(app, generator=FakeGenerator(
        {"world": {"proposals": [_world_amend_row()]}}, delay=0.3
    ))
    log = app.state.log

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            _seed_window(log, session_id)
            first = asyncio.create_task(
                client.post(f"/api/sessions/{session_id}/historian/run")
            )
            await asyncio.sleep(0.05)
            second = await client.post(f"/api/sessions/{session_id}/historian/run")
            done = await first
            return second, done

    second, done = run(scenario())
    assert second.status_code == 409
    assert second.json()["detail"] == "historian already running"
    assert done.status_code == 200
    assert done.json()["filed"] == 1


def test_a_send_during_a_historian_run_neither_409s_nor_waits(harness):
    """THE no-turn-slot-claim pin (§3.6.7).

    The planner's runs hold the session turn slot because a plan steers the NEXT
    turn; the historian's output feeds nothing in the turn flow, so a send issued
    while it works must go straight through. Two assertions: the POST /turns is
    accepted (200, not 409) and it completes while the run is STILL in flight.
    """
    build, _path = harness
    app = build()
    _wire_historian(
        app,
        generator=FakeGenerator({"world": {"proposals": [_world_amend_row()]}}, delay=0.4),
    )
    log = app.state.log

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            _seed_window(log, session_id)
            historian_task = asyncio.create_task(
                client.post(f"/api/sessions/{session_id}/historian/run")
            )
            await asyncio.sleep(0.05)
            during = await client.post(
                f"/api/sessions/{session_id}/turns",
                json={"content": "I wait out the rain.", "kind": "in_character"},
            )
            still_running = app.state.historian.inflight(session_id)
            result = await historian_task
            return during, still_running, result

    during, still_running, result = run(scenario())
    assert during.status_code == 200
    assert "historian" not in during.text.lower()
    assert still_running is True  # the send did not wait for the run
    assert result.status_code == 200


# -- the per-connection bound ----------------------------------------------


class TimingOutGenerator(FakeGenerator):
    """A generator whose call for one lane raises the CONNECTION's timeout.

    The transport bound is the connection's ``timeout`` field; expiry raises out
    of the stream (the OpenAI client's ``APITimeoutError``), so a builtin
    ``TimeoutError`` here is what the lane seam sees. With no run-total bound,
    the run's own per-lane failure path is the whole containment.
    """

    def __init__(self, batches=None, *, late_lane: str):
        super().__init__(batches)
        self.late_lane = late_lane

    async def __call__(self, messages, stream=True, **kwargs):
        if self.lane_of(messages) == self.late_lane:
            raise TimeoutError("Request timed out.")
        async for event in super().__call__(messages, stream=stream, **kwargs):
            yield event


def test_a_lane_timeout_is_a_run_result_not_an_error_status(harness):
    """The door reports the run's counts; a slow LANE is not a failed run.

    There is no run-total cap to map to an error status: the lane that hit its
    connection's ``timeout`` lands in ``failed_lanes``, the lanes that answered
    still file, and the door answers ``200`` with those counts. The second call
    is a fresh run — not the in-flight ``409`` — which is what "the guard was
    released" means for the owner.
    """
    build, _path = harness
    app = build()
    runner = _wire_historian(
        app,
        generator=TimingOutGenerator(
            {"world": {"proposals": [_world_amend_row()]}}, late_lane="books"
        ),
    )
    log = app.state.log

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            _seed_window(log, session_id)
            first = await client.post(f"/api/sessions/{session_id}/historian/run")
            second = await client.post(f"/api/sessions/{session_id}/historian/run")
            return session_id, first, second

    session_id, first, second = run(scenario())
    assert first.status_code == 200
    body = first.json()
    assert body["gate"] == "proceeded"
    assert body["filed"] == 1  # the world lane's amendment
    assert [row["lane"] for row in body["failed_lanes"]] == ["books"]
    assert "timed out" in body["failed_lanes"][0]["reason"]
    assert "timed_out" not in body
    assert second.status_code == 200  # never the in-flight 409
    # Both runs closed as finished runs, with no failed row between them.
    rows = _rows(log, session_id, "historian_run")
    assert [row["status"] for row in rows] == ["started", "finished"] * 2
    assert runner.inflight(session_id) is False


def test_the_cadence_hook_contains_a_lane_timeout_the_same_way(harness):
    """The fire-and-forget path gets the SAME containment, and it stays quiet.

    The hook body is ``wiring.build_engine``'s three lines; what is pinned here
    is that a lane hitting its connection's timeout cannot raise into the hook's
    task (no bogus ``compaction_failed`` row), the run finishes with its own
    counts, the in-flight guard is released, and the STARTED stamp backs the
    cadence off one cycle.
    """
    build, _path = harness
    holder: dict = {}

    async def historian_hook(session_id: str) -> None:
        runner = holder["runner"]
        if runner.inflight(session_id):
            return
        if runner.is_due(session_id):
            await runner.run(session_id, trigger="cadence")

    app = build(historian_hook=historian_hook)
    runner = _wire_historian(
        app,
        generator=TimingOutGenerator(
            {"world": {"proposals": [_world_amend_row()]}}, late_lane="books"
        ),
        cadence=1,
    )
    holder["runner"] = runner
    log = app.state.log

    async def scenario():
        async with _client(app) as client:
            session_id = await _session(client)
            _seed_window(log, session_id)
            resp = await client.post(
                f"/api/sessions/{session_id}/turns",
                json={"content": "I ask about the mill.", "kind": "in_character"},
            )
            assert resp.status_code == 200
            from fabulist.api.app import _BACKGROUND_TASKS

            deadline = asyncio.get_running_loop().time() + 5.0
            while _BACKGROUND_TASKS and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            return session_id

    session_id = run(scenario())
    rows = _rows(log, session_id, "historian_run")
    assert [row["status"] for row in rows] == ["started", "finished"]
    assert [row["lane"] for row in rows[-1]["failed_lanes"]] == ["books"]
    assert rows[-1]["filed"] == 1
    assert runner.inflight(session_id) is False
    # The hook never raised into its post-turn task (a raise is recorded there).
    assert _rows(log, session_id, "compaction_failed") == []
    # ...and the clock advanced, so the next turn does not re-spawn.
    assert runner.is_due(session_id) is False
