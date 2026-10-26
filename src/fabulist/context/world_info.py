"""SillyTavern World Info v1 engine (PLAN §2).

PLAN §2 lists "Full ST World Info semantics" as out of scope and pinned the
v1 contract as **"v1 = keywords + budget + recursion-lite. Sticky/cooldown/
vector later."** The owner re-ruled the budget on 2026-10-26 (issue #83): the
engine never trims user-provided lore. Whatever activation calls for is what
renders. This module implements that slice as three pure, side-effect-free
functions (no I/O, no event-log writes):

* :func:`parse_book` — normalize a character card's ``character_book`` (or a
  standalone SillyTavern world-info JSON file) into a list of
  :class:`WorldInfoEntry`. Disabled entries are dropped; optional fields are
  defaulted. Both the ``{"entries": [...]}`` character-book shape and the raw
  ST ``{"0": {...}, "1": {...}}`` numeric-id mapping (and a bare list) are
  accepted.
* :func:`activate` — select the entries whose keys appear in a scan text, plus
  the always-on ``constant`` entries, plus one *recursion-lite* pass over the
  content of the first-pass hits (depth capped at 1). No stickiness, no
  cooldown, no vectors — those are explicitly deferred by PLAN §2.
* :func:`render_block` — render EVERY activated entry in insertion order, with
  no size filter and no truncation.

The parser is permissive about missing optional fields (real cards carry them)
and strict about structure (garbage raises :class:`WorldInfoError`). Wiring the
block into the prompt compiler is a later wave; this module owns only the
data-shaping math.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "BLOCK_HEADER",
    "DEFAULT_RECURSION_DEPTH",
    "WorldInfoEntry",
    "WorldInfoError",
    "activate",
    "parse_book",
    "render_block",
]

#: Header line that opens a rendered world-info block.
BLOCK_HEADER = "### World info"

#: v1 activates at most one recursion pass (PLAN §2 "recursion-lite").
DEFAULT_RECURSION_DEPTH = 1


class WorldInfoError(Exception):
    """A world-info book or entry could not be parsed."""


@dataclass(frozen=True)
class WorldInfoEntry:
    """One normalized world-info entry.

    ``order`` is the source ``insertion_order`` (default ``0``). ``keys`` is
    never ``None``; empty keys are dropped at parse time because an empty
    substring would match every scan text. ``selective``/sticky/cooldown fields
    are intentionally not modeled at v1 (PLAN §2).
    """

    keys: list[str]
    content: str
    constant: bool = False
    order: int = 0
    case_sensitive: bool = False
    comment: str = ""


# --- normalization helpers ---------------------------------------------------------


def _as_bool(value: Any, *, default: bool) -> bool:
    """Coerce a JSON-ish value to bool, tolerating strings like ``"false"``."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _as_int(value: Any, *, default: int = 0) -> int:
    """Coerce a JSON-ish value to int, falling back to ``default``."""

    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_keys(raw: Any) -> list[str]:
    """Normalize the ``keys`` field to a list of non-empty strings."""

    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise WorldInfoError(
            "world-info entry 'keys' must be a list of strings, "
            f"got {type(raw).__name__}"
        )
    keys: list[str] = []
    for item in raw:
        if isinstance(item, str):
            key = item
        elif isinstance(item, (int, float)):
            key = str(item)
        else:
            raise WorldInfoError(
                f"world-info key must be a string, got {type(item).__name__}"
            )
        if key:  # an empty key is a substring of everything -> drop it
            keys.append(key)
    return keys


def _normalize_content(raw: Any) -> str:
    """Normalize the ``content`` field; missing content becomes ``""``."""

    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    raise WorldInfoError(
        f"world-info entry 'content' must be a string, got {type(raw).__name__}"
    )


def _normalize_comment(raw: Any) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else str(raw)


def _entry_enabled(raw: dict) -> bool:
    """Whether an entry should be kept (``enabled`` default True)."""

    if "enabled" in raw:
        return _as_bool(raw.get("enabled"), default=True)
    if "disable" in raw:  # legacy spelling, inverted
        return not _as_bool(raw.get("disable"), default=False)
    if "disabled" in raw:
        return not _as_bool(raw.get("disabled"), default=False)
    return True


def _build_entry(raw: dict) -> WorldInfoEntry:
    """Build one :class:`WorldInfoEntry` from a raw entry mapping."""

    # ``keys`` is the card-v2 / modern ST field; ``key`` is the older spelling.
    keys = _normalize_keys(raw.get("keys", raw.get("key")))
    return WorldInfoEntry(
        keys=keys,
        content=_normalize_content(raw.get("content")),
        constant=_as_bool(raw.get("constant"), default=False),
        order=_as_int(raw.get("insertion_order"), default=0),
        case_sensitive=_as_bool(raw.get("case_sensitive"), default=False),
        comment=_normalize_comment(raw.get("comment")),
    )


