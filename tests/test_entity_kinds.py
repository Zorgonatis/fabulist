"""W9.32 W1 — the registry's three new object kinds: conditions, bodies, economy.

The store layer of plan §3.1, and therefore the pins are STORE pins: identity
(two ``set_condition`` calls are one ``cd_`` object; a body is keyed by its host;
an economy is one per session), the two writer validations that the tool door
turns into corrective text (severity vocabulary, instant format), the money
guardrails (non-zero amount, no cross-currency, a balance that never goes
negative), the engine sweep (``expire_conditions`` retires by the clock and is
idempotent by tombstone), and the one place this kind deliberately leaves the
D10 resurrection shape (a re-established condition is a NEW occurrence).

Payloads are built through the writers where the writer contract is the subject
(the ``test_entities`` convention); the fold-drops-a-nameless-row pin is
hand-built, because that is a replay fact about a row the writers never produce.
Every payload a writer appends is also pushed back through
``schema_gate.validate_entity_payload`` — the W0 gate and the W1 writers must
agree on the shapes, and that agreement is what a silent drift would break.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.log.schema_gate import validate_entity_payload
from fabulist.state.cast import admit_member
from fabulist.state.entities import (
    ENTITY_ESTABLISH_EVENT,
    ENTITY_RETIRE_EVENT,
    ENTITY_UPDATE_EVENT,
    PLAYER,
    SCENE,
    Condition,
    EntityError,
    active_conditions,
    adjust_economy,
    being_render,
    body_for,
    conditions_for,
    economy_of,
    establish_economy,
    expire_conditions,
    expired_conditions,
    flat_render,
    registry,
    retire_object,
    set_body,
    set_condition,
    update_object,
)

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


def _payloads(log: EventLog, sid: str, kind: str) -> list[dict]:
    return [e.payload for e in log.get_events(sid) if e.payload.get("event") == kind]


def test_set_condition_upserts_one_id_and_the_second_call_is_a_patch(build_log):
    """Identity is ``(host, key)``: turn 2's "suspicious" is turn 1's object.

    The establish carries the synthesized ``name`` (the schema gate requires one on
    every kind), and the second call patches only what it stated — an omitted
    expiry must not be silently restated as "permanent".
    """
    log = build_log()
    sid = "chat"
    first = set_condition(
        log,
        sid,
        YARA,
        "suspicious",
        severity="minor",
        source="the letter",
        expires_at="2025-06-03 21:30",
    )
    second = set_condition(log, sid, YARA, "Suspicious", severity="major")

    assert isinstance(first, Condition)
    assert first.id.startswith("cd_")
    assert second.id == first.id
    assert second.severity == "major"
    assert second.expires_at == "2025-06-03 21:30"  # untouched by the second call

    establishes = _payloads(log, sid, ENTITY_ESTABLISH_EVENT)
    assert len(establishes) == 1
    assert establishes[0] == {
        "event": ENTITY_ESTABLISH_EVENT,
        "kind": "condition",
        "id": first.id,
        "name": "suspicious",
        "key": "suspicious",
        "host": YARA,
        "severity": "minor",
        "source": "the letter",
        "expires_at": "2025-06-03 21:30",
    }
    patches = _payloads(log, sid, ENTITY_UPDATE_EVENT)
    assert [row["patch"] for row in patches] == [{"severity": "major"}]
    assert len(conditions_for(registry(log, sid), YARA)) == 1


def test_an_omitted_severity_never_downgrades_a_stored_one(build_log):
    """The default belongs to FIRST SIGHT: a source-only amendment keeps "major"."""
    log = build_log()
    sid = "chat"
    set_condition(log, sid, YARA, "bleeding", severity="major", source="the knife")
    amended = set_condition(log, sid, YARA, "bleeding", source="the knife, and the fall")

    assert amended.severity == "major"
    patches = _payloads(log, sid, ENTITY_UPDATE_EVENT)
    assert [row["patch"] for row in patches] == [{"source": "the knife, and the fall"}]


def test_a_condition_stating_nothing_new_is_refused_and_appends_nothing(build_log):
    log = build_log()
    sid = "chat"
    set_condition(log, sid, YARA, "wary", severity="moderate", source="the noise")
    with pytest.raises(EntityError) as exc:
        set_condition(log, sid, YARA, "wary", severity="moderate", source="the noise")
    assert "already" in str(exc.value)
    assert _payloads(log, sid, ENTITY_UPDATE_EVENT) == []


def test_set_condition_refuses_an_unknown_severity_and_a_malformed_instant(build_log):
    log = build_log()
    sid = "chat"
    with pytest.raises(EntityError) as severity:
        set_condition(log, sid, YARA, "dazed", severity="catastrophic")
    assert "major, minor, moderate" in str(severity.value)

    with pytest.raises(EntityError) as instant:
        set_condition(log, sid, YARA, "dazed", expires_at="tomorrow evening")
    assert "2025-06-03 21:30" in str(instant.value)
    assert _payloads(log, sid, ENTITY_ESTABLISH_EVENT) == []


def test_a_loose_instant_is_repadded_so_string_order_is_clock_order(build_log):
    """The expiry comparison is plain ``<=``, so the stored text must sort by time."""
    log = build_log()
    sid = "chat"
    condition = set_condition(log, sid, YARA, "dazed", expires_at="2025-6-3 7:5")
    assert condition.expires_at == "2025-06-03 07:05"


def test_a_condition_may_hang_on_the_scene_but_not_on_the_absence_bucket(build_log):
    log = build_log()
    sid = "chat"
    storm = set_condition(log, sid, SCENE, "storm")
    assert storm.host == SCENE
    assert conditions_for(registry(log, sid), SCENE) == (storm,)

    with pytest.raises(EntityError) as exc:
        set_condition(log, sid, "off-screen", "storm")
    assert "absence bucket" in str(exc.value)


def test_set_body_mints_once_per_being_and_patches_afterwards(build_log):
    """One body per host: the duplicate door here is identity, not an error path."""
    log = build_log()
    sid = "chat"
    first = set_body(
        log,
        sid,
        YARA,
        pose="standing",
        clothing=["coat", "nightgown", "slippers"],
        surface="a faint bruise",
    )
    assert first.id.startswith("bd_")
    assert first.clothing == ("coat", "nightgown", "slippers")

    second = set_body(log, sid, YARA, clothing="nightgown, slippers")
    assert second.id == first.id
    assert second.pose == "standing"  # an omitted field is left alone
    assert second.surface == "a faint bruise"
    assert second.clothing == ("nightgown", "slippers")

    bodies = [row for row in _payloads(log, sid, ENTITY_ESTABLISH_EVENT) if row["kind"] == "body"]
    assert len(bodies) == 1
    # The wire carries the layer list; the fold carries the ordered tuple.
    assert bodies[0]["clothing"] == ["coat", "nightgown", "slippers"]
    assert bodies[0]["name"] == YARA  # no roster entry: the host key is the label
    patches = _payloads(log, sid, ENTITY_UPDATE_EVENT)
    assert [row["patch"] for row in patches] == [{"clothing": ["nightgown", "slippers"]}]
    assert body_for(registry(log, sid), YARA) == second


def test_a_body_is_named_for_the_beings_roster_display(build_log):
    """``name`` is required on every establish, so the writer synthesizes a display."""
    log = build_log()
    sid = "chat"
    member_id = admit_member(log, sid, "Yara", provenance="card", admitted_by="engine")
    set_body(log, sid, member_id, pose="sprawled")
    bodies = [row for row in _payloads(log, sid, ENTITY_ESTABLISH_EVENT) if row["kind"] == "body"]
    assert bodies[0]["name"] == "Yara"
    assert bodies[0]["host"] == member_id


def test_the_scene_and_a_room_have_no_body_and_an_empty_write_is_refused(build_log):
    log = build_log()
    sid = "chat"
    with pytest.raises(EntityError) as scene:
        set_body(log, sid, SCENE, pose="looming")
    assert "no body" in str(scene.value)
    with pytest.raises(EntityError) as empty:
        set_body(log, sid, YARA)
    assert "nothing to set" in str(empty.value)
    assert _payloads(log, sid, ENTITY_ESTABLISH_EVENT) == []


def test_establish_economy_is_a_singleton_and_adjust_appends_a_string_balance(build_log):
    log = build_log()
    sid = "chat"
    purse = establish_economy(log, sid, "credits", balance=85, note="the float")
    assert purse.id.startswith("eco_")
    assert purse.denomination == "credits"
    assert purse.balance == 85
    assert economy_of(registry(log, sid)) == purse

    after = adjust_economy(log, sid, -30, denomination="Credits")
    assert after.balance == 55
    assert after.id == purse.id
    patches = _payloads(log, sid, ENTITY_UPDATE_EVENT)
    assert [row["patch"] for row in patches] == [{"balance": "55"}]

    with pytest.raises(EntityError) as second:
        establish_economy(log, sid, "gold", balance=1)
    assert purse.id in str(second.value)

    # The O3 channel: retiring the purse frees the kind for an audited re-seed.
    retire_object(log, sid, purse.id, reason="the currency changed")
    assert economy_of(registry(log, sid)) is None
    gold = establish_economy(log, sid, "gold", balance=3)
    assert gold.denomination == "gold"
    assert economy_of(registry(log, sid)) == gold


def test_update_object_reaches_the_new_kinds_and_refuses_the_identity_fields(build_log):
    """The ✎ door is the same one door: the patch vocabulary grew, identity did not."""
    log = build_log()
    sid = "chat"
    condition = set_condition(log, sid, YARA, "wary")
    update_object(log, sid, condition.id, note="watching the door")
    assert registry(log, sid).conditions[condition.id].note == "watching the door"
    with pytest.raises(EntityError) as identity:
        update_object(log, sid, condition.id, name="paranoid")
    assert "expires_at, note, severity, source" in str(identity.value)

    purse = establish_economy(log, sid, "credits", balance=5)
    update_object(log, sid, purse.id, note="the float")
    assert economy_of(registry(log, sid)).note == "the float"
    with pytest.raises(EntityError) as amount:
        update_object(log, sid, purse.id, name="gold")
    assert "balance, note" in str(amount.value)


def test_adjust_economy_refusals_name_the_fix_and_append_nothing(build_log):
    log = build_log()
    sid = "chat"
    with pytest.raises(EntityError) as missing:
        adjust_economy(log, sid, 5)
    assert "establish_economy" in str(missing.value)

    establish_economy(log, sid, "credits", balance=10)
    with pytest.raises(EntityError) as zero:
        adjust_economy(log, sid, 0)
    assert "0 changes nothing" in str(zero.value)
    with pytest.raises(EntityError) as currency:
        adjust_economy(log, sid, 5, denomination="gold")
    assert "different currency" in str(currency.value)
    with pytest.raises(EntityError) as overdrawn:
        adjust_economy(log, sid, -11)
    assert "never goes negative" in str(overdrawn.value)
    assert _payloads(log, sid, ENTITY_UPDATE_EVENT) == []
    assert economy_of(registry(log, sid)).balance == 10


def test_expire_conditions_retires_by_the_clock_and_is_idempotent(build_log):
    log = build_log()
    sid = "chat"
    passed = set_condition(log, sid, YARA, "suspicious", expires_at="2025-06-03 21:30")
    pending = set_condition(log, sid, YARA, "wary", expires_at="2025-06-03 23:00")
    forever = set_condition(log, sid, PLAYER, "scarred")

    reg = registry(log, sid)
    assert expired_conditions(reg, "2025-06-03 22:00") == (passed,)
    assert active_conditions(reg, "2025-06-03 22:00") == (pending, forever)
    assert active_conditions(reg, "2025-06-03 21:29") == (passed, pending, forever)

    assert expire_conditions(log, sid, now="2025-06-03 22:00") == [passed.id]
    retires = _payloads(log, sid, ENTITY_RETIRE_EVENT)
    assert retires == [
        {
            "event": ENTITY_RETIRE_EVENT,
            "id": passed.id,
            "reason": "expired at 2025-06-03 21:30",
        }
    ]
    clocked = [e for e in log.get_events(sid) if e.payload.get("event") == ENTITY_RETIRE_EVENT]
    assert clocked[0].actor == "clock"

    # Idempotent by tombstone: the retired condition is gone, so nothing re-retires.
    assert expire_conditions(log, sid, now="2025-06-03 22:00") == []
    assert expire_conditions(log, sid, now="2025-06-04 09:00") == [pending.id]
    assert set(registry(log, sid).conditions) == {forever.id}


def test_a_re_established_condition_is_a_new_occurrence_not_a_resurrection(build_log):
    """The one kind that leaves the D10 shape: a healed wound and a new wound.

    Reviving the tombstoned id would make the expiry sweep's own history attach to a
    different occurrence, so the second establish mints the next counter index.
    """
    log = build_log()
    sid = "chat"
    first = set_condition(log, sid, YARA, "bleeding", severity="major", source="the knife")
    retire_object(log, sid, first.id, reason="bandaged")
    again = set_condition(log, sid, YARA, "bleeding", severity="minor")

    assert again.id != first.id
    establishes = _payloads(log, sid, ENTITY_ESTABLISH_EVENT)
    assert [row["id"] for row in establishes] == [first.id, again.id]
    assert len(registry(log, sid).conditions) == 1
    assert registry(log, sid).conditions[again.id] == again


def test_the_fold_still_drops_a_nameless_establish(build_log):
    """``name`` stays universally required — the writers comply, the fold enforces it.

    Hand-built events through a stored-log stub: the append door's schema gate
    refuses a nameless establish outright (W0), so this is a REPLAY fact about a row
    the writers can never produce, which is exactly what the fold must stay tolerant
    of.
    """
    from fabulist.log.events import Event

    class _StoredLog:
        def __init__(self, events):
            self._events = events

        def get_chain_events(self, _session_id):
            return self._events

    def _stored(seq: int, payload: dict) -> Event:
        return Event(
            id=seq,
            ts="2026-10-21T00:00:00+00:00",
            schema_version=1,
            session_id="chat",
            seq=seq,
            type="system",
            actor="bts",
            payload=payload,
        )

    stored = _StoredLog(
        [
            _stored(
                1,
                {
                    "event": ENTITY_ESTABLISH_EVENT,
                    "kind": "condition",
                    "id": "cd_hand_built",
                    "host": YARA,
                    "key": "dazed",
                    "severity": "minor",
                },
            ),
            _stored(
                2,
                {
                    "event": ENTITY_ESTABLISH_EVENT,
                    "kind": "body",
                    "id": "bd_hand_built",
                    "host": YARA,
                    "pose": "slumped",
                },
            ),
            _stored(
                3,
                {
                    "event": ENTITY_ESTABLISH_EVENT,
                    "kind": "economy",
                    "id": "eco_hand_built",
                    "denomination": "credits",
                    "balance": 5,
                },
            ),
        ]
    )
    reg = registry(stored, "chat")
    assert reg.conditions == {}
    assert reg.bodies == {}
    assert reg.economy is None
    assert body_for(reg, YARA) is None


def test_every_new_writer_payload_passes_the_schema_gate(build_log):
    log = build_log()
    sid = "chat"
    set_condition(log, sid, YARA, "wary", expires_at="2025-06-03 23:00")
    set_condition(log, sid, YARA, "wary", severity="major")
    set_body(log, sid, YARA, pose="standing", clothing=["coat"])
    establishment = establish_economy(log, sid, "credits", balance=5)
    adjust_economy(log, sid, 5)
    expire_conditions(log, sid, now="2025-06-04 00:00")

    entity_rows = [
        event.payload
        for event in log.get_events(sid)
        if str(event.payload.get("event", "")).startswith("entity_")
    ]
    assert len(entity_rows) >= 5
    for payload in entity_rows:
        assert validate_entity_payload(payload) == []
    assert economy_of(registry(log, sid)).id == establishment.id


def test_the_writers_default_their_actors(build_log):
    """Who wrote what is the ownership map (§5): planner seeds, tracker and clock move."""
    log = build_log()
    sid = "chat"
    set_condition(log, sid, YARA, "wary", expires_at="2025-06-03 23:00")
    set_body(log, sid, YARA, pose="standing")
    establish_economy(log, sid, "credits", balance=5)
    adjust_economy(log, sid, 5)
    expire_conditions(log, sid, now="2025-06-04 00:00")

    actors = [event.actor for event in log.get_events(sid)]
    assert actors == ["bts", "bts", "planner", "bts", "clock"]


def test_the_new_kinds_never_leak_into_the_flat_render_helpers(build_log):
    """``flat_render``/``being_render`` are byte-compatible: no new keys (§3.1)."""
    log = build_log()
    sid = "chat"
    set_condition(log, sid, PLAYER, "bleeding")
    set_body(log, sid, PLAYER, pose="slumped", clothing=["coat"])
    establish_economy(log, sid, "credits", balance=5)
    reg = registry(log, sid)
    assert flat_render(reg) == {}
    assert being_render(reg, PLAYER) == ""
