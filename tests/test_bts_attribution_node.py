"""Post-turn integration for BTS world-state.

Covers the D21/B4 absorption (BTS owns state-delta; extractor retired when the
world-state seam is present; clock still ticks), the checkpoint trigger wiring,
and world-state failure degradation. The W6-B leading PLAN-block strip and the
plan-persistence rows this file used to pin are gone (W9.20 §9: the stripper
never fired and the planner owns plan events now).
"""

from __future__ import annotations

import asyncio

from fabulist.graph.nodes import make_post_turn_node
from fabulist.state.bts import BtsResult


def run(coro):
    return asyncio.run(coro)


# --- BTS world-state node ---------------------------------------------------


class RecordingExtractor:
    def __init__(self):
        self.calls = []

    async def __call__(self, user_msg, prose):
        self.calls.append((user_msg, prose))
        return {"deltas": {"loc": "SHOULD NOT APPEAR"}, "action_class": "other"}


class FakeWorldState:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def __call__(self, user_msg, narrative, prior_state=None, *, checkpoint=False):
        self.calls.append(
            {"user": user_msg, "narrative": narrative, "prior": prior_state, "checkpoint": checkpoint}
        )
        if self.error is not None:
            raise self.error
        return self.result


def _state(**overrides):
    state = {
        "session_id": "s1",
        "user_content": "I speak",
        "narrative_parts": ["Elara moves to the gallery."],
        "dsl": "",
        "segments": [{"kind": "prose", "text": "Elara moves to the gallery."}],
        "facts": "time: 09:15",
        "action_class": "dialogue",
    }
    state.update(overrides)
    return state


def test_world_state_owns_state_delta_and_retires_extractor(log):
    extractor = RecordingExtractor()
    wsf = FakeWorldState(
        BtsResult(
            kind="delta",
            state={"Elara": {"physical": {"hp": "78→62"}}},
            deltas={"loc": "gallery_wing", "weather": "light rain", "tone": "tense"},
            venue={"Elara": {"venue": "gallery_wing"}},
            threads=[{"status": "new", "name": "seal", "detail": ""}],
            tension={"level": 6},
        )
    )
    node = make_post_turn_node(extractor=extractor, log=log, world_state=wsf)
    run(node(_state()))

    # The extractor's delta emission is retired — never called.
    assert extractor.calls == []

    # FLIPPED (W9.29 §6/W4): the compat ``world_state`` render is retired, so the
    # payload keys this test used to read are gone. What the turn records is the
    # per-turn ``entity_summary`` row (counts) — the payload's state/venue now live
    # in the registry, which this fake tracker never wrote.
    assert [e for e in log.get_events("s1") if e.payload.get("event") == "world_state"] == []
    summaries = [e for e in log.get_events("s1") if e.payload.get("event") == "entity_summary"]
    assert len(summaries) == 1
    assert summaries[0].payload["seq"] >= 1

    deltas = [e for e in log.get_events("s1") if e.type == "state-delta"]
    assert len(deltas) == 1
    assert deltas[0].actor == "bts"
    payload = deltas[0].payload
    assert payload["loc"] == "gallery_wing"
    assert payload["weather"] == "light rain"
    assert payload["tone"] == "tense"
    # The clock still ticks (time is authoritative, D3).
    assert payload["time"] == "09:15"
    # Downstream consumers read the same folded keys.
    assert log.get_projection("s1")["state"]["loc"] == "gallery_wing"


def test_world_state_failure_degrades_and_keeps_clock(log):
    # Seed a prior good world-state + folded loc.
    log.append_event("s1", "state-delta", "bts", {"loc": "courtyard", "time": "09:00"})
    wsf = FakeWorldState(error=ValueError("bad block after repair"))
    node = make_post_turn_node(extractor=RecordingExtractor(), log=log, world_state=wsf)
    run(node(_state()))

    failures = [
        e for e in log.get_events("s1") if e.payload.get("event") == "world_state_failed"
    ]
    assert len(failures) == 1
    # W9.29 §6: the tracker's failure stage is the registry loop (a legacy
    # notation parse failure would say "parse", a Tier D connection "tier_d").
    assert failures[0].payload["stage"] == "registry"
    # No world update this turn: last good loc survives; only the clock moves on.
    state = log.get_projection("s1")["state"]
    assert state["loc"] == "courtyard"
    assert state["time"] == "09:15"


def test_the_delta_checkpoint_cadence_is_retired(log):
    """W9.29 §6: the registry is current by construction, so nothing checkpoints.

    The three old triggers (an explicit ``[OOC: BTS checkpoint]`` ask, a
    travel-classified scene change, ten turns without one) all fed a whole-payload
    rewrite that no longer exists. The runner is handed ``checkpoint=False`` every
    turn; the cadence question's successor is the reconciliation pass (§12).
    """
    _ = BtsResult(kind="checkpoint", state={}, deltas={})
    for content, action_class in (
        ("[OOC: BTS checkpoint]", "dialogue"),
        ("I walk out", "travel"),
        ("I wait", "dialogue"),
    ):
        wsf = FakeWorldState(BtsResult(kind="delta", state={}, deltas={}))
        node = make_post_turn_node(
            extractor=RecordingExtractor(), log=log, world_state=wsf
        )
        run(node(_state(user_content=content, action_class=action_class)))
        assert wsf.calls[-1]["checkpoint"] is False


