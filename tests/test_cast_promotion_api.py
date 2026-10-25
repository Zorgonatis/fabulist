"""W9.30 §5.3 — the cast ladder's MANUAL promote door (cast API routes).

The engine files a promotion PROPOSAL on the historian cadence (pinned in
``tests/test_promotion.py`` and ``tests/test_historian.py``); these tests cover the
owner's direct door on the already-mounted cast router:

* ``GET  /api/sessions/{sid}/cast/{member_id}/promotion`` -> ``{eligible,
  suggested_name, reason}`` — what the promote dialog pre-fills;
* ``POST /api/sessions/{sid}/cast/{member_id}/promotion`` ``{name?}`` -> the
  created persona wire (``personas_api``'s projection), name omitted -> the
  collision suffix, name given -> VERBATIM (never refused, never merged), and an
  ineligible member -> 400 with a readable reason;
* ``GET /api/sessions/{sid}/cast`` rows carry ``promoted_persona_id`` (either
  door), so the pane can badge an already-promoted member.

The real FastAPI app over the real log (httpx ASGI transport), no model calls: the
manual door is a studio-class write and the engine door's output is seeded straight
into the log.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.personas.registry import append_persona
from fabulist.state import entities, promotion
from fabulist.state.cast import admit_member, member_by_name, roster
from fabulist.state.proposals import accept_proposal

SID = "s-promote"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build():
        app = create_app(FakeTurnEngine(), log_path)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# -- seeding (the log the app will read) ------------------------------------


def _seed_actor(log, name="Tessa", *, session_id=SID, facts=True):
    """One member on the promotion rung, optionally carrying the fact bar."""
    log.append_event(session_id, "user", "user", {"text": "Take the grain up."})
    admit_member(
        log, session_id, name, provenance="actor", admitted_by="owner"
    )
    member = member_by_name(roster(log, session_id), name)
    assert member is not None
    if facts:
        entities.establish_relationship(
            log, session_id, member.member_id, "player", name="employer"
        )
        entities.set_attribute(
            log, session_id, member.member_id, key="resolve", value=7
        )
        entities.establish_item(
            log, session_id, "brass key", holder=member.member_id
        )
    return member


def _seed_off_rung(log, name="Clerk", *, provenance="bts", session_id=SID):
    """A member NOT on the rung (learned in play — no owner-accepted proposal)."""
    log.append_event(session_id, "user", "user", {"text": "Ask the clerk."})
    admit_member(log, session_id, name, provenance=provenance, admitted_by="bts")
    member = member_by_name(roster(log, session_id), name)
    assert member is not None
    return member


def _cast_rows(body: dict) -> list[dict]:
    return [row for row in body["cast"] if row["member_id"]]


# -- GET: what the dialog pre-fills -----------------------------------------


def test_the_read_suggests_the_display_name_when_it_is_free(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        member = _seed_actor(log)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get(
                f"/api/sessions/{SID}/cast/{member.member_id}/promotion"
            )

    response = run(scenario())
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is True
    assert body["suggested_name"] == "Tessa"
    assert "owner-accepted cast proposal" in body["reason"]


def test_the_read_suggests_the_suffixed_name_on_a_collision(build_app):
    """req. 7: the dialog opens PRE-FILLED with a free name (Tessa1)."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        member = _seed_actor(log)
        append_persona(log, {"name": "Tessa"})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get(
                f"/api/sessions/{SID}/cast/{member.member_id}/promotion"
            )

    assert run(scenario()).json()["suggested_name"] == "Tessa1"


