"""Owner-editable BTS attribute catalog (W9.29 §4).

The meters/traits a BTS turn may set are an ASSET: log-owned rows on the
``__entity_catalog__`` anchor, editable with no chat open (D9 / non-negotiable 7),
stored in the decision-bank pattern. These tests pin what the lane must not
break:

* **Seed parity** — an absent/empty asset yields EXACTLY the plan §4 seed list
  (15 rows: the relational/self/scene meters with their ranges, then the traits
  with none), and reading it writes NOTHING to the log (no lazy write, no
  migration — the bank's behaviour).
* **Storage** — replace semantics latest-wins, a malformed stored row is SKIPPED
  with the rest of the catalog intact, and a catalog survives a fresh log read.
* **Validation** — the write door's ``ValueError`` messages, which W4's Studio
  CRUD will surface as a 400.
* **The W2 tool door** — ``row_for``/``meters_only``/``traits_only`` and
  ``range_check``'s accept/boundary/reject/non-numeric answers, with the plan §6
  error-text shape.

No live model, no API, no graph: the catalog is a log read plus pure functions.
"""

from __future__ import annotations

import pytest

from fabulist.log import EventLog
from fabulist.state.entity_catalog import (
    DEFAULT_ROWS,
    ENTITY_CATALOG_ANCHOR,
    ENTITY_CATALOG_EVENT,
    catalog_for,
    default_rows,
    meters_only,
    range_check,
    replace_rows,
    row_for,
    traits_only,
    validate_rows,
)

#: The plan §4 seed, as (key, type, host) — order is the seed order.
_SEED_SHAPE = [
    ("hostility", "meter", "relational"),
    ("trust", "meter", "relational"),
    ("affection", "meter", "relational"),
    ("fear", "meter", "relational"),
    ("respect", "meter", "relational"),
    ("stress", "meter", "self"),
    ("arousal", "meter", "self"),
    ("energy", "meter", "self"),
    ("tension", "meter", "scene"),
    ("mood", "trait", "self"),
    ("weather", "trait", "scene"),
    ("tone", "trait", "scene"),
    ("forecast", "trait", "scene"),
    ("irony", "trait", "scene"),
    ("genre", "trait", "scene"),
]

CUSTOM_ROWS = [
    {
        "key": "suspicion",
        "label": "Suspicion",
        "type": "meter",
        "host": "relational",
        "range": [0, 20],
        "description": "How sure they are that something is being hidden.",
    },
    {
        "key": "posture",
        "label": "Posture",
        "type": "trait",
        "host": "self",
        "description": "How they are carrying themselves.",
    },
]


def _append_payload(log: EventLog, rows) -> None:
    """Append a raw catalog payload, bypassing the validating write door.

    A hand-edited / older-build row is exactly what the read path must tolerate,
    so the tolerance tests cannot go through :func:`replace_rows`.
    """
    log.append_event(
        ENTITY_CATALOG_ANCHOR,
        "system",
        "user",
        {"event": ENTITY_CATALOG_EVENT, "rows": rows},
    )


# --- (a) the lazy seed: the code rows, and nothing written ----------------


def test_an_empty_anchor_reads_as_the_code_seed(log):
    rows = catalog_for(log)
    assert [r["key"] for r in rows] == [key for key, _t, _h in _SEED_SHAPE]
    assert rows == default_rows()
    assert catalog_for(None) == default_rows()


def test_the_seed_matches_the_plan_shape(log):
    by_key = {row["key"]: row for row in catalog_for(log)}
    assert len(by_key) == 15
    for key, row_type, host in _SEED_SHAPE:
        row = by_key[key]
        assert row["type"] == row_type
        assert row["host"] == host
        assert row["label"].strip()
        assert row["description"].strip()
        if row_type == "meter":
            assert set(row) == {
                "key", "label", "type", "host", "range", "description"
            }
        else:
            assert set(row) == {"key", "label", "type", "host", "description"}
    # Meters carry an ascending range; the one scene meter is 0–10 (today's line).
    assert by_key["hostility"]["range"] == [0, 100]
    assert by_key["tension"]["range"] == [0, 10]
    assert [r["key"] for r in meters_only(catalog_for(log))] == [
        "hostility", "trust", "affection", "fear", "respect",
        "stress", "arousal", "energy", "tension",
    ]
    assert [r["key"] for r in traits_only(catalog_for(log))] == [
        "mood", "weather", "tone", "forecast", "irony", "genre",
    ]


