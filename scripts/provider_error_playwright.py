#!/usr/bin/env python3
"""Provider-failure acceptance gate — the abort is named, and the error state is recoverable.

The owner's live bug had two halves, and this harness drives both through the real
frontend (its own vite dev server) against a REAL ``GraphTurnEngine`` whose planner
connection is stubbed to return a 429:

  1. the planner's 429 ABORTS the turn (it used to be contained as
     ``plan_refresh_failed``, so the reader only met the failure at the narrator),
     and the message names the PHASE and the provider status;
  2. with the connection status in ``error`` the rail's retry door stays pressable,
     and pressing it re-runs the failed turn — which succeeds once the provider
     answers — instead of leaving the session a dead end.

No model is called: the planner caller raises a real ``openai.RateLimitError``
(built from an ``httpx.Response``, exactly the object the SDK raises) while
``--fail-planner`` is armed, and answers a valid plan once the door is clicked.

Playwright only (real trusted input, actionability waits) — never synthetic
``.click()`` over raw CDP (AGENTS.md).

Run::

  uv run --with playwright python scripts/provider_error_playwright.py

  # --shots DIR     : screenshot directory (default /tmp/provider_error)
  # --api-port N    : isolated engine port (default 8131)
  # --vite-port N   : vite dev-server port (default 5231)
"""

from __future__ import annotations

import argparse
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

#: The provider status the stub refuses with — the owner's own failure.
PROVIDER_STATUS = 429
PROVIDER_MODEL = "glm-4.6"
PROVIDER_MESSAGE = "Rate limit reached for requests"
RETRY_AFTER = "20"

#: The reply the narrator streams once the turn survives the planner.
REPLY_PROSE = "The lamp gutters once, then holds, and the room settles around you."

PLAN = {
    "strings": [
        "the seal goes missing from the vault",
        "Mara's debt comes due",
        "the archivist stalls the search",
        "the duel is postponed, not resolved",
    ],
    "datetime": "Monday, June 02, 2025 at 19:40",
    "weather": "light rain",
    "forecast": "clearing before dawn",
    "established": ["Mara owes the guild 40 silver"],
}

COMPOSER = "form.composer textarea"
SEND_BUTTON = "form.composer button.send"
STATUS = ".sidebar .status"
RAIL_RETRY = ".sidebar button[data-retry]"
RAIL_ERROR = ".sidebar .connection-error"
ARTICLE = ".transcript article.turn"


def _rate_limited():
    """The object the SDK raises for a 429 — status, body and Retry-After."""
    import httpx
    import openai

    request = httpx.Request("POST", "http://provider.example/v1/chat/completions")
    body = {"error": {"message": PROVIDER_MESSAGE, "type": "rate_limit_error"}}
    response = httpx.Response(
        PROVIDER_STATUS,
        request=request,
        headers={"retry-after": RETRY_AFTER},
        json=body,
    )
    return openai.RateLimitError(
        f"Error code: {PROVIDER_STATUS} - {body}", response=response, body=body
    )


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


class _PlannerCaller:
    """The planner's model seam: a 429 while ``fault`` is armed, a plan after."""

    def __init__(self, fault: dict) -> None:
        self.fault = fault
        self.calls = 0

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls += 1
        if self.fault["fail"]:
            raise _rate_limited()
        yield {"type": "content", "delta": json.dumps(PLAN)}


class _NarratorCaller:
    """The narrator: one short reply, streamed in deltas like the real one."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls += 1
        for index in range(0, len(REPLY_PROSE), 24):
            yield {"type": "content", "delta": REPLY_PROSE[index : index + 24]}


class _Extractor:
    async def __call__(self, user_msg, prose):
        return {"deltas": {"tone": "quiet"}, "action_class": "dialogue"}


def _start_api(log_path: Path, port: int, vite_port: int, fault: dict):
    """Boot ``create_app`` over the REAL graph on a throwaway log."""
    import uvicorn

    from fabulist.api.app import create_app

    holder: dict[str, object] = {}

    def serve() -> None:
        from fabulist.graph import GraphTurnEngine, build_graph
        from fabulist.graph.nodes import make_planner_node
        from fabulist.log import EventLog

        log = EventLog(log_path)
        narrator = _NarratorCaller()
        planner = _PlannerCaller(fault)
        graph = build_graph(
            model=narrator,
            extractor=_Extractor(),
            log=log,
            planner=make_planner_node(
                log=log,
                caller=planner,
                # The label the abort message names — what the wiring resolves.
                provider_label_fn=lambda: PROVIDER_MODEL,
            ),
        )
        engine = GraphTurnEngine(graph=graph, log=log, model=narrator)
        holder["planner"] = planner
        holder["narrator"] = narrator
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
    return holder["server"], holder


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


def _rail_error_text(page) -> dict:
    """The rail's error surface: status slot, cause line, retry door."""
    status = page.locator(STATUS)
    rail_error = page.locator(RAIL_ERROR)
    retry = page.locator(RAIL_RETRY)
    return {
        "status": (status.first.text_content() or "").strip()
        if status.count()
        else "",
        "status_state": (status.first.get_attribute("data-status") or "")
        if status.count()
        else "",
        "cause": (rail_error.first.text_content() or "").strip()
        if rail_error.count()
        else "",
        "retry_count": retry.count(),
        "retry_enabled": retry.first.is_enabled() if retry.count() else False,
    }


