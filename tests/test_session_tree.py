"""W8 — the session tree: one pure, render-ready projection of a session.

The tree is the reading surface (``GET /api/sessions/{id}/tree``): the clock,
what the room is doing, who is where, what is carried, what is unfinished, what
is metered, what is wrong with whom, the purse, and one node per cast member
whose identity splits into three SEPARABLE layers — ``participation`` (the
roster's facts), ``definition`` (the card text the chat was seeded with) and
``live`` (their state right now).

Pinned here: the top-level key ORDER (it is the render order), the addressing
contract (every object node carries its real registry ``id`` beside names — the
sidebar edits by id), the display surface's promise (no raw registry id in a
field a human reads: ``id``/``member_id`` are the identity fields, blanked by the
pin), the ``Player (Name)`` label with and without a persona, ``meters`` holding
no scene-hosted rows, the clock's proposal/``applied`` honesty, one expiring
beside one permanent condition, the place tree with its occupants by name, and
the purity claim — reading a tree appends nothing and materialises no roster.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.state import cast, entities
from fabulist.state.session_tree import (
    PLAYER_NODE,
    TREE_KEYS,
    session_tree,
)

SID = "s1"
SERAPHINE = "Seraphine"
YARA = "Yara"
EXPIRING = "2025-06-02 14:00"
PERMANENT = ""


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for event logs sharing one tmp dir; closes them after."""
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


def _seed(log: EventLog, session_id: str = SID) -> dict:
    """The pinned fixture: card + BTS member, two places, body, purse, clock.

    Returns the ids/keys the assertions cite. Every object kind the tree renders
    appears at least once, and the two cast members differ exactly where the
    three layers are supposed to differ: one was seeded from a card
    (``definition`` non-null), one was discovered in play (``definition`` null).
    """
    log.append_event(
        session_id,
        "system",
        "system",
        {
            "card": {
                "name": SERAPHINE,
                "description": "the host",
                "personality": "watchful",
                "scenario": "the estate",
            },
            "first_mes": "",
        },
    )
    log.append_event(session_id, "user", "user", {"text": "hi"})
    carded = cast.ensure_roster(log, session_id)[0]
    yara = cast.admit_member(
        log, session_id, YARA, provenance="bts", admitted_by="bts"
    )

    hall = entities.establish_location(log, session_id, "upstairs hall")
    bath = entities.establish_location(
        log, session_id, "bathroom", parent_id=hall, note="clawfoot tub"
    )
    entities.move_being(log, session_id, entities.PLAYER, bath)
    entities.move_being(log, session_id, carded.member_id, bath)
    entities.move_being(log, session_id, yara, entities.OFF_SCREEN, at=bath)

    entities.set_attribute(log, session_id, entities.PLAYER, key="stress", value=40)
    entities.set_attribute(
        log, session_id, carded.member_id, key="mood", value="watchful"
    )
    entities.set_attribute(log, session_id, entities.SCENE, key="tension", value=4)
    entities.set_attribute(
        log, session_id, entities.SCENE, key="weather", value="light rain"
    )
    entities.set_attribute(
        log, session_id, entities.SCENE, key="irony", value="practised politeness"
    )

    entities.set_condition(
        log,
        session_id,
        yara,
        "bleeding",
        severity="major",
        source="the fall",
        expires_at=EXPIRING,
    )
    entities.set_condition(
        log, session_id, carded.member_id, "suspicious", source="the letter",
        expires_at=PERMANENT,
    )
    entities.set_body(
        log,
        session_id,
        yara,
        pose="slumped",
        clothing=["coat", "shirt"],
        surface="blood on the sleeve",
    )
    purse = entities.establish_economy(
        log, session_id, "marks", balance=12, note="the purse"
    )

    item = entities.establish_item(log, session_id, "brass key", holder=entities.PLAYER)
    knife = entities.establish_item(log, session_id, "bread knife", holder=yara)
    thread = entities.establish_thread(
        log, session_id, "the debt", detail="unpaid", note="Seraphine's ledger"
    )
    edge = entities.establish_relationship(
        log, session_id, entities.PLAYER, yara, name="uneasy", note="a project"
    )

    # The clock: one accepted proposal, then the rules clock winning the stamp —
    # the accept that a crashed turn leaves behind (applied must stay False).
    log.append_event(
        session_id,
        "system",
        "bts",
        {
            "event": "clock_proposal",
            "time": "14:00",
            "date": "2025-06-02",
            "day": 3,
            "evidence": "the clock on the landing struck twice",
            "advanced_minutes": 30,
        },
    )
    log.append_event(
        session_id, "state-delta", "extractor", {"time": "13:00", "date": "2025-06-02"}
    )
    log.append_event(
        session_id,
        "state-delta",
        "extractor",
        {"time": "14:30", "date": "2025-06-02", "day": "3"},
    )
    return {
        "carded": carded.member_id,
        "yara": yara,
        "hall": hall,
        "bath": bath,
        "purse": purse.id,
        "item": item,
        "knife": knife,
        "thread": thread,
        "edge": edge,
    }


