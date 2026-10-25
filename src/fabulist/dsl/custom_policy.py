"""Custom-component policy — the gate-side checks for narrator-authored raw HTML/CSS.

``Custom`` is the narrator's raw-HTML escape hatch (D60): the model ships an
``html`` fragment and a ``css`` string that the browser renders inside a shadow
root. The browser-side sanitizer (DOMPurify + a ``url()`` scrubber,
``frontend/src/lib/openui/customSanitize.ts``) is **defense in depth** — it can
only strip what reaches the DOM. The *first* line is this module: the narrator
gate runs it before a ``Custom`` statement is ever allowed to reach the event
log. A rejected statement never becomes a DOM node, and the gate's failure
ladder (one constrained re-synthesis → a deterministic paper-scrap rewrite)
keeps the turn renderable while the log stays truthful.

Eight violation classes, each answering a concrete threat:

* ``html_cap`` (>4000 chars) — an unbounded ``html`` string is a token-budget
  and retention attack: a Custom payload rides the ``dsl_keep_last`` window, so
  the cap bounds the per-turn size the model can spend.
* ``css_cap`` (>2000 chars) — the same bound for the style channel.
* ``banned_tag`` — ``<script>``/``<iframe>``/``<object>``/``<embed>``/
  ``<link>``/``<meta>``/``<style>``/``<form>``: execution, navigation and
  exfiltration surfaces with no place in a diegetic artifact. ``<style>`` is
  banned *inside html* by design — the ``css`` prop is the style channel (it
  rides as ``textContent`` and is never parsed as markup).
* ``event_handler`` — ``on*=`` inline handlers are script by another name
  (``<img onerror=...>`` needs no ``<script>`` tag to run).
* ``remote_uri`` — any ``http(s)://`` in html **or** css: narrator artifacts
  never fetch the network. Only ``data:`` URIs and app ``/api/`` asset
  endpoints are addressable.
* ``css_at_import`` — ``@import`` pulls a stylesheet from anywhere, including
  the network; the ``css`` prop is the *only* style channel and must be self-
  contained.
* ``remote_css_url`` — a ``url(...)`` target that is neither ``data:`` nor an
  app ``/api/`` endpoint is the same network fetch wearing css clothes.
* ``root_percentage_height`` — a PERCENTAGE ``height`` on the fragment's
  outermost element resolves against the artifact host, which is content-sized
  (``.custom-host { display: block }``, no definite height) → the percentage
  computes to ``auto`` → an artifact whose children are all absolutely
  positioned collapses to its borders and renders as an invisible strip. Live
  case: a session, a 2px panel that looked like an empty element.
  A px/rem height has no such dependency, so only the ROOT is checked — nested
  percentages resolve against their declared ancestor and stay legal.

Every function here is pure and dependency-free (only ``re``) so the gate stays
importable from any layer and this policy is unit-testable without a browser or
a model. Quote/paren balance is deliberately *not* re-implemented here — the
gate's existing whole-segment ``_check_balance`` owns that. Caps are measured on
the decoded string values (what the browser would actually receive), not on the
DSL source.
"""

from __future__ import annotations

import re

__all__ = ["CSS_CAP", "CUSTOM_COMPONENT", "HTML_CAP", "validate_custom"]

#: The escaped component name the narrator gate exempts from its generic
#: HTML-smuggle scan and routes here instead.
CUSTOM_COMPONENT = "Custom"

#: Hard caps (chars) on the two payload channels (D60, gate-enforced).
HTML_CAP = 4000
CSS_CAP = 2000

#: Tags that must never appear inside a Custom ``html`` fragment. ``<\s*``
#: (no optional closing ``/``) matches the opener; a lone ``</script>`` is inert.
_BANNED_TAG_RE = re.compile(
    r"<\s*(?:script|iframe|object|embed|link|meta|style|form)\b", re.IGNORECASE
)
#: Inline event-handler attributes (``onerror=``, ``onclick =``).
_EVENT_HANDLER_RE = re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)
#: Any absolute HTTP(S) URL, in either channel.
_REMOTE_URI_RE = re.compile(r"https?://", re.IGNORECASE)
#: ``@import`` in the css channel.
_CSS_AT_IMPORT_RE = re.compile(r"@import\b", re.IGNORECASE)
#: A ``url(...)`` reference and its (optionally quoted) target.
_CSS_URL_RE = re.compile(
    r"url\(\s*(?P<quote>['\"]?)(?P<target>.*?)(?P=quote)\s*\)",
    re.IGNORECASE | re.DOTALL,
)

