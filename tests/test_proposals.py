"""W9.22 §4 — the proposal store: fold, decide, apply, and UNDO by append.

The review home's backend half is the one place the owner's disposition becomes
an asset write, so its rules are pinned here by name:

* the LIVE set is a fold: a terminal decision takes a proposal out, and the
  newest proposal for one asset supersedes the older ones (finding 11) while the
  collapsed ids stay reported;
* ACCEPT writes through the asset's existing append API — never a second write
  path — and captures the PRE-IMAGE it overwrote in the same step (finding 13:
  worlds/books/personas fold whole definitions latest-wins, so an undo needs it);
* UNDO is a compensating append: the pre-image goes back through the SAME API,
  an accepted ``add`` is tombstoned, and the asset's own history stays readable
  (D9/D10 — nothing is rewritten);
* with three kinds (world / book / actor) the round trip is EXACT — the folded
  definition after accept+undo equals the one before, field for field;
* mass approval appends one decision per item, so granular undos survive it;
* ``pending`` is fork-aware (a decision on a branch must not hide the parent's
  proposal) while ``inbox`` is asset-scoped (the accepted write is a catalog
  asset every session shares);
* every row this module writes passes the schema gate, and a malformed decision
  is refused at the append door.

Real seams only: a tmp file-backed :class:`~fabulist.log.EventLog`, the real
world/book/persona registries, the real promotion gate (the cross-lane producer),
and the real schema gate.
"""

from __future__ import annotations

import uuid

import pytest

from fabulist.context.books import (
    BOOKS_ANCHOR,
    append_book,
    book_by_id,
    delete_book,
    list_books,
)
from fabulist.log import EventLog
from fabulist.log.schema_gate import LogSchemaError, validate_event
from fabulist.personas.registry import PERSONAS_ANCHOR, append_persona, list_personas
from fabulist.state import proposals
from fabulist.worlds.registry import (
    append_world,
    delete_world,
    set_session_books,
    world_by_id,
    world_to_wire,
)

SID = "s1"


# -- builders ----------------------------------------------------------------


def _propose(
    log: EventLog,
    *,
    kind: str,
    action: str,
    target: str | None = None,
    patch: dict | None = None,
    origin: str = "bts",
    signal: float | None = 0.18,
    session_id: str = SID,
):
    """One ``asset_proposal`` row, shaped exactly as the producers write it."""
    return log.append_event(
        session_id,
        "system",
        origin,
        {
            "event": "asset_proposal",
            "kind": kind,
            "action": action,
            "target": target,
            "patch": dict(patch or {}),
            "origin": origin,
            "signal": signal,
        },
    )


def _persona(log: EventLog, persona_id: str):
    return next((p for p in list_personas(log) if p.persona_id == persona_id), None)


def _seed(log: EventLog, kind: str) -> tuple[str, object, dict]:
    """Define one asset of ``kind``; return (id, folded definition, amend patch)."""
    if kind == "world":
        world_id = append_world(
            log,
            {
                "world_id": "w1",
                "name": "Rain-World",
                "scenario": "It rains on the square.",
                "cast": ["p1"],
            },
        )
        return world_id, world_by_id(log, world_id), {"scenario": "The square floods."}
    if kind == "book":
        book_id = append_book(
            log,
            {
                "book_id": "b1",
                "name": "Square Lore",
                "entries": [{"keys": ["square"], "content": "It rains."}],
            },
        )
        return book_id, book_by_id(log, book_id), {
            "entries": [{"keys": ["square"], "content": "It floods."}]
        }
    persona_id = append_persona(
        log, {"persona_id": "p1", "name": "Millie", "description": "A quiet one."}
    )
    return persona_id, _persona(log, persona_id), {"description": "A loud one."}


def _read(log: EventLog, kind: str, target: str):
    if kind == "world":
        return world_by_id(log, target)
    if kind == "book":
        return book_by_id(log, target)
    return _persona(log, target)


def _rows(log: EventLog, session_id: str, kind: str) -> list[dict]:
    """Every payload of one ``system`` variant in a session (audit view)."""
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.type == "system" and event.payload.get("event") == kind
    ]


# -- the live fold -----------------------------------------------------------


def test_reject_and_dismiss_both_take_a_proposal_out_of_the_live_set(log):
    """A terminal decision wins: a declined item is never offered again."""
    rejected = _propose(log, kind="world", action="add", patch={"name": "Fen"})
    dismissed = _propose(log, kind="world", action="add", patch={"name": "Mire"})
    kept = _propose(log, kind="world", action="add", patch={"name": "Reed"})

    proposals.reject_proposal(log, SID, rejected.id)
    proposals.dismiss_proposal(log, SID, dismissed.id)

    (live,) = proposals.pending(log, SID)
    assert live.proposal_id == str(kept.id)
    # Nothing was applied, so nothing is committed and nothing is undoable.
    assert proposals.committed(log, SID) == []


def test_the_newest_proposal_for_one_asset_supersedes_the_older_ones(log):
    """Finding 11 under op-path identity (W9.23 §3.4.8): overlapping claims collapse.

    Two amends that claim the SAME field are one offer, newest-wins, and the
    collapsed ids stay reported. (Disjoint claims COEXIST — pinned separately in
    :func:`test_two_amends_with_disjoint_op_paths_coexist`.)
    """
    target, _, _ = _seed(log, "world")
    first = _propose(
        log, kind="world", action="amend", target=target, patch={"scenario": "A"}
    )
    middle = _propose(
        log,
        kind="world",
        action="amend",
        target=target,
        patch={"scenario": "B", "description": "B"},
    )
    newest = _propose(
        log, kind="world", action="amend", target=target, patch={"scenario": "C"}
    )

    (live,) = proposals.pending(log, SID)

    assert live.proposal_id == str(newest.id)
    assert live.patch == {"scenario": "C"}
    assert live.supersedes == (str(first.id), str(middle.id))
    # A collapsed id is not an offer: accepting it is refused, not double-applied.
    with pytest.raises(proposals.ProposalError, match="superseded"):
        proposals.accept_proposal(log, SID, first.id)


def test_a_new_proposal_of_the_same_name_supersedes_an_earlier_add(log):
    """An ``add`` has no target, so its identity is the NAME it proposes."""
    _propose(
        log, kind="actor", action="add", patch={"name": "Marcus", "description": "a"}
    )
    newest = _propose(
        log, kind="actor", action="add", patch={"name": "marcus", "description": "b"}
    )

    (live,) = proposals.pending(log, SID)

    assert live.proposal_id == str(newest.id)
    assert live.patch["description"] == "b"
    assert len(live.supersedes) == 1


# -- accept: the one write, and the pre-image --------------------------------


def test_accepting_an_amend_writes_the_asset_and_captures_the_pre_image(log):
    """Finding 13: the decision row is a snapshot of both sides of the write."""
    target, before, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)

    decision = proposals.accept_proposal(log, SID, filed.id)

    assert world_by_id(log, target).scenario == "The square floods."
    # The pre-image is the asset FOLD as it stood (the shape an undo re-appends).
    assert decision.pre_image == world_to_wire(before)
    assert decision.applied == world_to_wire(world_by_id(log, target))
    assert decision.kind == "world" and decision.action == "amend"
    assert decision.target == target
    # The undo handle is what the review home's committed list offers.
    (committed,) = proposals.committed(log, SID)
    assert committed.decision_id == decision.decision_id
    assert proposals.pending(log, SID) == []


def test_accepting_an_add_mints_the_definition_the_patch_names(log):
    filed = _propose(
        log,
        kind="actor",
        action="add",
        patch={"name": "Marcus", "description": "Keeps the ledger."},
        origin="promotion",
        signal=0.03,
    )

    decision = proposals.accept_proposal(log, SID, filed.id)

    created = _persona(log, decision.applied["persona_id"])
    assert created.name == "Marcus"
    assert created.description == "Keeps the ledger."
    # Nothing existed before an add, so there is no pre-image to restore.
    assert decision.pre_image is None


# -- undo: a compensating append over all three kinds ------------------------


@pytest.mark.parametrize("kind", ["world", "book", "actor"])
def test_an_amend_round_trips_exactly_through_accept_and_undo(log, kind):
    """The undo restores the pre-image field for field — across all three kinds."""
    target, before, patch = _seed(log, kind)
    filed = _propose(log, kind=kind, action="amend", target=target, patch=patch)

    decision = proposals.accept_proposal(log, SID, filed.id)
    assert _read(log, kind, target) != before  # the accept really wrote

    proposals.undo_decision(log, SID, decision.decision_id)

    assert _read(log, kind, target) == before
    assert proposals.committed(log, SID) == []


