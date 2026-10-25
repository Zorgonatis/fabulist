"""W9.30 §5 — the cast ladder's promotion rung (engine door + manual door).

A session-cast member (``provenance: "actor"``, i.e. admitted from an owner-accepted
cast proposal) becomes a real actor persona card through TWO doors that share ONE
assembly function and ONE suffix algorithm:

* the ENGINE door (:func:`fabulist.state.promotion.promotion_pass`) — the
  historian cadence's deterministic pass, which files at most one ordinary
  ``kind="actor" action="add"`` proposal row (``origin="promotion"``);
* the MANUAL door (:func:`fabulist.state.promotion.apply_promotion`) — a
  studio-class persona write through ``append_persona``.

Each test name states the rule it pins: the collision suffix (req. 7), the card's
deterministic bounded content (D7), the fact bar, the ONE-shot dedupe with its
single O2 re-arm, the gate-legal emit shape, and the documented undo asymmetry.

No models anywhere: the pass is engine code, which is the whole point of §5.2.
"""

from __future__ import annotations

from fabulist.log.schema_gate import validate_event
from fabulist.personas.registry import append_persona, delete_persona, list_personas
from fabulist.state import entities, promotion
from fabulist.state.cast import (
    admit_member,
    member_by_name,
    patch_member,
    retire_member,
    roster,
)
from fabulist.state.proposals import accept_proposal, committed, reject_proposal
from fabulist.state.promotion import (
    MAX_ITEM_LINES,
    MAX_RELATIONSHIP_LINES,
    PROMOTION_MIN_FACTS,
    PromotionError,
    apply_promotion,
    candidates,
    fact_count,
    member_definition,
    promotion_pass,
    suffixed_name,
)

SID = "s-promotion"
TITLE = "The Miller's Daughter"


# -- fixtures ----------------------------------------------------------------


def _title(log, title=TITLE, session_id=SID):
    """The session's title anchor (what the card's closing line quotes)."""
    return log.append_event(
        session_id, "system", "user", {"event": "session_created", "title": title}
    )


def _narrative(log, prose="Rain on the mill roof.", session_id=SID):
    return log.append_event(session_id, "narrative", "narrator", {"prose": prose})


def _actor(log, name, *, session_id=SID):
    """A member on the promotion rung: the D79 accept's provenance pair."""
    admit_member(log, session_id, name, provenance="actor", admitted_by="owner")
    member = member_by_name(roster(log, session_id), name)
    assert member is not None
    return member


def _relationship(log, member_id, other="player", *, name="", note=""):
    return entities.establish_relationship(
        log, SID, member_id, other, name=name, note=note
    )


def _meter(log, member_id, key, value, *, subject=None):
    entities.set_attribute(log, SID, member_id, key=key, value=value, subject=subject)


def _item(log, member_id, name, *, note=""):
    return entities.establish_item(log, SID, name, holder=member_id, note=note)


def _three_facts(log, member_id, *, tag=""):
    """Exactly the bar: one edge, one meter, one item (``tag`` keeps items distinct)."""
    _relationship(log, member_id, "player", name="employer", note="hired at the mill")
    _meter(log, member_id, "resolve", 7)
    _item(log, member_id, f"brass key{tag}", note="opens the mill")
    return 3


def _rows(log, session_id=SID):
    """Every ``asset_proposal`` payload on the session (the pass's output)."""
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.type == "system" and event.payload.get("event") == "asset_proposal"
    ]


def _promotion_rows(log, session_id=SID):
    return [row for row in _rows(log, session_id) if row.get("origin") == "promotion"]


def _row_id(log, index=-1, session_id=SID):
    """The log row id (the proposal handle) of one filed promotion row."""
    events = [
        event
        for event in log.get_events(session_id)
        if event.type == "system" and event.payload.get("event") == "asset_proposal"
    ]
    return str(events[index].id)


def _registry(log, session_id=SID):
    return entities.registry(log, session_id)


# -- the collision rule (req. 7, §5.4) ---------------------------------------


