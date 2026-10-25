"""Tests for the narrator prompt replay rig (W3.5, second half).

The rig is fully offline: every test injects a ``build_facts`` callable and
records hashes with the same ``sha256(utf-8)[:16]`` convention wiring uses, so a
match/mismatch/unknown verdict is decided without a model or a live builder.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fabulist.eval.narrator_replay import (
    APPROXIMATION_NOTE,
    ReplaySummary,
    TurnReplay,
    facts_hash,
    log_facts_builder,
    main,
    render_summary,
    replay_facts_hashes,
    replay_report,
    replays_to_dict,
)
from fabulist.graph.clock import advance, classify_action, render_facts
from fabulist.log import EventLog
from fabulist.media.images import sha256_hex


def _event(
    seq: int,
    etype: str,
    *,
    payload: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A minimal stand-in for an Event row (the rig accepts dicts)."""
    return {"seq": seq, "type": etype, "payload": payload or {}, "meta": meta or {}}


def _narrative(
    seq: int,
    facts_hash_value: str | None,
    *,
    with_config: bool = True,
    legacy: bool = False,
) -> dict[str, Any]:
    """A narrative event carrying a ``meta["config"]`` provenance snapshot.

    Post-recording convention: ``recall`` rides config unconditionally
    (``""`` = no recall that turn). ``legacy=True`` omits it to model a turn
    recorded before the convention (classified legacy-unknown, not drift).
    """
    config: dict[str, Any] = {"model": "m", "sampler": {}, "prompt_version": 1}
    if facts_hash_value is not None:
        config["facts_hash"] = facts_hash_value
    if not legacy:
        config["recall"] = ""
    meta = {"config": config} if with_config else {}
    return _event(seq, "narrative", payload={"prose": "ok"}, meta=meta)


# --- hash convention --------------------------------------------------------


def test_facts_hash_matches_wiring_convention():
    text = "time: 14:35 | loc: market"
    assert facts_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    # Exactly what wiring does: sha256_hex(facts.encode("utf-8"))[:16].
    assert facts_hash(text) == sha256_hex(text.encode("utf-8"))[:16]
    assert facts_hash("") == sha256_hex(b"")[:16]


# --- match / mismatch / unknown --------------------------------------------


def test_match_case():
    text = "time: 14:35 | loc: market"
    events = [_event(1, "user", payload={"text": "hi"}), _narrative(2, facts_hash(text))]
    replays = replay_facts_hashes(events, lambda _events, _seq: text)

    assert len(replays) == 1
    replay = replays[0]
    assert (replay.user_seq, replay.narrative_seq) == (1, 2)
    assert replay.recorded_hash == facts_hash(text)
    assert replay.replayed_hash == facts_hash(text)
    assert replay.matches is True


def test_mismatch_case():
    events = [
        _event(1, "user", payload={"text": "hi"}),
        _narrative(2, facts_hash("time: 08:00")),
    ]
    replays = replay_facts_hashes(events, lambda _events, _seq: "time: 14:35")

    assert len(replays) == 1
    assert replays[0].recorded_hash == facts_hash("time: 08:00")
    assert replays[0].replayed_hash == facts_hash("time: 14:35")
    assert replays[0].matches is False


def test_unknown_when_no_config():
    events = [_event(1, "user"), _event(2, "narrative", meta={})]
    replays = replay_facts_hashes(events, lambda _events, _seq: "anything")

    assert replays[0].recorded_hash is None
    assert replays[0].replayed_hash == facts_hash("anything")
    assert replays[0].matches is None


def test_unknown_when_config_lacks_facts_hash():
    events = [
        _event(1, "user"),
        _event(2, "narrative", meta={"config": {"model": "m"}}),
    ]
    replays = replay_facts_hashes(events, lambda _events, _seq: "anything")
    assert replays[0].recorded_hash is None
    assert replays[0].matches is None


def test_unknown_when_config_is_not_a_dict():
    events = [_event(1, "user"), _event(2, "narrative", meta={"config": "nope"})]
    replays = replay_facts_hashes(events, lambda _events, _seq: "anything")
    assert replays[0].matches is None


# --- pairing + window -------------------------------------------------------


def test_user_without_narrative_is_ignored():
    events = [_event(1, "user", payload={"text": "hi"})]
    assert replay_facts_hashes(events, lambda _events, _seq: "") == []


def test_narrative_without_user_is_skipped():
    events = [_narrative(1, facts_hash("x"))]
    assert replay_facts_hashes(events, lambda _events, _seq: "x") == []


def test_build_facts_receives_events_up_to_turn_and_seq():
    seen: list[tuple[list[int], int]] = []

    def build(events_up_to: list[Any], narrative_seq: int) -> str:
        seen.append(([event["seq"] for event in events_up_to], narrative_seq))
        return ""

    events = [
        _event(1, "user"),
        _narrative(2, facts_hash("")),
        _event(3, "state-delta", payload={"time": "14:35"}),
        _event(4, "user"),
        _narrative(5, facts_hash("")),
    ]
    replay_facts_hashes(events, build)

    # Turn 1 window is seq<=2 (its own post-narrative delta seq3 is excluded);
    # turn 2 window includes the delta and the second user.
    assert seen == [([1, 2], 2), ([1, 2, 3, 4, 5], 5)]


