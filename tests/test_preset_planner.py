"""W9.20 A2: the preset-carried planner config (cadence / steer / extra fields).

Covers the store half of the planner — the ``Preset.planner`` field, its JSON
round-trip (present AND absent rows), and the two helpers wave B consumes:
:func:`fabulist.prompts.preset_store.planner_config` (the read, defaults
merged) and :func:`~fabulist.prompts.preset_store.normalize_planner_config`
(the write door: normalized config + an error list).

The contract under test is "absent row = defaults": a preset written before
W9.20 must load, render and export byte-identically to today, which is why the
key is only emitted when it differs from the default shape and why
``FACTORY_VERSION`` is not bumped.
"""

from __future__ import annotations

from fabulist.log import EventLog
from fabulist.prompts import preset_store as store

#: A non-default config: cadence + steer + one preset-defined extra field.
CUSTOM_PLANNER = {
    "cadence": 3,
    "steer": "Keep the guild politics simmering.",
    "fields": [
        {
            "id": "reputation",
            "title": "Guild reputation",
            "type": "number",
            "hint": "-100..100, 0 neutral",
            "required": True,
        }
    ],
}


def test_absent_planner_row_reads_as_the_default_config():
    """No ``planner`` key -> the default dict, and the export stays key-free."""
    legacy = store.preset_from_json({"preset_id": "old", "name": "Old"})
    assert legacy.planner == {"cadence": 5, "steer": "", "fields": []}
    # The ONE read waves B/C use merges the defaults, so absent == default dict.
    assert store.planner_config(legacy) == store.planner_config(None)
    assert store.planner_config(legacy) == store.default_planner_config()
    # The dataclass Mapping seam (the D19 ``preset.get(...)`` read) carries it.
    assert legacy.get("planner") == legacy.planner
    assert "planner" not in store.preset_to_json(legacy)


def test_custom_planner_round_trips_through_export_import():
    preset = store.preset_from_json(
        {"preset_id": "gm", "name": "GM", "planner": CUSTOM_PLANNER}
    )
    payload = store.preset_to_json(preset)
    assert payload["planner"] == CUSTOM_PLANNER
    assert store.preset_from_json(payload).planner == CUSTOM_PLANNER
    # Idempotent: re-exporting the imported row emits the same block.
    assert store.preset_to_json(store.preset_from_json(payload))["planner"] == (
        CUSTOM_PLANNER
    )


def test_default_planner_config_round_trips_without_the_key():
    """A row carrying the default shape exports as absent and reloads the same."""
    explicit = store.preset_from_json(
        {
            "preset_id": "p",
            "name": "P",
            "planner": {"cadence": 5, "steer": "", "fields": []},
        }
    )
    payload = store.preset_to_json(explicit)
    assert "planner" not in payload
    assert store.preset_from_json(payload).planner == store.default_planner_config()
    assert store.planner_config(payload) == store.default_planner_config()


def test_old_export_without_planner_key_loads_with_defaults():
    """A pre-W9.20 export blob (no ``planner``) folds to the defaults, unbumped."""
    old_export = {
        "event": "preset",
        "preset_id": "legacy",
        "name": "Legacy",
        "modules": [],
        "flows": {"narrate": []},
        "agent_bindings": {},
        "version": 1,
        "is_default": False,
        "factory_version": 0,
        "surfaces": {},
    }
    loaded = store.preset_from_json(old_export)
    assert loaded.planner == store.default_planner_config()
    assert loaded.version == 1
    assert loaded.factory_version == 0
    assert "planner" not in store.preset_to_json(loaded)


def test_planner_round_trips_through_the_event_log(tmp_path):
    """The persisted path: append -> fold -> export carries the same config."""
    log = EventLog(tmp_path / "planner.db")
    try:
        preset_id = store.append_preset(
            log, {"name": "GM", "planner": CUSTOM_PLANNER}
        )
        row = store.get_preset(log, preset_id)
        assert row is not None
        assert store.planner_config(row) == CUSTOM_PLANNER
        assert store.preset_to_json(row)["planner"] == CUSTOM_PLANNER
    finally:
        log.close()


