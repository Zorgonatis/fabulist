#!/usr/bin/env python3
"""Issue #40 live acceptance gate — typed card imports + rail retirement.

Runs the SHIPPED SvelteKit UI in a real browser (Playwright, trusted input,
actionability waits) against an ISOLATED engine built in-thread from
``fabulist.wiring.build_app`` over a REAL engine (RegistryStore + sqlite
checkpointer on throwaway ``/tmp`` paths). No model is called: this gate reads
and writes DEFINITIONS only, so the engine needs no reachable narrator.

Assertions (each reported PASS/FAIL in the run's table):

  1. rail (top left): NO import-card control exists any more — the lore hint
     renders in the vacated slot (world name, two-line-clamped scenario, book
     list), and a chat with no lore shows the quiet one-line empty state;
  2. the lore hint's affordances still work — attach a book to a real chat and
     clear it again, through the UI;
  3. World studio: its ONE "Import" door (issue #60) imports a PNG card WITH a
     ``character_book`` through the single entry point
     (``POST /api/cards/import?world=1``) — the world opens in the editor,
     name/description/scenario come from the card, the book is in the Books
     studio catalog and the world's image IS the imported PNG bytes;
  4. a PNG card WITHOUT a lorebook still imports (book absent), inline status
     names it, no error;
  5. a non-card file renamed ``.json`` → a precise inline message, no world
     created (the door's own JSON parse refuses before any write);
  6. the same door also takes an exported/hand-written world JSON (no separate
     "Import world" control exists any more);
  7. the Persona studio's door is the same control and imports a persona PNG;
  8. chat ↔ studio ↔ chat re-mounts (the interactive mount path where Svelte's
     ``each_key_duplicate`` would fire) raise ZERO page errors and no
     unexpected console error across the whole run;
  9. after a reload the imported world and book persist.

Playwright only — never synthetic ``.click()`` over raw CDP (AGENTS.md: that
produced phantom 500s and masked real bugs). Isolated verification rules also
apply: spare ports, throwaway ``/tmp`` paths, ``FABULIST_DEV_ORIGINS`` for the
vite origin, kill the spawned vite server BY PORT (``fuser -k``), never
``pkill -f``. The API listener lives in THIS process, so it is stopped
gracefully (``fuser -k`` on it would SIGKILL the gate itself).

Prerequisites: ``frontend/node_modules`` must exist (``cd frontend && npm ci``) —
a fresh worktree has none, and the gate spawns ``npm run dev`` from it.

Run::

  uv run --with playwright python scripts/import40_playwright.py

  # --dir DIR      evidence root (default /tmp/import40-gate): screenshots,
  #                fixtures, the engine log/checkpoints, evidence.json, vite.log
  # --api-port N   isolated engine port (default 8131)
  # --vite-port N  vite dev-server port (default 5261)
  # --headed       show the browser

Exit status: 0 when all nine assertions PASS, 1 otherwise (the table names the
failing assertion and the evidence dir holds the screenshot). Evidence is
rewritten per run — stale failure shots are removed at start.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

DEFAULT_API_PORT = 8131
DEFAULT_VITE_PORT = 5261
DEFAULT_DIR = Path("/tmp/import40-gate")

ARM = 9  # assertion count

#: Console noise that is NOT a failure: the browser's own favicon probe, the dev
#: server's HMR chatter, and the two DELIBERATE network probes this gate makes —
#: the inline-422 rejection (assertion 5) and the definition-image 404 for a
#: world that has no picture (the pre-existing "no image" answer, see
#: ``fetchDefinitionImage``). Each classified message is written to the evidence
#: file, so a filtered error is never silently swallowed.
BENIGN_CONSOLE: tuple[str, ...] = ("favicon", "[vite]")


def log(msg: str) -> None:
    print(msg, flush=True)


class GateError(AssertionError):
    """A failed assertion (not a harness crash)."""


# ---------------------------------------------------------------------------
# report: per-assertion PASS/FAIL with sub-checks
# ---------------------------------------------------------------------------


class Report:
    """Collects sub-checks per numbered assertion; an assertion PASSes iff clean."""

    def __init__(self) -> None:
        self.checks: dict[int, int] = {n: 0 for n in range(1, ARM + 1)}
        self.failures: dict[int, list[str]] = {n: [] for n in range(1, ARM + 1)}
        self.titles: dict[int, str] = {}
        self.details: dict[int, list[str]] = {n: [] for n in range(1, ARM + 1)}

    def section(self, num: int, title: str) -> None:
        # The title is set ONCE (a later phase of the same assertion logs its own
        # label without renaming the report row).
        self.titles.setdefault(num, title)
        log(f"\n-- assertion {num}: {title}")

    def check(self, num: int, condition: bool, message: str) -> bool:
        self.checks[num] += 1
        if condition:
            log(f"  PASS  [A{num}] {message}")
            return True
        self.failures[num].append(message)
        log(f"  FAIL  [A{num}] {message}")
        return False

    def fail(self, num: int, message: str) -> None:
        self.checks[num] += 1
        self.failures[num].append(message)
        log(f"  FAIL  [A{num}] {message}")

    def note(self, num: int, message: str) -> None:
        self.details[num].append(message)
        log(f"  note  [A{num}] {message}")

    def status(self, num: int) -> str:
        return "PASS" if not self.failures[num] else "FAIL"

    def table(self) -> str:
        rows = [f"{'#':<3} {'RESULT':<6} ASSERTION"]
        for num in range(1, ARM + 1):
            title = self.titles.get(num, "(not reached)")
            rows.append(f"{num:<3} {self.status(num):<6} {title}")
        return "\n".join(rows)


REPORT = Report()


def section(num: int, title: str) -> None:
    """Begin (or continue) a numbered assertion's section of the report."""
    REPORT.section(num, title)

#: Evidence collected for the report (API bodies, filtered console noise).
EVIDENCE: dict[str, object] = {"api": {}, "console": [], "page_errors": []}


# ---------------------------------------------------------------------------
# fixtures: REAL card PNGs (the same STv2 tEXt construction the repo's tests use)
# ---------------------------------------------------------------------------


