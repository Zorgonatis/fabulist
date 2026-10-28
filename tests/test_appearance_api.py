"""Tests for the appearance surface (issue #12 + #37, ADR-026).

Four presets — light / dark (default) / oled / savilen — selected by a setting, plus ONE
customisation surface: a stylesheet the reader pastes. This module covers the
three things that can rot silently:

1. **The store** (:mod:`fabulist.theme.appearance`) — per-install user state in
   ``data/appearance.json``, NOT the event log. Defaults on a first run, atomic
   writes, unknown keys preserved (the #13 wallpaper extends this document), and
   a malformed file never taking the UI down over furniture.
2. **The guard** (:mod:`fabulist.theme.custom_css`) — the layout contract enforced
   mechanically: the must-not list is dropped on write with a reason, the may list
   survives, ``!important`` is KEPT (ruling D-D's escape hatch), and the stored
   CSS is always something the guard built.
3. **The precedence contract** — ``base -> preset -> user CSS -> session tone``
   (user CSS beats the preset, the tone's inline properties beat user CSS, a
   user's ``!important`` beats the tone), pinned from BOTH the CSS/DOM side and
   the source that implements it, because neither half alone proves the order.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.appearance_api import IMPORTANT_NOTE, router
from fabulist.api.fake import FakeTurnEngine
from fabulist.theme.appearance import (
    ALIGNMENTS,
    APPEARANCE_FILE_ENV,
    DEFAULT_ALIGN,
    DEFAULT_PRESET,
    MAX_CUSTOM_CSS_CHARS,
    PRESET_META,
    PRESETS,
    AppearanceError,
    AppearanceStore,
    appearance_path,
    dropped_wire,
)
from fabulist.theme.custom_css import ALLOWED_PROPERTIES, MIN_FONT_PX, guard

ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "frontend" / "src" / "app.css"
APPLY_TS = ROOT / "frontend" / "src" / "lib" / "theme" / "apply.ts"
APPEARANCE_TS = ROOT / "frontend" / "src" / "lib" / "api" / "appearance.ts"
DRAWER = ROOT / "frontend" / "src" / "routes" / "chat" / "AppearanceDrawer.svelte"
CHAT_PAGE = ROOT / "frontend" / "src" / "routes" / "chat" / "+page.svelte"
LAYOUT = ROOT / "frontend" / "src" / "routes" / "+layout.svelte"


def run(coro):
    return asyncio.run(coro)


# --- harness -----------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> AppearanceStore:
    return AppearanceStore(tmp_path / "data" / "appearance.json")


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event log; closes logs after."""
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


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def appearance_app(build_app, tmp_path: Path):
    """An app whose appearance store points at a scratch file (no real data/)."""
    build, _ = build_app
    app = build()
    app.state.appearance = AppearanceStore(tmp_path / "data" / "appearance.json")
    return app


# --- the store ---------------------------------------------------------------------


def test_presets_are_the_ruled_four_in_derived_order_with_dark_as_default():
    assert PRESETS == ("light", "dark", "oled", "savilen")
    assert DEFAULT_PRESET == "dark"


def test_preset_order_is_derived_greyscale_white_to_black_then_rgb_hue():
    """Issue #37: the catalog order is a sort key, not a hand-maintained tuple.

    Every greyscale preset precedes every coloured one, greys run white -> black
    by descending page lightness, and colours run in RGB order by primary hue.
    """
    grey = [p for p in PRESETS if PRESET_META[p]["greyscale"]]
    colour = [p for p in PRESETS if not PRESET_META[p]["greyscale"]]
    assert PRESETS == (*grey, *colour)
    lightness = [PRESET_META[p]["lightness"] for p in grey]
    assert lightness == sorted(lightness, reverse=True)
    hues = [PRESET_META[p]["hue"] for p in colour]
    assert hues == sorted(hues)


def test_first_run_reads_the_shipped_defaults(store):
    """A checkout with no data/appearance.json is a FIRST RUN, not an error."""
    appearance = store.read()
    assert appearance.preset == "dark"
    assert appearance.custom_css == ""
    assert not store.path.exists(), "reading must not create the document"


def test_first_run_alignment_is_centered(store):
    """Issue #41: centered is the shipped default (today's reading column)."""
    assert store.read().align == DEFAULT_ALIGN == "center"
    assert ALIGNMENTS == ("left", "center", "right")


