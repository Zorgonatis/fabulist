"""W5C backend completions: pending-HB gaps closed.

Covers the three Wave-5 lane W5C backend gaps against the real FastAPI app
(httpx ASGI transport) over a synthetic tmp SQLite log:

* W5C-1 — per-turn RAG ``results`` rows surfaced by ``inspect/turns`` from the
  narrative snapshot's ``recall_rows`` (seq/text-distance projected, clipped).
* W5C-2 — ``PATCH /api/sessions/{sid}/speaker_colors`` overrides the engine
  assignment as a user edit (validation + 404 + refreshed registry).
* W5C-3 — ``GET /api/sessions/{sid}/cast`` merges card-derived roster names
  with the speaker-colour registry (W3: plus the BTS-discovered cast from the
  ``world_state`` events, auto-coloured at first sight).
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
    assign_member_color,
    assign_speaker_color,
    set_speaker_color,
)
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.state.cast import admit_member, ensure_roster, retire_member


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
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


def _seed_card(log: EventLog, session_id: str, name: str, **fields) -> None:
    """Append the ``system``/card event shape ``to_session_seed`` writes."""
    payload = {"card": {"name": name, "description": fields.get("description", "")}}
    payload.update(fields)
    log.append_event(session_id, "system", "system", payload)


def _seed_world_state(
    log: EventLog, session_id: str, state: dict, *, kind: str = "delta"
) -> None:
    """Append the ``system``/``world_state`` shape the BTS post-turn node writes."""
    log.append_event(
        session_id,
        "system",
        "bts",
        {"event": "world_state", "kind": kind, "state": state},
    )


# -- W5C-1: per-result memory rows ------------------------------------------


def _seed_turn_with_rows(log_path: Path, rows: list[dict]) -> None:
    with EventLog(log_path) as log:
        log.append_event("s1", "user", "user", {"text": "who was at the keep?"})
        log.append_event(
            "s1",
            "narrative",
            "narrator",
            {"prose": "The keep remembers."},
            meta={
                "config": {
                    "model": "alpha",
                    "recall_queries": ["who was at the keep?"],
                    "recall_config": {"k": 6, "rerank_enabled": False, "query_mode": "message"},
                    "recall_rows": rows,
                }
            },
        )


def test_rag_results_surface_seq_text_distance(build_app):
    build, log_path = build_app
    _seed_turn_with_rows(
        log_path,
        [
            {"seq": 6, "kind": "user", "text": "She stares.", "distance": 0.123},
            {"seq": 7, "kind": "narrative", "text": "It rained.", "distance": 0.5},
        ],
    )
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/inspect/turns")).json()

    rag = run(scenario())["turns"][0]["rag"]
    assert rag["results"] == [
        {"seq": 6, "text": "She stares.", "distance": pytest.approx(0.123)},
        {"seq": 7, "text": "It rained.", "distance": pytest.approx(0.5)},
    ]


def test_rag_results_collapse_and_clip_text(build_app):
    build, log_path = build_app
    long = "line one\nline two\t  spaced   out " + ("x" * 400)
    _seed_turn_with_rows(log_path, [{"seq": 3, "text": long, "distance": 1.0}])
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/inspect/turns")).json()

    row = run(scenario())["turns"][0]["rag"]["results"][0]
    # Whitespace collapsed (newlines/tabs/runs -> single spaces), no newline.
    assert "\n" not in row["text"] and "\t" not in row["text"]
    assert "  " not in row["text"]
    assert row["text"].startswith("line one line two spaced out ")
    # Clipped to the bullet limit with a trailing ellipsis.
    assert len(row["text"]) <= 241
    assert row["text"].endswith("…")


def test_rag_results_skips_malformed_and_legacy(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        # A legacy turn: no recall_rows at all -> [] (never a guess).
        log.append_event("s1", "user", "user", {"text": "hi"})
        log.append_event("s1", "narrative", "narrator", {"prose": "hello"})
        # A turn whose rows carry junk entries.
        log.append_event("s1", "user", "user", {"text": "again"})
        log.append_event(
            "s1",
            "narrative",
            "narrator",
            {"prose": "welcome"},
            meta={"config": {"recall_rows": ["nope", {"seq": "1", "text": None}, 7]}},
        )
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/inspect/turns")).json()

    turns = run(scenario())["turns"]
    assert turns[0]["rag"]["results"] == []
    # Malformed rows are dropped; the one usable row reports null text/seq.
    assert turns[1]["rag"]["results"] == [
        {"seq": None, "text": "", "distance": None}
    ]


# -- W5C-2: speaker-colour write endpoint ------------------------------------


def test_set_speaker_color_normalizes_hex():
    import tempfile

    with EventLog(Path(tempfile.mkdtemp()) / "e.db") as log:
        log.append_event("s1", "user", "user", {"text": "seed"})
        assert set_speaker_color(log, "s1", "Aria", "#ABC") == "#aabbcc"
        assert set_speaker_color(log, "s1", "Aria", "a1b2c3") == "#a1b2c3"
        with pytest.raises(ValueError):
            set_speaker_color(log, "s1", "Aria", "not-a-color")
        with pytest.raises(ValueError):
            set_speaker_color(log, "s1", "   ", "#aabbcc")


def test_patch_overrides_engine_assignment(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("s1", "user", "user", {"text": "seed"})
        assign_speaker_color(log, "s1", "Aria")  # engine -> palette[0]
    app = build()

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/sessions/s1/speaker_colors",
                json={"name": "Aria", "color": "#123456"},
            )
            listed = await client.get("/api/sessions/s1/speaker_colors")
            return patched, listed

    patched, listed = run(scenario())
    assert patched.status_code == 200
    body = patched.json()
    # No roster in this session, so the name matches no member (W7.9).
    assert body["speaker_colors"] == [
        {
            "member_id": None,
            "name": "Aria",
            "color": "#123456",
            "assigned_by": "user",
        }
    ]
    # Latest-wins: the read endpoint agrees (same fold, same shape).
    assert listed.json()["speaker_colors"] == body["speaker_colors"]


def test_patch_accepts_new_speaker_and_returns_list(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("s1", "user", "user", {"text": "seed"})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = await client.patch(
                "/api/sessions/s1/speaker_colors",
                json={"name": "Borin", "color": "#ff0000"},
            )
            second = await client.patch(
                "/api/sessions/s1/speaker_colors",
                json={"name": "Cyra", "color": "#00ff00"},
            )
            return first.json(), second.json()

    first, second = run(scenario())
    assert first["speaker_colors"] == [
        {
            "member_id": None,
            "name": "Borin",
            "color": "#ff0000",
            "assigned_by": "user",
        }
    ]
    assert second["speaker_colors"] == [
        {
            "member_id": None,
            "name": "Borin",
            "color": "#ff0000",
            "assigned_by": "user",
        },
        {
            "member_id": None,
            "name": "Cyra",
            "color": "#00ff00",
            "assigned_by": "user",
        },
    ]


def test_patch_unknown_session_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/sessions/nope/speaker_colors",
                json={"name": "Aria", "color": "#123456"},
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


def test_patch_invalid_color_and_empty_name_422(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("s1", "user", "user", {"text": "seed"})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            bad_color = await client.patch(
                "/api/sessions/s1/speaker_colors",
                json={"name": "Aria", "color": "nope"},
            )
            empty_name = await client.patch(
                "/api/sessions/s1/speaker_colors",
                json={"name": "   ", "color": "#aabbcc"},
            )
            missing = await client.patch(
                "/api/sessions/s1/speaker_colors", json={"name": "Aria"}
            )
            return bad_color, empty_name, missing

    bad_color, empty_name, missing = run(scenario())
    assert bad_color.status_code == 422
    assert "hex value" in bad_color.json()["detail"]
    assert empty_name.status_code == 422
    assert "non-empty" in empty_name.json()["detail"]
    assert missing.status_code == 422  # pydantic body validation


# -- W5C-3: roster / cast read -----------------------------------------------


def _wire_row(row: dict) -> dict:
    """The pre-W7 wire fields of a ``/cast`` row (kept for back-compat).

    W7 extends each row with ``member_id``/``provenance``/``status``/
    ``provisional``/``aliases``; the four legacy fields below stay present with
    the identical values, which is what this helper asserts on.
    """
    return {
        key: row[key] for key in ("name", "color", "assigned_by", "from_card")
    }


def test_cast_merges_roster_and_registry(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        log.append_event("s1", "user", "user", {"text": "[Imported card: Aria]"})
        _seed_card(log, "s1", "Aria")
        _seed_card(log, "s1", "Borin")
        assign_speaker_color(log, "s1", "Aria")  # engine palette[0]
        # A registry-only name (hue assigned before any card row).
        assign_speaker_color(log, "s1", "Ghost")
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/cast")).json()

    body = run(scenario())
    assert body["session_id"] == "s1"
    assert body["roster"] == ["Aria", "Borin"]
    # W3: the read assigns a hue to every not-yet-coloured name at first sight
    # (Borin), so no cast row is left blank.
    assert body["colors"] == {
        "Aria": SPEAKER_PALETTE[0],
        "Ghost": SPEAKER_PALETTE[1],
        "Borin": SPEAKER_PALETTE[2],
    }
    # Cards first (in card order), then registry-only names.
    assert [_wire_row(row) for row in body["cast"]] == [
        {
            "name": "Aria",
            "color": SPEAKER_PALETTE[0],
            "assigned_by": "engine",
            "from_card": True,
        },
        {
            "name": "Borin",
            "color": SPEAKER_PALETTE[2],
            "assigned_by": "engine",
            "from_card": True,
        },
        {
            "name": "Ghost",
            "color": SPEAKER_PALETTE[1],
            "assigned_by": "engine",
            "from_card": False,
        },
    ]


def test_cast_includes_bts_discovered_speaker_with_color(build_app):
    """W3: a world-state character with no card appears in ``/cast``, coloured."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_card(log, "s1", "Aria")
        _seed_world_state(
            log,
            "s1",
            {
                "Aria": {"emotional": {"mood": "calm"}},
                "Kesh": {"physical": {"hp": "9"}},
            },
        )
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/cast")).json()

    body = run(scenario())
    assert body["roster"] == ["Aria"]  # roster stays card-derived
    by_name = {row["name"]: row for row in body["cast"]}
    assert list(by_name) == ["Aria", "Kesh"]
    assert by_name["Kesh"]["from_card"] is False
    assert by_name["Kesh"]["color"] == SPEAKER_PALETTE[1]
    assert by_name["Kesh"]["assigned_by"] == "engine"
    assert body["colors"] == {"Aria": SPEAKER_PALETTE[0], "Kesh": SPEAKER_PALETTE[1]}