def test_an_accepted_add_is_undone_by_a_tombstone_that_keeps_the_history(log):
    """D9: undo never destroys the created definition, it removes it from the fold."""
    filed = _propose(log, kind="actor", action="add", patch={"name": "Marcus"})
    decision = proposals.accept_proposal(log, SID, filed.id)
    created = decision.applied["persona_id"]

    proposals.undo_decision(log, SID, decision.decision_id)

    assert _persona(log, created) is None
    variants = [
        payload.get("event")
        for payload in (
            event.payload
            for event in log.get_events(PERSONAS_ANCHOR)
            if event.type == "system"
        )
        if payload.get("persona_id") == created
    ]
    # The definition row AND its tombstone are both readable: nothing was rewritten.
    assert variants == ["persona", "persona_deleted"]


def test_an_accepted_remove_is_undone_back_into_the_catalog(log):
    target, before, _ = _seed(log, "book")
    filed = _propose(log, kind="book", action="remove", target=target)

    decision = proposals.accept_proposal(log, SID, filed.id)
    assert book_by_id(log, target) is None
    # The entries carry the stable key ``append_book`` mints (W9.23 §3.4.3); the
    # pre-image is still the exact fold the undo re-appends.
    assert decision.pre_image["book_id"] == "b1"
    assert decision.pre_image["name"] == "Square Lore"
    assert decision.pre_image["provenance"] == {}
    assert [
        {key: value for key, value in entry.items() if key != "entry_id"}
        for entry in decision.pre_image["entries"]
    ] == [{"keys": ["square"], "content": "It rains."}]
    assert decision.applied is None

    proposals.undo_decision(log, SID, decision.decision_id)

    assert book_by_id(log, target) == before


# -- mass approval -----------------------------------------------------------


def test_mass_accept_records_one_decision_per_item_so_each_undo_is_granular(log):
    """The UI batches; the log does not — granular undos must survive a batch."""
    filed = [
        _propose(log, kind="actor", action="add", patch={"name": name})
        for name in ("Marcus", "Ilse", "Reed")
    ]

    decisions = proposals.accept_many(log, SID, [item.id for item in filed])

    assert len(decisions) == 3
    assert len(proposals.committed(log, SID)) == 3
    assert {persona.name for persona in list_personas(log)} == {
        "Marcus",
        "Ilse",
        "Reed",
    }

    proposals.undo_decision(log, SID, decisions[1].decision_id)

    assert {persona.name for persona in list_personas(log)} == {"Marcus", "Reed"}
    assert len(proposals.committed(log, SID)) == 2


def test_mass_accept_refuses_a_batch_holding_a_stale_id_before_writing_anything(log):
    filed = _propose(log, kind="actor", action="add", patch={"name": "Marcus"})

    with pytest.raises(proposals.ProposalError, match="not a live proposal"):
        proposals.accept_many(log, SID, [filed.id, 999999])

    assert list_personas(log) == []
    assert len(proposals.pending(log, SID)) == 1


# -- scopes: session (fork-aware) vs asset (inbox) ---------------------------


def test_a_decision_on_a_branch_does_not_hide_the_proposal_on_its_parent(log):
    """Fork-aware read: a branch is a different storyline, so its verdict is local."""
    target, _, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)
    log.fork_session(SID, filed.seq, "branch")

    decision = proposals.accept_proposal(log, "branch", filed.id)

    assert [row.decision_id for row in proposals.committed(log, "branch")] == [
        decision.decision_id
    ]
    (still_pending,) = proposals.pending(log, SID)
    assert still_pending.proposal_id == str(filed.id)
    assert proposals.committed(log, SID) == []
    # The asset write is GLOBAL, which is why the inbox scope below differs.
    assert world_by_id(log, target).scenario == "The square floods."


def test_the_inbox_folds_every_sessions_live_proposals(log):
    """One fold over the union: each item says which chat proposed it."""
    first = _propose(
        log, kind="actor", action="add", patch={"name": "Marcus"}, session_id="s1"
    )
    second = _propose(
        log, kind="world", action="add", patch={"name": "Fen"}, session_id="s2"
    )
    settled = _propose(
        log, kind="actor", action="add", patch={"name": "Ilse"}, session_id="s2"
    )
    proposals.reject_proposal(log, "s2", settled.id)

    items = proposals.inbox(log)

    assert [item.proposal_id for item in items] == [str(second.id), str(first.id)]
    assert {item.session_id for item in items} == {"s1", "s2"}


def test_the_inbox_collapses_one_asset_proposed_in_two_sessions(log):
    """Finding 11's cross-session dedupe: one catalog asset, one decision."""
    target, _, _ = _seed(log, "world")
    older = _propose(
        log, kind="world", action="amend", target=target, patch={"scenario": "A"}
    )
    newer = _propose(
        log,
        kind="world",
        action="amend",
        target=target,
        patch={"scenario": "B"},
        session_id="s2",
    )

    (live,) = proposals.inbox(log)

    assert live.proposal_id == str(newer.id)
    assert live.session_id == "s2"
    assert live.supersedes == (str(older.id),)


def test_a_decision_recorded_anywhere_retires_the_item_from_the_inbox(log):
    """Asset scope: the accepted write is a catalog asset every session shares."""
    target, _, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)
    log.fork_session(SID, filed.seq, "branch")

    proposals.accept_proposal(log, "branch", filed.id)

    assert proposals.inbox(log) == []


# -- refusals ----------------------------------------------------------------


def test_a_proposal_is_decided_once_and_an_undo_is_applied_once(log):
    target, _, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)
    decision = proposals.accept_proposal(log, SID, filed.id)

    with pytest.raises(proposals.ProposalError, match="already accept"):
        proposals.accept_proposal(log, SID, filed.id)
    with pytest.raises(proposals.ProposalError, match="unknown proposal"):
        proposals.accept_proposal(log, SID, 999999)

    proposals.undo_decision(log, SID, decision.decision_id)
    with pytest.raises(proposals.ProposalError, match="already undone"):
        proposals.undo_decision(log, SID, decision.decision_id)


def test_only_an_accepted_decision_can_be_undone(log):
    filed = _propose(log, kind="world", action="add", patch={"name": "Fen"})
    decision = proposals.dismiss_proposal(log, SID, filed.id)

    with pytest.raises(proposals.ProposalError, match="applied nothing"):
        proposals.undo_decision(log, SID, decision.decision_id)


def test_an_amend_of_a_vanished_asset_is_refused_instead_of_invented(log):
    """A proposal can outlive its target (the studio deleted it meanwhile)."""
    target, _, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)
    delete_world(log, target)

    with pytest.raises(proposals.ProposalError, match="does not exist"):
        proposals.accept_proposal(log, SID, filed.id)


# -- the schema contract -----------------------------------------------------


def test_every_row_the_store_writes_passes_the_schema_gate(log):
    target, _, patch = _seed(log, "world")
    added = _propose(log, kind="actor", action="add", patch={"name": "Marcus"})
    amended = _propose(log, kind="world", action="amend", target=target, patch=patch)
    dismissed = _propose(log, kind="book", action="add", patch={"name": "Lore"})

    kept = proposals.accept_proposal(log, SID, added.id)
    restored = proposals.accept_proposal(log, SID, amended.id)
    proposals.dismiss_proposal(log, SID, dismissed.id)
    proposals.undo_decision(log, SID, restored.decision_id)

    kinds = {"asset_proposal", "proposal_decision", "proposal_undo"}
    checked = [
        event.payload
        for event in log.get_events(SID)
        if event.type == "system" and event.payload.get("event") in kinds
    ]
    assert {payload["event"] for payload in checked} == kinds
    for payload in checked:
        assert validate_event("system", payload) == []
    # The undo records the handle it compensated, not a copy of the asset.
    (undo,) = _rows(log, SID, "proposal_undo")
    assert undo["decision_id"] == restored.decision_id
    assert kept.decision_id != restored.decision_id


