"""Wallpaper surface pins (issue #13, the ADR-027 extension).

Issue #13 adds one image behind the prose. The owner ruling in the thread is
narrow and testable, so this module pins exactly that:

1. **A wallpaper is a field of the appearance document, not a second store.**
   ``data/appearance.json`` gains ``{"wallpaper", "fit"}`` and the bytes live in
   ``data/appearance/wallpaper.<ext>`` — beside the document, derived from its
   path (:attr:`AppearanceStore.wallpaper_dir`). User state, never the event log
   (ADR-001's carve-out, restated by ADR-027), so the last test walks both files.
2. **The caps are the ruling's, and a refusal is a sentence.** PNG/JPEG/GIF/WebP,
   8 MB, 4096px an edge, ``cover`` as the only fit — enforced at the write door
   (:mod:`fabulist.theme.wallpaper`) and surfaced verbatim by the API.
3. **The frontend applies it as an attribute layer.** ``<html data-wallpaper>``
   turns on the ``:root[data-wallpaper]`` block in app.css and the URL rides ONE
   inline custom property; removing the image removes both, so a no-wallpaper
   install is byte-identical to the sheet before this feature.
4. **The double paint is fixed at the source.** app.css paints ``html, body`` and
   ``chat/+page.svelte`` used to paint ``.chat`` again — an opaque second layer
   over the page, which is exactly what a wallpaper cannot survive. The rule is
   pinned out here so it cannot come back.

The contrast half of the ruling (scrim floors, the re-solved ladder at the
bright-pixel worst case) lives next to the other WCAG math in
``tests/test_theme_contrast_ladder.py``.
"""

from __future__ import annotations

import asyncio
import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.appearance_api import WALLPAPER_ROUTE, router
from fabulist.api.fake import FakeTurnEngine
from fabulist.theme.appearance import (
    DEFAULT_FIT,
    FITS,
    AppearanceError,
    AppearanceStore,
)
from fabulist.theme.wallpaper import (
    FORMATS,
    MAX_WALLPAPER_BYTES,
    MAX_WALLPAPER_EDGE,
    WallpaperError,
    image_size,
    sniff_format,
    validate_upload,
)

ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "frontend" / "src" / "app.css"
APPLY_TS = ROOT / "frontend" / "src" / "lib" / "theme" / "apply.ts"
APPEARANCE_TS = ROOT / "frontend" / "src" / "lib" / "api" / "appearance.ts"
DRAWER = ROOT / "frontend" / "src" / "routes" / "chat" / "AppearanceDrawer.svelte"
CHAT_PAGE = ROOT / "frontend" / "src" / "routes" / "chat" / "+page.svelte"

WALLPAPER_PY = ROOT / "src" / "fabulist" / "theme" / "wallpaper.py"
APPEARANCE_PY = ROOT / "src" / "fabulist" / "theme" / "appearance.py"


def run(coro):
    return asyncio.run(coro)


# --- fixtures: real container headers (no Pillow in this project's deps) ----------


