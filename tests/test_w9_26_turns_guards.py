"""W9.26 §2.3 step 1 / §2.6 — the two ``POST /turns`` guards, and the seams.

Two refusals protect an era boundary, both with the EXACT bodies the frontend
parses (``{detail:{reason, id?}}``, §3's binding contract):

* ``compacting`` — a compaction job holds the session claim, so the ask belongs
  to whatever era the job is about to create. The check is the handler's FIRST
  statement and the claim+slot pair is one await-free block (§2.3 step 1's
  done/claim race).
* ``superseded`` — the session has a compaction successor, so the turn is
  refused and the client follows ``id`` (TRANSITIVELY: an era can be compacted
  again) and replays its ask there. Ordinary swipe forks stay turnable: only a
  marker-bearing (compaction) child makes a parent superseded.

The last section pins what ``wiring.build_app`` CONSTRUCTS, since the design
makes those ``app.state`` seams Lane B's: the compact service with the real
summarizer caller + §2.3 step-2 budget, the job registry, the claim registry and
the turn-slot adapter — plus the retired ``enable_compaction`` flag being gone.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

import fabulist.wiring as wiring
from fabulist.api import create_app
from fabulist.api.app import compaction_successor
from fabulist.api.fake import FakeTurnEngine
from fabulist.connections.registry import Registry
from fabulist.graph.compaction import CompactService
from fabulist.graph.context_monitor import (
    COMPACTION_MARKER_EVENT,
    CompactionClaims,
    ContextJobs,
    PollingTurnSlot,
)
from fabulist.log import EventLog

SID = "s-guards"
ERA2 = "s-guards-era2"
ERA3 = "s-guards-era3"


def run(coro):
    return asyncio.run(coro)


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.fixture
def log(tmp_path: Path):
    repo = EventLog(tmp_path / "events.db")
    yield repo
    repo.close()


@pytest.fixture
def app_factory(tmp_path: Path):
    """Apps sharing one tmp log path (create_app + a scripted engine)."""
    log_path = tmp_path / "guards.db"
    built: list = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


async def _create_session(client) -> str:
    resp = await client.post("/api/sessions", json={"title": "Guards"})
    assert resp.status_code == 200
    return resp.json()["session_id"]


async def _await_barrier(app, sid: str, timeout: float = 5.0) -> None:
    """Block until the live turn's commit barrier fired (turn kept)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        turn = app.state.sessions[sid].turn
        if turn is not None and turn.committed.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("commit barrier never fired")


def _seed_era(log: EventLog, parent: str, child: str, *, title: str = "Era") -> None:
    """Seed one compaction era exactly as Lane A's step 8/9 writes it."""
    log.append_event(parent, "user", "user", {"text": "I wait."})
    log.append_event(
        parent, "narrative", "narrator", {"prose": "Rain.", "dsl": "", "segments": []}
    )
    head = max(event.seq for event in log.get_chain_events(parent))
    summary = log.append_event(
        parent,
        "summary",
        "summarizer",
        {"text": "SUMMARY", "covers_seqs": [1, head], "trigger": "manual"},
    )
    log.fork_session(parent, summary.seq, child)
    log.append_event(
        child,
        "system",
        "system",
        {
            "event": COMPACTION_MARKER_EVENT,
            "source_session": parent,
            "summary_ref": [parent, summary.seq],
            "covers_seqs": [1, head],
            "trigger": "manual",
        },
    )
    log.append_event(child, "system", "system", {"event": "session_renamed", "title": title})


# --- the `compacting` 409 ---------------------------------------------------


def test_a_claimed_session_refuses_a_turn_with_the_pinned_body(app_factory):
    app = app_factory()
    claims = CompactionClaims()
    app.state.compaction_claims = claims

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            claims.add(sid)
            return sid, await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "hi", "kind": "in_character"},
            )

    sid, response = run(scenario())
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "compacting"}}
    # No turn started behind the refusal: no slot claimed, no user row written.
    assert app.state.sessions[sid].turn is None
    assert all(
        event.type != "user" for event in app.state.log.get_events(sid)
    )


