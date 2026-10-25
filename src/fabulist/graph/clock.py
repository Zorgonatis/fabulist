"""Rules-based clock node (D3) + the user-action classifier seam.

Deterministic by design: no LLM call, no randomness, no wall-clock reads. Given
the classified user action and the prior facts line, the clock advances the
in-fiction time and re-emits the facts block with every other key passed
through untouched.

Advancement table (W1.1 contract)
---------------------------------
    dialogue    +5..15 min  -> +10 (midpoint)
    inspection  +20 min
    travel      +25 min
    task        +2 h  (120 min)
    rest        skip to the next morning (07:00)

The clock also folds an in-fiction date. A session whose facts carry no ``date``
starts at the canonical epoch (:data:`EPOCH_DATE`, day 1); the date advances one
day whenever the time crosses midnight — including a ``rest`` that lands on or
before its own start time (a same-day rest does not). A ``date`` already present
in the facts (log replay) is honored, not reset. The fold carries the full
rendered ``date`` plus the numeric ``day`` (days since epoch, 1-based).

The facts block convention (shared W1.1 contract)::

    <state>
    time: 14:35 | loc: night market | weather: light rain | date: Monday, June 02, 2025 | day: 1
    </state>

The table above stays the DEFAULT mover. W9.32 R4 adds a proposal channel on
top of it: the tracker may propose a new instant with prose evidence and the
engine judges it with :func:`validate_time_proposal` (never earlier, bounded
jump, date/format consistency, non-empty evidence). Accepting a proposal is the
caller's job — nothing here writes; the engine appends the accepted instant.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from .types import ClockInputs, ClockOutputs

__all__ = [
    "ADVANCE_MINUTES",
    "ActionClassifier",
    "ClockInputs",
    "ClockOutputs",
    "DATE_FORMAT",
    "DEFAULT_TIME",
    "EPOCH_DATE",
    "MAX_PROPOSAL_MINUTES",
    "REST_MORNING",
    "TimeProposalVerdict",
    "advance",
    "classify_action",
    "format_instant",
    "parse_facts",
    "parse_instant",
    "parse_plan_instant",
    "parse_time_of_day",
    "render_facts",
    "render_state_block",
    "validate_time_proposal",
]

#: Minutes added per action class (``rest`` is handled out of band).
ADVANCE_MINUTES: dict[str, int] = {
    "dialogue": 10,  # midpoint of the +5..15 window
    "inspection": 20,
    "travel": 25,
    "task": 120,
}

#: Clock actions understood by the rules table.
ACTION_CLASSES = ("dialogue", "inspection", "travel", "task", "rest")

#: Used when the prior facts carry no ``time`` key yet.
DEFAULT_TIME = "08:00"

#: ``rest`` wakes the character at this local time.
REST_MORNING = "07:00"

#: Canonical day-1 date for a session whose facts carry no ``date`` yet.
EPOCH_DATE = date(2025, 6, 2)

#: Rendered ``date`` format, e.g. ``"Monday, June 02, 2025"`` (also parsed).
DATE_FORMAT = "%A, %B %d, %Y"


class ActionClassifier(Protocol):
    """Classifies a raw user message into one of `ACTION_CLASSES`."""

    def __call__(self, text: str) -> str: ...


# Ordered keyword tables. First table with a hit wins; `dialogue` is the
# default because speaking is the most common turn. These are intentionally
# crude — the post-turn extractor (W1.3) supplies the resolved classification.
_REST_KEYWORDS = ("rest", "sleep", "camp", "bed", "nap", "doze", "make camp")
_TRAVEL_KEYWORDS = (
    "go ",
    "walk",
    "travel",
    "ride",
    "head to",
    "head for",
    "leave",
    "journey",
    "ride to",
    "north",
    "south",
    "east",
    "west",
)
_TASK_KEYWORDS = (
    "craft",
    "build",
    "repair",
    "cook",
    "forge",
    "brew",
    "train",
    "practice",
    "smith",
    "work on",
)
_INSPECTION_KEYWORDS = (
    "look",
    "inspect",
    "examine",
    "search",
    "check",
    "study",
    "read",
    "investigate",
    "survey",
)


def classify_action(text: str) -> str:
    """Keyword heuristic for the user's action class (default classifier)."""
    lowered = text.lower()
    for keywords, action in (
        (_REST_KEYWORDS, "rest"),
        (_TRAVEL_KEYWORDS, "travel"),
        (_TASK_KEYWORDS, "task"),
        (_INSPECTION_KEYWORDS, "inspection"),
    ):
        if any(keyword in lowered for keyword in keywords):
            return action
    return "dialogue"


