"""W7 canonical cast identity: one member per person, rename-safe, split-on-doubt.

Covers the four hard requirements of the W7 backend lane:

* a BTS-discovered name is **admitted** (canonical id) and gets a colour;
* a **rename patches the same member**, keeps its colour and demotes the old
  name to an alias (ADDENDUM 1);
* a **descriptor** ("the woman") is scene/presence-scoped: a new scene yields a
  SECOND member, and an ambiguous reuse is refused rather than merged
  (ADDENDUM 2/3);
* a **fork inherits members up to its cut** and nothing after it.

It also pins the cache contract: admitting or renaming a member never changes the
STABLE PREFIX (the ``{{char}}`` / card block), because the change rides the
dynamic END notice instead (AGENTS.md non-negotiable 6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.api.cast_api import (
    SPEAKER_PALETTE,
    assign_member_color,
    list_member_colors,
)
from fabulist.log import EventLog
from fabulist.log.schema_gate import LogSchemaError, validate_event
from fabulist.prompts.cast import cast_delta_notice, ensure_cast_snapshot
from fabulist.prompts.variables import resolve_variables
from fabulist.state.cast import (
    CAST_ADMIT_EVENT,
    CAST_ALIAS_EVENT,
    CAST_AMBIGUOUS_EVENT,
    CAST_LINK_EVENT,
    CAST_PATCH_EVENT,
    CAST_RETIRE_EVENT,
    REFERENCE_DESCRIPTOR,
    admit_member,
    canonical_key,
    ensure_roster,
    member_by_id,
    patch_member,
    TIER_ACTOR,
    TIER_PRESENCE,
    member_tier,
    patch_member,
    resolve_descriptor,
    retire_member,
    roster,
    runtime_notice,
)

ARIA = "Aria"


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


def _seed_card(log: EventLog, sid: str, name: str) -> None:
    log.append_event(sid, "system", "system", {"card": {"name": name}, "first_mes": ""})


def _events(log: EventLog, sid: str, kind: str) -> list:
    return [e for e in log.get_events(sid) if e.payload.get("event") == kind]


# -- canonical_key: the write-time alias index -------------------------------


def test_canonical_key_collapses_spellings():
    assert canonical_key("Clerk") == canonical_key("clerk") == canonical_key("  the clerk ")
    assert canonical_key("Zahra") == "zahra"
    # A single leading article is dropped only when a token survives.
    assert canonical_key("The") == "the"
    assert canonical_key("The Doctor") == "doctor"


def test_canonical_key_counts_separators_as_spaces():
    """Underscore/hyphen spellings are ONE surface (the live ``the_courier`` split).

    A notation or state-header spelling used to mint a second member beside the
    same person; the key is the alias index, so the fix belongs here.
    """
    assert canonical_key("the_courier") == canonical_key("the courier") == "courier"
    assert canonical_key("the-courier") == canonical_key("the courier")
    assert canonical_key("child-of-the-storm") == canonical_key("child of the storm")
    assert canonical_key("  Kael  ") == "kael"
    assert canonical_key("KAEL_9") == canonical_key("kael 9") == "kael 9"


def test_underscore_surface_resolves_to_one_member(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    first = admit_member(log, sid, "the courier", provenance="bts", admitted_by="bts")
    # The live failure: a header spelled "the_courier" minted its own member.
    second = admit_member(log, sid, "the_courier", provenance="bts", admitted_by="bts")
    assert first == second
    members = roster(log, sid)
    assert [m.display_name for m in members] == [ARIA, "the courier"]
    assert "the_courier" in member_by_id(members, first).aliases


# -- admit: the single path, deterministic id --------------------------------


def test_bts_discovered_name_is_admitted_and_coloured(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    arias = roster(log, sid)
    assert [m.display_name for m in arias] == [ARIA]
    assert arias[0].provenance == "card"
    # Deterministic seed id: same log -> same handle (no stored counter).
    assert arias[0].member_id == ensure_roster(log, sid)[0].member_id

    # A BTS-learned character enters through the SAME admit path.
    kesh_id = admit_member(log, sid, "Kesh", provenance="bts", admitted_by="bts")
    members = roster(log, sid)
    assert [m.display_name for m in members] == [ARIA, "Kesh"]
    assert member_by_id(members, kesh_id).provenance == "bts"
    assert member_by_id(members, kesh_id).admitted_by == "bts"

    # Both get a colour, keyed by member_id, in roster order.
    assert assign_member_color(log, sid, arias[0].member_id, ARIA) == SPEAKER_PALETTE[0]
    assert assign_member_color(log, sid, kesh_id, "Kesh") == SPEAKER_PALETTE[1]
    colors = list_member_colors(log, sid)
    assert colors[arias[0].member_id] == SPEAKER_PALETTE[0]
    assert colors[kesh_id] == SPEAKER_PALETTE[1]


def test_reading_the_roster_twice_appends_no_second_admit(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    ensure_roster(log, sid)
    assert len(_events(log, sid, CAST_ADMIT_EVENT)) == 1


def test_spelling_variants_resolve_to_one_member(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, "Clerk")
    ensure_roster(log, sid)

    first = admit_member(log, sid, "Clerk", provenance="bts", admitted_by="bts")
    second = admit_member(log, sid, "clerk", provenance="bts", admitted_by="bts")
    third = admit_member(log, sid, "the clerk", provenance="extractor", admitted_by="bts")
    assert first == second == third
    # Exactly one member, and the novel surface forms are persisted as aliases.
    assert len(roster(log, sid)) == 1
    assert len(_events(log, sid, CAST_ALIAS_EVENT)) == 2
    assert len(_events(log, sid, CAST_ADMIT_EVENT)) == 1


# -- cache contract: admit / rename never touch the stable prefix ------------


def test_admit_and_rename_leave_the_stable_prefix_byte_identical(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    prefix = ensure_cast_snapshot(log, sid)
    assert prefix == "[cast: Aria]"

    mid = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")
    patch_member(log, sid, mid, display_name="Zahra", reason="she gives her name")

    # The frozen prefix is UNCHANGED; the correction rides the dynamic notice.
    assert resolve_variables(log, sid, "hi")["char"] == "[cast: Aria]"
    notice = cast_delta_notice(log, sid)
    assert notice is not None
    assert '"Woman" is Zahra' in notice


# -- rename (ADDENDUM 1): same member, same colour, old name -> alias ---------


def test_rename_keeps_the_same_colour_and_demotes_the_old_name(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    arias = roster(log, sid)[0]

    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")
    assert assign_member_color(log, sid, arias.member_id, ARIA) == SPEAKER_PALETTE[0]
    color_before = assign_member_color(log, sid, woman_id, "Woman")
    assert color_before == SPEAKER_PALETTE[1]

    same_id = patch_member(
        log, sid, woman_id, display_name="Zahra", reason="she gives her name"
    )
    assert same_id == woman_id  # member_id is IMMUTABLE

    member = member_by_id(roster(log, sid), woman_id)
    assert member.display_name == "Zahra"
    assert "Woman" in member.aliases
    # The colour registry is keyed by member_id, so the hue is unchanged.
    assert list_member_colors(log, sid)[woman_id] == color_before
    # A re-assign short-circuits: no second colour row.
    assert assign_member_color(log, sid, woman_id, "Zahra") == color_before

    # Old text still resolves to the SAME member (aliases resolve backward).
    assert admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts") == woman_id
    assert admit_member(log, sid, "the woman", provenance="bts", admitted_by="bts") == woman_id
    assert len(roster(log, sid)) == 2

    notice = runtime_notice(log, sid)
    assert notice is not None and '"Woman"' in notice and "Zahra" in notice


def test_rename_that_would_collide_is_refused_and_logged_ambiguous(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, "Kael")
    ensure_roster(log, sid)
    kael_id = roster(log, sid)[0].member_id
    other_id = admit_member(log, sid, "Zahra", provenance="bts", admitted_by="bts")

    with pytest.raises(ValueError):
        patch_member(log, sid, kael_id, display_name="Zahra")

    # Refused: never a silent merge.
    assert member_by_id(roster(log, sid), kael_id).display_name == "Kael"
    assert member_by_id(roster(log, sid), other_id).display_name == "Zahra"
    ambiguous = _events(log, sid, CAST_AMBIGUOUS_EVENT)
    assert ambiguous and ambiguous[-1].payload["candidates"] == [other_id]


# -- descriptors (ADDENDUM 2/3): presence-scoped, never aliased --------------


def test_descriptor_same_presence_reuses_the_member(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    first = admit_member(
        log,
        sid,
        "the woman",
        provenance="bts",
        admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
    )
    assert member_by_id(roster(log, sid), first).provisional is True
    # Same scene, exactly one live candidate -> the same member.
    assert resolve_descriptor(log, sid, "the woman", presence=[first]) == first
    again = admit_member(
        log,
        sid,
        "the woman",
        provenance="bts",
        admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
        presence=[first],
    )
    assert again == first
    assert len(roster(log, sid)) == 2  # Aria + the woman


def test_descriptor_in_a_new_scene_is_a_second_member(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    first = admit_member(
        log,
        sid,
        "the woman",
        provenance="bts",
        admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
    )
    # New scene with no presence (ADDENDUM 3 Q2: presence, no timer).
    assert resolve_descriptor(log, sid, "the woman", presence=[]) is None
    second = admit_member(
        log,
        sid,
        "the woman",
        provenance="bts",
        admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
        presence=[],
    )
    assert second != first
    members = roster(log, sid)
    assert len(members) == 3
    # The first member is untouched, and the new one is disambiguated.
    assert member_by_id(members, first).display_name == "the woman"
    assert member_by_id(members, second).display_name == "the woman (2)"
    # A descriptor is NEVER auto-aliased onto the member.
    assert "the woman" not in member_by_id(members, second).aliases


def test_ambiguous_descriptor_reuse_refuses_merge(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    first = admit_member(
        log, sid, "the woman", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
    )
    second = admit_member(
        log, sid, "the woman", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR, presence=[],
    )
    # Two live candidates at once: refuse, log, and split.
    assert resolve_descriptor(log, sid, "the woman", presence=[first, second]) is None
    third = admit_member(
        log, sid, "the woman", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR, presence=[first, second],
    )
    assert third not in {first, second}
    ambiguous = _events(log, sid, CAST_AMBIGUOUS_EVENT)
    assert ambiguous
    assert set(ambiguous[-1].payload["candidates"]) == {first, second}


def test_descriptor_conflict_signal_forces_a_new_member(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    first = admit_member(
        log, sid, "the woman", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
    )
    # "a different woman" is a typed conflict signal: never a merge.
    second = admit_member(
        log, sid, "the woman", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR, presence=[first], conflict=True,
    )
    assert second != first


def test_first_sight_descriptor_bridges_to_a_present_identity_member(build_log):
    """W8.11 FIX 2 at the admit seam: no link yet, one canonical identity match.

    The typed descriptor path must not be WORSE than the canonical match it
    replaced: "the woman" while the member "Woman" is present used to resolve to
    her. The bridge writes a ``cast_link`` — never an alias (ADDENDUM 2 A9) — and
    mints nothing.
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")

    bridged = admit_member(
        log,
        sid,
        "the woman",
        provenance="bts",
        admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
        presence=[woman_id],
    )
    assert bridged == woman_id
    assert [e.payload["member_id"] for e in _events(log, sid, CAST_LINK_EVENT)] == [woman_id]
    assert not _events(log, sid, CAST_ALIAS_EVENT)
    assert len(roster(log, sid)) == 2  # Aria + Woman: the phrase minted nothing
    assert member_by_id(roster(log, sid), woman_id).provisional is False
    # The link is live exactly while its member is present (ADDENDUM 3 Q2).
    assert resolve_descriptor(log, sid, "the woman", presence=[woman_id]) == woman_id
    assert resolve_descriptor(log, sid, "the woman", presence=[]) is None