def test_patch_alignment_writes_the_document(store):
    appearance, guarded = store.patch(align="left")
    assert appearance.align == "left"
    assert guarded is None, "an alignment-only patch runs no CSS guard"
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["align"] == "left"
    assert store.read().align == "left"


def test_unknown_alignment_is_refused_and_nothing_is_written(store):
    with pytest.raises(AppearanceError) as excinfo:
        store.patch(align="diagonal")
    assert "unknown align" in str(excinfo.value)
    assert "left, center, right" in str(excinfo.value)
    assert not store.path.exists()


def test_invalid_alignment_falls_back_to_center(store):
    """A hand-edited document is furniture: unknown values read as the default."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"align": "diagonal"}), encoding="utf-8")
    assert store.read().align == DEFAULT_ALIGN


def test_alignment_survives_an_unrelated_write(store):
    store.patch(align="right")
    store.patch(preset="light")
    assert store.read().align == "right"


def test_patch_preset_writes_the_document(store):
    appearance, guarded = store.patch(preset="light")
    assert appearance.preset == "light"
    assert guarded is None, "a preset-only patch runs no CSS guard"
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["preset"] == "light"
    assert store.read().preset == "light"


def test_patch_stores_the_guarded_css_not_the_raw_paste(store):
    """The file holds exactly what the browser is handed (guard output)."""
    raw = "body { color: #f0f; position: fixed; }"
    appearance, guarded = store.patch(custom_css=raw)
    assert guarded is not None
    assert "position" not in appearance.custom_css
    assert "color: #f0f;" in appearance.custom_css
    assert json.loads(store.path.read_text(encoding="utf-8"))["custom_css"] == (
        appearance.custom_css
    )
    assert [item.declaration for item in guarded.dropped] == ["position: fixed"]


def test_unknown_preset_is_refused_and_nothing_is_written(store):
    with pytest.raises(AppearanceError) as excinfo:
        store.patch(preset="sepia")
    assert "unknown preset" in str(excinfo.value)
    assert "light, dark, oled, savilen" in str(excinfo.value)
    assert not store.path.exists()


def test_oversized_css_is_refused_and_nothing_is_written(store):
    with pytest.raises(AppearanceError) as excinfo:
        store.patch(custom_css="x" * (MAX_CUSTOM_CSS_CHARS + 1))
    assert "limit is" in str(excinfo.value)
    assert not store.path.exists()


def test_unknown_document_keys_survive_a_write(store, tmp_path: Path):
    """Forward-compat: issue #13's wallpaper rides this document, not a new one."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"preset": "savilen", "wallpaper": {"asset": "a.png"}, "future": [1, 2]}),
        encoding="utf-8",
    )
    store.patch(preset="light")
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["preset"] == "light"
    assert document["wallpaper"] == {"asset": "a.png"}
    assert document["future"] == [1, 2]


def test_malformed_document_falls_back_to_defaults(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.read().preset == DEFAULT_PRESET
    # ... and the next write repairs it.
    store.patch(preset="oled")
    assert store.read().preset == "oled"


def test_invalid_field_types_fall_back_rather_than_raising(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"preset": 7, "custom_css": []}), encoding="utf-8")
    appearance = store.read()
    assert appearance.preset == DEFAULT_PRESET
    assert appearance.custom_css == ""


def test_appearance_path_honours_the_env_pin(monkeypatch, tmp_path: Path):
    pinned = tmp_path / "volume" / "appearance.json"
    monkeypatch.setenv(APPEARANCE_FILE_ENV, str(pinned))
    assert appearance_path() == pinned
    assert appearance_path(tmp_path / "explicit.json") == tmp_path / "explicit.json"


def test_document_lives_in_data_beside_the_log():
    """The location class is the repo's existing user-state one, not a new one."""
    assert str(appearance_path()) == "data/appearance.json"


# --- the guard: what is enforced vs documented -------------------------------------


def _kept(css: str) -> str:
    return guard(css).css


def _dropped(css: str) -> list[str]:
    return [item.declaration for item in guard(css).dropped]


def test_allowed_declarations_survive():
    css = (
        ".prose { color: #222; background-color: #fafafa; border-radius: 10px; "
        "font-family: Georgia, serif; font-size: 16px; letter-spacing: 0.01em; "
        "box-shadow: 0 1px 2px #0002; text-transform: none; }"
    )
    kept = _kept(css)
    for fragment in (
        "color: #222;",
        "background-color: #fafafa;",
        "border-radius: 10px;",
        "font-family: Georgia, serif;",
        "font-size: 16px;",
        "box-shadow: 0 1px 2px #0002;",
    ):
        assert fragment in kept
    assert _dropped(css) == []


