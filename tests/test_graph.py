"""Tests for the W1.1 engine graph (clock, nodes, graph, engine).

Uses fake `ModelCaller` / `ExtractorFn` and a tmp-file `EventLog` (PLAN §9:
canned transcripts -> deterministic assertions on resulting log state). The
suite has no pytest-asyncio dependency, so async scenarios run via
``asyncio.run`` inside sync tests.
"""

from __future__ import annotations

import asyncio
import json

from datetime import date, datetime

import pytest

from fabulist.graph import (
    GraphTurnEngine,
    advance,
    build_graph,
    classify_action,
    open_sqlite_checkpointer,
)
from fabulist.graph.clock import (
    format_instant,
    parse_facts,
    parse_instant,
    parse_plan_instant,
)
from fabulist.state.env_hud import parse_env_hud_statement
from fabulist.graph.nodes import (
    make_narrator_node,
    make_post_turn_node,
    segment_narrative,
)
from fabulist.log import EventLog


# -- helpers / fakes ---------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    return [event async for event in agen]


class FakeModel:
    """A `ModelCaller`: yields one reasoning delta then the content chunks."""

    def __init__(self, chunks=("The ", "night ", "market ", "hums."), reasoning="thinking"):
        self.chunks = list(chunks)
        self.reasoning = reasoning
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        if self.reasoning is not None:
            yield {"type": "reasoning", "delta": self.reasoning}
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield {"type": "content", "delta": chunk}


class FakeExtractor:
    """An `ExtractorFn` recording its inputs."""

    def __init__(self, deltas=None, action_class="dialogue"):
        self.deltas = dict(deltas) if deltas is not None else {"tone": "warm"}
        self.action_class = action_class
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, user_msg, prose):
        self.calls.append((user_msg, prose))
        return {"deltas": dict(self.deltas), "action_class": self.action_class}


class ChannelModel:
    """A `ModelCaller` yielding caller-supplied content events verbatim."""

    def __init__(self, events):
        self.events = events
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        for event in self.events:
            await asyncio.sleep(0)
            yield event


def events_of(log, session_id, type):
    return [e for e in log.get_events(session_id) if e.type == type]


def make_engine(log, model, extractor, **kwargs):
    graph = build_graph(model=model, extractor=extractor, log=log, **kwargs)
    return GraphTurnEngine(graph=graph, log=log, model=model)


# -- clock (deterministic rules table) ---------------------------------------


@pytest.mark.parametrize(
    ("action", "minutes", "expected"),
    [
        ("dialogue", 10, "14:45"),
        ("inspection", 20, "14:55"),
        ("travel", 25, "15:00"),
        ("task", 120, "16:35"),
    ],
)
def test_clock_advances_per_rules_table(action, minutes, expected):
    out = advance(
        action, "<state>\ntime: 14:35 | loc: night market | weather: light rain\n</state>"
    )

    assert out.advanced_minutes == minutes
    assert out.time == expected
    # non-time facts pass through untouched
    assert "loc: night market" in out.facts
    assert "weather: light rain" in out.facts


def test_clock_rest_skips_to_next_morning():
    out = advance("rest", "time: 23:30 | loc: camp")

    assert out.time == "07:00"
    assert out.advanced_minutes == 7 * 60 + 30


def test_clock_defaults_when_no_prior_time():
    out = advance("dialogue", "")

    assert out.time == "08:10"


# -- clock date fold (additive keys: date + day) -----------------------------


def test_clock_defaults_date_to_epoch():
    facts = parse_facts(advance("dialogue", "").facts)

    assert facts["date"] == "Monday, June 02, 2025"
    assert facts["day"] == "1"


def test_clock_midnight_wrap_advances_date():
    out = advance("travel", "time: 23:50 | loc: camp")
    facts = parse_facts(out.facts)

    assert out.time == "00:15"
    assert facts["date"] == "Tuesday, June 03, 2025"
    assert facts["day"] == "2"
    # other facts still pass through, now last after the date fold
    assert facts["loc"] == "camp"


