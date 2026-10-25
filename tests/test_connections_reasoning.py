"""``reasoning_effort`` on the connections HTTP surface (issue #26 R5).

The registry already owns the field (parse, persist, validate) and the client
already sends it top-level, but the HTTP surface never learned it: the
``extra="forbid"`` bodies rejected a PATCH carrying it (422) and ``_redact()``
never returned it (so the drawer could not show GLM's existing ``"high"``).

Pinned here:

* create + patch ACCEPT ``reasoning_effort`` and the redacted GET returns it as
  a string or ``null`` (the value the drawer renders);
* ``null`` is a real value — it CLEARS the setting and the client then sends
  NOTHING (that is what keeps every other provider byte-identical);
* an undocumented value (``"ultra"``) is a 4xx naming the allowed vocabulary,
  validated by the registry's OWN cleaner — the enum is never copied into the
  API layer;
* a hand-configured document's value is visible through GET.

Mount pattern mirrors ``tests/test_connections_api.py``: a tmp connections.json
behind a :class:`RegistryStore` installed as ``app.state.registry``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.connections_api import router
from fabulist.api.fake import FakeTurnEngine
from fabulist.connections import build_request
from fabulist.connections.registry import REASONING_EFFORT_VALUES, RegistryStore


def run(coro):
    return asyncio.run(coro)


def _write_config(path: Path, *, reasoning_effort: str | None = None) -> Path:
    connection: dict = {
        "base_url": "http://localhost:4444/v1",
        "model": "your-model-id",
        "sampling": {"temperature": 0.85, "max_tokens": 16384},
        "timeout": 900,
        "api_key_env": "MY_TEST_KEY",
    }
    if reasoning_effort is not None:
        connection["reasoning_effort"] = reasoning_effort
    data = {
        "connections": {"your-model-id": connection},
        "agents": {"narrator": "your-model-id", "extractor": "your-model-id"},
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Factory for apps sharing one tmp config + log (mirrors the API tests)."""
    config_path = _write_config(tmp_path / "connections.json")
    log_path = tmp_path / "events.db"
    monkeypatch.setenv("MY_TEST_KEY", "super-secret-value")
    store = RegistryStore(config_path, env_path=tmp_path / ".env")
    built = []

    def _build():
        app = create_app(FakeTurnEngine(), log_path)
        app.include_router(router)
        app.state.registry = store
        built.append(app)
        return app

    yield _build, store, config_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# --- patch -> store -> GET round-trip --------------------------------------


def test_patch_sets_reasoning_effort_and_get_returns_it(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            patched = await client.patch(
                "/api/connections/your-model-id", json={"reasoning_effort": "high"}
            )
            listed = await client.get("/api/connections")
            return patched, listed

    patched, listed = run(scenario())
    assert patched.status_code == 200
    assert patched.json()["connection"]["reasoning_effort"] == "high"
    assert patched.json()["reloaded"] is True

    # The SAME store callers hold resolves the patched depth (no restart) ...
    assert store.connection_for("narrator").reasoning_effort == "high"
    # ... it is durable ...
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["connections"]["your-model-id"]["reasoning_effort"] == "high"
    # ... and the redacted GET shows it (the gap this lane closes).
    assert listed.json()["connections"]["your-model-id"]["reasoning_effort"] == "high"


def test_patch_normalizes_case_and_padding(build_app):
    """The value space is closed, so normalizing cannot change behaviour."""
    build, store, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/your-model-id", json={"reasoning_effort": " High "}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["connection"]["reasoning_effort"] == "high"
    assert store.connection_for("narrator").reasoning_effort == "high"


def test_patch_null_clears_and_the_client_then_sends_nothing(build_app):
    build, store, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.patch(
                "/api/connections/your-model-id", json={"reasoning_effort": "high"}
            )
            cleared = await client.patch(
                "/api/connections/your-model-id", json={"reasoning_effort": None}
            )
            listed = await client.get("/api/connections")
            return cleared, listed

    cleared, listed = run(scenario())
    assert cleared.status_code == 200
    assert cleared.json()["connection"]["reasoning_effort"] is None
    assert store.connection_for("narrator").reasoning_effort is None
    assert listed.json()["connections"]["your-model-id"]["reasoning_effort"] is None
    # Null means "send nothing": no key in the outbound request body at all.
    kwargs = build_request(
        "narrator", [{"role": "user", "content": "hi"}], registry=store.registry
    )
    assert "reasoning_effort" not in kwargs


# --- create ----------------------------------------------------------------


def test_post_creates_with_reasoning_effort(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/connections/glm",
                json={
                    "base_url": "http://localhost:4444/v1",
                    "model": "glm-5.3",
                    "reasoning_effort": "low",
                },
            )
            listed = await client.get("/api/connections")
            return created, listed

    created, listed = run(scenario())
    assert created.status_code == 201
    assert created.json()["connection"]["reasoning_effort"] == "low"
    assert store.connections["glm"].reasoning_effort == "low"
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["connections"]["glm"]["reasoning_effort"] == "low"
    assert listed.json()["connections"]["glm"]["reasoning_effort"] == "low"


def test_post_without_reasoning_effort_reports_null(build_app):
    """Absent on create = not set; GET serializes it as ``null``, not a fake default."""
    build, _, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.post(
                "/api/connections/plain",
                json={"base_url": "http://localhost:4444/v1", "model": "m"},
            )
            return await client.get("/api/connections")

    conn = run(scenario()).json()["connections"]["plain"]
    assert "reasoning_effort" in conn
    assert conn["reasoning_effort"] is None


# --- invalid values (the registry's vocabulary, not a copied enum) ---------


@pytest.mark.parametrize("value", ["ultra", "", "HIGHEST"])
def test_patch_rejects_an_undocumented_reasoning_effort(build_app, value):
    build, store, config_path = build_app
    app = build()
    before = config_path.read_text(encoding="utf-8")

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/your-model-id", json={"reasoning_effort": value}
            )

    resp = run(scenario())
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "allowed:" in detail
    for allowed in sorted(REASONING_EFFORT_VALUES):
        assert allowed in detail
    # A rejected patch leaves the file and the live registry untouched.
    assert config_path.read_text(encoding="utf-8") == before
    assert store.connection_for("narrator").reasoning_effort is None


def test_post_rejects_an_undocumented_reasoning_effort_and_creates_nothing(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/typo",
                json={
                    "base_url": "http://localhost:4444/v1",
                    "model": "m",
                    "reasoning_effort": "ultra",
                },
            )

    resp = run(scenario())
    assert resp.status_code == 400
    assert "ultra" in resp.json()["detail"]
    assert "allowed:" in resp.json()["detail"]
    assert "typo" not in store.connections
    assert "typo" not in json.loads(config_path.read_text(encoding="utf-8"))["connections"]


# --- the field the user hand-configured in the document --------------------


def test_get_shows_a_hand_configured_reasoning_effort(build_app):
    """The drawer must see the value already in ``connections.json`` (GLM's
    existing ``"high"``) without any write through this API."""
    build, store, config_path = build_app
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["connections"]["your-model-id"]["reasoning_effort"] = "max"
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    store.reload()
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections")

    conn = run(scenario()).json()["connections"]["your-model-id"]
    assert conn["reasoning_effort"] == "max"
