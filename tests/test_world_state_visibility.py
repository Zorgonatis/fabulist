"""User-gated admission visibility (wave close, item 12 integration).

A BTS-detected character may live in the BTS ledger (``world_state`` events)
while its ``kind=cast`` proposal is pending — or after the owner rejected it.
Those names must never surface in the USER-FACING world-state reads:

* ``cast_api._bts_cast`` (the cast list's BTS layer)
* ``cast_api._world_state_rows`` (the dashboard history)
* ``state_api._fold_world_state`` (the projected fold)

Accepting the proposal (a ``cast_admit``) makes the name visible again; undo
(a ``cast_retire`` tombstone) hides it. Names with neither proposal nor member
(legacy entities, venues) are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass

from fabulist.api.cast_api import _bts_cast, _world_state_rows
from fabulist.api.state_api import _fold_world_state
from fabulist.state.cast import unaccepted_candidate_names


@dataclass
class _E:
    """Minimal event stand-in both folds accept (type/payload/seq/ts/id)."""

    seq: int
    type: str
    payload: dict
    ts: str = "2026-10-19T00:00:00Z"
    id: int = 0

    def __post_init__(self) -> None:
        self.id = self.seq


def _ws(seq: int, names: list[str]) -> _E:
    return _E(seq, "system", {"event": "world_state", "kind": "delta",
                              "state": {n: {"emotional": {"mood": "calm"}} for n in names}})


def _cast_proposal(seq: int, name: str) -> _E:
    return _E(seq, "system", {"event": "asset_proposal", "kind": "cast",
                              "action": "add", "target": None,
                              "patch": {"name": name, "aliases": [], "origin": "bts"},
                              "origin": "bts"})


def _admit(seq: int, member_id: str, name: str) -> _E:
    return _E(seq, "system", {"event": "cast_admit", "member_id": member_id,
                              "name": name, "provenance": "actor",
                              "admitted_by": "owner"})


def _retire(seq: int, member_id: str, name: str) -> _E:
    return _E(seq, "system", {"event": "cast_retire", "member_id": member_id,
                              "name": name, "retired_by": "owner"})


class _Log:
    def __init__(self, events: list[_E]) -> None:
        self._events = events

    def get_session(self, session_id: str):
        return {"base_session": None}

    def get_chain_events(self, session_id: str) -> list[_E]:
        return list(self._events)

    def get_events(self, session_id: str) -> list[_E]:
        return list(self._events)


def test_pending_candidate_hidden_accepted_visible():
    events = [
        _ws(1, ["Hobbs", "Marisol", "The Shop"]),
        _cast_proposal(2, "Marisol"),
    ]
    assert unaccepted_candidate_names(events) == {"Marisol"}
    assert _bts_cast(events) == ["Hobbs", "The Shop"]
    assert set(_fold_world_state(_Log(events), "s").state if hasattr(_fold_world_state(_Log(events), "s"), "state") else _fold_world_state(_Log(events), "s")["state"]) == {"Hobbs", "The Shop"}
    rows = _world_state_rows(_Log(events), "s")
    assert rows and set(rows[-1]["state"]) == {"Hobbs", "The Shop"}

    accepted = events + [_admit(3, "cm_x", "Marisol")]
    assert unaccepted_candidate_names(accepted) == set()
    assert _bts_cast(accepted) == ["Hobbs", "Marisol", "The Shop"]


def test_rejected_then_retired_stays_hidden():
    events = [
        _ws(1, ["Hobbs", "Marisol"]),
        _cast_proposal(2, "Marisol"),
        _admit(3, "cm_x", "Marisol"),
        _retire(4, "cm_x", "Marisol"),
    ]
    assert unaccepted_candidate_names(events) == {"Marisol"}
    assert _bts_cast(events) == ["Hobbs"]


def test_no_proposal_no_filter():
    """Legacy entities and venues never gain or lose visibility."""
    events = [_ws(1, ["Hobbs", "Tessa", "Vale & Co."])]
    assert unaccepted_candidate_names(events) == set()
    assert _bts_cast(events) == ["Hobbs", "Tessa", "Vale & Co."]
