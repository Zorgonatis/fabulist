"""Node-level regression tests for the narrator DSL gate + repair ladder.

Covers the four live defects end-to-end through the narrator/post_turn nodes:

1. the last-resort ``revalidate_with_retry`` path is now wired (was dead code);
2. dangling refs are dropped WITH a surfaced flag (never a silent empty pane);
3. kwargs forms are detected and deterministically repaired (no model call);
4. ``dsl_validation`` outcomes surface through the inspector read model.

No live models: the model is a scripted ``ModelCaller`` fake.

W1f: the last-resort re-synthesis also carries its own capture label
(``narrate_resynth``), so a repaired turn does not read as one more base pass.
The re-synthesis itself is POSITION-PRESERVING (live 54d73764): it may only
replace the DSL statements, by slot, and can never rewrite the prose or move a
panel.
"""

from __future__ import annotations

import asyncio
import json

from fabulist.agents.debug import CURRENT_INTERACTION
from fabulist.graph import build_graph
from fabulist.graph import nodes as nodes_module
from fabulist.graph.nodes import make_narrator_node, make_post_turn_node, segment_narrative
from fabulist.log import EventLog

STATE = {"session_id": "s1", "user_content": "go", "facts": "", "plan": ""}


def run(coro):
    return asyncio.run(coro)


class ScriptedModel:
    """A ``ModelCaller`` returning one whole content response per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        text = self.responses.pop(0) if self.responses else ""
        yield {"type": "content", "delta": text}


class FakeExtractor:
    async def __call__(self, user_msg, prose):
        return {"deltas": {}, "action_class": "dialogue"}


class LabelRecordingModel:
    """Scripted model that records the capture label in force per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.labels: list[str] = []
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        self.labels.append(CURRENT_INTERACTION.get(""))
        text = self.responses.pop(0) if self.responses else ""
        yield {"type": "content", "delta": text}


# ---------------------------------------------------------------------------
# deterministic repair through the narrator node
# ---------------------------------------------------------------------------


def test_narrator_repairs_kwargs_without_model_retry():
    text = (
        "The room is cold.\n"
        'card = ArtifactCard(name="Tense", kind="Study", description="08:30")\n'
        'root = ScenePane("Study", [card])\n'
    )
    model = ScriptedModel([text])
    node = make_narrator_node(model=model)
    out = run(node(dict(STATE)))

    # One model call only: kwargs repair is deterministic.
    assert len(model.calls) == 1
    dsl_text = out["dsl"]
    assert 'ArtifactCard("Tense", "Study", "08:30")' in dsl_text
    assert "mood=" not in dsl_text
    assert any(e["code"] == "named_args" and e["repaired"] for e in out["validation_errors"])


def test_narrator_drops_dangling_refs_with_flag():
    text = (
        "The market hums.\n"
        'card = ArtifactCard("Calm", "Market", "10:00")\n'
        'root = ScenePane("Market", [card, cat_mind])\n'
    )
    node = make_narrator_node(model=ScriptedModel([text]))
    out = run(node(dict(STATE)))

    assert out["dsl"].endswith('root = ScenePane("Market", [card])')
    codes = [e["code"] for e in out["validation_errors"]]
    assert "dangling_ref" in codes
    assert "cat_mind" in next(e["message"] for e in out["validation_errors"] if e["code"] == "dangling_ref")


# ---------------------------------------------------------------------------
# issue #30: a prune that would EMPTY the root is a repair round, not a commit
# ---------------------------------------------------------------------------

#: The exact composition one live narration produced: the root's ONLY child is an
#: identifier no segment declares. The prune emptied the child list, the empty
#: result was filed as an `info` repair, and the turn committed
#: `root = ScenePane("…", [])` — a hollow panel as a successful turn.
HOLLOW_DRAFT = (
    "The kitchen narrows and the house business goes quiet.\n"
    'root = ScenePane("Kitchen — house business, whispered", [chline])\n'
)


