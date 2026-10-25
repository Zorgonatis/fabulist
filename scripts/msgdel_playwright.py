#!/usr/bin/env python3
"""#35 live gate — deleting a message tail is a FORK, through the real browser.

"Delete messages N..end" is a branch-pointer move: the engine forks the session
just below the pivot and the reader is switched to the new branch, while the old
line stays in the append-only log one ‹ away. The engine half is covered by
``tests/test_delete_tail.py``; the frontend half is only real when a browser
does it. This harness drives the shipped SvelteKit UI with Playwright's trusted
input against an isolated ``create_app`` over a scripted turn engine (no model,
throwaway log under ``/tmp``), and asserts, in one run:

  1. three turns are created with REAL input through the composer;
  2. clicking ✂ on an early (non-first) user message opens ``window.confirm``
     naming the correct N (the dialog handler below ACCEPTS explicitly --
     Playwright's auto-dismissal would silently cancel the delete);
  3. accepting changes the URL's session id to the new branch, truncates the
     transcript to the kept prefix, lists the branch in the rail, and renders
     the two-line branch controls (the brief writes it "1/2"; the rendered
     index is the new sibling, so the exact text is recorded and checked as a
     2-member group);
  4. one ‹ swipe brings the FULL old line back;
  5. a reload still lands on the new branch, still truncated (the mount path
     -- this is where a Svelte ``each_key_duplicate`` would fire);
  6. the FIRST message has no ✂ at all;
  7. dismissing the confirm elsewhere changes nothing (same id, same rows);
  8. zero page errors and zero console errors across the run;
  9. inherited-pivot smoke: on the new branch, take another turn, then delete
     from an INHERITED message. The second fork cuts BELOW the branch's own
     ``base_seq`` -- the chain-bound regression (tightest descendant cut) -- so
     the new branch must expose only the kept prefix, not ancestor rows above
     the cut.

The session is pre-created WITH a title because ``delete-tail`` carries the
source title onto the new branch only when the title is non-empty (that carry
row is what makes ``GET /api/sessions`` summarize the branch). A fresh chat
keeps the default empty title until the user renames it; that untitled case is
reported as a residual risk rather than asserted here.

Playwright only (real trusted input, actionability waits) -- never synthetic
``.click()`` over raw CDP (AGENTS.md). The spawned vite server is killed by
port; the API listener is stopped gracefully because it lives in this process
(``fuser -k 8001/tcp`` would SIGKILL the gate itself).

Run::

  uv run --with playwright python scripts/msgdel_playwright.py

  # --shots DIR     : screenshot/evidence dir (default /tmp/msgdel-gate)
  # --api-port N    : isolated engine port (default 8001)
  # --vite-port N   : vite dev-server port (default 5212)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

#: Title of the gate chat. Non-empty ON PURPOSE: ``delete-tail`` only writes the
#: title-carry row when the source has a title, and that row is what makes the
#: new branch visible in ``GET /api/sessions`` (see the module docstring).
TITLE = "Msgdel gate chat"

#: Three real turns. The middle one is the pivot (message #3 of six).
TURNS = [
    "One: the lamp is lit.",
    "Two: the door is shut.",
    "Three: the key turns.",
]
#: A fourth turn, taken ON the new branch (scenario 9).
FOURTH = "Four: the room is quiet."


COMPOSER = "form.composer textarea"
SEND_BUTTON = "form.composer button.send"
STOP_BUTTON = "form.composer button.stop"
DELETE_BUTTON = "button.delete-btn"
BRANCH_COUNT = ".branch-controls .count"
BRANCH_PREV = '.branch-controls button[aria-label="Previous reply"]'
RAIL_TITLE = "aside.drawer ul.chat-rows button.entry .title"


def _chunks(text: str, size: int = 34) -> list[str]:
    """Split prose into stream-sized deltas."""
    return [text[i : i + size] for i in range(0, len(text), size)]


@dataclass
class TurnRecord:
    """One committed scripted turn, as the browser will see it in the log."""

    session_id: str
    text: str
    user_id: int
    user_seq: int
    narrative_id: int
    narrative_seq: int
    prose: str


class ScriptedTurnEngine:
    """No model: stream a distinct prose per turn and commit its narrative row.

    The API layer appends the user row before calling ``run_turn`` (the real
    seam, ``api/app._drive_turn``); this engine commits the narrator row at the
    same point the graph's post-turn barrier does, so a client rehydrating from
    the projection sees exactly what a live turn writes.
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._log = None  # opened on first turn: sqlite3 objects are thread-bound
        self.calls: list[tuple[str, str, str]] = []
        self.turns: list[TurnRecord] = []

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
        self.calls.append((session_id, content, kind))
        log = self._log_for_write()
        # The user row the API just appended for THIS turn (own rows: every
        # non-regenerate turn appends one on the session it streams into).
        user = next(
            (
                event
                for event in reversed(log.get_events(session_id))
                if event.type == "user"
            ),
            None,
        )
        number = len(self.turns) + 1
        prose = f"Gate reply {number}: the scene answers {content!r}."
        for delta in _chunks(prose):
            yield {"type": "content", "delta": delta, "meta": {"channel": "prose"}}
        narrative = log.append_event(
            session_id,
            "narrative",
            "narrator",
            {
                "prose": prose,
                "dsl": "",
                "segments": [{"kind": "prose", "text": prose}],
            },
        )
        if user is not None:
            self.turns.append(
                TurnRecord(
                    session_id=session_id,
                    text=content,
                    user_id=user.id,
                    user_seq=user.seq,
                    narrative_id=narrative.id,
                    narrative_seq=narrative.seq,
                    prose=prose,
                )
            )
        yield {"type": "committed", "meta": {"session_id": session_id}}
        yield {"type": "done", "meta": {"session_id": session_id}}

    def close(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            except Exception:  # noqa: BLE001 - harness teardown, never fatal
                pass


# ---------------------------------------------------------------------------
# process / protocol helpers (the df_validation_notice harness pattern)
# ---------------------------------------------------------------------------


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


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _post_json(url: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 - local
        return json.load(resp)


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 - local
        return json.load(resp)


def _start_api(log_path: Path, port: int, vite_port: int):
    """Boot ``create_app`` over the scripted engine on a throwaway log."""
    import uvicorn

    from fabulist.api.app import create_app

    # The task requires the env knob too; create_app's explicit argument wins.
    os.environ["FABULIST_DEV_ORIGINS"] = f"http://localhost:{vite_port}"
    engine = ScriptedTurnEngine(log_path)
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
    # connection bound to the thread that opened it.
    threading.Thread(target=serve, daemon=True).start()
    _wait_http(f"http://127.0.0.1:{port}/api/sessions")
    return holder["server"], engine


def _start_vite(port: int, api_port: int, log_file: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["VITE_API_BASE"] = f"http://127.0.0.1:{api_port}"
    handle = open(log_file, "wb")  # noqa: SIM115 - kept for the child's lifetime
    return (
        subprocess.Popen(
            ["npm", "run", "dev", "--", "--port", str(port), "--host", "127.0.0.1"],
            cwd=FRONTEND,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        ),
        handle,
    )


# ---------------------------------------------------------------------------
# browser-side readers
# ---------------------------------------------------------------------------


def _sid(page) -> str | None:
    return page.evaluate("() => new URL(location.href).searchParams.get('s')")


def _rows(page) -> list[dict]:
    """Transcript rows in reading order, with their log anchors and affordances."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('.transcript article.turn')).map((a) => ({
            id: a.id || null,
            user: a.classList.contains('user'),
            text: (a.textContent || '').replace(/\\s+/g, ' ').trim(),
            deletes: a.querySelectorAll('button.delete-btn').length
        }))"""
    )


def _wait_rows(page, count: int, *, timeout: float = 60_000) -> None:
    """Wait for a settled transcript of ``count`` rows, each carrying its log id.

    A live entry has no log id until the post-``done`` reconcile rebuilds it from
    the projection, so this gate is exactly the rehydrate path the delete relies
    on (and the mount path where ``each_key_duplicate`` would surface).
    """
    page.wait_for_function(
        """(n) => {
            const rows = Array.from(document.querySelectorAll('.transcript article.turn'));
            return rows.length === n && rows.every((a) => /^history-\\d+$/.test(a.id || ''));
        }""",
        arg=count,
        timeout=timeout,
    )


def _wait_engine_turns(engine, count: int, *, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(engine.turns) >= count:
            return
        time.sleep(0.05)
    raise RuntimeError(f"engine never committed turn {count} (got {len(engine.turns)})")


def _wait_sid(page, wanted: str, *, timeout: float = 60_000) -> None:
    page.wait_for_function(
        "(want) => new URL(location.href).searchParams.get('s') === want",
        arg=wanted,
        timeout=timeout,
    )


def _rail_titles(page) -> list[str]:
    return page.eval_on_selector_all(
        RAIL_TITLE, "els => els.map((e) => (e.textContent || '').trim())"
    )


def _branch_count_text(page) -> str:
    return page.locator(BRANCH_COUNT).first.evaluate(
        "(el) => (el.textContent || '').replace(/\\s+/g, ' ').trim()"
    )


def _click_delete(page, log_id: int) -> None:
    """Real trusted click on a row's ✂ (hover first: the door is hover-revealed)."""
    article = page.locator(f"#history-{log_id}")
    article.hover()
    article.locator(DELETE_BUTTON).click()


def _send(page, engine, text: str, expected_turns: int, expected_rows: int) -> None:
    page.fill(COMPOSER, text)
    page.click(SEND_BUTTON)
    _wait_engine_turns(engine, expected_turns)
    page.wait_for_selector(STOP_BUTTON, state="detached", timeout=60_000)
    _wait_rows(page, expected_rows)


class DialogGate:
    """The ONE ``page.on('dialog')`` handler: accept/dismiss explicitly.

    Playwright auto-DISMISSES any dialog with no listener, which would silently
    cancel the delete (the exact trap the brief warns about). Recording every
    message also lets the gate assert the N the UI asked about.
    """

    def __init__(self) -> None:
        self.mode = "accept"
        self.messages: list[str] = []

    def __call__(self, dialog) -> None:
        self.messages.append(dialog.message)
        if self.mode == "accept":
            dialog.accept()
        else:
            dialog.dismiss()


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


class Report:
    """PASS/FAIL table with the evidence each assertion needs."""

    def __init__(self) -> None:
        self.checks: dict[int, dict] = {}
        self.evidence: dict[str, object] = {}

    def check(self, number: int, name: str, ok: bool, detail: str = "") -> None:
        self.checks[number] = {"name": name, "ok": ok, "detail": detail}
        print(f"  [{number}] {'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))

    def fail(self, message: str) -> None:
        self.evidence.setdefault("aborted", []).append(message)  # type: ignore[union-attr]
        print(f"  !! {message}")

    @property
    def failed(self) -> list[dict]:
        return [{"number": n, **item} for n, item in self.checks.items() if not item["ok"]]

    def print_table(self) -> None:
        print("\nRESULTS")
        for number in sorted(self.checks):
            item = self.checks[number]
            print(
                f"  {number}. {'PASS' if item['ok'] else 'FAIL'}  {item['name']}"
                + (f"\n       {item['detail']}" if item["detail"] else "")
            )
        aborted = self.evidence.get("aborted") or []
        if aborted:
            print("\nABORTED")
            for message in aborted:
                print(f"  - {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8001)
    parser.add_argument("--vite-port", type=int, default=5212)
    parser.add_argument("--shots", default="/tmp/msgdel-gate")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    work = Path(args.shots)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / "events.db"
    checkpoints = work / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    for stale in (log_path, Path(f"{log_path}-wal"), Path(f"{log_path}-shm")):
        stale.unlink(missing_ok=True)

    started = time.time()
    report = Report()
    page_errors: list[str] = []
    console_errors: list[str] = []
    api_failures: list[str] = []
    delete_requests: list[dict] = []
    delete_responses: list[dict] = []

    _free(args.api_port)
    _free(args.vite_port)
    server, engine = _start_api(log_path, args.api_port, args.vite_port)
    vite, vite_log = _start_vite(args.vite_port, args.api_port, work / "vite.log")
    browser = None
    try:
        _wait_port(args.vite_port)
        base = f"http://127.0.0.1:{args.vite_port}"
        api = f"http://127.0.0.1:{args.api_port}"
        # Setup (not a turn): a titled chat. See the module docstring.
        source_sid = _post_json(f"{api}/api/sessions", {"title": TITLE})["session_id"]

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=CHROME, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(45_000)
            page.set_default_navigation_timeout(90_000)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )

            def on_request(request) -> None:
                if request.method != "POST" or not request.url.endswith("/delete-tail"):
                    return
                delete_requests.append(
                    {"url": request.url, "body": request.post_data}
                )

            def on_response(response) -> None:
                if not response.url.endswith("/delete-tail"):
                    return
                body = None
                try:
                    body = response.json()
                except Exception:  # noqa: BLE001 - evidence only
                    body = None
                delete_responses.append(
                    {"url": response.url, "status": response.status, "body": body}
                )

            def on_request_failed(request) -> None:
                if "/api/" in request.url:
                    api_failures.append(f"requestfailed {request.url}: {request.failure}")

            def on_response_failed(response) -> None:
                if "/api/" in response.url and response.status >= 500:
                    api_failures.append(f"HTTP {response.status} {response.url}")

            page.on("request", on_request)
            page.on("response", on_response)
            page.on("response", on_response_failed)
            page.on("requestfailed", on_request_failed)

            dialogs = DialogGate()
            page.on("dialog", dialogs)

            try:
                # -- assertion 1: three turns, real input ---------------------
                page.goto(f"{base}/chat?s={source_sid}", wait_until="domcontentloaded")
                page.wait_for_selector(COMPOSER, state="visible", timeout=60_000)
                _wait_sid(page, source_sid)
                _wait_rows(page, 0)
                for index, text in enumerate(TURNS, start=1):
                    _send(page, engine, text, index, index * 2)
                rows = _rows(page)
                report.check(
                    1,
                    "three turns created with real composer input",
                    len(engine.turns) == 3
                    and len(rows) == 6
                    and [row["user"] for row in rows]
                    == [True, False, True, False, True, False]
                    and all(turn.text in rows[i * 2]["text"] for i, turn in enumerate(engine.turns)),
                    f"{len(engine.turns)} engine turns, {len(rows)} transcript rows, "
                    f"ids={[row['id'] for row in rows]}",
                )
                page.screenshot(path=str(work / "01-three-turns.png"), full_page=True)

                turn1, turn2, turn3 = engine.turns

                # -- assertion 6: the first message has NO ✂ ------------------
                first = rows[0]
                report.check(
                    6,
                    "the FIRST message has no ✂",
                    first["deletes"] == 0
                    and sum(row["deletes"] for row in rows) == 5
                    and all(row["deletes"] == 1 for row in rows[1:]),
                    f"first row deletes={first['deletes']}, total delete buttons="
                    f"{sum(row['deletes'] for row in rows)} on {len(rows)} rows",
                )

                # -- assertion 7: DISMISS leaves everything untouched ---------
                before_ids = [row["id"] for row in rows]
                before_texts = [row["text"] for row in rows]
                dialogs.mode = "dismiss"
                requests_before = len(delete_requests)
                _click_delete(page, turn3.user_id)
                _wait_for = time.time() + 5
                while time.time() < _wait_for and not dialogs.messages:
                    page.wait_for_timeout(50)
                page.wait_for_timeout(500)
                after_cancel = _rows(page)
                report.check(
                    7,
                    "cancelling the confirm changes nothing",
                    dialogs.messages[-1:] == ["Delete 2 messages?"]
                    and _sid(page) == source_sid
                    and [row["id"] for row in after_cancel] == before_ids
                    and [row["text"] for row in after_cancel] == before_texts
                    and len(delete_requests) == requests_before,
                    f"dialog={dialogs.messages[-1:]}, sid unchanged, "
                    f"{len(after_cancel)} rows unchanged, "
                    f"{len(delete_requests) - requests_before} delete request(s)",
                )
                dialogs.mode = "accept"

                # -- assertion 2: ✂ on an early user message asks the right N --
                pivot_index = next(
                    index
                    for index, row in enumerate(rows)
                    if row["id"] == f"history-{turn2.user_id}"
                )
                expected_n = len(rows) - pivot_index
                _click_delete(page, turn2.user_id)
                deadline = time.time() + 15
                while time.time() < deadline and len(dialogs.messages) < 2:
                    page.wait_for_timeout(50)
                asked = dialogs.messages[-1] if len(dialogs.messages) >= 2 else ""
                report.check(
                    2,
                    "✂ opens window.confirm naming the correct N",
                    asked == f"Delete {expected_n} messages?",
                    f"expected 'Delete {expected_n} messages?', got {asked!r}",
                )
                page.screenshot(path=str(work / "02-confirm-accepted.png"), full_page=True)

                # -- assertion 3: switched, truncated, railed, controls -------
                page.wait_for_function(
                    "(old) => new URL(location.href).searchParams.get('s') !== old",
                    arg=source_sid,
                    timeout=60_000,
                )
                new_sid = _sid(page)
                _wait_rows(page, 2)
                truncated = _rows(page)
                expected_prefix = [turn1.user_id, turn1.narrative_id]
                controlled = new_sid is not None and new_sid != source_sid
                page.wait_for_function(
                    """(title) => Array.from(
                        document.querySelectorAll('aside.drawer ul.chat-rows button.entry .title')
                    ).filter((el) => (el.textContent || '').trim() === title).length >= 2""",
                    arg=TITLE,
                    timeout=30_000,
                )
                titles = _rail_titles(page)
                current_titles = page.eval_on_selector_all(
                    "aside.drawer ul.chat-rows li.current button.entry .title",
                    "els => els.map((e) => (e.textContent || '').trim())",
                )
                page.wait_for_selector(BRANCH_COUNT, state="visible", timeout=30_000)
                count_text = _branch_count_text(page)
                match = re.search(r"(\d+)\s*/\s*(\d+)", count_text)
                count_pair = (int(match.group(1)), int(match.group(2))) if match else None
                kept_ids = [row["id"] for row in truncated]
                last_delete = delete_responses[-1]["body"] if delete_responses else None
                report.check(
                    3,
                    "accept → new branch: URL, truncation, rail row, 2-line controls",
                    controlled
                    and kept_ids == [f"history-{event_id}" for event_id in expected_prefix]
                    and sum(1 for title in titles if title == TITLE) == 2
                    and current_titles == [TITLE]
                    and count_pair is not None
                    and count_pair[1] == 2
                    and (last_delete or {}).get("deleted_count") == expected_n,
                    f"new sid {new_sid} (old {source_sid}); kept ids={kept_ids}; "
                    f"rail titles={titles}; current={current_titles}; "
                    f"controls={count_text!r}; API deleted_count="
                    f"{(last_delete or {}).get('deleted_count')}",
                )
                page.screenshot(path=str(work / "03-after-delete.png"), full_page=True)

                # -- assertion 5: reload keeps the branch + truncation -------
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(COMPOSER, state="visible", timeout=60_000)
                _wait_sid(page, new_sid or "")
                _wait_rows(page, 2)
                reloaded = _rows(page)
                report.check(
                    5,
                    "reload persists the new branch and the truncation",
                    _sid(page) == new_sid
                    and [row["id"] for row in reloaded] == kept_ids,
                    f"sid after reload={_sid(page)}; rows={[row['id'] for row in reloaded]}",
                )
                page.screenshot(path=str(work / "04-after-reload.png"), full_page=True)

                # -- assertion 4: one ‹ brings the FULL old line back --------
                prev = page.locator(BRANCH_PREV)
                prev_enabled = prev.is_enabled()
                prev.click()
                _wait_sid(page, source_sid)
                _wait_rows(page, 6)
                old_rows = _rows(page)
                original_ids = [row["id"] for row in rows]
                report.check(
                    4,
                    "one ‹ swipe restores the full old line",
                    prev_enabled
                    and [row["id"] for row in old_rows] == original_ids
                    and all(
                        turn.text in old_rows[i * 2]["text"]
                        for i, turn in enumerate(engine.turns)
                    ),
                    f"prev enabled={prev_enabled}; sid={_sid(page)}; "
                    f"{len(old_rows)} rows; ids match={[row['id'] for row in old_rows] == original_ids}",
                )
                page.screenshot(path=str(work / "05-old-line.png"), full_page=True)

                # -- assertion 9: inherited-pivot delete (chain-bound fix) ---
                # Back to the new branch through the RAIL (the old line's own
                # branch controls can no longer merge a fork cut that is not its
                # last user seq; the rail is the shipped way back).
                other = page.locator(
                    "aside.drawer ul.chat-rows li:not(.current) button.entry"
                ).filter(
                    has_text=TITLE
                )
                other.first.click()
                _wait_sid(page, new_sid or "")
                _wait_rows(page, 2)

                _send(page, engine, FOURTH, 4, 4)
                _wait_rows(page, 4)
                on_branch = _rows(page)
                turn4 = engine.turns[3]
                # Pivot: turn 1's narrative, INHERITED from the source chain.
                inheritable = turn1.narrative_id
                dialogs_expected = 3  # narr#1 + user#4 + reply#4
                _click_delete(page, inheritable)
                deadline = time.time() + 15
                while time.time() < deadline and len(dialogs.messages) < 3:
                    page.wait_for_timeout(50)
                asked_inherited = dialogs.messages[-1] if len(dialogs.messages) >= 3 else ""
                page.wait_for_function(
                    "(old) => new URL(location.href).searchParams.get('s') !== old",
                    arg=new_sid,
                    timeout=60_000,
                )
                inherited_sid = _sid(page)
                _wait_rows(page, 1)
                inherited_rows = _rows(page)
                branch_payload = _get_json(
                    f"{api}/api/sessions/{inherited_sid}/branches"
                )
                projection = _get_json(
                    f"{api}/api/sessions/{inherited_sid}/events"
                )
                projection_users = [
                    item["payload"]["text"] for item in projection["user_messages"]
                ]
                projection_narratives = [
                    item["payload"]["prose"] for item in projection["narratives"]
                ]
                inherited_response = delete_responses[-1]["body"] if delete_responses else None
                report.check(
                    9,
                    "inherited-pivot delete truncates below the branch's own cut",
                    asked_inherited == f"Delete {dialogs_expected} messages?"
                    and inherited_sid not in (None, source_sid, new_sid)
                    and [row["id"] for row in inherited_rows]
                    == [f"history-{turn1.user_id}"]
                    and (inherited_response or {}).get("deleted_count") == dialogs_expected
                    # The chain-bound proof: this fork cut BELOW the branch's own
                    # base_seq (the delete branch was cut at narr#1, seq 3; this
                    # one cuts at user#1, seq 2) and yet inherits only the prefix.
                    and (inherited_response or {}).get("base_seq") == turn1.user_seq
                    and (inherited_response or {}).get("base_session") == new_sid
                    and (branch_payload.get("parent") or {}).get("session_id") == new_sid
                    and projection_users == [TURNS[0]]
                    and projection_narratives == [],
                    f"new sid={inherited_sid}; dialog={asked_inherited!r}; "
                    f"rows={[row['id'] for row in inherited_rows]}; "
                    f"cut base_seq={(inherited_response or {}).get('base_seq')} "
                    f"(user#1 seq={turn1.user_seq}, inherited narr#1 seq={turn1.narrative_seq}); "
                    f"base_session={(inherited_response or {}).get('base_session')}; "
                    f"projection users={projection_users}; "
                    f"narratives={projection_narratives}; API deleted_count="
                    f"{(inherited_response or {}).get('deleted_count')}",
                )
                page.screenshot(path=str(work / "06-inherited-delete.png"), full_page=True)

                report.evidence.update(
                    {
                        "source_sid": source_sid,
                        "deleted_sid": new_sid,
                        "inherited_sid": inherited_sid,
                        "dialogs": dialogs.messages,
                        "delete_requests": delete_requests,
                        "delete_responses": delete_responses,
                        "turn_rows": [
                            {
                                "text": turn.text,
                                "user_id": turn.user_id,
                                "user_seq": turn.user_seq,
                                "narrative_id": turn.narrative_id,
                                "narrative_seq": turn.narrative_seq,
                            }
                            for turn in engine.turns
                        ],
                        "three_turns_ids": [row["id"] for row in rows],
                        "truncated_ids": kept_ids,
                        "reloaded_ids": [row["id"] for row in reloaded],
                        "old_line_ids": [row["id"] for row in old_rows],
                        "on_branch_ids": [row["id"] for row in on_branch],
                        "inherited_ids": [row["id"] for row in inherited_rows],
                        "rail_titles": titles,
                        "branch_count_text": count_text,
                        "turn4_was_appended": turn4.prose in (on_branch[-1]["text"] if on_branch else ""),
                    }
                )

                # -- assertion 8: zero page / console errors -----------------
                report.check(
                    8,
                    "zero page errors and zero console errors",
                    not page_errors and not console_errors and not api_failures,
                    f"pageerrors={page_errors}; console-errors={console_errors}; "
                    f"api failures={api_failures}",
                )
            except Exception as exc:  # noqa: BLE001 - red gate, with evidence
                report.fail(f"scenario aborted: {type(exc).__name__}: {exc}")
                try:
                    page.screenshot(path=str(work / "failure.png"), full_page=True)
                except Exception:  # noqa: BLE001 - screenshot is best-effort
                    pass
            finally:
                evidence = {
                    **report.evidence,
                    "page_errors": page_errors,
                    "console_errors": console_errors,
                    "api_failures": api_failures,
                    "dialogs": dialogs.messages,
                }
                (work / "evidence.json").write_text(json.dumps(evidence, indent=2))
                lines = [f"PAGEERROR {item}" for item in page_errors]
                lines += [f"CONSOLE-ERROR {item}" for item in console_errors]
                lines += [f"API-FAILURE {item}" for item in api_failures]
                lines += [f"DIALOG {item}" for item in dialogs.messages]
                (work / "console.log").write_text("\n".join(lines) + "\n")
                browser.close()
    finally:
        vite.terminate()
        try:
            vite.wait(timeout=10)
        except subprocess.TimeoutExpired:
            vite.kill()
        try:
            vite_log.close()
        except Exception:  # noqa: BLE001 - teardown only
            pass
        if server is not None:
            server.should_exit = True
        engine.close()
        # The API listener lives IN THIS PROCESS (a serving thread), so
        # ``fuser -k 8001/tcp`` would SIGKILL the gate itself. Stop it
        # gracefully and wait for the socket to close instead; the vite child
        # is a separate process and is torn down by port, as the rules say.
        api_deadline = time.time() + 15
        while time.time() < api_deadline and _port_open(args.api_port):
            time.sleep(0.1)
        _free(args.vite_port)

    elapsed = time.time() - started
    report.print_table()
    print(f"\nevidence: {work}/evidence.json  console: {work}/console.log  "
          f"shots: {work}/*.png")
    print(f"wall time: {elapsed:.1f}s")
    if report.failed or report.evidence.get("aborted"):
        print("\nFAIL — message-deletion gate did not pass")
        return 1
    print(
        "\nPASS — delete N..end forks the chat through the real UI: confirm N "
        "correct, branch switched + truncated + railed, ‹ restores the old line, "
        "reload persists, first message undeletable, cancel is a no-op, and an "
        "inherited-pivot delete obeys the chain bound"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
