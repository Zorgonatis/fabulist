"""Recall seam integration: recall_fn -> state.recall -> post-user <recalled>.

Proves the W3.2 wiring contract with fakes (no network): the engine awaits
the recall fn and injects its block; the message builder renders it in the
D22 post-user `<recalled>` reference message (NOT in the final user message).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.wiring import make_narrator_message_builder


def test_builder_renders_recall_in_post_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fabulist.wiring as wiring

    log = EventLog(tmp_path / "log.db")
    log.append_event("s", "user", "user", {"text": "hello"})
    captured: dict = {}

    def fake_compile(config, toggles, **kwargs):
        captured.update(kwargs)
        return [{"role": "user", "content": "x"}]

    monkeypatch.setattr(wiring, "compiler_compile", fake_compile)
    builder = wiring.make_narrator_message_builder(log)
    builder(
        {
            "session_id": "s",
            "user_content": "what did the merchant wear?",
            "facts": "time: 09:00",
            "recall": "### Recalled from earlier (vector memory)\n- [seq 3] [narrative] The merchant wore a amber cloak",
        }
    )
    # D22: recall LEAVES the final user message and rides the post-user system block.
    facts = captured["facts"] or ""
    recalled = captured["recalled"] or ""
    assert "Recalled from earlier" not in facts
    assert "time: 09:00" in facts
    # The envelope owns the tag pair; the seam hands over the memory BODY.
    assert not recalled.startswith("<")
    assert "Reference material for this turn" in recalled
    assert "Recalled from earlier" in recalled


def test_engine_injects_recall_into_initial_state() -> None:
    import asyncio

    asyncio.run(_engine_recall_flow())


async def _engine_recall_flow() -> None:
    """GraphTurnEngine awaits recall_fn and seeds state['recall'] (fake graph)."""
    from fabulist.graph.engine import GraphTurnEngine

    captured: dict = {}

    class FakeGraph:
        async def astream(self, initial, config, stream_mode=None):  # noqa: ANN001
            captured.update(initial)
            yield {"type": "done", "meta": {}}

    engine = GraphTurnEngine(
        graph=FakeGraph(),
        log=None,  # type: ignore[arg-type]
        model=object(),  # type: ignore[arg-type]
        recall_fn=lambda sid, text: _recall(sid, text),
    )
    events = [e async for e in engine.run_turn("s1", "find the amber cloak")]
    assert events
    assert "amber cloak" in captured["recall"]


async def _recall(session_id: str, text: str) -> str:
    return f"### Recalled from earlier (vector memory)\n- [seq 3] amber cloak ({text[:12]}…)"