def test_partial_planner_config_reads_with_the_other_keys_defaulted():
    """Only the authored keys win; everything else is the default (no KeyError)."""
    preset = store.preset_from_json(
        {"preset_id": "p", "name": "P", "planner": {"cadence": 2}}
    )
    assert store.planner_config(preset) == {"cadence": 2, "steer": "", "fields": []}
    # ...and the partial block round-trips as stored, not as a baked default.
    assert store.preset_to_json(preset)["planner"] == {"cadence": 2, "steer": "", "fields": []}


def test_planner_config_returns_a_fresh_copy():
    """A caller mutating the read never reaches the preset or the defaults."""
    preset = store.preset_from_json({"preset_id": "p", "name": "P"})
    config = store.planner_config(preset)
    config["cadence"] = 99
    config["fields"].append({"id": "x"})
    assert store.planner_config(preset) == store.default_planner_config()
    assert store.default_planner_config()["fields"] == []


def test_normalize_planner_config_accepts_valid_extras():
    config, errors = store.normalize_planner_config(CUSTOM_PLANNER)
    assert errors == []
    assert config == CUSTOM_PLANNER


def test_normalize_planner_config_fills_defaults_for_absent_keys():
    config, errors = store.normalize_planner_config({})
    assert errors == []
    assert config == store.default_planner_config()
    # A non-mapping value is the defaults too, and says why.
    config, errors = store.normalize_planner_config("nope")
    assert config == store.default_planner_config()
    assert errors == ["planner must be an object, got str"]


def test_normalize_planner_config_rejects_cadence_below_one():
    config, errors = store.normalize_planner_config({"cadence": 0})
    assert errors == ["cadence must be >= 1, got 0"]
    assert config["cadence"] == 0  # as written; the caller refuses the write
    _, errors = store.normalize_planner_config({"cadence": -2})
    assert errors == ["cadence must be >= 1, got -2"]
    _, errors = store.normalize_planner_config({"cadence": "5"})
    assert errors == ["cadence must be an integer, got '5'"]
    _, errors = store.normalize_planner_config({"cadence": True})
    assert errors == ["cadence must be an integer, got True"]


def test_normalize_planner_config_rejects_duplicate_field_id():
    _, errors = store.normalize_planner_config(
        {
            "fields": [
                {"id": "gold", "type": "number"},
                {"id": "gold", "type": "string"},
            ]
        }
    )
    assert errors == ["fields[1].id duplicates 'gold'"]


def test_normalize_planner_config_rejects_unknown_field_type():
    _, errors = store.normalize_planner_config(
        {"fields": [{"id": "gold", "type": "integer"}]}
    )
    assert errors == [
        "fields[0].type must be one of ['boolean', 'number', 'string'], "
        "got 'integer'"
    ]
    _, errors = store.normalize_planner_config({"fields": [{"id": "gold", "type": ["x"]}]})
    assert errors == [
        "fields[0].type must be one of ['boolean', 'number', 'string'], got ['x']"
    ]


def test_normalize_planner_config_rejects_bad_field_identifier():
    for bad in ("2gold", "has space", "gold-bar", ""):
        _, errors = store.normalize_planner_config({"fields": [{"id": bad}]})
        assert errors == [
            "fields[0].id must be an identifier "
            f"([A-Za-z_][A-Za-z0-9_]*), got {bad!r}"
        ]
    # A missing id is the same refusal, and a non-mapping row is dropped-loudly.
    _, errors = store.normalize_planner_config({"fields": [{"type": "string"}, "row"]})
    assert errors == [
        "fields[0].id must be an identifier ([A-Za-z_][A-Za-z0-9_]*), got None",
        "fields[1] must be an object",
    ]
    # Identifier-safe ids (leading underscore, digits after the first char) pass.
    _, errors = store.normalize_planner_config(
        {"fields": [{"id": "_internal1"}, {"id": "Gold2"}]}
    )
    assert errors == []


def test_normalize_planner_config_rejects_non_boolean_required():
    _, errors = store.normalize_planner_config(
        {"fields": [{"id": "gold", "required": "yes"}]}
    )
    assert errors == ["fields[0].required must be a boolean, got 'yes'"]
    _, errors = store.normalize_planner_config({"fields": "nope"})
    assert errors == ["fields must be a list, got str"]
