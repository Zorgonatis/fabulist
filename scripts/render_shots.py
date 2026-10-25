#!/usr/bin/env python3
"""Render-capture harness for the narration surfaces.

Boots the SvelteKit frontend in MOCK mode (``VITE_MOCK=1``) on a scratch port,
opens the ``/components`` gallery with Playwright, and captures:
  (a) one full-page PNG per theme, and
  (b) one clipped PNG per rendered component element (``[data-openui]``).

Output lands in ``data/shots/<UTC-timestamp>/`` with a ``manifest.json`` mapping
fixture/element -> png path, plus the viewport and the accent tokens per theme.
``--themes`` cycles every theme button on the gallery (real trusted clicks);
``--baseline``/``--diff`` store/compare against a baseline at a per-pixel
tolerance.

Run:
  uv run --with playwright python scripts/render_shots.py
  uv run --with playwright python scripts/render_shots.py --themes
  uv run --with playwright python scripts/render_shots.py --baseline
  uv run --with playwright python scripts/render_shots.py --diff

The dev server is torn down on exit; the user's :5173 dev server is never touched.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SHOTS = ROOT / "data" / "shots"
FIXTURES = FRONTEND / "src" / "lib" / "openui" / "fixtures"
DEFAULT_BASELINE = SHOTS / "baseline"
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

# Never bind the user's live dev server.
RESERVED_PORTS = {5173}


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> "None":
    print(f"render_shots: ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_port(port: int) -> None:
    """Free a scratch port if anything still holds it. `pkill -f` self-matches
    in this repo, so use `fuser -k <port>/tcp`."""
    if port_in_use(port):
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "theme"


# ── fixtures ────────────────────────────────────────────────────────────────

STMT_RE = re.compile(r"^(\w+) = (\w+)\(")


def components_in(path: Path) -> list[str]:
    """Component names named by the statements in one .openui file."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = STMT_RE.match(line.strip())
        if m:
            names.append(m.group(2))
    return names


def load_fixture_map() -> tuple[list[str], dict[str, str], list[str]]:
    """Return (fixture filenames, component -> fixture, expected leaf components)."""
    if not FIXTURES.is_dir():
        die(f"fixtures dir missing: {FIXTURES}")
    files = sorted(p for p in FIXTURES.glob("*.openui"))
    if not files:
        die(f"no .openui fixtures in {FIXTURES}")

    component_fixture: dict[str, str] = {}
    expected: set[str] = set()
    for path in files:
        if path.name == "kitchen-sink.openui":
            continue
        for comp in components_in(path):
            if comp == "ScenePane":
                continue
            component_fixture.setdefault(comp, path.name)
            expected.add(comp)
    component_fixture["ScenePane"] = "kitchen-sink.openui"
    return [p.name for p in files], component_fixture, sorted(expected)


# Argument order for the four defaults, mirroring frontend/src/lib/openui/library.ts.
# The ten VTK flavors read their signature from the generated schemas mirror
# (``schemas/flavors.json``) so a schema regeneration cannot silently drift from
# the fixtures.
# The four infrastructure/default components. The trailing `options` entry is
# the Wave 2b expressive options object — optional, so accepted arity is
# (len-1)..len (mirrors the flavors.json required/total range logic).
DEFAULT_SIGNATURES: dict[str, tuple[str, ...]] = {
    "ScenePane": ("title", "children", "options"),
    "MeterBar": ("label", "value", "max", "options"),
    "ObjectCard": ("name", "kind", "description", "options"),
    # Legacy alias for ObjectCard (Wave 3A rename); kept so an old fixture still audits.
    "ArtifactCard": ("name", "kind", "description", "options"),
    "BeatCard": ("text", "options"),
}


