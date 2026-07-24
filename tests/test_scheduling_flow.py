"""Unit tests for read-only SchedulingFlow + EMA write gate (no live EMA)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from liora_tools.exceptions import WriteGatedError
from liora_tools.modmed.client import EmaClient
from liora_tools.modmed.scheduling_flow import SchedulingFlow
from liora_tools.modmed.write_gate import ema_writes_enabled, require_ema_writes


# ── Fixtures / helpers ──────────────────────────────────────────────────────


def _patient(pid, last="Doe", first="Jane", status="ACTIVE", mrn="MRN001",
             dob="1980-01-08T00:00:00.000+0000", phone=None):
    p = {
        "id": pid,
        "lastName": last,
        "firstName": first,
        "patientStatus": status,
        "mrn": mrn,
        "dateOfBirth": dob,
        "cellPhone": {},
        "phoneNumbers": [],
    }
    if phone:
        p["cellPhone"] = {"phoneNumber": phone}
    return p


def _appt(aid, status="CONFIRMED", start="2026-08-01T14:00:00.000+0000"):
    return {
        "id": aid,
        "status": status,
        "scheduledStartDate": start,
        "scheduledEndDate": "2026-08-01T14:15:00.000+0000",
        "scheduledDuration": 15,
        "appointmentTypeName": "Follow Up",
        "provider": {"id": 10, "name": "Dr Test"},
        "facility": {"id": 20, "name": "Main Clinic"},
    }


@pytest.fixture
def client():
    return MagicMock(spec=EmaClient)


@pytest.fixture
def flow(client):
    return SchedulingFlow(client)


# ── validate_patient ────────────────────────────────────────────────────────


def test_validate_none(flow, client):
    client.list_patients.return_value = []
    result = flow.validate_patient(last_name="Nobody")
    assert result["status"] == "none"
    assert result["match_count"] == 0
    assert result["patient"] is None
    assert result["candidates"] == []


def test_validate_matched(flow, client):
    client.list_patients.return_value = [_patient(1001)]
    result = flow.validate_patient(last_name="Doe", first_name="Jane")
    assert result["status"] == "matched"
    assert result["match_count"] == 1
    assert result["patient"]["id"] == 1001
    assert result["patient"]["last_name"] == "Doe"
    assert "cellPhone" not in result["patient"]


def test_validate_ambiguous_multiple_active(flow, client):
    client.list_patients.return_value = [
        _patient(1001, first="Jane"),
        _patient(1002, first="Janet"),
    ]
    result = flow.validate_patient(last_name="Doe")
    assert result["status"] == "ambiguous"
    assert result["patient"] is None
    assert result["match_count"] == 2
    assert len(result["candidates"]) == 2


def test_validate_inactive_single(flow, client):
    client.list_patients.return_value = [_patient(1003, status="INACTIVE")]
    result = flow.validate_patient(last_name="Doe", mrn="MRN001")
    assert result["status"] == "inactive"
    assert result["patient"]["id"] == 1003
    assert result["patient"]["status"] == "INACTIVE"


def test_validate_phone_filter(flow, client):
    client.list_patients.return_value = [
        _patient(1001, phone="555-111-2222"),
        _patient(1002, phone="555-999-8888"),
    ]
    result = flow.validate_patient(last_name="Doe", phone="(555) 111-2222")
    assert result["status"] == "matched"
    assert result["patient"]["id"] == 1001
    # Larger fetch when phone filter is used
    assert client.list_patients.call_args.kwargs["page_size"] == 100


def test_validate_prefers_single_active_among_mixed(flow, client):
    client.list_patients.return_value = [
        _patient(1001, status="INACTIVE"),
        _patient(1002, status="ACTIVE"),
    ]
    result = flow.validate_patient(last_name="Doe")
    assert result["status"] == "matched"
    assert result["patient"]["id"] == 1002


# ── list_upcoming_appointments ──────────────────────────────────────────────


def test_upcoming_excludes_canceled(flow, client):
    client.list_appointments.return_value = [
        _appt(2001, status="CONFIRMED"),
        _appt(2002, status="CANCELED"),
        _appt(2003, status="COMPLETED"),
        _appt(2004, status="NO_SHOW"),
        _appt(2005, status="PENDING"),
        _appt(2006, status="RESCHEDULED"),
    ]
    result = flow.list_upcoming_appointments(1001, days_ahead=30)
    assert result["patient_id"] == 1001
    assert result["count"] == 2
    ids = {a["id"] for a in result["appointments"]}
    assert ids == {2001, 2005}
    appt = result["appointments"][0]
    assert "type_name" in appt
    assert "provider_name" in appt
    assert "facility_name" in appt


# ── find_open_slots ─────────────────────────────────────────────────────────


def test_find_open_slots_flattens_and_limits(flow, client):
    client.find_slots.return_value = [
        {
            "provider": {"id": 11, "firstName": "Ada", "lastName": "Lovelace"},
            "facility": {"id": 21, "name": "East", "timeZone": "America/New_York"},
            "appointments": [
                {
                    "scheduledStartDate": "2026-08-10T13:00:00.000+0000",
                    "scheduledEndDate": "2026-08-10T13:15:00.000+0000",
                    "scheduledDuration": 15,
                    "timeZoneId": "America/New_York",
                },
                {
                    "scheduledStartDate": "2026-08-10T14:00:00.000+0000",
                    "scheduledEndDate": "2026-08-10T14:15:00.000+0000",
                    "scheduledDuration": 15,
                },
            ],
        },
        {
            "provider": {"id": 12, "name": "Dr Second"},
            "facility": {"id": 22, "name": "West"},
            "appointments": [
                {
                    "scheduledStartDate": "2026-08-11T15:00:00.000+0000",
                    "scheduledEndDate": "2026-08-11T15:15:00.000+0000",
                    "scheduledDuration": 15,
                },
            ],
        },
    ]
    result = flow.find_open_slots(99, limit=2)
    assert result["appt_type_id"] == 99
    assert result["count"] == 2
    assert len(result["slots"]) == 2
    s0 = result["slots"][0]
    assert s0["provider_id"] == 11
    assert s0["provider_name"] == "Ada Lovelace"
    assert s0["facility_name"] == "East"
    assert s0["start"] == "2026-08-10T13:00:00.000+0000"
    assert s0["time_zone"] == "America/New_York"


# ── lookup next_actions ─────────────────────────────────────────────────────


def test_lookup_matched_with_existing_and_slots(flow, client):
    client.list_patients.return_value = [_patient(1001)]
    client.list_appointments.return_value = [_appt(2001)]
    client.find_slots.return_value = [
        {
            "provider": {"id": 11, "name": "Dr A"},
            "facility": {"id": 21, "name": "Main"},
            "appointments": [{
                "scheduledStartDate": "2026-08-10T13:00:00.000+0000",
                "scheduledEndDate": "2026-08-10T13:15:00.000+0000",
                "scheduledDuration": 15,
            }],
        },
    ]
    result = flow.lookup(last_name="Doe", appt_type_id=99, slot_limit=5)
    assert result["patient_result"]["status"] == "matched"
    assert result["appointments"]["count"] == 1
    assert result["slots"]["count"] == 1
    assert "confirm_existing" in result["next_actions"]
    assert "offer_slots" in result["next_actions"]
    assert result["writes_enabled"] is False


def test_lookup_ambiguous_next_action(flow, client):
    client.list_patients.return_value = [
        _patient(1001),
        _patient(1002, first="Janet"),
    ]
    result = flow.lookup(last_name="Doe")
    assert result["patient_result"]["status"] == "ambiguous"
    assert result["appointments"] is None
    assert result["slots"] is None
    assert "handoff_ambiguous" in result["next_actions"]


def test_lookup_none_next_action(flow, client):
    client.list_patients.return_value = []
    result = flow.lookup(last_name="Nobody")
    assert "handoff_no_match" in result["next_actions"]
    assert result["slots"] is None


def test_lookup_matched_no_type_no_appts(flow, client):
    client.list_patients.return_value = [_patient(1001)]
    client.list_appointments.return_value = []
    result = flow.lookup(last_name="Doe")
    assert result["slots"] is None
    assert "ask_visit_type" in result["next_actions"]


def test_list_visit_types(flow, client):
    client.list_appointment_types.return_value = [
        {"id": 1, "name": "New Patient", "defaultDuration": 30, "defaultAsNewPatient": True},
        {"id": 2, "name": "Follow Up", "defaultDuration": 15, "defaultAsNewPatient": False},
    ]
    types = flow.list_visit_types()
    assert types == [
        {"id": 1, "name": "New Patient", "default_duration": 30, "default_as_new_patient": True},
        {"id": 2, "name": "Follow Up", "default_duration": 15, "default_as_new_patient": False},
    ]


# ── write gate ──────────────────────────────────────────────────────────────


def test_require_ema_writes_blocked_by_default(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    assert ema_writes_enabled() is False
    with pytest.raises(WriteGatedError, match="reschedule"):
        require_ema_writes("reschedule")


def test_require_ema_writes_allowed(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    assert ema_writes_enabled() is True
    require_ema_writes("reschedule")  # does not raise


def _gated_client():
    """Minimal EmaClient with mocked transport (no network)."""
    session = MagicMock()
    cfg = MagicMock()
    cfg.base_url = "https://example.test"
    return EmaClient(session, cfg)


def test_client_reschedule_gated(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    c = _gated_client()
    with pytest.raises(WriteGatedError):
        c.reschedule(999, "2026-08-01T13:00:00.000Z")


def test_client_cancel_gated(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    c = _gated_client()
    with pytest.raises(WriteGatedError):
        c.cancel_appointment(999)


def test_client_create_gated(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    c = _gated_client()
    with pytest.raises(WriteGatedError):
        c.create_appointment({"status": "PENDING"})


def test_client_update_gated(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    c = _gated_client()
    with pytest.raises(WriteGatedError):
        c.update_appointment(999, {"status": "PENDING"})


def test_client_portal_email_gated(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    c = _gated_client()
    with pytest.raises(WriteGatedError):
        c.send_portal_email("1", "user", "a@example.com")


def test_client_create_allowed_when_enabled(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    c = _gated_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": 555}
    with patch.object(c, "_post", return_value=mock_resp) as post:
        result = c.create_appointment({"status": "PENDING"})
    assert result == {"id": 555}
    post.assert_called_once()


def test_client_reschedule_allowed_when_enabled(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "1")
    c = _gated_client()
    current = {
        "id": 999,
        "scheduledDuration": 15,
        "provider": {"id": 1},
        "objectLockVersion": 3,
    }
    mock_get = MagicMock()
    mock_get.status_code = 200
    mock_get.ok = True
    mock_get.json.return_value = current

    mock_post = MagicMock()
    mock_post.status_code = 200
    mock_post.ok = True
    mock_post.json.return_value = {"id": 999, "status": "PENDING"}

    with patch.object(c, "_get", return_value=mock_get), \
         patch.object(c, "_post", return_value=mock_post) as post:
        result = c.reschedule(999, "2026-08-01T13:00:00.000Z")
    assert result["id"] == 999
    post.assert_called_once()


def test_client_cancel_allowed_when_enabled(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "yes")
    c = _gated_client()
    reasons_resp = MagicMock()
    reasons_resp.status_code = 200
    reasons_resp.ok = True
    reasons_resp.json.return_value = [
        {"id": 7, "name": "Patient Cancelled", "reasonId": "PATIENT_CANCELLED"},
    ]
    cancel_resp = MagicMock()
    cancel_resp.status_code = 200
    cancel_resp.ok = True
    cancel_resp.json.return_value = {"id": 999, "status": "CANCELED"}

    with patch.object(c, "_get", return_value=reasons_resp), \
         patch.object(c, "_post", return_value=cancel_resp):
        result = c.cancel_appointment(999, reason="PATIENT_CANCELLED")
    assert result["status"] == "CANCELED"
