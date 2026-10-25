"""Issue #25: a streaming model call rides a WALL-CLOCK bound, not a per-read one.

The failure this pins: ``Connection.timeout`` is a per-READ timeout, and every
streamed delta RESETS it. A provider that keeps dripping one token every few
hundred milliseconds is therefore never silent — it was measured live as 63
deltas over 25s, no terminal frame, the transport never giving up and the turn
hanging forever. The answer is ``Connection.call_timeout_s``: ONE deadline per
CALL, measured from the send, enforced by ``wiring.RegistryModelCaller`` (the
one transport every model call rides) and surfaced through the pre-commit phase
rule of ADR-023 as ``PhaseCallError`` — fatal, phase-named, model-named.

Pinned here:

1. a TRICKLING stream ends at the deadline (and would otherwise never end — the
   test's own outer budget fails it as a hang rather than hanging the suite);
2. a SILENT stream ends at the deadline too, without borrowing a read timeout;
3. a NORMAL stream is byte-identically unaffected by the 900s default;
4. the surfacing is FATAL, phase-named ("Narration failed: …") and model-named
   ("… from trickle-1"), and it names the slot the limit belongs to;
5. the planner SEED hands the same bound to its tool loop as provenance, so its
   ``agent_loop`` rows no longer claim the phase is unbounded;
6. the setting itself: default 900s, positive-only, parsed and patchable.

Fake transports only — no live inference, no network, no sleeps longer than a
test needs.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import fabulist.wiring as wiring
from fabulist.connections.client import (
    DEFAULT_CALL_TIMEOUT_S,
    ModelCallDeadlineExceeded,
    call_deadline_s,
)
from fabulist.connections.registry import ConnectionConfigError, Registry
from fabulist.graph.provider_errors import PhaseCallError, phase_failure

SID = "deadline-test"

#: How long a test waits before declaring "this call did not end": the whole
#: point of the issue is that an unbounded stream never ends, so the test must
#: FAIL rather than hang if the bound regresses.
HANG_BUDGET_S = 5.0


def run(coro):
    return asyncio.run(coro)


# -- fakes -------------------------------------------------------------------


class _FakeDelta:
    def __init__(self, content: str) -> None:
        self.content = content
        self.reasoning_content = None
        self.tool_calls = None


class _FakeChoice:
    def __init__(self, content: str, finish_reason: str | None = None) -> None:
        self.delta = _FakeDelta(content)
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, content: str, finish_reason: str | None = None) -> None:
        self.choices = [_FakeChoice(content, finish_reason)]
        self.usage = None


class _FakeCompletions:
    """``chat.completions.create`` serving whatever generator it is handed."""

    def __init__(self, stream_factory) -> None:
        self._stream_factory = stream_factory

    async def create(self, **kwargs):
        return self._stream_factory()


class _FakeClient:
    def __init__(self, stream_factory) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(stream_factory))


def _install(monkeypatch, stream_factory) -> None:
    fake = _FakeClient(stream_factory)
    monkeypatch.setattr(wiring, "_async_client", lambda agent, registry: fake)


def _registry(connection: dict, agent: str = "narrator") -> Registry:
    return Registry.from_dict(
        {
            "connections": {
                "c": {
                    "base_url": "http://localhost:4444/v1",
                    "model": "trickle-1",
                    **connection,
                }
            },
            "agents": {agent: "c"},
        }
    )


def _drain(caller, messages=None):
    """Drain ``caller`` under a hang budget; returns ``(events, error, elapsed)``.

    The outer ``asyncio.timeout`` is the TEST's guard, not the code under test:
    without the wall-clock bound the fake stream never ends, and the test must
    report that as a failure instead of hanging the suite.
    """
    messages = messages or [{"role": "user", "content": "continue"}]

    async def _run():
        events: list[dict] = []
        started = time.monotonic()
        try:
            async with asyncio.timeout(HANG_BUDGET_S):
                async for event in caller(messages):
                    events.append(event)
        except TimeoutError as exc:  # pragma: no cover - only on a regression
            raise AssertionError(
                "the call never ended: no wall-clock bound fired "
                f"({len(events)} frames had arrived in {HANG_BUDGET_S}s)"
            ) from exc
        except ModelCallDeadlineExceeded as exc:
            return events, exc, time.monotonic() - started
        return events, None, time.monotonic() - started

    return run(_run())


# -- 1. the trickle ----------------------------------------------------------


def test_a_trickling_stream_ends_at_the_wall_clock_deadline(monkeypatch):
    """The issue's exact shape: a live stream that never closes must be cut."""

    async def trickle():
        chunk = _FakeChunk("trickle ")
        while True:  # never closes — the per-read timeout is reset by every delta
            yield chunk
            await asyncio.sleep(0.02)

    _install(monkeypatch, trickle)
    caller = wiring.RegistryModelCaller(
        "narrator", _registry({"call_timeout_s": 0.2})
    )

    events, error, elapsed = _drain(caller)

    assert isinstance(error, ModelCallDeadlineExceeded)
    # It ENDED at the deadline (not instantly, not at the test's hang budget).
    assert 0.2 <= elapsed < 2.0
    # The stream was ALIVE when the clock fired: that is why the per-read
    # timeout could never fire, and it is what the message says out loud.
    assert len(events) >= 2
    assert error.delivered is True
    assert "still delivering" in str(error)
    assert "exceeded the 0.2s call deadline" in str(error)
    assert "call_timeout_s for completion.narration" in str(error)


