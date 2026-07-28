"""Unit tests for Genie remote-refill 12-month visit lapse policy (pure logic)."""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

import pytest

from liora_tools.modmed.refill_lapse_policy import (
    DEFAULT_REFILL_LAPSE_DAYS,
    RefillLapseDecision,
    evaluate_refill_visit_lapse,
    is_refill_lapsed,
    last_completed_visit_from_appointments,
)


FIXED_NOW = date(2026, 7, 28)


# ── evaluate_refill_visit_lapse ──────────────────────────────────────────────


def test_no_visit_not_allowed_offer_booking():
    d = evaluate_refill_visit_lapse(None, now=FIXED_NOW)
    assert d.allowed is False
    assert d.reason == "no_visit"
    assert d.offer_booking is True
    assert d.last_completed_visit_at is None
    assert d.age_days is None
    assert d.threshold_days == DEFAULT_REFILL_LAPSE_DAYS
    assert d.now == FIXED_NOW
    assert isinstance(d.message, str) and d.message


def test_visit_yesterday_allowed():
    visit = FIXED_NOW - timedelta(days=1)
    d = evaluate_refill_visit_lapse(visit, now=FIXED_NOW)
    assert d.allowed is True
    assert d.reason == "in_window"
    assert d.offer_booking is False
    assert d.age_days == 1
    assert d.last_completed_visit_at == visit


def test_visit_exactly_threshold_days_ago_allowed_boundary():
    """Exactly threshold_days ago is still in window (not older than)."""
    visit = FIXED_NOW - timedelta(days=DEFAULT_REFILL_LAPSE_DAYS)
    d = evaluate_refill_visit_lapse(visit, now=FIXED_NOW)
    assert d.allowed is True
    assert d.reason == "in_window"
    assert d.offer_booking is False
    assert d.age_days == DEFAULT_REFILL_LAPSE_DAYS


def test_visit_threshold_plus_one_lapsed_offer_booking():
    visit = FIXED_NOW - timedelta(days=DEFAULT_REFILL_LAPSE_DAYS + 1)
    d = evaluate_refill_visit_lapse(visit, now=FIXED_NOW)
    assert d.allowed is False
    assert d.reason == "lapsed"
    assert d.offer_booking is True
    assert d.age_days == DEFAULT_REFILL_LAPSE_DAYS + 1


def test_custom_threshold_days():
    visit = FIXED_NOW - timedelta(days=30)
    d = evaluate_refill_visit_lapse(visit, now=FIXED_NOW, threshold_days=30)
    assert d.allowed is True
    assert d.reason == "in_window"
    assert d.threshold_days == 30
    assert d.age_days == 30

    d2 = evaluate_refill_visit_lapse(visit, now=FIXED_NOW, threshold_days=29)
    assert d2.allowed is False
    assert d2.reason == "lapsed"
    assert d2.offer_booking is True


def test_iso_string_inputs():
    d = evaluate_refill_visit_lapse(
        "2026-07-27",
        now="2026-07-28",
    )
    assert d.allowed is True
    assert d.reason == "in_window"
    assert d.age_days == 1

    d2 = evaluate_refill_visit_lapse(
        "2025-07-28T10:30:00",
        now="2026-07-28T15:00:00",
    )
    assert d2.age_days == 365
    assert d2.allowed is True
    assert d2.reason == "in_window"


def test_datetime_with_tz_date_only_compare():
    """Timezone stripped; compare calendar dates only."""
    # Visit evening UTC on day D; now morning US/Eastern next calendar day UTC-wise
    # but both convert via .date() after normalizing.
    visit = datetime(2026, 7, 27, 23, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 28, 1, 0, 0, tzinfo=timezone.utc)
    d = evaluate_refill_visit_lapse(visit, now=now)
    assert d.age_days == 1
    assert d.allowed is True
    assert d.reason == "in_window"

    # Same calendar date in different zones still age 0
    visit2 = datetime(2026, 7, 28, 8, 0, 0, tzinfo=timezone.utc)
    now2 = datetime(2026, 7, 28, 20, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    d2 = evaluate_refill_visit_lapse(visit2, now=now2)
    # now2.date() is 2026-07-28 (local), visit2.date() is 2026-07-28
    assert d2.age_days == 0
    assert d2.allowed is True


def test_is_refill_lapsed_mirrors_evaluate():
    assert is_refill_lapsed(None, now=FIXED_NOW) is True
    assert is_refill_lapsed(FIXED_NOW - timedelta(days=1), now=FIXED_NOW) is False
    assert is_refill_lapsed(
        FIXED_NOW - timedelta(days=DEFAULT_REFILL_LAPSE_DAYS), now=FIXED_NOW
    ) is False
    assert is_refill_lapsed(
        FIXED_NOW - timedelta(days=DEFAULT_REFILL_LAPSE_DAYS + 1), now=FIXED_NOW
    ) is True

    for visit in (
        None,
        FIXED_NOW - timedelta(days=1),
        FIXED_NOW - timedelta(days=DEFAULT_REFILL_LAPSE_DAYS),
        FIXED_NOW - timedelta(days=DEFAULT_REFILL_LAPSE_DAYS + 1),
    ):
        decision = evaluate_refill_visit_lapse(visit, now=FIXED_NOW)
        assert is_refill_lapsed(visit, now=FIXED_NOW) is (not decision.allowed)


def test_invalid_threshold_raises():
    with pytest.raises(ValueError):
        evaluate_refill_visit_lapse(FIXED_NOW, now=FIXED_NOW, threshold_days=0)
    with pytest.raises(ValueError):
        evaluate_refill_visit_lapse(FIXED_NOW, now=FIXED_NOW, threshold_days=-5)
    with pytest.raises(ValueError):
        is_refill_lapsed(FIXED_NOW, now=FIXED_NOW, threshold_days=0)


def test_decision_is_frozen_dataclass():
    d = evaluate_refill_visit_lapse(FIXED_NOW, now=FIXED_NOW)
    assert isinstance(d, RefillLapseDecision)
    with pytest.raises(Exception):
        d.allowed = False  # type: ignore[misc]


# ── last_completed_visit_from_appointments ───────────────────────────────────


def test_appointments_helper_picks_latest_completed_ignores_cancelled():
    appts = [
        {
            "status": "CANCELLED",
            "scheduledStartDate": "2026-07-20T10:00:00",
        },
        {
            "status": "checked_out",
            "scheduledStartDate": "2026-06-01T10:00:00",
        },
        {
            "status": "COMPLETED",
            "start_date": "2026-07-15T14:00:00",
        },
        {
            "status": "CONFIRMED",
            "start": "2026-07-25T09:00:00",
        },
        {
            "status": "Completed",
            "start": "2026-05-01T09:00:00",
        },
    ]
    latest = last_completed_visit_from_appointments(appts)
    assert latest is not None
    # Most recent completed is 2026-07-15
    if isinstance(latest, datetime):
        assert latest.date() == date(2026, 7, 15)
    else:
        assert latest == date(2026, 7, 15)


def test_appointments_helper_empty_or_none_completed():
    assert last_completed_visit_from_appointments([]) is None
    assert last_completed_visit_from_appointments(
        [{"status": "CANCELLED", "scheduledStartDate": "2026-01-01"}]
    ) is None