def test_the_schema_gate_refuses_a_malformed_decision(log):
    """A row that cannot be read back (or undone) never reaches the log."""
    base = {
        "event": "proposal_decision",
        "proposal_id": "1",
        "decision": "accept",
        "kind": "world",
        "action": "amend",
        "target": "w1",
        "pre_image": {"world_id": "w1", "name": "Rain-World"},
        "applied": {"world_id": "w1", "name": "Rain-World"},
    }
    assert validate_event("system", base) == []

    for broken in (
        {**base, "decision": "maybe"},
        {**base, "kind": "monster"},
        {**base, "pre_image": None},  # an amend that cannot be undone
        {**base, "applied": None},
        {**base, "decision": "reject"},  # a declining row cannot claim an apply
    ):
        with pytest.raises(LogSchemaError):
            log.append_event(SID, "system", "user", broken)


def test_the_schema_gate_requires_an_appliable_proposal(log):
    """A queued item the store could not apply is refused at the append door."""
    for broken in (
        # an add names the new asset; without a name the append API raises
        {"kind": "actor", "action": "add", "target": None, "patch": {}},
        # an amend names the asset it changes
        {"kind": "world", "action": "amend", "target": None, "patch": {"name": "X"}},
        # an amend patch that writes nothing would apply as a no-op
        {"kind": "world", "action": "amend", "target": "w1", "patch": {"aliases": []}},
        # only an add may omit the target
        {"kind": "book", "action": "add", "target": "b1", "patch": {"name": "Lore"}},
    ):
        with pytest.raises(LogSchemaError):
            log.append_event(
                SID,
                "system",
                "bts",
                {"event": "asset_proposal", "origin": "bts", "signal": None, **broken},
            )
    # The legal shapes pass, including a remove (no fields to change).
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "asset_proposal",
            "kind": "book",
            "action": "remove",
            "target": "b1",
            "patch": {},
            "origin": "bts",
            "signal": None,
        },
    )


def test_the_schema_gate_refuses_a_wrong_shaped_patch_value(log):
    """A value the fold would silently discard is refused at the append door.

    The live defect: a book add carried the turn's PROSE as ``entries``, the gate
    accepted it, ``append_book`` dropped every non-object entry and the owner
    accepted a proposal that wrote an empty book.
    """
    for broken in (
        # entries is a list of ST entry OBJECTS, never a paragraph
        {"kind": "book", "action": "add", "target": None,
         "patch": {"name": "Vale & Co.", "entries": "A jeweller's shop."}},
        # ... nor a list of bare strings
        {"kind": "book", "action": "add", "target": None,
         "patch": {"name": "Vale & Co.", "entries": ["A jeweller's shop."]}},
        # an id list is a list, not one id
        {"kind": "world", "action": "amend", "target": "w1",
         "patch": {"cast": "Marisol"}},
        # a dotted path whose root is not a writable mapping field
        {"kind": "actor", "action": "amend", "target": "p1",
         "patch": {"name.first": "Roz"}},
    ):
        with pytest.raises(LogSchemaError):
            log.append_event(
                SID,
                "system",
                "bts",
                {"event": "asset_proposal", "origin": "bts", "signal": None, **broken},
            )


def test_a_nested_patch_key_writes_one_mapping_key_and_keeps_its_siblings(log):
    """Coverage widening (L4): a CUSTOM extension is proposable without clobbering.

    Before this, the only way to touch one custom extension key was to propose
    the whole ``extensions`` dict — which REPLACED every sibling key on accept.
    """
    persona_id = append_persona(
        log,
        {
            "persona_id": "p1",
            "name": "Millie",
            "extensions": {"mood": "wry", "card": {"v": 2}},
        },
    )
    filed = _propose(
        log,
        kind="actor",
        action="amend",
        target=persona_id,
        patch={"extensions.mood": "cold"},
    )

    proposals.accept_proposal(log, SID, filed.id)

    extensions = _persona(log, persona_id).extensions
    assert extensions == {"mood": "cold", "card": {"v": 2}}


def test_the_schema_gate_requires_a_full_cast_candidate_patch(log):
    """``kind="cast"`` is add-only and names the member, its aliases and its origin."""
    base = {
        "event": "asset_proposal",
        "kind": "cast",
        "action": "add",
        "target": None,
        "patch": {"name": "Marisol", "aliases": ["B."], "origin": "bts"},
        "origin": "bts",
        "signal": None,
    }
    assert validate_event("system", base) == []

    for broken in (
        {**base, "patch": {"name": "Marisol", "origin": "bts"}},  # no aliases
        {**base, "patch": {"name": "Marisol", "aliases": ["B."]}},  # no origin
        {**base, "patch": {"name": "Marisol", "aliases": "B.", "origin": "bts"}},
        # a member is admitted, never amended or removed by a proposal
        {**base, "action": "amend", "target": "m1"},
    ):
        with pytest.raises(LogSchemaError):
            log.append_event(SID, "system", "bts", broken)


# -- the cast kind: a candidate the owner gates (L4) -------------------------
#
# INTEGRATION-PENDING: ``admit_from_proposal`` / ``retire_from_proposal`` are
# Lane L3's pinned seam and land in parallel. These tests stub them exactly at
# the signatures the store calls, so the store's half is pinned today and the
# real helpers only need wiring.


def _cast_stubs(monkeypatch, calls: list[tuple]):
    """Install both pinned cast helpers as recording stubs."""

    def admit(log, session_id, name, aliases, origin):
        calls.append(("admit", session_id, name, list(aliases), origin))
        return "m-1"

    def retire(log, session_id, name):
        calls.append(("retire", session_id, name))

    monkeypatch.setattr(proposals, "admit_from_proposal", admit)
    monkeypatch.setattr(proposals, "retire_from_proposal", retire)


def test_emit_cast_proposal_files_the_pinned_candidate_shape(log):
    """The candidate door: the pinned event, and NOTHING admitted yet."""
    filed = proposals.emit_cast_proposal(
        log, SID, "Marisol", ["B.", "the jeweller", "Marisol"], "bts"
    )

    assert (filed.kind, filed.action, filed.target, filed.origin) == (
        "cast",
        "add",
        None,
        "bts",
    )
    assert filed.patch == {
        "name": "Marisol",
        "aliases": ["B.", "the jeweller"],  # de-duplicated, name dropped
        "origin": "bts",
    }
    assert proposals.pending(log, SID) == [filed]
    # A candidate is a PROPOSAL: no roster row exists until the owner accepts.
    from fabulist.state.cast import roster

    assert roster(log, SID) == []


def test_accepting_a_cast_proposal_admits_through_the_pinned_helper(log, monkeypatch):
    """Accept = the cast layer's ``admit_from_proposal``, recorded as the apply."""
    calls: list[tuple] = []
    _cast_stubs(monkeypatch, calls)
    filed = proposals.emit_cast_proposal(log, SID, "Marisol", ["B."], "bts")

    decision = proposals.accept_proposal(log, SID, filed.proposal_id)

    assert calls == [("admit", SID, "Marisol", ["B."], "bts")]
    assert decision.pre_image is None
    assert decision.applied == {
        "name": "Marisol",
        "aliases": ["B."],
        "origin": "bts",
        "member_id": "m-1",
    }
    assert proposals.pending(log, SID) == []
    (committed,) = proposals.committed(log, SID)
    assert committed.decision_id == decision.decision_id
    assert committed.kind == "cast"


def test_undoing_a_cast_accept_retires_the_member_it_admitted(log, monkeypatch):
    """Undo = ``retire_from_proposal`` on the recorded name (a tombstone, D10)."""
    calls: list[tuple] = []
    _cast_stubs(monkeypatch, calls)
    filed = proposals.emit_cast_proposal(log, SID, "Marisol", [], "bts")
    decision = proposals.accept_proposal(log, SID, filed.proposal_id)

    proposals.undo_decision(log, SID, decision.decision_id)

    assert calls == [("admit", SID, "Marisol", [], "bts"), ("retire", SID, "Marisol")]
    assert proposals.committed(log, SID) == []
    assert _rows(log, SID, "proposal_undo")[0]["decision_id"] == decision.decision_id


def test_rejecting_a_cast_proposal_touches_no_member(log, monkeypatch):
    """Reject is a no-op drop: the cast layer is never called."""
    calls: list[tuple] = []
    _cast_stubs(monkeypatch, calls)
    filed = proposals.emit_cast_proposal(log, SID, "Marisol", [], "bts")

    decision = proposals.reject_proposal(log, SID, filed.proposal_id)

    assert calls == []
    assert (decision.pre_image, decision.applied) == (None, None)
    assert proposals.pending(log, SID) == []