def _chunk(ctype: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def card_png(chara_json: dict, *, side: int = 8, rgb: tuple[int, int, int] = (46, 120, 160)) -> bytes:
    """An STv2/STv1 card embedded in a PNG ``chara`` tEXt chunk.

    Same construction as ``tests/test_worlds_import.py::_png`` /
    ``tests/test_cards_api.py`` (CRC-validated ``chara`` tEXt carrying
    base64 card JSON) — with one deliberate addition: a REAL ``IDAT`` scanline
    payload, so the bytes are a decodable image. The repo's test helper writes a
    header-only PNG (enough to PARSE a card, not enough for a browser to DRAW
    one); this gate asserts the world's image is the PNG *as rendered*, so the
    fixture must actually decode.
    """
    from fabulist.cards import PNG_SIGNATURE

    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
    payload = base64.b64encode(json.dumps(chara_json).encode("utf-8"))
    text = _chunk(b"tEXt", b"chara\x00" + payload)
    scanlines = b"".join(b"\x00" + bytes(rgb) * side for _ in range(side))
    idat = _chunk(b"IDAT", zlib.compress(scanlines, 9))
    return PNG_SIGNATURE + ihdr + text + idat + _chunk(b"IEND", b"")


#: Long enough to exceed two rail lines, so the clamp is OBSERVED not assumed.
WORLD_SCENARIO = (
    "A rain-soaked night market, two bells past dusk. Lanterns gutter along the "
    "drowned quarter while the cartographers argue over a coastline that moved "
    "overnight, and somewhere below the boardwalk a bell rings that nobody hung."
)

CARD_WITH_BOOK = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Lyra Voss",
        "description": "A wandering cartographer who maps the drowned quarter.",
        "scenario": WORLD_SCENARIO,
        "character_book": {
            "name": "Night Market Lore",
            "entries": [
                {
                    "keys": ["lantern"],
                    "content": "The night market opens only after the second bell.",
                    "insertion_order": 1,
                },
                {
                    "keys": ["drowned quarter", "boardwalk"],
                    "content": "The drowned quarter floods at every high tide; the boardwalk "
                    "is rebuilt each spring on the old pilings.",
                    "insertion_order": 2,
                },
                {
                    "keys": ["cartographer"],
                    "content": "Cartographers guild-sworn to the observatory never map the "
                    "coast from memory — the guild treats a remembered coastline as a lie.",
                    "insertion_order": 3,
                },
            ],
        },
        "creator": "guild",
        "character_version": "1.2",
    },
}

CARD_NO_BOOK = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Rook",
        "description": "A quiet archivist with too many keys.",
        "scenario": "The stacks below the observatory.",
    },
}

PERSONA_CARD = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Mira Quill",
        "description": "A court scribe with a sharp memory and sharper pen.",
        "personality": "Precise, dry, quietly amused.",
        "scenario": "The scriptorium at dawn, ink still cold.",
        "first_mes": "You are late. Sit — the deposition started without you.",
        "tags": ["scribe", "court"],
    },
}

WORLD_JSON = {
    "name": "Harbour District",
    "description": "Warehouses, rope-walks and a tide that keeps its own hours.",
    "scenario": "The harbourmaster's office at the turn of the tide.",
    "cast": [],
    "book_ids": [],
}

BAD_JSON_TEXT = b"This is a plain text file wearing a .json extension.\n"


def build_fixtures(directory: Path) -> dict[str, Path]:
    """Write the four card/world fixtures; return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "card_with_book": directory / "lyra-with-lorebook.png",
        "card_no_book": directory / "rook-no-lorebook.png",
        "persona": directory / "mira-persona.png",
        "world_json": directory / "harbour-district.world.json",
        "bad_json": directory / "not-a-card.json",
    }
    paths["card_with_book"].write_bytes(card_png(CARD_WITH_BOOK))
    paths["card_no_book"].write_bytes(card_png(CARD_NO_BOOK))
    paths["persona"].write_bytes(card_png(PERSONA_CARD))
    paths["world_json"].write_text(json.dumps(WORLD_JSON, indent=2))
    paths["bad_json"].write_bytes(BAD_JSON_TEXT)
    for name, path in paths.items():
        log(f"fixture {name}: {path} ({path.stat().st_size} bytes)")
    return paths


# ---------------------------------------------------------------------------
# process / engine helpers
# ---------------------------------------------------------------------------


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


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
    raise SystemExit(f"timed out waiting for {url}")


def _wait_port(port: int, *, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return
        time.sleep(0.2)
    raise SystemExit(f"timed out waiting for :{port}")


def _free_by_port(port: int) -> None:
    """Kill whatever LISTENS on ``port`` (never ``pkill -f``, AGENTS.md)."""
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, check=False)


def get_json(url: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310 - local
        return json.loads(resp.read().decode() or "{}")


def get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - local
        return resp.read()


def boot_engine(config: Path, log_path: Path, ckpt_path: Path, port: int) -> dict:
    """``build_app`` (the ONE build path) over a real engine, served in-thread."""
    import uvicorn

    from fabulist.connections.registry import RegistryStore
    from fabulist.graph.graph import open_sqlite_checkpointer
    from fabulist.wiring import build_app

    holder: dict[str, object] = {}

    def serve() -> None:
        async def run() -> None:
            registry = RegistryStore(config)
            if not registry.connections:
                log("  note  no connections in the config copy (definitions need none)")
            # The checkpointer is an async CM; the EventLog binds to the thread
            # that opened it, so app construction happens INSIDE this thread.
            async with open_sqlite_checkpointer(ckpt_path) as checkpointer:
                app = build_app(registry=registry, log_path=log_path, checkpointer=checkpointer)
                app.state.connections_store = registry
                holder["app"] = app
                server = uvicorn.Server(
                    uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
                )
                holder["server"] = server
                await server.serve()

        asyncio.run(run())

    started = time.time()
    threading.Thread(target=serve, daemon=True).start()
    _wait_http(f"http://127.0.0.1:{port}/api/sessions")
    log(f"engine up on :{port} in {time.time() - started:.1f}s (real build_app)")
    return holder


def start_vite(port: int, api_port: int, log_file: Path) -> tuple[subprocess.Popen, object]:
    env = dict(os.environ)
    env["VITE_API_BASE"] = f"http://127.0.0.1:{api_port}"
    handle = open(log_file, "wb")  # noqa: SIM115 - kept for the child's lifetime
    process = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(port), "--host", "127.0.0.1"],
        cwd=FRONTEND,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_port(port)
        _wait_http(f"http://127.0.0.1:{port}/chat")
    except SystemExit:
        _free_by_port(port)  # a half-started dev server must not outlive the gate
        handle.close()
        raise
    log(f"vite up on :{port} (API base http://127.0.0.1:{api_port})")
    return process, handle


# ---------------------------------------------------------------------------
# browser helpers
# ---------------------------------------------------------------------------


def shot(page, shots: Path, name: str) -> None:
    path = shots / name
    try:
        page.screenshot(path=str(path))
        log(f"  shot  {path}")
    except Exception as err:  # noqa: BLE001 - evidence is best-effort
        log(f"  shot FAILED {path}: {err}")


def choose_file(page, trigger, path: Path) -> None:
    """Open a hidden ``<input type=file>`` by CLICKING its visible door.

    The click is real trusted input (the door itself is under test); the file
    chooser is the browser's own path, so the input's ``onchange`` runs exactly
    as it does for a user.
    """
    with page.expect_file_chooser() as chooser_info:
        trigger.click()
    chooser_info.value.set_files(str(path))


def wait_visible(locator, *, timeout: float = 20_000) -> bool:
    """Wait for ``locator`` to be visible; True when it appears, False on timeout."""
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        locator.first.wait_for(state="visible", timeout=timeout)
        return True
    except PWTimeout:
        return False


def wait_text(locator, needle: str, *, timeout: float = 30_000) -> str:
    """Wait until ``locator`` is visible and its text contains ``needle``."""
    deadline = time.time() + timeout / 1000
    last = ""
    while time.time() < deadline:
        try:
            if locator.count():
                text = locator.first.inner_text().strip()
                last = text
                if needle in text:
                    return text
        except Exception:  # noqa: BLE001 - detached mid-read (re-render)
            pass
        time.sleep(0.1)
    raise GateError(f"text {needle!r} never appeared (last: {last!r})")


def rail(page):
    return page.locator("aside.drawer").first


def studio(page):
    """The Design Studio frame, docked or floating (both carry the same label)."""
    return page.locator('[aria-label="Design studio"]').first


def open_studio(page) -> None:
    """Open the studio from the shell header, retrying the pre-hydration window.

    The header is SSR-rendered and visible BEFORE hydration, so a click landing in
    that window is swallowed by an un-hydrated button (the classic SSR-first trap
    that looks exactly like "the door is broken"). The click is retried until the
    frame is on screen — no input is faked.
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    if studio(page).count() and studio(page).is_visible():
        return
    button = page.get_by_role(
        "button", name="Design studio — define personas, worlds, books, assistants"
    )
    for _attempt in range(6):
        button.click()
        try:
            studio(page).wait_for(state="visible", timeout=5_000)
            return
        except PWTimeout:
            page.wait_for_timeout(400)
    raise GateError("the design studio never opened")


