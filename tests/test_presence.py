"""W9.29 §8 — registry-first presence (the identity-stable rung).

Owner requirement 13: presence is OBJECT EQUALITY. A member the tracker assigned
is in the scene iff ``assignment[member_id] == assignment[player]``; a move to
any other location — the off-screen bucket included — is the deterministic
presence EXIT, with no freshness window (an assignment is exact where a
``world_state`` delta is only "what changed").

The legacy walk stays BENEATH that rung for members the registry has no
assignment for (forward-only, req. 15): pre-regime turns, attribution-rung
members, and any session whose tracker has not placed a being yet. These tests
pin the precedence in both directions — a registry answer wins even when a stale
legacy venue disagrees, and the walk still answers where the registry is silent.

Fake models only — no live inference.
"""

from __future__ import annotations

from fabulist.log import EventLog
from fabulist.state import entities
from fabulist.state.cast import REFERENCE_DESCRIPTOR, admit_member, member_by_name, roster
from fabulist.state.presence import (
    _presence_fact,
    active_members,
    presence_walk,
)

SID = "s1"


def _admit(log: EventLog, name: str, *, descriptor: bool = False) -> str:
    """Admit one card-tier member through the single admit path."""
    return admit_member(
        log,
        SID,
        name,
        provenance="card",
        admitted_by="test",
        reference_kind=REFERENCE_DESCRIPTOR if descriptor else "identity",
    )


def _room(log: EventLog, name: str) -> str:
    return entities.establish_location(log, SID, name)


def _move(log: EventLog, who: str, to: str, *, at: str | None = None) -> None:
    entities.move_being(log, SID, who, to, at=at)


def _world_state_venue(log: EventLog, venue: dict[str, str]) -> None:
    """Append one legacy ``world_state`` delta naming present characters."""
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {},
            "venue": {name: {"venue": place} for name, place in venue.items()},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )


def _member(log: EventLog, name: str):
    return member_by_name(roster(log, SID), name)


# -- the registry rung -------------------------------------------------------


def test_a_shared_assignment_is_present_by_object_equality(log):
    """No world_state evidence at all: the registry alone puts Yara in the scene."""
    yara = _admit(log, "Yara")
    bedroom = _room(log, "master bedroom")
    bathroom = _room(log, "bathroom")
    _move(log, entities.PLAYER, bedroom)
    _move(log, yara, bedroom)
    assert bathroom  # a second room exists and nobody is in it

    assert active_members(log, SID) == ([yara], "presence")


def test_moving_out_of_the_players_room_is_a_deterministic_exit(log):
    """The player moves away; the member left behind stops laning this turn."""
    yara = _admit(log, "Yara")
    bedroom = _room(log, "master bedroom")
    bathroom = _room(log, "bathroom")
    _move(log, entities.PLAYER, bedroom)
    _move(log, yara, bedroom)
    assert active_members(log, SID) == ([yara], "presence")

    # The member moves; the exit is the inequality, nothing else.
    _move(log, yara, bathroom)
    assert active_members(log, SID) == ([], "none")

    # ...and equality brings her back with no freshness window involved.
    _move(log, entities.PLAYER, bathroom)
    assert active_members(log, SID) == ([yara], "presence")


def test_the_off_screen_bucket_is_any_other_location(log):
    """``off_screen`` exits like any room — it is only the absence destination."""
    yara = _admit(log, "Yara")
    bedroom = _room(log, "master bedroom")
    _move(log, entities.PLAYER, bedroom)
    _move(log, yara, bedroom)
    assert active_members(log, SID) == ([yara], "presence")

    _move(log, yara, entities.OFF_SCREEN, at=bedroom)

    assert active_members(log, SID) == ([], "none")


def test_a_newcomer_who_shares_the_room_joins_and_leaves_by_assignment(log):
    """Two members, two rooms: only the one sharing the player's room lanes."""
    yara = _admit(log, "Yara")
    seraphine = _admit(log, "Seraphine")
    bedroom = _room(log, "master bedroom")
    hall = _room(log, "upstairs hall")
    _move(log, entities.PLAYER, bedroom)
    _move(log, yara, bedroom)
    _move(log, seraphine, hall)

    assert active_members(log, SID) == ([yara], "presence")

    _move(log, seraphine, bedroom)
    assert active_members(log, SID) == ([yara, seraphine], "presence")


def test_a_provisional_member_is_still_not_laned_in_the_shared_room(log):
    """The registry rung answers presence, never eligibility (§8 + W9.22 tiers)."""
    woman = _admit(log, "the woman", descriptor=True)
    bedroom = _room(log, "master bedroom")
    _move(log, entities.PLAYER, bedroom)
    _move(log, woman, bedroom)

    assert active_members(log, SID) == ([], "none")


# -- the precedence against the legacy walk ----------------------------------