def test_a_cast_proposal_supersedes_an_earlier_one_for_the_same_name(log):
    """The candidate fold keys on the name, so a repeat is one card (finding 11)."""
    first = proposals.emit_cast_proposal(log, SID, "Marisol", ["B."], "bts")
    second = proposals.emit_cast_proposal(log, SID, "marisol", ["B.", "the smith"], "bts")

    (live,) = proposals.pending(log, SID)
    assert live.proposal_id == second.proposal_id
    assert live.supersedes == (first.proposal_id,)


def test_a_cast_accept_without_the_pinned_seam_is_refused_not_faked(log, monkeypatch):
    """Integration honesty: no helper ⇒ a readable refusal, never a silent admit."""
    monkeypatch.setattr(proposals, "admit_from_proposal", None)
    filed = proposals.emit_cast_proposal(log, SID, "Marisol", [], "bts")

    with pytest.raises(proposals.ProposalError, match="admit_from_proposal"):
        proposals.accept_proposal(log, SID, filed.proposal_id)


def test_a_cast_accept_naming_the_persona_is_refused_readably(log):
    """The admit path's player refusal reaches the owner as a 4xx, not a 500.

    ``admit_member`` refuses a name that is the player's persona (W9.32/R3: the
    player is a node, never a cast member). The accept must translate that
    ``ValueError`` into the decide door's :class:`proposals.ProposalError`, and
    the refusal lands nothing: no member, no ``cast_admit`` row, no decision.
    """
    from fabulist.personas.registry import assign_persona

    persona_id = append_persona(log, {"name": "Wanderer"})
    assign_persona(log, SID, persona_id)
    filed = proposals.emit_cast_proposal(log, SID, "Wanderer", [], "bts")

    with pytest.raises(proposals.ProposalError) as refusal:
        proposals.accept_proposal(log, SID, filed.proposal_id)

    # The readable type, not the bare ``ValueError`` the seam raised.
    assert type(refusal.value) is proposals.ProposalError
    message = str(refusal.value)
    assert filed.proposal_id in message
    assert "the player is not a cast member" in message
    assert _rows(log, SID, "cast_admit") == []
    assert proposals.committed(log, SID) == []
    assert proposals.pending(log, SID) == [filed]


def test_the_readable_target_name_comes_from_the_catalog(log):
    """A card names its target: the emitter's label first, the catalog second."""
    target, _, patch = _seed(log, "world")
    _propose(log, kind="world", action="amend", target=target, patch=patch)

    (filed,) = proposals.pending(log, SID)
    assert filed.target_name == ""
    assert proposals.target_name(log, filed) == "Rain-World"
    assert proposals.catalog_names(log, "world") == {target: "Rain-World"}
    assert proposals.catalog_names(log, "cast") == {}


def test_an_emitter_target_name_wins_over_a_later_rename(log):
    """The label is what the proposal MEANT, not what the asset says today."""
    target, _, patch = _seed(log, "world")
    from fabulist.worlds.registry import append_world

    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "asset_proposal",
            "kind": "world",
            "action": "amend",
            "target": target,
            "target_name": "Rain-World (before the flood)",
            "patch": patch,
            "origin": "bts",
            "signal": None,
        },
    )
    append_world(
        log,
        {"world_id": target, "name": "Flood-World", "scenario": "It flooded."},
    )

    (filed,) = proposals.pending(log, SID)
    assert filed.target_name == "Rain-World (before the flood)"
    assert proposals.target_name(log, filed) == "Rain-World (before the flood)"


# -- the cross-lane producer -------------------------------------------------


def test_an_actor_proposal_filed_from_a_turn_is_acceptance_ready(log):
    """Producer -> review home seam: the row a turn files, the owner accepts.

    The producer shape is the one BTS's actor lane writes (``kind=actor``,
    ``action=add``, no target, the assembled definition in ``patch`` plus the
    justification line); the accept path is the store's. Cast ADMISSION is gated
    separately (``state.cast.admit_from_proposal``, kind ``cast``), so this pins
    the reusable-definition flavour end to end.
    """
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "asset_proposal",
            "kind": "actor",
            "action": "add",
            "target": None,
            "patch": {
                "name": "Marcus",
                "description": (
                    "Present in the scene on turn(s) 1, 2. Speaks in the scene "
                    "(1 attributed line)."
                ),
            },
            "origin": "bts",
            "signal": 0.03,
            "evidence": "judged a presence at signal 0.03",
        },
    )

    (filed,) = proposals.pending(log, SID)
    assert (filed.kind, filed.action, filed.origin) == ("actor", "add", "bts")
    assert filed.signal == pytest.approx(0.03)

    decision = proposals.accept_proposal(log, SID, filed.proposal_id)

    created = _persona(log, decision.applied["persona_id"])
    assert created.name == "Marcus"
    assert created.description == filed.patch["description"]


# -- the diff-first operation contract (W9.23 §3.4) --------------------------
#
# Every pin in this section is one half of the owner's reversal: an amendment is
# a list of operations over the CURRENT definition, the accept verifies each
# `old` against the fold before it writes anything, and a delete removes exactly
# its path. The regenerate-the-blob shape is never applied.


def _op_propose(
    log: EventLog,
    *,
    kind: str,
    target: str,
    ops: list[dict],
    action: str = "amend",
    session_id: str = SID,
):
    """One op-typed ``asset_proposal`` row, shaped as the lane parser files it."""
    return log.append_event(
        session_id,
        "system",
        "bts",
        {
            "event": "asset_proposal",
            "kind": kind,
            "action": action,
            "target": target,
            "patch": {},
            "ops": [dict(op) for op in ops],
            "origin": "bts",
            "signal": 0.5,
        },
    )


def _decisions(log: EventLog, session_id: str = SID) -> list[dict]:
    return _rows(log, session_id, "proposal_decision")


def _turns(log: EventLog, count: int, session_id: str = SID) -> None:
    """Append ``count`` committed narratives — the archive clock (§3.2)."""
    for index in range(count):
        log.append_event(
            session_id, "narrative", "narrator", {"prose": f"turn {index}", "segments": []}
        )


def _entries(book) -> list[dict]:
    return [dict(entry) for entry in book.entries]


def _unkeyed(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "entry_id"}


def test_an_op_amend_writes_only_the_paths_it_names(log):
    """A delta is applied as a delta: every other field stays byte-identical."""
    target, before, _ = _seed(log, "world")
    filed = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": "scenario",
                "old": before.scenario,
                "new": "The square floods at dusk.",
            }
        ],
    )

    decision = proposals.accept_proposal(log, SID, filed.id)

    after = world_by_id(log, target)
    assert after.scenario == "The square floods at dusk."
    assert after.description == before.description
    assert after.cast == list(before.cast)
    assert after.book_ids == list(before.book_ids)
    assert decision.pre_image == world_to_wire(before)
    assert decision.applied == world_to_wire(after)


def test_a_stale_old_refuses_and_writes_nothing(log):
    """§8.2: an ``old`` that no longer matches the fold never clobbers it."""
    target, before, _ = _seed(log, "world")
    filed = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": "scenario",
                "old": "Something the card never said.",
                "new": "The square floods at dusk.",
            }
        ],
    )

    with pytest.raises(proposals.ProposalError, match="is stale: 'scenario' changed"):
        proposals.accept_proposal(log, SID, filed.id)

    # Nothing landed: the asset is byte-identical, no decision row exists, and
    # the row stays LIVE for the owner to reject or re-propose.
    assert world_to_wire(world_by_id(log, target)) == world_to_wire(before)
    assert _decisions(log) == []
    assert [item.proposal_id for item in proposals.pending(log, SID)] == [
        str(filed.id)
    ]


def test_an_add_whose_path_now_exists_refuses(log):
    """§8.2: ``add`` means "not there yet"; someone got there first."""
    target, _, _ = _seed(log, "world")
    filed = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[{"op": "add", "path": "scenario", "new": "Another scenario."}],
    )

    with pytest.raises(proposals.ProposalError, match="is stale: 'scenario'"):
        proposals.accept_proposal(log, SID, filed.id)
    assert world_by_id(log, target).scenario == "It rains on the square."


