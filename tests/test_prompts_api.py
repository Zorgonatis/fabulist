"""Tests for D18 preset store + prompt API.

Exercises the real FastAPI app (httpx ASGI transport) with the prompts router
mounted, against a tmp SQLite event log — the same mount pattern as
``tests/test_state_api.py`` (router included on a ``create_app`` app; tmp event
log path; logs closed after). The log stays the source of truth (D9): CRUD
appends ``preset`` events to ``__presets__`` and reads fold latest-wins per id.

Preview is covered in both modes: the **degraded** fallback (no
``app.state.message_builder`` — the router mounted without the wiring layer)
and the **builder** path (an injected production-shaped callable), proving the
parity contract's branch and ``preview_mode`` marker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.prompts_api import router
from fabulist.log import EventLog
from fabulist.prompts import preset_store
from fabulist.prompts.agent_sets import INTERACTIONS
from fabulist.prompts.modules import (
    DEFAULT_MODULES,
    FLOW_STRUCTURAL_TYPES,
    from_default,
)
from fabulist.prompts.parts import default_directives, directive_to_row
from fabulist.prompts.variables import VARIABLE_SPECS
from fabulist.state.settings import SETTINGS_SESSION_ID


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _seed_session(log: EventLog, session_id: str = "s1") -> None:
    log.append_event(session_id, "user", "user", {"text": "hello"})


# -- store unit surface ------------------------------------------------------


def test_preset_store_round_trip(tmp_path: Path):
    """preset_to_json / preset_from_json round-trip the export shape."""
    log = EventLog(tmp_path / "store.db")
    try:
        pid = preset_store.append_preset(
            log,
            {
                "name": "Mine",
                "modules": [asdict(m) for m in from_default()],
                "toggles": {"director": False},
                "prompt_overrides": {"ooc_system": "be terse"},
                "agent_bindings": {"ooc": {"agent": "narrator", "overrides": {}}},
                "version": 1,
            },
        )
        preset = preset_store.get_preset(log, pid)
        assert preset is not None
        payload = preset_store.preset_to_json(preset)
        again = preset_store.preset_from_json(payload)
        assert preset_store.preset_to_json(again) == payload
    finally:
        log.close()


def test_store_latest_wins_per_id_and_active_default(tmp_path: Path):
    """No preset_active event -> active_preset is None (default pipeline)."""
    log = EventLog(tmp_path / "store2.db")
    try:
        assert preset_store.active_preset(log) is None
        pid = preset_store.append_preset(log, {"name": "A"})
        preset_store.append_preset(log, {"preset_id": pid, "name": "A2"})
        assert preset_store.get_preset(log, pid).name == "A2"
        assert len(preset_store.list_presets(log)) == 1
        preset_store.set_active(log, pid)
        assert preset_store.active_preset(log).preset_id == pid
        preset_store.set_active(log, None)
        assert preset_store.active_preset(log) is None
        with pytest.raises(ValueError):
            preset_store.set_active(log, "ghost")
    finally:
        log.close()


def test_active_preset_is_mapping_for_agent_sets_seam(tmp_path: Path):
    """The D19 seam reads the active preset as a Mapping (`preset.get(...)`)."""
    log = EventLog(tmp_path / "store3.db")
    try:
        pid = preset_store.append_preset(
            log,
            {
                "name": "Bound",
                "agent_bindings": {"ooc": {"agent": "narrator", "overrides": {}}},
            },
        )
        preset_store.set_active(log, pid)
        preset = preset_store.active_preset(log)
        assert isinstance(preset, Mapping)
        assert preset.get("agent_bindings") == {
            "ooc": {"agent": "narrator", "overrides": {}}
        }
    finally:
        log.close()


# -- CRUD --------------------------------------------------------------------


def test_create_list_get_roundtrip(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/prompts/presets", json={"name": "My preset"}
            )
            listed = await client.get("/api/prompts/presets")
            pid = created.json()["preset"]["preset_id"]
            fetched = await client.get(f"/api/prompts/presets/{pid}")
            return created, listed, fetched, pid

    created, listed, fetched, pid = run(scenario())
    assert created.status_code == 200
    preset = created.json()["preset"]
    assert preset["name"] == "My preset"
    assert preset["preset_id"] == pid
    # modules default from from_default(): today's five structural rows.
    assert [m["type"] for m in preset["modules"]] == [
        m.type for m in DEFAULT_MODULES
    ]

    assert listed.status_code == 200
    assert [p["preset_id"] for p in listed.json()["presets"]] == [pid]
    assert fetched.status_code == 200
    assert fetched.json()["preset"]["preset_id"] == pid


def test_put_full_replace(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/prompts/presets", json={"name": "Before"}
            )
            pid = created.json()["preset"]["preset_id"]
            replaced = await client.put(
                f"/api/prompts/presets/{pid}",
                json={
                    "name": "After",
                    "modules": [
                        {
                            "id": "m1",
                            "name": "Custom",
                            "type": "text",
                            "role": "system",
                            "content": "Hi {{user}}",
                            "enabled": True,
                        }
                    ],
                    "toggles": {"director": False},
                },
            )
            return pid, replaced

    pid, replaced = run(scenario())
    assert replaced.status_code == 200
    preset = replaced.json()["preset"]
    assert preset["name"] == "After"
    assert preset["modules"] == [
        {
            "id": "m1",
            "name": "Custom",
            "type": "text",
            "role": "system",
            "content": "Hi {{user}}",
            "enabled": True,
        }
    ]
    assert preset["toggles"] == {"director": False}
    # Full replace keeps the same id; the list still holds exactly one preset.
    assert preset["preset_id"] == pid


def test_put_unknown_preset_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.put(
                "/api/prompts/presets/ghost", json={"name": "Nope"}
            )

    assert run(scenario()).status_code == 404


def test_create_and_put_empty_name_is_422(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post("/api/prompts/presets", json={"name": "  "})
            made = await client.post("/api/prompts/presets", json={"name": "ok"})
            pid = made.json()["preset"]["preset_id"]
            replaced = await client.put(
                f"/api/prompts/presets/{pid}", json={"name": ""}
            )
            return created, replaced

    created, replaced = run(scenario())
    assert created.status_code == 422
    assert replaced.status_code == 422


def test_get_unknown_preset_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/presets/ghost")

    assert run(scenario()).status_code == 404


# -- active pointer ----------------------------------------------------------


def test_activate_deactivate_latest_wins(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = (await client.post("/api/prompts/presets", json={"name": "A"})).json()[
                "preset"
            ]["preset_id"]
            second = (await client.post("/api/prompts/presets", json={"name": "B"})).json()[
                "preset"
            ]["preset_id"]
            await client.post(f"/api/prompts/presets/{first}/activate")
            await client.post(f"/api/prompts/presets/{second}/activate")
            active = await client.get("/api/prompts/active")
            deactivated = await client.post("/api/prompts/presets/deactivate")
            after = await client.get("/api/prompts/active")
            return second, active, deactivated, after

    second, active, deactivated, after = run(scenario())
    assert active.json()["preset"]["preset_id"] == second
    assert deactivated.json() == {"active": None, "preset": None}
    assert after.json() == {"preset": None}


def test_active_is_null_with_no_pointer(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/active")

    assert run(scenario()).json() == {"preset": None}


def test_activate_unknown_is_404_and_writes_nothing(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/prompts/presets/ghost/activate")

    resp = run(scenario())
    assert resp.status_code == 404
    assert preset_store.active_preset(app.state.log) is None


# -- seed + variables --------------------------------------------------------


def test_seed_from_default_produces_editable_modules(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            seeded = await client.post(
                "/api/prompts/seed-from-default", json={"name": "Seeded"}
            )
            preset = seeded.json()["preset"]
            return preset

    preset = run(scenario())
    assert preset["name"] == "Seeded"
    assert [m["type"] for m in preset["modules"]] == [m.type for m in DEFAULT_MODULES]
    # Editable: fresh unique ids, not the DEFAULT_MODULES singleton ids.
    ids = [m["id"] for m in preset["modules"]]
    assert len(set(ids)) == len(ids)
    assert set(ids).isdisjoint({m.id for m in DEFAULT_MODULES})


def test_variables_endpoint_lists_specs(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/variables")

    resp = run(scenario())
    assert resp.status_code == 200
    variables = resp.json()["variables"]
    assert {v["name"] for v in variables} == {s.name for s in VARIABLE_SPECS}
    assert all({"name", "description", "example"} <= set(v) for v in variables)


# -- preview -----------------------------------------------------------------


def test_preview_degraded_mode_messages(build_app):
    build, _ = build_app
    app = build()
    _seed_session(app.state.log)

    # A preset with a text module that references a variable, to prove the
    # degraded path compiles preset modules (not just DEFAULT_MODULES).
    log = app.state.log
    pid = preset_store.append_preset(
        log,
        {
            "name": "Previewable",
            "modules": [
                {"id": "t", "name": "T", "type": "text", "role": "system",
                 "content": "hello {{message}}", "enabled": True},
                {"id": "u", "name": "U", "type": "user", "role": "user",
                 "content": "", "enabled": True},
            ],
        },
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "go", "preset_id": pid},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["preview_mode"] == "degraded"
    assert body["messages"] == [
        {"role": "system", "content": "hello go"},
        {"role": "user", "content": "go"},
    ]
    assert body["chars"] == len("hello go") + len("go")


def test_preview_builder_mode_uses_production_builder(build_app):
    build, _ = build_app
    app = build()
    _seed_session(app.state.log)

    seen = {}

    async def builder(state):
        seen.update(state)
        return [{"role": "system", "content": "BUILT"}, {"role": "user", "content": "U"}]

    app.state.message_builder = builder

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "turn text"},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["preview_mode"] == "builder"
    assert body["messages"] == [
        {"role": "system", "content": "BUILT"},
        {"role": "user", "content": "U"},
    ]
    # Parity: the production builder saw the TurnState-shaped dict.
    assert seen["session_id"] == "s1"
    assert seen["user_content"] == "turn text"


def test_preview_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "ghost", "user_text": "hi"},
            )

    assert run(scenario()).status_code == 404


def test_preview_empty_user_text_is_422(build_app):
    build, _ = build_app
    app = build()
    _seed_session(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "   "},
            )

    assert run(scenario()).status_code == 422


def test_preview_unknown_preset_is_404(build_app):
    build, _ = build_app
    app = build()
    _seed_session(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "hi", "preset_id": "ghost"},
            )

    assert run(scenario()).status_code == 404


# -- directives CRUD (W2a) ----------------------------------------------------


def _dir_rows() -> list[dict]:
    """Two editable directive rows (id-bearing; the store normalizes the rest)."""
    return [
        {"id": "director", "name": "Director", "content": "KEEP SCENES TIGHT"},
        {"id": "house_rule", "name": "House rule", "content": "no purple prose"},
    ]


def test_put_directives_persist_null_keeps_empty_clears(build_app):
    """PUT directives: a list persists; ``null``/omitted KEEPS; ``[]`` clears.

    The keep-on-null is the deliberate divergence from ``modules``-on-PUT (full
    replace): the directive catalog is inherit-by-default, so an unrelated PUT
    must not silently destroy the owner's list.
    """
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post("/api/prompts/presets", json={"name": "Dir"})
            pid = created.json()["preset"]["preset_id"]
            put = await client.put(
                f"/api/prompts/presets/{pid}",
                json={"name": "Dir", "directives": _dir_rows()},
            )
            kept_null = await client.put(
                f"/api/prompts/presets/{pid}",
                json={"name": "Dir 2", "directives": None},
            )
            kept_omitted = await client.put(
                f"/api/prompts/presets/{pid}", json={"name": "Dir 2b"}
            )
            cleared = await client.put(
                f"/api/prompts/presets/{pid}", json={"name": "Dir 3", "directives": []}
            )
            return put, kept_null, kept_omitted, cleared

    put, kept_null, kept_omitted, cleared = run(scenario())
    assert put.status_code == 200
    rows = put.json()["preset"]["directives"]
    assert [row["id"] for row in rows] == ["director", "house_rule"]
    assert rows[0]["content"] == "KEEP SCENES TIGHT"
    # Store-side normalization filled the nine-field row shape.
    assert rows[0]["enabled"] is True
    assert rows[0]["priority"] == 50

    for kept in (kept_null, kept_omitted):
        assert kept.status_code == 200
        assert [row["id"] for row in kept.json()["preset"]["directives"]] == [
            "director",
            "house_rule",
        ]

    # [] is the explicit clear: back to inheriting the code catalog (the wire
    # omits the key for an empty list).
    assert cleared.status_code == 200
    assert "directives" not in cleared.json()["preset"]
    stored = preset_store.get_preset(
        app.state.log, cleared.json()["preset"]["preset_id"]
    )
    assert stored.directives == []
    assert preset_store.preset_directives(stored) is None  # inherit signal


def test_patch_directives_omitted_keeps_provided_replaces(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/prompts/presets",
                json={"name": "Dir", "directives": _dir_rows()},
            )
            pid = created.json()["preset"]["preset_id"]
            kept = await client.patch(
                f"/api/prompts/presets/{pid}", json={"name": "Renamed"}
            )
            replaced = await client.patch(
                f"/api/prompts/presets/{pid}",
                json={
                    "directives": [
                        {"id": "only", "name": "Only", "content": "one rule"}
                    ]
                },
            )
            return pid, kept, replaced

    pid, kept, replaced = run(scenario())
    assert kept.status_code == 200
    assert kept.json()["preset"]["name"] == "Renamed"
    assert [row["id"] for row in kept.json()["preset"]["directives"]] == [
        "director",
        "house_rule",
    ]
    assert replaced.status_code == 200
    assert [row["id"] for row in replaced.json()["preset"]["directives"]] == ["only"]
    # Persisted in the log (log-owned, latest wins).
    stored = preset_store.get_preset(app.state.log, pid)
    assert [row["id"] for row in stored.directives] == ["only"]


def test_directive_duplicate_ids_is_422(build_app):
    build, _ = build_app
    app = build()

    rows = [
        {"id": "director", "name": "A", "content": "x"},
        {"id": "director", "name": "B", "content": "y"},
        {"id": "scene_images", "name": "C", "content": "z"},
        {"id": "scene_images", "name": "D", "content": "w"},
    ]

    async def scenario():
        async with client_for(app) as client:
            made = await client.post("/api/prompts/presets", json={"name": "ok"})
            pid = made.json()["preset"]["preset_id"]
            created = await client.post(
                "/api/prompts/presets", json={"name": "Dupes", "directives": rows}
            )
            patched = await client.patch(
                f"/api/prompts/presets/{pid}", json={"directives": rows}
            )
            put = await client.put(
                f"/api/prompts/presets/{pid}", json={"name": "ok", "directives": rows}
            )
            return pid, created, patched, put

    pid, created, patched, put = run(scenario())
    for resp in (created, patched, put):
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["reason"] == "duplicate directive ids in one payload"
        assert detail["ids"] == ["director", "scene_images"]
    # Rejected at the door: nothing was written.
    assert preset_store.get_preset(app.state.log, pid).directives == []


def test_put_directive_disabled_persists_without_settings_event(build_app):
    """(a) A preset row ``enabled=False`` is the one disable source (W6.3).

    Disabling a non-protected directive through the preset write path must
    persist on the row and must NOT append a ``settings_changed`` event — the
    settings toggle map is no longer the ``enabled`` source.
    """
    build, _ = build_app
    app = build()
    log = app.state.log

    async def scenario():
        async with client_for(app) as client:
            created = await client.post("/api/prompts/presets", json={"name": "Rules"})
            pid = created.json()["preset"]["preset_id"]
            return await client.put(
                f"/api/prompts/presets/{pid}",
                json={
                    "name": "Rules",
                    "directives": [
                        {"id": "scene_images", "name": "Scene images", "enabled": False}
                    ],
                },
            )

    resp = run(scenario())
    assert resp.status_code == 200
    rows = resp.json()["preset"]["directives"]
    assert rows[0]["id"] == "scene_images"
    assert rows[0]["enabled"] is False

    # The disable rode the preset event, not a settings_changed event.
    settings_events = [
        e
        for e in log.get_events(SETTINGS_SESSION_ID)
        if e.type == "system" and e.payload.get("event") == "settings_changed"
    ]
    assert settings_events == []


def test_disabling_core_directive_is_allowed(build_app):
    """Nothing is protected (user decision 2026-10-09): PUT/PATCH may disable a core row."""
    build, _ = build_app
    app = build()

    core_rows = [
        {"id": "director", "name": "Director", "content": "x", "enabled": False}
    ]

    async def scenario():
        async with client_for(app) as client:
            created = await client.post("/api/prompts/presets", json={"name": "Guard"})
            pid = created.json()["preset"]["preset_id"]
            put = await client.put(
                f"/api/prompts/presets/{pid}",
                json={"name": "Guard", "directives": core_rows},
            )
            patch = await client.patch(
                f"/api/prompts/presets/{pid}", json={"directives": core_rows}
            )
            return pid, put, patch

    pid, put, patch = run(scenario())
    assert put.status_code == 200
    assert patch.status_code == 200
    stored = preset_store.get_preset(app.state.log, pid)
    rows = preset_store.preset_directives(stored)
    assert rows is not None
    assert [r.id for r in rows] == ["director"]
    assert rows[0].enabled is False


def test_steps_endpoint_lists_every_interaction_including_critic(build_app):
    """(d) GET /api/prompts/steps: one step per interaction, correct system_source.

    Against the factory image the authored interactions report
    ``system_source == "authored"`` (their seeded ``system`` row carries text),
    while ``narrate`` (no ``system`` row) and ``assistant`` (empty body) report
    ``"engine"``.
    """
    build, _ = build_app
    app = build()
    preset_store.ensure_default_preset(app.state.log)  # the factory image

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/prompts/steps")).json()

    body = run(scenario())
    steps = {step["id"]: step for step in body["steps"]}
    assert [step["id"] for step in body["steps"]] == list(INTERACTIONS)
    assert len(body["steps"]) == len(INTERACTIONS)
    assert "critic" in steps
    assert "npc" in steps

    critic = steps["critic"]
    assert critic["title"] == "Turn critic"
    assert critic["default_agent"] == "extractor"
    assert critic["bound_agent"] == "extractor"
    assert critic["system_source"] == "authored"
    assert critic["structural_types"] == sorted(FLOW_STRUCTURAL_TYPES["critic"])
    assert critic["block_count"] == 2
    assert critic["enabled"] is True

    # narrate has no ``system`` row (narrator_system) -> engine-supplied.
    assert steps["narrate"]["system_source"] == "engine"
    # assistant's factory ``system`` row body is empty -> engine-supplied.
    assert steps["assistant"]["system_source"] == "engine"
    # ooc's factory row carries authored text.
    assert steps["ooc"]["system_source"] == "authored"

    for step in body["steps"]:
        assert step["title"] and step["blurb"] and step["trigger"]


def test_patch_keeps_surfaces(build_app):
    """R7: PATCH used to rebuild the payload WITHOUT surfaces, wiping them."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/prompts/presets",
                json={"name": "Surfaced", "surfaces": {"library_doc": "MY DOC"}},
            )
            pid = created.json()["preset"]["preset_id"]
            patched = await client.patch(
                f"/api/prompts/presets/{pid}", json={"name": "Surfaced 2"}
            )
            stored_kept = dict(
                preset_store.get_preset(app.state.log, pid).surfaces
            )
            replaced = await client.patch(
                f"/api/prompts/presets/{pid}",
                json={"surfaces": {"library_doc": "NEW DOC"}},
            )
            return pid, patched, stored_kept, replaced

    pid, patched, stored_kept, replaced = run(scenario())
    assert patched.status_code == 200
    # Omitted surfaces keep the stored block (the R7 fix)...
    assert patched.json()["preset"]["surfaces"]["library_doc"] == "MY DOC"
    assert stored_kept["library_doc"] == "MY DOC"
    # ...and providing them still replaces.
    assert replaced.status_code == 200
    assert replaced.json()["preset"]["surfaces"]["library_doc"] == "NEW DOC"