def test_all_dangling_children_bounce_the_turn_into_one_resynthesis_round():
    """THE PIN: the observed case bounces and never commits a hollow pane.

    Refusing the prune makes the composition a HARD failure, so the turn takes
    its one re-synthesis round (the position-preserving DSL seam) instead of
    committing `ScenePane(title, [])`. Nothing here is a live model: the reply is
    scripted, and the assertion is on the node's own decision.
    """
    model = ScriptedModel(
        [
            HOLLOW_DRAFT,
            _reply(
                'chline = BeatCard("House business, whispered.")\n'
                'root = ScenePane("Kitchen — house business, whispered", [chline])'
            ),
        ]
    )
    node = make_narrator_node(model=model)
    out = run(node(dict(STATE)))

    assert len(model.calls) == 2  # the bounce: one round, never a silent commit
    assert "[]" not in out["dsl"]
    assert out["dsl"].endswith(
        'root = ScenePane("Kitchen — house business, whispered", [chline])'
    )
    assert "chline = BeatCard(" in out["dsl"]
    assert out["validation_errors"] == []
    assert out["validation"]["retry_applied"] is True


def test_hollow_root_is_kept_with_its_finding_when_the_round_changes_nothing():
    """Budget spent: a VISIBLE failure, never a hollow pane and never a drop.

    The re-synthesis returns the draft's own text (so it is discarded as no
    repair), and the authored statement stands — with the undeclared ref still in
    the channel and an unrepaired, warn-severity finding anchored to the segment.
    """
    from fabulist.graph.validation import dsl_error_flags

    model = ScriptedModel(
        [
            HOLLOW_DRAFT,
            # Same statement back: repairs nothing, so the draft report stands.
            _reply('root = ScenePane("Kitchen — house business, whispered", [chline])'),
        ]
    )
    node = make_narrator_node(model=model)
    out = run(node(dict(STATE)))

    assert len(model.calls) == 2  # capped: one round, never a spiral
    # Kept as authored — nothing dropped, and no empty child list either way.
    assert "[]" not in out["dsl"]
    assert "chline" in out["dsl"]
    # Visible: an UNREPAIRED finding, which the flags projection makes a `warn`.
    assert [e["code"] for e in out["validation_errors"]] == ["empty_after_prune"]
    assert out["validation_errors"][0]["repaired"] is False
    flags = dsl_error_flags(out["validation_errors"])
    assert [f["severity"] for f in flags] == ["warn"]
    assert "chline" in flags[0]["message"]


# ---------------------------------------------------------------------------
# last-resort model re-synthesis (defect 1: dead repair now wired)
# ---------------------------------------------------------------------------


#: An arity-broken statement naming a *known* component is segmented as DSL
#: (the segmenter recognizes the `ident = Component(` shape; the gate rejects
#: the arity), so it exercises the last-resort retry path. Wave 2b widened
#: every signature with a trailing optional `options` arg, so over-arity now
#: needs one arg beyond THAT (3 of max 2).
_BROKEN = 'hook = BeatCard("a", "b", "c")\n'


def _reply(*statements: str) -> str:
    """The structured re-synthesis reply the narrowed contract asks for."""
    return json.dumps({"statements": list(statements)})


def test_narrator_last_resort_model_resynthesis_recovers():
    model = ScriptedModel([
        _BROKEN,  # first attempt is unrecoverable deterministically
        _reply('hook = BeatCard("recovered")'),  # constrained re-synthesis succeeds
    ])
    node = make_narrator_node(model=model)
    out = run(node(dict(STATE)))

    assert len(model.calls) == 2  # first call + exactly one retry
    assert out["dsl"] == 'hook = BeatCard("recovered")'
    # The retry prompt is the constrained, DSL-ONLY repair prompt: the rejected
    # statement is handed over as a numbered slot, never the prose.
    repair_system = model.calls[1][0]["content"]
    repair_user = model.calls[1][-1]["content"]
    assert '"statements"' in repair_system
    assert "positional" in repair_system
    assert "Statements to repair" in repair_user
    assert _BROKEN.strip() in repair_user