def test_the_prior_world_state_block_is_not_pushed_any_more(log):
    """§7: the entity palette replaced the pushed ``prior_state`` block.

    The registry render rides the runner's own tail (so it cannot go stale or be
    dropped by a preset flow); the node pushes nothing.
    """
    log.append_event(
        "s1",
        "system",
        "bts",
        {"event": "world_state", "kind": "delta", "state": {"Elara": {"physical": {"hp": "90"}}}},
    )
    wsf = FakeWorldState(BtsResult(kind="delta", state={}, deltas={}))
    node = make_post_turn_node(extractor=RecordingExtractor(), log=log, world_state=wsf)
    run(node(_state()))
    assert wsf.calls[-1]["prior"] is None


# -- attribution stage (markup: no model caller) -----------------------------
#
# Speaker attribution sits on the same post-turn path as BTS. It used to run an
# extractor-class model call every turn, then a deterministic prose detector;
# since W9.28 the narrator wraps each spoken line in the speaking character's
# palette colour and the engine parses it. These tests pin the NODE contract
# (event shape, first-sighting colours, failure row) — not any request shape.

#: Aria is palette slot 0, Borin slot 1 (the hues ``palette_for_turn`` mints for
#: a two-member card cast on a fresh session).
ARIA_HEX = "#e06c75"
BORIN_HEX = "#e5c07b"

ATTRIBUTION_PROSE = (
    f'<span style="color:{ARIA_HEX}">"Well met."</span> '
    f'<span style="color:{BORIN_HEX}">"Aye."</span>'
)


def _attribution_state(**overrides):
    state = {
        "session_id": "s1",
        "user_content": "I speak",
        "narrative_parts": [ATTRIBUTION_PROSE],
        "dsl": "",
        "segments": [{"kind": "prose", "text": ATTRIBUTION_PROSE}],
        "facts": "time: 09:15",
        "action_class": "dialogue",
    }
    state.update(overrides)
    return state


def _palette(log):
    """The turn's palette through the real seam (presence ladder + registry)."""
    from fabulist.state.attribution import palette_for_turn

    return palette_for_turn(log, "s1")


def _present(*names):
    """A BTS delta putting ``names`` in the scene (the presence rung's evidence)."""
    return FakeWorldState(
        BtsResult(
            kind="delta",
            state={},
            deltas={},
            venue={name: {"venue": "tavern", "area": "bar"} for name in names},
        )
    )


def test_attribution_node_attributes_from_the_colour_markup_without_a_model_call(log):
    from fabulist.api.cast_api import list_speaker_colors
    from fabulist.state.attribution import MarkupAttributionRunner
    from fabulist.state.cast import admit_member

    admit_member(log, "s1", "Aria", provenance="card", admitted_by="system")
    admit_member(log, "s1", "Borin", provenance="card", admitted_by="system")
    node = make_post_turn_node(
        extractor=RecordingExtractor(),
        log=log,
        world_state=_present("Aria", "Borin"),
        attribution=MarkupAttributionRunner(lambda: _palette(log)),
    )
    run(node(_attribution_state()))

    rows = [
        e.payload["attributions"]
        for e in log.get_events("s1")
        if e.payload.get("event") == "attribution"
    ]
    assert len(rows) == 1
    assert [(row["speaker"], row["quote"]) for row in rows[0]] == [
        ("Aria", "Well met."),
        ("Borin", "Aye."),
    ]
    # The row carries the roster's own member ids (nothing re-admitted), and the
    # first sighting assigned both colours.
    assert all(row["speaker_id"].startswith("cm_") for row in rows[0])
    colours = list_speaker_colors(log, "s1")
    assert "Aria" in colours and "Borin" in colours
    # No capture/usage row: the pass made no model call at all.
    assert not [
        e for e in log.get_events("s1") if e.payload.get("event") == "agent_call"
    ]


def test_attribution_stage_frame_carries_its_user_facing_label(log):
    """The step bar shows a name that says what the step does.

    The header's in-progress step bar renders ``meta.label`` verbatim from the
    emitted ``stage`` frame, so the label IS user-facing copy — not derived from
    the wire-key ``stage`` name. Pinned here because ``attribution`` alone is
    opaque: it reads the narrator's colour markup back into speakers, with no
    model call.
    """
    import fabulist.graph.nodes as nodes

    async def no_rows(user_msg, prose):  # noqa: ARG001 - the runner's signature
        return []

    frames: list[dict] = []
    original = nodes._stream_writer
    nodes._stream_writer = lambda: frames.append
    try:
        node = nodes.make_attribution_node(log=log, attribution=no_rows)
        run(node(_attribution_state()))
    finally:
        nodes._stream_writer = original

    assert [(f["meta"]["stage"], f["meta"]["state"]) for f in frames] == [
        ("attribution", "start"),
        ("attribution", "done"),
    ]
    assert {f["meta"]["label"] for f in frames} == {"Attributing speakers"}
    assert frames[0]["meta"]["index"] == 8
    assert frames[0]["meta"]["total"] == 8


