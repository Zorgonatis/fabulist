"""Issue #83: uncapped world-info rendering + the turn-time size advisory.

The owner ruling (2026-10-26) removed the 1200-character budget: every
activated entry renders, in insertion order, with no size filter. These tests
pin the three acceptance halves that a pure unit test cannot see:

(a) a book whose activated entries total far past 1200 renders ALL of them;
(b) an under-budget activation renders byte-identically to the pre-change code
    (the snapshot below was taken from ``master`` at 4db1ffc, the commit the
    RCA was verified against);
(c) the new ``world_info_large`` notice fires ONCE per turn on a very large
    block, carries the rendered size, and never fires on a small one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fabulist.context.facts import (
    WORLD_INFO_ADVISORY_CHARS,
    session_world_info_block,
)
from fabulist.context.world_info import BLOCK_HEADER
from fabulist.log import EventLog
from fabulist.log.degradation import DEGRADATION_EVENTS, STAGES, WORLD_INFO_LARGE_EVENT


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for an :class:`EventLog` on a tmp path; closes logs after."""

    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


def _book(entries: list[dict]) -> dict:
    return {"name": "Uncapped lore", "entries": entries}


def _append_book(log: EventLog, sid: str, entries: list[dict]) -> None:
    log.append_event(sid, "system", "system", {"event": "world_info", "book": _book(entries)})


def _notices(log: EventLog, sid: str) -> list[dict]:
    return [
        event.payload
        for event in log.get_events(sid)
        if event.type == "system"
        and isinstance(event.payload, dict)
        and event.payload.get("event") == WORLD_INFO_LARGE_EVENT
    ]


# -- (a) every activated entry renders, past the old 1200-char line ----------

#: Four distinct entries of 400 characters each: ~1600 rendered characters,
#: well past the removed 1200-character budget.
FAR_PAST_OLD_BUDGET = [
    {"constant": True, "content": "A" * 400, "insertion_order": 4},
    {"constant": True, "content": "B" * 400, "insertion_order": 1},
    {"constant": True, "content": "C" * 400, "insertion_order": 3},
    {"constant": True, "content": "D" * 400, "insertion_order": 2},
]


def test_far_past_1200_renders_all_entries_in_insertion_order(build_log):
    log = build_log()
    sid = "uncapped"
    log.append_event(sid, "user", "user", {"text": "look around"})
    _append_book(log, sid, FAR_PAST_OLD_BUDGET)

    block = session_world_info_block(log, sid, "look around")
    assert block is not None
    expected = (
        f"{BLOCK_HEADER}\n- {'B' * 400}\n- {'D' * 400}\n"
        f"- {'C' * 400}\n- {'A' * 400}"
    )
    assert block == expected
    assert len(block) > 1200
    # No entry was dropped and no head/tail marker was invented.
    assert block.count("\n- ") == 4


# -- (b) under-budget content is byte-identical to the pre-change code -------

#: The exact render of :data:`SNAPSHOT_BOOK` under the OLD 1200-char budget,
#: produced by ``render_block`` at master 4db1ffc (pre-change code).
PRE_CHANGE_SNAPSHOT = (
    "### World info\n"
    "- The rift hums at dusk. The hum is a warning.\n"
    "- Warden Ilyra keeps the eastern gate and the ledger.\n"
    "- The brass lantern burns green near the rift."
)

#: sha256(...)[:16] of :data:`PRE_CHANGE_SNAPSHOT` — the byte-identity pin.
PRE_CHANGE_SNAPSHOT_SHA256 = "c3849d6fad6a1e6b"

SNAPSHOT_BOOK = [
    {
        "keys": ["lantern"],
        "content": "The brass lantern burns green near the rift.",
        "insertion_order": 3,
    },
    {
        "keys": ["rift"],
        "content": "The rift hums at dusk. The hum is a warning.",
        "insertion_order": 1,
    },
    {
        "keys": ["warden"],
        "content": "Warden Ilyra keeps the eastern gate and the ledger.",
        "insertion_order": 2,
    },
]