def test_narrator_retry_is_capped_at_one_attempt():
    model = ScriptedModel([_BROKEN, _reply('hook = BeatCard("x", "y", "z")')])
    node = make_narrator_node(model=model)
    out = run(node(dict(STATE)))

    assert len(model.calls) == 2  # never spirals
    # Keep + tag: the still-broken statement is committed verbatim (nothing
    # dropped); its arity flag anchors to the segment.
    assert out["dsl"] == 'hook = BeatCard("x", "y", "z")'
    assert [e["code"] for e in out["validation_errors"]] == ["arity_mismatch"]


def test_narrator_explicit_resynthesize_seam_is_used():
    async def resynthesize(errors):
        return [{"kind": "dsl", "text": 'hook = BeatCard("via fake")'}]

    node = make_narrator_node(
        model=ScriptedModel([_BROKEN]), resynthesize=resynthesize
    )
    out = run(node(dict(STATE)))
    assert out["dsl"] == 'hook = BeatCard("via fake")'


def test_resynthesis_is_labeled_narrate_resynth_for_capture():
    """The model re-synthesis relabels the call it makes (W1f).

    The pump labels the base narrator pass ``narrate``; the last-resort
    re-synthesis is a real extra model call and must say so, or a repaired
    turn's captures/usage rows show two identical ``narrate`` rows (the W1e
    audit's labeling defect). ``narrate_resynth`` distinguishes it from the
    in-node repair loop's own rows.
    """
    model = LabelRecordingModel([_BROKEN, _reply('hook = BeatCard("repaired")')])
    token = CURRENT_INTERACTION.set("narrate")  # what the pump sets per turn
    try:
        node = make_narrator_node(model=model)
        out = run(node(dict(STATE)))
    finally:
        CURRENT_INTERACTION.reset(token)

    assert model.labels == ["narrate", "narrate_resynth"]
    assert out["dsl"] == 'hook = BeatCard("repaired")'


def test_no_repair_pass_leaves_the_narrate_label_alone():
    """A clean turn calls the model once and never relabels."""
    model = LabelRecordingModel(['hook = BeatCard("clean")\n'])
    token = CURRENT_INTERACTION.set("narrate")
    try:
        node = make_narrator_node(model=model)
        run(node(dict(STATE)))
        assert CURRENT_INTERACTION.get("") == "narrate"
    finally:
        CURRENT_INTERACTION.reset(token)

    assert model.labels == ["narrate"]


# ---------------------------------------------------------------------------
# position-preserving re-synthesis (live 54d73764: panels stacked at the bottom)
# ---------------------------------------------------------------------------

#: An INTERLEAVED draft — prose → dsl → prose → dsl — that fails the gate in the
#: UNRECOVERABLE class: the ``Custom`` fragment's root declares ``height:100%``,
#: which the policy rejects (the artifact host is content-sized), so the gate's
#: deterministic floor is a paper-scrap and ``ok`` stays false.
INTERLEAVED_DRAFT = (
    "The lanterns gutter along the pier.\n"
    'art = Custom("Pier", "<div style=\"height:100%\">docks</div>", "div {border:0}")\n'
    "Nobody answers the bell.\n"
    'hook = BeatCard("A gull screams overhead.")\n'
)

#: The repaired statements, one per DSL slot in draft order. The reply is a JSON
#: object because the narrowed contract says so — no prose, no re-layout.
INTERLEAVED_REPLY = _reply(
    'art = Document("scrap", "Pier", "docks")',
    'hook = BeatCard("A gull screams overhead.")',
)


def _kinds(segments):
    return [segment["kind"] for segment in segments]


def _prose(segments):
    return [segment["text"] for segment in segments if segment["kind"] == "prose"]


