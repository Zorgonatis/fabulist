"""Tests for the settings router (W2.11, reshaped by W6.3).

Exercises the real FastAPI app (httpx ASGI transport) with the settings router
mounted, against a tmp SQLite event log. The log stays the source of truth: the
``toggles`` map is folded from the ACTIVE PRESET's directive rows (the single
``enabled`` source, W6.3) and ``PATCH {toggles}`` writes those rows; features
still ride ``settings_changed`` system events. A protected/core directive
refuses disable with a 422. ``legacy_toggles`` exposes the raw pre-W6.3
``settings_changed`` fold read-only for the migration.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.settings import (
    SETTINGS_EVENT,
    SETTINGS_SESSION_ID,
    router,
)
from fabulist.log import EventLog
from fabulist.prompts.parts import default_directives
from fabulist.prompts.preset_store import PRESETS_ANCHOR


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


def test_get_default_catalog(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return (await client.get("/api/settings")).json()

    body = run(scenario())
    ids = [d.id for d in default_directives()]
    assert list(body["toggles"]) == ids
    assert body["meta"]["count"] == len(ids)
    assert body["meta"]["order"] == ids
    # No preset row yet -> every directive keeps its catalog (enabled) default.
    assert body["legacy_toggles"] == {}

    director = body["toggles"]["director"]
    assert director["label"] == "The Content Engine"
    assert director["enabled"] is True
    assert director["protected"] is False  # user decision: nothing protected
    assert director["core"] is True

    # The core directives are core (never trimmed) but NOT protected. The set
    # grew by engine_is_not with the Content Engine consolidation (Q6).
    assert set(body["meta"]["core"]) == {
        "director",
        "engine_is_not",
        "stop_and_pass",
        "no_user_control",
        "no_parroting",
        "anti_slop",
    }
    assert body["meta"]["protected"] == []  # nothing protected (user decision)


def test_patch_toggle_writes_preset_rows_not_settings(build_app):
    """(a-ish) PATCH {toggles} writes the preset's directive rows (one source)."""
    build, log_path = build_app
    app = build()
    target = next(d.id for d in default_directives() if not (d.core or d.protected))

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/settings", json={"toggles": {target: False}}
            )
            assert patched.status_code == 200
            first = patched.json()
            second = (await client.get("/api/settings")).json()
            return first, second

    first, second = run(scenario())
    assert first["toggles"][target]["enabled"] is False
    assert second["toggles"][target]["enabled"] is False

    # The write rode a PRESET event (the one enabled source), not a settings
    # event the narrator ignores.
    log = EventLog(log_path)
    preset_events = [
        e
        for e in log.get_events(PRESETS_ANCHOR)
        if e.type == "system" and e.payload.get("event") == "preset"
    ]
    settings_events = log.get_events(SETTINGS_SESSION_ID)
    log.close()
    assert preset_events
    rows = preset_events[-1].payload["directives"]
    assert {row["id"]: row["enabled"] for row in rows}[target] is False
    assert not [
        e
        for e in settings_events
        if e.type == "system" and e.payload.get("event") == SETTINGS_EVENT
    ]


def test_latest_write_wins_and_survives_app_rebuild(build_app):
    build, _ = build_app
    target = next(d.id for d in default_directives() if not (d.core or d.protected))

    async def flip(toggles):
        async with client_for(build()) as client:
            resp = await client.patch("/api/settings", json={"toggles": toggles})
            assert resp.status_code == 200
            return resp.json()

    run(flip({target: False}))
    run(flip({target: True}))
    run(flip({target: False}))

    # A fresh app over the same log folds history: the last write wins.
    async def read():
        async with client_for(build()) as client:
            return (await client.get("/api/settings")).json()

    body = run(read())
    assert body["toggles"][target]["enabled"] is False


def test_core_directive_disable_is_allowed(build_app):
    """Nothing is protected (user decision 2026-10-09): a core directive disables."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.patch(
                "/api/settings", json={"toggles": {"director": False}}
            )
            return resp, (await client.get("/api/settings")).json()

    resp, after = run(scenario())
    assert resp.status_code == 200
    assert after["toggles"]["director"]["enabled"] is False

    # The write rode a PRESET event (the one enabled source), not a settings one.
    log = EventLog(log_path)
    preset_events = log.get_events(PRESETS_ANCHOR)
    log.close()
    assert preset_events


def test_unknown_toggle_id_is_404(build_app):
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/settings", json={"toggles": {"not_a_directive": False}}
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert "not_a_directive" in resp.json()["detail"]["reason"]

    log = EventLog(log_path)
    events = log.get_events(SETTINGS_SESSION_ID)
    log.close()
    assert events == []


def test_enabling_a_protected_directive_is_allowed(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.patch(
                "/api/settings", json={"toggles": {"panel_placement": True}}
            )
            return resp, (await client.get("/api/settings")).json()

    resp, after = run(scenario())
    assert resp.status_code == 200
    assert after["toggles"]["panel_placement"]["enabled"] is True
