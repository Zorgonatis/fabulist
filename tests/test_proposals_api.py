"""W9.22 §4 — the proposals API: the review home's queue, decisions, and UNDO.

Exercises the REAL FastAPI app (``create_app`` + httpx ASGI transport, so the
router is mounted exactly as the engine mounts it) over a real tmp file-backed
:class:`~fabulist.log.EventLog`, driving the real proposal store behind it: an
accept through these routes really writes the persona/world catalog asset and an
undo really re-appends the pre-image. No handler-level fakes, no stubbed store.

Rules pinned by name:

* the reads serve the pinned cross-lane wire shape (``pending`` / ``committed``),
  including ``evidence: null`` — no log row records an evidence line today;
* accept writes the asset AND returns the decision row the surface lists;
* reject/dismiss leave the live set and write no asset;
* a SET of ids is decided one at a time: a stale id lands in ``rejected`` and the
  rest still apply (mass approval), and every item gets its own decision row so a
  granular undo survives a mass accept (plan §4 finding 13);
* when NOTHING in the set can be decided the request is a 400 carrying ``detail``
  and the per-id list (the pinned contract's second error shape);
* undo restores the pre-image and takes the item off the committed list, while
  the proposal stays decided (an undo is not a re-open);
* the inbox groups the cross-session fold by session, only for sessions with a
  pending item, titled from the rail's own fold with a session-id fallback;
* unknown sessions are 404 and malformed bodies / unknown decision handles are
  400 ``{"detail"}`` (``ProposalError`` -> 400).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.personas.registry import append_persona, list_personas
from fabulist.state import proposals as store
from fabulist.worlds.registry import append_world, world_by_id

SID = "s1"
OTHER = "s2"

#: The pinned cross-lane shape of one live proposal (plan §4, contract).
#: ``target_name`` is the L4 additive amendment: the readable label for the
#: opaque ``target`` id, so a surface can SHOW a name and still ACT on an id.
#: The W9.23 §4 amendments are the next four: the operation list (filed or
#: server-hydrated), the derived age partition, and the read-time staleness.
#: ``target_new`` is W9.30 §6: the born-parented marker (old rows fold as false).
PROPOSAL_KEYS = {
    "id",
    "kind",
    "action",
    "target",
    "target_name",
    "patch",
    "ops",
    "origin",
    "signal",
    "evidence",
    "supersedes",
    "archived",
    "age_turns",
    "stale_paths",
    "target_new",
}

#: The pinned cross-lane shape of one decision (``kind``/``action`` are the
#: W9.23 §4 amendment: the committed list names what an applied item did;
#: ``target_new`` is W9.30 §6's marker, licensed only on a born-parented accept).
DECISION_KEYS = {
    "decision_id",
    "proposal_id",
    "decision",
    "kind",
    "action",
    "pre_image",
    "applied",
    "applied_summary",
    "target_new",
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None):
        app = create_app(engine or FakeTurnEngine(), log_path)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# -- seeding (the real producers' row shapes) --------------------------------


def _session(log: EventLog, session_id: str, title: str) -> None:
    """The app factory's own title anchor (what the rail's fold reads)."""
    log.append_event(
        session_id,
        "system",
        "system",
        {"title": title, "event": "session_created"},
    )


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


def _parented(
    log: EventLog,
    *,
    name: str = "The Mill Ledger",
    entries: tuple[tuple[tuple[str, ...], str], ...] = (
        (("the greenhouse",), "Glass, warm, behind the mill."),
        (("the mill office",), "Ledgers, a lamp, one chair."),
    ),
):
    """One BORN-PARENTED books amend, shaped exactly as the lane parser files it."""
    ops = []
    for keys, content in entries:
        entry_id = uuid.uuid4().hex
        ops.append(
            {
                "op": "add",
                "path": f"entries.{entry_id}",
                "new": {"keys": list(keys), "content": content, "entry_id": entry_id},
            }
        )
    return log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "asset_proposal",
            "kind": "book",
            "action": "amend",
            "target": name,
            "patch": {},
            "ops": ops,
            "origin": "bts",
            "signal": 0.42,
            "target_name": name,
            "target_new": True,
            "evidence": f"The turn named {name}.",
        },
    )


def _persona_names(log: EventLog) -> set[str]:
    return {persona.name for persona in list_personas(log)}


# -- reads -------------------------------------------------------------------


def test_pending_and_committed_reads_serve_the_pinned_wire_shape(build_app):
    """The queue is the store's fold, projected onto the agreed keys only."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        filed = _propose(
            log,
            kind="actor",
            action="add",
            patch={"name": "Marcus", "description": "Keeps the ledger."},
            origin="promotion",
            signal=0.03,
        )

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get(f"/api/proposals?session={SID}")).json()

    body = run(scenario())

    assert set(body) == {"pending", "archived", "committed"}
    assert body["archived"] == []
    (pending,) = body["pending"]
    assert set(pending) == PROPOSAL_KEYS
    # ``id`` is the log row's handle; ``proposal_id`` is the store's spelling of
    # the same thing and must NOT leak onto the wire.
    assert pending["id"] == str(filed.id)
    assert pending["kind"] == "actor"
    assert pending["action"] == "add"
    assert pending["origin"] == "promotion"
    assert pending["patch"] == {"name": "Marcus", "description": "Keeps the ledger."}
    assert pending["signal"] == 0.03
    assert pending["supersedes"] == []
    # An ``add`` names its asset in the patch, so it has no target to label.
    assert pending["target_name"] is None
    # W9.30 §6: every row written before the born-parented marker folds as false.
    assert pending["target_new"] is False
    # No log row records an evidence line (the lanes strip it at the append
    # door); the contract keeps the slot with a null so the surface can render it.
    assert pending["evidence"] is None
    # Nothing accepted yet, so nothing is committed and nothing is undoable.
    assert body["committed"] == []


