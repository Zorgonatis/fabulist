"""Tests for deterministic Style-State theme tokens (R4 / W2.4).

Pure-function coverage of :mod:`fabulist.theme.tokens`: every curated tone returns
its exact override map, unknown / missing / malformed tones fall back to
:data:`NEUTRAL`, matching is case-insensitive with the documented key priority, and
:func:`token_style` renders the inline declarations (including the empty case).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_theme_contrast_ladder import _hsl_to_rgb, contrast

from fabulist.theme.tokens import (
    CURATED_PALETTE,
    NEUTRAL,
    TONE_GEOMETRY,
    TONE_HUE_OFFSETS,
    TONE_KEYS,
    TONE_LINE_ALPHA,
    TONE_SOFT_ALPHA,
    style_tokens,
    token_style,
)

REQUESTED_TONES = (
    "warm",
    "tense",
    "dark",
    "playful",
    "intimate",
    "mysterious",
    "hopeful",
    "somber",
)

TOKEN_KEYS = ("--accent-hue", "--accent-sat", "--bg-warmth")


def test_neutral_constant_is_empty_mapping():
    assert NEUTRAL == {}
    assert isinstance(NEUTRAL, dict)


def test_palette_covers_all_requested_tones():
    missing = [tone for tone in REQUESTED_TONES if tone not in CURATED_PALETTE]
    assert missing == []
    assert len(CURATED_PALETTE) >= 8


@pytest.mark.parametrize("tone", REQUESTED_TONES)
def test_curated_tone_returns_its_map(tone):
    expected = CURATED_PALETTE[tone]
    tokens = style_tokens({"tone": tone})
    assert tokens == expected
    # Each curated entry defines exactly the documented token set.
    assert set(tokens) == set(TOKEN_KEYS)
    assert tokens["--accent-hue"].isdigit()
    assert tokens["--accent-sat"].endswith("%")
    assert tokens["--bg-warmth"].endswith("%")


@pytest.mark.parametrize("tone", REQUESTED_TONES)
def test_curated_match_is_case_insensitive(tone):
    assert style_tokens({"tone": tone.upper()}) == CURATED_PALETTE[tone]
    assert style_tokens({"tone": f"  {tone.title()}  "}) == CURATED_PALETTE[tone]


# -- freeform phrase resolution (issue #20) ----------------------------------


def test_phrase_containing_a_key_resolves_to_that_key():
    """The issue's example: a comma-separated phrase themes on its one curated word."""
    phrase = "warm, cluttered, and suddenly watchful"
    assert style_tokens({"tone": phrase}) == CURATED_PALETTE["warm"]


@pytest.mark.parametrize(
    ("phrase", "tone"),
    [
        ("cluttered and warm", "warm"),
        ("A tense, rain-slick street at midnight", "tense"),
        ("(mysterious)", "mysterious"),
        ("HOPEFUL — a thin grey dawn", "hopeful"),
        ("quiet / intimate / low light", "intimate"),
    ],
)
def test_keyword_inside_freeform_phrase(phrase, tone):
    assert style_tokens({"tone": phrase}) == CURATED_PALETTE[tone]


def test_multiple_keys_precedence_is_earliest_occurrence():
    """Documented precedence: the curated key that starts earliest in the value."""
    assert style_tokens({"tone": "tense but warm"}) == CURATED_PALETTE["tense"]
    assert style_tokens({"tone": "warm but tense"}) == CURATED_PALETTE["warm"]


def test_multiple_keys_precedence_is_not_palette_order():
    """Position decides, so adding palette entries cannot silently re-rank a phrase."""
    assert style_tokens({"tone": "playful then somber"}) == CURATED_PALETTE["playful"]
    assert style_tokens({"tone": "somber then playful"}) == CURATED_PALETTE["somber"]


def test_keyword_must_be_a_whole_word():
    # Derived words and hyphenated compounds stay unmatched (documented boundary).
    assert style_tokens({"tone": "darkness at the edge"}) == {}
    assert style_tokens({"tone": "warmth"}) == {}
    assert style_tokens({"tone": "tense-ish"}) == {}
    assert style_tokens({"tone": "warm_tone"}) == {}