def test_first_sight_descriptor_bridges_when_the_member_is_not_in_presence(build_log):
    """Presence is the READ-side boundary, not a write gate (W8.11 decision B).

    The observed live turn typed the descriptor in the very turn the model
    switched its own state header to the name it was proposing, so the identity
    member "Woman" was absent from that turn's presence set. The link is still
    correct — it is inert until she is present, because
    :func:`resolve_descriptor` re-checks presence on every read — and it is what
    the legacy canonical match did with no presence gate at all.
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    aria_id = roster(log, sid)[0].member_id
    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")

    bridged = admit_member(
        log,
        sid,
        "the woman",
        provenance="bts",
        admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
        presence=[aria_id],  # this turn's headers name nobody but Aria
    )
    assert bridged == woman_id
    assert [e.payload["member_id"] for e in _events(log, sid, CAST_LINK_EVENT)] == [woman_id]
    assert len(roster(log, sid)) == 2
    # Inert while she is away, live the moment she is present: one relation.
    assert resolve_descriptor(log, sid, "the woman", presence=[aria_id]) is None
    assert resolve_descriptor(log, sid, "the woman", presence=[woman_id]) == woman_id


def test_conflict_signal_forces_a_split_even_with_an_identity_match(build_log):
    """The typed "a different woman" outranks the canonical identity match."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")

    fresh = admit_member(
        log,
        sid,
        "the woman",
        provenance="bts",
        admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
        presence=[woman_id],
        conflict=True,
    )
    assert fresh != woman_id
    assert [e.payload["member_id"] for e in _events(log, sid, CAST_LINK_EVENT)] == [fresh]
    assert member_by_id(roster(log, sid), woman_id).display_name == "Woman"


