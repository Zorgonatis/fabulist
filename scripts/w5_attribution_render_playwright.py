#!/usr/bin/env python3
"""W9.28 W5 — the frontend render guard for the colour-markup contract.

The narrator writes each spoken line inside the speaking character's palette
colour (``<span style="color:#e06c75">"You're late."</span>``) and the ENGINE
attributes speakers from that markup (W1). The frontend's quote scan
(``TranscriptList.quotedSpans``) feeds ``colorMarkdownSource``, which rewrites
the MARKDOWN SOURCE to tint attributed quotes. Two defects lived at that seam:

  * **R2 P0-1 (masking parity).** ``style="color:#e06c75"`` carries two quote
    marks. Scanned raw they are a phantom quote span, and an attribution row
    naming that phantom makes ``colorMarkdownSource`` splice a
    ``<span class="quote">`` INTO the attribute — corrupting the style value.
    The engine masks colour tags before its scan (``state/attribution.py::
    _mask_tags``); W5 masks identically.
  * **double-wrap.** A quote the model already wrapped in its speaker's colour
    got a second wrapper in the same colour.

Plus the one-span-per-line rule: a span the model straddles across a blank line
must render as plain text rather than leak ``<span>``/``</span>`` across two
blocks.

This harness drives the REAL frontend (its own vite dev server) against an
isolated ``create_app`` whose scripted engine commits four narratives — the
colour markup, the attribution rows and the colour registry rows the real
post-turn stages would write — and asserts on the rendered DOM:

  1. model-wrapped line  : ONE span, with the palette colour, no `.quote`
                           wrapper injected, no raw markup text;
  2. unwrapped line      : the engine-colour `.quote` wrapper is still injected
                           (the skip predicate must not disable the feature);
  3. phantom row         : the style attribute is intact (``color:#hex``), no
                           nested span inside an attribute value, no `.quote`;
  4. straddling span     : the paragraph renders as plain text — no colour
                           span, no leaked/raw markup, both blocks intact.

Playwright only (real trusted input, actionability waits) — never synthetic
``.click()`` over raw CDP (AGENTS.md).

Run::

  uv run --with playwright python scripts/w5_attribution_render_playwright.py

  # --shots DIR     : screenshot directory (default /tmp/w5_render)
  # --api-port N    : isolated engine port (default 8122)
  # --vite-port N   : vite dev-server port (default 5212)
"""

from __future__ import annotations

import argparse
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

SPEAKER = "Mara"

COMPOSER = "form.composer textarea"
SEND_BUTTON = "form.composer button.send"
ARTICLE = ".transcript article.turn"


def _rgb(hex_color: str) -> str:
    """The computed-style form of a ``#rrggbb`` colour."""
    value = hex_color.lstrip("#")
    return "rgb({}, {}, {})".format(
        int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    )


