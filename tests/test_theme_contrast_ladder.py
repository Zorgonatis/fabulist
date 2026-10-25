"""Contrast-ladder pins for issue #21 — every surface step and boundary holds at every tone.

``frontend/src/app.css`` is the source of truth: this module parses the ``:root``
custom-property block, resolves the warmth-driven ``calc(...)`` expressions at every
``--bg-warmth`` value the curated palette can actually emit (plus a fine grid between
them), and recomputes WCAG relative-luminance contrast. It is the deterministic half of
the audit — no browser needed — so a drifted base lightness or a retuned token fails here
instead of silently regressing the ladder.

Floors carried over from the #21 audit:

* surface steps: ``>= 1.15:1`` against the immediate neighbour, ``>= 1.20:1`` off the
  raised rung (panel→raised, raised→active);
* non-text boundaries (WCAG 1.4.11): ``>= 3.0:1`` against the surface they are drawn on;
* ``--text-dim`` is a 11px micro-label colour, so it holds AA ``>= 4.5:1`` on every
  surface it can land on, at the lightest (warm-tone) rung included.

Defect A's fix is pinned structurally: the one rule that drew ``--line`` on a hover wash
(``StepEditor.svelte``'s ``.icon:hover``) must use the wash token ``--line-strong``, which
is the token whose contrast is verified against ``--bg-hover`` / ``--bg-active`` below.
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

import pytest

from fabulist.theme.tokens import CURATED_PALETTE

ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "frontend" / "src" / "app.css"
STEP_EDITOR = ROOT / "frontend" / "src" / "routes" / "chat" / "prompt" / "StepEditor.svelte"

SURFACE_TOKENS = ("--bg", "--bg-inset", "--bg-panel", "--bg-raised", "--bg-hover", "--bg-active")

#: (upper rung, lower rung, floor) — each measured against its immediate neighbour.
LADDER_STEPS = (
    ("--bg-inset", "--bg", 1.15),
    ("--bg-panel", "--bg-inset", 1.15),
    ("--bg-raised", "--bg-panel", 1.20),
    ("--bg-hover", "--bg-raised", 1.15),
    ("--bg-active", "--bg-raised", 1.20),
)

#: (token, surfaces it is drawn on, floor). --line is the structural boundary on the
#: settled rungs; --line-strong owns the transient washes (see app.css).
BOUNDARIES = (
    ("--line", ("--bg-raised", "--bg-panel", "--bg-inset", "--bg"), 3.0),
    ("--line-strong", ("--bg-hover", "--bg-active"), 3.0),
)

#: (token, surfaces, floor) for text; --text-dim is micro-label copy (AA, 4.5:1).
TEXT = (
    ("--text-dim", ("--bg-raised", "--bg-panel", "--bg-inset", "--bg"), 4.5),
    ("--text-muted", ("--bg-raised", "--bg-panel", "--bg-inset", "--bg"), 4.5),
    ("--text", ("--bg-raised", "--bg-panel", "--bg-inset", "--bg"), 4.5),
)

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")
#: hsl(H, S%, calc(<base>% + var(--bg-warmth))) or ... + max(0%, var(--bg-warmth))
#: (issue #12 adds the light preset's mirrored `calc(<base>% - max(0%, …))`).
_HSL_CALC_RE = re.compile(
    r"^hsl\(\s*(?P<hue>[\d.]+)\s*,\s*(?P<sat>[\d.]+)%\s*,\s*"
    r"calc\(\s*(?P<base>[\d.]+)%\s*(?P<sign>[+-])\s*"
    r"(?P<clamped>max\(\s*0%\s*,\s*)?var\(--bg-warmth\)\s*\)?\s*\)\s*\)$"
)


def _read_root_tokens() -> dict[str, str]:
    """Return the ``:root`` custom-property declarations of app.css (comments stripped).

    Only lines whose first non-space character is ``--`` are declarations; prose lines in
    the block's comments frequently mention token names and must not be parsed.
    """
    text = APP_CSS.read_text(encoding="utf-8")
    block = re.search(r":root\s*\{(?P<body>.*?)\n\}", text, re.DOTALL)
    assert block is not None, "app.css must keep a :root token block"
    tokens: dict[str, str] = {}
    for raw in block.group("body").splitlines():
        line = raw.strip()
        if not line.startswith("--"):
            continue
        line = line.split("/*", 1)[0].strip()
        if not line.endswith(";"):
            continue
        name, _, value = line[:-1].partition(":")
        tokens[name.strip()] = value.strip()
    return tokens


def _parse_pct(value: str) -> float:
    match = re.fullmatch(r"\s*([+-]?[\d.]+)%\s*", value)
    assert match, f"not a percentage: {value!r}"
    return float(match.group(1))


def _hsl_to_rgb(hue: float, sat: float, light: float) -> tuple[int, int, int]:
    """CSS HSL → 8-bit sRGB (the browser's own conversion, lightness clamped)."""
    light = min(100.0, max(0.0, light))
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, light / 100.0, sat / 100.0)
    return tuple(round(channel * 255) for channel in (r, g, b))  # type: ignore[return-value]


