"""W9.29 §7 — the entity palette (the registry render in the BTS prompt tail).

The block's shape is pinned here: the directive line, one entry per object
grouped by kind and id-first, the assignment line, the footer counts, the
per-entry 120-char clip, the absence bucket's permanence (req. 2), the
establish-order stability (cache contract) and the name-only LEGACY rows the
forward-only shim renders (§11.1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.prompts.entity_palette import (
    ENTITY_PALETTE_CLIP_CHARS,
    ENTITY_PALETTE_DIRECTIVE,
    render_entity_palette,
)
from fabulist.state.cast import admit_member, roster
from fabulist.state.entities import (
    OFF_SCREEN_LOCATION_ID,
    PLAYER,
    Registry,
    establish_economy,
    establish_item,
    establish_location,
    establish_relationship,
    establish_thread,
    expire_conditions,
    move_being,
    registry,
    set_attribute,
    set_body,
    set_condition,
)

SERAPHINE = "cm_seraphine"
YARA = "cm_yara"


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


def _scene(log: EventLog, sid: str = "chat") -> dict[str, str]:
    """The plan §7 worked example's registry (locations, moves, meters, thread, item)."""
    hall = establish_location(log, sid, "upstairs hall")
    bath = establish_location(log, sid, "bathroom", parent_id=hall)
    thread = establish_thread(
        log, sid, "the slow courtship", detail="Seraphine tests the newcomer's boundaries"
    )
    establish_relationship(
        log, sid, SERAPHINE, PLAYER, note="she has decided the newcomer is a project"
    )
    meter, _ = set_attribute(log, sid, SERAPHINE, key="hostility", value=42, subject=PLAYER)
    set_attribute(log, sid, YARA, key="stress", value=55)
    set_attribute(log, sid, YARA, key="mood", value="wary")
    set_attribute(log, sid, "scene", key="weather", value="rain")
    item = establish_item(log, sid, "brass key", holder=PLAYER)
    move_being(log, sid, PLAYER, hall)
    move_being(log, sid, YARA, bath)
    move_being(log, sid, SERAPHINE, "off_screen", at=bath)
    return {"hall": hall, "bath": bath, "thread": thread, "meter": meter, "item": item}


def test_the_palette_renders_the_plan_section_order_and_ids(build_log):
    log = build_log()
    ids = _scene(log)

    text = render_entity_palette(registry(log, "chat"), actions=3)
    lines = text.splitlines()

    assert lines[0] == ENTITY_PALETTE_DIRECTIVE
    # W9.31 follow-up: LOCATIONS is a BLOCK — one place per line, tree order, so
    # a place that already exists under a parent cannot hide in a run-on line.
    assert lines[1] == "LOCATIONS:"
    locations_block = lines[2 : lines.index(next(l for l in lines if l.startswith("  > WHO IS WHERE")))]
    assert locations_block[0].startswith("- loc_off_screen off-screen (at: ")
    assert f"loc_off_screen off-screen (at: {ids['bath']})" in locations_block[0]
    assert any(line.strip() == f"- {ids['hall']} upstairs hall" for line in locations_block)
    where = next(l for l in lines if l.startswith("  > WHO IS WHERE"))
    # The player is the USER's persona, never a cast member: rendered as ``Player``
    # (with the persona's name when the caller hands one in — see the W9.32 pin).
    assert where == (
        f"  > WHO IS WHERE — Player: {ids['hall']} · {YARA}: {ids['bath']} · "
        f"{SERAPHINE}: off_screen"
    )
    after = lines[lines.index(where) + 1 :]
    assert after[0].startswith(f"METERS: {ids['meter']} hostility {SERAPHINE}→player = 42")
    assert f"{YARA} = 55" in after[0]
    assert after[1].startswith("TRAITS: ")
    assert f"mood {YARA} = wary" in after[1]
    assert "weather scene = rain" in after[1]
    assert after[2].startswith("RELATIONSHIPS: rel_")
    assert "she has decided the newcomer is a project" in after[2]
    assert after[3] == (
        f"THREADS: {ids['thread']} [active] the slow courtship — "
        "Seraphine tests the newcomer's boundaries"
    )
    assert after[4] == f"ITEMS: {ids['item']} brass key (player)"
    assert after[5] == "(10 objects · 3 actions last turn)"