def test_cast_keeps_speakers_only_seen_in_earlier_bts_deltas(build_app):
    """A delta carries only changed characters; the known set is their union."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_world_state(log, "s1", {"Kesh": {"emotional": {"mood": "calm"}}})
        _seed_world_state(log, "s1", {"Toma": {"emotional": {"mood": "wary"}}})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/cast")).json()

    body = run(scenario())
    assert [row["name"] for row in body["cast"]] == ["Kesh", "Toma"]
    assert body["colors"] == {"Kesh": SPEAKER_PALETTE[0], "Toma": SPEAKER_PALETTE[1]}


def test_cast_first_sighting_coloring_is_idempotent(build_app):
    """Two reads append no second colour row (the write is a first sighting)."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_world_state(log, "s1", {"Kesh": {"emotional": {"mood": "calm"}}})
        # W9.16: the cast is BTS-admitted; a bare world_state write is display
        # only. Seed the member the way a turn does.
        from fabulist.state.cast import admit_member

        admit_member(log, "s1", "Kesh", provenance="bts", admitted_by="bts")
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = (await client.get("/api/sessions/s1/cast")).json()
            second = (await client.get("/api/sessions/s1/cast")).json()
            colors = (await client.get("/api/sessions/s1/speaker_colors")).json()
            return first, second, colors

    first, second, colors = run(scenario())
    assert first["cast"] == second["cast"]
    # W7.9: the row is roster-resolved, so it carries Kesh's member id.
    (row,) = colors["speaker_colors"]
    assert row["name"] == "Kesh"
    assert row["color"] == SPEAKER_PALETTE[0]
    assert row["assigned_by"] == "engine"
    assert isinstance(row["member_id"], str) and row["member_id"]


