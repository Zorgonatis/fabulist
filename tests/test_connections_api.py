"""Tests for the connections read/patch router + hot re-configure (D25 C1).

Mount pattern mirrors the other API tests: build the app over a tmp event-log
path via :func:`create_app`, include this lane's router, install a
:class:`RegistryStore` (pointing at a tmp connections.json) as
``app.state.registry`` — exactly how the hot-reload wiring installs it. The key
claim under test is that patching the store changes what ``connection_for``
returns for the SAME object callers already hold (no restart).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.connections_api import TRANSIENT_KEY_HEADER, router
from fabulist.api.fake import FakeTurnEngine
from fabulist.connections.registry import ConnectionConfigError, RegistryStore


def run(coro):
    return asyncio.run(coro)


def _write_config(path: Path, *, with_key: bool = True) -> Path:
    connection: dict = {
        "base_url": "http://localhost:4444/v1",
        "model": "your-model-id",
        "sampling": {"temperature": 0.85, "max_tokens": 16384},
        "timeout": 900,
        "note": "user keeps this",  # unknown-to-parser key must survive a write
    }
    if with_key:
        connection["api_key_env"] = "MY_TEST_KEY"
    data = {
        "connections": {"your-model-id": connection},
        "agents": {"narrator": "your-model-id", "extractor": "your-model-id"},
        "custom_top_level": {"keep": True},  # unknown top-level key must survive
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Factory for apps sharing one tmp config + log; installs the store.

    The store gets a SCRATCH env file (``tmp_path/.env``) so these tests never
    read the worktree's own ``.env`` — and so the key-paste tests can assert the
    exact lines written.
    """
    config_path = _write_config(tmp_path / "connections.json")
    log_path = tmp_path / "events.db"
    env_path = tmp_path / ".env"
    monkeypatch.setenv("MY_TEST_KEY", "super-secret-value")
    store = RegistryStore(config_path, env_path=env_path)
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