def test_unknown_phrase_is_neutral():
    assert style_tokens({"tone": "apocalyptic neon and chrome"}) == {}


def test_phrase_resolution_is_deterministic():
    phrase = {"tone": "warm, cluttered, and suddenly watchful"}
    first = style_tokens(phrase)
    assert all(style_tokens(phrase) == first for _ in range(5))


def test_keyword_tie_prefers_longest_key_and_exact_still_wins(monkeypatch):
    """Tie at the same offset -> longest key; an exact key always outranks keywords."""
    winner = {"--accent-hue": "1", "--accent-sat": "2%", "--bg-warmth": "+0%"}
    monkeypatch.setitem(CURATED_PALETTE, "dark fantasy", winner)
    assert style_tokens({"tone": "a dark fantasy room"}) == winner
    assert style_tokens({"tone": "Dark Fantasy"}) == winner
    # Different offsets keep the earliest-occurrence rule even against a longer key.
    assert style_tokens({"tone": "dark and a dark fantasy room"}) == CURATED_PALETTE["dark"]


def test_unknown_tone_is_neutral():
    assert style_tokens({"tone": "tense-ish"}) == {}
    assert style_tokens({"tone": "apocalyptic neon"}) == {}


def test_empty_state_is_neutral():
    assert style_tokens({}) == {}


def test_missing_tone_key_is_neutral():
    assert style_tokens({"weather": "light rain", "loc": "night market"}) == {}


def test_blank_and_non_string_tone_values_are_neutral():
    assert style_tokens({"tone": "   "}) == {}
    assert style_tokens({"tone": None}) == {}
    assert style_tokens({"tone": 42}) == {}


def test_non_dict_state_is_neutral():
    assert style_tokens(None) == {}  # type: ignore[arg-type]


def test_tone_key_priority_first_non_empty_wins():
    state = {"tone": "", "style": "playful", "mood": "dark"}
    assert style_tokens(state) == CURATED_PALETTE["playful"]


def test_documented_tone_keys_are_consulted():
    assert TONE_KEYS == ("tone", "style", "mood")
    assert style_tokens({"style": "somber"}) == CURATED_PALETTE["somber"]
    assert style_tokens({"mood": "HOPEFUL"}) == CURATED_PALETTE["hopeful"]


def test_returned_map_is_a_copy():
    tokens = style_tokens({"tone": "warm"})
    tokens["--accent-hue"] = "0"
    assert CURATED_PALETTE["warm"]["--accent-hue"] == "28"
    assert style_tokens({"tone": "warm"})["--accent-hue"] == "28"


def test_token_style_renders_declarations():
    rendered = token_style(
        {"--accent-hue": "220", "--accent-sat": "40%", "--bg-warmth": "-4%"}
    )
    assert rendered == "--accent-hue: 220; --accent-sat: 40%; --bg-warmth: -4%;"


def test_token_style_empty_is_empty_string():
    assert token_style({}) == ""
    assert token_style(style_tokens({"tone": "unknown"})) == ""


@pytest.mark.parametrize("tone", REQUESTED_TONES)
def test_token_style_round_trips_curated_tone(tone):
    rendered = token_style(style_tokens({"tone": tone}))
    for key, value in CURATED_PALETTE[tone].items():
        assert f"{key}: {value};" in rendered


# -- derived tone rotation (issue #14) ---------------------------------------
#
# The stylesheet is the implementation: every tone token is computed from the
# curated base (--accent-hue / --accent-sat), so the default accent and every
# curated tone rotate through the same fixed geometry. These tests parse the
# declarations and evaluate them against the owner ruling's table
# (:data:`TONE_GEOMETRY` / :data:`TONE_HUE_OFFSETS`), so a drifted stylesheet
# fails here rather than shipping a second, hand-authored palette.

APP_CSS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "app.css"

#: ``:root`` is the dark preset; the other blocks carry tone geometry only.
PRESET_SELECTORS = {
    "dark": ":root",
    "oled": ":root[data-theme='oled']",
    "light": ":root[data-theme='light']",
    "savilen": ":root[data-theme='savilen']",
}

TONE_NAMES = tuple(TONE_HUE_OFFSETS)

