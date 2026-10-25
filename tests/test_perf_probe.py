"""The Stage 0 measurement harness (PLAN W9.24 §3.1) — fold logic + CLI surface.

Every test here works on SEEDED event fixtures and FAKE streams: no engine, no
model, no log file. The live half (real turns and real reference calls against
``:4444``) is verified by running ``scripts/perf_probe.py`` itself, which is what
the wave's acceptance requires.

The folds pinned below are the probe's whole value: the stage-frame fold with the
``plan``/``lanes`` model overlay, the usage-row call fold (including the
capture-less decision rows), the interaction -> stage assignment,
``prefix_signal`` (the derived prompt-cache signal), the post-turn hook
approximation, and the two Stage 0 experiments — the contention comparison
(serial-sum vs concurrent wall) and the prompt-cache pair (first vs second call
over one identical prefix).
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # scripts/ is a namespace package, not installed
    sys.path.insert(0, str(_ROOT))

from scripts import perf_probe as pp  # noqa: E402
from scripts import turn_trace as tt  # noqa: E402


# ---------------------------------------------------------------------------
# seeded fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Event:
    """The duck-typed log row the folds read (``seq``/``type``/``payload``/``ts``)."""

    seq: int
    type: str
    actor: str
    payload: dict
    ts: str = "2026-01-01T00:00:00+00:00"
    ephemeral: bool = False
    meta: dict | None = None
    id: int = 0
    supersedes: int | None = None
    session_id: str = "sess"


def _usage(
    seq: int,
    *,
    agent: str = "narrator",
    model: str = "your-model-id",
    prompt: int = 100,
    completion: int = 50,
    latency: float = 1.0,
    ts: str = "2026-01-01T00:00:00+00:00",
) -> _Event:
    return _Event(
        seq=seq,
        type="system",
        actor=agent,
        payload={
            "event": "usage",
            "agent": agent,
            "model": model,
            "session_id": "sess",
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "latency_s": latency,
        },
        ts=ts,
    )


def _capture(
    seq: int,
    interaction: str,
    *,
    messages: list[dict] | None = None,
    latency: float = 1.0,
    finish: str = "stop",
) -> _Event:
    return _Event(
        seq=seq,
        type="system",
        actor=interaction,
        payload={
            "event": "agent_call",
            "agent": interaction,
            "messages": messages
            if messages is not None
            else [{"role": "system", "content": "sys"}, {"role": "user", "content": "ask"}],
            "response_text": "out",
            "reasoning_text": "",
            "finish": finish,
            "latency_s": latency,
        },
    )


def _frame(step: str, state: str, t: float, *, label: str = "", index: int = 1,
           total: int = 8) -> tt.StageFrame:
    return tt.StageFrame(step=step, state=state, label=label or step, index=index,
                         total=total, t=t)


def _clean_turn_events() -> list[_Event]:
    """One clean in-character turn as the log writes it: planner -> narrate ->
    decision gate -> critic -> bts, with the decision gate capture-less."""
    return [
        _Event(seq=1, type="system", actor="human",
               payload={"event": "session_created", "title": "t"}),
        _usage(2, prompt=418, completion=801, latency=21.4),
        _capture(3, "planner"),
        _usage(4, prompt=4815, completion=2263, latency=88.1),
        _capture(5, "narrate"),
        _Event(seq=6, type="narrative", actor="narrator",
               payload={"prose": "x" * 40, "dsl": "hud()", "segments": []}),
        _usage(7, agent="decision", model="d1-3b", prompt=4364, completion=0,
               latency=0.52),
        _usage(8, agent="extractor", prompt=1263, completion=594, latency=22.5),
        _capture(9, "critic"),
        _usage(10, agent="extractor", prompt=2389, completion=1501, latency=47.2),
        _capture(11, "bts"),
    ]


def _clean_turn_frames() -> list[tt.StageFrame]:
    return [
        _frame("plan", "start", 0.0, index=1),
        _frame("plan", "done", 21.4, index=1),
        _frame("lanes", "start", 21.4, index=2),
        _frame("lanes", "done", 21.4, index=2),
        _frame("narrate", "start", 21.4, index=3),
        _frame("narrate", "done", 109.5, index=3),
        _frame("dsl", "start", 109.5, index=4),
        _frame("dsl", "done", 109.5, index=4),
        _frame("critic", "start", 110.0, index=5),
        _frame("critic", "done", 132.5, index=5),
        _frame("world_state", "start", 132.5, index=6),
        _frame("world_state", "done", 180.0, index=6),
        _frame("env_hud", "start", 180.0, index=7),
        _frame("env_hud", "done", 180.0, index=7),
        _frame("attribution", "start", 180.0, index=8),
        _frame("attribution", "done", 180.1, index=8),
    ]


def _report(events: list[_Event] | None = None, *, prior_narrate=None) -> dict:
    events = _clean_turn_events() if events is None else events
    return pp.build_turn_report(
        session_id="sess",
        turn=2 if prior_narrate else 1,
        kind="in_character",
        user_content="I push through the doors.",
        started_at="2026-01-01T00:00:00+00:00",
        total_s=180.3,
        frames=_clean_turn_frames(),
        calls=pp.collect_calls(events),
        events=events,
        summary=tt.summarize_turn_events(events),
        barrier_seq=max(event.seq for event in events),
        barrier_ts="2026-01-01T00:03:00+00:00",
        first_content_s=3.2,
        first_reasoning_s=1.1,
        prior_narrate=prior_narrate,
        environment={"narrator_model": "your-model-id", "config": "c.json", "log": "l.db",
                     "engine": "in-process build_engine (no service)"},
    )


# ---------------------------------------------------------------------------
# the call fold (usage rows are the accounting)
# ---------------------------------------------------------------------------


def test_collect_calls_pairs_each_capture_with_its_own_usage_row():
    calls = pp.collect_calls(_clean_turn_events())
    assert [call.interaction for call in calls] == [
        "planner", "narrate", "", "critic", "bts",
    ]
    assert [call.agent for call in calls] == [
        "narrator", "narrator", "decision", "extractor", "extractor",
    ]
    assert calls[0].model == "your-model-id" and calls[2].model == "d1-3b"
    assert calls[1].prompt_tokens == 4815 and calls[1].completion_tokens == 2263
    assert calls[1].captured is True and calls[1].finish == "stop"
    assert calls[1].messages[0]["role"] == "system"


def test_collect_calls_keeps_captureless_usage_rows_and_labels_them():
    calls = pp.collect_calls(_clean_turn_events())
    decision = calls[2]
    assert decision.captured is False
    assert decision.note == "no agent_call capture"
    assert decision.total_tokens == 4364


def test_collect_calls_does_not_pair_a_decision_row_to_the_next_capture():
    """The decision row sits BEFORE the critic's usage row: the capture must
    pair with the usage row appended immediately before it, never the decision."""
    events = [
        _usage(1, agent="decision", model="d1-3b", prompt=99, completion=0),
        _usage(2, agent="extractor", prompt=10, completion=5),
        _capture(3, "critic"),
    ]
    calls = pp.collect_calls(events)
    assert [call.interaction for call in calls] == ["", "critic"]
    assert calls[0].prompt_tokens == 99
    assert calls[1].prompt_tokens == 10


def test_call_row_derivations():
    calls = pp.collect_calls(_clean_turn_events())
    narrate = calls[1]
    assert narrate.prompt_share == round(100.0 * 4815 / 7078, 1)
    assert narrate.ms_per_1k_prompt == round(1000.0 * 88.1 / 4815, 1)


# ---------------------------------------------------------------------------
# the stage fold + the plan/lanes overlay
# ---------------------------------------------------------------------------


def test_stage_rows_overlay_marks_plan_and_lanes_as_model_steps():
    rows = pp.stage_rows(_clean_turn_frames())
    kinds = {row.step: row.kind for row in rows}
    assert kinds["plan"] == "model"
    assert kinds["lanes"] == "model"
    assert kinds["narrate"] == "model"
    assert kinds["dsl"] == "deterministic"
    assert kinds["env_hud"] == "deterministic"
    assert kinds["attribution"] == "deterministic"


def test_stage_rows_pairing_is_the_shared_core():
    """turn_trace owns the pairing edge cases; the probe only re-kinds."""
    steps = tt._steps_from_frames(_clean_turn_frames())
    rows = pp.stage_rows(_clean_turn_frames())
    assert [(step.step, step.start_s, step.end_s) for step in steps] == [
        (row.step, row.start_s, row.end_s) for row in rows
    ]
    assert [row.duration_s for row in rows if row.step == "narrate"] == [88.1]


def test_stage_rows_carry_an_unclosed_start_note():
    rows = pp.stage_rows([_frame("narrate", "start", 5.0)])
    assert rows[0].note == "step never emitted a done frame"


# ---------------------------------------------------------------------------
# the interaction -> stage assignment (the plan's known gap, closed)
# ---------------------------------------------------------------------------


def test_assign_stage_calls_maps_planner_and_npc_into_their_model_stages():
    stages = pp.stage_rows(_clean_turn_frames())
    calls = pp.collect_calls([
        _usage(1, prompt=10), _capture(2, "planner"),
        _usage(3, prompt=20), _capture(4, "npc"),
        _usage(5, prompt=21), _capture(6, "npc"),
        _usage(7, prompt=30), _capture(8, "narrate"),
    ])
    rows = pp.assign_stage_calls(stages, calls)
    plan = next(row for row in rows if row.step == "plan")
    lanes = next(row for row in rows if row.step == "lanes")
    assert [call.interaction for call in plan.calls] == ["planner"]
    assert [call.interaction for call in lanes.calls] == ["npc", "npc"]
    assert plan.kind == "model" and lanes.kind == "model"
    assert all(call.stage == "lanes" for call in lanes.calls)
    assert not [row for row in rows if row.measured_by != "stage frame"]


def test_assign_stage_calls_synthesizes_the_decision_gate():
    stages = pp.stage_rows(_clean_turn_frames())
    calls = pp.collect_calls([_usage(1, agent="decision", model="d1-3b",
                                     prompt=4364, completion=0, latency=0.52)])
    rows = pp.assign_stage_calls(stages, calls)
    gate = next(row for row in rows if row.step == "decision_gate")
    assert gate.measured_by == "call latency (no stage frame)"
    assert gate.duration_s == 0.52
    assert gate.calls[0].model == "d1-3b"
    assert gate.calls[0].stage == "decision_gate"  # the call is placed, not orphaned
    assert "no stage frame" in gate.note


def test_assign_stage_calls_gives_an_unknown_interaction_its_own_row():
    stages = pp.stage_rows(_clean_turn_frames())
    calls = pp.collect_calls([_usage(1), _capture(2, "mystery")])
    rows = pp.assign_stage_calls(stages, calls)
    orphan = next(row for row in rows if row.step == "mystery")
    assert "call had no matching stage frame" in orphan.note
    assert orphan.calls[0].latency_s == 1.0


def test_assign_stage_calls_routes_a_second_narrate_call_to_the_dsl_stage():
    stages = pp.stage_rows(_clean_turn_frames())
    calls = pp.collect_calls([
        _usage(1, prompt=10), _capture(2, "narrate"),
        _usage(3, prompt=11), _capture(4, "narrate"),
    ])
    rows = pp.assign_stage_calls(stages, calls)
    narrate = next(row for row in rows if row.step == "narrate")
    dsl = next(row for row in rows if row.step == "dsl")
    assert len(narrate.calls) == 1
    assert len(dsl.calls) == 1 and dsl.kind == "model"


def test_assign_stage_calls_keeps_a_regen_call_on_the_regen_stage():
    frames = [*_clean_turn_frames(),
              _frame("regen", "start", 132.5, label="Reworking the scene", index=5)]
    frames.append(_frame("regen", "done", 150.0, label="Reworking the scene", index=5))
    stages = pp.stage_rows(frames)
    calls = pp.collect_calls([_usage(1), _capture(2, "narrate_regen")])
    rows = pp.assign_stage_calls(stages, calls)
    regen = next(row for row in rows if row.step == "regen")
    assert regen.calls[0].interaction == "narrate_regen"
    assert regen.duration_s == 17.5


# ---------------------------------------------------------------------------
# the derived prompt-cache signal
# ---------------------------------------------------------------------------


def test_prefix_signal_counts_the_leading_identical_messages():
    previous = [{"role": "system", "content": "prefix"},
                {"role": "system", "content": "cast"},
                {"role": "user", "content": "turn one"}]
    messages = [{"role": "system", "content": "prefix"},
                {"role": "system", "content": "cast"},
                {"role": "user", "content": "turn two"}]
    signal = pp.prefix_signal(previous, messages)
    assert signal["shared_messages"] == 2
    assert signal["identical"] is False
    assert 0.0 < signal["ratio"] < 1.0
    assert signal["shared_chars"] < signal["total_chars"]


def test_prefix_signal_marks_a_fully_identical_prompt():
    messages = [{"role": "system", "content": "same"}]
    assert pp.prefix_signal(messages, list(messages))["identical"] is True


def test_prefix_signal_of_disjoint_prompts_shares_nothing():
    signal = pp.prefix_signal([{"role": "system", "content": "a"}],
                              [{"role": "user", "content": "b"}])
    assert signal["shared_messages"] == 0 and signal["ratio"] == 0.0


# ---------------------------------------------------------------------------
# the turn report
# ---------------------------------------------------------------------------


def test_build_turn_report_totals_coverage_and_headline():
    report = _report()
    assert report["tokens"]["calls"] == 5
    assert report["tokens"]["prompt_tokens"] == 418 + 4815 + 4364 + 1263 + 2389
    assert report["tokens"]["completion_tokens"] == 801 + 2263 + 0 + 594 + 1501
    assert report["tokens"]["by_model"]["d1-3b"]["calls"] == 1
    # every framed stage is measured; the coverage gap is the decision gate
    assert report["coverage"]["measured_stage_s"] == round(21.4 + 0.0 + 88.1 + 0.0
                                                          + 22.5 + 47.5 + 0.0 + 0.1, 3)
    assert report["coverage"]["unaccounted_s"] > 0.0
    durations = [row["duration_s"] for row in report["headline"]]
    assert durations == sorted(durations, reverse=True)
    assert report["headline"][0]["step"] == "narrate"
    # the decision gate is reported WITHOUT a frame, never dropped
    gate = next(row for row in report["approximated_stages"]
                if row["step"] == "decision_gate")
    assert gate["calls"][0]["model"] == "d1-3b"
    assert json.loads(json.dumps(report))["turn"] == 1


def test_build_turn_report_assigns_every_stage_its_calls():
    report = _report()
    by_step = {stage["step"]: stage for stage in report["stages"]}
    assert [call["interaction"] for call in by_step["plan"]["calls"]] == ["planner"]
    assert [call["interaction"] for call in by_step["narrate"]["calls"]] == ["narrate"]
    assert [call["interaction"] for call in by_step["critic"]["calls"]] == ["critic"]
    assert [call["interaction"] for call in by_step["world_state"]["calls"]] == ["bts"]
    assert by_step["plan"]["kind"] == "model"
    # the capture-less decision call is placed on its synthesized stage, never
    # left with an empty stage column
    assert report["calls"][2]["stage"] == "decision_gate"


def test_build_turn_report_threads_the_cross_turn_prefix():
    shared = {"role": "system", "content": "stable prefix"}
    messages = [dict(shared), {"role": "user", "content": "turn two"}]
    events = _clean_turn_events()
    # the planner and the narrate captures share the stable prefix; the narrate
    # call is also compared against the PREVIOUS turn's narrate messages
    events[2] = _capture(3, "planner", messages=[dict(shared),
                                                  {"role": "user", "content": "plan"}])
    events[4] = _capture(5, "narrate", messages=messages)
    report = _report(events, prior_narrate=[dict(shared),
                                            {"role": "user", "content": "turn one"}])
    prefix = report["cache"]["turn_prefix"]
    assert prefix["shared_messages"] == 1
    assert prefix["vs"] == "turn 1 narrate call"
    per_call = {entry["order"]: entry for entry in report["cache"]["calls"]}
    assert "shared_messages" not in per_call[1]
    assert per_call[2]["shared_messages"] == 1
    assert per_call[2]["vs"] == "planner#1"


def test_build_turn_report_notes_a_turn_with_no_stage_frames():
    events = [_usage(1, prompt=700, completion=120, latency=9.0),
              _capture(2, "ooc")]
    report = pp.build_turn_report(
        session_id="sess", turn=3, kind="ooc", user_content="OOC: recap?",
        started_at="2026-01-01T00:00:00+00:00", total_s=9.0, frames=[],
        calls=pp.collect_calls(events), events=events,
        summary=tt.summarize_turn_events(events), barrier_seq=2,
        barrier_ts="2026-01-01T00:00:09+00:00",
    )
    assert report["stages"] == []
    assert report["approximated_stages"][0]["step"] == "ooc"
    assert any("no stage frames observed" in note for note in report["notes"])


# ---------------------------------------------------------------------------
# the post-turn hook approximation
# ---------------------------------------------------------------------------


def test_fold_post_turn_hooks_is_honest_when_nothing_follows_the_barrier():
    folded = pp.fold_post_turn_hooks(_clean_turn_events(), barrier_seq=11)
    assert folded["observed"] is False
    assert folded["calls"] == [] and folded["spans"] == []
    assert folded["reason"] == pp.NO_HOOK_REASON
    assert folded["note"] == pp.HOOK_NOTE


def test_fold_post_turn_hooks_reads_calls_spans_and_marks():
    events = [
        *_clean_turn_events(),
        _usage(12, agent="summarizer", prompt=300, completion=40, latency=2.5,
               ts="2026-01-01T00:03:02+00:00"),
        _capture(13, "summarize"),
        _Event(seq=14, type="system", actor="bts",
               payload={"event": "historian_run", "status": "started", "turn_seq": 1},
               ts="2026-01-01T00:03:05+00:00"),
        _usage(15, agent="extractor", prompt=900, completion=200, latency=11.0,
               ts="2026-01-01T00:03:16+00:00"),
        _capture(16, "proposals"),
        _Event(seq=17, type="system", actor="bts",
               payload={"event": "asset_proposal", "kind": "world", "action": "add"},
               ts="2026-01-01T00:03:17+00:00"),
        _Event(seq=18, type="summary", actor="engine",
               payload={"text": "covers 4 turns", "covers_seqs": [1, 2, 3, 4]},
               ts="2026-01-01T00:03:18+00:00"),
        _Event(seq=19, type="system", actor="bts",
               payload={"event": "historian_run", "status": "finished", "filed": 2,
                        "gate": "ok", "failed_lanes": [], "duration_ms": 12000},
               ts="2026-01-01T00:03:17+00:00"),
    ]
    folded = pp.fold_post_turn_hooks(
        events, barrier_seq=11, barrier_ts="2026-01-01T00:03:00+00:00")
    assert folded["observed"] is True
    assert folded["rows_after_barrier"] == 8
    assert [call["interaction"] for call in folded["calls"]] == ["summarize", "proposals"]
    assert folded["calls"][0]["stage"] == "compaction"
    assert folded["calls"][1]["stage"] == "historian lanes"
    assert folded["calls"][0]["latency_s"] == 2.5
    span = folded["spans"][0]
    assert span["hook"] == "historian"
    assert span["status"] == "finished"
    assert span["duration_s"] == 12.0          # the row's own duration_ms
    assert span["offset_from_barrier_s"] == 5.0  # placement is the approximation
    assert span["filed"] == 2
    assert folded["marks"][0]["event"] == "compaction_summary"
    assert folded["marks"][0]["offset_from_barrier_s"] == 18.0
    assert folded["proposal_rows"] == 1


def test_fold_post_turn_hooks_falls_back_to_ts_when_duration_is_absent():
    events = [
        _Event(seq=1, type="system", actor="bts",
               payload={"event": "historian_run", "status": "started"},
               ts="2026-01-01T00:00:00+00:00"),
        _Event(seq=2, type="system", actor="bts",
               payload={"event": "historian_run", "status": "failed",
                        "error": "transport"},
               ts="2026-01-01T00:00:07.5+00:00"),
    ]
    folded = pp.fold_post_turn_hooks(
        events, barrier_seq=0, barrier_ts="2026-01-01T00:00:00+00:00")
    assert folded["spans"][0]["duration_s"] == 7.5
    assert folded["spans"][0]["status"] == "failed"


# ---------------------------------------------------------------------------
# rendering + CLI
# ---------------------------------------------------------------------------


def test_render_turn_markdown_carries_the_headline_sections():
    text = pp.render_turn_markdown(_report())
    for heading in ("## Where the wall-clock went", "## Stage timeline",
                    "## Model calls", "## Prompt-cache signal",
                    "## After the barrier"):
        assert heading in text
    assert "| 1 | `plan` |" not in text  # headline rows are ordered, not numbered
    assert "your-model-id" in text and "d1-3b" in text
    assert "not observed" in text


def test_render_run_markdown_summarizes_turns_and_models():
    run = {
        "started_at": "2026-01-01T00:00:00+00:00",
        "session_id": "sess",
        "environment": {"narrator_model": "your-model-id", "config": "c.json",
                        "log": "l.db", "engine": "in-process build_engine (no service)"},
        "turns": [_report()],
    }
    text = pp.render_run_markdown(run)
    assert "# perf_probe — Stage 0 cost report" in text
    assert "## Cross-turn table" in text
    assert "## Per-model totals" in text
    assert "`your-model-id`" in text and "`d1-3b`" in text
    # a turns-only run has no experiment answers to print
    assert "## CONTENTION" not in text and "## PROMPT CACHE" not in text


def test_parse_args_defaults_are_the_isolated_two_turn_run():
    args = pp.parse_args([])
    assert args.mode == "serial"          # today's behaviour is the default
    assert args.turns == 2
    assert args.config == "configs/connections.json"
    assert args.log == "data/perf_probe.db"
    assert args.out == "data/perf"
    assert args.calls == 3
    assert args.reference_tokens == 800
    assert args.cross_connection == "auto"
    assert args.cache_agent == "narrator"
    assert args.cache_prefix_tokens == pp.CACHE_PREFIX_TOKENS


def test_parse_args_accepts_the_experiment_modes():
    for mode in ("serial", "concurrent", "cache", "all"):
        assert pp.parse_args(["--mode", mode]).mode == mode
    assert pp.CALL_MODES == ("serial", "concurrent", "cache", "all")


# ---------------------------------------------------------------------------
# the contention experiment (serial-sum vs concurrent wall) — fake streams only
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _fake_stream(*, chunks: int = 3, delay: float = 0.0, prompt_tokens: int = 900,
                 completion_tokens: int = 12, cached_tokens=None, error: str = ""):
    """A stand-in for ``_stream_completion``: the caller protocol's events."""
    async def stream(messages, max_tokens=pp.REFERENCE_MAX_TOKENS):
        if error:
            raise RuntimeError(error)
        for _ in range(chunks):
            if delay:
                await asyncio.sleep(delay)
            yield {"type": "content", "delta": "tok "}
        yield {"type": "usage", "prompt_tokens": prompt_tokens,
               "completion_tokens": completion_tokens,
               "cached_tokens": cached_tokens}
    return stream


