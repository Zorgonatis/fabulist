"""The custom-CSS guard for the appearance override (issue #12, ruling D-D).

A reader may paste ONE stylesheet into the appearance surface. It is applied as a
single ``<style>`` element appended LAST in ``<head>``, so the cascade is::

    base (app.css :root) -> preset (:root[data-theme=…]) -> user CSS -> session tone

The last step is not a stylesheet at all: the tone tokens are **inline custom
properties** on ``<html>`` (set by ``chat/+page.svelte`` from
``GET /api/sessions/{id}/theme``), and an inline declaration outranks every
stylesheet rule that is not ``!important`` — i.e. the fiction's palette beats the
reader's furniture (owner ruling D-D). A reader who wants to pin a hue writes
``!important``, and the drawer says exactly that in one sentence. This module
therefore PRESERVES ``!important`` rather than stripping it: it is the escape
hatch the ruling names, not a hazard.

What this guard is, and is not
-----------------------------
It is **not a security boundary**. CSS cannot execute in this app (the frontend
is a SvelteKit static SPA; no ``expression()``/``javascript:`` survives a modern
engine in a property value, and pasted CSS is the user's own furniture on their
own machine). The issue's own line — "sanitise nothing that cannot execute" —
is why nothing here filters values for "dangerous" text.

What it IS: a **mechanical guarantee of the layout contract**. The owner ruled
which parts of the interface a pasted stylesheet may retune and which parts are
not the reader's to move, and a promise kept only by documentation drifts. So the
guard is an **allowlist of properties**, checked on write:

ENFORCED (mechanically dropped, and reported back to the drawer)
    * layout geometry — ``position``, ``inset``/``top``/``right``/``bottom``/
      ``left``, ``display``, ``float``, ``clear``, ``flex*``, ``grid*``,
      ``gap``, ``order``, ``place-*``, ``align-*``, ``justify-*``, ``columns``;
    * every box dimension — ``width``/``height``/``min-*``/``max-*``/
      ``inline-size``/``block-size``/``aspect-ratio`` and the logical twins. The
      **reading measure** is a ``max-width`` on the prose container, so blocking
      the dimension family is what keeps the measure;
    * ``padding*``/``margin*`` (they move the measure and the drawer chrome);
    * scroll containers — ``overflow*``, ``overscroll-*``, ``scroll-*``,
      ``scrollbar-*``, ``scroll-behavior``;
    * the z-lanes — the ``z-index`` property AND any ``--z-*`` custom property
      (so a user cannot re-lane a drawer by redefining a token either);
    * the typography scale tokens — any ``--font-*`` custom property (a user who
      wanted 9px type could otherwise redefine ``--font-ui``); concrete
      ``font-size`` is allowed but floored at :data:`MIN_FONT_PX` (14px, the
      shell floor), and a value whose size cannot be bounded (``em``/``%``/
      keyword sizes) is dropped rather than guessed at;
    * focus visibility — the whole ``outline`` family, ``:focus`` suppression
      props (``visibility``/``display`` are already blocked as layout) — a
      pasted rule can never make the keyboard's focus ring disappear;
    * ``pointer-events``, ``cursor``, ``transform``/``translate``/``scale``/
      ``rotate`` (a transform moves a drawer without touching ``position``),
      ``content``, ``all`` (the unset-everything escape), ``filter``,
      ``backdrop-filter``;
    * at-rules (``@import``, ``@media``, ``@font-face``, …) — dropped whole: the
      precedence contract is one flat block, and ``@import`` would also let a
      stylesheet load from a remote origin.

ALLOWED (the ruling's "MAY")
    * colours — ``color``, ``background``(-color/-image/…), ``border*`` colours,
      ``caret-color``, ``accent-color``;
    * radii — ``border-radius`` and its corners;
    * typography — ``font-family``, ``font-weight``, ``font-style``,
      ``font-variant*``, ``font-feature-settings``, ``font-size`` (>= 14px),
      ``line-height``, ``letter-spacing``, ``word-spacing``, ``text-align``,
      ``text-transform``, ``text-decoration*``, ``text-indent``;
    * decoration — ``box-shadow``, ``text-shadow``, ``border-style``/
      ``-width``, ``opacity`` (advisory: an opacity of 0 hides content, which is
      the user's own foot, not a contract break);
    * custom properties — everything except the reserved prefixes above, so a
      user may author their own tokens (e.g. ``--my-page-tint``) and a future
      wallpaper token (issue #13) is reachable without touching this list.

ADVISORY (documented, deliberately NOT enforced)
    * ``!important`` is kept (it is the ruling's pin-a-hue escape hatch), so a
      user CAN outrank the session tone on a specific property. That is the
      documented, intended leak in the precedence contract.
    * an ``opacity: 0`` or a ``color`` equal to the background can hide the
      message to yourself; the guard is about the layout contract, not about
      protecting a reader from their own paste box.
    * selector breadth: a pasted ``*`` rule is allowed (it is the classic way to
      restyle), so a global colour sweep is possible by design.

Anything dropped is returned in :attr:`GuardedCss.dropped` with a reason, the
router surfaces it in the ``PATCH`` response, and the drawer lists it — a silent
drop is how a user concludes the feature is broken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "ALLOWED_PROPERTIES",
    "BLOCKED_PROPERTY_FAMILIES",
    "DroppedDeclaration",
    "GuardedCss",
    "MIN_FONT_PX",
    "RESERVED_CUSTOM_PROPERTY_PREFIXES",
    "guard",
]

#: The shell's readable-type floor (app.css ``--font-ui``). A pasted rule may
#: raise sizes freely; it may not go below this.
MIN_FONT_PX = 14.0

#: Properties a pasted stylesheet may set. Everything not listed here — and not a
#: custom property — is dropped.
ALLOWED_PROPERTIES: frozenset[str] = frozenset(
    {
        "accent-color",
        "background",
        "background-attachment",
        "background-blend-mode",
        "background-clip",
        "background-color",
        "background-image",
        "background-origin",
        "background-position",
        "background-repeat",
        "background-size",
        "border",
        "border-block",
        "border-block-color",
        "border-bottom",
        "border-bottom-color",
        "border-bottom-left-radius",
        "border-bottom-right-radius",
        "border-bottom-style",
        "border-bottom-width",
        "border-color",
        "border-inline",
        "border-inline-color",
        "border-left",
        "border-left-color",
        "border-left-style",
        "border-left-width",
        "border-radius",
        "border-right",
        "border-right-color",
        "border-right-style",
        "border-right-width",
        "border-style",
        "border-top",
        "border-top-color",
        "border-top-left-radius",
        "border-top-right-radius",
        "border-top-style",
        "border-top-width",
        "border-width",
        "box-shadow",
        "caret-color",
        "color",
        "font-family",
        "font-feature-settings",
        "font-kerning",
        "font-optical-sizing",
        "font-size",
        "font-style",
        "font-variant",
        "font-variant-caps",
        "font-variant-numeric",
        "font-weight",
        "hyphens",
        "letter-spacing",
        "line-height",
        "opacity",
        "overflow-wrap",
        "text-align",
        "text-decoration",
        "text-decoration-color",
        "text-decoration-line",
        "text-decoration-style",
        "text-decoration-thickness",
        "text-indent",
        "text-rendering",
        "text-shadow",
        "text-transform",
        "text-underline-offset",
        "white-space",
        "word-break",
        "word-spacing",
        "word-wrap",
    }
)

#: The must-not families, described for the drawer + the docs. The guard blocks by
#: allowlist (so this table is documentation, not the filter); it is asserted
#: disjoint from :data:`ALLOWED_PROPERTIES` by the tests.
BLOCKED_PROPERTY_FAMILIES: dict[str, str] = {
    "layout geometry": (
        "position, inset/top/right/bottom/left, display, float, clear, flex*, "
        "grid*, gap*, order, place-*, align-*, justify-*, columns"
    ),
    "box dimensions": (
        "width, height, min-*/max-*, inline-size, block-size, aspect-ratio — "
        "this is what holds the reading measure (a max-width on the prose column)"
    ),
    "spacing": "padding*, margin*",
    "scroll containers": "overflow*, overscroll-*, scroll-*, scrollbar-*",
    "z-lanes": "z-index and any --z-* custom property",
    "type scale": "any --font-* custom property (font-size itself is allowed >= 14px)",
    "focus rings": "the whole outline family",
    "interaction": "pointer-events, cursor, transform, translate, scale, rotate",
    "escapes": "content, all, filter, backdrop-filter, and every at-rule (@import, @media, …)",
}

#: Custom-property prefixes a pasted stylesheet may NOT declare: the z-lane
#: tokens, the typography scale, and (reserved for the reading measure's future
#: tokenisation) the reading measure.
RESERVED_CUSTOM_PROPERTY_PREFIXES: tuple[str, ...] = ("--z-", "--font-", "--reading-")


@dataclass(frozen=True)
class DroppedDeclaration:
    """One declaration the guard refused, with the reason the drawer shows."""

    selector: str
    declaration: str
    reason: str


@dataclass(frozen=True)
class GuardedCss:
    """The accepted stylesheet plus everything that was dropped and why."""

    css: str = ""
    dropped: list[DroppedDeclaration] = field(default_factory=list)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_AT_RULE_BLOCK_RE = re.compile(r"@[^{};]*\{(?:[^{}]|\{[^{}]*\})*\}", re.DOTALL)
_AT_RULE_STMT_RE = re.compile(r"@[^{};]*;")
_RULE_RE = re.compile(r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}")
_FONT_SIZE_RE = re.compile(r"^\s*(?P<value>[\d.]+)\s*(?P<unit>px|rem|pt)\s*$", re.IGNORECASE)


def _font_size_px(value: str) -> float | None:
    """Pixel size of a concrete ``font-size`` value, or ``None`` when unbounded.

    ``px`` is itself, ``rem`` is 16px (the browser default this app never
    changes), ``pt`` is 4/3 px. Anything relative (``em``/``%``/viewport), a
    keyword size, or a ``calc()`` is unbounded without the live DOM, so it is
    refused rather than guessed at.
    """
    bare = value.replace("!important", "").strip()
    match = _FONT_SIZE_RE.match(bare)
    if match is None:
        return None
    size = float(match.group("value"))
    unit = match.group("unit").lower()
    if unit == "rem":
        return size * 16.0
    if unit == "pt":
        return size * (4.0 / 3.0)
    return size


def _check_custom_property(name: str) -> str | None:
    """Return a refusal reason for a reserved custom property, else ``None``."""
    lowered = name.lower()
    for prefix in RESERVED_CUSTOM_PROPERTY_PREFIXES:
        if lowered.startswith(prefix):
            return f"{prefix}* tokens are reserved (layout/z-order/type scale)"
    if lowered in ("--reading-measure", "--measure"):
        return "the reading measure is not a reader-tunable token"
    return None


def _check_declaration(raw: str, selector: str) -> tuple[str | None, DroppedDeclaration | None]:
    """Validate one ``prop: value`` declaration.

    Returns ``(kept_declaration_or_None, dropped_or_None)``.
    """
    text = raw.strip()
    if not text:
        return None, None
    name, sep, value = text.partition(":")
    name = name.strip().lower()
    value = value.strip()
    if not sep or not name or not value:
        return None, DroppedDeclaration(selector, text, "not a property: value pair")

    if name.startswith("--"):
        refusal = _check_custom_property(name)
        if refusal is not None:
            return None, DroppedDeclaration(selector, text, refusal)
        return f"{name}: {value};", None

    if name not in ALLOWED_PROPERTIES:
        return None, DroppedDeclaration(
            selector,
            text,
            "not in the user-CSS allowlist (layout, z-lanes, scroll containers, "
            "box dimensions, the reading measure and focus rings are not tunable)",
        )

    if name == "font-size":
        size = _font_size_px(value)
        if size is None:
            return None, DroppedDeclaration(
                selector,
                text,
                "relative or keyword font sizes cannot be checked against the "
                f"{MIN_FONT_PX:g}px floor — use px or rem",
            )
        if size < MIN_FONT_PX:
            return None, DroppedDeclaration(
                selector,
                text,
                f"font-size is below the {MIN_FONT_PX:g}px readable floor",
            )

    return f"{name}: {value};", None


def guard(text: str) -> GuardedCss:
    """Return the paste-box stylesheet reduced to what the layout contract allows.

    Comments are stripped, at-rules are dropped whole, every rule is filtered
    declaration by declaration (see the module docstring for the allowlist), and
    the accepted CSS is re-rendered from the parsed pieces — so the stylesheet the
    browser receives is always something this module built, never the raw paste.

    An empty or all-dropped paste returns an empty stylesheet: the appearance
    then renders exactly like the preset, which is the honest result.
    """
    if not text or not text.strip():
        return GuardedCss()

    cleaned = _COMMENT_RE.sub("", text)
    dropped: list[DroppedDeclaration] = []

    def drop_at(match: re.Match[str]) -> str:
        snippet = " ".join(match.group(0).split())
        kind = snippet.split()[0] if snippet.split() else "@"
        dropped.append(
            DroppedDeclaration(
                snippet[:60],
                snippet[:80],
                f"at-rules are not allowed ({kind}); the override is one flat block",
            )
        )
        return " "

    cleaned = _AT_RULE_BLOCK_RE.sub(drop_at, cleaned)
    cleaned = _AT_RULE_STMT_RE.sub(drop_at, cleaned)

    blocks: list[str] = []
    for match in _RULE_RE.finditer(cleaned):
        # Collapse the selector's whitespace, then normalise around commas so a
        # re-rendered stylesheet is stable (what a reader sees is what ships).
        selector = re.sub(r"\s*,\s*", ", ", " ".join(match.group("selector").split()))
        if not selector or "@" in selector:
            continue
        kept: list[str] = []
        for raw in match.group("body").split(";"):
            declaration, refusal = _check_declaration(raw, selector)
            if refusal is not None:
                dropped.append(refusal)
            if declaration is not None:
                kept.append(declaration)
        if kept:
            body = "\n".join(f"\t{line}" for line in kept)
            blocks.append(f"{selector} {{\n{body}\n}}")

    # Anything left outside a parsed rule is unparseable tail (a stray `{`/`}`,
    # a bare declaration): refuse it loudly rather than emitting broken CSS.
    remainder = _RULE_RE.sub("", cleaned)
    tail = " ".join(remainder.split())
    if tail:
        dropped.append(
            DroppedDeclaration(tail[:60], tail[:80], "not inside a selector { … } block")
        )

    return GuardedCss(css="\n".join(blocks), dropped=dropped)
