"""Issue #80 live gate — the ↻ on a FAILED turn forks at THAT turn's user seq.

Issue #80 shipped two approved tests that were never written. The merged
behaviour (fix ``3de7959``, refined by #81's ``7e779e0``) has two halves, and
this gate pins both in ONE end-to-end run:

  a. ``retry()`` in ``frontend/src/routes/chat/+page.svelte`` re-reads the
     transcript UNCONDITIONALLY before it forks (it used to refresh only while
     ``connection`` sat in ``error``), so ``lastUserSeq`` names the message the
     reader actually acted on — the FAILED turn's user row;
  b. that same trailing UNANSWERED user row is the row that owns the ↻
     (``branchRowIndex`` → ``branchOwner`` in ``TranscriptList.svelte``): a live
     failure commits the user row and NO narrative, so the "last reply" is the
     PREVIOUS turn's.

The old bug forked at the PREVIOUS turn's user seq (rewinding the story one
turn). So the pin is one engine-side equality:

    the new branch's ``base_seq`` == the trailing user event's seq

which is exactly "the fork carried the FAILED turn's user seq, not the previous
turn's". The mapping onto the two approved tests is deliberate and explicit:

  * **"a turn-level failure, not a connection error"** — the narrator's
    connection is REACHABLE and answers HTTP 500, so the turn dies at the model
    (a pre-commit provider abort: the user row is in the log, no narrative) and
    nothing about the failure is a dead endpoint. Asserted engine-side: turn 2's
    user row exists, no narrative row follows it, and the UI renders the failure
    on that turn (``p.error`` + the empty reply placeholder).
  * **"the trailing unanswered user row sets ``lastUserSeq``"** — the fork cut is
    read back from the log (``sessions.base_seq``), from the fork response body,
    and from the ``POST /fork`` request body on the wire: all three must name the
    trailing user seq of the SOURCE session, and must NOT name any earlier user
    seq (the negative is implied by the equality; it is asserted anyway because
    it is free here, no extra browser time).

Everything is isolated: a throwaway engine on a spare port over a throwaway
``--log``/``--checkpoints`` under ``/tmp``, a throwaway connections document
whose every agent slot points at a stub OpenAI-compatible server started inside
this gate, and its own vite dev server on a spare port. Nothing under ``data/``
or ``configs/`` is touched.

Playwright only — real trusted input, actionability waits, strict locators
(AGENTS.md: never synthetic ``.click()`` over raw CDP).

Run it (it is skipped unless the env target is present, so ``uv run pytest -q``
stays green with no browser)::

    FABULIST_LIVE_UI=1 uv run --with playwright pytest \\
        tests/test_regenerate_gate_live.py -q -s

or as a plain script::

    uv run --with playwright python tests/test_regenerate_gate_live.py

Ports default to 8144 (engine) / 5244 (vite) / 8145 (stub) and can be moved with
``FABULIST_GATE_API_PORT`` / ``FABULIST_GATE_VITE_PORT`` / ``FABULIST_GATE_STUB_PORT``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

#: The env target: without it the whole module is skipped (no browser in the suite).
LIVE_ENV = "FABULIST_LIVE_UI"

API_PORT = int(os.environ.get("FABULIST_GATE_API_PORT", "8144"))
VITE_PORT = int(os.environ.get("FABULIST_GATE_VITE_PORT", "5244"))
STUB_PORT = int(os.environ.get("FABULIST_GATE_STUB_PORT", "8145"))

WORK = Path(os.environ.get("FABULIST_GATE_WORK", "/tmp/regenerate-gate-80"))

#: The stub's model id, echoed back so a log row can be traced to this gate.
STUB_MODEL = "stub-narrator"
#: The prose the stub returns while it is answering.
REPLY_PROSE = "Rain drums on the shutters and the lamp holds."
#: What the stub says when it refuses — a provider fault, NOT a dead endpoint.
STUB_ERROR = "stub upstream exploded"
PROVIDER_STATUS = 500

TURN_1 = "I step inside and close the door."
TURN_2 = "I cross the room and wait by the lamp."

COMPOSER = "form.composer textarea"
SEND_BUTTON = "form.composer button.send"
STOP_BUTTON = "form.composer button.stop"
ERROR_LINE = ".transcript article.turn p.error"
RAIL_STATUS = ".sidebar .status"
RAIL_ERROR = ".sidebar .connection-error"
REGEN_NAME = "Generate another reply"

#: Console noise that is not a page defect (the browser's favicon probe, HMR).
BENIGN_CONSOLE = ("favicon", "[vite]")

pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_ENV) != "1",
    reason=f"set {LIVE_ENV}=1 to run the live #80 browser gate (needs vite + chromium)",
)


# ---------------------------------------------------------------------------
# the stub OpenAI-compatible server (it refuses; it is never unreachable)
# ---------------------------------------------------------------------------


class StubProvider:
    """A tiny OpenAI-compatible endpoint whose ``/chat/completions`` says 500.

    The point of a 500 over a dead port is the DIFFERENCE the engine draws: the
    connection answers, so the failure is the PROVIDER refusing, and the turn
    aborts pre-commit (user row kept, no narrative). A connection error would be
    a different shape entirely.

    ``fail`` starts False because the chat has to own ONE settled turn before the
    gate can tell "the failed turn's user seq" from "the previous turn's": a
    settled turn is what seeds the stale pointer the bug left behind. The gate
    flips it (in-process — the stub lives in this process) around turn 2.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.fail = False
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # silence per-request logs
                return

            def _json(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                if self.path.startswith("/v1/models"):
                    self._json(
                        200,
                        {"object": "list", "data": [{"id": STUB_MODEL, "object": "model"}]},
                    )
                    return
                self._json(404, {"error": {"message": "no such route"}})

            def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                messages = body.get("messages") or [{}]
                head = messages[0].get("content")
                with stub._lock:
                    stub.calls.append(
                        {
                            "path": self.path,
                            "stream": bool(body.get("stream")),
                            "structured": bool(body.get("response_format")),
                            "model": body.get("model"),
                            "prompt_head": (head[:80] if isinstance(head, str) else "<parts>"),
                            "refused": stub.fail,
                        }
                    )
                    refusing = stub.fail
                if not self.path.endswith("/chat/completions"):
                    self._json(404, {"error": {"message": "no such route"}})
                    return
                if refusing:
                    self._json(
                        PROVIDER_STATUS,
                        {"error": {"message": STUB_ERROR, "type": "server_error"}},
                    )
                    return
                # A structured call (the extractor, a lane) must get JSON back; the
                # narrator gets prose. One tolerance, no per-agent routing.
                content = (
                    json.dumps({"deltas": {}, "action_class": "dialogue"})
                    if body.get("response_format")
                    else REPLY_PROSE
                )
                if body.get("stream"):
                    self._stream(content)
                    return
                self._json(
                    200,
                    {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion",
                        "created": 0,
                        "model": STUB_MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
                    },
                )

            def _stream(self, content: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for index in range(0, len(content), 24):
                    chunk = {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": STUB_MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": content[index : index + 24]},
                                "finish_reason": None,
                            }
                        ],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                final = {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": STUB_MODEL,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True

    def start(self) -> None:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()

    def arm(self, fail: bool) -> None:
        with self._lock:
            self.fail = fail

    @property
    def refusals(self) -> int:
        with self._lock:
            return sum(1 for call in self.calls if call["refused"])


# ---------------------------------------------------------------------------
# process / protocol helpers (the msgdel + provider_error harness pattern)
# ---------------------------------------------------------------------------


def _free(port: int) -> None:
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, check=False)