def close_studio(page) -> None:
    from playwright.sync_api import TimeoutError as PWTimeout

    if not (studio(page).count() and studio(page).is_visible()):
        return
    for _attempt in range(4):
        try:
            studio(page).get_by_role("button", name="Close design studio").click()
            studio(page).wait_for(state="detached", timeout=5_000)
            return
        except PWTimeout:
            page.wait_for_timeout(300)
    raise GateError("the design studio never closed")


def studio_tab(page, label: str) -> None:
    from playwright.sync_api import expect

    tab = studio(page).locator('nav.tabs[aria-label="Entity kinds"]').get_by_role(
        "button", name=label, exact=True
    )
    tab.click()
    expect(tab).to_have_attribute("aria-pressed", "true", timeout=20_000)


def chat_row(page, title: str):
    rows = rail(page).locator("ul.chat-rows button.entry")
    for index in range(rows.count()):
        row = rows.nth(index)
        label = row.locator(".title")
        if label.count() and label.inner_text().strip() == title:
            return row
    raise GateError(f"no chat row titled {title!r} in the rail")


def open_chat(page, title: str) -> None:
    """Click a rail chat row and wait until the app agrees it is CURRENT."""
    chat_row(page, title).click()
    page.wait_for_function(
        """(title) => {
            const rows = Array.from(document.querySelectorAll('aside.drawer ul.chat-rows li'));
            const row = rows.find(
                (candidate) => (candidate.querySelector('.title')?.textContent || '').trim() === title
            );
            return !!row && row.classList.contains('current');
        }""",
        arg=title,
        timeout=30_000,
    )


# ---------------------------------------------------------------------------
# assertion 1 (part a): the rail's top slot — no import door, quiet empty state
# ---------------------------------------------------------------------------


def assert_rail_retired(page, shots: Path) -> None:
    section(1, "rail: no import-card control; lore hint in its place")

    drawer = rail(page)
    drawer.wait_for(state="visible", timeout=30_000)
    wait_text(drawer.locator("h2.section-label"), "Chats")

    # The retired control must be GONE — not merely hidden: no card-import
    # button, no image file picker, no leftover rail-import block.
    import_buttons = drawer.get_by_role("button", name=re.compile(r"import card", re.I))
    REPORT.check(1, import_buttons.count() == 0, "no 'Import card' control in the rail")
    png_inputs = drawer.locator('input[type="file"][accept*=".png"]')
    REPORT.check(1, png_inputs.count() == 0, "no PNG card file-picker in the rail")
    REPORT.check(
        1, drawer.locator(".rail-import").count() == 0, "the .rail-import block is gone"
    )

    # The lore hint occupies that slot, and it sits ABOVE the asset layers.
    hint = drawer.locator(".session-world")
    hint.wait_for(state="visible", timeout=30_000)
    worlds_layer = drawer.locator('section.layer[aria-label="Worlds"]')
    hint_box = hint.bounding_box()
    worlds_box = worlds_layer.bounding_box()
    REPORT.check(1, hint_box is not None and worlds_box is not None, "hint + layers measured")
    if hint_box and worlds_box:
        REPORT.check(
            1,
            hint_box["y"] < worlds_box["y"],
            f"the hint renders ABOVE the Worlds layer (y {hint_box['y']:.0f} < {worlds_box['y']:.0f})",
        )
        REPORT.check(
            1,
            hint_box["x"] < page.viewport_size["width"] / 2,
            f"the hint is in the top-left rail (x {hint_box['x']:.0f})",
        )

    # A chat with no lore: the QUIET empty state (one dim line, no "Lore" head).
    empty = drawer.locator(".sw-none")
    empty_text = empty.first.inner_text().strip() if empty.count() else "(missing)"
    REPORT.check(
        1,
        empty.count() == 1 and empty_text == "No lore attached.",
        f"a loreless chat shows the quiet empty state ({empty_text!r})",
    )
    REPORT.check(1, drawer.locator(".sw-books").count() == 0, "no book list on a loreless chat")
    labels = [t.strip() for t in drawer.locator(".sw-label").all_inner_texts()]
    REPORT.check(
        1, "Lore" not in labels, f"no empty 'Lore' section head is announced ({labels})"
    )
    shot(page, shots, "01-rail-no-import-quiet-empty.png")


