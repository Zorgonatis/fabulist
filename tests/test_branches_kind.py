"""Branch ``kind`` + superseded-era surfaces (W9.26 §2.6, lane F).

Covers the two era distinctions the chat UI reads off the branches/sessions
surfaces:

* a branch child is ``kind:"user"`` (a swipe alternate) or
  ``kind:"compaction"`` (a CONTINUATION era), derived from marker PRESENCE among
  the child's OWN rows -- including the carried-config case, where the fork
  re-appends the parent's config rows after the cut so the marker is never the
  first row;
* a compaction child is labeled ("compacted continuation"), never
  snippet-blank, and it is KEPT in the ``branches`` list while being EXCLUDED
  from the ``siblings`` swipe group (a swipe must never cross eras);
* ``GET /api/sessions`` marks the superseded PARENT as ``compacted`` (its history
  continues in the era child); the live continuation stays a normal row.

The marker payload is the real one the compact service appends (schema-gated), so
these tests fail if the producer's shape drifts.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.branches import COMPACTION_LABEL, router as branches_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps (branches router mounted) sharing one tmp log."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(branches_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create(client: httpx.AsyncClient, title: str = "") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def _seed_reply(log: EventLog, sid: str, prose: str) -> None:
    log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": "", "segments": [{"kind": "prose", "text": prose}]},
    )


def _mark_era(log: EventLog, parent: str, child: str, *, at: int, covers=(1, 2)) -> None:
    """Append the era marker the compact service writes to the SUCCESSOR."""
    log.append_event(
        child,
        "system",
        "compaction",
        {
            "event": "compaction",
            "source_session": parent,
            "summary_ref": [parent, at],
            "covers_seqs": list(covers),
            "trigger": "manual",
            "stats": {"calls": 1, "prompt_tokens": 10, "model": "m"},
        },
    )


def test_branch_kind_user_vs_compaction_and_label(build_app):
    """A user fork keeps its snippet; a continuation is labeled, not blank."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            log.append_event(base, "user", "user", {"text": "enter"})
            _seed_reply(log, base, "Welcome to the market.")

            alt = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": 1, "new_session_id": "alt-1"},
                )
            ).json()["session_id"]
            _seed_reply(log, alt, "The lantern flickers.")

            era = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": 1, "new_session_id": "era-1"},
                )
            ).json()["session_id"]
            _mark_era(log, base, era, at=1)

            listed = await client.get(f"/api/sessions/{base}/branches")
            return listed

    body = run(scenario()).json()
    by_id = {child["session_id"]: child for child in body["branches"]}

    assert by_id["alt-1"]["kind"] == "user"
    assert by_id["alt-1"]["label"] == ""
    assert by_id["alt-1"]["snippet"] == "The lantern flickers."

    assert by_id["era-1"]["kind"] == "compaction"
    assert by_id["era-1"]["label"] == COMPACTION_LABEL == "compacted continuation"
    # Not snippet-blank: the label stands in for the reply the era has not taken.
    assert by_id["era-1"]["snippet"] == COMPACTION_LABEL


def test_compaction_kind_is_own_rows_not_chain_nor_first_row(build_app):
    """The carried-config first row must not hide the marker; a fork of an era is a user fork."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            log.append_event(base, "user", "user", {"text": "enter"})
            _seed_reply(log, base, "Lanterns sway.")
            # Session-constant config written AFTER the cut: the fork re-appends
            # it, so the child's FIRST own row is carried config and the marker
            # lands second.
            log.append_event(
                base, "system", "user", {"event": "chat_memory", "enabled": False}
            )

            era = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": 1, "new_session_id": "era-1"},
                )
            ).json()["session_id"]
            _mark_era(log, base, era, at=1)
            first_event = log.get_events(era)[0].payload.get("event")

            # A fork OF the era: the marker is inherited on the chain, never owned.
            await client.post(
                f"/api/sessions/{era}/fork",
                json={"at_seq": 1, "new_session_id": "swipe-of-era"},
            )

            listed = await client.get(f"/api/sessions/{base}/branches")
            return first_event, listed

    first_event, listed = run(scenario())
    # The trap is real: the carried config row precedes the marker row, so a
    # "first row is a marker" test would call this era a user swipe.
    assert first_event == "chat_memory"

    body = listed.json()
    by_id = {child["session_id"]: child for child in body["branches"]}
    assert by_id["era-1"]["kind"] == "compaction"
    # The fork of the era is a child of the ERA, not of the base.
    assert "swipe-of-era" not in by_id


def test_fork_of_an_era_is_kind_user(build_app):
    """Chain inheritance must not label a swipe branch of an era as a continuation."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            log.append_event(base, "user", "user", {"text": "enter"})
            _seed_reply(log, base, "Lanterns sway.")
            era = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": 1, "new_session_id": "era-1"},
                )
            ).json()["session_id"]
            _mark_era(log, base, era, at=1)
            _seed_reply(log, era, "The new era begins.")
            swipe = (
                await client.post(
                    f"/api/sessions/{era}/fork",
                    json={"at_seq": 1, "new_session_id": "swipe-of-era"},
                )
            ).json()["session_id"]
            _seed_reply(log, swipe, "The new era begins differently.")
            listed = await client.get(f"/api/sessions/{era}/branches")
            return listed

    body = run(scenario()).json()
    rows = {child["session_id"]: child for child in body["branches"]}
    assert rows["swipe-of-era"]["kind"] == "user"
    assert rows["swipe-of-era"]["label"] == ""
    assert rows["swipe-of-era"]["snippet"] == "The new era begins differently."