@dataclass
class _InFlight:
    """Counts how many fake calls are running at once (the concurrency proof)."""

    active: int = 0
    peak: int = 0


def _counting_stream(state: _InFlight, *, delay: float = 0.02):
    async def stream(messages, max_tokens=pp.REFERENCE_MAX_TOKENS):
        state.active += 1
        state.peak = max(state.peak, state.active)
        try:
            await asyncio.sleep(delay)
            yield {"type": "content", "delta": "tok"}
            await asyncio.sleep(delay)
            yield {"type": "usage", "prompt_tokens": 900,
                   "completion_tokens": 4, "cached_tokens": None}
        finally:
            state.active -= 1
    return stream


def _plan(count: int = 3, *, stream_fn=None, messages=None) -> list[pp.CallSpec]:
    messages = messages or pp.reference_messages(prefix_tokens=800)
    stream_fn = stream_fn or _fake_stream(delay=0.0)
    return [
        pp.CallSpec(label=f"local-{index}", server="your-model-id", model="your-model-id",
                    messages=messages, stream_fn=stream_fn)
        for index in range(1, count + 1)
    ]


def test_reference_messages_are_fixed_and_sized_from_one_line():
    short = pp.reference_messages(prefix_tokens=100)
    long = pp.reference_messages(prefix_tokens=3000)
    assert short == pp.reference_messages(prefix_tokens=100)  # deterministic
    assert short[0] == {"role": "system", "content": pp.REFERENCE_INSTRUCTION}
    assert len(long[1]["content"]) == 12000  # 4 chars per estimated token
    assert long[1]["content"].startswith(pp.REFERENCE_LINE[:40])


