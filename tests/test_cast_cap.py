"""W9.15/W9.22: the cast cap — two budgets (actor tier never pays).

W9.15 capped the ACTIVE cast at 8 members, LRU by appearance. W9.21-D pinned the
SEED provenances out of eviction after a live session retired both card
characters mid-scene. W9.22 §2 generalises the pin into two BUDGETS: the actor
tier (``card`` / ``world`` / ``actor`` / ``user``) is never an eviction candidate,
and the presence tier (learned in play) is LRU-bounded by ``cap - actors``.
"""

from __future__ import annotations

import pytest

from fabulist.log import EventLog
from fabulist.state.cast import (
    CAST_RETIRE_EVENT,
    DEFAULT_CAST_CAP,
    TIER_ACTOR,
    TIER_PRESENCE,
    CastMember,
    admit_member,
    enforce_cast_cap,
    member_tier,
    roster,
)
from fabulist.state.presence import ACTOR_PROVENANCE

SID = "s-cap"


@pytest.fixture
def log(tmp_path):
    event_log = EventLog(tmp_path / "events.db")
    try:
        yield event_log
    finally:
        event_log.close()


def _admit(log, name: str) -> str:
    return admit_member(log, SID, name, provenance="bts", admitted_by="bts")


def test_cap_is_eight():
    assert DEFAULT_CAST_CAP == 8


def test_no_eviction_at_or_below_the_cap(log):
    ids = [_admit(log, f"NPC {index}") for index in range(8)]
    assert enforce_cast_cap(log, SID, last_seen={mid: 10 + i for i, mid in enumerate(ids)}) == []
    assert len([member for member in roster(log, SID) if member.is_active]) == 8


def test_ninth_member_retires_the_least_recently_seen(log):
    ids = [_admit(log, f"NPC {index}") for index in range(9)]
    # NPC 3 has never been seen (no entry) -> oldest -> must go.
    last_seen = {mid: 50 + index for index, mid in enumerate(ids) if index != 3}
    retired = enforce_cast_cap(log, SID, last_seen=last_seen)
    assert retired == [ids[3]]
    active = [member.member_id for member in roster(log, SID) if member.is_active]
    assert len(active) == 8 and ids[3] not in active


def test_oldest_seen_member_is_retired_before_newer_ones(log):
    ids = [_admit(log, f"NPC {index}") for index in range(10)]
    last_seen = {mid: 100 + index for index, mid in enumerate(ids)}
    retired = enforce_cast_cap(log, SID, last_seen=last_seen)
    assert retired == [ids[0], ids[1]]


def test_retirement_is_recoverable_by_a_later_admit(log):
    ids = [_admit(log, f"NPC {index}") for index in range(9)]
    last_seen = {mid: 10 + index for index, mid in enumerate(ids) if index != 0}
    retired = enforce_cast_cap(log, SID, last_seen=last_seen)
    assert retired == [ids[0]]
    # A later identity admit resurrects the same member id (W8.9 semantics).
    assert _admit(log, "NPC 0") == ids[0]
    assert any(member.member_id == ids[0] and member.is_active for member in roster(log, SID))


def test_player_surfaces_never_own_a_lane(log):
    """W9.16: BTS headers say 'User'; the player must never lane (live session
    a live session laned its own 'User' member)."""
    from fabulist.state.cast import admit_member
    from fabulist.state.presence import _eligible

    player = admit_member(log, SID, "User", provenance="bts", admitted_by="bts")
    # ACTOR tier: the player subtraction is what this pins, not the cast gate.
    npc = admit_member(log, SID, "Hobbs", provenance="card", admitted_by="card")
    ids = [member.member_id for member in _eligible(roster(log, SID), None)]
    assert player not in ids
    assert npc in ids


# -- the seed pin (live RCA) --------------------------------------------------
#
# That session's cap retired BOTH card characters mid-scene ("Hobbs" at seq 86,
# "Tessa" at seq 117) while seven members play invented stayed active: the seed
# is not the least-recently-SEEN, it is the least-recently-HEADERED, and BTS is
# delta-first. A member the owner created is never the eviction candidate.


def _seed_cards(log, *names: str) -> dict[str, str]:
    for name in names:
        log.append_event(
            SID, "system", "system", {"card": {"name": name}, "first_mes": ""}
        )
    from fabulist.state.cast import ensure_roster

    return {member.display_name: member.member_id for member in ensure_roster(log, SID)}


def test_card_members_are_pinned_when_the_cap_squeezes(log):
    seeds = _seed_cards(log, "Hobbs", "Tessa")
    junk = [_admit(log, f"NPC {index}") for index in range(7)]
    retired = enforce_cast_cap(
        log, SID, last_seen={mid: 100 + index for index, mid in enumerate(junk)}
    )
    # Nine active, eight allowed: the one member nobody ever named pays.
    assert retired == [junk[0]]
    active = {member.member_id for member in roster(log, SID) if member.is_active}
    assert seeds["Hobbs"] in active and seeds["Tessa"] in active


