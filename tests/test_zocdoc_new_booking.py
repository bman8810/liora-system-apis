"""Unit tests for zocdoc_new_booking pure helpers (no live APIs)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from liora_tools.genies_bottle.client import GenieBottleClient
from liora_tools.scripts.zocdoc_new_booking import (
    SMS_FINGERPRINT,
    SMS_TEMPLATE_BODY,
    SMS_TEMPLATE_ID,
    SMS_TEMPLATE_NAME,
    JobLock,
    JobStepError,
    StepLedger,
    booking_call_already_requested,
    build_correlation_id,
    build_sms_body,
    extract_candidates,
    make_step,
    merge_step_lists,
    process_one,
    render_sms,
    report_failure,
    step_done,
    validate_correlation_id,
    validate_sms_template,
    _mask_email,
    _mask_phone,
    _patient_gb_payload,
    _redact_error,
    _safe_log_activity,
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


def test_build_sms_ok():
    body = build_sms_body(SMS_TEMPLATE_BODY, first_name="Alex")
    assert "Hello Alex" in body
    assert SMS_FINGERPRINT in body
    assert "{{" not in body
    assert "}}" not in body
    # Constants document the approved template surface
    assert SMS_TEMPLATE_ID
    assert SMS_TEMPLATE_NAME == "Genie - New Zocdoc Patient"


def test_validate_sms_template_ok():
    assert validate_sms_template(SMS_TEMPLATE_BODY) == SMS_TEMPLATE_BODY.strip()


def test_refuse_missing_fingerprint():
    bad = "Hello {{FIRST_NAME}}, welcome to Liora."
    with pytest.raises(ValueError, match="fingerprint"):
        validate_sms_template(bad)
    with pytest.raises(ValueError, match="fingerprint"):
        build_sms_body(bad, first_name="Sam")


def test_refuse_unsubstituted_or_unknown_var():
    # Unknown merge field rejected at validate
    with_unknown = SMS_TEMPLATE_BODY.replace("{{FIRST_NAME}}", "{{FIRST_NAME}} {{DOB}}")
    with pytest.raises(ValueError, match="disallowed"):
        validate_sms_template(with_unknown)

    # Free-form without FIRST_NAME placeholder
    freeform = (
        "Hello there,\n\n"
        "Thanks for scheduling. This message has booking cost of $100 language "
        "but no merge field."
    )
    with pytest.raises(ValueError, match="FIRST_NAME"):
        build_sms_body(freeform, first_name="Sam")


def test_refuse_empty_template():
    with pytest.raises(ValueError, match="empty"):
        validate_sms_template("   ")


def test_phi_masks():
    assert _mask_phone("2125551212").endswith("1212")
    assert "***" in _mask_email("jane.doe@example.com")
    assert "[email]" in _redact_error("fail for jane@x.com")
    assert "[phone]" in _redact_error("dial +1 212-555-9999 now")


def test_redact_secrets():
    jwt = (
        "header eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
    )
    assert "[token]" in _redact_error(jwt)
    assert "eyJ" not in _redact_error(jwt)
    assert "Bearer [redacted]" in _redact_error("Authorization Bearer super-secret-value")
    red = _redact_error("api_key=sk-live-abc password: hunter2 token: xyz")
    assert "sk-live" not in red
    assert "hunter2" not in red
    assert "[redacted]" in red


def test_patient_gb_payload_masks_name():
    payload = _patient_gb_payload("MRN9", "Jane Doe", phone="+12125551212")
    assert payload["mrn"] == "MRN9"
    assert payload["name"] == "J*** D***"
    assert "Jane" not in payload["name"]
    assert payload.get("phone_last4") == "1212"
    assert "2125551212" not in str(payload)
    assert "phone" not in payload or payload.get("phone") is None


def test_job_lock_exclusive(tmp_path):
    path = str(tmp_path / "job.lock")
    a = JobLock(path)
    b = JobLock(path)
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


# ── StepLedger + merge ───────────────────────────────────────────────────────


def test_step_ledger_roundtrip_and_merge_prefers_done(tmp_path):
    path = str(tmp_path / "ledger.json")
    ledger = StepLedger(path)
    corr = "zocdoc-app_test1"
    ledger.record(
        corr,
        steps=[make_step(2, "Sent call office request on ZocDoc", "failed", "timeout")],
        status="failed",
        appointment_id="app_test1",
    )
    ledger.record(
        corr,
        steps=[make_step(2, "Sent call office request on ZocDoc", "done")],
        status="running",
    )
    # Fresh instance reads disk
    ledger2 = StepLedger(path)
    ent = ledger2.get(corr)
    assert ent is not None
    assert ent["status"] == "running"
    assert ent["appointment_id"] == "app_test1"
    assert step_done(ent["steps"], "call")
    # failed must not win over done
    statuses = [s["status"] for s in ent["steps"] if s.get("step") == 2]
    assert statuses == ["done"]

    merged = merge_step_lists(
        [make_step(2, "call", "failed"), make_step(4, "sms", "in_progress")],
        [make_step(2, "call", "skipped"), make_step(4, "sms", "done")],
    )
    by_num = {s["step"]: s["status"] for s in merged}
    assert by_num[2] == "skipped"  # skipped > failed
    assert by_num[4] == "done"


def test_ledger_step_resume_step_done(tmp_path):
    path = str(tmp_path / "ledger.json")
    ledger = StepLedger(path)
    corr = "zocdoc-app_resume"
    ledger.record(
        corr,
        steps=[make_step(2, "Sent call office request on ZocDoc", "done")],
        status="running",
        appointment_id="app_resume",
    )
    prior_steps = merge_step_lists(None, (ledger.get(corr) or {}).get("steps"))
    assert step_done(prior_steps, "call", "send_call_request") is True
    assert step_done(prior_steps, "sms") is False


def test_booking_call_already_requested_true_false():
    assert booking_call_already_requested(None) is False
    assert booking_call_already_requested({}) is False
    assert booking_call_already_requested({"patient": {}}) is False
    assert booking_call_already_requested({"patient": {"requestedToCallTimestamp": ""}}) is False
    assert booking_call_already_requested({
        "patient": {"requestedToCallTimestamp": "2026-07-22T12:00:00Z"},
    }) is True
    assert booking_call_already_requested({
        "requestedToCallTimestamp": "2026-07-22T12:00:00Z",
    }) is True


def test_job_step_error_format_no_phi():
    err = JobStepError(
        "boom for jane@example.com at +1 212-555-9999",
        step="call_request",
        correlation_id="zocdoc-app_x",
        next_action="check Zocdoc requestId / Kernel auth; do not force-retry until ledger/GB shows step 2 done",
        steps=[make_step(2, "call", "failed")],
    )
    text = str(err)
    assert "step=call_request" in text
    assert "corr=zocdoc-app_x" in text
    assert "next:" in text
    assert "jane@example.com" not in text
    assert "212-555-9999" not in text
    assert "[email]" in text or "boom" in text
    missing = JobStepError(
        "missing requestId",
        step="call_request",
        correlation_id="zocdoc-app_y",
        next_action="open booking in Zocdoc; ensure requestId present; cannot skip $100 fee step",
    )
    assert "cannot skip $100 fee step" in str(missing)


def _mock_gb():
    gb = MagicMock()
    gb.query_executions.return_value = []
    gb.report_process.return_value = {"id": "x", "status": "running"}
    gb.log_activity.return_value = {"ok": True}
    gb.request_feedback.return_value = {"ok": True}
    return gb


def test_process_one_second_run_skips_call_and_sms(tmp_path):
    """Ledger-marked call+sms must not re-invoke send_call_request / send_message."""
    path = str(tmp_path / "ledger.json")
    ledger = StepLedger(path)
    corr = "zocdoc-app_idem"
    ledger.record(
        corr,
        steps=[
            make_step(2, "Sent call office request on ZocDoc", "done"),
            make_step(4, "Sent Genie SMS via Weave", "done"),
        ],
        status="running",
        appointment_id="app_idem",
    )

    zoc = MagicMock()
    zoc.get_booking.return_value = {
        "data": {
            "appointmentDetails": {
                "requestId": "req-1",
                "patient": {
                    "firstName": "Pat",
                    "lastName": "Ent",
                    "phoneNumber": "2125551212",
                    "email": "p@example.com",
                },
            }
        }
    }
    weave = MagicMock()
    weave.search_messages.return_value = {"numResults": 0, "threads": []}
    ema = MagicMock()
    ema.search_patients.return_value = []
    gb = _mock_gb()

    appt = {
        "appointmentId": "app_idem",
        "patientType": "NEW",
        "patient": {"firstName": "Pat", "lastName": "Ent"},
        "appointmentTimeUtc": "2026-07-22T15:00:00Z",
    }
    result = process_one(
        appt=appt,
        zoc=zoc,
        weave=weave,
        ema=ema,
        gb=gb,
        sms_template=SMS_TEMPLATE_BODY,
        dry_run=False,
        force=False,
        ledger=ledger,
    )
    assert result == "processed"
    zoc.send_call_request.assert_not_called()
    weave.send_message.assert_not_called()

    # force still must not double-charge / double-SMS when ledger says done
    zoc.send_call_request.reset_mock()
    weave.send_message.reset_mock()
    result2 = process_one(
        appt=appt,
        zoc=zoc,
        weave=weave,
        ema=ema,
        gb=gb,
        sms_template=SMS_TEMPLATE_BODY,
        dry_run=False,
        force=True,
        ledger=ledger,
    )
    assert result2 == "processed"
    zoc.send_call_request.assert_not_called()
    weave.send_message.assert_not_called()


def test_process_one_booking_timestamp_skips_call(tmp_path):
    path = str(tmp_path / "ledger.json")
    ledger = StepLedger(path)
    zoc = MagicMock()
    zoc.get_booking.return_value = {
        "data": {
            "appointmentDetails": {
                "requestId": "req-9",
                "patient": {
                    "firstName": "Pat",
                    "lastName": "Ent",
                    "phoneNumber": "2125551212",
                    "requestedToCallTimestamp": "2026-07-22T10:00:00Z",
                },
            }
        }
    }
    weave = MagicMock()
    weave.search_messages.return_value = {"numResults": 0, "threads": []}
    weave.send_message.return_value = {"smsId": "s1"}
    ema = MagicMock()
    ema.search_patients.return_value = []
    gb = _mock_gb()

    appt = {
        "appointmentId": "app_ts",
        "patient": {"firstName": "Pat", "lastName": "Ent"},
        "appointmentTimeUtc": "2026-07-22T15:00:00Z",
    }
    result = process_one(
        appt=appt,
        zoc=zoc,
        weave=weave,
        ema=ema,
        gb=gb,
        sms_template=SMS_TEMPLATE_BODY,
        dry_run=False,
        ledger=ledger,
    )
    assert result == "processed"
    zoc.send_call_request.assert_not_called()
    weave.send_message.assert_called_once()


# ── Correlation-id validation + activity (from corr branch) ──────────────────


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


def test_process_one_failure_reports_with_steps(tmp_path):
    """process_one reports failure WITH steps then raises JobStepError."""
    path = str(tmp_path / "ledger.json")
    ledger = StepLedger(path)
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
    gb = _mock_gb()

    with pytest.raises(JobStepError) as ei:
        process_one(
            appt=appt,
            zoc=zoc,
            weave=weave,
            ema=ema,
            gb=gb,
            sms_template=SMS_TEMPLATE_BODY,
            dry_run=False,
            force=False,
            ledger=ledger,
        )
    assert ei.value.step == "call_request"
    assert ei.value.correlation_id == "zocdoc-app_fail_1"
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
    fb_kw = gb.request_feedback.call_args.kwargs
    assert fb_kw.get("bot_context", {}).get("correlation_id") == "zocdoc-app_fail_1"


def test_process_one_passes_correlation_id_to_weave(tmp_path):
    """SMS path must pass correlation_id= to Weave (relatedIds), never body."""
    path = str(tmp_path / "ledger.json")
    ledger = StepLedger(path)
    zoc = MagicMock()
    zoc.get_booking.return_value = {
        "data": {
            "appointmentDetails": {
                "requestId": "req-corr",
                "patient": {
                    "firstName": "Pat",
                    "lastName": "Ent",
                    "phoneNumber": "2125551212",
                    "email": "p@example.com",
                },
            }
        }
    }
    weave = MagicMock()
    weave.search_messages.return_value = {"numResults": 0, "threads": []}
    weave.send_message.return_value = {"smsId": "s1", "threadId": "t1"}
    ema = MagicMock()
    ema.search_patients.return_value = []
    gb = _mock_gb()

    appt = {
        "appointmentId": "app_corr_sms",
        "patient": {"firstName": "Pat", "lastName": "Ent"},
        "appointmentTimeUtc": "2026-07-22T15:00:00Z",
    }
    result = process_one(
        appt=appt,
        zoc=zoc,
        weave=weave,
        ema=ema,
        gb=gb,
        sms_template=SMS_TEMPLATE_BODY,
        dry_run=False,
        ledger=ledger,
    )
    assert result == "processed"
    weave.send_message.assert_called_once()
    args, kwargs = weave.send_message.call_args
    # body is positional arg 1 — must not contain correlation id
    body = args[1] if len(args) > 1 else kwargs.get("body", "")
    assert "zocdoc-app_corr_sms" not in body
    assert kwargs.get("correlation_id") == "zocdoc-app_corr_sms"
    # Activity trail after side effects
    actions = [c.args[0] for c in gb.log_activity.call_args_list if c.args]
    assert "zocdoc_call_request" in actions
    assert "ema_portal" in actions
    assert "weave_sms" in actions
    assert "zocdoc_new_patient_processed" in actions
