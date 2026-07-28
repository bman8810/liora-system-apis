"""Tests for transfer_to_staff warm handoff (ops path + staff queue)."""

from __future__ import annotations

import json

import pytest

from voice_agent.ops_tools import (
    OPS_TOOL_DEFINITIONS,
    OPS_TOOL_NAMES,
    handle_ops_tool,
    transfer_to_staff,
)
from voice_agent import staff_queue


@pytest.fixture
def queue_path(tmp_path, monkeypatch):
    path = tmp_path / "staff-queue.jsonl"
    monkeypatch.setenv("LIORA_STAFF_QUEUE_PATH", str(path))
    return path


def _loads(s: str) -> dict:
    return json.loads(s)


def test_transfer_persists_artifact(queue_path):
    out = _loads(
        transfer_to_staff(
            {
                "reason": "wants billing help",
                "call_summary": "Caller asked about invoice and then a person",
                "patient_id": 42,
                "callback_numbers": ["555-0100", "555-0199"],
                "callback_windows": "after 3pm",
                "active_intents": ["billing"],
                "parked_intents": ["reschedule"],
                "mode": "transfer",
            }
        )
    )

    assert out["status"] == "queued"
    assert out["handoff"] is True
    assert out["mode"] == "transfer"
    assert out["note_id"]
    assert out["note_path"]
    assert out["next_step"] == "speak_warm_handoff_then_hold"
    assert "connecting you" in out["speak"].lower()
    assert "note" in out["speak"].lower()

    art = out["artifact"]
    assert art["reason"] == "wants billing help"
    assert art["patient_id"] == 42
    assert art["callback_numbers"] == ["555-0100", "555-0199"]
    assert art["call_summary"] == "Caller asked about invoice and then a person"
    assert art["active_intents"] == ["billing"]
    assert art["parked_intents"] == ["reschedule"]
    assert art["kind"] == "transfer_to_staff"
    assert art["schema_version"] == 1

    lines = queue_path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "transfer_to_staff"
    assert rec["reason"] == "wants billing help"
    assert rec["patient_id"] == 42
    assert rec["callback_numbers"] == ["555-0100", "555-0199"]
    assert rec["summary"] == "Caller asked about invoice and then a person"
    assert rec["schema_version"] == 1
    assert str(rec["id"]).startswith("xfer_")
    assert rec["source"] == "voice_ops"
    assert rec["payload"]["mode"] == "transfer"
    assert rec["status"] == "queued"

    notes = staff_queue.read_notes(path=queue_path)
    assert len(notes) == 1
    assert notes[0]["id"] == rec["id"]


def test_transfer_needs_fields(queue_path):
    out = _loads(transfer_to_staff({"reason": "", "call_summary": "x"}))
    assert out["status"] == "needs_fields"
    assert out["handoff"] is False
    assert "speak" in out and out["speak"]
    assert not queue_path.exists() or queue_path.read_text().strip() == ""

    out2 = _loads(transfer_to_staff({"reason": "need help", "call_summary": ""}))
    assert out2["status"] == "needs_fields"
    assert out2["handoff"] is False

    out3 = _loads(transfer_to_staff({}))
    assert out3["status"] == "needs_fields"
    assert out3["handoff"] is False


def test_hold_mode_copy_differs(queue_path):
    transfer = _loads(
        transfer_to_staff(
            {
                "reason": "angry about wait",
                "call_summary": "Caller upset, wants manager",
                "mode": "transfer",
            }
        )
    )
    hold = _loads(
        transfer_to_staff(
            {
                "reason": "angry about wait",
                "call_summary": "Caller upset, wants manager",
                "mode": "hold",
            }
        )
    )
    assert transfer["status"] == "queued"
    assert hold["status"] == "queued"
    assert transfer["mode"] == "transfer"
    assert hold["mode"] == "hold"
    assert transfer["speak"] != hold["speak"]
    assert "hold" in hold["speak"].lower()
    assert "connecting" in transfer["speak"].lower()


def test_in_ops_definitions():
    names = {t["name"] for t in OPS_TOOL_DEFINITIONS}
    assert "transfer_to_staff" in names
    assert "transfer_to_staff" in OPS_TOOL_NAMES


def test_dispatch_via_handle_ops_tool(queue_path):
    out = _loads(
        handle_ops_tool(
            "transfer_to_staff",
            {
                "reason": "pharmacy chase",
                "call_summary": "Needs refill status escalated",
                "patient_id": 7,
            },
        )
    )
    assert out["status"] == "queued"
    assert out["handoff"] is True
    assert out["artifact"]["kind"] == "transfer_to_staff"
    assert queue_path.exists()
    rec = json.loads(queue_path.read_text().strip().splitlines()[0])
    assert rec["kind"] == "transfer_to_staff"
    assert rec["patient_id"] == 7