# ---------------------------------------------------------------------------
# assertion 3: World studio card door — PNG WITH a lorebook
# ---------------------------------------------------------------------------


def assert_world_card_import(page, shots: Path, fixtures: dict[str, Path], api: str) -> None:
    section(3, "World studio: the ONE 'Import' door takes a PNG card WITH lorebook")
    open_studio(page)
    studio_tab(page, "Worlds")

    pane = studio(page).locator("div.world-pane")
    pane.wait_for(state="visible", timeout=30_000)
    door = pane.get_by_role("button", name="Import", exact=True)
    REPORT.check(3, door.count() == 1, "the world pane has exactly ONE import control")
    REPORT.check(
        3,
        pane.get_by_role("button", name=re.compile(r"import .*(card|json|png)", re.I)).count() == 0,
        "no import control names a file type any more",
    )

    with page.expect_response(
        lambda r: "/api/cards/import" in r.url and r.request.method == "POST",
        timeout=60_000,
    ) as response_info:
        choose_file(page, door, fixtures["card_with_book"])
    response = response_info.value
    body = response.json()
    EVIDENCE["api"]["card_import_as_world_with_book"] = {
        "status": response.status,
        "url": response.url,
        "body": body,
    }
    REPORT.check(3, response.status == 200 and body.get("ok") is True, f"POST /api/cards/import?world=1 -> {response.status}")
    REPORT.check(3, "world=1" in response.url, f"the door posts the world target ({response.url})")
    REPORT.check(3, bool(body.get("book_id")), f"the card's lorebook materialized ({body.get('book_id')})")
    world_id = (body.get("world") or {}).get("world_id")

    # Inline status names what landed; no inline error.
    status = pane.locator("p.status")
    text = wait_text(status, "Imported")
    REPORT.check(3, "Lyra Voss" in text, f"the status names the imported world ({text!r})")
    REPORT.check(3, pane.locator("p.error").count() == 0, "no inline error is shown")

    # The world opens in the editor, carrying the card's prose.
    editor = pane.locator("div.editor-col")
    wait_text(editor.locator("h4"), "Edit world")
    form = editor.locator("form.world-form")
    name_value = form.locator('input[id$="-name"]').input_value()
    REPORT.check(3, name_value == "Lyra Voss", f"editor Name = the card's name ({name_value!r})")
    REPORT.check(
        3,
        form.locator('textarea[id$="-description"]').input_value() == CARD_WITH_BOOK["data"]["description"],
        "editor Description = the card's description",
    )
    REPORT.check(
        3,
        form.locator('textarea[id$="-scenario"]').input_value() == WORLD_SCENARIO,
        "editor Scenario = the card's scenario",
    )
    book_checkbox = form.locator("fieldset", has_text="Lore books").get_by_role(
        "checkbox", name="Night Market Lore"
    )
    REPORT.check(
        3,
        book_checkbox.count() == 1 and book_checkbox.is_checked(),
        "the editor's Lore books ticks the card's book",
    )
    labels = [t.strip() for t in pane.locator(".list-col .row-label").all_inner_texts()]
    REPORT.check(3, "Lyra Voss" in labels, f"the world is in the pane's list ({labels})")

    # The world's image IS the imported PNG: the editor avatar's blob bytes and
    # the served image bytes both equal the fixture.
    image_b64 = page.evaluate(
        """async () => {
            const img = document.querySelector('div.editor-col span.editor-avatar img');
            if (!img) return null;
            const res = await fetch(img.src);
            const buf = new Uint8Array(await res.arrayBuffer());
            let out = '';
            for (const byte of buf) out += String.fromCharCode(byte);
            return btoa(out);
        }"""
    )
    wanted = base64.b64encode(fixtures["card_with_book"].read_bytes()).decode()
    REPORT.check(3, image_b64 is not None, "the editor renders an image (not the initials fallback)")
    REPORT.check(3, image_b64 == wanted, "the editor's avatar bytes ARE the imported PNG")
    decoded = page.evaluate(
        """() => {
            const img = document.querySelector('div.editor-col span.editor-avatar img');
            return img ? {w: img.naturalWidth, h: img.naturalHeight, complete: img.complete} : null;
        }"""
    )
    REPORT.check(
        3,
        bool(decoded) and decoded["complete"] and decoded["w"] > 0,
        f"the browser DECODES the world image ({decoded})",
    )
    served = get_bytes(f"{api}/api/worlds/{world_id}/image")
    EVIDENCE["api"]["world_image_bytes"] = len(served)
    REPORT.check(3, served == fixtures["card_with_book"].read_bytes(), "GET /api/worlds/{id}/image serves the PNG bytes")

    EVIDENCE["api"]["worlds_after_card"] = get_json(f"{api}/api/worlds")
    shot(page, shots, "02-world-studio-card-import.png")

    # (part of 3) the book is visible in the Books studio catalog
    studio_tab(page, "Books")
    catalog = studio(page).locator('section[aria-label="Book catalog"]')
    catalog.wait_for(state="visible", timeout=30_000)
    wait_text(catalog, "Night Market Lore")
    rows = [t.strip() for t in catalog.locator(".row-label").all_inner_texts()]
    metas = [t.strip() for t in catalog.locator(".row-meta").all_inner_texts()]
    REPORT.check(3, "Night Market Lore" in rows, f"the Books studio catalog lists the book ({rows})")
    REPORT.check(3, "3 entries" in metas, f"the catalog reports the book's entries ({metas})")
    books = get_json(f"{api}/api/books")["books"]
    REPORT.check(
        3,
        [b["book_id"] for b in books] == [body["book_id"]],
        "the catalog's one book IS the world's book_id",
    )
    shot(page, shots, "03-books-studio-catalog.png")


# ---------------------------------------------------------------------------
# assertion 4: a PNG card WITHOUT a lorebook still imports
# ---------------------------------------------------------------------------