def test_learned_members_pay_for_the_cap_and_the_seed_never_does(log):
    seeds = _seed_cards(log, *(f"Card {index}" for index in range(6)))
    junk = [_admit(log, f"NPC {index}") for index in range(3)]
    retired = enforce_cast_cap(
        log, SID, last_seen={mid: 50 + index for index, mid in enumerate(junk)}
    )
    # The cap is 8 and six of those slots belong to the seed, so play gets two.
    assert retired == [junk[0]]
    active = {member.member_id for member in roster(log, SID) if member.is_active}
    assert set(seeds.values()) <= active


def test_a_seed_roster_over_the_cap_retires_nothing(log):
    """The cap is never paid for with the characters the owner created."""
    seeds = _seed_cards(log, *(f"Card {index}" for index in range(9)))
    assert enforce_cast_cap(log, SID) == []
    assert all(
        member.is_active for member in roster(log, SID) if member.member_id in seeds.values()
    )


# -- W9.22 §2: two budgets — the actor tier never pays ----------------------
#
# The W9.21-D pin was keyed on the SEED provenances (card/world). W9.22 §2
# generalises it to the whole actor tier and gives the presence tier the only
# budget left over. The tests below state the rule in both directions: an actor
# never pays even when it is the STALEST member on the roster, and a presence
# member pays even when it is the most recently seen one.


def test_actor_provenance_is_the_contract_constant():
    """The cross-lane contract (W9.22 §2): the four actor provenances, no more."""
    assert ACTOR_PROVENANCE == frozenset({"card", "world", "actor", "user"})


@pytest.mark.parametrize("provenance", ["card", "world", "actor", "user"])
def test_the_actor_tier_is_the_actor_provenances(provenance):
    assert member_tier(CastMember("cm_x", "X", provenance=provenance)) == TIER_ACTOR


@pytest.mark.parametrize("provenance", ["bts", "extractor", "unknown"])
def test_a_learned_member_is_the_presence_tier(provenance):
    assert member_tier(CastMember("cm_x", "X", provenance=provenance)) == TIER_PRESENCE


def test_a_promoted_actor_is_pinned_like_a_card_member(log):
    """A promotion re-tiers by provenance, so the W9.21-D pin carries over.

    Live sessions lose card characters to an LRU clock; a promoted actor is the
    same kind of member one promotion later, so it must be the same kind of
    pinned.
    """
    promoted = admit_member(log, SID, "Millie", provenance="actor", admitted_by="gate")
    junk = [_admit(log, f"NPC {index}") for index in range(8)]
    retired = enforce_cast_cap(
        log, SID, last_seen={mid: 900 + index for index, mid in enumerate(junk)}
    )
    assert retired == [junk[0]]  # the presence tier pays, never the actor
    active = {member.member_id for member in roster(log, SID) if member.is_active}
    assert promoted in active


def test_the_user_actor_is_never_cap_evicted(log):
    """User-as-Actor (provenance ``user``) is an ACTOR: it never pays the cap."""
    user = admit_member(log, SID, "Zero", provenance="user", admitted_by="user")
    junk = [_admit(log, f"NPC {index}") for index in range(8)]
    retired = enforce_cast_cap(
        log, SID, last_seen={mid: 700 + index for index, mid in enumerate(junk)}
    )
    assert retired == [junk[0]]
    assert any(
        member.member_id == user and member.is_active for member in roster(log, SID)
    )


def test_the_presence_budget_is_the_cap_minus_the_actor_tier(log):
    """Actors FILL the cap; the leftover is the whole presence budget.

    Eight card actors leave a presence budget of zero, so every learned member
    pays — even the most recently seen one — while not a single actor is
    touched.
    """
    seeds = _seed_cards(log, *(f"Card {index}" for index in range(8)))
    learned = [_admit(log, f"NPC {index}") for index in range(3)]
    retired = enforce_cast_cap(
        log, SID, last_seen={mid: 10 + index for index, mid in enumerate(learned)}
    )
    assert retired == learned
    active = {member.member_id for member in roster(log, SID) if member.is_active}
    assert set(seeds.values()) <= active
    assert not (set(learned) & active)
    # The tombstone says which budget paid, so an inspection can tell them apart.
    reasons = [
        event.payload["reason"]
        for event in log.get_events(SID)
        if event.payload.get("event") == CAST_RETIRE_EVENT
    ]
    assert reasons and all("presence" in reason for reason in reasons)


def test_an_actor_roster_over_the_cap_retires_nothing(log):
    """Nine PROMOTED actors: the cap is never paid for with the owner's cast."""
    actors = [
        admit_member(log, SID, f"Actor {index}", provenance="actor", admitted_by="gate")
        for index in range(9)
    ]
    assert enforce_cast_cap(log, SID) == []
    assert all(
        member.is_active for member in roster(log, SID) if member.member_id in actors
    )
