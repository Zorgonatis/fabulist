"""W9.29 §3/§5/§11.1 — the entity registry: identity-stable BTS objects.

Covers the Wave-1 gate (plan §13): fold replay over hand-built event lists
(establish/set/update/move/retire, fork chain, resurrection), id-mint
determinism (same log -> same ids, collision suffix, reserved literals never
minted), legacy-merge precedence (the ``world_state`` fold beneath the registry:
registry wins per being/key, legacy-only facts render name-only), and the pinned
read helpers (``location_path``/``occupants``/``same_room``/``being_render``/
``flat_render``, including the deliberate ``forecast`` absence of review R2).

Event payloads are built BY HAND where the fold is the subject (a hand-built
list is the only honest fixture for replay), and through the writers where the
writer contract is the subject (§5's payload table).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.state.entities import (
    ENTITY_ESTABLISH_EVENT,
    ENTITY_MOVE_EVENT,
    ENTITY_RETIRE_EVENT,
    ENTITY_SET_EVENT,
    ENTITY_SUMMARY_EVENT,
    ENTITY_UPDATE_EVENT,
    OFF_SCREEN,
    OFF_SCREEN_LOCATION_ID,
    PLAYER,
    SCENE,
    EntityError,
    append_entity_summary,
    being_render,
    establish_item,
    establish_location,
    establish_relationship,
    establish_thread,
    flat_render,
    location_path,
    mint_location_id,
    mint_meter_id,
    mint_relationship_id,
    move_being,
    occupants,
    registry,
    resolve_location,
    retire_object,
    same_room,
    scene_traits,
    set_attribute,
    update_object,
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


def _append(log: EventLog, sid: str, payload: dict, actor: str = "bts") -> None:
    log.append_event(sid, "system", actor, payload)


def _payloads(log: EventLog, sid: str, kind: str) -> list[dict]:
    return [e.payload for e in log.get_events(sid) if e.payload.get("event") == kind]


def _the_courtship_scene(log: EventLog, sid: str) -> tuple[str, str]:
    """The §3.7 worked turn, hand-built: two locations + the off-screen move."""
    _append(log, sid, {"event": ENTITY_ESTABLISH_EVENT, "kind": "location",
                       "id": "loc_hall", "name": "upstairs hall", "seq": 1})
    _append(log, sid, {"event": ENTITY_ESTABLISH_EVENT, "kind": "location",
                       "id": "loc_bath", "name": "bathroom", "parent_id": "loc_hall",
                       "note": "clawfoot tub, door removed", "seq": 1})
    _append(log, sid, {"event": ENTITY_MOVE_EVENT, "who": PLAYER, "to": "loc_hall", "seq": 1})
    _append(log, sid, {"event": ENTITY_MOVE_EVENT, "who": YARA, "to": "loc_bath", "seq": 1})
    _append(log, sid, {"event": ENTITY_MOVE_EVENT, "who": SERAPHINE, "to": OFF_SCREEN,
                       "at": "loc_bath", "seq": 1})
    return "loc_hall", "loc_bath"


# -- fold replay -------------------------------------------------------------


def test_fold_replays_the_plan_object_kinds(build_log):
    """Every §3 object kind + the §3.6 assignment folds from its own event."""
    log = build_log()
    sid = "chat"
    _the_courtship_scene(log, sid)
    _append(log, sid, {"event": ENTITY_ESTABLISH_EVENT, "kind": "thread",
                       "id": "th_court", "name": "the slow courtship",
                       "detail": "Seraphine tests the newcomer", "seq": 1})
    _append(log, sid, {"event": ENTITY_ESTABLISH_EVENT, "kind": "relationship",
                       "id": "rel_project", "name": "Seraphine→player",
                       "from": SERAPHINE, "to": PLAYER,
                       "note": "the newcomer is a project", "seq": 1})
    _append(log, sid, {"event": ENTITY_ESTABLISH_EVENT, "kind": "item",
                       "id": "itm_key", "name": "brass key", "holder": PLAYER,
                       "note": "opens the basement door", "seq": 1})
    _append(log, sid, {"event": ENTITY_SET_EVENT, "id": "mt_hostility", "host": SERAPHINE,
                       "subject": PLAYER, "key": "hostility", "value": 42, "seq": 1})

    reg = registry(log, sid)

    assert [loc.name for loc in reg.locations.values()] == [
        "upstairs hall",
        "bathroom",
        "off-screen",
    ]
    bathroom = reg.locations["loc_bath"]
    assert bathroom.parent_id == "loc_hall"
    assert bathroom.note == "clawfoot tub, door removed"
    assert bathroom.established_seq > 0
    assert reg.threads["th_court"].status == "active"
    assert reg.threads["th_court"].detail == "Seraphine tests the newcomer"
    relationship = reg.relationships["rel_project"]
    assert (relationship.source, relationship.target) == (SERAPHINE, PLAYER)
    assert relationship.note == "the newcomer is a project"
    assert reg.items["itm_key"].holder == PLAYER
    meter = reg.meters["mt_hostility"]
    assert (meter.host, meter.subject, meter.key, meter.value) == (
        SERAPHINE,
        PLAYER,
        "hostility",
        42,
    )
    assert reg.assignments == {PLAYER: "loc_hall", YARA: "loc_bath", SERAPHINE: OFF_SCREEN}
    assert reg.off_screen_ref == "loc_bath"


def test_entity_set_is_latest_wins_per_id(build_log):
    """Turn 2's hostility 30 is the SAME object (the epic's headline example)."""
    log = build_log()
    sid = "chat"
    meter_id, prior = set_attribute(
        log, sid, SERAPHINE, key="hostility", value=42, subject=PLAYER
    )
    assert prior is None
    again, prior = set_attribute(
        log, sid, SERAPHINE, key="hostility", value=30, subject=PLAYER
    )
    assert again == meter_id
    assert prior == 42
    reg = registry(log, sid)
    assert list(reg.meters) == [meter_id]
    assert reg.meters[meter_id].value == 30
    # A self meter keys without a subject and is its own identity.
    stress, _ = set_attribute(log, sid, YARA, key="stress", value=55)
    assert stress != meter_id
    assert registry(log, sid).meters[stress].subject is None
    # Re-setting the same self meter reuses the id.
    assert set_attribute(log, sid, YARA, key="stress", value=60)[0] == stress


def test_entity_update_patches_in_place_and_keeps_the_id(build_log):
    log = build_log()
    sid = "chat"
    location_id = establish_location(log, sid, "bathroom", note="clawfoot tub")
    assert update_object(log, sid, location_id, name="master bath") == location_id
    reg = registry(log, sid)
    assert reg.locations[location_id].name == "master bath"
    assert reg.locations[location_id].note == "clawfoot tub"

    thread_id = establish_thread(log, sid, "the slow courtship")
    update_object(log, sid, thread_id, status="paused", detail="standoff")
    thread = registry(log, sid).threads[thread_id]
    assert (thread.status, thread.detail) == ("paused", "standoff")


def test_entity_retire_tombstones_and_a_later_establish_resurrects(build_log):
    """D10 shape: the tombstone removes; re-establishing the same name resurrects."""
    log = build_log()
    sid = "chat"
    location_id = establish_location(log, sid, "bathroom")
    established = registry(log, sid).locations[location_id].established_seq
    retire_object(log, sid, location_id, reason="merged into the hallway")
    assert location_id not in registry(log, sid).locations

    again = establish_location(log, sid, "bathroom")
    # The mint excludes tombstones, so the identity comes back as ITSELF.
    assert again == location_id
    reg = registry(log, sid)
    assert location_id in reg.locations
    assert reg.locations[location_id].established_seq == established


def test_entity_move_latest_wins_per_being(build_log):
    log = build_log()
    sid = "chat"
    hall, bath = _the_courtship_scene(log, sid)
    move_being(log, sid, PLAYER, bath)
    reg = registry(log, sid)
    assert reg.assignments[PLAYER] == bath
    assert occupants(reg, bath) == (PLAYER, YARA)  # assignment order
    # Moving back to off_screen without `at` CLEARS the bucket reference.
    move_being(log, sid, YARA, OFF_SCREEN)
    assert registry(log, sid).off_screen_ref is None


def test_fork_inherits_the_registry_up_to_its_cut(build_log):
    log = build_log()
    parent = "s1"
    bath = establish_location(log, parent, "bathroom")
    cut = log.get_events(parent)[-1].seq
    log.fork_session(parent, cut, "fork1")
    kitchen = establish_location(log, parent, "kitchen")

    branch = registry(log, "fork1")
    assert bath in branch.locations
    assert kitchen not in branch.locations

    own = establish_location(log, "fork1", "pantry")
    assert own in registry(log, "fork1").locations
    assert own not in registry(log, parent).locations
    # The same name mints the same id on every line (replay-stable, §5).
    assert mint_location_id("bathroom") == bath


# -- mint determinism + collision suffix -------------------------------------


def _scenario(log: EventLog, sid: str) -> dict[str, str]:
    hall = establish_location(log, sid, "upstairs hall")
    bath = establish_location(log, sid, "bathroom", parent_id=hall)
    thread = establish_thread(log, sid, "the slow courtship")
    relationship = establish_relationship(log, sid, SERAPHINE, PLAYER)
    meter, _ = set_attribute(
        log, sid, SERAPHINE, key="hostility", value=42, subject=PLAYER
    )
    item = establish_item(log, sid, "brass key", holder=PLAYER)
    return {
        "hall": hall,
        "bath": bath,
        "thread": thread,
        "relationship": relationship,
        "meter": meter,
        "item": item,
    }


def test_mint_is_deterministic_for_the_same_operations(build_log):
    first = _scenario(build_log("a.db"), "chat")
    second = _scenario(build_log("b.db"), "chat")
    assert first == second
    assert first["hall"].startswith("loc_")
    assert first["thread"].startswith("th_")
    assert first["relationship"].startswith("rel_")
    assert first["meter"].startswith("mt_")
    assert first["item"].startswith("itm_")
    assert len(first["hall"]) == len("loc_") + 12


def test_colliding_identity_gets_a_counter_suffix_and_frees_on_retire(build_log):
    log = build_log()
    sid = "chat"
    # The write door REFUSES a live duplicate name (the live-gate guard below),
    # so a mint collision is reachable only from a historical/imported row or by
    # calling the mint directly — the collision behaviour itself stays pinned.
    first = establish_location(log, sid, "bathroom")
    second = mint_location_id("Bathroom", [first])
    assert first != second
    assert second.startswith("loc_")
    # A relationship pair is an identity too — duplicates are legal (req. 4).
    edge_one = establish_relationship(log, sid, SERAPHINE, PLAYER)
    edge_two = establish_relationship(log, sid, SERAPHINE, PLAYER)
    assert edge_one != edge_two
    # Retiring the first frees index 0: the name mints the ORIGINAL id again.
    retire_object(log, sid, first)
    assert establish_location(log, sid, "bathroom") == first


def test_reserved_literals_are_never_minted(build_log):
    log = build_log()
    sid = "chat"
    for reserved in ("off_screen", "Off-Screen", OFF_SCREEN_LOCATION_ID, "player", "scene"):
        with pytest.raises(EntityError):
            mint_location_id(reserved)
    with pytest.raises(EntityError):
        establish_thread(log, sid, "scene")
    # ...but `player`/`scene` are legal HOSTS (they are identities, not objects).
    assert mint_meter_id(PLAYER, PLAYER, "hostility").startswith("mt_")
    assert mint_relationship_id(YARA, PLAYER).startswith("rel_")


def test_the_off_screen_bucket_is_always_present_and_never_retired(build_log):
    log = build_log()
    sid = "chat"
    reg = registry(log, sid)
    assert list(reg.locations) == [OFF_SCREEN_LOCATION_ID]
    assert reg.locations[OFF_SCREEN_LOCATION_ID].name == "off-screen"
    assert location_path(reg, OFF_SCREEN_LOCATION_ID) == "off-screen"
    with pytest.raises(EntityError):
        retire_object(log, sid, OFF_SCREEN_LOCATION_ID)


# -- the legacy shim (§11.1) -------------------------------------------------


def _legacy_world_state(log: EventLog, sid: str, **overrides) -> None:
    payload = {
        "event": "world_state",
        "kind": "delta",
        "state": {
            "player": {"emotional": {"hostility": "30", "mood": "wary"}},
            "Yara": {"inventory": {"carrying": "+brass key"}},
        },
        "venue": {"player": {"venue": "bathroom", "area": "downstairs"}},
        "threads": [{"status": "active", "name": "the slow courtship", "detail": "x"}],
        "off_screen": [{"name": "Seraphine"}],
        "tension": {"level": 6},
    }
    payload.update(overrides)
    _append(log, sid, payload)


def test_legacy_world_state_folds_beneath_the_registry(build_log):
    """Registry wins per being/key; legacy-only facts surface name-only."""
    log = build_log()
    sid = "chat"
    _legacy_world_state(log, sid)
    set_attribute(log, sid, PLAYER, key="hostility", value=42, subject=SERAPHINE)

    reg = registry(log, sid)
    assert reg.legacy.threads[0]["name"] == "the slow courtship"
    assert reg.legacy.off_screen[0]["name"] == "Seraphine"
    assert reg.legacy.tension == {"level": 6}

    rendered = being_render(reg, PLAYER)
    assert "= 42" in rendered  # the registry meter
    assert "30" not in rendered  # the legacy row it supersedes
    assert "legacy: mood: wary" in rendered  # a legacy-only fact, no id

    # A being the registry knows nothing about renders entirely name-only.
    yara = being_render(reg, YARA.replace("cm_", "").capitalize())
    assert yara.startswith("legacy: ")


def test_legacy_venue_fills_the_flat_loc_until_the_registry_assigns(build_log):
    log = build_log()
    sid = "chat"
    _legacy_world_state(log, sid)
    assert flat_render(registry(log, sid))["loc"] == "bathroom"

    hall = establish_location(log, sid, "upstairs hall")
    # DEFAULT-PARENT (owner 2026-10-21): the hall is the session's only root, so an
    # unparented "bathroom" NESTS under it — the canonical path is therefore the
    # parent chain, not the bare room name. flat_render still prefers the registry
    # over the legacy venue, which is what this test pins.
    bath = establish_location(log, sid, "bathroom")
    move_being(log, sid, PLAYER, bath)
    reg = registry(log, sid)
    assert reg.assignments[PLAYER] == bath
    assert flat_render(reg)["loc"] == "upstairs hall > bathroom"


def test_legacy_checkpoint_replaces_the_accumulated_shim(build_log):
    log = build_log()
    sid = "chat"
    _legacy_world_state(log, sid)
    _append(log, sid, {
        "event": "world_state",
        "kind": "checkpoint",
        "state": {"Yara": {"emotional": {"mood": "wary"}}},
        "venue": {"Yara": {"venue": "kitchen"}},
        "threads": [],
        "off_screen": [],
        "tension": {},
    })
    legacy = registry(log, sid).legacy
    assert list(legacy.state) == ["Yara"]
    assert list(legacy.venue) == ["Yara"]
    assert legacy.threads == []
    assert legacy.off_screen == []


# -- read helpers ------------------------------------------------------------


def test_location_path_walks_the_parent_chain(build_log):
    log = build_log()
    sid = "chat"
    hall = establish_location(log, sid, "upstairs hall")
    bath = establish_location(log, sid, "bathroom", parent_id=hall)
    reg = registry(log, sid)
    assert location_path(reg, bath) == "upstairs hall > bathroom"
    assert location_path(reg, hall) == "upstairs hall"
    assert location_path(reg, "loc_missing") == ""
    # A live duplicate name is refused at the write door (the live-gate guard).
    with pytest.raises(EntityError) as refused:
        establish_location(log, sid, "Bathroom")
    assert bath in str(refused.value)
    # Ambiguity itself is reachable only from a historical duplicate row
    # (below the write door): the resolver still refuses to pick one silently.
    from fabulist.log.events import Event

    class _StoredLog:
        def __init__(self, events):
            self._events = events

        def get_chain_events(self, _session_id):
            return self._events

    def _stored(seq, location_id):
        return Event(
            id=seq,
            ts="2026-10-21T00:00:00+00:00",
            schema_version=1,
            session_id=sid,
            seq=seq,
            type="system",
            actor="bts",
            payload={
                "event": ENTITY_ESTABLISH_EVENT,
                "kind": "location",
                "id": location_id,
                "name": "bathroom",
            },
        )

    historical = _StoredLog([_stored(1, "loc_old1"), _stored(2, "loc_old2")])
    reg = registry(historical, sid)
    with pytest.raises(EntityError) as ambiguous:
        resolve_location(reg, "bathroom")
    assert "matches 2 locations" in str(ambiguous.value)


def test_occupants_and_same_room_are_object_equality(build_log):
    log = build_log()
    sid = "chat"
    hall, bath = _the_courtship_scene(log, sid)
    reg = registry(log, sid)
    assert occupants(reg, hall) == (PLAYER,)
    assert occupants(reg, OFF_SCREEN) == (SERAPHINE,)
    assert occupants(reg, OFF_SCREEN_LOCATION_ID) == (SERAPHINE,)
    assert same_room(reg, YARA) is False

    move_being(log, sid, PLAYER, bath)
    reg = registry(log, sid)
    assert same_room(reg, YARA) is True  # equality, not freshness (req. 13)
    assert same_room(reg, SERAPHINE) is False  # the bucket is an exit
    move_being(log, sid, PLAYER, OFF_SCREEN)
    reg = registry(log, sid)
    assert same_room(reg, SERAPHINE) is True
    assert same_room(reg, "cm_unassigned") is False


def test_being_render_groups_meters_and_traits(build_log):
    log = build_log()
    sid = "chat"
    hostile, _ = set_attribute(
        log, sid, SERAPHINE, key="hostility", value=42, subject=PLAYER
    )
    mood, _ = set_attribute(log, sid, SERAPHINE, key="mood", value="wary")
    reg = registry(log, sid)
    assert being_render(reg, SERAPHINE) == (
        f"meters: {hostile} hostility {SERAPHINE}→{PLAYER} = 42\n"
        f"traits: {mood} mood {SERAPHINE} = wary"
    )
    assert being_render(reg, YARA) == ""


def test_flat_render_pins_loc_weather_tone_inv_and_withholds_forecast(build_log):
    """The node's ``state-delta`` slice stays byte-compatible — minus forecast."""
    log = build_log()
    sid = "chat"
    hall = establish_location(log, sid, "upstairs hall")
    bath = establish_location(log, sid, "bathroom", parent_id=hall)
    move_being(log, sid, PLAYER, bath)
    establish_item(log, sid, "brass key", holder=PLAYER)
    establish_item(log, sid, "hairpin", holder=YARA)
    set_attribute(log, sid, SCENE, key="weather", value="rain")
    set_attribute(log, sid, SCENE, key="tone", value="intimate")
    set_attribute(log, sid, SCENE, key="forecast", value="storm by nightfall")
    set_attribute(log, sid, SCENE, key="tension", value=7)

    reg = registry(log, sid)
    assert flat_render(reg) == {
        "loc": "upstairs hall > bathroom",
        "weather": "rain",
        "tone": "intimate",
        "inv": "brass key",
    }
    # Review R2: `forecast` is the D45 MANUAL pin in the flat fold; a model
    # trait must never clobber it, so the flat render never carries it...
    assert "forecast" not in flat_render(reg)
    # ...while the env_hud ladder's own rung reads it here.
    assert scene_traits(reg)["forecast"] == "storm by nightfall"
    assert "tension" not in scene_traits(reg)  # numbers are meters, not traits