def png_bytes(width: int = 80, height: int = 40, rgb: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    """A REAL, decodable PNG — the browser assertion needs one, not just a header."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def gif_bytes(width: int = 64, height: int = 48) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00" + b";"


def jpeg_bytes(width: int = 120, height: int = 90) -> bytes:
    """SOI + a minimal APP0 + one SOF0 carrying the size + EOI (headers only)."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp_vp8x_bytes(width: int = 300, height: int = 200) -> bytes:
    payload = (
        b"\x00\x00\x00\x00"  # flags + reserved
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    body = b"WEBP" + b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


IMAGES = {
    "png": (png_bytes(), "image/png", "PNG"),
    "jpeg": (jpeg_bytes(), "image/jpeg", "JPEG"),
    "gif": (gif_bytes(), "image/gif", "GIF"),
    "webp": (webp_vp8x_bytes(), "image/webp", "WebP"),
}


@pytest.fixture
def store(tmp_path: Path) -> AppearanceStore:
    return AppearanceStore(tmp_path / "data" / "appearance.json")


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


@pytest.fixture
def wallpaper_app(build_app, tmp_path: Path):
    build, _ = build_app
    app = build()
    app.state.appearance = AppearanceStore(tmp_path / "data" / "appearance.json")
    return app


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- the container sniffers (stdlib only: the caps need four integers) -----------


@pytest.mark.parametrize("kind", sorted(IMAGES))
def test_each_accepted_container_is_sniffed_from_magic_bytes(kind):
    data, mime, _label = IMAGES[kind]
    fmt = sniff_format(data)
    assert fmt is not None
    assert fmt.mime == mime
    assert fmt in FORMATS


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (png_bytes(640, 480), (640, 480)),
        (gif_bytes(320, 200), (320, 200)),
        (jpeg_bytes(1920, 1080), (1920, 1080)),
        (webp_vp8x_bytes(4096, 2160), (4096, 2160)),
    ],
)
def test_each_container_header_yields_its_size(data, expected):
    fmt = sniff_format(data)
    assert fmt is not None
    assert image_size(data, fmt) == expected


def test_webp_lossless_and_lossy_headers_are_read_too():
    """The two other WebP flavours (VP8L, VP8 ) carry the size differently."""
    # VP8L: 0x2f signature, then 14 bits (width-1) and 14 bits (height-1).
    width, height = 1024, 768
    bits = (width - 1) | ((height - 1) << 14)
    vp8l_payload = b"\x2f" + struct.pack("<I", bits) + b"\x00\x00\x00"
    vp8l_body = b"WEBP" + b"VP8L" + struct.pack("<I", len(vp8l_payload)) + vp8l_payload
    vp8l = b"RIFF" + struct.pack("<I", len(vp8l_body)) + vp8l_body
    fmt = sniff_format(vp8l)
    assert fmt is not None and image_size(vp8l, fmt) == (width, height)

    # VP8 (lossy): frame tag, the 9d 01 2a start code, then 14-bit dims.
    vp8_payload = (
        b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", 800 & 0x3FFF, 600 & 0x3FFF)
    )
    vp8_body = b"WEBP" + b"VP8 " + struct.pack("<I", len(vp8_payload)) + vp8_payload
    vp8 = b"RIFF" + struct.pack("<I", len(vp8_body)) + vp8_body
    fmt = sniff_format(vp8)
    assert fmt is not None and image_size(vp8, fmt) == (800, 600)


# --- the caps (owner ruling, thread #13) -----------------------------------------


def test_a_plan_the_caps_are_the_ruled_numbers():
    assert MAX_WALLPAPER_BYTES == 8 * 1024 * 1024
    assert MAX_WALLPAPER_EDGE == 4096
    assert FITS == ("cover",)
    assert DEFAULT_FIT == "cover"


@pytest.mark.parametrize("kind", sorted(IMAGES))
def test_every_accepted_format_validates(kind):
    data, mime, _label = IMAGES[kind]
    info = validate_upload(data)
    assert info.format.mime == mime
    assert info.size == len(data)
    assert info.width > 0 and info.height > 0


def test_exactly_the_edge_cap_is_accepted_and_one_pixel_over_is_refused():
    validate_upload(png_bytes(MAX_WALLPAPER_EDGE, 16))  # at the cap: fine
    with pytest.raises(WallpaperError) as excinfo:
        validate_upload(png_bytes(MAX_WALLPAPER_EDGE + 1, 16))
    assert f"{MAX_WALLPAPER_EDGE + 1}×16px" in str(excinfo.value)
    assert f"{MAX_WALLPAPER_EDGE}px" in str(excinfo.value)


