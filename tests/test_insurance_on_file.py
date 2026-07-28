"""Unit tests for get_insurance_on_file (no live EMA)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from voice_agent import ops_tools
from voice_agent.ops_tools import (
    OPS_TOOL_DEFINITIONS,
    OPS_TOOL_NAMES,
    handle_ops_tool,
    strip_pan_like,
    summarize_insurance_on_file,
)


@pytest.fixture(autouse=True)
def _clear_ops_caches():
    ops_tools.clear_ops_caches()
    yield
    ops_tools.clear_ops_caches()


def _loads(s: str) -> dict:
    return json.loads(s)


def test_tool_registered():
    names = {t["name"] for t in OPS_TOOL_DEFINITIONS}
    assert "get_insurance_on_file" in names
    assert "get_insurance_on_file" in OPS_TOOL_NAMES
    # No card capture params ever
    props = OPS_TOOL_DEFINITIONS[0]["parameters"]["properties"]
    for banned in ("card_number", "pan", "member_id", "ssn", "cvv"):
        assert banned not in props


def test_strip_pan_like():
    assert "[card redacted]" in strip_pan_like("card 4111111111111111 on file")
    assert "[card redacted]" in strip_pan_like("4111-1111-1111-1111")
    assert "1234" in strip_pan_like("member 1234")


def test_summarize_ema_active_policies():
    patient = {
        "id": 1,
        "allActiveInsurancePolicies": [
            {
                "position": 1,
                "insurancePolicy": {
                    "name": "UnitedHealthcare",
                    "memberId": "4111111111111111",
                    "groupNumber": "G99",
                },
            }
        ],
        "activeInsurances": [{"payerName": "Aetna", "cardNumber": "4111-1111-1111-1111"}],
    }
    info = summarize_insurance_on_file(patient)
    assert info["on_file"] is True
    blob = json.dumps(info)
    assert "4111111111111111" not in blob
    assert "4111-1111-1111-1111" not in blob
    assert any("United" in p or "Aetna" in p for p in info["payers"])


def test_get_insurance_strips_pan_and_high_level_only():
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
    assert out.get("balance_asserted") is False
    assert out["writes_attempted"] is False
    assert out["writes_enabled"] is False
    blob = json.dumps(out)
    assert "4111111111111111" not in blob
    assert "4111-1111-1111-1111" not in blob
    assert "GRP99" not in blob  # group numbers not surfaced
    assert "Aetna" in blob
    assert "insurance card" in out["speak"].lower() or "insurance cards" in out["speak"].lower()
    assert "referral" in out["speak"].lower()
    assert "you're covered" not in blob.lower()
    assert "you are covered" not in blob.lower()
    # High-level insurance only
    assert out["insurance"] == {"on_file": True, "payers": out["payers"]}


def test_get_insurance_none_on_file_fallback():
    mock_client = MagicMock()
    mock_client.get_patient.return_value = {"id": 1, "lastName": "Doe"}
    with patch.object(ops_tools, "_get_client", return_value=mock_client):
        out = _loads(handle_ops_tool("get_insurance_on_file", {"patient_id": 1}))
    assert out["status"] == "none_on_file"
    assert out["on_file"] is False
    assert out["fallback"] == "bring_cards_and_referral"
    assert "bring" in out["speak"].lower()
    assert "card" in out["speak"].lower()
    assert "referral" in out["speak"].lower()
    assert out["writes_attempted"] is False


def test_get_insurance_requires_patient_id():
    out = _loads(handle_ops_tool("get_insurance_on_file", {}))
    assert out["status"] == "patient_id_required"
    assert out["speak"]
    assert out["writes_attempted"] is False


def test_get_insurance_lookup_failed_fallback():
    mock_client = MagicMock()
    mock_client.get_patient.side_effect = RuntimeError("session expired")
    with patch.object(ops_tools, "_get_client", return_value=mock_client):
        out = _loads(handle_ops_tool("get_insurance_on_file", {"patient_id": 9}))
    assert out["status"] == "lookup_failed"
    assert out["fallback"] == "bring_cards_and_referral"
    assert "card" in out["speak"].lower()
    assert out["writes_attempted"] is False


def test_get_insurance_dry_run_read_only():
    mock_client = MagicMock()
    mock_client.get_patient.return_value = {
        "id": 3,
        "activeInsurances": [{"companyName": "BCBS"}],
    }
    with patch.object(ops_tools, "_get_client", return_value=mock_client):
        out = _loads(
            handle_ops_tool(
                "get_insurance_on_file",
                {"patient_id": 3, "dry_run": True},
            )
        )
    assert out["status"] == "ok"
    assert out["dry_run"] is True
    assert out["writes_attempted"] is False
    assert out["writes_enabled"] is False
    mock_client.get_patient.assert_called()  # reads still allowed
    # dry-run must not imply a write path
    assert "booked" not in json.dumps(out).lower()


def test_get_insurance_ema_nested_policy():
    mock_client = MagicMock()
    mock_client.get_patient.return_value = {
        "id": 11,
        "allActiveInsurancePolicies": [
            {
                "position": "PRIMARY",
                "insurancePolicy": {
                    "insuranceCompany": {"name": "Cigna"},
                    "memberId": "9999888877776666",
                },
            }
        ],
    }
    with patch.object(ops_tools, "_get_client", return_value=mock_client):
        out = _loads(handle_ops_tool("get_insurance_on_file", {"patient_id": 11}))
    assert out["status"] == "ok"
    assert out["on_file"] is True
    assert any("Cigna" in p for p in out["payers"])
    assert "9999888877776666" not in json.dumps(out)


def test_no_clinical_or_balance_keys():
    mock_client = MagicMock()
    mock_client.get_patient.return_value = {
        "id": 2,
        "primaryInsurance": {"name": "Oscar"},
        "balance": 999.0,
        "diagnosis": "do not leak",
    }
    with patch.object(ops_tools, "_get_client", return_value=mock_client):
        out = _loads(handle_ops_tool("get_insurance_on_file", {"patient_id": 2}))
    blob = json.dumps(out).lower()
    assert "999" not in blob
    assert "diagnosis" not in blob
    assert "do not leak" not in blob
    assert out.get("balance_asserted") is False
