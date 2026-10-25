"""W3a: the engine serves the built frontend at the SAME ORIGIN.

The deployable artifact is ``frontend/build`` — an ``adapter-static`` SPA (see
``frontend/vite.config.ts``) whose client router owns every extension-less
path. These tests pin the mount contract in ``fabulist.api.app``:

* the API stays ahead of the static mount (a ``Mount`` at ``"/"`` matches every
  path, so mounting it before the routers would shadow the whole API);
* no build present = API only (the ``vite dev`` workflow), logged once;
* the SPA fallback answers client routes with ``index.html`` but never masks a
  real 404 (an unknown ``/api`` route or a missing hashed asset).

The build itself is not exercised here (Node is not a test dependency); the
served directory is a tmp stand-in for ``frontend/build``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.routing import Mount

from fabulist.api import app as api_app
from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine

SHELL = '<!doctype html><html><head><title>Fabulist</title></head><body></body></html>'
ASSET = 'console.log("asset");'


def run(coro):
    return asyncio.run(coro)


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://engine"
    )


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    """A stand-in for adapter-static's output: the shell + one hashed asset."""
    build = tmp_path / "build"
    (build / "_app" / "immutable").mkdir(parents=True)
    (build / "index.html").write_text(SHELL, encoding="utf-8")
    (build / "_app" / "immutable" / "entry.js").write_text(ASSET, encoding="utf-8")
    (build / "robots.txt").write_text("# allow crawling\n", encoding="utf-8")
    return build


def frontend_app(build_dir: Path) -> FastAPI:
    """A minimal app with an API route FIRST and the static mount LAST."""
    app = FastAPI()

    @app.get("/api/ping")
    async def ping() -> dict:
        return {"ok": True}

    api_app.mount_frontend(app, build_dir)
    return app


# --- the mount itself --------------------------------------------------------------


def test_mount_frontend_serves_the_built_shell_at_root(build_dir: Path) -> None:
    async def main():
        async with client_for(frontend_app(build_dir)) as client:
            return await client.get("/")

    res = run(main())
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "<title>Fabulist</title>" in res.text


def test_mount_frontend_serves_the_builds_files(build_dir: Path) -> None:
    async def main():
        async with client_for(frontend_app(build_dir)) as client:
            return await client.get("/_app/immutable/entry.js")

    res = run(main())
    assert res.status_code == 200
    assert res.text == ASSET


def test_mount_frontend_leaves_the_api_routes_ahead_of_it(build_dir: Path) -> None:
    """The mount at ``/`` matches everything — the API must be registered first."""

    async def main():
        async with client_for(frontend_app(build_dir)) as client:
            return await client.get("/api/ping")

    res = run(main())
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_mount_frontend_absent_serves_api_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No build (the ``vite dev`` case): API only, one visible WARNING line."""
    missing = tmp_path / "no-build-here"
    monkeypatch.setattr(api_app, "_FRONTEND_BUILD", missing)
    app = FastAPI()

    @app.get("/api/ping")
    async def ping() -> dict:
        return {"ok": True}

    with caplog.at_level(logging.WARNING, logger="fabulist.api.app"):
        assert api_app.mount_frontend(app) is None
    assert "frontend build not found at frontend/build — serving API only" in caplog.text

    async def main():
        async with client_for(app) as client:
            root = await client.get("/")
            api = await client.get("/api/ping")
            return root, api

    root, api = run(main())
    assert root.status_code == 404
    assert api.json() == {"ok": True}


# --- create_app wiring -------------------------------------------------------------


def test_create_app_mounts_the_frontend_last(
    build_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``create_app`` mounts the build at the END of the route table."""
    monkeypatch.setattr(api_app, "_FRONTEND_BUILD", build_dir)
    app = create_app(FakeTurnEngine(), tmp_path / "events.db")
    try:
        # Starlette normalizes a root mount's path to "" — it is the LAST route.
        last = app.routes[-1]
        assert isinstance(last, Mount)
        assert last.name == "web"

        async def main():
            async with client_for(app) as client:
                return await client.get("/"), await client.get("/api/sessions")

        root, sessions = run(main())
        assert root.status_code == 200
        assert "<title>Fabulist</title>" in root.text
        assert sessions.status_code == 200
        assert sessions.json() == {"sessions": []}
    finally:
        app.state.log.close()


# --- the SPA fallback --------------------------------------------------------------


def test_spa_fallback_answers_a_deep_link_with_the_shell(build_dir: Path) -> None:
    """``/`` client-redirects to ``/chat``: a hard load of it must not 404."""

    async def main():
        async with client_for(frontend_app(build_dir)) as client:
            return await client.get("/chat")

    res = run(main())
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "<title>Fabulist</title>" in res.text


def test_spa_fallback_keeps_an_unknown_api_route_a_json_404(build_dir: Path) -> None:
    async def main():
        async with client_for(frontend_app(build_dir)) as client:
            return await client.get("/api/missing")

    res = run(main())
    assert res.status_code == 404
    assert res.json() == {"detail": "Not Found"}


def test_spa_fallback_keeps_a_missing_asset_a_404(build_dir: Path) -> None:
    """A suffixed path is a file, not a client route: no HTML substitution."""
    async def main():
        async with client_for(frontend_app(build_dir)) as client:
            return await client.get("/_app/immutable/nope.js")

    res = run(main())
    assert res.status_code == 404
    assert "Fabulist" not in res.text


def test_spa_fallback_leaves_a_non_get_deep_link_alone(build_dir: Path) -> None:
    async def main():
        async with client_for(frontend_app(build_dir)) as client:
            return await client.post("/chat")

    assert run(main()).status_code == 405
