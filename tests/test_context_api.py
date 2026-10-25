"""W9.26 §2.2/§2.7 — the compact doors, the job DTO and the context ledger.

The routers are exercised through a REAL FastAPI app over a REAL
:class:`~fabulist.log.EventLog` (httpx ASGI transport). The app is built here
with fakes for the seams Lane B constructs in ``build_app`` —
``app.state.compact_service`` / ``context_jobs`` / ``compaction_claims`` /
``turn_slot`` / ``registry`` — because this lane defines the CONSUMPTION shapes
and B owns the construction.

Pins: 202 + job id, the 409 ``{detail:{reason:"compacting"}}`` body, the
idempotent ``200`` successor, the four job DTO states, and the ledger's
declared-window / undeclared-window rows with their measured prompt tokens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from fabulist.api.context_api import LEDGER_TASKS, router as context_router
from fabulist.connections.registry import Connection, Registry
from fabulist.graph.compaction import RESERVE_TOKENS
from fabulist.graph.context_monitor import (
    COMPACTION_MARKER_EVENT,
    MANUAL_TRIGGER,
    NOOP_ALREADY_COMPACTED,
    CompactionClaims,
    ContextJobs,
    run_compact_job,
)
from fabulist.log import EventLog

SID = "s-api"
CHILD = "s-api-era2"
WINDOW = 20512
RESPONSE = 4000
BUDGET = WINDOW - RESPONSE - RESERVE_TOKENS


def run(coro):
    return asyncio.run(coro)


# --- harness ---------------------------------------------------------------


def connection(
    name="your-model-id", *, model="narrate-model", window=WINDOW, response=RESPONSE
) -> Connection:
    return Connection(
        name=name,
        base_url="http://localhost:4444/v1",
        model=model,
        context_window=window,
        sampling={} if response is None else {"max_tokens": response},
    )


def make_registry(*connections: Connection, agents=None) -> Registry:
    return Registry(
        connections={c.name: c for c in connections},
        agents=agents or {"narrator": connections[0].name},
    )


def add_usage(log, session, *, agent="narrator", model="narrate-model", prompt_tokens=12000):
    return log.append_event(
        session,
        "system",
        agent,
        {
            "event": "usage",
            "agent": agent,
            "model": model,
            "session_id": session,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 10,
            "total_tokens": prompt_tokens + 10,
            "latency_s": 0.5,
        },
    )


def add_turn(log, session):
    log.append_event(session, "user", "user", {"text": "I wait."})
    return log.append_event(
        session, "narrative", "narrator", {"prose": "Rain.", "dsl": "", "segments": []}
    )


def complete_era(log, session, *, child=CHILD, title="The Long Night"):
    head = max(event.seq for event in log.get_chain_events(session))
    summary = log.append_event(
        session,
        "summary",
        "summarizer",
        {
            "text": "SUMMARY",
            "covers_seqs": [1, head],
            "reason": "seeded",
            "trigger": MANUAL_TRIGGER,
            "stats": {"calls": 1, "prompt_tokens": 900, "model": "your-model-id"},
        },
    )
    log.fork_session(session, summary.seq, child)
    log.append_event(
        child,
        "system",
        "system",
        {
            "event": COMPACTION_MARKER_EVENT,
            "source_session": session,
            "summary_ref": [session, summary.seq],
            "covers_seqs": [1, head],
            "trigger": MANUAL_TRIGGER,
            "stats": {"calls": 1, "prompt_tokens": 900, "model": "your-model-id"},
        },
    )
    log.append_event(child, "system", "system", {"event": "session_renamed", "title": title})
    return child


class FakeService:
    """Contract-shaped fake of Lane A's ``compact()`` service."""

    def __init__(self, *, status="done", child=CHILD, stage=None, error=None, reason=None, block=False):
        self.status = status
        self.child = child
        self.stage = stage
        self.error = error
        self.reason = reason
        self.block = block
        self.release: asyncio.Event | None = None
        self.calls: list[dict] = []

    async def compact(self, session_id, log, *, trigger, job):
        self.calls.append({"session_id": session_id, "trigger": trigger})
        if self.block:
            self.release = asyncio.Event()
            await self.release.wait()
        if self.status == "done":
            complete_era(log, session_id, child=self.child)
            return {"status": "done", "successor_session_id": self.child}
        return {
            "status": self.status,
            "stage": self.stage,
            "error": self.error,
            "reason": self.reason,
        }