# -- shape -------------------------------------------------------------------


def test_tree_keys_are_the_pinned_render_order(build_log):
    """The top-level keys are exactly the render order W8 pins."""
    log = build_log()
    _seed(log)

    tree = session_tree(log, SID)

    assert list(tree) == [
        "clock",
        "scene",
        "places",
        "items",
        "threads",
        "relations",
        "meters",
        "conditions",
        "economy",
        "cast",
        "Player",
    ]
    assert tuple(tree) == TREE_KEYS


#: The tree's IDENTITY fields — handles for addressing, never display. ``id`` is
#: every object's registry id; ``member_id`` is the cast API's join key.
_IDENTITY_FIELDS = ("id", "member_id")

#: Every registry id prefix the tree can legitimately carry in an identity field
#: (``mt_``/``bd_`` are listed for completeness: no node exposes those today).
_REGISTRY_ID_PREFIXES = ("cm_", "loc_", "itm_", "th_", "rel_", "cd_", "eco_", "mt_", "bd_")


def _without_ids(node):
    """The tree with every IDENTITY field blanked out, recursively.

    ``id``/``member_id`` are addresses the owner's doors take, so they are meant
    to be there. Blanking them is what makes the pin below a statement about the
    fields a human READS.
    """
    if isinstance(node, dict):
        return {
            key: ("" if key in _IDENTITY_FIELDS else _without_ids(value))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_without_ids(value) for value in node]
    return node


def test_display_fields_never_carry_a_raw_registry_id(build_log):
    """No field a human READS carries a raw registry id; ids are addressing only.

    Every node carries an ``id`` (and ``participation.member_id``) on purpose —
    they are the handles the owner's doors take. The invariant is about the
    DISPLAY surface — occupants, item holders, relation endpoints and labels,
    meter and condition host labels, and the names of places/threads/items — so
    the pin blanks every identity field and greps what is LEFT. The ids must
    still be the real, joinable ones (the positive half).
    """
    log = build_log()
    ids = _seed(log)

    tree = session_tree(log, SID)
    display_only = json.dumps(_without_ids(tree))

    for prefix in _REGISTRY_ID_PREFIXES:
        assert prefix not in display_only
    assert [node["participation"]["member_id"] for node in tree["cast"]] == [
        ids["carded"],
        ids["yara"],
    ]
    assert {row["id"] for row in tree["items"]} == {ids["item"], ids["knife"]}
    # The same people, everywhere else, are names.
    assert YARA in display_only and SERAPHINE in display_only


def test_every_object_node_carries_its_object_id(build_log):
    """Addressing: each node carries the REAL registry id beside its names.

    The sidebar's edit affordances call ``update_object``/``retire_object``/
    ``move_being`` with the object id, so every node has to hand one out — and it
    is exactly the folded registry's id (``loc_``/``itm_``/``th_``/``rel_``/
    ``cd_``/``eco_``), never a re-minted or trimmed one (§2: ids are the object
    ids). The absence bucket is the exception: it is the reserved always-live
    literal, so there is nothing to address.
    """
    log = build_log()
    _seed(log)
    entities.set_condition(log, SID, entities.SCENE, "candlelit")

    tree = session_tree(log, SID)
    reg = entities.registry(log, SID)

    assert {row["id"] for row in tree["places"]["tree"]} == set(reg.locations) - {
        entities.OFF_SCREEN_LOCATION_ID
    }
    assert "id" not in tree["places"]["off_screen"]
    assert {row["id"] for row in tree["items"]} == set(reg.items)
    assert {row["id"] for row in tree["threads"]} == set(reg.threads)
    assert {row["id"] for row in tree["relations"]} == set(reg.relationships)
    assert {row["id"] for row in tree["conditions"]} == set(reg.conditions)
    assert {row["id"] for row in tree["scene"]["conditions"]} == {
        condition.id for condition in entities.conditions_for(reg, entities.SCENE)
    }
    assert tree["economy"]["id"] == reg.economy.id

    for prefix, nodes in (
        ("loc_", tree["places"]["tree"]),
        ("itm_", tree["items"]),
        ("th_", tree["threads"]),
        ("rel_", tree["relations"]),
        ("cd_", tree["conditions"]),
        ("eco_", [tree["economy"]]),
    ):
        assert nodes and all(node["id"].startswith(prefix) for node in nodes)