def _payload_events(log, event):
    return [
        row.payload
        for row in log.get_events("s1")
        if row.type == "system"
        and isinstance(row.payload, dict)
        and row.payload.get("event") == event
    ]


def test_resynthesis_repairs_dsl_in_place_and_never_touches_prose(tmp_path):
    """The gate may never rewrite prose or reorder panels (live 54d73764).

    A draft whose DSL the gate cannot repair deterministically goes through the
    last-resort model pass. The committed turn must be the DRAFT's layout with
    the DSL statements swapped in: prose byte-identical, the two dsl segments at
    the ordinals they were drafted at (interleaved, never stacked at the end).
    """
    frames: list[dict] = []
    original_writer = nodes_module._stream_writer
    nodes_module._stream_writer = lambda: frames.append
    log = EventLog(tmp_path / "events.db")
    model = LabelRecordingModel([INTERLEAVED_DRAFT, INTERLEAVED_REPLY])
    token = CURRENT_INTERACTION.set("narrate")  # what the pump sets per turn
    try:
        node = make_narrator_node(model=model, log=log)
        out = run(node(dict(STATE)))
    finally:
        CURRENT_INTERACTION.reset(token)
        nodes_module._stream_writer = original_writer

    try:
        draft = segment_narrative(INTERLEAVED_DRAFT)
        assert len(model.calls) == 2  # draft + exactly one re-synthesis

        # (i) the committed ORDER is the draft's: prose → dsl → prose → dsl.
        assert _kinds(out["segments"]) == ["prose", "dsl", "prose", "dsl"]
        assert _kinds(out["segments"]) == _kinds(draft)
        # (ii) the prose is byte-identical (and equal modulo whitespace).
        assert _prose(out["segments"]) == _prose(draft)
        assert " ".join(_prose(out["segments"])) == " ".join(_prose(draft))
        assert out["narrative_parts"] == ["\n".join(_prose(draft))]
        # The DSL statements were replaced in their own slots, nothing moved.
        assert 'art = Document("scrap", "Pier", "docks")' == out["segments"][1]["text"]
        assert 'hook = BeatCard("A gull screams overhead.")' == out["segments"][3]["text"]
        assert "Custom(" not in out["dsl"]

        # (iii) ONE `dsl_resynthesis` row records the pass and its outcome.
        rows = _payload_events(log, "dsl_resynthesis")
        assert len(rows) == 1
        row = rows[0]
        assert row["retry_applied"] is True
        assert row["kept_draft"] is False
        assert row["call_latency_s"] >= 0.0
        assert [error["code"] for error in row["errors"]] == ["root_percentage_height"]

        # (iv) the extra call is tagged as its own pass, not a second `narrate`.
        assert model.labels == ["narrate", "narrate_resynth"]

        # The state tag says the pass ran and the replacement landed.
        assert out["validation"] == {"retry_applied": True, "resynth_kept_draft": False}

        # The dsl step explains itself (the note rides its `done` frame).
        dsl_notes = [
            frame["meta"].get("notes") or []
            for frame in frames
            if frame.get("type") == "stage"
            and (frame.get("meta") or {}).get("stage") == "dsl"
            and (frame.get("meta") or {}).get("state") == "done"
        ]
        assert len(dsl_notes) == 1
        assert any("re-synthes" in note for note in dsl_notes[0])
    finally:
        log.close()