# -- entity_summary ----------------------------------------------------------


def test_entity_summary_appends_counts_for_the_turn(build_log):
    log = build_log()
    sid = "chat"
    payload = append_entity_summary(
        log, sid, actions=3, rejected=["unknown attribute 'hostilty'"], flags=0, seq=9
    )
    assert payload == {
        "event": ENTITY_SUMMARY_EVENT,
        "seq": 9,
        "actions": 3,
        "rejected": 1,
        "flags": 0,
    }
    assert _payloads(log, sid, ENTITY_SUMMARY_EVENT)[-1]["actions"] == 3
    # `seq` is a required key on this variant: an unknown turn records 0.
    assert append_entity_summary(log, sid, actions=0)["seq"] == 0
    assert _payloads(log, sid, ENTITY_SUMMARY_EVENT)[-1]["rejected"] == 0


# -- writer validation (§5/§6 readable errors) -------------------------------


def test_writer_rejections_are_readable_and_append_nothing(build_log):
    log = build_log()
    sid = "chat"
    bath = establish_location(log, sid, "bathroom")
    move_being(log, sid, YARA, bath)
    appends = len(log.get_events(sid))

    with pytest.raises(EntityError) as occupied:
        retire_object(log, sid, bath)
    assert str(occupied.value) == f"{bath} is occupied by {YARA} — move_being them first"

    with pytest.raises(EntityError):
        retire_object(log, sid, "loc_kitchen")
    with pytest.raises(EntityError):
        update_object(log, sid, "loc_kitchen", name="kitchen")
    with pytest.raises(EntityError):
        move_being(log, sid, YARA, "loc_kitchen")
    with pytest.raises(EntityError):
        move_being(log, sid, OFF_SCREEN, bath)
    with pytest.raises(EntityError):
        move_being(log, sid, YARA, bath, at=bath)  # `at` is a bucket-only field
    with pytest.raises(EntityError):
        set_attribute(log, sid, YARA, key="stress", value=None)
    with pytest.raises(EntityError):
        update_object(log, sid, bath)  # nothing to patch
    thread = establish_thread(log, sid, "the slow courtship")
    with pytest.raises(EntityError):
        update_object(log, sid, thread, status="smouldering")
    item = establish_item(log, sid, "brass key", holder=PLAYER)
    with pytest.raises(EntityError):
        update_object(log, sid, item, status="active")  # items have no status
    with pytest.raises(EntityError):
        establish_item(log, sid, "hairpin", holder="  ")

    assert len(log.get_events(sid)) == appends + 2  # only the thread + item landed