def test_cast_bts_palette_gives_eight_plus_distinct_colors(build_app):
    """W3: the merged list uses the 10-hue palette before any wrap."""
    build, log_path = build_app
    names = [f"Npc{i:02d}" for i in range(12)]
    with EventLog(log_path) as log:
        _seed_world_state(log, "s1", {name: {} for name in names})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/s1/cast")).json()

    body = run(scenario())
    assert [row["name"] for row in body["cast"]] == names
    slots = [row["color"] for row in body["cast"]]
    assert slots[: len(SPEAKER_PALETTE)] == list(SPEAKER_PALETTE)
    assert len(set(slots[: len(SPEAKER_PALETTE)])) == len(SPEAKER_PALETTE) >= 8
    # Palette exhaustion wraps deterministically instead of inventing a hue.
    assert slots[len(SPEAKER_PALETTE) :] == [SPEAKER_PALETTE[0], SPEAKER_PALETTE[1]]


def test_cast_fork_inherits_bts_cast_names(build_app):
    """A branch reads its parent's BTS cast (chain walk) and colours it locally."""
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_card(log, "s1", "Aria")
        _seed_world_state(log, "s1", {"Kesh": {"emotional": {"mood": "calm"}}})
        log.fork_session("s1", len(log.get_events("s1")), "fork1")
        # A speaker the branch discovers itself, after the fork point.
        _seed_world_state(log, "fork1", {"Toma": {"emotional": {"mood": "wary"}}})
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/sessions/fork1/cast")).json()

    body = run(scenario())
    # The inherited card + BTS name come through the chain walk; the branch's own
    # speaker follows them, so the palette continues instead of resetting.
    assert [row["name"] for row in body["cast"]] == ["Aria", "Kesh", "Toma"]
    assert body["colors"] == {
        "Aria": SPEAKER_PALETTE[0],
        "Kesh": SPEAKER_PALETTE[1],
        "Toma": SPEAKER_PALETTE[2],
    }