def _resolve(token: str, tokens: dict[str, str], warmth: float) -> tuple[int, int, int]:
    """Resolve one token at a given ``--bg-warmth`` value (percent points)."""
    value = tokens[token]
    hex_match = _HEX_RE.match(value)
    if hex_match:
        digits = hex_match.group(1)
        return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    hsl_match = _HSL_CALC_RE.match(value)
    assert hsl_match, f"{token}: unsupported value {value!r}"
    base = float(hsl_match.group("base"))
    shift = max(0.0, warmth) if hsl_match.group("clamped") else warmth
    if hsl_match.group("sign") == "-":
        shift = -shift
    return _hsl_to_rgb(float(hsl_match.group("hue")), float(hsl_match.group("sat")), base + shift)


def _linear(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_linear(channel) for channel in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG 2.x relative-luminance contrast ratio (1.0 … 21.0)."""
    first, second = _luminance(a), _luminance(b)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _palette_warmths() -> list[float]:
    return sorted({_parse_pct(entry["--bg-warmth"]) for entry in CURATED_PALETTE.values()})


TOKENS = _read_root_tokens()
PALETTE_WARMTHS = _palette_warmths()
MAX_WARMTH = max(PALETTE_WARMTHS)
MIN_WARMTH = min(PALETTE_WARMTHS)
#: Every palette value the tone resolver can emit, plus a fine grid across the range.
WARMTH_GRID = sorted(
    set(PALETTE_WARMTHS)
    | {MIN_WARMTH + 0.5 * step for step in range(int((MAX_WARMTH - MIN_WARMTH) * 2) + 1)}
    | {0.0}
)


def _label(warmth: float) -> str:
    return f"warmth {warmth:+.1f}%"


# -- the ladder's shape -------------------------------------------------------


def test_surface_ladder_is_warmth_driven():
    """Defect B: every rung — not just the page — is a --bg-warmth expression."""
    for token in SURFACE_TOKENS:
        value = TOKENS[token]
        assert "var(--bg-warmth)" in value, f"{token} must ride --bg-warmth: {value!r}"
        assert _HSL_CALC_RE.match(value), f"{token} must be hsl(<base> + warmth): {value!r}"


def test_warming_half_moves_the_ladder_and_cooling_half_does_not():
    """The rungs follow warming; a cool tone darkens only the page (see app.css note)."""
    for token in SURFACE_TOKENS[1:]:
        assert "max(0%, var(--bg-warmth))" in TOKENS[token], f"{token} needs the cooling floor"
    page = TOKENS["--bg"]
    assert "var(--bg-warmth)" in page
    assert "max(0%," not in page, "--bg must follow the tone in both directions"


def test_palette_reaches_a_positive_warm_maximum():
    """The audit's binding case (+6%) must still exist in the curated palette."""
    assert MAX_WARMTH > 0
    assert MAX_WARMTH == pytest.approx(_parse_pct(CURATED_PALETTE["warm"]["--bg-warmth"]))
    assert MIN_WARMTH == pytest.approx(_parse_pct(CURATED_PALETTE["dark"]["--bg-warmth"]))


# -- the audit's floors, at every tone ---------------------------------------


@pytest.mark.parametrize(("upper", "lower", "floor"), LADDER_STEPS)
def test_surface_steps_hold_at_every_tone(upper, lower, floor):
    for warmth in WARMTH_GRID:
        ratio = contrast(_resolve(upper, TOKENS, warmth), _resolve(lower, TOKENS, warmth))
        assert ratio >= floor, f"{upper}→{lower} at {_label(warmth)}: {ratio:.2f} < {floor}"


def test_input_well_never_collapses_toward_the_audit_ratio():
    """Defect B's headline pair: bg→inset must stay a real step at maximum warmth."""
    neutral = contrast(_resolve("--bg-inset", TOKENS, 0.0), _resolve("--bg", TOKENS, 0.0))
    warm = contrast(
        _resolve("--bg-inset", TOKENS, MAX_WARMTH), _resolve("--bg", TOKENS, MAX_WARMTH)
    )
    assert neutral >= 1.15
    assert warm >= 1.15, f"warm input well collapsed to {warm:.2f}:1"
    assert abs(warm - neutral) < 0.15, "warming must not bend the step out of shape"


@pytest.mark.parametrize(("token", "surfaces", "floor"), BOUNDARIES)
def test_boundaries_hold_at_every_tone(token, surfaces, floor):
    for warmth in WARMTH_GRID:
        boundary = _resolve(token, TOKENS, warmth)
        for surface in surfaces:
            ratio = contrast(boundary, _resolve(surface, TOKENS, warmth))
            assert ratio >= floor, f"{token} on {surface} at {_label(warmth)}: {ratio:.2f} < {floor}"


@pytest.mark.parametrize(("token", "surfaces", "floor"), TEXT)
def test_text_holds_at_every_tone(token, surfaces, floor):
    for warmth in WARMTH_GRID:
        colour = _resolve(token, TOKENS, warmth)
        for surface in surfaces:
            ratio = contrast(colour, _resolve(surface, TOKENS, warmth))
            assert ratio >= floor, f"{token} on {surface} at {_label(warmth)}: {ratio:.2f} < {floor}"


def test_text_dim_stays_below_muted_and_both_below_body():
    """The hierarchy survives the lift: dim < muted < body on the raised rung."""
    for warmth in WARMTH_GRID:
        surface = _resolve("--bg-raised", TOKENS, warmth)
        dim = contrast(_resolve("--text-dim", TOKENS, warmth), surface)
        muted = contrast(_resolve("--text-muted", TOKENS, warmth), surface)
        body = contrast(_resolve("--text", TOKENS, warmth), surface)
        assert dim < muted < body, f"text hierarchy inverted at {_label(warmth)}"


# -- Defect A: no boundary token drawn on a wash ----------------------------

_STEP_EDITOR_RULES = re.findall(
    r"([^{}]+)\{([^{}]*)\}", STEP_EDITOR.read_text(encoding="utf-8")
)


def test_hover_rule_uses_the_wash_boundary_token():
    """The one rule that paints --line on a hover wash now uses --line-strong."""
    hover_rules = [
        body
        for selector, body in _STEP_EDITOR_RULES
        if selector.strip().endswith(".icon:hover:not(:disabled)")
    ]
    assert hover_rules, "StepEditor.svelte must keep its .icon:hover rule"
    rule = hover_rules[0]
    assert "background: var(--bg-hover)" in rule
    assert "border-color: var(--line-strong)" in rule
    assert "border-color: var(--line)" not in rule


def test_no_other_hover_wash_draws_the_structured_line_token():
    """--line on a --bg-hover / --bg-active wash is exactly the #21 defect; pin it out."""
    offenders = []
    for path in (ROOT / "frontend" / "src").rglob("*"):
        if path.suffix not in (".svelte", ".css"):
            continue
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", path.read_text(encoding="utf-8")):
            if "var(--line)" not in body:
                continue
            on_wash = bool(re.search(r"var\(--line\)", body)) and (
                ":hover" in selector or ":active" in selector
            )
            if on_wash:
                offenders.append(f"{path.relative_to(ROOT)}: {selector.strip()[-40:]}")
    assert offenders == [], f"--line drawn on a hover/active wash: {offenders}"


# -- the presets (issue #12, #37; rulings D-A / D-D) --------------------------
#
# `:root` IS the dark preset; the other blocks extend the tone geometry that
# landed with #14 with the full token tables (surfaces, text, lines, accent
# geometry, color-scheme, status). This half of the module re-audits every preset
# with the same WCAG math, so a retuned table fails here instead of shipping.
# Issue #37 removed `mid` and added `oled` (true-black panels) and `savilen`
# (the reference image's palette).
#
# What binds, and what is an accepted exception (owner rulings, thread #12):
#
#   * surface steps: 1.15 / 1.15 / 1.20 / 1.15 / 1.20 at EVERY tone, all presets
#     except OLED's two black-panel steps (see PRESET_LADDER_FLOORS);
#   * --text / --text-muted / --text-dim: AA 4.5:1 on the settled rungs (page,
#     inset, panel, raised) at every tone, all presets;
#   * --text-muted on the selected row: AA at the NEUTRAL active rung — the
#     stated rule is "active rows use --text-muted or brighter", and a warm wash
#     is a transient hover the micro-labels do not sit on;
#   * --line-strong: 3:1 on EVERY surface including both washes (it borders
#     interactive chrome — the D-A half that is enforced, not exempted);
#   * --line: D-A keeps it a decorative-exempt hairline. dark already clears the
#     3:1 graphical minimum, so its #21 floor is left standing rather than
#     weakened; OLED keeps the hairline (~1.95:1 at its binding, warm, rung);
#     light and savilen are STRICT presets, so their --line holds 3:1 too;
#   * accepted exceptions, pinned as measured numbers rather than floors, so a
#     silent drift is still a failure: dark's on-accent 3.78 (fills, not small
#     text); oled's black-on-grey accent 13.75; savilen's accent-vs-page 4.92
#     (a fill, not small text).

PRESET_SELECTORS: dict[str, str] = {
    "dark": ":root",
    "oled": ":root[data-theme='oled']",
    "light": ":root[data-theme='light']",
    "savilen": ":root[data-theme='savilen']",
}
PRESET_IDS = tuple(PRESET_SELECTORS)

#: The token set the owner ruled DIFFERS per preset (thread #12).
PRESET_DIFFERING_TOKENS = (
    "--bg",
    "--bg-panel",
    "--bg-raised",
    "--bg-inset",
    "--bg-hover",
    "--bg-active",
    "--text",
    "--text-muted",
    "--text-dim",
    "--line",
    "--line-strong",
    "--accent-l",
    "--accent-hover-l",
    "--accent-strong-l",
    "--on-accent",
)

#: D-A: --line's floor per preset (see the block comment above). Savilen's
#: --line is the reference border teal lifted to the STRICT 3:1; OLED keeps the
#: decorative-hairline exemption on its black page (1.95:1 worst on a warm card).
LINE_FLOORS: dict[str, float] = {"dark": 3.0, "oled": 1.9, "light": 3.0, "savilen": 3.0}

#: The measured ratios the thread states for the new presets (accepted values,
#: not floors): preset -> {measurement: ratio}.
THREAD_RATIOS: dict[str, dict[str, float]] = {
    "dark": {
        # Shipped #21/#14 values, restated so a retune has to argue with them.
        "text-dim on raised": 5.75,
        "line on raised": 3.90,
        "line-strong on hover": 4.40,
        "on-accent": 3.78,  # FLAGGED: fills only, never small text
    },
    "light": {
        "accent vs page": 7.04,
        "on-accent": 7.78,
    },
    "savilen": {
        # The reference palette measured at neutral: #1f8f86 accent on the
        # #001012 page, #4fe6d8 dim copy on the raised card, black-ish on-accent.
        "accent vs page": 4.92,
        "on-accent": 4.92,
        "text-dim on raised": 6.30,
    },
    "oled": {
        # True black page, grey accent fill: black-on-fill is the one ratio the
        # page cannot supply on its own.
        "on-accent": 13.75,
    },
}


def _preset_tokens(preset: str) -> dict[str, str]:
    """The declarations of one preset block, comments stripped.

    A preset block only carries what it CHANGES: ``--accent-hue`` / ``--accent-sat``
    (and the typography, z-lanes …) are inherited from ``:root``, so the read
    returns the block's own declarations and callers fall back to dark.
    """
    text = APP_CSS.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    selector = PRESET_SELECTORS[preset]
    pattern = (
        re.escape(selector) + r"\s*\{(?P<body>.*?)\n\}"
        if preset != "dark"
        else r"(?<![\w\]'])" + re.escape(selector) + r"\s*\{(?P<body>.*?)\n\}"
    )
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None, f"app.css must keep a {selector} block"
    declarations: dict[str, str] = {}
    for raw in match.group("body").splitlines():
        line = raw.strip()
        if not line.startswith("--") or not line.endswith(";"):
            continue
        name, _, value = line[:-1].partition(":")
        declarations[name.strip()] = value.strip()
    return declarations


PRESET_TOKENS: dict[str, dict[str, str]] = {
    # dark inherits nothing; the others fall back to it for the shared tokens.
    "dark": _preset_tokens("dark"),
    "oled": {**_preset_tokens("dark"), **_preset_tokens("oled")},
    "light": {**_preset_tokens("dark"), **_preset_tokens("light")},
    "savilen": {**_preset_tokens("dark"), **_preset_tokens("savilen")},
}


def _token(preset: str, name: str) -> str:
    return PRESET_TOKENS[preset][name]


def _pct(value: str) -> float:
    match = re.fullmatch(r"\s*([+-]?[\d.]+)%\s*", value)
    assert match, f"not a percentage: {value!r}"
    return float(match.group(1))


def _color(preset: str, name: str, warmth: float = 0.0) -> tuple[int, int, int]:
    """Resolve a hex-or-calc token of one preset at a given warmth."""
    return _resolve(name, PRESET_TOKENS[preset], warmth)


def _accent(preset: str, role: str = "--accent-l", warmth: float = 0.0) -> tuple[int, int, int]:
    """The preset's accent at ``role`` (``--accent-l`` / ``-hover-l`` / ``-strong-l``)."""
    hue = float(_token(preset, "--accent-hue"))
    sat = _pct(_token(preset, "--accent-sat"))
    return _hsl_to_rgb(hue, sat, _pct(_token(preset, role)))


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return contrast(a, b)


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_every_preset_declares_the_differing_token_set(preset):
    """The ruled differing set is present in every block (no inherited stale value)."""
    for token in PRESET_DIFFERING_TOKENS:
        assert token in _preset_tokens(preset), f"{preset} must declare {token}"

    # color-scheme is the one non-custom-property that differs: light must reach
    # native scrollbars / form controls, so it cannot inherit `dark`.
    text = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    block = re.search(
        re.escape(PRESET_SELECTORS[preset]) + r"\s*\{(?P<body>.*?)\n\}", text, re.DOTALL
    )
    assert block is not None
    expected = "light" if preset == "light" else "dark"
    assert f"color-scheme: {expected};" in block.group("body"), f"{preset}: color-scheme"


#: The #21 step floors, per preset. OLED is the ONE ruled exception (issue #37):
#: its page, inset and panel are all true black, so those two steps cannot hold
#: 1.15:1 by definition — the separation between the black rungs is drawn by
#: --line-strong, which the boundary test still pins at 3:1 on every rung. Every
#: rung that CAN carry a luminance step keeps the standard floor.
STANDARD_LADDER_FLOORS: dict[tuple[str, str], float] = {
    (upper, lower): floor for upper, lower, floor in LADDER_STEPS
}
PRESET_LADDER_FLOORS: dict[str, dict[tuple[str, str], float]] = {
    "dark": STANDARD_LADDER_FLOORS,
    "light": STANDARD_LADDER_FLOORS,
    "savilen": STANDARD_LADDER_FLOORS,
    "oled": {
        **STANDARD_LADDER_FLOORS,
        ("--bg-inset", "--bg"): 1.0,
        ("--bg-panel", "--bg-inset"): 1.0,
    },
}


@pytest.mark.parametrize(("upper", "lower"), [(u, l) for u, l, _ in LADDER_STEPS])
@pytest.mark.parametrize("preset", PRESET_IDS)
def test_preset_surface_steps_hold_at_every_tone(preset, upper, lower):
    """The #21 ladder floors are per-preset, not per-stylesheet."""
    floor = PRESET_LADDER_FLOORS[preset][(upper, lower)]
    for warmth in WARMTH_GRID:
        ratio = _contrast(_color(preset, upper, warmth), _color(preset, lower, warmth))
        assert ratio >= floor, f"{preset} {upper}->{lower} at {_label(warmth)}: {ratio:.2f} < {floor}"


def test_oled_black_panels_are_actual_black_and_the_boundary_carries_the_rest():
    """Issue #37: the page/inset/panel rungs ARE #000; --line-strong separates them.

    This is the exception the ladder floors above grant, pinned positively: the
    black is real (not a near-black approximation), and the boundary token that
    replaces the missing luminance step holds 3:1 on every surface.
    """
    for token in ("--bg", "--bg-inset", "--bg-panel"):
        assert _color("oled", token) == (0, 0, 0), token
    # The rungs that can move are not black, so the ladder still has depth.
    assert _luminance(_color("oled", "--bg-raised")) > _luminance((0, 0, 0))
    for surface in SURFACE_TOKENS:
        for warmth in WARMTH_GRID:
            ratio = _contrast(_color("oled", "--line-strong", warmth), _color("oled", surface, warmth))
            assert ratio >= 3.0, f"oled --line-strong on {surface} at {_label(warmth)}: {ratio:.2f}"


@pytest.mark.parametrize("preset", ("oled", "light", "savilen"))
def test_preset_ladder_shape_matches_its_warmth_direction(preset):
    """Every preset rung rides --bg-warmth; the direction is the preset's own.

    dark, oled and savilen warm UPWARD (only the warming half moves the rungs,
    because a uniform down-shift would squeeze the ladder against black); light
    warms DOWNWARD and holds the cooling half, because white is its ceiling.
    """
    page = _token(preset, "--bg")
    assert "var(--bg-warmth)" in page
    if preset == "light":
        assert "- max(0%, var(--bg-warmth))" in page, "light's page creams DOWN"
        for token in SURFACE_TOKENS:
            assert "- max(0%, var(--bg-warmth))" in _token(preset, token), token
    else:
        assert "max(0%," not in page, f"{preset}'s page must follow both directions"
        for token in SURFACE_TOKENS[1:]:
            assert "max(0%, var(--bg-warmth))" in _token(preset, token), token
    for token in SURFACE_TOKENS:
        assert _HSL_CALC_RE.match(_token(preset, token)), f"{preset} {token}"


@pytest.mark.parametrize(("token", "floor"), (("--text", 4.5), ("--text-muted", 4.5), ("--text-dim", 4.5)))
@pytest.mark.parametrize("preset", PRESET_IDS)
def test_preset_text_holds_on_the_settled_rungs(preset, token, floor):
    """AA on every settled rung the micro-labels and body copy can land on."""
    settled = ("--bg-raised", "--bg-panel", "--bg-inset", "--bg")
    for warmth in WARMTH_GRID:
        colour = _color(preset, token, warmth)
        for surface in settled:
            ratio = _contrast(colour, _color(preset, surface, warmth))
            assert ratio >= floor, f"{preset} {token} on {surface} at {_label(warmth)}: {ratio:.2f}"


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_preset_muted_holds_on_the_selected_row(preset):
    """The stated rule: active rows use --text-muted or brighter, so muted is AA there."""
    for warmth in WARMTH_GRID:
        ratio = _contrast(_color(preset, "--text-muted", warmth), _color(preset, "--bg-active", warmth))
        # The neutral row is the binding case; a +6% warm wash is transient.
        if warmth <= 0.0:
            assert ratio >= 4.5, f"{preset} muted on active at {_label(warmth)}: {ratio:.2f}"


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_preset_text_hierarchy_survives(preset):
    """dim < muted < body on the raised rung, at every tone."""
    for warmth in WARMTH_GRID:
        surface = _color(preset, "--bg-raised", warmth)
        dim = _contrast(_color(preset, "--text-dim", warmth), surface)
        muted = _contrast(_color(preset, "--text-muted", warmth), surface)
        body = _contrast(_color(preset, "--text", warmth), surface)
        assert dim < muted < body, f"{preset} text hierarchy inverted at {_label(warmth)}"


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_preset_line_strong_holds_where_chrome_borders(preset):
    """D-A's enforced half: every interactive boundary is 3:1 on every surface."""
    for warmth in WARMTH_GRID:
        boundary = _color(preset, "--line-strong", warmth)
        for surface in SURFACE_TOKENS:
            ratio = _contrast(boundary, _color(preset, surface, warmth))
            assert ratio >= 3.0, f"{preset} --line-strong on {surface} at {_label(warmth)}: {ratio:.2f}"


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_preset_line_holds_its_role_floor(preset):
    """--line is decorative-exempt per D-A; light gets the strict treatment."""
    floor = LINE_FLOORS[preset]
    settled = ("--bg-raised", "--bg-panel", "--bg-inset", "--bg")
    for warmth in WARMTH_GRID:
        line = _color(preset, "--line", warmth)
        for surface in settled:
            ratio = _contrast(line, _color(preset, surface, warmth))
            assert ratio >= floor, f"{preset} --line on {surface} at {_label(warmth)}: {ratio:.2f} < {floor}"


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_preset_accent_geometry_is_a_preset_decision(preset):
    """--accent-l / -hover-l / -strong-l replace the hard-coded 66/60/78.

    Every preset retunes all three (so no block silently inherits dark's accent),
    the accent-as-text role clears AA-ish on its own surfaces, and the fill keeps
    its on-accent text readable.
    """
    l_base = _pct(_token(preset, "--accent-l"))
    l_hover = _pct(_token(preset, "--accent-hover-l"))
    l_strong = _pct(_token(preset, "--accent-strong-l"))
    assert len({l_base, l_hover, l_strong}) == 3, f"{preset}: accent roles collapsed"
    strong = _accent(preset, "--accent-strong-l")
    for surface in ("--bg", "--bg-inset", "--bg-panel", "--bg-raised"):
        ratio = _contrast(strong, _color(preset, surface, 0.0))
        assert ratio >= 4.0, f"{preset}: --accent-strong on {surface} is {ratio:.2f}"
    on_accent = _color(preset, "--on-accent")
    fill = _contrast(on_accent, _accent(preset))
    assert fill >= 3.5, f"{preset}: on-accent on the accent fill is {fill:.2f}"


def test_accent_geometry_differs_between_presets():
    """Every preset has its own accent lightness; no block silently inherits."""
    values = {preset: _pct(_token(preset, "--accent-l")) for preset in PRESET_IDS}
    assert len(set(values.values())) == len(PRESET_IDS), values


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_preset_status_tokens_are_aa_when_used_as_text(preset):
    """--danger-text / --warn / --ok are text roles, not decoration.

    Measured on the surfaces a notice actually renders on (the page and the wells
    / drawers). dark's shipped --danger-text sits at 4.14:1 on a raised CARD — the
    pre-#12 value, left alone deliberately: a retune of the shipped preset is not
    this lane's to make, and the notice rows do not live on cards.
    """
    for token in ("--danger-text", "--warn", "--ok"):
        colour = _color(preset, token, 0.0)
        for surface in ("--bg", "--bg-inset", "--bg-panel"):
            ratio = _contrast(colour, _color(preset, surface, 0.0))
            assert ratio >= 4.5, f"{preset} {token} on {surface}: {ratio:.2f}"


def test_savilen_table_reproduces_the_reference_palette():
    """Issue #37: the extracted hexes land where the stylesheet says they do.

    The image's page (#001012), accent (#1f8f86) and primary text (#4fe6d8) are
    the actual extracted values, not eyeballed approximations; --line-strong is
    the same bright cyan. The accent's on-text is the page colour (4.92:1).
    """
    assert _color("savilen", "--bg") == (0x00, 0x10, 0x12)
    accent = _accent("savilen")
    assert accent == (0x1F, 0x8F, 0x86)
    assert _color("savilen", "--text") == (0x4F, 0xE6, 0xD8)
    assert _color("savilen", "--line-strong") == (0x4F, 0xE6, 0xD8)
    assert _contrast(_color("savilen", "--on-accent"), accent) == pytest.approx(4.92, abs=0.02)


def test_light_table_reproduces_the_threads_stated_numbers():
    """The light row: #f4f3f7 page, #1a1726 text, #6322d3 accent, 7.04 / 7.78."""
    assert _color("light", "--bg") == (0xF4, 0xF3, 0xF7)
    assert _color("light", "--text") == (0x1A, 0x17, 0x26)
    accent = _accent("light")
    assert accent == (0x63, 0x22, 0xD3)
    assert _contrast(accent, _color("light", "--bg")) == pytest.approx(7.04, abs=0.01)
    assert _contrast((255, 255, 255), accent) == pytest.approx(7.78, abs=0.01)


def test_dark_table_keeps_the_landed_numbers_and_the_flagged_on_accent():
    """dark is the shipped palette: the thread's on-accent 3.78 is FILLS-only.

    The thread's Option-1 dark line/text row (#6c6293 / #9790a6, 3:1 against the
    PAGE only) would drop --line to 2.08:1 on a card and --text-dim to 3.74:1 —
    under the landed #21 floors — so D-A keeps this table instead. The flagged
    exception is the accent's white text: fine inside a fill, never for copy.
    """
    assert _contrast((255, 255, 255), _accent("dark")) == pytest.approx(3.78, abs=0.01)
    assert _contrast(_color("dark", "--text-dim"), _color("dark", "--bg-raised")) == pytest.approx(
        5.75, abs=0.02
    )
    assert _contrast(_color("dark", "--line"), _color("dark", "--bg-raised")) == pytest.approx(
        3.90, abs=0.02
    )


@pytest.mark.parametrize(("preset", "measurement", "expected"), [
    (preset, measurement, expected)
    for preset, table in THREAD_RATIOS.items()
    for measurement, expected in table.items()
])
def test_thread_ratios_are_reproduced(preset, measurement, expected):
    """Every ratio the issue thread states, measured from the stylesheet itself."""
    if measurement == "text-dim on raised":
        measured = _contrast(_color(preset, "--text-dim"), _color(preset, "--bg-raised"))
    elif measurement == "text-dim on active":
        measured = _contrast(_color(preset, "--text-dim"), _color(preset, "--bg-active"))
    elif measurement == "line on raised":
        measured = _contrast(_color(preset, "--line"), _color(preset, "--bg-raised"))
    elif measurement == "line-strong on hover":
        measured = _contrast(_color(preset, "--line-strong"), _color(preset, "--bg-hover"))
    elif measurement == "accent vs page":
        measured = _contrast(_accent(preset), _color(preset, "--bg"))
    elif measurement == "on-accent":
        measured = _contrast(_color(preset, "--on-accent"), _accent(preset))
    else:  # pragma: no cover - the table above is exhaustive
        raise AssertionError(measurement)
    assert measured == pytest.approx(expected, abs=0.03), f"{preset}: {measurement}"


def test_theme_stylesheet_never_uses_important():
    """The precedence contract (D-D) needs the stylesheet to stay outrankable.

    Tone tokens are INLINE on <html>, so any `!important` in a DECLARATION here
    would flip the ruling on its head (reader furniture outranking the fiction's
    palette). The user's pasted CSS is where `!important` belongs, and the guard
    keeps it. Prose in comments may mention it — that is documentation, not a
    declaration.
    """
    text = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    assert "!important" not in text, "app.css must not use !important (ruling D-D)"


# -- the wallpaper layer (issue #13) ------------------------------------------
#
# The owner ruling in the #13 thread: the image sits BEHIND the prose with a scrim
# of at least 0.60 under --text areas and 0.80 (--scrim-muted) under every surface
# carrying --text-muted/-dim, and the ladder is re-solved in the same pass. The
# layer is attribute-scoped (`:root[data-wallpaper]`), so the pins ABOVE are the
# no-wallpaper contract and keep reading `:root` / the preset blocks untouched:
# nothing in this section can weaken them, and the guard test below asserts the
# scrims never appear outside a wallpaper-scoped selector.
#
# The math: an image pixel is composited UP through the scrims (sRGB, like the
# browser), so the binding case for light text is the BRIGHTEST pixel an image can
# have (white) and the binding case for the light preset's dark text is the
# DARKEST (black). Both are measured for every preset — the test asserts the
# floors at both ends, which is strictly stronger than "the bright-pixel worst
# case" and costs nothing.

#: The ladder tokens the wallpaper layer re-solves, page first.
WALLPAPER_RUNGS = ("--bg", "--bg-inset", "--bg-panel", "--bg-raised", "--bg-hover", "--bg-active")

#: Floors carried over from the #21/#12 audit (unchanged by the layer).
WALLPAPER_TEXT_FLOOR = 4.5
WALLPAPER_LINE_FLOOR = 3.0

#: The pixels an image can end at; a real wallpaper is somewhere between.
EXTREME_PIXELS = ((255, 255, 255), (0, 0, 0))

_RGBA_RE = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)$")