def _as_entry_list(items: Sequence[Any]) -> list[dict]:
    """Validate that every item is an entry object, else raise."""

    out: list[dict] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise WorldInfoError(
                f"world-info entry #{idx} must be an object, "
                f"got {type(item).__name__}"
            )
        out.append(item)
    return out


def _entry_objects(data: Any) -> list[dict]:
    """Locate the entry mappings inside any accepted book shape."""

    if isinstance(data, list):
        return _as_entry_list(data)
    if isinstance(data, dict):
        if "entries" in data:
            inner = data["entries"]
            if isinstance(inner, list):
                return _as_entry_list(inner)
            if isinstance(inner, dict):
                # character_book.entries may itself be an id->entry mapping.
                return _as_entry_list(list(inner.values()))
            raise WorldInfoError(
                "world-info 'entries' must be a list or mapping, "
                f"got {type(inner).__name__}"
            )
        # Raw ST world-info file: a mapping of numeric id -> entry object.
        return _as_entry_list(list(data.values()))
    raise WorldInfoError(
        f"world-info book must be a dict or list, got {type(data).__name__}"
    )


# --- public API --------------------------------------------------------------------


def parse_book(data: Any) -> list[WorldInfoEntry]:
    """Parse a character book / ST world-info payload into entries.

    Accepts a card ``character_book`` dict (``{"entries": [...]}``), a raw ST
    world-info file (a list, or a numeric-id mapping of entries), or a mapping
    whose ``entries`` value is itself an id->entry mapping. Disabled entries are
    skipped. Raises :class:`WorldInfoError` on structurally invalid input.
    """

    entries: list[WorldInfoEntry] = []
    for raw in _entry_objects(data):
        if not _entry_enabled(raw):
            continue
        entries.append(_build_entry(raw))
    return entries


def _matches(entry: WorldInfoEntry, text: str) -> bool:
    """Whether ``entry`` fires against ``text`` (constants always fire)."""

    if entry.constant:
        return True
    if not entry.keys:
        return False
    if entry.case_sensitive:
        return any(key in text for key in entry.keys)
    haystack = text.lower()
    return any(key.lower() in haystack for key in entry.keys)


def activate(
    entries: Sequence[WorldInfoEntry],
    scan_text: str,
    *,
    recursion_depth: int = DEFAULT_RECURSION_DEPTH,
) -> list[WorldInfoEntry]:
    """Select the entries that should be injected for ``scan_text``.

    v1 semantics (PLAN §2): ``constant`` entries are always active; a normal
    entry is active when any of its keys is a substring of ``scan_text``
    (case-insensitive unless the entry sets ``case_sensitive``); ``selective``
    is ignored. When ``recursion_depth >= 1`` the *content* of the first-pass
    hits is appended to the scan text for exactly one more pass, so entries
    referenced only by other entries' content also fire. Depth is capped at 1
    ("recursion-lite"): content pulled in by that second pass is not rescanned.

    The result is sorted by ``insertion_order`` and is stable for ties.
    """

    materialized = list(entries)
    active: set[int] = set()

    for index, entry in enumerate(materialized):
        if _matches(entry, scan_text):
            active.add(index)

    if recursion_depth >= 1 and active:
        joined = "\n".join(materialized[i].content for i in sorted(active))
        augmented = f"{scan_text}\n{joined}" if joined else scan_text
        for index, entry in enumerate(materialized):
            if index not in active and _matches(entry, augmented):
                active.add(index)

    picked = [entry for index, entry in enumerate(materialized) if index in active]
    return sorted(picked, key=lambda entry: entry.order)


def render_block(entries: Sequence[WorldInfoEntry]) -> str | None:
    """Render EVERY active entry as a ``### World info`` block.

    Entries are emitted in ``insertion_order`` (stable) as ``- {content}``
    lines under a :data:`BLOCK_HEADER`. There is no character budget and no
    size filter: the owner ruling of 2026-10-26 (issue #83) makes user-provided
    lore uncapped, so whatever activation calls for is what renders. An entry
    is never dropped or truncated for size. Returns ``None`` only when nothing
    was activated — nothing was called for, so nothing renders.
    """

    ordered = sorted(entries, key=lambda entry: entry.order)
    if not ordered:
        return None
    lines = [f"- {entry.content}" for entry in ordered]
    return BLOCK_HEADER + "\n" + "\n".join(lines)