def assert_world_card_no_lorebook(page, shots: Path, fixtures: dict[str, Path], api: str) -> None:
    section(4, "World studio: a card WITHOUT a lorebook imports fine (no error)")
    studio_tab(page, "Worlds")
    pane = studio(page).locator("div.world-pane")
    pane.wait_for(state="visible", timeout=30_000)
    books_before = len(get_json(f"{api}/api/books")["books"])
    worlds_before = len(get_json(f"{api}/api/worlds")["worlds"])

    with page.expect_response(
        lambda r: "/api/cards/import" in r.url and r.request.method == "POST",
        timeout=60_000,
    ) as response_info:
        choose_file(page, pane.get_by_role("button", name="Import", exact=True), fixtures["card_no_book"])
    response = response_info.value
    body = response.json()
    EVIDENCE["api"]["card_import_as_world_no_book"] = {"status": response.status, "body": body}
    REPORT.check(4, response.status == 200 and body.get("ok") is True, f"import -> {response.status}")
    REPORT.check(4, body.get("book_id") is None, f"no book was created ({body.get('book_id')})")

    text = wait_text(pane.locator("p.status"), "Imported")
    REPORT.check(4, "Rook" in text, f"the inline status names the new world ({text!r})")
    REPORT.check(4, "lorebook" not in text, "the status does not claim a lorebook")
    REPORT.check(4, pane.locator("p.error").count() == 0, "NO inline error is shown")

    form = pane.locator("div.editor-col form.world-form")
    REPORT.check(
        4,
        form.locator('input[id$="-name"]').input_value() == "Rook",
        "the editor opened on the imported world",
    )
    checked = form.locator("fieldset", has_text="Lore books").locator("input:checked").count()
    REPORT.check(4, checked == 0, f"no lore book is ticked ({checked} checked)")
    REPORT.check(4, len(get_json(f"{api}/api/books")["books"]) == books_before, "the book catalog is unchanged")
    worlds = get_json(f"{api}/api/worlds")["worlds"]
    REPORT.check(4, len(worlds) == worlds_before + 1, f"exactly one world was added ({len(worlds)} total)")
    REPORT.check(
        4,
        next((w for w in worlds if w["name"] == "Rook"), {}).get("book_ids") == [],
        "the imported world carries no book_ids",
    )
    shot(page, shots, "04-world-card-without-lorebook.png")


# ---------------------------------------------------------------------------
# assertion 5: a non-card file renamed .json → precise inline 422
# ---------------------------------------------------------------------------


def assert_world_card_rejection(page, shots: Path, fixtures: dict[str, Path], api: str) -> None:
    section(5, "World studio: a non-card .json is refused inline (no world)")
    pane = studio(page).locator("div.world-pane")
    pane.wait_for(state="visible", timeout=30_000)
    worlds_before = get_json(f"{api}/api/worlds")["worlds"]
    listed_before = [t.strip() for t in pane.locator(".list-col .row-label").all_inner_texts()]

    choose_file(page, pane.get_by_role("button", name="Import", exact=True), fixtures["bad_json"])

    error = pane.locator("p.error[role='alert']")
    error.wait_for(state="visible", timeout=30_000)
    text = error.inner_text().strip()
    EVIDENCE["api"]["rejection_inline_text"] = text
    REPORT.check(5, bool(text), f"an inline error is rendered ({text!r})")
    REPORT.check(
        5,
        "not valid JSON" in text or "neither a PNG" in text,
        f"the inline error is the PRECISE parser message ({text!r})",
    )
    REPORT.check(5, pane.locator("p.status").count() == 0, "no success status is shown")

    worlds_after = get_json(f"{api}/api/worlds")["worlds"]
    REPORT.check(5, len(worlds_after) == len(worlds_before), f"no world was created ({len(worlds_after)})")
    listed_after = [t.strip() for t in pane.locator(".list-col .row-label").all_inner_texts()]
    REPORT.check(5, listed_after == listed_before, f"the world list is unchanged ({listed_after})")
    shot(page, shots, "05-world-card-422-inline.png")


# ---------------------------------------------------------------------------
# assertion 6: the World JSON door (DefinitionControls) — regression
# ---------------------------------------------------------------------------


def assert_world_json_door(page, shots: Path, fixtures: dict[str, Path], api: str) -> None:
    section(6, "World studio: the same door also takes a world JSON")
    pane = studio(page).locator("div.world-pane")
    pane.wait_for(state="visible", timeout=30_000)
    door = pane.get_by_role("button", name="Import", exact=True)
    REPORT.check(6, door.count() == 1, "one door serves both file shapes")
    choose_file(page, door, fixtures["world_json"])
    # The door renders its status inside the pane head (one line, plain copy).
    status = pane.locator("header.pane-head p.status")
    text = wait_text(status, "Imported")
    REPORT.check(6, "Imported" in text, f"the JSON door reports the import ({text!r})")
    wait_text(pane.locator(".list-col"), "Harbour District")
    labels = [t.strip() for t in pane.locator(".list-col .row-label").all_inner_texts()]
    REPORT.check(6, "Harbour District" in labels, f"the imported world is listed ({labels})")
    worlds = get_json(f"{api}/api/worlds")["worlds"]
    imported = next((w for w in worlds if w["name"] == "Harbour District"), None)
    REPORT.check(6, imported is not None, "the world was written to the engine")
    REPORT.check(
        6,
        imported is not None and imported["scenario"] == WORLD_JSON["scenario"],
        "the JSON body round-trips (scenario carried)",
    )
    shot(page, shots, "06-world-json-door.png")


# ---------------------------------------------------------------------------
# assertion 7: the Persona studio's existing card door — regression
# ---------------------------------------------------------------------------


