"""A fork persists the WHOLE session state into the new chat (issue #62).

Owner ruling: "We should be persisting the entire session state, all objects,
everything - into the new chat. It is as important as the preceding history
itself." Chosen shape (owner's option 1): fold through the projection log
properly rather than inventing a parallel store or a second fold.

What this pins:

* the fork writes ONE ``system``/``session_state_snapshot`` row into the NEW
  session, in ``fork_session``'s transaction (the one chokepoint every fork
  goes through -- the fork route, delete-tail, compaction's era successor);
* its content is the state AT THE CUT, read through the existing projections
  (``get_projection(at_seq=...)``, ``entities.registry``, ``load_last_plan``)
  over a cut view -- so the entity registry, the plan and every tracked object
  ride along, and NOTHING above the cut does;
* reads resolve the branch's own snapshot FIRST and fall back to the parent
  chain, so a pre-#62 branch (no snapshot row) keeps working unchanged;
* the row is append-only and revisable like every other row (ADR-001/002).

Test order follows the acceptance list in the issue.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fabulist.api import create_app
from fabulist.graph.plans import Plan, load_last_plan, save_plan
from fabulist.log import EventLog
from fabulist.log.repo import (
    STATE_SNAPSHOT_EVENT,
    is_state_snapshot_event,
)
from fabulist.state.entities import (
    PLAYER,
    establish_item,
    establish_location,
    establish_thread,
    move_being,
    registry,
    rendered_state,
    set_attribute,
)


SESSION = "chat"


# -- helpers ----------------------------------------------------------------
def _turn(log: EventLog, sid: str, text: str = "I step into the market.") -> int:
    """Append one user turn + reply; return the narrative's ``seq`` (the cut)."""
    log.append_event(sid, "user", "user", {"text": text})
    reply = log.append_event(
        sid, "narrative", "narrator", {"prose": "Lanterns sway.", "segments": []}
    )
    return reply.seq


def _plan(turn_seq: int, *strings: str) -> Plan:
    return Plan(
        turn_seq=turn_seq,
        created_from="graph",
        strings=list(strings),
        datetime="Evening, the market",
        weather="rain",
        established=["the debt is unpaid"],
        extras={"currency": "40 silver"},
    )


def _seed_state(log: EventLog, sid: str) -> None:
    """The scene the cut inherits: state, registry objects, a plan."""
    log.append_event(
        sid, "state-delta", "extractor", {"time": "18:20", "forecast": "rain"}
    )
    market = establish_location(log, sid, "Night Market")
    move_being(log, sid, PLAYER, market)
    establish_thread(log, sid, "The unpaid debt", detail="a lender waits")
    establish_item(log, sid, "brass lantern", holder=PLAYER)
    set_attribute(log, sid, PLAYER, key="resolve", value=7)
    save_plan(log, sid, _plan(2, "the lender names a price"))


def _snapshot_row(log: EventLog, sid: str):
    """The session's own snapshot row (``None`` when it has none)."""
    rows = [e for e in log.get_events(sid) if is_state_snapshot_event(e)]
    return rows[-1] if rows else None


# -- 1. the fork persists the full state at the cut -------------------------
def test_fork_persists_the_full_state_at_the_cut(log: EventLog) -> None:
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)

    log.fork_session(SESSION, cut, "branch")

    row = _snapshot_row(log, "branch")
    assert row is not None
    assert row.type == "system"
    payload = row.payload
    assert payload["event"] == STATE_SNAPSHOT_EVENT
    assert payload["at_seq"] == cut

    # ... the projection state (clock + the manual forecast pin)
    assert payload["state"]["time"] == "18:20"
    assert payload["state"]["forecast"] == "rain"

    # ... the entity registry: every object kind the session held
    reg = payload["registry"]
    assert [t["name"] for t in reg["threads"].values()] == ["The unpaid debt"]
    assert [i["name"] for i in reg["items"].values()] == ["brass lantern"]
    assert [m["key"] for m in reg["meters"].values()] == ["resolve"]
    assert sorted(loc["name"] for loc in reg["locations"].values()) == [
        "Night Market",
        "off-screen",
    ]
    assert reg["assignments"][PLAYER] in reg["locations"]

    # ... and the plan (it sits below the cut, so it is inherited state)
    assert payload["plan"]["strings"] == ["the lender names a price"]
    assert payload["plan"]["turn_seq"] == 2


def test_the_persisted_row_is_the_branchs_first_own_row(log: EventLog) -> None:
    """seq bookkeeping stays ordered: the snapshot takes ``base_seq + 1``."""
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    log.fork_session(SESSION, cut, "branch")

    own = log.get_events("branch")
    assert len(own) == 1
    assert is_state_snapshot_event(own[0])
    assert own[0].seq == cut + 1
    # No history was copied (F2 stays true): the branch owns no prose/user row.
    assert all(event.type not in ("user", "narrative") for event in own)


