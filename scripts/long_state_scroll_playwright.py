#!/usr/bin/env python3
"""Issue #66 acceptance gate — a long state pane must not scroll the document.

The chat shell is a 100vh row (``.chat`` in ``routes/chat/+page.svelte``): the
transcript scrolls in its own column and the state pane docks beside it. When
the pane's content grew past the window the DOCUMENT became scrollable as well,
so the reader had two competing scroll contexts — scrolling over the state rows
moved the whole page instead of the pane.

The cause was a height, not an overflow: the docked pane's height was its
CONTENT (a block in a block), so its ``overflow-y: auto`` had no box to scroll
against and simply grew, pushing the shell's corner past the viewport. The fix
gives the pane the shell's height (``min-height: 0`` on the shell's column and
on the pane) so it scrolls inside itself.

This harness proves it in a real browser with the real frontend:

  1. an isolated engine (``create_app`` over ``FakeTurnEngine`` — no model) gets
     a session whose state tree is grown through the PRODUCT'S OWN write door
     (``POST /api/sessions/{id}/entities`` — the ✎ pencil's route), not by poking
     fixtures into the DOM;
  2. at a tall window and again at a window shorter than the state panel, the
     document is NOT scrollable (``scrollHeight <= clientHeight`` for both
     ``<html>`` and ``<body>``) and the pane's bottom edge sits inside the
     viewport;
  3. the pane IS the scroll container (``scrollHeight > clientHeight``), a real
     wheel over it moves the pane and leaves ``window.scrollY`` at 0;
  4. the transcript column still scrolls on its own (the two contexts are
     independent);
  5. no page errors, no console errors.

Playwright only (real trusted input, actionability waits) — never synthetic
``.click()`` over raw CDP (AGENTS.md).

Run::

  uv run --with playwright python scripts/long_state_scroll_playwright.py

  # --api-port N    : isolated engine port (default 8122)
  # --vite-port N   : vite dev-server port (default 5212)
  # --shots DIR     : screenshot directory (default /tmp/long_state)
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

#: Enough registry rows to outrun any sane window: each thread is one entry in
#: the pane's threads section, so 60 rows is comfortably taller than 1000px.
THREAD_COUNT = 60
THREAD_DETAIL = "keeps asking about the ledger she is not supposed to have read"

#: The state pane. Its own root is the pane; the shell is ``.chat``.
SIDEBAR = "aside.sidebar"
EXPAND = "Expand state bar"


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


def _post(url: str, body: dict) -> dict:
    request = urllib.request.Request(  # noqa: S310 - local
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310 - local
        return json.loads(resp.read() or b"{}")


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 - local
        return json.loads(resp.read() or b"{}")


def _start_api(log_path: Path, port: int, vite_port: int):
    """Boot ``create_app`` over the fake engine on a throwaway log."""
    import uvicorn

    from fabulist.api.app import create_app
    from fabulist.api.fake import FakeTurnEngine

    holder: dict[str, object] = {}

    def serve() -> None:
        # The app is BUILT inside the serving thread: its EventLog holds a
        # sqlite3 connection, which is bound to the thread that opened it.
        app = create_app(
            FakeTurnEngine(),  # type: ignore[arg-type] - structural protocol
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
    return holder["server"]


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


def _seed_session(api: str) -> tuple[str, int]:
    """A session whose state pane is long, written through the ✎ door.

    ``establish_thread`` is the tracker's own action (the same code the BTS loop
    runs): the rows that end up in the pane are the engine's real registry
    objects, so the height under test is the height a real chat produces.
    """
    session_id = _post(f"{api}/api/sessions", {"title": "long state"})["session_id"]
    accepted = 0
    for index in range(THREAD_COUNT):
        result = _post(
            f"{api}/api/sessions/{session_id}/entities",
            {
                "op": "establish_thread",
                "args": {
                    "name": f"the {index + 1:02d} unresolved errand in the eastern quarter",
                    "detail": THREAD_DETAIL,
                },
            },
        )
        if result.get("ok") is not False:
            accepted += 1
    return session_id, accepted


#: Everything the assertions need, measured in one round trip.
LAYOUT = """
() => {
  const de = document.documentElement;
  const body = document.body;
  const pane = document.querySelector('aside.sidebar');
  const chat = document.querySelector('div.chat');
  const transcript = document.querySelector('.transcript') || document.querySelector('.thread-scroll');
  const paneRect = pane ? pane.getBoundingClientRect() : null;
  const read = (el) => el ? { scrollH: el.scrollHeight, clientH: el.clientHeight } : null;
  return {
    innerHeight: window.innerHeight,
    innerWidth: window.innerWidth,
    docScrollH: de.scrollHeight,
    docClientH: de.clientHeight,
    bodyScrollH: body.scrollHeight,
    docScrollable: de.scrollHeight > de.clientHeight + 1 || body.scrollHeight > window.innerHeight + 1,
    windowScrollY: window.scrollY,
    docScrollTop: de.scrollTop,
    chat: read(chat),
    pane: pane ? {
      ...read(pane),
      top: paneRect.top,
      bottom: paneRect.bottom,
      scrollTop: pane.scrollTop,
      overflowY: getComputedStyle(pane).overflowY,
      rows: pane.querySelectorAll('dl > div').length + pane.querySelectorAll('ul > li').length,
      text: (pane.textContent || '').replace(/\\s+/g, ' ').trim().length
    } : null,
    transcript: transcript ? { ...read(transcript), overflowY: getComputedStyle(transcript).overflowY } : null
  };
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8122)
    parser.add_argument("--vite-port", type=int, default=5212)
    parser.add_argument("--shots", default="/tmp/long_state")
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
    server = _start_api(log_path, args.api_port, args.vite_port)
    api = f"http://127.0.0.1:{args.api_port}"
    session_id, threads = _seed_session(api)
    tree = _get(f"{api}/api/sessions/{session_id}/tree")
    vite = _start_vite(args.vite_port, args.api_port)
    failures: list[str] = []
    try:
        _wait_port(args.vite_port)
        base = f"http://127.0.0.1:{args.vite_port}/chat?s={session_id}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page_errors: list[str] = []
            console_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector(SIDEBAR, state="visible", timeout=30_000)
            expand = page.get_by_role("button", name=EXPAND)
            if expand.count():
                expand.first.click()
            page.wait_for_timeout(800)

            tall = page.evaluate(LAYOUT)
            page.screenshot(path=str(work / "tall.png"))

            # A real wheel over the pane: the PANE scrolls, the WINDOW does not.
            box = page.locator(SIDEBAR).bounding_box()
            if box is None:
                failures.append("the state pane has no box to scroll")
                wheeled = tall
            else:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.mouse.wheel(0, 600)
                page.wait_for_timeout(400)
                wheeled = page.evaluate(LAYOUT)

            # A window SHORTER than the state panel — the defect's own case.
            page.set_viewport_size({"width": 1180, "height": 520})
            page.wait_for_timeout(500)
            short = page.evaluate(LAYOUT)
            page.screenshot(path=str(work / "short.png"))
            browser.close()
    finally:
        vite.terminate()
        try:
            vite.wait(timeout=10)
        except subprocess.TimeoutExpired:
            vite.kill()
        server.should_exit = True

    print(f"session             : {session_id}")
    print(f"thread writes       : {threads}/{THREAD_COUNT} accepted")
    print(f"tree bytes          : {len(json.dumps(tree))}")
    print(f"tall   ({tall['innerWidth']}x{tall['innerHeight']}): {tall}")
    print(f"wheeled            : pane.scrollTop={wheeled['pane'] and wheeled['pane']['scrollTop']} "
          f"window.scrollY={wheeled['windowScrollY']}")
    print(f"short  ({short['innerWidth']}x{short['innerHeight']}): {short}")

    if threads < THREAD_COUNT:
        failures.append(f"the state door only accepted {threads} of {THREAD_COUNT} threads")
    if not tree.get("tree"):
        failures.append("the engine served no session tree — the pane has nothing long to show")

    for label, state in (("tall", tall), ("short", short)):
        pane = state["pane"]
        if state["docScrollable"]:
            failures.append(
                f"{label} window: the DOCUMENT scrolls "
                f"(html {state['docScrollH']} / body {state['bodyScrollH']} vs "
                f"{state['docClientH']} client)"
            )
        if pane is None:
            failures.append(f"{label} window: no state pane")
            continue
        if pane["scrollH"] <= pane["clientH"]:
            failures.append(
                f"{label} window: the pane is not its own scroll container "
                f"({pane['scrollH']} <= {pane['clientH']}) — the panel is too short "
                "for this assertion to mean anything"
            )
        if pane["bottom"] > state["innerHeight"] + 1 or pane["top"] < -1:
            failures.append(
                f"{label} window: the pane sits outside the viewport "
                f"(top {pane['top']}, bottom {pane['bottom']} vs {state['innerHeight']})"
            )
        if pane["overflowY"] != "auto":
            failures.append(f"{label} window: the pane's overflow-y is {pane['overflowY']}")

    if wheeled["pane"] and wheeled["pane"]["scrollTop"] <= 0:
        failures.append("a real wheel over the pane did not scroll the pane")
    if wheeled["windowScrollY"] != 0 or wheeled["docScrollTop"] != 0:
        failures.append(
            "scrolling the pane moved the document "
            f"(window.scrollY {wheeled['windowScrollY']}, html.scrollTop {wheeled['docScrollTop']})"
        )
    if tall["transcript"] is None:
        failures.append("no transcript column in the DOM")
    elif tall["transcript"]["overflowY"] != "auto":
        failures.append(
            "the transcript column is not its own scroll context "
            f"(overflow-y {tall['transcript']['overflowY']})"
        )
    if page_errors:
        failures.append(f"page errors: {page_errors}")
    if console_errors:
        failures.append(f"console errors: {console_errors}")

    if failures:
        print("\nFAIL")
        for item in failures:
            print(f" - {item}")
        return 1
    print(
        "\nPASS — a state pane longer than the window scrolls inside its own pane, "
        "the document does not scroll, and the transcript keeps its own scroll"
    )
    print(f"screenshots: {work}/tall.png {work}/short.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
