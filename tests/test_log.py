"""Unit tests for the SQLite event log (schema, events, repository)."""

import pytest

from fabulist.log import SCHEMA_VERSION, EventLog


def test_append_and_query_roundtrip(log):
    event = log.append_event("s1", "user", "user", {"text": "hello"}, meta={"k": "v"})

    assert event.id is not None
    assert event.seq == 1
    assert event.schema_version == SCHEMA_VERSION

    fetched = log.get_event(event.id)
    assert fetched == event
    assert log.get_events("s1") == [event]


def test_seq_monotonic_per_session(log):
    first = [log.append_event("s1", "user", "user", {"i": i}) for i in range(3)]
    second = [log.append_event("s2", "narrative", "narrator", {"i": i}) for i in range(2)]

    assert [e.seq for e in first] == [1, 2, 3]
    assert [e.seq for e in second] == [1, 2]


def test_revision_supersedes(log):
    original = log.append_event("s1", "narrative", "narrator", {"text": "v1"})
    revision = log.append_revision(
        "s1", original.id, "human", {"revised": {"text": "v2"}, "reason": "typo"}
    )

    assert revision.type == "revision"
    assert revision.supersedes == original.id

    # Original is retained in the raw query; the pointer moved to the revision.
    retained = [e for e in log.get_events("s1") if e.id == original.id][0]
    assert retained.payload == {"text": "v1"}
    assert retained.superseded_by == revision.id

    projection = log.get_projection("s1")
    assert [n["payload"]["text"] for n in projection["narratives"]] == ["v2"]
    assert len(projection["narratives"]) == 1  # the revision is not a narrative


def test_state_fold_last_write_wins(log):
    log.append_event("s1", "state-delta", "extractor", {"time": "08:00", "loc": "camp"})
    log.append_event("s1", "state-delta", "extractor", {"time": "09:00", "weather": "rain"})

    state = log.get_projection("s1")["state"]
    assert state == {"time": "09:00", "loc": "camp", "weather": "rain"}


def test_fork_diverges(log):
    log.append_event("base", "user", "user", {"text": "hi"})
    log.append_event("base", "narrative", "narrator", {"text": "hello"})
    log.append_event("base", "narrative", "narrator", {"text": "later"})

    log.fork_session("base", 2, "branch")
    assert log.list_branches("base") == ["branch"]

    # Fork projection is the base prefix up to at_seq (inclusive).
    before = log.get_projection("branch")
    assert [n["payload"]["text"] for n in before["narratives"]] == ["hello"]

    # Appending to the fork leaves the base projection untouched.
    forked = log.append_event("branch", "narrative", "narrator", {"text": "branch-only"})
    # issue #62: seq 3 (``base_seq + 1``) is the branch's own state snapshot row,
    # so the first branch WRITE continues from 4 -- still strictly ordered.
    assert forked.seq == 4

    base_projection = log.get_projection("base")
    assert [n["payload"]["text"] for n in base_projection["narratives"]] == [
        "hello",
        "later",
    ]

    fork_projection = log.get_projection("branch")
    assert [n["payload"]["text"] for n in fork_projection["narratives"]] == [
        "hello",
        "branch-only",
    ]

    # The base session's own rows are untouched by the fork.
    assert [e.seq for e in log.get_events("base")] == [1, 2, 3]


def test_branch_revision_does_not_leak_to_base(log):
    original = log.append_event("base", "narrative", "narrator", {"text": "v1"})
    log.append_event("base", "narrative", "narrator", {"text": "second"})
    log.fork_session("base", 1, "branch")

    log.append_revision(
        "branch",
        original.id,
        "human",
        {"revised": {"text": "v1-edit"}, "reason": "fix"},
    )

    branch = log.get_projection("branch")
    assert [n["payload"]["text"] for n in branch["narratives"]] == ["v1-edit"]

    base = log.get_projection("base")
    assert [n["payload"]["text"] for n in base["narratives"]] == ["v1", "second"]


def test_fork_rejects_existing_session(log):
    log.append_event("base", "user", "user", {"text": "hi"})
    log.fork_session("base", 1, "branch")
    with pytest.raises(ValueError):
        log.fork_session("base", 1, "branch")


def test_ephemeral_prune_removes_only_ephemeral(log):
    keep = log.append_event("s1", "narrative", "narrator", {"text": "keep"})
    drop = log.append_event(
        "s1", "narrative", "narrator", {"text": "drop"}, ephemeral=True
    )
    assert len(log.get_events("s1")) == 2

    assert log.prune_ephemeral("s1") == 1

    remaining = log.get_events("s1")
    assert [e.id for e in remaining] == [keep.id]
    assert log.get_event(drop.id) is None
    assert [n["payload"]["text"] for n in log.get_projection("s1")["narratives"]] == [
        "keep"
    ]


def test_mark_ephemeral_then_prune_session(log):
    log.append_event("s", "user", "user", {"text": "a"})
    log.append_event("s", "narrative", "narrator", {"text": "b"})

    assert log.mark_ephemeral("s") == 2
    assert log.prune_ephemeral("s") == 2
    assert log.get_projection("s")["narratives"] == []
    assert log.get_events("s") == []