# -- accept: the one write ---------------------------------------------------


def test_an_amend_card_names_its_target(build_app):
    """L4: the queue serves a readable label next to the opaque target id."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        world_id = append_world(
            log, {"world_id": "w1", "name": "Miller's Bend", "description": "A town."}
        )
        _propose(
            log,
            kind="world",
            action="amend",
            target=world_id,
            patch={"description": "The mill burned down."},
        )

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get(f"/api/proposals?session={SID}")).json()

    body = run(scenario())

    (pending,) = body["pending"]
    assert pending["target"] == world_id  # still the id the decide door takes
    assert pending["target_name"] == "Miller's Bend"
    # Reading the label wrote nothing.
    assert world_by_id(app.state.log, world_id).description == "A town."


def test_a_cast_candidate_rides_the_wire_and_reads_as_membership(build_app, monkeypatch):
    """L4 kind="cast": the wire shape, the accept, and the committed wording.

    INTEGRATION-PENDING: the cast layer's admit helper is Lane L3's; it is
    stubbed at its pinned signature here so the API half is pinned now.
    """
    build, log_path = build_app
    monkeypatch.setattr(store, "admit_from_proposal", lambda *args: "m-1")
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        filed = store.emit_cast_proposal(log, SID, "Marisol", ["B."], "bts")

    app = build()

    async def scenario():
        async with client_for(app) as client:
            queue = (await client.get(f"/api/proposals?session={SID}")).json()
            decided = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [filed.proposal_id], "decision": "accept"},
            )
            after = (await client.get(f"/api/proposals?session={SID}")).json()
            return queue, decided, after

    queue, decided, after = run(scenario())

    (pending,) = queue["pending"]
    assert set(pending) == PROPOSAL_KEYS
    assert pending["kind"] == "cast"
    assert pending["action"] == "add"
    assert pending["target"] is None and pending["target_name"] is None
    assert pending["patch"] == {"name": "Marisol", "aliases": ["B."], "origin": "bts"}

    assert decided.status_code == 200
    (decision,) = decided.json()["decisions"]
    assert decision["pre_image"] is None  # nothing existed to overwrite
    assert decision["applied"]["name"] == "Marisol"
    assert decision["applied_summary"] == "Added to the cast 'Marisol'"
    assert after["pending"] == []
    assert after["committed"][0]["decision_id"] == decision["decision_id"]


# -- accept: the one write ---------------------------------------------------


def test_accept_writes_the_asset_and_returns_the_decision(build_app):
    """Doctrine 3: the accept route is where a proposal becomes a real asset."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        filed = _propose(
            log,
            kind="actor",
            action="add",
            patch={"name": "Marcus", "description": "Keeps the ledger."},
        )

    app = build()

    async def scenario():
        async with client_for(app) as client:
            decided = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [str(filed.id)], "decision": "accept"},
            )
            queue = await client.get(f"/api/proposals?session={SID}")
            return decided, queue.json()

    decided, queue = run(scenario())

    assert decided.status_code == 200
    body = decided.json()
    assert body["rejected"] == []
    (decision,) = body["decisions"]
    assert set(decision) == DECISION_KEYS
    assert decision["proposal_id"] == str(filed.id)
    assert decision["decision"] == "accept"
    # An accepted ``add`` created something and overwrote nothing.
    assert decision["pre_image"] is None
    assert decision["applied"]["name"] == "Marcus"
    # The committed list's readable line names the class and what it did.
    assert decision["applied_summary"] == "Created actor 'Marcus'"

    # The asset really exists in the catalog, and the pile now reads committed.
    assert _persona_names(app.state.log) == {"Marcus"}
    assert queue["pending"] == []
    (committed,) = queue["committed"]
    assert committed["decision_id"] == decision["decision_id"]
    assert committed["applied"]["persona_id"] == decision["applied"]["persona_id"]


