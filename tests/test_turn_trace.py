"""The diagnostic turn trace (PLAN W8.3 / D46) — assembly + CLI surface.

These tests exercise the PURE parts of ``scripts/turn_trace.py`` from fabricated
step/usage records and the CLI argument surface. No model call is made: the
live path (``:4444``) is verified by running the script itself against the real
engine, not by the unit suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # scripts/ is a namespace package, not installed
    sys.path.insert(0, str(_ROOT))

from scripts import turn_trace as tt  # noqa: E402


# ---------------------------------------------------------------------------
# fabricated records
# ---------------------------------------------------------------------------


def _frame(step: str, state: str, t: float, *, label: str = "", index: int = 0,
           total: int = 6) -> tt.StageFrame:
    return tt.StageFrame(step=step, state=state, label=label or step, index=index,
                         total=total, t=t)


def _call(interaction: str, seq: int, duration: float, *, agent: str = "extractor",
          model: str = "m", prompt: int = 10, completion: int = 5,
          response: str = "out", reasoning: str = "") -> tt.ModelCall:
    return tt.ModelCall(
        interaction=interaction, agent=agent, model=model, seq=seq,
        latency_s=duration, prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=prompt + completion, finish="stop",
        roles=["system", "user"],
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "ask"}],
        response_text=response, reasoning_text=reasoning,
    )


def _events(**overrides) -> dict:
    base = {
        "narrative": {"seq": 3, "prose_chars": 100, "dsl_chars": 5},
        "revisions": [],
        "state_delta": None,
        "validation_flags": [],
        "critic_verdicts": [],
        "errors": [],
        "world_state": None,
        "env_hud": None,
        "attribution": None,
    }
    base.update(overrides)
    return base


def _trace(frames, calls, events=None, total_s=40.0) -> dict:
    return tt.build_trace(
        session_id="sess",
        turn=1,
        kind="in_character",
        user_content="I go in",
        wall_started_at="2026-01-01T00:00:00+00:00",
        total_s=total_s,
        frames=frames,
        calls=calls,
        events=events or _events(),
    )


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def test_build_trace_orders_steps_kinds_and_headline():
    frames = [
        _frame("narrate", "start", 0.0, label="Writing the scene", index=1),
        _frame("narrate", "done", 24.0, label="Writing the scene", index=1),
        _frame("dsl", "start", 24.0, label="Checking the composition", index=2),
        _frame("dsl", "done", 24.2, label="Checking the composition", index=2),
        _frame("critic", "start", 24.3, label="Reviewing the turn", index=3),
        _frame("critic", "done", 34.5, label="Reviewing the turn", index=3),
        _frame("env_hud", "start", 34.5, label="Updating the HUD", index=5),
        _frame("env_hud", "done", 34.6, label="Updating the HUD", index=5),
    ]
    calls = [
        _call("narrate", 10, 24.0, agent="narrator", model="your-model-id", prompt=900,
              completion=400),
        _call("critic", 12, 10.0, prompt=500, completion=50),
    ]
    trace = _trace(frames, calls, total_s=35.0)

    assert [s["step"] for s in trace["steps"]] == ["narrate", "dsl", "critic", "env_hud"]
    assert [s["kind"] for s in trace["steps"]] == ["model", "deterministic", "model",
                                                   "deterministic"]
    assert trace["steps"][0]["duration_s"] == 24.0
    assert trace["steps"][2]["duration_s"] == pytest.approx(10.2)

    # headline ranks by duration, model step first, and reports % of turn.
    assert [r["step"] for r in trace["headline"]] == ["narrate", "critic", "dsl", "env_hud"]
    assert trace["headline"][0]["pct_of_turn"] == pytest.approx(68.6, abs=0.1)

    # tokens summed across model steps.
    assert trace["tokens"] == {"prompt_tokens": 1400, "completion_tokens": 450,
                               "total_tokens": 1850}

    # the narrate step carries its exact call record + message shape.
    narrate = trace["steps"][0]["call"]
    assert narrate["agent"] == "narrator"
    assert narrate["model"] == "your-model-id"
    assert narrate["roles"] == ["system", "user"]
    assert [row["chars"] for row in narrate["message_shapes"]] == [3, 3]


def test_dsl_resynthesis_and_regen_calls_assigned_to_right_steps():
    frames = [
        _frame("narrate", "start", 0.0, index=1),
        _frame("narrate", "done", 5.0, index=1),
        _frame("dsl", "start", 5.0, index=2),
        _frame("dsl", "done", 8.0, index=2),
        _frame("critic", "start", 8.1, index=3),
        _frame("critic", "done", 10.0, index=3),
        _frame("regen", "start", 10.0, label="Reworking the scene (attempt 1)", index=3),
        _frame("regen", "done", 14.0, label="Reworking the scene (attempt 1)", index=3),
    ]
    calls = [
        _call("narrate", 20, 5.0),
        _call("narrate", 21, 3.0),      # the dsl re-synthesis call
        _call("critic", 22, 2.0),
        _call("narrate", 25, 4.0),      # the regen call
    ]
    trace = _trace(frames, calls, total_s=14.0)
    by_step = {s["step"]: s for s in trace["steps"]}
    assert by_step["narrate"]["call"]["seq"] == 20
    assert by_step["dsl"]["kind"] == "model"
    assert by_step["dsl"]["call"]["seq"] == 21
    assert by_step["critic"]["call"]["seq"] == 22
    assert by_step["regen"]["call"]["seq"] == 25


def test_critic_verdict_and_revision_attach_to_step():
    frames = [
        _frame("narrate", "start", 0.0, index=1),
        _frame("narrate", "done", 5.0, index=1),
        _frame("critic", "start", 5.0, index=3),
        _frame("critic", "done", 9.0, index=3),
    ]
    calls = [_call("narrate", 1, 5.0), _call("critic", 2, 4.0)]
    events = _events(
        critic_verdicts=[{"verdict": "diffs", "attempt": 0, "reason": "raw DSL",
                          "edits": [{"find": "a", "replace": "b"}]}],
        revisions=[{"id": 42, "seq": 42, "actor": "critic", "supersedes": 3,
                    "verdict": "diffs", "reason": "critic repair", "prose_chars": 90}],
    )
    trace = _trace(frames, calls, events=events)
    critic = next(s for s in trace["steps"] if s["step"] == "critic")
    assert critic["verdict"]["verdict"] == "diffs"
    assert critic["revisions"][0]["id"] == 42


def test_ooc_turn_synthesizes_timeline_from_calls():
    trace = _trace([], [_call("ooc", 5, 3.5, agent="narrator")], total_s=3.6)
    assert len(trace["steps"]) == 1
    step = trace["steps"][0]
    assert step["step"] == "ooc"
    assert step["kind"] == "model"
    assert step["duration_s"] == 3.5
    assert step["call"]["seq"] == 5
    assert any("synthesized" in note for note in trace["notes"])


def test_empty_run_is_annotated_not_crashing():
    trace = _trace([], [], total_s=0.0)
    assert trace["steps"] == []
    assert trace["tokens"]["total_tokens"] == 0
    assert any("no model calls" in note for note in trace["notes"])


# ---------------------------------------------------------------------------
# usage pairing
# ---------------------------------------------------------------------------


class _Ev:
    def __init__(self, seq: int, payload: dict, etype: str = "system") -> None:
        self.seq = seq
        self.payload = payload
        self.type = etype


def test_collect_calls_pairs_capture_with_its_preceding_usage_row():
    events = [
        # RegistryModelCaller records usage, THEN DebugCapture appends the capture.
        _Ev(10, {"event": "usage", "agent": "narrator", "model": "your-model-id",
                 "prompt_tokens": 900, "completion_tokens": 400, "total_tokens": 1300}),
        _Ev(11, {"event": "agent_call", "agent": "narrate", "latency_s": 24.0,
                 "messages": [{"role": "system", "content": "s"}], "finish": "stop",
                 "response_text": "prose", "reasoning_text": "hmm"}),
    ]
    calls = tt.collect_calls(events)
    assert len(calls) == 1
    call = calls[0]
    assert call.interaction == "narrate"        # the capture's interaction label
    assert call.agent == "narrator"             # the usage row's resolved agent
    assert call.model == "your-model-id"
    assert call.total_tokens == 1300
    assert call.roles == ["system"]
    assert call.response_text == "prose"


def test_collect_calls_leaves_unpaired_capture_without_tokens():
    events = [
        _Ev(5, {"event": "agent_call", "agent": "critic", "latency_s": 2.0,
                 "messages": [], "finish": None, "response_text": "", "reasoning_text": ""}),
    ]
    calls = tt.collect_calls(events)
    assert calls[0].total_tokens == 0
    assert calls[0].model == ""


# ---------------------------------------------------------------------------
# render + artifacts
# ---------------------------------------------------------------------------


def test_render_markdown_headlines_the_expensive_step():
    frames = [
        _frame("narrate", "start", 0.0, index=1),
        _frame("narrate", "done", 24.0, index=1),
        _frame("env_hud", "start", 24.0, index=5),
        _frame("env_hud", "done", 24.1, index=5),
    ]
    trace = _trace(frames, [_call("narrate", 1, 24.0, agent="narrator")],
                   events=_events(critic_verdicts=[
                       {"verdict": "ok", "attempt": 0, "reason": "fine", "edits": []}]))
    markdown = tt.render_markdown(trace)
    assert "## Where the wall-clock went (ranked)" in markdown
    assert "| narrate | model | 24.0 |" in markdown
    assert "## Ordered steps" in markdown


def test_write_artifacts_atomic_pair(tmp_path):
    trace = _trace([], [_call("ooc", 1, 1.0, agent="narrator")], total_s=1.1)
    json_path, md_path = tt._write_artifacts(str(tmp_path), "sess", 1, trace)
    assert json_path.name == "turn-01.json"
    assert md_path.name == "turn-01.md"
    assert json_path.parent.name == "sess"
    assert not list(json_path.parent.glob("*.tmp"))
    import json

    assert json.loads(json_path.read_text())["session_id"] == "sess"
    assert "# Turn trace" in md_path.read_text()


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_defaults_run_one_turn_against_default_config():
    args = tt.parse_args([])
    assert args.turns == 1
    assert args.interaction is None
    assert args.config == "configs/connections.json"
    assert args.log == "data/turn_trace.db"
    assert args.out == "data/traces"


def test_cli_accepts_turns_and_isolated_interaction():
    args = tt.parse_args(["--turns", "3", "--interaction", "extract",
                          "--user-message", "hi", "--narrative", "prose"])
    assert args.turns == 3
    assert args.interaction == "extract"
    assert args.user_message == "hi"
    assert args.narrative == "prose"


def test_cli_rejects_unknown_interaction():
    with pytest.raises(SystemExit):
        tt.parse_args(["--interaction", "not_a_thing"])


def test_isolated_interactions_cover_the_post_turn_agents():
    """Only the post-turn MODEL passes have an isolated mode (attribution is code)."""
    assert set(tt._ISOLATED_INTERACTIONS) == {"extract", "bts", "critic"}


# ---------------------------------------------------------------------------
# isolated message assembly (no model)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("interaction", ["extract", "bts", "critic"])
def test_assemble_interaction_messages_builds_two_message_flows(interaction):
    messages, evidence = tt.assemble_interaction_messages(
        interaction, preset=None, user_msg="UMARK", narrative="NMARK",
        prior_state={"loc": "dock"}, checkpoint=False,
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    joined = " ".join(str(m["content"]) for m in messages)
    assert "NMARK" in joined
    assert "UMARK" in joined


def test_attribution_has_no_isolated_mode():
    """Deterministic code: no messages to assemble, so no ``--interaction`` mode."""
    assert "attribution" not in tt._ISOLATED_INTERACTIONS
    with pytest.raises(ValueError, match="unknown interaction"):
        tt.assemble_interaction_messages(
            "attribution", preset=None, user_msg="U", narrative="N"
        )


def test_critic_evidence_bundle_carries_deterministic_flags():
    _, evidence = tt.assemble_interaction_messages(
        "critic", preset=None, user_msg="I draw my blade.",
        narrative="I draw my blade. I draw my blade. A blade.",
        prior_prose=["earlier"], state_line="time: 14:35 | loc: dock",
    )
    assert evidence["user_message"] == "I draw my blade."
    assert evidence["prior_prose"] == ["earlier"]
    assert evidence["state_line"] == "time: 14:35 | loc: dock"
    assert isinstance(evidence["flags"], list)  # computed by the real validator


def test_assemble_interaction_rejects_unknown():
    with pytest.raises(ValueError):
        tt.assemble_interaction_messages("summarize", preset=None,
                                         user_msg="u", narrative="n")
