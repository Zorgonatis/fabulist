#!/usr/bin/env python3
"""D-C acceptance gate — the live transcript's FINAL state equals a reload.

The defect (owner-verified, session ``bad71c7af``, narrative 4559): the narrator
streamed a draft whose DSL the engine's gate then REWROTE (that turn: the draft
was rejected and re-synthesized — ``narrator usage`` twice, the second a 1233-token
repair prompt). Nothing on the stream carries the repaired text (only the critic
emits a ``revision`` frame, and the critic was skipped there), so the client kept
rendering the DRAFT: ``phone_signal = Phone(…)`` stayed raw text on screen while a
browser refresh — which rebuilds the message model from the committed payload —
rendered the panel. Client state, not persistence.

This harness reproduces that seam end to end, deterministically, with no model:
an isolated ``create_app`` engine streams a draft, appends the committed
(repaired) narrative to the real event log, then emits ``committed``/``done``.
The real frontend (own vite dev server) drives a turn with real input and the
pass asserts:

  1. after the stream, WITHOUT a reload, the committed component renders
     (``section[data-openui="Phone"]``) and the transcript holds no raw-DSL
     ``<pre class="raw">`` — the draft's invalid statement must have been
     replaced by the committed one;
  2. the composed case: the child variable and ``root = ScenePane(…)`` are in
     SEPARATE segments (the shape that stranded the live defect), and the panel
     still mounts;
  3. a reload produces the SAME render markers as the live view — the pin on
     "final state == reload state";
  4. no page errors and no console errors.

Playwright only (real trusted input, actionability waits) — never synthetic
``.click()`` over raw CDP (AGENTS.md).

Run::

  uv run --with playwright python scripts/dc_transcript_final_state_playwright.py

  # --shots DIR     : screenshot directory (default /tmp/dc_final_state)
  # --api-port N    : isolated engine port (default 8120)
  # --vite-port N   : vite dev-server port (default 5210)
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

#: The composite the client must render live (the ``bad71c7af`` shape): a child
#: statement and the structural root in SEPARATE segments.
PROSE = "Tessa holds the phone above her head, hunting for a signal that is not there."

#: The committed panel statement — the exact argument shape the persisted
#: narrative 4559 carries (it renders in the browser).
PHONE = (
    'phone_signal = Phone("Kick Notifications", '
    '[{"from": "System", "text": "Searching for network...", "time": "08:21", '
    '"status": "failed", "day": "Monday"}, '
    '{"from": "System", "text": "No Service", "time": "08:22", "status": "error", '
    '"day": "Monday"}], {"glyph": "📶", "battery": 14})'
)
ROOT = 'root = ScenePane("Morning Mist in the Glade", [phone_signal])'
COMMITTED_TEXT = f"{PROSE}\n\n{PHONE}\n\n{ROOT}"

#: What the model actually streamed first: the same statement with keyword
#: arguments, which openui lang rejects — the class the engine's gate repairs
#: (deterministic kwargs→positional, or a resynthesis pass). The client renders
#: a rejected payload as raw text.
DRAFT_TEXT = (
    f"{PROSE}\n\n"
    'phone_signal = Phone("Kick Notifications", '
    '[{"from": "System", "text": "No Service", "time": "08:22", "status": "error", '
    '"day": "Monday"}], glyph="📶", battery=14)'
)

PHONE_SELECTOR = 'section[data-openui="Phone"]'
RAW_SELECTOR = ".dsl pre.raw"
COMPOSER = "form.composer textarea"
SEND_BUTTON = "form.composer button.send"


def _chunks(text: str, size: int = 90) -> list[str]:
    """Split the draft into stream-sized deltas (a boundary lands mid-statement)."""
    return [text[i : i + size] for i in range(0, len(text), size)]


class DraftThenCommittedEngine:
    """Stream a draft, commit the repaired narrative, then ``committed``/``done``.

    This is the narrator node's observable contract in one place: model deltas go
    to the writer BEFORE validation, the DSL gate may rewrite the segments, and
    only the COMMITTED payload reaches the log (``nodes.narrator``). The repair
    itself is not re-streamed and carries no frame.
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._log = None  # opened on first turn: sqlite3 objects are thread-bound
        self.calls: list[tuple[str, str]] = []

    def _log_for_write(self):
        from fabulist.log.repo import EventLog

        if self._log is None:
            self._log = EventLog(self._log_path)
        return self._log

    async def run_turn(
        self,
        session_id: str,
        content: str,
        kind: str,
        *,
        image_url: str | None = None,
        image_kind: str = "image",
    ):
        from fabulist.graph.nodes import segment_narrative

        self.calls.append((session_id, content))
        for delta in _chunks(DRAFT_TEXT):
            yield {"type": "content", "delta": delta, "meta": {"channel": "prose"}}
        segments = segment_narrative(COMMITTED_TEXT)
        prose = "\n".join(s["text"] for s in segments if s["kind"] == "prose")
        dsl = "\n".join(s["text"] for s in segments if s["kind"] == "dsl")
        self._log_for_write().append_event(
            session_id,
            "narrative",
            "narrator",
            {"prose": prose, "dsl": dsl, "segments": segments},
        )
        yield {"type": "committed", "meta": {"session_id": session_id}}
        yield {"type": "done", "meta": {"session_id": session_id}}

    def close(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            except Exception:  # noqa: BLE001 - harness teardown, never fatal
                pass


def _free(port: int) -> None:
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, check=False)


