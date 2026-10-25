"""``comfy.base_url``: the ComfyUI endpoint is a settings value (issue #57).

The endpoint used to be a constant in code plus an injection seam
(``app.state.imagegen_base_url``) that only a test could reach — so a user whose
ComfyUI does not live on ``127.0.0.1:8188`` had nowhere to say so from the Image
Prompter dropdown in the connections pane.

It is now an ordinary persisted setting, riding the SAME log fold as the boolean
feature flags: a ``settings_changed`` system event on the ``__settings__`` anchor
carrying a ``values`` map (latest write wins per id). ``GET/PATCH /api/settings``
expose it (``values`` — the OVERRIDE only, ``null`` when nothing is authored, so
the surface can show the shipped default as a placeholder), and
:func:`fabulist.api.imagegen_api._base_url` resolves it per call:

    setting  >  app.state.imagegen_base_url (injection seam)  >  DEFAULT_BASE_URL

which is what makes a URL typed into the drawer reach the ComfyUI client on the
next request with no restart. The default stays ``http://127.0.0.1:8188``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api import imagegen_api
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.imagegen_api import DEFAULT_BASE_URL, router as imagegen_router
from fabulist.api.settings import (
    COMFY_BASE_URL_KEY,
    SETTINGS_EVENT,
    SETTINGS_SESSION_ID,
)
from fabulist.api.settings import router as settings_router
from fabulist.api.settings import setting_value, settings_values
from fabulist.comfy import ComfyClient

WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
}

CUSTOM_URL = "http://comfy.lan:9000"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """App factory over a tmp log; the reachability cache starts empty."""
    log_path = tmp_path / "events.db"
    monkeypatch.setattr(imagegen_api, "_reach_cache", {})
    built = []

    def _build(*, inject: str | None = None):
        app = create_app(FakeTurnEngine(), log_path)
        app.include_router(settings_router)
        app.include_router(imagegen_router)
        if inject is not None:
            app.state.imagegen_base_url = inject
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _comfy_handler(seen: list[str], *, stats_status: int = 200):
    """Minimal ComfyUI stand-in: ``/system_stats`` answers, everything else fails."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/system_stats"):
            return httpx.Response(stats_status, json={"system": {}})
        return httpx.Response(400, json={"error": "nope"})

    return handler


def _install_client(app, seen: list[str], *, stats_status: int = 200) -> list[str]:
    """Install a fake ComfyUI client; return the list of base URLs it was built with."""
    bases: list[str] = []
    transport = httpx.MockTransport(_comfy_handler(seen, stats_status=stats_status))

    def factory(base: str) -> ComfyClient:
        bases.append(base)
        return ComfyClient(
            base,
            client=httpx.AsyncClient(transport=transport),
            poll_interval_s=0.0,
        )

    app.state.imagegen_client_factory = factory
    return bases


def _enable(app) -> None:
    app.state.log.append_event(
        SETTINGS_SESSION_ID,
        "system",
        "user",
        {"event": SETTINGS_EVENT, "features": {"comfy.enabled": True}},
    )


def _seed_session(app, session_id: str = "s1") -> None:
    app.state.log.append_event(
        session_id, "system", "system", {"event": "session_created"}
    )


# --- the setting itself ----------------------------------------------------


def test_the_override_is_unset_by_default(build_app):
    app = build_app()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/settings")

    body = run(scenario()).json()
    entry = body["values"][COMFY_BASE_URL_KEY]
    assert entry["id"] == COMFY_BASE_URL_KEY
    assert entry["name"]  # a human label for the surface
    assert entry["value"] is None  # nothing authored -> the shipped default rules


def test_setting_round_trip(build_app):
    app = build_app()

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/settings", json={"values": {COMFY_BASE_URL_KEY: CUSTOM_URL}}
            )
            read = await client.get("/api/settings")
            return patched, read

    patched, read = run(scenario())
    assert patched.status_code == 200
    assert patched.json()["values"][COMFY_BASE_URL_KEY]["value"] == CUSTOM_URL
    assert read.json()["values"][COMFY_BASE_URL_KEY]["value"] == CUSTOM_URL
    # the fold the engine reads agrees, and it rides the settings anchor
    log = app.state.log
    assert setting_value(log, COMFY_BASE_URL_KEY) == CUSTOM_URL
    assert settings_values(log) == {COMFY_BASE_URL_KEY: CUSTOM_URL}
    events = [
        event
        for event in log.get_events(SETTINGS_SESSION_ID)
        if event.payload.get("event") == SETTINGS_EVENT
    ]
    assert events and events[-1].payload["values"] == {COMFY_BASE_URL_KEY: CUSTOM_URL}


