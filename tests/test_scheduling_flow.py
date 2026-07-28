"""Unit tests for SchedulingFlow + EMA write gate + P0 voice tools (no live EMA)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from liora_tools.exceptions import WriteGatedError
from liora_tools.modmed.client import EmaClient
from liora_tools.modmed.scheduling_flow import SchedulingFlow, _to_ny_fields
from liora_tools.modmed.write_gate import ema_writes_enabled, require_ema_writes


# ── Fixtures / helpers ──────────────────────────────────────────────────────


def _patient(
    pid,
    last="Doe",
    first="Jane",
    status="ACTIVE",
    mrn="MRN001",
    dob="1980-01-08T00:00:00.000+0000",
    phone=None,
):
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


def _appt(aid, status="CONFIRMED", start="2026-08-01T18:10:00.000Z", provider_name="Dr Test"):
    return {
        "id": aid,
        "status": status,
        "scheduledStartDate": start,
        "scheduledEndDate": "2026-08-01T18:25:00.000Z",
        "scheduledDuration": 15,
        "appointmentTypeName": "Follow Up",
        "provider": {"id": 10, "name": provider_name},
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


def test_validate_phone_last10_digits(flow, client):
    client.list_patients.return_value = [
        _patient(1001, phone="13302067819"),
    ]
    result = flow.validate_patient(phone="+1 (330) 206-7819", dob="1990-01-01")
    assert result["status"] == "matched"
    assert result["patient"]["id"] == 1001


def test_validate_filters_test_phreesia_when_multiple(flow, client):
    client.list_patients.return_value = [
        _patient(1, last="PHREESIA", first="Test", phone="3302067819"),
        _patient(2, last="Reed", first="Barric", phone="3302067819"),
        _patient(3, last="TEST", first="Patient", phone="3302067819"),
    ]
    result = flow.validate_patient(phone="3302067819")
    assert result["status"] == "matched"
    assert result["patient"]["id"] == 2
    assert result["patient"]["last_name"] == "Reed"


def test_validate_prefers_single_active_among_mixed(flow, client):
    client.list_patients.return_value = [
        _patient(1001, status="INACTIVE"),
        _patient(1002, status="ACTIVE"),
    ]
    result = flow.validate_patient(last_name="Doe")
    assert result["status"] == "matched"
    assert result["patient"]["id"] == 1002


# ── timezone / speak_as ─────────────────────────────────────────────────────


def test_to_ny_fields_eastern_not_utc_plus5():
    # 18:10Z = 2:10 PM Eastern (EDT, July) — not 7:10 PM
    fields = _to_ny_fields("2026-07-28T18:10:00.000Z")
    assert fields["local_timezone"] == "America/New_York"
    assert fields["local_time"] == "2:10 PM"
    assert "2:10 PM Eastern" in fields["speak_as"]
    assert "Tuesday" in fields["speak_as"]
    assert fields["start_utc"].endswith("Z")


def test_upcoming_includes_speak_as(flow, client):
    client.list_appointments.return_value = [
        _appt(1, status="CONFIRMED", start="2026-07-28T18:10:00.000Z"),
        _appt(2, status="CANCELED"),
        _appt(3, status="COMPLETED"),
        _appt(4, status="PENDING"),
    ]
    result = flow.list_upcoming_appointments(99, days_ahead=30)
    assert result["count"] == 2
    ids = {a["id"] for a in result["appointments"]}
    assert ids == {1, 4}
    appt = next(a for a in result["appointments"] if a["id"] == 1)
    assert appt["speak_as"]
    assert "Eastern" in appt["speak_as"]
    assert "2:10 PM" in appt["local_time"]


def test_past_excludes_cancelled_and_orders_latest(flow, client):
    client.list_appointments.return_value = [
        _appt(1, status="CHECKED_OUT", start="2026-01-01T15:00:00.000Z"),
        _appt(2, status="CANCELLED", start="2026-06-01T15:00:00.000Z"),
        _appt(3, status="CANCELED", start="2026-05-01T15:00:00.000Z"),
        _appt(4, status="CHECKED_OUT", start="2026-07-01T15:00:00.000Z"),
    ]
    result = flow.list_past_appointments(7, limit=5)
    assert result["count"] == 2
    assert result["latest"]["id"] == 4
    assert all(a["id"] not in {2, 3} for a in result["appointments"])


# ── slots Rhee-first / no zzz-only starvation ───────────────────────────────


def test_find_open_slots_rhee_before_zzz(flow, client):
    client.find_slots.return_value = [
        {
            "provider": {"id": 99, "name": "zzzJessica Lab"},
            "facility": {"id": 2040, "name": "Main", "timeZone": "America/New_York"},
            "appointments": [
                {
                    "scheduledStartDate": "2026-08-01T14:00:00.000Z",
                    "scheduledEndDate": "2026-08-01T14:15:00.000Z",
                    "scheduledDuration": 15,
                },
            ],
        },
        {
            "provider": {"id": 8327689, "name": "Libby Rhee, MD"},
            "facility": {"id": 2040, "name": "Main", "timeZone": "America/New_York"},
            "appointments": [
                {
                    "scheduledStartDate": "2026-08-01T15:00:00.000Z",
                    "scheduledEndDate": "2026-08-01T15:15:00.000Z",
                    "scheduledDuration": 15,
                },
            ],
        },
    ]
    result = flow.find_open_slots(6188, limit=2)
    assert result["count"] == 2
    assert "Rhee" in (result["slots"][0]["provider_name"] or "")
    assert result["slots"][0]["speak_as"]
    assert "Eastern" in result["slots"][0]["speak_as"]
    assert result["ranking"].startswith("non_zzz")


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


# ── write gate + confirmed ──────────────────────────────────────────────────


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
    client._post.assert_not_called()


def test_book_writes_disabled_when_confirmed(flow, client, monkeypatch):
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


def test_cancel_needs_confirmation(flow, client, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    result = flow.cancel_appointment(appointment_id=9, confirmed=False)
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
    client.reschedule.assert_not_called()


def test_cancel_then_book_path_gated(flow, client, monkeypatch):
    """Multi-step reschedule fallback: cancel then book both need confirm+writes."""
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    c1 = flow.cancel_appointment(appointment_id=1, confirmed=False)
    assert c1["status"] == "needs_confirmation"
    with pytest.raises(WriteGatedError):
        flow.cancel_appointment(appointment_id=1, confirmed=True)
    b1 = flow.book_appointment(
        patient_id=1,
        provider_id=2,
        facility_id=3,
        appointment_type_id=4,
        scheduled_start="2026-08-01T14:00:00.000Z",
        confirmed=False,
    )
    assert b1["status"] == "needs_confirmation"


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


def test_ema_tool_injects_outbound_dial_phone(monkeypatch):
    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()
    monkeypatch.setenv("OUTBOUND_DIAL_PHONE", "3302067819")
    mock_flow = MagicMock()
    mock_flow.validate_patient.return_value = {
        "status": "matched",
        "match_count": 1,
        "patient": {"id": 1},
        "candidates": [],
        "message": "ok",
    }
    with patch.object(ema_tools, "_get_flow", return_value=mock_flow):
        ema_tools.handle_ema_tool("lookup_patient", {"dob": "1990-01-01"})
    kwargs = mock_flow.validate_patient.call_args.kwargs
    assert kwargs["phone"] == "3302067819"
    assert kwargs["dob"] == "1990-01-01"


def test_ema_tool_book_needs_confirmation_without_flag(monkeypatch):
    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    mock_flow = MagicMock()
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
                    "confirmed": False,
                },
            )
        )
    assert out["status"] == "needs_confirmation"
    mock_flow.book_appointment.assert_not_called()


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
    assert out["booking_available"] is False


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


def test_tool_definitions_p0_complete():
    from voice_agent.ema_tools import EMA_TOOL_DEFINITIONS

    names = {t["name"] for t in EMA_TOOL_DEFINITIONS}
    assert names == {
        "lookup_patient",
        "list_upcoming_appointments",
        "list_past_appointments",
        "list_visit_types",
        "find_open_slots",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "schedule_lookup",
    }
    book = next(t for t in EMA_TOOL_DEFINITIONS if t["name"] == "book_appointment")
    assert "confirmed" in book["parameters"]["required"]


def test_scheduling_instructions_require_speak_as_and_confirm():
    from voice_agent import config

    text = config.SYSTEM_INSTRUCTIONS_SCHEDULING.format(
        patient_name="Barric",
        dial_phone="3302067819",
    )
    assert "speak_as" in text
    assert "Eastern" in text
    assert "confirmed=true" in text
    assert "cancel-then-book" in text
    assert "Rhee" in text
    assert "3302067819" in text
    assert "TEST/PHREESIA" in text


def test_allowlist_has_real_e164_not_placeholders():
    from voice_agent import config

    for n in config.ALLOWED_DIAL_PHONES:
        assert "****" not in n
        assert n.startswith("+1")
        assert len("".join(c for c in n if c.isdigit())) >= 11
