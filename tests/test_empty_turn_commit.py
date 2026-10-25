"""Issue #46 — an empty turn must never commit a narrative row.

The defect (deploy experiment run 2): closing the browser mid-generation left a
COMMITTED turn whose payload was ``prose: "", dsl: "", segments: [...]`` with
only reasoning. The transcript rendered an envelope plus "thinking…" and every
later reader (history passback, compaction, recall) saw a scene that did not
exist.

Two boundaries are pinned here:

* the CLIENT DISCONNECT path — an SSE consumer that goes away does NOT cancel
  the producer (the turn is buffered for Last-Event-ID reconnect), so the turn
  still reaches ``commit``; the commit guard is what refuses an empty one;
* the COMMIT seam — a payload with no non-whitespace prose and no DSL appends an
  explicit ``turn_aborted`` system row and raises, so the narrative row and the
  post-commit tail never happen. The session stays playable.

A non-empty turn is untouched: a stream that ends after prose commits that
prose, and a cancellation that lands after prose aborts cleanly (the existing
cancel contract keeps only an ephemeral partial, never a committed row).
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

from fabulist.api import create_app
from fabulist.graph import GraphTurnEngine, build_graph, open_sqlite_checkpointer
from fabulist.graph.nodes import _payload_is_empty
from fabulist.log import EventLog


# --- harness -----------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


class _NoExtractor:
    """Extractor seam: the abort path must never reach it, non-empty turns may."""

    async def __call__(self, user_msg, prose):
        return {"deltas": {}, "action_class": "dialogue"}


class ScriptedModel:
    """A ``ModelCaller`` with one script per call (script may be a coroutine fn)."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.scripts) - 1)
        script = self.scripts[index]
        async for event in script():
            yield event