def test_factory_directives_endpoint_lists_seed_rows(build_app):
    """GET /api/prompts/directives is the factory seed as data (18 code rows)."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/directives")

    resp = run(scenario())
    assert resp.status_code == 200
    rows = resp.json()["directives"]
    assert rows == [directive_to_row(d) for d in default_directives()]
    assert len(rows) == 19  # the code catalog (scene_images + engine_context)
    # The frozen provenance contract leads the catalog (right after the framing).
    assert rows[0]["id"] == "engine_context"
    assert rows[1]["id"] == "director"


# -- export / import (W2b) ----------------------------------------------------


def test_export_preset_is_attachment_download(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/prompts/presets",
                json={"name": "My Exported Preset", "directives": _dir_rows()},
            )
            pid = created.json()["preset"]["preset_id"]
            exported = await client.get(f"/api/prompts/presets/{pid}/export")
            return pid, exported

    pid, exported = run(scenario())
    assert exported.status_code == 200
    assert exported.headers["content-disposition"] == (
        'attachment; filename="My-Exported-Preset.preset.json"'
    )
    body = exported.json()
    assert body["preset_id"] == pid
    assert [row["id"] for row in body["directives"]] == ["director", "house_rule"]


def test_export_unknown_preset_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/presets/ghost/export")

    assert run(scenario()).status_code == 404


def test_import_always_mints_new_preset(build_app):
    """Import never overwrites: fresh id, is_default=False, factory_version=0."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/prompts/presets",
                json={"name": "Source", "directives": _dir_rows()},
            )
            pid = created.json()["preset"]["preset_id"]
            exported = (await client.get(f"/api/prompts/presets/{pid}/export")).json()
            imported = await client.post("/api/prompts/presets/import", json=exported)
            wrapped = await client.post(
                "/api/prompts/presets/import", json={"preset": exported}
            )
            return pid, imported, wrapped

    pid, imported, wrapped = run(scenario())
    assert imported.status_code == 200
    new_preset = imported.json()["preset"]
    assert new_preset["preset_id"] != pid
    assert new_preset["name"] == "Source"
    assert new_preset["is_default"] is False
    assert new_preset["factory_version"] == 0
    assert [row["id"] for row in new_preset["directives"]] == [
        "director",
        "house_rule",
    ]
    # The source row is untouched (no overwrite path).
    assert preset_store.get_preset(app.state.log, pid).name == "Source"

    # The {"preset": {...}} wrapper is accepted and mints yet another id.
    assert wrapped.status_code == 200
    assert wrapped.json()["preset"]["preset_id"] != new_preset["preset_id"]
    assert len(preset_store.list_presets(app.state.log)) == 3