def test_shape_warmup_messages_warm_the_shape_but_not_the_prefix():
    """llama.cpp reuses cached KV by matching text, so the warm-up prompt must share
    the reference prompt's SIZE and none of its words."""
    reference = pp.reference_messages(prefix_tokens=3000)
    shape = pp.shape_warmup_messages(3000)
    assert shape[0] != reference[0]
    assert shape[1] != reference[1]
    assert len(shape[1]["content"]) == len(reference[1]["content"])   # same size
    assert pp.prefix_signal(shape, reference)["shared_messages"] == 0
    assert pp.REFERENCE_WARMUP_LINE not in reference[1]["content"]


def test_reference_messages_nonce_makes_a_prefix_unmatchable_against_prior_runs():
    """A fresh run's prefix must not be served from a previous run's KV cache."""
    first = pp.reference_messages(prefix_tokens=200, nonce="aaa")
    second = pp.reference_messages(prefix_tokens=200, nonce="bbb")
    assert "[run aaa]" in first[1]["content"]
    assert "bbb" not in first[1]["content"]
    assert "[run bbb]" in second[1]["content"]
    assert first[1]["content"] != second[1]["content"]
    # only the instruction is shared: no filler text can be reused
    assert pp.prefix_signal(first, second)["shared_messages"] == 1