class _StallingEmpty:
    """First call: reasoning then BLOCK; second call: one clean prose sentence.

    The block is the mid-stream window a browser close lands in. It ends with a
    reasoning-only completion (no content, no error frame) — the exact shape the
    deploy log recorded.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def first(self):
        yield {"type": "reasoning", "delta": "Weighing the scene, then the browser closes."}
        self.started.set()
        await self.release.wait()
        yield {"type": "finish", "finish_reason": "stop"}

    async def second(self):
        yield {"type": "content", "delta": "The lamp gutters, then steadies."}
        yield {"type": "finish", "finish_reason": "stop"}


@pytest.fixture
def graph_app(tmp_path):
    """Build an API app over a REAL graph + fake model; close both log handles."""
    built: list[tuple[object, EventLog]] = []
    counter = 0

    def _build(model) -> tuple[object, EventLog]:
        nonlocal counter
        counter += 1
        log_path = tmp_path / f"events-{counter}.db"
        log = EventLog(log_path)
        graph = build_graph(model=model, extractor=_NoExtractor(), log=log)
        engine = GraphTurnEngine(graph=graph, log=log, model=model)
        app = create_app(engine, log_path)
        built.append((app, log))
        return app, log

    yield _build
    for app, log in built:
        with contextlib.suppress(Exception):
            app.state.log.close()
        with contextlib.suppress(Exception):
            log.close()


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create_session(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/sessions", json={"title": "issue-46"})
    assert response.status_code == 200
    return response.json()["session_id"]


def _abort_rows(log: EventLog, session_id: str) -> list:
    return [
        event
        for event in log.get_events(session_id)
        if event.type == "system" and event.payload.get("event") == "turn_aborted"
    ]


def _narratives(log: EventLog, session_id: str) -> list:
    return [event for event in log.get_events(session_id) if event.type == "narrative"]


# --- the guard's definition --------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        {"narrative_parts": [], "dsl": "", "segments": []},
        {"narrative_parts": [""], "dsl": "", "segments": []},
        {"narrative_parts": ["   \n\t"], "dsl": " ", "segments": []},
        {
            "narrative_parts": [""],
            "dsl": "",
            "segments": [{"kind": "prose", "text": "   "}],
        },
        {
            "narrative_parts": [],
            "dsl": "",
            "segments": [{"kind": "prose", "text": ""}],
        },
    ],
)
def test_payload_is_empty_for_no_prose_and_no_dsl(state):
    assert _payload_is_empty(state) is True


@pytest.mark.parametrize(
    "state",
    [
        {"narrative_parts": ["The rain falls."], "dsl": "", "segments": []},
        {"narrative_parts": [], "dsl": "card = ArtifactCard(a, b, c)", "segments": []},
        {
            "narrative_parts": [],
            "dsl": "",
            "segments": [{"kind": "dsl", "text": "card = ArtifactCard(a, b, c)"}],
        },
    ],
)
def test_payload_is_not_empty_with_prose_or_dsl(state):
    # A DSL-only turn is legal (the interweave contract allows no prose); the
    # guard must never refuse it.
    assert _payload_is_empty(state) is False


# --- the disconnect path + the genuine empty completion ----------------------------


def test_cancelled_sse_consumer_mid_stream_never_commits_an_empty_turn(graph_app):
    """Browser closes mid-generation: producer survives, empty commit is REFUSED.

    The SSE consumer being cancelled is exactly what Starlette does on
    ``http.disconnect``; the producer task is independent (buffered for
    Last-Event-ID). This test proves the turn still completed AND that the guard
    — not the disconnect — is what keeps the empty row out of the log.
    """
    stall = _StallingEmpty()
    model = ScriptedModel([stall.first, stall.second])
    app, log = graph_app(model)

    async def scenario():
        async with _client(app) as client:
            sid = await _create_session(client)
            request = asyncio.create_task(
                client.post(
                    f"/api/sessions/{sid}/turns",
                    json={"content": "I listen.", "kind": "in_character"},
                )
            )
            await asyncio.wait_for(stall.started.wait(), timeout=5)
            turn = app.state.sessions[sid].turn
            assert turn is not None and not turn.done.is_set()

            # The browser goes away: the response consumer is cancelled.
            request.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request

            # The producer was NOT cancelled with the consumer — release the
            # model so the turn reaches commit.
            stall.release.set()
            await asyncio.wait_for(turn.done.wait(), timeout=5)

            assert turn.task is not None and turn.task.cancelled() is False
            return sid

    sid = run(scenario())

    assert _narratives(log, sid) == []
    aborts = _abort_rows(log, sid)
    assert len(aborts) == 1
    assert aborts[0].payload["stage"] == "commit"
    assert aborts[0].payload["reason"] == "empty_content"
    assert aborts[0].payload["reasoning_chars"] > 0

    # Playable: the next turn runs normally and commits real prose.
    async def next_turn():
        async with _client(app) as client:
            response = await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "Continue.", "kind": "in_character"},
            )
            assert response.status_code == 200
            assert "event: committed" in response.text

    run(next_turn())
    committed = [event for event in _narratives(log, sid) if not event.ephemeral]
    assert len(committed) == 1
    assert committed[0].payload["prose"] == "The lamp gutters, then steadies."
    assert len(log.get_projection(sid)["narratives"]) == 1


def test_genuine_empty_completion_is_aborted_not_committed(graph_app):
    """No disconnect at all: an empty completion takes the SAME refusal."""
    async def empty():
        yield {"type": "reasoning", "delta": "The model only deliberates."}
        yield {"type": "finish", "finish_reason": "stop"}

    model = ScriptedModel([empty, empty])
    app, log = graph_app(model)

    async def scenario():
        async with _client(app) as client:
            sid = await _create_session(client)
            response = await client.post(
                f"/api/sessions/{sid}/turns",
                json={"content": "Describe the room.", "kind": "in_character"},
            )
            return sid, response

    sid, response = run(scenario())

    assert response.status_code == 200
    assert '"reason":"empty_content"' in response.text
    assert "event: committed" not in response.text
    assert _narratives(log, sid) == []
    aborts = _abort_rows(log, sid)
    assert len(aborts) == 1
    assert aborts[0].payload["prose_chars"] == 0
    assert aborts[0].payload["dsl_chars"] == 0
    # The user's ask is not lost: it was appended at turn start and stays
    # (append-only); nothing rendered it as an answered turn.
    users = [event for event in log.get_events(sid) if event.type == "user"]
    assert [event.payload["text"] for event in users] == ["Describe the room."]


# --- non-empty turns are untouched -------------------------------------------------


def test_cancellation_after_prose_aborts_cleanly_and_never_commits_an_empty_row(
    tmp_path,
):
    """A cancel that lands AFTER prose keeps nothing — and never an empty row.

    The codebase's cancel contract is pre-commit: a cancelled turn leaves at
    most an ephemeral partial (``GraphTurnEngine._finish_cancelled``); it never
    commits. There is no narrative to revise yet, so "abort cleanly" is the
    revision model's honest answer.
    """
    log = EventLog(tmp_path / "cancel.db")

    class ProseThenCancel:
        async def __call__(self, messages, stream=True):
            yield {"type": "content", "delta": "The lamp gutters. "}
            await asyncio.sleep(0)
            raise asyncio.CancelledError()

    model = ProseThenCancel()
    graph = build_graph(model=model, extractor=_NoExtractor(), log=log)
    engine = GraphTurnEngine(graph=graph, log=log, model=model)

    async def scenario():
        events = []
        try:
            async for event in engine.run_turn("s1", "I look.", "in_character"):
                events.append(event)
        except asyncio.CancelledError:  # pragma: no cover - LangGraph wraps it
            pass
        return events

    events = run(scenario())
    try:
        narratives = _narratives(log, "s1")
        assert narratives == []
        assert log.get_projection("s1")["narratives"] == []
        assert not any(event.type == "narrative" for event in log.get_events("s1"))
        assert not any(
            event.get("type") == "committed" for event in events
        )
    finally:
        log.close()


def test_stream_ending_after_prose_commits_what_existed(tmp_path):
    """The guard is about EMPTY, not about partial: real prose still commits.

    A stream that ends (provider side-band, no explicit error) after prose is a
    turn the reader saw; the existing commit path keeps it, unchanged.
    """
    log = EventLog(tmp_path / "partial.db")

    class ProseThenEnd:
        async def __call__(self, messages, stream=True):
            yield {"type": "content", "delta": "The lamp gutters, then steadies."}
            yield {"type": "finish", "finish_reason": "stop"}

    model = ProseThenEnd()
    graph = build_graph(model=model, extractor=_NoExtractor(), log=log)
    engine = GraphTurnEngine(graph=graph, log=log, model=model)

    async def scenario():
        return [event async for event in engine.run_turn("s1", "I wait.", "in_character")]

    events = run(scenario())
    try:
        assert any(event.get("type") == "committed" for event in events)
        narratives = _narratives(log, "s1")
        assert len(narratives) == 1
        assert narratives[0].payload["prose"] == "The lamp gutters, then steadies."
        assert narratives[0].payload["prose"].strip()
        assert log.get_projection("s1")["narratives"]
    finally:
        log.close()


def test_checkpointed_session_survives_an_empty_turn_abort(tmp_path):
    """Production wiring uses a checkpointer (ADR-003: cache, not truth).

    A refused commit must not poison the thread: the next turn starts, commits,
    and leaves exactly one narrative. The failed checkpoint is rebuildable cache;
    the log is what the next turn reads.
    """
    log = EventLog(tmp_path / "ckpt-events.db")

    class FirstEmptyThenProse:
        def __init__(self):
            self.calls = 0

        async def __call__(self, messages, stream=True):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "reasoning", "delta": "Only deliberation."}
            else:
                yield {"type": "content", "delta": "The lamp gutters."}

    model = FirstEmptyThenProse()

    async def scenario():
        async with open_sqlite_checkpointer(tmp_path / "ckpt.db") as checkpointer:
            graph = build_graph(
                model=model,
                extractor=_NoExtractor(),
                log=log,
                checkpointer=checkpointer,
            )
            engine = GraphTurnEngine(graph=graph, log=log, model=model)
            first = [event async for event in engine.run_turn("s1", "hi", "in_character")]
            second = [
                event async for event in engine.run_turn("s1", "again", "in_character")
            ]
            return first, second

    first, second = run(scenario())
    try:
        assert any(event.get("type") == "error" for event in first)
        assert any(event.get("type") == "committed" for event in second)
        narratives = _narratives(log, "s1")
        assert len(narratives) == 1
        assert narratives[0].payload["prose"] == "The lamp gutters."
        assert len(_abort_rows(log, "s1")) == 1
    finally:
        log.close()


def test_wrapper_only_reply_is_empty_after_the_45_strip():
    """Composition pin (#45 × #46): a reply that is NOTHING but a leaked
    engine wrapper tag strips to empty inside the emptiness check and must
    abort — not slip past as \"non-empty\" and commit through as a blank row
    after _commit_narrative's strip runs."""
    from fabulist.graph.nodes import _payload_is_empty

    assert _payload_is_empty(
        {"narrative_parts": ["<preset_def>"], "dsl": "", "segments": []}
    )
    assert _payload_is_empty(
        {"narrative_parts": ["<preset_def>\n</preset_def>\n"], "dsl": "", "segments": []}
    )
    assert not _payload_is_empty(
        {
            "narrative_parts": ["<preset_def>\nHesta laughs.</preset_def>"],
            "dsl": "",
            "segments": [],
        }
    )