def test_tree_renders_an_empty_session_without_raising(build_log):
    """A session no producer has touched answers the full shape, not an error."""
    log = build_log()

    tree = session_tree(log, "never-written")

    assert list(tree) == list(TREE_KEYS)
    assert tree["clock"]["time"] == "" and tree["clock"]["proposal"] is None
    assert tree["places"]["tree"] == []
    assert tree["places"]["off_screen"]["occupants"] == []
    assert tree["economy"] is None
    assert tree["cast"] == []
    assert tree["Player"]["identity"] == {"key": "player", "display": "Player"}


# -- clock -------------------------------------------------------------------


def test_clock_reads_the_fold_its_writer_and_the_newest_proposal(build_log):
    """``time``/``date``/``day`` fold; ``last_writer`` is the row that moved it."""
    log = build_log()
    _seed(log)

    clock = session_tree(log, SID)["clock"]

    assert clock["time"] == "14:30"
    assert clock["date"] == "2025-06-02"
    assert clock["day"] == "3"
    assert clock["last_writer"] == "extractor"
    assert clock["proposal"] == {
        "time": "14:00",
        "date": "2025-06-02",
        "day": 3,
        "evidence": "the clock on the landing struck twice",
        "advanced_minutes": 30,
    }


def test_clock_proposal_is_not_applied_until_the_clock_takes_it(build_log):
    """An accepted-but-unapplied proposal is never reported as applied."""
    log = build_log()
    _seed(log)
    assert session_tree(log, SID)["clock"]["applied"] is False

    # The same proposal, this time stamped into the fold by the accept path.
    other = build_log("applied.db")
    _seed(other)
    other.append_event(
        SID,
        "state-delta",
        "bts",
        {"time": "14:00", "date": "2025-06-02", "day": "3"},
    )

    clock = session_tree(other, SID)["clock"]
    assert clock["time"] == "14:00"
    assert clock["applied"] is True


def test_clock_last_writer_ignores_a_row_that_changed_nothing(build_log):
    """A ``state-delta`` repeating the time in force did not write the clock."""
    log = build_log()
    _seed(log)
    log.append_event(
        SID, "state-delta", "user", {"time": "14:30", "weather": "light rain"}
    )

    assert session_tree(log, SID)["clock"]["last_writer"] == "extractor"


# -- scene -------------------------------------------------------------------


def test_scene_carries_its_traits_tension_and_conditions(build_log):
    """Scene traits are text; tension is a number; the scene owns its conditions."""
    log = build_log()
    _seed(log)
    entities.set_condition(log, SID, entities.SCENE, "candlelit")

    scene = session_tree(log, SID)["scene"]

    assert scene["weather"] == "light rain"
    assert scene["irony"] == "practised politeness"
    assert scene["tension"] == 4
    assert scene["tone"] == "" and scene["genre"] == ""
    assert [row["key"] for row in scene["conditions"]] == ["candlelit"]
    assert scene["conditions"][0]["host"] == "scene"
    assert scene["forecast"] == {"value": "", "pinned": False}


def test_scene_forecast_honours_the_manual_pin_before_the_tracker_trait(build_log):
    """The D45 pin wins; the model's scene trait is only the fallback (R2)."""
    log = build_log()
    _seed(log)
    entities.set_attribute(log, SID, entities.SCENE, key="forecast", value="clearing")
    assert session_tree(log, SID)["scene"]["forecast"] == {
        "value": "clearing",
        "pinned": False,
    }

    log.append_event(SID, "state-delta", "user", {"forecast": "storm"})

    assert session_tree(log, SID)["scene"]["forecast"] == {
        "value": "storm",
        "pinned": True,
    }


# -- places, inventory, plot -------------------------------------------------