#: URL prefixes a Custom artifact may legitimately reference.
_ALLOWED_URL_PREFIXES = ("data:", "/api/")

#: The fragment's FIRST (outermost) start tag and its attribute run.
_ROOT_TAG_RE = re.compile(r"<\s*[a-zA-Z][\w-]*\b([^>]*)>")
#: A ``style="…"`` / ``style='…'`` attribute inside that attribute run.
_STYLE_ATTR_RE = re.compile(
    r"\bstyle\s*=\s*(['\"])(?P<style>.*?)\1", re.IGNORECASE | re.DOTALL
)
#: A PERCENTAGE ``height`` declaration inside the style body.
_PCT_HEIGHT_RE = re.compile(r"\bheight\s*:\s*\d+(?:\.\d+)?\s*%", re.IGNORECASE)


def _root_percentage_height(html: str) -> str | None:
    """The percentage ``height`` on the fragment's OUTERMOST element, if any.

    The Custom host is content-sized, so a percentage height on the fragment's
    root resolves to ``auto``: an artifact built only from absolutely-positioned
    children then collapses to its border box (measured live: 2px). Nested
    percentages are legal — they resolve against a declared ancestor — so only
    the root element's ``style`` is inspected.
    """
    root = _ROOT_TAG_RE.search(html or "")
    if root is None:
        return None
    style = _STYLE_ATTR_RE.search(root.group(1) or "")
    if style is None:
        return None
    match = _PCT_HEIGHT_RE.search(style.group("style") or "")
    return match.group(0) if match else None


def _remote_css_url(css: str) -> str | None:
    """The first disallowed ``url()`` target in ``css``, or ``None`` when clean.

    Empty targets are ignored (nothing is fetched); ``data:`` URIs and app
    ``/api/`` endpoints are the only allowed references.
    """
    for match in _CSS_URL_RE.finditer(css or ""):
        target = (match.group("target") or "").strip()
        if not target:
            continue
        if target.lower().startswith("data:") or target.startswith("/api/"):
            continue
        return target
    return None


def validate_custom(html: str, css: str) -> list[str]:
    """Return the policy violations for a Custom payload (empty when clean).

    Pure and synchronous. Each returned string is ``"<class>: <detail>"`` so the
    class is the first ``:``-delimited token (the gate uses it as the finding's
    ``code``). A clean payload returns ``[]``; an exactly-at-cap payload is
    clean (the check is strictly ``>``).
    """
    html = html or ""
    css = css or ""
    violations: list[str] = []

    if len(html) > HTML_CAP:
        violations.append(f"html_cap: html is {len(html)} chars (max {HTML_CAP})")
    if len(css) > CSS_CAP:
        violations.append(f"css_cap: css is {len(css)} chars (max {CSS_CAP})")

    match = _BANNED_TAG_RE.search(html)
    if match:
        violations.append(f"banned_tag: forbidden tag {match.group(0)!r}")

    match = _EVENT_HANDLER_RE.search(html)
    if match:
        violations.append(f"event_handler: inline event handler {match.group(0)!r}")

    match = _REMOTE_URI_RE.search(html) or _REMOTE_URI_RE.search(css)
    if match:
        violations.append(f"remote_uri: remote URL {match.group(0)!r} is not allowed")

    if _CSS_AT_IMPORT_RE.search(css):
        violations.append("css_at_import: @import is not allowed in the css prop")

    target = _remote_css_url(css)
    if target is not None:
        violations.append(f"remote_css_url: url() target {target!r} is not data: or /api/")

    root_height = _root_percentage_height(html)
    if root_height is not None:
        violations.append(
            f"root_percentage_height: the fragment's root declares '{root_height}' — "
            "the artifact host has no definite height, so a percentage height "
            "collapses the panel (declare an explicit px/rem height, e.g. "
            "height: 220px)"
        )

    return violations