def _wallpaper_block(selector: str) -> dict[str, str]:
    """Declarations of one wallpaper-layer block (comments already stripped)."""
    text = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    match = re.search(re.escape(selector) + r"\s*\{(?P<body>.*?)\n\}", text, re.DOTALL)
    assert match is not None, f"app.css must keep a {selector} block"
    declarations: dict[str, str] = {}
    for raw in match.group("body").splitlines():
        line = raw.strip()
        if not line.startswith("--") or not line.endswith(";"):
            continue
        name, _, value = line[:-1].partition(":")
        declarations[name.strip()] = value.strip()
    return declarations


def _wallpaper_tokens(preset: str) -> dict[str, str]:
    """The layer's tokens for one preset: the base block plus its own overrides."""
    tokens = dict(_wallpaper_block(":root[data-wallpaper]"))
    if preset != "dark":
        tokens.update(_wallpaper_block(f":root[data-wallpaper][data-theme='{preset}']"))
    else:
        # dark IS the base block; assert it is also the preset's own answer.
        assert tokens == _wallpaper_block(":root[data-wallpaper]")
    return tokens


WALLPAPER_TOKENS: dict[str, dict[str, str]] = {
    preset: _wallpaper_tokens(preset) for preset in PRESET_IDS
}


def _resolve_rgba(tokens: dict[str, str], name: str) -> tuple[int, int, int, float]:
    """Resolve a token to ``(r, g, b, alpha)``, following ONE `var(…)` indirection.

    The layer writes the muted rungs as `var(--scrim-muted)` so the preset blocks
    can retune the colour without restating the alpha — the indirection is CSS's,
    so the test resolves it the same way the browser would.
    """
    value = tokens[name]
    if value.startswith("var("):
        inner = value[value.index("(") + 1 : value.rindex(")")].strip()
        return _resolve_rgba(tokens, inner)
    match = _RGBA_RE.match(value)
    assert match is not None, f"{name}: expected an rgba composite, got {value!r}"
    r, g, b, alpha = match.groups()
    return int(r), int(g), int(b), float(alpha)