def test_a_mass_accept_leaves_one_undoable_decision_per_item(build_app):
    """The UI batches, the API rows do not: a batch stays granularly undoable."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        filed = [
            _propose(log, kind="actor", action="add", patch={"name": name})
            for name in ("Marcus", "Ilse", "Reed")
        ]

    app = build()

    async def scenario():
        async with client_for(app) as client:
            accepted = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [str(item.id) for item in filed], "decision": "accept"},
            )
            batch = accepted.json()
            undone = await client.post(
                f"/api/proposals/{SID}/undo",
                json={"decision_id": batch["decisions"][1]["decision_id"]},
            )
            queue = await client.get(f"/api/proposals?session={SID}")
            return accepted, batch, undone, queue.json()

    accepted, batch, undone, queue = run(scenario())

    assert accepted.status_code == 200
    assert [row["proposal_id"] for row in batch["decisions"]] == [
        str(item.id) for item in filed
    ]
    assert undone.status_code == 200
    # The middle item's own row undid exactly that item.
    assert _persona_names(app.state.log) == {"Marcus", "Reed"}
    assert {row["proposal_id"] for row in queue["committed"]} == {
        str(filed[0].id),
        str(filed[2].id),
    }


# -- reject / dismiss: no write ----------------------------------------------


def test_reject_and_dismiss_leave_the_live_set_without_writing_an_asset(build_app):
    """A declined proposal is terminal: gone from the queue, nothing applied."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        rejected = _propose(log, kind="actor", action="add", patch={"name": "Marcus"})
        dismissed = _propose(log, kind="actor", action="add", patch={"name": "Ilse"})

    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [str(rejected.id)], "decision": "reject"},
            )
            second = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [str(dismissed.id)], "decision": "dismiss"},
            )
            queue = await client.get(f"/api/proposals?session={SID}")
            return first, second, queue.json()

    first, second, queue = run(scenario())

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["decisions"][0]["applied"] is None
    assert second.json()["decisions"][0]["applied"] is None
    # A declined row applied nothing, so its line reads as the refusal.
    assert first.json()["decisions"][0]["applied_summary"] == "Rejected actor"
    assert second.json()["decisions"][0]["applied_summary"] == "Dismissed actor"
    assert queue["pending"] == []
    # Nothing was applied, so nothing is committed and nothing is undoable.
    assert queue["committed"] == []
    assert _persona_names(app.state.log) == set()


# -- the mixed set (the pinned partial-success rule) -------------------------