def test_timed_completion_measures_ttft_and_tokens():
    result = _run(pp.timed_completion(
        _fake_stream(chunks=3, delay=0.02, prompt_tokens=900,
                     completion_tokens=12, cached_tokens=880),
        [{"role": "user", "content": "hi"}], label="local-1",
        server="your-model-id", model="your-model-id",
    ))
    assert result.prompt_tokens == 900 and result.completion_tokens == 12
    assert result.cached_tokens == 880
    assert result.ttft_s is not None and 0.0 < result.ttft_s <= result.latency_s
    assert result.text_chars == len("tok ") * 3
    assert result.error == ""
    assert result.ended_s >= result.started_s


def test_timed_completion_records_a_failing_call_without_raising():
    result = _run(pp.timed_completion(
        _fake_stream(error="connection reset"), [], label="local-1",
        server="your-model-id", model="your-model-id",
    ))
    assert result.error == "RuntimeError: connection reset"
    assert result.prompt_tokens == 0 and result.ttft_s is None
    assert result.latency_s >= 0.0


def test_run_arm_serial_walls_sum_while_concurrent_calls_overlap():
    serial_state, concurrent_state = _InFlight(), _InFlight()
    serial = _run(pp.run_arm(_plan(stream_fn=_counting_stream(serial_state)),
                             concurrent=False))
    concurrent = _run(pp.run_arm(_plan(stream_fn=_counting_stream(concurrent_state)),
                                 concurrent=True))
    assert serial_state.peak == 1          # one at a time, by construction
    assert concurrent_state.peak == 3      # every call in flight at once
    assert serial["wall_s"] >= 3 * 0.04    # 2 × 0.02s per call, sequentially
    assert concurrent["wall_s"] < serial["wall_s"] / 2
    assert len(serial["calls"]) == len(concurrent["calls"]) == 3
    assert serial["sum_call_s"] > 0.0


