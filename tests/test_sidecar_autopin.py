"""Issue #36: auto-pin the models-sidecar connections at engine boot.

The owner-approved contract (issue #36), pinned here as the pin/no-pin matrix:

* ``FABULIST_SIDECAR_URL`` unset -> nothing is probed, nothing is written.
* A role already pointing somewhere -> untouched (narrator/completion untouched
  always; a slot naming a MISSING connection is left alone too).
* A served model -> a REAL connection entry (``sidecar-embed`` /
  ``sidecar-rerank`` / ``sidecar-decision``) + the agent slot, through the
  ordinary registry store: validated, atomic, hot-reloadable, drawer-visible.
* A model not served -> that role is skipped and ONE notice names it.
* Sidecar unreachable -> no pin, ONE notice, boot proceeds.
* Issue #38: every pinned entry also carries ``context_window`` — the window its
  model's ``ctx-size`` declares in the shipped ``deploy/sidecar/models.ini`` —
  so a fresh deployment's drawer shows a real number instead of "unknown".

The ``/v1/models`` responses are faked two ways: an ``httpx.MockTransport`` for
the matrix (the existing connections-API test pattern) and a real local
``http.server`` on a spare port for the boot-hook end-to-end test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fabulist import sidecar as sidecar_module
from fabulist.api import create_app
from fabulist.api.connections_api import router as connections_router
from fabulist.api.fake import FakeTurnEngine
from fabulist.connections.registry import RegistryStore, load_registry
from fabulist.sidecar import (
    CONNECTION_TIMEOUT_S,
    SIDECAR_CONTEXT_WINDOWS,
    SIDECAR_ROLES,
    SIDECAR_URL_ENV,
    autopin_sidecar,
    models_url,
    schedule_autopin_retry,
)
from fabulist.wiring import build_app

SIDECAR = "http://fabulist-models:8080/v1"
ALL_MODELS = ("embeddinggemma-2", "qwen3-reranker", "d1-3b")
REPO_ROOT = Path(__file__).resolve().parents[1]


# --- helpers --------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Every test starts with the trigger OFF and no host alias rewriting URLs."""
    monkeypatch.delenv(SIDECAR_URL_ENV, raising=False)
    monkeypatch.delenv("FABULIST_HOST_ALIAS", raising=False)


def run(coro):
    return asyncio.run(coro)