def test_a_mixed_decide_set_applies_the_rest_and_reports_the_bad_id(build_app):
    """One stale id must not sink a batch: ``decisions`` + ``rejected`` together."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        first = _propose(log, kind="actor", action="add", patch={"name": "Marcus"})
        second = _propose(log, kind="actor", action="add", patch={"name": "Ilse"})
        untouched = _propose(log, kind="actor", action="add", patch={"name": "Reed"})

    app = build()

    async def scenario():
        async with client_for(app) as client:
            response = await client.post(
                f"/api/proposals/{SID}/decide",
                json={
                    "ids": [str(first.id), "999999", str(second.id)],
                    "decision": "accept",
                },
            )
            queue = await client.get(f"/api/proposals?session={SID}")
            return response, queue.json()

    response, queue = run(scenario())

    assert response.status_code == 200
    body = response.json()
    assert [row["proposal_id"] for row in body["decisions"]] == [
        str(first.id),
        str(second.id),
    ]
    (bad,) = body["rejected"]
    assert bad["id"] == "999999"
    assert "unknown proposal" in bad["error"]
    # The good ids really applied; the unmentioned one is still on offer.
    assert _persona_names(app.state.log) == {"Marcus", "Ilse"}
    assert [item["id"] for item in queue["pending"]] == [str(untouched.id)]
    assert len(queue["committed"]) == 2


def test_a_decide_set_where_nothing_applies_is_a_400_with_the_per_id_list(build_app):
    """No id could be decided => the whole request failed, not a silent no-op."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")

    app = build()

    async def scenario():
        async with client_for(app) as client:
            single = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": ["999999"], "decision": "accept"},
            )
            pair = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": ["999999", "888888"], "decision": "reject"},
            )
            return single, pair

    single, pair = run(scenario())

    assert single.status_code == 400
    body = single.json()
    assert "unknown proposal" in body["detail"]
    assert [row["id"] for row in body["rejected"]] == ["999999"]
    assert pair.status_code == 400
    assert pair.json()["detail"] == "none of the 2 proposals could be decided"
    assert [row["id"] for row in pair.json()["rejected"]] == ["999999", "888888"]


def test_a_decide_set_deduplicates_repeated_ids(build_app):
    """One click, one decision row: a repeated id is not "already accepted"."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        filed = _propose(log, kind="actor", action="add", patch={"name": "Marcus"})

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [str(filed.id), str(filed.id)], "decision": "accept"},
            )

    response = run(scenario())

    assert response.status_code == 200
    assert len(response.json()["decisions"]) == 1
    assert response.json()["rejected"] == []


# -- undo --------------------------------------------------------------------


def test_undo_restores_the_pre_image_and_clears_the_committed_item(build_app):
    """Finding 13: the undo re-appends what the accept replaced — no rewrite."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        world_id = append_world(
            log,
            {
                "world_id": "w1",
                "name": "Rain-World",
                "scenario": "It rains on the square.",
                "cast": ["p1"],
            },
        )
        before = world_by_id(log, world_id)
        filed = _propose(
            log,
            kind="world",
            action="amend",
            target=world_id,
            patch={"scenario": "The square floods."},
        )

    app = build()

    async def scenario():
        async with client_for(app) as client:
            accepted = (
                await client.post(
                    f"/api/proposals/{SID}/decide",
                    json={"ids": [str(filed.id)], "decision": "accept"},
                )
            ).json()
            mid = (await client.get(f"/api/proposals?session={SID}")).json()
            undone = await client.post(
                f"/api/proposals/{SID}/undo",
                json={"decision_id": accepted["decisions"][0]["decision_id"]},
            )
            queue = await client.get(f"/api/proposals?session={SID}")
            return accepted, mid, undone, queue.json()

    accepted, mid, undone, queue = run(scenario())

    assert mid["committed"][0]["pre_image"]["scenario"] == "It rains on the square."
    assert mid["committed"][0]["applied"]["scenario"] == "The square floods."
    assert mid["committed"][0]["applied_summary"] == "Amended world 'Rain-World'"
    assert undone.status_code == 200
    # The compensating decision reported is the ACCEPT row it took back.
    assert undone.json()["decision"]["decision_id"] == (
        accepted["decisions"][0]["decision_id"]
    )
    # The world folded back, field for field, through a fresh append.
    assert world_by_id(app.state.log, world_id) == before
    assert queue["committed"] == []
    # An undo compensates the write; it does not re-open the proposal.
    assert queue["pending"] == []


