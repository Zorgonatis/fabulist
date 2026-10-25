"""Pins for the W9.32 R4 clock-proposal helpers (pure time helpers).

The rules table (`advance`) stays the default mover — these helpers only read,
judge and format. Every guardrail in the plan's §3.4 list is pinned here with
the refusal text the tracker will actually see.
"""

from __future__ import annotations

from datetime import date

import pytest

from fabulist.graph.clock import (
    EPOCH_DATE,
    MAX_PROPOSAL_MINUTES,
    format_instant,
    parse_instant,
    parse_plan_instant,
    validate_time_proposal,
)

#: The epoch date IS a Monday, which makes every hand-checked instant below easy
#: to read: day 1 = Monday June 02 2025, day 2 = Tuesday June 03 2025.
MONDAY = "Monday, June 02, 2025"
TUESDAY = "Tuesday, June 03, 2025"


# -- parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2025-06-02 14:35", (date(2025, 6, 2), 14 * 60 + 35)),
        ("Monday, June 02, 2025 at 14:35", (date(2025, 6, 2), 14 * 60 + 35)),
        ("2025-06-03T07:00:00", (date(2025, 6, 3), 7 * 60)),
        # a bare time of day: the caller supplies the date (see the verdict fn)
        ("14:35", (EPOCH_DATE, 14 * 60 + 35)),
        # surrounding whitespace and a trailing full stop are tolerated
        ("  2025-06-03   07:00.  ", (date(2025, 6, 3), 7 * 60)),
    ],
)
def test_parse_instant_reads_iso_date_format_and_bare_time(text, expected):
    assert parse_instant(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "evening",
        "2025-06-02",  # a date with no time of day is not an instant
        "2025-13-45 10:00",  # a stated date that does not exist is not an instant
        "February 30, 2025 at 10:00",
        "24:00",
        "14:75",
        "+2h",  # a delta is a proposal form, not an instant
        None,
        12345,
    ],
)
def test_parse_instant_returns_none_for_junk_and_never_raises(text):
    assert parse_instant(text) is None


def test_format_instant_is_the_canonical_iso_absolute():
    assert format_instant(date(2025, 6, 2), 14 * 60 + 35) == "2025-06-02 14:35"


def test_parse_plan_instant_seeds_time_date_and_day():
    # `day` is the clock's own facts-block index — 1-based days since
    # EPOCH_DATE, the number `advance` folds — so the epoch date is day 1.
    assert parse_plan_instant("Monday, June 02, 2025 at 14:35") == ("14:35", MONDAY, 1)


def test_parse_plan_instant_tolerates_weekday_name_and_missing_weekday():
    # the weekday is never trusted: a wrong name, a missing name, or no name at
    # all all parse to the same calendar date.
    assert parse_plan_instant("Tuesday, June 03, 2025 at 07:00") == ("07:00", TUESDAY, 2)
    assert parse_plan_instant("June 03, 2025 at 07:00") == ("07:00", TUESDAY, 2)
    assert parse_plan_instant("2025-06-03 07:00") == ("07:00", TUESDAY, 2)


def test_parse_plan_instant_without_a_date_seeds_the_epoch_date():
    # what the clock would have defaulted to anyway (advance honors EPOCH_DATE).
    assert parse_plan_instant("9:15 in the morning") == ("09:15", MONDAY, 1)


def test_parse_plan_instant_without_a_time_seeds_nothing():
    assert parse_plan_instant("evening") is None
    assert parse_plan_instant("") is None


# -- guardrail 1: never earlier ---------------------------------------------


def test_earlier_time_of_day_on_the_same_date_is_refused_quoting_both_instants():
    verdict = validate_time_proposal("14:35", MONDAY, "09:00", evidence="we talked")

    assert verdict.ok is False
    assert "2025-06-02 09:00" in verdict.reason
    assert "2025-06-02 14:35" in verdict.reason
    # a refusal never moves the clock
    assert (verdict.time, verdict.date, verdict.day) == ("14:35", MONDAY, 1)
    assert verdict.advanced_minutes == 0