def test_build_contention_report_answers_with_serialized_numbers():
    serial, concurrent = _arms(
        serial_local=[20.0, 20.0, 20.0], concurrent_local=[20.5, 40.5, 59.0],
        concurrent_wall=60.0,
    )
    report = pp.build_contention_report(
        serial, concurrent, local_server="your-model-id",
        server_facts={"local": {"reachable": True, "parallel_slots": 1,
                                "model": "your-model-id", "loaded": "loaded"}},
    )
    assert report["factor"] == 1.0            # 60/60: the queue, not parallelism
    assert report["local_factor"] == 0.98     # the local calls spanned 59s of 60s
    assert report["local_mean_serial_s"] == 20.0
    assert report["local_mean_concurrent_s"] == 40.0
    assert report["local_inflation"] == 2.0
    assert report["ideal_factor"] == 0.33
    assert report["verdict"] == "serialized"
    answer = "\n".join(report["answer"])
    assert "serialized" in answer and "60.0s" in answer and "1.0×" in answer
    assert "--parallel 1" in answer           # the server's own config explains it
    rows = {row["label"]: row for row in report["rows"]}
    assert rows["local-3"]["inflation"] == 2.95


def test_build_contention_report_verdict_bands():
    bands = (
        ([0.35, 0.35, 0.35], "parallel"),            # 0.35s span over 3.0s serial
        ([1.0, 1.5, 2.0], "partially parallel"),      # 2.0s span over 3.0s serial
        ([2.9, 2.95, 3.0], "serialized"),            # 3.0s span over 3.0s serial
    )
    for concurrent_local, expected in bands:
        serial, concurrent = _arms(
            serial_local=[1.0, 1.0, 1.0], concurrent_local=concurrent_local,
            concurrent_wall=max(concurrent_local),
        )
        report = pp.build_contention_report(serial, concurrent, local_server="your-model-id")
        assert report["verdict"] == expected, report["local_factor"]
        assert any("Answer:" in sentence for sentence in report["answer"])