@pytest.mark.parametrize(
    ("declaration", "reason_hint"),
    [
        ("position: fixed", "allowlist"),
        ("inset: 0", "allowlist"),
        ("top: 0", "allowlist"),
        ("z-index: 999", "allowlist"),
        ("width: 100vw", "allowlist"),
        ("max-width: 120ch", "allowlist"),  # the reading measure
        ("display: none", "allowlist"),
        ("overflow-y: hidden", "allowlist"),
        ("pointer-events: none", "allowlist"),
        ("outline: none", "allowlist"),
        ("transform: translateX(40px)", "allowlist"),
        ("margin: 0 auto", "allowlist"),
        ("padding: 4rem", "allowlist"),
        ("cursor: none", "allowlist"),
        ("filter: blur(2px)", "allowlist"),
        ("all: unset", "allowlist"),
    ],
)
def test_layout_geometry_is_dropped(declaration, reason_hint):
    """The must-not list is enforced mechanically, with a reason for the drawer."""
    result = guard(f".prose {{ {declaration}; color: #fff }}")
    assert result.css == ".prose {\n\tcolor: #fff;\n}"
    assert len(result.dropped) == 1
    assert result.dropped[0].declaration == declaration
    assert reason_hint in result.dropped[0].reason


@pytest.mark.parametrize("name", ["--z-drawer", "--z-pane", "--font-ui", "--font-micro"])
def test_reserved_custom_properties_are_dropped(name):
    """Re-laning a drawer or shrinking the type scale by proxy is still a break."""
    result = guard(f":root {{ {name}: 99 }}")
    assert result.css == ""
    assert len(result.dropped) == 1
    assert "reserved" in result.dropped[0].reason


def test_free_custom_properties_survive():
    """A reader may author their own tokens (and later, #13's wallpaper token)."""
    kept = _kept(":root { --my-page-tint: #fff; --wallpaper-opacity: 0.4; }")
    assert "--my-page-tint: #fff;" in kept
    assert "--wallpaper-opacity: 0.4;" in kept


def test_font_size_floor_is_enforced():
    result = guard(".prose { font-size: 13px; }")
    assert result.css == ""
    assert MIN_FONT_PX == 14
    assert "readable floor" in result.dropped[0].reason
    # rem converts against the browser default (16px), so 0.875rem is exactly 14px.
    assert _kept(".prose { font-size: 0.875rem; }") != ""
    assert _kept(".prose { font-size: 14px; }") != ""
    assert _kept(".prose { font-size: 0.8rem; }") == ""


def test_relative_font_size_is_refused_rather_than_guessed():
    result = guard(".prose { font-size: 1.2em; }")
    assert result.css == ""
    assert "relative or keyword" in result.dropped[0].reason


@pytest.mark.parametrize(
    "at_rule",
    [
        "@import url('https://example.com/x.css');",
        "@media (min-width: 40em) { .a { color: red } }",
        "@font-face { font-family: X; src: url(y.woff2) }",
    ],
)
def test_at_rules_are_dropped_whole(at_rule):
    """One flat block keeps the precedence story true; @import also reaches out."""
    result = guard(f"{at_rule}\n.prose {{ color: #123 }}")
    assert result.css == ".prose {\n\tcolor: #123;\n}", result.css
    assert len(result.dropped) == 1
    assert "at-rules are not allowed" in result.dropped[0].reason


def test_important_is_preserved():
    """Ruling D-D: `!important` is how a reader pins a hue over the session tone."""
    kept = _kept(".prose { color: #f0f !important; }")
    assert "color: #f0f !important;" in kept


def test_comments_and_whitespace_are_normalised():
    kept = _kept("/* a note: position: fixed */\n\n.prose   ,   .x\n{ color : #abc ; }")
    assert kept == ".prose, .x {\n\tcolor: #abc;\n}"


def test_stray_text_is_reported_not_emitted():
    result = guard("color: #fff;")
    assert result.css == ""
    assert any("not inside a selector" in item.reason for item in result.dropped)


def test_empty_paste_is_an_empty_override():
    assert guard("").css == ""
    assert guard("   \n ").dropped == []


