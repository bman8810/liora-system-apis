"""Tests for after-hours and provider-unavailable voice scripts."""

from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from voice_agent.call_scripts import (
    SCRIPT_TOOL_DEFINITIONS,
    SCRIPT_TOOL_NAMES,
    build_after_hours_script,
    build_provider_unavailable_script,
    handle_script_tool,
)
from voice_agent.clinic_hours import check_hours, hours_speak
from voice_agent import config

TZ = ZoneInfo("America/New_York")

# Wednesday 2026-07-29 14:00 ET — open
OPEN_DT = datetime(2026, 7, 29, 14, 0, tzinfo=TZ)
# Wednesday 2026-07-29 21:00 ET — after hours
CLOSED_EVE = datetime(2026, 7, 29, 21, 0, tzinfo=TZ)
# Sunday 2026-07-26 noon ET — closed
SUNDAY = datetime(2026, 7, 26, 12, 0, tzinfo=TZ)


def test_hours_weekday_open_and_closed():
    open_st = check_hours(OPEN_DT)
    assert open_st.is_open is True
    assert open_st.after_hours is False

    closed_st = check_hours(CLOSED_EVE)
    assert closed_st.is_open is False
    assert closed_st.after_hours is True
    assert closed_st.next_open_speak
    assert "Thursday" in closed_st.next_open_speak or "9" in closed_st.next_open_speak

    sun = check_hours(SUNDAY)
    assert sun.is_open is False
    assert sun.next_open_speak


def test_hours_speak_grounded():
    text = hours_speak()
    assert "Monday through Thursday" in text
    assert "Sunday" in text
    assert "closed" in text.lower()


def test_after_hours_script_reachable_and_coherent(tmp_path, monkeypatch):
    q = tmp_path / "queue.jsonl"
    monkeypatch.setenv("LIORA_STAFF_QUEUE_PATH", str(q))

    menu = build_after_hours_script(
        caller_goal="book a follow-up",
        parked_intents=["insurance question"],
        now=CLOSED_EVE,
    )
    assert menu["script"] == "after_hours"
    assert menu["status"] == "ok"
    assert menu["hours"]["after_hours"] is True
    assert menu["transfer_allowed"] is False
    assert any(o["id"] == "leave_message" for o in menu["options"])
    assert any(o["id"] == "schedule_callback" for o in menu["options"])
    speak = menu["speak"].lower()
    assert "after hours" in speak or "closed" in speak or "open" in speak
    assert "clinical advice" in speak or "same-day" in speak
    assert menu["parked_intents"] == ["insurance question"]
    assert menu["preserve_parked"] is True

    # needs confirmation before queue
    need = build_after_hours_script(
        caller_goal="book a follow-up",
        preferred_action="leave_message",
        confirmed=False,
        parked_intents=["insurance question"],
        now=CLOSED_EVE,
    )
    assert need["status"] == "needs_confirmation"

    done = build_after_hours_script(
        caller_goal="book a follow-up",
        preferred_action="leave_message",
        confirmed=True,
        message_summary="Wants FU next week",
        callback_number="3302067819",
        parked_intents=["insurance question"],
        now=CLOSED_EVE,
    )
    assert done["status"] in {"queued", "accepted"}
    assert done["queued"]["queued"] is True
    assert "insurance question" in done["reoffer_speak"]
    assert q.exists()
    line = q.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["kind"] == "after_hours_message"
    assert rec["payload"]["parked_intents"] == ["insurance question"]


def test_after_hours_when_office_open_redirects():
    res = build_after_hours_script(now=OPEN_DT)
    assert res["status"] == "office_open"
    assert res["transfer_allowed"] is True


