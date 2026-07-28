"""Unit tests for P2 ops voice tools (no live EMA)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from voice_agent import clinic_facts, ops_tools, staff_queue
from voice_agent.clinic_facts import CLINIC_FACTS, get_topic
from voice_agent.ops_tools import (
    OPS_TOOL_DEFINITIONS,
    handle_ops_tool,
    lab_results_disclose_enabled,
    strip_pan_like,
)


@pytest.fixture(autouse=True)
def _clear_ops_caches():
    ops_tools.clear_ops_caches()
    yield
    ops_tools.clear_ops_caches()


@pytest.fixture
def queue_path(tmp_path, monkeypatch):
    path = tmp_path / "staff-queue.jsonl"
    monkeypatch.setenv("LIORA_STAFF_QUEUE_PATH", str(path))
    return path


def _loads(s: str) -> dict:
    return json.loads(s)


# ── clinic_faq / clinic_facts ────────────────────────────────────────────────


def test_clinic_facts_grounded_keys():
    assert CLINIC_FACTS["name"] == "Liora Dermatology & Aesthetics"
    assert "110 E 60th" in CLINIC_FACTS["address"]
    assert "212-433-4569" in CLINIC_FACTS["phone_speak"]
    assert CLINIC_FACTS["hours"]["Sun"] == "Closed"
    # No invented garage brand name
    assert "Icon" not in CLINIC_FACTS["parking_note"]
    assert "we don't validate parking" in CLINIC_FACTS["parking_note"].lower() or (
        "does not guarantee" in CLINIC_FACTS["parking_note"].lower()
    )


def test_clinic_faq_hours_and_address():
    hours = _loads(handle_ops_tool("clinic_faq", {"topic": "hours"}))
    assert hours["status"] == "ok"
    assert hours["topic"] == "hours"
    assert "hours" in hours
    assert "message" in hours and "speak" in hours
    assert "Monday" in hours["speak"] or "Monday" in hours["message"]
    # No hallucinated fields
    assert "telehealth" not in hours
    assert "billing" not in hours
    assert "copay" not in json.dumps(hours).lower()

    addr = _loads(handle_ops_tool("clinic_faq", {"topic": "address"}))
    assert addr["status"] == "ok"
    assert "110 E 60th" in addr["address"]
    assert addr["address"] == CLINIC_FACTS["address"]


def test_clinic_faq_unknown_topic():
    out = get_topic("billing_balance")
    assert out["status"] == "unknown_topic"
    assert "speak" in out


def test_clinic_faq_parking_no_false_garage():
    out = _loads(handle_ops_tool("clinic_faq", {"topic": "parking"}))
    assert out["status"] == "ok"
    blob = json.dumps(out).lower()
    assert "garage" in blob or "street" in blob
    assert "icon parking" not in blob
    assert "validate" in blob or "guarantee" in blob


# ── triage_lab_results ───────────────────────────────────────────────────────


def test_triage_lab_needs_confirmation(queue_path):
    out = _loads(
        handle_ops_tool(
            "triage_lab_results",
            {"patient_id": 99, "reason": "labs from last week", "confirmed": False},
        )
    )
    assert out["status"] == "needs_confirmation"
    assert "speak" in out and out["speak"]
    assert "result_values" not in out
    assert out.get("lab_content_disclosed") is False
    assert not queue_path.exists() or queue_path.read_text() == ""


def test_triage_lab_queues_when_confirmed(queue_path):
    out = _loads(
        handle_ops_tool(
            "triage_lab_results",
            {
                "patient_id": 42,
                "reason": "checking on biopsy",
                "preferred_callback": "555-0100",
                "confirmed": True,
            },
        )
    )
    assert out["status"] == "queued"
    assert out["queued"] is True
    assert out["queue_kind"] == "lab_results_callback"
    assert "call you back" in out["speak"].lower() or "call you back" in out["message"].lower()
    assert "result_values" not in out
    assert out.get("lab_content_disclosed") is False

    lines = queue_path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "lab_results_callback"
    assert rec["patient_id"] == 42
    assert rec["source"] == "voice_ops"
    assert "result_values" not in rec.get("payload", {})


def test_lab_results_disclose_default_denies(monkeypatch):
    monkeypatch.delenv("LIORA_LAB_RESULTS_DISCLOSE", raising=False)
    assert lab_results_disclose_enabled() is False
    out = _loads(
        handle_ops_tool(
            "triage_lab_results",
            {"confirmed": True, "reason": "CBC results please read them"},
        )
    )
    blob = json.dumps(out)
    assert "result_values" not in out
    assert "hemoglobin" not in blob.lower()
    assert out.get("lab_content_disclosed") is False


# ── get_insurance_on_file ────────────────────────────────────────────────────


def test_strip_pan_like():
    assert "[card redacted]" in strip_pan_like("card 4111111111111111 on file")
    assert "[card redacted]" in strip_pan_like("4111-1111-1111-1111")
    # short numbers preserved
    assert "1234" in strip_pan_like("member 1234")


def test_get_insurance_strips_pan(monkeypatch):
    mock_client = MagicMock()
    mock_client.get_patient.return_value = {
        "id": 7,
        "primaryInsurance": {
            "name": "Aetna PPO",
            "memberId": "4111111111111111",
            "cardNumber": "4111-1111-1111-1111",
            "groupNumber": "GRP99",
        },
    }
    with patch.object(ops_tools, "_get_client", return_value=mock_client):
        out = _loads(handle_ops_tool("get_insurance_on_file", {"patient_id": 7}))

    assert out["status"] == "ok"
    assert out["on_file"] is True
    assert out["eligibility_checked"] is False
    assert out.get("coverage_asserted") is False
    blob = json.dumps(out)
    assert "4111111111111111" not in blob
    assert "4111-1111-1111-1111" not in blob
    assert "[card redacted]" in blob
    assert "insurance card" in out["speak"].lower() or "insurance card" in out["message"].lower()
    assert "referral" in out["speak"].lower() or "referral" in out["message"].lower()
    # Never invent eligibility language as asserted coverage
    assert "you're covered" not in blob.lower()
    assert "you are covered" not in blob.lower()


def test_get_insurance_none_on_file():
    mock_client = MagicMock()
    mock_client.get_patient.return_value = {"id": 1, "lastName": "Doe"}
    with patch.object(ops_tools, "_get_client", return_value=mock_client):
        out = _loads(handle_ops_tool("get_insurance_on_file", {"patient_id": 1}))
    assert out["status"] == "none_on_file"
    assert out["on_file"] is False
    assert "speak" in out


def test_get_insurance_requires_patient_id():
    out = _loads(handle_ops_tool("get_insurance_on_file", {}))
    assert out["status"] == "patient_id_required"
    assert out["speak"]


# ── flag_running_late ────────────────────────────────────────────────────────


def test_flag_running_late_needs_confirm(queue_path):
    out = _loads(
        handle_ops_tool(
            "flag_running_late",
            {"patient_id": 5, "eta_minutes": 15, "confirmed": False},
        )
    )
    assert out["status"] == "needs_confirmation"
    assert not queue_path.exists() or queue_path.read_text().strip() == ""


def test_flag_running_late_queues(queue_path):
    out = _loads(
        handle_ops_tool(
            "flag_running_late",
            {
                "patient_id": 5,
                "appointment_id": 900,
                "eta_minutes": 20,
                "confirmed": True,
            },
        )
    )
    assert out["status"] == "queued"
    assert out["queue_kind"] == "running_late"
    assert "front desk" in out["speak"].lower()
    lines = queue_path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "running_late"
    assert rec["appointment_id"] == 900
    assert rec["payload"].get("eta_minutes") == 20


def test_flag_running_late_matches_today_appt(queue_path, monkeypatch):
    from datetime import date

    today = date.today().isoformat()
    mock_flow = MagicMock()
    mock_flow.list_upcoming_appointments.return_value = {
        "patient_id": 5,
        "count": 1,
        "appointments": [
            {
                "id": 777,
                "start": f"{today}T15:00:00.000+0000",
                "status": "CONFIRMED",
            }
        ],
    }
    with patch.object(ops_tools, "_get_flow", return_value=mock_flow):
        out = _loads(
            handle_ops_tool(
                "flag_running_late",
                {"patient_id": 5, "eta_minutes": 10, "confirmed": True},
            )
        )
    assert out["status"] == "queued"
    assert out["appointment_id"] == 777


# ── forms_intake_nudge ───────────────────────────────────────────────────────


def test_forms_intake_verbal_path():
    out = _loads(handle_ops_tool("forms_intake_nudge", {}))
    assert out["status"] in {"verbal_only", "needs_confirmation"}
    assert "ModMed" in out["speak"] or "ModMed" in out["message"]
    assert "http" not in out["speak"].lower()
    assert out.get("resend_attempted") is False


def test_forms_intake_writes_disabled(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    mock_client = MagicMock()
    mock_client.get_patient.return_value = {
        "id": 3,
        "email": "pat@example.com",
        "username": "pat@example.com",
    }
    with patch.object(ops_tools, "_get_client", return_value=mock_client):
        out = _loads(
            handle_ops_tool(
                "forms_intake_nudge",
                {"patient_id": 3, "confirmed": True},
            )
        )
    assert out["status"] == "writes_disabled"
    assert out["resend_attempted"] is False
    mock_client.send_portal_email.assert_not_called()
    assert "speak" in out


def test_forms_intake_resend_when_enabled(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    mock_client = MagicMock()
    mock_client.get_patient.return_value = {
        "id": 3,
        "email": "pat@example.com",
        "username": "patuser",
    }
    with patch.object(ops_tools, "_get_client", return_value=mock_client):
        out = _loads(
            handle_ops_tool(
                "forms_intake_nudge",
                {"patient_id": 3, "confirmed": True},
            )
        )
    assert out["status"] == "resent"
    mock_client.send_portal_email.assert_called_once_with(
        "3", "patuser", "pat@example.com"
    )


# ── staff_queue / wiring ─────────────────────────────────────────────────────


def test_staff_queue_append(tmp_path):
    path = tmp_path / "q.jsonl"
    r = staff_queue.enqueue(
        "test_kind",
        patient_id=1,
        summary="hi",
        payload={"a": 1},
        path=path,
    )
    assert r["queued"] is True
    rec = json.loads(path.read_text().strip())
    assert rec["kind"] == "test_kind"
    assert rec["source"] == "voice_ops"


def test_ops_tool_definitions_names():
    names = {t["name"] for t in OPS_TOOL_DEFINITIONS}
    assert names == {
        "triage_lab_results",
        "forms_intake_nudge",
        "flag_running_late",
        "clinic_faq",
        "get_insurance_on_file",
    }


def test_handle_ops_unknown():
    out = _loads(handle_ops_tool("not_a_tool", {}))
    assert out["status"] == "unknown_tool" or out.get("error") == "unknown_tool"
    assert "speak" in out or "message" in out


def test_grok_bridge_registers_ops_tools():
    from voice_agent.ema_tools import EMA_TOOL_DEFINITIONS
    from voice_agent.ops_tools import OPS_TOOL_DEFINITIONS

    combined = list(EMA_TOOL_DEFINITIONS) + list(OPS_TOOL_DEFINITIONS)
    names = {t["name"] for t in combined}
    assert "clinic_faq" in names
    assert "lookup_patient" in names


def test_system_instructions_have_ops_hooks():
    from voice_agent import config

    text = config.SYSTEM_INSTRUCTIONS_SCHEDULING
    assert "triage_lab_results" in text
    assert "get_insurance_on_file" in text
    assert "flag_running_late" in text
    assert "clinic_faq" in text
    assert "eligibility" in text.lower() or "covered" in text.lower()