def test_clock_rest_crossing_midnight_advances_date():
    out = advance("rest", "time: 23:30 | loc: camp")
    facts = parse_facts(out.facts)

    assert out.time == "07:00"
    assert facts["date"] == "Tuesday, June 03, 2025"
    assert facts["day"] == "2"


def test_clock_rest_landing_on_start_time_advances_date():
    out = advance("rest", "time: 07:00 | loc: camp")
    facts = parse_facts(out.facts)

    assert out.time == "07:00"
    assert facts["day"] == "2"


def test_clock_rest_same_day_does_not_advance_date():
    out = advance("rest", "time: 02:00 | loc: camp")
    facts = parse_facts(out.facts)

    assert out.time == "07:00"
    assert facts["date"] == "Monday, June 02, 2025"
    assert facts["day"] == "1"


def test_clock_date_round_trips_through_fold():
    first = advance("dialogue", "time: 09:00 | loc: camp")
    second = advance("dialogue", first.facts)

    assert parse_facts(second.facts)["date"] == parse_facts(first.facts)["date"]
    assert parse_facts(second.facts)["day"] == parse_facts(first.facts)["day"]


def test_clock_honors_date_already_in_facts():
    # log replay hands the clock its own prior date/day: honor, do not reset.
    out = advance("dialogue", "time: 09:00 | date: Friday, June 27, 2025 | day: 26")
    facts = parse_facts(out.facts)

    assert facts["date"] == "Friday, June 27, 2025"
    assert facts["day"] == "26"


def test_clock_instants_round_trip_through_the_proposal_helpers():
    # the fold's own spelling (HH:MM + DATE_FORMAT) is exactly what the W9.32
    # proposal helpers read, and format_instant writes the ISO absolute the
    # accepted clock_proposal audit row and the tree carry.
    out = advance("travel", "time: 23:50 | date: Monday, June 02, 2025 | day: 1")
    facts = parse_facts(out.facts)

    assert parse_instant(f"{facts['date']} at {facts['time']}") == (date(2025, 6, 3), 15)
    assert format_instant(date(2025, 6, 3), 15) == "2025-06-03 00:15"
    # the planner seeder reports the same day index the clock fold stores
    assert parse_plan_instant(f"{facts['date']} at {facts['time']}") == (
        out.time,
        facts["date"],
        int(facts["day"]),
    )


def test_classify_action_keyword_heuristic():
    assert classify_action("I rest by the fire") == "rest"
    assert classify_action("I walk down to the docks") == "travel"
    assert classify_action("I inspect the crate") == "inspection"
    assert classify_action("I forge a blade") == "task"
    assert classify_action("Hello there, friend") == "dialogue"


# -- graph end-to-end --------------------------------------------------------