def _chunks(text: str, size: int = 40) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class ColourMarkupEngine:
    """Four scripted turns covering the four render cases (see the module doc).

    The engine writes exactly what the real turn writes: the narrate stream, the
    committed ``narrative`` row, and the post-turn ``speaker_color`` +
    ``attribution`` rows the attribution stage appends. The prose is composed
    from the colour the registry actually assigned, so "the model's colour" and
    "the palette colour" are the same hue — the double-wrap condition.
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._log = None  # sqlite3 objects are thread-bound; opened on first turn
        self.narratives: list[dict] = []
        self.colors: dict[str, str] = {}

    def _log_for_write(self):
        from fabulist.log.repo import EventLog

        if self._log is None:
            self._log = EventLog(self._log_path)
        return self._log

    def _color(self, session_id: str) -> str:
        from fabulist.api.cast_api import assign_speaker_color

        log = self._log_for_write()
        if SPEAKER not in self.colors:
            self.colors[SPEAKER] = assign_speaker_color(log, session_id, SPEAKER)
        return self.colors[SPEAKER]

    def _prose(self, index: int, color: str) -> str:
        if index == 0:
            # 1. The narrator wrapped the line itself (the contract's happy path).
            return f'<span style="color:{color}">"You\'re late."</span> she says, without looking up.'
        if index == 1:
            # 2. An untagged spoken line — the frontend still tints it from the row.
            return 'She shrugs at the rain. "Then we walk."'
        if index == 2:
            # 3. The colour span, plus a PHANTOM attribution row whose quote is the
            #    attribute's own text (what a pre-masking quote scan produced).
            return f'<span style="color:{color}">"The road is closed."</span> the guard says.'
        # 4. A span straddling a blank line — the one-span-per-line violation.
        return (
            f'<span style="color:{color}">"First half of the line\n\n'
            'second half of the line."</span> she finishes.'
        )

    def _rows(self, index: int, color: str) -> list[dict]:
        if index == 0:
            return [{"speaker": SPEAKER, "quote": "You're late."}]
        if index == 1:
            return [{"speaker": SPEAKER, "quote": "Then we walk."}]
        if index == 2:
            return [{"speaker": SPEAKER, "quote": f"color:{color}"}]
        return []

    async def run_turn(
        self,
        session_id: str,
        content: str,
        kind: str,
        *,
        image_url: str | None = None,
        image_kind: str = "image",
    ):
        index = len(self.narratives)
        color = self._color(session_id)
        prose = self._prose(index, color)
        for delta in _chunks(prose):
            yield {"type": "content", "delta": delta, "meta": {"channel": "prose"}}
        log = self._log_for_write()
        narrative = log.append_event(
            session_id,
            "narrative",
            "narrator",
            {"prose": prose, "dsl": "", "segments": [{"kind": "prose", "text": prose}]},
        )
        self.narratives.append(
            {"id": narrative.id, "seq": narrative.seq, "prose": prose, "index": index}
        )
        # The post-turn attribution stage: the rows it persists, AFTER the commit
        # and before the turn ends (so the live reconcile sees them).
        log.append_event(
            session_id,
            "system",
            "attribution",
            {"event": "attribution", "attributions": self._rows(index, color)},
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
    import uvicorn

    from fabulist.api.app import create_app

    engine = ColourMarkupEngine(log_path)
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

    # Built inside the serving thread: its EventLog holds a sqlite3 connection,
    # which is bound to the thread that opened it.
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(engine.narratives) > index:
            return engine.narratives[index]
        time.sleep(0.05)
    return None


def _needle_ready(page, anchor: str, needle: str) -> None:
    """Wait until the article for `anchor` renders the turn's prose."""
    page.wait_for_function(
        """([sel, needle]) => {
            const article = document.querySelector(sel);
            return !!article && (article.textContent || '').includes(needle);
        }""",
        arg=[anchor, needle],
        timeout=30_000,
    )


def _no_raw_markup(state: dict) -> bool:
    text = state["text"] or ""
    return "<span" not in text and "&lt;span" not in text