def test_an_accepted_remove_reads_as_removed_in_the_committed_list(build_app):
    """An accepted remove writes a tombstone and says so, with the pre-image kept."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        world_id = append_world(log, {"world_id": "w1", "name": "Rain-World"})
        filed = _propose(log, kind="world", action="remove", target=world_id)

    app = build()

    async def scenario():
        async with client_for(app) as client:
            accepted = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [str(filed.id)], "decision": "accept"},
            )
            return accepted.json(), (
                await client.get(f"/api/proposals?session={SID}")
            ).json()

    body, queue = run(scenario())

    (decision,) = body["decisions"]
    assert decision["applied"] is None
    assert decision["applied_summary"] == "Removed world 'Rain-World'"
    # The undo handle keeps the definition the tombstone removed.
    assert queue["committed"][0]["pre_image"]["name"] == "Rain-World"
    assert world_by_id(app.state.log, world_id) is None


def test_undoing_twice_is_a_400_detail(build_app):
    """One compensating append per decision — the second undo is refused."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        filed = _propose(log, kind="actor", action="add", patch={"name": "Marcus"})

    app = build()

    async def scenario():
        async with client_for(app) as client:
            accepted = (
                await client.post(
                    f"/api/proposals/{SID}/decide",
                    json={"ids": [str(filed.id)], "decision": "accept"},
                )
            ).json()
            decision_id = accepted["decisions"][0]["decision_id"]
            first = await client.post(
                f"/api/proposals/{SID}/undo", json={"decision_id": decision_id}
            )
            second = await client.post(
                f"/api/proposals/{SID}/undo", json={"decision_id": decision_id}
            )
            return first, second

    first, second = run(scenario())

    assert first.status_code == 200
    assert second.status_code == 400
    assert "already undone" in second.json()["detail"]


# -- inbox: the cross-session queue ------------------------------------------


def test_inbox_groups_pending_by_session_with_titles(build_app):
    """One fold, grouped by session; a settled session disappears entirely."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        _session(log, OTHER, "The Ledger Job")
        # A third chat with no title anchor falls back to its id.
        _session(log, "s3", "")
        marcus = _propose(log, kind="actor", action="add", patch={"name": "Marcus"})
        fen = _propose(log, kind="world", action="add", patch={"name": "Fen"})
        ledger = _propose(
            log, kind="book", action="add", patch={"name": "Ledger"}, session_id=OTHER
        )
        declined = _propose(
            log, kind="actor", action="add", patch={"name": "Ilse"}, session_id=OTHER
        )
        settled = _propose(
            log, kind="world", action="add", patch={"name": "Mire"}, session_id="s3"
        )
        store.reject_proposal(log, OTHER, declined.id)
        store.reject_proposal(log, "s3", settled.id)

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/proposals/inbox")).json()

    body = run(scenario())

    assert set(body) == {"by_session"}
    assert set(body["by_session"]) == {SID, OTHER}  # s3 settled -> no group
    assert body["by_session"][SID]["title"] == "Rain over the square"
    assert body["by_session"][OTHER]["title"] == "The Ledger Job"
    assert [item["id"] for item in body["by_session"][SID]["pending"]] == [
        str(fen.id),
        str(marcus.id),
    ]
    assert [item["id"] for item in body["by_session"][OTHER]["pending"]] == [
        str(ledger.id)
    ]
    # Every item carries the pinned proposal shape, from either scope, and no
    # declined proposal rides along.
    for group in body["by_session"].values():
        assert set(group) == {"title", "pending", "archived"}
        for item in group["pending"]:
            assert set(item) == PROPOSAL_KEYS
            assert item["id"] not in {str(declined.id), str(settled.id)}


def test_inbox_falls_back_to_the_session_id_for_an_untitled_chat(build_app):
    """Plan §4: a chat with no title still needs a heading in the queue."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, "s3", "")
        _propose(
            log, kind="actor", action="add", patch={"name": "Marcus"}, session_id="s3"
        )

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/proposals/inbox")).json()

    body = run(scenario())

    assert body["by_session"]["s3"]["title"] == "s3"


def test_the_inbox_is_empty_when_no_session_holds_a_pending_proposal(build_app):
    """An empty queue is an empty mapping, not an error and not a stub session."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/proposals/inbox")).json()

    assert run(scenario()) == {"by_session": {}}


# -- error shapes ------------------------------------------------------------


def test_reads_and_decisions_on_an_unknown_session_are_404(build_app):
    """A proposal route on an id the log never saw is a 404, never an empty queue."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")

    app = build()

    async def scenario():
        async with client_for(app) as client:
            read = await client.get("/api/proposals?session=nope")
            decided = await client.post(
                "/api/proposals/nope/decide",
                json={"ids": ["1"], "decision": "accept"},
            )
            undone = await client.post(
                "/api/proposals/nope/undo", json={"decision_id": "1"}
            )
            return read, decided, undone

    read, decided, undone = run(scenario())

    for response in (read, decided, undone):
        assert response.status_code == 404
        assert response.json()["detail"] == "unknown session"


