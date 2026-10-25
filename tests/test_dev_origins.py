"""Tests for the ``FABULIST_DEV_ORIGINS`` CORS hook (D51 L3.c).

The dev-server allowlist used to hardcode :5173, so verification lanes that ran
the frontend on a spare port had to patch this module through a /tmp runpy
wrapper. ``create_app`` now also reads extra origins from
``FABULIST_DEV_ORIGINS`` (comma-separated, whitespace-trimmed, empty entries
dropped), additive to the :5173 defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from fabulist.api import create_app
from fabulist.api.app import _DEV_ORIGINS
from fabulist.api.fake import FakeTurnEngine


def _cors_origins(app: FastAPI) -> list[str]:
    """The ``allow_origins`` list of the app's CORS middleware."""
    for middleware in app.user_middleware:
        if middleware.cls.__name__ == "CORSMiddleware":
            return list(middleware.kwargs["allow_origins"])
    raise AssertionError("CORS middleware not installed")


def test_no_env_leaves_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FABULIST_DEV_ORIGINS", raising=False)
    app = create_app(FakeTurnEngine(), tmp_path / "events.db")
    try:
        assert _cors_origins(app) == list(_DEV_ORIGINS)
    finally:
        app.state.log.close()


def test_env_origins_are_appended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FABULIST_DEV_ORIGINS", "http://127.0.0.1:5202, http://127.0.0.1:5210"
    )
    app = create_app(FakeTurnEngine(), tmp_path / "events.db")
    try:
        assert _cors_origins(app) == [
            *_DEV_ORIGINS,
            "http://127.0.0.1:5202",
            "http://127.0.0.1:5210",
        ]
    finally:
        app.state.log.close()


def test_blank_env_entries_are_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FABULIST_DEV_ORIGINS", "  , http://127.0.0.1:5202 ,  ")
    app = create_app(FakeTurnEngine(), tmp_path / "events.db")
    try:
        assert _cors_origins(app) == [*_DEV_ORIGINS, "http://127.0.0.1:5202"]
    finally:
        app.state.log.close()