def test_the_claim_is_checked_before_the_in_flight_turn_refusal(app_factory):
    """Order matters (§2.3 step 1): the claim wins over ``turn in progress``.

    A job that is about to fork the session makes the streaming turn the LAST
    turn of this era — "compacting" is the answer the client acts on.
    """
    from fabulist.api.fake import FakeTurnEngine

    slow = [
        {"type": "content", "delta": "The rain "},
        {"type": "content", "delta": "keeps falling."},
        {"type": "done", "meta": {}},
    ]
    app = app_factory(FakeTurnEngine(script=slow, delay=0.5))
    app.state.compaction_claims = CompactionClaims()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            first = asyncio.create_task(
                client.post(
                    f"/api/sessions/{sid}/turns",
                    json={"content": "a", "kind": "in_character"},
                )
            )
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                turn = app.state.sessions[sid].turn
                if turn is not None and not turn.done.is_set():
                    break
                await asyncio.sleep(0.01)
            app.state.compaction_claims.add(sid)  # the job claims mid-turn
            second = await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "b", "kind": "in_character"},
            )
            await first
            return second

    response = run(scenario())
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "compacting"}}


def test_a_claim_taken_while_a_turn_request_waits_is_still_honored(app_factory):
    """The post-wait re-check: the wait can outlast the claim-free window.

    A committed turn's background tail can take seconds, and the request that
    queued behind it must not start a turn on a session a compaction has claimed
    in the meantime (it would race the fork).
    """
    release: dict[str, asyncio.Event] = {}

    class _TailEngine:
        """Yields the commit barrier, then holds the tail until released."""

        async def run_turn(self, session_id, content, kind, **kwargs):
            gate = release.setdefault("gate", asyncio.Event())
            yield {"type": "content", "delta": "Kept prose. "}
            yield {"type": "committed", "meta": {}}
            await gate.wait()
            yield {"type": "done", "meta": {}}

    app = app_factory(_TailEngine())
    app.state.compaction_claims = CompactionClaims()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            url = f"/api/sessions/{sid}/turns"
            first = asyncio.create_task(
                client.post(url, json={"content": "a", "kind": "in_character"})
            )
            await _await_barrier(app, sid)
            queued = asyncio.create_task(
                client.post(url, json={"content": "b", "kind": "in_character"})
            )
            # Let the queued request reach `prior_turn.done.wait()`.
            for _ in range(20):
                await asyncio.sleep(0.01)
            app.state.compaction_claims.add(sid)
            release.setdefault("gate", asyncio.Event()).set()
            await first
            return await queued

    response = run(scenario())
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "compacting"}}


def test_an_unclaimed_session_still_takes_a_turn(app_factory):
    app = app_factory()
    app.state.compaction_claims = CompactionClaims()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            return await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "hi", "kind": "in_character"},
            )

    assert run(scenario()).status_code == 200


# --- the `superseded` 409 ---------------------------------------------------


def test_a_compacted_parent_refuses_a_turn_and_names_the_successor(app_factory):
    app = app_factory()
    log = app.state.log
    _seed_era(log, SID, ERA2)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                f"/api/sessions/{SID}/turns",
                json={"content": "carry on", "kind": "in_character"},
            )

    response = run(scenario())
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "superseded", "id": ERA2}}


