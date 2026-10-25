"""Shared fixtures for the Fabulist test suite."""

import pytest

from fabulist.log import EventLog


@pytest.fixture
def log(tmp_path):
    """A fresh file-backed EventLog (tmp SQLite, WAL) for one test."""
    repo = EventLog(tmp_path / "events.db")
    yield repo
    repo.close()