def test_build_contention_report_names_the_cross_server_call():
    serial, concurrent = _arms(
        serial_local=[10.0, 10.0], concurrent_local=[10.0, 20.0],
        concurrent_wall=20.0, cross=(5.0, 5.0),
    )
    report = pp.build_contention_report(
        serial, concurrent, local_server="your-model-id", cross_server="lfm25-350m",
    )
    cross = next(row for row in report["rows"] if row["label"] == "cross")
    assert cross["server"] == "lfm25-350m"
    assert "a DIFFERENT server" in "\n".join(report["answer"])
    # the local factor must ignore the cross call entirely
    assert report["local_factor"] == round(20.0 / 20.0, 2)
    assert report["calls_per_server"] == 2 and report["calls_total"] == 3


def test_build_contention_report_is_honest_when_nothing_ran():
    serial, concurrent = _arms(serial_local=[0.0], concurrent_local=[0.0],
                               concurrent_wall=0.0)
    report = pp.build_contention_report(serial, concurrent, local_server="your-model-id")
    assert report["factor"] is None and report["verdict"].startswith("unknown")
    assert "not measured" in report["answer"][0]


def test_one_slot_server_guards_against_a_parallel_looking_measurement():
    """One slot cannot run two calls at once: a parallel-band factor is drift, not
    parallelism, and the report must say so instead of claiming an answer."""
    serial, concurrent = _arms(serial_local=[20.0, 20.0], concurrent_local=[3.0, 5.0],
                               concurrent_wall=5.0)
    report = pp.build_contention_report(
        serial, concurrent, local_server="your-model-id",
        server_facts={"local": {"reachable": True, "parallel_slots": 1,
                                "model": "your-model-id", "loaded": "loaded"}},
    )
    assert report["local_factor"] == 0.12
    assert report["verdict"].startswith("inconsistent")
    answer = "\n".join(report["answer"])
    assert "re-run this mode" in answer and "1 slot(s)" in answer


def test_a_multi_slot_server_may_report_parallel():
    serial, concurrent = _arms(serial_local=[20.0, 20.0], concurrent_local=[3.0, 5.0],
                               concurrent_wall=5.0)
    report = pp.build_contention_report(
        serial, concurrent, local_server="your-model-id",
        server_facts={"local": {"reachable": True, "parallel_slots": 2,
                                "model": "your-model-id", "loaded": "loaded"}},
    )
    assert report["verdict"] == "parallel"
    assert "ran the calls in parallel" in "\n".join(report["answer"])