def test_appended_payloads_match_the_plan_table(build_log):
    """§5's table is the schema_gate contract: exact keys, no inventions."""
    log = build_log()
    sid = "chat"
    hall = establish_location(log, sid, "upstairs hall", note="landing", seq=4)
    establish_location(log, sid, "bathroom", parent_id=hall)
    establish_thread(log, sid, "the slow courtship", detail="tests boundaries")
    establish_relationship(log, sid, SERAPHINE, PLAYER, note="a project")
    establish_item(log, sid, "brass key", holder=PLAYER, note="basement")
    meter_id, _ = set_attribute(
        log, sid, SERAPHINE, key="hostility", value=42, subject=PLAYER
    )
    update_object(log, sid, hall, name="upstairs landing")
    move_being(log, sid, SERAPHINE, OFF_SCREEN, at=hall)
    retire_object(log, sid, meter_id, reason="scene closed")
    append_entity_summary(log, sid, actions=1, rejected=0, flags=0, seq=4)

    location = next(
        p for p in _payloads(log, sid, ENTITY_ESTABLISH_EVENT)
        if p["kind"] == "location" and p["id"] == hall
    )
    assert set(location) == {"event", "kind", "id", "name", "note", "seq"}
    assert location["seq"] == 4  # the turn's narrative anchor rides the payload

    by_kind = {
        kind: next(
            p for p in _payloads(log, sid, ENTITY_ESTABLISH_EVENT) if p["kind"] == kind
        )
        for kind in ("thread", "relationship", "item")
    }
    assert set(by_kind["thread"]) == {"event", "kind", "id", "name", "detail"}
    assert set(by_kind["relationship"]) == {
        "event", "kind", "id", "name", "from", "to", "note",
    }
    assert set(by_kind["item"]) == {"event", "kind", "id", "name", "holder", "note"}

    assert set(_payloads(log, sid, ENTITY_SET_EVENT)[-1]) == {
        "event", "id", "host", "key", "value", "subject",
    }
    assert set(_payloads(log, sid, ENTITY_MOVE_EVENT)[-1]) == {
        "event", "who", "to", "at",
    }
    assert set(_payloads(log, sid, ENTITY_UPDATE_EVENT)[-1]) == {"event", "id", "patch"}
    assert set(_payloads(log, sid, ENTITY_RETIRE_EVENT)[-1]) == {"event", "id", "reason"}
    assert set(_payloads(log, sid, ENTITY_SUMMARY_EVENT)[-1]) == {
        "event", "seq", "actions", "rejected", "flags",
    }