# -- facts (de)serialization -------------------------------------------------


def parse_facts(facts: str) -> dict[str, str]:
    """Parse a facts block or bare facts line into an ordered dict.

    Accepts either the full ``<state>...</state>`` block or the inner pipe-YAML
    line. Keys with no ``:`` (or empty chunks) are skipped.
    """
    text = facts.strip()
    if text.startswith("<state>"):
        text = text[len("<state>") :]
    if text.endswith("</state>"):
        text = text[: -len("</state>")]
    parsed: dict[str, str] = {}
    for chunk in text.replace("\n", "|").split("|"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        key, value = chunk.split(":", 1)
        key = key.strip()
        if key:
            parsed[key] = value.strip()
    return parsed


#: An ``HH:MM`` clock time inside a free-text datetime (see
#: :func:`parse_time_of_day`).
_TIME_OF_DAY_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def parse_time_of_day(value: str | None) -> str | None:
    """Pull an ``HH:MM`` clock time out of free text, or ``None``.

    The planner writes the scene's start as a free-text ``datetime`` ("Monday,
    June 02, 2025 at 14:35", "2025-06-02 14:35", "14:35"), because that is what a
    model naturally produces. Seeding the rules clock needs only the time of day
    (D3: the clock owns ``time``), so the first valid ``HH:MM`` in the text wins
    and is normalized to :func:`_format_time`. A date-only or unparseable
    datetime seeds nothing — it still rides the plan prompt.
    """
    if not value:
        return None
    for match in _TIME_OF_DAY_RE.finditer(str(value)):
        hours, minutes = int(match.group(1)), int(match.group(2))
        if hours < 24 and minutes < 60:
            return _format_time(hours * 60 + minutes)
    return None


def render_facts(facts: dict[str, str]) -> str:
    """Render an ordered dict as the inner pipe-YAML line."""
    return " | ".join(f"{key}: {value}" for key, value in facts.items())


def render_state_block(facts: dict[str, str]) -> str:
    """Render the full ``<state>...</state>`` block injected pre-turn."""
    return f"<state>\n{render_facts(facts)}\n</state>"


# -- rules engine ------------------------------------------------------------


def _parse_time(value: str) -> int:
    """``"14:35"`` -> minutes since midnight. Falls back to `DEFAULT_TIME`."""
    try:
        hours, minutes = value.split(":", 1)
        total = int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError):
        return _parse_time(DEFAULT_TIME)
    return total % (24 * 60)


def _format_time(total: int) -> str:
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def _parse_date(value: str) -> date | None:
    """``"Monday, June 02, 2025"`` -> ``date``. ``None`` when unparseable."""
    try:
        return datetime.strptime(value, DATE_FORMAT).date()
    except (ValueError, TypeError):
        return None


def _format_date(value: date) -> str:
    return value.strftime(DATE_FORMAT)


def _day_number(value: date) -> int:
    """1-based day index of `value` relative to :data:`EPOCH_DATE`."""
    return (value - EPOCH_DATE).days + 1


def advance(action_class: str, facts: str) -> ClockOutputs:
    """Advance the clock by the rules table, passing other facts through.

    Also folds the in-fiction ``date``/``day`` (additive keys, appended last so
    the legacy fold order is untouched): the date advances when the advance
    crosses midnight, and a ``date`` already in `facts` is honored, not reset.

    Unknown action classes fall back to the dialogue advance so a classifier
    miss can never stall time.
    """
    prior = parse_facts(facts)
    start = _parse_time(prior.get("time", DEFAULT_TIME))
    start_date = _parse_date(prior.get("date", "")) or EPOCH_DATE

    if action_class == "rest":
        end = _parse_time(REST_MORNING)
        advanced = (end - start) % (24 * 60)
        # A rest landing on/before its start time slept through midnight.
        crossed_midnight = end <= start
    else:
        advanced = ADVANCE_MINUTES.get(action_class, ADVANCE_MINUTES["dialogue"])
        raw_end = start + advanced
        end = raw_end % (24 * 60)
        crossed_midnight = raw_end >= 24 * 60

    new_time = _format_time(end)
    current_date = start_date + timedelta(days=1 if crossed_midnight else 0)
    ordered: dict[str, str] = {"time": new_time}
    for key, value in prior.items():
        if key in ("time", "date", "day"):
            continue
        ordered[key] = value
    ordered["date"] = _format_date(current_date)
    ordered["day"] = str(_day_number(current_date))

    return ClockOutputs(
        facts=render_state_block(ordered),
        time=new_time,
        advanced_minutes=advanced,
    )