def test_negative_delta_follows_the_same_never_earlier_rule():
    verdict = validate_time_proposal("14:35", MONDAY, "-30", evidence="we rewound")

    assert verdict.ok is False
    assert "before the current instant" in verdict.reason


def test_earlier_time_of_day_on_an_explicitly_later_date_is_legal():
    # 23:00 -> 07:00 is judged on the resulting instant: with no sleep claimed the
    # bare form is backwards, while the dated form is 8 h forward.
    refused = validate_time_proposal("23:00", MONDAY, "07:00", evidence="we argued on the pier")
    assert refused.ok is False
    assert "2025-06-02 07:00" in refused.reason
    assert "2025-06-02 23:00" in refused.reason

    accepted = validate_time_proposal(
        "23:00",
        MONDAY,
        f"{TUESDAY} at 07:00",
        evidence="we slept through the night",
        stated_sleep=True,
    )
    assert accepted.ok is True
    assert accepted.advanced_minutes == 8 * 60
    assert (accepted.time, accepted.date, accepted.day) == ("07:00", TUESDAY, 2)


def test_midnight_crossing_computes_advanced_minutes():
    verdict = validate_time_proposal(
        "23:00", MONDAY, "2025-06-03 00:30", evidence="the bell rang at midnight"
    )

    assert verdict.ok is True
    assert verdict.advanced_minutes == 90
    assert (verdict.time, verdict.date, verdict.day) == ("00:30", TUESDAY, 2)


# -- cross-midnight sleep (a blanket claim before an already-past time) ------
#
# The rules table's own `rest` lands on 07:00 the next morning, so a stated sleep
# with an earlier time of day and no stated date means tomorrow, not backwards.


def test_sleep_with_an_earlier_time_and_no_date_lands_tomorrow():
    verdict = validate_time_proposal(
        "22:00", MONDAY, "07:00", evidence="you sleep until morning", stated_sleep=True
    )

    assert verdict.ok is True
    assert (verdict.time, verdict.date, verdict.day) == ("07:00", TUESDAY, 2)
    assert verdict.advanced_minutes == 9 * 60  # 22:00 -> 07:00 tomorrow
    # the accepted verdict says why the date moved
    assert TUESDAY in verdict.reason


def test_rest_keyword_evidence_alone_lands_the_overnight_time_tomorrow():
    verdict = validate_time_proposal(
        "23:30", MONDAY, "06:15", evidence="we camp and sleep at the roadside"
    )

    assert verdict.ok is True
    assert (verdict.time, verdict.date, verdict.day) == ("06:15", TUESDAY, 2)
    assert verdict.advanced_minutes == 6 * 60 + 45  # 23:30 -> 06:15 tomorrow


def test_earlier_time_without_a_sleep_claim_is_still_backwards():
    verdict = validate_time_proposal("22:00", MONDAY, "07:00", evidence="we argued on the pier")

    assert verdict.ok is False
    assert "never runs backwards" in verdict.reason
    assert (verdict.time, verdict.date, verdict.day) == ("22:00", MONDAY, 1)


def test_sleep_does_not_rescue_a_stated_earlier_date():
    verdict = validate_time_proposal(
        "22:00", MONDAY, "2025-06-01 07:00", evidence="you sleep", stated_sleep=True
    )

    assert verdict.ok is False
    assert "before the current date" in verdict.reason


def test_sleep_does_not_rescue_a_stated_same_day_time():
    verdict = validate_time_proposal(
        "22:00", MONDAY, "2025-06-02 07:00", evidence="you sleep", stated_sleep=True
    )

    assert verdict.ok is False
    assert "never runs backwards" in verdict.reason


def test_sleep_does_not_rescue_a_negative_delta():
    # a delta is elapsed time, never a "tomorrow": no re-reading is possible.
    verdict = validate_time_proposal(
        "22:00", MONDAY, "-60", evidence="you sleep", stated_sleep=True
    )

    assert verdict.ok is False
    assert "never runs backwards" in verdict.reason