def test_exactly_the_byte_cap_is_accepted_and_one_byte_over_is_refused():
    """Padding is not an image, but the cap is a byte count — the reason says so."""
    head = png_bytes(16, 16)
    at_cap = head + b"\x00" * (MAX_WALLPAPER_BYTES - len(head))
    assert validate_upload(at_cap).size == MAX_WALLPAPER_BYTES
    with pytest.raises(WallpaperError) as excinfo:
        validate_upload(at_cap + b"\x00")
    assert str(MAX_WALLPAPER_BYTES) in str(excinfo.value)
    assert "8 MB" in str(excinfo.value)


def test_a_non_image_is_refused_with_the_accepted_formats():
    with pytest.raises(WallpaperError) as excinfo:
        validate_upload(b"%PDF-1.7\n not an image")
    message = str(excinfo.value)
    for label in ("PNG", "JPEG", "GIF", "WebP"):
        assert label in message


def test_an_empty_upload_is_refused():
    with pytest.raises(WallpaperError):
        validate_upload(b"")


def test_a_truncated_image_is_refused_rather_than_stored_unmeasurable():
    with pytest.raises(WallpaperError) as excinfo:
        validate_upload(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    assert "dimensions" in str(excinfo.value)


# --- the store: one document, one directory beside it ----------------------------


def test_first_run_has_no_wallpaper_and_creates_nothing(store):
    appearance = store.read()
    assert appearance.wallpaper is None
    assert appearance.fit == DEFAULT_FIT
    assert not store.path.exists()
    assert not store.wallpaper_dir.exists()


def test_the_wallpaper_directory_is_the_documents_own_stem():
    """``data/appearance.json`` -> ``data/appearance/`` — derived, not configured."""
    assert str(AppearanceStore("data/appearance.json").wallpaper_dir) == "data/appearance"


def test_saving_a_wallpaper_writes_the_bytes_and_the_fields(store):
    appearance = store.save_wallpaper(png_bytes(80, 40))
    assert appearance.wallpaper == "wallpaper.png"
    assert appearance.fit == DEFAULT_FIT
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["wallpaper"] == "wallpaper.png"
    assert document["fit"] == "cover"
    stored = store.wallpaper_dir / "wallpaper.png"
    assert stored.is_file()
    info = store.wallpaper_info()
    assert info is not None and (info.width, info.height) == (80, 40)
    assert store.wallpaper_file() == stored


def test_a_re_upload_in_another_format_replaces_the_previous_file(store):
    store.save_wallpaper(png_bytes())
    appearance = store.save_wallpaper(gif_bytes())
    assert appearance.wallpaper == "wallpaper.gif"
    names = sorted(path.name for path in store.wallpaper_dir.iterdir())
    assert names == ["wallpaper.gif"], "one wallpaper at a time"


def test_a_refused_upload_leaves_the_previous_wallpaper_untouched(store):
    store.save_wallpaper(png_bytes())
    before = store.path.read_text(encoding="utf-8")
    with pytest.raises(WallpaperError):
        store.save_wallpaper(png_bytes(MAX_WALLPAPER_EDGE + 1, 8))
    assert store.path.read_text(encoding="utf-8") == before
    assert (store.wallpaper_dir / "wallpaper.png").is_file()


def test_removing_the_wallpaper_deletes_the_bytes_and_clears_the_field(store):
    store.save_wallpaper(png_bytes())
    appearance = store.remove_wallpaper()
    assert appearance.wallpaper is None
    assert store.wallpaper_dir.exists() and list(store.wallpaper_dir.iterdir()) == []
    assert store.wallpaper_file() is None
    assert json.loads(store.path.read_text(encoding="utf-8"))["wallpaper"] is None


def test_the_wallpaper_and_the_preset_never_overwrite_each_other(store):
    """One document, two writers — each touches only its own keys."""
    store.save_wallpaper(png_bytes())
    store.patch(preset="light", custom_css=".a { color: #fff }")
    appearance = store.read()
    assert (appearance.preset, appearance.wallpaper) == ("light", "wallpaper.png")
    store.save_wallpaper(gif_bytes())
    appearance = store.read()
    assert appearance.preset == "light"
    assert ".a {" in appearance.custom_css


def test_unknown_document_keys_survive_a_wallpaper_save(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"preset": "savilen", "future": {"a": 1}}), encoding="utf-8")
    store.save_wallpaper(png_bytes())
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["future"] == {"a": 1}
    assert document["preset"] == "savilen"