# -- instants (W9.32 R4) ------------------------------------------------------
#
# Three spellings reach us and all three must round-trip: the fold's own
# ``time``/``date`` pair (``"14:35"`` + ``"Monday, June 02, 2025"``), the ISO
# absolute the log/tree carries (``"2025-06-02 14:35"``), and the planner's free
# text (``"Monday, June 02, 2025 at 14:35"``).

#: Hard cap on an unevidenced time jump (the R4 "bounded jump" guardrail).
MAX_PROPOSAL_MINUTES = 360

#: An ``HH:MM`` clock time inside free text.
_TIME_TOKEN_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")

#: Whole-string datetime spellings, exact and in preference order.
_INSTANT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    DATE_FORMAT + " at %H:%M",
    DATE_FORMAT + " %H:%M",
)

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_MONTH_DATE_RE = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")

#: ``"+2h"``, ``"2h30"``, ``"-2h30m"`` — a signed/explicit-hour delta.
_HOUR_DELTA_RE = re.compile(
    r"(?P<sign>[+-])?(?P<hours>\d{1,3})\s*(?:h|hr|hrs|hour|hours)"
    r"(?:\s*(?P<minutes>\d{1,3}))?\s*(?:m|min|mins|minute|minutes)?",
    re.IGNORECASE,
)

#: ``"+90"``, ``"-30"``, ``"+90m"`` — a signed minutes delta. A bare integer
#: with no sign is deliberately NOT a delta: it is refused as unreadable.
_MINUTE_DELTA_RE = re.compile(
    r"(?P<sign>[+-])(?P<minutes>\d{1,4})\s*(?:m|min|mins|minute|minutes)?",
    re.IGNORECASE,
)


def _normalize_text(text: object) -> str | None:
    """Collapse whitespace and trim edge punctuation; ``None`` when empty."""
    if not isinstance(text, str):
        return None
    cleaned = " ".join(text.split()).strip(" .,;")
    return cleaned or None


def _date_in_text(text: str) -> date | None:
    """Find a calendar date anywhere in `text` (ISO or month-name spelling)."""
    iso = _ISO_DATE_RE.search(text)
    if iso is not None:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    named = _MONTH_DATE_RE.search(text)
    if named is not None:
        for month_format in ("%B", "%b"):
            try:
                return datetime.strptime(
                    f"{named.group(1)} {named.group(2)} {named.group(3)}",
                    f"{month_format} %d %Y",
                ).date()
            except ValueError:
                continue
    return None


def _parse_instant_parts(text: object) -> tuple[date | None, int] | None:
    """Parse an instant, reporting whether the text carried a date.

    The date is ``None`` when the text held only a time of day (bare ``"14:35"``
    or a time inside prose) — the caller supplies the date for those. ``None``
    return means no valid instant at all; this never raises.
    """
    cleaned = _normalize_text(text)
    if cleaned is None:
        return None
    for fmt in _INSTANT_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return parsed.date(), parsed.hour * 60 + parsed.minute
    # ISO with seconds/offset. A date with no time of day is NOT an instant.
    if ":" in cleaned:
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            pass
        else:
            return parsed.date(), parsed.hour * 60 + parsed.minute
    # Lenient: a time token anywhere, plus a date anywhere else in the text.
    # ``strptime`` ignores which weekday name ``%A`` sees, so a wrong weekday
    # (or a missing one) does not stop a date from parsing.
    token = _TIME_TOKEN_RE.search(cleaned)
    if token is None:
        return None
    hours, minutes = int(token.group(1)), int(token.group(2))
    if hours > 23 or minutes > 59:
        return None
    rest = cleaned[: token.start()] + " " + cleaned[token.end() :]
    found = _date_in_text(rest)
    if found is None and (_ISO_DATE_RE.search(rest) or _MONTH_DATE_RE.search(rest)):
        # A date was stated but it does not exist: never silently fall back to
        # "today", or the refusal would hide a wrong calendar in the proposal.
        return None
    return found, hours * 60 + minutes