def write_config(
    path: Path,
    *,
    agents: dict | None = None,
    extra_connections: dict | None = None,
) -> Path:
    """A valid document: a narrator connection plus whatever the test adds."""
    connections = {
        "local": {"base_url": "http://127.0.0.1:4444/v1", "model": "narrator-model"}
    }
    connections.update(extra_connections or {})
    document = {
        "connections": connections,
        "agents": {"narrator": "local"} if agents is None else agents,
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def make_store(tmp_path: Path, **kwargs) -> RegistryStore:
    config = write_config(tmp_path / "connections.json", **kwargs)
    return RegistryStore(config, env_path=tmp_path / ".env")


def serve_transport(model_ids, status: int = 200) -> httpx.MockTransport:
    """An httpx transport answering ``/models`` with the given ids."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models"), request.url.path
        assert request.headers.get("Accept") == "application/json"
        return httpx.Response(status, json={"data": [{"id": i} for i in model_ids]})

    return httpx.MockTransport(handler)


def down_transport() -> httpx.MockTransport:
    """An httpx transport that fails like a stopped sidecar."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


def get_connections(app) -> dict:
    async def _run() -> dict:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/connections")
        assert response.status_code == 200
        return response.json()

    return run(_run())


class _ModelsHandler(BaseHTTPRequestHandler):
    """``GET /v1/models`` for the real-socket end-to-end test."""

    model_ids: tuple[str, ...] = ()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps({"data": [{"id": m} for m in self.model_ids]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass


@pytest.fixture
def fake_sidecar():
    """Start a real local ``/v1/models`` server; returns its base URL."""
    started: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    def _start(model_ids) -> str:
        handler = type("_Handler", (_ModelsHandler,), {"model_ids": tuple(model_ids)})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    yield _start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _RecordingStore:
    """Minimal writable store double for the "slot names a ghost" case.

    A real document cannot express ``agents: {"embedder": "ghost"}`` —
    ``Registry.from_dict`` refuses a binding to a missing connection at load —
    so the deliberate half-edited state is simulated here. The hook must leave it
    alone instead of "repairing" it.
    """

    def __init__(self, agents: dict) -> None:
        self.agents = dict(agents)
        self.connections: dict = {}
        self.calls: list[tuple] = []

    def add_connection(self, name: str, entry: dict) -> None:
        self.calls.append(("add", name))
        self.connections[name] = SimpleNamespace(
            name=name, model=entry["model"], base_url=entry["base_url"]
        )

    def bind_agent(self, slot: str, connection: str) -> None:
        self.calls.append(("bind", slot, connection))
        self.agents[slot] = connection


# --- 1. env absent: nothing happens ---------------------------------------


def test_env_absent_pins_nothing_and_never_probes(tmp_path, monkeypatch):
    """Unset (or empty) FABULIST_SIDECAR_URL is a strict no-op."""
    store = make_store(tmp_path)
    before = store.path.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the sidecar must not be probed when the env is unset")

    result = autopin_sidecar(store, transport=httpx.MockTransport(handler))

    assert result.pinned == {}
    assert result.notice is None
    assert result.unreachable is False
    assert store.path.read_text(encoding="utf-8") == before
    assert set(store.connections) == {"local"}
    assert store.agents == {"narrator": "local"}

    # Empty string is the app-only compose's explicit "off" spelling.
    monkeypatch.setenv(SIDECAR_URL_ENV, "   ")
    again = autopin_sidecar(store, transport=httpx.MockTransport(handler))
    assert again.pinned == {} and again.notice is None
    assert store.path.read_text(encoding="utf-8") == before


def test_models_url_accepts_a_version_root_and_a_bare_host():
    assert models_url("http://fabulist-models:8080/v1") == (
        "http://fabulist-models:8080/v1/models"
    )
    assert models_url("http://fabulist-models:8080") == (
        "http://fabulist-models:8080/v1/models"
    )
    assert models_url("fabulist-models:8080") == (
        "http://fabulist-models:8080/v1/models"
    )
    assert models_url("https://proxy.example/api/v4") == (
        "https://proxy.example/api/v4/models"
    )


# --- 2. a filled role is untouched ----------------------------------------


def test_a_role_already_pointing_somewhere_is_never_touched(tmp_path):
    """Fill-missing only: the authored embedder slot and its entry survive."""
    store = make_store(
        tmp_path,
        agents={"narrator": "local", "embedder": "my-embed"},
        extra_connections={
            "my-embed": {
                "base_url": "http://my-server:9999/v1",
                "model": "custom-embed",
            }
        },
    )

    result = autopin_sidecar(store, url=SIDECAR, transport=serve_transport(ALL_MODELS))

    assert result.pinned == {
        "reranker": "sidecar-rerank",
        "decision": "sidecar-decision",
    }
    assert result.skipped["embedder"] == "already bound to 'my-embed'"
    # No sidecar-embed entry was created, and the user's binding is byte-identical.
    assert "sidecar-embed" not in store.connections
    assert store.connection_for("embedder").name == "my-embed"
    assert store.connection_for("embedder").model == "custom-embed"
    # The narrator is never in scope at all.
    assert store.connection_for("narrator").name == "local"
    assert result.notice is None, "a pre-existing binding is not a notice"


def test_a_second_boot_is_a_no_op(tmp_path):
    """Stickiness: the pins from boot one mean boot two writes nothing."""
    store = make_store(tmp_path)
    first = autopin_sidecar(store, url=SIDECAR, transport=serve_transport(ALL_MODELS))
    assert len(first.pinned) == 3
    after_first = store.path.read_text(encoding="utf-8")

    second = autopin_sidecar(store, url=SIDECAR, transport=serve_transport(ALL_MODELS))

    assert second.pinned == {}
    assert second.notice is None
    assert store.path.read_text(encoding="utf-8") == after_first
    assert set(store.agents) == {"narrator", "embedder", "reranker", "decision"}


def test_a_slot_naming_a_missing_connection_is_left_alone():
    """Deliberate user state (a half-edited agents map) is not "repaired"."""
    store = _RecordingStore({"narrator": "local", "embedder": "ghost"})

    result = autopin_sidecar(store, url=SIDECAR, transport=serve_transport(ALL_MODELS))

    assert result.pinned == {
        "reranker": "sidecar-rerank",
        "decision": "sidecar-decision",
    }
    assert result.skipped["embedder"] == "already bound to 'ghost'"
    assert store.agents["embedder"] == "ghost"
    assert ("add", "sidecar-embed") not in store.calls
    assert ("bind", "embedder", "sidecar-embed") not in store.calls
    assert result.notice is None


# --- 3. a model not served: skip + ONE notice -----------------------------


def test_model_not_served_is_skipped_with_one_naming_notice(tmp_path, caplog):
    """A partial sidecar pins what it serves and says what it skipped."""
    store = make_store(tmp_path)

    with caplog.at_level(logging.WARNING, logger="fabulist.sidecar"):
        result = autopin_sidecar(
            store,
            url=SIDECAR,
            transport=serve_transport(("embeddinggemma-2", "d1-3b")),
        )

    assert result.pinned == {
        "embedder": "sidecar-embed",
        "decision": "sidecar-decision",
    }
    assert result.skipped["reranker"] == "qwen3-reranker not served"
    assert "sidecar-rerank" not in store.connections
    assert store.connection_for("decision").model == "d1-3b"
    assert result.notice is not None
    assert "reranker" in result.notice and "qwen3-reranker" in result.notice
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "partial sidecar logs exactly ONE notice"
    assert result.notice in warnings[0].getMessage()


# --- 4. all served: three real pins, drawer-visible -----------------------


def test_all_served_pins_three_validated_connection_entries(tmp_path):
    """The happy path: real entries + slots, written through the store."""
    store = make_store(tmp_path)

    result = autopin_sidecar(store, url=SIDECAR, transport=serve_transport(ALL_MODELS))

    assert result.pinned == {
        "embedder": "sidecar-embed",
        "reranker": "sidecar-rerank",
        "decision": "sidecar-decision",
    }
    assert result.notice is None
    for slot, (name, model) in SIDECAR_ROLES.items():
        conn = store.connection_for(slot)
        assert conn.name == name
        assert conn.model == model
        assert conn.base_url == SIDECAR
        assert conn.timeout == CONNECTION_TIMEOUT_S
        # Issue #38: the pin declares the window the model's preset serves, so a
        # fresh deployment's drawer is self-documenting instead of "unknown".
        assert conn.context_window == SIDECAR_CONTEXT_WINDOWS[model]

    # The document on disk is valid and carries the same bindings: this is the
    # "validated exactly like a hand-made connection" claim, checked by reload.
    disk = load_registry(store.path)
    assert disk.connection_for("embedder").name == "sidecar-embed"
    assert disk.connection_for("reranker").model == "qwen3-reranker"
    assert disk.connection_for("decision").name == "sidecar-decision"
    assert disk.connection_for("narrator").name == "local"
    assert disk.agents["embedder"] == "sidecar-embed"


def test_pinned_entries_and_slots_are_drawer_visible(tmp_path):
    """GET /api/connections shows the real entries and the resolved slots."""
    store = make_store(tmp_path)
    autopin_sidecar(store, url=SIDECAR, transport=serve_transport(ALL_MODELS))

    app = create_app(FakeTurnEngine(), tmp_path / "drawer.db")
    app.include_router(connections_router)
    app.state.registry = store
    try:
        body = get_connections(app)
    finally:
        app.state.log.close()

    for name, model in (
        ("sidecar-embed", "embeddinggemma-2"),
        ("sidecar-rerank", "qwen3-reranker"),
        ("sidecar-decision", "d1-3b"),
    ):
        entry = body["connections"][name]
        assert entry["base_url"] == SIDECAR
        assert entry["model"] == model
        assert entry["timeout"] == CONNECTION_TIMEOUT_S
        assert entry["api_key_set"] is False
        assert entry["context_window"] == SIDECAR_CONTEXT_WINDOWS[model]

    assert body["agents"]["embedder"] == "sidecar-embed"
    assert body["agents"]["reranker"] == "sidecar-rerank"
    assert body["agents"]["decision"] == "sidecar-decision"
    assert body["agents"]["narrator"] == "local"

    slots = {row["slot"]: row for row in body["slots"]}
    # Issue #48: ONE row per resolved role — the pinned ``embedder`` key is the
    # embedding role's alias, so the drawer row is ``embedding`` (resolved via it).
    assert "embedder" not in slots
    assert slots["embedding"]["connection"] == "sidecar-embed"
    assert slots["embedding"]["via"] == "item:embedder"
    assert slots["reranker"]["connection"] == "sidecar-rerank"
    assert slots["decision"]["connection"] == "sidecar-decision"


# --- 5. the sidecar is down: no pin, ONE notice, boot proceeds ------------


def _served_ctx_sizes() -> dict[str, int]:
    """The shipped router preset's ``model id -> ctx-size`` (deploy/models.ini).

    llama.cpp's router preset is not ``configparser``-shaped: it carries
    top-level keys (``version = 1``) before the first section, so the two lines
    that matter are read directly: a ``[section]`` is a model id, its
    ``ctx-size`` is the window that model serves.
    """
    sizes: dict[str, int] = {}
    section: str | None = None
    for raw in (REPO_ROOT / "deploy" / "sidecar" / "models.ini").read_text(
        encoding="utf-8"
    ).splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section is not None and line.startswith("ctx-size"):
            sizes[section] = int(line.split("=", 1)[1].strip())
    return sizes


def test_the_context_table_mirrors_the_shipped_router_preset():
    """Issue #38: :data:`SIDECAR_CONTEXT_WINDOWS` and models.ini change TOGETHER.

    The pin writes the window the deployment's own preset declares, so the table
    is a mirror, not a second opinion: every served model has one row, and a
    ``ctx-size`` edit in ``models.ini`` that does not move the table fails here.
    """
    served = _served_ctx_sizes()

    assert set(SIDECAR_CONTEXT_WINDOWS) == set(served), (
        "models.ini and SIDECAR_CONTEXT_WINDOWS must name the same models"
    )
    assert SIDECAR_CONTEXT_WINDOWS == served
    # The cited values (issue #38 owner note): reranker 2048, embedder 8192.
    assert SIDECAR_CONTEXT_WINDOWS["qwen3-reranker"] == 2048
    assert SIDECAR_CONTEXT_WINDOWS["embeddinggemma-2"] == 8192
    # Every model a role pins is in the table: no pin leaves the window unset.
    assert {model for _name, model in SIDECAR_ROLES.values()} <= set(
        SIDECAR_CONTEXT_WINDOWS
    )


def test_unreachable_sidecar_pins_nothing_and_logs_one_notice(tmp_path, caplog):
    store = make_store(tmp_path)
    before = store.path.read_text(encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="fabulist.sidecar"):
        result = autopin_sidecar(store, url=SIDECAR, transport=down_transport())

    assert result.pinned == {}
    assert result.unreachable is True
    assert result.notice is not None
    assert "unreachable" in result.notice and "ConnectError" in result.notice
    assert store.path.read_text(encoding="utf-8") == before
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert result.notice in warnings[0].getMessage()


def test_boot_proceeds_when_the_sidecar_is_down(tmp_path, monkeypatch):
    """build_app still builds the engine when the probe fails (never raises)."""

    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    store = make_store(tmp_path)
    monkeypatch.setenv(SIDECAR_URL_ENV, SIDECAR)
    monkeypatch.setattr(sidecar_module, "fetch_served_models", boom)

    app = build_app(registry=store, log_path=tmp_path / "events.db")
    try:
        assert app.state.sidecar_pin.unreachable is True
        assert app.state.sidecar_pin.pinned == {}
        assert app.state.sidecar_pin.notice is not None
        assert app.state.engine is not None
        # No running loop in this (sync) build_app call: nothing is armed, and
        # nothing raises — the retry seam is loop-bound (see section 7).
        assert app.state.sidecar_retry is None
        assert store.agents == {"narrator": "local"}
        assert set(store.connections) == {"local"}
        assert app.state.memorizer is None, "no embedder pin -> memory stays off"
    finally:
        app.state.log.close()


# --- 6. the boot hook end-to-end against a real socket --------------------


def test_boot_hook_pins_against_a_real_sidecar_socket(tmp_path, monkeypatch, fake_sidecar):
    """The real path: build_app probes a local /v1/models and turns memory on."""
    url = fake_sidecar(ALL_MODELS)
    store = make_store(tmp_path)
    monkeypatch.setenv(SIDECAR_URL_ENV, url)

    app = build_app(registry=store, log_path=tmp_path / "events.db")
    try:
        pin = app.state.sidecar_pin
        assert pin.unreachable is False
        assert pin.pinned == {
            "embedder": "sidecar-embed",
            "reranker": "sidecar-rerank",
            "decision": "sidecar-decision",
        }
        assert pin.notice is None
        assert pin.served == ALL_MODELS
        assert store.connection_for("embedder").base_url == url
        # The memorizer is built AFTER the pin, so a fresh deploy has recall.
        assert app.state.memorizer is not None
    finally:
        app.state.log.close()


# --- 7. the boot race: a BOUNDED repeating re-probe schedule --------------


def test_unreachable_at_boot_then_reachable_at_retry_pins(tmp_path, monkeypatch):
    """The race: the sidecar is not ready at boot, up on the first re-probe.

    The boot probe logs its one notice and arms the bounded schedule
    (``RETRY_DELAY_S`` is the knob pinned here); the first re-probe runs against
    a sidecar that is now serving, so the ordinary fill-empty-slots pin lands,
    refreshes ``app.state.sidecar_pin``, and the schedule STOPS — the budget it
    had left is never spent.
    """
    store = make_store(tmp_path)
    monkeypatch.setenv(SIDECAR_URL_ENV, SIDECAR)
    monkeypatch.setattr(sidecar_module, "RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(sidecar_module, "RETRY_MAX_ATTEMPTS", 5)
    attempts: list[int] = []

    def flaky(url, *, timeout=0.0, transport=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("[Errno -2] Name or service not known")
        return ALL_MODELS

    monkeypatch.setattr(sidecar_module, "fetch_served_models", flaky)

    async def _scenario():
        app = build_app(registry=store, log_path=tmp_path / "events.db")
        booted = app.state.sidecar_pin
        retry = app.state.sidecar_retry
        assert booted.unreachable is True, "the boot probe lost the race"
        assert retry is not None, "an unreachable boot probe must arm the schedule"
        result = await asyncio.wait_for(retry, timeout=5)
        # The task is DONE: waiting longer must not produce another attempt.
        await asyncio.sleep(0.05)
        return app, booted, result

    app, booted, result = asyncio.run(_scenario())
    try:
        assert booted.notice is not None
        assert len(attempts) == 2, "boot + ONE re-probe: success stops the schedule"
        assert result is not None and result.pinned == {
            "embedder": "sidecar-embed",
            "reranker": "sidecar-rerank",
            "decision": "sidecar-decision",
        }
        assert result.unreachable is False and result.notice is None
        # The pins are REAL store entries, exactly like the boot path's.
        for slot, (name, model) in SIDECAR_ROLES.items():
            assert store.connection_for(slot).name == name
            assert store.connection_for(slot).model == model
        assert load_registry(store.path).agents["embedder"] == "sidecar-embed"
        # The diagnostic moved with the retry (boot-time notice -> clean pin).
        assert app.state.sidecar_pin is result
        # No strong ref survives a finished task.
        assert sidecar_module._RETRY_TASKS == set()
    finally:
        app.state.log.close()


@pytest.mark.parametrize("success_at", [2, 3])
def test_autopin_retry_repeats_until_success_then_stops(
    tmp_path, monkeypatch, success_at
):
    """A slow sidecar is still pinned: the schedule REPEATS, then stops.

    ``success_at`` counts probes including the boot one: 2 = the first re-probe
    answers, 3 = the second does. Either way every role is pinned once, and no
    attempt happens after the answering one (the budget had retries left).
    """
    store = make_store(tmp_path)
    monkeypatch.setenv(SIDECAR_URL_ENV, SIDECAR)
    monkeypatch.setattr(sidecar_module, "RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(sidecar_module, "RETRY_MAX_ATTEMPTS", 5)
    attempts: list[int] = []

    def flaky(url, *, timeout=0.0, transport=None):
        attempts.append(1)
        if len(attempts) < success_at:
            raise httpx.ConnectError("connection refused")
        return ALL_MODELS

    monkeypatch.setattr(sidecar_module, "fetch_served_models", flaky)

    async def _scenario():
        app = build_app(registry=store, log_path=tmp_path / "events.db")
        retry = app.state.sidecar_retry
        assert retry is not None
        result = await asyncio.wait_for(retry, timeout=5)
        await asyncio.sleep(0.05)  # a live schedule would have probed again
        return app, result

    app, result = asyncio.run(_scenario())
    try:
        assert len(attempts) == success_at, "stops at the first answering probe"
        assert result is not None and not result.unreachable
        assert set(result.pinned) == {"embedder", "reranker", "decision"}
        assert store.connection_for("decision").name == "sidecar-decision"
        assert app.state.sidecar_pin is result
        assert sidecar_module._RETRY_TASKS == set()
    finally:
        app.state.log.close()


def test_unreachable_at_boot_and_at_retry_pins_nothing(tmp_path, monkeypatch, caplog):
    """Still down through a spent budget: no pin, ONE notice per attempt.

    The chosen logging discipline: every failed attempt logs its own ONE notice
    (the probe's), and the exhausted budget adds exactly ONE final giving-up
    line — never a pair per attempt, never silence.
    """
    store = make_store(tmp_path)
    before = store.path.read_text(encoding="utf-8")
    monkeypatch.setenv(SIDECAR_URL_ENV, SIDECAR)
    monkeypatch.setattr(sidecar_module, "RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(sidecar_module, "RETRY_MAX_ATTEMPTS", 2)
    attempts: list[int] = []

    def down(url, *, timeout=0.0, transport=None):
        attempts.append(1)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(sidecar_module, "fetch_served_models", down)

    async def _scenario():
        app = build_app(registry=store, log_path=tmp_path / "events.db")
        retry = app.state.sidecar_retry
        assert retry is not None
        return app, await asyncio.wait_for(retry, timeout=5)

    with caplog.at_level(logging.WARNING, logger="fabulist.sidecar"):
        app, result = asyncio.run(_scenario())
    try:
        assert len(attempts) == 3, "boot + the two attempts the budget allowed"
        assert result is not None
        assert result.pinned == {} and result.unreachable is True
        assert result.notice is not None and "unreachable" in result.notice
        assert store.path.read_text(encoding="utf-8") == before
        assert set(store.connections) == {"local"}
        assert store.agents == {"narrator": "local"}
        assert app.state.sidecar_pin is result
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "fabulist.sidecar"
        ]
        # ONE notice PER ATTEMPT (boot + each retry), plus ONE final line.
        assert len(warnings) == 4, warnings
        assert sum("models endpoint is unreachable" in m for m in warnings) == 3
        assert sum("giving up" in m for m in warnings) == 1
        assert sidecar_module._RETRY_TASKS == set()
    finally:
        app.state.log.close()


def test_never_reachable_stops_after_the_budget_with_one_final_notice(
    tmp_path, monkeypatch, caplog
):
    """A sidecar that never comes up: the budget ENDS the schedule, loudly once."""
    store = make_store(tmp_path)
    before = store.path.read_text(encoding="utf-8")
    monkeypatch.setenv(SIDECAR_URL_ENV, SIDECAR)
    monkeypatch.setattr(sidecar_module, "RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(sidecar_module, "RETRY_MAX_ATTEMPTS", 3)
    attempts: list[int] = []

    def down(url, *, timeout=0.0, transport=None):
        attempts.append(1)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(sidecar_module, "fetch_served_models", down)

    async def _scenario():
        app = build_app(registry=store, log_path=tmp_path / "events.db")
        retry = app.state.sidecar_retry
        assert retry is not None
        result = await asyncio.wait_for(retry, timeout=10)
        await asyncio.sleep(0.05)  # proves the schedule ended, not paused
        return app, result

    with caplog.at_level(logging.WARNING, logger="fabulist.sidecar"):
        app, result = asyncio.run(_scenario())
    try:
        assert len(attempts) == 4, "boot + exactly the three allowed re-probes"
        assert result is not None and result.pinned == {}
        assert store.path.read_text(encoding="utf-8") == before
        assert store.agents == {"narrator": "local"}
        giving_up = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
            and r.name == "fabulist.sidecar"
            and "giving up" in r.getMessage()
        ]
        assert len(giving_up) == 1, "ONE final line when the budget is spent"
        assert "3 re-probe" in giving_up[0]
        assert sidecar_module._RETRY_TASKS == set()
    finally:
        app.state.log.close()


def test_no_env_var_arms_no_retry_schedule(tmp_path, monkeypatch):
    """Unset FABULIST_SIDECAR_URL: no probe, no schedule (the app-only shape)."""
    store = make_store(tmp_path)
    before = store.path.read_text(encoding="utf-8")

    def handler(url, *, timeout=0.0, transport=None):  # pragma: no cover
        raise AssertionError("the sidecar must not be probed when the env is unset")

    monkeypatch.setattr(sidecar_module, "fetch_served_models", handler)

    async def _scenario():
        app = build_app(registry=store, log_path=tmp_path / "events.db")
        assert app.state.sidecar_retry is None, "nothing may be armed"
        await asyncio.sleep(0.05)
        return app

    app = asyncio.run(_scenario())
    try:
        assert app.state.sidecar_pin.url is None
        assert app.state.sidecar_pin.notice is None
        assert store.path.read_text(encoding="utf-8") == before
        assert sidecar_module._RETRY_TASKS == set()
    finally:
        app.state.log.close()


def test_no_running_event_loop_arms_nothing_and_never_raises(tmp_path, monkeypatch):
    """A sync ``build_app`` (no loop) has no task to arm - and no exception."""
    store = make_store(tmp_path)
    monkeypatch.setenv(SIDECAR_URL_ENV, SIDECAR)

    def down(url, *, timeout=0.0, transport=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(sidecar_module, "fetch_served_models", down)

    boot = autopin_sidecar(store)  # deliberately synchronous
    assert boot.unreachable is True
    task = schedule_autopin_retry(store, boot, delay=0.01, attempts=2)
    assert task is None, "no running loop -> no task"
    assert sidecar_module._RETRY_TASKS == set()
    assert store.agents == {"narrator": "local"}


def test_success_at_boot_arms_no_deferred_task(tmp_path, monkeypatch):
    """A served sidecar (or an unset env) keeps today's single-shot boot."""
    store = make_store(tmp_path)
    monkeypatch.setenv(SIDECAR_URL_ENV, SIDECAR)
    monkeypatch.setattr(sidecar_module, "RETRY_DELAY_S", 0.01)
    attempts: list[int] = []

    def served(url, *, timeout=0.0, transport=None):
        attempts.append(1)
        return ALL_MODELS

    monkeypatch.setattr(sidecar_module, "fetch_served_models", served)

    async def _scenario():
        app = build_app(registry=store, log_path=tmp_path / "events.db")
        # Nothing is armed; wait past the retry delay to prove none fires.
        await asyncio.sleep(0.05)
        return app

    app = asyncio.run(_scenario())
    try:
        assert app.state.sidecar_retry is None, "a successful boot probes exactly once"
        assert len(attempts) == 1
        assert app.state.sidecar_pin.pinned == {
            "embedder": "sidecar-embed",
            "reranker": "sidecar-rerank",
            "decision": "sidecar-decision",
        }
        assert sidecar_module._RETRY_TASKS == set()
    finally:
        app.state.log.close()
