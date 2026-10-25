"""W9.32 §6 (W11) — ``rendered_state``: the ONE read shim for the flat facts.

The unit this file pins is atomic by design: the shim lands, every reader
re-points, and the two shadow writes (the BTS delta's ``flat_render`` merge and
``_enrich_genre``'s flat ``genre`` key) stop in the same wave. So the pins are

* composition — the projection fold (clock keys, the D45 ``forecast`` pin, any
  legacy key) overlaid by ``flat_render(registry)`` (registry-first, legacy
  beneath), plus the derived ``genre`` and one derived ``conds`` line;
* the registry winning per shadow key over a stale flat value;
* a pre-W9.32-shaped log (legacy ``world_state`` rows only) still rendering;
* ``conds`` carrying ACTIVE conditions only, in the scene → in-scene → rest
  order, and being absent when there is nothing to say (the facts line rides
  the clock fold turn to turn, so a stale line must be cleared, not inherited);
* purity — the shim folds, it never appends;
* the prompt's own facts block (``nodes.facts_from_log``) carrying the derived
  keys, which is what "no reader starves" means from the narrator's side.

Everything runs against a real (tmp-path) SQLite log seeded through the store's
own writers, the pattern ``tests/test_entities.py`` uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.state.cast import admit_member, member_by_name, roster
from fabulist.state.entities import (
    CONDS_LINE_MAX,
    PLAYER,
    SCENE,
    establish_item,
    establish_location,
    move_being,
    registry,
    rendered_state,
    set_attribute,
    set_condition,
    retire_object,
)

SID = "chat"
MONDAY = "Monday, June 02, 2025"


@pytest.fixture
def build_log(tmp_path: Path):
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


def _delta(log: EventLog, payload: dict, session_id: str = SID) -> None:
    """One flat ``state-delta`` row, exactly the shape the writers append."""
    log.append_event(session_id, "state-delta", "extractor", payload)


def _legacy_world_state(log: EventLog, session_id: str = SID) -> None:
    """A pre-W9.32 ``world_state`` row: the legacy lane the shim folds beneath."""
    log.append_event(
        session_id,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {
                "player": {"inventory": {"carrying": "+rope"}},
                "Yara": {"emotional": {"mood": "wary"}},
            },
            "venue": {"player": {"venue": "night market", "area": "docks"}},
            "threads": [],
            "off_screen": [],
            "tension": {"level": 4},
        },
    )


def _yara(log: EventLog, session_id: str = SID) -> str:
    admit_member(
        log, session_id, "Yara", provenance="card", admitted_by="user"
    )
    return member_by_name(roster(log, session_id), "Yara").member_id


def _registry_scene(log: EventLog, session_id: str = SID) -> tuple[str, str, str]:
    """Player + Yara in ``upstairs hall > bathroom``; returns both loc ids and Yara."""
    yara = _yara(log, session_id)
    hall = establish_location(log, session_id, "upstairs hall")
    bath = establish_location(log, session_id, "bathroom", parent_id=hall)
    move_being(log, session_id, PLAYER, bath)
    move_being(log, session_id, yara, bath)
    return hall, bath, yara


def _clock(log: EventLog, time: str = "21:00", session_id: str = SID) -> None:
    _delta(log, {"time": time, "date": MONDAY, "day": "1"}, session_id)


# -- composition and precedence ---------------------------------------------


def test_the_registry_wins_every_shadow_key_over_a_stale_flat_value(build_log):
    """loc/weather/tone/inv are DERIVED: a stale flat copy can never win."""
    log = build_log()
    _registry_scene(log)
    establish_item(log, SID, "brass key", holder=PLAYER)
    set_attribute(log, SID, SCENE, key="weather", value="rain")
    set_attribute(log, SID, SCENE, key="tone", value="intimate")
    # The pre-W11 shadow writes, still sitting in the projection fold.
    _delta(
        log,
        {"loc": "cellar", "weather": "hail", "tone": "icy", "inv": "-rope"},
    )

    state = rendered_state(log, SID)

    assert state["loc"] == "upstairs hall > bathroom"
    assert state["weather"] == "rain"
    assert state["tone"] == "intimate"
    assert state["inv"] == "brass key"


def test_the_shim_carries_the_clock_keys_and_the_forecast_pin(build_log):
    """The clock keys and the D45 pin are storage the registry knows nothing about."""
    log = build_log()
    set_attribute(log, SID, SCENE, key="forecast", value="clear by dawn")
    _delta(log, {"time": "21:15", "date": MONDAY, "day": "2", "forecast": "storm"})

    state = rendered_state(log, SID)

    assert state["time"] == "21:15"
    assert state["date"] == MONDAY
    assert state["day"] == "2"
    # R2: the flat key is the owner's PIN; the model's trait never clobbers it.
    assert state["forecast"] == "storm"


def test_the_derived_genre_prefers_the_scene_trait_over_the_stale_key(build_log):
    """R8's home is the scene trait; the flat key is a read of it (or a legacy log)."""
    log = build_log()
    _delta(log, {"genre": "urban fantasy"})
    assert rendered_state(log, SID)["genre"] == "urban fantasy"

    set_attribute(log, SID, SCENE, key="genre", value="cosmic horror")
    assert rendered_state(log, SID)["genre"] == "cosmic horror"