def test_a_free_name_is_written_unchanged():
    """No collision -> no suffix. The owner's own name is never rewritten."""
    assert suffixed_name("Tessa", []) == "Tessa"
    assert suffixed_name("Tessa", ["Marcus", "Millie"]) == "Tessa"


def test_a_colliding_name_gets_the_first_free_numeric_suffix():
    """The plan's example: "Tessa" taken -> "Tessa1" (never a refusal)."""
    assert suffixed_name("Tessa", ["Tessa"]) == "Tessa1"


def test_the_suffix_skips_names_that_are_already_taken():
    """"Tessa" and "Tessa1" taken -> "Tessa2" (the first FREE index, in order)."""
    assert suffixed_name("Tessa", ["Tessa", "Tessa1"]) == "Tessa2"


def test_the_collision_is_judged_through_the_canonical_key():
    """``tessa`` / ``the Millie`` are the same surface (cast.canonical_key)."""
    assert suffixed_name("Tessa", ["tessa"]) == "Tessa1"
    assert suffixed_name("Millie", ["the millie"]) == "Millie1"
    assert suffixed_name("Tessa", ["TESSA", "tessa1"]) == "Tessa2"


def test_a_blank_member_name_is_never_suffixed_into_a_card():
    """``append_persona`` refuses a nameless card, so the door refuses first."""
    try:
        suffixed_name("   ", ["Tessa"])
    except PromotionError as exc:
        assert "non-empty" in str(exc)
    else:  # pragma: no cover - the refusal is the assertion
        raise AssertionError("a blank name must not be promoted")


# -- the card (one assembly function, §5.5) ----------------------------------


def test_the_card_is_assembled_deterministically_from_the_log(log):
    """Same log -> byte-identical card: the assembly is a fold, never a model."""
    _title(log)
    _narrative(log, "Tessa pours the grain.")
    member = _actor(log, "Tessa")
    log.append_event(
        SID,
        "system",
        "engine",
        {"event": "cast_alias", "member_id": member.member_id, "alias": "the miller"},
    )
    member = member_by_name(roster(log, SID), "Tessa")
    _relationship(log, member.member_id, "player", name="employer", note="hired her")
    _item(log, member.member_id, "brass key")

    first = member_definition(log, SID, member, _registry(log))
    second = member_definition(log, SID, member, _registry(log))

    assert first == second
    assert first["name"] == "Tessa"  # the DOOR suffixes; the assembly does not
    assert first["extensions"] == {"cast_aliases": ["the miller"]}
    assert first["description"].splitlines() == [
        "Also known as the miller.",
        "Towards the player: employer — hired her.",
        "Holds brass key.",
        f'Promoted from chat "{TITLE}" at turn 1.',
    ]
    # The authored fields are the owner's in the studio and stay empty (§5.5).
    assert first["personality"] == ""
    assert first["scenario"] == ""
    assert first["first_mes"] == ""
    assert first["book_ids"] == []
    assert first["alternate_greetings"] == []
    assert first["image_asset"] is None


def test_the_card_links_back_to_the_member_in_its_provenance(log):
    """``provenance.member_id`` is the dedupe's and the badge's join key."""
    _narrative(log)
    member = _actor(log, "Tessa")
    definition = member_definition(log, SID, member, _registry(log))

    assert definition["provenance"]["origin"] == "promotion"
    assert definition["provenance"]["member_id"] == member.member_id
    assert definition["provenance"]["session_id"] == SID
    assert isinstance(definition["provenance"]["at_seq"], int)


def test_the_card_carries_no_transient_meter_state(log):
    """D7: identity facts travel, turn state does not (a stress value is not a card)."""
    _title(log)
    member = _actor(log, "Tessa")
    _relationship(log, member.member_id, "player", name="employer")
    _meter(log, member.member_id, "hostility", 42, subject="player")

    definition = member_definition(log, SID, member, _registry(log))

    assert "hostility" not in definition["description"]
    assert "42" not in definition["description"]
    assert "hostility" not in definition["personality"]


