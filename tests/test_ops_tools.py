"""Unit tests for flag_running_late ops tool + staff queue (no live EMA)."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from voice_agent import ops_tools, staff_queue
from voice_agent.ops_tools import flag_running_late, handle_ops_tool
from voice_agent.staff_queue import enqueue, ops_dry_run


_NY = ZoneInfo("America/New_York")


def _today_start_utc(hour_et: int = 14, minute: int = 0) -> str:
    """Build an ISO UTC start for today at hour_et:minute America/New_York."""
    today = datetime.now(_NY).date()
    local = datetime(today.year, today.month, today.day, hour_et, minute, tzinfo=_NY)
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _tomorrow_start_utc(hour_et: int = 10) -> str:
    from datetime import timedelta

    today = datetime.now(_NY).date() + timedelta(days=1)
    local = datetime(today.year, today.month, today.day, hour_et, 0, tzinfo=_NY)
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _appt(aid=501, start=None, status="CONFIRMED", type_name="Follow Up"):
    return {
        "id": aid,
        "start": start or _today_start_utc(14, 0),
        "end": None,
        "duration": 15,
        "type_name": type_name,
        "status": status,
        "provider_name": "Dr Rhee",
        "facility_name": "Main",
    }


@pytest.fixture
def qpath(tmp_path, monkeypatch):
    p = tmp_path / "staff-queue.jsonl"
    monkeypatch.setenv("LIORA_STAFF_QUEUE_PATH", str(p))
    monkeypatch.delenv("LIORA_OPS_DRY_RUN", raising=False)
    monkeypatch.setenv("LIORA_OPS_WRITES", "1")
    return p


@pytest.fixture
def flow_one_today():
    flow = MagicMock()
    flow.list_upcoming_appointments.return_value = {
        "patient_id": 1001,
        "count": 1,
        "appointments": [_appt(501)],
    }
    return flow


def test_ops_dry_run_env(monkeypatch):
    monkeypatch.setenv("LIORA_OPS_DRY_RUN", "1")
    assert ops_dry_run({}) is True
    monkeypatch.delenv("LIORA_OPS_DRY_RUN")
    assert ops_dry_run({"dry_run": True}) is True
    monkeypatch.setenv("LIORA_OPS_WRITES", "0")
    assert ops_dry_run({}) is True


def test_enqueue_dry_run_no_file(qpath):
    r = enqueue("running_late", summary="test", patient_id=1, dry_run=True)
    assert r["queued"] is False
    assert r["dry_run"] is True
    assert not qpath.exists()


def test_enqueue_writes_jsonl(qpath):
    r = enqueue(
        "running_late",
        summary="late pt",
        patient_id=9,
        appointment_id=501,
        payload={"eta_minutes": 10},
        dry_run=False,
    )
    assert r["queued"] is True
    assert qpath.exists()
    lines = qpath.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "running_late"
    assert rec["patient_id"] == 9
    assert rec["appointment_id"] == 501
    assert rec["source"] == "voice_ops"


def test_flag_needs_confirmation(flow_one_today, qpath):
    result = flag_running_late(
        {"patient_id": 1001},
        flow=flow_one_today,
    )
    assert result["status"] == "needs_confirmation"
    assert result["confirmation"]["appointment_id"] == 501
    assert result["confirmation"]["flag"] == "running_late"
    assert "speak" in result and result["speak"]
    assert not qpath.exists()


def test_flag_confirmed_queues(flow_one_today, qpath):
    result = flag_running_late(
        {"patient_id": 1001, "confirmed": True, "eta_minutes": 15},
        flow=flow_one_today,
    )
    assert result["status"] == "ok"
    assert result["flag_applied"] is True
    assert result["queue"]["queued"] is True
    assert "front desk" in result["message"].lower()
    rec = json.loads(qpath.read_text().strip())
    assert rec["kind"] == "running_late"
    assert rec["payload"]["eta_minutes"] == 15
    assert rec["payload"]["no_clinical_advice"] is True


def test_flag_dry_run_no_side_effect(flow_one_today, qpath):
    result = flag_running_late(
        {"patient_id": 1001, "confirmed": True, "dry_run": True},
        flow=flow_one_today,
    )
    assert result["status"] == "dry_run"
    assert result["flag_applied"] is False
    assert result["queue"]["dry_run"] is True
    assert not qpath.exists()


def test_flag_writes_off_env(flow_one_today, qpath, monkeypatch):
    monkeypatch.setenv("LIORA_OPS_WRITES", "off")
    result = flag_running_late(
        {"patient_id": 1001, "confirmed": True},
        flow=flow_one_today,
    )
    assert result["status"] == "dry_run"
    assert result["flag_applied"] is False
    assert not qpath.exists()


def test_flag_no_same_day(qpath):
    flow = MagicMock()
    flow.list_upcoming_appointments.return_value = {
        "patient_id": 1001,
        "count": 1,
        "appointments": [_appt(777, start=_tomorrow_start_utc())],
    }
    result = flag_running_late({"patient_id": 1001, "confirmed": True}, flow=flow)
    assert result["status"] == "error"
    assert result["error"] == "no_same_day_appointment"
    assert not qpath.exists()


def test_flag_not_same_day_explicit_id(qpath):
    flow = MagicMock()
    flow.list_upcoming_appointments.return_value = {
        "patient_id": 1001,
        "count": 1,
        "appointments": [_appt(777, start=_tomorrow_start_utc())],
    }
    result = flag_running_late(
        {"patient_id": 1001, "appointment_id": 777, "confirmed": True},
        flow=flow,
    )
    assert result["status"] == "error"
    assert result["error"] == "not_same_day"


def test_flag_ambiguous_multiple(qpath):
    flow = MagicMock()
    flow.list_upcoming_appointments.return_value = {
        "patient_id": 1001,
        "count": 2,
        "appointments": [
            _appt(501, start=_today_start_utc(10)),
            _appt(502, start=_today_start_utc(16)),
        ],
    }
    result = flag_running_late({"patient_id": 1001, "confirmed": True}, flow=flow)
    assert result["status"] == "ambiguous"
    assert result["error"] == "multiple_same_day"
    assert len(result["appointments"]) == 2
    assert not qpath.exists()


def test_flag_explicit_id_disambiguates(qpath):
    flow = MagicMock()
    flow.list_upcoming_appointments.return_value = {
        "patient_id": 1001,
        "count": 2,
        "appointments": [
            _appt(501, start=_today_start_utc(10)),
            _appt(502, start=_today_start_utc(16)),
        ],
    }
    result = flag_running_late(
        {"patient_id": 1001, "appointment_id": 502, "confirmed": True},
        flow=flow,
    )
    assert result["status"] == "ok"
    assert result["confirmation"]["appointment_id"] == 502


def test_handle_ops_tool_json_roundtrip(flow_one_today, qpath, monkeypatch):
    # Patch flow getter so handle_ops_tool path works without EMA
    monkeypatch.setattr(ops_tools, "_get_flow", lambda: flow_one_today)
    out = handle_ops_tool(
        "flag_running_late",
        {"patient_id": 1001, "confirmed": True},
    )
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["flag_applied"] is True


def test_tool_definition_present():
    names = {t["name"] for t in ops_tools.OPS_TOOL_DEFINITIONS}
    assert "flag_running_late" in names
    assert ops_tools.is_ops_tool("flag_running_late")
    assert not ops_tools.is_ops_tool("lookup_patient")


def test_no_clinical_advice_in_payload(flow_one_today, qpath):
    result = flag_running_late(
        {
            "patient_id": 1001,
            "confirmed": True,
            "note": "stuck in traffic",
        },
        flow=flow_one_today,
    )
    assert result["queue"]["record"]["payload"]["no_clinical_advice"] is True
    # Ensure we never invent billing fields
    assert "balance" not in result
    assert "copay" not in json.dumps(result)