def test_a_delete_of_an_entry_that_is_already_gone_refuses(log):
    """§8.2: a delete carries the value it removes, and verifies it first."""
    target, before, _ = _seed(log, "book")
    gone = before.entries[0]["entry_id"]
    append_book(log, {"book_id": target, "name": "Square Lore", "entries": []})
    filed = _op_propose(
        log,
        kind="book",
        target=target,
        ops=[
            {
                "op": "delete",
                "path": f"entries.{gone}",
                "old": {"keys": ["square"], "content": "It rains."},
            }
        ],
    )

    with pytest.raises(proposals.ProposalError, match="is stale: 'entries\\."):
        proposals.accept_proposal(log, SID, filed.id)


def test_a_delete_removes_exactly_its_entry(log):
    """§8.3 (owner invariant): siblings survive byte-identical, in order."""
    book_id = append_book(
        log,
        {
            "book_id": "b1",
            "name": "Vale lore",
            "entries": [
                {"keys": ["mill"], "content": "A mill.", "constant": True},
                {"keys": ["square"], "content": "A square."},
                {"keys": ["river"], "content": "A river.", "insertion_order": 3},
            ],
        },
    )
    before = _entries(book_by_id(log, book_id))
    filed = _op_propose(
        log,
        kind="book",
        target=book_id,
        ops=[
            {
                "op": "delete",
                "path": f"entries.{before[1]['entry_id']}",
                "old": {"keys": ["square"], "content": "A square."},
            }
        ],
    )

    proposals.accept_proposal(log, SID, filed.id)

    after = _entries(book_by_id(log, book_id))
    assert [entry["content"] for entry in after] == ["A mill.", "A river."]
    assert after[0] == before[0]
    assert after[1] == before[2]


def test_a_delete_removes_exactly_one_extension_key(log):
    """§8.3: the sibling extensions and every other persona field are untouched."""
    persona_id = append_persona(
        log,
        {
            "persona_id": "p1",
            "name": "Millie",
            "description": "A quiet one.",
            "extensions": {"mood": "wry", "voice": "low", "nested": {"a": 1}},
        },
    )
    before = _persona(log, persona_id)
    filed = _op_propose(
        log,
        kind="actor",
        target=persona_id,
        ops=[{"op": "delete", "path": "extensions.mood", "old": "wry"}],
    )

    proposals.accept_proposal(log, SID, filed.id)

    after = _persona(log, persona_id)
    assert after.extensions == {"voice": "low", "nested": {"a": 1}}
    assert after.description == before.description
    assert after.name == before.name


def test_a_scenario_only_amend_leaves_the_worlds_other_fields_alone(log):
    """§8.3: a world amend touching only ``scenario`` rewrites nothing else."""
    target, before, _ = _seed(log, "world")
    filed = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": "scenario",
                "old": before.scenario,
                "new": "Snow.",
            }
        ],
    )

    proposals.accept_proposal(log, SID, filed.id)

    after = world_by_id(log, target)
    assert after.scenario == "Snow."
    assert after.description == before.description
    assert after.cast == list(before.cast)
    assert after.book_ids == list(before.book_ids)


def test_an_entry_modify_merges_over_flags_the_slice_never_showed(log):
    """§3.4.5: comparison is the shown projection; the write merges over the raw."""
    book_id = append_book(
        log,
        {
            "book_id": "b1",
            "name": "Vale lore",
            "entries": [
                {
                    "keys": ["mill"],
                    "content": "A mill.",
                    "comment": "mill",
                    "constant": True,
                    "insertion_order": 2,
                    "extensions": {"st": True},
                }
            ],
        },
    )
    entry_id = book_by_id(log, book_id).entries[0]["entry_id"]
    filed = _op_propose(
        log,
        kind="book",
        target=book_id,
        ops=[
            {
                "op": "modify",
                "path": f"entries.{entry_id}",
                "old": {"keys": ["mill"], "content": "A mill.", "comment": "mill"},
                "new": {"keys": ["mill", "watermill"], "content": "A water mill."},
            }
        ],
    )

    proposals.accept_proposal(log, SID, filed.id)

    (entry,) = book_by_id(log, book_id).entries
    assert entry["content"] == "A water mill."
    assert entry["keys"] == ["mill", "watermill"]
    # Every key the slice never showed survives byte-identical.
    assert entry["comment"] == "mill"
    assert entry["constant"] is True
    assert entry["insertion_order"] == 2
    assert entry["extensions"] == {"st": True}
    assert entry["entry_id"] == entry_id


def test_an_old_echoed_with_the_slices_id_and_truncation_still_matches(log):
    """Both sides compare through the shown projection (§3.4.5)."""
    book_id = append_book(
        log,
        {"book_id": "b1", "name": "Vale lore", "entries": [{"keys": ["mill"], "content": "A mill."}]},
    )
    entry_id = book_by_id(log, book_id).entries[0]["entry_id"]
    filed = _op_propose(
        log,
        kind="book",
        target=book_id,
        ops=[
            {
                "op": "modify",
                "path": f"entries.{entry_id}",
                "old": {
                    "id": entry_id,
                    "keys": ["mill"],
                    "content": "A mill.",
                    "truncated": True,
                },
                "new": {"keys": ["mill"], "content": "A water mill."},
            }
        ],
    )

    proposals.accept_proposal(log, SID, filed.id)

    assert book_by_id(log, book_id).entries[0]["content"] == "A water mill."


def test_two_amends_with_disjoint_op_paths_coexist_and_both_apply(log):
    """§8.1/§8.9: disjoint path sets are two offers, and both verify in either order."""
    target, before, _ = _seed(log, "world")
    scenario_row = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": "scenario",
                "old": before.scenario,
                "new": "The square floods at dusk.",
            }
        ],
    )
    provenance_row = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[{"op": "add", "path": "provenance.origin", "new": "play"}],
    )

    live = proposals.pending(log, SID)
    assert {item.proposal_id for item in live} == {
        str(scenario_row.id),
        str(provenance_row.id),
    }
    assert all(item.supersedes == () for item in live)

    # Accept-applies-both: the second row verifies against the post-first fold
    # because its path set does not intersect the first one's.
    proposals.accept_proposal(log, SID, scenario_row.id)
    proposals.accept_proposal(log, SID, provenance_row.id)

    after = world_by_id(log, target)
    assert after.scenario == "The square floods at dusk."
    assert after.provenance == {"origin": "play"}


def test_intersecting_op_paths_collapse_newest_wins(log):
    """§3.4.8: an intersecting cluster is one offer; the older id rides ``supersedes``."""
    target, before, _ = _seed(log, "world")
    older = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": "scenario",
                "old": before.scenario,
                "new": "The square floods.",
            }
        ],
    )
    newer = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": "scenario",
                "old": before.scenario,
                "new": "The square floods at dusk.",
            }
        ],
    )

    (live,) = proposals.pending(log, SID)

    assert live.proposal_id == str(newer.id)
    assert live.supersedes == (str(older.id),)


def test_a_legacy_row_bridges_two_disjoint_op_rows_into_one_cluster(log):
    """§8.4 transitive-chain pin: documented, intentional, and bounded.

    No new legacy amend can be FILED after B1, so the only rows that can glue two
    disjoint op clusters together are the ones already in the log — the fold is
    honest about what they claim rather than pretending the paths are separate.
    """
    target, before, _ = _seed(log, "world")
    first = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": "description",
                "old": before.description,
                "new": "A staging post.",
            }
        ],
    )
    bridge = _propose(
        log,
        kind="world",
        action="amend",
        target=target,
        patch={"description": "A staging post.", "scenario": "Snow."},
    )
    third = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[{"op": "modify", "path": "scenario", "old": before.scenario, "new": "Snow."}],
    )

    (live,) = proposals.pending(log, SID)

    assert live.proposal_id == str(third.id)
    assert live.supersedes == (str(first.id), str(bridge.id))


def test_a_legacy_whole_value_amend_still_folds_and_applies(log):
    """§8.4: both shapes fold forever — the pre-op rows keep working newest-wins."""
    target, before, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)

    (live,) = proposals.pending(log, SID)
    assert live.ops == ()
    assert live.stale_paths == ()

    decision = proposals.accept_proposal(log, SID, filed.id)

    assert world_by_id(log, target).scenario == "The square floods."
    assert decision.pre_image == world_to_wire(before)