def test_guard_rebuilds_the_stylesheet_it_ships():
    """Nothing is echoed back verbatim: the output is re-rendered from parsed pieces."""
    kept = _kept(".a{color:#123;position:absolute}")
    assert kept == ".a {\n\tcolor: #123;\n}"
    assert "position" not in kept


def test_dropped_wire_shape_is_what_the_drawer_renders():
    result = guard(".a { z-index: 3 }")
    assert dropped_wire(result.dropped) == [
        {
            "selector": ".a",
            "declaration": "z-index: 3",
            "reason": result.dropped[0].reason,
        }
    ]


# --- the API -----------------------------------------------------------------------


def test_get_returns_defaults_with_catalog_and_guard(appearance_app):
    async def go():
        async with client_for(appearance_app) as client:
            res = await client.get("/api/appearance")
            return res.status_code, res.json()

    status, body = run(go())
    assert status == 200
    assert body["preset"] == "dark"
    assert body["custom_css"] == ""
    assert [entry["id"] for entry in body["presets"]] == ["light", "dark", "oled", "savilen"]
    assert body["guard"]["important_note"] == IMPORTANT_NOTE
    assert body["guard"]["min_font_px"] == MIN_FONT_PX
    assert "position" in body["guard"]["blocked"]["layout geometry"]


def test_patch_preset_round_trips(appearance_app):
    async def go():
        async with client_for(appearance_app) as client:
            patched = await client.patch("/api/appearance", json={"preset": "light"})
            read = await client.get("/api/appearance")
            return patched, read

    patched, read = run(go())
    assert patched.status_code == 200
    assert patched.json()["preset"] == "light"
    assert read.json()["preset"] == "light"


def test_patch_css_reports_what_the_guard_dropped(appearance_app):
    async def go():
        async with client_for(appearance_app) as client:
            res = await client.patch(
                "/api/appearance",
                json={"custom_css": ".prose { color: #f0f; position: fixed; }"},
            )
            return res

    res = run(go())
    body = res.json()
    assert res.status_code == 200
    assert "color: #f0f;" in body["custom_css"]
    assert body["dropped"] == [
        {
            "selector": ".prose",
            "declaration": "position: fixed",
            "reason": body["dropped"][0]["reason"],
        }
    ]


def test_patch_preset_only_leaves_the_stylesheet_alone(appearance_app):
    async def go():
        async with client_for(appearance_app) as client:
            await client.patch("/api/appearance", json={"custom_css": ".a { color: #fff }"})
            res = await client.patch("/api/appearance", json={"preset": "savilen"})
            return res

    body = run(go()).json()
    assert body["preset"] == "savilen"
    assert ".a {" in body["custom_css"]


def test_alignment_is_on_the_wire_and_round_trips(appearance_app):
    async def go():
        async with client_for(appearance_app) as client:
            read = await client.get("/api/appearance")
            patched = await client.patch("/api/appearance", json={"align": "right"})
            return read, patched

    read, patched = run(go())
    assert read.json()["align"] == "center"
    assert [entry["id"] for entry in read.json()["alignments"]] == ["left", "center", "right"]
    assert [entry["label"] for entry in read.json()["alignments"]] == ["Left", "Center", "Right"]
    assert patched.json()["align"] == "right"


def test_patch_rejects_an_unknown_alignment_with_a_reason(appearance_app):
    async def go():
        async with client_for(appearance_app) as client:
            return await client.patch("/api/appearance", json={"align": "diagonal"})

    res = run(go())
    assert res.status_code == 422
    assert "unknown align" in res.json()["detail"]


def test_patch_rejects_an_unknown_preset_with_a_reason(appearance_app):
    async def go():
        async with client_for(appearance_app) as client:
            return await client.patch("/api/appearance", json={"preset": "sepia"})

    res = run(go())
    assert res.status_code == 422
    assert "unknown preset" in res.json()["detail"]


def test_patch_rejects_an_empty_body(appearance_app):
    async def go():
        async with client_for(appearance_app) as client:
            return await client.patch("/api/appearance", json={})

    res = run(go())
    assert res.status_code == 422
    assert "no appearance fields" in res.json()["detail"]