def test_confirmed_descriptor_patches_the_same_member(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    woman_id = admit_member(
        log, sid, "the woman", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
    )
    assign_member_color(log, sid, woman_id, "the woman")
    color = list_member_colors(log, sid)[woman_id]

    patch_member(log, sid, woman_id, display_name="Zahra", reason="she gives her name")
    member = member_by_id(roster(log, sid), woman_id)
    assert member.display_name == "Zahra"
    assert member.provisional is False
    assert "the woman" in member.aliases
    assert list_member_colors(log, sid)[woman_id] == color


# -- fork: inherit up to the cut, nothing after ------------------------------


def test_fork_inherits_members_up_to_its_cut(build_log):
    log = build_log()
    parent = "s1"
    _seed_card(log, parent, ARIA)
    ensure_roster(log, parent)
    kesh_id = admit_member(log, parent, "Kesh", provenance="bts", admitted_by="bts")
    cut = len(log.get_events(parent))
    log.fork_session(parent, cut, "fork1")
    # A member admitted AFTER the cut must not leak into the branch.
    admit_member(log, parent, "Toma", provenance="bts", admitted_by="bts")

    branch = roster(log, "fork1")
    assert [m.display_name for m in branch] == [ARIA, "Kesh"]
    assert member_by_id(branch, kesh_id) is not None
    assert "Toma" not in {m.display_name for m in branch}
    # The branch can still admit its own member.
    own = admit_member(log, "fork1", "Nia", provenance="bts", admitted_by="bts")
    assert own not in {m.member_id for m in branch}
    assert [m.display_name for m in roster(log, "fork1")] == [ARIA, "Kesh", "Nia"]


def test_fork_cut_before_an_admit_does_not_see_it(build_log):
    log = build_log()
    parent = "s1"
    _seed_card(log, parent, ARIA)
    ensure_roster(log, parent)
    cut = len(log.get_events(parent))
    log.fork_session(parent, cut, "fork1")
    admit_member(log, parent, "Kesh", provenance="bts", admitted_by="bts")
    assert [m.display_name for m in roster(log, "fork1")] == [ARIA]


# -- retire: the tombstone writer -------------------------------------------


def test_retire_member_tombstones_without_touching_the_identity(build_log):
    """Retire folds to ``status='retired'``; the id, name and colour survive."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    member_id = admit_member(log, sid, "Narrative", provenance="bts", admitted_by="bts")

    assert retire_member(log, sid, member_id=member_id, reason='role word "Narrative"') == member_id

    member = member_by_id(roster(log, sid), member_id)
    assert member is not None
    assert member.status == "retired" and not member.is_active
    assert member.display_name == "Narrative"  # never rewritten, only tombstoned
    (event,) = _events(log, sid, CAST_RETIRE_EVENT)
    assert event.payload == {
        "event": CAST_RETIRE_EVENT,
        "member_id": member_id,
        "reason": 'role word "Narrative"',
    }
    # A tombstone is invisible to the runtime paths the fold already filters.
    assert runtime_notice(log, sid) is None
    assert member.display_name not in {
        m.display_name for m in roster(log, sid) if m.is_active
    }


def test_retire_member_is_idempotent_and_refuses_unknown_ids(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    member_id = admit_member(log, sid, "Kesh", provenance="bts", admitted_by="bts")

    retire_member(log, sid, member_id=member_id, reason="first")
    retire_member(log, sid, member_id=member_id, reason="second")
    assert len(_events(log, sid, CAST_RETIRE_EVENT)) == 1

    with pytest.raises(ValueError, match="unknown cast member"):
        retire_member(log, sid, member_id="cm_missing", reason="nope")


def test_a_later_patch_resurrects_a_retired_member(build_log):
    """D10: the fold is latest-wins, so one tombstone is not permanent."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    member_id = admit_member(log, sid, "Narrative", provenance="bts", admitted_by="bts")
    retire_member(log, sid, member_id=member_id, reason="role word")

    patch_member(log, sid, member_id, display_name="Nora")

    member = member_by_id(roster(log, sid), member_id)
    assert member is not None and member.is_active
    assert member.display_name == "Nora"


# -- schema gate: the new typed ``system`` variants --------------------------


def test_cast_admit_payload_passes_the_schema_gate():
    assert (
        validate_event(
            "system",
            {
                "event": CAST_ADMIT_EVENT,
                "member_id": "cm_1",
                "name": "Kesh",
                "provenance": "bts",
                "admitted_by": "bts",
                "provisional": True,
                "aliases": ["Kesh the Grey"],
            },
        )
        == []
    )


def test_malformed_cast_patch_is_rejected():
    errors = validate_event("system", {"event": CAST_PATCH_EVENT})
    assert "missing required key 'member_id'" in errors


def test_cast_retire_payload_passes_the_schema_gate():
    assert (
        validate_event(
            "system",
            {"event": CAST_RETIRE_EVENT, "member_id": "cm_1", "reason": "role word"},
        )
        == []
    )
    assert validate_event("system", {"event": CAST_RETIRE_EVENT}) == [
        "missing required key 'member_id'"
    ]


def test_malformed_cast_admit_is_rejected_at_the_append_door(log):
    with pytest.raises(LogSchemaError):
        log.append_event(
            "s1",
            "system",
            "engine",
            {"event": CAST_ADMIT_EVENT, "name": "Kesh"},  # no member_id
        )


# -- live-RCA admit hygiene --------------------------------------------------


def test_camel_case_surface_is_the_same_person(build_log):
    """The model writes an article phrase run-together: ``TheHeron``.

    A live session repeated the pair every turn — the state/venue HEADER
    as ``TheHeron`` and its own descriptor reference as ``the heron`` —
    so the header admitted an identity member beside the phrase's provisional and
    the descriptor then churned a fresh one each turn. A camelCase boundary is a
    word boundary: both spellings are one surface, hence one member.
    """
    from fabulist.state.cast import member_by_name, normalised_surface

    assert canonical_key("TheHeron") == canonical_key("the heron") == "heron"
    assert normalised_surface("TheHeron") == "the heron"
    # All-caps names are NOT split (a name is not two words because it shouts).
    assert canonical_key("SEGFAULT") == "segfault"
    assert canonical_key("McCoy") == canonical_key("Mc Coy")

    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    entity = admit_member(
        log,
        sid,
        "the heron",
        provenance="bts",
        admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
    )
    # The drifted header resolves to the member the phrase already points at.
    assert member_by_name(roster(log, sid), "TheHeron").member_id == entity
    assert admit_member(log, sid, "TheHeron", provenance="bts", admitted_by="bts") == entity
    assert [member.display_name for member in roster(log, sid)] == [ARIA, "the heron"]


def test_descriptor_echoing_the_engine_suffix_relinks_instead_of_minting(build_log):
    """The ``(2)`` the engine appends comes back as a reference surface.

    Live turn 3 wrote ``the heron (2)`` — the engine's own
    disambiguation of the descriptor — as a reference, and the engine minted
    ``(3)`` for it, then ``(4)`` the turn after. The suffix is engine spelling,
    never a second person: it re-links to the phrase's one live member.
    """
    from fabulist.state.cast import CAST_LINK_EVENT, descriptor_candidates

    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    first = admit_member(
        log, sid, "the heron", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR,
    )
    echoed = admit_member(
        log, sid, "the heron (2)", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR, presence=[first],
    )
    assert echoed == first
    assert len(roster(log, sid)) == 2  # Aria + one heron
    # The echoed spelling now resolves directly, on the phrase's member.
    assert descriptor_candidates(log, sid, "the heron (2)", presence=[first]) == [first]
    assert [
        event.payload["member_id"]
        for event in _events(log, sid, CAST_LINK_EVENT)
        if event.payload["descriptor_key"].startswith("heron (2")
    ] == [first]
    # Two live candidates for the base phrase stay ambiguous: an echo the engine
    # has never linked splits, exactly like the base phrase does (never a merge).
    second = admit_member(
        log, sid, "the heron", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR, presence=[],
    )
    assert second != first
    split = admit_member(
        log, sid, "the heron (3)", provenance="bts", admitted_by="bts",
        reference_kind=REFERENCE_DESCRIPTOR, presence=[first, second],
    )
    assert split not in {first, second}


# -- W9.22 §2: a promotion re-tiers the member it promotes -------------------


def test_a_promotion_retiers_the_same_member_instead_of_minting_a_new_one(build_log):
    """Promotion is a provenance PATCH, so the tier follows the member it made.

    W9.22 §2 splits membership into tiers (actor: lane-eligible, never
    cap-evicted; presence: learned in play) and derives the tier from
    ``provenance`` (:func:`fabulist.state.cast.member_tier`) rather than storing
    it. The consequence this test pins: a promoted character is the SAME member
    it was as a presence — ``member_id``, aliases and colour all survive the
    re-tier — so no promotion can orphan the colour registry, the attribution
    rows or the memory bound to it, and there is no second tier field to get out
    of sync.
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    learned = admit_member(log, sid, "Millie", provenance="bts", admitted_by="bts")
    # A second spelling, admitted in play: the alias the member carries.
    assert admit_member(log, sid, "millie", provenance="bts", admitted_by="bts") == learned
    color = assign_member_color(log, sid, learned, "Millie")
    before = member_by_id(roster(log, sid), learned)
    assert before is not None and before.aliases == ("millie",)
    assert member_tier(before) == TIER_PRESENCE

    patch_member(log, sid, learned, provenance="actor", reason="promotion gate")

    after = member_by_id(roster(log, sid), learned)
    assert after is not None
    assert member_tier(after) == TIER_ACTOR
    assert after.member_id == before.member_id
    assert after.display_name == before.display_name == "Millie"
    assert after.aliases == before.aliases
    assert list_member_colors(log, sid)[learned] == color


def test_an_exact_name_phrase_relinks_instead_of_minting_a_duplicate(log):
    """W9.22 live regression: the phrase IS the name.

    The identity "TheHeron" and the reference phrase "the heron" share
    one canonical key ("heron", the camelCase + article rules), yet turns
    2-4 had linked the phrase to engine-minted duplicates. Because a descriptor
    link existed the identity bridge refused, so resolve_descriptor saw several
    live candidates and SPLIT every turn — "the heron (2)", "(3)", "(4)",
    "(5)" — while the identity sat beside the churn. A phrase whose canonical
    key equals an identity's canonical display name re-links to that identity
    and mints nothing, even when stale links exist.
    """
    from fabulist.state.cast import admit_member, descriptor_candidates, roster

    sid = "exact-name"
    identity = admit_member(log, sid, "TheHeron", provenance="bts", admitted_by="bts")
    # The churn this fix must stop: the phrase was linked to a minted duplicate.
    stale = admit_member(
        log,
        sid,
        "the heron",
        provenance="bts",
        admitted_by="bts",
        reference_kind="descriptor",
    )
    assert descriptor_candidates(log, sid, "heron") == [stale]

    again = admit_member(
        log,
        sid,
        "the heron",
        provenance="bts",
        admitted_by="bts",
        reference_kind="descriptor",
    )

    assert again == identity  # the phrase re-linked to the identity
    assert descriptor_candidates(log, sid, "heron") == [identity]
    names = [member.display_name for member in roster(log, sid)]
    assert "the heron (2)" not in names  # no duplicate minted


def test_a_conflicted_phrase_still_splits_from_the_same_spelling(log):
    """ADDENDUM 2 A9 survives: "a DIFFERENT heron" is not the identity.

    The exact-name rule is the phrase-is-the-name case, never an override of the
    model's typed conflict signal — a conflict splits as it always did.
    """
    from fabulist.state.cast import admit_member

    sid = "conflict-name"
    identity = admit_member(log, sid, "TheHeron", provenance="bts", admitted_by="bts")
    other = admit_member(
        log,
        sid,
        "the heron",
        provenance="bts",
        admitted_by="bts",
        reference_kind="descriptor",
        conflict=True,
    )
    assert other != identity


# -- W9.32 / R3: the player is a NODE, not a cast member ---------------------
#
# The session persona is the player: identity ``player``, the persona name is the
# player node's DISPLAY name. A member carrying that name would split the player
# across two registry keys (presence compares ``assignment[who] ==
# assignment[player]``), so both cast doors keep the name out of the roster — the
# admit path refuses it and the card SEED loop skips it. ``provenance="user"``
# stays exempt: W9.22 User-as-Actor's member IS the player's participation layer.

PERSONA = "Wanderer"


def _assign_persona(log: EventLog, sid: str, name: str = PERSONA) -> str:
    """Give ``sid`` a persona — the chat's PLAYER node, displayed as ``name``."""
    from fabulist.personas.registry import append_persona, assign_persona

    persona_id = append_persona(log, {"name": name})
    assign_persona(log, sid, persona_id)
    return persona_id


def test_admitting_the_persona_name_is_refused_readably(build_log):
    """The one admit path refuses the player, citing ``who:'player'``."""
    log = build_log()
    sid = "persona-chat"
    _assign_persona(log, sid)

    with pytest.raises(ValueError) as refusal:
        admit_member(log, sid, PERSONA, provenance="bts", admitted_by="bts")

    message = str(refusal.value)
    assert f"{PERSONA!r} is the player's persona name" in message
    assert message.endswith("— the player is not a cast member (cite who:'player')")
    # A refused admission writes nothing at all.
    assert roster(log, sid) == []
    assert _events(log, sid, CAST_ADMIT_EVENT) == []

    # The canonical key is the comparison, so a respelling is the same player.
    with pytest.raises(ValueError, match="cite who:'player'"):
        admit_member(log, sid, "wanderer", provenance="extractor", admitted_by="bts")

    # ...and so is a generic player surface (the header words live logs admitted).
    with pytest.raises(ValueError, match="cite who:'player'"):
        admit_member(log, sid, "User", provenance="bts", admitted_by="bts")

    # A non-player name is admitted on the same session exactly as before.
    yara = admit_member(log, sid, "Yara", provenance="bts", admitted_by="bts")
    assert [member.member_id for member in roster(log, sid)] == [yara]


def test_a_user_provenance_admission_of_the_persona_name_is_accepted(build_log):
    """W9.22 User-as-Actor admits the persona name ON PURPOSE — keep it working."""
    log = build_log()
    sid = "user-as-actor"
    _assign_persona(log, sid)

    member_id = admit_member(
        log, sid, PERSONA, provenance="user", admitted_by="user"
    )

    members = roster(log, sid)
    assert [member.member_id for member in members] == [member_id]
    assert members[0].display_name == PERSONA
    assert members[0].provenance == "user"


def test_ensure_roster_skips_the_persona_seed_card(build_log):
    """The persona's seed card renders; it admits nobody (W9.32 review-2 P1)."""
    from fabulist.context.roster import extract_cards

    log = build_log()
    sid = "seeded-persona-chat"
    _assign_persona(log, sid)
    _seed_card(log, sid, PERSONA)  # the persona materialised as a system card
    _seed_card(log, sid, "Yara")

    members = ensure_roster(log, sid)

    assert [member.display_name for member in members] == ["Yara"]
    assert [
        event.payload["name"] for event in _events(log, sid, CAST_ADMIT_EVENT)
    ] == ["Yara"]
    # The card EVENT stays: the persona's text still renders in the prefix.
    assert [card.get("name") for card in extract_cards(log.get_events(sid))] == [
        PERSONA,
        "Yara",
    ]


def test_ensure_roster_writes_nothing_when_every_card_is_the_player(build_log):
    """An all-skipped seed loop is still idempotent — no admit rows, ever."""
    log = build_log()
    sid = "persona-only-chat"
    _assign_persona(log, sid)
    _seed_card(log, sid, PERSONA)

    assert ensure_roster(log, sid) == []
    after_first_read = len(log.get_events(sid))

    assert ensure_roster(log, sid) == []

    assert len(log.get_events(sid)) == after_first_read
    assert _events(log, sid, CAST_ADMIT_EVENT) == []


def test_a_session_without_a_persona_admits_everything_as_today(build_log):
    """No persona ⇒ no player node ⇒ nothing is guarded."""
    log = build_log()
    sid = "sandbox-chat"
    _seed_card(log, sid, "User")
    _seed_card(log, sid, "Yara")

    assert [member.display_name for member in ensure_roster(log, sid)] == [
        "User",
        "Yara",
    ]
    # The admit path stays open for the generic player words too.
    admitted = admit_member(log, sid, "Player", provenance="bts", admitted_by="bts")
    assert admitted
    assert [member.display_name for member in roster(log, sid)] == [
        "User",
        "Yara",
        "Player",
    ]