def test_malformed_decide_and_undo_bodies_are_400_details(build_app):
    """Structural problems are 400 ``{"detail"}`` — the pinned error shape."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")

    app = build()

    async def scenario():
        async with client_for(app) as client:
            unknown_word = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": ["1"], "decision": "approve"},
            )
            not_a_list = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": "1", "decision": "accept"},
            )
            empty = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [], "decision": "accept"},
            )
            bad_id_type = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [{"id": "1"}], "decision": "accept"},
            )
            no_handle = await client.post(f"/api/proposals/{SID}/undo", json={})
            stray_handle = await client.post(
                f"/api/proposals/{SID}/undo", json={"decision_id": "999999"}
            )
            return (
                unknown_word,
                not_a_list,
                empty,
                bad_id_type,
                no_handle,
                stray_handle,
            )

    responses = run(scenario())

    assert [response.status_code for response in responses] == [400] * 6
    details = [response.json()["detail"] for response in responses]
    assert details[0] == "'decision' must be one of accept|reject|dismiss"
    assert details[1] == "'ids' must be a list"
    assert details[2] == "'ids' must name at least one proposal"
    assert details[3] == "'ids' must hold proposal id strings"
    assert details[4] == "'decision_id' must be a non-empty string"
    assert "unknown decision" in details[5]


# -- the diff-first wire (W9.23 §4) ------------------------------------------


def _op_propose(
    log: EventLog,
    *,
    kind: str,
    target: str,
    ops: list[dict],
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
            "action": "amend",
            "target": target,
            "patch": {},
            "ops": [dict(op) for op in ops],
            "origin": "bts",
            "signal": 0.5,
        },
    )


def _turns(log: EventLog, count: int, session_id: str = SID) -> None:
    for index in range(count):
        log.append_event(
            session_id, "narrative", "narrator", {"prose": f"turn {index}", "segments": []}
        )


def test_the_wire_serves_ops_staleness_and_the_archive_partition(build_app):
    """§4: ops / archived / age_turns / stale_paths ride every proposal row."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        world_id = append_world(
            log, {"world_id": "w1", "name": "Miller's Bend", "description": "A town."}
        )
        aged = _op_propose(
            log,
            kind="world",
            target=world_id,
            ops=[
                {
                    "op": "modify",
                    "path": "description",
                    "old": "A town.",
                    "new": "A mill town.",
                }
            ],
        )
        _turns(log, 10)
        fresh = _op_propose(
            log,
            kind="world",
            target=world_id,
            ops=[{"op": "add", "path": "provenance.origin", "new": "play"}],
        )

        app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get(f"/api/proposals?session={SID}")).json()

    body = run(scenario())

    (live,) = body["pending"]
    (old,) = body["archived"]
    assert live["id"] == f"{fresh.id}" and live["archived"] is False
    assert live["age_turns"] == 0
    assert old["id"] == f"{aged.id}" and old["archived"] is True
    assert old["age_turns"] == 10
    # Nothing was applied, so the filed op still matches the fold: NOT stale.
    assert old["stale_paths"] == []
    assert old["ops"] == [
        {
            "op": "modify",
            "path": "description",
            "old": "A town.",
            "new": "A mill town.",
        }
    ]
    assert live["ops"] == [{"op": "add", "path": "provenance.origin", "new": "play"}]