def test_an_empty_registry_still_offers_the_off_screen_destination(build_log):
    """Req. 2: the absence bucket is always a destination the model may use."""
    log = build_log()

    text = render_entity_palette(registry(log, "chat"))

    lines = text.splitlines()
    assert lines[0] == ENTITY_PALETTE_DIRECTIVE
    assert lines[1] == "LOCATIONS:"
    assert lines[2].startswith("- loc_off_screen off-screen")
    assert lines[-1] == "(1 object · 0 actions last turn)"
    # No empty sections: a label pointing at nothing teaches nothing.
    assert not any(
        line.startswith(
            (
                "METERS:",
                "TRAITS:",
                "RELATIONSHIPS:",
                "THREADS:",
                "ITEMS:",
                "CONDITIONS:",
                "BODIES:",
                "ECONOMY:",
            )
        )
        for line in lines
    )
    assert not any(line.startswith("  > WHO IS WHERE") for line in lines)


def test_a_bare_registry_object_renders_with_the_directive(build_log):
    text = render_entity_palette(Registry())

    assert text.splitlines()[0] == ENTITY_PALETTE_DIRECTIVE
    assert "loc_off_screen" in text.splitlines()[1]


def test_the_assignment_line_follows_the_roster_order(build_log):
    log = build_log()
    _scene(log)
    admit_member(log, "chat", "Yara", provenance="card", admitted_by="user")
    admit_member(log, "chat", "Seraphine", provenance="card", admitted_by="user")
    members = roster(log, "chat")

    text = render_entity_palette(registry(log, "chat"), members)

    where = next(line for line in text.splitlines() if line.startswith("  > WHO IS WHERE"))
    assert where.index(YARA) < where.index(SERAPHINE)


def test_notes_are_clipped_per_entry_not_per_section(build_log):
    log = build_log()
    long_note = "x" * 400
    first = establish_location(log, "chat", "bathroom", note=long_note)
    second = establish_location(log, "chat", "kitchen", note=long_note)

    text = render_entity_palette(registry(log, "chat"))
    lines = text.splitlines()
    start = lines.index("LOCATIONS:")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith((" ", "- ")):
            break
        block.append(line)
    entries = [line.split("- ", 1)[1] for line in block if "- " in line]
    assert all(len(entry) <= ENTITY_PALETTE_CLIP_CHARS for entry in entries)
    # The two noted rooms are the clipped ones (the bucket has no note).
    assert sum(entry.endswith("…") for entry in entries) == 2
    # A long note never crowds another object's id out of view (req. 11: no cap).
    assert first in text and second in text


def test_retired_objects_never_render(build_log):
    from fabulist.state.entities import retire_object

    log = build_log()
    spare = establish_location(log, "chat", "spare room")
    establish_location(log, "chat", "bathroom")
    retire_object(log, "chat", spare)

    text = render_entity_palette(registry(log, "chat"))

    assert spare not in text
    assert "(2 objects · 0 actions last turn)" in text


def test_the_order_is_establish_order_turn_to_turn(build_log):
    """Cache contract (AGENTS 6): the same registry renders byte-identically."""
    log = build_log()
    _scene(log)

    first = render_entity_palette(registry(log, "chat"), actions=1)
    second = render_entity_palette(registry(log, "chat"), actions=1)

    assert first == second
    # And a NEW object appends at the end of its section, never re-sorts it.
    establish_location(log, "chat", "attic")
    later = render_entity_palette(registry(log, "chat"), actions=1)
    block = "\n".join(
        line
        for line in later.splitlines()
        if line.startswith(("- ", "  - ")) or line == "LOCATIONS:"
    )
    assert block.index("upstairs hall") < block.index("attic")


def test_legacy_facts_render_name_only_under_the_legacy_marker(build_log):
    """§11.1: a pre-regime session's facts are visible, without ids."""
    log = build_log()
    log.append_event(
        "chat",
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {"Yara": {"emotional": {"mood": "wary"}}},
            "venue": {"Yara": {"venue": "bathroom"}},
        },
    )

    text = render_entity_palette(registry(log, "chat"))

    legacy = next(line for line in text.splitlines() if line.startswith("LEGACY"))
    assert "Yara: emotional.mood=wary" in legacy
    assert "Yara at bathroom" in legacy
    assert "no ids yet" in legacy


def test_the_palette_is_pure_no_log_and_no_clock():
    """No log reads: the same frozen registry renders the same text (two args)."""
    assert render_entity_palette(Registry()) == render_entity_palette(Registry())


# -- W9.32 §3.1/§3.3: conditions, bodies, the purse --------------------------
#
# The three kinds that joined the registry after §7 was written render in the same
# id-first shape, in their own sections AFTER the W9.29 ones and before the LEGACY
# shim (never in the seeded prefix: AGENTS 6). An EXPIRED condition never reaches
# the palette at all — the engine's clock sweep tombstones it in the log (D9), so
# what the fold hands over is live conditions only.

EXPIRY = "2025-06-03 21:30"


