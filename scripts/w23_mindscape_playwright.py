#!/usr/bin/env python3
"""Issue #23 acceptance gate — the Mindscape sizing contract + the HUD chip row.

Two audit findings, one browser pass:

  1. the Mindscape surface overflowed its own box by 317px at 1280 and 1440
     widths (its `scrollWidth`/`scrollHeight` reported content the panel clips
     but never fits — the decorative layers escaped the layout box);
  2. docking the studio shrank the thread to ~535px, where the env-tracker chip
     row clipped its right-hand chips.

The pass boots an isolated engine over a scripted turn engine (no model, no
live connection) on a throwaway log, streams ONE real turn carrying a Mindscape
panel and writes the engine's ``system``/``env_hud`` statement, then drives the
real chat page (``/chat``) with Playwright's trusted input:

  * the turn is SENT from the composer (fill + click — never a synthetic
    ``.click()`` over raw CDP, AGENTS.md);
  * the panel is measured only once it has MOUNTED interactively (the stream
    mounts one renderer per statement; the post-``done`` reconcile remounts it
    from the projection — both paths run here);
  * the Studio button is clicked to dock the studio, and the HUD chip row is
    measured in the docked layout.

Assertions, at BOTH 1280x800 and 1440x900:

  * "Mindscape fits its box": ``scrollWidth == clientWidth`` and
    ``scrollHeight == clientHeight`` on ``[data-openui="Mindscape"]`` (zero
    overflow delta), and the panel's border box fits its container;
  * "no chip clips while the studio is docked": the thread has actually
    narrowed, the tracker has no horizontal overflow, and every chip's rect
    lies inside the tracker's rect.

Playwright only (real trusted input, actionability waits) — never synthetic
``.click()`` over raw CDP (AGENTS.md).

Run::

  uv run --with playwright python scripts/w23_mindscape_playwright.py

  # --api-port N    : isolated engine port (default 8131)
  # --vite-port N   : vite dev-server port (default 5221)
  # --shots DIR     : screenshot directory (default /tmp/w23_mindscape)
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

#: Viewports the issue reported (the audit's own two).
VIEWPORTS: tuple[tuple[int, int], ...] = ((1280, 800), (1440, 900))

COMPOSER = "form.composer textarea"
SEND_BUTTON = "form.composer button.send"
STUDIO_BUTTON = 'button[aria-label^="Design studio"]'
MINDSCAPE = '[data-openui="Mindscape"]'
ENV_TRACKER = ".env-tracker"
THREAD = "main.thread"

#: A narrator turn with a Mindscape panel. The fragments cover every stance and
#: register (scream / aside / thought, loud / quiet) so the loudest lane — the
#: one that presses on the panel's right edge — is on screen.
PROSE = (
    "The corridor narrows until the walls are close enough to hear, and the "
    "thought you were carrying splits into several."
)
MINDSCAPE_DSL = (
    'mind = Mindscape("Cracks in the Facade", '
    '[{text: "Hold it together", weight: 45, kind: "thought"}, '
    '{text: "IT IS ALREADY OPEN", weight: 95, kind: "scream"}, '
    '{text: "they can smell fear", weight: 20, kind: "aside"}, '
    '{text: "I am fine", weight: 70, kind: "thought"}, '
    '{text: "count the doors on the way out", weight: 30, kind: "thought"}], '
    '"grain", "", "dread", {glyph: "(・ε・)"})'
)
ROOT_DSL = 'root = ScenePane("Cracks in the Facade", [mind])'

#: The engine-emitted HUD statement (`hud = EnvTracker(time, date, loc, weather,
#: forecast)`) — five positional strings, prod-length so the row is full.
HUD_STATEMENT = (
    'hud = EnvTracker("23:58", "Monday, June 02, 2025", '
    '"the night market below the harbour wall", '
    '"light rain and a rising wind off the water", '
    '"clearing by midnight, cold front")'
)


def log(msg: str) -> None:
    print(f"w23_mindscape: {msg}", flush=True)


def _chunks(text: str, size: int = 40) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class MindscapeTurnEngine:
    """One scripted turn: prose + a Mindscape panel, then the env HUD statement.

    It writes the same rows the real graph writes at the commit barrier — a
    ``narrative`` with per-statement ``segments`` (so the post-``done``
    projection rebuild mounts the same panel) and the ``system``/``env_hud``
    statement the HUD chip row renders.
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._log = None  # opened on first turn: sqlite3 objects are thread-bound
        self.calls: list[tuple[str, str]] = []
        self.narratives: list[dict] = []

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
        self.calls.append((session_id, content))
        raw = f"{PROSE}\n\n```openui\n{MINDSCAPE_DSL}\n{ROOT_DSL}\n```\n"
        for delta in _chunks(raw):
            yield {"type": "content", "delta": delta, "meta": {"channel": "prose"}}
        narrative = self._log_for_write().append_event(
            session_id,
            "narrative",
            "narrator",
            {
                "prose": PROSE,
                "dsl": f"{MINDSCAPE_DSL}\n{ROOT_DSL}",
                "segments": [
                    {"kind": "prose", "text": PROSE},
                    {"kind": "dsl", "text": MINDSCAPE_DSL},
                    {"kind": "dsl", "text": ROOT_DSL},
                ],
            },
        )
        self.narratives.append(
            {"id": narrative.id, "seq": narrative.seq, "prose": PROSE}
        )
        # The env HUD is engine-emitted (ADR-004): the statement the chip row
        # renders, read back by GET /api/sessions/{id}/hud.
        self._log_for_write().append_event(
            session_id,
            "system",
            "env_hud",
            {"event": "env_hud", "statement": HUD_STATEMENT},
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
    # `pkill -f` self-matches in this repo — kill by port (AGENTS.md).
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
    import uvicorn

    from fabulist.api.app import create_app

    engine = MindscapeTurnEngine(log_path)
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

    # Built inside the serving thread: the EventLog's sqlite3 connection is
    # bound to the thread that opens it.
    threading.Thread(target=serve, daemon=True).start()
    _wait_http(f"http://127.0.0.1:{port}/api/sessions")
    return holder["server"], engine


def _start_vite(port: int, api_port: int) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["VITE_API_BASE"] = f"http://127.0.0.1:{api_port}"
    env.pop("VITE_MOCK", None)
    return subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(port), "--host", "127.0.0.1"],
        cwd=FRONTEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def _await_narrative(engine, index: int, *, timeout: float = 90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(engine.narratives) > index:
            return engine.narratives[index]
        time.sleep(0.05)
    return None


def _mindscape_metrics(page) -> dict:
    return page.evaluate(
        """() => {
            const el = document.querySelector('[data-openui="Mindscape"]');
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            const parent = el.parentElement;
            const prect = parent ? parent.getBoundingClientRect() : null;
            return {
                clientWidth: el.clientWidth,
                scrollWidth: el.scrollWidth,
                clientHeight: el.clientHeight,
                scrollHeight: el.scrollHeight,
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                parentWidth: prect ? Math.round(prect.width) : null,
                fitsParent: prect ? rect.right <= prect.right + 0.5 && rect.left >= prect.left - 0.5 : null,
            };
        }"""
    )


def _chip_metrics(page) -> dict:
    return page.evaluate(
        """(sel) => {
            const tracker = document.querySelector(sel);
            if (!tracker) return null;
            const rect = tracker.getBoundingClientRect();
            const chips = Array.from(tracker.children).map((el) => {
                const r = el.getBoundingClientRect();
                const cls = (el.classList && el.classList[0]) || el.tagName.toLowerCase();
                return {
                    name: cls,
                    left: Math.round(r.left * 10) / 10,
                    right: Math.round(r.right * 10) / 10,
                    top: Math.round(r.top * 10) / 10,
                    bottom: Math.round(r.bottom * 10) / 10,
                    insideX: r.left >= rect.left - 0.5 && r.right <= rect.right + 0.5,
                    insideY: r.top >= rect.top - 0.5 && r.bottom <= rect.bottom + 0.5,
                    visible: r.width > 0 && r.height > 0,
                };
            });
            const bar = tracker.parentElement;
            return {
                width: Math.round(rect.width),
                clientWidth: tracker.clientWidth,
                scrollWidth: tracker.scrollWidth,
                clientHeight: tracker.clientHeight,
                scrollHeight: tracker.scrollHeight,
                barClientWidth: bar ? bar.clientWidth : null,
                barScrollWidth: bar ? bar.scrollWidth : null,
                chips,
            };
        }""",
        ENV_TRACKER,
    )


def _thread_width(page) -> int:
    return page.eval_on_selector(THREAD, "el => Math.round(el.getBoundingClientRect().width)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8131)
    parser.add_argument("--vite-port", type=int, default=5221)
    parser.add_argument("--shots", default="/tmp/w23_mindscape")
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
    measurements: dict[str, dict] = {}
    try:
        _wait_port(args.vite_port)
        base = f"http://127.0.0.1:{args.vite_port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": VIEWPORTS[0][0], "height": VIEWPORTS[0][1]})
            page = context.new_page()
            page_errors: list[str] = []
            console_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )

            # -- one real turn, sent from the composer (trusted input) --------
            page.goto(f"{base}/chat", wait_until="domcontentloaded")
            page.wait_for_selector(COMPOSER, timeout=30_000)
            page.fill(COMPOSER, "I keep walking toward the sound.")
            page.click(SEND_BUTTON)
            narrative = _await_narrative(engine, 0)
            if narrative is None:
                failures.append("engine never committed the scripted turn")
            page.wait_for_selector(f"#history-{narrative['id']}" if narrative else MINDSCAPE, timeout=30_000)
            # The panel mounts from the stream (and again from the post-done
            # projection rebuild) — wait for the mounted surface itself.
            page.wait_for_selector(MINDSCAPE, state="visible", timeout=30_000)
            page.wait_for_selector(ENV_TRACKER, state="visible", timeout=30_000)
            page.wait_for_timeout(400)
            page.screenshot(path=str(work / "chat-1280-undocked.png"), full_page=True)

            # -- 1. Mindscape fits its box at both reported viewports ---------
            for width, height in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(300)
                metrics = _mindscape_metrics(page)
                measurements[f"mindscape@{width}"] = metrics or {}
                if not metrics:
                    failures.append(f"Mindscape did not mount at {width}px")
                    continue
                dw = metrics["scrollWidth"] - metrics["clientWidth"]
                dh = metrics["scrollHeight"] - metrics["clientHeight"]
                if dw > 0:
                    failures.append(
                        f"Mindscape overflows its box horizontally at {width}px by "
                        f"{dw}px (content {metrics['scrollWidth']} vs box {metrics['clientWidth']})"
                    )
                if dh > 0:
                    failures.append(
                        f"Mindscape overflows its box vertically at {width}px by "
                        f"{dh}px (content {metrics['scrollHeight']} vs box {metrics['clientHeight']})"
                    )
                if metrics["fitsParent"] is False:
                    failures.append(
                        f"Mindscape is wider than its panel at {width}px: "
                        f"{metrics['width']} vs parent {metrics['parentWidth']}"
                    )
                page.screenshot(path=str(work / f"chat-{width}-undocked.png"), full_page=True)

            # -- 2. dock the studio, then measure the HUD chip row ------------
            undocked = _thread_width(page)
            page.click(STUDIO_BUTTON)
            page.wait_for_selector(".studio.docked", state="visible", timeout=15_000)
            page.wait_for_timeout(400)
            docked = _thread_width(page)
            measurements["thread"] = {"undocked": undocked, "docked": docked}
            if docked >= undocked:
                failures.append(
                    f"docking the studio did not narrow the thread: {undocked} -> {docked}"
                )

            for width, height in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(300)
                # The docked layout narrows the panel again — the same contract
                # has to hold here, not just in the 46rem reading column.
                panel = _mindscape_metrics(page)
                measurements[f"mindscape-docked@{width}"] = panel or {}
                if not panel:
                    failures.append(f"Mindscape did not mount in the docked layout at {width}px")
                else:
                    dw = panel["scrollWidth"] - panel["clientWidth"]
                    dh = panel["scrollHeight"] - panel["clientHeight"]
                    if dw > 0:
                        failures.append(
                            f"Mindscape overflows its box horizontally in the docked "
                            f"layout at {width}px by {dw}px (content {panel['scrollWidth']} vs "
                            f"box {panel['clientWidth']})"
                        )
                    if dh > 0:
                        failures.append(
                            f"Mindscape overflows its box vertically in the docked "
                            f"layout at {width}px by {dh}px (content {panel['scrollHeight']} vs "
                            f"box {panel['clientHeight']})"
                        )
                    if panel["fitsParent"] is False:
                        failures.append(
                            f"Mindscape is wider than its panel in the docked layout at "
                            f"{width}px: {panel['width']} vs parent {panel['parentWidth']}"
                        )
                chips = _chip_metrics(page)
                measurements[f"chips@{width}"] = chips or {}
                if not chips:
                    failures.append(f"env-tracker did not render at {width}px")
                    continue
                if chips["scrollWidth"] > chips["clientWidth"]:
                    failures.append(
                        f"env-tracker overflows the chip row at {width}px by "
                        f"{chips['scrollWidth'] - chips['clientWidth']}px"
                    )
                if chips["barScrollWidth"] is not None and chips["barScrollWidth"] > chips["barClientWidth"]:
                    failures.append(
                        f"the HUD bar overflows the thread at {width}px by "
                        f"{chips['barScrollWidth'] - chips['barClientWidth']}px"
                    )
                for chip in chips["chips"]:
                    if not chip["visible"]:
                        continue
                    if not chip["insideX"]:
                        failures.append(
                            f"chip {chip['name']!r} clips horizontally at {width}px: "
                            f"[{chip['left']}, {chip['right']}] outside the tracker"
                        )
                    if not chip["insideY"]:
                        failures.append(
                            f"chip {chip['name']!r} clips vertically at {width}px: "
                            f"[{chip['top']}, {chip['bottom']}] outside the tracker"
                        )
                page.screenshot(path=str(work / f"chat-{width}-studio-docked.png"), full_page=True)

            browser.close()

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

    for key, value in measurements.items():
        print(f"{key}: {value}")

    if failures:
        print("\nFAIL")
        for item in failures:
            print(f" - {item}")
        return 1
    print(
        "\nPASS — the Mindscape fits its panel at 1280/1440, and the env-tracker "
        "chip row does not clip while the studio is docked"
    )
    print(f"screenshots: {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