def test_the_shim_renders_a_pre_w932_log_exactly_as_before(build_log):
    """No registry at all: the legacy ``world_state`` fold is the whole answer."""
    log = build_log()
    _legacy_world_state(log)
    _delta(log, {"time": "08:00", "date": MONDAY, "day": "1"})
    _delta(log, {"weather": "light rain"})

    state = rendered_state(log, SID)

    # The legacy venue is the loc, the legacy carrying tokens the inventory —
    # byte-identical to the fold the pre-W11 ``facts_from_log`` rendered.
    assert state["loc"] == "night market"
    assert state["weather"] == "light rain"
    assert state["inv"] == "+rope"
    assert state["time"] == "08:00"
    assert "conds" not in state


def test_extra_facts_overlay_the_fold_but_never_outrank_the_registry(build_log):
    """pre_turn's advanced clock rides through; a stale shadow value does not."""
    log = build_log()
    _registry_scene(log)
    _delta(log, {"time": "09:15", "date": MONDAY, "day": "1", "loc": "cellar"})

    state = rendered_state(
        log, SID, extra_facts={"time": "09:40", "loc": "cellar", "day": "2"}
    )

    assert (state["time"], state["day"]) == ("09:40", "2")
    assert state["loc"] == "upstairs hall > bathroom"


# -- the derived ``conds`` line (§3.6) --------------------------------------


def test_conds_lists_the_active_conditions_scene_and_in_scene_first(build_log):
    log = build_log()
    _, _, yara = _registry_scene(log)
    admit_member(log, SID, "Seraphine", provenance="card", admitted_by="user")
    seraphine = member_by_name(roster(log, SID), "Seraphine").member_id
    # Seraphine is off screen; her condition is established FIRST on purpose.
    set_condition(log, SID, seraphine, "exhausted")
    set_condition(log, SID, SCENE, "foggy", expires_at="2025-06-02 23:00")
    set_condition(log, SID, yara, "suspicious", expires_at="2025-06-02 21:30")
    set_condition(log, SID, PLAYER, "bleeding")
    _clock(log, "21:00")

    line = rendered_state(log, SID)["conds"]

    # Scene first, then the beings in the room in establish order, then the
    # off-screen one — a clipped line keeps what the scene is visibly doing.
    assert line == (
        "foggy (scene, until 23:00) · suspicious (Yara, until 21:30)"
        " · bleeding (Player) · exhausted (Seraphine)"
    )


def test_conds_skips_a_condition_the_clock_has_passed(build_log):
    log = build_log()
    _, _, yara = _registry_scene(log)
    set_condition(log, SID, yara, "suspicious", expires_at="2025-06-02 20:00")
    set_condition(log, SID, PLAYER, "bleeding")
    _clock(log, "21:00")

    line = rendered_state(log, SID)["conds"]

    assert line == "bleeding (Player)"


def test_conds_is_absent_when_there_is_nothing_to_say(build_log):
    """An empty derived line is ABSENT, and a stale one is cleared, not inherited."""
    log = build_log()
    _, _, yara = _registry_scene(log)
    _clock(log)
    condition = set_condition(log, SID, yara, "suspicious")
    assert "conds" in rendered_state(log, SID)

    retire_object(log, SID, condition.id)
    # The facts line rides the clock fold turn to turn, so the previous turn's
    # line arrives as a ``conds`` fact — it must not outlive the condition.
    assert "conds" not in rendered_state(
        log, SID, extra_facts={"conds": "suspicious (Yara)"}
    )


def test_conds_stays_one_bounded_line(build_log):
    log = build_log()
    _, _, yara = _registry_scene(log)
    for index in range(12):
        set_condition(log, SID, yara, f"wound-{index}-of-a-very-long-name")
    _clock(log)

    line = rendered_state(log, SID)["conds"]

    assert len(line) <= CONDS_LINE_MAX
    assert line.endswith("…")  # clipped whole entries, never mid-word
    assert "\n" not in line


# -- purity and the prompt's own facts block ---------------------------------


def test_the_shim_appends_nothing(build_log):
    log = build_log()
    _registry_scene(log)
    set_condition(log, SID, PLAYER, "bleeding")
    _clock(log)
    before = [(event.id, event.type) for event in log.get_chain_events(SID)]

    rendered_state(log, SID, extra_facts={"time": "10:00"})

    assert [(event.id, event.type) for event in log.get_chain_events(SID)] == before


def test_the_prompt_facts_line_carries_the_derived_keys(build_log):
    """``nodes.facts_from_log`` is the shim's reader: derived values reach the prompt."""
    from fabulist.graph.clock import parse_facts
    from fabulist.graph.nodes import facts_from_log

    log = build_log()
    _, _, yara = _registry_scene(log)
    set_attribute(log, SID, SCENE, key="tone", value="tense")
    set_condition(log, SID, yara, "suspicious", expires_at="2025-06-02 21:30")
    _clock(log, "21:00")

    facts = parse_facts(facts_from_log(log, SID))

    assert facts["time"] == "21:00"
    assert facts["loc"] == "upstairs hall > bathroom"
    assert facts["tone"] == "tense"
    assert facts["conds"] == "suspicious (Yara, until 21:30)"


def test_the_projection_is_never_written_by_the_shim(build_log):
    """The fold the shim reads is untouched — it renders, it does not fold in."""
    log = build_log()
    _registry_scene(log)
    _delta(log, {"loc": "cellar"})
    fold = dict(log.get_projection(SID)["state"])
    assignments = dict(registry(log, SID).assignments)

    rendered_state(log, SID)

    assert log.get_projection(SID)["state"] == fold
    assert registry(log, SID).assignments == assignments
