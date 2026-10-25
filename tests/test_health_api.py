"""``GET /health`` — liveness plus the configuration a probe needs (issue #4).

Why the endpoint exists: a configuration problem used to surface mid-turn. A
first-run checkout (no configs document at all) had no cheap way to ask "is a
narrator bound, and where is the config I am reading?" without spending a
model call. The body therefore reports what the ENGINE resolved — the narrator
slot's connection NAME, the declared slots that resolve to nothing, the config
document's path and whether it exists — and never a key.

Mount pattern mirrors the other API tests: build the app over a tmp event-log
path via :func:`create_app`, install a :class:`RegistryStore` as
``app.state.registry`` — exactly how the hot-reload wiring installs it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.connections.registry import RegistryStore


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    built = []

    def _build(store: RegistryStore):
        app = create_app(FakeTurnEngine(), tmp_path / "events.db")
        app.state.registry = store
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


def get_health(app) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health")

    return asyncio.run(_run())


def write_config(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def test_health_reports_the_bound_narrator(tmp_path: Path, build_app) -> None:
    """A configured run names its narrator connection and its config document."""
    config = write_config(
        tmp_path / "connections.json",
        {
            "connections": {
                "local": {
                    "base_url": "http://127.0.0.1:8080/v1",
                    "model": "your-model-id",
                }
            },
            "agents": {"narrator": "local"},
        },
    )
    store = RegistryStore(config, env_path=tmp_path / ".env")

    response = get_health(build_app(store))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["narrator"] == "local"
    assert body["config_path"] == str(config)
    assert body["config_exists"] is True
    # The narration slot resolves through the legacy twin, so it is NOT unbound;
    # a slot the document never covered is reported honestly.
    assert "completion.narration" not in body["unbound_slots"]
    assert "completion.state" in body["unbound_slots"]


def test_health_answers_on_a_first_run_with_no_config_document(
    tmp_path: Path, build_app
) -> None:
    """The case the endpoint exists for: no configs document, nothing bound."""
    config = tmp_path / "connections.json"  # never written — a fresh checkout
    store = RegistryStore(config, env_path=tmp_path / ".env")

    response = get_health(build_app(store))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["narrator"] is None
    assert body["unbound_slots"], "an empty registry leaves every declared slot unbound"
    assert "completion.narration" in body["unbound_slots"]
    assert body["config_path"] == str(config)
    assert body["config_exists"] is False


def test_health_is_not_shadowed_by_the_served_frontend(
    tmp_path: Path, build_app
) -> None:
    """The SPA mount at ``/`` must not swallow the probe (route order guard)."""
    from fabulist.api.app import mount_frontend

    config = write_config(
        tmp_path / "connections.json",
        {
            "connections": {
                "local": {
                    "base_url": "http://127.0.0.1:8080/v1",
                    "model": "your-model-id",
                }
            },
            "agents": {"narrator": "local"},
        },
    )
    store = RegistryStore(config, env_path=tmp_path / ".env")
    app = build_app(store)
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text(
        "<!doctype html><title>ui</title>", encoding="utf-8"
    )
    assert mount_frontend(app, build_dir=build) == build

    response = get_health(app)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_never_leaks_a_key_or_its_env_value(
    tmp_path: Path, build_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The secret the connection resolves is nowhere in the response."""
    secret = "sk-health-probe-must-never-be-serialized"
    monkeypatch.setenv("HEALTH_TEST_KEY", secret)
    config = write_config(
        tmp_path / "connections.json",
        {
            "connections": {
                "cloud": {
                    "base_url": "https://api.example.com/v1",
                    "model": "your-model-id",
                    "api_key_env": "HEALTH_TEST_KEY",
                }
            },
            "agents": {"narrator": "cloud"},
        },
    )
    store = RegistryStore(config, env_path=tmp_path / ".env")
    # The key IS loaded onto the resolved connection — the leak check is real.
    assert store.connection_for("narrator").api_key == secret

    response = get_health(build_app(store))

    assert response.json()["narrator"] == "cloud"
    assert secret not in response.text
    assert secret not in json.dumps(response.json())