def test_events_are_sorted_by_seq_before_pairing():
    events = [
        _narrative(2, facts_hash("x")),
        _event(1, "user"),
    ]
    replays = replay_facts_hashes(events, lambda _events, _seq: "x")
    assert len(replays) == 1
    assert replays[0].user_seq == 1
    assert replays[0].narrative_seq == 2


def test_swipe_narratives_each_replay_against_the_same_user():
    events = [
        _event(1, "user"),
        _narrative(2, facts_hash("x")),
        _narrative(3, facts_hash("x")),
    ]
    replays = replay_facts_hashes(events, lambda _events, _seq: "x")
    assert [replay.narrative_seq for replay in replays] == [2, 3]
    assert all(replay.user_seq == 1 for replay in replays)


# --- summary math -----------------------------------------------------------


def _three_turn_events() -> list[dict[str, Any]]:
    return [
        _event(1, "user"),
        _narrative(2, facts_hash("A")),
        _event(3, "user"),
        _narrative(4, facts_hash("B")),
        _event(5, "user"),
        _narrative(6, None, with_config=False),
    ]


def test_summary_math_counts_each_bucket():
    replays = replay_facts_hashes(
        _three_turn_events(), lambda _events, seq: {2: "A", 4: "C", 6: "D"}[seq]
    )
    summary = replay_report(replays)

    assert summary.turns == 3
    assert summary.matches == 1
    assert summary.mismatches == 1
    assert summary.unknowns == 1
    assert summary.turns == summary.matches + summary.mismatches + summary.unknowns
    assert summary.passes() is False


def test_unknowns_do_not_fail_the_run():
    replays = replay_facts_hashes(
        _three_turn_events(), lambda _events, seq: {2: "A", 4: "B", 6: "D"}[seq]
    )
    summary = replay_report(replays)

    assert summary.matches == 2
    assert summary.mismatches == 0
    assert summary.unknowns == 1
    assert summary.passes() is True


def test_empty_run_passes():
    summary = replay_report([])
    assert summary == ReplaySummary(turns=0, matches=0, mismatches=0, unknowns=0)
    assert summary.passes() is True


# --- rendering --------------------------------------------------------------


def test_render_summary_is_stable_text():
    summary = ReplaySummary(turns=3, matches=1, mismatches=1, unknowns=1)
    text = render_summary(summary)
    assert text == render_summary(summary)
    assert text == (
        "Fabulist narrator prompt replay — 3 turns: 1 match, 1 mismatch, 1 unknown\n"
        "RESULT: FAIL"
    )
    assert render_summary(ReplaySummary(2, 2, 0, 0)).endswith("RESULT: PASS")


def test_replays_to_dict_statuses():
    replays = replay_facts_hashes(
        _three_turn_events(), lambda _events, seq: {2: "A", 4: "C", 6: "D"}[seq]
    )
    payload = replays_to_dict(replays, replay_report(replays))

    assert payload["turns"] == 3
    assert payload["passes"] is False
    assert [row["status"] for row in payload["results"]] == [
        "match",
        "mismatch",
        "unknown",
    ]
    assert payload["results"][2]["recorded_hash"] is None


# --- CLI builder + entry point ---------------------------------------------


def test_log_facts_builder_folds_state_and_renders_clock_facts(tmp_path):
    # Full-fidelity builder: the clock facts line joins the real suffix
    # (persona/roster/WI absent for a bare session -> facts-only suffix).
    log = EventLog(tmp_path / "replay.db")
    log.append_event("s1", "user", "user", {"text": "hello"})
    build = log_facts_builder(log, "s1")
    events = [
        _event(1, "state-delta", payload={"time": "14:35", "loc": "market"}),
        _event(2, "state-delta", payload={"time": "14:45"}),
    ]
    suffix = build(events, 2)
    # full-fidelity: the facts part is the ADVANCED <state> block (pre_turn runs
    # classify+advance; "go" -> travel -> +10min from the folded 14:45)
    assert "<state>" in suffix
    assert render_facts({"time": "14:55", "loc": "market"}) in suffix
    # no log (None) -> blank, documented
    assert log_facts_builder()([] if False else events, 2) == "" or True
    no_log = log_facts_builder()
    assert no_log(events, 2) == ""
    # state-less session -> the clock advances from the default (pre_turn does
    # the same unconditionally): a first turn is NOT a blank suffix.
    log2 = EventLog(tmp_path / "replay2.db")
    log2.append_event("sx", "user", "user", {"text": "hi"})
    build2 = log_facts_builder(log2, "sx")
    first = build2([_event(1, "user", payload={"text": "hi"})], 1)
    assert first == advance(classify_action("hi"), "").facts


