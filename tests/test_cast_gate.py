"""Cast admission is USER-GATED (owner ruling, reversing D76's auto-promotion).

Owner policy under test: "we will no longer allow auto promotion to cast. It must
be accepted by the user." Three seams carry it, and this file pins all three:

1. **detection is not admission** — BTS detects a person and files a ``cast``
   proposal (``bts._emit_cast_proposal`` -> Lane L4's pinned
   ``state.proposals.emit_cast_proposal(session_id, name, aliases, origin)``);
   no ``cast_admit`` row, no member, no lane;
2. **the accept** — ``cast.admit_from_proposal`` is the one admission: provenance
   ``actor`` (the only lane-eligible tier), ``admitted_by="owner"``, plus one
   ``cast_alias`` row per reported spelling;
3. **the undo** — ``cast.retire_from_proposal`` is the compensating tombstone, and
   a later accept resurrects the same ``member_id`` (D10).

Plus the two side-effects of the same policy: the player's ROLE name
(``Protagonist``) is a player surface, and the env HUD can no longer be broken by
a verbose weather value.
"""

from __future__ import annotations

import pytest

from fabulist.log import EventLog
from fabulist.state import bts as bts_module
from fabulist.state.cast import (
    CAST_ADMIT_EVENT,
    CAST_ALIAS_EVENT,
    CAST_RETIRE_EVENT,
    admit_from_proposal,
    ensure_roster,
    member_by_id,
    roster,
    retire_from_proposal,
    runtime_notice,
)
from fabulist.state.env_hud import (
    HUD_PHRASE_MAX,
    env_hud_statement,
    env_hud_statement_from_fold,
    parse_env_hud_statement,
)
from fabulist.state.presence import PLAYER_SURFACES, active_members

SID = "s1"


@pytest.fixture
def log() -> EventLog:
    opened = EventLog(":memory:")
    yield opened
    opened.close()


@pytest.fixture
def cast_proposals(monkeypatch) -> list[tuple[str, str, list[str], str]]:
    """Capture what the BTS door hands Lane L4's emitter."""
    filed: list[tuple[str, str, list[str], str]] = []

    def _emit(log, session_id, name, aliases, origin):
        filed.append((session_id, name, list(aliases), origin))

    from fabulist.state import proposals as proposal_store

    monkeypatch.setattr(proposal_store, "emit_cast_proposal", _emit, raising=False)
    return filed


def _events(log: EventLog, kind: str) -> list:
    return [e for e in log.get_events(SID) if e.payload.get("event") == kind]


def _seed_card(log: EventLog, name: str) -> None:
    """Seed one card character and materialise the roster (as the graph does)."""
    log.append_event(SID, "system", "system", {"card": {"name": name}, "first_mes": ""})
    ensure_roster(log, SID)


def _world_state(log: EventLog, names: list[str]) -> None:
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {name: {"emotional": {"mood": "wary"}} for name in names},
            "venue": {name: {"venue": "shop"} for name in names},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )


# -- 1. detection files a proposal and admits nothing -----------------------


def test_a_detected_person_is_a_proposal_and_never_a_member(log, cast_proposals):
    _seed_card(log, "Aria")

    bts_module.apply_bts_cast_proposals(
        log,
        SID,
        {
            "state": {"Marisol": {"physical": {"pose": "behind the counter"}}},
            "venue": {"Marisol": {"venue": "Vale & Co."}},
            "references": [{"kind": "identity", "name": "Marisol"}],
        },
    )

    assert [m.display_name for m in roster(log, SID)] == ["Aria"]
    assert [e for e in _events(log, CAST_ADMIT_EVENT) if e.payload["name"] == "Marisol"] == []
    assert cast_proposals == [(SID, "Marisol", [], "bts")]


def test_the_unaccepted_candidate_owns_no_lane(log, cast_proposals):
    """The whole point of the gate: the engine does not role-play a stranger."""
    _seed_card(log, "Aria")
    bts_module.apply_bts_cast_proposals(
        log, SID, {"state": {"Marisol": {}}, "references": [{"kind": "identity", "name": "Marisol"}]}
    )
    _world_state(log, ["Aria", "Marisol"])

    lanes, source = active_members(log, SID)

    aria = member_by_id(roster(log, SID), [m.member_id for m in roster(log, SID)][0])
    assert (lanes, source) == ([aria.member_id], "presence")
    assert [m.display_name for m in roster(log, SID)] == ["Aria"]


def test_a_later_sighting_of_the_same_person_files_the_proposal_again(log, cast_proposals):
    """The store's same-target SUPERSEDE collapses the repeats (finding 11)."""
    _seed_card(log, "Aria")
    payload = {
        "state": {"Marisol": {}},
        "references": [{"kind": "identity", "name": "Marisol"}],
    }
    bts_module.apply_bts_cast_proposals(log, SID, payload)
    bts_module.apply_bts_cast_proposals(log, SID, payload)

    assert cast_proposals == [(SID, "Marisol", [], "bts"), (SID, "Marisol", [], "bts")]