def assert_persona_card_door(page, shots: Path, fixtures: dict[str, Path], api: str) -> None:
    section(7, "Persona studio: the same ONE door imports a persona PNG")
    studio_tab(page, "Personas")
    pane = studio(page).locator("div.pane")
    pane.wait_for(state="visible", timeout=30_000)
    door = pane.get_by_role("button", name="Import", exact=True)
    REPORT.check(7, door.count() == 1, "the persona pane has exactly ONE import control")

    with page.expect_response(
        lambda r: "/api/cards/import" in r.url and r.request.method == "POST",
        timeout=60_000,
    ) as response_info:
        choose_file(page, door, fixtures["persona"])
    response = response_info.value
    body = response.json()
    EVIDENCE["api"]["persona_card_import"] = {"status": response.status, "body": body}
    REPORT.check(7, response.status == 200 and body.get("ok") is True, f"import -> {response.status}")

    text = wait_text(pane.locator("p.status"), "Imported")
    REPORT.check(7, "Mira Quill" in text, f"the status names the persona ({text!r})")
    wait_text(pane.locator(".list-col"), "Mira Quill")
    labels = [t.strip() for t in pane.locator(".list-col .row-label").all_inner_texts()]
    REPORT.check(7, "Mira Quill" in labels, f"the persona is in the Personas studio list ({labels})")

    personas = get_json(f"{api}/api/personas")["personas"]
    mira = next((p for p in personas if p["name"] == "Mira Quill"), None)
    REPORT.check(7, mira is not None, "the persona was written to the engine")
    if mira is not None:
        carried = {
            key: mira.get(key)
            for key in ("name", "description", "personality", "scenario", "first_mes")
        }
        wanted = {key: PERSONA_CARD["data"][key] for key in carried}
        REPORT.check(7, carried == wanted, f"the card's fields carry onto the persona ({carried})")
    REPORT.check(
        7,
        mira is not None and bool(mira.get("image_asset")),
        f"the persona's art is stored ({mira.get('image_asset') if mira else None})",
    )
    if mira is not None and mira.get("image_asset"):
        served = get_bytes(f"{api}/api/personas/{mira['persona_id']}/image")
        EVIDENCE["api"]["persona_image_bytes"] = len(served)
        REPORT.check(
            7,
            served == fixtures["persona"].read_bytes(),
            "GET /api/personas/{id}/image serves the card's PNG bytes",
        )

    # OBSERVED, NOT ASSERTED (and NOT a #40 regression): the row renders the
    # initials fallback for a PERSISTED persona because `normalizePersona`
    # (frontend/src/lib/api/personas.ts:91-113) drops `image_asset` while
    # `hydrateAvatars` (PersonaPane.svelte:104-126) gates its fetch on it. Both
    # files date from 0eeedc4 (v0.2.0); #40 touched neither. Recorded here as
    # evidence so the gate never claims a picture the UI does not draw.
    row = pane.locator("li.row", has_text="Mira Quill").first
    has_img = wait_visible(row.locator("span.row-avatar img"), timeout=8_000)
    avatar_text = row.locator("span.row-avatar").inner_text().strip()
    REPORT.note(
        7,
        "pre-existing (not #40): the persisted persona row draws initials "
        f"({avatar_text!r}), not its stored image (img rendered: {has_img}) — "
        "normalizePersona omits image_asset",
    )
    EVIDENCE["api"]["persona_row_avatar"] = {"img_rendered": has_img, "text": avatar_text}
    shot(page, shots, "07-persona-card-door.png")


# ---------------------------------------------------------------------------
# assertion 1 (part b): the lore hint with a world-materialized chat
# ---------------------------------------------------------------------------


def assert_lore_hint(page, shots: Path, api: str) -> None:
    section(1, "rail: the lore hint renders world name + clamped scenario + books")
    close_studio(page)
    drawer = rail(page)

    # Materialize the imported world into a chat through the rail's ▸ door (the
    # D51 chat-assembly affordance — no import involved).
    start = drawer.get_by_role("button", name="Start a chat in Lyra Voss")
    start.wait_for(state="visible", timeout=30_000)
    start.click()

    name = drawer.locator(".sw-name")
    name.wait_for(state="visible", timeout=60_000)
    wait_text(name, "Lyra Voss")
    REPORT.check(1, name.inner_text().strip() == "Lyra Voss", "the hint names the chat's world")

    scenario = drawer.locator(".sw-scenario")
    REPORT.check(1, scenario.count() == 1, "the hint renders the world's scenario")
    REPORT.check(
        1,
        scenario.inner_text().strip() == WORLD_SCENARIO,
        "the scenario text is the world's (full text is in `title`)",
    )
    clamp = page.evaluate(
        """() => {
            const el = document.querySelector('aside.drawer .sw-scenario');
            if (!el) return null;
            const style = getComputedStyle(el);
            return {
                clamp: style.webkitLineClamp || style.lineClamp,
                overflow: style.overflow,
                hidden: el.scrollHeight > el.clientHeight + 1
            };
        }"""
    )
    REPORT.check(1, clamp is not None, "the scenario element is measurable")
    if clamp:
        REPORT.check(1, clamp["clamp"] == "2", f"the scenario is clamped to 2 lines (clamp={clamp['clamp']})")
        REPORT.check(1, clamp["overflow"] == "hidden", f"the clamp clips overflow ({clamp['overflow']})")
        REPORT.check(1, clamp["hidden"] is True, "the long scenario really is truncated on screen")

    books = [t.strip() for t in drawer.locator(".sw-book-name").all_inner_texts()]
    REPORT.check(1, books == ["Night Market Lore"], f"the hint lists the world's book ({books})")
    # `.sw-label` is CSS-uppercased (text-transform), so the RENDERED text reads
    # WORLD / LORE — compare case-insensitively rather than pinning the styling.
    labels = [t.strip().lower() for t in drawer.locator(".sw-label").all_inner_texts()]
    REPORT.check(1, labels == ["world", "lore"], f"the hint heads are World + Lore ({labels})")
    opened = page.url
    EVIDENCE["api"]["world_chat_url"] = opened
    REPORT.check(1, "s=" in opened, f"the world chat is the open session ({opened})")
    shot(page, shots, "08-rail-lore-hint-world-chat.png")


# ---------------------------------------------------------------------------
# assertion 2: attach + clear a book on a real chat, through the UI
# ---------------------------------------------------------------------------


def assert_lore_attach_clear(page, shots: Path, api: str, plain_title: str, plain_id: str) -> None:
    section(2, "rail: attach/clear a lore book on a real chat")
    drawer = rail(page)
    open_chat(page, plain_title)
    empty = drawer.locator(".sw-none")
    empty.wait_for(state="visible", timeout=30_000)
    REPORT.check(2, empty.inner_text().strip() == "No lore attached.", "the plain chat starts loreless")

    select = drawer.locator("#session-book-add")
    select.wait_for(state="visible", timeout=30_000)
    options = [t.strip() for t in select.locator("option").all_inner_texts()]
    REPORT.check(2, "Night Market Lore" in options, f"the attach picker lists the catalog book ({options})")

    select.select_option(label="Night Market Lore")
    book_name = drawer.locator(".sw-book-name")
    book_name.wait_for(state="visible", timeout=30_000)
    wait_text(book_name, "Night Market Lore")
    books = [t.strip() for t in drawer.locator(".sw-book-name").all_inner_texts()]
    REPORT.check(2, books == ["Night Market Lore"], f"the attached book renders in the hint ({books})")
    REPORT.check(2, drawer.locator(".sw-none").count() == 0, "the empty state is gone")
    world = get_json(f"{api}/api/sessions/{plain_id}/world")
    EVIDENCE["api"]["session_world_after_attach"] = world
    REPORT.check(
        2,
        [b["name"] for b in world["books"]] == ["Night Market Lore"],
        f"the engine resolves the attached book ({[b['name'] for b in world['books']]})",
    )
    shot(page, shots, "09-rail-lore-attached.png")

    drawer.locator(".sw-clear").click()
    drawer.locator(".sw-none").wait_for(state="visible", timeout=30_000)
    REPORT.check(2, drawer.locator(".sw-books").count() == 0, "Clear removes the book from the hint")
    cleared = get_json(f"{api}/api/sessions/{plain_id}/world")
    EVIDENCE["api"]["session_world_after_clear"] = cleared
    REPORT.check(2, cleared["books"] == [], f"the engine resolves no books after Clear ({cleared['books']})")
    shot(page, shots, "10-rail-lore-cleared.png")