def test_the_wire_hydrates_a_legacy_row_from_the_current_fold(log):
    """§4 (B2 seam): a legacy amend is served as whole-field modify ops."""
    target, before, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)

    (live,) = proposals.pending(log, SID)
    ops = proposals.display_ops(log, live)

    assert ops == [
        {
            "op": "modify",
            "path": "scenario",
            "old": before.scenario,
            "new": patch["scenario"],
            "legacy": True,
        }
    ]
    # A whole-value row anchored nothing, so nothing can be stale on it.
    assert live.stale_paths == ()


def test_display_ops_serves_a_filed_op_list_verbatim(log):
    """The wire's op paths are BYTE-IDENTICAL to the filed ones (§4 B2 pin)."""
    target, before, _ = _seed(log, "world")
    path = "scenario"
    filed = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[{"op": "modify", "path": path, "old": before.scenario, "new": "Snow."}],
    )

    (live,) = proposals.pending(log, SID)
    (op,) = proposals.display_ops(log, live)

    assert op["path"] == path
    assert op["old"] == before.scenario
    assert "legacy" not in op


def test_stale_paths_are_reported_at_read_before_any_click(log):
    """§4: the review shows the stale badge using the accept's own comparison."""
    target, before, _ = _seed(log, "world")
    stale_path = "scenario"
    fresh_path = "description"
    filed = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {"op": "modify", "path": stale_path, "old": "Gone.", "new": "Snow."},
            {"op": "modify", "path": fresh_path, "old": before.description, "new": "A town."},
        ],
    )

    (live,) = proposals.pending(log, SID)

    assert live.stale_paths == (stale_path,)
    # Byte-identical to the op path the surface renders.
    assert [op["path"] for op in live.ops if op["path"] in live.stale_paths] == [
        stale_path
    ]


def test_a_vanished_target_makes_every_op_path_stale(log):
    """Nothing can be applied to an asset that is gone; the badge says so."""
    target, _, _ = _seed(log, "world")
    path = "scenario"
    _op_propose(
        log,
        kind="world",
        target=target,
        ops=[{"op": "add", "path": "scenario", "new": "Snow."}],
    )
    delete_world(log, target)

    (live,) = proposals.pending(log, SID)

    assert live.stale_paths == (path,)


def test_an_asset_write_that_refuses_becomes_a_readable_refusal(log, monkeypatch):
    """§8.2: an accept never 500s — the append API's ValueError is mapped.

    No gate-legal definition is refused by the real writers (that is what the
    gate is for: every one of their refusals is also a gate refusal), so the
    mapping is exercised at the write seam itself. What is pinned is the STORE's
    behaviour: the ValueError becomes a ``ProposalError`` the decide door can
    show, and no decision row is written.
    """
    target, _, patch = _seed(log, "book")
    filed = _propose(log, kind="book", action="amend", target=target, patch=patch)
    real = proposals._KINDS["book"]

    def refuse(log_, definition):
        raise ValueError("book requires a non-empty 'name'")

    monkeypatch.setitem(
        proposals._KINDS,
        "book",
        proposals._AssetKind(real.id_key, refuse, real.read, real.tombstone),
    )

    with pytest.raises(proposals.ProposalError, match="could not be applied"):
        proposals.accept_proposal(log, SID, filed.id)
    assert _decisions(log) == []


# -- the archive partition (W9.23 §3.2) --------------------------------------


def test_the_archive_boundary_is_ten_committed_turns(log):
    """§8.6 boundary: age 9 is live, age 10 is archived, and the row survives."""
    target, _, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)

    _turns(log, proposals.ARCHIVE_AFTER_TURNS - 1)
    (pending,) = proposals.pending(log, SID)
    assert pending.age_turns == proposals.ARCHIVE_AFTER_TURNS - 1
    assert pending.archived is False
    assert proposals.review(log, SID).archived == []

    _turns(log, 1)
    found = proposals.review(log, SID)
    assert found.pending == []
    (archived,) = found.archived
    assert archived.proposal_id == str(filed.id)
    assert archived.age_turns == proposals.ARCHIVE_AFTER_TURNS
    assert archived.archived is True


def test_a_decided_row_enters_neither_partition(log):
    """A terminal decision is out of the fold entirely (§3.2 fold order)."""
    target, _, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)
    proposals.reject_proposal(log, SID, filed.id)

    _turns(log, proposals.ARCHIVE_AFTER_TURNS + 5)

    found = proposals.review(log, SID)
    assert found.pending == [] and found.archived == []
    assert proposals.archived_ids(log, SID) == frozenset()


def test_review_reads_the_whole_home_in_one_fold(log):
    """§8.1 one-scan read: queue, archive and committed from the same fold."""
    target, _, patch = _seed(log, "world")
    aged_row = _propose(log, kind="world", action="amend", target=target, patch=patch)
    _turns(log, proposals.ARCHIVE_AFTER_TURNS)
    persona_id = append_persona(log, {"persona_id": "p2", "name": "Ilse"})
    live_row = _propose(
        log,
        kind="actor",
        action="amend",
        target=persona_id,
        patch={"description": "A tall one."},
    )
    applied = _propose(log, kind="actor", action="add", patch={"name": "Bram"})
    proposals.accept_proposal(log, SID, applied.id)

    found = proposals.review(log, SID)

    assert [item.proposal_id for item in found.archived] == [str(aged_row.id)]
    assert [item.proposal_id for item in found.pending] == [str(live_row.id)]
    (decision,) = found.committed
    assert decision.proposal_id == str(applied.id)
    assert decision.decision == "accept"


def test_a_fresh_same_path_amend_pulls_an_archived_row_out_of_the_drawer(log):
    """§8.1 archive-mutability corollary: supersede runs BEFORE the partition."""
    target, before, _ = _seed(log, "world")
    path = "scenario"
    old = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": path,
                "old": before.scenario,
                "new": "The square floods.",
            }
        ],
    )
    _turns(log, proposals.ARCHIVE_AFTER_TURNS)
    assert [item.proposal_id for item in proposals.review(log, SID).archived] == [
        str(old.id)
    ]

    fresh = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": path,
                "old": before.scenario,
                "new": "The square floods at dusk.",
            }
        ],
    )

    found = proposals.review(log, SID)
    # The PAIR is gone from both partitions; the collapsed survivor is a fresh
    # offer again (age 0), and it carries the archived id on its chip.
    assert found.archived == []
    (survivor,) = found.pending
    assert survivor.proposal_id == str(fresh.id)
    assert survivor.supersedes == (str(old.id),)


def test_a_single_id_accept_of_an_archived_row_is_legal(log):
    """§3.3: the drawer keeps per-row actions even though mass ops exclude it."""
    target, before, _ = _seed(log, "world")
    filed = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": "scenario",
                "old": before.scenario,
                "new": "The square floods at dusk.",
            }
        ],
    )
    _turns(log, proposals.ARCHIVE_AFTER_TURNS)
    assert proposals.archived_ids(log, SID) == frozenset({str(filed.id)})

    proposals.accept_proposal(log, SID, filed.id)

    assert world_by_id(log, target).scenario == "The square floods at dusk."
    # An apply may itself change the fold the op verified against: that is the
    # owner's click, not a staleness bug.
    assert proposals.review(log, SID).archived == []


def test_a_mass_accept_still_requires_a_live_id(log):
    """§3.3: ``accept_many`` keeps its live-only check (the API guards multi-sets)."""
    target, _, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)
    _turns(log, proposals.ARCHIVE_AFTER_TURNS)

    with pytest.raises(proposals.ProposalError, match="not a live proposal"):
        proposals.accept_many(log, SID, [filed.id])


def test_the_inbox_derives_age_from_the_filing_session(log):
    """§3.2/§8.8a: turns passing in ANOTHER chat never archive a queue."""
    target, _, patch = _seed(log, "world")
    filed = _propose(
        log, kind="world", action="amend", target=target, patch=patch, session_id="s1"
    )

    _turns(log, proposals.ARCHIVE_AFTER_TURNS + 3, session_id="s2")

    live, archived = proposals.inbox_review(log)
    assert [item.proposal_id for item in live] == [str(filed.id)]
    assert live[0].age_turns == 0
    assert archived == []


