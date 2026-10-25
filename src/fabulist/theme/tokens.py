"""Deterministic theme tokens derived from Style State (R4 / W2.4).

The extractor's state projection (``EventLog.get_projection(sid)["state"]``)
carries narrative mood keys — primarily ``tone`` (e.g. ``"tense"``, ``"warm"``,
``"playful"``) and optionally ``weather`` / ``time`` — as *freeform* strings. R4
asks the UI theme to follow the fiction automatically, so this module maps the
extractor's tone vocabulary onto CSS custom-property overrides that are injected
verbatim into a ``style`` attribute.

Design contract — **curated or neutral, never invented**:

* :data:`CURATED_PALETTE` is plain data: a mapping from known tone/style values to
  a fixed set of token overrides. Extend it freely by adding entries; nothing here
  hashes or derives values.
* A freeform value resolves **deterministically**, in this documented order:

  1. **Exact key** — the trimmed, lower-cased value equals a palette key
     (``"Warm"``, ``"  warm  "``).
  2. **Keyword** — otherwise the palette key whose *whole-word* occurrence starts
     earliest in the value wins; ties go to the longest key, then the key name.
     A word boundary is any character outside letters/digits/underscore/hyphen, so
     ``"warm, cluttered, and suddenly watchful"`` resolves to ``warm`` while a
     derived compound such as ``"tense-ish"`` stays unmatched.

  The rule is content-driven (position in the phrase), not palette-order-driven, so
  adding a palette entry cannot silently re-rank an existing phrase.
* An unknown or missing tone falls back to :data:`NEUTRAL` (an empty mapping), which
  means "inherit the base ``app.css`` palette". A hallucinated ``"tense-ish"`` or
  ``"apocalyptic neon"`` yields neutral, not a surprise colour.
* There is no randomness, no animation, and no time-of-day interpolation, so a given
  state always renders the same theme (deterministic theming).

The token set is intentionally small and documented per palette entry:

``--accent-hue``
    Hue in degrees (unitless string), consumed by ``hsl()`` / ``oklch()`` in CSS.
``--accent-sat``
    Accent saturation as a percentage string (``"48%"``).
``--bg-warmth``
    Signed background-warmth shift as a percentage string (``"+6%"`` / ``"-4%"``).
    Consumed by the page (``--bg``) and by every rung of the surface ladder in
    ``app.css`` (panel / raised / inset / hover / active), so a warm tone warms
    the whole UI while the contrast steps between the layers hold.

Derived tone rotation (issue #14)
---------------------------------

Those three values are the *curated base* — the one thing a tone may author.
Every other tone token is **derived** from them in ``frontend/src/app.css``:
``--tone-1`` is the base accent, ``--tone-2`` / ``--tone-3`` sit at ±30°, and
``--tone-4`` is the TRUE complement at +180° (replacing the old ±150° pair that
left no complement), each with ``-soft`` / ``-line`` variants at the preset's
alphas. The rotation is CSS so the base accent itself (neutral tones, the
default purple) rotates too; :data:`TONE_GEOMETRY` pins the fixed geometry here
so the tests can check the stylesheet against the owner ruling. A freeform tone
still resolves only through :data:`CURATED_PALETTE`; unknown values return
:data:`NEUTRAL` and inherit the default accent, which the same CSS geometry
rotates — no text ever invents a colour.

Example::

    >>> style_tokens({"tone": "Tense"})
    {'--accent-hue': '220', '--accent-sat': '48%', '--bg-warmth': '-4%'}
    >>> style_tokens({"tone": "warm, cluttered, and suddenly watchful"})
    {'--accent-hue': '28', '--accent-sat': '62%', '--bg-warmth': '+6%'}
    >>> token_style(style_tokens({"tone": "tense"}))
    '--accent-hue: 220; --accent-sat: 48%; --bg-warmth: -4%;'
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = [
    "CURATED_PALETTE",
    "NEUTRAL",
    "TONE_GEOMETRY",
    "TONE_HUE_OFFSETS",
    "TONE_KEYS",
    "TONE_LINE_ALPHA",
    "TONE_SOFT_ALPHA",
    "style_tokens",
    "token_style",
]

#: Neutral fallback: an empty override map that inherits the base ``app.css`` palette.
NEUTRAL: dict[str, str] = {}

#: State keys consulted for a tone, in priority order (first non-empty wins).
TONE_KEYS: tuple[str, ...] = ("tone", "style", "mood")

#: Fixed rotation geometry per brightness preset (owner ruling, issue #14).
#: ``sat_cap`` clamps the curated base saturation (``Sa = min(base, cap)``);
#: ``light`` is every analogous tone's lightness; the complement keeps
#: ``complement_sat_factor × Sa`` at ``complement_light``. These are the values
#: the stylesheet implements — the Python table is the spec the tests pin so a
#: drifted ``app.css`` cannot silently re-derive a different palette.
TONE_GEOMETRY: dict[str, dict[str, float]] = {
    "dark": {
        "sat_cap": 68.0,
        "light": 66.0,
        "complement_sat_factor": 0.55,
        "complement_light": 64.0,
    },
    "oled": {
        "sat_cap": 60.0,
        "light": 82.0,
        "complement_sat_factor": 0.55,
        "complement_light": 74.0,
    },
    "light": {
        "sat_cap": 55.0,
        "light": 44.0,
        "complement_sat_factor": 0.60,
        "complement_light": 40.0,
    },
    "savilen": {
        "sat_cap": 68.0,
        "light": 60.0,
        "complement_sat_factor": 0.55,
        "complement_light": 58.0,
    },
}

#: Fixed hue rotation (degrees) for each derived tone token. ``--tone-1`` is the
#: reserved emphasis colour; ``--tone-4`` is the true complement.
TONE_HUE_OFFSETS: dict[str, int] = {
    "--tone-1": 0,
    "--tone-2": 30,
    "--tone-3": -30,
    "--tone-4": 180,
}

#: Alpha of the translucent soft fill / line variants (fixed across presets).
TONE_SOFT_ALPHA = 0.16
TONE_LINE_ALPHA = 0.45

#: Curated tone -> CSS custom-property overrides. This is data; extend freely.
CURATED_PALETTE: dict[str, dict[str, str]] = {
    "warm": {
        "--accent-hue": "28",
        "--accent-sat": "62%",
        "--bg-warmth": "+6%",
    },
    "tense": {
        "--accent-hue": "220",
        "--accent-sat": "48%",
        "--bg-warmth": "-4%",
    },
    "dark": {
        "--accent-hue": "265",
        "--accent-sat": "35%",
        "--bg-warmth": "-7%",
    },
    "playful": {
        "--accent-hue": "320",
        "--accent-sat": "68%",
        "--bg-warmth": "+2%",
    },
    "intimate": {
        "--accent-hue": "350",
        "--accent-sat": "45%",
        "--bg-warmth": "+5%",
    },
    "mysterious": {
        "--accent-hue": "275",
        "--accent-sat": "52%",
        "--bg-warmth": "-3%",
    },
    "hopeful": {
        "--accent-hue": "95",
        "--accent-sat": "55%",
        "--bg-warmth": "+4%",
    },
    "somber": {
        "--accent-hue": "210",
        "--accent-sat": "25%",
        "--bg-warmth": "-5%",
    },
}


#: Characters that count as part of a word: a curated key must not be preceded or
#: followed by one of these. Hyphen and underscore are word characters on purpose,
#: so a derived compound (``"tense-ish"``, ``"warm_tone"``) never matches ``tense``
#: / ``warm``; punctuation and whitespace (``"warm,"`` / ``"(warm)"``) are bounds.
_WORD_CHAR = r"[\w-]"


@lru_cache(maxsize=None)
def _key_pattern(key: str) -> re.Pattern[str]:
    """Compiled whole-word matcher for one palette key (cached)."""
    return re.compile(
        rf"(?<!{_WORD_CHAR}){re.escape(key)}(?!{_WORD_CHAR})", re.IGNORECASE
    )


def _keyword_match(value: str) -> str | None:
    """Return the palette key that wins :func:`style_tokens`' keyword pass.

    Precedence, documented in the module docstring: the occurrence that starts
    earliest wins; a tie at the same start goes to the longest key, then to the
    lexicographically smallest key name. Returns ``None`` when no curated key
    occurs as a whole word in ``value``.
    """
    best: tuple[int, int, str] | None = None
    for key in CURATED_PALETTE:
        match = _key_pattern(key.lower()).search(value)
        if match is None:
            continue
        rank = (match.start(), -len(key), key)
        if best is None or rank < best:
            best = rank
    return best[2] if best is not None else None


def style_tokens(state: dict) -> dict[str, str]:
    """Return theme-token overrides for an extractor *state* mapping.

    Reads the first non-empty value among :data:`TONE_KEYS`, resolves it against
    :data:`CURATED_PALETTE` (exact key first, then earliest whole-word keyword —
    see the module docstring for the documented precedence), and returns a copy of
    the matching override map. Unknown, empty, or missing tones return
    :data:`NEUTRAL` (a fresh empty dict) — values are curated or neutral, never
    invented from the freeform string.

    Args:
        state: A state projection mapping (e.g.
            ``log.get_projection(session_id)["state"]``). Non-dict input and
            non-string tone values are treated as neutral.

    Returns:
        A new ``{css_custom_property: value}`` dict safe for the caller to mutate.
    """
    if not isinstance(state, dict):
        return dict(NEUTRAL)

    tone = ""
    for key in TONE_KEYS:
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            tone = value.strip()
            break

    if not tone:
        return dict(NEUTRAL)

    normalized = tone.lower()
    palette = CURATED_PALETTE.get(normalized)
    if palette is None:
        matched = _keyword_match(normalized)
        if matched is None:
            return dict(NEUTRAL)
        palette = CURATED_PALETTE[matched]
    return dict(palette)


def token_style(tokens: dict) -> str:
    """Render token overrides as inline CSS declarations for a ``style`` attribute.

    Each ``key: value`` pair is emitted as ``"{key}: {value};"`` joined by single
    spaces, ready to drop straight into an element's ``style`` attribute (or a
    ``:style`` binding). An empty mapping renders the empty string.

    Args:
        tokens: Mapping of CSS custom properties to values, e.g. the result of
            :func:`style_tokens`.

    Returns:
        A declaration string such as ``"--accent-hue: 220; --accent-sat: 48%;"``,
        or ``""`` when ``tokens`` is empty.
    """
    return " ".join(f"{key}: {value};" for key, value in tokens.items())