def test_places_render_names_their_parent_and_their_occupants(build_log):
    """A place node carries its id for addressing and NAMES for everything read."""
    log = build_log()
    ids = _seed(log)

    places = session_tree(log, SID)["places"]

    bathroom = next(row for row in places["tree"] if row["name"] == "bathroom")
    assert bathroom["id"] == ids["bath"]
    assert bathroom["path"] == "upstairs hall > bathroom"
    assert bathroom["parent"] == "upstairs hall"
    assert bathroom["note"] == "clawfoot tub"
    assert bathroom["occupants"] == ["Player", SERAPHINE]
    hall = next(row for row in places["tree"] if row["name"] == "upstairs hall")
    assert hall["id"] == ids["hall"]
    assert hall["parent"] is None and hall["occupants"] == []
    # The absence bucket is separate, with the place it hangs off by path — and
    # no id: the reserved literal is always live, so nothing addresses it.
    assert places["off_screen"] == {
        "name": "off-screen",
        "at": "upstairs hall > bathroom",
        "occupants": [YARA],
    }


def test_items_threads_and_relations_render_display_names(build_log):
    """Holder and both edge endpoints are people, never raw occupant keys."""
    log = build_log()
    ids = _seed(log)

    tree = session_tree(log, SID)

    assert {(row["id"], row["name"], row["holder"]) for row in tree["items"]} == {
        (ids["item"], "brass key", "Player"),
        (ids["knife"], "bread knife", YARA),
    }
    assert tree["threads"] == [
        {
            "id": ids["thread"],
            "name": "the debt",
            "status": "active",
            "detail": "unpaid",
            "note": "Seraphine's ledger",
        }
    ]
    assert tree["relations"] == [
        {
            "id": ids["edge"],
            "from": "Player",
            "to": YARA,
            "label": "uneasy",
            "note": "a project",
        }
    ]


def test_relation_label_defaults_to_display_names_not_keys(build_log):
    """The store's synthesized ``from→to`` label must not leak the raw ends.

    ``establish_relationship`` names an unlabeled edge ``"from→to"``, and those
    ends are registry keys — the tree rebuilds that default from the labels it
    renders everywhere else (a label the writer wrote is prose and stays).
    """
    log = build_log()
    ids = _seed(log)
    entities.establish_relationship(log, SID, entities.PLAYER, ids["yara"])

    labels = [row["label"] for row in session_tree(log, SID)["relations"]]

    assert labels == ["uneasy", "Player→Yara"]


# -- meters / conditions / economy -------------------------------------------


def test_meters_group_by_host_label_and_split_numbers_from_text(build_log):
    """One group per BEING host, numbers and free text kept apart."""
    log = build_log()
    _seed(log)

    groups = {group["host"]: group for group in session_tree(log, SID)["meters"]}

    assert groups["Player"]["numbers"] == [{"key": "stress", "value": 40}]
    assert groups["Player"]["traits"] == []
    assert groups[SERAPHINE]["traits"] == [{"key": "mood", "value": "watchful"}]
    assert groups[SERAPHINE]["numbers"] == []


def test_meters_exclude_the_scene_hosted_attributes(build_log):
    """The scene's rows belong to ``scene``; repeating them here doubles the pane.

    §2: ``meters`` is every NON-scene attribute grouped by host. The scene's own
    ``tension``/``weather``/``irony`` are already served (and rendered) by the
    ``scene`` branch, so they must not appear in this list as well.
    """
    log = build_log()
    _seed(log)

    tree = session_tree(log, SID)

    assert "scene" not in {group["host"] for group in tree["meters"]}
    assert tree["scene"]["tension"] == 4
    assert tree["scene"]["weather"] == "light rain"
    assert tree["scene"]["irony"] == "practised politeness"


def test_conditions_carry_their_host_severity_and_expiry_verdict(build_log):
    """One expiring and one permanent condition; only the expiring one is expired."""
    log = build_log()
    _seed(log)

    rows = {row["key"]: row for row in session_tree(log, SID)["conditions"]}

    bleeding = rows["bleeding"]
    assert bleeding.pop("id").startswith("cd_")
    assert bleeding == {
        "host": YARA,
        "key": "bleeding",
        "severity": "major",
        "source": "the fall",
        "expires_at": EXPIRING,
        "expired": True,
    }
    assert rows["suspicious"]["id"].startswith("cd_")
    assert rows["suspicious"]["host"] == SERAPHINE
    assert rows["suspicious"]["expires_at"] is None
    assert rows["suspicious"]["expired"] is False


def test_economy_is_the_purse_or_null(build_log):
    """The session's one purse renders, or ``None`` when the planner has not seeded it."""
    log = build_log()
    assert session_tree(log, SID)["economy"] is None

    _seed(log)

    purse = session_tree(log, SID)["economy"]
    assert purse.pop("id").startswith("eco_")
    assert purse == {
        "denomination": "marks",
        "balance": 12,
        "note": "the purse",
    }