def parse_instant(text: str) -> tuple[date, int] | None:
    """Parse ``"YYYY-MM-DD HH:MM"``, ``DATE_FORMAT`` + ``HH:MM``, or ``"HH:MM"``.

    A bare time of day (or a time inside prose) carries no date, so it takes
    :data:`EPOCH_DATE` as a placeholder for the caller to replace — see
    :func:`validate_time_proposal`, which supplies the current date. Returns
    ``None`` for anything that yields no valid instant, and never raises.
    """
    parts = _parse_instant_parts(text)
    if parts is None:
        return None
    parsed_date, minutes = parts
    return (parsed_date or EPOCH_DATE), minutes


def format_instant(value: date, minutes: int) -> str:
    """Render the canonical absolute instant, ISO ``"YYYY-MM-DD HH:MM"``."""
    return f"{value.isoformat()} {_format_time(minutes)}"


def parse_plan_instant(value: str) -> tuple[str, str, int] | None:
    """Planner free-text datetime -> ``(time, date render, day)``.

    The plan's ``datetime`` is whatever the model wrote, so the weekday is
    optional and never trusted. ``day`` is the clock's own facts-block day index
    (1-based days since :data:`EPOCH_DATE`), the number ``advance`` folds and the
    tree reads — not a weekday index. A datetime with no date seeds the epoch
    date (what the clock would have defaulted to anyway); no readable time of day
    seeds nothing.
    """
    parts = _parse_instant_parts(value)
    if parts is None:
        return None
    parsed_date, minutes = parts
    parsed_date = parsed_date or EPOCH_DATE
    return _format_time(minutes), _format_date(parsed_date), _day_number(parsed_date)


def _absolute_minutes(value: date, minutes: int) -> int:
    """Minutes from the epoch instant — absolute, so dates compare linearly."""
    return (value - EPOCH_DATE).days * (24 * 60) + minutes