def test_a_trailing_slash_is_normalized(build_app):
    app = build_app()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/settings", json={"values": {COMFY_BASE_URL_KEY: CUSTOM_URL + "/"}}
            )

    body = run(scenario()).json()
    assert body["values"][COMFY_BASE_URL_KEY]["value"] == CUSTOM_URL


def test_an_empty_value_clears_the_override(build_app):
    app = build_app()

    async def scenario():
        async with client_for(app) as client:
            await client.patch(
                "/api/settings", json={"values": {COMFY_BASE_URL_KEY: CUSTOM_URL}}
            )
            cleared = await client.patch(
                "/api/settings", json={"values": {COMFY_BASE_URL_KEY: "  "}}
            )
            return cleared

    body = run(scenario()).json()
    assert body["values"][COMFY_BASE_URL_KEY]["value"] is None
    assert setting_value(app.state.log, COMFY_BASE_URL_KEY) is None


def test_an_unusable_url_is_422_and_writes_nothing(build_app):
    app = build_app()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/settings", json={"values": {COMFY_BASE_URL_KEY: "127.0.0.1:8188"}}
            )

    resp = run(scenario())
    assert resp.status_code == 422
    assert COMFY_BASE_URL_KEY in resp.json()["detail"]["reason"]
    assert settings_values(app.state.log) == {}


def test_an_unknown_setting_id_is_404(build_app):
    app = build_app()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/settings", json={"values": {"comfy.bogus": CUSTOM_URL}}
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert "comfy.bogus" in resp.json()["detail"]["reason"]


# --- it is effective ------------------------------------------------------


def test_config_reports_the_overridden_url_and_probes_it(build_app):
    """The URL typed in the drawer is the URL the ComfyUI client is built with."""
    app = build_app()  # no injection seam: the setting is the only source
    _seed_session(app)
    _enable(app)
    seen: list[str] = []
    bases = _install_client(app, seen)

    async def scenario():
        async with client_for(app) as client:
            await client.patch(
                "/api/settings", json={"values": {COMFY_BASE_URL_KEY: CUSTOM_URL}}
            )
            return await client.get("/api/imagegen/config")

    body = run(scenario()).json()
    assert body["base_url"] == CUSTOM_URL
    assert body["enabled"] is True
    assert body["reachable"] is True
    # the probe actually went to the configured host, not the built-in default
    assert bases == [CUSTOM_URL]
    assert seen == [f"{CUSTOM_URL}/system_stats"]


def test_the_setting_reaches_the_generation_client(build_app):
    app = build_app()
    _seed_session(app)
    _enable(app)
    seen: list[str] = []
    bases = _install_client(app, seen)

    async def scenario():
        async with client_for(app) as client:
            await client.patch(
                "/api/settings", json={"values": {COMFY_BASE_URL_KEY: CUSTOM_URL}}
            )
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "a lantern",
                    "workflow": WORKFLOW,
                    "overrides": {"positive": "a brass lantern"},
                },
            )

    resp = run(scenario())
    # the submit fails against the fake ComfyUI (HTTP 200 error payload), but the
    # client it failed on was the CONFIGURED endpoint — that is the claim
    assert resp.status_code == 200
    assert resp.json().get("error")
    assert bases == [CUSTOM_URL]


def test_the_setting_outranks_the_injection_seam(build_app):
    app = build_app(inject="http://injected:8188")
    _seed_session(app)
    _enable(app)
    seen: list[str] = []
    bases = _install_client(app, seen)

    async def scenario():
        async with client_for(app) as client:
            await client.patch(
                "/api/settings", json={"values": {COMFY_BASE_URL_KEY: CUSTOM_URL}}
            )
            return await client.get("/api/imagegen/config")

    assert run(scenario()).json()["base_url"] == CUSTOM_URL
    assert bases == [CUSTOM_URL]


def test_the_injection_seam_still_wins_when_nothing_is_authored(build_app):
    """Tests/deployments that inject an endpoint keep working (no setting set)."""
    app = build_app(inject="http://injected:8188")
    _seed_session(app)
    _enable(app)
    seen: list[str] = []
    bases = _install_client(app, seen)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/imagegen/config")

    assert run(scenario()).json()["base_url"] == "http://injected:8188"
    assert bases == ["http://injected:8188"]


def test_the_shipped_default_is_localhost(build_app):
    """Issue #57's premise: with nothing authored, 127.0.0.1:8188 is the endpoint."""
    app = build_app()
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8188"

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/imagegen/config")

    assert run(scenario()).json()["base_url"] == DEFAULT_BASE_URL