def test_the_trickle_is_cut_by_the_connection_deadline_not_by_a_frame_count(monkeypatch):
    """Same stream, a longer deadline: it survives longer before being cut."""

    async def trickle():
        chunk = _FakeChunk("trickle ")
        while True:
            yield chunk
            await asyncio.sleep(0.02)

    _install(monkeypatch, trickle)
    caller = wiring.RegistryModelCaller(
        "narrator", _registry({"call_timeout_s": 0.4})
    )

    events, error, elapsed = _drain(caller)

    assert isinstance(error, ModelCallDeadlineExceeded)
    assert 0.4 <= elapsed < 2.0
    assert len(events) >= 5


# -- 2. a silent stream ------------------------------------------------------


def test_a_silent_stream_ends_at_the_deadline_too(monkeypatch):
    """No delta at all: the deadline still ends it (no read timeout borrowed)."""

    async def silent():
        await asyncio.sleep(30)  # a provider that never sends anything
        yield _FakeChunk("too late", finish_reason="stop")  # pragma: no cover

    _install(monkeypatch, silent)
    caller = wiring.RegistryModelCaller(
        "narrator", _registry({"call_timeout_s": 0.15})
    )

    events, error, elapsed = _drain(caller)

    assert isinstance(error, ModelCallDeadlineExceeded)
    assert elapsed < 2.0
    assert events == []
    assert error.delivered is False
    assert "no chunk arrived before the deadline" in str(error)


# -- 3. a normal stream ------------------------------------------------------


def test_a_normal_stream_is_unaffected(monkeypatch):
    """A short stream under the DEFAULT bound finishes with today's frames."""

    async def normal():
        yield _FakeChunk("The rain ")
        yield _FakeChunk("stops.", finish_reason="stop")

    _install(monkeypatch, normal)
    registry = _registry({})  # no call_timeout_s => the 900s default
    caller = wiring.RegistryModelCaller("narrator", registry)

    events, error, elapsed = _drain(caller)

    assert error is None
    assert elapsed < 1.0
    assert [event["delta"] for event in events if event["type"] == "content"] == [
        "The rain ",
        "stops.",
    ]
    assert call_deadline_s(registry.connection_for("narrator")) == DEFAULT_CALL_TIMEOUT_S
    assert DEFAULT_CALL_TIMEOUT_S == 900.0


def test_a_connection_can_tighten_but_never_unbind_the_deadline(monkeypatch):
    """``call_timeout_s`` unset/None means 900s — never "no bound"."""
    assert call_deadline_s(_registry({}).connection_for("narrator")) == 900.0
    assert (
        call_deadline_s(_registry({"call_timeout_s": 12}).connection_for("narrator"))
        == 12.0
    )


# -- 4. the surfacing --------------------------------------------------------


def test_the_deadline_surfaces_fatal_phase_named_and_model_named(log):
    """ADR-023: pre-commit, a transport-class failure ABORTS naming both."""
    from fabulist.graph.nodes import make_narrate_node

    failure = ModelCallDeadlineExceeded(
        "completion.narration", 3.0, delivered=True
    )

    class _DeadlineCaller:
        """Yields a frame, then dies the way the transport dies."""

        async def __call__(self, messages, stream=True, **kwargs):
            yield {"type": "content", "delta": "The rain "}
            raise failure

    node = make_narrate_node(
        model=_DeadlineCaller(),
        log=log,
        build_messages=lambda state: [{"role": "user", "content": "continue"}],
        provider_label_fn=lambda: "trickle-1",
    )

    with pytest.raises(PhaseCallError) as caught:
        run(node({"session_id": SID, "user_content": "go", "facts": "", "plan": ""}))

    message = str(caught.value)
    assert caught.value.phase == "Narration"
    assert message.startswith("Narration failed: ModelCallDeadlineExceeded from trickle-1")
    # The named slot is the one the owner edits, and the reason is the WALL
    # clock, not the transport giving up on a silent socket.
    assert "call_timeout_s for completion.narration" in message
    assert "the per-read timeout never fired" in message
    # Pre-commit: nothing was kept.
    assert [e for e in log.get_events(SID) if e.type == "narrative"] == []