def test_a_hand_edited_name_cannot_escape_the_wallpaper_directory(store):
    outside = store.path.parent / "secret.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(png_bytes())
    store.path.write_text(json.dumps({"wallpaper": "../secret.png"}), encoding="utf-8")
    assert store.wallpaper_file() is None
    assert store.wallpaper_info() is None


def test_a_dangling_wallpaper_name_reads_as_no_wallpaper(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"wallpaper": "wallpaper.png"}), encoding="utf-8")
    assert store.wallpaper_file() is None
    assert store.wallpaper_info() is None


def test_fit_is_validated_at_the_store(store):
    with pytest.raises(AppearanceError) as excinfo:
        store.patch(fit="contain")
    assert "unknown fit" in str(excinfo.value)
    assert "cover" in str(excinfo.value)
    assert store.patch(fit="cover")[0].fit == "cover"


def test_a_garbage_fit_in_the_document_falls_back(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"fit": 7, "wallpaper": 3}), encoding="utf-8")
    appearance = store.read()
    assert appearance.fit == DEFAULT_FIT
    assert appearance.wallpaper is None


# --- the API ---------------------------------------------------------------------


def test_get_reports_no_wallpaper_and_the_limits(wallpaper_app):
    async def go():
        async with client_for(wallpaper_app) as client:
            return await client.get("/api/appearance")

    body = run(go()).json()
    assert body["wallpaper"] is None
    assert body["fit"] == "cover"
    assert body["fits"] == ["cover"]
    limits = body["wallpaper_limits"]
    assert limits["max_bytes"] == MAX_WALLPAPER_BYTES
    assert limits["max_edge"] == MAX_WALLPAPER_EDGE
    assert [item["label"] for item in limits["formats"]] == ["PNG", "JPEG", "GIF", "WebP"]
    assert limits["fits"] == ["cover"]


def test_upload_stores_serves_and_reports_the_image(wallpaper_app):
    data = png_bytes(320, 180)

    async def go():
        async with client_for(wallpaper_app) as client:
            uploaded = await client.post(
                "/api/appearance/wallpaper", files={"file": ("wall.png", data, "image/png")}
            )
            image = await client.get("/api/appearance/wallpaper")
            state = await client.get("/api/appearance")
            return uploaded, image, state

    uploaded, image, state = run(go())
    assert uploaded.status_code == 200
    wire = uploaded.json()["wallpaper"]
    assert wire["name"] == "wallpaper.png"
    assert wire["url"] == WALLPAPER_ROUTE
    assert wire["mime"] == "image/png"
    assert (wire["width"], wire["height"]) == (320, 180)
    assert wire["bytes"] == len(data)
    assert wire["version"], "the mtime is the cache key"
    assert wire["fit"] == "cover"

    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert image.content == data
    assert state.json()["wallpaper"]["name"] == "wallpaper.png"


def test_upload_refuses_an_oversized_edge_with_the_measured_size(wallpaper_app):
    data = png_bytes(MAX_WALLPAPER_EDGE + 1, 8)

    async def go():
        async with client_for(wallpaper_app) as client:
            return await client.post(
                "/api/appearance/wallpaper", files={"file": ("big.png", data, "image/png")}
            )

    res = run(go())
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert f"{MAX_WALLPAPER_EDGE + 1}×8px" in detail
    assert f"{MAX_WALLPAPER_EDGE}px" in detail