#: hsl(<hue expr>, <sat expr>, <light>%, <alpha>?) — the exact shapes app.css uses.
_TONE_RE = re.compile(
    r"^hsl\(\s*"
    r"(?P<hue>var\(--accent-hue\)|calc\(var\(--accent-hue\)\s*(?P<sign>[+-])\s*(?P<offset>[\d.]+)\))\s*,\s*"
    r"(?P<sat>min\(var\(--accent-sat\),\s*(?P<cap>[\d.]+)%\)|"
    r"calc\(min\(var\(--accent-sat\),\s*(?P<cap_calc>[\d.]+)%\)\s*\*\s*(?P<factor>[\d.]+)\))\s*,\s*"
    r"(?P<light>[\d.]+)%\s*(?:,\s*(?P<alpha>[\d.]+)\s*)?\)$"
)


def _read_block(selector: str) -> dict[str, str]:
    """Return the declarations of one top-level rule in app.css (comments stripped)."""
    text = APP_CSS.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    match = re.search(
        rf"(?<![\w-]){re.escape(selector)}\s*\{{(?P<body>.*?)\n\}}", text, re.DOTALL
    )
    assert match is not None, f"app.css must keep a {selector} block"
    declarations: dict[str, str] = {}
    for raw in match.group("body").splitlines():
        line = raw.strip().split("/*", 1)[0].strip()
        if not line.startswith("--") or not line.endswith(";"):
            continue
        name, _, value = line[:-1].partition(":")
        declarations[name.strip()] = value.strip()
    return declarations


def _evaluate_tone(value: str, hue: float, sat: float) -> tuple[float, float, float, float | None]:
    """Resolve one parsed tone declaration to ``(hue, saturation%, lightness%, alpha)``."""
    match = _TONE_RE.match(value)
    assert match, f"tone declaration is not a derived hsl() expression: {value!r}"
    if match.group("offset") is None:
        resolved_hue = hue
    else:
        offset = float(match.group("offset"))
        resolved_hue = hue + (offset if match.group("sign") == "+" else -offset)
    if match.group("cap") is not None:
        resolved_sat = min(sat, float(match.group("cap")))
    else:
        resolved_sat = min(sat, float(match.group("cap_calc"))) * float(match.group("factor"))
    alpha = float(match.group("alpha")) if match.group("alpha") is not None else None
    return resolved_hue, resolved_sat, float(match.group("light")), alpha


def _tones(selector: str) -> dict[str, str]:
    block = _read_block(selector)
    return {name: block[name] for name in block if name.startswith("--tone-")}


def _variant(name: str) -> str:
    """Return ``plain`` / ``soft`` / ``line`` for a --tone-N[-variant] key."""
    return name.rsplit("-", 1)[-1] if name.rsplit("-", 1)[-1] in ("soft", "line") else "plain"


def _base_name(name: str) -> str:
    """Strip a soft/line suffix: ``--tone-2-soft`` -> ``--tone-2``."""
    variant = _variant(name)
    return name[: -(len(variant) + 1)] if variant != "plain" else name


def _expected(preset: str, name: str, hue: float, sat: float) -> tuple[float, float, float, float | None]:
    """Expected ``(hue, sat, light, alpha)`` from the owner geometry table."""
    base = _base_name(name)
    geometry = TONE_GEOMETRY[preset]
    tone_sat = min(sat, geometry["sat_cap"])
    if base == "--tone-4":
        tone_sat *= geometry["complement_sat_factor"]
        light = geometry["complement_light"]
    else:
        light = geometry["light"]
    variant = _variant(name)
    alpha = {"plain": None, "soft": TONE_SOFT_ALPHA, "line": TONE_LINE_ALPHA}[variant]
    return hue + TONE_HUE_OFFSETS[base], tone_sat, light, alpha


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


def _colour(name: str, preset: str, hue: float, sat: float) -> tuple[int, int, int]:
    value = _read_block(PRESET_SELECTORS[preset])[name]
    resolved = _evaluate_tone(value, hue, sat)
    return _hsl_to_rgb(resolved[0], resolved[1], resolved[2])