def test_appearance_never_appends_to_the_event_log(appearance_app):
    """ADR-026: a theme is not narrative history and must not touch the log."""
    async def go():
        async with client_for(appearance_app) as client:
            await client.patch(
                "/api/appearance",
                json={"preset": "light", "custom_css": ".a { color: #fff }"},
            )
            await client.get("/api/appearance")

    run(go())
    log = appearance_app.state.log
    assert log.get_events("__settings__") == []
    from fabulist.theme import appearance as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "fabulist.log" not in source, "the appearance store must not reach for the log"


# --- the precedence contract (D-D) -------------------------------------------------
#
#   base (app.css :root) -> preset (data-theme) -> user CSS -> session tone
#
# Each step is pinned where it is IMPLEMENTED, plus the CSS half that makes the
# step true: a stylesheet can only be outranked by a later/stronger source, so
# "user CSS beats preset" needs BOTH "app.css has no !important" and "the style
# element is appended last in head"; and "tone beats user CSS" needs the tone to
# be an inline declaration on <html>, not a stylesheet.


def test_step_one_base_then_preset_is_attribute_scoped():
    css = APP_CSS.read_text(encoding="utf-8")
    assert ":root {" in css, "the base/dark tokens must live on :root"
    assert ":root[data-theme='oled']" in css
    assert ":root[data-theme='light']" in css
    assert ":root[data-theme='savilen']" in css
    assert "data-theme" in APPLY_TS.read_text(encoding="utf-8")


def test_step_two_user_css_beats_the_preset():
    """No !important in the theme stylesheet + the override appended LAST in head."""
    declarations = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    assert "!important" not in declarations
    apply_source = APPLY_TS.read_text(encoding="utf-8")
    assert "head.appendChild(style)" in apply_source, "the override must be appended last"
    assert "style#${USER_CSS_STYLE_ID}" in apply_source, "exactly ONE style element"
    assert "USER_CSS_STYLE_ID = 'fabulist-user-css'" in apply_source


def test_step_three_the_session_tone_is_inline_on_the_root():
    """Tone tokens ride `style` on <html> — that is why they outrank user CSS."""
    chat = CHAT_PAGE.read_text(encoding="utf-8")
    assert "document.documentElement" in chat
    assert "root.style.setProperty" in chat
    for token in ("--accent-hue", "--accent-sat", "--bg-warmth"):
        assert token in chat
    # The tone endpoint is the server half (fablist.theme.tokens -> inline style).
    from fabulist.api.state_api import session_theme  # noqa: F401  (import pins the seam)


def test_step_four_important_beats_the_tone():
    """The documented escape hatch: the guard keeps !important, the tone cannot."""
    assert "!important" in _kept(".prose { color: #fff !important }")
    # And the drawer says exactly that, in one sentence, from the backend constant.
    drawer = DRAWER.read_text(encoding="utf-8")
    assert "guard?.important_note" in drawer
    assert IMPORTANT_NOTE.count(".") == 1, IMPORTANT_NOTE


def test_appearance_surface_is_one_selector_and_one_paste_box():
    """No theme editor (ruled out): a preset selector and ONE textarea."""
    drawer = DRAWER.read_text(encoding="utf-8")
    assert drawer.count("<textarea") == 1
    assert 'type="color"' not in drawer
    assert 'role="radiogroup"' in drawer
    # The catalog and the guard both come from the API; the drawer never keeps a
    # second copy of the preset list or of the property allowlist.
    assert "getAppearance" in drawer and "patchAppearance" in drawer


def test_alignment_dom_and_measure_are_attribute_driven():
    """Issue #41: one attribute on <html>, one margin switch on the measure.

    The column keeps its 46rem width; the alignment rules only move its inline
    margins, so no markup/layout is restructured and the three modes cannot drift
    into three different measures.
    """
    apply_source = APPLY_TS.read_text(encoding="utf-8")
    assert "ALIGN_ATTR = 'data-align'" in apply_source
    assert "applyAlign" in apply_source
    transcript = (ROOT / "frontend" / "src" / "routes" / "chat" / "TranscriptList.svelte").read_text(
        encoding="utf-8"
    )
    assert "max-width: 46rem" in transcript
    assert "[data-align='left']" in transcript
    assert "[data-align='right']" in transcript
    drawer = DRAWER.read_text(encoding="utf-8")
    assert 'aria-label="Content alignment"' in drawer
    assert "patchAppearance({ align: next })" in drawer


def test_root_layout_applies_the_appearance_once():
    layout = LAYOUT.read_text(encoding="utf-8")
    assert "initAppearance()" in layout
    assert "onMount" in layout