def split_top_level_args(text: str) -> list[str]:
    """Split a positional argument list on commas that sit at bracket depth 0."""
    args: list[str] = []
    cur, depth, quote = "", 0, None
    for ch in text:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch in "([{":
            depth += 1
            cur += ch
        elif ch in ")]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def load_flavor_signatures() -> dict[str, list[dict]]:
    path = FRONTEND / "src" / "lib" / "openui" / "schemas" / "flavors.json"
    if not path.exists():
        die(f"missing schema mirror: {path}")
    return {e["component"]: e["props"] for e in json.loads(path.read_text("utf-8"))["flavors"]}


def audit_fixtures() -> None:
    """Reject a fixture whose arity or nested shape no longer matches the schema.

    The renderer DROPS a node the parser rejects, so a stale fixture would
    otherwise become an empty clip that still looks like a green capture. This
    runs before any server boots, so drift fails fast with the offending line.
    """
    flavors = load_flavor_signatures()
    errors: list[str] = []
    for path in sorted(FIXTURES.glob("*.openui")):
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            m = STMT_RE.match(line)
            if not m or not line.endswith(")"):
                errors.append(f"{path.name}:{lineno}: not a positional statement: {line[:60]}")
                continue
            comp = m.group(2)
            args = split_top_level_args(line[line.index("(") + 1:-1])
            if comp in flavors:
                props = flavors[comp]
                required = sum(1 for p in props if p.get("required", True))
                if not required <= len(args) <= len(props):
                    errors.append(
                        f"{path.name}:{lineno}: {comp} takes {required}..{len(props)} args, "
                        f"statement has {len(args)}"
                    )
                for arg, prop in zip(args, props):
                    if prop["type"] == "object[]" and not (arg.startswith("[") and "{" in arg):
                        errors.append(
                            f"{path.name}:{lineno}: {comp}.{prop['name']} must be a "
                            f"non-empty object[] literal, got {arg[:32]!r}"
                        )
            elif comp in DEFAULT_SIGNATURES:
                props = DEFAULT_SIGNATURES[comp]
                required = len(props) - 1  # trailing `options` is optional
                if not required <= len(args) <= len(props):
                    errors.append(
                        f"{path.name}:{lineno}: {comp} takes {required}..{len(props)} args, "
                        f"statement has {len(args)}"
                    )
            else:
                errors.append(f"{path.name}:{lineno}: unknown component {comp}")
    if errors:
        log("render_shots: fixture schema audit FAILED:")
        for e in errors:
            log("  " + e)
        die(f"{len(errors)} fixture statement(s) do not match the current schema", 5)
    log(f"fixture audit: {len(list(FIXTURES.glob('*.openui')))} files match the current schema")


# ── dev server ──────────────────────────────────────────────────────────────

class DevServer:
    def __init__(self, port: int, log_path: Path):
        self.port = port
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        env = {**os.environ, "VITE_MOCK": "1", "OPENUI_TELEMETRY_DISABLED": "1"}
        self.proc = subprocess.Popen(
            [
                "npm", "run", "dev", "--",
                "--port", str(self.port),
                "--host", "127.0.0.1",
                "--strictPort",
            ],
            cwd=FRONTEND,
            env=env,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        atexit.register(self.stop)

    def wait_ready(self, timeout: float = 90.0) -> None:
        url = f"http://127.0.0.1:{self.port}/components"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                self._fail("dev server exited before becoming ready")
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.5)
        self._fail(f"dev server never became ready on :{self.port} within {timeout:.0f}s")

    def _fail(self, msg: str) -> None:
        self.log_file.flush()
        log(f"render_shots: {msg}")
        log(f"--- {self.log_path} (tail) ---")
        try:
            for line in self.log_path.read_text(encoding="utf-8").splitlines()[-25:]:
                log("  " + line)
        except OSError:
            pass
        self.stop()
        die(msg, 2)

    def stop(self) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    self.proc.wait(timeout=5)
            self.proc = None
        try:
            self.log_file.close()
        except Exception:
            pass
        # Belt-and-suspenders: nothing may linger on the scratch port.
        free_port(self.port)