def test_a_registry_placement_wins_over_a_stale_legacy_venue(log):
    """The walk still says "present in tavern"; the registry moved her out."""
    yara = _admit(log, "Yara")
    bedroom = _room(log, "master bedroom")
    bathroom = _room(log, "bathroom")
    _move(log, entities.PLAYER, bedroom)
    _move(log, yara, bathroom)
    # A pre-regime delta, oldest evidence, still in the log's walk.
    _world_state_venue(log, {"Yara": "tavern"})
    walk = presence_walk(log, SID)
    assert walk.is_present("Yara") is True

    assert active_members(log, SID) == ([], "none")


def test_a_member_without_an_assignment_keeps_the_legacy_walk(log):
    """Forward-only: a being the registry never placed is the walk's answer."""
    yara = _admit(log, "Yara")
    bedroom = _room(log, "master bedroom")
    _move(log, entities.PLAYER, bedroom)
    _world_state_venue(log, {"Yara": "tavern"})

    assert active_members(log, SID) == ([yara], "presence")


def test_without_a_player_room_the_registry_cannot_answer_and_the_walk_does(log):
    """No player assignment ⇒ nothing to compare, so no member is "exited"."""
    yara = _admit(log, "Yara")
    bedroom = _room(log, "master bedroom")
    _move(log, yara, bedroom)
    _world_state_venue(log, {"Yara": "tavern"})

    assert active_members(log, SID) == ([yara], "presence")


# -- the dispatch description names the registry room ------------------------


def test_the_presence_fact_names_the_registry_location(log):
    """D88's candidate description is a FACT, now backed by a location object."""
    yara = _admit(log, "Yara")
    bedroom = _room(log, "master bedroom")
    _move(log, entities.PLAYER, bedroom)
    _move(log, yara, bedroom)

    walk = presence_walk(log, SID)
    registry = entities.registry(log, SID)
    assert _presence_fact(walk, _member(log, "Yara"), registry) == (
        "Present in master bedroom."
    )


def test_the_presence_fact_falls_back_to_the_walk_venue(log):
    """The registry has no placement, so the last stated venue is the answer."""
    _admit(log, "Yara")
    _world_state_venue(log, {"Yara": "tavern"})

    walk = presence_walk(log, SID)
    registry = entities.registry(log, SID)
    assert _presence_fact(walk, _member(log, "Yara"), registry) == "Present in tavern."


def test_the_presence_fact_never_says_off_screen_is_a_room(log):
    """The absence bucket is not a place anyone is "present in"."""
    yara = _admit(log, "Yara")
    bedroom = _room(log, "master bedroom")
    _move(log, entities.PLAYER, bedroom)
    _move(log, yara, entities.OFF_SCREEN, at=bedroom)

    walk = presence_walk(log, SID)
    registry = entities.registry(log, SID)
    assert _presence_fact(walk, _member(log, "Yara"), registry) == "In the scene."


# -- W9.32 / R3: the player is a NODE, not a cast member ---------------------
#
# The persona is the player: identity ``player``, the persona name is that node's
# display name. W3 keeps the name out of the roster at both cast doors, so the
# lane read side has nothing to subtract — these pins cover the presence
# consequence, and the ONE exemption (User-as-Actor's ``user`` member, which IS
# the player's participation layer).


def _assign_persona(log: EventLog, name: str) -> str:
    """Give ``SID`` a persona — the chat's player node, displayed as ``name``."""
    from fabulist.personas.registry import append_persona, assign_persona

    persona_id = append_persona(log, {"name": name})
    assign_persona(log, SID, persona_id)
    return persona_id


def test_the_persona_is_never_a_member_and_never_owns_a_lane(log):
    """The persona's seed card renders, but mints no member to lane."""
    _assign_persona(log, "Wanderer")
    log.append_event(
        SID, "system", "system", {"card": {"name": "Wanderer"}, "first_mes": ""}
    )
    yara = _admit(log, "Yara")
    # The legacy walk still names the persona: a member that does not exist
    # cannot be laned, so the turn stays NPC-only.
    _world_state_venue(log, {"Wanderer": "tavern", "Yara": "tavern"})

    assert member_by_name(roster(log, SID), "Wanderer") is None
    assert active_members(log, SID) == ([yara], "presence")


def test_the_user_actor_is_the_one_member_the_persona_name_may_admit(log):
    """User-as-Actor's ``user`` member is the player's participation layer."""
    _assign_persona(log, "Wanderer")
    user_id = admit_member(
        log, SID, "Wanderer", provenance="user", admitted_by="user"
    )
    bedroom = _room(log, "master bedroom")
    _move(log, entities.PLAYER, bedroom)
    _move(log, user_id, bedroom)

    assert active_members(log, SID) == ([user_id], "presence")
    member = member_by_name(roster(log, SID), "Wanderer")
    assert member is not None
    assert member.provenance == "user"
