"""Machinery pin: an ancestor's chain bound is the TIGHTEST descendant cut.

``EventLog._session_chain`` returns root-first ``(session_id, upper_bound)``.
The upper bound is what a branch may see of an ancestor: for a normal fork it is
the ``base_seq`` the child recorded, but a fork BELOW that child's own
``base_seq`` (issue #35: delete-tail from a branch, where the pivot is an
INHERITED row) makes the descendant's window tighter than the cut its parent
recorded. The ancestor must then be bounded by the minimum, never by its child's
stored ``base_seq`` -- otherwise the rows above the tighter cut stay visible and
a "deleted" message reappears on the new line.

These tests exercise the rule directly (no API, no endpoint): every derived
fork-aware read (``get_chain_events``, ``get_projection``, HUD/state folds)
inherits it.
"""

from __future__ import annotations

import pytest

from fabulist.log import EventLog


@pytest.fixture
def log(tmp_path):
    repo = EventLog(tmp_path / "events.db")
    yield repo
    repo.close()


def _seed(log: EventLog, sid: str, count: int) -> None:
    for index in range(count):
        log.append_event(sid, "user", "user", {"text": f"{sid}-{index}"})


def test_ordinary_nested_fork_bounds_by_the_child_base_seq(log):
    """Baseline (unchanged): a normal nested fork bounds each node at its cut."""
    _seed(log, "root", 4)
    log.fork_session("root", 4, "mid")
    _seed(log, "mid", 1)  # own row at seq 5
    log.fork_session("mid", 5, "leaf")

    assert log._session_chain("root") == [("root", None)]
    assert log._session_chain("mid") == [("root", 4), ("mid", None)]
    assert log._session_chain("leaf") == [
        ("root", 4),
        ("mid", 5),
        ("leaf", None),
    ]


def test_fork_below_the_child_base_seq_tightens_every_ancestor(log):
    """The regression: the root sees no row above the DESCENDANT's cut."""
    _seed(log, "root", 6)
    log.fork_session("root", 6, "mid")
    log.fork_session("mid", 2, "leaf")  # below mid's stored base_seq (6)

    # Old rule: [("root", 6), ("mid", 2), ...] -- root rows 3..6 leaked.
    assert log._session_chain("leaf") == [
        ("root", 2),
        ("mid", 2),
        ("leaf", None),
    ]
    # The intermediate branch keeps its own fuller window.
    assert log._session_chain("mid") == [("root", 6), ("mid", None)]

    chain = log.get_chain_events("leaf")
    assert [(event.session_id, event.seq) for event in chain] == [
        ("root", 1),
        ("root", 2),
    ]
    # And the middle branch still inherits through its own cut.
    assert len(log.get_chain_events("mid")) == 6


def test_chain_bound_is_the_minimum_across_a_multi_level_path(log):
    """Three nested cuts: every ancestor is bounded by the deepest one."""
    _seed(log, "root", 10)
    log.fork_session("root", 8, "mid")
    log.fork_session("mid", 5, "leaf")
    log.fork_session("leaf", 3, "deep")

    # min(8, min(5, 3)) = 3 -- not root's child cut (8) nor leaf's own (5).
    assert log._session_chain("deep") == [
        ("root", 3),
        ("mid", 3),
        ("leaf", 3),
        ("deep", None),
    ]
    assert [event.seq for event in log.get_chain_events("deep")] == [1, 2, 3]
