"""Post-turn critic-fixer integration (W8.1, D41/D49).

Covers the graph call site end to end against the real event log and the real
pure core (:mod:`fabulist.graph.quality`):

* each verdict (``ok`` / ``diffs`` / ``fail``) and its log event,
* a ``diffs`` verdict supersedes the narrative AND updates ``segments``,
* a rejected patch leaves the turn byte-identical (non-fatal), and a patch whose
  edits PARTLY land commits the landed subset (edits apply independently),
* a ``fail`` verdict KEEPS the turn and logs it — the regeneration it used to ask
  for is retired (W9.24 §3.3.7), so one sweep judges once and rewrites nothing,
* a raising critic and an unparseable reply are both non-fatal,
* the evidence bundle the critic sees (and what it deliberately does not),
* an ACTIVE preset with no ``critic`` flow row still compiles (``preset_store.py:804``),
* the narrate builder's single-ask rule (the loop appends to that same prompt).
"""

from __future__ import annotations

import asyncio
import json

from fabulist.graph.engine import GraphTurnEngine
from fabulist.graph.graph import build_graph
from fabulist.graph.nodes import make_post_turn_node
from fabulist.prompts.preset_store import compile_preset_flow
from fabulist.wiring import _render_critic_payload


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    return [event async for event in agen]


# --- fakes ------------------------------------------------------------------


class ChannelModel:
    """ModelCaller yielding pre-built content events (narrator stand-in)."""

    def __init__(self, text: str):
        self.text = text

    async def __call__(self, messages, stream=True):
        yield {"type": "content", "delta": self.text}


class FakeCritic:
    """Records every evidence bundle it sees; returns a canned reply or raises."""

    def __init__(self, reply):
        self.reply = reply
        self.evidence: list[dict] = []

    async def __call__(self, evidence):
        self.evidence.append(evidence)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class CapturingExtractor:
    """Sync extractor that records the (user, prose) it was handed."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, user_msg, prose):
        self.calls.append({"user_msg": user_msg, "prose": prose})
        return {"deltas": {}, "action_class": "dialogue"}


class CapturingWorldState:
    """Async BTS seam that records the prose it was handed."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, user_msg, prose, prior_state, checkpoint=False):
        self.calls.append({"user_msg": user_msg, "prose": prose})
        return {"kind": "delta", "state": {}, "deltas": {}}