def test_every_writer_defaults_the_actor_to_bts(build_log):
    log = build_log()
    sid = "chat"
    establish_location(log, sid, "bathroom")
    establish_thread(log, sid, "the slow courtship")
    set_attribute(log, sid, YARA, key="stress", value=55)
    establish_item(log, sid, "brass key", holder=PLAYER)
    for event in log.get_events(sid):
        if event.payload.get("event") == "world_state":
            continue
        assert event.type == "system"
        assert event.actor == "bts"


def test_duplicate_names_are_refused_with_the_existing_id(build_log):
    """The live-gate guard: a 4B re-established "dormitory" and moved the cast in.

    The store never merges (req. 4), but a live identity with the same canonical
    name is a corrective error naming the existing id — the recovery the small
    model already performs for ambiguity and range rejections.
    """
    import pytest

    log = build_log()
    sid = "chat"
    loc = establish_location(log, sid, "Dormitory")
    with pytest.raises(EntityError) as exc:
        establish_location(log, sid, "dormitory")
    assert loc in str(exc.value)
    thread = establish_thread(log, sid, "the watch assignment")
    with pytest.raises(EntityError) as exc:
        establish_thread(log, sid, "The Watch Assignment")
    assert thread in str(exc.value)
    item = establish_item(log, sid, "brass key", holder=PLAYER)
    with pytest.raises(EntityError) as exc:
        establish_item(log, sid, "Brass Key", holder=PLAYER)
    assert item in str(exc.value)


def test_a_retired_name_may_be_established_again(build_log):
    log = build_log()
    sid = "chat"
    loc = establish_location(log, sid, "dormitory")
    retire_object(log, sid, loc, reason="merged by hand")
    again = establish_location(log, sid, "dormitory")
    # Resurrection (D10 shape): the same identity mints the SAME id again.
    assert again == loc