def test_cast_hides_a_retired_member_but_the_colour_still_resolves(build_app):
    """A tombstoned member leaves the pane; its colour row stays for the history.

    The pane lists who IS in the cast (a retired role word is not), while
    ``.../speaker_colors`` keeps serving the retired row: that is the read the
    transcript colours PAST quotes from, and the colour of a quote written
    before the retirement must not change.
    """
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_card(log, "s1", "Aria")
        ensure_roster(log, "s1")
        # The engine's own role-word member (bts-admitted, never card-captured).
        narrative_id = admit_member(
            log, "s1", "Narrative", provenance="bts", admitted_by="bts"
        )
        narrative_color = assign_member_color(log, "s1", narrative_id, "Narrative")
        assign_member_color(log, "s1", ensure_roster(log, "s1")[0].member_id, "Aria")
        retire_member(
            log, "s1", member_id=narrative_id, reason='role word "Narrative"'
        )
    app = build()

    async def scenario():
        async with client_for(app) as client:
            cast = (await client.get("/api/sessions/s1/cast")).json()
            colors = (await client.get("/api/sessions/s1/speaker_colors")).json()
            return cast, colors

    cast, colors = run(scenario())
    assert [row["name"] for row in cast["cast"]] == ["Aria"]
    assert "Narrative" not in cast["colors"]
    retired_row = next(
        row for row in colors["speaker_colors"] if row["member_id"] == narrative_id
    )
    assert retired_row["name"] == "Narrative"
    assert retired_row["color"] == narrative_color


def test_cast_unknown_session_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/nope/cast")

    assert run(scenario()).status_code == 404


def test_cast_survives_user_override(build_app):
    build, log_path = build_app
    with EventLog(log_path) as log:
        _seed_card(log, "s1", "Aria")
        assign_speaker_color(log, "s1", "Aria")
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.patch(
                "/api/sessions/s1/speaker_colors",
                json={"name": "Aria", "color": "#abcdef"},
            )
            return (await client.get("/api/sessions/s1/cast")).json()

    body = run(scenario())
    assert [_wire_row(row) for row in body["cast"]] == [
        {"name": "Aria", "color": "#abcdef", "assigned_by": "user", "from_card": True}
    ]
    assert body["colors"] == {"Aria": "#abcdef"}


def test_user_color_constant_is_not_palette():
    # Guard the reserved near-white stays out of the palette (B5 contract).
    assert SPEAKER_USER_COLOR not in SPEAKER_PALETTE