def test_the_read_explains_why_a_member_is_not_eligible(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        member = _seed_off_rung(log)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get(
                f"/api/sessions/{SID}/cast/{member.member_id}/promotion"
            )

    body = run(scenario()).json()
    assert body["eligible"] is False
    assert "cast proposal" in body["reason"]


def test_the_read_404s_an_unknown_session_or_member(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        member = _seed_actor(log)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            unknown_session = await client.get(
                f"/api/sessions/nope/cast/{member.member_id}/promotion"
            )
            unknown_member = await client.get(
                f"/api/sessions/{SID}/cast/cm_missing/promotion"
            )
            return unknown_session, unknown_member

    unknown_session, unknown_member = run(scenario())
    assert unknown_session.status_code == 404
    assert unknown_member.status_code == 404


# -- POST: the door itself --------------------------------------------------


def test_the_door_suffixes_an_omitted_name(build_app):
    """Name omitted -> the §5.4 suffix against the live catalog."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        member = _seed_actor(log)
        append_persona(log, {"name": "Tessa"})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                f"/api/sessions/{SID}/cast/{member.member_id}/promotion", json={}
            )
            return posted, (await client.get("/api/personas")).json()

    posted, personas = run(scenario())
    assert posted.status_code == 200
    body = posted.json()
    assert body["name"] == "Tessa1"
    assert body["session_id"] == SID
    assert body["member_id"] == member.member_id
    # The wire is personas_api's projection: the definition the studio reads.
    assert body["provenance"]["origin"] == "promotion"
    assert body["provenance"]["member_id"] == member.member_id
    assert body["personality"] == ""
    assert body["image_asset"] is None
    assert sorted(persona["name"] for persona in personas["personas"]) == [
        "Tessa",
        "Tessa1",
    ]


def test_the_door_writes_a_typed_name_verbatim_even_on_a_collision(build_app):
    """req. 7 / D5: never refused, never merged — duplicates are legal."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        member = _seed_actor(log)
        append_persona(log, {"name": "Tessa"})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                f"/api/sessions/{SID}/cast/{member.member_id}/promotion",
                json={"name": "Tessa"},
            )
            return posted, (await client.get("/api/personas")).json()

    posted, personas = run(scenario())
    assert posted.status_code == 200
    assert posted.json()["name"] == "Tessa"
    assert [p["name"] for p in personas["personas"]].count("Tessa") == 2


def test_the_door_refuses_an_ineligible_member_with_a_readable_reason(build_app):
    """Eligibility is a 400 (req. 7's never-refuse is about NAMES)."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        member = _seed_off_rung(log)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                f"/api/sessions/{SID}/cast/{member.member_id}/promotion", json={}
            )
            return posted, (await client.get("/api/personas")).json()

    posted, personas = run(scenario())
    assert posted.status_code == 400
    assert "cast proposal" in posted.json()["detail"]
    assert personas["personas"] == []  # nothing was written


def test_the_door_404s_an_unknown_member(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_actor(log)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                f"/api/sessions/{SID}/cast/cm_missing/promotion", json={}
            )

    assert run(scenario()).status_code == 404


# -- the cast wire's badge (§5.3) -------------------------------------------


def test_the_cast_wire_carries_the_promoted_persona_id_from_the_manual_door(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        member = _seed_actor(log)
    app = build()

    async def scenario():
        async with client_for(app) as client:
            before = (await client.get(f"/api/sessions/{SID}/cast")).json()
            posted = await client.post(
                f"/api/sessions/{SID}/cast/{member.member_id}/promotion", json={}
            )
            after = (await client.get(f"/api/sessions/{SID}/cast")).json()
            return before, posted, after

    before, posted, after = run(scenario())
    assert _cast_rows(before)[0]["promoted_persona_id"] is None
    assert _cast_rows(after)[0]["promoted_persona_id"] == posted.json()["persona_id"]
    # Every row carries the key, virtual rows included (never a missing field).
    assert all("promoted_persona_id" in row for row in after["cast"])


def test_the_cast_wire_carries_the_id_for_an_accepted_engine_promotion(build_app):
    """Either door shows the badge: the read is the persona fold, not the row."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        member = _seed_actor(log)
        assert promotion.promotion_pass(log, SID) == 1
        row = [
            event
            for event in log.get_events(SID)
            if event.type == "system"
            and event.payload.get("event") == "asset_proposal"
        ][-1]
        decision = accept_proposal(log, SID, str(row.id))
        assert decision.applied is not None
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get(f"/api/sessions/{SID}/cast")).json()

    rows = _cast_rows(run(scenario()))
    assert rows[0]["member_id"] == member.member_id
    assert rows[0]["promoted_persona_id"] == decision.applied["persona_id"]


def test_the_cast_wire_carries_the_definition_for_a_card_member(build_app):
    """A card-admitted member has no promotion row; the badge joins by name.

    The card import captures a persona (with the card art). The member the seed
    admitted carries no ``promoted_persona_id``, so the row must resolve the
    definition itself — otherwise the badge has no id to key the image route by
    and the character shows initials where its portrait belongs.
    """
    build, _ = build_app
    app = build()
    card_png = (
        Path(__file__).resolve().parent / "fixtures" / "cards" / "full_v2.png"
    ).read_bytes()

    async def scenario():
        async with client_for(app) as client:
            imported = await client.post(
                "/api/cards/import?seed=1",
                content=card_png,
                headers={"content-type": "image/png"},
            )
            sid = imported.json()["session_id"]
            personas = (await client.get("/api/personas")).json()["personas"]
            cast = (await client.get(f"/api/sessions/{sid}/cast")).json()
            return personas, cast

    personas, cast = run(scenario())
    assert len(personas) == 1
    rows = [row for row in cast["cast"] if row["name"] == personas[0]["name"]]
    assert rows
    assert rows[0]["promoted_persona_id"] is None
    assert rows[0]["persona_id"] == personas[0]["persona_id"]
    assert rows[0]["image_asset"] == personas[0]["image_asset"]
    assert rows[0]["image_asset"]