def test_a_branch_derives_its_own_age_while_the_parent_stays_live(log):
    """§8.8b (two-scopes doctrine): the same row, two chains, two ages."""
    target, before, _ = _seed(log, "world")
    filed = _op_propose(
        log,
        kind="world",
        target=target,
        ops=[
            {
                "op": "modify",
                "path": "scenario",
                "old": before.scenario,
                "new": "The square floods at dusk.",
            }
        ],
    )
    log.fork_session(SID, filed.seq, "branch")

    _turns(log, proposals.ARCHIVE_AFTER_TURNS, session_id="branch")

    branch = proposals.review(log, "branch")
    parent = proposals.review(log, SID)
    assert [item.proposal_id for item in branch.archived] == [str(filed.id)]
    assert branch.archived[0].age_turns == proposals.ARCHIVE_AFTER_TURNS
    assert parent.pending[0].proposal_id == str(filed.id)
    assert parent.pending[0].age_turns == 0


def test_only_committed_narratives_advance_the_archive_clock(log):
    """§3.2: the predicate is ``committed_turn_count``'s — revisions and
    ephemeral rows never age a proposal.

    A revision is its own event type (so a revised turn counts ONCE, at the
    narrative it revises), and an ephemeral row is not a committed turn at all.
    Both are pinned here because the archive drawer is otherwise unexplainable.
    """
    target, _, patch = _seed(log, "world")
    filed = _propose(log, kind="world", action="amend", target=target, patch=patch)

    log.append_revision(
        SID, filed.seq, "narrator", {"revised": {"prose": "again"}, "reason": "typo"}
    )
    log.append_event(
        SID, "narrative", "narrator", {"prose": "draft", "segments": []}, ephemeral=True
    )

    (live,) = proposals.pending(log, SID)
    assert live.age_turns == 0
    assert live.archived is False


def test_accepting_an_op_amend_keys_a_previously_unkeyed_book(log):
    """§8.6: the first re-write migrates a legacy book — no backfill script."""
    from fabulist.context.books import BOOK_EVENT, BOOKS_ANCHOR

    log.append_event(
        BOOKS_ANCHOR,
        "system",
        "user",
        {
            "event": BOOK_EVENT,
            "book_id": "b_old",
            "name": "Old lore",
            "entries": [
                {"keys": ["mill"], "content": "A mill."},
                {"keys": ["road"], "content": "A road."},
            ],
            "provenance": {},
        },
    )
    before = _entries(book_by_id(log, "b_old"))
    assert all("entry_id" not in entry for entry in before)  # legacy: unkeyed

    entry_id = "0" * 32
    filed = _op_propose(
        log,
        kind="book",
        target="b_old",
        ops=[
            {
                "op": "add",
                "path": f"entries.{entry_id}",
                "new": {"keys": ["river"], "content": "A river.", "entry_id": entry_id},
            }
        ],
    )

    proposals.accept_proposal(log, SID, filed.id)

    after = _entries(book_by_id(log, "b_old"))
    assert [entry["content"] for entry in after] == ["A mill.", "A road.", "A river."]
    # EVERY entry is keyed by the write, so both old entries become addressable.
    assert all(isinstance(entry.get("entry_id"), str) for entry in after)
    assert [_unkeyed(entry) for entry in after[:2]] == before


# -- W9.30 §4: a parented accept CREATES its own book -----------------------
#
# The D90 defect's answer: a books-lane row may target a book that does not exist
# yet (the lane parser files it with ``target_new``), and the ACCEPT — this
# module's one asset-write door — materializes the book, applies the filed entry
# adds to it, and attaches it to the chat's book registration. Every pin below is
# one half of the contract the owner locked: accept creates, reject writes
# nothing, undo compensates exactly.


def _entry_adds(
    entries: tuple[tuple[tuple[str, ...], str], ...],
) -> list[dict]:
    """``entries.<minted id>`` add ops — the shape the lane parser files."""
    ops: list[dict] = []
    for keys, content in entries:
        entry_id = uuid.uuid4().hex
        ops.append(
            {
                "op": "add",
                "path": f"entries.{entry_id}",
                "new": {"keys": list(keys), "content": content, "entry_id": entry_id},
            }
        )
    return ops


def _parented_propose(
    log: EventLog,
    *,
    name: str = "The Mill Ledger",
    entries: tuple[tuple[tuple[str, ...], str], ...] = (
        (("the greenhouse",), "Glass, warm, behind the mill."),
    ),
    session_id: str = SID,
):
    """One BORN-PARENTED books amend, shaped EXACTLY as the lane parser files it.

    ``target``/``target_name`` are the book's NAME (there is no id yet), every op
    is an entry add carrying the parser's minted ``entry_id`` (in the path AND the
    entry), and ``target_new`` is the marker the accept reads.
    """
    return log.append_event(
        session_id,
        "system",
        "bts",
        {
            "event": "asset_proposal",
            "kind": "book",
            "action": "amend",
            "target": name,
            "patch": {},
            "ops": _entry_adds(entries),
            "origin": "bts",
            "signal": 0.42,
            "target_name": name,
            "target_new": True,
            "evidence": f"The turn named {name}.",
        },
    )


def _registrations(log: EventLog, session_id: str = SID) -> list[list[str]]:
    """Every ``session_books`` payload written to the session, oldest first."""
    return [payload["book_ids"] for payload in _rows(log, session_id, "session_books")]


def _registration(log: EventLog, session_id: str = SID) -> list[str] | None:
    rows = _registrations(log, session_id)
    return rows[-1] if rows else None


def _book_names(log: EventLog) -> list[str]:
    return sorted(book.name for book in list_books(log))


def test_a_parented_accept_creates_the_book_its_entries_and_its_provenance(log):
    """§4.1: one decision row creates the parent AND applies the entry adds."""
    filed = _parented_propose(
        log,
        entries=(
            (("the greenhouse",), "Glass, warm, behind the mill."),
            (("the mill office",), "Ledgers, a lamp, one chair."),
        ),
    )
    (filed_op, *rest) = filed.payload["ops"]

    decision = proposals.accept_proposal(log, SID, filed.id)

    (book,) = list_books(log)
    assert book.name == "The Mill Ledger"
    assert [entry["content"] for entry in book.entries] == [
        "Glass, warm, behind the mill.",
        "Ledgers, a lamp, one chair.",
    ]
    # The accept wrote the parser's minted id, so the filed path still addresses
    # the entry it created (what makes a later amend of it a diff, not a rewrite).
    assert [entry["entry_id"] for entry in book.entries] == [
        filed_op["new"]["entry_id"],
        rest[0]["new"]["entry_id"],
    ]
    assert all(entry["keys"] for entry in book.entries)
    # Provenance points back at the row the owner accepted (nothing existed).
    assert book.provenance == {"origin": "proposal", "proposal_id": str(filed.id)}
    # Nothing existed before, so there is no pre-image — the marker licenses it.
    assert decision.pre_image is None
    assert decision.target_new is True
    assert decision.applied["book_id"] == book.book_id
    assert decision.kind == "book" and decision.action == "amend"
    # Every row the accept wrote passes the append door.
    for payload in _rows(log, SID, "proposal_decision"):
        assert validate_event("system", payload) == []
    assert proposals.pending(log, SID) == []