def _w9_32_scene(log: EventLog, sid: str = "chat") -> dict[str, str]:
    """One condition (with an expiry), one permanent condition, one body, a purse."""
    condition = set_condition(
        log,
        sid,
        YARA,
        "suspicious",
        severity="moderate",
        source="the letter",
        expires_at=EXPIRY,
    )
    permanent = set_condition(log, sid, SERAPHINE, "grieving", severity="minor")
    body = set_body(
        log,
        sid,
        YARA,
        pose="standing",
        clothing=["housecoat", "slippers"],
        surface="faint bruise",
    )
    economy = establish_economy(log, sid, "credits", balance=85)
    return {
        "condition": condition.id,
        "permanent": permanent.id,
        "body": body.id,
        "economy": economy.id,
    }


def test_conditions_bodies_and_the_purse_render_id_first_after_the_w9_29_sections(build_log):
    log = build_log()
    ids = _w9_32_scene(log)

    lines = render_entity_palette(registry(log, "chat")).splitlines()

    conditions = next(line for line in lines if line.startswith("CONDITIONS: "))
    assert conditions == (
        f"CONDITIONS: {ids['condition']} suspicious {YARA} (moderate, expires 21:30) "
        f"— source: the letter · {ids['permanent']} grieving {SERAPHINE} (minor)"
    )
    bodies = next(line for line in lines if line.startswith("BODIES: "))
    assert bodies == (
        f"BODIES: {ids['body']} {YARA}: standing · clothes: housecoat, slippers "
        "· surface: faint bruise"
    )
    economy = next(line for line in lines if line.startswith("ECONOMY: "))
    assert economy == f"ECONOMY: {ids['economy']} 85 credits"

    # Position: after the W9.29 groups, before the footer (the tail's contract).
    assert lines.index(conditions) < lines.index(bodies) < lines.index(economy)
    assert lines.index(economy) == len(lines) - 2
    assert lines[-1].startswith("(") and lines[-1].endswith("actions last turn)")


def test_the_purse_and_the_two_being_kinds_count_as_objects(build_log):
    log = build_log()
    _w9_32_scene(log)

    footer = render_entity_palette(registry(log, "chat")).splitlines()[-1]

    # off-screen bucket + 2 conditions + 1 body + 1 economy = 5 objects.
    assert footer == "(5 objects · 0 actions last turn)"


def test_an_expired_condition_never_renders(build_log):
    log = build_log()
    ids = _w9_32_scene(log)

    retired = expire_conditions(log, "chat", now="2025-06-04 08:00")

    assert ids["condition"] in retired
    text = render_entity_palette(registry(log, "chat"))
    assert ids["condition"] not in text
    # The permanent one is still a live fact of the scene.
    assert ids["permanent"] in text


def test_a_permanent_condition_shows_no_expiry(build_log):
    log = build_log()
    condition = set_condition(log, "chat", PLAYER, "wounded", severity="major")

    conditions = next(
        line
        for line in render_entity_palette(registry(log, "chat")).splitlines()
        if line.startswith("CONDITIONS: ")
    )

    assert conditions == f"CONDITIONS: {condition.id} wounded {PLAYER} (major)"
    assert "expires" not in conditions


def test_the_new_sections_are_clipped_per_entry_like_the_old_ones(build_log):
    log = build_log()
    set_condition(
        log, "chat", YARA, "suspicious", source="the " + "x" * 400, expires_at=EXPIRY
    )

    entry = next(
        line
        for line in render_entity_palette(registry(log, "chat")).splitlines()
        if line.startswith("CONDITIONS: ")
    ).split(": ", 1)[1]

    assert len(entry) <= ENTITY_PALETTE_CLIP_CHARS
    assert entry.endswith("…")


def test_the_new_sections_are_absent_when_empty(build_log):
    log = build_log()
    establish_location(log, "chat", "bathroom")

    labels = {
        line.split(":", 1)[0]
        for line in render_entity_palette(registry(log, "chat")).splitlines()
        if line.split(":", 1)[0] in {"CONDITIONS", "BODIES", "ECONOMY"}
    }

    assert labels == set()


def test_the_player_renders_with_the_persona_name_when_one_is_handed_in(build_log):
    log = build_log()
    ids = _scene(log)

    with_persona = render_entity_palette(
        registry(log, "chat"), player_display="Wanderer"
    )
    without = render_entity_palette(registry(log, "chat"))

    assert (
        f"  > WHO IS WHERE — Player (Wanderer): {ids['hall']} · {YARA}: {ids['bath']}"
        in with_persona
    )
    assert f"  > WHO IS WHERE — Player: {ids['hall']}" in without
