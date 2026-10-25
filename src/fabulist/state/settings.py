"""Global settings projectors (log-owned; D9).

Pure folds over the reserved ``__settings__`` anchor session. This module
deliberately lives OUTSIDE :mod:`fabulist.api.settings`: the pinned-default-
persona fold is consumed by non-API modules (persona name resolution, world-chat
materialization, the prompt-variable resolver), and reaching into the API layer
for a log projection is the wrong dependency direction. The router re-exports
these names, so ``GET/PATCH /api/settings`` and its payload shape are unchanged.

Seams:

* :data:`SETTINGS_SESSION_ID` — the reserved anchor session (shared with the
  toggles fold in :mod:`fabulist.api.settings`).
* :data:`SETTINGS_EVENT` / :func:`settings_map` — the generic
  ``settings_changed`` fold (``{id: enabled}``, latest wins per id) both maps
  ride (``toggles`` / ``features``).
* :data:`ANTI_INJECTION_FLAG` / :func:`anti_injection_enabled` — the persisted
  Anti Injection product setting (issue #39; engine FEATURE flag, default
  False). Read per prompt build by :mod:`fabulist.wiring`, so flipping it
  applies to the next turn without a restart.
* :data:`DEFAULT_PERSONA_EVENT` / :func:`default_persona_id` — the
  ``default_persona_changed`` pin (latest wins; ``persona_id: null`` clears).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "ANTI_INJECTION_DEFAULT",
    "ANTI_INJECTION_FLAG",
    "DEFAULT_PERSONA_EVENT",
    "SETTINGS_EVENT",
    "SETTINGS_SESSION_ID",
    "anti_injection_enabled",
    "default_persona_id",
    "settings_map",
]

#: Reserved session id that anchors global settings events. Deliberately not a
#: real chat session; the session list filters it (``__`` prefix).
SETTINGS_SESSION_ID = "__settings__"

#: System-event kind carrying a settings write (latest wins, per flag id).
#: The payload carries one of the two maps :func:`settings_map` folds:
#: ``toggles`` (legacy directive fold, read-only migration input) or
#: ``features`` (engine FEATURE flags).
SETTINGS_EVENT = "settings_changed"

#: System-event kind carrying a pin write for the global default persona
#: (latest wins; ``persona_id: null`` clears it). Folded by
#: :func:`default_persona_id`.
DEFAULT_PERSONA_EVENT = "default_persona_changed"

#: Issue #39: the Anti Injection product setting — an engine FEATURE flag (it
#: gates prompt RENDERING, it is not a directive row). Named "Anti Injection"
#: in the settings surface.
ANTI_INJECTION_FLAG = "anti_injection"

#: Shipped default for :data:`ANTI_INJECTION_FLAG` (owner ruling, issue #39):
#: OFF — the ``## Engine context`` clause is left out of the built prompt; the
#: per-turn engine envelope (its ``<preset_runtime>`` payload) still rides its
#: ONE universal carrier, an ephemeral ``assistant`` message immediately before
#: the final user message (issue #44) — the injection is not gated by this flag.
ANTI_INJECTION_DEFAULT = False


def settings_map(
    log: "EventLog", *, key: str = "toggles"
) -> tuple[dict[str, bool], str | None]:
    """Fold ``settings_changed`` events into ``{id: enabled}`` (latest wins).

    Walks the ``__settings__`` anchor in seq order and keeps the LAST boolean
    written per id under ``key`` (``toggles`` or ``features``); non-boolean
    values are ignored. Unknown ids stay in the map so a future catalog addition
    can surface them. Returns the map plus the timestamp of the most recent
    write (``None`` when never touched).
    """
    enabled: dict[str, bool] = {}
    updated_at: str | None = None
    for event in log.get_events(SETTINGS_SESSION_ID):
        if event.type != "system":
            continue
        if event.payload.get("event") != SETTINGS_EVENT:
            continue
        flags = event.payload.get(key)
        if not isinstance(flags, dict):
            continue
        for flag_id, value in flags.items():
            if isinstance(value, bool):
                enabled[flag_id] = value
        updated_at = event.ts
    return enabled, updated_at


def anti_injection_enabled(log: "EventLog") -> bool:
    """The persisted Anti Injection setting (issue #39), default OFF.

    Latest ``features`` write wins; never written reads as
    :data:`ANTI_INJECTION_DEFAULT`. Pure log fold (D9) so every prompt build —
    and every forked session — sees the same current value.
    """
    flags, _ = settings_map(log, key="features")
    return flags.get(ANTI_INJECTION_FLAG, ANTI_INJECTION_DEFAULT)


def default_persona_id(log: "EventLog") -> str | None:
    """Fold the pinned default persona from the ``__settings__`` anchor.

    Walks ``default_persona_changed`` system events in seq order, latest wins;
    ``None`` when never pinned or explicitly cleared (``persona_id: null``).
    This is a pure log projection (D9) — it does not consult the personas
    catalog, so a pin left behind by a since-deleted persona still folds (the
    read-time fallback tolerates it; the label simply resolves to ``None``).
    """
    persona_id: str | None = None
    for event in log.get_events(SETTINGS_SESSION_ID):
        if event.type != "system":
            continue
        if event.payload.get("event") != DEFAULT_PERSONA_EVENT:
            continue
        value = event.payload.get("persona_id")
        persona_id = value if isinstance(value, str) and value else None
    return persona_id