def test_attribution_node_persists_an_empty_row_for_untagged_prose(log):
    """Strict omission: the event still lands, with no attributions."""
    from fabulist.state.attribution import MarkupAttributionRunner
    from fabulist.state.cast import admit_member

    admit_member(log, "s1", "Aria", provenance="card", admitted_by="system")
    node = make_post_turn_node(
        extractor=RecordingExtractor(),
        log=log,
        world_state=_present("Aria"),
        attribution=MarkupAttributionRunner(lambda: _palette(log)),
    )
    prose = 'Aria said, "Well met."'
    run(
        node(
            _attribution_state(
                narrative_parts=[prose],
                segments=[{"kind": "prose", "text": prose}],
            )
        )
    )

    rows = [
        e.payload["attributions"]
        for e in log.get_events("s1")
        if e.payload.get("event") == "attribution"
    ]
    assert rows == [[]]


def test_attribution_failure_is_logged_and_the_turn_survives(log):
    from fabulist.state.attribution import MarkupAttributionRunner

    def boom():
        raise RuntimeError("palette unreadable")

    node = make_post_turn_node(
        extractor=RecordingExtractor(),
        log=log,
        world_state=FakeWorldState(BtsResult(kind="delta", state={}, deltas={})),
        attribution=MarkupAttributionRunner(boom),
    )
    run(node(_attribution_state()))  # must not raise

    failures = [
        e
        for e in log.get_events("s1")
        if e.payload.get("event") == "attribution_failed"
    ]
    assert len(failures) == 1
    assert "palette unreadable" in failures[0].payload["error"]
    # The BTS half of the tail still landed (best-effort, B5): the per-turn
    # ``entity_summary`` row plus the flat ``state-delta``. FLIPPED (W9.29 §6/W4):
    # the compat ``world_state`` payload row it used to check is retired.
    assert [
        e for e in log.get_events("s1") if e.payload.get("event") == "entity_summary"
    ]
    assert [e for e in log.get_events("s1") if e.type == "state-delta"]


def test_build_engine_wires_the_markup_attribution_seam(monkeypatch, tmp_path):
    """Wiring-level pin: ``build_engine`` hands the graph the markup seam.

    ``build_graph`` is intercepted only to reach the constructed seam without
    running a whole graph. The seam is ``MarkupAttributionRunner`` over the
    CURRENT session's ``palette_for_turn`` — the SAME palette the narrator's
    tail block renders, so the prose the model was told to colour is the prose
    the engine parses back.
    """
    import fabulist.wiring as wiring
    from fabulist.agents.debug import CURRENT_SESSION
    from fabulist.connections.registry import Registry
    from fabulist.log import EventLog
    from fabulist.state.attribution import MarkupAttributionRunner
    from fabulist.state.cast import admit_member

    captured: dict = {}
    monkeypatch.setattr(
        wiring, "build_graph", lambda **kwargs: captured.update(kwargs) or None
    )
    log = EventLog(tmp_path / "events.db")
    try:
        wiring.build_engine(
            registry=Registry.from_dict(
                {
                    "connections": {
                        "c": {"base_url": "http://localhost:4444/v1", "model": "your-model-id"}
                    },
                    "agents": {"narrator": "c"},
                }
            ),
            log=log,
            bts_enabled=False,
        )
        attribution = captured["attribution"]
        assert isinstance(attribution, MarkupAttributionRunner)

        # The seam the engine got produces rows from the palette it renders.
        admit_member(log, "s1", "Aria", provenance="card", admitted_by="system")
        admit_member(log, "s1", "Borin", provenance="card", admitted_by="system")
        token = CURRENT_SESSION.set("s1")
        try:
            rows = run(attribution("I speak", ATTRIBUTION_PROSE))
        finally:
            CURRENT_SESSION.reset(token)
        assert [(row["speaker"], row["quote"]) for row in rows] == [
            ("Aria", "Well met."),
            ("Borin", "Aye."),
        ]
        assert all(row["speaker_id"].startswith("cm_") for row in rows)

        # No session scope -> no palette -> nothing invented (strict omission).
        assert run(attribution("I speak", ATTRIBUTION_PROSE)) == []
    finally:
        log.close()


def test_build_engine_skips_attribution_when_disabled(monkeypatch, tmp_path):
    import fabulist.wiring as wiring
    from fabulist.connections.registry import Registry
    from fabulist.log import EventLog

    captured: dict = {}
    monkeypatch.setattr(
        wiring, "build_graph", lambda **kwargs: captured.update(kwargs) or None
    )
    log = EventLog(tmp_path / "events.db")
    try:
        wiring.build_engine(
            registry=Registry.from_dict(
                {
                    "connections": {
                        "c": {"base_url": "http://localhost:4444/v1", "model": "your-model-id"}
                    },
                    "agents": {"narrator": "c"},
                }
            ),
            log=log,
            bts_enabled=False,
            attribution_enabled=False,
        )
        assert captured["attribution"] is None
    finally:
        log.close()