def test_the_wire_hydrates_a_legacy_amend_into_whole_field_modify_ops(build_app):
    """§4 (B2 seam): a pre-op row is ALWAYS served with its ops, from the fold."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        world_id = append_world(
            log, {"world_id": "w1", "name": "Miller's Bend", "description": "A town."}
        )
        _propose(
            log,
            kind="world",
            action="amend",
            target=world_id,
            patch={"description": "A mill town."},
        )

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get(f"/api/proposals?session={SID}")).json()

    (pending,) = run(scenario())["pending"]

    assert pending["ops"] == [
        {
            "op": "modify",
            "path": "description",
            "old": "A town.",
            "new": "A mill town.",
            "legacy": True,
        }
    ]
    # Nothing was anchored at file time, so nothing can be stale.
    assert pending["stale_paths"] == []


def test_a_stale_op_reaches_the_client_as_a_per_id_refusal(build_app):
    """§8.2: a stale accept lands in ``rejected`` — never a 500, never a write."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        world_id = append_world(
            log, {"world_id": "w1", "name": "Miller's Bend", "description": "A town."}
        )
        filed = _op_propose(
            log,
            kind="world",
            target=world_id,
            ops=[
                {
                    "op": "modify",
                    "path": "description",
                    "old": "A town nobody wrote.",
                    "new": "A mill town.",
                }
            ],
        )
        other = _propose(log, kind="actor", action="add", patch={"name": "Bram"})

    app = build()

    async def scenario():
        async with client_for(app) as client:
            refused = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [f"{filed.id}"], "decision": "accept"},
            )
            mixed = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [f"{other.id}", f"{filed.id}"], "decision": "accept"},
            )
            return refused, mixed, (await client.get(f"/api/proposals?session={SID}")).json()

    refused, mixed, after = run(scenario())

    assert refused.status_code == 400
    assert refused.json()["rejected"][0]["error"].startswith(
        f"proposal {filed.id} is stale: 'description' changed"
    )
    # The mixed set applies the good id and reports the stale one.
    assert mixed.status_code == 200
    assert [row["proposal_id"] for row in mixed.json()["decisions"]] == [
        f"{other.id}"
    ]
    assert mixed.json()["rejected"][0]["id"] == f"{filed.id}"
    # Nothing was written for the stale row and it is still offered.
    assert world_by_id(app.state.log, world_id).description == "A town."
    assert [row["id"] for row in after["pending"]] == [f"{filed.id}"]


def test_the_wire_serves_the_stale_badge_before_any_click(build_app):
    """§4: ``stale_paths`` is computed at READ, byte-identical to ``ops[].path``."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        world_id = append_world(
            log, {"world_id": "w1", "name": "Miller's Bend", "description": "A town."}
        )
        _op_propose(
            log,
            kind="world",
            target=world_id,
            ops=[
                {
                    "op": "modify",
                    "path": "description",
                    "old": "A town.",
                    "new": "A mill town.",
                }
            ],
        )
        # The owner edits the card in the studio between filing and reviewing.
        append_world(
            log, {"world_id": world_id, "name": "Miller's Bend", "description": "A city."}
        )

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get(f"/api/proposals?session={SID}")).json()

    (pending,) = run(scenario())["pending"]

    assert pending["stale_paths"] == ["description"]
    assert [op["path"] for op in pending["ops"]] == pending["stale_paths"]


def test_a_parented_books_row_rides_the_wire_as_a_create(build_app):
    """§6: the row carries the marker, the name as its label, and no stale badge."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        filed = _parented(log, name="The Mill Ledger")

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get(f"/api/proposals?session={SID}")).json()

    body = run(scenario())

    (pending,) = body["pending"]
    assert set(pending) == PROPOSAL_KEYS
    assert pending["kind"] == "book" and pending["action"] == "amend"
    assert pending["target_new"] is True
    # The target IS the name (no id exists yet), so the surface can label it
    # without a catalog read and the ops render as ordinary entry adds.
    assert pending["target"] == "The Mill Ledger"
    assert pending["target_name"] == "The Mill Ledger"
    assert len(pending["ops"]) == 2
    assert pending["patch"] == {}
    # A born-parented row can never be stale, so the surface shows the hint.
    assert pending["stale_paths"] == []