SNAPSHOT_SCAN = "I raise the lantern at the rift and ask the warden about the ledger."


def test_under_budget_render_is_byte_identical_to_pre_change_snapshot(build_log):
    log = build_log()
    sid = "snapshot"
    log.append_event(sid, "user", "user", {"text": SNAPSHOT_SCAN})
    _append_book(log, sid, SNAPSHOT_BOOK)

    block = session_world_info_block(log, sid, SNAPSHOT_SCAN)
    assert block == PRE_CHANGE_SNAPSHOT
    assert len(block) < 1200
    assert hashlib.sha256(block.encode("utf-8")).hexdigest()[:16] == (
        PRE_CHANGE_SNAPSHOT_SHA256
    )


# -- (c) the size advisory --------------------------------------------------

#: Three distinct 3000-char entries: ~9,000 rendered characters, far over the
#: 8,000-char advisory line.
VERY_LARGE = [
    {"constant": True, "content": "X" * 3000, "insertion_order": 1},
    {"constant": True, "content": "Y" * 3000, "insertion_order": 2},
    {"constant": True, "content": "Z" * 3000, "insertion_order": 3},
]


def test_large_block_emits_one_notice_per_turn_with_the_size(build_log):
    log = build_log()
    sid = "advisory"
    user = log.append_event(sid, "user", "user", {"text": "read the walls"})
    _append_book(log, sid, VERY_LARGE)

    block = session_world_info_block(log, sid, "read the walls")
    assert block is not None
    size = len(block)
    assert size > WORLD_INFO_ADVISORY_CHARS

    # The turn performs several renders ({{facts}}, {{wi}}, the builder); the
    # notice still lands exactly once for this turn.
    assert session_world_info_block(log, sid, "read the walls") == block
    assert session_world_info_block(log, sid, "read the walls") == block

    notices = _notices(log, sid)
    assert len(notices) == 1
    notice = notices[0]
    assert notice["stage"] == STAGES[WORLD_INFO_LARGE_EVENT]
    assert notice["turn"] == user.seq
    assert notice["chars"] == size
    assert notice["threshold"] == WORLD_INFO_ADVISORY_CHARS
    assert str(size) in notice["reason"]
    # It is an advisory, not a degradation of the block: the prompt bytes still
    # carry every entry.
    assert block.count("\n- ") == 3


def test_large_block_on_a_later_turn_emits_its_own_notice(build_log):
    log = build_log()
    sid = "next-turn"
    log.append_event(sid, "user", "user", {"text": "first"})
    _append_book(log, sid, VERY_LARGE)

    session_world_info_block(log, sid, "first")
    log.append_event(sid, "narrative", "narrator", {"prose": "prose", "dsl": "", "segments": []})
    second = log.append_event(sid, "user", "user", {"text": "second"})

    session_world_info_block(log, sid, "second")
    notices = _notices(log, sid)
    assert len(notices) == 2
    assert notices[-1]["turn"] == second.seq


def test_small_block_does_not_emit_the_advisory(build_log):
    log = build_log()
    sid = "quiet"
    log.append_event(sid, "user", "user", {"text": SNAPSHOT_SCAN})
    _append_book(log, sid, SNAPSHOT_BOOK)

    block = session_world_info_block(log, sid, SNAPSHOT_SCAN)
    assert block is not None
    assert len(block) < WORLD_INFO_ADVISORY_CHARS
    assert _notices(log, sid) == []


def test_no_user_event_means_no_notice(build_log):
    # A preview / replay / definition read renders a block but is not a turn.
    log = build_log()
    sid = "no-turn"
    _append_book(log, sid, VERY_LARGE)

    block = session_world_info_block(log, sid, "read the walls")
    assert block is not None
    assert len(block) > WORLD_INFO_ADVISORY_CHARS
    assert _notices(log, sid) == []


def test_world_info_large_is_registered_in_the_notices_channel():
    assert WORLD_INFO_LARGE_EVENT in DEGRADATION_EVENTS
    assert STAGES[WORLD_INFO_LARGE_EVENT] == "world_info_render"