def test_resynthesis_with_a_statement_count_mismatch_keeps_the_draft(tmp_path):
    """The structured contract is slot-counted: a short reply repairs nothing.

    Two DSL slots, one statement back → the reply is rejected and the draft
    stands (the Custom statement stays, as the gate's deterministic paper-scrap),
    with the discard tagged in the state and in the log row.
    """
    log = EventLog(tmp_path / "events.db")
    model = LabelRecordingModel(
        [INTERLEAVED_DRAFT, _reply('hook = BeatCard("A gull screams overhead.")')]
    )
    try:
        node = make_narrator_node(model=model, log=log)
        out = run(node(dict(STATE)))

        assert len(model.calls) == 2
        assert _kinds(out["segments"]) == ["prose", "dsl", "prose", "dsl"]
        assert 'art = Document("scrap", "Pier"' in out["segments"][1]["text"]
        assert out["segments"][3]["text"] == 'hook = BeatCard("A gull screams overhead.")'
        assert _prose(out["segments"]) == _prose(segment_narrative(INTERLEAVED_DRAFT))
        assert out["validation"] == {"retry_applied": True, "resynth_kept_draft": True}
        rows = _payload_events(log, "dsl_resynthesis")
        assert len(rows) == 1 and rows[0]["kept_draft"] is True
        # The gate finding that triggered the pass rides the row.
        assert [e["code"] for e in rows[0]["errors"]] == ["root_percentage_height"]
        # Keep + tag: the draft's channel is what commits, finding included.
        assert out["validation_errors"]
    finally:
        log.close()


# ---------------------------------------------------------------------------
# defect 4: dsl_validation outcomes surface as inspector-visible flags
# ---------------------------------------------------------------------------


def test_post_turn_logs_dsl_validation_and_validator_flags(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        node = make_post_turn_node(extractor=FakeExtractor(), log=log)
        state = {
            "session_id": "s1",
            "user_content": "look around",
            "narrative_parts": ["The market hums."],
            "dsl": 'card = ArtifactCard("Calm", "Market", "10:00")',
            "segments": [
                {"kind": "prose", "text": "The market hums."},
                {"kind": "dsl", "text": 'card = ArtifactCard("Calm", "Market", "10:00")'},
            ],
            "facts": "time: 09:00",
            "validation_errors": [
                {
                    "segment_index": 1,
                    "code": "dangling_ref",
                    "message": "dropped undeclared child refs: ghost",
                    "statement": 'root = ScenePane("Market", [status, ghost])',
                    "repaired": True,
                }
            ],
        }
        run(node(state))

        events = log.get_events("s1")

        dsl_events = [
            e for e in events
            if e.type == "system" and e.payload.get("event") == "dsl_validation"
        ]
        assert len(dsl_events) == 1

        flag_events = [
            e for e in events
            if e.type == "system" and e.payload.get("event") == "validator_flags"
        ]
        assert len(flag_events) == 1
        flags = flag_events[0].payload["flags"]
        assert any(f["check"] == "dsl_validation" for f in flags)
        dsl_flag = next(f for f in flags if f["check"] == "dsl_validation")
        assert dsl_flag["rule_id"] == "dangling_ref"
        assert "ghost" in dsl_flag["evidence"]
    finally:
        log.close()


def test_inspector_read_model_surfaces_dsl_flags(tmp_path):
    """The /inspect read projection picks the DSL findings up (no api/ change)."""
    from fabulist.api.inspector_api import _build_turns
    from fabulist.graph.engine import GraphTurnEngine

    log = EventLog(tmp_path / "events.db")
    try:
        model = ScriptedModel([
            # kwargs + dangling ref -> repaired deterministically, flagged.
            'The market hums.\ncard = ArtifactCard(name="Calm", kind="Market", description="10:00")\n'
            'root = ScenePane("Market", [card, ghost])\n'
        ])
        graph = build_graph(model=model, extractor=FakeExtractor(), log=log)
        engine = GraphTurnEngine(graph=graph, log=log, model=model)

        async def scenario():
            async for _ in engine.run_turn("s1", "look around", "in_character"):
                pass

        run(scenario())

        turns = _build_turns(log.get_events("s1"))
        assert turns, "no turns built"
        turn = turns[-1]
        assert turn["validation_flags"] >= 1
        checks = [
            flag["check"]
            for detail in turn["validation_details"]
            for flag in detail.get("flags", [])
        ]
        assert "dsl_validation" in checks
    finally:
        log.close()