# -- guardrail 2: bounded jump ----------------------------------------------


def test_seven_hour_jump_is_refused_quoting_the_bound():
    verdict = validate_time_proposal("08:00", MONDAY, "+7h", evidence="we walked a while")

    assert verdict.ok is False
    assert f"{MAX_PROPOSAL_MINUTES}-minute" in verdict.reason
    assert "sleep: true" in verdict.reason
    assert verdict.advanced_minutes == 0


def test_seven_hour_jump_with_stated_sleep_is_accepted():
    verdict = validate_time_proposal(
        "08:00", MONDAY, "+7h", evidence="we slept off the fever", stated_sleep=True
    )

    assert verdict.ok is True
    assert (verdict.time, verdict.date, verdict.day) == ("15:00", MONDAY, 1)
    assert verdict.advanced_minutes == 7 * 60
    assert verdict.reason == ""


def test_rest_keyword_evidence_clears_the_jump_bound():
    # "sleep"/"camp" are existing `_REST_KEYWORDS`, so the flag is not required
    # when the prose itself says the character rested.
    verdict = validate_time_proposal(
        "20:00", MONDAY, "+8h", evidence="we make camp and sleep until dawn"
    )

    assert verdict.ok is True
    assert verdict.advanced_minutes == 8 * 60


@pytest.mark.parametrize(
    ("text", "minutes", "expected_time"),
    [
        ("+90", 90, "16:05"),
        ("+2h", 120, "16:35"),
        ("2h30", 150, "17:05"),
        ("+2h30m", 150, "17:05"),
    ],
)
def test_delta_forms_are_read_as_minutes_from_the_current_time(text, minutes, expected_time):
    verdict = validate_time_proposal("14:35", MONDAY, text, evidence="the meeting dragged")

    assert verdict.ok is True, verdict.reason
    assert verdict.advanced_minutes == minutes
    assert (verdict.time, verdict.date, verdict.day) == (expected_time, MONDAY, 1)


# -- guardrail 3: date/format consistency and guardrail 4: evidence ----------


def test_junk_target_is_refused_naming_the_accepted_forms():
    verdict = validate_time_proposal("14:35", MONDAY, "when the sun sets", evidence="we waited")

    assert verdict.ok is False
    assert "HH:MM" in verdict.reason
    assert "YYYY-MM-DD HH:MM" in verdict.reason
    assert "+90" in verdict.reason
    assert f"{MONDAY} at 14:35" in verdict.reason


def test_stated_date_before_the_current_date_is_refused():
    verdict = validate_time_proposal("14:35", MONDAY, "2025-06-01 15:00", evidence="we recalled")

    assert verdict.ok is False
    assert "before the current date" in verdict.reason
    assert "Sunday, June 01, 2025" in verdict.reason
    assert MONDAY in verdict.reason


def test_impossible_stated_date_is_refused_rather_than_read_as_today():
    verdict = validate_time_proposal("14:35", MONDAY, "2025-13-45 16:00", evidence="we waited")

    assert verdict.ok is False
    assert "is not a time I can read" in verdict.reason


@pytest.mark.parametrize("evidence", ["", "   ", "\n\t "])
def test_empty_evidence_is_refused(evidence):
    verdict = validate_time_proposal("14:35", MONDAY, "+30", evidence=evidence)

    assert verdict.ok is False
    assert "which prose moved the clock" in verdict.reason


def test_accepted_proposal_reports_the_clock_in_the_apps_stored_formats():
    verdict = validate_time_proposal(
        "14:35", MONDAY, "2025-06-02 16:20", evidence="the courier took an hour"
    )

    assert verdict.ok is True
    assert verdict.time == "16:20"  # HH:MM, exactly what `advance` writes
    assert verdict.date == MONDAY  # DATE_FORMAT, exactly what `advance` writes
    assert verdict.day == 1
    assert verdict.advanced_minutes == 105