def test_the_successor_is_the_transitive_tip(app_factory):
    """``A -> B -> C`` answers ``C``: the client must land on the live era."""
    app = app_factory()
    log = app.state.log
    _seed_era(log, SID, ERA2, title="First era")
    log.append_event(ERA2, "user", "user", {"text": "Still going."})
    log.append_event(
        ERA2, "narrative", "narrator", {"prose": "More rain.", "dsl": "", "segments": []}
    )
    head = max(event.seq for event in log.get_chain_events(ERA2))
    summary = log.append_event(
        ERA2,
        "summary",
        "summarizer",
        {"text": "SUMMARY 2", "covers_seqs": [1, head], "trigger": "manual"},
    )
    log.fork_session(ERA2, summary.seq, ERA3)
    log.append_event(
        ERA3,
        "system",
        "system",
        {
            "event": COMPACTION_MARKER_EVENT,
            "source_session": ERA2,
            "summary_ref": [ERA2, summary.seq],
            "covers_seqs": [1, head],
            "trigger": "manual",
        },
    )

    assert compaction_successor(log, SID) == ERA3
    assert compaction_successor(log, ERA2) == ERA3
    # The tip itself is turnable.
    assert compaction_successor(log, ERA3) is None


def test_an_ordinary_swipe_fork_never_supersedes_its_parent(app_factory):
    """Only a MARKER-bearing child makes a parent read-only (§2.6).

    Forking readable history is reading — a swipe branch must not lock the turn
    door on the session it was forked from.
    """
    app = app_factory()
    log = app.state.log
    log.append_event(SID, "user", "user", {"text": "I wait."})
    log.append_event(
        SID, "narrative", "narrator", {"prose": "Rain.", "dsl": "", "segments": []}
    )
    log.fork_session(SID, at_seq=1, new_session_id="swipe")

    assert compaction_successor(log, SID) is None

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                f"/api/sessions/{SID}/turns",
                json={"content": "again", "kind": "in_character"},
            )

    assert run(scenario()).status_code == 200


def test_a_session_without_children_is_never_superseded(log):
    log.append_event(SID, "user", "user", {"text": "hi"})
    assert compaction_successor(log, SID) is None


# --- what build_app constructs (the design's binding contract) --------------


def _registry() -> Registry:
    return Registry.from_dict(
        {
            "connections": {
                "c": {
                    "base_url": "http://localhost:4444/v1",
                    "model": "your-model-id",
                    "sampling": {"max_tokens": 16384},
                },
                "narr": {
                    "base_url": "http://localhost:4444/v1",
                    "model": "narration-model",
                    "context_window": 65536,
                    "sampling": {"max_tokens": 2048},
                },
            },
            "agents": {"narrator": "narr", "summarizer": "c"},
        }
    )


def test_build_app_constructs_the_four_shared_seams(tmp_path):
    app = wiring.build_app(registry=_registry(), log_path=tmp_path / "seams.db")
    try:
        assert isinstance(app.state.compact_service, CompactService)
        assert isinstance(app.state.context_jobs, ContextJobs)
        assert isinstance(app.state.compaction_claims, CompactionClaims)
        assert isinstance(app.state.turn_slot, PollingTurnSlot)
        # §2.3 step 2: the summarizer connection declares no window, so the chain
        # falls through to the NARRATE connection's declared 65536; budget =
        # window − the summarizer's OWN response length − RESERVE_TOKENS.
        assert app.state.compact_service._budget_tokens == 65536 - 16384 - 512
        assert app.state.compact_service._model == "your-model-id"
        assert app.state.compact_service._summarizer is not None
        assert app.state.compact_service._messages_builder is not None
    finally:
        app.state.log.close()


def test_build_app_registers_four_hooks_naming_the_monitor_and_no_compactor(
    tmp_path, monkeypatch
):
    """The W9.26 hook set: the monitor takes the retired compactor's slot.

    ``memory``, ``assistant`` and ``historian`` keep their existing slots; the
    W2.9 threshold auto-hook is GONE (§2.8) and its ``enable_compaction``
    flag is retired with it — no ``compact_hook`` is passed at all.
    """
    captured: dict = {}
    real = wiring.create_app

    def spy(engine, log_path, **kwargs):
        captured.update(kwargs)
        return real(engine, log_path, **kwargs)

    monkeypatch.setattr(wiring, "create_app", spy)

    app = wiring.build_app(
        registry=_registry(), log_path=tmp_path / "hooks.db"
    )
    try:
        assert "compact_hook" not in captured
        assert {name: getattr(hook, "__name__", None) for name, hook in captured.items()} == {
            "context_monitor_hook": "context_monitor",
            "memory_hook": "memory_hook",
            "assistant_hook": "assistant_turn_hook",
            "historian_hook": "historian_hook",
        }
        # Every hook is wired (not None): the wave-1 enable flag no longer gates
        # anything.
        assert all(callable(hook) for hook in captured.values())
    finally:
        app.state.log.close()