def test_era_with_its_own_prose_keeps_prose_and_label(build_app):
    """Once the era takes a turn its real snippet wins; the label still marks it."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            log.append_event(base, "user", "user", {"text": "enter"})
            _seed_reply(log, base, "Lanterns sway.")
            era = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": 1, "new_session_id": "era-1"},
                )
            ).json()["session_id"]
            _mark_era(log, base, era, at=1)
            _seed_reply(log, era, "Morning comes to the Rusty Anchor.")
            return await client.get(f"/api/sessions/{base}/branches")

    body = run(scenario()).json()
    row = body["branches"][0]
    assert row["kind"] == "compaction"
    assert row["label"] == COMPACTION_LABEL
    # Real prose wins over the label once the era has a reply of its own.
    assert row["snippet"] == "Morning comes to the Rusty Anchor."


def test_swipe_group_excludes_continuation_but_list_keeps_it(build_app):
    """A compaction child never joins a swipe group -- shown, never swiped."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Base")
            log = app.state.log
            log.append_event(base, "user", "user", {"text": "enter"})
            _seed_reply(log, base, "Lanterns sway.")
            alt = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": 1, "new_session_id": "alt-1"},
                )
            ).json()["session_id"]
            _seed_reply(log, alt, "A different reply.")
            era = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": 1, "new_session_id": "era-1"},
                )
            ).json()["session_id"]
            _mark_era(log, base, era, at=1)

            from_alt = await client.get(f"/api/sessions/{alt}/branches")
            from_era = await client.get(f"/api/sessions/{era}/branches")
            from_base = await client.get(f"/api/sessions/{base}/branches")
            return base, alt, era, from_alt, from_era, from_base

    base, alt, era, from_alt, from_era, from_base = run(scenario())

    # The user fork's swipe group holds only real alternatives (self + base line).
    alt_ids = [entry["session_id"] for entry in from_alt.json()["siblings"]]
    assert sorted(alt_ids) == sorted([base, alt])

    # The continuation is its own one-line group: no arrows across eras.
    era_body = from_era.json()
    assert [entry["session_id"] for entry in era_body["siblings"]] == [era]
    assert era_body["siblings"][0]["is_self"] is True

    # The children list keeps BOTH, kinds intact (honest read).
    base_body = from_base.json()
    kinds = {child["session_id"]: child["kind"] for child in base_body["branches"]}
    assert kinds == {alt: "user", era: "compaction"}


def test_sessions_list_marks_only_the_superseded_parent(build_app):
    """Drawer badge source: the compacted parent is flagged, the era child is not."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            base = await _create(client, title="Rusty Anchor")
            plain = await _create(client, title="Untouched")
            log = app.state.log
            log.append_event(base, "user", "user", {"text": "enter"})
            _seed_reply(log, base, "Lanterns sway.")
            era = (
                await client.post(
                    f"/api/sessions/{base}/fork",
                    json={"at_seq": 1, "new_session_id": "era-1"},
                )
            ).json()["session_id"]
            _mark_era(log, base, era, at=1, covers=(1, 1))

            first = await client.get("/api/sessions")

            # Compact the era in turn: the era becomes the superseded parent.
            era2 = (
                await client.post(
                    f"/api/sessions/{era}/fork",
                    json={"at_seq": 2, "new_session_id": "era-2"},
                )
            ).json()["session_id"]
            _mark_era(log, era, era2, at=2, covers=(1, 1))
            second = await client.get("/api/sessions")
            return base, plain, first, second

    base, plain, first, second = run(scenario())

    rows = {row["session_id"]: row for row in first.json()["sessions"]}
    assert rows[base]["compacted"] is True
    assert rows["era-1"]["compacted"] is False
    assert rows[plain]["compacted"] is False

    rows = {row["session_id"]: row for row in second.json()["sessions"]}
    assert rows[base]["compacted"] is True
    assert rows["era-1"]["compacted"] is True  # superseded in turn
    assert rows["era-2"]["compacted"] is False  # the live continuation