def test_a_parented_accept_attaches_the_book_to_the_session_registration_only(log):
    """§4.1/D3 + review S1: the REGISTRATION fold, never the pooled render fold.

    The chat reaches its lore through ``_entity_book_ids``, which POOLS the
    session registration with the chat persona's books and the ``world_ref``
    world's books. Writing that pool into the latest-wins registration would copy
    those ids into the session row permanently (defeating clear/detach); the
    accept therefore seeds from the registration fold alone plus the new id.
    """
    from fabulist.context.facts import _entity_book_ids

    append_world(log, {"world_id": "w_ref", "name": "Rain-World", "book_ids": ["wb1"]})
    log.append_event(
        SID, "system", "user", {"event": "world_ref", "world_id": "w_ref", "name": "Rain-World"}
    )
    append_persona(log, {"persona_id": "p1", "name": "Millie", "book_ids": ["pb1"]})
    log.append_event(SID, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    set_session_books(log, SID, ["sb1"])
    filed = _parented_propose(log)

    decision = proposals.accept_proposal(log, SID, filed.id)

    created = decision.applied["book_id"]
    # The registration keeps what the owner had and gains exactly the new id.
    assert _registration(log) == ["sb1", created]
    # The pooled RENDER fold does carry the persona/world books — that is why it
    # must never be the seed — and now also reaches the created book.
    pooled = _entity_book_ids(log, SID, log.get_events(SID))
    assert {"pb1", "wb1", "sb1", created} <= set(pooled)
    # ... while the registration row itself names none of the pooled extras.
    assert "pb1" not in _registration(log) and "wb1" not in _registration(log)


def test_a_parented_accept_of_a_name_the_catalog_already_holds_is_refused(log):
    """Review B1b: a second same-name book is the conflation, not a new asset."""
    append_book(log, {"book_id": "b_mill", "name": "the mill ledger"})
    filed = _parented_propose(log, name="The Mill Ledger")
    before = _book_names(log)
    registrations = _registrations(log)

    with pytest.raises(proposals.ProposalError, match="the mill ledger") as refused:
        proposals.accept_proposal(log, SID, filed.id)

    # Readable: it names the book that already exists and its id.
    assert "b_mill" in str(refused.value)
    # Nothing landed: no book, no registration, no decision row.
    assert _book_names(log) == before
    assert _registrations(log) == registrations
    assert _decisions(log) == []
    # The proposal is still live (the owner re-points or renames it).
    assert [row.proposal_id for row in proposals.pending(log, SID)] == [str(filed.id)]


def test_rejecting_a_parented_row_writes_nothing(log):
    """Req. 2/11: reject creates no book, no registration, no tombstone."""
    filed = _parented_propose(log)
    before = _book_names(log)

    decision = proposals.reject_proposal(log, SID, filed.id)

    assert decision.pre_image is None and decision.applied is None
    assert _book_names(log) == before == []
    assert _registrations(log) == []
    assert proposals.committed(log, SID) == []


def test_dismissing_a_parented_row_writes_nothing(log):
    """Req. 11: "not now" is also a no-op on the catalog and the registration."""
    filed = _parented_propose(log)
    before = _book_names(log)

    proposals.dismiss_proposal(log, SID, filed.id)

    assert _book_names(log) == before == []
    assert _registrations(log) == []
    assert proposals.committed(log, SID) == []


def test_a_parented_row_never_reads_as_stale(log):
    """§3.2.5: nothing exists to anchor an ``old`` against, so nothing is stale."""
    filed = _parented_propose(log)

    (live,) = proposals.pending(log, SID)

    assert live.proposal_id == str(filed.id)
    assert live.stale_paths == ()
    # The contrast the exemption exists for: the SAME ops on an ordinary row
    # whose book never existed read stale on every path.
    _op_propose(log, kind="book", target="b_gone", ops=list(filed.payload["ops"]))

    parented, ordinary = sorted(
        proposals.pending(log, SID), key=lambda row: row.proposal_id
    )

    assert parented.stale_paths == ()
    assert ordinary.stale_paths == tuple(op["path"] for op in filed.payload["ops"])


def test_same_name_parented_rows_are_one_live_row(log):
    """Review B1a: fresh-minted op paths must not let two same-name rows coexist."""
    first = _parented_propose(log, entries=((("the greenhouse",), "Glass."),))
    second = _parented_propose(
        log, entries=((("the mill office",), "Ledgers."), (("the yard",), "Mud."))
    )
    # Disjoint op paths by construction — one live offer per name, newest wins.
    assert {op["path"] for op in first.payload["ops"]} & {
        op["path"] for op in second.payload["ops"]
    } == set()

    (live,) = proposals.pending(log, SID)

    assert live.proposal_id == str(second.id)
    assert live.supersedes == (str(first.id),)
    with pytest.raises(proposals.ProposalError, match="superseded"):
        proposals.accept_proposal(log, SID, first.id)


def test_a_parented_row_does_not_collapse_with_an_ordinary_amend(log):
    """The cluster special case is scoped: an id-targeted amend keeps its own row."""
    append_book(log, {"book_id": "b_other", "name": "Other lore"})
    ordinary = _op_propose(
        log, kind="book", target="b_other", ops=_entry_adds(((("road",), "A road."),))
    )
    parented = _parented_propose(log)

    live = proposals.pending(log, SID)

    assert sorted(row.proposal_id for row in live) == sorted(
        [str(ordinary.id), str(parented.id)]
    )
    assert all(row.supersedes == () for row in live)


def test_undoing_a_parented_accept_tombstones_the_book_and_detaches_exactly(log):
    """§4.2: tombstone + the registration fold MINUS the created id, nothing else."""
    append_book(log, {"book_id": "b_ext", "name": "External lore"})
    filed = _parented_propose(log)
    decision = proposals.accept_proposal(log, SID, filed.id)
    created = decision.applied["book_id"]
    # The owner attaches something of their own in the studio meanwhile.
    set_session_books(log, SID, [created, "b_ext"])

    proposals.undo_decision(log, SID, decision.decision_id)

    assert _registration(log) == ["b_ext"]
    assert book_by_id(log, created) is None
    assert _book_names(log) == ["External lore"]
    # D9: the definition row AND its tombstone stay readable — nothing rewritten.
    tombstones = [
        payload
        for payload in (
            event.payload
            for event in log.get_events(BOOKS_ANCHOR)
            if event.type == "system"
        )
        if payload.get("book_id") == created
    ]
    assert [payload["event"] for payload in tombstones] == ["book", "book_deleted"]
    assert proposals.committed(log, SID) == []


def test_a_parented_accept_without_entry_operations_refuses(log):
    """A hand-written whole-value parented row must not create an empty book."""
    filed = log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "asset_proposal",
            "kind": "book",
            "action": "amend",
            "target": "The Mill Ledger",
            "patch": {"provenance": {"origin": "studio"}},
            "origin": "bts",
            "signal": 0.5,
            "target_name": "The Mill Ledger",
            "target_new": True,
            "evidence": "x",
        },
    )

    with pytest.raises(proposals.ProposalError, match="entry operations"):
        proposals.accept_proposal(log, SID, filed.id)

    assert _book_names(log) == []


def test_an_ordinary_book_amend_of_a_vanished_book_still_refuses(log):
    """The marker is what licenses creation: without it the stale refusal stands."""
    target, _, _ = _seed(log, "book")
    filed = _op_propose(
        log, kind="book", target=target, ops=_entry_adds(((("road",), "A road."),))
    )
    delete_book(log, target)

    with pytest.raises(proposals.ProposalError, match="does not exist"):
        proposals.accept_proposal(log, SID, filed.id)

    assert _registrations(log) == []


def test_the_parsers_parented_row_is_what_the_accept_materializes(log):
    """Both halves in one breath: the lane's reply, appended verbatim, accepts."""
    from fabulist.state.proposal_lanes import (
        lane_spec,
        parse_proposal_batch,
        proposal_event_payload,
    )

    rows, dropped = parse_proposal_batch(
        {
            "proposals": [
                {
                    "action": "amend",
                    "target": "The Mill Ledger",
                    "ops": [
                        {
                            "op": "add",
                            "path": "entries",
                            "new": {"keys": ["the greenhouse"], "content": "Glass."},
                        }
                    ],
                    "evidence": "The greenhouse behind the mill is named.",
                }
            ]
        },
        lane_spec("books"),
        [],
    )

    assert dropped == []
    filed = log.append_event(SID, "system", "bts", proposal_event_payload(rows[0]))
    decision = proposals.accept_proposal(log, SID, filed.id)

    (book,) = list_books(log)
    assert book.name == "The Mill Ledger"
    assert [entry["keys"] for entry in book.entries] == [["the greenhouse"]]
    assert _registration(log) == [book.book_id]
    assert decision.applied["book_id"] == book.book_id


def test_an_accepted_book_add_attaches_to_the_session_and_undo_detaches(tmp_path):
    """W9.30 live gate: the books lane emits plain ADD rows (patch with name +
    entries) at least as readily as parented amends — both shapes must reach the
    prompt. An accepted add attaches the created book; the undo detaches it."""
    from fabulist.state.proposals import _session_books_registration

    log = EventLog(tmp_path / "l.db")
    filed = _propose(
        log,
        kind="book",
        action="add",
        target=None,
        patch={
            "name": "Lantern Ledger",
            "entries": [
                {
                    "keys": ["lantern"],
                    "content": "Every lamp lit in the taproom, listed by night.",
                }
            ],
        },
    )
    decision = proposals.accept_proposal(log, SID, filed.id)
    created = decision.applied["book_id"]
    assert created in _session_books_registration(log, SID)
    # the owner attaches another book meanwhile; the undo preserves it
    other = append_book(log, {"name": "Owner Notes", "entries": []})
    set_session_books(log, SID, [created, other])
    proposals.undo_decision(log, SID, decision.decision_id)
    assert _session_books_registration(log, SID) == [other]
