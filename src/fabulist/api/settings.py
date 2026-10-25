"""Settings / toggle surface router (W2.11).

The pure default-persona fold + its event names live in
:mod:`fabulist.state.settings` (neutral), and this router imports/re-exports
them; the HTTP surface is a thin transport over that log projection.

W6.3: the single directive ``enabled`` source is the preset's directive rows.
This router still EXPOSES the directive catalog (so the settings drawer keeps
working), but its ``toggles`` map is folded from the PRESET rows, not from a
settings event; ``PATCH {toggles}`` writes those preset rows (the D26
materialize-on-first-edit path), never an inert settings copy. A protected/core
directive is refused with a 422. The remaining settings state is the engine
FEATURE flags, the engine STRING settings (issue #57: the ComfyUI endpoint the
Image Prompter submits to) and the global default-persona pin. The event log
owns the state (D9/D10): this router never mutates a side table — it *derives*
the current flags by folding ``settings_changed`` system events and expresses
every user intent as a new append.

  GET   /api/settings            read the preset-backed toggles + features + values + pin
  PATCH /api/settings {toggles}  write the preset's directive rows (one source)
  PATCH /api/settings {features} append one ``settings_changed`` system event
  PATCH /api/settings {values}   same event, the STRING map (issue #57: comfy.base_url)

Because the log is per-session, global settings anchor to a reserved session id
(:data:`SETTINGS_SESSION_ID`). ``GET /api/settings`` folds that session's
``settings_changed`` feature events in seq order, latest write wins per flag id.

Default persona (pin)
---------------------
The same anchor also carries the **pinned default persona** — a
``default_persona_changed`` system event folded latest-wins (an explicit
``null`` / empty ``persona_id`` clears it). It exists so a chat created with no
persona still gives the narrator a ``{{user}}`` name: the pin is the read-time
fallback whenever a chat has no chat-local ``chat_persona`` (see
:func:`fabulist.personas.registry.persona_for_chat` and the ``{{user}}``
resolver; memory scope does NOT consult the pin — D53), and it is materialized as a
real ``chat_persona`` row at chat creation so the row lands near seq 2 (inside
a later fork's chain window). ``GET /api/settings`` exposes it as
``default_persona_id`` plus ``default_persona_label`` (resolved from the
personas catalog so the UI need not join); ``PATCH /api/settings`` accepts it.
A ``PATCH`` naming no known persona is rejected atomically with a ``404``.

Wiring (orchestrator-owned, one line)::

    from fabulist.api.settings import router
    app.include_router(router)

Like :mod:`fabulist.api.sessions`, the router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log`` (the app factory
stores it there), so no signature change is needed in ``app.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from fabulist.prompts.parts import Directive, default_directives
from fabulist.state.settings import (
    ANTI_INJECTION_FLAG,
    DEFAULT_PERSONA_EVENT,
    SETTINGS_EVENT,
    SETTINGS_SESSION_ID,
    default_persona_id,
    settings_map,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["settings"])

#: ``SETTINGS_SESSION_ID`` / ``SETTINGS_EVENT`` / ``DEFAULT_PERSONA_EVENT`` /
#: ``default_persona_id`` are imported above from
#: :mod:`fabulist.state.settings` and therefore re-exported from this module —
#: the pure folds live in that neutral module so non-API consumers (the prompt
#: builder's Anti Injection read, persona resolution) can read settings without
#: importing the API layer. The route + payload shape are unchanged.

#: Human labels for the preset's TIER 0-4 grouping (frontend groups by tier).
_TIER_LABELS: dict[int, str] = {
    0: "Scene safeguards",
    1: "Direction",
    2: "Narrative",
    3: "Prose style",
    4: "Visual panels",
}


class SettingsPatch(BaseModel):
    """Body of ``PATCH /api/settings``.

    ``toggles`` is a ``{directive_id: enabled}`` map written to the PRESET's
    directive rows (the single ``enabled`` source, W6.3) — an accepted toggle
    is never inert. ``features`` is a ``{feature_id: enabled}`` map (see
    :data:`FEATURE_FLAGS`); ``values`` is a ``{setting_id: value}`` map for the
    STRING settings a boolean flag cannot carry (see :data:`SETTING_VALUES`,
    e.g. ``comfy.base_url``; an empty string CLEARS the override).
    ``default_persona_id`` pins the global default persona (``null`` clears it).
    The pin field is only special when it appears in the body
    (``model_fields_set``): an absent key leaves the pin untouched.
    """

    toggles: dict[str, bool] = Field(default_factory=dict)
    #: Engine FEATURE flags (see :data:`FEATURE_FLAGS`) — same event, separate map.
    features: dict[str, bool] = Field(default_factory=dict)
    #: STRING settings (see :data:`SETTING_VALUES`) — same event, separate map.
    values: dict[str, str] = Field(default_factory=dict)
    default_persona_id: str | None = None


#: Engine FEATURE flags: capabilities the ENGINE gates, as opposed to prompt
#: directives the narrator renders. Kept OUT of the directive catalog so a
#: feature toggle can never leak into the prompt; validated by id on write.
FEATURE_FLAGS: dict[str, str] = {
    "comfy.enabled": "Image generation (ComfyUI)",
    # Issue #39: the Anti Injection toggle (default OFF) — gates the
    # ``## Engine context`` clause in the narrator prompt. A FEATURE flag, not a
    # directive row: the clause's CONTENT stays in the factory artifact / preset
    # row, the flag gates its RENDERING (see ``wiring``'s prompt builder).
    ANTI_INJECTION_FLAG: "Anti Injection",
}

#: The settings id carrying the ComfyUI endpoint override (issue #57). Owned
#: here so the label registry and the reader name ONE id.
COMFY_BASE_URL_KEY = "comfy.base_url"

#: Engine SETTINGS whose value is a STRING, not a boolean: the boolean ``features``
#: fold cannot carry them. Same event/anchor/latest-wins discipline, separate map
#: (``values``). A registry entry is required to write the id (unknown ids are a
#: 404, exactly like feature flags) and supplies the settings surface's label.
SETTING_VALUES: dict[str, str] = {
    # Issue #57: the ComfyUI endpoint the Image Prompter submits to. Empty/unset
    # = the shipped default (``imagegen_api.DEFAULT_BASE_URL``); this map holds
    # the OVERRIDE only, so there is exactly one default in code.
    COMFY_BASE_URL_KEY: "Image Prompter endpoint (ComfyUI URL)",
}


def settings_values(log: "EventLog") -> dict[str, str]:
    """Fold the ``settings_changed`` ``values`` map: ``{setting_id: value}``.

    Latest write wins per id, walking the ``__settings__`` anchor in seq order —
    the same event and discipline as :func:`fabulist.state.settings.settings_map`,
    which cannot carry these because it keeps booleans only. Non-string values
    are ignored. An EMPTY string is a stored value here; readers treat it as
    "cleared" (:func:`setting_value` returns ``None`` for it) so the shipped
    default applies again without a second, deleteable state.
    """
    values: dict[str, str] = {}
    for event in log.get_events(SETTINGS_SESSION_ID):
        if event.type != "system":
            continue
        if event.payload.get("event") != SETTINGS_EVENT:
            continue
        written = event.payload.get("values")
        if not isinstance(written, dict):
            continue
        for setting_id, value in written.items():
            if isinstance(value, str):
                values[setting_id] = value
    return values


def setting_value(log: "EventLog", setting_id: str) -> str | None:
    """The current STRING setting, or ``None`` when unset/cleared.

    Engine-side readers (the imagegen router is the first) call this per use, so
    an accepted write applies on the NEXT request with no restart — the same
    contract the feature flags keep.
    """
    return settings_values(log).get(setting_id) or None


def _clean_setting_values(values: dict[str, str]) -> dict[str, str]:
    """Validate/normalize a ``values`` write body (422 on anything unusable).

    Unknown ids are the caller's 404 (checked by id, like feature flags). Every
    value is stripped (a pasted URL carries whitespace); an empty string is a
    CLEAR and passes through as ``""``. The one URL setting must then be a real
    absolute ``http(s)`` URL with a host — a typo'd endpoint is refused at the
    door rather than silently failing every later submit, and a trailing slash
    is dropped so ``http://host:8188/`` and ``http://host:8188`` are one value.
    """
    cleaned: dict[str, str] = {}
    for setting_id, raw in values.items():
        value = raw.strip()
        if value:
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "reason": f"{setting_id}: must be an absolute http:// or https:// URL",
                        "id": setting_id,
                    },
                )
            value = value.rstrip("/")
        cleaned[setting_id] = value
    return cleaned


def current_features(log: "EventLog") -> dict[str, bool]:
    """Public fold for engine-side consumers: ``{feature_id: enabled}``.

    Absent ids are simply not present (callers apply their own default). Rides
    the same ``settings_changed`` event as the directive toggles, under the
    ``features`` key. The fold itself lives in
    :func:`fabulist.state.settings.settings_map` (neutral module) so non-API
    consumers can read a feature without importing this router.
    """
    enabled, _ = settings_map(log, key="features")
    return enabled


def _persona_label(log: "EventLog", persona_id: str | None) -> str | None:
    """Display name for a pinned persona id, or ``None`` when unknown."""
    if not persona_id:
        return None
    from fabulist.personas.registry import list_personas

    for persona in list_personas(log):
        if persona.persona_id == persona_id:
            return persona.name
    return None


def _known_persona_ids(log: "EventLog") -> set[str]:
    """Ids of the personas currently in the catalog (lazy import, no cycle)."""
    from fabulist.personas.registry import list_personas

    return {persona.persona_id for persona in list_personas(log)}


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _enabled_from_log(
    log: "EventLog", *, key: str = "toggles"
) -> tuple[dict[str, bool], str | None]:
    """Back-compat alias: the neutral ``settings_changed`` fold.

    Every code path goes through :func:`fabulist.state.settings.settings_map`
    (one implementation); this name is kept because the payload builders below
    read it positionally (map + last-write timestamp).
    """
    return settings_map(log, key=key)


def _is_protected(directive: Directive) -> bool:
    """Always False — NOTHING is protected (user decision 2026-10-09).

    The preset's whole customization philosophy is runtime-tunable directives,
    including the distilled cores. The ``protected`` flag stays in the response
    shape (the UI renders locks off it) but is always False; this function is the
    single point to re-protect later if ever wanted. W6.3 briefly re-enabled the
    core-five set and the owner reverted it — do not reintroduce it silently.
    """
    return False


def _definition(directive: Directive, enabled: bool) -> dict[str, Any]:
    """Project one directive into its UI definition (label/description/state)."""
    return {
        "id": directive.id,
        "label": directive.name,
        "description": directive.description or "",
        "enabled": enabled,
        "protected": _is_protected(directive),
        "core": directive.core,
        "tier": directive.tier,
        "priority": directive.priority,
    }


def _preset_enabled_map(log: "EventLog") -> dict[str, bool]:
    """The preset-backed ``{directive_id: enabled}`` map (the single source).

    Folds the ACTIVE preset's directive rows (``None`` = inherit the code
    catalog -> ``{}``, so every directive keeps its catalog default).
    """
    from fabulist.prompts.preset_store import active_preset, preset_directives

    directives = preset_directives(active_preset(log))
    if not directives:
        return {}
    return {directive.id: directive.enabled for directive in directives}


def _settings_payload(log: "EventLog") -> dict[str, Any]:
    """Build the wire response: preset-backed toggles + features + persona pin.

    ``toggles`` is folded from the ACTIVE preset's directive rows (the single
    ``enabled`` source); ``legacy_toggles`` is the raw pre-W6.3
    ``settings_changed`` fold, kept READ-ONLY as the migration input (it
    disappears once ``migrate_directive_toggles.py`` has folded it).
    """
    enabled_map = _preset_enabled_map(log)
    catalog = default_directives()

    toggles: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    protected: list[str] = []
    core: list[str] = []
    for directive in catalog:
        toggles[directive.id] = _definition(
            directive, enabled_map.get(directive.id, directive.enabled)
        )
        order.append(directive.id)
        if _is_protected(directive):
            protected.append(directive.id)
        if directive.core:
            core.append(directive.id)

    legacy_toggles, _ = _enabled_from_log(log, key="toggles")

    feature_fold, _ = _enabled_from_log(log, key="features")
    features = {
        flag_id: {"id": flag_id, "name": name, "enabled": feature_fold.get(flag_id, False)}
        for flag_id, name in FEATURE_FLAGS.items()
    }

    # STRING settings (issue #57): the OVERRIDE only — ``None`` means "nothing is
    # authored, the shipped default applies", so the surface can show a
    # placeholder instead of a fake value.
    stored_values = settings_values(log)
    values = {
        setting_id: {
            "id": setting_id,
            "name": name,
            "value": stored_values.get(setting_id) or None,
        }
        for setting_id, name in SETTING_VALUES.items()
    }

    pinned = default_persona_id(log)
    return {
        "toggles": toggles,
        # READ-ONLY legacy migration input (pre-W6.3 ``settings_changed`` fold).
        # Deprecated: dropped once the directive-toggles migration has applied.
        "legacy_toggles": dict(legacy_toggles),
        "features": features,
        # STRING settings (issue #57): ``{id: {id, name, value}}`` — ``value`` is
        # the authored override, or None when nothing is stored.
        "values": values,
        # Global default persona pin (may be None): the read-time fallback and
        # the UI's "inherited" indicator both read these two fields.
        "default_persona_id": pinned,
        "default_persona_label": _persona_label(log, pinned),
        "meta": {
            "session": SETTINGS_SESSION_ID,
            "count": len(order),
            "order": order,
            "protected": protected,
            "core": core,
            "source": "fabulist.prompts.parts",
            "tier_labels": _TIER_LABELS,
        },
    }


def _apply_directive_toggles(log: "EventLog", toggles: dict[str, bool]) -> None:
    """Write directive ``enabled`` flags onto the active preset's rows (D26).

    Materializes the code catalog into the preset's directive rows on first
    edit (empty -> :data:`~fabulist.prompts.parts.DEFAULT_DIRECTIVES`), flips
    the requested ids, then appends a superseding ``preset`` event — the one
    ``enabled`` source the narrator reads. The persisted default is seeded first
    when no preset row exists yet, so a toggle write is never dropped.
    """
    from fabulist.prompts.parts import directive_to_row
    from fabulist.prompts.preset_store import (
        DEFAULT_PRESET_ID,
        active_preset,
        append_preset,
        ensure_default_preset,
        get_preset,
        preset_to_json,
    )

    preset = active_preset(log)
    if preset is None:
        ensure_default_preset(log)
        preset = get_preset(log, DEFAULT_PRESET_ID)
    if preset is None:  # pragma: no cover - ensure_default_preset always seeds
        raise HTTPException(status_code=500, detail="no preset to write toggles to")

    rows = [dict(row) for row in preset.directives]
    if not rows:
        rows = [directive_to_row(directive) for directive in default_directives()]
    catalog = {directive.id: directive for directive in default_directives()}
    for ident, value in toggles.items():
        for row in rows:
            if row.get("id") == ident:
                row["enabled"] = value
                break
        else:
            row = directive_to_row(catalog[ident])
            row["enabled"] = value
            rows.append(row)

    payload = preset_to_json(preset)
    payload["directives"] = rows
    append_preset(log, payload)


@router.get("/api/settings")
async def get_settings(request: Request) -> dict:
    """Return the preset-backed toggle catalog, feature flags and persona pin."""
    return _settings_payload(_log_for(request))


@router.patch("/api/settings")
async def update_settings(body: SettingsPatch, request: Request) -> dict:
    """Flip directive toggles / feature flags and/or pin the default persona (D9).

    ``toggles`` writes the active preset's directive rows (the one ``enabled``
    source); ``features`` appends one ``settings_changed`` system event, and so
    does ``values`` (the STRING settings map, issue #57); when
    ``default_persona_id`` appears in the body it appends one
    ``default_persona_changed`` event (``null`` clears the pin). Validation is
    atomic: the whole body is checked (unknown toggle/feature/setting id -> 404,
    protected/core disable -> 422, an unusable URL -> 422, unknown persona ->
    404) before anything is written, so a rejected request leaves the log
    untouched.
    """
    log = _log_for(request)
    catalog = {directive.id: directive for directive in default_directives()}

    unknown = [ident for ident in body.toggles if ident not in catalog]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": f"unknown toggle id(s): {', '.join(sorted(unknown))}",
                "ids": sorted(unknown),
            },
        )

    unknown_features = [ident for ident in body.features if ident not in FEATURE_FLAGS]
    if unknown_features:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": f"unknown feature id(s): {', '.join(sorted(unknown_features))}",
                "ids": sorted(unknown_features),
            },
        )

    unknown_values = [ident for ident in body.values if ident not in SETTING_VALUES]
    if unknown_values:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": f"unknown setting id(s): {', '.join(sorted(unknown_values))}",
                "ids": sorted(unknown_values),
            },
        )
    # A bad VALUE is a 422 raised before anything is appended (atomic).
    values_write = _clean_setting_values(dict(body.values)) if body.values else {}

    # Default-persona pin is atomic with the validation above: an unknown id is
    # rejected with a 404 before anything is appended. A key that is present
    # with value ``null`` is an explicit unpin (not "untouched").
    pin_provided = "default_persona_id" in body.model_fields_set
    if pin_provided and body.default_persona_id is not None:
        if body.default_persona_id not in _known_persona_ids(log):
            raise HTTPException(
                status_code=404,
                detail={
                    "reason": (
                        f"unknown persona id: {body.default_persona_id}"
                    ),
                    "id": body.default_persona_id,
                },
            )

    if body.toggles:
        # W6.3: one source — write the preset's directive rows, not an inert
        # ``settings_changed`` copy the narrator no longer reads.
        _apply_directive_toggles(log, dict(body.toggles))
    if body.features:
        log.append_event(
            SETTINGS_SESSION_ID,
            "system",
            "user",
            {"event": SETTINGS_EVENT, "features": dict(body.features)},
        )
    if values_write:
        log.append_event(
            SETTINGS_SESSION_ID,
            "system",
            "user",
            {"event": SETTINGS_EVENT, "values": values_write},
        )
    if pin_provided:
        log.append_event(
            SETTINGS_SESSION_ID,
            "system",
            "user",
            {
                "event": DEFAULT_PERSONA_EVENT,
                "persona_id": body.default_persona_id,
            },
        )

    return _settings_payload(log)
