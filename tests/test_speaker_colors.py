"""Tests for the cast / speaker-colour registry (D25, B5 backend half).

Covers the deterministic first-free-slot palette, latest-wins reads, the
role-reserved near-white for the user character, fork replay fidelity, the
``system``/``speaker_color`` schema variant, and (W7.9) the roster-resolved row
shape shared by ``GET`` and ``PATCH .../speaker_colors``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.cast_api import (
    SPEAKER_PALETTE,
    SPEAKER_USER_COLOR,
    assign_speaker_color,
    list_speaker_colors,
)
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.log.schema_gate import (
    SPEAKER_COLOR_EVENT,
    LogSchemaError,
    validate_event,
)
from fabulist.state.cast import ensure_roster


def test_first_speaker_gets_slot_one_second_slot_two(log):
    assert assign_speaker_color(log, "s1", "Aria") == SPEAKER_PALETTE[0]
    assert assign_speaker_color(log, "s1", "Borin") == SPEAKER_PALETTE[1]
    assert list_speaker_colors(log, "s1") == {
        "Aria": SPEAKER_PALETTE[0],
        "Borin": SPEAKER_PALETTE[1],
    }


def test_same_speaker_reasked_is_latest_wins_stable(log):
    first = assign_speaker_color(log, "s1", "Aria")
    again = assign_speaker_color(log, "s1", "Aria")
    assert first == again == SPEAKER_PALETTE[0]
    # Re-asking does not append a second event (exactly one assignment row).
    color_events = [
        event
        for event in log.get_events("s1")
        if event.payload.get("event") == SPEAKER_COLOR_EVENT
    ]
    assert len(color_events) == 1


def test_user_character_gets_reserved_near_white(log):
    color = assign_speaker_color(log, "s1", "You", is_user=True)
    assert color == SPEAKER_USER_COLOR
    assert color not in SPEAKER_PALETTE
    # The near-white does not consume a palette slot for the next speaker.
    assert assign_speaker_color(log, "s1", "Aria") == SPEAKER_PALETTE[0]


def test_palette_exhaustion_wraps_deterministically(log):
    names = [f"N{i}" for i in range(len(SPEAKER_PALETTE) + 1)]
    colors = [assign_speaker_color(log, "s1", name) for name in names]
    assert colors[: len(SPEAKER_PALETTE)] == list(SPEAKER_PALETTE)
    # The 11th speaker wraps to the first slot (deterministic).
    assert colors[-1] == SPEAKER_PALETTE[0]


def test_empty_name_rejected(log):
    with pytest.raises(ValueError):
        assign_speaker_color(log, "s1", "   ")


def test_fork_replays_identical_colors(log):
    assign_speaker_color(log, "s1", "Aria")
    assign_speaker_color(log, "s1", "Borin")
    assign_speaker_color(log, "s1", "You", is_user=True)
    base = list_speaker_colors(log, "s1")

    # Fork at the tip; the branch copies no rows, only a pointer.
    log.fork_session("s1", 3, "fork1")
    assert list_speaker_colors(log, "fork1") == base
    # A new speaker on the branch continues the palette, not a reset.
    assert assign_speaker_color(log, "fork1", "Cyra") == SPEAKER_PALETTE[2]


# -- schema variant ---------------------------------------------------------


def test_speaker_color_variant_is_valid_and_typed():
    payload = {
        "event": "speaker_color",
        "name": "Aria",
        "color": "#e06c75",
        "assigned_by": "engine",
    }
    assert validate_event("system", payload) == []
    assert validate_event("system", {**payload, "assigned_by": "user"}) == []


def test_speaker_color_variant_requires_core_keys():
    errors = validate_event("system", {"event": "speaker_color", "name": "Aria"})
    assert any("color" in error for error in errors)
    assert any("assigned_by" in error for error in errors)


def test_speaker_color_variant_rejects_bad_assigned_by():
    payload = {
        "event": "speaker_color",
        "name": "Aria",
        "color": "#e06c75",
        "assigned_by": "model",
    }
    errors = validate_event("system", payload)
    assert errors == ["key 'assigned_by' expects one of engine|user"]


def test_other_system_payloads_still_ungated_by_variant():
    # The opaque system schema must not be tightened by the variant.
    assert validate_event("system", {"event": "session_created", "title": "x"}) == []
    assert validate_event("system", ["not", "a", "dict"]) == []


def test_append_gate_rejects_malformed_speaker_color(log):
    with pytest.raises(LogSchemaError):
        log.append_event(
            "s1", "system", "engine", {"event": "speaker_color", "name": "Aria"}
        )


# -- W7.9: roster-resolved rows (read + write share one fold) ----------------


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """A real FastAPI app over a tmp SQLite log (httpx ASGI transport)."""
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


def _seed_card(log: EventLog, session_id: str, name: str) -> None:
    """Append the ``system``/card event shape ``to_session_seed`` writes."""
    log.append_event(
        session_id, "system", "system", {"card": {"name": name, "description": ""}}
    )


def test_legacy_name_rows_collapse_onto_one_member(build_app):
    """``clerk`` + ``the clerk`` are one member, so the read is ONE row (W7.9)."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_card(log, "s1", "Clerk")
        # Two legacy (pre-W7) name-keyed rows for the same person.
        assign_speaker_color(log, "s1", "clerk")  # palette[0]
        assign_speaker_color(log, "s1", "the clerk")  # palette[1]
        clerk_id = ensure_roster(log, "s1")[0].member_id
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/speaker_colors")).json()

    body = run(scenario())
    assert body["session_id"] == "s1"
    assert body["speaker_colors"] == [
        {
            "member_id": clerk_id,
            "name": "Clerk",  # the member's canonical display name
            "color": SPEAKER_PALETTE[1],  # latest-wins
            "assigned_by": "engine",
        }
    ]