# ---------------------------------------------------------------------------
# assertion 8: mount paths (chat ↔ studio ↔ chat), zero page/console errors
# ---------------------------------------------------------------------------


def assert_mount_paths(page, shots: Path, plain_title: str, world_title: str) -> None:
    section(8, "mount paths: chat <-> studio <-> chat, reload — no page errors")
    for cycle in range(3):
        open_studio(page)
        REPORT.check(8, studio(page).is_visible(), f"studio mounts (cycle {cycle + 1})")
        close_studio(page)
        REPORT.check(8, studio(page).count() == 0, f"studio unmounts (cycle {cycle + 1})")

    for label in ("Personas", "Worlds", "Books", "Assistants", "Personas"):
        open_studio(page)
        studio_tab(page, label)
        pane_body = studio(page).locator("div.studio-body")
        pane_body.wait_for(state="visible", timeout=30_000)
        text = pane_body.inner_text()
        REPORT.check(
            8,
            bool(text.strip()) and "pane not wired yet" not in text,
            f"the {label} pane re-mounts with content",
        )
        close_studio(page)

    # Chat switches with the studio open and closed: the rail's lore hint
    # re-fetches per chat and the page re-renders the shell each time.
    open_chat(page, world_title)
    wait_text(rail(page).locator(".sw-name"), "Lyra Voss")
    open_chat(page, plain_title)
    rail(page).locator(".sw-none").wait_for(state="visible", timeout=30_000)
    REPORT.check(8, True, "chat switches re-render the lore hint both ways")
    open_studio(page)
    open_chat(page, world_title)
    wait_text(rail(page).locator(".sw-name"), "Lyra Voss")
    close_studio(page)
    REPORT.check(8, True, "chat switch with the studio docked")

    shot(page, shots, "11-mount-paths.png")


# ---------------------------------------------------------------------------
# assertion 9: persistence across reload
# ---------------------------------------------------------------------------