def test_the_description_is_bounded_in_relationships_and_items(log):
    """A being with a hundred edges still yields a readable card (§5.5's bound)."""
    _title(log)
    member = _actor(log, "Tessa")
    for index in range(MAX_RELATIONSHIP_LINES + 3):
        _relationship(log, member.member_id, f"cm_other{index}", name=f"knows {index}")
    for index in range(MAX_ITEM_LINES + 3):
        _item(log, member.member_id, f"item {index}")

    card = member_definition(log, SID, member, _registry(log))
    lines = card["description"].splitlines()

    assert sum(line.startswith(("Towards ", "From ")) for line in lines) == (
        MAX_RELATIONSHIP_LINES
    )
    assert sum(line.startswith("Holds ") for line in lines) == MAX_ITEM_LINES
    assert lines[-1].startswith('Promoted from chat "')


def test_an_untitled_chat_is_named_as_such_in_the_card(log):
    """No title anchor -> the closing line still reads (never ``""``)."""
    member = _actor(log, "Tessa")
    card = member_definition(log, SID, member, _registry(log))
    assert card["description"].splitlines()[-1] == (
        "Promoted from an untitled chat at turn 0."
    )


def test_the_fact_count_is_meters_edges_and_items_only(log):
    """The §5.2 unit: hosted meters, relationships either way, held items."""
    member = _actor(log, "Tessa")
    assert fact_count(_registry(log), member.member_id) == 0

    _relationship(log, member.member_id, "player")  # member is the source
    _relationship(log, "player", member.member_id)  # member is the target
    _meter(log, member.member_id, "resolve", 5)  # hosted
    _meter(log, "player", "trust", 3)  # somebody else's meter
    _item(log, member.member_id, "brass key")  # held
    _item(log, "player", "coin")  # not held by this member

    assert fact_count(_registry(log), member.member_id) == 4


# -- the engine door: bar, ranking, dedupe, emit (§5.1/§5.2) -----------------


def test_a_member_below_the_fact_bar_is_not_proposed(log):
    """Under PROMOTION_MIN_FACTS the card would be an empty shell — no row."""
    member = _actor(log, "Tessa")
    _relationship(log, member.member_id, "player")
    _item(log, member.member_id, "brass key")

    assert promotion_pass(log, SID) == 0
    assert _promotion_rows(log) == []


def test_the_pass_files_one_actor_add_row_for_the_richest_candidate(log):
    """§5.2: rank by fact count, roster order as the tiebreak, ONE per run."""
    _narrative(log)
    poor = _actor(log, "Millie")  # exactly the bar: 3
    _three_facts(log, poor.member_id, tag=" (Millie)")
    rich = _actor(log, "Tessa")  # above it: 4
    _three_facts(log, rich.member_id, tag=" (Tessa)")
    _item(log, rich.member_id, "ledger")

    assert promotion_pass(log, SID) == 1

    rows = _promotion_rows(log)
    assert len(rows) == 1
    row = rows[0]
    assert (row["kind"], row["action"], row["target"]) == ("actor", "add", None)
    assert row["origin"] == "promotion"
    assert row["signal"] is None
    assert row["patch"]["name"] == "Tessa"
    # The evidence is the deterministic count summary the owner judges by.
    assert "cast member Tessa has 4 tracked facts" in row["evidence"]
    assert "relationship with the player" in row["evidence"]


def test_the_filed_name_is_suffixed_at_file_time(log):
    """req. 7: the review row SHOWS the final name before the owner accepts."""
    append_persona(log, {"name": "Tessa"})
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)

    assert promotion_pass(log, SID) == 1
    assert _promotion_rows(log)[0]["patch"]["name"] == "Tessa1"


def test_the_re_arm_count_rides_the_row_for_the_next_read(log):
    """O2 stores the count where the re-arm read finds it (provenance.fact_count)."""
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)

    assert promotion_pass(log, SID) == 1
    assert _promotion_rows(log)[0]["patch"]["provenance"]["fact_count"] == 3