def test_graph_runs_end_to_end_and_appends_log_events(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        model = FakeModel()
        extractor = FakeExtractor(deltas={"tone": "warm", "loc": "night market"})
        engine = make_engine(log, model, extractor)

        streamed = run(collect(engine.run_turn("s1", "I greet the merchant", "in_character")))

        types = [e["type"] for e in streamed]
        assert "reasoning" in types
        assert types.count("content") == 4
        assert types[-1] == "done"

        # user -> narrative -> [advisory system events] -> state-delta ->
        # env_hud (D38: the HUD statement is appended after the fold).
        types = [e.type for e in log.get_events("s1")]
        assert types[0] == "user"
        assert types[1] == "narrative"
        assert types[-2] == "state-delta"
        assert types[-1] == "system"
        assert set(types[2:-2]) <= {"system"}

        narrative = events_of(log, "s1", "narrative")[0]
        assert narrative.payload["prose"] == "The night market hums."
        assert narrative.payload["dsl"] == ""
        assert "config" in narrative.meta

        delta = events_of(log, "s1", "state-delta")[0]
        assert delta.payload["tone"] == "warm"
        assert delta.payload["loc"] == "night market"
        assert delta.payload["time"] == "08:10"  # clock advance persisted (D2)
        assert delta.meta["action_class"] == "dialogue"

        # the extractor sees (user message, full prose)
        assert extractor.calls == [("I greet the merchant", "The night market hums.")]
    finally:
        log.close()


def test_post_turn_appends_env_hud_and_persists_calendar(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        model = FakeModel()
        extractor = FakeExtractor(deltas={"tone": "warm"})
        engine = make_engine(log, model, extractor)

        run(collect(engine.run_turn("s1", "I greet the merchant", "in_character")))

        hud = [
            e
            for e in log.get_events("s1")
            if e.type == "system"
            and isinstance(e.payload, dict)
            and e.payload.get("event") == "env_hud"
        ]
        assert len(hud) == 1
        parsed = parse_env_hud_statement(hud[0].payload["statement"])
        assert parsed is not None
        assert parsed["time"] == "08:10"
        assert parsed["date"] == "Monday, June 02, 2025"

        # The calendar is persisted in the fold (epoch-reset regression guard:
        # without date/day in the state-delta the projection forgets the date
        # every turn and the calendar can never advance past day 1).
        delta = events_of(log, "s1", "state-delta")[0]
        assert delta.payload["date"] == "Monday, June 02, 2025"
        assert delta.payload["day"] == "1"
    finally:
        log.close()


# -- OOC short-circuit -------------------------------------------------------


def test_ooc_short_circuit_does_not_advance_scene(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        model = FakeModel(chunks=("Sure, ", "happy to help."), reasoning=None)
        extractor = FakeExtractor()
        engine = make_engine(log, model, extractor)

        streamed = run(collect(engine.run_turn("s1", "what time is it?", "ooc")))

        assert [e["type"] for e in streamed] == ["content", "content", "done"]
        assert streamed[-1]["meta"]["kind"] == "ooc"

        # scene did not advance: no state-delta, no narrative
        assert events_of(log, "s1", "state-delta") == []
        assert events_of(log, "s1", "narrative") == []
        assert log.get_projection("s1")["state"] == {}

        ooc = events_of(log, "s1", "ooc")
        assert len(ooc) == 1
        assert ooc[0].payload["text"] == "Sure, happy to help."

        # post-turn extractor never ran; simplified prompt marker was used
        assert extractor.calls == []
        assert "out-of-character" in model.calls[0][0]["content"]
    finally:
        log.close()


# -- cancel mid-narrative ----------------------------------------------------


def test_cancel_mid_narrative_leaves_ephemeral_partial(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        model = FakeModel(chunks=("one ", "two ", "three ", "four "), reasoning=None)
        extractor = FakeExtractor()
        engine = make_engine(log, model, extractor)

        async def scenario():
            got = []
            content_seen = 0
            async for event in engine.run_turn("s1", "I speak", "in_character"):
                got.append(event)
                if event["type"] == "content":
                    content_seen += 1
                    if content_seen == 1:
                        engine.cancel()
            return got

        streamed = run(scenario())

        assert streamed[-1] == {"type": "done", "meta": {"cancelled": True}}
        # post_turn never ran: no extractor call, no state-delta
        assert extractor.calls == []
        assert events_of(log, "s1", "state-delta") == []

        narratives = events_of(log, "s1", "narrative")
        assert len(narratives) == 1
        assert narratives[0].ephemeral is True
        assert narratives[0].payload["prose"] == "one "
        assert narratives[0].meta["partial"] is True

        # ephemeral partials stay out of the derived projection
        assert log.get_projection("s1")["narratives"] == []
    finally:
        log.close()


# -- checkpointer (cache) resumes a thread -----------------------------------


def test_checkpointer_resumes_second_turn_on_same_thread(tmp_path):
    log_path = tmp_path / "events.db"

    async def scenario():
        log = EventLog(log_path)
        try:
            model = FakeModel()
            extractor = FakeExtractor(deltas={"tone": "warm"})
            async with open_sqlite_checkpointer(tmp_path / "ckpt.db") as checkpointer:
                graph = build_graph(
                    model=model, extractor=extractor, log=log, checkpointer=checkpointer
                )
                engine = GraphTurnEngine(graph=graph, log=log, model=model)

                await collect(engine.run_turn("s1", "I greet the merchant", "in_character"))
                first_time = log.get_projection("s1")["state"]["time"]

                snapshot = await graph.aget_state({"configurable": {"thread_id": "s1"}})
                assert snapshot.values["session_id"] == "s1"
                assert snapshot.values["action_class"] == "dialogue"

                await collect(engine.run_turn("s1", "I greet again", "in_character"))
                second_time = log.get_projection("s1")["state"]["time"]

                return first_time, second_time, len(events_of(log, "s1", "narrative"))
        finally:
            log.close()

    first_time, second_time, narrative_count = run(scenario())

    assert first_time == "08:10"
    assert second_time == "08:20"  # resumed on the same thread and advanced
    assert narrative_count == 2


# -- Lane A: ordered narrative segments (log boundary) -----------------------


def test_segment_narrative_interleaved_unfenced_keeps_positions():
    raw = (
        "The market hums.\n"
        "card = ArtifactCard(a, b, c)\n"
        "Rain starts.\n"
        "beat = BeatCard(\n  c,\n  d,\n)\n"
        "We leave."
    )

    assert segment_narrative(raw) == [
        {"kind": "prose", "text": "The market hums."},
        {"kind": "dsl", "text": "card = ArtifactCard(a, b, c)"},
        {"kind": "prose", "text": "Rain starts."},
        {"kind": "dsl", "text": "beat = BeatCard(\n  c,\n  d,\n)"},
        {"kind": "prose", "text": "We leave."},
    ]


def test_segment_narrative_fenced_interleaved():
    raw = (
        "Dawn breaks.\n"
        "```openui\n"
        "card = ArtifactCard(a, b, c)\n"
        "meter = MeterBar(b)\n"
        "```\n"
        "By dusk we rest."
    )

    assert segment_narrative(raw) == [
        {"kind": "prose", "text": "Dawn breaks."},
        {"kind": "dsl", "text": "card = ArtifactCard(a, b, c)"},
        {"kind": "dsl", "text": "meter = MeterBar(b)"},
        {"kind": "prose", "text": "By dusk we rest."},
    ]


def test_segment_narrative_prose_only_is_single_segment_no_dsl():
    segments = segment_narrative("Just prose.\n\n\n\nMore prose.")

    assert segments == [{"kind": "prose", "text": "Just prose.\n\nMore prose."}]
    assert all(s["kind"] == "prose" for s in segments)


def test_segment_narrative_dsl_only():
    assert segment_narrative("card = ArtifactCard(a, b, c)\n") == [
        {"kind": "dsl", "text": "card = ArtifactCard(a, b, c)"}
    ]


def test_segment_narrative_empty():
    assert segment_narrative("") == []


def test_narrator_returns_segments_with_legacy_fields_as_concatenations():
    model = ChannelModel(
        [
            {"type": "content", "delta": "The market hums.\n"},
            {
                "type": "content",
                "delta": "card = ArtifactCard(a, b, c)\n",
                "meta": {"channel": "dsl"},
            },
            {"type": "content", "delta": "Rain starts."},
        ]
    )
    node = make_narrator_node(model=model)
    state = {"session_id": "s1", "user_content": "go", "facts": "", "plan": ""}

    out = run(node(state))

    assert out["segments"] == [
        {"kind": "prose", "text": "The market hums."},
        {"kind": "dsl", "text": "card = ArtifactCard(a, b, c)"},
        {"kind": "prose", "text": "Rain starts."},
    ]
    prose_concat = "\n".join(
        s["text"] for s in out["segments"] if s["kind"] == "prose"
    )
    dsl_concat = "\n".join(s["text"] for s in out["segments"] if s["kind"] == "dsl")
    assert out["narrative_parts"] == [prose_concat] == ["The market hums.\nRain starts."]
    assert out["dsl"] == dsl_concat == "card = ArtifactCard(a, b, c)"


def test_narrator_unfenced_interleaved_segments_from_raw_stream():
    # g4-class narrators never emit a fence: the raw stream carries DSL inline.
    model = ChannelModel(
        [
            {
                "type": "content",
                "delta": "The market hums.\ncard = ArtifactCard(a, b, c)\nRain starts.",
            }
        ]
    )
    node = make_narrator_node(model=model)
    state = {"session_id": "s1", "user_content": "go", "facts": "", "plan": ""}

    out = run(node(state))

    assert out["segments"] == [
        {"kind": "prose", "text": "The market hums."},
        {"kind": "dsl", "text": "card = ArtifactCard(a, b, c)"},
        {"kind": "prose", "text": "Rain starts."},
    ]
    assert out["narrative_parts"] == ["The market hums.\nRain starts."]
    assert out["dsl"] == "card = ArtifactCard(a, b, c)"


def test_post_turn_payload_carries_all_three_narrative_fields(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        segments = [
            {"kind": "prose", "text": "The market hums."},
            {"kind": "dsl", "text": "card = ArtifactCard(a, b, c)"},
            {"kind": "prose", "text": "Rain starts."},
        ]
        node = make_post_turn_node(extractor=FakeExtractor(), log=log)
        state = {
            "session_id": "s1",
            "user_content": "I look around",
            "narrative_parts": ["The market hums.\nRain starts."],
            "dsl": "card = ArtifactCard(a, b, c)",
            "segments": segments,
            "facts": "time: 14:35 | loc: night market",
        }

        run(node(state))

        narrative = events_of(log, "s1", "narrative")[0]
        payload = narrative.payload
        assert payload["prose"] == "The market hums.\nRain starts."
        assert payload["dsl"] == "card = ArtifactCard(a, b, c)"
        assert payload["segments"] == segments
    finally:
        log.close()


def test_graph_commits_segments_end_to_end(tmp_path):
    """The real graph path must not drop segments between narrator and post_turn."""
    log = EventLog(tmp_path / "events.db")
    try:
        model = ChannelModel(
            [
                {
                    "type": "content",
                    "delta": "The market hums.\ncard = ArtifactCard(a, b, c)\nRain starts.",
                }
            ]
        )
        engine = make_engine(log, model, FakeExtractor())

        run(collect(engine.run_turn("s1", "I look around", "in_character")))

        narrative = events_of(log, "s1", "narrative")[0]
        assert narrative.payload["segments"] == [
            {"kind": "prose", "text": "The market hums."},
            {"kind": "dsl", "text": "card = ArtifactCard(a, b, c)"},
            {"kind": "prose", "text": "Rain starts."},
        ]
        assert narrative.payload["prose"] == "The market hums.\nRain starts."
        assert narrative.payload["dsl"] == "card = ArtifactCard(a, b, c)"
    finally:
        log.close()


def test_backticked_statements_extract_as_dsl():
    # Live defect: narrator emitted markdown inline-code statements when the
    # library doc showed examples backticked; extraction must tolerate both.
    raw = (
        "Prose line.\n"
        '`card = ArtifactCard("Hectic", "Cafe", "08:10")`\n'
        "More prose.\n"
        '`root = ScenePane("Cafe", [card])`\n'
    )
    segs = segment_narrative(raw)
    assert [s["kind"] for s in segs] == ["prose", "dsl", "prose", "dsl"]
    assert segs[1]["text"].startswith('card = ArtifactCard(')
    assert "`" not in segs[1]["text"] and "`" not in segs[3]["text"]


def test_segment_narrative_splits_unfenced_flavor_statements():
    """Flavor statements must segment like core components (flavor-probe fix).

    The backend regex once hand-listed only the core components, stranding
    every flavor statement in ``prose`` (never validated, pruned from the root
    as dangling_ref). The regex derives from the flavors.json mirror now —
    same source as the validation gate and the frontend segmenter.
    """
    raw = (
        "The rain hammers the stones.\n"
        "mind = Mindscape(\n"
        '  "the storm pressing in",\n'
        "  brief=True,\n"
        ")\n"
        "She waits by the door."
    )
    segments = segment_narrative(raw)
    kinds = [segment["kind"] for segment in segments]
    assert kinds[0] == "prose"
    assert "dsl" in kinds
    dsl = next(segment for segment in segments if segment["kind"] == "dsl")
    assert "Mindscape(" in dsl["text"]
    # flavor statements no longer stranded: prose parts must not carry DSL
    assert all("Mindscape(" not in segment["text"] for segment in segments if segment["kind"] == "prose")


def test_component_stmt_matches_all_flavor_names():
    """Every flavors.json component name matches the segmenter statement regex."""
    from fabulist.dsl.statements import component_stmt_re
    from fabulist.graph.validation import flavor_names

    names = set(flavor_names())
    assert {"Mindscape", "Interface", "Document", "Clash", "Entrance"} <= names
    # The segmenter reads the recogniser at its use-site (W9.27 hot reload);
    # binding it at import froze the first vocabulary.
    pattern = component_stmt_re()
    for name in sorted(names):
        assert pattern.match(f"x = {name}("), name


# -- stage frames on the turn stream (D42) -----------------------------------
#
# One ``stage`` frame per IN-GRAPH processing step the user waits on, emitted at
# its start/done boundaries. ``done`` now means every in-graph step finished;
# the post-turn fire-and-forget hooks run after ``done`` and are out of scope.


def _stage_frames(streamed):
    return [e for e in streamed if e["type"] == "stage"]


def _pairs(streamed):
    return [(f["meta"]["stage"], f["meta"]["state"]) for f in _stage_frames(streamed)]


class FakeWorldState:
    """Async BTS seam: the world-state pass always ticks."""

    async def __call__(self, user_msg, prose, prior_state, checkpoint=False):
        return {"kind": "delta", "state": {}, "deltas": {}}


class FakeAttribution:
    """Async attribution seam: no quotes, still a real step."""

    async def __call__(self, user_msg, prose):
        return []


class FakeCritic:
    """Async critic seam returning a canned fenced-JSON verdict."""

    def __init__(self, reply):
        self.reply = reply
        self.evidence: list[dict] = []

    async def __call__(self, evidence):
        self.evidence.append(evidence)
        return self.reply


def _critic_reply(verdict, **extra):
    payload = {
        "verdict": verdict,
        "reason": extra.pop("reason", "because"),
        "edits": extra.pop("edits", []),
    }
    payload.update(extra)
    return json.dumps(payload)


def test_stage_frames_are_start_done_pairs_in_execution_order(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        model = FakeModel()
        engine = GraphTurnEngine(
            graph=build_graph(
                model=model,
                extractor=FakeExtractor(),
                log=log,
                world_state=FakeWorldState(),
                attribution=FakeAttribution(),
                critic=FakeCritic(_critic_reply("ok")),
                decision=FlaggingGate(),
            ),
            log=log,
            model=model,
        )
        streamed = run(collect(engine.run_turn("s1", "I greet the merchant", "in_character")))

        assert _pairs(streamed) == [
            ("lanes", "start"),
            ("lanes", "done"),
            ("narrate", "start"),
            ("narrate", "done"),
            ("dsl", "start"),
            ("dsl", "done"),
            ("critic", "start"),
            ("critic", "done"),
            ("world_state", "start"),
            ("world_state", "done"),
            ("env_hud", "start"),
            ("env_hud", "done"),
            ("attribution", "start"),
            ("attribution", "done"),
        ]
        # Every frame carries a label + a deriveable percentage slot...
        for frame in _stage_frames(streamed):
            meta = frame["meta"]
            assert meta["label"]
            # W9.20 §5.6: the planner stage took index 1, so the plan is 8 steps.
            assert meta["total"] == 8
            assert 1 <= meta["index"] <= meta["total"]
        # ...and the terminal done is genuinely last.
        assert streamed[-1]["type"] == "done"
    finally:
        log.close()


def test_completed_turn_has_no_dangling_stage_start(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        model = FakeModel()
        engine = GraphTurnEngine(
            graph=build_graph(
                model=model,
                extractor=FakeExtractor(),
                log=log,
                world_state=FakeWorldState(),
                attribution=FakeAttribution(),
            ),
            log=log,
            model=model,
        )
        streamed = run(collect(engine.run_turn("s1", "I greet the merchant", "in_character")))

        pairs = _pairs(streamed)
        started = [stage for stage, state in pairs if state == "start"]
        finished = [stage for stage, state in pairs if state == "done"]
        assert started == finished
        assert started  # the turn did emit stage frames
        # The last frame before done belongs to a closed step.
        assert streamed[-1]["type"] == "done"
        assert pairs[-1][1] == "done"
    finally:
        log.close()


def test_revision_frame_precedes_done_on_a_critic_repair(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        model = ChannelModel(
            [{"type": "content", "delta": "The rain kept falling on the empty street."}]
        )
        critic = FakeCritic(
            _critic_reply(
                "diffs",
                reason="repeats the closing image",
                edits=[{"find": "kept falling", "replace": "still drummed"}],
            )
        )
        engine = GraphTurnEngine(
            graph=build_graph(
                model=model, extractor=FakeExtractor(), log=log,
                critic=critic, decision=FlaggingGate(),
            ),
            log=log,
            model=model,
        )
        streamed = run(collect(engine.run_turn("s1", "I wait under the awning.", "in_character")))

        types = [e["type"] for e in streamed]
        assert types.count("revision") == 1
        assert types.index("revision") < types.index("done")
        # the critic step brackets its call; the revision lands inside it
        assert ("critic", "start") in _pairs(streamed)
        assert ("critic", "done") in _pairs(streamed)
        frame = next(e for e in streamed if e["type"] == "revision")
        assert frame["meta"]["verdict"] == "diffs"
        assert frame["meta"]["segments"] == [
            {"kind": "prose", "text": "The rain still drummed on the empty street."}
        ]
    finally:
        log.close()


def test_fail_verdict_keeps_the_turn_and_never_re_enters_narrate(tmp_path):
    """W9.24 §3.3.7: the critic's ``fail`` no longer re-runs the narrator.

    The retired arm was the most expensive serial path left (draft + critic + up
    to two full narrator re-runs). A ``fail`` now keeps the delivered turn with
    its verdict row: ONE narrator completion, ONE narrative row, no ``regen``
    stage frame, no revision.
    """
    log = EventLog(tmp_path / "events.db")
    try:
        model = ChannelModel([{"type": "content", "delta": "First draft."}])
        critic = FakeCritic(_critic_reply("fail", reason="no movement"))

        engine = GraphTurnEngine(
            graph=build_graph(
                model=model,
                extractor=FakeExtractor(),
                log=log,
                critic=critic,
                decision=FlaggingGate(),
            ),
            log=log,
            model=model,
        )
        streamed = run(collect(engine.run_turn("s1", "I wait.", "in_character")))

        assert len(model.calls) == 1  # no re-run: the draft is the turn
        assert len(critic.evidence) == 1  # judged once, never re-judged
        narratives = [
            event for event in log.get_projection("s1")["narratives"]
        ]
        assert len(narratives) == 1
        assert narratives[0]["payload"]["prose"] == "First draft."
        assert not [e for e in log.get_events("s1") if e.type == "revision"]
        # The retired stage name is gone from the frames and the step trace.
        assert not [
            f for f in _stage_frames(streamed) if f["meta"]["stage"] == "regen"
        ]
        verdict = [
            e
            for e in log.get_events("s1")
            if e.payload.get("event") == "critic_verdict"
        ]
        assert [e.payload["verdict"] for e in verdict] == ["fail"]
        assert streamed[-1]["type"] == "done"
    finally:
        log.close()


def test_stage_frames_carry_an_iso_timestamp_for_the_step_trace(tmp_path):
    """Every stage frame stamps `ts` (ISO-8601 UTC), in emission order.

    The header's step summary orders the trace from these frames alone, so the
    timestamp must be present on EVERY boundary and never run backwards.
    """
    log = EventLog(tmp_path / "events.db")
    try:
        model = FakeModel()
        engine = GraphTurnEngine(
            graph=build_graph(model=model, extractor=FakeExtractor(), log=log),
            log=log,
            model=model,
        )
        streamed = run(collect(engine.run_turn("s1", "I look around.", "in_character")))

        frames = _stage_frames(streamed)
        assert frames
        stamps = [datetime.fromisoformat(frame["meta"]["ts"]) for frame in frames]
        assert stamps == sorted(stamps)
        assert all(frame["meta"]["ts"] for frame in frames)
    finally:
        log.close()


def test_dsl_step_done_frame_carries_validator_notes(tmp_path):
    """A DSL gate finding rides the `dsl` step's `done` frame as a note.

    A kwargs statement is repaired deterministically by the gate (no model
    retry), so the `dsl` step still succeeds — its note records WHY the
    composition was rewritten. A clean step omits the key entirely.
    """
    log = EventLog(tmp_path / "events.db")
    try:
        model = ChannelModel(
            [
                {
                    "type": "content",
                    "delta": (
                        "The lantern glows.\n"
                        'card = ArtifactCard(name="Lantern", kind="Quest item", '
                        'description="A cold glow.")\n'
                    ),
                }
            ]
        )
        engine = GraphTurnEngine(
            graph=build_graph(model=model, extractor=FakeExtractor(), log=log),
            log=log,
            model=model,
        )
        streamed = run(collect(engine.run_turn("s1", "I lift the lantern.", "in_character")))

        dsl_done = next(
            frame
            for frame in _stage_frames(streamed)
            if frame["meta"]["stage"] == "dsl" and frame["meta"]["state"] == "done"
        )
        notes = dsl_done["meta"]["notes"]
        assert notes and any("named_args" in note for note in notes)

        narrate_done = next(
            frame
            for frame in _stage_frames(streamed)
            if frame["meta"]["stage"] == "narrate" and frame["meta"]["state"] == "done"
        )
        assert "notes" not in narrate_done["meta"]
    finally:
        log.close()


def test_validator_notes_projects_findings_and_drops_duplicates():
    """`_validator_notes` reads the inspector's flag shape into one-liners."""
    from fabulist.graph.nodes import _validator_notes

    assert _validator_notes(None) == []
    assert _validator_notes([]) == []
    assert _validator_notes([{"rule_id": "named_args", "message": "converted X"}]) == [
        "named_args: converted X"
    ]
    assert _validator_notes(
        [
            {"rule_id": "named_args", "message": "converted X"},
            {"rule_id": "named_args", "message": "converted X"},
            {"rule_id": "empty_after_prune"},
        ]
    ) == ["named_args: converted X", "empty_after_prune"]


def test_segment_narrative_splits_unassigned_component_statements():
    """Bare calls are DSL: the parser shim in DslFallback supplies the id.

    Owner report 2026-10-17: mid-stream a `DialogueSpotlight(…)` with no
    assignment stayed inside the prose run and never mounted as a panel.
    """
    raw = (
        "The tapping stops.\n"
        'DialogueSpotlight("Hobbs", "Tied up?", "The words are slow.")\n'
        "He does not pull back.\n"
        'ObjectCard("Tarnished Silver Teapot", "Antique", "A heavy piece.")'
    )

    assert segment_narrative(raw) == [
        {"kind": "prose", "text": "The tapping stops."},
        {"kind": "dsl", "text": 'DialogueSpotlight("Hobbs", "Tied up?", "The words are slow.")'},
        {"kind": "prose", "text": "He does not pull back."},
        {"kind": "dsl", "text": 'ObjectCard("Tarnished Silver Teapot", "Antique", "A heavy piece.")'},
    ]


class FlaggingGate:
    """Decision seam that always fires the bank's first question (W9.19 B).

    The critic is conditional now: it runs only when the validator or the
    decision gate produced a flag. Tests that exercise the critic path need a
    real flag from a real seam, not a state hack.
    """

    enabled = True

    def __init__(self):
        self.ask_calls: list[tuple[dict, list[dict]]] = []

    async def ask(self, state, questions):
        self.ask_calls.append((state, questions))
        # ``questions`` is the payload map {id: row}; the wire shape back is
        # {question id: {"noul": P(true)}}. Answer EVERY question in its own
        # defect polarity so at least one flag fires.
        # The payload map's KEY is the question id (the rows carry only
        # type/instructions); answer every noul row with P(true)=1.0, which
        # fires every true_is_defect question in the bank.
        items = questions.items() if isinstance(questions, dict) else ()
        return {qid: {"noul": 1.0} for qid, row in items
                if isinstance(row, dict) and row.get("type") == "noul"}