def test_the_seed_gives_genre_a_home_as_a_scene_trait():
    """W9.32: the genre register is a SCENE trait, never a being's own value."""
    assert row_for("genre") == {
        "key": "genre",
        "label": "Genre",
        "type": "trait",
        "host": "scene",
        "description": "The story's current genre register.",
    }
    assert DEFAULT_ROWS[-1]["key"] == "genre"
    ok, value, message = range_check("genre", "gothic romance")
    assert (ok, value) == (False, None)
    assert message == "genre is a free-text trait — it takes text, not a number"


def test_the_lazy_seed_writes_nothing_and_re_reads_identically(log):
    """Seeding is READ-ONLY (the bank's pattern): no append, no migration."""
    first = catalog_for(log)
    second = catalog_for(log)
    assert first == second
    assert log.get_events(ENTITY_CATALOG_ANCHOR) == []


def test_the_seed_constants_are_not_aliased_by_readers(log):
    """A caller may edit what it got back; the module constants stay intact."""
    row = row_for("hostility", catalog_for(log))
    assert row is not None
    row["range"].append(0)
    row["label"] = "clobbered"
    assert row_for("hostility")["range"] == [0, 100]
    assert row_for("hostility")["label"] == "Hostility"
    assert [r["key"] for r in DEFAULT_ROWS][0] == "hostility"


# --- (b) storage: replace semantics, latest wins -------------------------


def test_a_saved_catalog_is_what_the_engine_reads(log):
    replace_rows(log, CUSTOM_ROWS)
    events = log.get_events(ENTITY_CATALOG_ANCHOR)
    assert [e.payload["event"] for e in events] == [ENTITY_CATALOG_EVENT]
    assert events[0].type == "system"
    assert catalog_for(log) == CUSTOM_ROWS


def test_a_second_save_replaces_the_whole_catalog(log):
    replace_rows(log, CUSTOM_ROWS)
    replacement = [
        {
            "key": "tension",
            "label": "Tension",
            "type": "meter",
            "host": "scene",
            "range": [0, 3],
            "description": "Tighter than the seed says.",
        }
    ]
    replace_rows(log, replacement)
    assert catalog_for(log) == replacement
    # Append-only (D9): the superseded catalog stays queryable.
    stored = log.get_events(ENTITY_CATALOG_ANCHOR)
    assert len(stored) == 2
    assert [r["key"] for r in stored[0].payload["rows"]] == ["suspicion", "posture"]


def test_the_catalog_persists_across_a_fresh_log_read(log):
    replace_rows(log, CUSTOM_ROWS)
    reopened = EventLog(log.path)
    try:
        assert catalog_for(reopened) == CUSTOM_ROWS
    finally:
        reopened.close()


def test_a_malformed_stored_row_is_skipped_and_the_rest_stands(log):
    """Hand-edited data never blanks the catalog (reads tolerate, D9)."""
    _append_payload(
        log,
        [
            CUSTOM_ROWS[0],
            {"key": "Not A Slug", "label": "Broken", "type": "meter",
             "host": "self", "range": [0, 10], "description": "x"},
            CUSTOM_ROWS[1],
        ],
    )
    assert [r["key"] for r in catalog_for(log)] == ["suspicion", "posture"]


def test_a_payload_with_no_usable_row_falls_back_to_the_seed(log):
    _append_payload(log, [{"key": "nope"}, "not even an object"])
    assert catalog_for(log) == default_rows()


def test_a_non_list_rows_payload_reads_as_the_seed(log):
    # Read tolerance covers a HAND-EDITED or pre-gate row: the append gate now
    # refuses a non-list ``rows`` (schema_gate's entity family, W9.29 §5), so the
    # malformed row is injected the only way it can exist in a real log — below
    # the write door — and the read must still fall back to the seed.
    from fabulist.log.events import Event

    class _StoredLog:
        def __init__(self, event):
            self._event = event

        def get_chain_events(self, _session_id):
            return [self._event]

    stored = Event(
        id=1,
        ts="2026-10-21T00:00:00+00:00",
        schema_version=1,
        session_id=ENTITY_CATALOG_ANCHOR,
        seq=1,
        type="system",
        actor="user",
        payload={"event": ENTITY_CATALOG_EVENT, "rows": {"key": "hostility"}},
    )
    assert catalog_for(_StoredLog(stored)) == default_rows()