def test_the_emit_is_gate_legal(log):
    """S3: the row is appended by this module through the schema gate itself."""
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)

    assert promotion_pass(log, SID) == 1
    row = _promotion_rows(log)[0]
    assert validate_event("system", row) == []
    # ...and the gate is the REAL door: a nameless patch is refused, not queued.
    assert validate_event("system", promotion._promotion_payload({}, "no name"))


def test_a_live_row_blocks_the_next_run(log):
    """One promotion per member per session: the queue is not re-stocked."""
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)

    assert promotion_pass(log, SID) == 1
    assert promotion_pass(log, SID) == 0
    assert len(_promotion_rows(log)) == 1


def test_a_manual_persona_blocks_the_engine_pass(log):
    """Review S2: BOTH doors count — the badge's persona fold is the second read."""
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)
    apply_promotion(log, SID, member)

    assert promotion_pass(log, SID) == 0
    assert _promotion_rows(log) == []


def test_an_accepted_engine_promotion_blocks_the_pass(log):
    """Once the card exists, the member is off the cadence for good."""
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)
    assert promotion_pass(log, SID) == 1
    accept_proposal(log, SID, _row_id(log))

    assert promotion_pass(log, SID) == 0
    assert len(_promotion_rows(log)) == 1


def test_a_rejected_row_silences_the_member_at_an_unchanged_fact_count(log):
    """O2: silence after a refusal — an unchanged member must not nag each cadence."""
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)
    assert promotion_pass(log, SID) == 1
    reject_proposal(log, SID, _row_id(log))

    assert promotion_pass(log, SID) == 0
    assert promotion_pass(log, SID) == 0
    assert len(_promotion_rows(log)) == 1


def test_a_rejected_row_re_arms_once_when_the_facts_double(log):
    """O2: ONE re-arm, at ``current >= 2 x recorded`` (nothing vaguer)."""
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)
    assert promotion_pass(log, SID) == 1
    reject_proposal(log, SID, _row_id(log))

    # 3 -> 5 facts: growth, but not the doubling the rule demands.
    _item(log, member.member_id, "ledger")
    _meter(log, member.member_id, "grit", 6)
    assert promotion_pass(log, SID) == 0

    # 5 -> 6 facts: exactly 2 x the rejected row's recorded 3.
    _item(log, member.member_id, "lantern")
    assert promotion_pass(log, SID) == 1
    rows = _promotion_rows(log)
    assert len(rows) == 2
    assert rows[-1]["patch"]["provenance"]["fact_count"] == 6


def test_the_single_re_arm_is_never_repeated(log):
    """O2: at most ONE re-arm ever — a second refusal is final."""
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)
    assert promotion_pass(log, SID) == 1
    reject_proposal(log, SID, _row_id(log))
    for index in range(6):
        _item(log, member.member_id, f"item {index}")
    assert promotion_pass(log, SID) == 1
    reject_proposal(log, SID, _row_id(log))

    for index in range(6, 16):
        _item(log, member.member_id, f"item {index}")
    assert promotion_pass(log, SID) == 0
    assert len(_promotion_rows(log)) == 2


def test_only_actor_provenance_members_are_candidates(log):
    """The rung BELOW decides: a card/world/presence member is not promotable."""
    card_member = _actor(log, "Marcus")
    admit_member(log, SID, "Clerk", provenance="bts", admitted_by="bts")
    _three_facts(log, card_member.member_id)

    patch_member(
        log, SID, card_member.member_id, provenance="card", reason="seed provenance"
    )

    assert candidates(log, SID) == []
    assert promotion_pass(log, SID) == 0
    assert _promotion_rows(log) == []


def test_candidates_ignore_the_fact_bar_which_the_pass_applies(log):
    """``candidates`` answers dedupe only; the count is the pass's rule."""
    member = _actor(log, "Tessa")  # zero facts
    assert [row.member_id for row in candidates(log, SID)] == [member.member_id]
    assert promotion_pass(log, SID) == 0


def test_a_retired_member_is_not_a_candidate(log):
    """A tombstoned member is off the rung (the pane lists who IS in the cast)."""
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)
    retire_member(log, SID, member_id=member.member_id, reason="left the story")

    assert promotion_pass(log, SID) == 0
    assert _promotion_rows(log) == []


