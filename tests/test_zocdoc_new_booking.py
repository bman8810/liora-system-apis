"""Unit tests for zocdoc_new_booking pure helpers (no live APIs)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from liora_tools.genies_bottle.client import GenieBottleClient
from liora_tools.scripts.zocdoc_new_booking import (
    SMS_FINGERPRINT,
    SMS_TEMPLATE_BODY,
    JobLock,
    build_correlation_id,
    extract_candidates,
    make_step,
    process_one,
    render_sms,
    report_failure,
    step_done,
    validate_correlation_id,
    _mask_email,
    _mask_phone,
    _redact_error,
    _safe_log_activity,
)


def test_build_correlation_id_prefers_appointment():
    cid = build_correlation_id("app_ABC-123", mrn="99", appt_date="2026-07-22")
    assert cid == "zocdoc-app_ABC-123"


def test_build_correlation_id_fallback():
    cid = build_correlation_id("", mrn="MRN-1", appt_date="2026-07-22")
    assert cid == "zocdoc-MRN-1-2026-07-22"


def test_validate_correlation_id_accepts_good():
    assert validate_correlation_id("zocdoc-app_ABC") == "zocdoc-app_ABC"
    assert validate_correlation_id("  zocdoc-x  ") == "zocdoc-x"


def test_validate_correlation_id_rejects_empty():
    with pytest.raises(ValueError, match="blank|required"):
        validate_correlation_id("")
    with pytest.raises(ValueError, match="blank|required"):
        validate_correlation_id("   ")
    with pytest.raises(ValueError, match="required"):
        validate_correlation_id(None)


def test_validate_correlation_id_rejects_non_zocdoc_prefix():
    with pytest.raises(ValueError, match="zocdoc-"):
        validate_correlation_id("booking-app_123")
    with pytest.raises(ValueError, match="too short"):
        validate_correlation_id("zocdoc")  # no hyphen+rest / len < 8


def test_safe_log_activity_payload_no_phi():
    gb = MagicMock()
    _safe_log_activity(
        gb, "weave_sms", "sms done",
        correlation_id="zocdoc-app_1", step="weave_sms", status="done",
        extra={"smsId": "s1", "threadId": "t1", "phone": "+12125551212", "body": "secret"},
    )
    gb.log_activity.assert_called_once()
    kwargs = gb.log_activity.call_args
    payload = kwargs.kwargs.get("payload") or kwargs[1].get("payload")
    assert payload == {
        "correlation_id": "zocdoc-app_1",
        "step": "weave_sms",
        "status": "done",
        "smsId": "s1",
        "threadId": "t1",
    }
    assert "phone" not in payload
    assert "body" not in payload


def test_report_process_rejects_blank_correlation_id():
    client = GenieBottleClient.__new__(GenieBottleClient)
    with pytest.raises(ValueError, match="correlation_id"):
        client.report_process("zocdoc-new-booking", "running", correlation_id="  ")


def test_process_one_failure_reports_with_steps(monkeypatch):
    """process_one reports failure WITH steps and returns error (no raise)."""
    appt = {
        "appointmentId": "app_fail_1",
        "patientType": "NEW",
        "mrn": "M1",
        "appointmentTimeUtc": "2026-07-22T12:00:00Z",
        "patient": {"firstName": "Pat", "lastName": "Ient"},
        "requestId": None,
    }
    zoc = MagicMock()
    zoc.get_booking.return_value = {
        "data": {"appointmentDetails": {"patient": {}, "requestId": None}}
    }
    weave = MagicMock()
    ema = MagicMock()
    ema.search_patients.return_value = []
    gb = MagicMock()
    gb.query_executions.return_value = []
    gb.report_process.return_value = {"id": "x", "status": "running"}

    result = process_one(
        appt=appt,
        zoc=zoc,
        weave=weave,
        ema=ema,
        gb=gb,
        sms_template=SMS_TEMPLATE_BODY,
        dry_run=False,
        force=False,
    )
    assert result == "error"
    # failed report should include steps
    failed_calls = [
        c for c in gb.report_process.call_args_list
        if (c.args[1] if len(c.args) > 1 else c.kwargs.get("status")) == "failed"
        or c.kwargs.get("status") == "failed"
    ]
    assert failed_calls, "expected report_process(..., status='failed')"
    failed_kw = failed_calls[0].kwargs
    assert failed_kw.get("steps"), "failure report must include steps"
    assert failed_kw.get("correlation_id") == "zocdoc-app_fail_1"
    gb.request_feedback.assert_called()


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