def test_an_unrelated_system_event_on_the_anchor_is_ignored(log):
    log.append_event(ENTITY_CATALOG_ANCHOR, "system", "user", {"event": "world"})
    assert catalog_for(log) == default_rows()


def test_a_log_without_get_events_reads_as_the_seed():
    """The ``LogPort`` test seam: an object with no ``get_events`` is no asset."""
    class _Port:
        pass

    assert catalog_for(_Port()) == default_rows()


# --- (c) validation: the write door (W4's ValueError -> 400) -------------


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param({"rows": []}, id="not-a-list"),
        pytest.param([{"key": "hostility"}], id="missing-fields"),
        pytest.param([{**CUSTOM_ROWS[0], "key": "Hostility"}], id="upper-case-key"),
        pytest.param(
            [{**CUSTOM_ROWS[0], "key": "sexual tension"}], id="key-with-space"
        ),
        pytest.param([{**CUSTOM_ROWS[0], "key": "2fast"}], id="key-leading-digit"),
        pytest.param([{**CUSTOM_ROWS[0], "key": ""}], id="empty-key"),
        pytest.param([CUSTOM_ROWS[0], dict(CUSTOM_ROWS[0])], id="duplicate-key"),
        pytest.param([{**CUSTOM_ROWS[0], "type": "score"}], id="bad-type"),
        pytest.param([{**CUSTOM_ROWS[0], "host": "party"}], id="bad-host"),
        pytest.param([{**CUSTOM_ROWS[0], "range": None}], id="meter-without-range"),
        pytest.param([{**CUSTOM_ROWS[0], "range": [0]}], id="range-too-short"),
        pytest.param([{**CUSTOM_ROWS[0], "range": [100, 0]}], id="range-descending"),
        pytest.param([{**CUSTOM_ROWS[0], "range": [0, 0]}], id="range-flat"),
        pytest.param([{**CUSTOM_ROWS[0], "range": [0, "100"]}], id="range-not-int"),
        pytest.param([{**CUSTOM_ROWS[0], "range": [0, True]}], id="range-bool"),
        pytest.param([{**CUSTOM_ROWS[1], "range": [0, 10]}], id="trait-with-range"),
        pytest.param([{**CUSTOM_ROWS[0], "label": "  "}], id="empty-label"),
        pytest.param([{**CUSTOM_ROWS[0], "description": ""}], id="empty-description"),
        pytest.param(["not even an object"], id="row-not-object"),
    ],
)
def test_a_malformed_row_is_refused_with_a_value_error(log, rows):
    with pytest.raises(ValueError):
        replace_rows(log, rows)
    # Nothing was written by any refusal.
    assert log.get_events(ENTITY_CATALOG_ANCHOR) == []


def test_a_refusal_names_the_offending_row():
    with pytest.raises(ValueError, match=r"rows\[1\]: duplicate key 'suspicion'"):
        validate_rows([CUSTOM_ROWS[0], dict(CUSTOM_ROWS[0])])
    with pytest.raises(ValueError, match=r"rows\[0\].*'range' must ascend"):
        validate_rows([{**CUSTOM_ROWS[0], "range": [9, 1]}])


def test_validation_returns_the_canonical_row(log):
    """A saved row round-trips its canonical shape (field order + no extras)."""
    [row] = validate_rows([{**CUSTOM_ROWS[0], "note": "dropped"}])
    assert row == CUSTOM_ROWS[0]
    assert list(row) == ["key", "label", "type", "host", "range", "description"]
    # A trait's range stays absent rather than becoming a null.
    [trait] = validate_rows([CUSTOM_ROWS[1]])
    assert "range" not in trait


def test_an_empty_catalog_save_restores_the_seed(log):
    """Empty means "the code rows apply" — the bank's documented fallback."""
    replace_rows(log, [])
    assert catalog_for(log) == default_rows()


# --- (d) host round-trip: relational / self / scene ----------------------


