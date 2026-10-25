#!/usr/bin/env python3
"""Acceptance gate — the API KEY is a key on the user surface (real browser, real engine).

The owner's report: "Unable to set API key until connection saved; cannot query
models (required) prior to save. User has to manually populate a model, save,
enter key, save, then query model. Then I out of habit put the key into the
field instead of a var name. Because a var name is dumb for the user surface.
Take a key! ... Don't block the entry of this key on def of the connection."

This pass drives the LIVE connections drawer against an ISOLATED engine and a
stub OpenAI-compatible upstream it starts itself, and asserts the reported flow
works in ONE UNSAVED draft:

  base_url -> paste the key -> query models -> pick a model -> save

Assertions (every one through real trusted input, Playwright only — AGENTS.md
forbids synthetic ``.click()`` over raw CDP):

  1. the credential field of a NEW draft is ``API key`` — ``type=password``,
     ENABLED, with no "Save the connection first" wall and no second store-key
     form: the old sequence (save -> type key -> save -> query) is gone, and
     nothing about the key requires a prior save;
  2. "Discover models" on the unsaved draft sends the pasted key in the
     ``X-Fabulist-Key`` HEADER (never the query string: the engine's access log
     records the URL), the engine probes through to the stub upstream, the
     upstream SEES ``Authorization: Bearer <key>``, and the drawer renders the
     returned model ids — with nothing written to disk yet (no ``.env`` line,
     no connection entry);
  3. picking a chip fills the Model field, and saving creates the connection with
     ``api_key_env = FABULIST_<NAME>_KEY`` + ``api_key_set = true``, writes
     ``.env`` mode 600 with exactly that one line, never puts the secret in the
     connections document and never returns it in the save response;
  4. the env-var NAME is reachable but SECONDARY (inside a collapsed
     ``<details>``), and an edit that types no key keeps the stored name;
  5. zero page errors and no unexpected console errors.

Run against an ISOLATED pair (never the live :2727 DB — AGENTS.md). The engine's
``RegistryStore`` writes its ``.env`` beside its CWD, so run it FROM A SCRATCH
DIRECTORY (``uv run --project <repo>``) or a pasted test key lands in the real
project ``.env``::

  mkdir -p /tmp/fab_key_e2e && cd /tmp/fab_key_e2e
  printf '{"connections": {}, "agents": {}}' > connections.json
  FABULIST_DEV_ORIGINS=http://127.0.0.1:5210 \\
    uv run --project /path/to/repo python /path/to/repo/scripts/serve.py \\
      --config /tmp/fab_key_e2e/connections.json \\
      --log /tmp/fab_key_e2e/log.db --checkpoints /tmp/fab_key_e2e/ckpt.db \\
      --port 8110

  cd /path/to/repo/frontend
  VITE_API_BASE=http://127.0.0.1:8110 npm run dev -- --port 5210 --host 127.0.0.1

  cd .. && uv run --with playwright python scripts/connections_key_playwright.py

Kill by port afterwards (``fuser -k 8110/tcp``, ``fuser -k 5210/tcp``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:5210"
DEFAULT_API_URL = "http://127.0.0.1:8110"
DEFAULT_STUB_PORT = 8123
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

#: The drawer's trigger on the chat shell.
CONNECTIONS_BUTTON = 'button[aria-label^="Connections"]'

#: The literal this pass pastes. It must never appear in the document, a
#: response, or the engine's log.
PASTED_KEY = "sk-e2e-pasted-secret-123"

#: Console noise that is NOT a page error: the browser's favicon probe and the
#: dev server's HMR chatter.
BENIGN_CONSOLE: tuple[str, ...] = ("favicon", "[vite]")

log_lines: list[str] = []
SHOTS: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    log_lines.append(msg)


def ok(msg: str) -> None:
    print(f"  PASS  {msg}", flush=True)
    log_lines.append(f"  PASS  {msg}")


class GateError(AssertionError):
    pass


def check(condition: bool, msg: str) -> None:
    if not condition:
        raise GateError(msg)
    ok(msg)


def http_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - local
        return json.loads(response.read().decode() or "{}")


@dataclass(frozen=True)
class Run:
    """This pass's connection identity.

    ``env`` is asserted against the engine's own derivation rule: the connection
    name uppercased with every non-alphanumeric character replaced by ``_``,
    wrapped as ``FABULIST_<NAME>_KEY`` (``fabulist.connections.registry.api_key_env_name``).
    """

    name: str
    env: str


def derive_env_name(connection_name: str) -> str:
    """Mirror of the engine's ``api_key_env_name`` — asserted, not assumed."""
    return f"FABULIST_{re.sub(r'[^A-Z0-9]', '_', connection_name.upper())}_KEY"