def test_import_default_export_never_claims_default(build_app):
    build, _ = build_app
    app = build()
    preset_store.ensure_default_preset(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            exported = (
                await client.get("/api/prompts/presets/default/export")
            ).json()
            imported = await client.post("/api/prompts/presets/import", json=exported)
            return exported, imported

    exported, imported = run(scenario())
    # The default row exports like any other...
    assert exported["preset_id"] == "default"
    assert exported["is_default"] is True
    # ...but its import is a copy, never the default id.
    assert imported.status_code == 200
    new_preset = imported.json()["preset"]
    assert new_preset["preset_id"] != "default"
    assert new_preset["is_default"] is False
    assert preset_store.default_preset(app.state.log).preset_id == "default"


def test_import_legacy_payload_without_directives(build_app):
    """A pre-directives export (no ``directives`` key) imports as inheriting."""
    build, _ = build_app
    app = build()

    legacy = {
        "name": "Legacy",
        "modules": [
            {
                "id": "m1",
                "name": "Custom",
                "type": "text",
                "role": "system",
                "content": "Hi",
                "enabled": True,
            }
        ],
        "toggles": {},
        "version": 1,
    }

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/prompts/presets/import", json=legacy)

    resp = run(scenario())
    assert resp.status_code == 200
    preset = resp.json()["preset"]
    assert preset["name"] == "Legacy"
    assert "directives" not in preset
    stored = preset_store.get_preset(app.state.log, preset["preset_id"])
    assert stored.directives == []
    assert preset_store.preset_directives(stored) is None  # inherit signal
    assert stored.modules[0]["id"] == "m1"


def test_import_name_fallback_and_bad_body(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            unnamed = await client.post(
                "/api/prompts/presets/import", json={"modules": []}
            )
            not_object = await client.post(
                "/api/prompts/presets/import", content=b"[1, 2]"
            )
            return unnamed, not_object

    unnamed, not_object = run(scenario())
    assert unnamed.status_code == 200
    assert unnamed.json()["preset"]["name"] == "Imported preset"
    assert not_object.status_code == 422


def test_every_step_renders_even_with_no_stored_flow():
    """W9.16: a NEW interaction has no stored rows, so the route falls through to
    DEFAULT_FLOWS (PromptModule dataclasses). It must normalise them to dicts —
    the raw objects 500'd the step list on `row.get`."""
    from fabulist.api.prompts_api import _effective_flow_rows
    from fabulist.prompts.agent_sets import INTERACTIONS

    for interaction in INTERACTIONS:
        rows = _effective_flow_rows(None, interaction)
        assert rows, interaction
        assert all(isinstance(row, dict) for row in rows), interaction