def _wait_http(url: str, *, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - local
                if resp.status < 500:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {url}")


def _wait_port(port: int, *, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for :{port}")


def _post_json(url: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310 - local
        return json.load(resp)


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310 - local
        return json.load(resp)


def _write_connections(path: Path, stub_port: int) -> Path:
    """The throwaway connections document: EVERY agent slot answers 500.

    The mechanism is the one the tree uses for live tests (``tests/conftest.py``
    has no binding at all; ``tests/test_agent_loop.py``'s live pass rewrites the
    ``agents`` map of a scratch copy of the connections document): a
    ``connections`` entry plus ``agents`` slots naming it. Declared here so the
    first model call of any phase meets the stub — the turn then fails at the
    MODEL, whatever phase reaches it first.
    """
    document = {
        "connections": {
            "stub": {
                "base_url": f"http://127.0.0.1:{stub_port}/v1",
                "model": STUB_MODEL,
                "timeout": 30,
            }
        },
        "agents": {
            "narrator": "stub",
            "extractor": "stub",
            "captioner": "stub",
            "summarize": "stub",
            "critic": "stub",
            "planner": "stub",
        },
    }
    path.write_text(json.dumps(document, indent=2))
    return path


def _start_engine(config: Path, log_path: Path, checkpoints: Path, work: Path):
    """Boot the REAL ``scripts/serve.py`` engine on a spare port (throwaway paths)."""
    env = dict(os.environ)
    # CORS defaults to :5173 only; this vite is on a spare port (AGENTS.md).
    env["FABULIST_DEV_ORIGINS"] = (
        f"http://localhost:{VITE_PORT},http://127.0.0.1:{VITE_PORT}"
    )
    # Never read the checkout's own dotenv from an isolated run.
    env["FABULIST_ENV_FILE"] = str(work / ".env")
    (work / ".env").write_text("")
    handle = open(work / "engine.log", "wb")  # noqa: SIM115 - kept for the child's life
    proc = subprocess.Popen(
        [
            sys.executable,
            "scripts/serve.py",
            "--config",
            str(config),
            "--log",
            str(log_path),
            "--checkpoints",
            str(checkpoints),
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return proc, handle


def _start_vite(work: Path):
    """The real dev server over this checkout's working tree, on a spare port."""
    env = dict(os.environ)
    env["VITE_API_BASE"] = f"http://127.0.0.1:{API_PORT}"
    handle = open(work / "vite.log", "wb")  # noqa: SIM115 - kept for the child's life
    proc = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(VITE_PORT), "--host", "127.0.0.1"],
        cwd=FRONTEND,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return proc, handle


# ---------------------------------------------------------------------------
# the log, read-only (the throwaway DB is ours: nothing here writes)
# ---------------------------------------------------------------------------


class LogReader:
    """Read-only view of the gate's throwaway event log."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        import sqlite3

        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return list(connection.execute(sql, params))
        finally:
            connection.close()

    def user_events(self, session_id: str) -> list[dict]:
        """The session's OWN user rows, in seq order — ``[{id, seq, text}]``."""
        rows = self._query(
            "SELECT id, seq, payload FROM events "
            "WHERE session_id = ? AND type = 'user' AND ephemeral = 0 ORDER BY seq",
            (session_id,),
        )
        events = []
        for row_id, seq, payload in rows:
            text = ""
            try:
                text = str(json.loads(payload).get("text") or "")
            except ValueError:
                pass
            events.append({"id": row_id, "seq": seq, "text": text})
        return events

    def narratives_after(self, session_id: str, seq: int) -> list[dict]:
        rows = self._query(
            "SELECT id, seq, payload FROM events "
            "WHERE session_id = ? AND type = 'narrative' AND seq > ? ORDER BY seq",
            (session_id, seq),
        )
        return [{"id": row_id, "seq": row_seq} for row_id, row_seq, _ in rows]

    def session_row(self, session_id: str) -> dict:
        """``sessions`` bookkeeping: the fork pointer the engine recorded."""
        rows = self._query(
            "SELECT session_id, base_session, base_seq FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        if not rows:
            return {}
        return {"session_id": rows[0][0], "base_session": rows[0][1], "base_seq": rows[0][2]}

    def poll_user_events(self, session_id: str, count: int, *, timeout: float = 180.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.user_events(session_id)
            if len(events) >= count:
                return events
            time.sleep(0.2)
        raise RuntimeError(
            f"timed out waiting for {count} user rows on {session_id} "
            f"(got {len(self.user_events(session_id))})"
        )

    def poll_narrative(self, session_id: str, *, timeout: float = 180.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = self._query(
                "SELECT id, seq FROM events "
                "WHERE session_id = ? AND type = 'narrative' ORDER BY seq",
                (session_id,),
            )
            if rows:
                return {"id": rows[0][0], "seq": rows[0][1]}
            time.sleep(0.2)
        raise RuntimeError(f"timed out waiting for a narrative row on {session_id}")


# ---------------------------------------------------------------------------
# browser-side readers
# ---------------------------------------------------------------------------


def _sid(page) -> str | None:
    return page.evaluate("() => new URL(location.href).searchParams.get('s')")


def _rows(page) -> list[dict]:
    """Transcript rows in reading order, with the affordances each row carries."""
    return page.evaluate(
        """(regen) => Array.from(document.querySelectorAll('.transcript article.turn')).map((a) => ({
            id: a.id || null,
            user: a.classList.contains('user'),
            text: (a.textContent || '').replace(/\\s+/g, ' ').trim(),
            errors: a.querySelectorAll('p.error').length,
            retry: a.querySelectorAll(`button[aria-label="${regen}"]`).length,
            streaming: a.querySelectorAll('.streaming').length
        }))""",
        REGEN_NAME,
    )


def _rail(page) -> dict:
    """The rail's connection surface, recorded as evidence (never asserted)."""
    status = page.locator(RAIL_STATUS)
    cause = page.locator(RAIL_ERROR)
    return {
        "status_text": (status.first.text_content() or "").strip() if status.count() else "",
        "status_state": (status.first.get_attribute("data-status") or "")
        if status.count()
        else "",
        "cause": (cause.first.text_content() or "").strip() if cause.count() else "",
    }


def _send(page, text: str) -> None:
    page.fill(COMPOSER, text)
    page.click(SEND_BUTTON)


def _settled(page, *, timeout: int = 180_000) -> None:
    """Wait until the turn's stop door is gone (the turn finished, kept or not)."""
    page.wait_for_selector(STOP_BUTTON, state="detached", timeout=timeout)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


class Report:
    """PASS/FAIL table plus ``evidence.json`` — the run's transcript."""

    def __init__(self, work: Path) -> None:
        self.work = work
        self.checks: list[dict] = []
        self.evidence: dict[str, object] = {}

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        verdict = "PASS" if ok else "FAIL"
        print(f"  {verdict}  {name}" + (f" — {detail}" if detail else ""), flush=True)

    @property
    def failures(self) -> list[dict]:
        return [item for item in self.checks if not item["ok"]]

    def write(self) -> None:
        (self.work / "evidence.json").write_text(
            json.dumps(
                {"checks": self.checks, "evidence": self.evidence}, indent=2, default=str
            )
        )


def _run_gate() -> int:
    """Drive the whole live pass; return a process exit code (0 = green)."""
    if not Path(CHROME).is_file():
        print(f"SKIP: no chromium at {CHROME} (set FABULIST_CHROME)", flush=True)
        return 0
    if not (FRONTEND / "node_modules").exists():
        print("SKIP: frontend/node_modules missing (npm install in frontend/)", flush=True)
        return 0
    if shutil.which("npm") is None:
        print("SKIP: npm not on PATH", flush=True)
        return 0

    from playwright.sync_api import sync_playwright

    work = WORK
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / "events.db"
    checkpoints = work / "checkpoints.db"
    for stale in (log_path, checkpoints, Path(f"{log_path}-wal"), Path(f"{log_path}-shm")):
        stale.unlink(missing_ok=True)

    report = Report(work)
    page_errors: list[str] = []
    console_errors: list[str] = []
    api_5xx: list[str] = []
    fork_requests: list[dict] = []
    fork_responses: list[dict] = []

    _free(API_PORT)
    _free(VITE_PORT)
    stub = StubProvider(STUB_PORT)
    stub.start()
    config = _write_connections(work / "connections.json", STUB_PORT)
    engine, engine_log = _start_engine(config, log_path, checkpoints, work)
    vite = None
    vite_log = None
    browser = None
    try:
        vite, vite_log = _start_vite(work)
        _wait_http(f"http://127.0.0.1:{API_PORT}/api/sessions")
        _wait_port(VITE_PORT)
        log = LogReader(log_path)
        base = f"http://127.0.0.1:{VITE_PORT}"
        api = f"http://127.0.0.1:{API_PORT}"

        source_sid = _post_json(f"{api}/api/sessions", {"title": "Regenerate gate #80"})[
            "session_id"
        ]
        report.evidence["source_session"] = source_sid
        print(f"source session: {source_sid}", flush=True)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(60_000)
            page.set_default_navigation_timeout(120_000)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )

            def on_request(request) -> None:
                if request.method == "POST" and request.url.endswith("/fork"):
                    fork_requests.append({"url": request.url, "body": request.post_data})

            def on_response(response) -> None:
                if "/api/" in response.url and response.status >= 500:
                    api_5xx.append(f"HTTP {response.status} {response.url}")
                if response.url.endswith("/fork"):
                    try:
                        body = response.json()
                    except Exception:  # noqa: BLE001 - evidence only
                        body = None
                    fork_responses.append(
                        {"url": response.url, "status": response.status, "body": body}
                    )

            page.on("request", on_request)
            page.on("response", on_response)

            # -- step 1: ONE settled turn, so the stale pointer has a value ------
            # This is what makes the bug observable at all: `lastUserSeq` now names
            # THIS turn's user message. If the failed turn's refresh is missing
            # (#80's bug), the fork below cuts here instead of at turn 2.
            page.goto(f"{base}/chat?s={source_sid}", wait_until="domcontentloaded")
            page.wait_for_selector(COMPOSER, state="visible", timeout=120_000)
            _send(page, TURN_1)
            first_narrative = log.poll_narrative(source_sid)
            _settled(page)
            page.wait_for_function(
                """(prose) => Array.from(document.querySelectorAll('.transcript article.turn'))
                    .some((a) => (a.textContent || '').includes(prose))""",
                arg=REPLY_PROSE[:30],
                timeout=120_000,
            )
            user_1 = log.poll_user_events(source_sid, 1)[0]
            report.evidence["turn_1"] = {
                "user": user_1,
                "narrative": first_narrative,
                "rows": _rows(page),
            }
            page.screenshot(path=str(work / "01-turn-1-settled.png"), full_page=True)
            report.check(
                "the seed turn settled (user row + narrative row, no error on the transcript)",
                user_1["text"] == TURN_1
                and first_narrative["seq"] > user_1["seq"]
                and page.locator(ERROR_LINE).count() == 0,
                f"user seq {user_1['seq']}, narrative seq {first_narrative['seq']}",
            )

            # -- step 2: the provider starts refusing; turn 2 fails AT THE MODEL --
            stub.arm(True)
            _send(page, TURN_2)
            users = log.poll_user_events(source_sid, 2)
            page.wait_for_selector(ERROR_LINE, timeout=180_000)
            _settled(page)
            failed_user = users[-1]
            failure_text = (page.locator(ERROR_LINE).first.text_content() or "").strip()
            stragglers = log.narratives_after(source_sid, failed_user["seq"])
            rail_after_failure = _rail(page)
            rows_after_failure = _rows(page)
            report.evidence["turn_2"] = {
                "user": failed_user,
                "failure_text": failure_text,
                "narrative_after": stragglers,
                "rows": rows_after_failure,
                "rail": rail_after_failure,
                "stub_refusals": stub.refusals,
            }
            page.screenshot(path=str(work / "02-failed-turn.png"), full_page=True)
            # The FAILURE SHAPE (approved test #1, "turn-level failure, no dead
            # connection"): the user row is IN the log, no narrative answers it,
            # the provider ANSWERED (HTTP 500) instead of being unreachable, and
            # the transcript says so on that turn.
            report.check(
                "the failed turn kept its user row in the log and committed no narrative",
                failed_user["text"] == TURN_2 and stragglers == [],
                f"user seq {failed_user['seq']}, narratives after it: {len(stragglers)}",
            )
            report.check(
                "the failure came from the reachable provider (a 500 refusal, not a dead endpoint)",
                stub.refusals > 0 and str(PROVIDER_STATUS) in failure_text,
                f"{stub.refusals} refused model calls; transcript line: {failure_text!r}",
            )

            # -- step 3: the ↻ belongs to the trailing UNANSWERED user row ------
            # #81's half of the contract: a failed turn's empty reply placeholder
            # must not take the controls, or the ↻ would regenerate the previous
            # turn's reply (whose group the page happens to hold).
            controls = page.get_by_role("button", name=REGEN_NAME, exact=True)
            owner = [row for row in rows_after_failure if row["retry"]]
            tail = rows_after_failure[-1]
            report.check(
                "exactly ONE ↻, and it sits on the trailing user row that the failure left unanswered",
                controls.count() == 1
                and len(owner) == 1
                and owner[0]["user"]
                and owner[0]["id"] == tail["id"]
                and TURN_2 in owner[0]["text"],
                f"owner row: {owner[0] if owner else None}",
            )

            # -- step 4: click it (real trusted input; strict locator) ----------
            # The provider answers again, so the forked regenerate turn can SUCCEED
            # and the page can switch to the branch (the fork itself is what the
            # assertion is about; the switch is the UI-visible completion).
            stub.arm(False)
            controls.first.click()
            deadline = time.time() + 180
            while time.time() < deadline and not fork_responses:
                page.wait_for_timeout(100)
            branch_sid = None
            page.wait_for_function(
                "(source) => new URL(location.href).searchParams.get('s') !== source",
                arg=source_sid,
                timeout=180_000,
            )
            branch_sid = _sid(page)
            report.evidence["fork"] = {
                "requests": fork_requests,
                "responses": fork_responses,
                "branch_session": branch_sid,
            }
            page.screenshot(path=str(work / "03-branch-switch.png"), full_page=True)
            report.check(
                "the click forked and the page switched to the new branch",
                branch_sid is not None and branch_sid != source_sid,
                f"branch {branch_sid}",
            )

            # -- step 5: THE PIN — the fork cut at the FAILED turn's user seq ----
            # One equality covers both approved tests: the branch carried the
            # failed (trailing, unanswered) user row's seq, and NOT the previous
            # settled turn's — the rewind the bug produced.
            source_row = log.session_row(source_sid)
            branch_row = log.session_row(branch_sid or "")
            user_seqs = [event["seq"] for event in users]
            previous_user_seq = user_seqs[-2] if len(user_seqs) > 1 else None
            resolved = None
            resolve_deadline = time.time() + 30
            while time.time() < resolve_deadline and not branch_row.get("base_session"):
                time.sleep(0.2)
                branch_row = log.session_row(branch_sid or "")
            fork_body = (fork_requests[-1]["body"] if fork_requests else "") or ""
            try:
                wire_at = json.loads(fork_body).get("at_seq")
            except ValueError:
                wire_at = None
            report.evidence["pin"] = {
                "trailing_user_seq": failed_user["seq"],
                "previous_user_seq": previous_user_seq,
                "branch_base_session": branch_row.get("base_session"),
                "branch_base_seq": branch_row.get("base_seq"),
                "source_base_session": source_row.get("base_session"),
                "fork_wire_at_seq": wire_at,
                "fork_response": fork_responses[-1] if fork_responses else None,
            }
            report.check(
                "the branch's base_seq is the FAILED turn's user seq (not the previous turn's)",
                branch_row.get("base_session") == source_sid
                and branch_row.get("base_seq") == failed_user["seq"]
                and (previous_user_seq is None or branch_row.get("base_seq") != previous_user_seq),
                f"base_seq {branch_row.get('base_seq')} vs trailing user seq "
                f"{failed_user['seq']} (previous turn's user seq {previous_user_seq})",
            )
            report.check(
                "the wire agrees: POST /fork carried at_seq = the failed turn's user seq",
                wire_at == failed_user["seq"]
                and bool(fork_responses)
                and (fork_responses[-1]["body"] or {}).get("base_seq") == failed_user["seq"],
                f"at_seq {wire_at}, response base_seq "
                f"{(fork_responses[-1]['body'] if fork_responses else {})}",
            )
            # Free (no browser time): the regenerated turn carried the RIGHT text.
            branch_users = log.poll_user_events(branch_sid or "", 1)
            report.check(
                "the regenerated turn re-sent the FAILED turn's own message",
                branch_users[-1]["text"] == TURN_2,
                f"branch user text {branch_users[-1]['text']!r}",
            )

            if page_errors:
                report.check("no page errors", False, f"{page_errors}")
            else:
                report.check("no page errors", True)
            noisy = [
                text
                for text in console_errors
                if not any(benign in text for benign in BENIGN_CONSOLE)
            ]
            report.check("no console errors", not noisy, f"{noisy[:3]}")

            browser.close()
            browser = None

        report.evidence["stub_calls"] = stub.calls
        report.evidence["api_5xx"] = api_5xx
        report.write()

        print("\n=== transcript ===", flush=True)
        print(f"  turn 1: user seq {report.evidence['turn_1']['user']['seq']} -> narrative", flush=True)
        print(
            f"  turn 2: user seq {failed_user['seq']} FAILED "
            f"({stub.refusals} provider refusals, rail {rail_after_failure['status_state']!r})",
            flush=True,
        )
        print(
            f"  ↻ click -> POST /fork at_seq={wire_at} -> branch {branch_sid} "
            f"base_seq={branch_row.get('base_seq')}",
            flush=True,
        )
        print(f"  evidence: {work / 'evidence.json'}", flush=True)

        failures = report.failures
        if failures:
            print("\nFAILURES", flush=True)
            for item in failures:
                print(f"  - {item['name']}: {item['detail']}", flush=True)
            return 1
        print("\nOK: the ↻ on the failed turn forked at that turn's own user seq (#80)", flush=True)
        return 0
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - teardown only
                pass
        for proc in (vite, engine):
            if proc is None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for handle in (vite_log, engine_log):
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001 - teardown only
                    pass
        # Backstop: the two spawned servers are named by port (never `pkill -f`).
        # The stub is IN THIS PROCESS, so it is stopped through its own handle.
        _free(VITE_PORT)
        _free(API_PORT)
        stub.stop()
        report.write()
        sys.stdout.flush()
        sys.stderr.flush()


def test_the_regenerate_control_forks_at_the_failed_turns_user_seq() -> None:
    """Issue #80 end to end: the failed turn's ↻ forks at THAT turn's user seq."""
    assert _run_gate() == 0, "the live #80 gate reported failures (see the transcript above)"


if __name__ == "__main__":
    os.environ.setdefault(LIVE_ENV, "1")
    raise SystemExit(_run_gate())