def assert_persistence(page, shots: Path, api: str) -> None:
    section(9, "reload: the imported world and book persist")
    world_names = ["Lyra Voss", "Rook", "Harbour District"]
    books_before = get_json(f"{api}/api/books")["books"]
    worlds_before = get_json(f"{api}/api/worlds")["worlds"]

    # The open chat is the world chat (mount_paths left it current): after the
    # reload its lore hint must re-fetch the SAME world + book from the engine.
    page.reload(wait_until="domcontentloaded")
    rail(page).wait_for(state="visible", timeout=60_000)
    wait_text(rail(page).locator(".sw-name"), "Lyra Voss", timeout=60_000)
    REPORT.check(9, True, "the rail re-hydrates with the world chat's lore")
    wait_text(rail(page).locator(".sw-book-name"), "Night Market Lore", timeout=30_000)
    REPORT.check(9, True, "the chat's book survives the reload")

    open_studio(page)
    studio_tab(page, "Worlds")
    pane = studio(page).locator("div.world-pane")
    pane.wait_for(state="visible", timeout=30_000)
    wait_text(pane.locator(".list-col"), "Lyra Voss")
    labels = [t.strip() for t in pane.locator(".list-col .row-label").all_inner_texts()]
    REPORT.check(9, all(name in labels for name in world_names), f"all imported worlds persist ({labels})")

    studio_tab(page, "Books")
    catalog = studio(page).locator('section[aria-label="Book catalog"]')
    wait_text(catalog, "Night Market Lore")
    REPORT.check(9, True, "the imported book persists in the Books catalog")
    shot(page, shots, "12-reload-persistence.png")

    books_after = get_json(f"{api}/api/books")["books"]
    worlds_after = get_json(f"{api}/api/worlds")["worlds"]
    EVIDENCE["api"]["worlds_after_reload"] = worlds_after
    EVIDENCE["api"]["books_after_reload"] = books_after
    REPORT.check(
        9,
        [w["world_id"] for w in worlds_after] == [w["world_id"] for w in worlds_before],
        "the engine's world set is unchanged by the reload",
    )
    REPORT.check(
        9, [b["book_id"] for b in books_after] == [b["book_id"] for b in books_before], "the book set is unchanged"
    )
    # Reload lands on the same chat (the URL pins `?s=`).
    REPORT.check(9, "s=" in page.url, f"the reload kept the chat open ({page.url})")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="evidence root (default /tmp/import40-gate)")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--vite-port", type=int, default=DEFAULT_VITE_PORT)
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()

    started = time.time()
    root = Path(args.dir)
    shots = root / "shots"
    fixtures_dir = root / "fixtures"
    for directory in (root, shots, fixtures_dir):
        directory.mkdir(parents=True, exist_ok=True)
    # Evidence must describe THIS run: clear last run's failure shots so a green
    # result can never sit beside a stale crash screenshot.
    for stale in [*shots.glob("fail-*.png"), *shots.glob("crash-*.png")]:
        stale.unlink(missing_ok=True)

    if not Path(CHROME).exists():
        log(f"ERROR: chromium not found at {CHROME} (set FABULIST_CHROME)")
        return 2
    for port in (args.api_port, args.vite_port):
        if _port_open(port):
            log(f"ERROR: port {port} is already in use — pick another (--api-port/--vite-port)")
            return 2

    # Isolated state: a config COPY, throwaway log + checkpoints under /tmp.
    config = root / "connections.json"
    shutil.copyfile(REPO_ROOT / "configs" / "connections.example.json", config)
    log_path = root / "events.db"
    ckpt_path = root / "checkpoints.db"
    for stale in (log_path, ckpt_path):
        stale.unlink(missing_ok=True)
    os.environ["FABULIST_DEV_ORIGINS"] = (
        f"http://127.0.0.1:{args.vite_port},http://localhost:{args.vite_port}"
    )

    # `-P` / `-L` are NOT used: `uv run` already has the repo's src on the path via
    # the project's editable install; this script imports fabulist directly.
    fixtures = build_fixtures(fixtures_dir)

    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    holder = boot_engine(config, log_path, ckpt_path, args.api_port)
    api = f"http://127.0.0.1:{args.api_port}"
    vite_log = root / "vite.log"
    vite_process, vite_handle = start_vite(args.vite_port, args.api_port, vite_log)
    base = f"http://127.0.0.1:{args.vite_port}"

    page_errors: list[str] = []
    console_errors: list[str] = []
    filtered: list[dict[str, str]] = []
    crashes: list[str] = []

    def classify(message, label: str) -> None:
        text = message.text
        url = (message.location or {}).get("url") or ""
        if any(benign in text or benign in url for benign in BENIGN_CONSOLE):
            filtered.append({"label": label, "text": text, "url": url, "reason": "browser/vite noise"})
            return
        # Deliberate probe 1: the non-card rejection (assertion 5) — the refusal
        # is the POINT of that step; the UI renders it inline (asserted).
        if "422" in text and url.endswith("/api/cards/import"):
            filtered.append({"label": label, "text": text, "url": url, "reason": "deliberate refusal probe"})
            return
        # Deliberate probe 2: a definition with no image answers 404, which
        # `fetchDefinitionImage` documents as the ordinary "no image" answer.
        if "404" in text and "/api/" in url and url.endswith("/image"):
            filtered.append({"label": label, "text": text, "url": url, "reason": "no-image 404 probe"})
            return
        console_errors.append(f"{label}: console: {text} [{url}]")

    try:
        plain = get_json(f"{api}/api/sessions", "POST", {"title": "Plain gate chat"})
        plain_id = plain.get("session_id") or plain.get("id")
        if not plain_id:
            log(f"ERROR: could not create the throwaway chat: {plain}")
            return 2
        log(f"throwaway chat: {plain_id}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=CHROME, args=["--no-sandbox"], headless=not args.headed
            )
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 950})
                page.set_default_timeout(30_000)
                page.on("console", lambda message: message.type == "error" and classify(message, "chat"))
                page.on(
                    "pageerror", lambda err: page_errors.append(f"chat: {err}")
                )

                page.goto(f"{base}/chat?s={plain_id}", wait_until="domcontentloaded")

                steps: list[tuple[int, str, "callable"]] = [
                    (1, "rail retirement + quiet empty state", lambda: assert_rail_retired(page, shots)),
                    (
                        3,
                        "world card door (with lorebook)",
                        lambda: assert_world_card_import(page, shots, fixtures, api),
                    ),
                    (
                        4,
                        "world card door (no lorebook)",
                        lambda: assert_world_card_no_lorebook(page, shots, fixtures, api),
                    ),
                    (
                        5,
                        "world card door (non-card 422)",
                        lambda: assert_world_card_rejection(page, shots, fixtures, api),
                    ),
                    (6, "world JSON door", lambda: assert_world_json_door(page, shots, fixtures, api)),
                    (7, "persona card door", lambda: assert_persona_card_door(page, shots, fixtures, api)),
                    (1, "lore hint content", lambda: assert_lore_hint(page, shots, api)),
                    (
                        2,
                        "lore attach/clear",
                        lambda: assert_lore_attach_clear(page, shots, api, "Plain gate chat", plain_id),
                    ),
                    (
                        8,
                        "mount paths",
                        lambda: assert_mount_paths(page, shots, "Plain gate chat", "Lyra Voss"),
                    ),
                    (9, "reload persistence", lambda: assert_persistence(page, shots, api)),
                ]
                for num, label, step in steps:
                    try:
                        step()
                    except (GateError, PWTimeout, KeyError, AssertionError, RuntimeError) as err:
                        REPORT.fail(num, f"{label}: {err}")
                        shot(page, shots, f"fail-A{num}-{label.replace(' ', '-').replace('/', '_')}.png")
                    except Exception as err:  # noqa: BLE001 - a harness crash is evidence too
                        REPORT.fail(num, f"{label}: unexpected {type(err).__name__}: {err}")
                        crashes.append(f"A{num} {label}: {type(err).__name__}: {err}")
                        shot(page, shots, f"crash-A{num}.png")
            finally:
                browser.close()
    finally:
        _free_by_port(args.vite_port)
        try:
            vite_handle.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
        server = holder.get("server")
        if server is not None:
            server.should_exit = True  # in-process: stop gracefully, never fuser -k it
        log(f"vite on :{args.vite_port} killed by port; engine stopped")

    # --- the sweep: zero uncaught page errors, no unexpected console errors -----
    section(8, "mount paths: zero page errors / no unexpected console errors")
    REPORT.check(8, not page_errors, f"zero page errors across the run ({page_errors[:3]})")
    REPORT.check(8, not console_errors, f"no unexpected console errors ({console_errors[:3]})")
    REPORT.check(8, not crashes, f"no harness crashes ({crashes[:3]})")
    EVIDENCE["page_errors"] = page_errors
    EVIDENCE["console"] = filtered

    elapsed = time.time() - started
    evidence_path = root / "evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "git_head": subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip(),
                "engine": {"port": args.api_port, "log": str(log_path), "checkpoints": str(ckpt_path)},
                "vite": {"port": args.vite_port, "log": str(vite_log)},
                "fixtures": {name: str(path) for name, path in fixtures.items()},
                "filtered_console": filtered,
                "page_errors": page_errors,
                "console_errors": console_errors,
                "crashes": crashes,
                "assertions": {
                    str(num): {
                        "status": REPORT.status(num),
                        "checks": REPORT.checks[num],
                        "failures": REPORT.failures[num],
                        "notes": REPORT.details[num],
                    }
                    for num in range(1, ARM + 1)
                },
                **EVIDENCE,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    log("")
    log(REPORT.table())
    log("")
    log(f"evidence: {evidence_path}")
    log(f"shots:    {shots}")
    log(f"wall time: {elapsed:.1f}s")
    failed = [num for num in range(1, ARM + 1) if REPORT.failures[num]]
    if failed:
        log(f"failed assertions: {failed}")
        log("RESULT: FAIL")
        return 1
    log("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