# -- the manual door (§5.3) -------------------------------------------------


def test_the_manual_door_suffixes_an_omitted_name(log):
    """Name omitted -> the §5.4 suffix, exactly like the engine's file-time name."""
    append_persona(log, {"name": "Tessa"})
    member = _actor(log, "Tessa")
    created = apply_promotion(log, SID, member)

    assert created.name == "Tessa1"
    assert created.provenance["member_id"] == member.member_id


def test_the_manual_door_writes_a_typed_name_verbatim(log):
    """req. 7/D5: a duplicate is a deliberate owner choice — never refused."""
    append_persona(log, {"name": "Tessa"})
    member = _actor(log, "Tessa")

    created = apply_promotion(log, SID, member, name="Tessa")

    assert created.name == "Tessa"
    assert [persona.name for persona in list_personas(log)].count("Tessa") == 2


def test_the_manual_door_refuses_a_member_off_the_rung(log):
    """Eligibility is a refusal (req. 7's never-refuse is about NAMES)."""
    admit_member(log, SID, "Clerk", provenance="bts", admitted_by="bts")
    member = member_by_name(roster(log, SID), "Clerk")
    assert member is not None

    try:
        apply_promotion(log, SID, member)
    except PromotionError as exc:
        assert "cast proposal" in str(exc)
    else:  # pragma: no cover - the refusal is the assertion
        raise AssertionError("an off-rung member must not be promoted")
    assert list_personas(log) == []


def test_the_manual_card_is_the_same_assembly_as_the_engine_card(log):
    """ONE assembly function, two doors: the description is door-independent."""
    _narrative(log)
    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)

    manual = apply_promotion(log, SID, member)
    assert promotion_pass(log, SID) == 0  # the manual card already blocked it
    engine_definition = member_definition(log, SID, member, _registry(log))

    assert manual.description == engine_definition["description"]
    assert manual.extensions == engine_definition["extensions"]


def test_a_second_manual_promotion_is_allowed_and_suffixed(log):
    """The door stays open (never refuse) — and the second card gets a free name."""
    member = _actor(log, "Tessa")
    first = apply_promotion(log, SID, member)
    second = apply_promotion(log, SID, member)

    assert (first.name, second.name) == ("Tessa", "Tessa1")


# -- the undo asymmetry (documented behaviour) ------------------------------


def test_an_accepted_engine_promotion_is_undoable_by_its_decision_row(log):
    """Engine door: an ordinary actor-add decision -> undo tombstones the persona."""
    from fabulist.state.proposals import undo_decision

    member = _actor(log, "Tessa")
    _three_facts(log, member.member_id)
    assert promotion_pass(log, SID) == 1
    decision = accept_proposal(log, SID, _row_id(log))
    assert [persona.name for persona in list_personas(log)] == ["Tessa"]

    undo_decision(log, SID, decision.decision_id)  # type: ignore[union-attr]

    assert list_personas(log) == []


def test_a_manual_promotion_has_no_decision_row_and_delete_is_its_reverse(log):
    """Manual door: a studio-class write, so nothing to undo — delete is the inverse."""
    member = _actor(log, "Tessa")
    created = apply_promotion(log, SID, member)

    assert committed(log, SID) == []  # no decision row was filed
    assert _promotion_rows(log) == []  # and no proposal row either

    delete_persona(log, created.persona_id)

    assert list_personas(log) == []


def test_card_relationship_lines_render_names_not_id_arrows(log):
    """The live gate filed "Towards Yara: cm_x→cm_y — neighbors": an edge
    established without a label carries the registry's mechanical from→to
    default as its name, and the card must not print it."""
    member = _actor(log, "Tessa")
    _relationship(log, member.member_id, "player", note="easy trust")
    card = promotion.member_definition(
        log, SID, member, entities.registry(log, SID)
    )
    line = next(
        ln for ln in card["description"].splitlines() if ln.startswith("Towards")
    )
    assert "cm_" not in line
    assert "easy trust" in line