def test_provider_unavailable_script_reachable(tmp_path, monkeypatch):
    q = tmp_path / "queue.jsonl"
    monkeypatch.setenv("LIORA_STAFF_QUEUE_PATH", str(q))

    menu = build_provider_unavailable_script(
        requested_provider="Dr. Rhee",
        reason="no_slots",
        visit_type="Follow-up",
        alternate_providers=["zzzJessica", "Dr. Rhee associate"],
        parked_intents=["Rx refill question"],
        now=OPEN_DT,
    )
    assert menu["script"] == "provider_unavailable"
    assert menu["status"] == "ok"
    assert menu["transfer_allowed"] is True
    ids = {o["id"] for o in menu["options"]}
    assert "alternate_provider" in ids
    assert "transfer_to_staff" in ids
    # zzz filtered out
    assert all(not a.lower().startswith("zzz") for a in menu["alternate_providers"])
    assert menu["parked_intents"] == ["Rx refill question"]
    assert "Rhee" in menu["speak"] or "available" in menu["speak"].lower()

    # after hours: no transfer
    ah = build_provider_unavailable_script(
        requested_provider="Dr. Rhee",
        reason="teaching_day",
        preferred_action="transfer_to_staff",
        confirmed=True,
        parked_intents=["insurance Q"],
        now=CLOSED_EVE,
    )
    assert ah["status"] == "transfer_unavailable_after_hours"
    assert ah["transfer_allowed"] is False
    assert "insurance Q" in ah["parked_intents"]

    # confirmed callback during hours preserves parked in queue
    cb = build_provider_unavailable_script(
        requested_provider="Dr. Rhee",
        reason="booked_out",
        preferred_action="schedule_callback",
        confirmed=True,
        parked_intents=["insurance Q"],
        now=OPEN_DT,
    )
    assert cb["status"] in {"queued", "accepted"}
    assert "insurance Q" in cb["reoffer_speak"]
    rec = json.loads(q.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["payload"]["parked_intents"] == ["insurance Q"]


def test_provider_alternate_and_other_times_next_steps():
    alt = build_provider_unavailable_script(
        requested_provider="Dr. Rhee",
        preferred_action="alternate_provider",
        alternate_providers=["Provider A"],
        parked_intents=["forms"],
        now=OPEN_DT,
    )
    assert alt["next_step"] == "find_open_slots_alternate"
    assert "forms" in alt["speak"] or alt["preserve_parked"]

    ot = build_provider_unavailable_script(
        requested_provider="Dr. Rhee",
        preferred_action="other_times",
        parked_intents=["forms"],
        now=OPEN_DT,
    )
    assert ot["next_step"] == "find_open_slots_same_provider"


def test_handle_script_tool_json_roundtrip():
    raw = handle_script_tool(
        "check_office_hours",
        {"as_of": CLOSED_EVE.isoformat()},
    )
    data = json.loads(raw)
    assert data["after_hours"] is True
    assert "speak" in data

    raw2 = handle_script_tool(
        "after_hours_script",
        {
            "as_of": CLOSED_EVE.isoformat(),
            "parked_intents": ["billing"],
            "caller_goal": "cancel",
        },
    )
    data2 = json.loads(raw2)
    assert data2["script"] == "after_hours"
    assert data2["parked_intents"] == ["billing"]

    raw3 = handle_script_tool(
        "provider_unavailable_script",
        {
            "as_of": OPEN_DT.isoformat(),
            "requested_provider": "Dr. Rhee",
            "reason": "no_slots",
            "parked_intents": ["insurance"],
        },
    )
    data3 = json.loads(raw3)
    assert data3["script"] == "provider_unavailable"
    assert data3["parked_intents"] == ["insurance"]


def test_tool_definitions_and_instruction_coverage():
    names = {t["name"] for t in SCRIPT_TOOL_DEFINITIONS}
    assert names == SCRIPT_TOOL_NAMES
    assert "check_office_hours" in names
    assert "after_hours_script" in names
    assert "provider_unavailable_script" in names

    instr = config.SYSTEM_INSTRUCTIONS_SCHEDULING
    assert "after_hours_script" in instr
    assert "provider_unavailable_script" in instr
    assert "parked_intents" in instr
    assert "check_office_hours" in instr
