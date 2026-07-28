"""Unit tests for P2 ops tools — forms/portal incomplete check + nudge (no live EMA)."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from liora_tools.exceptions import WriteGatedError
from liora_tools.modmed.client import EmaClient
from voice_agent import ops_tools
from voice_agent.ops_tools import (
    OPS_TOOL_DEFINITIONS,
    assess_portal_forms,
    forms_intake_nudge,
    handle_ops_tool,
)


@pytest.fixture(autouse=True)
def _clear_writes_env(monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)


def _patient(
    pid=1001,
    username=None,
    email="jane.doe@example.com",  # pass None to omit
    first="Jane",
    last="Doe",
):
    p = {
        "id": pid,
        "firstName": first,
        "lastName": last,
        "mrn": "MRN001",
    }
    if email is not None:
        p["email"] = email
    if username is not None:
        p["username"] = username
    return p


# ── assess_portal_forms ─────────────────────────────────────────────────────


def test_assess_inactive_no_username():
    a = assess_portal_forms(_patient(username=None))
    assert a["portal_status"] == "inactive"
    assert a["forms_status"] == "incomplete"
    assert a["incomplete"] is True
    assert a["has_email"] is True
    assert a["actionable"] == "activate_portal_and_complete_forms"
    assert "ModMed" in a["speak"]
    assert "http" not in a["speak"].lower()


def test_assess_active_username_still_nudge():
    a = assess_portal_forms(_patient(username="jane.doe@example.com"))
    assert a["portal_status"] == "active"
    assert a["forms_status"] == "unknown"
    assert a["incomplete"] is True
    assert a["actionable"] == "nudge_complete_forms"
    assert "portal" in a["speak"].lower()


def test_assess_no_email_inactive():
    a = assess_portal_forms(_patient(username=None, email=None))
    assert a["has_email"] is False
    assert "front desk" in a["speak"].lower() or "email" in a["speak"].lower()


def test_email_masked_not_full():
    a = assess_portal_forms(_patient(email="jane.doe@example.com"))
    assert a["email_masked"] is not None
    assert "jane.doe" not in a["email_masked"]
    assert "@example.com" in a["email_masked"]


# ── forms_intake_nudge ───────────────────────────────────────────────────────


def test_check_without_patient_id_verbal_path():
    out = forms_intake_nudge()
    assert out["status"] == "check_ok"
    assert out["incomplete"] is True
    assert out["speak"]
    assert out["clinical_advice"] is False
    assert out["billing"] is False


def test_check_inactive_patient():
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(username=None)
    out = forms_intake_nudge(patient_id=1001, client=client)
    assert out["status"] == "check_ok"
    assert out["portal_status"] == "inactive"
    assert out["forms_status"] == "incomplete"
    assert out["incomplete"] is True
    client.get_patient.assert_called_once()
    client.send_portal_email.assert_not_called()


def test_check_active_portal():
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(username="jane.doe@example.com")
    out = forms_intake_nudge(patient_id=1001, client=client)
    assert out["portal_status"] == "active"
    assert out["incomplete"] is True
    client.send_portal_email.assert_not_called()


def test_resend_needs_confirmation():
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(username=None)
    out = forms_intake_nudge(patient_id=1001, resend=True, confirmed=False, client=client)
    assert out["status"] == "needs_confirmation"
    assert out["resend"]["attempted"] is False
    assert "would_send" in out["resend"]
    client.send_portal_email.assert_not_called()


def test_resend_writes_disabled(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "0")
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(username="jane.doe@example.com")
    out = forms_intake_nudge(
        patient_id=1001, resend=True, confirmed=True, client=client
    )
    assert out["status"] == "writes_disabled"
    assert out["resend"]["reason"] == "writes_disabled"
    assert out["resend"]["would_send"]["endpoint"].endswith("/portal")
    client.send_portal_email.assert_not_called()


def test_resend_dry_run_even_if_writes_on(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(username="u@example.com")
    out = forms_intake_nudge(
        patient_id=1001,
        resend=True,
        confirmed=True,
        dry_run=True,
        client=client,
    )
    assert out["status"] == "would_resend"
    assert out["resend"]["reason"] == "dry_run"
    assert out["dry_run"] is True
    client.send_portal_email.assert_not_called()


def test_resend_live_when_confirmed_and_writes_on(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(
        username="jane.doe@example.com", email="jane.doe@example.com"
    )
    out = forms_intake_nudge(
        patient_id=1001, resend=True, confirmed=True, client=client
    )
    assert out["status"] == "resent"
    assert out["resend"]["ok"] is True
    client.send_portal_email.assert_called_once_with(
        "1001", "jane.doe@example.com", "jane.doe@example.com"
    )
    assert "resent" in out["speak"].lower() or "ModMed" in out["speak"]


def test_resend_missing_email():
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(username=None, email=None)
    out = forms_intake_nudge(
        patient_id=1001, resend=True, confirmed=True, client=client
    )
    assert out["status"] == "missing_email"
    client.send_portal_email.assert_not_called()


def test_resend_without_patient_id():
    out = forms_intake_nudge(resend=True, confirmed=True)
    assert out["status"] == "patient_id_required"
    assert out["error"] == "patient_id_required"


def test_resend_write_gated_error_from_client(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(username="u@example.com")
    client.send_portal_email.side_effect = WriteGatedError("blocked")
    out = forms_intake_nudge(
        patient_id=1001, resend=True, confirmed=True, client=client
    )
    assert out["status"] == "writes_disabled"


def test_no_clinical_or_billing_keys_on_success():
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(username=None)
    out = forms_intake_nudge(patient_id=1, client=client)
    blob = json.dumps(out).lower()
    assert "diagnosis" not in blob
    assert "copay" not in blob
    assert "balance" not in blob
    assert out.get("clinical_advice") is False
    assert out.get("billing") is False


def test_handle_ops_tool_json_roundtrip(monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "")
    client = MagicMock(spec=EmaClient)
    client.get_patient.return_value = _patient(username=None)
    with patch.object(ops_tools, "_get_ema_client", return_value=client):
        raw = handle_ops_tool(
            "forms_intake_nudge",
            {"patient_id": 1001, "resend": True, "confirmed": True},
        )
    data = json.loads(raw)
    assert data["status"] == "writes_disabled"
    assert "speak" in data


def test_handle_unknown_ops_tool():
    data = json.loads(handle_ops_tool("nope", {}))
    assert data["error"] == "unknown_ops_tool"


def test_tool_definition_present():
    names = {t["name"] for t in OPS_TOOL_DEFINITIONS}
    assert "forms_intake_nudge" in names
    schema = next(t for t in OPS_TOOL_DEFINITIONS if t["name"] == "forms_intake_nudge")
    props = schema["parameters"]["properties"]
    assert "patient_id" in props
    assert "resend" in props
    assert "confirmed" in props
    assert "dry_run" in props


def test_grok_bridge_registers_ops_tools():
    from voice_agent.ema_tools import EMA_TOOL_DEFINITIONS
    from voice_agent.ops_tools import OPS_TOOL_DEFINITIONS as OPS

    combined = list(EMA_TOOL_DEFINITIONS) + list(OPS)
    names = [t["name"] for t in combined]
    assert "forms_intake_nudge" in names
    assert "lookup_patient" in names