def _summary_budget(agents: dict, connections: dict) -> int:
    from fabulist.wiring import summarizer_budget_tokens

    registry = Registry.from_dict({"connections": connections, "agents": agents})
    log_path = Path(tempfile.mkdtemp()) / "budget.db"
    log = EventLog(log_path)
    try:
        return summarizer_budget_tokens(registry, log)
    finally:
        log.close()


def test_the_summarizer_budget_follows_the_three_rung_fallback_chain():
    """§2.3 step 2: summarizer declared ▸ narrate declared ▸ a SCALED floor.

    The floor must scale: a flat 8192 against the live summarizer binding
    (``max_tokens=16384``) computes a NEGATIVE budget, which reads as a
    declaration failure that has nothing to do with the owner's config.
    """
    from fabulist.connections.client import DEFAULT_SAMPLING

    summarizer = {"base_url": "http://localhost:4444/v1", "model": "s",
                  "sampling": {"max_tokens": 16384}}
    narrator = {"base_url": "http://localhost:4444/v1", "model": "n",
                "sampling": {"max_tokens": 2048}}
    agents = {"narrator": "narr", "summarizer": "sum"}

    # 1. the summarizer's own declaration wins.
    budget = _summary_budget(
        agents,
        {
            "sum": {**summarizer, "context_window": 32768},
            "narr": {**narrator, "context_window": 65536},
        },
    )
    assert budget == 32768 - 16384 - 512

    # 2. ...else the narrate connection's (execution order, not ownership).
    budget = _summary_budget(
        agents,
        {"sum": summarizer, "narr": {**narrator, "context_window": 65536}},
    )
    assert budget == 65536 - 16384 - 512

    # 3. ...else max(8192, response_length + 4096), i.e. positive by construction.
    budget = _summary_budget(agents, {"sum": summarizer, "narr": narrator})
    assert budget == 20480 - 16384 - 512
    assert budget > 0
    # An unauthored sampler still gets the real default response length.
    default_response = int(DEFAULT_SAMPLING["max_tokens"])
    assert _summary_budget(agents, {"sum": {"base_url": "http://x/v1", "model": "s"},
                                    "narr": narrator}) == (
        max(8192, default_response + 4096) - default_response - 512
    )


def test_the_turn_slot_reads_the_live_turn_registry(tmp_path):
    """The slot adapter polls ``api/app.py``'s own ``_Session.turn``/``done``."""
    from fabulist.api.app import _Session, _Turn

    app = wiring.build_app(registry=_registry(), log_path=tmp_path / "slot.db")
    try:
        sessions = app.state.sessions
        sid = "slot-session"
        sessions[sid] = _Session(sid, "Slot")
        assert app.state.turn_slot.busy(sid) is False
        turn = _Turn()
        sessions[sid].turn = turn
        assert app.state.turn_slot.busy(sid) is True
        turn.done.set()
        assert app.state.turn_slot.busy(sid) is False
        assert app.state.turn_slot.busy("unknown") is False
    finally:
        app.state.log.close()


def test_the_context_router_is_mounted_on_the_real_app(tmp_path):
    """§3: `create_app` mounts Lane D's router (the ledger answers on :2727)."""
    app = wiring.build_app(registry=_registry(), log_path=tmp_path / "routes.db")

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            ledger = await client.get(f"/api/sessions/{sid}/context")
            job = await client.get(f"/api/sessions/{sid}/context/compaction/nope")
            return ledger, job

    ledger, job = run(scenario())
    try:
        assert ledger.status_code == 200
        assert [row["task"] for row in ledger.json()["tasks"]][:2] == ["narrate", "ooc"]
        assert job.status_code == 404
    finally:
        app.state.log.close()