def test_owner_geometry_table_is_the_ruled_one():
    """The fixed numbers from the issue thread — degree offsets, caps, alphas."""
    assert TONE_HUE_OFFSETS == {
        "--tone-1": 0,
        "--tone-2": 30,
        "--tone-3": -30,
        "--tone-4": 180,
    }
    assert TONE_SOFT_ALPHA == 0.16
    assert TONE_LINE_ALPHA == 0.45
    assert set(TONE_GEOMETRY) == {"dark", "oled", "light", "savilen"}
    assert TONE_GEOMETRY["dark"] == {
        "sat_cap": 68.0,
        "light": 66.0,
        "complement_sat_factor": 0.55,
        "complement_light": 64.0,
    }
    assert TONE_GEOMETRY["oled"] == {
        "sat_cap": 60.0,
        "light": 82.0,
        "complement_sat_factor": 0.55,
        "complement_light": 74.0,
    }
    assert TONE_GEOMETRY["light"] == {
        "sat_cap": 55.0,
        "light": 44.0,
        "complement_sat_factor": 0.60,
        "complement_light": 40.0,
    }
    assert TONE_GEOMETRY["savilen"] == {
        "sat_cap": 68.0,
        "light": 60.0,
        "complement_sat_factor": 0.55,
        "complement_light": 58.0,
    }


@pytest.mark.parametrize(("preset", "hue", "sat"), [
    (preset, hue, sat)
    for preset in ("dark", "oled", "light", "savilen")
    for hue, sat in ((28, 62), (220, 48))
] + [
    ("dark", 320, 68),  # saturation cap binds
    ("dark", 95, 55),
])
def test_stylesheet_implements_the_ruled_geometry(preset, hue, sat):
    """Every tone + soft/line variant matches TONE_GEOMETRY, for warm and cool."""
    tones = _tones(PRESET_SELECTORS[preset])
    expected_keys = {
        key
        for name in TONE_NAMES
        for key in (name, f"{name}-soft", f"{name}-line")
    }
    assert set(tones) == expected_keys, f"{preset}: tone token set drifted"
    for name, value in tones.items():
        actual = _evaluate_tone(value, hue, sat)
        expected = _expected(preset, name, hue, sat)
        assert actual == pytest.approx(expected, abs=1e-6), f"{preset} {name}"


def test_tone_one_stays_the_base_accent_and_tone_four_is_a_true_complement():
    """tone-1 is the reserved emphasis accent; tone-4 is 180°, never the old 150°."""
    for preset in ("dark", "oled", "light", "savilen"):
        plain: dict[str, tuple[float, float, float, float | None]] = {
            name: _evaluate_tone(_read_block(PRESET_SELECTORS[preset])[name], 28, 62)
            for name in TONE_NAMES
        }
        assert plain["--tone-1"][0] == 28
        assert plain["--tone-1"][1] == min(62, TONE_GEOMETRY[preset]["sat_cap"])
        assert plain["--tone-4"][0] - plain["--tone-1"][0] == 180
        assert plain["--tone-2"][0] - plain["--tone-1"][0] == 30
        assert plain["--tone-3"][0] - plain["--tone-1"][0] == -30


def test_worked_example_warm_dark_hexes():
    """The thread's warm/dark example: analogous trio exact, complement ±3 LSB.

    ``tone-4`` is ``hsl(208, 34.1%, 64%)`` = #84a5c3 by the ruled formula; the
    thread printed #81a5c5, a 1–2 channel rounding difference from its probe.
    The contrast ratios (next test) are the binding, reproducible evidence.
    """
    expected = {
        "--tone-1": "#dea573",
        "--tone-2": "#deda73",
        "--tone-3": "#de7376",
    }
    for name, hex_value in expected.items():
        assert _hex(_colour(name, "dark", 28, 62)) == hex_value
    complement = _colour("--tone-4", "dark", 28, 62)
    probe_complement = (0x81, 0xA5, 0xC5)
    assert all(abs(a - b) <= 3 for a, b in zip(complement, probe_complement)), _hex(complement)


