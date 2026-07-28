"""Unit tests for P3 read-only billing voice tools (no live network)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from voice_agent import billing_tools
from voice_agent.billing_tools import (
    BILLING_TOOL_DEFINITIONS,
    BILLING_TOOL_NAMES,
    handle_billing_tool,
    strip_payment_trees,
)
from voice_agent.ops_tools import strip_pan_like


@pytest.fixture(autouse=True)
def _clear_billing_caches():
    billing_tools.clear_billing_caches()
    yield
    billing_tools.clear_billing_caches()


def _loads(s: str) -> dict:
    return json.loads(s)


FORBIDDEN_TOOLS = {
    "take_card_payment",
    "create_invoice",
    "send_text_to_pay",
    "post_ema_payment",
    "get_statement_pdf",
    "read_card_on_file_last4",
}

PCI_KEYS = {
    "lastFour",
    "last4",
    "brand",
    "cardholderName",
    "confirmationCode",
    "paymentDetails",
    "payment",
    "payments",
}


def _assert_no_pci(blob: str | dict) -> None:
    if isinstance(blob, dict):
        text = json.dumps(blob)
        data = blob
    else:
        text = blob
        data = json.loads(blob) if blob.strip().startswith("{") else {}

    lower = text.lower()
    for k in PCI_KEYS:
        # status_invoice etc. ok; bare PCI keys must not appear as JSON keys
        assert f'"{k}"' not in text, f"PCI key {k} leaked"
    assert "lastFour" not in text
    assert "cardholderName" not in text
    assert "confirmationCode" not in text
    # Nested scan
    def walk(o):
        if isinstance(o, dict):
            for key, val in o.items():
                assert key not in PCI_KEYS
                assert key not in {"payment", "payments", "paymentDetails"}
                walk(val)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    if isinstance(data, (dict, list)):
        walk(data)


# ── definitions / forbidden ──────────────────────────────────────────────────


def test_billing_tool_names_ship_only():
    assert BILLING_TOOL_NAMES == {
        "get_patient_balance",
        "get_weave_pay_link",
        "get_visit_finance",
    }
    names = {t["name"] for t in BILLING_TOOL_DEFINITIONS}
    assert names == BILLING_TOOL_NAMES
    for forbidden in FORBIDDEN_TOOLS:
        assert forbidden not in BILLING_TOOL_NAMES
        assert forbidden not in names


def test_forbidden_tools_not_handled():
    for name in FORBIDDEN_TOOLS:
        out = _loads(handle_billing_tool(name, {}))
        assert out["status"] == "unknown_tool" or out.get("error") == "unknown_tool"


# ── get_patient_balance ──────────────────────────────────────────────────────


def test_balance_missing_patient_id():
    out = _loads(handle_billing_tool("get_patient_balance", {}))
    assert out["status"] == "patient_id_required"
    assert out["speak"]
    assert "date of birth" in out["speak"].lower() or "confirm" in out["speak"].lower()


def test_balance_zero(monkeypatch):
    mock_client = MagicMock()
    mock_client.list_charges.return_value = [
        {
            "id": 1,
            "patient": {"id": 9},
            "patientResponsibleBalance": 0,
            "status": "CHARGED",
            "description": "PAID ITEM",
            "serviceDateLd": "2026-01-01",
        },
        {
            "id": 2,
            "patientResponsibleBalance": 50.0,
            "status": "CANCELED",
            "description": "canceled fee",
            "serviceDateLd": "2026-01-02",
        },
    ]
    with patch.object(billing_tools, "_get_ema_client", return_value=mock_client):
        out = _loads(handle_billing_tool("get_patient_balance", {"patient_id": 9}))

    assert out["status"] == "zero_balance"
    assert out["amount_due"] == 0.0
    assert out["currency"] == "USD"
    assert out["source"] == "ema_charges"
    assert out["open_charge_count"] == 0
    assert "don't see an open balance" in out["speak"].lower() or "open balance" in out["speak"].lower()
    assert "as_of" in out
    mock_client.list_charges.assert_called()
    kwargs = mock_client.list_charges.call_args
    assert "patient==9" in str(kwargs)


def test_balance_sums_charged_positive_only():
    mock_client = MagicMock()
    mock_client.list_charges.return_value = [
        {
            "id": 1,
            "patientResponsibleBalance": 100.0,
            "status": "CHARGED",
            "description": "NO SHOW FEE MEDICAL",
            "serviceDateLd": "2026-03-01",
        },
        {
            "id": 2,
            "patientResponsibleBalance": 50.5,
            "status": "CHARGED",
            "description": "balance visit",
            "serviceDateLd": "2026-04-01",
        },
        {
            "id": 3,
            "patientResponsibleBalance": 999.0,
            "status": "CANCELED",
            "description": "should ignore",
            "serviceDateLd": "2026-02-01",
        },
        {
            "id": 4,
            "patientResponsibleBalance": 0,
            "status": "CHARGED",
            "description": "zero bal",
            "serviceDateLd": "2026-05-01",
        },
        {
            "id": 5,
            "patientResponsibleBalance": -10.0,
            "status": "CHARGED",
            "description": "credit",
            "serviceDateLd": "2026-06-01",
        },
    ]
    with patch.object(billing_tools, "_get_ema_client", return_value=mock_client):
        out = _loads(handle_billing_tool("get_patient_balance", {"patient_id": 42}))

    assert out["status"] == "ok"
    assert out["amount_due"] == 150.5
    assert out["open_charge_count"] == 2
    assert out["currency"] == "USD"
    assert out["source"] == "ema_charges"
    assert len(out["open_items"]) <= 5
    descs = [i.get("description") for i in out["open_items"]]
    assert "should ignore" not in descs
    # No nested patient PHI
    blob = json.dumps(out)
    assert "dateOfBirth" not in blob
    assert "mrn" not in blob.lower() or '"mrn"' not in blob
    assert "ssn" not in blob.lower()
    assert out["speak"]
    _assert_no_pci(out)


def test_balance_pagination_sums_pages():
    page1 = [
        {
            "id": i,
            "patientResponsibleBalance": 10.0,
            "status": "CHARGED",
            "description": f"item {i}",
            "serviceDateLd": "2026-01-15",
        }
        for i in range(100)
    ]
    page2 = [
        {
            "id": 200,
            "patientResponsibleBalance": 25.0,
            "status": "CHARGED",
            "description": "page2",
            "serviceDateLd": "2026-02-01",
        }
    ]

    def _list_charges(*args, **kwargs):
        page = int(kwargs.get("page_number") or 1)
        if page == 1:
            return page1
        if page == 2:
            return page2
        return []

    mock_client = MagicMock()
    mock_client.list_charges.side_effect = _list_charges
    with patch.object(billing_tools, "_get_ema_client", return_value=mock_client):
        out = _loads(handle_billing_tool("get_patient_balance", {"patient_id": 7}))

    assert out["status"] == "ok"
    assert out["amount_due"] == 100 * 10.0 + 25.0
    assert out["open_charge_count"] == 101
    assert mock_client.list_charges.call_count >= 2


def test_balance_session_expired():
    from liora_tools.exceptions import AuthenticationError

    mock_client = MagicMock()
    mock_client.list_charges.side_effect = AuthenticationError("302")
    with patch.object(billing_tools, "_get_ema_client", return_value=mock_client):
        out = _loads(handle_billing_tool("get_patient_balance", {"patient_id": 1}))
    assert out["status"] == "session_expired"
    assert out.get("amount_due") is None
    assert "billing" in out["speak"].lower() or "portal" in out["speak"].lower()


def test_balance_strips_pan_in_description():
    mock_client = MagicMock()
    mock_client.list_charges.return_value = [
        {
            "id": 1,
            "patientResponsibleBalance": 20.0,
            "status": "CHARGED",
            "description": "fee card 4111111111111111 note",
            "serviceDateLd": "2026-01-01",
        }
    ]
    with patch.object(billing_tools, "_get_ema_client", return_value=mock_client):
        out = _loads(handle_billing_tool("get_patient_balance", {"patient_id": 3}))
    blob = json.dumps(out)
    assert "4111111111111111" not in blob
    assert "[card redacted]" in blob
    assert out["status"] == "ok"


# ── get_weave_pay_link ───────────────────────────────────────────────────────


def test_weave_person_unresolved():
    mock_client = MagicMock()
    with patch.object(billing_tools, "_get_weave_client", return_value=mock_client):
        out = _loads(handle_billing_tool("get_weave_pay_link", {}))
    assert out["status"] == "person_unresolved"
    assert out["found"] is False
    mock_client.search_invoices.assert_not_called()


def test_weave_pay_link_none_all_paid():
    mock_client = MagicMock()
    mock_client.search_invoices.return_value = {
        "invoices": [
            {
                "id": "inv-1",
                "status": "PAID",
                "isActive": False,
                "uniqueLink": "abc",
                "billedAmount": 12586,
                "payment": {
                    "confirmationCode": "ch_secret",
                    "paymentDetails": {
                        "lastFour": "1234",
                        "brand": "AMEX",
                        "cardholderName": "JANE DOE",
                    },
                },
                "payments": [{"lastFour": "9999"}],
            }
        ]
    }
    with patch.object(billing_tools, "_get_weave_client", return_value=mock_client):
        out = _loads(
            handle_billing_tool(
                "get_weave_pay_link",
                {"weave_person_id": "person-uuid-1", "patient_id": 5},
            )
        )
    assert out["status"] == "none"
    assert out["found"] is False
    blob = json.dumps(out)
    _assert_no_pci(out)
    assert "1234" not in blob or "last" not in blob.lower()
    assert "ch_secret" not in blob
    assert "AMEX" not in blob
    assert "JANE DOE" not in blob
    assert "payment" not in out


def test_weave_pay_link_found_strips_payment_trees():
    mock_client = MagicMock()
    mock_client.search_invoices.return_value = {
        "invoices": [
            {
                "id": "inv-2",
                "status": "UNPAID",
                "isActive": True,
                "uniqueLink": "paytok99",
                "billedAmount": 15000,  # $150.00
                "payment": {
                    "confirmationCode": "ch_should_strip",
                    "paymentDetails": {
                        "lastFour": "4242",
                        "brand": "VISA",
                        "cardholderName": "NOPE",
                    },
                },
                "payments": [{"brand": "MC", "lastFour": "1111"}],
            }
        ]
    }
    with patch.object(billing_tools, "_get_weave_client", return_value=mock_client):
        out = _loads(
            handle_billing_tool(
                "get_weave_pay_link",
                {"weave_person_id": "p-1"},
            )
        )
    assert out["status"] == "found"
    assert out["found"] is True
    assert out["pay_url"] == "https://app.getweave.com/pay/paytok99"
    assert out["amount_due"] == 150.0
    blob = json.dumps(out)
    _assert_no_pci(out)
    assert "ch_should_strip" not in blob
    assert "4242" not in blob
    assert "VISA" not in blob
    assert "NOPE" not in blob
    assert "payment" not in out
    assert "payments" not in out
    assert "paymentDetails" not in blob


def test_strip_payment_trees_helper():
    raw = {
        "invoices": [
            {
                "uniqueLink": "x",
                "status": "PAID",
                "payment": {"confirmationCode": "ch_1", "paymentDetails": {"lastFour": "12"}},
                "ok": 1,
            }
        ],
        "payments": [{"a": 1}],
    }
    clean = strip_payment_trees(raw)
    assert "payments" not in clean
    assert "payment" not in clean["invoices"][0]
    assert clean["invoices"][0]["ok"] == 1
    assert clean["invoices"][0]["uniqueLink"] == "x"


def test_strip_pan_like_on_free_text():
    assert "[card redacted]" in strip_pan_like("4111-1111-1111-1111")
    cleaned = strip_payment_trees({"description": "paid with 4111111111111111"})
    assert "4111111111111111" not in json.dumps(cleaned)


# ── get_visit_finance ────────────────────────────────────────────────────────


def test_visit_finance_ok():
    mock_client = MagicMock()
    mock_client.get_appointment.return_value = {
        "id": 900,
        "scheduledStartDate": "2026-07-28T15:00:00.000Z",
        "facility": {"id": 2040},
    }
    mock_client.get_appointments_finance_info.return_value = [
        {"appointmentId": 899, "balance": 5.0, "paidCopay": 0},
        {"appointmentId": 900, "balance": 40.0, "paidCopay": 25},
    ]
    with patch.object(billing_tools, "_get_ema_client", return_value=mock_client):
        out = _loads(handle_billing_tool("get_visit_finance", {"appointment_id": 900}))
    assert out["status"] == "ok"
    assert out["balance"] == 40.0
    assert out["paid_copay"] == 25.0
    assert out["source"] == "ema_finance_info"
    assert "40" in out["speak"] or "balance" in out["speak"].lower()
    mock_client.get_appointments_finance_info.assert_called()
    where = mock_client.get_appointments_finance_info.call_args.kwargs.get("where") or ""
    assert "2040" in where


def test_visit_finance_missing_appointment_id():
    out = _loads(handle_billing_tool("get_visit_finance", {}))
    assert out["status"] == "appointment_id_required"


def test_visit_finance_not_found():
    mock_client = MagicMock()
    mock_client.get_appointment.return_value = {
        "id": 1,
        "scheduledStartDate": "2026-07-28T15:00:00.000Z",
    }
    mock_client.get_appointments_finance_info.return_value = []
    with patch.object(billing_tools, "_get_ema_client", return_value=mock_client):
        out = _loads(handle_billing_tool("get_visit_finance", {"appointment_id": 1}))
    assert out["status"] == "not_found"
    assert "balance" not in out or out.get("balance") is None


# ── wiring ───────────────────────────────────────────────────────────────────


def test_grok_bridge_registers_billing_tools():
    from voice_agent.billing_tools import BILLING_TOOL_DEFINITIONS
    from voice_agent.ema_tools import EMA_TOOL_DEFINITIONS
    from voice_agent.ops_tools import OPS_TOOL_DEFINITIONS

    combined = (
        list(EMA_TOOL_DEFINITIONS)
        + list(OPS_TOOL_DEFINITIONS)
        + list(BILLING_TOOL_DEFINITIONS)
    )
    names = {t["name"] for t in combined}
    assert "get_patient_balance" in names
    assert "get_weave_pay_link" in names
    assert "get_visit_finance" in names
    assert "lookup_patient" in names
    for f in FORBIDDEN_TOOLS:
        assert f not in names


def test_system_instructions_have_billing_addendum():
    from voice_agent import config

    text = config.SYSTEM_INSTRUCTIONS_SCHEDULING
    assert "get_patient_balance" in text
    assert "get_weave_pay_link" in text
    assert "get_visit_finance" in text
    assert "BILLING" in text
    assert "never invent balances" in text.lower() or "never invent" in text.lower()
    assert "last four" in text.lower() or "last4" in text.lower() or "CVV" in text