def test_legacy_turn_is_unknown_not_drift():
    """A turn recorded before recall-recording is classified unknown, not drift."""
    text = "clock"
    events = [_event(1, "user", payload={"text": "hi"}), _narrative(2, facts_hash(text), legacy=True)]
    replays = replay_facts_hashes(events, lambda window, seq: text)
    assert len(replays) == 1
    assert replays[0].recorded_hash == facts_hash(text)
    assert replays[0].replayed_hash == facts_hash(text)
    # identical hashes, but no recorded recall -> unverifiable (legacy), never drift
    assert replays[0].matches is None


def _advanced_hash(user_text: str, prior: str = "time: 14:35") -> str:
    """The hash the LIVE convention records: the pre-turn clock advance of the
    folded state (classify + advance — the same deterministic rules engine
    pre_turn runs), then facts_hash. ``prior`` is empty for a first turn (the
    clock falls back to its default and STILL advances)."""
    from fabulist.graph.clock import advance, classify_action

    advanced = advance(classify_action(user_text), prior).facts
    return facts_hash(advanced)


def _seed_session(path: Path, session_id: str, second_hash: str) -> None:
    """A two-turn session: turn 1 advances from the clock default, turn 2 folds
    ``time: 14:35``."""
    log = EventLog(path)
    try:
        log.append_event(session_id, "user", "user", {"text": "hello"})
        log.append_event(
            session_id,
            "narrative",
            "narrator",
            {"prose": "hi"},
            meta={"config": {"facts_hash": _advanced_hash("hello", ""), "recall": ""}},
        )
        log.append_event(session_id, "state-delta", "extractor", {"time": "14:35"})
        log.append_event(session_id, "user", "user", {"text": "go"})
        log.append_event(
            session_id,
            "narrative",
            "narrator",
            {"prose": "ok"},
            meta={"config": {"facts_hash": second_hash, "recall": ""}},
        )
    finally:
        log.close()


def test_main_exits_zero_when_closure_matches(tmp_path, capsys):
    log_path = tmp_path / "events.db"
    _seed_session(log_path, "s1", _advanced_hash("go"))

    code = main(["--log", str(log_path), "--session", "s1"])
    captured = capsys.readouterr()

    assert code == 0
    assert "2 turns: 2 match, 0 mismatch, 0 unknown" in captured.out
    assert "RESULT: PASS" in captured.out
    assert APPROXIMATION_NOTE in captured.err


def test_main_exits_one_on_mismatch_and_writes_json(tmp_path, capsys):
    log_path = tmp_path / "events.db"
    _seed_session(log_path, "s1", "deadbeefdeadbeef")  # deliberate drift
    out_path = tmp_path / "report.json"

    code = main(["--log", str(log_path), "--session", "s1", "--json", str(out_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "RESULT: FAIL" in captured.out
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["turns"] == 2
    assert payload["mismatches"] == 1
    assert payload["passes"] is False
    assert [row["status"] for row in payload["results"]] == ["match", "mismatch"]


def test_main_unknown_session_returns_two(tmp_path, capsys):
    log_path = tmp_path / "events.db"
    _seed_session(log_path, "s1", _advanced_hash("go"))

    code = main(["--log", str(log_path), "--session", "ghost"])
    captured = capsys.readouterr()

    assert code == 2
    assert "unknown session" in captured.err


def test_main_missing_log_returns_two(tmp_path, capsys):
    code = main(["--log", str(tmp_path / "nope.db"), "--session", "s1"])
    captured = capsys.readouterr()

    assert code == 2
    assert "does not exist" in captured.err
    assert not (tmp_path / "nope.db").exists()


def test_log_facts_builder_threads_macro_variables(tmp_path):
    """Replay renders card macros like the live builder (D43 parity).

    The live builder resolves the fixed vocabulary before hashing the canonical
    context, so the replay rig must too — otherwise a macro-bearing card drifts
    (recorded hash carries the rendered name, replay the literal token).
    """
    log = EventLog(tmp_path / "macro.db")
    sid = "s"
    card = {
        "name": "Rex",
        "description": '{{user}} is a new recruit - "Assistant (to the) Captain".',
        "scenario": "Aboard the {{world}}.",
        "first_mes": "Welcome aboard, {{user}}.",
    }
    log.append_event(sid, "system", "system", {"card": card, "first_mes": card["first_mes"]})
    from fabulist.personas.registry import PERSONAS_ANCHOR

    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": "p1", "name": "Alice"},
    )
    log.append_event(sid, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    log.append_event(
        sid, "system", "system", {"event": "world_ref", "world_id": "w1", "name": "Epoch 27"}
    )
    log.append_event(sid, "user", "user", {"text": "I report to the bridge."})
    events = [_event(e.seq, e.type, payload=e.payload) for e in log.get_events(sid)]
    suffix = log_facts_builder(log, sid)(events, events[-1]["seq"])
    assert "Alice is a new recruit" in suffix
    assert "{{user}}" not in suffix
    assert "Aboard the Epoch 27." in suffix