# -- 2. a fork BELOW a newer plan does not inherit it -----------------------
def test_fork_below_a_newer_plan_does_not_inherit_it(log: EventLog) -> None:
    log.append_event(
        SESSION, "state-delta", "extractor", {"time": "18:20", "forecast": "rain"}
    )
    save_plan(log, SESSION, _plan(1, "the old heading"))
    cut = _turn(log, SESSION)
    # Above the cut: the session moved on after the position being forked.
    save_plan(log, SESSION, _plan(9, "the NEW heading"))
    log.append_event(SESSION, "state-delta", "extractor", {"time": "23:45"})

    log.fork_session(SESSION, cut, "branch")

    payload = _snapshot_row(log, "branch").payload
    assert payload["plan"]["strings"] == ["the old heading"]
    assert payload["plan"]["turn_seq"] == 1
    # The cut's clock, not the parent's current one.
    assert payload["state"]["time"] == "18:20"

    # And the live plan read on the branch agrees: no re-plan inside the window
    # (the fork inherits the plan that was in force at the cut).
    inherited = load_last_plan(log, "branch")
    assert inherited is not None
    assert inherited.strings == ["the old heading"]


def test_fork_at_the_end_inherits_everything(log: EventLog) -> None:
    _seed_state(log, SESSION)
    tip = _turn(log, SESSION)

    log.fork_session(SESSION, tip, "branch")

    payload = _snapshot_row(log, "branch").payload
    parent_projection = log.get_projection(SESSION)
    parent_registry = registry(log, SESSION)
    parent_plan = load_last_plan(log, SESSION)

    assert payload["state"] == parent_projection["state"]
    assert payload["registry"]["threads"].keys() == parent_registry.threads.keys()
    assert payload["registry"]["items"].keys() == parent_registry.items.keys()
    assert payload["registry"]["meters"].keys() == parent_registry.meters.keys()
    assert payload["registry"]["assignments"] == parent_registry.assignments
    assert payload["plan"] == parent_plan.to_dict()


# -- 3. nothing above the cut is copied ------------------------------------
def test_nothing_above_the_cut_is_copied(log: EventLog) -> None:
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    before = registry(log, SESSION)

    # Everything below would be a leak: a later turn, a later delta, a later
    # thread/meter/item, and a newer plan.
    _turn(log, SESSION, "I go upstairs.")
    log.append_event(SESSION, "state-delta", "extractor", {"time": "02:00"})
    establish_thread(log, SESSION, "A brand new thread")
    set_attribute(log, SESSION, PLAYER, key="resolve", value=99)
    establish_item(log, SESSION, "a stolen key", holder=PLAYER)
    save_plan(log, SESSION, _plan(7, "the LATE heading"))
    after = registry(log, SESSION)
    assert after.threads.keys() != before.threads.keys()  # the fixture moved

    log.fork_session(SESSION, cut, "branch")

    payload = _snapshot_row(log, "branch").payload
    assert payload["state"] == {"time": "18:20", "forecast": "rain"}
    assert [t["name"] for t in payload["registry"]["threads"].values()] == [
        "The unpaid debt"
    ]
    assert [i["name"] for i in payload["registry"]["items"].values()] == [
        "brass lantern"
    ]
    assert [m["value"] for m in payload["registry"]["meters"].values()] == [7]
    assert payload["plan"]["strings"] == ["the lender names a price"]

    # The branch's projection carries the cut's history only ...
    projection = log.get_projection("branch")
    assert [n["payload"]["prose"] for n in projection["narratives"]] == [
        "Lanterns sway."
    ]
    # ... and it owns exactly one row: the snapshot (nothing was copied in).
    assert len(log.get_events("branch")) == 1