def new_run() -> Run:
    """A unique name per run, so re-running against one isolated engine is clean."""
    name = f"key-flow-{int(time.time())}"
    return Run(name=name, env=derive_env_name(name))


def fetch_catalog(api_url: str) -> dict:
    return http_json(f"{api_url}/api/connections")


def config_text(config_path: Path) -> str:
    """The isolated document's text; "" before the first write (first run)."""
    try:
        return config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# --- the stub upstream: a real HTTP endpoint that records what it was sent -----


class StubUpstream:
    """A minimal OpenAI-compatible ``/v1/models`` endpoint.

    It records the ``Authorization`` header of every request, which is the proof
    that the key PASTED INTO THE UNSAVED DRAFT reached the probe: the engine sent
    it, the browser never talked to this server.
    """

    MODELS = ("stub-model-alpha", "stub-model-beta")

    def __init__(self, port: int) -> None:
        self.requests: list[dict[str, str | None]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                stub.requests.append(
                    {"path": self.path, "authorization": self.headers.get("Authorization")}
                )
                body = json.dumps(
                    {"object": "list", "data": [{"id": m} for m in stub.MODELS]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # silence the stub's own access log
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# --- browser helpers ----------------------------------------------------------


def open_drawer(page, base_url: str):
    """Open ``/chat`` and the Connections drawer; return the drawer locator.

    The header is SSR-rendered and visible BEFORE hydration, so a click in that
    window is swallowed by an un-hydrated button (the SSR-first trap). The click
    is retried until the drawer is on screen; no input is faked.
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
    button = page.locator(CONNECTIONS_BUTTON).first
    button.wait_for(state="visible", timeout=30_000)
    dialog = page.locator('div[role="dialog"][aria-label="Connections"]').first
    for _attempt in range(6):
        button.click()
        try:
            dialog.wait_for(state="visible", timeout=5_000)
            break
        except PWTimeout:
            page.wait_for_timeout(500)
    else:
        raise GateError("the connections drawer never opened (hydration or a render error)")
    dialog.locator(".list-head, .draft-view").first.wait_for(state="visible", timeout=20_000)
    return dialog


def shot(page, shots_dir: Path, name: str) -> None:
    path = shots_dir / name
    page.screenshot(path=str(path))
    SHOTS.append(str(path))
    log(f"  shot  {path}")


# --- phase 1: the credential field is a KEY, usable before any save -----------


def check_key_field_has_no_wall(dialog) -> None:
    form = dialog.locator("form.form").first
    form.wait_for(state="visible", timeout=15_000)
    key_input = form.locator('input[data-field="api_key"]')
    check(key_input.count() == 1, "the form has one `API key` field")
    check(
        key_input.get_attribute("type") == "password",
        "the key field is `type=password` (a secret is masked on screen)",
    )
    check(key_input.is_editable(), "the key field is EDITABLE in an unsaved draft")
    check(
        key_input.get_attribute("placeholder") == "paste the key — stored server-side",
        f"the field says what it takes ({key_input.get_attribute('placeholder')!r})",
    )
    text = " ".join(form.inner_text().split())
    check("API key" in text, "the field is labelled `API key`")
    check(
        "stored in .env, mode 600; never shown again" in text,
        "the helper states where the key goes and that it is never shown again",
    )
    # The old wall: a disabled field plus "Save the connection first".
    check(
        "save the connection first" not in text.lower(),
        "no 'Save the connection first' instruction remains anywhere in the form",
    )
    check(
        form.get_by_role("button", name="Store key").count() == 0,
        "the separate save-then-'Store key' form is gone (one credential surface)",
    )
    # The env-var NAME still exists, but secondarily.
    advanced = form.locator("details[data-advanced-key]")
    check(advanced.count() == 1, "the env-var name lives in an `Advanced` disclosure")
    check(
        advanced.evaluate("(el) => el.open === false"),
        "the disclosure starts COLLAPSED — the key is the primary surface",
    )
    check(
        form.locator('input[data-field="api_key_env"]').count() == 1,
        "the env-var NAME field is still reachable for env-var users",
    )


# --- phase 2: base_url + pasted key -> query models, nothing saved ------------


def probe_models_from_the_draft(page, dialog, run: Run, api_url: str, stub: StubUpstream,
                                env_file: Path, config_path: Path) -> None:
    form = dialog.locator("form.form").first
    form.locator('input[data-field="name"]').fill(run.name)
    base_input = form.locator('input[data-field="base_url"]')
    base_input.fill(stub.base_url)
    key_input = form.locator('input[data-field="api_key"]')
    key_input.fill(PASTED_KEY)
    check(
        base_input.input_value() == stub.base_url and key_input.input_value() == PASTED_KEY,
        "the unsaved draft holds the endpoint and the pasted key",
    )
    check(
        run.name not in fetch_catalog(api_url)["connections"],
        "nothing is saved yet — the draft exists only in the form",
    )

    seen: dict[str, object] = {}

    def on_request(request) -> None:
        if not request.url.split("?")[0].endswith("/models"):
            return
        seen["url"] = request.url
        seen["header"] = request.headers.get("x-fabulist-key")
        seen["method"] = request.method

    page.on("request", on_request)
    with page.expect_response(
        lambda response: response.url.split("?")[0].endswith("/models")
    ) as response_info:
        form.get_by_role("button", name="Discover models").click()
    response = response_info.value
    body = response.json()
    page.remove_listener("request", on_request)

    check(response.status == 200, f"the models probe answered 200 ({response.status})")
    check(seen.get("method") == "GET", "the probe is a GET on the models endpoint")
    check(
        seen.get("header") == PASTED_KEY,
        "the request carries the pasted key in the `X-Fabulist-Key` header",
    )
    check(
        "api_key" not in str(seen.get("url")),
        f"the key is NOT in the URL (an access log records it) — {seen.get('url')}",
    )
    check(body.get("api_key_set") is True, "the engine reports the probe authenticated")
    check(
        body.get("models") == list(stub.MODELS),
        f"the response parses the upstream's model ids ({body.get('models')})",
    )
    check(
        PASTED_KEY not in response.text(),
        "the probe response never echoes the key",
    )

    # The engine really used the key upstream (the browser never talked to it).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not stub.requests:
        time.sleep(0.05)
    check(bool(stub.requests), "the stub upstream received the probe")
    last = stub.requests[-1]
    check(
        last["path"] == "/v1/models",
        f"the engine asked the draft's endpoint for its models ({last['path']})",
    )
    check(
        last["authorization"] == f"Bearer {PASTED_KEY}",
        "the probe authenticated upstream with the PASTED key (Bearer <key>)",
    )

    # Nothing was persisted by a probe.
    check(
        not env_file.exists() or PASTED_KEY not in env_file.read_text(encoding="utf-8"),
        "the transient probe key never reached the .env file",
    )
    check(
        PASTED_KEY not in config_text(config_path),
        "the transient probe key never reached the connections document",
    )
    chips = form.locator("ul.models button.chip")
    chips.first.wait_for(state="visible", timeout=15_000)
    check(
        [text.strip() for text in chips.all_inner_texts()] == list(stub.MODELS),
        "the drawer renders the discovered models as pickable chips",
    )


# --- phase 3: pick a model, save once, the key is stored server-side ----------


def pick_model_and_save(page, dialog, run: Run, api_url: str, shots_dir: Path,
                        env_file: Path, config_path: Path) -> None:
    form = dialog.locator("form.form").first
    form.locator("ul.models button.chip").nth(1).click()
    picked = form.locator('input[data-field="model"]').input_value()
    check(
        picked == StubUpstream.MODELS[1],
        f"picking a chip fills the Model field ({picked})",
    )
    shot(page, shots_dir, "03-draft-ready.png")

    with page.expect_response(
        lambda response: response.request.method == "POST"
        and response.url.rstrip("/").endswith(f"/api/connections/{run.name}")
    ) as response_info:
        form.get_by_role("button", name="Save & ping").click()
    saved_response = response_info.value
    saved_body = saved_response.json()["connection"]
    check(saved_response.status == 201, f"the save created the connection ({saved_response.status})")
    check(PASTED_KEY not in json.dumps(saved_body), "the save response never contains the key")
    check(
        saved_body.get("api_key_env") == run.env,
        f"the response names the derived env var ({saved_body.get('api_key_env')})",
    )
    check(saved_body.get("api_key_set") is True, "the response reports `api_key_set: true`")

    form.wait_for(state="detached", timeout=60_000)
    status = dialog.locator("p.status").first
    status.wait_for(state="visible", timeout=60_000)
    status_text = " ".join(status.inner_text().split())
    check("Key set" in status_text, f"the drawer confirms the key after save ({status_text!r})")
    shot(page, shots_dir, "04-saved.png")

    entry = fetch_catalog(api_url)["connections"][run.name]
    check(entry["api_key_env"] == run.env, f"GET reports `{run.env}`")
    check(entry["api_key_set"] is True, "GET reports the key as set")
    check(entry["model"] == StubUpstream.MODELS[1], "the picked model was saved")

    lines = [line for line in env_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    check(
        lines == [f"{run.env}={PASTED_KEY}"],
        f"the .env holds exactly the derived line ({lines})",
    )
    check(
        stat.S_IMODE(env_file.stat().st_mode) == 0o600,
        f"the .env is mode 600 ({oct(stat.S_IMODE(env_file.stat().st_mode))})",
    )
    document = json.loads(config_path.read_text(encoding="utf-8"))
    check(
        document["connections"][run.name]["api_key_env"] == run.env,
        "the connections document carries the NAME only",
    )
    check(
        PASTED_KEY not in config_text(config_path),
        "the key is nowhere in the connections document",
    )


# --- phase 4: editing without typing a key keeps the stored one ---------------


def check_edit_keeps_the_key(dialog, run: Run, api_url: str) -> None:
    card = None
    cards = dialog.locator("li.connection")
    for index in range(cards.count()):
        if cards.nth(index).locator(".conn-name").inner_text().strip() == run.name:
            card = cards.nth(index)
            break
    check(card is not None, f"the new connection appears in the list ({run.name})")
    card.get_by_role("button", name="Edit").click()
    form = dialog.locator("form.form").first
    form.wait_for(state="visible", timeout=15_000)
    key_input = form.locator('input[data-field="api_key"]')
    check(key_input.is_editable(), "the key field is editable while editing too")
    check(key_input.input_value() == "", "the stored key is NOT read back into the field")
    key_state = form.locator("[data-key-state]")
    key_state.wait_for(state="visible", timeout=10_000)
    state_text = " ".join(key_state.inner_text().split())
    check(
        run.env in state_text and "set" in state_text,
        f"the form shows the resolved name + state ({state_text!r})",
    )
    form.get_by_role("button", name="Save & ping").click()
    form.wait_for(state="detached", timeout=60_000)
    after = fetch_catalog(api_url)["connections"][run.name]
    check(
        after["api_key_env"] == run.env and after["api_key_set"] is True,
        "saving with an empty key field keeps the stored key",
    )


# --- main ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="frontend origin (engine-backed)")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="isolated engine origin")
    parser.add_argument("--stub-port", type=int, default=DEFAULT_STUB_PORT)
    parser.add_argument(
        "--env-file",
        default="/tmp/fab_key_e2e/.env",
        help="the ISOLATED engine's .env (created beside its CWD)",
    )
    parser.add_argument(
        "--config",
        default="/tmp/fab_key_e2e/connections.json",
        help="the ISOLATED engine's connections document",
    )
    parser.add_argument("--shots-dir", default="/tmp/fab_key_e2e/shots")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--keep", action="store_true", help="leave the created connection in place")
    args = parser.parse_args()

    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    if not Path(CHROME).exists():
        log(f"ERROR: chromium not found at {CHROME} (set FABULIST_CHROME)")
        return 2
    env_file = Path(args.env_file)
    config_path = Path(args.config)  # absent on a first-run engine: created on save
    shots_dir = Path(args.shots_dir)
    shots_dir.mkdir(parents=True, exist_ok=True)

    run = new_run()
    stub = StubUpstream(args.stub_port)
    stub.start()

    errors: list[str] = []
    page_errors: list[str] = []
    failures = 0
    created = False
    try:
        catalog = fetch_catalog(args.api_url)
        check(
            run.name not in catalog["connections"],
            f"the engine starts without this run's connection `{run.name}` (re-runnable)",
        )
        if env_file.exists() and f"{run.env}=" in env_file.read_text(encoding="utf-8"):
            log(f"ERROR: {env_file} already holds `{run.env}` — clear it first")
            return 2

        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=CHROME, args=["--no-sandbox"], headless=not args.headed
            )
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 950})
                page.set_default_timeout(30_000)

                def on_console(message) -> None:
                    if message.type != "error":
                        return
                    if any(benign in message.text for benign in BENIGN_CONSOLE):
                        return
                    # The save ping probes again; a failure there is surfaced in the
                    # drawer's own status line (asserted above).
                    if "Failed to load resource" in message.text and "/models" in message.text:
                        return
                    errors.append(f"console: {message.text}")

                page.on("console", on_console)
                page.on("pageerror", lambda err: page_errors.append(str(err)))

                dialog = open_drawer(page, args.base_url)
                dialog.get_by_role("button", name="+ New connection").click()
                check_key_field_has_no_wall(dialog)
                shot(page, shots_dir, "02-new-draft.png")
                probe_models_from_the_draft(
                    page, dialog, run, args.api_url, stub, env_file, config_path
                )
                shot(page, shots_dir, "02b-models.png")
                pick_model_and_save(
                    page, dialog, run, args.api_url, shots_dir, env_file, config_path
                )
                created = True
                check_edit_keeps_the_key(dialog, run, args.api_url)

                check(not page_errors, f"zero page errors ({page_errors[:3]})")
                check(not errors, f"no unexpected console errors ({errors[:3]})")
            except (GateError, PWTimeout) as err:
                log(f"  FAIL  {err}")
                failures = 1
            finally:
                browser.close()
    except (urllib.error.URLError, KeyError, GateError) as err:
        log(f"ERROR: preflight failed: {err}")
        failures = 1
    finally:
        stub.stop()
        if created and not args.keep:
            try:
                http_json(f"{args.api_url}/api/connections/{run.name}", "DELETE")
                log(f"deleted the pass's connection {run.name!r}")
            except urllib.error.HTTPError as err:
                # The engine refuses to leave a document with NO connections
                # behind (that shape is neither first-run nor valid) — an
                # isolated throwaway config keeps this run's entry.
                log(f"  note  the isolated engine kept {run.name!r}: {err}")

    log("")
    log(f"screenshots: {', '.join(SHOTS) or '(none)'}")
    if failures:
        log("RESULT: FAIL")
        return 1
    log("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