def test_upload_refuses_bytes_over_the_limit_with_the_limit(wallpaper_app):
    data = png_bytes(16, 16) + b"\x00" * (MAX_WALLPAPER_BYTES + 1)

    async def go():
        async with client_for(wallpaper_app) as client:
            return await client.post(
                "/api/appearance/wallpaper", files={"file": ("huge.png", data, "image/png")}
            )

    res = run(go())
    assert res.status_code == 422
    assert str(MAX_WALLPAPER_BYTES) in res.json()["detail"]


def test_upload_refuses_a_non_image_with_the_formats(wallpaper_app):
    async def go():
        async with client_for(wallpaper_app) as client:
            return await client.post(
                "/api/appearance/wallpaper",
                files={"file": ("notes.txt", b"just some text", "text/plain")},
            )

    res = run(go())
    assert res.status_code == 422
    assert "unsupported image format" in res.json()["detail"]


def test_upload_leaves_the_preset_and_the_stylesheet_alone(wallpaper_app):
    async def go():
        async with client_for(wallpaper_app) as client:
            await client.patch(
                "/api/appearance", json={"preset": "oled", "custom_css": ".a { color: #fff }"}
            )
            uploaded = await client.post(
                "/api/appearance/wallpaper",
                files={"file": ("wall.png", png_bytes(), "image/png")},
            )
            return uploaded

    body = run(go()).json()
    assert body["preset"] == "oled"
    assert ".a {" in body["custom_css"]
    assert body["wallpaper"]["name"] == "wallpaper.png"


def test_delete_removes_the_image_and_the_bytes_404_afterwards(wallpaper_app):
    async def go():
        async with client_for(wallpaper_app) as client:
            await client.post(
                "/api/appearance/wallpaper",
                files={"file": ("wall.png", png_bytes(), "image/png")},
            )
            deleted = await client.delete("/api/appearance/wallpaper")
            image = await client.get("/api/appearance/wallpaper")
            return deleted, image

    deleted, image = run(go())
    assert deleted.status_code == 200
    assert deleted.json()["wallpaper"] is None
    assert image.status_code == 404


def test_get_wallpaper_is_404_when_none_is_set(wallpaper_app):
    async def go():
        async with client_for(wallpaper_app) as client:
            return await client.get("/api/appearance/wallpaper")

    assert run(go()).status_code == 404


def test_patch_fit_round_trips_and_refuses_an_unknown_mode(wallpaper_app):
    async def go():
        async with client_for(wallpaper_app) as client:
            ok = await client.patch("/api/appearance", json={"fit": "cover"})
            bad = await client.patch("/api/appearance", json={"fit": "contain"})
            return ok, bad

    ok, bad = run(go())
    assert ok.status_code == 200 and ok.json()["fit"] == "cover"
    assert bad.status_code == 422
    assert "unknown fit" in bad.json()["detail"]


def test_the_wallpaper_never_appends_to_the_event_log(wallpaper_app):
    """ADR-027's carve-out: furniture, not narrative history."""
    async def go():
        async with client_for(wallpaper_app) as client:
            await client.post(
                "/api/appearance/wallpaper",
                files={"file": ("wall.png", png_bytes(), "image/png")},
            )
            await client.get("/api/appearance")
            await client.delete("/api/appearance/wallpaper")

    run(go())
    log = wallpaper_app.state.log
    assert log.get_events("__settings__") == []
    for module_path in (APPEARANCE_PY, WALLPAPER_PY):
        source = module_path.read_text(encoding="utf-8")
        assert "fabulist.log" not in source, f"{module_path.name} must not reach for the log"


# --- the frontend: an attribute layer, one inline URL, one control ----------------


def _css_without_comments() -> str:
    import re

    return re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)


def _rules(css: str) -> list[tuple[str, str]]:
    import re

    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def test_the_wallpaper_layer_is_opt_in_on_an_attribute():
    """Absent ``data-wallpaper`` = the shipped sheet; nothing here can leak."""
    css = _css_without_comments()
    assert ":root[data-wallpaper]" in css
    # Every scrim declaration lives in a wallpaper-scoped selector — a scrim in
    # the base block would veil every install that never set an image.
    for selector, body in _rules(css):
        if "--wallpaper-scrim" in body or "--scrim-muted" in body:
            assert "data-wallpaper" in selector, selector
    # ... and the re-solve only redefines tokens the base sheet already ships.
    assert "--wallpaper-scrim" not in _base_root_tokens()
    assert "--scrim-muted" not in _base_root_tokens()