def test_read_and_patch_return_identical_rows(build_app):
    """The PATCH reply is the GET reply: one fold, one shape (W7.9)."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_card(log, "s1", "Clerk")
        ensure_roster(log, "s1")
    app = build()

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/sessions/s1/speaker_colors",
                json={"name": "the clerk", "color": "#123456"},
            )
            listed = await client.get("/api/sessions/s1/speaker_colors")
            return patched, listed

    patched, listed = run(scenario())
    assert patched.status_code == 200
    assert patched.json() == listed.json()
    (row,) = patched.json()["speaker_colors"]
    assert set(row) == {"member_id", "name", "color", "assigned_by"}
    assert row["name"] == "Clerk"  # the alias resolves to the canonical name
    assert row["color"] == "#123456"
    assert row["assigned_by"] == "user"
    assert isinstance(row["member_id"], str) and row["member_id"]


def test_color_matching_no_member_is_kept_with_null_member(build_app):
    """An unresolvable name keeps its colour, its literal name, no id (W7.9)."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("s1", "user", "user", {"text": "seed"})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/sessions/s1/speaker_colors",
                json={"name": "Ghost", "color": "#0f0f0f"},
            )
            listed = await client.get("/api/sessions/s1/speaker_colors")
            return patched, listed

    patched, listed = run(scenario())
    assert patched.json() == listed.json()
    assert listed.json()["speaker_colors"] == [
        {
            "member_id": None,
            "name": "Ghost",
            "color": "#0f0f0f",
            "assigned_by": "user",
        }
    ]


def test_rows_follow_roster_order_then_unknown_names(build_app):
    """Roster members first (roster order), then the unmatched names (W7.9)."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_card(log, "s1", "Aria")
        _seed_card(log, "s1", "Borin")
        ensure_roster(log, "s1")
        assign_speaker_color(log, "s1", "Ghost")  # matches no member
        assign_speaker_color(log, "s1", "Borin")
        assign_speaker_color(log, "s1", "aria")  # alias of the Aria member
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/speaker_colors")).json()

    rows = run(scenario())["speaker_colors"]
    assert [row["name"] for row in rows] == ["Aria", "Borin", "Ghost"]
    assert [row["member_id"] is None for row in rows] == [False, False, True]
    assert [row["color"] for row in rows] == [
        SPEAKER_PALETTE[2],  # "aria" -> the Aria member
        SPEAKER_PALETTE[1],
        SPEAKER_PALETTE[0],
    ]