def _split_absolute(total: int) -> tuple[date, int]:
    """:func:`_absolute_minutes` inverse: minutes before the epoch stay valid."""
    return EPOCH_DATE + timedelta(days=total // (24 * 60)), total % (24 * 60)


def _parse_delta_minutes(value: object) -> int | None:
    """``"+90"`` / ``"+2h"`` / ``"2h30"`` / ``"-30"`` -> signed minutes."""
    cleaned = _normalize_text(value)
    if cleaned is None:
        return None
    hours_match = _HOUR_DELTA_RE.fullmatch(cleaned)
    if hours_match is not None:
        sign = -1 if hours_match.group("sign") == "-" else 1
        hours = int(hours_match.group("hours"))
        minutes = int(hours_match.group("minutes") or 0)
        return sign * (hours * 60 + minutes)
    minutes_match = _MINUTE_DELTA_RE.fullmatch(cleaned)
    if minutes_match is not None:
        sign = -1 if minutes_match.group("sign") == "-" else 1
        return sign * int(minutes_match.group("minutes"))
    return None


def _is_rest_evidence(evidence: str) -> bool:
    """Does the prose itself claim a sleep or a wait (the ``rest`` class)?"""
    lowered = evidence.lower()
    return any(keyword in lowered for keyword in _REST_KEYWORDS)


@dataclass(frozen=True)
class TimeProposalVerdict:
    """The engine's judgement of one proposed clock instant.

    ``time``/``date``/``day`` describe the clock AFTER the verdict: the proposed
    instant on accept, the unchanged current clock on refusal (a refusal never
    moves time). ``advanced_minutes`` is the forward step in minutes (0 on
    refusal) and ``reason`` is the corrective text for the tracker: empty when
    the proposal is accepted, except that a cross-midnight sleep resolution says
    so there (the date/day already show the next day).
    """

    ok: bool
    time: str
    date: str
    day: int
    advanced_minutes: int
    reason: str


def validate_time_proposal(
    current_time: str,
    current_date: str,
    to: str,
    *,
    evidence: str = "",
    stated_sleep: bool = False,
) -> TimeProposalVerdict:
    """Judge a proposed clock instant against this turn's live clock.

    `to` is an absolute ``"HH:MM"``, ``"YYYY-MM-DD HH:MM"`` or ``DATE_FORMAT``
    render, or a delta (``"+90"``, ``"+2h"``, ``"2h30"``, ``"-30"``). The R4
    guardrails, in the order they are checked:

    1. readability — text yielding no instant is refused naming the accepted
       forms;
    2. evidence — empty/whitespace proof is refused (the tracker must say which
       prose moved the clock);
    3. date consistency — a stated date must parse and may not precede the
       current date;
    4. never earlier — the resulting absolute instant must be >= the current one,
       so an earlier time of day is only legal with an explicitly later date —
       or as a cross-midnight sleep (next paragraph);
    5. bounded jump — at most :data:`MAX_PROPOSAL_MINUTES` forward, unless
       ``stated_sleep`` or the evidence names a rest/wait (``_REST_KEYWORDS``).

    **Cross-midnight sleep.** A bare time of day with no stated date is read on
    the current date, except when the proposal claims a sleep/rest
    (``stated_sleep`` or ``_REST_KEYWORDS`` in the evidence) and the time of day
    is already past: then it is TOMORROW's clock, the way the rules table's own
    ``rest`` lands on 07:00 the next morning. 22:00 + ``"07:00"`` + sleep is 540
    minutes forward, not backwards. A stated date is never re-read this way, and
    a delta is elapsed time (never a tomorrow), so neither can be rescued.

    Pure: nothing is written. The caller appends an accepted verdict.
    """
    current_day = _parse_date(current_date) or EPOCH_DATE
    current_parts = _parse_instant_parts(current_time)
    current_minutes = (
        current_parts[1] if current_parts is not None else _parse_time(DEFAULT_TIME)
    )
    current_absolute = _absolute_minutes(current_day, current_minutes)

    def refusal(reason: str) -> TimeProposalVerdict:
        return TimeProposalVerdict(
            ok=False,
            time=_format_time(current_minutes),
            date=_format_date(current_day),
            day=_day_number(current_day),
            advanced_minutes=0,
            reason=reason,
        )

    # 1. read `to`
    stated_date: date | None = None
    is_delta = False
    delta = _parse_delta_minutes(to)
    if delta is not None:
        is_delta = True
        target_absolute = current_absolute + delta
    else:
        parts = _parse_instant_parts(to)
        if parts is None:
            return refusal(
                f"refused: {str(to).strip()!r} is not a time I can read — say it as "
                '"HH:MM", "YYYY-MM-DD HH:MM", '
                f'"{_format_date(EPOCH_DATE)} at 14:35", or a delta like '
                '"+90", "+2h", "2h30".'
            )
        stated_date, target_minutes = parts
        # A bare time of day lives on the current date: earlier is earlier,
        # unless the next block resolves a sleep across midnight.
        target_absolute = _absolute_minutes(stated_date or current_day, target_minutes)

    # 2. evidence
    if not evidence.strip():
        return refusal(
            "refused: say which prose moved the clock (evidence is required)."
        )

    # 3. date consistency
    if stated_date is not None and stated_date < current_day:
        return refusal(
            f"refused: the stated date {_format_date(stated_date)} is before the "
            f"current date {_format_date(current_day)}."
        )

    # 3b. cross-midnight sleep: a sleep with no stated date and an already-past
    # time of day means tomorrow morning (the rules table's `rest` -> 07:00),
    # not a backwards jump.
    overnight = False
    rest_claimed = stated_sleep or _is_rest_evidence(evidence)
    if (
        not is_delta
        and stated_date is None
        and rest_claimed
        and target_absolute % (24 * 60) < current_minutes
    ):
        target_absolute += 24 * 60
        overnight = True

    advanced = target_absolute - current_absolute
    target_day, target_minutes = _split_absolute(target_absolute)

    # 4. never earlier
    if advanced < 0:
        return refusal(
            f"refused: {format_instant(target_day, target_minutes)} is before the "
            f"current instant {format_instant(current_day, current_minutes)} — the "
            "clock never runs backwards."
        )

    # 5. bounded jump
    if (
        advanced > MAX_PROPOSAL_MINUTES
        and not stated_sleep
        and not _is_rest_evidence(evidence)
    ):
        return refusal(
            f"refused: {advanced} minutes is past the {MAX_PROPOSAL_MINUTES}-minute "
            f"jump bound ({format_instant(current_day, current_minutes)} -> "
            f"{format_instant(target_day, target_minutes)}) — pass sleep: true with "
            "the evidence if the prose covers a sleep or a wait."
        )

    return TimeProposalVerdict(
        ok=True,
        time=_format_time(target_minutes),
        date=_format_date(target_day),
        day=_day_number(target_day),
        advanced_minutes=advanced,
        reason=(
            f"accepted: no date was stated and {_format_time(target_minutes)} had "
            "already passed today, so the stated sleep carries the clock to "
            f"{_format_date(target_day)} ({advanced} minutes)."
            if overnight
            else ""
        ),
    )