def _over(top: tuple[int, int, int, float], bottom: tuple[int, int, int]) -> tuple[int, int, int]:
    """Composite ``top`` (translucent) over ``bottom`` in sRGB — the browser's math."""
    r, g, b, alpha = top
    return tuple(round(alpha * channel + (1 - alpha) * under) for channel, under in zip((r, g, b), bottom))  # type: ignore[return-value]


def _wallpaper_ladder(preset: str, pixel: tuple[int, int, int]) -> dict[str, tuple[int, int, int]]:
    """Every surface's ON-SCREEN colour for one preset over one image pixel."""
    tokens = WALLPAPER_TOKENS[preset]
    page = _over(_resolve_rgba(tokens, "--bg"), pixel)
    ladder = {"--bg": page}
    for name in WALLPAPER_RUNGS[1:]:
        ladder[name] = _over(_resolve_rgba(tokens, name), page)
    return ladder


def _wallpaper_colour(preset: str, name: str) -> tuple[int, int, int]:
    match = _HEX_RE.match(WALLPAPER_TOKENS[preset][name])
    assert match is not None, f"{name}: expected a hex colour, got {WALLPAPER_TOKENS[preset][name]!r}"
    digits = match.group(1)
    return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


# -- the layer is opt-in and re-solves, never re-bases ------------------------


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_wallpaper_layer_resolves_only_after_the_attribute_is_set(preset):
    """The layer is a re-solve: every token it sets already ships elsewhere."""
    tokens = WALLPAPER_TOKENS[preset]
    for name in tokens:
        if name in ("--wallpaper-scrim", "--scrim-muted"):
            continue  # new tokens, scoped to the layer (asserted by the guard below)
        known = name in TOKENS or name in PRESET_TOKENS[preset]
        assert known, f"{preset}: {name} is not part of the shipped sheet"