# ── capture ─────────────────────────────────────────────────────────────────

NO_MOTION_CSS = "*,*::before,*::after{animation:none!important;transition:none!important}"


def capture(port: int, themes: bool, viewport: tuple[int, int], run_dir: Path,
            component_fixture: dict[str, str]) -> dict:
    from playwright.sync_api import sync_playwright

    if not Path(CHROME).exists():
        die(f"chromium not found at {CHROME} (set FABULIST_CHROME)")

    url = f"http://127.0.0.1:{port}/components"
    theme_entries: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            page = browser.new_page(
                viewport={"width": viewport[0], "height": viewport[1]},
                device_scale_factor=1,
            )
            page.emulate_media(reduced_motion="reduce")
            page.goto(url, wait_until="networkidle")
            page.add_style_tag(content=NO_MOTION_CSS)
            page.wait_for_selector("[data-openui]", timeout=20_000)
            page.wait_for_timeout(400)

            buttons = page.locator(".themes button")
            labels = [buttons.nth(i).inner_text().strip() for i in range(buttons.count())]
            if not labels:
                die("gallery rendered no theme buttons -- is VITE_MOCK=1 active?")
            chosen = labels if themes else [page.locator(".themes button.active").inner_text().strip() or labels[0]]

            for label in chosen:
                if themes and buttons.count():
                    buttons.filter(has_text=label).first.click(timeout=5_000)
                    page.wait_for_timeout(350)

                theme_dir = run_dir / slugify(label)
                theme_dir.mkdir(parents=True, exist_ok=True)

                tokens = page.eval_on_selector(
                    "main.gallery",
                    """el => {
                        const s = getComputedStyle(el);
                        return {
                            accent_hue: s.getPropertyValue('--accent-hue').trim(),
                            accent_sat: s.getPropertyValue('--accent-sat').trim(),
                            bg_warmth: s.getPropertyValue('--bg-warmth').trim(),
                        };
                    }""",
                )

                # (a) one full-page PNG. This also forces a full paint of the
                # current theme, which the per-element clips below depend on.
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(120)
                full_rel = f"{slugify(label)}/full-page.png"
                page.screenshot(
                    path=str(run_dir / full_rel), full_page=True, animations="disabled"
                )

                # (b) one clipped PNG per rendered component element. Clips are
                # taken from the full-page paint path: `locator.screenshot()`
                # can return stale raster for offscreen content after a live
                # token change, so every clip is a document-relative crop.
                boxes = page.eval_on_selector_all(
                    "[data-openui]",
                    """els => els.map(e => {
                        const r = e.getBoundingClientRect();
                        return {
                            name: e.getAttribute('data-openui'),
                            x: r.x + window.scrollX,
                            y: r.y + window.scrollY,
                            w: r.width,
                            h: r.height,
                        };
                    })""",
                )
                if not boxes:
                    die("gallery rendered zero component elements", 3)
                entries = []
                for i, box in enumerate(boxes):
                    name = box["name"] or f"element-{i}"
                    if box["w"] < 1 or box["h"] < 1:
                        log(f"  (skipping zero-size element {name})")
                        continue
                    rel = f"{slugify(label)}/{i:02d}-{name}.png"
                    page.screenshot(
                        path=str(run_dir / rel),
                        full_page=True,
                        animations="disabled",
                        clip={"x": box["x"], "y": box["y"], "width": box["w"], "height": box["h"]},
                    )
                    entries.append(
                        {
                            "index": i,
                            "element": name,
                            "fixture": component_fixture.get(name),
                            "png": rel,
                            "width": round(box["w"]),
                            "height": round(box["h"]),
                        }
                    )

                theme_entries.append(
                    {
                        "label": label,
                        "slug": slugify(label),
                        "tokens": tokens,
                        "full_page": full_rel,
                        "elements": entries,
                    }
                )
        finally:
            browser.close()

    return {"themes": theme_entries}