def test_a_parented_accept_reads_as_created_book_with_its_entry_count(build_app):
    """§6: "Created book '<name>' (N entries)" — the amend verb would be a lie."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        two = _parented(log, name="The Mill Ledger")
        one = _parented(log, name="Road Lore", entries=((("the road",), "Mud."),))

    app = build()

    async def scenario():
        async with client_for(app) as client:
            decided = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [str(two.id), str(one.id)], "decision": "accept"},
            )
            queue = await client.get(f"/api/proposals?session={SID}")
            world = await client.get(f"/api/sessions/{SID}/world")
            return decided, queue.json(), world.json()

    decided, queue, world = run(scenario())

    assert decided.status_code == 200
    assert decided.json()["rejected"] == []
    summaries = [row["applied_summary"] for row in decided.json()["decisions"]]
    assert summaries == [
        "Created book 'The Mill Ledger' (2 entries)",
        "Created book 'Road Lore' (1 entry)",
    ]
    for decision in decided.json()["decisions"]:
        assert set(decision) == DECISION_KEYS
        assert decision["target_new"] is True
        # Nothing existed before, so there is no pre-image to restore.
        assert decision["pre_image"] is None
        assert decision["applied"]["entries"]
    # The committed list serves the same readable line, and the book the accept
    # created is what this chat's world read now lists (the attach, §4.1).
    assert sorted(row["applied_summary"] for row in queue["committed"]) == sorted(
        summaries
    )
    assert sorted(book["name"] for book in world["books"]) == [
        "Road Lore",
        "The Mill Ledger",
    ]
    # Every created entry has the id the filed op named (the studio's own mint).
    for decision in decided.json()["decisions"]:
        for entry in decision["applied"]["entries"]:
            assert entry["entry_id"]


def test_a_decision_names_its_kind_and_action(build_app):
    """§4: the committed list can say what an applied item did to what."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        filed = _propose(log, kind="actor", action="add", patch={"name": "Bram"})

    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [f"{filed.id}"], "decision": "accept"},
            )
            return (await client.get(f"/api/proposals?session={SID}")).json()

    (decision,) = run(scenario())["committed"]

    assert set(decision) == DECISION_KEYS
    assert decision["kind"] == "actor"
    assert decision["action"] == "add"


def test_an_archived_id_inside_a_multi_id_decide_is_refused_for_that_id(build_app):
    """§3.3: the archive drawer is never mass-selected — a single id stays legal."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        persona_ids = [
            append_persona(log, {"persona_id": f"p{index}", "name": f"Ilse {index}"})
            for index in range(3)
        ]
        aged = _propose(
            log,
            kind="actor",
            action="amend",
            target=persona_ids[0],
            patch={"description": "Aged."},
        )
        _turns(log, 10)
        live = _propose(
            log,
            kind="actor",
            action="amend",
            target=persona_ids[1],
            patch={"description": "Live."},
        )

    app = build()

    async def scenario():
        async with client_for(app) as client:
            mass = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [f"{live.id}", f"{aged.id}"], "decision": "accept"},
            )
            single = await client.post(
                f"/api/proposals/{SID}/decide",
                json={"ids": [f"{aged.id}"], "decision": "accept"},
            )
            return mass, single

    mass, single = run(scenario())

    assert mass.status_code == 200
    assert [row["proposal_id"] for row in mass.json()["decisions"]] == [
        f"{live.id}"
    ]
    (refusal,) = mass.json()["rejected"]
    assert refusal["id"] == f"{aged.id}"
    assert "archived" in refusal["error"]
    # The single-id click on the drawer row is legal and applies.
    assert single.status_code == 200
    assert single.json()["decisions"][0]["proposal_id"] == f"{aged.id}"


def test_the_inbox_serves_both_partitions_per_session(build_app):
    """§3.2/§4: the inbox carries the archive drawer too, aged on its own chain."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _session(log, SID, "Rain over the square")
        _session(log, OTHER, "A second chat")
        world_id = append_world(
            log, {"world_id": "w1", "name": "Miller's Bend", "description": "A town."}
        )
        aged = _propose(
            log,
            kind="world",
            action="amend",
            target=world_id,
            patch={"description": "A mill town."},
        )
        _turns(log, 10)
        live = _propose(
            log,
            kind="actor",
            action="add",
            patch={"name": "Bram"},
            session_id=OTHER,
        )

    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/proposals/inbox")).json()

    body = run(scenario())

    assert [row["id"] for row in body["by_session"][SID]["archived"]] == [
        f"{aged.id}"
    ]
    assert body["by_session"][SID]["pending"] == []
    assert [row["id"] for row in body["by_session"][OTHER]["pending"]] == [
        f"{live.id}"
    ]
    assert body["by_session"][OTHER]["archived"] == []