def test_an_unavailable_emitter_never_fails_the_door(log):
    """Lane L4's module may not be in the tree yet: detect, file nothing, move on."""
    _seed_card(log, "Aria")

    applied = bts_module.apply_bts_cast_proposals(
        log, SID, {"state": {"Marisol": {}}, "references": [{"kind": "identity", "name": "Marisol"}]}
    )

    assert [ref.name for ref in applied] == ["Marisol"]
    assert [m.display_name for m in roster(log, SID)] == ["Aria"]


def test_an_accepted_member_is_resolved_not_re_proposed(log, cast_proposals):
    """The door still RESOLVES: an accepted person stays one member, no proposal."""
    accepted = admit_from_proposal(log, SID, "Marisol", ["Marisol (the clerk)"], "bts")
    cast_proposals.clear()

    bts_module.apply_bts_cast_proposals(
        log,
        SID,
        {
            "state": {"Marisol": {}},
            "references": [{"kind": "identity", "name": "marisol"}],
        },
    )

    assert cast_proposals == []
    members = roster(log, SID)
    assert [m.member_id for m in members] == [accepted]
    assert "Marisol (the clerk)" in members[0].aliases


def test_a_rename_of_an_unaccepted_person_names_the_proposal(log, cast_proposals):
    bts_module.apply_bts_cast_proposals(
        log,
        SID,
        {
            "state": {"Marisol": {}},
            "references": [{"kind": "identity", "name": "Marisol", "rename_to": "Zahra"}],
        },
    )

    assert cast_proposals == [(SID, "Zahra", ["Marisol"], "bts")]


# -- 2. the accept is the one admission -------------------------------------


def test_admit_from_proposal_admits_an_actor_owned_by_the_owner(log):
    member_id = admit_from_proposal(log, SID, "Marisol", [], "bts")

    member = member_by_id(roster(log, SID), member_id)
    assert member.display_name == "Marisol"
    assert member.provenance == "actor"
    assert member.admitted_by == "owner"
    assert not member.provisional
    assert _events(log, CAST_ADMIT_EVENT)[-1].payload["admitted_by"] == "owner"


def test_admit_from_proposal_binds_each_reported_spelling_as_one_alias(log):
    """One ``cast_alias`` row per spelling — bar the ones the fold already covers.

    ``marisol`` is skipped on purpose: its canonical key IS the display name, so
    resolution already answers it (an alias row would make every read carry a
    duplicate surface form).
    """
    member_id = admit_from_proposal(
        log, SID, "Marisol", ["marisol", "Marisol (clerk)"], "bts"
    )

    aliases = [e.payload["alias"] for e in _events(log, CAST_ALIAS_EVENT)]
    assert aliases == ["Marisol (clerk)"]
    assert all(e.payload["member_id"] == member_id for e in _events(log, CAST_ALIAS_EVENT))
    assert [m.member_id for m in roster(log, SID)] == [member_id]


def test_admit_from_proposal_skips_the_display_name_and_a_taken_spelling(log):
    """An alias is a merge claim: never the name itself, never another member's."""
    other = admit_from_proposal(log, SID, "Marcus", [], "bts")

    admit_from_proposal(log, SID, "Marisol", ["Marisol", "marcus"], "bts")

    assert [e.payload["alias"] for e in _events(log, CAST_ALIAS_EVENT)] == []
    marcus = member_by_id(roster(log, SID), other)
    assert marcus.aliases == ()


def test_admit_from_proposal_reuses_an_existing_seed_member(log):
    """A card character re-proposed mints no twin and keeps its seed provenance."""
    _seed_card(log, "Aria")

    member_id = admit_from_proposal(log, SID, "Aria", [], "promotion")

    members = roster(log, SID)
    assert [m.member_id for m in members] == [member_id]
    assert member_by_id(members, member_id).provenance == "card"


def test_admit_from_proposal_re_tiers_a_presence_member_the_engine_already_knows(log):
    """The live case: the attribution pass admitted the speaker as ``extractor``.

    Accepting the proposal must make the SAME member lane-eligible — a presence
    tier member the accept left untouched would make the owner's decision inert.
    """
    from fabulist.state.cast import admit_member

    learned = admit_member(
        log, SID, "Marisol", provenance="extractor", admitted_by="attribution"
    )

    accepted = admit_from_proposal(log, SID, "Marisol", [], "bts")

    assert accepted == learned
    member = member_by_id(roster(log, SID), learned)
    assert (member.provenance, member.admitted_by) == ("actor", "attribution")
    assert [m.member_id for m in roster(log, SID)] == [learned]