def _prose_state(page, narrative_id: int) -> dict | None:
    """Everything the assertions need from one rendered article's prose."""
    return page.evaluate(
        """(sel) => {
            const article = document.querySelector(sel);
            if (!article) return null;
            const blocks = Array.from(article.querySelectorAll('.prose'));
            const spans = [];
            const attrs = [];
            for (const block of blocks) {
                for (const el of block.querySelectorAll('*')) {
                    spans.push({
                        tag: el.tagName.toLowerCase(),
                        cls: el.getAttribute('class'),
                        style: el.getAttribute('style'),
                        color: getComputedStyle(el).color,
                        text: (el.textContent || '').trim()
                    });
                    for (const attr of Array.from(el.attributes)) attrs.push(attr.value);
                }
            }
            const styleAttr = Array.from(article.querySelectorAll('.prose span'))
                .map((el) => el.getAttribute('style'))
                .filter((v) => v !== null);
            return {
                text: blocks.map((b) => b.textContent || '').join('\\n'),
                html: blocks.map((b) => b.innerHTML).join('\\n'),
                quoteCount: article.querySelectorAll('.prose span.quote').length,
                colourSpans: spans.filter((s) => (s.style || '').includes('color')),
                colourSpanCount: spans.filter((s) => (s.style || '').includes('color')).length,
                attrValues: attrs,
                styleAttrs: styleAttr,
                paragraphs: Array.from(article.querySelectorAll('.prose p')).length
            };
        }""",
        f"#history-{narrative_id}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8122)
    parser.add_argument("--vite-port", type=int, default=5212)
    parser.add_argument("--shots", default="/tmp/w5_render")
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
    #: label -> {"live": state after the turn, "settled": state after a reload}.
    states: dict[str, dict] = {}
    page_errors: list[str] = []
    console_errors: list[str] = []
    try:
        _wait_port(args.vite_port)
        base = f"http://127.0.0.1:{args.vite_port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.goto(f"{base}/chat", wait_until="domcontentloaded")

            labels = ["wrapped", "unwrapped", "phantom", "straddle"]
            # A plain-text fragment per turn: the markers are markup, so the needle
            # must be prose that survives both the render and the plain-text
            # downgrade (turn 4).
            needles = {
                "wrapped": "without looking up",
                "unwrapped": "Then we walk.",
                "phantom": "the guard says",
                "straddle": "First half of the line",
            }
            for index, label in enumerate(labels):
                page.fill(COMPOSER, f"Turn {index + 1}.")
                page.click(SEND_BUTTON)
                narrative = _await_narrative(engine, index)
                if narrative is None:
                    failures.append(f"engine never committed turn {index + 1} ({label})")
                    continue
                anchor = f"#history-{narrative['id']}"
                page.wait_for_selector(anchor, state="attached", timeout=30_000)
                # The live entry is rebuilt from the committed payload post-`done`
                # (D-C), so wait for the rendered article to carry the turn's prose.
                _needle_ready(page, anchor, needles[label])
                page.wait_for_timeout(400)
                live = _prose_state(page, narrative["id"])
                page.screenshot(path=str(work / f"{index + 1}-{label}-live.png"), full_page=True)
                # The attribution rows + colour registry land through
                # `loadQuoteColorData`'s effect, which drops an in-flight load when
                # a same-shape re-render lands on top of it (a pre-existing
                # live-path race in the W6C-2 colour loader, outside this lane).
                # A reload is the deterministic way to read the SAME render path
                # with the rows present — the D-C "live == reload" invariant.
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(anchor, state="attached", timeout=30_000)
                _needle_ready(page, anchor, needles[label])
                page.wait_for_timeout(600)
                settled = _prose_state(page, narrative["id"])
                page.screenshot(
                    path=str(work / f"{index + 1}-{label}-settled.png"), full_page=True
                )
                states[label] = {"live": live, "settled": settled}
            browser.close()
    finally:
        if vite.poll() is None:
            vite.terminate()
        try:
            vite.wait(timeout=10)
        except subprocess.TimeoutExpired:
            vite.kill()
        engine.close()
        server.should_exit = True

    color = engine.colors.get(SPEAKER, "")
    expected_rgb = _rgb(color) if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else ""
    print(f"palette colour : {color} -> {expected_rgb}")
    for label, pair in states.items():
        for phase in ("live", "settled"):
            state = pair.get(phase)
            if state is None:
                print(f"{label:10s} {phase:8s}: MISSING")
                continue
            print(
                f"{label:10s} {phase:8s}: quote={state['quoteCount']} "
                f"colourSpans={state['colourSpanCount']} "
                f"styles={state['styleAttrs']} paragraphs={state['paragraphs']}"
            )

    # -- 1. the model's own span renders once, in the palette colour ----------
    wrapped = states.get("wrapped", {})
    for phase, state in wrapped.items():
        if state is None:
            failures.append(f"the wrapped turn never rendered ({phase})")
            continue
        if not _no_raw_markup(state):
            failures.append(f"[{phase}] raw markup leaked as text: {state['text']!r}")
    settled = wrapped.get("settled")
    if settled:
        if settled["quoteCount"] != 0:
            failures.append(
                "a quote the narrator already coloured got a second (injected) "
                f"wrapper: {settled['quoteCount']} `.quote` spans"
            )
        if settled["colourSpanCount"] != 1:
            failures.append(
                f"expected exactly 1 colour span on the wrapped line, got "
                f"{settled['colourSpanCount']}: {settled['colourSpans']}"
            )
        elif settled["colourSpans"][0]["color"] != expected_rgb:
            failures.append(
                "the model's colour span did not render in the palette colour: "
                f"{settled['colourSpans'][0]['color']} != {expected_rgb}"
            )
        if settled["styleAttrs"] != [f"color:{color}"]:
            failures.append(
                f"the model's style attribute was rewritten: {settled['styleAttrs']}"
            )

    # -- 2. an unwrapped attributed line still gets the engine colour ---------
    unwrapped = states.get("unwrapped", {}).get("settled")
    if unwrapped is None:
        failures.append("the untagged turn never rendered")
    else:
        if unwrapped["quoteCount"] != 1:
            failures.append(
                f"expected 1 injected `.quote` span on the untagged line, got "
                f"{unwrapped['quoteCount']}"
            )
        if unwrapped["colourSpanCount"] != 1:
            failures.append(
                f"expected the injected wrapper to carry the colour, got "
                f"{unwrapped['colourSpanCount']} colour spans"
            )
        elif unwrapped["colourSpans"][0]["color"] != expected_rgb:
            failures.append(
                "the injected wrapper is not the palette colour: "
                f"{unwrapped['colourSpans'][0]['color']} != {expected_rgb}"
            )
        if "Then we walk." not in (unwrapped["text"] or ""):
            failures.append(f"the spoken line did not render: {unwrapped['text']!r}")

    # -- 3. a phantom (pre-masking) row cannot splice into the attribute ------
    for phase, phantom in states.get("phantom", {}).items():
        if phantom is None:
            failures.append(f"the phantom turn never rendered ({phase})")
            continue
        if phantom["quoteCount"] != 0:
            failures.append(
                f"[{phase}] a phantom attribution row spliced a `.quote` wrapper "
                f"into the colour markup: {phantom['quoteCount']} `.quote` spans — "
                f"{phantom['html']!r}"
            )
        if phantom["styleAttrs"] != [f"color:{color}"]:
            failures.append(
                f"[{phase}] the colour attribute is not intact "
                f"(expected ['color:{color}']): {phantom['styleAttrs']}"
            )
        spliced = [v for v in phantom["attrValues"] if "<span" in v or "class=" in v]
        if spliced:
            failures.append(f"[{phase}] markup nested inside an attribute value: {spliced}")
        if not _no_raw_markup(phantom):
            failures.append(f"[{phase}] raw markup leaked as text: {phantom['text']!r}")

    # -- 4. a straddling span renders as plain text, never leaks --------------
    for phase, straddle in states.get("straddle", {}).items():
        if straddle is None:
            failures.append(f"the straddling turn never rendered ({phase})")
            continue
        if straddle["colourSpanCount"] != 0:
            failures.append(
                f"[{phase}] a straddling span was not downgraded to plain text: "
                f"{straddle['colourSpans']}"
            )
        if not _no_raw_markup(straddle):
            failures.append(f"[{phase}] straddling markup leaked as text: {straddle['text']!r}")
        if "First half of the line" not in (straddle["text"] or ""):
            failures.append(
                f"[{phase}] the straddling prose did not render: {straddle['text']!r}"
            )
        if straddle["paragraphs"] < 2:
            failures.append(
                f"[{phase}] the two blocks did not render as separate paragraphs: "
                f"{straddle['paragraphs']}"
            )

    if page_errors:
        failures.append(f"page errors: {page_errors}")
    if console_errors:
        failures.append(f"console errors: {console_errors}")

    if failures:
        print("\nFAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS — model span rendered as-is; no phantom wrapper; no attribute "
          "splice; straddle downgraded to plain text")
    return 0


if __name__ == "__main__":
    sys.exit(main())
