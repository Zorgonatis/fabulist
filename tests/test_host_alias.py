"""Issue #33 — loopback host alias and the connectivity doctor.

The gap: the Docker deployment cannot reach a model server running on the host.
``configs/connections.json`` points local connections at ``127.0.0.1``, which
inside a container is the container itself, so every local call failed with
``ConnectError`` while narration (a remote endpoint) kept working — an
invisible, silent failure.

Two seams are pinned here:

* ``FABULIST_HOST_ALIAS`` (read at call time) rewrites ONLY a loopback host
  (``127.0.0.1`` / ``localhost`` / ``::1``) of an http(s) URL, preserving
  scheme, port, path and query byte-for-byte. Unset/empty = no rewrite at all,
  so the bare image keeps raw behaviour; remote/LAN/service-name hosts are
  never touched; the rewrite is idempotent.
* ``GET /api/connections/doctor`` performs the probe ``/health`` must NOT do:
  one short-timeout, concurrent request per connection, reporting the resolved
  URL, reachability, latency and an actionable failure message (including the
  loopback provenance when the alias rewrote it) — never a key.

No real network: the doctor is exercised through the same
``_http_client_factory`` / ``httpx.MockTransport`` seam the model-discovery
tests use.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api import connections_api
from fabulist.api.connections_api import DOCTOR_TIMEOUT_SECONDS, router
from fabulist.api.fake import FakeTurnEngine
from fabulist.connections.registry import (
    Registry,
    RegistryStore,
    _normalize_base_url,
    configuration_report,
)

ALIAS = "host.docker.internal"


# --- the rewrite rule ------------------------------------------------------


def _connection(url: str):
    registry = Registry.from_dict(
        {"connections": {"c": {"base_url": url, "model": "m"}}, "agents": {}}
    )
    return registry.connections["c"]


@pytest.mark.parametrize(
    "written,expected",
    [
        # IPv4 loopback, port and path preserved exactly.
        ("http://127.0.0.1:4445/v1", "http://host.docker.internal:4445/v1"),
        # hostname loopback, trailing slash stripped by the existing normalizer.
        ("http://localhost:4444/v1/", "http://host.docker.internal:4444/v1"),
        # IPv6 loopback: brackets belong to the URL, not the host.
        ("http://[::1]:4445/api/v1", "http://host.docker.internal:4445/api/v1"),
        # Query string survives the rewrite byte-for-byte.
        (
            "http://127.0.0.1:4445/api/paas/v4?trace=1",
            "http://host.docker.internal:4445/api/paas/v4?trace=1",
        ),
        # scheme is preserved (https stays https).
        ("https://127.0.0.1:8443/v1", "https://host.docker.internal:8443/v1"),
        # The bare host:port form the drawer hint teaches.
        ("127.0.0.1:4445", "http://host.docker.internal:4445/v1"),
        # No port, no path: the existing /v1 rule still applies after the swap.
        ("http://localhost", "http://host.docker.internal/v1"),
    ],
)
def test_loopback_host_is_rewritten(monkeypatch, written, expected):
    monkeypatch.setenv("FABULIST_HOST_ALIAS", ALIAS)
    conn = _connection(written)
    assert conn.base_url == expected
    assert conn.base_url_rewritten is True


def test_alias_unset_leaves_the_url_byte_identical(monkeypatch):
    """No env var = NO magic: the image without the compose env is unchanged."""
    monkeypatch.delenv("FABULIST_HOST_ALIAS", raising=False)
    conn = _connection("http://127.0.0.1:4445/v1")
    assert conn.base_url == "http://127.0.0.1:4445/v1"
    assert conn.base_url_rewritten is False


def test_empty_alias_leaves_the_url_byte_identical(monkeypatch):
    """An empty value disables the rewrite exactly like unset does."""
    monkeypatch.setenv("FABULIST_HOST_ALIAS", "")
    conn = _connection("http://localhost:4444/v1")
    assert conn.base_url == "http://localhost:4444/v1"
    assert conn.base_url_rewritten is False


@pytest.mark.parametrize(
    "written,expected",
    [
        # A LAN server must be left alone.
        ("http://192.168.0.50:8080/v1", "http://192.168.0.50:8080/v1"),
        # A remote provider must be left alone (explicit path semantics too).
        ("https://api.z.ai/api/paas/v4", "https://api.z.ai/api/paas/v4"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        # A compose service name is not loopback.
        ("http://fabulist-models:8080/v1", "http://fabulist-models:8080/v1"),
        # Only http/https URLs are rewrite candidates.
        ("ftp://127.0.0.1:21/v1", "ftp://127.0.0.1:21/v1"),
    ],
)
def test_non_loopback_and_non_http_urls_are_never_rewritten(
    monkeypatch, written, expected
):
    monkeypatch.setenv("FABULIST_HOST_ALIAS", ALIAS)
    conn = _connection(written)
    assert conn.base_url == expected
    assert conn.base_url_rewritten is False


def test_rewrite_is_idempotent_when_the_config_already_names_the_alias(monkeypatch):
    """A config that already says ``host.docker.internal`` is not double-applied."""
    monkeypatch.setenv("FABULIST_HOST_ALIAS", ALIAS)
    conn = _connection(f"http://{ALIAS}:4445/v1")
    assert conn.base_url == f"http://{ALIAS}:4445/v1"
    assert conn.base_url_rewritten is False

    # And normalizing the RESOLVED url again is a no-op (the loopback is gone).
    again = _normalize_base_url(conn.base_url, "connections['c']")
    assert again == conn.base_url


# --- the report ------------------------------------------------------------


def test_configuration_report_names_the_alias_and_the_resolved_url(
    tmp_path: Path, monkeypatch
):
    """``GET /health`` says which URL a container will dial, and never a key."""
    monkeypatch.setenv("FABULIST_HOST_ALIAS", ALIAS)
    monkeypatch.setenv("MY_TEST_KEY", "super-secret-value")
    config = tmp_path / "connections.json"
    config.write_text(
        json.dumps(
            {
                "connections": {
                    "local": {
                        "base_url": "http://127.0.0.1:4445/v1",
                        "model": "m",
                        "api_key_env": "MY_TEST_KEY",
                    },
                    "cloud": {
                        "base_url": "https://api.z.ai/api/paas/v4",
                        "model": "glm",
                        "api_key_env": "MY_TEST_KEY",
                    },
                },
                "agents": {"narrator": "local"},
            }
        )
    )
    store = RegistryStore(config, env_path=tmp_path / ".env")

    report = configuration_report(store)

    assert report["host_alias"] == ALIAS
    assert report["connections"]["local"] == {
        "base_url": f"http://{ALIAS}:4445/v1",
        "base_url_rewritten": True,
    }
    assert report["connections"]["cloud"] == {
        "base_url": "https://api.z.ai/api/paas/v4",
        "base_url_rewritten": False,
    }
    serialized = json.dumps(report)
    assert "super-secret-value" not in serialized
    assert "MY_TEST_KEY" not in serialized  # the env NAME is not a health field


def test_configuration_report_reports_no_alias_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("FABULIST_HOST_ALIAS", raising=False)
    config = tmp_path / "connections.json"
    config.write_text(
        json.dumps(
            {
                "connections": {"local": {"base_url": "http://127.0.0.1:4445/v1", "model": "m"}},
                "agents": {},
            }
        )
    )
    report = configuration_report(RegistryStore(config, env_path=tmp_path / ".env"))
    assert report["host_alias"] is None
    assert report["connections"]["local"] == {
        "base_url": "http://127.0.0.1:4445/v1",
        "base_url_rewritten": False,
    }


# --- the doctor ------------------------------------------------------------


def _write_doctor_config(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "connections": {
                    # Reachable: rewritten from loopback through the alias.
                    "live": {
                        "base_url": "http://127.0.0.1:4445/v1",
                        "model": "m",
                        "api_key_env": "MY_TEST_KEY",
                    },
                    # Unreachable: also rewritten; the error must say so.
                    "dead": {
                        "base_url": "http://localhost:4444/v1",
                        "model": "m",
                    },
                },
                "agents": {"narrator": "live"},
            }
        )
        + "\n"
    )
    return path


@pytest.fixture
def doctor_app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FABULIST_HOST_ALIAS", ALIAS)
    monkeypatch.setenv("MY_TEST_KEY", "super-secret-value")
    config = _write_doctor_config(tmp_path / "connections.json")
    store = RegistryStore(config, env_path=tmp_path / ".env")
    app = create_app(FakeTurnEngine(), tmp_path / "events.db")
    app.include_router(router)
    app.state.registry = store
    yield app
    app.state.log.close()


@pytest.fixture
def mock_http(monkeypatch):
    """Install a MockTransport-backed client factory and record its timeouts."""

    def install(handler):
        seen: dict = {}

        def factory(*args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            kwargs["transport"] = httpx.MockTransport(handler)
            return httpx.AsyncClient(*args, **kwargs)

        monkeypatch.setattr(connections_api, "_http_client_factory", factory)
        return seen

    return install


def _get_doctor(app) -> httpx.Response:
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.get("/api/connections/doctor")

    # A hard ceiling: the doctor must never hang a request.
    return asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_doctor_reports_reachable_and_unreachable_connections(doctor_app, mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 4445:
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        raise httpx.ConnectError("All connection attempts failed", request=request)

    seen = mock_http(handler)

    response = _get_doctor(doctor_app)

    assert response.status_code == 200
    body = response.json()
    assert body["host_alias"] == ALIAS
    assert body["count"] == 2
    assert body["ok"] is False  # AND over the rows

    rows = {row["name"]: row for row in body["connections"]}
    live = rows["live"]
    assert live["ok"] is True
    assert live["error"] is None
    assert live["status"] == 200
    assert live["base_url"] == f"http://{ALIAS}:4445/v1"
    assert live["latency_ms"] >= 0

    dead = rows["dead"]
    assert dead["ok"] is False
    assert dead["base_url"] == f"http://{ALIAS}:4444/v1"
    assert dead["error"]["class"] == "ConnectError"
    reason = dead["error"]["reason"]
    # Actionable: names the RESOLVED url and the loopback provenance.
    assert f"http://{ALIAS}:4444/v1" in reason
    assert "loopback" in reason
    assert "FABULIST_HOST_ALIAS" in reason
    assert ALIAS in reason

    # A short timeout independent of the generation timeout, so no hanging.
    assert seen["timeout"] == DOCTOR_TIMEOUT_SECONDS
    assert DOCTOR_TIMEOUT_SECONDS <= 5.0
    # Never leak the key (header only, scrubbed out of messages).
    assert "super-secret-value" not in response.text


def test_doctor_counts_an_http_error_as_a_failed_probe(doctor_app, mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "no key"})

    mock_http(handler)

    response = _get_doctor(doctor_app)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    for row in body["connections"]:
        assert row["ok"] is False
        assert row["error"]["class"] == "HTTPStatus"
        assert row["error"]["reason"].startswith("upstream returned 401 from ")


def test_doctor_is_503_when_the_store_is_unwired(tmp_path):
    app = create_app(FakeTurnEngine(), tmp_path / "events.db")
    app.include_router(router)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.get("/api/connections/doctor")

    try:
        response = asyncio.run(scenario())
        assert response.status_code == 503
    finally:
        app.state.log.close()
