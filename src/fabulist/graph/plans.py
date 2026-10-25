"""Turn plans: the planner's steering plane persisted in the event log (W9.20).

The planner is an **infrequent turn step** (W9.20 §1): turn 1 is mandatory, then it
re-runs every ``cadence`` committed turns. It reads the whole context and writes a
:class:`Plan` — the forward **strings** the turn should steer toward (Story-Strings
heritage, now deliberate), the scene's ``datetime``/``weather``/``forecast``, the
``established`` fact picks that must not be contradicted, and any preset-defined
``extras`` (currency, reputation, ...). String sets persist across the N turns
between runs, so a plan is not a per-turn artefact.

Storage rule (D9): a plan is **not** a side table. It is one ``system`` event whose
payload is ``{"event": "plan", "plan": <Plan.to_dict()>}``. The log owns all data,
so :func:`load_last_plan` simply folds the stream and the latest non-ephemeral plan
wins. Ephemeral plans (abandoned swipe branches, W2.2) are ignored exactly like
every other ephemeral event.

Fork discipline (W9.20 §2 / M2 — deliberate behavior change): the fold reads the
**fork-aware chain** (:meth:`EventLog.get_chain_events`), so a branch inherits its
parent's plan instead of rendering none. Plan history is session-constant context,
which is what the AGENTS.md fork rule requires; this is not a bug to "fix" back.

The :func:`render_plan_for_prompt` string is what the compiler receives through its
``plan=`` seam (``compile(..., plan=...)``); it is deliberately tiny
(<= :data:`MAX_PLAN_CHARS`) and feeds the NPC lanes' ``beat`` on every turn, not
just on planner turns (W9.20 §5.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..log import Event

__all__ = [
    "MAX_PLAN_CHARS",
    "PLAN_ACTOR",
    "Plan",
    "load_last_plan",
    "plans_differ",
    "render_plan_for_prompt",
    "save_plan",
]

#: Actor recorded on appended plan events (D9: the dispatcher/proposer writes).
PLAN_ACTOR = "planner"

#: Hard cap on the prompt-facing plan string (keeps the ``plan=`` seam compact).
#: The new shape carries 4-6 strings + established + extras, so the cap is
#: generous enough for a full plan yet still a handful of lines in the prompt.
MAX_PLAN_CHARS = 700

#: The two legitimate origins of a plan: the planner node, or a manual edit
#: (Studio / ``PUT /api/sessions/{id}/plan``). Anything else is a bug.
_VALID_CREATED_FROM = frozenset({"graph", "manual"})


@dataclass
class Plan:
    """The steering plan for the next N turns.

    ``strings`` are the 4-6 forward paths the narrator should steer toward;
    ``established`` are the folded fact picks that matter (the "reranker" role —
    what the plan refuses to contradict); ``extras`` holds whatever fields the
    active preset added to the planner schema (validated against that field list
    by :func:`fabulist.graph.planner_schema.validate_planner_payload`).
    ``turn_seq`` is the committed-turn count at generation time, which is what
    the cadence compares against.
    """

    turn_seq: int
    created_from: str = "graph"
    strings: list[str] = field(default_factory=list)
    datetime: str | None = None
    weather: str | None = None
    forecast: str | None = None
    established: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serializable payload shape stored in the plan event."""
        return {
            "turn_seq": self.turn_seq,
            "created_from": self.created_from,
            "strings": list(self.strings),
            "datetime": self.datetime,
            "weather": self.weather,
            "forecast": self.forecast,
            "established": list(self.established),
            "extras": dict(self.extras),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        """Rebuild a :class:`Plan` from a plan-event payload dict.

        A payload WITHOUT a ``strings`` key is the legacy beats/blocking/threads
        shape (pre-W9.20). It raises rather than guessing a translation: callers
        that fold a stream treat it as ABSENT instead (see
        :func:`load_last_plan`), which is the accepted upgrade consequence — an
        existing session re-plans on its next turn, exactly like turn 1.
        """
        if "strings" not in data:
            raise ValueError("legacy plan payload (no `strings`): treat as absent")
        extras = data.get("extras")
        return cls(
            turn_seq=int(data.get("turn_seq", 0)),
            created_from=str(data.get("created_from") or "graph"),
            strings=[str(s) for s in data.get("strings") or []],
            datetime=_optional_text(data.get("datetime")),
            weather=_optional_text(data.get("weather")),
            forecast=_optional_text(data.get("forecast")),
            established=[str(e) for e in data.get("established") or []],
            extras=dict(extras) if isinstance(extras, dict) else {},
        )


def _optional_text(value: Any) -> str | None:
    """Normalize an optional string field: blank/absent -> ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def save_plan(log: Any, session_id: str, plan: Plan) -> Event:
    """Append the plan as ONE ``system`` event and return it.

    The event payload is ``{"event": "plan", "plan": plan.to_dict()}`` so plan
    rows are distinguishable from other system events. The log owns the data
    (D9); callers never write a plan anywhere else. ``created_from`` must be
    ``"graph"`` or ``"manual"`` — anything else is a programming error.
    """
    if plan.created_from not in _VALID_CREATED_FROM:
        raise ValueError(
            f"created_from must be one of {sorted(_VALID_CREATED_FROM)}, "
            f"got {plan.created_from!r}"
        )
    return log.append_event(
        session_id,
        "system",
        PLAN_ACTOR,
        {"event": "plan", "plan": plan.to_dict()},
    )


def load_last_plan(events_or_log: Any, session_id: str) -> Plan | None:
    """Return the latest non-ephemeral plan on the CHAIN, or ``None``.

    ``events_or_log`` may be an :class:`~fabulist.log.EventLog` (or any object
    exposing ``get_chain_events(session_id)`` / ``get_events(session_id)``) or an
    iterable of events/projection dicts. Ephemeral rows are skipped (D10/W2.2);
    when a stream carries more than one plan, the one with the greatest ``seq``
    wins.

    Two rules matter and are deliberate (W9.20 §2/M2, §3/M3):

    * the log read is the fork-aware **chain** — a branch inherits its parent's
      plan (today's session-local fold rendered no plan for a fork);
    * a payload carrying the legacy beats/blocking/threads shape (no ``strings``
      key) counts as **absent**, so an upgraded session plans again on its next
      turn instead of steering from a plan the narrator cannot read.
    """
    events = _coerce_events(events_or_log, session_id)
    events = _sorted_by_seq(events)

    latest: dict | None = None
    for event in events:
        if _field(event, "type") != "system" or _field(event, "ephemeral"):
            continue
        payload = _field(event, "payload") or {}
        if payload.get("event") != "plan":
            continue
        plan = payload.get("plan")
        if isinstance(plan, dict) and "strings" in plan:
            latest = plan
    return None if latest is None else Plan.from_dict(latest)


def plans_differ(a: Plan | None, b: Plan | None) -> bool:
    """Drift check: do two plans describe materially different steering?

    Comparison is deliberately loose — strings and established picks compare as
    **sets** (reordering the same lines is not drift) and the scene fields as
    normalized text. ``None`` counts as "no plan": two ``None`` values do not
    differ, one does.
    """
    if a is None or b is None:
        return (a is None) != (b is None)
    if set(a.strings) != set(b.strings):
        return True
    if set(a.established) != set(b.established):
        return True
    for left, right in ((a.datetime, b.datetime), (a.weather, b.weather), (a.forecast, b.forecast)):
        if (left or "").strip() != (right or "").strip():
            return True
    return a.extras != b.extras


def render_plan_for_prompt(plan: Plan) -> str:
    """Render a compact multi-line block for the compiler's ``plan=`` seam.

    Line order is the steering priority: the forward ``STRINGS`` first, then the
    scene (``WHEN``/``WEATHER``), then what the plan holds ``ESTABLISHED``, then
    the preset extras. The result never exceeds :data:`MAX_PLAN_CHARS`
    characters; ``\u2026`` marks a truncated plan.
    """
    lines = [f"PLAN (turn {plan.turn_seq})"]

    strings = [s.strip() for s in plan.strings if s and s.strip()]
    if strings:
        lines.append("STRINGS: " + "; ".join(strings))

    when = (plan.datetime or "").strip()
    if when:
        lines.append(f"WHEN: {when}")

    weather = (plan.weather or "").strip()
    forecast = (plan.forecast or "").strip()
    if weather and forecast:
        lines.append(f"WEATHER: {weather} (then {forecast})")
    elif weather:
        lines.append(f"WEATHER: {weather}")
    elif forecast:
        lines.append(f"FORECAST: {forecast}")

    established = [e.strip() for e in plan.established if e and e.strip()]
    if established:
        lines.append("ESTABLISHED: " + "; ".join(established))

    for key, value in (plan.extras or {}).items():
        rendered = _render_extra(value)
        if rendered:
            lines.append(f"{str(key).upper()}: {rendered}")

    text = "\n".join(lines)
    if len(text) > MAX_PLAN_CHARS:
        text = text[: MAX_PLAN_CHARS - 1].rstrip() + "\u2026"
    return text


def _render_extra(value: Any) -> str:
    """One preset extra as prompt text (booleans read as yes/no)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).strip()


# -- internals ---------------------------------------------------------------
_MISSING = object()


def _field(event: Any, key: str, default: Any = None) -> Any:
    """Read a field from an :class:`Event` or a projection dict alike."""
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def _coerce_events(events_or_log: Any, session_id: str) -> list[Any]:
    """Normalize a log or an event iterable to a list for ``session_id``.

    A log object is read through ``get_chain_events`` when it has one (fork-aware
    — the W9.20 §2/M2 rule for every plan read); ``get_events`` is the fallback
    for a session-local-only port.
    """
    get_chain_events = getattr(events_or_log, "get_chain_events", None)
    if callable(get_chain_events):
        return list(get_chain_events(session_id))

    get_events = getattr(events_or_log, "get_events", None)
    if callable(get_events):
        return list(get_events(session_id))

    events = list(events_or_log)
    # Only filter when the events actually carry a session id, so bare payload
    # dicts (which do not) are still usable.
    if any(_field(e, "session_id", _MISSING) is not _MISSING for e in events):
        events = [e for e in events if _field(e, "session_id") == session_id]
    return events


def _sorted_by_seq(events: Iterable[Any]) -> list[Any]:
    """Order by ``seq`` when every event exposes an int ``seq``; else as given."""
    ordered = list(events)
    seqs = [_field(e, "seq", _MISSING) for e in ordered]
    if ordered and all(isinstance(seq, int) for seq in seqs):
        return sorted(ordered, key=lambda e: _field(e, "seq"))
    return ordered
