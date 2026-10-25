"""W9.32 W6a — the clock's expiry sweep in ``pre_turn`` (R5, §3.5).

``pre_turn`` advances the rules clock and then retires every condition whose
instant the clock has passed: one ``entity_retire`` per expiry, actor ``clock``,
reason ``expired at <instant>``. The sweep APPENDS (D9 — the log owns the
removal, the palette and the tree never re-derive "was this still active last
turn") and the store makes it idempotent by tombstone, so running it on every
turn cannot mint a second tombstone for one expiry.

The pins that matter to a turn: a condition expiring at 21:30 survives the turn
that ends 21:25 and dies on the one that ends 21:50; the sweep reads the ADVANCED
clock, never the prior fold; a rest that crosses midnight retires yesterday's
expiry; and a sweep that cannot read never costs the player their turn.
"""

from __future__ import annotations

import asyncio

from fabulist.graph.clock import parse_facts
from fabulist.graph.nodes import make_pre_turn_node
from fabulist.log import EventLog
from fabulist.state import entities

SID = "s1"
MONDAY = "Monday, June 02, 2025"

#: ``travel`` advances 25 minutes (the rules table) — two of them cross 21:30.
TRAVEL = "I walk down to the docks"


def run(coro):
    return asyncio.run(coro)


def _seed_clock(log: EventLog, time: str = "21:00", date: str = MONDAY) -> None:
    """The clock keys as the previous turn's ``state-delta`` left them (D3 fold)."""
    log.append_event(SID, "state-delta", "bts", {"time": time, "date": date})


def _turn(log: EventLog, message: str = TRAVEL) -> dict:
    """Run ``pre_turn``, then commit its clock the way the bts node does.

    ``pre_turn`` returns the advanced facts and knows nothing about persistence
    (the bts node stamps the ``state-delta``), so a multi-turn scenario makes
    that one write itself — without it the next ``pre_turn`` would read the same
    prior fold and the sweep could never see a crossed instant.
    """
    node = make_pre_turn_node(log=log)
    update = run(node({"session_id": SID, "user_content": message}))
    log.append_event(SID, "state-delta", "bts", dict(parse_facts(update["facts"])))
    return update


def _retires(log: EventLog) -> list[dict]:
    return [
        event.payload
        for event in log.get_events(SID)
        if event.type == "system" and event.payload.get("event") == "entity_retire"
    ]


def _condition(
    log: EventLog, key: str = "suspicious", expires_at: str = "2025-06-02 21:30"
):
    return entities.set_condition(
        log, SID, "player", key, expires_at=expires_at, source="caught at the gate"
    )


# -- the crossing turn -------------------------------------------------------


def test_a_condition_survives_the_turn_that_ends_before_its_expiry(log):
    """21:00 + travel = 21:25: the 21:30 expiry has NOT passed, so it stands."""
    condition = _condition(log)
    _seed_clock(log)

    update = _turn(log)

    assert parse_facts(update["facts"])["time"] == "21:25"
    assert _retires(log) == []
    assert entities.registry(log, SID).conditions[condition.id].expires_at == (
        "2025-06-02 21:30"
    )


def test_a_condition_retires_on_the_turn_whose_travel_crosses_it(log):
    """21:25 + travel = 21:50 crosses 21:30: the turn that crossed it retires it."""
    condition = _condition(log)
    _seed_clock(log)
    _turn(log)  # 21:25 — survives

    update = _turn(log)  # 21:50 — crossed

    assert parse_facts(update["facts"])["time"] == "21:50"
    retires = _retires(log)
    assert [row["id"] for row in retires] == [condition.id]
    # The tombstone is the clock's: engine actor, the instant it passed, no model.
    event = [
        e
        for e in log.get_events(SID)
        if e.type == "system" and e.payload.get("event") == "entity_retire"
    ][0]
    assert event.actor == "clock"
    assert event.payload["reason"] == "expired at 2025-06-02 21:30"
    # … and the fold drops it, so every reader (palette, tree, facts) agrees.
    assert entities.registry(log, SID).conditions == {}


def test_the_sweep_reads_the_advanced_clock_not_the_prior_fold(log):
    """The boundary case: 21:00 is short of 21:10, the advanced 21:10 is not.

    A sweep run BEFORE ``advance`` would leave this condition standing forever
    (it would compare against the prior turn's clock every time), so the pin is
    "the instant the sweep judges is the one the turn just moved to".
    """
    condition = _condition(log, expires_at="2025-06-02 21:10")
    _seed_clock(log)

    update = _turn(log, "I remark on the gate")  # dialogue: +10

    assert parse_facts(update["facts"])["time"] == "21:10"
    assert [row["id"] for row in _retires(log)] == [condition.id]


def test_the_sweep_is_idempotent_across_two_turns(log):
    """One expiry, one tombstone: the second sweep finds the condition already gone."""
    _condition(log)
    _seed_clock(log)
    _turn(log)
    _turn(log)  # 21:50 — retires
    _turn(log)  # 22:15 — nothing left to retire

    assert len(_retires(log)) == 1


def test_a_rest_across_midnight_retires_yesterdays_evening_expiry(log):
    """07:00 tomorrow is past a 23:30 expiry, and the date moves with the clock."""
    condition = _condition(log, expires_at="2025-06-02 23:30")
    _seed_clock(log)

    update = _turn(log, "I sleep by the fire")

    facts = parse_facts(update["facts"])
    assert (facts["time"], facts["date"]) == ("07:00", "Tuesday, June 03, 2025")
    assert [row["id"] for row in _retires(log)] == [condition.id]


def test_a_permanent_condition_is_never_swept(log):
    """An empty ``expires_at`` means permanent: no instant can pass it."""
    condition = _condition(log, expires_at="")
    _seed_clock(log)
    _turn(log)
    _turn(log)

    assert _retires(log) == []
    assert condition.id in entities.registry(log, SID).conditions


def test_a_scene_condition_is_swept_like_a_beings(log):
    """The host is irrelevant to the sweep — the clock judges every expiry."""
    condition = entities.set_condition(
        log, SID, entities.SCENE, "storm", expires_at="2025-06-02 21:30"
    )
    _seed_clock(log)
    _turn(log)
    _turn(log)

    assert [row["id"] for row in _retires(log)] == [condition.id]


# -- containment -------------------------------------------------------------


def test_a_sweep_that_raises_never_costs_the_turn(log, monkeypatch):
    """A sweep is bookkeeping BESIDE the turn: it can never fail ``pre_turn``."""

    def _boom(*args, **kwargs):
        raise RuntimeError("the store is having a day")

    monkeypatch.setattr(entities, "expire_conditions", _boom)
    condition = _condition(log)
    _seed_clock(log)

    update = _turn(log, "I sleep by the fire")

    assert parse_facts(update["facts"])["time"] == "07:00"
    assert condition.id in entities.registry(log, SID).conditions


def test_a_turn_with_no_seeded_clock_still_sweeps_on_the_defaults(log):
    """Turn 1 has no fold at all: the clock defaults, and the sweep still runs.

    ``advance`` supplies ``08:00`` on the epoch date, so the instant the sweep
    judges is the default one — a condition already past it retires on turn 1
    rather than waiting for a fold that has not been written yet.
    """
    condition = _condition(log, expires_at="2025-06-02 07:00")

    update = _turn(log, "I greet the merchant")  # dialogue: 08:00 -> 08:10

    assert parse_facts(update["facts"])["time"] == "08:10"
    assert [row["id"] for row in _retires(log)] == [condition.id]