def _arms(*, serial_local: list[float], concurrent_local: list[float],
          concurrent_wall: float, cross: tuple[float, float] | None = None):
    """Fabricated arms: the same call labels with the numbers the live run reports."""
    def call(label, server, latency, start, ttft):
        return {"label": label, "server": server, "model": server,
                "latency_s": latency, "ttft_s": ttft,
                "prompt_tokens": 900, "completion_tokens": 12,
                "cached_tokens": None, "text_chars": 60,
                "started_s": start, "ended_s": start + latency, "error": ""}

    serial_calls, concurrent_calls = [], []
    cursor = 0.0
    for index, latency in enumerate(serial_local, 1):
        serial_calls.append(call(f"local-{index}", "your-model-id", latency, cursor, latency))
        cursor += latency
    for index, latency in enumerate(concurrent_local, 1):
        concurrent_calls.append(call(f"local-{index}", "your-model-id", latency, 0.0, latency))
    if cross is not None:
        serial_calls.append(call("cross", "lfm25-350m", cross[0], cursor, cross[0]))
        concurrent_calls.append(call("cross", "lfm25-350m", cross[1], 0.0, cross[1]))
        concurrent_wall = max(concurrent_wall, cross[1])
    return (
        {"concurrent": False, "wall_s": round(cursor, 3),
         "sum_call_s": round(cursor, 3), "max_call_s": max(serial_local, default=0.0),
         "mean_ttft_s": None, "calls": serial_calls, "errors": 0},
        {"concurrent": True, "wall_s": round(concurrent_wall, 3),
         "sum_call_s": round(sum(concurrent_local), 3),
         "max_call_s": max(concurrent_local, default=0.0),
         "mean_ttft_s": None, "calls": concurrent_calls, "errors": 0},
    )


def test_render_contention_section_puts_the_answer_before_the_table():
    serial, concurrent = _arms(serial_local=[10.0, 10.0], concurrent_local=[10.0, 18.0],
                               concurrent_wall=18.0)
    report = pp.build_contention_report(serial, concurrent, local_server="your-model-id",
                                        environment={"config": "c.json",
                                                     "mode": "concurrent"})
    text = pp.render_contention_markdown(report)
    heading = "## CONTENTION — is the model server contended or serialized?"
    assert heading in text
    assert text.index(heading) < text.index("| call | server | serial |")
    assert "Answer:" in text
    assert "| `local-1` | `your-model-id` | 10.0s | 10.0s |" in text


# ---------------------------------------------------------------------------
# the prompt-cache probe (first vs second call over one identical prefix)
# ---------------------------------------------------------------------------


def _pair(first: dict, second: dict, *, messages=None) -> dict:
    """Fabricated prefix pair with the numbers a live run reports."""
    messages = messages or pp.reference_messages(prefix_tokens=3000)

    def call(label, values):
        return pp.CallResult(label=label, server="your-model-id", model="your-model-id", **values)

    return pp.build_cache_report(call("prefix call 1", first),
                                 call("prefix call 2", second),
                                 messages=messages, prefix_tokens=3000)


def test_build_cache_report_uses_the_provider_accounting_as_primary():
    report = _pair(
        {"latency_s": 18.0, "ttft_s": 17.0, "prompt_tokens": 3005,
         "completion_tokens": 40, "cached_tokens": 0},
        {"latency_s": 1.2, "ttft_s": 0.4, "prompt_tokens": 3005,
         "completion_tokens": 40, "cached_tokens": 3000},
    )
    assert report["verdict"] == "cache hit"
    assert report["ttft_ratio"] == 0.02
    assert report["ttft_saved_s"] == 16.6
    assert report["same_prompt_tokens"] is True
    assert report["cached_tokens"] == {"first": 0, "second": 3000}
    assert report["derived_overlap"]["identical"] is True
    assert "secondary" in report["derived_overlap"]["role"]
    answer = "\n".join(report["answer"])
    assert "cached_tokens" in answer and "99.8%" in answer
    assert "iteration 2 rides the cached prefix" in answer


def test_build_cache_report_falls_back_to_timing_when_the_provider_says_nothing():
    hit = _pair(
        {"latency_s": 18.0, "ttft_s": 17.0, "prompt_tokens": 3005},
        {"latency_s": 1.2, "ttft_s": 0.4, "prompt_tokens": 3005},
    )
    assert hit["verdict"] == "cache hit (timing only)"
    assert "TIMING ONLY" in "\n".join(hit["answer"])
    miss = _pair(
        {"latency_s": 18.0, "ttft_s": 17.0, "prompt_tokens": 3005},
        {"latency_s": 19.0, "ttft_s": 17.5, "prompt_tokens": 3005},
    )
    assert miss["verdict"] == "cache miss (timing only)"


def test_build_cache_report_reports_a_prefix_the_server_did_not_keep():
    report = _pair(
        {"latency_s": 18.0, "ttft_s": 17.0, "prompt_tokens": 3005,
         "cached_tokens": 0},
        {"latency_s": 19.0, "ttft_s": 17.5, "prompt_tokens": 3005,
         "cached_tokens": 12},
    )
    assert report["verdict"] == "cache miss"
    assert "no cache reuse observed" in "\n".join(report["answer"])