# -- 4. the snapshot reads without walking the parent ----------------------
def test_the_snapshot_reads_without_walking_the_parent(
    log: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    log.fork_session(SESSION, cut, "branch")
    expected = _snapshot_row(log, "branch").payload

    def _no_chain(*_args, **_kwargs):
        raise AssertionError("a branch's own snapshot must not walk the parent")

    monkeypatch.setattr(EventLog, "get_chain_events", _no_chain)
    monkeypatch.setattr(EventLog, "_stream_events", _no_chain)

    snapshot = log.get_state_snapshot("branch")
    assert snapshot is not None
    assert snapshot["state"] == expected["state"]
    assert snapshot["registry"].keys() == expected["registry"].keys()

    # The public read prefers it too, and says so.
    state = log.get_session_state("branch")
    assert state["persisted"] is True
    assert state["plan"]["strings"] == ["the lender names a price"]


def test_a_pre_change_branch_still_reads_from_the_chain(log: EventLog) -> None:
    """A branch forked BEFORE issue #62 owns no snapshot: the chain is the fallback."""
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    log.fork_session(SESSION, cut, "branch")

    # Simulate the pre-change row set: a fork that wrote only its bookkeeping.
    log._conn.execute(
        "DELETE FROM events WHERE session_id = ? AND payload LIKE ?",
        ("branch", f'%"event": "{STATE_SNAPSHOT_EVENT}"%'),
    )
    assert log.get_events("branch") == []

    assert log.get_state_snapshot("branch") is None
    state = log.get_session_state("branch")
    assert state["persisted"] is False
    # Same answers, folded from the chain exactly as before the change.
    assert state["state"]["time"] == "18:20"
    assert [t["name"] for t in state["registry"]["threads"].values()] == [
        "The unpaid debt"
    ]
    assert state["plan"]["strings"] == ["the lender names a price"]
    assert log.get_projection("branch")["state"]["forecast"] == "rain"


# -- 5. registry / plan / tracks survive on the branch ---------------------
def test_registry_plan_and_tracks_survive_on_the_branch(log: EventLog) -> None:
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    at_cut = registry(log, SESSION)
    facts_at_cut = rendered_state(log, SESSION)

    log.fork_session(SESSION, cut, "branch")

    branch_registry = registry(log, "branch")
    assert branch_registry.threads.keys() == at_cut.threads.keys()
    assert branch_registry.items.keys() == at_cut.items.keys()
    assert branch_registry.meters.keys() == at_cut.meters.keys()
    assert branch_registry.assignments == at_cut.assignments
    assert list(branch_registry.threads.values())[0].detail == "a lender waits"
    assert rendered_state(log, "branch") == facts_at_cut
    assert load_last_plan(log, "branch").strings == ["the lender names a price"]


# -- 6. the read precedence: snapshot first, parent chain as fallback ------
def test_branch_state_wins_over_a_late_parent_revision(log: EventLog) -> None:
    """A revision written to an INHERITED row after the fork must not move it.

    ``_stream_events`` carries an ancestor's late revision across the fork
    boundary (F7), so without the persisted snapshot the branch's committed
    state would silently follow the parent's later edit. The snapshot is what
    the branch was cut from, so it wins.
    """
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    delta = next(e for e in log.get_events(SESSION) if e.type == "state-delta")
    log.fork_session(SESSION, cut, "branch")
    assert log.get_projection("branch")["state"]["time"] == "18:20"

    log.append_revision(
        SESSION, delta.id, "user", {"revised": {"time": "23:59"}, "reason": "edit"}
    )

    assert log.get_projection(SESSION)["state"]["time"] == "23:59"  # parent moved
    assert log.get_projection("branch")["state"]["time"] == "18:20"  # branch did not


def test_the_snapshot_is_revisable_like_every_other_row(log: EventLog) -> None:
    """ADR-002: a correction APPENDS a revision; reads render latest-wins."""
    _seed_state(log, SESSION)
    cut = _turn(log, SESSION)
    log.fork_session(SESSION, cut, "branch")
    row = _snapshot_row(log, "branch")

    revised = dict(row.payload)
    revised["state"] = {**revised["state"], "time": "19:05"}
    log.append_revision(
        "branch", row.id, "user", {"revised": revised, "reason": "clock was wrong"}
    )

    assert log.get_state_snapshot("branch")["state"]["time"] == "19:05"
    assert log.get_projection("branch")["state"]["time"] == "19:05"
    # Append-only: the original row is still there, superseded, not rewritten.
    assert log.get_event(row.id).superseded_by is not None


# -- 7. the routes fork through the same seam ------------------------------
@pytest.fixture
def build_app(tmp_path):
    """One tmp log, apps built on demand (the branches router is mounted)."""
    log_path = tmp_path / "events.db"
    built = []

    def _build():
        app = create_app(None, log_path)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def _run(coro):
    return asyncio.run(coro)


def test_the_fork_route_persists_the_state(build_app) -> None:
    build, _log_path = build_app
    app = build()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post("/api/sessions", json={"title": "Market"})
            session = created.json()["session_id"]
            log = app.state.log
            _seed_state(log, session)
            cut = _turn(log, session)
            resp = await client.post(
                f"/api/sessions/{session}/fork",
                json={"at_seq": cut, "new_session_id": "forked"},
            )
            return resp

    resp = _run(scenario())
    assert resp.status_code == 200, resp.text
    payload = _snapshot_row(app.state.log, "forked").payload
    assert payload["at_seq"] == resp.json()["base_seq"]
    assert [t["name"] for t in payload["registry"]["threads"].values()] == [
        "The unpaid debt"
    ]