# -- cast --------------------------------------------------------------------


def test_cast_layers_stay_separable(build_log):
    """participation / definition / live are three independent layers.

    The card-backed member carries the card TEXT; the member learned in play has
    no definition at all — and both still carry their full live state.
    """
    log = build_log()
    ids = _seed(log)

    nodes = {
        node["participation"]["display"]: node
        for node in session_tree(log, SID)["cast"]
    }

    assert nodes[SERAPHINE]["definition"] == {
        "name": SERAPHINE,
        "description": "the host",
        "personality": "watchful",
        "scenario": "the estate",
    }
    assert nodes[YARA]["definition"] is None

    carded = nodes[SERAPHINE]["participation"]
    assert carded == {
        "member_id": ids["carded"],
        "display": SERAPHINE,
        "aliases": [],
        "provenance": "card",
        "source_id": None,
        "status": "active",
        "tier": "actor",
        "promoted_persona_id": None,
    }
    assert nodes[YARA]["participation"]["tier"] == "presence"

    # Live: the card-backed member is in the player's room, Yara is off-screen.
    assert nodes[SERAPHINE]["live"]["location"] == "upstairs hall > bathroom"
    assert nodes[SERAPHINE]["live"]["presence"] == "in_scene"
    assert nodes[YARA]["live"]["location"] is None
    assert nodes[YARA]["live"]["off_screen"] is True
    assert nodes[YARA]["live"]["presence"] == "off_screen"
    assert nodes[YARA]["live"]["body"] == {
        "pose": "slumped",
        "clothing": ["coat", "shirt"],
        "surface": "blood on the sleeve",
        "note": "",
    }
    assert nodes[YARA]["live"]["items"] == ["bread knife"]
    assert [row["key"] for row in nodes[YARA]["live"]["conditions"]] == ["bleeding"]


def test_cast_marks_a_being_in_another_room_as_elsewhere(build_log):
    """A move to a place the player is not in is ``elsewhere``, not in-scene."""
    log = build_log()
    ids = _seed(log)
    entities.move_being(log, SID, ids["carded"], ids["hall"])

    node = next(
        row
        for row in session_tree(log, SID)["cast"]
        if row["participation"]["display"] == SERAPHINE
    )

    assert node["live"]["presence"] == "elsewhere"
    assert node["live"]["location"] == "upstairs hall"
    assert node["live"]["off_screen"] is False


# -- the player --------------------------------------------------------------


def test_player_node_presents_as_player_name_with_and_without_a_persona(build_log):
    """``Player (Name)`` when a persona is assigned, bare ``Player`` when not."""
    from fabulist.personas.registry import append_persona, assign_persona

    log = build_log()
    _seed(log)
    persona_id = append_persona(log, {"name": "Wanderer"})
    assign_persona(log, SID, persona_id)

    player = session_tree(log, SID)[PLAYER_NODE]
    assert player["identity"] == {"key": "player", "display": "Player (Wanderer)"}
    assert player["persona_ref"] == persona_id
    assert player["live"]["location"] == "upstairs hall > bathroom"
    assert player["live"]["presence"] == "in_scene"
    assert player["live"]["items"] == ["brass key"]
    assert player["live"]["meters"]["numbers"] == [{"key": "stress", "value": 40}]
    # The player's own label is used by every other row that names them.
    assert session_tree(log, SID)["relations"][0]["from"] == "Player (Wanderer)"

    other = build_log("bare.db")
    _seed(other)
    bare = session_tree(other, SID)[PLAYER_NODE]
    assert bare["identity"] == {"key": "player", "display": "Player"}
    assert "persona_ref" not in bare


# -- purity ------------------------------------------------------------------


def test_reading_a_tree_appends_nothing_and_materialises_no_roster(build_log):
    """The projection is a read: no roster mint, no event, no second answer."""
    log = build_log()
    log.append_event(
        SID,
        "system",
        "system",
        {"card": {"name": SERAPHINE, "description": "the host"}, "first_mes": ""},
    )
    before = len(log.get_events(SID))

    tree = session_tree(log, SID)
    again = session_tree(log, SID)

    assert tree == again
    assert tree["cast"] == []
    assert len(log.get_events(SID)) == before
    assert [e for e in log.get_events(SID) if e.payload.get("event") == "cast_admit"] == []