def test_schema_version_gating(log):
    log.append_event("s1", "narrative", "narrator", {"text": "v1"})
    log.append_event(
        "s1",
        "narrative",
        "narrator",
        {"text": "future"},
        schema_version=SCHEMA_VERSION + 1,
    )

    current = log.get_projection("s1")
    assert [n["payload"]["text"] for n in current["narratives"]] == ["v1"]

    future = log.get_projection("s1", max_schema_version=SCHEMA_VERSION + 1)
    assert [n["payload"]["text"] for n in future["narratives"]] == ["v1", "future"]


def test_asset_add_get(log):
    log.add_asset("a1", "image", "/tmp/a.png", {"w": 64})

    asset = log.get_asset("a1")
    assert asset["kind"] == "image"
    assert asset["path"] == "/tmp/a.png"
    assert asset["meta"] == {"w": 64}
    assert asset["created_ts"]
    assert log.get_asset("missing") is None


def test_actor_recorded(log):
    event = log.append_event("s1", "roll", "clock", {"dice": "1d20", "result": 12})
    assert event.actor == "clock"
    assert log.get_event(event.id).actor == "clock"


def test_unknown_event_type_rejected(log):
    with pytest.raises(ValueError):
        log.append_event("s1", "bogus", "user", {})


def test_asset_event_appears_in_projection(log):
    log.append_event("s1", "asset", "media", {"asset_id": "a1", "kind": "image"})
    projection = log.get_projection("s1")
    assert [a["payload"]["asset_id"] for a in projection["assets"]] == ["a1"]


# --- D-F: validation notices in the projection -------------------------------------
#
# A flagged turn's teaching notice is a ``system``/``validation_notice`` row naming
# the narrative it refers to. The projection exposes it as ``{seq, flags, score?}``
# so the transcript can render it directly after that narrative — the visible half
# of the owner ruling (the prompt half reads the same rows via
# ``context.facts.validation_notices``).

NOTICE_FLAG = {
    "rule_id": "prose_markup",
    "check": "format",
    "severity": "warn",
    "message": "raw HTML detected in prose (left untouched)",
    "anchor": "<div>TERMINAL READY</div>",
    "fix": "remove the raw markup from prose",
}


def _append_notice(log, session_id, narrative, flags=None, score=None):
    payload = {
        "event": "validation_notice",
        "seq": narrative.seq,
        "flags": flags or [NOTICE_FLAG],
    }
    if score is not None:
        payload["score"] = score
    return log.append_event(session_id, "system", "validator", payload)


def test_projection_carries_notices_anchored_and_ordered_by_narrative_seq(log):
    log.append_event("s1", "user", "user", {"text": "hi"})
    first = log.append_event("s1", "narrative", "narrator", {"text": "one"})
    log.append_event("s1", "user", "user", {"text": "again"})
    second = log.append_event("s1", "narrative", "narrator", {"text": "two"})
    _append_notice(log, "s1", first, score=88)
    _append_notice(log, "s1", second)

    notices = log.get_projection("s1")["notices"]

    assert [notice["seq"] for notice in notices] == [first.seq, second.seq]
    assert notices[0]["seq"] == first.seq
    assert notices[0]["flags"] == [NOTICE_FLAG]
    assert notices[0]["score"] == 88
    assert "score" not in notices[1]  # a notice without a score carries no key


def test_projection_has_no_notices_for_a_clean_session(log):
    log.append_event("s1", "user", "user", {"text": "hi"})
    log.append_event("s1", "narrative", "narrator", {"text": "clean"})

    assert log.get_projection("s1")["notices"] == []


def test_projection_notices_follow_the_fork_chain(log):
    log.append_event("base", "user", "user", {"text": "hi"})
    narrative = log.append_event("base", "narrative", "narrator", {"text": "flagged"})
    _append_notice(log, "base", narrative)

    # A cut at the notice's own seq inherits both the narrative and its notice.
    log.fork_session("base", narrative.seq + 1, "branch")
    assert [n["seq"] for n in log.get_projection("branch")["notices"]] == [
        narrative.seq
    ]

    # A cut BEFORE the notice row inherits the narrative without it — a branch
    # never sees a notice its own history did not contain.
    log.fork_session("base", narrative.seq, "early")
    assert log.get_projection("early")["notices"] == []


def test_projection_notice_fold_is_latest_wins_per_anchor(log):
    log.append_event("s1", "narrative", "narrator", {"text": "flagged"})
    narrative = [e for e in log.get_events("s1") if e.type == "narrative"][0]
    _append_notice(log, "s1", narrative)
    revised_flag = dict(NOTICE_FLAG, fix="strip the tags")
    _append_notice(log, "s1", narrative, flags=[revised_flag], score=64)

    notices = log.get_projection("s1")["notices"]

    assert len(notices) == 1
    assert notices[0]["flags"] == [revised_flag]
    assert notices[0]["score"] == 64


def test_projection_notice_item_drops_rows_that_cannot_be_placed():
    # The append door rejects these, but the projection must not depend on the
    # row's honesty: an unplaceable or empty notice renders at no position.
    assert EventLog._validation_notice({"seq": 0, "flags": [NOTICE_FLAG]}) is None
    assert EventLog._validation_notice({"flags": [NOTICE_FLAG]}) is None
    assert EventLog._validation_notice({"seq": True, "flags": [NOTICE_FLAG]}) is None
    assert EventLog._validation_notice({"seq": 4, "flags": []}) is None
    assert EventLog._validation_notice({"seq": 4, "flags": ["prose_markup"]}) is None
    assert EventLog._validation_notice({"seq": 4, "flags": [NOTICE_FLAG]}) == {
        "seq": 4,
        "flags": [NOTICE_FLAG],
    }