def _base_root_tokens() -> dict[str, str]:
    import re

    text = _css_without_comments()
    block = re.search(r":root\s*\{(?P<body>.*?)\n\}", text, re.DOTALL)
    assert block is not None
    tokens = {}
    for raw in block.group("body").splitlines():
        line = raw.strip()
        if line.startswith("--") and line.endswith(";"):
            name, _, value = line[:-1].partition(":")
            tokens[name.strip()] = value.strip()
    return tokens


def test_the_double_paint_is_gone_from_the_chat_page():
    """app.css paints the page; ``.chat`` must not paint it a second time."""
    import re

    css = _css_without_comments()
    assert "html,\nbody {" in css and "background: var(--bg);" in css
    page = CHAT_PAGE.read_text(encoding="utf-8")
    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", page, re.DOTALL))
    # Comments are stripped INSIDE the style block only: the page's script carries
    # CSS-looking string literals, so a whole-file strip pairs markers wrongly.
    styles = re.sub(r"/\*.*?\*/", "", styles, flags=re.DOTALL)
    rule = next(body for selector, body in _rules(styles) if selector.strip() == ".chat")
    assert "background" not in rule, f".chat paints the page again: {rule.strip()[:120]}"


def test_apply_ts_sets_the_attribute_and_the_url_and_clears_both():
    source = APPLY_TS.read_text(encoding="utf-8")
    assert "WALLPAPER_ATTR = 'data-wallpaper'" in source
    assert "WALLPAPER_VAR = '--wallpaper-image'" in source
    assert "root.setAttribute(WALLPAPER_ATTR, '')" in source
    assert "root.removeAttribute(WALLPAPER_ATTR)" in source
    assert "root.style.setProperty(WALLPAPER_VAR" in source
    assert "root.style.removeProperty(WALLPAPER_VAR)" in source
    # The whole state applies the image too — the root layout's one call is enough.
    assert "applyWallpaper(state.wallpaper)" in source


def test_the_api_client_uploads_and_removes_and_versions_the_url():
    source = APPEARANCE_TS.read_text(encoding="utf-8")
    assert "APPEARANCE_ENDPOINTS.wallpaper" in source
    assert "new FormData()" in source and "body.append('file'" in source
    assert "method: 'DELETE'" in source
    assert "?v=" in source and "API_BASE" in source, "the URL must be resolved + versioned"


def test_the_drawer_offers_pick_fit_and_remove():
    source = DRAWER.read_text(encoding="utf-8")
    assert 'type="file"' in source
    assert "accept={accept}" in source, "the accepted types come from the backend caps"
    assert "uploadWallpaper(file)" in source
    assert "removeWallpaper()" in source
    assert "Remove" in source
    # The caps and the fit list are RENDERED from the response, never duplicated.
    assert "wallpaper_limits" in source and "limitsLine" in source
    assert "fits" in source


def test_the_wallpaper_layer_paints_behind_the_content_and_carries_one_scrim():
    css = _css_without_comments()
    rule = next(
        body for selector, body in _rules(css) if selector.strip().endswith("body::before")
    )
    assert "position: fixed" in rule
    assert "z-index: -1" in rule
    assert "var(--wallpaper-image)" in rule
    # The scrim rides the SAME paint as the image (one veil, not two).
    assert "linear-gradient(var(--wallpaper-scrim), var(--wallpaper-scrim))" in rule
    # ... and the body must not repaint the page colour over it.
    body_rule = next(
        body
        for selector, body in _rules(css)
        if selector.strip() == ":root[data-wallpaper] body"
    )
    assert "background: transparent" in body_rule
