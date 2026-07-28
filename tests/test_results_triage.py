"""Results-request triage stub: MD/callback queue, no raw result disclosure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liora_tools.modmed.results_flow import (
    ResultsFlow,
    lab_results_disclose_enabled,
    strip_result_disclosure,
)
from liora_tools.modmed.staff_message_queue import StaffMessageQueue
from voice_agent import ops_tools


@pytest.fixture(autouse=True)
def _clear_ops_cache():
    ops_tools.clear_ops_cache()
    yield
    ops_tools.clear_ops_cache()


@pytest.fixture
def queue_path(tmp_path: Path) -> Path:
    return tmp_path / "staff_queue.jsonl"


@pytest.fixture
def flow(queue_path: Path) -> ResultsFlow:
    return ResultsFlow(message_queue=StaffMessageQueue(queue_path=queue_path))


def test_needs_confirmation_without_confirmed(flow: ResultsFlow):
    out = flow.request_results_triage(
        patient_id=42,
        reason="biopsy from last week",
        route="message_md",
        confirmed=False,
    )
    assert out["status"] == "needs_confirmation"
    assert out["message_queued"] is False
    assert out["clinical_results_disclosed"] is False
    assert "result_values" not in out
    assert "lab_values" not in out


def test_no_raw_result_disclosure_keys(flow: ResultsFlow, monkeypatch):
    monkeypatch.delenv("LIORA_LAB_RESULTS_DISCLOSE", raising=False)
    assert lab_results_disclose_enabled() is False

    dirty = {
        "status": "ok",
        "result_values": [{"wbc": 12.3}],
        "lab_values": "hidden",
        "reason": "check labs",
    }
    cleaned = strip_result_disclosure(dirty)
    assert "result_values" not in cleaned
    assert "lab_values" not in cleaned
    assert cleaned["clinical_results_disclosed"] is False
    assert cleaned["disclosure_policy"] == "no_raw_results"


def test_writes_off_logs_intended_no_side_effects(
    flow: ResultsFlow, queue_path: Path, monkeypatch
):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    monkeypatch.delenv("LIORA_VOICE_DRY_RUN", raising=False)

    out = flow.request_results_triage(
        patient_id=7,
        reason="pathology",
        route="message_md",
        confirmed=True,
    )
    assert out["status"] == "writes_disabled"
    assert out["message_queued"] is False
    assert out["clinical_results_disclosed"] is False
    assert out.get("intended_queue", {}).get("kind") == "results"
    assert out["intended_queue"]["route"] == "message_md"
    assert not queue_path.exists()
    # Patient-facing speak path present; no clinical content
    assert "results" in (out.get("speak") or "").lower()
    assert "12.3" not in json.dumps(out)


def test_dry_run_skips_queue(flow: ResultsFlow, queue_path: Path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    monkeypatch.setenv("LIORA_VOICE_DRY_RUN", "1")

    out = flow.request_results_triage(
        patient_id=9,
        reason="bloodwork",
        preferred_callback="330-555-0100",
        route="callback",
        confirmed=True,
    )
    assert out["status"] == "dry_run"
    assert out["dry_run"] is True
    assert out["message_queued"] is False
    assert out["intended_queue"]["kind"] == "results_callback"
    assert out["intended_queue"]["audience"] == "staff"
    assert not queue_path.exists()


def test_confirmed_queues_message_md(flow: ResultsFlow, queue_path: Path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    monkeypatch.delenv("LIORA_VOICE_DRY_RUN", raising=False)

    out = flow.request_results_triage(
        patient_id=11,
        reason="mole biopsy",
        route="message_md",
        confirmed=True,
    )
    assert out["status"] == "message_queued"
    assert out["message_queued"] is True
    assert out["kind"] == "results"
    assert out["audience"] == "provider"
    assert out["clinical_results_disclosed"] is False
    assert "result_values" not in out
    assert queue_path.exists()
    line = queue_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["kind"] == "results"
    assert rec["audience"] == "provider"
    assert rec["patient_id"] == 11
    assert rec.get("clinical_results_disclosed") is False
    assert "No lab values" in rec["body"]


def test_confirmed_queues_callback(flow: ResultsFlow, queue_path: Path, monkeypatch):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "true")
    monkeypatch.delenv("LIORA_VOICE_DRY_RUN", raising=False)

    out = flow.request_results_triage(
        patient_id=12,
        preferred_callback="+13302067819",
        route="callback",
        confirmed=True,
    )
    assert out["status"] == "message_queued"
    assert out["kind"] == "results_callback"
    assert out["audience"] == "staff"
    rec = json.loads(queue_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["kind"] == "results_callback"
    assert "+13302067819" in rec["body"]


def test_ops_tool_handler_json(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EMA_WRITES_ENABLED", "false")
    monkeypatch.setenv("LIORA_STAFF_MESSAGE_QUEUE", str(tmp_path / "q.jsonl"))
    ops_tools.clear_ops_cache()

    raw = ops_tools.handle_ops_tool(
        "triage_lab_results",
        {
            "patient_id": 1,
            "reason": "labs",
            "route": "message_md",
            "confirmed": True,
        },
    )
    out = json.loads(raw)
    assert out["status"] == "writes_disabled"
    assert out["clinical_results_disclosed"] is False
    assert out.get("billing_invented") is False
    assert out.get("pan_captured") is False
    assert out.get("clinical_advice") is False


def test_ops_tool_needs_confirm_json():
    raw = ops_tools.handle_ops_tool(
        "triage_lab_results",
        {"reason": "path report", "confirmed": False},
    )
    out = json.loads(raw)
    assert out["status"] == "needs_confirmation"
    assert out["clinical_results_disclosed"] is False


def test_tool_definition_present():
    names = {t["name"] for t in ops_tools.OPS_TOOL_DEFINITIONS}
    assert "triage_lab_results" in names
    assert ops_tools.is_ops_tool("triage_lab_results")
    assert not ops_tools.is_ops_tool("lookup_patient")


def test_non_goals_flags_held(flow: ResultsFlow, monkeypatch):
    monkeypatch.delenv("EMA_WRITES_ENABLED", raising=False)
    out = flow.request_results_triage(confirmed=True, route="message_md")
    assert out.get("erx") is False
    assert out.get("prescription_written") is False
    assert out.get("billing_invented") is False
    assert out.get("pan_captured") is False
    assert out.get("clinical_advice") is False