def _turn_texts(page) -> list[str]:
    nodes = page.locator(ARTICLE)
    return [
        " ".join((nodes.nth(index).inner_text() or "").split())
        for index in range(nodes.count())
    ]


def _await_prose(page, *, timeout: float = 60.0) -> bool:
    """Wait until the transcript carries the narrator's reply."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(REPLY_PROSE[:40] in text for text in _turn_texts(page)):
            return True
        page.wait_for_timeout(100)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8131)
    parser.add_argument("--vite-port", type=int, default=5231)
    parser.add_argument("--shots", default="/tmp/provider_error")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    work = Path(args.shots)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / "log.db"
    for stale in (log_path, Path(f"{log_path}-wal"), Path(f"{log_path}-shm")):
        stale.unlink(missing_ok=True)

    fault = {"fail": True}
    _free(args.api_port)
    _free(args.vite_port)
    server, engine = _start_api(log_path, args.api_port, args.vite_port, fault)
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

            # -- turn 1: the planner's 429 must ABORT the turn ----------------
            page.goto(f"{base}/chat", wait_until="domcontentloaded")
            page.fill(COMPOSER, "I step inside and close the door.")
            page.click(SEND_BUTTON)
            page.wait_for_selector(RAIL_ERROR, timeout=60_000)
            page.wait_for_selector(".transcript article.turn p.error", timeout=60_000)
            rail = _rail_error_text(page)
            turns = _turn_texts(page)
            page.screenshot(path=str(work / "1-aborted-turn.png"), full_page=True)

            expected_cause = (
                f"Planning failed: {PROVIDER_STATUS} from {PROVIDER_MODEL}"
            )
            if rail["status_state"] != "error":
                failures.append(f"rail status is {rail['status_state']!r}, expected 'error'")
            if expected_cause not in rail["cause"]:
                failures.append(f"rail cause does not name the phase: {rail['cause']!r}")
            if PROVIDER_MESSAGE not in rail["cause"]:
                failures.append(f"rail cause lost the provider's text: {rail['cause']!r}")
            if f"retry after {RETRY_AFTER}" not in rail["cause"]:
                failures.append(f"rail cause lost Retry-After: {rail['cause']!r}")
            if not any(expected_cause in text for text in turns):
                failures.append(f"transcript does not carry the abort: {turns!r}")
            if any(REPLY_PROSE[:40] in text for text in turns):
                failures.append("the narrator ran despite the planner abort")
            if engine["narrator"].calls:
                failures.append(
                    f"the narrator was called {engine['narrator'].calls}x after a planner abort"
                )

            # -- the dead end: the retry door must stay pressable ------------
            if rail["retry_count"] != 1:
                failures.append(f"rail retry doors found: {rail['retry_count']} (want 1)")
            elif not rail["retry_enabled"]:
                failures.append("the rail retry door is DISABLED while the status is 'error'")

            # -- press it: the turn re-runs and the error clears -------------
            fault["fail"] = False  # the provider answers again
            page.click(RAIL_RETRY)
            if not _await_prose(page):
                failures.append("the retry door did not re-run the failed turn")
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.sidebar .status');
                    return el && el.getAttribute('data-status') !== 'error';
                }""",
                timeout=60_000,
            )
            after = _rail_error_text(page)
            if after["status_state"] == "error":
                failures.append("the error state never cleared after a successful retry")
            if after["cause"]:
                failures.append(f"the cause line outlived the error state: {after['cause']!r}")
            page.screenshot(path=str(work / "2-recovered.png"), full_page=True)

            if page_errors:
                failures.append(f"page errors: {page_errors}")
            if console_errors:
                failures.append(f"console errors: {console_errors}")
            browser.close()

        print(f"planner calls: {engine['planner'].calls}, narrator calls: {engine['narrator'].calls}")
        print(f"shots: {work}")
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1
        print("OK: the planner 429 aborted the turn, named the phase, and the rail recovered")
        return 0
    finally:
        # uvicorn's Server.shutdown is a coroutine (and this one runs in its own
        # thread): the flag is the thread-safe stop. Never ``fuser -k`` our OWN
        # port here — the server is in this process, so that would SIGKILL us.
        server.should_exit = True  # type: ignore[attr-defined]
        vite.terminate()
        try:
            vite.wait(timeout=10)
        except subprocess.TimeoutExpired:
            vite.kill()
        _free(args.vite_port)
        sys.stdout.flush()
        sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