def test_each_host_round_trips_through_the_store(log):
    rows = [
        {
            "key": "suspicion",
            "label": "Suspicion",
            "type": "meter",
            "host": "relational",
            "range": [0, 20],
            "description": "Toward a specific person.",
        },
        {
            "key": "focus",
            "label": "Focus",
            "type": "meter",
            "host": "self",
            "range": [0, 5],
            "description": "Their own attention.",
        },
        {
            "key": "noise",
            "label": "Noise",
            "type": "meter",
            "host": "scene",
            "range": [0, 9],
            "description": "The room's own level.",
        },
        {
            "key": "posture",
            "label": "Posture",
            "type": "trait",
            "host": "self",
            "description": "How they carry themselves.",
        },
    ]
    replace_rows(log, rows)
    stored = catalog_for(log)
    assert [r["host"] for r in stored] == ["relational", "self", "scene", "self"]
    assert [r["key"] for r in meters_only(stored)] == ["suspicion", "focus", "noise"]
    assert [r["key"] for r in traits_only(stored)] == ["posture"]
    assert row_for("noise", stored)["range"] == [0, 9]


def test_row_for_returns_none_for_an_unknown_key(log):
    assert row_for("hostilty", catalog_for(log)) is None
    assert row_for("hostility", catalog_for(log))["host"] == "relational"


# --- (e) range_check: the W2 tool door -----------------------------------


def test_range_check_accepts_and_coerces_a_number(log):
    catalog = catalog_for(log)
    assert range_check("hostility", 42, catalog) == (True, 42, "")
    assert range_check("hostility", "42", catalog) == (True, 42, "")
    assert range_check("hostility", 42.0, catalog) == (True, 42, "")
    assert range_check("tension", 7, catalog) == (True, 7, "")


def test_range_check_accepts_both_boundaries(log):
    catalog = catalog_for(log)
    assert range_check("hostility", 0, catalog) == (True, 0, "")
    assert range_check("hostility", 100, catalog) == (True, 100, "")
    assert range_check("tension", 0, catalog) == (True, 0, "")
    assert range_check("tension", 10, catalog) == (True, 10, "")


def test_range_check_refuses_out_of_range_in_the_plan_error_shape(log):
    ok, value, message = range_check("hostility", 142, catalog_for(log))
    assert (ok, value) == (False, None)
    assert message == "hostility expects an integer in 0–100, got 142"
    ok, value, message = range_check("tension", 11, catalog_for(log))
    assert (ok, value) == (False, None)
    assert message == "tension expects an integer in 0–10, got 11"


def test_range_check_refuses_a_non_numeric_value(log):
    catalog = catalog_for(log)
    for bad in ("a lot", "", None, [], 42.5, True, False):
        ok, value, message = range_check("hostility", bad, catalog)
        assert (ok, value) == (False, None), bad
        assert "expects an integer in 0–100, got " in message
    assert range_check("hostility", "a lot", catalog)[2] == (
        "hostility expects an integer in 0–100, got 'a lot'"
    )


def test_range_check_a_custom_range_is_honoured(log):
    replace_rows(log, CUSTOM_ROWS)
    assert range_check("suspicion", 20, catalog_for(log)) == (True, 20, "")
    ok, _value, message = range_check("suspicion", 21, catalog_for(log))
    assert ok is False
    assert message == "suspicion expects an integer in 0–20, got 21"


def test_range_check_refuses_a_trait(log):
    ok, value, message = range_check("mood", "wary", catalog_for(log))
    assert (ok, value) == (False, None)
    assert message == "mood is a free-text trait — it takes text, not a number"


def test_range_check_an_unknown_key_names_the_catalog(log):
    ok, value, message = range_check("hostilty", 42, catalog_for(log))
    assert (ok, value) == (False, None)
    assert message.startswith("unknown attribute 'hostilty' — catalog keys: ")
    for key in ("hostility", "tension", "mood", "irony"):
        assert key in message
    assert message.endswith("(the owner edits this list in the Studio)")


def test_range_check_runs_over_the_code_seed_without_a_catalog():
    """The no-log case: the lookup defaults to :data:`DEFAULT_ROWS`."""
    assert range_check("hostility", 100) == (True, 100, "")
    assert range_check("hostilty", 1)[0] is False
