"""Scripted :class:`TurnEngine` for tests, dev boots, and the SSE smoke check.

No model calls: yields a fixed list of interleaved ``reasoning``/``content``
deltas and a terminal ``done``. ``delay`` inserts an await before each yield so
tests can exercise the turn lock, cooperative stop, and Last-Event-ID replay
mid-stream.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from .app import StreamEvent

#: Interleaved reasoning/content deltas, per the shared W1.4 contract.
DEFAULT_SCRIPT: list[StreamEvent] = [
    {"type": "reasoning", "delta": "Weighing the scene and the last beat. "},
    {"type": "content", "delta": "Rain hammers "},
    {"type": "reasoning", "delta": "Keeping the tone warm. "},
    {"type": "content", "delta": "the shutters."},
    {"type": "done", "meta": {"finish_reason": "stop"}},
]


class FakeTurnEngine:
    """Injected engine stub: deterministic, scriptable, abortable."""

    def __init__(
        self, script: list[StreamEvent] | None = None, *, delay: float = 0.0
    ) -> None:
        self.script = [dict(event) for event in (script or DEFAULT_SCRIPT)]
        self.delay = delay
        self.calls: list[tuple[str, str, str]] = []

    async def run_turn(
        self,
        session_id: str,
        content: str,
        kind: str,
        *,
        image_url: str | None = None,
        image_kind: str = "image",
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append((session_id, content, kind))
        if image_url:
            self.calls[-1] = (session_id, content, f"{kind}+image")
        for event in self.script:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield dict(event)
