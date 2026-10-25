"""The planner's dynamic output schema + request ladder (W9.20 §4 / M6).

The planner's field list **is preset data** ([OWNER] #6): core fields are always
present, a preset adds its own extras (currency, reputation, ...), and the same
list defines (a) the tool JSON schema the model answers against, (b) the payload
validation, and (c) what the Studio edits. Nothing here reads a preset or a
connection — callers pass the field list and the capability dict in, so the
module stays pure and testable.

Capability ladder (mirrors :func:`fabulist.state.bts.build_bts_request_profile`
exactly, W9.20 §4/M6):

* ``None`` — the connection has no function calling: content mode, the schema is
  stated in the prompt and the reply goes through the same ``structured_result``
  ladder as every other structured agent (Tier D floor, D69);
* ``tool_choice: "auto"`` — function calling without a forced choice;
* ``tool_choice: "required"`` — only where the provider enforces it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .plans import Plan

__all__ = [
    "CORE_FIELD_IDS",
    "PLANNER_TOOL_NAME",
    "build_planner_request_profile",
    "normalize_field_list",
    "planner_tool",
    "validate_planner_payload",
]

#: Tool name for the planner's structured reply.
PLANNER_TOOL_NAME = "plan_scenario"

#: The core fields every planner answer carries (extras may not shadow them).
CORE_FIELD_IDS = (
    "strings",
    "datetime",
    "weather",
    "forecast",
    "established",
)

#: Minimum / maximum number of forward strings the plan must carry (Story
#: Strings heritage: a run of 4-6 paths, not one beat).
MIN_STRINGS = 4
MAX_STRINGS = 6

#: The value types a preset field may declare.
FIELD_TYPES = ("string", "number", "boolean")

#: Identifier-safe field ids (the ids also become JSON property names).
_FIELD_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_STRING = {"type": "string"}


def normalize_field_list(fields: Any) -> list[dict]:
    """Validate + normalize a preset field list; raise ``ValueError`` on a fault.

    Each row is ``{"id", "title", "type", "hint", "required"}`` (``hint`` and
    ``required`` optional). Ids must be unique, identifier-safe and must not
    shadow a core field. The reason text is what the API returns in a 422, so it
    names the offending row rather than a code.
    """
    if fields is None:
        return []
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        raise ValueError("planner field list must be a list")
    normalized: list[dict] = []
    seen: set[str] = set()
    for raw in fields:
        if not isinstance(raw, Mapping):
            raise ValueError("planner field rows must be objects")
        field_id = str(raw.get("id") or "").strip()
        if not _FIELD_ID_RE.match(field_id):
            raise ValueError(f"field id {field_id!r} is not identifier-safe")
        if field_id in CORE_FIELD_IDS:
            raise ValueError(f"field id {field_id!r} is a core planner field")
        if field_id in seen:
            raise ValueError(f"duplicate field id {field_id!r}")
        seen.add(field_id)
        field_type = str(raw.get("type") or "string").strip()
        if field_type not in FIELD_TYPES:
            raise ValueError(
                f"field {field_id!r} has type {field_type!r}; "
                f"allowed: {', '.join(FIELD_TYPES)}"
            )
        normalized.append(
            {
                "id": field_id,
                "title": str(raw.get("title") or field_id).strip() or field_id,
                "type": field_type,
                "hint": str(raw.get("hint") or "").strip(),
                "required": bool(raw.get("required")),
            }
        )
    return normalized


def _extra_property(row: Mapping) -> dict:
    """One preset extra as a JSON Schema property."""
    schema: dict[str, Any] = {"type": row["type"], "description": row["title"]}
    if row.get("hint"):
        schema["description"] = f"{row['title']}: {row['hint']}"
    return schema


def planner_tool(fields: Any = None) -> dict:
    """The planner's tool declaration: core fields + the preset's extras.

    Core properties: ``strings`` (4-6 forward paths), ``datetime``, ``weather``,
    ``forecast`` (nullable strings) and ``established`` (the fact picks that
    matter). ``strings`` is the only unconditionally required field — a preset
    extra marked ``required`` joins it.
    """
    extras = normalize_field_list(fields)
    properties: dict[str, Any] = {
        "strings": {
            "type": "array",
            "items": _STRING,
            "minItems": MIN_STRINGS,
            "maxItems": MAX_STRINGS,
            "description": (
                "Forward story strings: the paths the next turns should steer toward. "
                f"{MIN_STRINGS}-{MAX_STRINGS} of them, one line each, in priority order."
            ),
        },
        "datetime": {
            "type": ["string", "null"],
            "description": "In-fiction date and time the scene opens at (follow the card/world when it states one).",
        },
        "weather": {
            "type": ["string", "null"],
            "description": (
                "In-fiction weather now, as a brief phrase like 'cool grey drizzle' — "
                "never a full sentence with qualifiers."
            ),
        },
        "forecast": {
            "type": ["string", "null"],
            "description": (
                "Weather a few hours ahead, as a brief phrase like 'rain by dusk' — "
                "never a full sentence with qualifiers."
            ),
        },
        "established": {
            "type": "array",
            "items": _STRING,
            "description": (
                "The facts this plan holds established — what the telling must not "
                "contradict (characters, places, commitments, tone)."
            ),
        },
    }
    required = ["strings"]
    for row in extras:
        properties[row["id"]] = _extra_property(row)
        if row["required"]:
            required.append(row["id"])
    return {
        "type": "function",
        "function": {
            "name": PLANNER_TOOL_NAME,
            "description": (
                "Set the story plan: the forward strings to steer toward, the scene "
                "clock and weather, and what is already established."
            ),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def build_planner_request_profile(
    capabilities: Mapping[str, bool] | None, fields: Any = None
) -> dict[str, Any] | None:
    """The planner's request profile, or ``None`` (content mode).

    Mirrors :func:`fabulist.state.bts.build_bts_request_profile` one ladder rung
    for rung: no function calling → ``None``; function calling → the tool with
    ``tool_choice: "auto"``; a provider that ENFORCES a forced choice → the same
    tool with ``tool_choice: "required"``. Declaring exactly one tool keeps
    ``"required"`` unambiguous.
    """
    caps = capabilities or {}
    if not caps.get("function_calling"):
        return None
    profile: dict[str, Any] = {"tools": [planner_tool(fields)]}
    profile["tool_choice"] = "required" if caps.get("tool_choice_forced") else "auto"
    return profile


def validate_planner_payload(
    payload: Any,
    fields: Any = None,
    *,
    turn_seq: int = 0,
    created_from: str = "graph",
) -> Plan:
    """Validate a structured planner payload against the field list → :class:`Plan`.

    This is the schema authority the transport boundary
    (:func:`fabulist.state.structured.structured_result`) deliberately leaves to
    the agent: unknown keys are rejected (a model that invents a field would
    otherwise be persisted as preset data), extras' declared types are enforced,
    ``required`` extras must be present, and ``strings`` must carry
    :data:`MIN_STRINGS`-:data:`MAX_STRINGS` non-blank lines. Every fault raises
    ``ValueError`` with the reason; the node treats that as a failed refresh and
    keeps the previous plan (never a failed turn).
    """
    if not isinstance(payload, Mapping):
        raise ValueError(f"planner payload must be an object, got {type(payload).__name__}")
    extras_spec = normalize_field_list(fields)
    allowed = {row["id"]: row for row in extras_spec}
    unknown = sorted(set(payload) - set(CORE_FIELD_IDS) - set(allowed))
    if unknown:
        raise ValueError(f"unknown planner fields: {', '.join(unknown)}")

    raw_strings = payload.get("strings")
    if not isinstance(raw_strings, (list, tuple)):
        raise ValueError("planner `strings` must be an array of strings")
    strings = [" ".join(str(s).split()) for s in raw_strings if str(s).strip()]
    if len(strings) > MAX_STRINGS:
        strings = strings[:MAX_STRINGS]
    if len(strings) < MIN_STRINGS:
        raise ValueError(
            f"planner needs {MIN_STRINGS}-{MAX_STRINGS} strings, got {len(strings)}"
        )

    raw_established = payload.get("established")
    if raw_established is None:
        raw_established = []
    if not isinstance(raw_established, (list, tuple)):
        raise ValueError("planner `established` must be an array of strings")
    established = [" ".join(str(e).split()) for e in raw_established if str(e).strip()]

    extras: dict[str, Any] = {}
    for field_id, row in allowed.items():
        value = payload.get(field_id)
        if value is None:
            if row["required"]:
                raise ValueError(f"required planner field {field_id!r} is missing")
            continue
        extras[field_id] = _checked_extra(field_id, row["type"], value)

    return Plan(
        turn_seq=int(turn_seq),
        created_from=created_from,
        strings=strings,
        datetime=_optional_text(payload.get("datetime"), "datetime"),
        weather=_optional_text(payload.get("weather"), "weather"),
        forecast=_optional_text(payload.get("forecast"), "forecast"),
        established=established,
        extras=extras,
    )


def _checked_extra(field_id: str, field_type: str, value: Any) -> Any:
    """One preset extra value, type-checked against its declared type."""
    if field_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"field {field_id!r} must be a string")
        return value.strip()
    if field_type == "number":
        # ``bool`` is an ``int`` in Python but never a number in a schema.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"field {field_id!r} must be a number")
        return value
    if not isinstance(value, bool):
        raise ValueError(f"field {field_id!r} must be a boolean")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    """A nullable core string field: ``None``/blank -> ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"planner `{name}` must be a string or null")
    text = value.strip()
    return text or None