def test_the_planner_phase_names_the_deadline_the_same_way():
    """The same exception through the planner's label path, verbatim."""
    failure = ModelCallDeadlineExceeded("completion.narration", 3.0, delivered=True)

    fatal = phase_failure("Planning", failure, provider="trickle-1")

    assert isinstance(fatal, PhaseCallError)
    assert str(fatal).startswith("Planning failed: ModelCallDeadlineExceeded from trickle-1")
    assert "exceeded the 3s call deadline" in str(fatal)


def test_a_deadline_is_not_mistaken_for_a_read_timeout():
    """The class marker that would let a tool loop swallow it must NOT match.

    ``agents.loop`` recovers a READ timeout by design (the per-connection
    doctrine); the wall-clock abort is a different failure and must keep its own
    name all the way to the client.
    """
    from fabulist.agents.loop import is_transport_timeout

    assert is_transport_timeout(ModelCallDeadlineExceeded("s", 3.0, delivered=True)) is False


# -- 5. the planner seed -----------------------------------------------------


class _SeededCaller:
    """A caller with the transport's deadline seam, as the seed phase sees it."""

    def __init__(self) -> None:
        self.deadline = 4.5

    def call_timeout_s(self) -> float:
        return self.deadline

    async def __call__(self, messages, stream=True, request_profile=None):
        yield {"type": "content", "delta": "seeded."}


def _capture_loop(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(timed_out=False, text="seeded.", actions=1)

    import fabulist.agents.loop as loop_module

    monkeypatch.setattr(loop_module, "run_agent_loop", fake_loop)
    return captured


def test_the_planner_seed_hands_the_wall_clock_bound_to_its_loop(monkeypatch, log):
    """The seed's ``agent_loop`` rows must report the bound, not "unbounded"."""
    from fabulist.state.planner_bootstrap import BOOTSTRAP, run_planner_tool_phase

    captured = _capture_loop(monkeypatch)

    run(
        run_planner_tool_phase(
            caller=_SeededCaller(),
            log=log,
            session_id=SID,
            directive="directive",
            context="context",
            turn_seq=1,
            mode=BOOTSTRAP,
            profile={"tools": []},
        )
    )

    assert captured["timeout_s"] == 4.5


def test_an_explicit_seed_timeout_wins_and_no_seam_means_none(monkeypatch, log):
    from fabulist.state.planner_bootstrap import BOOTSTRAP, run_planner_tool_phase

    captured = _capture_loop(monkeypatch)

    async def _caller(messages, stream=True, request_profile=None):
        yield {"type": "content", "delta": "seeded."}

    run(
        run_planner_tool_phase(
            caller=_caller,  # no ``call_timeout_s`` reader at all
            log=log,
            session_id=SID,
            directive="directive",
            context="context",
            turn_seq=1,
            mode=BOOTSTRAP,
            profile={"tools": []},
            timeout_s=2.0,
        )
    )
    assert captured["timeout_s"] == 2.0

    captured.clear()
    run(
        run_planner_tool_phase(
            caller=_caller,
            log=log,
            session_id=SID,
            directive="directive",
            context="context",
            turn_seq=1,
            mode=BOOTSTRAP,
            profile={"tools": []},
        )
    )
    # A missing number is never invented: the row then carries none.
    assert captured["timeout_s"] is None


# -- 6. the setting ----------------------------------------------------------


def test_call_timeout_s_is_parsed_and_defaults_to_none():
    conn = _registry({"call_timeout_s": 3}).connection_for("narrator")
    assert conn.call_timeout_s == 3.0

    assert _registry({}).connection_for("narrator").call_timeout_s is None
    assert _registry({"call_timeout_s": None}).connection_for("narrator").call_timeout_s is None


@pytest.mark.parametrize("value", [0, -1, "3", True])
def test_call_timeout_s_refuses_a_non_positive_or_non_numeric_value(value):
    with pytest.raises(ConnectionConfigError):
        _registry({"call_timeout_s": value})


def test_call_timeout_s_is_patchable():
    """The Connections API can set it (it is in the patchable field set)."""
    from fabulist.connections.registry import (
        CONNECTION_PATCH_FIELDS,
        validate_connection_patch,
    )

    assert "call_timeout_s" in CONNECTION_PATCH_FIELDS
    assert validate_connection_patch("c", {"call_timeout_s": 45}) == {
        "call_timeout_s": 45.0
    }
    assert validate_connection_patch("c", {"call_timeout_s": None}) == {
        "call_timeout_s": None
    }
    with pytest.raises(ConnectionConfigError):
        validate_connection_patch("c", {"call_timeout_s": 0})