@pytest.fixture
def build_app_with_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Same as :func:`build_app`, but also hands back the scratch env file."""
    config_path = _write_config(tmp_path / "connections.json")
    log_path = tmp_path / "events.db"
    env_path = tmp_path / ".env"
    monkeypatch.setenv("MY_TEST_KEY", "super-secret-value")
    store = RegistryStore(config_path, env_path=env_path)
    app = create_app(FakeTurnEngine(), log_path)
    app.include_router(router)
    app.state.registry = store
    yield app, store, config_path, env_path
    app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# --- read / redaction ------------------------------------------------------


def test_get_connections_redacts_keys(build_app):
    build, _, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections")

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    conn = body["connections"]["your-model-id"]
    assert conn["api_key_env"] == "MY_TEST_KEY"
    assert conn["api_key_set"] is True
    assert conn["model"] == "your-model-id"
    assert conn["timeout"] == 900.0
    assert body["agents"] == {"narrator": "your-model-id", "extractor": "your-model-id"}
    # the secret VALUE never appears anywhere in the payload
    assert "super-secret-value" not in resp.text


def test_get_connections_api_key_set_false_when_env_absent(build_app, monkeypatch):
    build, _, _ = build_app
    app = build()
    monkeypatch.delenv("MY_TEST_KEY", raising=False)
    # A fresh store reads api_key at parse time; reload picks up the env change.
    app.state.registry.reload()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections")

    conn = run(scenario()).json()["connections"]["your-model-id"]
    assert conn["api_key_env"] == "MY_TEST_KEY"
    assert conn["api_key_set"] is False


# --- patch + hot reload ----------------------------------------------------


def test_patch_updates_store_persists_and_preserves_unknown_keys(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/your-model-id",
                json={"model": "new-model", "sampling": {"temperature": 0.2}},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["connection"]["model"] == "new-model"
    assert resp.json()["reloaded"] is True

    # The SAME store object callers hold now resolves the patched values.
    assert store.connection_for("narrator").model == "new-model"
    assert store.connection_for("narrator").sampling == {"temperature": 0.2}

    # Persisted to disk, unknown keys (connection + top-level) preserved.
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["connections"]["your-model-id"]["model"] == "new-model"
    assert written["connections"]["your-model-id"]["note"] == "user keeps this"
    assert written["connections"]["your-model-id"]["base_url"] == "http://localhost:4444/v1"
    assert written["custom_top_level"] == {"keep": True}

    # And a fresh read agrees.
    async def read_back():
        async with client_for(app) as client:
            return await client.get("/api/connections")

    assert run(read_back()).json()["connections"]["your-model-id"]["model"] == "new-model"


def test_patch_base_url_is_rejected(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/your-model-id", json={"base_url": "http://elsewhere/v1"}
            )

    resp = run(scenario())
    assert resp.status_code == 422
    # untouched on disk
    assert (
        json.loads(config_path.read_text())["connections"]["your-model-id"]["base_url"]
        == "http://localhost:4444/v1"
    )
    assert store.connection_for("narrator").base_url == "http://localhost:4444/v1"


def test_patch_unknown_connection_is_404(build_app):
    build, _, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/ghost", json={"model": "x"}
            )

    assert run(scenario()).status_code == 404


def test_patch_empty_body_is_422(build_app):
    build, _, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch("/api/connections/your-model-id", json={})

    assert run(scenario()).status_code == 422


def test_patch_replaces_objects_and_clears_timeout(build_app):
    """Object fields replace wholesale; an explicit null timeout clears it."""
    build, store, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.patch(
                "/api/connections/your-model-id",
                json={"extra_body": {"thinking": {"type": "enabled"}}},
            )
            return await client.patch(
                "/api/connections/your-model-id", json={"timeout": None}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["connection"]["timeout"] is None
    conn = store.connection_for("narrator")
    assert conn.extra_body == {"thinking": {"type": "enabled"}}
    assert conn.timeout is None


# --- create ----------------------------------------------------------------


def test_post_creates_new_connection(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/fast",
                json={"base_url": "http://localhost:4444/v1", "model": "tiny"},
            )

    resp = run(scenario())
    assert resp.status_code == 201
    assert resp.json()["connection"]["model"] == "tiny"
    assert "fast" in store.connections
    written = json.loads(config_path.read_text())
    assert written["connections"]["fast"]["model"] == "tiny"


def test_post_duplicate_is_409(build_app):
    build, _, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/your-model-id",
                json={"base_url": "http://x/v1", "model": "y"},
            )

    assert run(scenario()).status_code == 409


def test_store_unavailable_is_503(tmp_path):
    app = create_app(FakeTurnEngine(), tmp_path / "e.db")
    app.include_router(router)  # no store installed on app.state

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections")

    assert run(scenario()).status_code == 503
    app.state.log.close()


# --- RegistryStore unit ----------------------------------------------------


def test_store_update_validates_before_write(tmp_path):
    path = _write_config(tmp_path / "connections.json")
    before = path.read_text(encoding="utf-8")
    store = RegistryStore(path)
    with pytest.raises(ConnectionConfigError):
        store.update_connection("your-model-id", {"base_url": "http://x"})
    assert path.read_text(encoding="utf-8") == before
    # normalized patch round-trips
    store.update_connection("your-model-id", {"timeout": 42, "model": "m2"})
    assert store.connection_for("narrator").timeout == 42.0
    assert store.connection_for("narrator").model == "m2"


# --- note passthrough (user state the registry parser ignores) --------------


def test_get_connections_surfaces_note(build_app):
    build, store, config_path = build_app
    # Seed a note directly (the parser ignores it, the document keeps it).
    data = json.loads(config_path.read_text())
    data["connections"]["your-model-id"]["note"] = "thinking depth matters here"
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    store.reload()
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections")

    conn = run(scenario()).json()["connections"]["your-model-id"]
    assert conn["note"] == "thinking depth matters here"


def test_patch_sets_and_clears_note_without_touching_registry_fields(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.patch("/api/connections/your-model-id", json={"note": "temp 0 on the 4445 router"})
            return await client.patch("/api/connections/your-model-id", json={"note": ""})

    resp = run(scenario())
    assert resp.status_code == 200
    assert "note" not in resp.json()["connection"]
    written = json.loads(config_path.read_text())
    assert "note" not in written["connections"]["your-model-id"]
    # Note-only patch never mangled the registry-visible fields.
    assert store.connection_for("narrator").model == "your-model-id"


# --- create with extra_body + note -----------------------------------------


def test_post_accepts_extra_body_and_note(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/llama-only",
                json={
                    "base_url": "http://127.0.0.1:4445/v1",
                    "model": "lfm25-350m",
                    "extra_body": {"top_k": 50, "repeat_penalty": 1.05},
                    "sampling": {"temperature": 0.1, "max_tokens": 16384},
                    "note": "vendor sampler: top-k is llama.cpp-only",
                },
            )

    resp = run(scenario())
    assert resp.status_code == 201
    conn = resp.json()["connection"]
    assert conn["extra_body"] == {"top_k": 50, "repeat_penalty": 1.05}
    assert conn["note"] == "vendor sampler: top-k is llama.cpp-only"
    # Passthrough reaches the registry's typed connection (the whole point of
    # extra_body) and the non-standard top_k key survives the round trip.
    assert store.connections["llama-only"].extra_body == {
        "top_k": 50,
        "repeat_penalty": 1.05,
    }
    # Persisted verbatim, including the note and the non-standard top_k key.
    written = json.loads(config_path.read_text())
    entry = written["connections"]["llama-only"]
    assert entry["extra_body"] == {"top_k": 50, "repeat_penalty": 1.05}
    assert entry["note"].startswith("vendor sampler")


def test_post_with_api_key_stores_it_and_never_echoes_it(
    build_app_with_env, scratch_env
):
    """A create carries the literal key: the OWNER pastes a KEY on the form, so
    the server writes ``.env`` (mode 600) and stores the derived NAME — the value
    never reaches the document, the response, or a second request."""
    app, store, config_path, env_path = build_app_with_env
    scratch_env("FABULIST_FRESH_ONE_KEY")

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/connections/fresh-one",
                json={
                    "base_url": "http://x/v1",
                    "model": "m",
                    "api_key": "sk-created-secret",
                },
            )
            listed = await client.get("/api/connections")
            return created, listed

    created, listed = run(scenario())
    assert created.status_code == 201
    entry = created.json()["connection"]
    assert entry["api_key_env"] == "FABULIST_FRESH_ONE_KEY"
    assert entry["api_key_set"] is True
    assert "sk-created-secret" not in created.text
    assert "sk-created-secret" not in listed.text

    # The key is live for the store's callers, and only the NAME is in the doc.
    assert store.connections["fresh-one"].api_key == "sk-created-secret"
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert document["connections"]["fresh-one"]["api_key_env"] == "FABULIST_FRESH_ONE_KEY"
    assert "sk-created-secret" not in config_path.read_text(encoding="utf-8")

    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "FABULIST_FRESH_ONE_KEY=sk-created-secret"
    ]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_post_with_api_key_wins_over_a_supplied_env_name(
    build_app_with_env, scratch_env
):
    app, store, _, env_path = build_app_with_env
    scratch_env("FABULIST_FRESH_TWO_KEY")

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/fresh-two",
                json={
                    "base_url": "http://x/v1",
                    "model": "m",
                    "api_key_env": "SOME_OTHER_VAR",
                    "api_key": "sk-wins",
                },
            )

    resp = run(scenario())
    assert resp.status_code == 201
    assert resp.json()["connection"]["api_key_env"] == "FABULIST_FRESH_TWO_KEY"
    assert store.connections["fresh-two"].api_key == "sk-wins"
    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "FABULIST_FRESH_TWO_KEY=sk-wins"
    ]


def test_post_keyless_create_never_touches_the_env_file(
    build_app_with_env, scratch_env
):
    """Env-var users stay exactly as before: ``api_key_env`` names a variable and
    the create writes no env file."""
    app, store, _, env_path = build_app_with_env

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/env-user",
                json={
                    "base_url": "http://x/v1",
                    "model": "m",
                    "api_key_env": "MY_TEST_KEY",
                },
            )

    resp = run(scenario())
    assert resp.status_code == 201
    assert resp.json()["connection"]["api_key_env"] == "MY_TEST_KEY"
    assert resp.json()["connection"]["api_key_set"] is True  # resolves MY_TEST_KEY
    assert not env_path.exists()


def test_post_rejects_an_unusable_api_key_without_echoing(build_app_with_env):
    app, store, config_path, env_path = build_app_with_env

    async def scenario():
        async with client_for(app) as client:
            blank = await client.post(
                "/api/connections/blank-key",
                json={"base_url": "http://x/v1", "model": "m", "api_key": "   "},
            )
            multiline = await client.post(
                "/api/connections/multiline-key",
                json={
                    "base_url": "http://x/v1",
                    "model": "m",
                    "api_key": "sk-a\nFABULIST_X=sk-b",
                },
            )
            return blank, multiline

    blank, multiline = run(scenario())
    assert blank.status_code == 422
    assert multiline.status_code == 422
    # The rejection names the field, never the pasted value, and nothing landed.
    assert "sk-a" not in multiline.text and "sk-b" not in multiline.text
    assert "blank-key" not in store.connections
    assert "multiline-key" not in store.connections
    assert "sk-a" not in config_path.read_text(encoding="utf-8")
    assert not env_path.exists()


def test_patch_api_key_overrides_api_key_env(build_app_with_env, scratch_env):
    """The literal wins over a supplied name: the key is stored under the derived
    name and ``api_key_env`` is repointed at it."""
    app, store, config_path, env_path = build_app_with_env
    scratch_env("FABULIST_YOUR_MODEL_ID_KEY")

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/your-model-id",
                json={"api_key_env": "MY_TEST_KEY", "api_key": "sk-patched-key"},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    entry = resp.json()["connection"]
    assert entry["api_key_env"] == "FABULIST_YOUR_MODEL_ID_KEY"
    assert entry["api_key_set"] is True
    assert "sk-patched-key" not in resp.text

    assert store.connections["your-model-id"].api_key == "sk-patched-key"
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert document["connections"]["your-model-id"]["api_key_env"] == "FABULIST_YOUR_MODEL_ID_KEY"
    assert "sk-patched-key" not in config_path.read_text(encoding="utf-8")
    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "FABULIST_YOUR_MODEL_ID_KEY=sk-patched-key"
    ]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_patch_api_key_alone_is_valid(build_app_with_env, scratch_env):
    """A key-only patch must not be a ``422 no patchable fields``: re-keying a
    saved connection is the edit the owner most often makes."""
    app, store, _, _ = build_app_with_env
    scratch_env("FABULIST_YOUR_MODEL_ID_KEY")

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/your-model-id", json={"api_key": "sk-only"}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["connection"]["api_key_set"] is True
    assert store.connections["your-model-id"].api_key == "sk-only"


def test_patch_api_key_on_unknown_connection_is_404_and_writes_nothing(
    build_app_with_env, scratch_env
):
    app, _, _, env_path = build_app_with_env

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/ghost", json={"api_key": "sk-nowhere"}
            )

    assert run(scenario()).status_code == 404
    assert not env_path.exists()


def test_patch_without_a_key_keeps_the_stored_env_name(
    build_app_with_env, scratch_env
):
    """Typing nothing in the key field must never clear an existing name."""
    app, store, _, env_path = build_app_with_env

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/your-model-id", json={"model": "your-model-id-next"}
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["connection"]["api_key_env"] == "MY_TEST_KEY"
    assert store.connections["your-model-id"].api_key_env == "MY_TEST_KEY"
    assert not env_path.exists()


# --- pasted key -> project .env (supersedes D25's literal-key refusal) ----


@pytest.fixture
def scratch_env(monkeypatch: pytest.MonkeyPatch):
    """Undo the endpoint's ``os.environ`` overlay at teardown.

    ``RegistryStore.reload`` writes parsed env-file values into the REAL process
    environment, so a test that pastes a key registers the derived names here
    (pytest's monkeypatch then restores the pre-test state).
    """

    def _register(*names: str) -> None:
        for name in names:
            monkeypatch.setitem(os.environ, name, "")

    return _register


def test_post_key_writes_env_line_and_marks_connection_keyed(
    build_app_with_env, scratch_env
):
    app, store, config_path, env_path = build_app_with_env
    scratch_env("FABULIST_YOUR_MODEL_ID_KEY")

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                "/api/connections/your-model-id/key", json={"key": "sk-pasted-123"}
            )
            listed = await client.get("/api/connections")
            return posted, listed

    posted, listed = run(scenario())
    assert posted.status_code == 200
    assert posted.json() == {
        "api_key_env": "FABULIST_YOUR_MODEL_ID_KEY",
        "api_key_set": True,
    }
    assert "sk-pasted-123" not in posted.text

    # Live for the SAME store object callers already hold — no restart.
    conn = store.connections["your-model-id"]
    assert conn.api_key_env == "FABULIST_YOUR_MODEL_ID_KEY"
    assert conn.api_key == "sk-pasted-123"

    # Exactly one line in the env file, private mode.
    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "FABULIST_YOUR_MODEL_ID_KEY=sk-pasted-123"
    ]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    # The document carries the NAME only.
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert document["connections"]["your-model-id"]["api_key_env"] == "FABULIST_YOUR_MODEL_ID_KEY"
    assert "sk-pasted-123" not in config_path.read_text(encoding="utf-8")

    # GET reports the key as set and never echoes it.
    listed_conn = listed.json()["connections"]["your-model-id"]
    assert listed_conn["api_key_env"] == "FABULIST_YOUR_MODEL_ID_KEY"
    assert listed_conn["api_key_set"] is True
    assert "sk-pasted-123" not in listed.text


def test_post_key_twice_updates_the_same_env_line(build_app_with_env, scratch_env):
    app, store, _, env_path = build_app_with_env
    scratch_env("FABULIST_YOUR_MODEL_ID_KEY")

    async def scenario():
        async with client_for(app) as client:
            first = await client.post(
                "/api/connections/your-model-id/key", json={"key": "sk-first"}
            )
            second = await client.post(
                "/api/connections/your-model-id/key", json={"key": "sk-second"}
            )
            return first, second

    first, second = run(scenario())
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["api_key_set"] is True

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["FABULIST_YOUR_MODEL_ID_KEY=sk-second"]  # replaced, not appended
    assert "sk-first" not in env_path.read_text(encoding="utf-8")
    # The reload picked the SECOND key up in-process.
    assert store.connections["your-model-id"].api_key == "sk-second"


def test_post_key_preserves_unrelated_env_lines(build_app_with_env, scratch_env):
    app, store, _, env_path = build_app_with_env
    scratch_env("FABULIST_YOUR_MODEL_ID_KEY", "OTHER_SECRET")
    env_path.write_text(
        "# user secrets\n"
        "OTHER_SECRET=keep-me\n"
        "FABULIST_YOUR_MODEL_ID_KEY=stale-value\n",
        encoding="utf-8",
    )
    store.reload()  # pick up OTHER_SECRET into the process env

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/your-model-id/key", json={"key": "sk-fresh"}
            )

    assert run(scenario()).status_code == 200
    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "# user secrets",
        "OTHER_SECRET=keep-me",
        "FABULIST_YOUR_MODEL_ID_KEY=sk-fresh",
    ]
    assert os.environ["OTHER_SECRET"] == "keep-me"
    assert store.connections["your-model-id"].api_key == "sk-fresh"


def test_post_key_round_trips_a_value_needing_quotes(build_app_with_env, scratch_env):
    app, store, _, env_path = build_app_with_env
    scratch_env("FABULIST_YOUR_MODEL_ID_KEY")

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/your-model-id/key", json={"key": 'sk "quoted" value'}
            )

    assert run(scenario()).status_code == 200
    assert env_path.read_text(encoding="utf-8").splitlines() == [
        'FABULIST_YOUR_MODEL_ID_KEY="sk \\"quoted\\" value"'
    ]
    assert store.connections["your-model-id"].api_key == 'sk "quoted" value'


def test_post_key_unknown_connection_is_404_and_writes_nothing(
    build_app_with_env, scratch_env
):
    app, _, _, env_path = build_app_with_env
    scratch_env("FABULIST_GHOST_KEY")

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/ghost/key", json={"key": "sk-ghost"}
            )

    assert run(scenario()).status_code == 404
    assert not env_path.exists()


def test_post_key_rejects_blank_and_multiline_without_echoing(
    build_app_with_env, scratch_env
):
    app, _, _, env_path = build_app_with_env
    scratch_env("FABULIST_YOUR_MODEL_ID_KEY")

    async def scenario():
        async with client_for(app) as client:
            blank = await client.post(
                "/api/connections/your-model-id/key", json={"key": "   "}
            )
            multiline = await client.post(
                "/api/connections/your-model-id/key", json={"key": "sk-a\nFABULIST_X=sk-b"}
            )
            return blank, multiline

    blank, multiline = run(scenario())
    assert blank.status_code == 422
    assert multiline.status_code == 422
    # The rejection names the field, never the pasted value.
    assert "sk-a" not in multiline.text
    assert "sk-b" not in multiline.text
    assert not env_path.exists()


# --- delete -----------------------------------------------------------------


def test_delete_unbound_connection_removes_entry(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.post(
                "/api/connections/spare",
                json={"base_url": "http://x/v1", "model": "m"},
            )
            return await client.delete("/api/connections/spare")

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "spare", "reloaded": True}
    assert "spare" not in store.connections
    assert "spare" not in json.loads(config_path.read_text())["connections"]
    # Unknown connections still resolve normally after the reload.
    assert store.connection_for("narrator").model == "your-model-id"


def test_delete_unknown_is_404(build_app):
    build, _, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.delete("/api/connections/ghost")

    assert run(scenario()).status_code == 404


def test_delete_agent_bound_is_409_and_leaves_file_untouched(build_app):
    build, store, config_path = build_app
    before = config_path.read_text()
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.delete("/api/connections/your-model-id")

    resp = run(scenario())
    assert resp.status_code == 409
    assert "your-model-id" in resp.json()["detail"]
    assert config_path.read_text() == before  # refused before any write
    assert "your-model-id" in store.connections


# --- model discovery proxy --------------------------------------------------


@pytest.fixture
def mock_http(monkeypatch):
    """Install a MockTransport-backed ``httpx.AsyncClient`` factory seam."""
    from fabulist.api import connections_api

    def install(handler):
        transport = httpx.MockTransport(handler)

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return httpx.AsyncClient(*args, **kwargs)

        monkeypatch.setattr(connections_api, "_http_client_factory", factory)

    return install


def test_discover_models_proxies_url_and_sends_env_key(build_app, mock_http):
    build, _, _ = build_app
    app = build()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "m1"}, {"id": "m2"}, {"id": "m1"}]},
        )

    mock_http(handler)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections/your-model-id/models")

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["models"] == ["m1", "m2"]  # first-occurrence dedupe
    assert body["count"] == 2
    assert body["url"] == "http://localhost:4444/v1/models"
    assert body["api_key_set"] is True
    assert seen["url"] == "http://localhost:4444/v1/models"
    # The env-resolved key is used server-side and never serialized back.
    assert seen["auth"] == "Bearer super-secret-value"
    assert "super-secret-value" not in resp.text


def test_discover_models_keyless_sends_no_auth(build_app, mock_http):
    build, _, _ = build_app
    app = build()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"models": [{"id": "tiny"}, "other"]})

    mock_http(handler)

    async def scenario():
        async with client_for(app) as client:
            # A draft name with a base_url override: no stored entry, no key.
            return await client.get(
                "/api/connections/draft-name/models",
                params={"base_url": "http://127.0.0.1:4445/v1"},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["models"] == ["tiny", "other"]
    assert resp.json()["api_key_set"] is False
    assert seen["auth"] is None


@pytest.mark.parametrize(
    "base,expected",
    [
        ("http://localhost:4444/v1", "http://localhost:4444/v1/models"),
        ("http://localhost:4444/v1/", "http://localhost:4444/v1/models"),
        ("http://localhost:4444", "http://localhost:4444/v1/models"),
        ("https://api.z.ai/api/coding/paas/v4", "https://api.z.ai/api/coding/paas/v4/models"),
    ],
)
def test_models_url_appends_the_version_root_once(build_app, mock_http, base, expected):
    build, _, _ = build_app
    app = build()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": []})

    mock_http(handler)

    async def scenario():
        async with client_for(app) as client:
            return await client.get(
                "/api/connections/draft/models", params={"base_url": base}
            )

    assert run(scenario()).status_code == 200
    assert seen["url"] == expected


def test_discover_unknown_connection_without_override_is_404(build_app, mock_http):
    build, _, _ = build_app
    app = build()
    mock_http(lambda request: httpx.Response(200, json={"data": []}))

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections/ghost/models")

    assert run(scenario()).status_code == 404


def test_discover_upstream_error_is_502(build_app, mock_http):
    build, _, _ = build_app
    app = build()
    mock_http(lambda request: httpx.Response(500, json={"error": "boom"}))

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections/your-model-id/models")

    resp = run(scenario())
    assert resp.status_code == 502
    assert resp.json()["detail"]["url"].endswith("/v1/models")


def test_discover_unreachable_is_502(build_app, mock_http):
    build, _, _ = build_app
    app = build()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_http(handler)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections/your-model-id/models")

    assert run(scenario()).status_code == 502


def test_discover_without_store_and_without_override_is_503(tmp_path, mock_http):
    app = create_app(FakeTurnEngine(), tmp_path / "e.db")
    app.include_router(router)  # no store installed
    mock_http(lambda request: httpx.Response(200, json={"data": []}))

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections/local/models")

    assert run(scenario()).status_code == 503
    app.state.log.close()


def test_discover_transient_key_is_used_and_never_persisted(
    build_app_with_env, mock_http, scratch_env
):
    """A key PASTED into an unsaved draft probes with it and changes nothing on
    disk: no ``.env`` write, no ``api_key_env`` change, no echo back."""
    app, store, config_path, env_path = build_app_with_env
    before = config_path.read_text(encoding="utf-8")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "draft-model"}]})

    mock_http(handler)

    async def scenario():
        async with client_for(app) as client:
            # A name that does not exist: base_url + a literal key is all it takes.
            return await client.get(
                "/api/connections/never-saved/models",
                params={
                    "base_url": "http://127.0.0.1:4445/v1",
                    "api_key": "sk-draft-paste",
                },
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["models"] == ["draft-model"]
    assert resp.json()["api_key_set"] is True
    assert seen["auth"] == "Bearer sk-draft-paste"
    assert "sk-draft-paste" not in resp.text
    assert not env_path.exists()
    assert config_path.read_text(encoding="utf-8") == before
    assert "never-saved" not in store.connections


def test_discover_transient_key_header_keeps_the_secret_out_of_the_url(
    build_app_with_env, mock_http
):
    """The browser sends the draft key in a HEADER — uvicorn's access log records
    the URL, so the secret must never be in it."""
    app, _, _, _ = build_app_with_env
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": ["m"]})

    mock_http(handler)

    async def scenario():
        async with client_for(app) as client:
            return await client.get(
                "/api/connections/draft/models",
                params={"base_url": "http://127.0.0.1:4445/v1"},
                headers={TRANSIENT_KEY_HEADER: "sk-header-key"},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.request.url.params.get("api_key") is None
    assert seen["auth"] == "Bearer sk-header-key"
    assert "sk-header-key" not in resp.text


def test_discover_transient_key_wins_over_api_key_env(build_app, mock_http):
    build, _, _ = build_app
    app = build()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": []})

    mock_http(handler)

    async def scenario():
        async with client_for(app) as client:
            return await client.get(
                "/api/connections/your-model-id/models",
                params={"api_key_env": "MY_TEST_KEY", "api_key": "sk-typed-now"},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    # ``MY_TEST_KEY`` resolves to super-secret-value; the pasted literal wins.
    assert seen["auth"] == "Bearer sk-typed-now"
    assert "sk-typed-now" not in resp.text


def test_discover_error_text_redacts_the_transient_key(build_app, mock_http):
    """A transport that echoes the key in its message must not leak it to the UI."""
    build, _, _ = build_app
    app = build()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"connection refused (Authorization: Bearer sk-leaky-key)", request=request
        )

    mock_http(handler)

    async def scenario():
        async with client_for(app) as client:
            return await client.get(
                "/api/connections/draft/models",
                params={"base_url": "http://127.0.0.1:4445/v1", "api_key": "sk-leaky-key"},
            )

    resp = run(scenario())
    assert resp.status_code == 502
    assert "sk-leaky-key" not in resp.text
    assert "***" in resp.json()["detail"]["reason"]


# --- W9.25 slots payload ---------------------------------------------------


def test_get_connections_carries_the_slot_view(build_app):
    build, _, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/connections")

    body = run(scenario()).json()
    slots = body["slots"]
    assert [row["slot"] for row in slots] == [
        "completion",
        "completion.narration",
        "completion.extraction",
        "completion.state",
        "summarizer",
        "captioner",
        "imagegen",
        "embedding",
        "embedder",
        "decision",
    ]
    rows = {row["slot"]: row for row in slots}
    # the legacy twin authored in the document is reported, not hidden
    assert rows["completion.narration"]["connection"] == "your-model-id"
    assert rows["completion.narration"]["via"] == "legacy:narrator"
    assert rows["completion.narration"]["aliases"] == ["narrator"]
    assert rows["completion"]["connection"] is None
    assert rows["embedder"]["type"] == "embedding"
    # the raw map stays verbatim for compatibility
    assert body["agents"] == {"narrator": "your-model-id", "extractor": "your-model-id"}


# --- W9.25 typed connection fields (context_window / thinking / streaming) --


def test_patch_accepts_typed_connection_fields(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/your-model-id",
                json={"context_window": 65536, "thinking": False, "streaming": False},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    conn = resp.json()["connection"]
    assert conn["context_window"] == 65536
    assert conn["thinking"] is False
    assert conn["streaming"] is False

    live = store.connection_for("narrator")
    assert (live.context_window, live.thinking, live.streaming) == (65536, False, False)
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["connections"]["your-model-id"]["context_window"] == 65536
    assert written["connections"]["your-model-id"]["thinking"] is False
    assert written["connections"]["your-model-id"]["note"] == "user keeps this"


def test_patch_null_clears_typed_connection_fields(build_app):
    build, store, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await client.patch(
                "/api/connections/your-model-id",
                json={"context_window": 8192, "thinking": False, "streaming": False},
            )
            return await client.patch(
                "/api/connections/your-model-id",
                json={"context_window": None, "thinking": None, "streaming": None},
            )

    conn = run(scenario()).json()["connection"]
    assert conn["context_window"] is None
    assert conn["thinking"] is None
    assert conn["streaming"] is None
    live = store.connection_for("narrator")
    assert (live.context_window, live.thinking, live.streaming) == (None, None, None)


@pytest.mark.parametrize(
    "patch",
    [
        {"context_window": 0},
        {"context_window": -4},
        {"context_window": True},
        {"context_window": "65536"},
        {"thinking": "yes"},
        {"thinking": 1},
        {"streaming": "false"},
    ],
)
def test_patch_rejects_loose_typed_values(build_app, patch):
    build, store, config_path = build_app
    app = build()
    before = config_path.read_text(encoding="utf-8")

    async def scenario():
        async with client_for(app) as client:
            return await client.patch("/api/connections/your-model-id", json=patch)

    assert run(scenario()).status_code == 422
    assert config_path.read_text(encoding="utf-8") == before
    assert store.connection_for("narrator").context_window is None


def test_post_accepts_typed_connection_fields(build_app):
    build, store, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/connections/wide",
                json={
                    "base_url": "http://localhost:4444/v1",
                    "model": "qwen35-4b",
                    "context_window": 32768,
                    "thinking": False,
                    "streaming": True,
                },
            )

    assert run(scenario()).status_code == 201
    live = store.connections["wide"]
    assert (live.context_window, live.thinking, live.streaming) == (32768, False, True)


# --- W9.25 agent PATCH: declared spellings + canonical normalization -------


async def create_alt(client) -> httpx.Response:
    """POST a second connection so a rebind has somewhere to move to."""
    return await client.post(
        "/api/connections/alt",
        json={"base_url": "http://localhost:4445/v1", "model": "other"},
    )


def test_patch_agent_accepts_a_typed_spelling(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await create_alt(client)
            return await client.patch(
                "/api/connections/agents/completion.narration",
                json={"connection": "alt"},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "completion.narration"
    assert body["requested"] == "completion.narration"
    assert body["agents"]["completion.narration"] == "alt"
    # the legacy key is untouched and inert; resolution sees the typed key
    assert body["agents"]["narrator"] == "your-model-id"
    assert store.connection_for("narrator").name == "alt"
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["agents"]["narrator"] == "your-model-id"
    assert written["agents"]["completion.narration"] == "alt"


def test_patch_agent_normalizes_a_legacy_twin_address(build_app):
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await create_alt(client)
            return await client.patch("/api/connections/agents/narrator", json={"connection": "alt"})

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    # the WRITE landed on the canonical key
    assert body["agent"] == "completion.narration"
    assert body["requested"] == "narrator"
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["agents"]["completion.narration"] == "alt"
    assert written["agents"]["narrator"] == "your-model-id"  # never rewritten
    assert store.connection_for("narrator").name == "alt"


def test_patch_agent_creates_a_declared_item_slot(build_app):
    build, store, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            await create_alt(client)
            return await client.patch(
                "/api/connections/agents/summarizer", json={"connection": "alt"}
            )

    body = run(scenario()).json()
    assert body["agent"] == "summarizer"
    assert body["agents"]["summarizer"] == "alt"
    assert store.connection_for("summarizer").name == "alt"


@pytest.mark.parametrize("agent", ["ghost", "completion.bogus", "not a slot"])
def test_patch_agent_unknown_name_is_404(build_app, agent):
    build, store, config_path = build_app
    app = build()
    before = config_path.read_text(encoding="utf-8")

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                f"/api/connections/agents/{agent}", json={"connection": "your-model-id"}
            )

    assert run(scenario()).status_code == 404
    assert config_path.read_text(encoding="utf-8") == before


def test_patch_agent_unknown_connection_is_404(build_app):
    build, _, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/connections/agents/narrator", json={"connection": "nope"}
            )

    assert run(scenario()).status_code == 404


# --- "Return thinking": the reasoning echo round-trips create/PATCH/view -----


def test_return_thinking_round_trips_through_create_patch_and_the_view(build_app):
    """AUTO (null) is a VALUE: create ON, PATCH OFF, PATCH back to AUTO, GET sees it."""
    build, store, config_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = await client.post(
                "/api/connections/reasoner",
                json={
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "return_thinking": True,
                },
            )
            off = await client.patch(
                "/api/connections/reasoner", json={"return_thinking": False}
            )
            auto = await client.patch(
                "/api/connections/reasoner", json={"return_thinking": None}
            )
            listed = await client.get("/api/connections")
            return created, off, auto, listed

    created, off, auto, listed = run(scenario())
    assert created.status_code == 201
    assert created.json()["connection"]["return_thinking"] is True
    assert off.status_code == 200
    assert off.json()["connection"]["return_thinking"] is False
    assert auto.status_code == 200
    assert auto.json()["connection"]["return_thinking"] is None

    # The view serializes AUTO as null (never a fake default) for every connection.
    view = listed.json()["connections"]
    assert view["reasoner"]["return_thinking"] is None
    assert view["your-model-id"]["return_thinking"] is None
    # The live registry agrees, and the write is durable.
    assert store.connections["reasoner"].return_thinking is None
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["connections"]["reasoner"]["return_thinking"] is None


@pytest.mark.parametrize(
    "patch",
    [
        {"return_thinking": "auto"},
        {"return_thinking": 1},
        {"return_thinking": 0},
        {"return_thinking": "true"},
    ],
)
def test_patch_rejects_a_non_boolean_return_thinking(build_app, patch):
    """No coercion: a string is the classic mis-set tri-state, so it is a 422."""
    build, store, config_path = build_app
    app = build()
    before = config_path.read_text(encoding="utf-8")

    async def scenario():
        async with client_for(app) as client:
            return await client.patch("/api/connections/your-model-id", json=patch)

    assert run(scenario()).status_code == 422
    assert config_path.read_text(encoding="utf-8") == before
    assert store.connections["your-model-id"].return_thinking is None
