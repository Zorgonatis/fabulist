#!/usr/bin/env python3
"""D-F acceptance gate — the validation notice is VISIBLE in the chat.

The D-F lane's first half sent a flagged turn's teaching findings to the
narration model as a ``system`` turn interleaved after its narrative. The
visible half is the owner's second sentence: the same notice must "surface as a
system message at the spot it naturally exists at (following the prior message
to which it refers)" — not a toast, not a trailing digest, not a reload-only
patch.

``EventLog.get_projection`` stops carrying only narrative/user rows: it now
exposes ``notices: [{seq, flags, score?}]``, and the transcript attaches each
notice to the narrator ENTRY whose seq it names. The engine's validate stage
runs in the tail AFTER the commit barrier, so no stream frame carries the
notice — the live turn learns of it from the post-``done`` projection, exactly
as a reload does (the D-C rebuild path).

This harness reproduces that seam end to end with no model: an isolated
``create_app`` over a scripted engine commits turn 1's narrative AND appends its
``validation_notice`` row, then turn 2 commits a CLEAN narrative. The real
frontend (own vite dev server) drives both turns with real input, and the pass
asserts:

  1. after turn 1 (NO reload) exactly one notice renders, INSIDE
     ``#history-{narrative_id}`` — the article of the narrative it refers to,
     which is also the last article at that point (nothing below it);
  2. the notice carries the per-finding message and fix;
  3. a reload rebuilds the SAME notice at the SAME anchor (live == reload);
  4. turn 2 is clean: no notice in its article, none added anywhere;
  5. no page errors and no console errors.

Issue #61 added the collapsed default, so this harness also pins it:

  6. the notice renders as ONE line — the finding list is not visible — and the
     line states how many findings there were;
  7. a reload comes back COLLAPSED (the open state is this session's view, never
     narrative state), while clicking the line expands the real list (message +
     fix visible) and clicking again closes it;
  8. toggling writes NOTHING to the event log: the ``events`` row count is the
     same before and after the two clicks.

Playwright only (real trusted input, actionability waits) — never synthetic
``.click()`` over raw CDP (AGENTS.md).

Run::

  uv run --with playwright python scripts/df_validation_notice_playwright.py

  # --shots DIR     : screenshot directory (default /tmp/df_notice)
  # --api-port N    : isolated engine port (default 8121)
  # --vite-port N   : vite dev-server port (default 5211)
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

#: Turn 1: the flagged reply. Its ``validation_notice`` row anchors to this
#: narrative's seq.
FLAGGED_PROSE = "The terminal blinks awake, its cursor waiting under a wall of raw markup."
#: Turn 2: a clean reply — it must render no notice at all.
CLEAN_PROSE = "She wipes the screen clear and leans back, satisfied."

#: The teaching finding the engine records (``graph/nodes._notice_flags`` shape
#: as the validator writes it: rule id + message + fix, plus its anchor).
FLAG = {
    "rule_id": "prose_markup",
    "check": "format",
    "severity": "warn",
    "message": "raw HTML detected in prose (left untouched)",
    "anchor": "<div>TERMINAL READY</div>",
    "fix": "remove the raw markup from prose",
}

NOTICE = ".validation-notice"
ARTICLE = ".transcript article.turn"
COMPOSER = "form.composer textarea"
SEND_BUTTON = "form.composer button.send"


def _chunks(text: str, size: int = 40) -> list[str]:
    """Split prose into stream-sized deltas."""
    return [text[i : i + size] for i in range(0, len(text), size)]


def _segments(prose: str) -> list[dict]:
    return [{"kind": "prose", "text": prose}]


class FlaggedThenCleanEngine:
    """Turn 1 commits a narrative + its notice row; turn 2 commits clean.

    This is the narrator/validate tail's observable contract in one place: the
    narrative is committed at the barrier (mirrored here by the ``committed``
    frame), and the validate stage appends the ``validation_notice`` row AFTER
    it — naming the narrative's seq — before the turn finishes.
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._log = None  # opened on first turn: sqlite3 objects are thread-bound
        self.calls: list[tuple[str, str]] = []
        #: The committed narrative rows, in order: ``{id, seq, prose}``.
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
        flagged = len(self.narratives) == 0
        prose = FLAGGED_PROSE if flagged else CLEAN_PROSE
        for delta in _chunks(prose):
            yield {"type": "content", "delta": delta, "meta": {"channel": "prose"}}
        segments = _segments(prose)
        narrative = self._log_for_write().append_event(
            session_id,
            "narrative",
            "narrator",
            {"prose": prose, "dsl": "", "segments": segments},
        )
        self.narratives.append(
            {"id": narrative.id, "seq": narrative.seq, "prose": prose}
        )
        if flagged:
            # D-F: the validate stage's teaching row, anchored to the turn it
            # refers to. Written AFTER the commit, before the turn ends.
            self._log_for_write().append_event(
                session_id,
                "system",
                "validator",
                {
                    "event": "validation_notice",
                    "seq": narrative.seq,
                    "flags": [dict(FLAG)],
                    "score": 88,
                },
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
    """Boot ``create_app`` over the scripted engine on a throwaway log."""
    import uvicorn

    from fabulist.api.app import create_app

    engine = FlaggedThenCleanEngine(log_path)
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

    # The app is BUILT inside the serving thread: its EventLog holds a sqlite3
    # connection, which is bound to the thread that opened it.
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


def _await_narrative(engine, index: int, *, timeout: float = 90.0):
    """Block until the engine has committed narrative ``index`` (0-based).

    The composer's Send button is the wrong completion gate: it flips at the
    commit BARRIER, which for a fast engine can precede the post-``done``
    reconcile by a frame — and the button's swapped-for-Stop remount can be read
    before Svelte flushes it. The narrative row is the engine's own signal.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(engine.narratives) > index:
            return engine.narratives[index]
        time.sleep(0.05)
    return None


def _notice_state(page) -> dict:
    """Where each rendered notice sits, in reading order.

    ``articles`` maps a notice to the transcript article it lives in (by the
    article's 0-based index and its ``id`` — ``history-{narrative_id}`` once the
    live entry carries its log id), plus the notice's text.

    ``open`` / ``summary`` / ``listVisible`` are the #61 collapse readout: the
    closed line's own words, whether the ``<details>`` is open, and whether the
    finding list actually paints. The paint probe is ``checkVisibility``: a
    closed ``<details>`` hides its content with ``content-visibility: hidden``,
    which leaves the box laid out — ``getClientRects`` still answers with rects,
    so geometry alone would report a collapsed list as "visible".
    """
    return page.evaluate(
        """() => {
            const articles = Array.from(document.querySelectorAll('.transcript article.turn'));
            return Array.from(document.querySelectorAll('.validation-notice')).map((el) => {
                const article = el.closest('article.turn');
                const details = el.querySelector('details.notice-details');
                const summary = details ? details.querySelector('summary') : null;
                const list = details ? details.querySelector('ul') : null;
                return {
                    articleIndex: articles.indexOf(article),
                    articleId: article ? (article.id || null) : null,
                    text: (el.textContent || '').replace(/\\s+/g, ' ').trim(),
                    inLastArticle: article === articles[articles.length - 1],
                    open: details ? details.open : null,
                    summary: summary
                        ? (summary.textContent || '').replace(/\\s+/g, ' ').trim()
                        : null,
                    listVisible: list
                        ? list.checkVisibility({
                              contentVisibilityAuto: true,
                              opacityProperty: true,
                              visibilityProperty: true
                          })
                        : null
                };
            });
        }"""
    )


def _event_rows(log_path: Path) -> int:
    """The ``events`` row count — read-only, from a second connection.

    The #61 toggle is a VIEW state: nothing about it may reach the log. Counting
    rows either side of the clicks is the direct proof (a leaked write would
    bump it, and any write also bumps ``seq``).
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{log_path}?mode=ro", uri=True)
    try:
        return int(conn.execute("select count(*) from events").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8121)
    parser.add_argument("--vite-port", type=int, default=5211)
    parser.add_argument("--shots", default="/tmp/df_notice")
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

            # -- turn 1: a flagged reply -------------------------------------
            page.goto(f"{base}/chat", wait_until="domcontentloaded")
            page.fill(COMPOSER, "Show me the terminal.")
            page.click(SEND_BUTTON)
            flagged = _await_narrative(engine, 0)
            if flagged is None:
                failures.append("engine never committed turn 1")
                live = reloaded = after_clean = []
                live_articles = reload_articles = []
                clean_article_count = -1
                expanded = recollapsed = []
                rows_before_toggle = rows_after_toggle = -1
                expanded_fix_visible = False
                browser.close()
            else:
                anchor = f"#history-{flagged['id']}"
                # The live entry only carries its log id (and its notice) once the
                # post-``done`` reconcile has rebuilt it from the projection. That
                # gate is the D-C rebuild path, exercised here on purpose.
                page.wait_for_selector(anchor, state="attached", timeout=30_000)
                try:
                    page.wait_for_selector(
                        f"{anchor} {NOTICE}", state="visible", timeout=15_000
                    )
                except Exception:  # noqa: BLE001 - reported below as a failure
                    pass
                page.wait_for_timeout(300)
                live = _notice_state(page)
                live_articles = page.eval_on_selector_all(
                    ARTICLE, "els => els.map(e => e.id || null)"
                )
                page.screenshot(path=str(work / "live-turn1.png"), full_page=True)

                # -- ground truth: the same session rebuilt from the log ------
                page.reload(wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(
                        f"{anchor} {NOTICE}", state="visible", timeout=15_000
                    )
                except Exception:  # noqa: BLE001 - reported below as a failure
                    pass
                page.wait_for_timeout(300)
                reloaded = _notice_state(page)
                reload_articles = page.eval_on_selector_all(
                    ARTICLE, "els => els.map(e => e.id || null)"
                )
                page.screenshot(path=str(work / "reload-turn1.png"), full_page=True)

                # -- issue #61: closed by default, and the line is the control --
                # Rows are counted before/after the two clicks: the open state is a
                # VIEW preference, so it must not add a single event-log row.
                rows_before_toggle = _event_rows(log_path)
                notice_row = page.locator(f"{anchor} {NOTICE}")
                notice_summary = notice_row.locator("details.notice-details > summary")
                notice_fix = page.locator(f"{anchor} {NOTICE} .notice-fix")
                if notice_fix.count():
                    if notice_fix.first.is_visible():
                        failures.append(
                            "the finding list is painted while the notice should be collapsed"
                        )
                notice_summary.click()
                page.wait_for_timeout(250)
                expanded = _notice_state(page)
                expanded_fix_visible = bool(
                    notice_fix.count() and notice_fix.first.is_visible()
                )
                page.screenshot(path=str(work / "expanded-turn1.png"), full_page=True)
                notice_summary.click()
                page.wait_for_timeout(250)
                recollapsed = _notice_state(page)
                rows_after_toggle = _event_rows(log_path)

                # -- turn 2: a clean reply renders NO notice -----------------
                page.fill(COMPOSER, "Good. Clear it.")
                page.click(SEND_BUTTON)
                clean = _await_narrative(engine, 1)
                if clean is None:
                    failures.append("engine never committed turn 2")
                    clean_article_count = -1
                else:
                    clean_anchor = f"#history-{clean['id']}"
                    page.wait_for_selector(clean_anchor, state="attached", timeout=30_000)
                    page.wait_for_timeout(300)
                    clean_article_count = page.eval_on_selector_all(
                        f"{clean_anchor} {NOTICE}", "els => els.length"
                    )
                after_clean = _notice_state(page)
                page.screenshot(path=str(work / "after-clean-turn.png"), full_page=True)
                browser.close()

        print(f"turn1 articles      : {live_articles}")
        print(f"turn1 live notices  : {live}")
        print(f"turn1 reload notices: {reloaded}")
        print(f"after clean turn    : {after_clean}")
        print(f"expanded notice     : {expanded}")
        print(f"re-collapsed notice : {recollapsed}")
        print(f"event rows          : {rows_before_toggle} -> {rows_after_toggle}")

        # 1. Exactly one notice, inside the flagged narrative's own article.
        if len(live) != 1:
            failures.append(f"expected exactly 1 live notice, got {len(live)}: {live}")
        else:
            notice = live[0]
            if notice["articleId"] != f"history-{flagged['id']}":
                failures.append(
                    "the live notice is not inside the flagged narrative's article: "
                    f"{notice['articleId']} != history-{flagged['id']}"
                )
            if not notice["inLastArticle"]:
                failures.append(
                    "the live notice is not at the natural position (after its "
                    f"narrative, with nothing below it): {notice}"
                )
            narrative_anchor = f"history-{flagged['id']}"
            if narrative_anchor in live_articles:
                expected_index = live_articles.index(narrative_anchor)
                if notice["articleIndex"] != expected_index:
                    failures.append(
                        f"notice article index {notice['articleIndex']} != "
                        f"narrative index {expected_index}"
                    )
            if FLAG["message"] not in notice["text"]:
                failures.append(f"notice is missing the flag message: {notice['text'][:300]}")
            if FLAG["fix"] not in notice["text"]:
                failures.append(f"notice is missing the fix: {notice['text'][:300]}")

        # 3. Reload builds the same thing (live == reload).
        if reloaded != live:
            failures.append(f"reload notice != live notice: {reloaded} vs {live}")
        if reload_articles != live_articles:
            failures.append(
                f"reload transcript != live transcript: {reload_articles} vs {live_articles}"
            )

        # 4. The clean turn renders nothing new.
        if len(after_clean) != 1:
            failures.append(
                f"a clean turn changed the notice count: {len(after_clean)}: {after_clean}"
            )
        if clean_article_count != 0:
            failures.append(
                f"the clean turn's article holds {clean_article_count} notice(s)"
            )

        # 5. Issue #61: closed by default — both live and after a reload — with the
        #    count on the one line, and a real click opens the real list.
        for label, state in (("live", live), ("reload", reloaded)):
            if len(state) != 1:
                continue
            row = state[0]
            if row["open"] is not False:
                failures.append(f"the {label} notice is not collapsed by default: {row}")
            if row["listVisible"] is not False:
                failures.append(f"the {label} notice paints its finding list closed: {row}")
            if not row["summary"]:
                failures.append(f"the {label} notice has no one-line summary: {row}")
            elif "1 note" not in row["summary"] or "already fixed" not in row["summary"]:
                failures.append(
                    f"the {label} summary does not state the count and the repair: "
                    f"{row['summary']}"
                )
        if len(expanded) == 1:
            if expanded[0]["open"] is not True:
                failures.append(f"the notice did not open on a click: {expanded}")
            if expanded[0]["listVisible"] is not True:
                failures.append(f"the opened notice still hides its list: {expanded}")
            if not expanded_fix_visible:
                failures.append("the opened notice does not show the finding's fix")
        else:
            failures.append(f"expected 1 notice while expanded, got {len(expanded)}")
        if len(recollapsed) == 1 and recollapsed[0]["open"] is not False:
            failures.append(f"the second click did not close the notice: {recollapsed}")
        # The toggle is view state: it must not have written to the log.
        if rows_after_toggle != rows_before_toggle:
            failures.append(
                "opening the notice wrote to the event log: "
                f"{rows_before_toggle} -> {rows_after_toggle} rows"
            )
        if len(engine.calls) != 2:
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
    print(
        "\nPASS — the validation notice renders after its narrative in the visible "
        "chat, survives a reload, a clean turn shows none, and the finding list is "
        "collapsed by default until the reader opens it (no log write)"
    )
    print(f"screenshots: {work}/live-turn1.png {work}/reload-turn1.png "
          f"{work}/expanded-turn1.png {work}/after-clean-turn.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
