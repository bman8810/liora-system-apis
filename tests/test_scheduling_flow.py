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


def test_is_confirmed_strict():
    from liora_tools.modmed.write_gate import is_confirmed

    assert is_confirmed(True) is True
    assert is_confirmed("true") is True
    assert is_confirmed("YES") is True
    assert is_confirmed(1) is True
    assert is_confirmed(False) is False
    assert is_confirmed(None) is False
    assert is_confirmed("false") is False  # bool("false") would be True — must not pass
    assert is_confirmed("no") is False
    assert is_confirmed(0) is False
    assert is_confirmed("") is False


def test_cancel_needs_confirmation(flow, client, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    result = flow.cancel_appointment(appointment_id=9, confirmed=False)
    assert result["status"] == "needs_confirmation"
    assert result["error"] == "needs_confirmation"
    assert result["pending_write"]["op"] == "cancel"
    assert result["confirm_policy"] == "one_write_per_confirm"
    client.cancel_appointment.assert_not_called()


def test_cancel_string_false_not_confirmed(flow, client, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    result = flow.cancel_appointment(appointment_id=9, confirmed="false")
    assert result["status"] == "needs_confirmation"
    client.cancel_appointment.assert_not_called()


def test_reschedule_needs_confirmation(flow, client, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    result = flow.reschedule_appointment(
        appointment_id=9,
        new_start="2026-08-02T14:00:00.000Z",
        confirmed=False,
    )
    assert result["status"] == "needs_confirmation"
    assert result["pending_write"]["op"] == "reschedule"
    client.reschedule.assert_not_called()


def test_book_needs_confirmation(flow, client, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    result = flow.book_appointment(
        patient_id=1,
        provider_id=2,
        facility_id=3,
        appointment_type_id=4,
        scheduled_start="2026-08-01T14:00:00.000Z",
        confirmed=False,
    )
    assert result["status"] == "needs_confirmation"
    assert result["pending_write"]["op"] == "book"
    client._post.assert_not_called()
    client.get_patient.assert_not_called()


def test_cancel_writes_disabled_when_confirmed_no_partial(flow, client, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    with pytest.raises(WriteGatedError):
        flow.cancel_appointment(appointment_id=9, confirmed=True)
    client.cancel_appointment.assert_not_called()


def test_reschedule_writes_disabled_when_confirmed_no_partial(flow, client, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    with pytest.raises(WriteGatedError):
        flow.reschedule_appointment(
            appointment_id=9,
            new_start="2026-08-02T14:00:00.000Z",
            confirmed=True,
        )
    client.reschedule.assert_not_called()


def test_book_writes_disabled_when_confirmed_no_partial(flow, client, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    with pytest.raises(WriteGatedError):
        flow.book_appointment(
            patient_id=1,
            provider_id=2,
            facility_id=3,
            appointment_type_id=4,
            scheduled_start="2026-08-01T14:00:00.000Z",
            confirmed=True,
        )
    client._post.assert_not_called()
    client.get_patient.assert_not_called()


def test_cancel_then_book_multi_step_requires_two_confirms(flow, client, monkeypatch):
    """Reschedule fallback: cancel then book — each step needs its own confirm."""
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    client.cancel_appointment.return_value = {
        "id": 1,
        "status": "CANCELLED",
        "scheduledStartDate": "2026-08-01T14:00:00.000Z",
    }
    # Step 1 without confirm — no write
    c0 = flow.cancel_appointment(appointment_id=1, confirmed=False)
    assert c0["status"] == "needs_confirmation"
    client.cancel_appointment.assert_not_called()

    # Step 1 with confirm — one write only
    c1 = flow.cancel_appointment(appointment_id=1, confirmed=True)
    assert c1["status"] == "cancelled"
    assert client.cancel_appointment.call_count == 1

    # Step 2 book without second confirm — must not write
    b0 = flow.book_appointment(
        patient_id=10,
        provider_id=2,
        facility_id=3,
        appointment_type_id=4,
        scheduled_start="2026-08-05T15:00:00.000Z",
        confirmed=False,
    )
    assert b0["status"] == "needs_confirmation"
    client._post.assert_not_called()

    # Step 2 with confirm
    client.get_patient.return_value = {"id": 10}
    client.list_facilities.return_value = [{"id": 3, "timeZone": "US/Eastern"}]
    client.list_appointment_types.return_value = [{"id": 4, "name": "FU"}]
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "id": 99,
        "status": "PENDING",
        "scheduledStartDate": "2026-08-05T15:00:00.000Z",
    }
    client._post.return_value = mock_resp
    b1 = flow.book_appointment(
        patient_id=10,
        provider_id=2,
        facility_id=3,
        appointment_type_id=4,
        scheduled_start="2026-08-05T15:00:00.000Z",
        confirmed=True,
    )
    assert b1["status"] == "booked"
    assert client._post.call_count == 1
    # Cancel was not called again as part of book
    assert client.cancel_appointment.call_count == 1


def test_book_verify_cancel_path_when_writes_enabled(flow, client, monkeypatch):
    """Lab-shaped path: book → list upcoming (verify) → cancel, each write confirmed."""
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    client.get_patient.return_value = {"id": 10, "lastName": "Reed"}
    client.list_facilities.return_value = [{"id": 3, "name": "Main", "timeZone": "US/Eastern"}]
    client.list_appointment_types.return_value = [{"id": 4, "name": "Follow Up"}]
    created = {
        "id": 555,
        "status": "PENDING",
        "scheduledStartDate": "2026-08-10T18:00:00.000Z",
        "scheduledDuration": 15,
        "appointmentTypeName": "Follow Up",
        "provider": {"id": 2, "name": "Dr Rhee"},
        "facility": {"id": 3, "name": "Main"},
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = created
    client._post.return_value = mock_resp

    booked = flow.book_appointment(
        patient_id=10,
        provider_id=2,
        facility_id=3,
        appointment_type_id=4,
        scheduled_start="2026-08-10T18:00:00.000Z",
        confirmed=True,
    )
    assert booked["status"] == "booked"
    assert booked["raw_id"] == 555

    client.list_appointments.return_value = [created]
    upcoming = flow.list_upcoming_appointments(10)
    assert upcoming["count"] == 1
    assert upcoming["appointments"][0]["id"] == 555

    client.cancel_appointment.return_value = {**created, "status": "CANCELLED"}
    cancelled = flow.cancel_appointment(appointment_id=555, confirmed=True)
    assert cancelled["status"] == "cancelled"
    client.cancel_appointment.assert_called_once()


# ── voice tools ─────────────────────────────────────────────────────────────


def test_ema_tool_lookup_patient_mocked(monkeypatch):
    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
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


def test_ema_tool_cancel_needs_confirmation_short_circuit(monkeypatch):
    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    mock_flow = MagicMock()
    with patch.object(ema_tools, "_get_flow", return_value=mock_flow):
        out = json.loads(
            ema_tools.handle_ema_tool(
                "cancel_appointment",
                {"appointment_id": 9, "confirmed": False},
            )
        )
    assert out["status"] == "needs_confirmation"
    assert out["confirm_policy"] == "one_write_per_confirm"
    mock_flow.cancel_appointment.assert_not_called()


def test_ema_tool_reschedule_string_false_needs_confirmation(monkeypatch):
    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()
    mock_flow = MagicMock()
    with patch.object(ema_tools, "_get_flow", return_value=mock_flow):
        out = json.loads(
            ema_tools.handle_ema_tool(
                "reschedule_appointment",
                {
                    "appointment_id": 9,
                    "new_start": "2026-08-02T14:00:00.000Z",
                    "confirmed": "false",
                },
            )
        )
    assert out["status"] == "needs_confirmation"
    mock_flow.reschedule_appointment.assert_not_called()


def test_ema_tool_cancel_writes_disabled(monkeypatch):
    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    mock_flow = MagicMock()
    mock_flow.cancel_appointment.side_effect = WriteGatedError("blocked")
    with patch.object(ema_tools, "_get_flow", return_value=mock_flow):
        out = json.loads(
            ema_tools.handle_ema_tool(
                "cancel_appointment",
                {"appointment_id": 9, "confirmed": True},
            )
        )
    assert out["status"] == "writes_disabled"
    assert out["error"] == "writes_disabled"
    assert out["booking_available"] is False


def test_ema_tool_book_writes_disabled(monkeypatch):
    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    mock_flow = MagicMock()
    mock_flow.book_appointment.side_effect = WriteGatedError("blocked")
    with patch.object(ema_tools, "_get_flow", return_value=mock_flow):
        out = json.loads(
            ema_tools.handle_ema_tool(
                "book_appointment",
                {
                    "patient_id": 1,
                    "provider_id": 2,
                    "facility_id": 3,
                    "appointment_type_id": 4,
                    "scheduled_start": "2026-08-01T14:00:00.000Z",
                    "confirmed": True,
                },
            )
        )
    assert out["status"] == "writes_disabled"


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


def test_tool_definitions_include_gated_writes():
    from voice_agent.ema_tools import EMA_TOOL_DEFINITIONS

    names = {t["name"] for t in EMA_TOOL_DEFINITIONS}
    assert names == {
        "lookup_patient",
        "list_upcoming_appointments",
        "list_visit_types",
        "find_open_slots",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "schedule_lookup",
    }
    for write_name in ("book_appointment", "reschedule_appointment", "cancel_appointment"):
        tool = next(t for t in EMA_TOOL_DEFINITIONS if t["name"] == write_name)
        assert "confirmed" in tool["parameters"]["required"]