class CapturingAttribution:
    """Async attribution seam that records the prose it was handed."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, user_msg, prose):
        self.calls.append({"user_msg": user_msg, "prose": prose})
        return []


def _reply(verdict, **extra):
    payload = {"verdict": verdict, "reason": extra.pop("reason", "because"), "edits": extra.pop("edits", [])}
    payload.update(extra)
    return json.dumps(payload)


PROSE = "The rain kept falling on the empty street."


def _state(**overrides):
    state = {
        "session_id": "s1",
        "user_content": "I wait under the awning.",
        "narrative_parts": [PROSE],
        "dsl": "",
        "segments": [{"kind": "prose", "text": PROSE}],
        "facts": "time: 09:15",
        "action_class": "dialogue",
    }
    state.update(overrides)
    return state


def _events(log, name):
    return [e for e in log.get_events("s1") if e.payload.get("event") == name]


def _revisions(log):
    return [e for e in log.get_events("s1") if e.type == "revision"]


def _node(log, critic, **kwargs):
    return make_post_turn_node(
        extractor=lambda u, p: {"deltas": {}, "action_class": "dialogue"},
        log=log,
        critic=critic,
        **kwargs,
    )


# --- verdict: ok ------------------------------------------------------------


def test_ok_keeps_turn_and_logs_verdict(log):
    critic = FakeCritic(_reply("ok", reason="advances the scene"))
    run(_node(log, critic)(_state()))

    assert _revisions(log) == []
    projection = log.get_projection("s1")
    assert projection["narratives"][-1]["payload"]["prose"] == PROSE

    verdicts = _events(log, "critic_verdict")
    assert len(verdicts) == 1
    assert verdicts[0].payload["verdict"] == "ok"
    assert verdicts[0].payload["attempt"] == 0
    assert verdicts[0].payload["reason"] == "advances the scene"
    assert verdicts[0].payload["edits"] == []


# --- verdict: diffs ---------------------------------------------------------


def test_diffs_supersedes_narrative_and_updates_segments(log):
    critic = FakeCritic(
        _reply(
            "diffs",
            reason="repeats the closing image",
            edits=[{"find": "kept falling", "replace": "still drummed"}],
        )
    )
    run(_node(log, critic)(_state()))

    revisions = _revisions(log)
    assert len(revisions) == 1
    assert revisions[0].actor == "critic"
    narrative = log.get_projection("s1")["narratives"][-1]
    # Supersede target is the narrative event, and BOTH channels are rebuilt.
    assert narrative["payload"]["prose"] == "The rain still drummed on the empty street."
    assert narrative["payload"]["segments"] == [
        {"kind": "prose", "text": "The rain still drummed on the empty street."}
    ]


def test_diffs_verdict_event_carries_applied_edits(log):
    critic = FakeCritic(
        _reply("diffs", edits=[{"find": "rain", "replace": "sleet"}])
    )
    run(_node(log, critic)(_state()))
    verdict = _events(log, "critic_verdict")[0]
    assert verdict.payload["verdict"] == "diffs"
    assert verdict.payload["edits"] == [{"find": "rain", "replace": "sleet", "count": 1}]
    assert _events(log, "critic_failed") == []


def test_rejected_patch_leaves_turn_byte_identical(log):
    # Every ``find`` is absent from the prose -> nothing lands, nothing revises.
    critic = FakeCritic(
        _reply("diffs", edits=[{"find": "not in the prose at all", "replace": "x"}])
    )
    run(_node(log, critic)(_state()))

    assert _revisions(log) == []
    projection = log.get_projection("s1")["narratives"][-1]["payload"]
    assert projection["prose"] == PROSE
    assert projection["segments"] == [{"kind": "prose", "text": PROSE}]

    verdict = _events(log, "critic_verdict")[0]
    assert verdict.payload["verdict"] == "diffs"
    assert verdict.payload["edits"] == []
    failed = _events(log, "critic_failed")
    assert len(failed) == 1
    assert failed[0].payload["stage"] == "apply"
    assert "not in the prose at all" in failed[0].payload["error"]
    assert failed[0].payload["landed"] == 0


def test_partial_patch_lands_the_valid_edit(log):
    """Edits apply INDEPENDENTLY: one bad edit costs only itself.

    The live failure behind the ruling: a ``find`` that copied a seeded line
    number was rejected and the atomic apply discarded a valid, verbatim prose
    repair from the same patch. The landed subset commits as the revision.
    """
    critic = FakeCritic(
        _reply(
            "diffs",
            edits=[
                {"find": "5 not in the prose at all", "replace": "x"},
                {"find": "kept falling", "replace": "still drummed"},
            ],
        )
    )
    run(_node(log, critic)(_state()))

    revisions = _revisions(log)
    assert len(revisions) == 1
    assert revisions[0].actor == "critic"
    narrative = log.get_projection("s1")["narratives"][-1]["payload"]
    assert narrative["prose"] == "The rain still drummed on the empty street."
    assert narrative["segments"] == [
        {"kind": "prose", "text": "The rain still drummed on the empty street."}
    ]

    verdict = _events(log, "critic_verdict")[0]
    assert verdict.payload["verdict"] == "diffs"
    # The row records the edits that LANDED, not the ones the critic proposed.
    assert verdict.payload["edits"] == [
        {"find": "kept falling", "replace": "still drummed", "count": 1}
    ]

    failed = _events(log, "critic_failed")
    assert len(failed) == 1
    row = failed[0].payload
    assert row["stage"] == "apply"
    assert row["landed"] == 1
    assert "not in the prose at all" in row["error"]
    # One per rejected edit, with the repair loop's teaching payload.
    (rejected,) = row["rejected"]
    assert rejected["failed_index"] == 0
    assert rejected["attempted_find"] == "5 not in the prose at all"
    assert any(
        "kept falling" in entry for entry in rejected["prose_segments"]
    ), rejected["prose_segments"]
    assert "identical retry will fail identically" in rejected["fix"]


def test_all_rejected_edits_each_get_a_reason(log):
    critic = FakeCritic(
        _reply(
            "diffs",
            edits=[
                {"find": "absent one", "replace": "x"},
                {"find": "absent two", "replace": "y"},
            ],
        )
    )
    run(_node(log, critic)(_state()))

    assert _revisions(log) == []
    failed = _events(log, "critic_failed")
    assert len(failed) == 1
    rejected = failed[0].payload["rejected"]
    assert [entry["failed_index"] for entry in rejected] == [0, 1]
    assert [entry["attempted_find"] for entry in rejected] == [
        "absent one",
        "absent two",
    ]


# --- verdict: fail ----------------------------------------------------------


def test_fail_keeps_the_turn_and_logs_the_verdict(log):
    """W9.24 §3.3.7: ``fail`` is KEEP + LOG — no re-entry, no revision.

    This is D49's cap-spent behaviour, now unconditional: the narrator's own
    repair loop runs (earlier and cheaper) before the commit, so a defect only
    the critic can see lands kept, with its verdict row keeping it visible (R1,
    the epic's accepted trade).
    """
    critic = FakeCritic(_reply("fail", reason="stalls", regen_feedback="advance it"))
    run(_node(log, critic)(_state()))

    assert _revisions(log) == []
    narrative = log.get_projection("s1")["narratives"][-1]["payload"]
    assert narrative["prose"] == PROSE
    assert narrative["segments"] == [{"kind": "prose", "text": PROSE}]
    assert len(critic.evidence) == 1  # judged once, never re-judged
    verdict = _events(log, "critic_verdict")[0]
    assert verdict.payload["verdict"] == "fail"
    assert verdict.payload["attempt"] == 0
    assert _events(log, "critic_failed") == []


def test_fail_requesting_a_rerun_still_keeps_the_turn(log):
    # A drifted prompt that still sends regen_feedback cannot re-enter anything:
    # the field is tolerated at parse and never consumed.
    critic = FakeCritic(_reply("fail", reason="beyond repair", regen_feedback="advance it"))
    run(_node(log, critic)(_state()))
    assert _revisions(log) == []
    assert log.get_projection("s1")["narratives"][-1]["payload"]["prose"] == PROSE


# --- non-fatal failures -----------------------------------------------------


def test_critic_raising_is_non_fatal(log):
    critic = FakeCritic(RuntimeError("critic exploded"))
    run(_node(log, critic)(_state()))

    assert _revisions(log) == []
    assert log.get_projection("s1")["narratives"][-1]["payload"]["prose"] == PROSE
    failed = _events(log, "critic_failed")
    assert len(failed) == 1
    assert failed[0].payload["stage"] == "call"
    assert "critic exploded" in failed[0].payload["error"]


def test_unparseable_output_is_non_fatal(log):
    critic = FakeCritic("I could not decide, sorry.")
    run(_node(log, critic)(_state()))

    assert _revisions(log) == []
    assert log.get_projection("s1")["narratives"][-1]["payload"]["prose"] == PROSE
    failed = _events(log, "critic_failed")
    assert len(failed) == 1
    assert failed[0].payload["stage"] == "parse"


# --- evidence bundle --------------------------------------------------------


def test_evidence_bundle_carries_only_the_agreed_artifacts(log):
    log.append_event("s1", "narrative", "narrator", {"prose": "An earlier beat.", "segments": []})
    log.append_event("s1", "state-delta", "bts", {"loc": "night market", "tone": "tense"})
    critic = FakeCritic(_reply("ok"))
    run(
        _node(log, critic)(
            _state(
                recall="SECRET RECALL",
                plan="action: dialogue",
                facts="time: 09:15 | loc: night market",
            )
        )
    )

    evidence = critic.evidence[0]
    # Exactly the agreed keys — no DSL, no facts/recall/roster, no full history.
    assert set(evidence) == {"user_message", "prose", "flags", "prior_prose", "state_line"}
    assert evidence["user_message"] == "I wait under the awning."
    assert evidence["prose"] == PROSE
    assert evidence["prior_prose"] == ["An earlier beat."]
    # The clock's advance overlays the fold so the state line is current.
    assert "time: 09:15" in evidence["state_line"]
    assert "loc: night market" in evidence["state_line"]


def test_prior_prose_skips_image_narratives(log):
    """A generated-image narrative row is a canon image carrying no prose: the
    critic's ``prior_prose`` must read the last REAL narrative, never the image."""
    log.append_event(
        "s1", "narrative", "narrator", {"prose": "An earlier beat.", "segments": []}
    )
    # A generated image lands as the NEWEST narrative row (imagegen, no prose).
    log.append_event(
        "s1",
        "narrative",
        "imagegen",
        {
            "prose": "",
            "kind": "image",
            "image_asset_id": "asset-img-1",
            "prompt": "the tavern",
        },
    )
    critic = FakeCritic(_reply("ok"))
    run(_node(log, critic)(_state()))

    evidence = critic.evidence[0]
    # The image row would otherwise contribute a trailing empty string here.
    assert evidence["prior_prose"] == ["An earlier beat."]


def test_critic_payload_omits_dsl_facts_and_line_numbers(log):
    """The prose block is PLAIN: one linebreak between lines, no numbering.

    Live turn: the critic copied the seeded 1-based number along with the line
    (``find`` ``"5 Sable lets out…"`` against prose matching verbatim), and the
    atomic apply threw away a valid correction. The prose block must therefore
    carry nothing the prose does not.
    """
    payload = _render_critic_payload(
        {
            "user_message": "I wait.",
            "prose": "First line.\nSecond line.",
            "flags": [],
            "prior_prose": ["An earlier beat."],
            "state_line": "time: 09:15",
        }
    )
    assert "First line.\nSecond line." in payload
    assert "1 First line." not in payload
    assert "2 Second line." not in payload
    assert "1-based" not in payload
    assert "### USER MESSAGE" in payload
    assert "### THIS TURN (the turn's prose; copy `find` text verbatim from it)" in payload
    assert "(none)" in payload  # empty flags
    assert "An earlier beat." in payload
    assert "time: 09:15" in payload
    # Nothing from the deliberately-excluded artifacts can appear: the builder is
    # never handed DSL / facts / recall / roster in the first place.
    assert "openui" not in payload


# --- preset fallback (preset_store.py:804) ----------------------------------


def test_critic_flow_falls_back_when_active_preset_lacks_row():
    # preset_store.py:804 — `modules = preset.flows.get(interaction) or
    # DEFAULT_FLOWS[interaction]`: an ACTIVE preset with no ``critic`` row still
    # compiles through the default flow, with ``parts["system"]`` supplying the
    # engine CRITIC_SYSTEM_PROMPT.
    class _ActivePreset:
        flows = {"narrate": []}  # active preset, but no "critic" row
        prompt_overrides: dict = {}

    messages = compile_preset_flow(
        _ActivePreset(),
        "critic",
        {"system": "SYS", "payload": "PAY", "variables": {}},
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == "SYS"
    assert messages[1]["content"] == "PAY"


# --- frame on the live stream -----------------------------------------------


def test_revision_frame_is_emitted_before_done(log):
    model = ChannelModel(PROSE)
    critic = FakeCritic(
        _reply("diffs", edits=[{"find": "kept falling", "replace": "still drummed"}])
    )
    graph = build_graph(
        model=model,
        extractor=lambda u, p: {"deltas": {}, "action_class": "dialogue"},
        log=log,
        critic=critic,
        decision=FlaggingGate(),
    )
    engine = GraphTurnEngine(graph=graph, log=log, model=model)
    events = run(collect(engine.run_turn("s1", "I wait", "in_character")))

    types = [e["type"] for e in events]
    assert "revision" in types
    assert types.index("revision") < types.index("done")
    frame = next(e for e in events if e["type"] == "revision")
    assert frame["meta"]["verdict"] == "diffs"
    assert frame["meta"]["segments"] == [
        {"kind": "prose", "text": "The rain still drummed on the empty street."}
    ]
    assert frame["meta"]["target_id"] is not None
    assert frame["meta"]["revision_id"] is not None


# --- downstream steps read the FINAL prose (D41/D49 integrity fix) ----------
#
# The critic runs AFTER the narrative/user events are committed, so the steps
# that follow it (extractor fallback, BTS world-state, attribution) must consume
# the prose the projection will actually render — not the superseded draft. A
# stale read here silently desyncs the fold from the narrative (the state would
# describe a turn the user was told to discard).

PATCHED_PROSE = "The rain still drummed on the empty street."


def test_diffs_patched_prose_reaches_downstream_extractor(log):
    extractor = CapturingExtractor()
    critic = FakeCritic(
        _reply("diffs", edits=[{"find": "kept falling", "replace": "still drummed"}])
    )
    run(make_post_turn_node(extractor=extractor, log=log, critic=critic)(_state()))

    assert extractor.calls[0]["prose"] == PATCHED_PROSE
    assert extractor.calls[0]["prose"] != PROSE


def test_diffs_patched_prose_reaches_downstream_world_state(log):
    world_state = CapturingWorldState()
    critic = FakeCritic(
        _reply("diffs", edits=[{"find": "kept falling", "replace": "still drummed"}])
    )
    node = make_post_turn_node(
        extractor=lambda u, p: {"deltas": {}, "action_class": "dialogue"},
        log=log,
        critic=critic,
        world_state=world_state,
    )
    run(node(_state()))

    assert world_state.calls[0]["prose"] == PATCHED_PROSE


def test_raising_critic_leaves_downstream_prose_byte_identical(log):
    extractor = CapturingExtractor()
    critic = FakeCritic(RuntimeError("critic exploded"))
    run(make_post_turn_node(extractor=extractor, log=log, critic=critic)(_state()))

    assert extractor.calls[0]["prose"] == PROSE
    assert _revisions(log) == []


def test_garbage_critic_leaves_downstream_prose_byte_identical(log):
    extractor = CapturingExtractor()
    critic = FakeCritic("not a verdict at all")
    run(make_post_turn_node(extractor=extractor, log=log, critic=critic)(_state()))

    assert extractor.calls[0]["prose"] == PROSE
    assert _revisions(log) == []


def test_rejected_patch_leaves_downstream_prose_byte_identical(log):
    extractor = CapturingExtractor()
    critic = FakeCritic(
        _reply("diffs", edits=[{"find": "absent text", "replace": "x"}])
    )
    run(make_post_turn_node(extractor=extractor, log=log, critic=critic)(_state()))

    assert extractor.calls[0]["prose"] == PROSE


def test_fail_verdict_leaves_downstream_prose_byte_identical(log):
    extractor = CapturingExtractor()
    critic = FakeCritic(_reply("fail", reason="beyond repair"))
    run(make_post_turn_node(extractor=extractor, log=log, critic=critic)(_state()))

    assert extractor.calls[0]["prose"] == PROSE


def test_narrate_builder_never_duplicates_the_current_user_row(log):
    # The builder drops the turn's own user row from history (the newest user
    # row in the projection) and appends it only as the state-wrapped final
    # user message — so the live narrate path carries the ask ONCE. The row is
    # dropped POSITIONALLY, never by text, so a repeat of the same line is
    # still dropped exactly once. (This golden previously pinned the old
    # duplication; the defect fix in ``history_from_log(drop_current_user=...)``
    # corrects it — see RESUME for the fix entry.)
    from fabulist.wiring import make_narrator_message_builder

    build = make_narrator_message_builder(log)

    # (a) narrate-node timing in isolation (turn not yet committed): once.
    before = build(_state())
    # system prefix, the ephemeral engine envelope (issue #44), then the BARE ask.
    assert [m["role"] for m in before] == ["system", "assistant", "user"]
    assert (
        sum(1 for m in before if "I wait under the awning." in str(m.get("content")))
        == 1
    )

    # (b) live API timing (``_drive_turn`` pre-appends the user event): still
    # once — the history row is dropped and only the state-wrapped ask remains.
    log.append_event(
        "s1",
        "user",
        "user",
        {"text": "I wait under the awning.", "kind": "in_character"},
    )
    after = build(_state())
    assert [m["role"] for m in after] == ["system", "assistant", "user"]
    assert (
        sum(1 for m in after if "I wait under the awning." in str(m.get("content")))
        == 1
    )


def _payload(content: object) -> str:
    """The message body without the served two-bound envelope (issue #26).

    ``<preset_def>`` wraps the engine definition through the player's latest
    message, ``<preset_runtime>`` the per-turn tail after it; the guard line (on
    the first def message) sits outside both.
    """
    from fabulist.prompts.wrapper import (
        DEF_CLOSE,
        DEF_OPEN,
        GUARD_LINE,
        RUNTIME_CLOSE,
        RUNTIME_OPEN,
    )

    text = str(content)
    if text.startswith(GUARD_LINE):
        text = text[len(GUARD_LINE) :].lstrip("\n")
    for open_tag, close_tag in ((DEF_OPEN, DEF_CLOSE), (RUNTIME_OPEN, RUNTIME_CLOSE)):
        prefix = f"{open_tag}\n"
        if text.startswith(prefix) and text.rstrip().endswith(close_tag):
            body = text[len(prefix) :].rstrip()
            return body[: -len(close_tag)].rstrip("\n")
    return text


def test_narrate_builder_keeps_prior_user_rows_and_a_repeat_drops_once(log):
    # The drop is positional: a prior user row survives in history, and a turn
    # that repeats an earlier line still drops exactly the newest row.
    from fabulist.wiring import make_narrator_message_builder

    log.append_event("s1", "user", "user", {"text": "I wait under the awning."})
    log.append_event(
        "s1",
        "narrative",
        "narrator",
        {"prose": "Rain drums.", "segments": [{"kind": "prose", "text": "Rain drums."}]},
    )
    # The current turn repeats the earlier line verbatim — text matching would
    # be ambiguous here; position is not.
    log.append_event(
        "s1", "user", "user", {"text": "I wait under the awning.", "kind": "in_character"}
    )
    build = make_narrator_message_builder(log)
    after = build(_state())

    assert [m["role"] for m in after] == [
        "system",
        "user",
        "assistant",
        "assistant",
        "user",
    ]
    # The prior user row is intact in history; the current (repeated) row rides
    # exactly once, as the live ask.
    assert _payload(after[1]["content"]) == "I wait under the awning."
    assert _payload(after[2]["content"]) == "Rain drums."
    assert sum(1 for m in after if "I wait under the awning." in str(m.get("content"))) == 2


class FlaggingGate:
    """Decision seam that always fires a flag (W9.19 B: the critic is conditional)."""

    enabled = True

    async def ask(self, state, questions):
        items = questions.items() if isinstance(questions, dict) else ()
        return {
            qid: {"noul": 1.0}
            for qid, row in items
            if isinstance(row, dict) and row.get("type") == "noul"
        }