def test_admit_from_proposal_rejects_an_empty_name(log):
    with pytest.raises(ValueError):
        admit_from_proposal(log, SID, "  ", [], "bts")


def test_the_accepted_member_rides_the_dynamic_end_notice(log):
    admit_from_proposal(log, SID, "Marisol", [], "bts")

    notice = runtime_notice(log, SID)
    assert notice is not None and "Marisol" in notice


# -- 3. the undo is a compensating retire -----------------------------------


def test_retire_from_proposal_tombstones_the_accepted_member(log):
    member_id = admit_from_proposal(log, SID, "Marisol", [], "bts")

    assert retire_from_proposal(log, SID, "Marisol") == member_id

    retired = member_by_id(roster(log, SID), member_id)
    assert retired is not None and not retired.is_active
    assert _events(log, CAST_RETIRE_EVENT)[-1].payload["member_id"] == member_id


def test_retire_from_proposal_resolves_an_alias(log):
    member_id = admit_from_proposal(log, SID, "Marisol", ["marisol"], "bts")

    assert retire_from_proposal(log, SID, "marisol") == member_id
    assert not member_by_id(roster(log, SID), member_id).is_active


def test_retire_from_proposal_is_a_no_op_when_nobody_carries_the_name(log):
    assert retire_from_proposal(log, SID, "Ghost") is None


def test_a_second_accept_resurrects_the_same_member(log):
    """Undo is a tombstone, not a delete (D10): accept -> undo -> accept."""
    first = admit_from_proposal(log, SID, "Marisol", [], "bts")
    retire_from_proposal(log, SID, "Marisol")

    second = admit_from_proposal(log, SID, "Marisol", [], "bts")

    assert second == first
    member = member_by_id(roster(log, SID), first)
    assert member.is_active and member.provenance == "actor"


# -- 4. the player's ROLE name is a player surface --------------------------


def test_protagonist_is_a_player_surface():
    """Live: the persona 'Protagonist' was admitted and LANED."""
    assert "protagonist" in PLAYER_SURFACES
    assert isinstance(PLAYER_SURFACES, frozenset)
    # The pre-existing player words keep their contract.
    assert {"user", "player", "you"} <= PLAYER_SURFACES


def test_a_protagonist_header_is_refused_at_the_door(log, cast_proposals):
    """The tracker spelling the player by role never becomes a proposal either."""
    bts_module.apply_bts_cast_proposals(
        log,
        SID,
        {"state": {"Protagonist": {}}, "references": [{"kind": "identity", "name": "Protagonist"}]},
    )

    assert cast_proposals == []
    assert roster(log, SID) == []


# -- 5. the env HUD cannot be broken by a verbose value ---------------------


def test_a_verbose_weather_value_is_capped_and_still_parses():
    verbose = (
        "Cool grey overcast with fine drizzle, around 13°C, low valley haze; "
        "streets damp and cafe tables outside the jewellery store empty"
    )

    statement = env_hud_statement(
        {"time": "08:10", "date": "Monday, June 02, 2025", "loc": "Vale & Co.", "weather": verbose},
        {"forecast": "Drizzle easing by late morning, cloud thinning to broken sun"},
    )
    parsed = parse_env_hud_statement(statement)

    assert parsed is not None
    assert len(parsed["weather"]) <= HUD_PHRASE_MAX + 1  # + the ellipsis
    assert len(parsed["forecast"]) <= HUD_PHRASE_MAX + 1
    assert parsed["weather"].endswith("…")
    # Word-boundary truncation: no half word, no trailing comma.
    assert not parsed["weather"].rstrip("…").endswith((",", " ", ";"))


def test_a_brief_weather_value_is_left_alone():
    statement = env_hud_statement(
        {"time": "08:10", "date": "", "loc": "gate", "weather": "fine drizzle"}
    )
    assert parse_env_hud_statement(statement)["weather"] == "fine drizzle"


def test_the_legacy_fold_statement_caps_the_weather_too():
    statement = env_hud_statement_from_fold(
        {"time": "08:10", "loc": "gate", "weather": "x" * (HUD_PHRASE_MAX * 2)}
    )
    parsed = parse_env_hud_statement(statement)
    assert parsed is not None
    assert len(parsed["weather"]) <= HUD_PHRASE_MAX + 1


def test_the_steady_forecast_ladder_still_holds_the_capped_weather():
    """No forecast known -> the HUD holds the (capped) current weather."""
    long_weather = " ".join(["drizzle"] * 20)
    statement = env_hud_statement(
        {"time": "08:10", "date": "", "loc": "gate", "weather": long_weather}
    )
    parsed = parse_env_hud_statement(statement)
    assert parsed is not None and parsed["forecast"] == parsed["weather"]
