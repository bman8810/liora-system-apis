"""Unit tests: Rx/product refill triage + 12mo lapse policy."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from liora_tools.exceptions import WriteGatedError
from liora_tools.modmed.refill_flow import RefillFlow, evaluate_lapse
from liora_tools.modmed.staff_message_queue import StaffMessageQueue


AS_OF = date(2026, 7, 28)


def test_evaluate_lapse_eligible_recent_checked_out():
    appts = [
        {"id": 1, "status": "CHECKED_OUT", "start_date": "2026-03-01", "type_name": "FU"},
    ]
    r = evaluate_lapse(appts, as_of=AS_OF, window_days=365)
    assert r["eligible"] is True
    assert r["status"] == "eligible"
    assert r["next_action"] == "queue_message"
    assert r["last_visit_date"] == "2026-03-01"


def test_evaluate_lapse_blocks_over_12_months():
    appts = [
        {"id": 2, "status": "CHECKED_OUT", "start": "2025-01-01T15:00:00.000Z"},
    ]
    r = evaluate_lapse(appts, as_of=AS_OF, window_days=365)
    assert r["eligible"] is False
    assert r["status"] == "lapsed"
    assert r["next_action"] == "offer_book"
    assert "year" in (r.get("speak_hint") or "").lower() or "visit" in (r.get("speak_hint") or "").lower()


def test_evaluate_lapse_no_history():
    r = evaluate_lapse([], as_of=AS_OF, window_days=365)
    assert r["eligible"] is False
    assert r["status"] == "no_visit_history"
    assert r["next_action"] == "offer_book"


def test_evaluate_lapse_ignores_cancelled():
    appts = [
        {"id": 3, "status": "CANCELLED", "start_date": "2026-06-01"},
        {"id": 4, "status": "CHECKED_OUT", "start_date": "2024-01-01"},
    ]
    r = evaluate_lapse(appts, as_of=AS_OF, window_days=365)
    assert r["eligible"] is False
    assert r["status"] == "lapsed"


def test_evaluate_lapse_boundary_exactly_window(monkeypatch):
    # visit exactly 365 days ago should still be eligible (last_d >= cutoff)
    d = AS_OF - timedelta(days=365)
    appts = [{"id": 5, "status": "CHECKED_OUT", "local_date": d.isoformat()}]
    r = evaluate_lapse(appts, as_of=AS_OF, window_days=365)
    assert r["eligible"] is True


def test_evaluate_lapse_boundary_one_day_over():
    d = AS_OF - timedelta(days=366)
    appts = [{"id": 6, "status": "CHECKED_OUT", "local_date": d.isoformat()}]
    r = evaluate_lapse(appts, as_of=AS_OF, window_days=365)
    assert r["eligible"] is False


class _FakeFlow:
    def __init__(self, appointments):
        self._appointments = appointments

    def list_past_appointments(self, patient_id, **kwargs):
        return {
            "patient_id": patient_id,
            "count": len(self._appointments),
            "appointments": self._appointments,
            "latest": self._appointments[0] if self._appointments else None,
        }


def test_request_rx_refill_lapsed_no_message(tmp_path, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    qpath = tmp_path / "q.jsonl"
    monkeypatch.setenv("LIORA_STAFF_MESSAGE_QUEUE", str(qpath))
    flow = RefillFlow(
        client=MagicMock(),
        scheduling_flow=_FakeFlow(
            [{"id": 1, "status": "CHECKED_OUT", "start_date": "2024-01-01"}]
        ),
        message_queue=StaffMessageQueue(queue_path=qpath),
    )
    r = flow.request_rx_refill(
        patient_id=99,
        medication="Spironolactone",
        confirmed=True,
        window_days=365,
    )
    assert r["status"] == "lapsed"
    assert r["message_queued"] is False
    assert r["erx"] is False
    assert r["prescription_written"] is False
    assert r["next_action"] == "offer_book"
    assert not qpath.exists() or qpath.read_text().strip() == ""


def test_request_rx_refill_needs_confirm_then_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    qpath = tmp_path / "q.jsonl"
    monkeypatch.setenv("LIORA_STAFF_MESSAGE_QUEUE", str(qpath))
    appts = [{"id": 1, "status": "CHECKED_OUT", "start_date": "2026-05-01"}]
    flow = RefillFlow(
        client=MagicMock(),
        scheduling_flow=_FakeFlow(appts),
        message_queue=StaffMessageQueue(queue_path=qpath),
    )
    r0 = flow.request_rx_refill(
        patient_id=42,
        medication="Spironolactone 50mg",
        pharmacy="CVS",
        confirmed=False,
    )
    assert r0["status"] == "needs_confirmation"
    assert r0["erx"] is False
    assert not qpath.exists()

    r1 = flow.request_rx_refill(
        patient_id=42,
        medication="Spironolactone 50mg",
        pharmacy="CVS",
        confirmed=True,
    )
    assert r1["status"] == "message_queued"
    assert r1["erx"] is False
    assert r1["prescription_written"] is False
    assert r1["message_queued"] is True
    line = qpath.read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["kind"] == "rx_refill"
    assert rec["erx"] is False
    assert "Spironolactone" in rec["subject"]
    assert rec["patient_id"] == 42


def test_request_rx_refill_writes_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    qpath = tmp_path / "q.jsonl"
    flow = RefillFlow(
        client=MagicMock(),
        scheduling_flow=_FakeFlow(
            [{"id": 1, "status": "CHECKED_OUT", "start_date": "2026-06-01"}]
        ),
        message_queue=StaffMessageQueue(queue_path=qpath),
    )
    r = flow.request_rx_refill(
        patient_id=1,
        medication="Tretinoin",
        confirmed=True,
    )
    assert r["status"] == "writes_disabled"
    assert r["message_queued"] is False
    assert r["erx"] is False
    assert not qpath.exists()


def test_request_product_refill_distinct_path(tmp_path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    qpath = tmp_path / "q.jsonl"
    flow = RefillFlow(
        client=MagicMock(),
        scheduling_flow=_FakeFlow([]),  # no visits — product still ok
        message_queue=StaffMessageQueue(queue_path=qpath),
    )
    r = flow.request_product_refill(
        product_name="Dandruff shampoo",
        patient_id=7,
        quantity="1 bottle",
        confirmed=True,
    )
    assert r["status"] == "message_queued"
    assert r["kind"] == "product_refill"
    assert r["erx"] is False
    rec = json.loads(qpath.read_text().strip())
    assert rec["kind"] == "product_refill"
    assert rec["audience"] == "inventory"


def test_voice_tool_definitions_include_refill():
    from voice_agent.ema_tools import EMA_TOOL_DEFINITIONS

    names = {t["name"] for t in EMA_TOOL_DEFINITIONS}
    assert "check_visit_lapse" in names
    assert "request_rx_refill" in names
    assert "request_product_refill" in names
    # Must not expose a prescribe tool
    assert "prescribe" not in names
    assert "send_erx" not in names


def test_handle_ema_tool_rx_lapsed(monkeypatch, tmp_path):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    monkeypatch.setenv("LIORA_STAFF_MESSAGE_QUEUE", str(tmp_path / "q.jsonl"))

    from voice_agent import ema_tools

    ema_tools.clear_flow_cache()

    class FakeRefill(RefillFlow):
        def __init__(self):
            pass

        def request_rx_refill(self, **kwargs):
            return {
                "status": "lapsed",
                "message_queued": False,
                "erx": False,
                "prescription_written": False,
                "next_action": "offer_book",
            }

    monkeypatch.setattr(ema_tools, "_get_refill_flow", lambda: FakeRefill())
    monkeypatch.setattr(ema_tools, "_get_flow", lambda: MagicMock())

    out = json.loads(
        ema_tools.handle_ema_tool(
            "request_rx_refill",
            {"patient_id": 1, "medication": "x", "confirmed": True},
        )
    )
    assert out["status"] == "lapsed"
    assert out.get("erx") is False