def _wait_http(url: str, *, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - local
                if resp.status < 500:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {url}")


def _wait_port(port: int, *, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise SystemExit(f"timed out waiting for :{port}")


def _start_api(log_path: Path, port: int, vite_port: int):
    """Boot ``create_app`` over the scripted engine on a throwaway log.

    The app is BUILT inside the serving thread: its ``EventLog`` holds a
    sqlite3 connection, which is bound to the thread that opened it.
    """
    import uvicorn

    from fabulist.api.app import create_app

    engine = DraftThenCommittedEngine(log_path)
    holder: dict[str, object] = {}

    def serve() -> None:
        app = create_app(
            engine,  # type: ignore[arg-type] - the TurnEngine protocol is structural
            log_path,
            allowed_origins=(
                f"http://127.0.0.1:{vite_port}",
                f"http://localhost:{vite_port}",
            ),
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        holder["server"] = server
        server.run()

    threading.Thread(target=serve, daemon=True).start()
    _wait_http(f"http://127.0.0.1:{port}/api/sessions")
    return holder["server"], engine


def _start_vite(port: int, api_port: int) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["VITE_API_BASE"] = f"http://127.0.0.1:{api_port}"
    return subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(port), "--host", "127.0.0.1"],
        cwd=FRONTEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def _render_markers(page) -> dict:
    """The transcript's render state: which openui components mounted, raw count."""
    return {
        "components": page.eval_on_selector_all(
            ".transcript [data-openui]",
            "els => els.map(e => e.getAttribute('data-openui')).sort()",
        ),
        "raw": page.eval_on_selector_all(RAW_SELECTOR, "els => els.length"),
        "raw_text": page.eval_on_selector_all(
            RAW_SELECTOR, "els => els.map(e => e.textContent).join(' | ')"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8120)
    parser.add_argument("--vite-port", type=int, default=5210)
    parser.add_argument("--shots", default="/tmp/dc_final_state")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    work = Path(args.shots)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / "log.db"
    for stale in (log_path, Path(f"{log_path}-wal"), Path(f"{log_path}-shm")):
        stale.unlink(missing_ok=True)

    _free(args.api_port)
    _free(args.vite_port)
    server, engine = _start_api(log_path, args.api_port, args.vite_port)
    vite = _start_vite(args.vite_port, args.api_port)
    failures: list[str] = []
    try:
        _wait_port(args.vite_port)
        base = f"http://127.0.0.1:{args.vite_port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page_errors: list[str] = []
            console_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )

            page.goto(f"{base}/chat", wait_until="domcontentloaded")
            page.fill(COMPOSER, "Show me the phone.")
            page.click(SEND_BUTTON)

            # The turn is over when the composer unlocks (Send replaces Stop).
            page.wait_for_selector(SEND_BUTTON, state="visible", timeout=30_000)
            page.wait_for_timeout(800)  # let the post-`done` projection round-trip settle
            live = _render_markers(page)
            page.screenshot(path=str(work / "live.png"), full_page=True)

            # Ground truth: the same session, rebuilt from the committed payload.
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_selector(PHONE_SELECTOR, state="visible", timeout=15_000)
            except Exception:  # noqa: BLE001 - reported below as a failure
                pass
            page.wait_for_timeout(400)
            reloaded = _render_markers(page)
            page.screenshot(path=str(work / "reload.png"), full_page=True)
            browser.close()

        print(f"live   : {live}")
        print(f"reload : {reloaded}")

        if "Phone" not in live["components"]:
            failures.append("live transcript did not mount the committed Phone panel")
        if "Phone" not in reloaded["components"]:
            failures.append("reload did not mount the committed Phone panel (ground truth broken)")
        if live["raw"] != 0:
            failures.append(f"live transcript still shows raw DSL: {live['raw_text'][:300]}")
        if reloaded["components"] != live["components"]:
            failures.append(
                f"final state != reload state: live {live['components']} vs reload {reloaded['components']}"
            )
        if len(engine.calls) != 1 or engine.calls[0][1] != "Show me the phone.":
            failures.append(f"unexpected engine calls: {engine.calls}")
        if page_errors:
            failures.append(f"page errors: {page_errors}")
        if console_errors:
            failures.append(f"console errors: {console_errors}")
    finally:
        vite.terminate()
        try:
            vite.wait(timeout=10)
        except subprocess.TimeoutExpired:
            vite.kill()
        server.should_exit = True
        engine.close()

    if failures:
        print("\nFAIL")
        for item in failures:
            print(f" - {item}")
        return 1
    print("\nPASS — the live transcript's final state renders the committed payload")
    print(f"screenshots: {work}/live.png {work}/reload.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
