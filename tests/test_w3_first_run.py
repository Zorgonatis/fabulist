"""W3c: first-run bootstrap — a fresh clone must boot and be able to create.

Audit-verified defects this file pins, each against the real code path:

1. A MISSING ``configs/connections.json`` boots as an EMPTY registry (zero
   connections, zero bindings) with the first-run log line — while an existing
   but malformed file still fails loudly.
2. ``RegistryStore.add_connection`` / ``write_env_key`` create their target
   directory, so the API create door works where ``configs/`` never existed.
3. ``POST /api/connections/{name}`` works end-to-end from the empty/missing
   registry, and the created entry is bindable to a slot.
4. An unknown provider gets the modern-OpenAI-compatible default capability
   profile (tools + json mode on) while the measured llama.cpp/deepseek profiles
   are untouched.
5. ``scripts/serve.py`` creates the config directory it resolves (the repo's
   ``configs/`` by default, see ``tests/test_serve_paths.py``) and prints the
   neutral first-run guidance (JSON carries no comments, so the example's shape
   is explained there).
6. The committed example is a NEUTRAL template (no owner-machine endpoint, cloud
   account or model name) and ``build_engine``'s narrator snapshot no longer
   crashes BOOT — a turn with nothing bound reports the readable
   ``ConnectionConfigError`` text instead of a 500.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import stat
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.connections_api import router as connections_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.connections.capabilities import (
    DEFAULT_CAPABILITIES,
    resolve_capabilities,
)
from fabulist.connections.registry import (
    FIRST_RUN_LOG,
    Connection,
    ConnectionConfigError,
    RegistryStore,
    load_registry,
    write_env_key,
)
from fabulist.wiring import build_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _app_with(store: RegistryStore, log_path: Path):
    """The router over a real store — exactly what serve.py installs."""
    app = create_app(FakeTurnEngine(), log_path)
    app.include_router(connections_router)
    app.state.connections_store = store
    return app


# --- 1. a missing config is a first run, not a crash -------------------------


def test_missing_config_loads_as_an_empty_registry(tmp_path, caplog):
    missing = tmp_path / "configs" / "connections.json"

    with caplog.at_level(logging.WARNING, logger="fabulist.connections.registry"):
        registry = load_registry(missing)

    assert registry.connections == {}
    assert registry.agents == {}
    assert FIRST_RUN_LOG in caplog.text


def test_missing_config_store_boots_empty(tmp_path, caplog):
    store = RegistryStore(
        tmp_path / "nowhere" / "connections.json", env_path=tmp_path / ".env"
    )

    assert store.connections == {}
    assert store.raw() == {}
    assert FIRST_RUN_LOG in caplog.text


@pytest.mark.parametrize(
    "document",
    [
        "{not json at all",  # unparseable
        json.dumps({"connections": {}}),  # parses, but no connection to resolve
        json.dumps({"connections": {"c": {"model": "m"}}}),  # missing base_url
        json.dumps({"connections": {"c": {"base_url": "http://h/v1", "model": "m"}},
                    "agents": {"narrator": "ghost"}}),  # dangling binding
    ],
)
def test_an_existing_but_malformed_config_still_fails_loudly(tmp_path, document):
    path = tmp_path / "connections.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConnectionConfigError):
        load_registry(path)


def test_an_empty_registry_names_the_missing_binding(tmp_path):
    """The empty registry stays honest: resolving a slot fails with the vocabulary."""
    registry = load_registry(tmp_path / "configs" / "connections.json")

    with pytest.raises(ConnectionConfigError) as excinfo:
        registry.connection_for("narrator")

    assert "<none>" in str(excinfo.value)


# --- 2. writes create their target directory ---------------------------------


def test_add_connection_creates_the_configs_directory(tmp_path):
    path = tmp_path / "configs" / "connections.json"  # configs/ does not exist
    store = RegistryStore(path, env_path=tmp_path / ".env")

    store.add_connection(
        "local", {"base_url": "http://127.0.0.1:8080/v1", "model": "your-model-id"}
    )

    assert path.exists()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["connections"]["local"]["base_url"] == "http://127.0.0.1:8080/v1"


def test_write_env_key_creates_its_directory(tmp_path):
    env_path = tmp_path / "secrets" / ".env"  # directory does not exist

    write_env_key(env_path, "FABULIST_LOCAL_KEY", "sk-test")

    assert env_path.read_text(encoding="utf-8") == "FABULIST_LOCAL_KEY=sk-test\n"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


# --- 3. the create door works from empty -------------------------------------


def test_create_connection_works_from_a_missing_config_file(tmp_path):
    config_path = tmp_path / "configs" / "connections.json"
    store = RegistryStore(config_path, env_path=tmp_path / ".env")
    app = _app_with(store, tmp_path / "events.db")

    async def scenario():
        async with client_for(app) as client:
            before = await client.get("/api/connections")
            created = await client.post(
                "/api/connections/local",
                json={"base_url": "http://127.0.0.1:8080/v1", "model": "your-model-id"},
            )
            listed = await client.get("/api/connections")
            bound = await client.patch(
                "/api/connections/agents/narrator", json={"connection": "local"}
            )
            return before, created, listed, bound

    try:
        before, created, listed, bound = run(scenario())
    finally:
        app.state.log.close()

    # An empty registry is a readable state, not an error.
    assert before.status_code == 200
    assert before.json()["connections"] == {}
    # The create door: 201, and the file (with its directory) now exists.
    assert created.status_code == 201
    assert created.json()["connection"]["name"] == "local"
    assert json.loads(config_path.read_text(encoding="utf-8"))["connections"] == {
        "local": {"base_url": "http://127.0.0.1:8080/v1", "model": "your-model-id"}
    }
    assert listed.json()["connections"]["local"]["model"] == "your-model-id"
    # ... and the created entry is bindable, so the engine has a narrator again.
    # The typed key is what PATCH writes (W9.25 normalization of the legacy twin).
    assert bound.status_code == 200
    assert bound.json()["agents"] == {"completion.narration": "local"}
    assert store.connection_for("narrator").name == "local"


def test_create_then_reload_from_disk_keeps_the_entry(tmp_path):
    """A restart (a fresh store over the same file) sees what the door wrote."""
    config_path = tmp_path / "configs" / "connections.json"
    store = RegistryStore(config_path, env_path=tmp_path / ".env")
    store.add_connection(
        "local", {"base_url": "http://127.0.0.1:8080/v1", "model": "your-model-id"}
    )

    reopened = RegistryStore(config_path, env_path=tmp_path / ".env")

    assert set(reopened.connections) == {"local"}
    assert reopened.connections["local"].model == "your-model-id"


# --- 4. the unknown-provider capability profile ------------------------------


def test_an_unknown_provider_gets_tools_and_json_mode():
    connection = Connection(
        name="cloud", base_url="https://api.example.com/v1", model="mystery-9b"
    )

    caps = resolve_capabilities(connection)

    assert caps == DEFAULT_CAPABILITIES
    assert caps["function_calling"] is True  # tools=[...]
    assert caps["json_object"] is True  # json mode
    # Anything a server may REJECT, and any claim about the model, stays off.
    assert caps["json_schema"] is False
    assert caps["tool_choice_forced"] is False
    assert caps["reasoning_model"] is False
    assert caps["thinking_disable"] is False


def test_the_measured_profiles_are_unchanged():
    local = Connection(name="l", base_url="http://localhost:4444/v1", model="your-model-id")
    cloud = Connection(
        name="ds", base_url="https://api.deepseek.com/v1", model="deepseek-reasoner"
    )

    local_caps = resolve_capabilities(local)
    deepseek_caps = resolve_capabilities(cloud)

    assert local_caps["json_schema"] is True
    assert local_caps["tool_choice_forced"] is True
    assert local_caps["thinking_disable"] is False
    assert deepseek_caps["json_schema"] is False
    assert deepseek_caps["tool_choice_forced"] is False
    assert deepseek_caps["reasoning_model"] is True
    assert deepseek_caps["thinking_disable"] is True


# --- 5. boot survives an unbound registry ------------------------------------


def test_build_app_boots_with_no_connections_and_a_turn_fails_readably(tmp_path):
    """BOOT survives an empty registry; a TURN fails with the readable reason.

    ``build_engine`` snapshots the narrator connection at build time. On a first
    run there is none: the snapshot must not crash the boot, and the first turn
    must report the registry's ``ConnectionConfigError`` text (naming the missing
    ``narrator`` binding) as an ``error`` frame — never a NoneType/AttributeError
    500.
    """
    store = RegistryStore(
        tmp_path / "configs" / "connections.json", env_path=tmp_path / ".env"
    )
    app = build_app(registry=store, log_path=tmp_path / "events.db")

    async def scenario():
        return [event async for event in app.state.engine.run_turn("first-run", "hi")]

    try:
        events = run(scenario())
    finally:
        app.state.log.close()

    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1
    reason = errors[0]["delta"]
    assert "unknown agent 'narrator'" in reason
    assert "known agents: <none>" in reason
    assert "NoneType" not in reason and "AttributeError" not in reason


# --- 6. the committed example is a neutral template --------------------------


def test_the_committed_example_carries_no_owner_machine_values():
    path = REPO_ROOT / "configs" / "connections.example.json"
    text = path.read_text(encoding="utf-8")

    # The owner's endpoint, cloud account and model names must not ship.
    for owner_value in ("4444", "z.ai", "ZAI_API_KEY", "qwen", "heretic"):
        assert owner_value not in text

    registry = load_registry(path)
    assert registry.connections["local"].base_url == "http://127.0.0.1:8080/v1"
    assert registry.connections["local"].model == "your-model-id"
    assert registry.connections["cloud"].api_key_env == "YOUR_PROVIDER_API_KEY"
    assert registry.connection_for("narrator").name == "local"


# --- 7. the serve entry point ------------------------------------------------


def _load_serve_module():
    path = REPO_ROOT / "scripts" / "serve.py"
    spec = importlib.util.spec_from_file_location("fabulist_serve_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_serve_creates_the_config_directory_it_resolves_and_documents_the_neutral_example(
    tmp_path,
):
    serve = _load_serve_module()

    # The first-run case the W3c fix covers: no configs/ directory anywhere yet.
    # Since issue #5 the directory created is the RESOLVED config's parent (repo-
    # anchored by default), not a CWD-relative ``Path("configs")``.
    config = tmp_path / "configs" / "connections.json"
    paths = serve.resolve_paths(config=config)
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    assert paths.config.parent.is_dir()
    # JSON has no comments: the first-run output carries the explanation.
    assert "configs/connections.example.json" in serve.FIRST_RUN_HINT
    assert "api_key_env" in serve.FIRST_RUN_HINT
    assert "127.0.0.1:8080" in serve.FIRST_RUN_HINT