def dropped_components(
    expected: list[str], rendered: set[str], component_fixture: dict[str, str]
) -> list[dict]:
    """Fixture components that never came back from the renderer.

    A node the parser rejects is dropped silently, so any expected element that
    is absent from the render is treated as a hard failure by the caller.
    """
    return [
        {"element": c, "fixture": component_fixture.get(c)}
        for c in expected
        if c not in rendered
    ]


# ── baseline / diff (dependency-free PNG decode + per-pixel compare) ──────────

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, height: int, bpp: int, stride: int) -> bytes:
    out = bytearray()
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        elif ftype != 0:
            raise ValueError(f"unsupported PNG filter {ftype}")
        out += line
        prev = line
    return bytes(out)


def decode_png(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit non-interlaced PNG to (width, height, RGBA bytes)."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")
    pos, idat = 8, bytearray()
    width = height = color = None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or interlace != 0:
                raise ValueError(f"{path}: only 8-bit non-interlaced PNG supported")
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    flat = _unfilter(zlib.decompress(bytes(idat)), height, channels, width * channels)
    rgba = bytearray()
    for i in range(0, len(flat), channels):
        px = flat[i:i + channels]
        if channels == 1:
            rgba += bytes((px[0], px[0], px[0], 255))
        elif channels == 2:
            rgba += bytes((px[0], px[0], px[0], px[1]))
        elif channels == 3:
            rgba += bytes((px[0], px[1], px[2], 255))
        else:
            rgba += bytes(px)
    return width, height, bytes(rgba)


def compare_pngs(a: Path, b: Path, tolerance: int) -> tuple[int, int]:
    """Return (differing pixel count where any channel exceeds tolerance, max delta)."""
    wa, ha, pa = decode_png(a)
    wb, hb, pb = decode_png(b)
    if (wa, ha) != (wb, hb):
        # Size change is itself a failure; report all pixels as differing.
        return max(wa * ha, wb * hb), 255
    differing = 0
    max_delta = 0
    for i in range(0, len(pa), 4):
        d = max(
            abs(pa[i] - pb[i]),
            abs(pa[i + 1] - pb[i + 1]),
            abs(pa[i + 2] - pb[i + 2]),
            abs(pa[i + 3] - pb[i + 3]),
        )
        if d > max_delta:
            max_delta = d
        if d > tolerance:
            differing += 1
    return differing, max_delta