def test_the_scrims_never_reach_a_non_wallpaper_install():
    """A scrim outside `[data-wallpaper]` would veil installs that set no image."""
    text = re.sub(r"/\*.*?\*/", "", APP_CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        if "--wallpaper-scrim" in body or "--scrim-muted" in body:
            assert "data-wallpaper" in selector, selector.strip()


def test_no_wallpaper_reads_are_untouched_by_the_layer():
    """The #21/#12 pins read `:root` and the preset blocks — the new tokens are absent."""
    for name in ("--wallpaper-scrim", "--scrim-muted"):
        assert name not in TOKENS
        for preset in PRESET_IDS:
            assert name not in PRESET_TOKENS[preset]
    # And the shipped --bg is still the warmth-driven hsl(), not an rgba composite.
    assert _HSL_CALC_RE.match(TOKENS["--bg"]), "the base page token must stay a warmth ladder"


# -- the owner floors: 0.60 under the prose, 0.80 under muted copy ------------


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_the_scrim_alphas_are_the_ruled_floors(preset):
    """Page >= 0.60, every muted rung >= 0.80, and the rungs step up in depth."""
    tokens = WALLPAPER_TOKENS[preset]
    page_alpha = _resolve_rgba(tokens, "--bg")[3]
    muted_alpha = _resolve_rgba(tokens, "--scrim-muted")[3]
    assert page_alpha >= 0.60, f"{preset}: prose scrim {page_alpha} < 0.60"
    assert muted_alpha >= 0.80, f"{preset}: muted scrim {muted_alpha} < 0.80"

    alphas = [_resolve_rgba(tokens, name)[3] for name in WALLPAPER_RUNGS]
    assert alphas[0] == page_alpha
    for name, alpha in zip(WALLPAPER_RUNGS[1:], alphas[1:]):
        assert alpha >= muted_alpha, f"{preset}: {name} is under the muted floor"
    assert alphas == sorted(alphas), f"{preset}: depth inverted: {alphas}"

    # The probe's stacked figure: a muted rung over the prose scrim.
    stacked = 1 - (1 - page_alpha) * (1 - muted_alpha)
    assert stacked >= 0.90, f"{preset}: stacked scrim only {stacked:.2f}"


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_the_wallpaper_ladder_does_not_read_warmth(preset):
    """--bg-warmth is FORCED to 0% and the composites stop depending on it.

    A session tone sets --bg-warmth INLINE (it outranks a stylesheet, by design),
    so the only way "forced to 0" can be true is for the ladder to stop reading
    the token at all — which is what this asserts.
    """
    tokens = WALLPAPER_TOKENS[preset]
    assert tokens["--bg-warmth"] == "0%"
    for name in WALLPAPER_RUNGS:
        assert "var(--bg-warmth)" not in tokens[name], f"{preset}: {name} still rides warmth"


# -- the floors, at every preset and at both image extremes -------------------


@pytest.mark.parametrize("preset", PRESET_IDS)
@pytest.mark.parametrize("pixel", EXTREME_PIXELS, ids=["white-pixel", "black-pixel"])
def test_text_holds_over_the_image_at_the_worst_pixel(preset, pixel):
    """AA for body, muted and dim copy on every surface the image shows through."""
    ladder = _wallpaper_ladder(preset, pixel)
    for surface, colour in ladder.items():
        for token in ("--text", "--text-muted", "--text-dim"):
            ratio = _contrast(_wallpaper_colour(preset, token), colour)
            assert ratio >= WALLPAPER_TEXT_FLOOR, (
                f"{preset} {token} on {surface} over {pixel}: {ratio:.2f} < {WALLPAPER_TEXT_FLOOR}"
            )


@pytest.mark.parametrize("preset", PRESET_IDS)
@pytest.mark.parametrize("pixel", EXTREME_PIXELS, ids=["white-pixel", "black-pixel"])
def test_boundaries_hold_over_the_image_at_the_worst_pixel(preset, pixel):
    """The ruling's strict re-solve: every line holds 3:1 on every rung."""
    ladder = _wallpaper_ladder(preset, pixel)
    for surface, colour in ladder.items():
        for token in ("--line", "--line-strong"):
            ratio = _contrast(_wallpaper_colour(preset, token), colour)
            assert ratio >= WALLPAPER_LINE_FLOOR, (
                f"{preset} {token} on {surface} over {pixel}: {ratio:.2f} < {WALLPAPER_LINE_FLOOR}"
            )


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_the_text_hierarchy_survives_the_lift(preset):
    """dim < muted < body on the raised rung, at that preset's binding pixel."""
    pixel = (0, 0, 0) if preset == "light" else (255, 255, 255)
    surface = _wallpaper_ladder(preset, pixel)["--bg-raised"]
    dim = _contrast(_wallpaper_colour(preset, "--text-dim"), surface)
    muted = _contrast(_wallpaper_colour(preset, "--text-muted"), surface)
    body = _contrast(_wallpaper_colour(preset, "--text"), surface)
    assert dim < muted < body, f"{preset}: hierarchy inverted under a wallpaper"


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_the_prose_surface_is_the_binding_rung(preset):
    """The page carries the fewest scrim layers, so it is where the floors bind.

    If the layer ever made a muted rung LIGHTER than the page (for dark text) or
    darker (for light text), the ladder would invert and the page would stop being
    the worst case — a silent hole in the guarantee above.
    """
    pixel = (0, 0, 0) if preset == "light" else (255, 255, 255)
    ladder = _wallpaper_ladder(preset, pixel)
    page = _luminance(ladder["--bg"])
    raised = _luminance(ladder["--bg-raised"])
    if preset == "light":
        assert raised >= page, "a muted rung must never be darker than the page here"
    else:
        assert raised <= page, "a muted rung must never be lighter than the page here"
