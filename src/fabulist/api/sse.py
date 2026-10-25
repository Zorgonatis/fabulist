"""Server-Sent Events framing and replay buffering for the turn stream (W1.4).

Framing follows the SSE wire format: ``id:`` / ``event:`` / ``data:`` lines
ended by a blank line, with one JSON object per ``data`` line. Heartbeats are
comment lines (``: ping``) that clients ignore, so an idle stream stays warm
without emitting a spurious named event.

Serialized turn chunks are retained in a bounded :class:`ReplayBuffer` so a
client that reconnects with ``Last-Event-ID`` can recover the missed suffix
before the live tail resumes (PLAN.md W1.4 "Last-Event-ID reconnect").
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator

#: Comment heartbeat cadence (seconds) on otherwise idle streams.
HEARTBEAT_SECONDS = 15.0

#: How many serialized chunks a session retains for Last-Event-ID replay.
REPLAY_CAP = 1000


def format_sse(*, id: int, event: str, data: dict) -> str:
    """Serialize one named SSE chunk (single-line JSON data)."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {id}\nevent: {event}\ndata: {payload}\n\n"


def format_comment(text: str) -> str:
    """Serialize one SSE comment line (heartbeat / keep-alive)."""
    return f": {text}\n\n"


@dataclass(frozen=True)
class BufferedEvent:
    """One serialized chunk retained for replay."""

    id: int
    event: str
    data: dict

    def serialize(self) -> str:
        return format_sse(id=self.id, event=self.event, data=self.data)


class ReplayBuffer:
    """Bounded, monotonically-id'd buffer of one session's turn events.

    ``id`` values are assigned on append and never reused, so a client's
    ``Last-Event-ID`` stays unambiguous across turns in a session. Only the
    most recent ``cap`` chunks are retained; older ids age out.
    """

    def __init__(self, cap: int = REPLAY_CAP) -> None:
        self._cap = cap
        self._items: deque[BufferedEvent] = deque(maxlen=cap)
        self._next_id = 0

    @property
    def last_id(self) -> int:
        """Id of the most recently appended chunk (0 before the first)."""
        return self._next_id

    def append(self, event: str, data: dict) -> BufferedEvent:
        self._next_id += 1
        item = BufferedEvent(id=self._next_id, event=event, data=data)
        self._items.append(item)
        return item

    def after(self, last_id: int) -> list[BufferedEvent]:
        """Chunks with an id strictly greater than ``last_id``, in order."""
        return [item for item in self._items if item.id > last_id]


async def event_stream(
    buffer: ReplayBuffer,
    updated: asyncio.Event,
    done: asyncio.Event,
    *,
    last_id: int = 0,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Yield serialized SSE chunks: replay after ``last_id``, then live tail.

    ``buffer`` is the shared source of truth; ``updated`` is set by the turn
    producer whenever it appends, and ``done`` when the turn is finished. A
    timeout while idle emits a comment heartbeat instead of a data event.
    """
    cursor = last_id
    while True:
        batch = buffer.after(cursor)
        if batch:
            for item in batch:
                cursor = item.id
                yield item.serialize()
            continue
        if done.is_set():
            return
        updated.clear()
        # Re-check after clearing to close the lost-wakeup window: if the
        # producer appended or finished in between, loop instead of waiting.
        if buffer.after(cursor) or done.is_set():
            continue
        try:
            await asyncio.wait_for(updated.wait(), timeout=heartbeat)
        except asyncio.TimeoutError:
            yield format_comment("ping")