def build_app(
    log,
    *,
    service="default",
    registry=None,
    jobs=None,
    claims=None,
    slot=None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(context_router)
    app.state.log = log
    app.state.registry = registry if registry is not None else make_registry(connection())
    app.state.context_jobs = jobs if jobs is not None else ContextJobs()
    app.state.compaction_claims = claims if claims is not None else CompactionClaims()
    app.state.compact_service = FakeService() if service == "default" else service
    app.state.turn_slot = slot
    return app


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def log(tmp_path: Path):
    repo = EventLog(tmp_path / "events.db")
    yield repo
    repo.close()


# --- POST /compact ---------------------------------------------------------


def test_post_compact_returns_202_and_a_pollable_job(log):
    add_turn(log, SID)
    app = build_app(log)
    service = app.state.compact_service

    async def scenario():
        async with client_for(app) as client:
            response = await client.post(f"/api/sessions/{SID}/compact")
            assert response.status_code == 202
            body = response.json()
            assert body["job_id"]
            assert body["status"] == "running"
            assert body["session_id"] == SID

            job = app.state.context_jobs.get(body["job_id"])
            assert job is not None and job.trigger == MANUAL_TRIGGER
            await job.task  # the task is held on the job record

            polled = await client.get(
                f"/api/sessions/{SID}/context/compaction/{body['job_id']}"
            )
            return body, polled

    body, polled = run(scenario())
    assert polled.status_code == 200
    dto = polled.json()
    assert dto["status"] == "done"
    assert dto["session_id"] == CHILD
    assert dto["title"] == "The Long Night"
    assert len(service.calls) == 1
    assert service.calls[0]["trigger"] == MANUAL_TRIGGER


def test_post_compact_409s_while_the_claim_is_held(log):
    add_turn(log, SID)
    claims = CompactionClaims()
    claims.add(SID)
    app = build_app(log, claims=claims)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(f"/api/sessions/{SID}/compact")

    response = run(scenario())
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "compacting"}}
    assert app.state.context_jobs.records_for(SID) == []


def test_post_compact_409s_while_a_job_is_running(log):
    add_turn(log, SID)
    jobs = ContextJobs()
    running = jobs.create(SID, MANUAL_TRIGGER)
    app = build_app(log, jobs=jobs)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(f"/api/sessions/{SID}/compact")

    response = run(scenario())
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "compacting"}}
    assert running is not None


