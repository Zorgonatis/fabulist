"""Owner-editable BTS attribute catalog (W9.29 §4): meters and traits as data.

The epic's requirement 3 — *meters are objects with identity, and their attributes
and ranges come from a STUDIO-EDITABLE catalog* — makes the attribute universe an
ASSET, not a constant: the event log owns everything (D9), and definitions are
plain CRUD with no chat open and no session required (non-negotiable 7). This is
the same lane as :mod:`fabulist.decision_queries` (the decision-question bank),
copied deliberately: same reserved-anchor storage, same replace semantics, same
"code seed is the fallback", same write-validates/read-tolerates split.

Event contract — one ``system`` event on :data:`ENTITY_CATALOG_ANCHOR`::

    {"event": "entity_catalog", "rows": [row, ...]}

Replace semantics, latest wins: the newest row IS the catalog (the earlier rows
stay queryable, D9). An absent anchor / absent payload reads as **empty**, and an
empty catalog means "the built-in code rows" (:data:`DEFAULT_ROWS`), so a fresh
log and every existing log behave exactly as before W9.29.

A catalog row (the plan §4 shape == the stored shape == the API/UI wire shape)::

    {
      "key": str,                    # unique handle; a lowercase slug
      "label": str,                  # what the palette/sidebar shows
      "type": "meter" | "trait",     # a range-checked number vs free text
      "host": "relational" | "self" | "scene",
                                     # relational = needs a subject (a meter/edge
                                     #   toward someone), self = the being's own,
                                     #   scene = the room/scene holds the value
      "range": [int, int],           # meters ONLY, ascending; enforced at the tool
                                     #   door (§6), never stored on a trait
      "description": str,            # one line: what the value means
    }

The seed is :data:`DEFAULT_ROWS` — 15 rows (plan §4, decision point §14.2):
relational meters ``hostility``/``trust``/``affection``/``fear``/``respect``
(0–100), self meters ``stress``/``arousal``/``energy`` (0–100), the scene meter
``tension`` (0–10, today's ``tension: n/10`` line), the self trait ``mood`` and
the scene traits ``weather``/``tone``/``forecast``/``irony``/``genre`` (the values
the flat ``state-delta`` fold and the env_hud ladder consume today, now
catalog-governed and sidebar-visible; ``genre`` is the story's genre register,
homed in the scene branch). Everything else is owner-added in the Studio;
outfit/pose/appearance are deliberately not seeded.

A STORED catalog asset shadows this code seed (:func:`catalog_for` prefers the
newest stored rows over :data:`DEFAULT_ROWS`), so a log whose catalog was already
saved keeps its old row set and does NOT gain a newly seeded row — gaining it is
a Studio edit on that asset (an owner action), which is expected behaviour, not a
bug. The code seed is only the fallback for an absent/empty asset.

Deliberate, boring constraints (the bank's discipline):

* **Seeding does not write.** Like :func:`decision_queries.effective_questions`,
  :func:`catalog_for` returns the code seed when the anchor holds nothing usable
  and appends NOTHING — a read never smuggles a write into the log, and no
  migration/backfill exists (§4: "no migration, no backfill"). The first real
  write is the owner's (Studio CRUD, W4) through :func:`replace_rows`.
* **Reads never raise.** A log without ``get_events`` (the ``LogPort`` test
  seam), a malformed anchor payload, or a single malformed row inside a
  well-formed payload all read as "no such row": bad rows are SKIPPED and the
  rest of the catalog stands, because the tracker is advisory and never fatal.
  A payload whose every row is malformed reads as the code seed (an empty
  catalog means "the seed applies", exactly like an empty bank).
* **Writes validate, reads tolerate.** :func:`validate_rows` /
  :func:`replace_rows` raise ``ValueError`` with a human message (the API turns
  it into a 400); :func:`_normalize_row` is the ONE rule set both sides share,
  so a row the Studio can save is exactly a row the fold can read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


__all__ = [
    "DEFAULT_ROWS",
    "ENTITY_CATALOG_ANCHOR",
    "ENTITY_CATALOG_EVENT",
    "HOST_RELATIONAL",
    "HOST_SCENE",
    "HOST_SELF",
    "METER",
    "ROW_HOSTS",
    "ROW_TYPES",
    "TRAIT",
    "catalog_for",
    "default_rows",
    "meters_only",
    "range_check",
    "replace_rows",
    "row_for",
    "traits_only",
    "validate_rows",
]

#: Reserved session id anchoring the catalog (like ``__personas__`` /
#: ``__worlds__`` / ``__decision_queries__``); the ``__`` prefix keeps it out of
#: the session list.
ENTITY_CATALOG_ANCHOR = "__entity_catalog__"

#: System-event kind carrying the whole catalog (latest wins, replace semantics).
ENTITY_CATALOG_EVENT = "entity_catalog"

#: Actor stamped on human edits. Mirrors the persona/world/bank appends.
USER_ACTOR = "user"

#: A row's ``type``: a range-checked integer, or free text.
METER = "meter"
TRAIT = "trait"
ROW_TYPES = (METER, TRAIT)

#: A row's ``host``: whose value it is. ``relational`` needs a subject (the
#: registry's ``entity_set`` requires ``subject`` for these — plan §3.4/§5),
#: ``self`` is the being's own state, ``scene`` belongs to the room/scene.
HOST_RELATIONAL = "relational"
HOST_SELF = "self"
HOST_SCENE = "scene"
ROW_HOSTS = (HOST_RELATIONAL, HOST_SELF, HOST_SCENE)

#: A key is a handle the model cites in a tool call, so it is a strict slug:
#: lowercase, digits and ``_`` between words, never starting with a digit
#: (``hostility``, ``sexual_tension``). Anything else is refused at the write
#: door — the palette renders keys verbatim.
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# the code seed (plan §4) — read as data, never mutated in place
# ---------------------------------------------------------------------------


def _row(
    key: str,
    label: str,
    row_type: str,
    host: str,
    description: str,
    bounds: tuple[int, int] | None = None,
) -> dict:
    """One seed row in the canonical field order (meters carry ``range``)."""
    row: dict[str, Any] = {
        "key": key,
        "label": label,
        "type": row_type,
        "host": host,
    }
    if bounds is not None:
        row["range"] = [bounds[0], bounds[1]]
    row["description"] = description
    return row


def _seed_rows() -> list[dict]:
    """The built-in catalog, fresh each call (plan §4's 15 rows)."""
    return [
        # meters, relational — how this being stands toward another
        _row(
            "hostility",
            "Hostility",
            METER,
            HOST_RELATIONAL,
            "Open animosity toward the subject.",
            (0, 100),
        ),
        _row(
            "trust",
            "Trust",
            METER,
            HOST_RELATIONAL,
            "Willingness to believe the subject means them no harm.",
            (0, 100),
        ),
        _row(
            "affection",
            "Affection",
            METER,
            HOST_RELATIONAL,
            "Warmth and liking felt toward the subject.",
            (0, 100),
        ),
        _row(
            "fear",
            "Fear",
            METER,
            HOST_RELATIONAL,
            "Dread of the subject; high fear reads as flight or freezing.",
            (0, 100),
        ),
        _row(
            "respect",
            "Respect",
            METER,
            HOST_RELATIONAL,
            "How much they credit the subject's competence and standing.",
            (0, 100),
        ),
        # meters, self — the being's own state
        _row(
            "stress",
            "Stress",
            METER,
            HOST_SELF,
            "Pressure they are carrying right now.",
            (0, 100),
        ),
        _row(
            "arousal",
            "Arousal",
            METER,
            HOST_SELF,
            "Physical charge currently running.",
            (0, 100),
        ),
        _row(
            "energy",
            "Energy",
            METER,
            HOST_SELF,
            "How much they have left to spend.",
            (0, 100),
        ),
        # meter, scene — the room's own pressure
        _row(
            "tension",
            "Tension",
            METER,
            HOST_SCENE,
            "How close the scene is to breaking.",
            (0, 10),
        ),
        # traits, self — free text
        _row(
            "mood",
            "Mood",
            TRAIT,
            HOST_SELF,
            "Their prevailing emotional colour this beat.",
        ),
        # traits, scene — free text; the flat fold + env_hud ladder read these
        _row(
            "weather",
            "Weather",
            TRAIT,
            HOST_SCENE,
            "What the sky is doing where the scene is.",
        ),
        _row(
            "tone",
            "Tone",
            TRAIT,
            HOST_SCENE,
            "The register the prose is playing in.",
        ),
        _row(
            "forecast",
            "Forecast",
            TRAIT,
            HOST_SCENE,
            "Where the weather is heading.",
        ),
        _row(
            "irony",
            "Irony",
            TRAIT,
            HOST_SCENE,
            "How much the prose means the opposite of what it says.",
        ),
        _row(
            "genre",
            "Genre",
            TRAIT,
            HOST_SCENE,
            "The story's current genre register.",
        ),
    ]


#: The built-in catalog: the rows served whenever a log's asset is absent or
#: empty (plan §4's seed, decision point §14.2). W4's Studio pane and the W2 tool
#: door's tests import these; treat them as read-only and copy before editing.
DEFAULT_ROWS: tuple[dict, ...] = tuple(_seed_rows())


def _copy_row(row: Mapping) -> dict:
    """A row copy safe to hand out: the ``range`` list is duplicated too."""
    copied = dict(row)
    bounds = copied.get("range")
    copied["range"] = list(bounds) if isinstance(bounds, (list, tuple)) else None
    if copied["range"] is None:
        del copied["range"]
    return copied


def default_rows() -> list[dict]:
    """A fresh, independently mutable copy of :data:`DEFAULT_ROWS`."""
    return [_copy_row(row) for row in DEFAULT_ROWS]


# ---------------------------------------------------------------------------
# validation (writes raise; reads tolerate)
# ---------------------------------------------------------------------------


def _require_text(raw: Mapping, name: str, where: str) -> str:
    """A required non-empty string field, stripped; ``ValueError`` otherwise."""
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: '{name}' must be a non-empty string")
    return value.strip()


def _normalize_range(value: Any, row_type: str, where: str) -> list[int] | None:
    """Validate ``range`` for the row type: meters need two ascending ints.

    A trait carries no ``range`` at all — traits are free text, and a range on one
    would promise a check the tool door never runs (§6). An explicit ``None`` is
    read as "absent" so a round-trip through a UI that nulls empty fields works.
    """
    if row_type == TRAIT:
        if value is not None:
            raise ValueError(f"{where}: a trait carries no 'range'")
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(
            f"{where}: a meter needs a 'range' of two integers, e.g. [0, 100]"
        )
    low, high = value
    for bound in (low, high):
        if isinstance(bound, bool) or not isinstance(bound, int):
            raise ValueError(
                f"{where}: a meter needs a 'range' of two integers, e.g. [0, 100]"
            )
    if low >= high:
        raise ValueError(f"{where}: 'range' must ascend, e.g. [0, 100]")
    return [low, high]


def _normalize_row(raw: Any, where: str) -> dict:
    """Validate one catalog row and return its canonical form (``ValueError``)."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: each row must be an object")
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY_PATTERN.match(key.strip()):
        raise ValueError(
            f"{where}: 'key' must be a lowercase slug (letters, digits, '_'), "
            "e.g. 'hostility'"
        )
    row_type = raw.get("type")
    if row_type not in ROW_TYPES:
        raise ValueError(f"{where}: 'type' must be one of {', '.join(ROW_TYPES)}")
    host = raw.get("host")
    if host not in ROW_HOSTS:
        raise ValueError(f"{where}: 'host' must be one of {', '.join(ROW_HOSTS)}")
    row = {
        "key": key.strip(),
        "label": _require_text(raw, "label", where),
        "type": row_type,
        "host": host,
    }
    bounds = _normalize_range(raw.get("range"), row_type, where)
    if bounds is not None:
        row["range"] = bounds
    row["description"] = _require_text(raw, "description", where)
    return row


def validate_rows(rows: Any) -> list[dict]:
    """Validate a whole catalog; return the canonical rows or raise ``ValueError``.

    Rules: ``key`` a lowercase slug, unique across the catalog and non-empty;
    ``label``/``description`` non-empty strings; ``type`` one of ``meter`` /
    ``trait``; ``host`` one of ``relational`` / ``self`` / ``scene``; a ``meter``
    carries ``range`` = two ascending integers; a ``trait`` carries no ``range``.

    This is the door the future Studio CRUD (W4) calls: the message names the
    offending row by index, and the API maps the ``ValueError`` to a 400.
    """
    if not isinstance(rows, (list, tuple)):
        raise ValueError("'rows' must be a list")
    validated: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        row = _normalize_row(raw, f"rows[{index}]")
        if row["key"] in seen:
            raise ValueError(f"rows[{index}]: duplicate key '{row['key']}'")
        seen.add(row["key"])
        validated.append(row)
    return validated


# ---------------------------------------------------------------------------
# storage (log-owned, session-independent)
# ---------------------------------------------------------------------------


def _stored_rows(log: Any) -> list[dict] | None:
    """The newest stored catalog, or ``None`` when the log holds no usable asset.

    Tolerates everything the read path can meet: a log without ``get_events``
    (the ``LogPort`` seam), a non-``system`` row, a malformed payload. Inside a
    well-formed payload a single malformed row is SKIPPED and the rest stands
    (the tracker is advisory; a hand-edited row never blanks the catalog). An
    empty result is returned as-is — :func:`catalog_for` folds it to the seed.
    """
    getter = getattr(log, "get_events", None)
    if not callable(getter):
        return None
    try:
        events = getter(ENTITY_CATALOG_ANCHOR)
    except Exception:  # noqa: BLE001 - a read failure means "no asset"
        return None
    latest: Any = None
    for event in events:
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != ENTITY_CATALOG_EVENT:
            continue
        latest = payload.get("rows")
    if not isinstance(latest, list):
        return None
    rows: list[dict] = []
    for index, raw in enumerate(latest):
        try:
            rows.append(_normalize_row(raw, f"rows[{index}]"))
        except ValueError:
            continue
    return rows


def catalog_for(log: Any | None = None) -> list[dict]:
    """The catalog the engine reads: the stored asset, or the code seed when empty.

    ``log=None`` (and any unreadable/empty/damaged asset) yields a fresh copy of
    :data:`DEFAULT_ROWS`, exactly like
    :func:`decision_queries.effective_questions`. This call is READ-ONLY: the
    seed is not appended (no lazy write, no migration — plan §4), so a fresh log
    still holds nothing on the anchor until an owner saves the catalog.
    """
    stored = _stored_rows(log) if log is not None else None
    return stored if stored else default_rows()


def replace_rows(log: "EventLog", rows: Any) -> list[dict]:
    """Replace the whole catalog with a validated row list (latest wins).

    Append-only (D9): the definitions are one ``system`` event on
    :data:`ENTITY_CATALOG_ANCHOR`; the previous catalog stays queryable and the
    newest event wins. ``ValueError`` (with a human message) on any invalid row —
    the API maps it to a 400.
    """
    validated = validate_rows(rows)
    log.append_event(
        ENTITY_CATALOG_ANCHOR,
        "system",
        USER_ACTOR,
        {"event": ENTITY_CATALOG_EVENT, "rows": validated},
    )
    return validated


# ---------------------------------------------------------------------------
# lookups (the W2 tool door)
# ---------------------------------------------------------------------------


def _as_catalog(catalog: Any) -> list[dict]:
    """The rows a lookup runs over: ``catalog`` as given, else the code seed."""
    return list(catalog) if catalog is not None else default_rows()


def row_for(key: str, catalog: Any = None) -> dict | None:
    """The row whose ``key`` is ``key``, or ``None`` when the catalog lacks it.

    ``catalog`` is what :func:`catalog_for` returned; omitted, the lookup runs
    over the code seed (:data:`DEFAULT_ROWS`) — the no-log case.
    """
    for row in _as_catalog(catalog):
        if row.get("key") == key:
            return _copy_row(row)
    return None


def meters_only(catalog: Any = None) -> list[dict]:
    """The catalog's ``meter`` rows (range-checked numbers), in catalog order."""
    return [row for row in _as_catalog(catalog) if row.get("type") == METER]


def traits_only(catalog: Any = None) -> list[dict]:
    """The catalog's ``trait`` rows (free text), in catalog order."""
    return [row for row in _as_catalog(catalog) if row.get("type") == TRAIT]


def _as_int(value: Any) -> int | None:
    """``value`` as an int, or ``None`` when it is not a number.

    The tool door receives JSON, so a model may send ``42``, ``42.0`` or
    ``"42"``; all three are the same number. ``True`` is NOT 1 here (a bool is a
    class error, not a measurement), and a fractional float is not rounded
    silently — it is refused with the same readable message.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _render_value(value: Any) -> str:
    """How a refused value is quoted back (strings quoted, numbers bare)."""
    return f"'{value}'" if isinstance(value, str) else repr(value)


def range_check(
    key: str, value: Any, catalog: Any = None
) -> tuple[bool, int | None, str]:
    """Range-check one meter value: ``(ok, coerced_int_or_none, message)``.

    The plan §6 error-text shape, readable and corrective::

        hostility expects an integer in 0–100, got 142

    ``ok`` is ``False`` with a message for (a) a key the catalog does not hold,
    (b) a ``trait`` row (free text — the door sets that one without a number),
    (c) a non-numeric value, and (d) an out-of-range value. ``catalog`` is what
    :func:`catalog_for` returned; omitted, the check runs against the code seed.
    """
    row = row_for(key, catalog)
    if row is None:
        known = ", ".join(str(other.get("key")) for other in _as_catalog(catalog))
        return (
            False,
            None,
            f"unknown attribute '{key}' — catalog keys: {known} "
            "(the owner edits this list in the Studio)",
        )
    if row["type"] != METER:
        return (
            False,
            None,
            f"{row['key']} is a free-text trait — it takes text, not a number",
        )
    low, high = row["range"]
    number = _as_int(value)
    if number is None:
        return (
            False,
            None,
            f"{row['key']} expects an integer in {low}–{high}, "
            f"got {_render_value(value)}",
        )
    if not low <= number <= high:
        return (
            False,
            None,
            f"{row['key']} expects an integer in {low}–{high}, got {number}",
        )
    return True, number, ""
