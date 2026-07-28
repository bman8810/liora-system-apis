"""Unit tests for read-only SchedulingFlow + EMA write gate (no live EMA)."""

from __future__ import annotations

import json
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


def test_validate_missing_status_treated_as_active(flow, client):
    p = _patient(55, status="")
    p.pop("patientStatus", None)
    client.list_patients.return_value = [p]
    result = flow.validate_patient(last_name="Doe")
    assert result["status"] == "matched"
    assert result["patient"]["id"] == 55


def test_validate_phone_filter(flow, client):
    client.list_patients.return_value = [
        _patient(1001, phone="555-111-2222"),
        _patient(1002, phone="555-999-8888"),
    ]
    result = flow.validate_patient(last_name="Doe", phone="(555) 111-2222")
    assert result["status"] == "matched"
    assert result["patient"]["id"] == 1001
    assert client.list_patients.call_args.kwargs["page_size"] == 100


def test_validate_prefers_single_active_among_mixed(flow, client):
    client.list_patients.return_value = [
        _patient(1001, status="INACTIVE"),
        _patient(1002, status="ACTIVE"),
    ]
    result = flow.validate_patient(last_name="Doe")
    assert result["status"] == "matched"
    assert result["patient"]["id"] == 1002


# ── upcoming ────────────────────────────────────────────────────────────────


def test_upcoming_filters_open_statuses(flow, client):
    client.list_appointments.return_value = [
        _appt(1, status="CONFIRMED"),
        _appt(2, status="CANCELED"),
        _appt(3, status="COMPLETED"),
        _appt(4, status="PENDING"),
    ]
    result = flow.list_upcoming_appointments(99, days_ahead=30)
    assert result["count"] == 2
    ids = {a["id"] for a in result["appointments"]}
    assert ids == {1, 4}
    assert result["patient_id"] == 99


def test_find_open_slots_flattens_and_limits(flow, client):
    client.find_slots.return_value = [
        {
            "provider": {"id": 7, "firstName": "A", "lastName": "Bee"},
            "facility": {"id": 2040, "name": "Main", "timeZone": "America/New_York"},
            "appointments": [
                {
                    "scheduledStartDate": "2026-08-01T15:00:00.000+0000",
                    "scheduledEndDate": "2026-08-01T15:15:00.000+0000",
                    "scheduledDuration": 15,
                },
                {
                    "scheduledStartDate": "2026-08-01T15:15:00.000+0000",
                    "scheduledEndDate": "2026-08-01T15:30:00.000+0000",
                    "scheduledDuration": 15,
                },
            ],
        }
    ]
    result = flow.find_open_slots(6188, limit=1)
    assert result["count"] == 1
    assert result["slots"][0]["provider_id"] == 7
    assert result["slots"][0]["facility_id"] == 2040


def test_lookup_next_actions_matched_with_slots(flow, client):
    client.list_patients.return_value = [_patient(10)]
    client.list_appointments.return_value = [_appt(1)]
    client.find_slots.return_value = [
        {
            "provider": {"id": 1, "name": "Dr X"},
            "facility": {"id": 2040, "name": "Main"},
            "appointments": [
                {
                    "scheduledStartDate": "2026-08-02T14:00:00.000+0000",
                    "scheduledEndDate": "2026-08-02T14:15:00.000+0000",
                    "scheduledDuration": 15,
                }
            ],
        }
    ]
    result = flow.lookup(last_name="Doe", appt_type_id=6188, slot_limit=3)
    assert result["patient_result"]["status"] == "matched"
    assert result["appointments"]["count"] == 1
    assert result["slots"]["count"] == 1
    assert "confirm_existing" in result["next_actions"]
    assert "offer_slots" in result["next_actions"]
    assert result["writes_enabled"] is False


def test_lookup_handoff_none(flow, client):
    client.list_patients.return_value = []
    result = flow.lookup(last_name="Nobody")
    assert result["next_actions"] == ["handoff_no_match"]
    assert result["appointments"] is None


# ── write gate ──────────────────────────────────────────────────────────────


def test_require_ema_writes_blocks_by_default(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    assert ema_writes_enabled() is False
    with pytest.raises(WriteGatedError):
        require_ema_writes("cancel_appointment")


def test_require_ema_writes_allows_when_enabled(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    assert ema_writes_enabled() is True
    require_ema_writes("cancel_appointment")  # no raise


def test_client_cancel_gated(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    client = EmaClient(MagicMock())
    with pytest.raises(WriteGatedError):
        client.cancel_appointment(1)


def test_client_reschedule_gated(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    client = EmaClient(MagicMock())
    with pytest.raises(WriteGatedError):
        client.reschedule(1, "2026-08-01T12:00:00.000Z")


def test_client_create_gated(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    client = EmaClient(MagicMock())
    with pytest.raises(WriteGatedError):
        client.create_appointment({})


# ── voice tools ─────────────────────────────────────────────────────────────


def test_ema_tool_lookup_patient_mocked():
    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()
    mock_flow = MagicMock()
    mock_flow.validate_patient.return_value = {
        "status": "matched",
        "match_count": 1,
        "patient": {"id": 1, "last_name": "Doe", "first_name": "J"},
        "candidates": [],
        "message": "ok",
    }
    with patch.object(ema_tools, "_get_flow", return_value=mock_flow):
        out = json.loads(ema_tools.handle_ema_tool("lookup_patient", {"last_name": "Doe"}))
    assert out["status"] == "matched"
    assert out["booking_available"] is False
    assert out["writes_enabled"] is False


def test_ema_tool_unknown():
    from voice_agent import ema_tools

    out = json.loads(ema_tools.handle_ema_tool("nope", {}))
    assert out["error"] == "unknown_tool"


def test_realtime_url_pins_model():
    from voice_agent.grok_bridge import _realtime_url
    from voice_agent import config

    url = _realtime_url()
    assert "model=" in url
    assert config.GROK_VOICE_MODEL in url or "grok-voice-latest" in url


def test_tool_definitions_present():
    from voice_agent.ema_tools import EMA_TOOL_DEFINITIONS

    names = {t["name"] for t in EMA_TOOL_DEFINITIONS}
    assert {
        "lookup_patient",
        "list_upcoming_appointments",
        "list_visit_types",
        "find_open_slots",
        "schedule_lookup",
        "request_product_refill",
        "request_rx_refill",
    }.issubset(names)