def test_build_cache_report_separates_a_hit_from_a_ttft_win():
    report = _pair(
        {"latency_s": 18.0, "ttft_s": 17.0, "prompt_tokens": 3005,
         "cached_tokens": 0},
        {"latency_s": 18.5, "ttft_s": 16.0, "prompt_tokens": 3005,
         "cached_tokens": 3000},
    )
    assert report["verdict"] == "cache hit without a TTFT win"
    assert "do not budget a loop iteration as a pure tail call" in "\n".join(
        report["answer"]
    )


def test_build_cache_report_refuses_a_slower_second_call_over_a_cached_prefix():
    """A warm prefix cannot make a call 20× slower: that is server drift, and the
    report must refuse to read it as a cache finding."""
    report = _pair(
        {"latency_s": 2.2, "ttft_s": 1.0, "prompt_tokens": 2517,
         "cached_tokens": 0},
        {"latency_s": 22.8, "ttft_s": 21.2, "prompt_tokens": 2517,
         "cached_tokens": 2515},
    )
    assert report["verdict"].startswith("inconsistent")
    answer = "\n".join(report["answer"])
    assert "re-run this mode" in answer and "20.2s LONGER" in answer


def test_cache_answer_words_the_ttft_delta_both_ways():
    saved = _pair(
        {"latency_s": 12.0, "ttft_s": 11.0, "prompt_tokens": 3005,
         "cached_tokens": 0},
        {"latency_s": 2.0, "ttft_s": 1.0, "prompt_tokens": 3005,
         "cached_tokens": 3000},
    )
    assert "10.0s saved" in saved["answer"][0]
    assert saved["verdict"] == "cache hit"


def test_build_cache_report_prefers_the_servers_own_prefill_split():
    """The live deployment queues hard outside the model: the client stopwatch then
    measures the queue, and only the server's prefill number shows the cache hit."""
    report = _pair(
        {"latency_s": 80.1, "ttft_s": 78.3, "prompt_tokens": 3152,
         "cached_tokens": 0, "server_prompt_ms": 18282.0,
         "server_predicted_ms": 1864.0},
        {"latency_s": 104.6, "ttft_s": 102.7, "prompt_tokens": 3152,
         "cached_tokens": 3147, "server_prompt_ms": 294.2,
         "server_predicted_ms": 1958.0},
    )
    assert report["verdict"] == "cache hit (server prefill)"
    assert report["prefill_ratio"] == 0.02
    assert report["server_prefill_saved_ms"] == 17987.8
    assert report["queue_s"]["second"] == 102.348
    answer = "\n".join(report["answer"])
    assert "prefill time collapsed" in answer
    assert "OUTSIDE the model" in answer          # the stopwatch caveat, stated
    assert "| 18282.0 ms | 1864.0 ms |" in pp.render_cache_markdown(report)


def test_build_cache_report_says_when_reuse_does_not_move_the_prefill():
    report = _pair(
        {"latency_s": 12.0, "ttft_s": 11.0, "prompt_tokens": 3005,
         "cached_tokens": 0, "server_prompt_ms": 5000.0,
         "server_predicted_ms": 1200.0},
        {"latency_s": 11.0, "ttft_s": 10.0, "prompt_tokens": 3005,
         "cached_tokens": 3000, "server_prompt_ms": 4000.0,
         "server_predicted_ms": 1200.0},
    )
    assert report["verdict"] == "cache hit without a prefill win"
    assert "prefill is not what this prompt's cost is made of" in "\n".join(
        report["answer"]
    )


def test_render_cache_section_carries_the_answer_and_the_pair_table():
    report = _pair(
        {"latency_s": 18.0, "ttft_s": 17.0, "prompt_tokens": 3005,
         "cached_tokens": 0},
        {"latency_s": 1.2, "ttft_s": 0.4, "prompt_tokens": 3005,
         "cached_tokens": 3000},
    )
    text = pp.render_cache_markdown(report)
    heading = "## PROMPT CACHE — does a loop iteration ride the cached prefix?"
    assert heading in text
    assert text.index(heading) < text.index("| call | latency | TTFT |")
    assert "Answer:" in text
    assert "| `prefix call 2` | 1.2s | 0.4s | — ms | — ms | 3005 | 3000 |" in text


def test_render_run_markdown_prints_both_answers_above_the_turn_tables():
    contention_report = pp.build_contention_report(
        *_arms(serial_local=[10.0, 10.0], concurrent_local=[10.0, 18.0],
               concurrent_wall=18.0),
        local_server="your-model-id", cross_server="",
    )
    cache_report = _pair(
        {"latency_s": 18.0, "ttft_s": 17.0, "prompt_tokens": 3005,
         "cached_tokens": 0},
        {"latency_s": 1.2, "ttft_s": 0.4, "prompt_tokens": 3005,
         "cached_tokens": 3000},
    )
    run = {
        "started_at": "2026-01-01T00:00:00+00:00",
        "session_id": "",
        "mode": "all",
        "environment": {"narrator_model": "your-model-id", "config": "c.json",
                        "log": "l.db", "engine": "in-process build_engine (no service)"},
        "turns": [_report()],
        "contention": contention_report,
        "cache": cache_report,
    }
    text = pp.render_run_markdown(run)
    assert "## CONTENTION" in text and "## PROMPT CACHE" in text
    assert text.index("## CONTENTION") < text.index("## Cross-turn table")
    assert text.index("## PROMPT CACHE") < text.index("## Cross-turn table")
    assert "| `prefix call 2` |" in text     # the pair table came along
    assert "## Cross-turn table" in text     # the turns still follow the answers