def run_baseline(run_dir: Path, manifest_path: Path, baseline: Path) -> None:
    if baseline.exists():
        shutil.rmtree(baseline)
    baseline.mkdir(parents=True)
    for png in sorted(run_dir.rglob("*.png")):
        dest = baseline / png.relative_to(run_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(png, dest)
    shutil.copy2(manifest_path, baseline / "manifest.json")
    log(f"baseline stored: {baseline} ({len(list(baseline.rglob('*.png')))} PNGs)")


def run_diff(run_dir: Path, baseline: Path, tolerance: int, max_diff_pixels: int) -> int:
    if not baseline.is_dir():
        die(f"baseline not found: {baseline} (run with --baseline first)")
    pngs = sorted(run_dir.rglob("*.png"))
    if not pngs:
        die("no PNGs to compare")
    failures: list[str] = []
    compared = 0
    for png in pngs:
        rel = png.relative_to(run_dir)
        base = baseline / rel
        if not base.exists():
            failures.append(f"NEW  {rel} (absent from baseline)")
            continue
        compared += 1
        differing, max_delta = compare_pngs(base, png, tolerance)
        if differing > max_diff_pixels:
            failures.append(
                f"DIFF {rel}: {differing} px over tolerance (max channel delta {max_delta})"
            )
    log(f"diff vs {baseline}: compared {compared} PNGs, tolerance {tolerance}/255, "
        f"allowed differing px {max_diff_pixels}")
    if failures:
        for f in failures:
            log("  " + f)
        log(f"RESULT: FAIL ({len(failures)} file(s) differ)")
        return 1
    log("RESULT: PASS (no per-pixel differences over tolerance)")
    return 0


# ── main ────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Capture narration-surface renders from seeded mock content.")
    ap.add_argument("--port", type=int, default=5199, help="scratch dev-server port (default 5199)")
    ap.add_argument("--themes", action="store_true", help="cycle every gallery theme and capture each")
    ap.add_argument("--viewport", default="1600x1000", help="WxH (default 1600x1000)")
    ap.add_argument("--baseline", nargs="?", const=str(DEFAULT_BASELINE), default=None,
                    help="store this run as a baseline (default data/shots/baseline)")
    ap.add_argument("--diff", nargs="?", const=str(DEFAULT_BASELINE), default=None,
                    help="compare this run against a baseline (default data/shots/baseline)")
    ap.add_argument("--tolerance", type=int, default=8, help="per-channel max delta tolerated (0-255, default 8)")
    ap.add_argument("--max-diff-pixels", type=int, default=0, help="allowed differing pixels before failing (default 0)")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.baseline and args.diff:
        die("--baseline and --diff are mutually exclusive")
    if args.port in RESERVED_PORTS:
        die(f"refusing port {args.port}: that is the user's live dev server")
    try:
        vw, vh = (int(x) for x in args.viewport.lower().split("x"))
    except ValueError:
        die(f"bad --viewport {args.viewport!r}; expected WxH")

    fixture_files, component_fixture, expected = load_fixture_map()
    audit_fixtures()

    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = SHOTS / utc
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"run dir: {run_dir.relative_to(ROOT)}")
    log(f"fixtures: {len(fixture_files)} files, {len(expected)} leaf components")

    if port_in_use(args.port):
        die(f"port {args.port} already in use -- pick another with --port")

    server = DevServer(args.port, run_dir / "server.log")
    server.start()
    server.wait_ready()
    log(f"dev server ready on :{args.port} (VITE_MOCK=1)")

    result = capture(args.port, args.themes, (vw, vh), run_dir, component_fixture)
    server.stop()

    rendered = {e["element"] for t in result["themes"] for e in t["elements"]}
    missing = dropped_components(expected, rendered, component_fixture)
    manifest = {
        "generated_utc": utc,
        "run_dir": str(run_dir.relative_to(ROOT)),
        "url": f"http://127.0.0.1:{args.port}/components",
        "mode": "VITE_MOCK=1",
        "viewport": {"width": vw, "height": vh},
        "fixtures_dir": str(FIXTURES.relative_to(ROOT)),
        "fixtures": fixture_files,
        "component_fixtures": component_fixture,
        "expected_components": expected,
        "missing_components": missing,
        "rendered_components": sorted(rendered),
        "themes": result["themes"],
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    n_elems = sum(len(t["elements"]) for t in result["themes"])
    log(f"captured {len(result['themes'])} theme(s), {n_elems} element clips, "
        f"{sum(len(list((run_dir / t['slug']).glob('*.png'))) for t in result['themes'])} PNGs")
    log(f"manifest: {manifest_path.relative_to(ROOT)}")
    unmapped = sorted(rendered - set(component_fixture))
    if unmapped:
        log(f"rendered elements with no fixture: {unmapped}")

    # A node the parser rejects is DROPPED silently; treat any fixture component
    # that did not render as a hard failure so drift can never look green.
    if missing:
        log("FAIL: expected components did not render (parser dropped a node):")
        for m in missing:
            log(f"  - {m['element']} ({m['fixture']})")
        return 4

    if args.baseline:
        run_baseline(run_dir, manifest_path, Path(args.baseline))
        return 0
    if args.diff:
        return run_diff(run_dir, Path(args.diff), args.tolerance, args.max_diff_pixels)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