def test_post_compact_is_idempotent_given_a_successor(log):
    """A compaction child already exists ⇒ 200 with THAT successor, no new job."""
    add_turn(log, SID)
    complete_era(log, SID)
    app = build_app(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(f"/api/sessions/{SID}/compact")

    response = run(scenario())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["session_id"] == CHILD
    assert body["title"] == "The Long Night"
    assert body["job_id"] is None
    assert app.state.compact_service.calls == []
    assert app.state.context_jobs.records_for(SID) == []


def test_post_compact_on_an_era_child_with_a_new_turn_compacts(log):
    """THE LIVE REPRO through the door.

    An era child OWNS the marker of the compaction that created it and has one
    uncovered own turn: the door must start a real job (202, era 2 → era 3), not
    answer "nothing to do". The own-marker predicate used to swallow this.
    """
    add_turn(log, SID)
    add_turn(log, SID)
    child = complete_era(log, SID)
    add_turn(log, child)
    grandchild = "s-api-era3"
    app = build_app(log, service=FakeService(child=grandchild))
    service = app.state.compact_service

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(f"/api/sessions/{child}/compact")
            assert posted.status_code == 202, posted.text
            job_id = posted.json()["job_id"]
            await app.state.context_jobs.get(job_id).task
            return await client.get(
                f"/api/sessions/{child}/context/compaction/{job_id}"
            )

    dto = run(scenario()).json()
    assert dto["status"] == "done"
    assert dto["session_id"] == grandchild
    assert len(service.calls) == 1 and service.calls[0]["session_id"] == child


def test_the_door_200_and_the_runner_noop_share_one_predicate(log):
    """A superseded parent: the door answers 200, the runner answers noop — from
    the SAME predicate (``idempotent_successor``), naming the same successor."""
    add_turn(log, SID)
    complete_era(log, SID)
    app = build_app(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(f"/api/sessions/{SID}/compact")

    body = run(scenario()).json()
    assert body["status"] == "done" and body["job_id"] is None

    # The runner, entered directly (the race the door's first check misses).
    job = ContextJobs().create(SID, MANUAL_TRIGGER)
    result = run(
        run_compact_job(
            job,
            log=log,
            service=FakeService(),
            claims=CompactionClaims(),
        )
    )
    assert result.status == "noop"
    assert result.stage == NOOP_ALREADY_COMPACTED
    assert result.successor_session_id == body["session_id"] == CHILD


def test_post_compact_404s_for_an_unknown_session(log):
    app = build_app(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/sessions/nope/compact")

    assert run(scenario()).status_code == 404


def test_post_compact_503s_without_a_job_registry(log):
    add_turn(log, SID)
    app = build_app(log)
    app.state.context_jobs = None

    async def scenario():
        async with client_for(app) as client:
            return await client.post(f"/api/sessions/{SID}/compact")

    assert run(scenario()).status_code == 503


# --- the job DTO -----------------------------------------------------------


def test_job_dto_states(log):
    """running / done / failed / noop each serialize their own keys."""
    jobs = ContextJobs()
    job = jobs.create(SID, MANUAL_TRIGGER)
    app = build_app(log, jobs=jobs)

    async def poll(job_id):
        async with client_for(app) as client:
            return await client.get(f"/api/sessions/{SID}/context/compaction/{job_id}")

    running = run(poll(job.job_id))
    assert running.status_code == 200
    assert running.json() == {"session_id": SID, "status": "running"}

    job.status = "done"
    job.successor_session_id = CHILD
    job.title = "The Long Night"
    done = run(poll(job.job_id)).json()
    assert done["status"] == "done"
    assert done["session_id"] == CHILD
    assert done["title"] == "The Long Night"
    assert "error" not in done

    job.status = "failed"
    job.stage = "map"
    job.error = "summarizer returned no text"
    failed = run(poll(job.job_id)).json()
    assert failed["status"] == "failed"
    assert failed["stage"] == "map"
    assert failed["error"] == "summarizer returned no text"

    job.status = "noop"
    job.stage = "already_compacted"
    job.reason = "compaction cannot free more window"
    noop = run(poll(job.job_id)).json()
    assert noop["status"] == "noop"
    assert noop["reason"] == "compaction cannot free more window"


def test_the_runner_noop_names_the_same_successor_as_the_door(log):
    """The door's 200 and the runner's noop are ONE answer: same 2 era id.

    ``already_compacted`` carries the successor on the noop DTO too, so a client
    polling the job can follow the era exactly as it would from the 200.
    """
    add_turn(log, SID)
    complete_era(log, SID)
    jobs = ContextJobs()
    app = build_app(log, jobs=jobs)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(f"/api/sessions/{SID}/compact")

    door = run(scenario()).json()
    assert door["status"] == "done" and door["session_id"] == CHILD

    job = jobs.create(SID, MANUAL_TRIGGER)
    run(
        run_compact_job(
            job, log=log, service=FakeService(), claims=app.state.compaction_claims
        )
    )
    dto = job.dto()
    assert dto["status"] == "noop"
    assert dto["stage"] == NOOP_ALREADY_COMPACTED
    assert dto["session_id"] == door["session_id"] == CHILD
    assert "reason" in dto


def test_job_dto_of_a_failed_run_surfaces_the_error(log):
    add_turn(log, SID)
    service = FakeService(status="failed", stage="config", error="budget ≤ 0 — declare a Context Size")
    app = build_app(log, service=service)

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(f"/api/sessions/{SID}/compact")
            job_id = posted.json()["job_id"]
            await app.state.context_jobs.get(job_id).task
            return await client.get(f"/api/sessions/{SID}/context/compaction/{job_id}")

    dto = run(scenario()).json()
    assert dto["status"] == "failed"
    assert dto["stage"] == "config"
    assert "Context Size" in dto["error"]


def test_unknown_or_foreign_job_id_404s(log):
    add_turn(log, SID)
    jobs = ContextJobs()
    job = jobs.create("s-other", MANUAL_TRIGGER)
    app = build_app(log, jobs=jobs)

    async def scenario():
        async with client_for(app) as client:
            missing = await client.get(f"/api/sessions/{SID}/context/compaction/nope")
            foreign = await client.get(
                f"/api/sessions/{SID}/context/compaction/{job.job_id}"
            )
            return missing, foreign

    missing, foreign = run(scenario())
    assert missing.status_code == 404
    assert foreign.status_code == 404


# --- the ledger ------------------------------------------------------------


def test_ledger_rows_cover_every_task_surface(log):
    add_usage(log, SID, prompt_tokens=8000)
    app = build_app(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get(f"/api/sessions/{SID}/context")

    response = run(scenario())
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == SID
    labels = [row["task"] for row in body["tasks"]]
    assert labels == [task for task, _, _ in LEDGER_TASKS]
    assert "npc-lane" in labels and "summarizer" in labels

    narrate = next(row for row in body["tasks"] if row["task"] == "narrate")
    assert narrate["agent"] == "narrator"
    assert narrate["connection"] == "your-model-id"
    assert narrate["model"] == "narrate-model"
    assert narrate["context_window"] == WINDOW
    assert narrate["window_declared"] is True
    assert narrate["response_length"] == RESPONSE
    assert narrate["budget_tokens"] == BUDGET
    assert narrate["prompt_tokens"] == 8000
    assert narrate["ratio"] == round(8000 / BUDGET, 4)
    assert narrate["basis"] == "measured"
    assert "note" not in narrate


def test_ledger_marks_undeclared_windows_and_measured_only(log):
    """⚑C: no Context Size ⇒ an explicit "window undeclared" row, no estimate."""
    reg = make_registry(connection(window=None))
    add_turn(log, SID)  # a session known to the log, with no usage rows at all
    app = build_app(log, registry=reg)

    async def scenario():
        async with client_for(app) as client:
            return await client.get(f"/api/sessions/{SID}/context")

    rows = run(scenario()).json()["tasks"]
    for row in rows:
        assert row["window_declared"] is False
        assert row["context_window"] is None
        assert row["budget_tokens"] is None
        assert row["ratio"] is None
        assert row["note"] == "window undeclared"
        # Measured-only (⚑R1a): no prompt tokens means no number, never a guess.
        assert row["prompt_tokens"] is None


def test_ledger_reads_the_last_measured_row_per_slot(log):
    """Each task reports ITS OWN slot's newest usage row (not the narrator's)."""
    reg = make_registry(
        connection(),
        connection("sum", model="summ-model", window=32768, response=8192),
        agents={"narrator": "your-model-id", "summarizer": "sum", "extractor": "your-model-id"},
    )
    add_usage(log, SID, agent="narrator", prompt_tokens=5000)
    add_usage(log, SID, agent="summarizer", model="summ-model", prompt_tokens=20000)
    add_usage(log, SID, agent="narrator", prompt_tokens=6000)
    app = build_app(log, registry=reg)

    async def scenario():
        async with client_for(app) as client:
            return await client.get(f"/api/sessions/{SID}/context")

    rows = {row["task"]: row for row in run(scenario()).json()["tasks"]}
    assert rows["narrate"]["prompt_tokens"] == 6000
    assert rows["ooc"]["prompt_tokens"] == 6000  # same narrator slot
    summarizer = rows["summarizer"]
    assert summarizer["agent"] == "summarizer"
    assert summarizer["connection"] == "sum"
    assert summarizer["prompt_tokens"] == 20000
    assert summarizer["context_window"] == 32768
    assert summarizer["response_length"] == 8192
    assert summarizer["budget_tokens"] == 32768 - 8192 - RESERVE_TOKENS
    assert summarizer["ratio"] == round(20000 / (32768 - 8192 - RESERVE_TOKENS), 4)


def test_ledger_narrate_row_matches_the_gauge_label_union(log):
    """The Inspector's narrate row accepts the same dialect aliases as the gauge."""
    add_usage(log, SID, agent="narrate", prompt_tokens=7000)
    app = build_app(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get(f"/api/sessions/{SID}/context")

    rows = {row["task"]: row for row in run(scenario()).json()["tasks"]}
    assert rows["narrate"]["prompt_tokens"] == 7000
    # ... but a SUMMARIZER row is never answered by a narrator row's label.
    assert rows["summarizer"]["prompt_tokens"] is None


def test_ledger_404s_for_an_unknown_session(log):
    app = build_app(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/nope/context")

    assert run(scenario()).status_code == 404


def test_ledger_route_does_not_shadow_the_job_route(log):
    """``/context/compaction/{job_id}`` and ``/context`` are distinct routes."""
    add_turn(log, SID)
    jobs = ContextJobs()
    job = jobs.create(SID, MANUAL_TRIGGER)
    app = build_app(log, jobs=jobs)

    async def scenario():
        async with client_for(app) as client:
            ledger = await client.get(f"/api/sessions/{SID}/context")
            status = await client.get(
                f"/api/sessions/{SID}/context/compaction/{job.job_id}"
            )
            return ledger, status

    ledger, status = run(scenario())
    assert ledger.status_code == 200 and "tasks" in ledger.json()
    assert status.status_code == 200 and "status" in status.json()
