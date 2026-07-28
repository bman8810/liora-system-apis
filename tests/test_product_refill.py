"""Unit tests: product/retail refill path (office stock) vs thin Rx routing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from liora_tools.modmed.refill_flow import RefillFlow
from liora_tools.modmed.staff_message_queue import StaffMessageQueue


FORBIDDEN_SPEAK = ("prescription", "erx", "called in", "script was sent")


def _product_flow(tmp_path, client=None):
    qpath = tmp_path / "q.jsonl"
    client = client or MagicMock()
    return (
        RefillFlow(
            client=client,
            message_queue=StaffMessageQueue(client, queue_path=qpath),
        ),
        qpath,
        client,
    )


def test_product_happy_path_queues_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    flow, qpath, _client = _product_flow(tmp_path)

    r = flow.request_product_refill(
        product_name="Dandruff shampoo",
        patient_id=7,
        quantity="1 bottle",
        confirmed=True,
    )
    assert r["status"] == "message_queued"
    assert r["kind"] == "product_refill"
    assert r["audience"] == "inventory"
    assert r["message_queued"] is True
    assert r["erx"] is False
    assert r["prescription_written"] is False

    assert qpath.exists()
    rec = json.loads(qpath.read_text().strip().splitlines()[-1])
    assert rec["kind"] == "product_refill"
    assert rec["audience"] == "inventory"
    assert rec["erx"] is False
    assert rec["prescription_written"] is False
    assert rec["patient_id"] == 7
    assert "pharmacy" not in (rec.get("payload") or {})


def test_product_needs_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    flow, qpath, _ = _product_flow(tmp_path)

    r = flow.request_product_refill(
        product_name="CeraVe cleanser",
        patient_id=1,
        confirmed=False,
    )
    assert r["status"] == "needs_confirmation"
    assert r["erx"] is False
    assert r["prescription_written"] is False
    # staff/inventory language only
    msg = (r.get("message") or "").lower()
    assert "front desk" in msg or "inventory" in msg or "stock" in msg
    assert not qpath.exists()


def test_product_need_product_empty_name(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    flow, qpath, _ = _product_flow(tmp_path)

    r = flow.request_product_refill(product_name="  ", confirmed=True)
    assert r["status"] == "need_product"
    assert r["erx"] is False
    assert r["prescription_written"] is False
    assert not qpath.exists()


def test_product_writes_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    flow, qpath, _ = _product_flow(tmp_path)

    r = flow.request_product_refill(
        product_name="Shampoo",
        patient_id=3,
        confirmed=True,
    )
    assert r["status"] == "writes_disabled"
    assert r["message_queued"] is False
    assert r["erx"] is False
    assert r["prescription_written"] is False
    assert not qpath.exists()


def test_product_does_not_call_erx_methods(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    client = MagicMock()
    client.send_prescription = MagicMock()
    client.send_erx = MagicMock()
    client.create_prescription = MagicMock()
    flow, _qpath, _ = _product_flow(tmp_path, client=client)

    r = flow.request_product_refill(
        product_name="Office shampoo",
        patient_id=9,
        confirmed=True,
    )
    assert r["status"] == "message_queued"
    client.send_prescription.assert_not_called()
    client.send_erx.assert_not_called()
    client.create_prescription.assert_not_called()


def test_product_ignores_pharmacy_kwarg(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    flow, qpath, _ = _product_flow(tmp_path)

    r = flow.request_product_refill(
        product_name="EltaMD sunscreen",
        patient_id=2,
        confirmed=True,
        pharmacy="CVS on 3rd Ave",
    )
    assert r["status"] == "message_queued"
    rec = json.loads(qpath.read_text().strip())
    payload = rec.get("payload") or {}
    assert "pharmacy" not in payload
    assert "pharmacy" not in rec
    body = rec.get("body") or ""
    assert "CVS" not in body
    assert "pharmacy" not in body.lower()


def test_rx_refill_skip_lapse_distinct_from_product(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    qpath = tmp_path / "q.jsonl"
    flow = RefillFlow(
        client=MagicMock(),
        message_queue=StaffMessageQueue(queue_path=qpath),
    )
    r = flow.request_rx_refill(
        patient_id=42,
        medication="Spironolactone 50mg",
        pharmacy="Duane Reade",
        confirmed=True,
        skip_lapse_check=True,
    )
    assert r["status"] == "message_queued"
    assert r["kind"] == "rx_refill"
    assert r["audience"] == "provider"
    assert r["erx"] is False
    assert r["prescription_written"] is False

    rec = json.loads(qpath.read_text().strip())
    assert rec["kind"] == "rx_refill"
    assert rec["audience"] == "provider"
    # distinct from product
    assert rec["kind"] != "product_refill"
    assert rec["audience"] != "inventory"


def test_product_speak_hint_no_rx_language(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    flow, _qpath, _ = _product_flow(tmp_path)

    r = flow.request_product_refill(
        product_name="Ketoconazole shampoo (OTC office)",
        patient_id=5,
        confirmed=True,
    )
    assert r["status"] == "message_queued"
    hint = (r.get("speak_hint") or "").lower()
    for bad in FORBIDDEN_SPEAK:
        assert bad not in hint, f"speak_hint must not contain {bad!r}: {hint!r}"
    # inventory language expected
    assert "front desk" in hint or "stock" in hint


def test_voice_handle_product_refill(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    monkeypatch.setenv("LIORA_STAFF_MESSAGE_QUEUE", str(tmp_path / "voice_q.jsonl"))

    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()

    qpath = tmp_path / "voice_q.jsonl"
    fake = RefillFlow(
        client=MagicMock(),
        message_queue=StaffMessageQueue(queue_path=qpath),
    )

    with patch.object(ema_tools, "_get_refill_flow", return_value=fake), patch.object(
        ema_tools, "_get_flow", return_value=MagicMock()
    ):
        out = json.loads(
            ema_tools.handle_ema_tool(
                "request_product_refill",
                {
                    "product_name": "Office cleanser",
                    "patient_id": 11,
                    "confirmed": True,
                },
            )
        )

    assert out["status"] == "message_queued"
    assert out["kind"] == "product_refill"
    assert out.get("erx") is False


def test_tool_definitions_include_product_and_rx():
    from voice_agent.ema_tools import EMA_TOOL_DEFINITIONS

    names = {t["name"] for t in EMA_TOOL_DEFINITIONS}
    assert "request_product_refill" in names
    assert "request_rx_refill" in names
    assert "prescribe" not in names
    assert "send_erx" not in names