def test_worked_example_ratios_reproduce():
    """Warm (H=28) at the ruled geometry against the probe's raised swatches.

    Dark is measured on the probe's neutral raised rung (#1e1e1e); light on its
    white raised rung — the preset surfaces #12 will ship. Tolerance is the
    probe's own rounding (its printed complement differs by 1–3 channel steps).
    """
    dark_raised = (0x1E, 0x1E, 0x1E)
    light_raised = (255, 255, 255)
    cases = [
        ("dark", dark_raised, (7.73, 11.41, 5.41, 6.44)),
        ("light", light_raised, (4.21, 2.44, 6.34, 5.87)),
    ]
    for preset, raised, expected in cases:
        measured = tuple(
            round(contrast(_colour(name, preset, 28, 62), raised), 2)
            for name in TONE_NAMES
        )
        assert measured == pytest.approx(expected, abs=0.05), f"{preset}: {measured}"


@pytest.mark.parametrize("preset", ("dark", "oled", "light", "savilen"))
def test_every_derived_token_moves_coherently_for_warm_and_a_cool_tone(preset):
    """Warm (28/62) and tense (220/48) rotate together — fill, line and all."""
    warm = {name: _colour(name, preset, 28, 62) for name in TONE_NAMES}
    cool = {name: _colour(name, preset, 220, 48) for name in TONE_NAMES}
    for name in TONE_NAMES:
        assert warm[name] != cool[name], f"{preset} {name} did not follow the tone"
    # The analogous trio keeps a 30° spacing in both directions and the
    # complement is exactly opposite, so a rotated base reaches new territory
    # instead of collapsing into the old (60°-apart) pair.
    block = _read_block(PRESET_SELECTORS[preset])
    warm_hues = {name: _evaluate_tone(block[name], 28, 62)[0] for name in TONE_NAMES}
    cool_hues = {name: _evaluate_tone(block[name], 220, 48)[0] for name in TONE_NAMES}
    assert warm_hues["--tone-4"] - warm_hues["--tone-1"] == 180
    assert cool_hues["--tone-4"] - cool_hues["--tone-1"] == 180
    assert warm_hues["--tone-2"] - warm_hues["--tone-1"] == 30
    assert cool_hues["--tone-3"] - cool_hues["--tone-1"] == -30


@pytest.mark.parametrize("tone", REQUESTED_TONES)
def test_every_curated_tone_rotates_with_its_own_base(tone):
    """The phrase resolver's key feeds the rotation: base hue/sat come from the palette."""
    hue = float(CURATED_PALETTE[tone]["--accent-hue"])
    sat = float(CURATED_PALETTE[tone]["--accent-sat"].rstrip("%"))
    for preset in ("dark", "oled", "light", "savilen"):
        for name in TONE_NAMES:
            value = _read_block(PRESET_SELECTORS[preset])[name]
            actual = _evaluate_tone(value, hue, sat)
            expected = _expected(preset, name, hue, sat)
            assert actual == pytest.approx(expected, abs=1e-6), f"{tone} {preset} {name}"


def test_unknown_tone_still_inherits_the_derived_default_accent():
    """NEUTRAL returns no inline overrides; the stylesheet rotates the default accent."""
    assert style_tokens({"tone": "apocalyptic neon"}) == {}
    assert style_tokens({}) == {}
    # The default accent (262 / 72%) still has a complete, coherent tone set —
    # saturation is clamped by the preset, not left at the raw accent value.
    dark = _read_block(PRESET_SELECTORS["dark"])
    resolved = {name: _evaluate_tone(dark[name], 262, 72) for name in TONE_NAMES}
    assert resolved["--tone-1"][1] == 68.0
    assert resolved["--tone-4"][1] == pytest.approx(68.0 * 0.55)
    assert all(value[3] is None for value in resolved.values())


def test_fixed_consumers_stay_fixed():
    """chrome.css .ui-alt still alternates tone-2/tone-3; tone-1 stays reserved."""
    chrome = (APP_CSS.parent / "lib" / "openui" / "chrome.css").read_text(encoding="utf-8")
    assert "--tone: var(--tone-2);" in chrome
    assert "--tone: var(--tone-3);" in chrome
    assert "--tone-1:" in chrome or "var(--tone-1)" in chrome