def test_the_turns_guard_reads_the_registry_build_app_constructed(tmp_path):
    """The claim the wiring builds is the claim the turn door honors (§3)."""
    app = wiring.build_app(registry=_registry(), log_path=tmp_path / "guard.db")

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            app.state.compaction_claims.add(sid)
            return await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "hi", "kind": "in_character"},
            )

    response = run(scenario())
    try:
        assert response.status_code == 409
        assert response.json() == {"detail": {"reason": "compacting"}}
    finally:
        app.state.log.close()


# --- the seams COMPOSE: wiring + Lane A's service + Lane D's monitor --------


def test_the_wiring_built_seams_drive_a_forced_compaction_end_to_end(tmp_path):
    """A measured ≥90% reading forces a REAL compaction through the real seams.

    Nothing here is Lane A's or Lane D's business: this is the integration the
    design assigns to ``build_app`` — the monitor hook receives the SAME service,
    job registry, claim registry and turn slot that land on ``app.state``, so a
    forced run writes a summary, forks an era and releases the claim. Only the
    MODEL is faked (the summarizer seam), because a test must not call :4444.
    """
    registry = Registry.from_dict(
        {
            "connections": {
                "c": {
                    "base_url": "http://localhost:4444/v1",
                    "model": "your-model-id",
                    "sampling": {"max_tokens": 1000},
                },
                "narr": {
                    "base_url": "http://localhost:4444/v1",
                    "model": "narration-model",
                    "context_window": 20000,
                    "sampling": {"max_tokens": 1000},
                },
            },
            "agents": {"narrator": "narr", "summarizer": "c"},
        }
    )
    captured: dict = {}
    real = wiring.create_app

    def spy(engine, log_path, **kwargs):
        captured.update(kwargs)
        return real(engine, log_path, **kwargs)

    wiring.create_app = spy
    try:
        app = wiring.build_app(registry=registry, log_path=tmp_path / "forced.db")
    finally:
        wiring.create_app = real

    log = app.state.log
    try:
        sid = "forced-session"
        log.append_event(sid, "system", "system", {"event": "session_created"})
        log.append_event(sid, "user", "user", {"text": "I wait in the rain."})
        log.append_event(
            sid, "narrative", "narrator", {"prose": "Rain.", "dsl": "", "segments": []}
        )
        log.append_event(
            sid,
            "system",
            "narrator",
            {
                "event": "usage",
                "agent": "narrator",
                "model": "narration-model",
                "session_id": sid,
                "prompt_tokens": 18000,  # 18000 / (20000 − 1000 − 512) ≈ 0.97
                "completion_tokens": 10,
                "total_tokens": 18010,
                "latency_s": 0.2,
            },
        )
        # The model seam only: the call is the ONE thing a unit test may not make.
        app.state.compact_service._summarizer = lambda messages: "SUMMARY OF ERA ONE"

        run(captured["context_monitor_hook"](sid))

        events = log.get_events(sid)
        summaries = [e for e in events if e.type == "summary"]
        assert len(summaries) == 1
        assert summaries[0].payload["text"] == "SUMMARY OF ERA ONE"
        assert summaries[0].payload["trigger"] == "forced_90"
        successor = log.list_branches(sid)
        assert len(successor) == 1
        assert COMPACTION_MARKER_EVENT in {
            (e.payload or {}).get("event") for e in log.get_events(successor[0])
        }
        # The claim is released (the job's `finally`) — the next turn is legal,
        # on the SUCCESSOR.
        assert not app.state.compaction_claims
        assert app.state.compaction_claims.held(sid) is False
        # The gauge also filed its 80% notice on the way through.
        assert any(
            (e.payload or {}).get("event") == "context_low" for e in events
        )
    finally:
        log.close()
