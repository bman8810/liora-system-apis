"""Unit tests for zocdoc_new_booking pure helpers (no live APIs)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from liora_tools.scripts.zocdoc_new_booking import (
    SMS_FINGERPRINT,
    SMS_TEMPLATE_BODY,
    JobLock,
    build_correlation_id,
    extract_candidates,
    make_step,
    render_sms,
    step_done,
    _mask_email,
    _mask_phone,
    _redact_error,
)


def test_build_correlation_id_prefers_appointment():
    cid = build_correlation_id("app_ABC-123", mrn="99", appt_date="2026-07-22")
    assert cid == "zocdoc-app_ABC-123"


def test_build_correlation_id_fallback():
    cid = build_correlation_id("", mrn="MRN-1", appt_date="2026-07-22")
    assert cid == "zocdoc-MRN-1-2026-07-22"


def test_extract_candidates_filters():
    now = datetime.now(timezone.utc)
    bookings = {
        "data": {
            "appointments": {
                "appointments": [
                    {
                        "appointmentId": "a1",
                        "patientType": "NEW",
                        "appointmentStatus": "UNCONFIRMED",
                        "bookingTimeUtc": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                        "patient": {"firstName": "A", "lastName": "B"},
                    },
                    {
                        "appointmentId": "a2",
                        "patientType": "NEW",
                        "appointmentStatus": "PATIENT_CANCELLED",
                        "bookingTimeUtc": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                    },
                    {
                        "appointmentId": "a3",
                        "patientType": "EXISTING",
                        "appointmentStatus": "UNCONFIRMED",
                        "bookingTimeUtc": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                    },
                    {
                        "appointmentId": "a4",
                        "patientType": "NEW",
                        "appointmentStatus": "SYNC_CONFIRMED",
                        "bookingTimeUtc": (now - timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
                    },
                ]
            }
        }
    }
    got = extract_candidates(bookings, lookback_minutes=60)
    ids = [g["appointmentId"] for g in got]
    assert ids == ["a1"]  # cancelled, existing, old excluded; SYNC_CONFIRMED would pass if fresh


def test_extract_includes_sync_confirmed_when_fresh():
    now = datetime.now(timezone.utc)
    bookings = {
        "data": {
            "appointments": {
                "appointments": [
                    {
                        "appointmentId": "s1",
                        "patientType": "NEW",
                        "appointmentStatus": "SYNC_CONFIRMED",
                        "bookingTimeUtc": (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                ]
            }
        }
    }
    assert len(extract_candidates(bookings, lookback_minutes=30)) == 1


def test_step_done_by_name_and_number():
    steps = [
        make_step(2, "Sent call office request on ZocDoc", "done"),
        {"step": 4, "action": "Sent Genie SMS via Weave", "status": "done"},
    ]
    assert step_done(steps, "call")
    assert step_done(steps, "sms")
    assert not step_done(steps, "portal")


def test_render_sms_template():
    body = render_sms(SMS_TEMPLATE_BODY, "Alex")
    assert "Hello Alex" in body
    assert SMS_FINGERPRINT in body
    assert "{{FIRST_NAME}}" not in body


def test_phi_masks():
    assert _mask_phone("2125551212").endswith("1212")
    assert "***" in _mask_email("jane.doe@example.com")
    assert "[email]" in _redact_error("fail for jane@x.com")
    assert "[phone]" in _redact_error("dial +1 212-555-9999 now")


def test_job_lock_exclusive(tmp_path):
    path = str(tmp_path / "job.lock")
    a = JobLock(path)
    b = JobLock(path)
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()
