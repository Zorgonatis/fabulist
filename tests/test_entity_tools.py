"""W9.29 §6 — the BTS write-action tool surface (+ W9.32 W5's five new doors).

Every tool's success AND rejection path, the per-turn budget, the two identity
seams (a name resolves; an unknown name is a D79 proposal with NO assignment) and
the three texts the plan pins verbatim (unknown catalog key, occupied location,
budget).

W9.32 W5 adds the session-tree doors (``set_condition``, ``set_body``,
``establish_economy``, ``adjust_economy``, ``propose_time``), the two ledger
fields they validate against (``EntityTurn.clock`` / ``.time_proposal``) and the
ROSTER-FIRST being door — the fix for a cast member being shadowed by the
player's persona name.

The tools are ``LoopTool``s, so the tests call the exact ``fn`` the loop runs
(:func:`fabulist.agents.loop._execute_tool` adds only the guard and the JSON
envelope). ``asyncio.run`` per call mirrors the loop's own single-call shape.

Attribution follows the LEDGER, never the tool: every store writer stamps
``EntityTurn.actor``, so a tracker ledger's rows say ``bts`` and a planner
ledger's say ``planner`` (W9.32 §5's ownership map is only checkable that way).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.state.cast import admit_member, member_by_id, member_by_name, roster
from fabulist.state.entities import PLAYER, SCENE, body_for, flat_render, registry
from fabulist.state.entity_tools import (
    ENTITY_ACTION_BUDGET,
    ENTITY_BUDGET_ERROR,
    EntityTurn,
    entity_action_tools,
    reconcile_drift,
    budget_error,
)
from fabulist.state.session_tree import session_tree

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


@pytest.fixture
def cast_proposals(monkeypatch):
    """Capture the ``cast`` proposals the identity door files (L4's emitter)."""
    filed: list[tuple[str, str, list[str], str]] = []

    def _emit(log, session_id, name, aliases, origin):
        filed.append((session_id, name, list(aliases), origin))

    from fabulist.state import proposals as proposal_store

    monkeypatch.setattr(proposal_store, "emit_cast_proposal", _emit, raising=False)
    return filed


def _narrative(log: EventLog, sid: str, prose: str = "She steps into the bathroom.") -> int:
    event = log.append_event(sid, "narrative", "narrator", {"prose": prose})
    return event.seq


def _last_narrative_seq(log: EventLog, sid: str = "chat") -> int:
    return max(e.seq for e in log.get_events(sid) if e.type == "narrative")


def _seeded(log: EventLog, sid: str = "chat") -> None:
    """A roster with two accepted members plus one narrative turn to anchor to."""
    admit_member(log, sid, "Seraphine", provenance="card", admitted_by="user")
    admit_member(log, sid, "Yara", provenance="card", admitted_by="user")
    assert {m.display_name for m in roster(log, sid)} == {"Seraphine", "Yara"}
    _narrative(log, sid)


def _tools(log: EventLog, sid: str = "chat"):
    turn = EntityTurn(log, sid)
    return {tool.name: tool for tool in entity_action_tools(turn)}, turn


def _clocked_tools(log: EventLog, time: str, date: str, sid: str = "chat"):
    """The tool set of a LEDGER that carries this turn's clock (W9.32 §3.5)."""
    turn = EntityTurn(log, sid, clock=(time, date))
    return {tool.name: tool for tool in entity_action_tools(turn)}, turn


def _planner_tools(log: EventLog, sid: str = "chat"):
    """The tool set of a PLANNER-driven ledger (the bootstrap/repair phases, §5)."""
    turn = EntityTurn(log, sid, actor="planner")
    return {tool.name: tool for tool in entity_action_tools(turn)}, turn


def _assign_persona(log: EventLog, sid: str, name: str) -> str:
    """Give ``sid`` a persona — the chat's PLAYER node, displayed as ``name``."""
    from fabulist.personas.registry import append_persona, assign_persona

    persona_id = append_persona(log, {"name": name})
    assign_persona(log, sid, persona_id)
    return persona_id


def _fold_clock(log: EventLog, time: str, date: str, sid: str = "chat") -> None:
    """A folded clock in the projection (what a tool reads mid-loop)."""
    log.append_event(sid, "state-delta", "clock", {"time": time, "date": date})


def _call(tools, tool: str, **args):
    """Run one tool's ``fn`` exactly as the loop does (one call, one argument dict)."""
    return asyncio.run(tools[tool].fn(args))


def _payloads(log: EventLog, sid: str, kind: str) -> list[dict]:
    return [e.payload for e in log.get_events(sid) if e.payload.get("event") == kind]


def _member_id(log: EventLog, sid: str, name: str) -> str:
    return member_by_name(roster(log, sid), name).member_id


# -- establish ---------------------------------------------------------------


def test_establish_location_returns_the_minted_id_and_cites_it(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    hall = _call(tools, "establish_location", name="upstairs hall")
    bath = _call(
        tools, "establish_location", name="bathroom", parent_id=hall["id"], note="no door"
    )

    assert hall["ok"] is True and hall["id"].startswith("loc_")
    assert bath["ok"] is True
    reg = registry(log, "chat")
    assert reg.locations[bath["id"]].parent_id == hall["id"]
    assert reg.locations[bath["id"]].note == "no door"
    assert turn.actions == 2 and turn.rejected == 0


def test_establish_location_rejects_a_reserved_name_and_writes_nothing(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    result = _call(tools, "establish_location", name="off_screen")

    assert result["ok"] is False
    assert "reserved literal" in result["error"]
    assert _payloads(log, "chat", "entity_establish") == []
    assert turn.rejected == 1 and turn.actions == 0


def test_establish_location_rejects_an_unknown_parent(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "establish_location", name="bathroom", parent_id="loc_nope")

    assert result["ok"] is False
    # §6's unknown-object text: the registry listing and the corrective call.
    assert "registry locations" in result["error"]
    assert "call establish_location to add one" in result["error"]


def test_establish_thread_and_item_and_relationship_return_their_ids(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    seraphine = _member_id(log, "chat", "Seraphine")

    thread = _call(tools, "establish_thread", name="the slow courtship", detail="tests her")
    edge = _call(tools, "establish_relationship", **{"from": seraphine, "to": PLAYER}, note="a project")
    item = _call(tools, "establish_item", name="brass key", holder=PLAYER)

    assert thread["id"].startswith("th_")
    assert edge["id"].startswith("rel_")
    assert item["id"].startswith("itm_")
    reg = registry(log, "chat")
    assert reg.threads[thread["id"]].detail == "tests her"
    assert reg.relationships[edge["id"]].note == "a project"
    assert reg.items[item["id"]].holder == PLAYER


def test_establish_relationship_resolves_names_to_member_ids(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    result = _call(tools, "establish_relationship", **{"from": "Seraphine", "to": "Yara"})

    assert result["ok"] is True
    edge = registry(log, "chat").relationships[result["id"]]
    assert edge.source == _member_id(log, "chat", "Seraphine")
    assert edge.target == _member_id(log, "chat", "Yara")
    # The being names ride the turn's mentioned set (the cast door's input, §6).
    assert "Seraphine" in turn.mentions and "Yara" in turn.mentions


def test_establish_relationship_for_an_unknown_person_proposes_and_writes_nothing(
    build_log, cast_proposals
):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "establish_relationship", **{"from": "Zahra", "to": PLAYER})

    assert result["ok"] is False
    assert "proposed for owner review" in result["error"]
    assert cast_proposals == [("chat", "Zahra", [], "bts")]
    assert _payloads(log, "chat", "entity_establish") == []


def test_establish_item_for_an_unknown_holder_proposes_and_writes_nothing(
    build_log, cast_proposals
):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "establish_item", name="brass key", holder="the clerk")

    assert result["ok"] is False
    assert "no item until accepted" in result["error"]
    assert cast_proposals == [("chat", "the clerk", [], "bts")]
    assert _payloads(log, "chat", "entity_establish") == []


# -- move --------------------------------------------------------------------


def test_move_being_accepts_an_id_and_returns_the_stored_target(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]

    result = _call(tools, "move_being", who=PLAYER, to=bath)

    assert result["ok"] is True
    assert result["who"] == PLAYER and result["to"] == bath
    assert registry(log, "chat").assignments[PLAYER] == bath
    payload = _payloads(log, "chat", "entity_move")[-1]
    assert payload["to"] == bath
    # Anchored to the newest narrative (the turn the actions belong to, §5).
    assert payload["seq"] == _last_narrative_seq(log)
    assert turn.actions == 2


def test_move_being_accepts_a_name_and_records_the_member_id(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    hall = _call(tools, "establish_location", name="upstairs hall")["id"]

    result = _call(tools, "move_being", who="Seraphine", to="upstairs hall")

    assert result["ok"] is True
    assert result["who"] == _member_id(log, "chat", "Seraphine")
    assert registry(log, "chat").assignments[result["who"]] == hall


def test_move_being_off_screen_carries_the_bucket_reference(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]

    result = _call(tools, "move_being", who="Seraphine", to="off_screen", at=bath)

    assert result["ok"] is True
    assert result["to"] == "off_screen" and result["at"] == bath
    reg = registry(log, "chat")
    assert reg.assignments[result["who"]] == "off_screen"
    assert reg.off_screen_ref == bath


def test_move_being_at_outside_off_screen_is_refused(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]

    result = _call(tools, "move_being", who=PLAYER, to=bath, at=bath)

    assert result["ok"] is False
    assert "'at' only applies when moving to off_screen" in result["error"]


def test_move_being_unknown_identity_files_a_proposal_and_writes_no_assignment(
    build_log, cast_proposals
):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]
    before = dict(registry(log, "chat").assignments)

    result = _call(tools, "move_being", who="Zahra", to=bath)

    assert result["ok"] is False
    assert "proposed for owner review; not movable until accepted" in result["error"]
    assert cast_proposals == [("chat", "Zahra", [], "bts")]
    assert registry(log, "chat").assignments == before
    assert _payloads(log, "chat", "entity_move") == []


def test_move_being_refuses_the_scene(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]

    result = _call(tools, "move_being", who="scene", to=bath)

    assert result["ok"] is False
    assert "the scene is not a being" in result["error"]
    assert _payloads(log, "chat", "entity_move") == []


def test_move_being_unknown_location_lists_the_registry(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    _call(tools, "establish_location", name="bathroom")

    result = _call(tools, "move_being", who=PLAYER, to="kitchen")

    assert result["ok"] is False
    assert "no object 'kitchen'" in result["error"]
    assert "bathroom" in result["error"]


def test_move_being_ambiguous_location_is_an_error_listing_both_ids(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    # A live duplicate name is refused by the writer guard, so ambiguity is
    # reachable only from historical rows (appended raw, below the writer).
    log.append_event("chat", "system", "bts", {"event": "entity_establish", "kind": "location", "id": "loc_old1", "name": "bathroom"})
    log.append_event("chat", "system", "bts", {"event": "entity_establish", "kind": "location", "id": "loc_old2", "name": "Bathroom "})
    first, second = "loc_old1", "loc_old2"

    result = _call(tools, "move_being", who=PLAYER, to="bathroom")

    assert result["ok"] is False
    assert f"'bathroom' matches 2 locations ({first}, {second}) — pass the id" == result["error"]
    assert _payloads(log, "chat", "entity_move") == []


# -- set_attr ----------------------------------------------------------------


def test_set_attr_mints_once_and_reuses_the_same_meter_id(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    seraphine = _member_id(log, "chat", "Seraphine")

    first = _call(
        tools, "set_attr", host=seraphine, subject=PLAYER, key="hostility", value=42
    )
    second = _call(
        tools, "set_attr", host="Seraphine", subject="player", key="hostility", value=30
    )

    assert first["ok"] is True and first["prior"] is None
    assert second["ok"] is True and second["prior"] == 42
    # THE headline requirement: the same attribute is the same object.
    assert first["id"] == second["id"]
    meter = registry(log, "chat").meters[first["id"]]
    assert meter.value == 30 and meter.subject == PLAYER


def test_set_attr_range_check_returns_the_plan_text(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    yara = _member_id(log, "chat", "Yara")
    result = _call(tools, "set_attr", host="player", subject=yara, key="hostility", value=142)

    assert result["ok"] is False
    assert result["error"] == "hostility expects an integer in 0–100, got 142"
    assert _payloads(log, "chat", "entity_set") == []


def test_set_attr_unknown_key_lists_the_catalog_keys(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "set_attr", host="player", key="hostilty", value=3)

    assert result["ok"] is False
    assert result["error"].startswith("unknown attribute 'hostilty' — catalog keys: ")
    assert "hostility" in result["error"]
    assert "(the owner edits this list in the Studio)" in result["error"]


def test_set_attr_trait_takes_text_and_scene_rows_ignore_the_host(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    yara = _member_id(log, "chat", "Yara")

    mood = _call(tools, "set_attr", host=yara, key="mood", value="wary")
    weather = _call(tools, "set_attr", host="bathroom", key="weather", value="rain")

    assert mood["ok"] is True and weather["ok"] is True
    reg = registry(log, "chat")
    assert reg.meters[mood["id"]].value == "wary"
    assert reg.meters[weather["id"]].host == "scene"
    assert reg.meters[weather["id"]].value == "rain"


def test_a_self_trait_on_the_scene_host_is_not_proposed_as_a_person(
    build_log, cast_proposals
):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "set_attr", host="scene", key="mood", value="wary")

    assert result["ok"] is True
    assert cast_proposals == []
    assert registry(log, "chat").meters[result["id"]].host == "scene"


def test_set_attr_relational_row_requires_a_subject(build_log, cast_proposals):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "set_attr", host="player", key="hostility", value=10)

    assert result["ok"] is False
    assert result["error"] == "a being is required — a member id, a name, or 'player'"


def test_set_attr_self_row_refuses_a_subject(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    yara = _member_id(log, "chat", "Yara")
    result = _call(tools, "set_attr", host="player", subject=yara, key="stress", value=20)

    assert result["ok"] is False
    assert "drop the subject" in result["error"]
    assert _payloads(log, "chat", "entity_set") == []


def test_set_attr_non_numeric_meter_value_is_refused_with_the_range_text(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    yara = _member_id(log, "chat", "Yara")
    result = _call(
        tools, "set_attr", host="player", subject=yara, key="trust", value="wary"
    )

    assert result["ok"] is False
    assert result["error"] == "trust expects an integer in 0–100, got 'wary'"


# -- update / retire ---------------------------------------------------------


def test_update_object_patches_in_place_and_keeps_the_id(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    thread = _call(tools, "establish_thread", name="the slow courtship")["id"]

    result = _call(
        tools, "update_object", id=thread, status="paused", detail="Yara pushes back"
    )

    assert result == {"ok": True, "id": thread}
    record = registry(log, "chat").threads[thread]
    assert record.status == "paused" and record.detail == "Yara pushes back"


def test_update_object_unknown_id_lists_the_registry(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "update_object", id="th_nope", status="active")

    assert result["ok"] is False
    assert "no object 'th_nope'" in result["error"]


def test_update_object_refuses_a_field_the_kind_does_not_have(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    item = _call(tools, "establish_item", name="brass key", holder=PLAYER)["id"]

    result = _call(tools, "update_object", id=item, status="paused")

    assert result["ok"] is False
    assert "it has no field 'status'" in result["error"]


def test_update_object_resolves_a_holder_name_to_the_member_key(build_log):
    """The stored holder is a being KEY — the door normalises a display name.

    A holder is matched against occupant keys everywhere it is READ (the tree's
    ``live.items`` compares canonically to the member id), so a raw "Yara"
    written here would stay invisible to every key-based read.
    """
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    yara = _member_id(log, "chat", "Yara")
    item = _call(tools, "establish_item", name="brass key", holder=PLAYER)["id"]

    result = _call(tools, "update_object", id=item, holder="Yara")

    assert result == {"ok": True, "id": item}
    assert registry(log, "chat").items[item].holder == yara
    tree = session_tree(log, "chat")
    node = next(
        row for row in tree["cast"] if row["participation"]["member_id"] == yara
    )
    assert node["live"]["items"] == ["brass key"]
    assert [(row["name"], row["holder"]) for row in tree["items"]] == [
        ("brass key", "Yara")
    ]


def test_update_object_resolves_the_player_literal_to_the_player_key(build_log):
    """``holder: "player"`` stores ``player``, which the flat HUD then reads."""
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    item = _call(tools, "establish_item", name="brass key", holder="Yara")["id"]

    result = _call(tools, "update_object", id=item, holder="player")

    assert result == {"ok": True, "id": item}
    assert registry(log, "chat").items[item].holder == PLAYER
    assert flat_render(registry(log, "chat"))["inv"] == "brass key"


def test_update_object_stores_an_unresolvable_holder_surface_verbatim(
    build_log, cast_proposals
):
    """The store stays PERMISSIVE: a surface the roster cannot place is kept as is.

    No D79 proposal is filed either — an item's holder patch is not a person the
    tracker just met.
    """
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    item = _call(tools, "establish_item", name="brass key", holder=PLAYER)["id"]

    result = _call(tools, "update_object", id=item, holder="the barman")

    assert result == {"ok": True, "id": item}
    assert registry(log, "chat").items[item].holder == "the barman"
    assert cast_proposals == []


def test_update_object_leaves_the_holder_alone_without_one(build_log):
    """A non-holder patch neither resolves nor rewrites the holder."""
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    item = _call(tools, "establish_item", name="brass key", holder="Yara")["id"]

    result = _call(tools, "update_object", id=item, note="worn brass")

    assert result == {"ok": True, "id": item}
    record = registry(log, "chat").items[item]
    yara = _member_id(log, "chat", "Yara")
    assert (record.note, record.holder) == ("worn brass", yara)


def test_retire_object_tombstones_and_frees_the_id(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    location = _call(tools, "establish_location", name="spare room")["id"]

    result = _call(tools, "retire_object", id=location, reason="left the house")

    assert result == {"ok": True, "id": location}
    assert location not in registry(log, "chat").locations
    assert _payloads(log, "chat", "entity_retire")[-1]["reason"] == "left the house"


def test_retire_object_refuses_an_occupied_location_with_the_plan_text(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]
    moved = _call(tools, "move_being", who="Yara", to=bath)

    result = _call(tools, "retire_object", id=bath)

    assert result["ok"] is False
    assert result["error"] == f"{bath} is occupied by {moved['who']} — move_being them first"
    assert bath in registry(log, "chat").locations


def test_retire_object_refuses_the_off_screen_bucket(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "retire_object", id="loc_off_screen")

    assert result["ok"] is False
    assert "absence bucket" in result["error"]


# -- rename ------------------------------------------------------------------


def test_rename_being_patches_the_member_and_keeps_the_id_and_alias(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    seraphine = _member_id(log, "chat", "Seraphine")

    result = _call(tools, "rename_being", who=seraphine, to="Seraphine R.")

    assert result == {"ok": True, "member_id": seraphine}
    member = member_by_id(roster(log, "chat"), seraphine)
    assert member.display_name == "Seraphine R."
    # One member id, one colour, the old spelling still resolving (ADDENDUM 1 A2).
    assert "Seraphine" in member.aliases


def test_rename_being_refuses_a_name_another_member_already_carries(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "rename_being", who="Seraphine", to="Yara")

    assert result["ok"] is False
    assert "rename refused" in result["error"]
    # Never a merge: both members are still distinct.
    names = sorted(member.display_name for member in roster(log, "chat"))
    assert names == ["Seraphine", "Yara"]


def test_rename_being_of_an_unknown_surface_proposes_the_new_name(build_log, cast_proposals):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "rename_being", who="the woman", to="Zahra")

    assert result["ok"] is False
    assert "proposed for owner review; the rename applies once accepted" in result["error"]
    # The NAME is the proposal; the old surface rides as its alias (R1's successor
    # to ``references[].rename_to``).
    assert cast_proposals == [("chat", "Zahra", ["the woman"], "bts")]


def test_rename_being_refuses_the_player(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "rename_being", who="player", to="Marc")

    assert result["ok"] is False
    assert "the player is not a cast member" in result["error"]


# -- flag_contradiction ------------------------------------------------------


def test_flag_contradiction_appends_one_notice_on_the_turns_narrative(build_log):
    log = build_log()
    _seeded(log)
    seq = _narrative(log, "chat", "Yara is in the kitchen.")
    tools, turn = _tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]
    _call(tools, "move_being", who="Yara", to=bath)

    result = _call(
        tools,
        "flag_contradiction",
        what="Yara's location",
        registry_says=f"{bath} (bathroom)",
        prose_says="Yara is in the kitchen.",
        anchor="Yara is in the kitchen.",
    )

    assert result == {"ok": True, "flagged": True}
    notices = _payloads(log, "chat", "validation_notice")
    assert len(notices) == 1
    assert notices[0]["seq"] == seq
    flag = notices[0]["flags"][0]
    assert flag["check"] == "registry_contradiction"
    assert flag["severity"] == "warn"
    assert flag["anchor"] == "Yara is in the kitchen."
    assert flag["fix"] == f"registry holds {bath} (bathroom); move_being if the move really happened"
    # The registry is unchanged: a flag is not a write to the world (req. 12).
    yara = _member_id(log, "chat", "Yara")
    assert registry(log, "chat").assignments[yara] == bath
    assert turn.flags == 1


def test_flag_contradiction_without_a_narrative_files_nothing(build_log):
    log = build_log()
    tools, turn = _tools(log)

    result = _call(tools, "flag_contradiction", anchor="the kitchen")

    assert result["ok"] is False
    assert "no narrative to anchor the flag" in result["error"]
    assert _payloads(log, "chat", "validation_notice") == []
    assert turn.rejected == 1


def test_flag_contradiction_needs_an_anchor(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    result = _call(tools, "flag_contradiction", what="location")

    assert result["ok"] is False
    assert "needs an anchor" in result["error"]


# -- the budget --------------------------------------------------------------


def test_refused_writes_cost_attempts_never_budget(build_log):
    """Owner ruling 2026-10-21 (supersedes the charge-before-you-try rule).

    Nine refused calls (an unknown parent) no longer eat the write allowance:
    the successful establish still lands inside a FULL budget, and the refusals
    are bounded by the attempt cap instead.
    """
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    for _ in range(ENTITY_ACTION_BUDGET - 1):
        assert _call(tools, "establish_location", name="x", parent_id="loc_nope")["ok"] is False
    assert _call(tools, "establish_location", name="bathroom")["ok"] is True

    assert turn.writes == 1            # only the success spends a write
    assert turn.actions == 1
    assert turn.rejected == ENTITY_ACTION_BUDGET - 1
    assert turn.attempts == ENTITY_ACTION_BUDGET


def test_a_model_looping_on_one_mistake_hits_the_attempt_cap(build_log):
    """The loop bound that replaces the old charged-before-you-try budget."""
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)          # budget 10, cap 10 + slack 4 = 14

    refusals = []
    for _ in range(turn.max_attempts):
        result = _call(tools, "establish_location", name="x", parent_id="loc_nope")
        refusals.append(result)
    assert all(r["ok"] is False for r in refusals)
    capped = _call(tools, "establish_location", name="bathroom")
    assert capped["ok"] is False
    assert "attempts used up (14)" in capped["error"]
    assert turn.writes == 0


def test_a_spent_budget_refuses_every_write_with_the_plan_text(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    for index in range(ENTITY_ACTION_BUDGET):
        assert _call(tools, "establish_thread", name=f"thread {index}")["ok"] is True

    for name in (
        "establish_location",
        "establish_thread",
        "establish_item",
        "move_being",
        "set_attr",
        "update_object",
        "retire_object",
        "rename_being",
        "flag_contradiction",
    ):
        result = _call(tools, name, name="late", who="player", to="off_screen", key="mood", value="x", id="th_1", anchor="a")
        assert result == {"ok": False, "error": ENTITY_BUDGET_ERROR}

    assert turn.actions == ENTITY_ACTION_BUDGET
    assert len(_payloads(log, "chat", "entity_establish")) == ENTITY_ACTION_BUDGET


def test_reads_are_free(build_log):
    """The three reads never charge the budget (only writes do, §6)."""
    from fabulist.state.bts import FOLDED_STATE_TOOL, RECENT_DELTAS_TOOL, ROSTER_TOOL, bts_loop_tools

    log = build_log()
    _seeded(log)
    turn = EntityTurn(log, "chat")
    tools = {tool.name: tool for tool in bts_loop_tools(log, "chat", turn)}

    for name in (ROSTER_TOOL, FOLDED_STATE_TOOL, RECENT_DELTAS_TOOL):
        assert asyncio.run(tools[name].fn({}))["ok"] is True

    assert turn.writes == 0 and turn.actions == 0 and turn.rejected == 0


def test_mentioned_names_merges_the_ledger_with_extra_surfaces(build_log):
    from fabulist.state.entity_tools import mentioned_names

    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)
    _call(tools, "move_being", who="Seraphine", to="off_screen")

    assert mentioned_names(turn, ["Yara", "Seraphine"]) == ["Seraphine", "Yara"]


def test_an_id_shaped_surface_is_never_filed_as_a_name(build_log):
    """Live gate: the model inferred "cm_kell" from get_roster and it was filed.

    An id-shaped surface is an inference, not a name the prose showed; the door
    refuses it so the model retries with the plain name.
    """
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    result = _call(tools, "establish_item", name="hiking pack", holder="cm_kell")

    assert result["ok"] is False
    assert "not a cast member" in result["error"]
    assert [
        p for p in _payloads(log, "chat", "asset_proposal") if "cm_kell" in str(p)
    ] == []


# -- reconcile (W9.29 §12: the flags-only reconciliation pass) ---------------


def _reconcile_tools(log: EventLog, sid: str = "chat"):
    """The tool set of a RECONCILIATION turn (the reconcile tool is offered)."""
    turn = EntityTurn(log, sid, reconcile=True)
    return {tool.name: tool for tool in entity_action_tools(turn)}, turn


def test_reconcile_is_offered_on_a_reconciliation_turn_only(build_log):
    """§12: the pass is a cadence — an ordinary turn never carries the tool."""
    from fabulist.state.entity_tools import RECONCILE_TOOL

    log = build_log()
    _seeded(log)
    ordinary, _turn = _tools(log)
    reconciling, _turn = _reconcile_tools(log)

    assert RECONCILE_TOOL not in ordinary
    assert RECONCILE_TOOL in reconciling
    # LAST, so the always-offered action set stays the head of the tool list (the
    # served set is pinned as "three reads + the ten actions").
    assert list(reconciling)[-1] == RECONCILE_TOOL


def test_reconcile_files_one_flag_per_disagreement_and_never_writes_the_registry(
    build_log,
):
    """§12: flags only — a disagreement is RECORDED, the registry is untouched."""
    log = build_log()
    _seeded(log)
    seq = _narrative(log, "chat", "Yara is in the kitchen and Seraphine has softened.")
    tools, turn = _reconcile_tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]
    thread = _call(tools, "establish_thread", name="the slow courtship")["id"]
    _call(tools, "move_being", who="Yara", to=bath)
    _call(tools, "set_attr", host="Seraphine", subject="player", key="hostility", value=30)
    yara = _member_id(log, "chat", "Yara")

    result = _call(
        tools,
        "reconcile",
        beliefs=[
            # disagrees: the registry has her in the bathroom
            {"kind": "location", "who": "Yara", "at": "kitchen"},
            # agrees: the exact value the registry holds
            {"kind": "meter", "host": "Seraphine", "subject": "player", "key": "hostility", "value": 30},
            # disagrees: the meter the model half-remembers
            {"kind": "meter", "host": "Seraphine", "subject": "player", "key": "trust", "value": 5},
            # disagrees: the registry has the thread active
            {"kind": "thread", "name": "the slow courtship", "status": "resolved"},
        ],
    )

    assert result["ok"] is True
    # Two of the four beliefs disagree; the agreeing meter files nothing.
    assert result["drift"] == 3
    assert turn.flags == 3
    notices = _payloads(log, "chat", "validation_notice")
    assert len(notices) == 3
    assert all(notice["seq"] == seq for notice in notices)
    checks = [notice["flags"][0]["check"] for notice in notices]
    assert checks == ["reconciliation_drift"] * 3
    # The registry is exactly what the actions made it: the pass wrote nothing.
    reg = registry(log, "chat")
    assert reg.assignments[yara] == bath
    assert _member_id(log, "chat", "Seraphine") not in reg.assignments
    assert reg.threads[thread].status == "active"
    assert [meter.value for meter in reg.meters.values() if meter.key == "hostility"] == [30]
    assert not [meter for meter in reg.meters.values() if meter.key == "trust"]


def test_a_reconciliation_flag_carries_the_plan_shape(build_log):
    """§10's flag shape with §12's check id: what/anchored-belief/registry/fix."""
    log = build_log()
    _seeded(log)
    _narrative(log, "chat", "Yara is in the kitchen.")
    tools, _turn = _reconcile_tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]
    _call(tools, "move_being", who="Yara", to=bath)

    _call(tools, "reconcile", beliefs=[{"kind": "location", "who": "Yara", "at": "kitchen"}])

    flag = _payloads(log, "chat", "validation_notice")[0]["flags"][0]
    assert flag["check"] == "reconciliation_drift"
    assert flag["severity"] == "warn"
    assert flag["what"] == "Yara's location"
    assert flag["anchor"] == "Yara is at kitchen"
    # The registry's own answer, readable: the name plus the id a correction needs.
    assert flag["registry_says"] == f"Yara is at bathroom ({bath})"
    assert "the registry is the record" in flag["fix"]


def test_reconcile_reports_no_drift_without_filing_a_row(build_log):
    log = build_log()
    _seeded(log)
    _narrative(log, "chat")
    tools, turn = _reconcile_tools(log)
    bath = _call(tools, "establish_location", name="bathroom")["id"]
    _call(tools, "move_being", who="player", to=bath)

    result = _call(
        tools, "reconcile", beliefs=[{"kind": "location", "who": "player", "at": bath}]
    )

    assert result == {"ok": True, "drift": 0, "disagreements": []}
    assert _payloads(log, "chat", "validation_notice") == []
    # Three accepted calls (two establishes/moves + the pass itself); no flag.
    assert turn.flags == 0 and turn.actions == 3


def test_reconcile_refuses_an_unreadable_belief_and_files_nothing(build_log):
    """The tool-loop steering rule: a readable error, nothing appended."""
    log = build_log()
    _seeded(log)
    _narrative(log, "chat")
    tools, turn = _reconcile_tools(log)

    result = _call(tools, "reconcile", beliefs=[{"kind": "mood", "who": "Yara"}])

    assert result["ok"] is False
    assert "beliefs[0]: 'kind' must be one of" in result["error"]
    assert _payloads(log, "chat", "validation_notice") == []
    assert turn.rejected == 1


def test_reconcile_counts_against_the_action_budget(build_log):
    """§6's one rule: EVERY non-read call counts — the pass included."""
    from fabulist.state.entity_tools import ENTITY_BUDGET_ERROR

    log = build_log()
    _seeded(log)
    _narrative(log, "chat")
    tools, turn = _reconcile_tools(log)

    assert _call(tools, "reconcile", beliefs=[{"kind": "location", "who": "player", "at": "off_screen"}])["ok"] is True
    assert turn.writes == 1
    for index in range(ENTITY_ACTION_BUDGET - 1):
        assert _call(tools, "establish_thread", name=f"thread {index}")["ok"] is True

    assert _call(tools, "reconcile", beliefs=[]) == {
        "ok": False,
        "error": ENTITY_BUDGET_ERROR,
    }
    assert turn.actions == ENTITY_ACTION_BUDGET


def test_reconcile_drift_files_one_row_per_disagreement(build_log):
    """A restated belief is ONE flag (live gate: duplicates appeared in pairs)."""
    log = build_log()
    _seeded(log)
    from fabulist.state.cast import member_by_name, roster

    yara = member_by_name(roster(log, "chat"), "Yara").member_id
    beliefs = [
        {"kind": "meter", "host": "Yara", "key": "stress", "value": 100},
        {"kind": "meter", "host": "Yara", "key": "stress", "value": 100},
        # The live model restated one drift by id with a different anchor spelling.
        {"kind": "meter", "host": yara, "key": "stress", "value": 100},
    ]
    rows = reconcile_drift(log, "chat", beliefs)
    assert len(rows) == 1


def test_reconcile_files_one_flag_per_subject_across_repeated_calls(build_log):
    """The live model calls reconcile several times per pass, restating drifts.

    The dedupe scope is the PASS (the turn ledger), not the call: 19 flags over
    2 subjects were observed live before this pin.
    """
    from fabulist.state.bts import reconcile_loop_tools

    log = build_log()
    _seeded(log)
    turn = EntityTurn(log, "chat", reconcile=True)
    tools = {tool.name: tool for tool in reconcile_loop_tools(turn)}
    beliefs = [{"kind": "meter", "host": "Yara", "key": "stress", "value": 100}]
    _call(tools, "reconcile", beliefs=beliefs)
    _call(tools, "reconcile", beliefs=beliefs)
    _call(tools, "reconcile", beliefs=beliefs)
    notices = _payloads(log, "chat", "validation_notice")
    assert len(notices) == 1
    assert turn.flags == 1


def test_a_refused_write_costs_an_attempt_but_not_the_budget(build_log):
    """Owner ruling 2026-10-21: mistakes are free, loops are not.

    A refusal (duplicate name, malformed args, validation) used to consume the
    write budget, so a model that erred twice seeded two places fewer. Now only
    SUCCESS settles a write; every attempt still counts toward the attempt cap.
    """
    log = build_log()
    _seeded(log)
    turn = EntityTurn(log, "chat", budget=3)
    assert turn.spend() is None
    turn.settle(False)          # refused: no write spent
    assert turn.writes == 0
    assert turn.attempts == 1
    for _ in range(3):          # three productive writes still fit
        assert turn.spend() is None
        turn.settle(True)
    assert turn.writes == 3
    assert turn.attempts == 4
    # the write budget is now spent, byte-identical text
    assert turn.spend() == budget_error(3)


def test_the_attempt_cap_bounds_a_model_that_loops_on_its_mistake(build_log):
    log = build_log()
    _seeded(log)
    turn = EntityTurn(log, "chat", budget=2)   # cap = 2 + slack(4) = 6
    assert turn.max_attempts == 6
    for _ in range(6):
        assert turn.spend() is None
        turn.settle(False)                     # every attempt refused
    refusal = turn.spend()
    assert refusal is not None and "attempts used up (6)" in refusal
    assert turn.writes == 0


def test_a_root_near_an_existing_root_is_refused_with_that_root(build_log):
    """Live: the tracker added "dorm" beside "Dormitory" as a new root.

    The exact-duplicate door compares whole canonical names, so a surface-form
    drift (dorm / dormitory) slipped through and grew a parallel tree. A ROOT is
    now near-match checked; a NESTED name is not — a similar room name inside a
    parent is exactly the fix the protocol teaches.
    """
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    dorm = _call(tools, "establish_location", name="Dormitory")
    assert dorm["ok"]

    # A near-duplicate is refused whether it would have nested or opened a tree.
    refused = _call(tools, "establish_location", name="dorm")
    assert refused["ok"] is False
    assert "Dormitory" in refused["error"]
    insisted = _call(tools, "establish_location", name="dormitor", root=True)
    assert insisted["ok"] is False
    assert "Dormitory" in insisted["error"]
    # A clearly different name is still free.
    other = _call(tools, "establish_location", name="attic")
    assert other["ok"] is True


def test_an_unparented_place_nests_under_the_single_root(build_log):
    """Owner 2026-10-21 (live): prompting would not stop a 4B opening a second
    tree for a room it knew was "of house" — so the store decides."""
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)
    house = _call(tools, "establish_location", name="The Crimson Lantern")
    assert house["ok"]
    room = _call(tools, "establish_location", name="master bedroom")
    assert room["ok"]
    reg = registry(log, "chat")
    assert reg.locations[room["id"]].parent_id == house["id"]
    assert turn.actions == 2


def test_root_true_still_opens_a_separate_building(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    house = _call(tools, "establish_location", name="The Crimson Lantern")
    shed = _call(tools, "establish_location", name="Garden Shed", root=True)
    assert shed["ok"]
    reg = registry(log, "chat")
    assert reg.locations[house["id"]].parent_id is None
    assert reg.locations[shed["id"]].parent_id is None


def test_several_roots_refuse_an_unparented_place_and_name_them(build_log):
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    _call(tools, "establish_location", name="The Crimson Lantern")
    _call(tools, "establish_location", name="Mews Cottage", root=True)
    refused = _call(tools, "establish_location", name="attic")
    assert refused["ok"] is False
    assert "The Crimson Lantern" in refused["error"] and "Mews Cottage" in refused["error"]


def test_a_location_can_be_reparented_and_a_cycle_is_refused(build_log):
    """How the historical junk roots get nested (owner 2026-10-21)."""
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)
    house = _call(tools, "establish_location", name="The Crimson Lantern")
    stray = _call(tools, "establish_location", name="master bedroom", root=True)
    assert stray["ok"]
    fixed = _call(tools, "update_object", id=stray["id"], parent_id=house["id"])
    assert fixed["ok"]
    assert registry(log, "chat").locations[stray["id"]].parent_id == house["id"]
    # a child cannot become the parent of its own ancestor
    cycle = _call(tools, "update_object", id=house["id"], parent_id=stray["id"])
    assert cycle["ok"] is False
    assert "cycle" in cycle["error"]


# -- W9.32 W5: the being door is ROSTER-FIRST (the R3 fix) -------------------
#
# The bug: the player keys were matched BEFORE the roster, so a real cast member
# who shared the persona's name was shadowed by the player and her live state
# landed on `player`. The order is now id -> reserved literal -> member name ->
# player keys, with a `user`-provenance member mapping to PLAYER (W9.22: that
# member IS the player's participation layer, so live state never splits).


def test_a_member_named_player_is_reachable_by_that_name_and_player_is_the_player(
    build_log,
):
    """Steps 2 and 3 in that order: the LITERAL is the player, the NAME is the member."""
    log = build_log()
    _seeded(log)
    player_member = admit_member(log, "chat", "Player", provenance="bts", admitted_by="bts")
    tools, _ = _tools(log)

    by_name = _call(tools, "set_body", host="Player", pose="lounging")
    by_literal = _call(tools, "set_body", host="player", pose="lounging")

    assert by_name["ok"] is True and by_name["host"] == player_member
    assert by_literal["ok"] is True and by_literal["host"] == PLAYER
    reg = registry(log, "chat")
    # Two beings, two bodies: the member is a person, `player` is the user.
    assert body_for(reg, player_member) is not None
    assert body_for(reg, PLAYER) is not None


def test_a_member_sharing_the_persona_name_resolves_to_the_member(build_log):
    """THE regression: roster step 3 beats the player keys in step 4.

    The member is admitted FIRST and the persona assigned after (W3 refuses a
    fresh admission of a player name; a member from before the persona — every
    throwaway session — is exactly the case the order has to get right).
    """
    log = build_log()
    _seeded(log)
    tessa = admit_member(log, "chat", "Tessa", provenance="bts", admitted_by="user")
    _assign_persona(log, "chat", "Tessa")
    tools, _ = _tools(log)

    result = _call(tools, "set_body", host="Tessa", pose="reading")

    assert result["ok"] is True
    assert result["host"] == tessa           # not PLAYER — the roster wins
    assert registry(log, "chat").bodies[result["id"]].host == tessa


def test_the_persona_name_with_no_member_resolves_to_the_player(build_log):
    """Step 4: the persona's display name is a player key when nobody holds it."""
    log = build_log()
    _seeded(log)
    _assign_persona(log, "chat", "Wanderer")
    tools, _ = _tools(log)

    named = _call(tools, "move_being", who="Wanderer", to="off_screen")
    literal = _call(tools, "move_being", who="player", to="off_screen")

    assert named["ok"] is True and named["who"] == PLAYER
    assert literal["ok"] is True and literal["who"] == PLAYER
    assert registry(log, "chat").assignments[PLAYER] == "off_screen"


def test_an_explicit_member_id_wins_and_a_user_provenance_member_is_the_player(
    build_log,
):
    """Step 1 resolves the id; the §5 remap keys that member's live state on player."""
    log = build_log()
    _seeded(log)
    _assign_persona(log, "chat", "Wanderer")
    user_member = admit_member(
        log, "chat", "Wanderer", provenance="user", admitted_by="user"
    )
    tools, _ = _tools(log)

    by_name = _call(tools, "set_condition", host="Wanderer", key="winded", severity="minor")
    by_id = _call(tools, "set_body", host=user_member, pose="sprawled")

    assert by_name["ok"] is True and by_name["host"] == PLAYER
    assert by_id["ok"] is True and by_id["host"] == PLAYER
    reg = registry(log, "chat")
    # One live key for one person: nothing landed on the member id.
    assert all(condition.host == PLAYER for condition in reg.conditions.values())
    assert body_for(reg, user_member) is None and body_for(reg, PLAYER) is not None


def test_the_scene_host_still_resolves_for_meters_and_conditions(build_log):
    """Step 2's other literal: `scene` is a legal HOST, never a person to propose."""
    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    meter = _call(tools, "set_attr", host="scene", key="mood", value="hushed")
    condition = _call(tools, "set_condition", host="Scene", key="storm", severity="major")

    assert meter["ok"] is True
    assert condition["ok"] is True and condition["host"] == SCENE
    reg = registry(log, "chat")
    assert reg.meters[meter["id"]].host == SCENE
    assert reg.conditions[condition["id"]].host == SCENE


def test_rename_being_still_refuses_the_player_and_its_persona_name(build_log):
    """The remap keeps the persona-owned name hitting the PLAYER refusal."""
    log = build_log()
    _seeded(log)
    _assign_persona(log, "chat", "Wanderer")
    admit_member(log, "chat", "Wanderer", provenance="user", admitted_by="user")
    tools, _ = _tools(log)

    result = _call(tools, "rename_being", who="Wanderer", to="Marc")

    assert result["ok"] is False
    assert "the player is not a cast member" in result["error"]
    assert [member.display_name for member in roster(log, "chat")].count("Wanderer") == 1


# -- W9.32 W5: set_condition (§3.1/R5) ---------------------------------------


def test_set_condition_mints_once_and_upserts_on_the_same_id(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _clocked_tools(log, "20:00", "Monday, June 02, 2025")

    minted = _call(
        tools,
        "set_condition",
        host="Yara",
        key="bleeding",
        severity="major",
        source="a knife",
        expires_at="2025-06-02 21:30",
    )
    amended = _call(
        tools, "set_condition", host="Yara", key="bleeding", source="a knife, then the alley"
    )

    assert minted["ok"] is True and minted["id"].startswith("cd_")
    assert minted["severity"] == "major" and minted["expires_at"] == "2025-06-02 21:30"
    # One condition per (host, key): the second call is a patch, never a second cd_.
    assert amended["ok"] is True and amended["id"] == minted["id"]
    reg = registry(log, "chat")
    assert len(reg.conditions) == 1
    condition = reg.conditions[minted["id"]]
    assert condition.host == _member_id(log, "chat", "Yara")
    assert condition.severity == "major"      # an omitted severity never downgrades
    assert condition.source == "a knife, then the alley"
    assert condition.expires_at == "2025-06-02 21:30"
    assert turn.actions == 2 and turn.writes == 2 and turn.rejected == 0


def test_set_condition_refuses_an_unreadable_severity_and_writes_nothing(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _clocked_tools(log, "20:00", "Monday, June 02, 2025")

    result = _call(tools, "set_condition", host="Yara", key="bleeding", severity="catastrophic")

    assert result["ok"] is False
    assert "is not a condition severity" in result["error"]
    assert "major, minor, moderate" in result["error"]
    assert _payloads(log, "chat", "entity_establish") == []
    # Attempts only: the refusal never spends a write (budget still 10 per turn).
    assert turn.attempts == 1 and turn.writes == 0 and turn.rejected == 1
    assert ENTITY_ACTION_BUDGET == 10


def test_set_condition_refuses_an_expiry_the_turns_clock_has_reached(build_log):
    """§3.3: judged against THIS turn's clock, and the text quotes both instants."""
    log = build_log()
    _seeded(log)
    tools, turn = _clocked_tools(log, "21:00", "Monday, June 02, 2025")

    result = _call(
        tools,
        "set_condition",
        host="Yara",
        key="bleeding",
        expires_at="2025-06-02 20:30",
        source="a knife",
    )

    assert result["ok"] is False
    assert "is not after the current instant 2025-06-02 21:00" in result["error"]
    assert _payloads(log, "chat", "entity_establish") == []
    assert turn.rejected == 1 and turn.writes == 0


def test_a_stale_fold_cannot_accept_an_expiry_this_turn_has_passed(build_log):
    """REGRESSION (review P1-5): the fold is one turn behind mid-loop.

    20:30 is still in the FUTURE for the folded clock (20:00) and already PAST for
    the turn's own clock (21:00) — the ledger must win.
    """
    log = build_log()
    _seeded(log)
    _fold_clock(log, "20:00", "Monday, June 02, 2025")
    tools, turn = _clocked_tools(log, "21:00", "Monday, June 02, 2025")

    result = _call(
        tools, "set_condition", host="Yara", key="bleeding", expires_at="2025-06-02 20:30"
    )

    assert result["ok"] is False
    assert "is not after the current instant" in result["error"]
    assert turn.writes == 0


def test_set_condition_without_any_clock_only_checks_the_instant_format(build_log):
    """No live clock and no fold: the store's format check is the only rule."""
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    dated = _call(
        tools, "set_condition", host="Yara", key="bleeding", expires_at="2025-06-02 21:30"
    )
    junk = _call(tools, "set_condition", host="Yara", key="cold", expires_at="tomorrow")

    assert dated["ok"] is True
    assert junk["ok"] is False
    assert "must be an instant like" in junk["error"]
    assert turn.writes == 1 and turn.rejected == 1


def test_set_condition_for_an_unknown_host_proposes_and_writes_nothing(
    build_log, cast_proposals
):
    log = build_log()
    _seeded(log)
    tools, turn = _clocked_tools(log, "20:00", "Monday, June 02, 2025")

    result = _call(tools, "set_condition", host="Zahra", key="winded")

    assert result["ok"] is False
    assert "proposed for owner review" in result["error"]
    assert cast_proposals == [("chat", "Zahra", [], "bts")]
    assert _payloads(log, "chat", "entity_establish") == []
    assert turn.rejected == 1 and turn.writes == 0


# -- W9.32 W5: set_body (§3.1/R6) -------------------------------------------


def test_set_body_mints_once_and_patches_only_what_was_stated(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    minted = _call(
        tools,
        "set_body",
        host="Yara",
        pose="standing",
        clothing=["raincoat", "slip"],
        surface="rain-damp",
    )
    undressed = _call(tools, "set_body", host="Yara", clothing=["slip"])

    assert minted["ok"] is True and minted["id"].startswith("bd_")
    assert minted["clothing"] == ["raincoat", "slip"]
    assert undressed["ok"] is True and undressed["id"] == minted["id"]
    reg = registry(log, "chat")
    assert len(reg.bodies) == 1
    body = body_for(reg, _member_id(log, "chat", "Yara"))
    assert body.pose == "standing"            # an omitted field is left alone
    assert body.clothing == ("slip",)         # outer→inner, one ordered edit
    assert body.surface == "rain-damp"
    # §3.1's name synthesis: the establish row is NAMED for the being.
    assert _payloads(log, "chat", "entity_establish")[0]["name"] == "Yara"
    assert turn.actions == 2 and turn.writes == 2


def test_set_body_refuses_the_scene_and_a_call_that_states_nothing(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    scene = _call(tools, "set_body", host="scene", pose="standing")
    empty = _call(tools, "set_body", host="Yara")

    assert scene["ok"] is False
    assert "the scene has no body" in scene["error"]
    assert empty["ok"] is False
    assert "nothing to set" in empty["error"]
    assert _payloads(log, "chat", "entity_establish") == []
    assert turn.attempts == 2 and turn.rejected == 2 and turn.writes == 0


# -- W9.32 W5: the economy (§3.1/R7) ----------------------------------------


def test_establish_economy_seeds_one_purse_and_names_the_live_id(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    seeded = _call(tools, "establish_economy", denomination="pounds", balance=40)
    again = _call(tools, "establish_economy", denomination="credits")

    assert seeded["ok"] is True and seeded["id"].startswith("eco_")
    assert seeded["denomination"] == "pounds" and seeded["balance"] == 40
    assert again["ok"] is False
    assert "already has an economy in 'pounds'" in again["error"]
    assert seeded["id"] in again["error"]
    assert len(_payloads(log, "chat", "entity_establish")) == 1
    assert turn.writes == 1 and turn.rejected == 1


def _every_store_writer_once(tools) -> None:
    """One ACCEPTED call per store writer, in dependency order (the audit surface).

    ``rename_being`` is absent on purpose: its row is the cast layer's
    (``cast_patch``, actor ``engine`` in :mod:`fabulist.state.cast`), not a
    registry write. ``flag_contradiction``/``reconcile``/``propose_time`` append
    notice rows directly rather than through a writer, so they are outside this
    surface too.
    """
    stair = _call(tools, "establish_location", name="Stair")
    thread = _call(tools, "establish_thread", name="the debt")
    assert _call(tools, "establish_relationship", **{"from": "Yara", "to": "Seraphine"})[
        "ok"
    ] is True
    assert _call(tools, "establish_item", name="the ledger", holder="Yara")["ok"] is True
    assert _call(tools, "move_being", who="Yara", to=stair["id"])["ok"] is True
    assert _call(tools, "set_attr", key="tension", value="3")["ok"] is True
    assert _call(tools, "update_object", id=thread["id"], status="resolved")["ok"] is True
    assert _call(tools, "retire_object", id=thread["id"])["ok"] is True
    assert _call(tools, "set_condition", host="Yara", key="wary")["ok"] is True
    assert _call(tools, "set_body", host="Yara", pose="standing")["ok"] is True
    assert _call(tools, "establish_economy", denomination="pounds", balance=5)["ok"] is True
    assert _call(tools, "adjust_economy", amount=-2)["ok"] is True


@pytest.mark.parametrize("actor", ["bts", "planner"])
def test_every_store_writer_stamps_the_ledgers_actor(build_log, actor):
    """Attribution follows the LEDGER, never the tool (§5's ownership map).

    A tool cannot know who is driving the loop, so it must not pick an actor: every
    ``entity_*`` row this file appends carries ``EntityTurn.actor`` — ``bts`` for the
    tracker's own ledger, ``planner`` for the phases that seed and repair. Without
    that split the planner's seed and a tracker re-seed audit identically, and
    §5's "the planner is the ONLY seeder" cannot be checked against the log.
    """
    log = build_log()
    _seeded(log)
    # One write over the tracker's own budget, so the ledger cannot refuse the last
    # call (the attribution is the pin here, not the ration).
    turn = EntityTurn(log, "chat", actor=actor, budget=ENTITY_ACTION_BUDGET + 2)
    tools = {tool.name: tool for tool in entity_action_tools(turn)}

    _every_store_writer_once(tools)

    rows = [
        event
        for event in log.get_events("chat")
        if str((event.payload or {}).get("event") or "").startswith("entity_")
    ]
    assert len(rows) == 12
    assert {event.actor for event in rows} == {actor}
    assert turn.actions == 12 and turn.writes == 12


def test_establish_economy_is_attributed_to_its_ledger_and_the_store_still_plans(
    build_log,
):
    """The row says who drove the loop; the writer's own default stays ``planner``.

    The store defaults to the planner (the seeder of record, §5's ownership map) and
    the TOOL no longer overrides it with a constant: the ledger decides. A tracker
    re-seed (the post-retire path O3 allows) says ``bts``, the planner's own seed
    says ``planner``, and a bare store call keeps the store's default.
    """
    from fabulist.state.entities import establish_economy as store_establish

    log = build_log()
    _seeded(log)
    tracker, _ = _tools(log)
    planner, _ = _planner_tools(log, "bootstrap")

    assert _call(tracker, "establish_economy", denomination="pounds", balance=5)["ok"] is True
    assert _call(planner, "establish_economy", denomination="credits", balance=5)["ok"] is True
    store_establish(log, "store-only", "crowns", balance=5)

    def actors(sid: str) -> list[str]:
        return [
            event.actor
            for event in log.get_events(sid)
            if event.payload.get("event") == "entity_establish"
        ]

    assert actors("chat") == ["bts"]
    assert actors("bootstrap") == ["planner"]
    assert actors("store-only") == ["planner"]


def test_adjust_economy_moves_the_balance_and_surfaces_the_store_refusals(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _tools(log)

    no_purse = _call(tools, "adjust_economy", amount=-5)
    _call(tools, "establish_economy", denomination="pounds", balance=10)
    spent = _call(tools, "adjust_economy", amount=-4, denomination="Pounds")
    foreign = _call(tools, "adjust_economy", amount=-1, denomination="credits")
    over = _call(tools, "adjust_economy", amount=-100)
    zero = _call(tools, "adjust_economy", amount=0)

    assert no_purse["ok"] is False and "no economy yet" in no_purse["error"]
    assert spent["ok"] is True and spent["balance"] == 6
    assert foreign["ok"] is False and "a different currency" in foreign["error"]
    assert over["ok"] is False and "never goes negative" in over["error"]
    assert zero["ok"] is False and "changes nothing" in zero["error"]
    assert registry(log, "chat").economy.balance == 6
    # establish + one spend are the only writes; four refusals, attempts only.
    assert turn.actions == 2 and turn.writes == 2 and turn.rejected == 4


# -- W9.32 W5: the clock proposal (§3.3/§3.4/§3.5) --------------------------


def test_propose_time_accepts_a_move_and_leaves_one_audit_row(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _clocked_tools(log, "14:35", "Monday, June 02, 2025")
    evidence = "They talk until the light goes."

    result = _call(tools, "propose_time", to="+90", evidence=evidence)

    assert result["ok"] is True
    assert (result["time"], result["date"], result["day"]) == (
        "16:05",
        "Monday, June 02, 2025",
        1,
    )
    assert result["advanced_minutes"] == 90
    rows = _payloads(log, "chat", "clock_proposal")
    assert len(rows) == 1
    assert rows[0] == {
        "event": "clock_proposal",
        "time": "16:05",
        "date": "Monday, June 02, 2025",
        "day": 1,
        "evidence": evidence,
        "advanced_minutes": 90,
    }
    # §3.5: the node folds `turn.time_proposal`, so the ledger carries the verdict.
    assert turn.time_proposal == {
        "time": "16:05",
        "date": "Monday, June 02, 2025",
        "day": 1,
        "advanced_minutes": 90,
        "evidence": evidence,
    }
    assert turn.actions == 1 and turn.writes == 1


def test_propose_time_refuses_a_backwards_move_and_appends_nothing(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _clocked_tools(log, "14:35", "Monday, June 02, 2025")

    result = _call(tools, "propose_time", to="09:00", evidence="Morning came back around.")

    assert result["ok"] is False
    assert "the clock never runs backwards" in result["error"]
    assert _payloads(log, "chat", "clock_proposal") == []
    assert turn.time_proposal is None
    assert turn.attempts == 1 and turn.writes == 0 and turn.rejected == 1


def test_propose_time_refuses_an_over_bound_jump_and_a_silent_proposal(build_log):
    log = build_log()
    _seeded(log)
    tools, turn = _clocked_tools(log, "08:00", "Monday, June 02, 2025")

    too_far = _call(tools, "propose_time", to="+7h", evidence="The afternoon slides by.")
    silent = _call(tools, "propose_time", to="+30", evidence="   ")
    unreadable = _call(tools, "propose_time", to="half past nine", evidence="Later.")

    assert too_far["ok"] is False
    assert "360-minute jump bound" in too_far["error"]
    assert "sleep: true" in too_far["error"]
    assert silent["ok"] is False and "evidence is required" in silent["error"]
    assert unreadable["ok"] is False and "is not a time I can read" in unreadable["error"]
    assert _payloads(log, "chat", "clock_proposal") == []
    assert turn.rejected == 3 and turn.writes == 0


def test_only_the_boolean_true_flag_lifts_the_jump_bound(build_log):
    """``sleep`` is a boolean, not a truthiness test: "false" and 1 state nothing.

    A model that answers the bound's own prompt ("pass sleep: true") with the
    string "false" — or with 1 — has NOT stated a sleep, so the six-hour bound
    still applies; only the JSON boolean ``true`` lifts it. Same guard as
    ``root=args.get("root") is True`` on ``establish_location``.
    """
    log = build_log()
    _seeded(log)
    evidence = "The afternoon slides by."

    def propose(**args):
        tools, _turn = _clocked_tools(log, "08:00", "Monday, June 02, 2025")
        return _call(tools, "propose_time", to="+7h", evidence=evidence, **args)

    as_string = propose(sleep="false")
    as_number = propose(sleep=1)
    as_boolean = propose(sleep=True)

    assert as_string["ok"] is False
    assert "360-minute jump bound" in as_string["error"]
    assert as_number["ok"] is False
    assert "360-minute jump bound" in as_number["error"]
    # The real flag does what the refusal invites: a stated sleep carries 7 hours.
    assert as_boolean["ok"] is True and as_boolean["time"] == "15:00"
    # One audit row — the two refusals appended nothing.
    assert len(_payloads(log, "chat", "clock_proposal")) == 1


def test_propose_time_judges_against_the_turns_clock_not_the_stale_fold(build_log):
    """REGRESSION (review P1-5): the fold holds the PREVIOUS turn's time.

    The folded clock says 08:00, so 09:00 would look like a forward move; this
    turn's clock is 14:00 and the same proposal runs backwards.
    """
    log = build_log()
    _seeded(log)
    _fold_clock(log, "08:00", "Monday, June 02, 2025")
    tools, turn = _clocked_tools(log, "14:00", "Monday, June 02, 2025")

    result = _call(tools, "propose_time", to="09:00", evidence="Hours pass in the hall.")

    assert result["ok"] is False
    assert "the clock never runs backwards" in result["error"]
    assert "no live clock" not in result["error"]   # the ledger's clock was used
    assert _payloads(log, "chat", "clock_proposal") == []


def test_propose_time_falls_back_to_the_fold_when_the_ledger_has_no_clock(build_log):
    """The ✎ door and the planner bootstrap act between turns — the fold IS now."""
    log = build_log()
    _seeded(log)
    _fold_clock(log, "22:00", "Monday, June 02, 2025")
    tools, turn = _tools(log)
    assert turn.clock is None

    accepted = _call(tools, "propose_time", to="23:00", evidence="One more hour of it.")
    refused = _call(tools, "propose_time", to="20:00", evidence="Earlier that evening.")

    assert accepted["ok"] is True and accepted["time"] == "23:00"
    assert refused["ok"] is False
    assert "the clock never runs backwards" in refused["error"]
    # The refusal says WHICH baseline judged it.
    assert "no live clock" in refused["error"]
    assert "folded projection" in refused["error"]
    assert len(_payloads(log, "chat", "clock_proposal")) == 1


# -- W9.32 W5: the tool surface itself --------------------------------------


def test_user_writable_tools_exclude_the_clock_proposal():
    """§3.3: the ✎ door must not propose time (the owner's clock door is flat, D45)."""
    from fabulist.state.entity_tools import (
        PROPOSE_TIME_TOOL,
        USER_WRITABLE_TOOLS,
        WRITE_TOOL_NAMES,
    )

    assert len(WRITE_TOOL_NAMES) == 15
    assert PROPOSE_TIME_TOOL == "propose_time"
    assert PROPOSE_TIME_TOOL in WRITE_TOOL_NAMES
    assert PROPOSE_TIME_TOOL not in USER_WRITABLE_TOOLS
    assert set(USER_WRITABLE_TOOLS) == set(WRITE_TOOL_NAMES) - {PROPOSE_TIME_TOOL}
    # A tuple, in the declared order: the door's "unknown op" listing reads it.
    assert USER_WRITABLE_TOOLS == tuple(
        name for name in WRITE_TOOL_NAMES if name != PROPOSE_TIME_TOOL
    )


def test_the_served_action_set_is_written_in_the_declared_order(build_log):
    """The loop's tool list IS WRITE_TOOL_NAMES (the five new doors appended last)."""
    from fabulist.state.entity_tools import WRITE_TOOL_NAMES

    log = build_log()
    _seeded(log)
    tools, _ = _tools(log)

    assert list(tools) == list(WRITE_TOOL_NAMES)
    assert list(tools)[-5:] == [
        "set_condition",
        "set_body",
        "establish_economy",
        "adjust_economy",
        "propose_time",
    ]


def test_the_new_doors_are_charged_by_the_same_ledger(build_log):
    """One wrapper for every write: a spent budget refuses the new tools too."""
    log = build_log()
    _seeded(log)
    tools, turn = _clocked_tools(log, "14:35", "Monday, June 02, 2025")
    for index in range(ENTITY_ACTION_BUDGET):
        assert _call(tools, "establish_thread", name=f"thread {index}")["ok"] is True

    for name, args in (
        ("set_condition", {"host": "Yara", "key": "bleeding"}),
        ("set_body", {"host": "Yara", "pose": "standing"}),
        ("establish_economy", {"denomination": "pounds"}),
        ("adjust_economy", {"amount": 5}),
        ("propose_time", {"to": "+30", "evidence": "Time passes."}),
    ):
        assert _call(tools, name, **args) == {"ok": False, "error": ENTITY_BUDGET_ERROR}
    assert turn.actions == ENTITY_ACTION_BUDGET
    assert _payloads(log, "chat", "clock_proposal") == []
