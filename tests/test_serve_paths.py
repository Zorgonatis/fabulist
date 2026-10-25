"""``scripts/serve.py`` resolves its defaults against the repo root (issue #5).

Before this, every default was a CWD-relative string and ``RegistryStore``
defaulted the dotenv to ``Path.cwd()/.env``: a server started from another
directory silently ignored the repo's ``.env``, created a stray ``data/`` beside
the caller and then failed on a config it could not find. These tests pin the
fix at the ENTRY POINT — resolution is factored into ``prepare_runtime`` so no
server boot is needed.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_serve_module():
    path = REPO_ROOT / "scripts" / "serve.py"
    spec = importlib.util.spec_from_file_location("fabulist_serve_paths", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unset_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave FABULIST_ENV_FILE unset, restoring the real value on teardown."""
    monkeypatch.setenv("FABULIST_ENV_FILE", "sentinel")  # records the real state
    monkeypatch.delenv("FABULIST_ENV_FILE")


def test_defaults_resolve_from_a_foreign_cwd_without_littering_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run from a temp CWD uses the repo's files and creates nothing beside it."""
    serve = load_serve_module()
    unset_env_file(monkeypatch)
    monkeypatch.chdir(tmp_path)

    paths = serve.prepare_runtime()

    assert paths.config == REPO_ROOT / "configs" / "connections.json"
    assert paths.log == REPO_ROOT / "data" / "fabulist.db"
    assert paths.checkpoints == REPO_ROOT / "data" / "checkpoints.db"
    # The directories are created where the resolved paths live...
    assert paths.config.parent.is_dir()
    assert paths.log.parent.is_dir()
    assert paths.checkpoints.parent.is_dir()
    # ...and NOT beside the caller.
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "configs").exists()
    # The dotenv follows the repo too, so the repo's keys are not ignored.
    assert os.environ["FABULIST_ENV_FILE"] == str(REPO_ROOT / ".env")


def test_explicit_flags_win_over_the_repo_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--config``/``--log``/``--checkpoints`` are taken verbatim, per flag."""
    serve = load_serve_module()
    unset_env_file(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "cfg" / "mine.json"

    paths = serve.prepare_runtime(config=config, log="relative.db")

    assert paths.config == config
    # A relative flag keeps its CWD-relative meaning.
    assert paths.log == Path("relative.db")
    # Unflagged paths still default to the repo.
    assert paths.checkpoints == REPO_ROOT / "data" / "checkpoints.db"
    assert config.parent.is_dir()


def test_exported_env_file_is_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-pinned dotenv (a container's mounted volume) still wins."""
    serve = load_serve_module()
    target = tmp_path / "mounted" / ".env"
    monkeypatch.setenv("FABULIST_ENV_FILE", str(target))

    serve.prepare_runtime(log=tmp_path / "events.db")

    assert os.environ["FABULIST_ENV_FILE"] == str(target)
