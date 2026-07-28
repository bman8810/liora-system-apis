"""EMA scheduling tools for Grok Realtime voice agent.

Reads always available. Mutations (book / reschedule / cancel) require:
  - verbal confirmed=true (strict parse; string \"false\" is NOT confirmed), and
  - EMA_WRITES_ENABLED=true on the server (default off → writes_disabled).

Multi-step (cancel-then-book): one tool call per write, each with its own confirm.
There is no batch-write tool.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Tool schemas for session.update (Grok Speech-to-Speech custom functions)
EMA_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "lookup_patient",
        "description": (
            "Find a patient in ModMed EMA by name, date of birth, phone, and/or MRN. "
            "Use before discussing appointments. Prefer last name + DOB. "
            "Returns match status: matched, none, ambiguous, or inactive."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "last_name": {"type": "string", "description": "Patient last name"},
                "first_name": {"type": "string", "description": "Patient first name"},
                "dob": {
                    "type": "string",
                    "description": "Date of birth YYYY-MM-DD",
                },
                "phone": {
                    "type": "string",
                    "description": "Phone number any format; paired with name/DOB",
                },
                "mrn": {"type": "string", "description": "Medical record number"},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "list_upcoming_appointments",
        "description": (
            "List upcoming open appointments for a validated patient_id "
            "(from lookup_patient). Speaks times in America/New_York context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id from lookup_patient",
                },
                "days_ahead": {
                    "type": "integer",
                    "description": "Days forward to search (default 90)",
                },
            },
            "required": ["patient_id"],
        },
    },
    {
        "type": "function",
        "name": "list_visit_types",
        "description": (
            "List bookable visit / appointment types (id, name, default duration). "
            "Call before find_open_slots if the patient does not already have a type."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "find_open_slots",
        "description": (
            "Find open appointment slots for a visit type. "
            "Offer only 2–3 options to the caller. READ ONLY — does not book."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "appt_type_id": {
                    "type": "integer",
                    "description": "Appointment type id from list_visit_types",
                },
                "duration": {
                    "type": "integer",
                    "description": "Duration minutes (default from type or 15)",
                },
                "time_of_day": {
                    "type": "string",
                    "description": "ANYTIME | MORNING | AFTERNOON | EVENING",
                },
                "specific_date": {
                    "type": "string",
                    "description": "Optional YYYY-MM-DD",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max slots to return (default 5)",
                },
            },
            "required": ["appt_type_id"],
        },
    },
    {
        "type": "function",
        "name": "book_appointment",
        "description": (
            "Book a NEW appointment after patient is validated and a slot was chosen "
            "from find_open_slots. MUST get clear verbal confirmation first, then call "
            "with confirmed=true. Requires EMA_WRITES_ENABLED. One write per confirm — "
            "never batch with cancel/reschedule."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "integer"},
                "provider_id": {"type": "integer"},
                "facility_id": {"type": "integer"},
                "appointment_type_id": {"type": "integer"},
                "scheduled_start": {
                    "type": "string",
                    "description": "ISO UTC start from find_open_slots",
                },
                "duration": {"type": "integer"},
                "notes": {"type": "string"},
                "new_patient": {"type": "boolean"},
                "confirmed": {
                    "type": "boolean",
                    "description": "true only after caller clearly agrees to this exact slot",
                },
            },
            "required": [
                "patient_id",
                "provider_id",
                "facility_id",
                "appointment_type_id",
                "scheduled_start",
                "confirmed",
            ],
        },
    },
    {
        "type": "function",
        "name": "reschedule_appointment",
        "description": (
            "Move an existing upcoming appointment to a new start time. "
            "Verbal confirm required (confirmed=true) for THIS write only. "
            "If you fall back to cancel-then-book, confirm cancel and book separately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "integer"},
                "new_start": {"type": "string", "description": "ISO UTC new start"},
                "new_duration": {"type": "integer"},
                "provider_id": {"type": "integer"},
                "reason": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["appointment_id", "new_start", "confirmed"],
        },
    },
    {
        "type": "function",
        "name": "cancel_appointment",
        "description": (
            "Cancel an upcoming appointment after clear verbal confirmation "
            "(confirmed=true). Single write only — do not also book in the same step."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "integer"},
                "reason": {"type": "string"},
                "notes": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["appointment_id", "confirmed"],
        },
    },
    {
        "type": "function",
        "name": "schedule_lookup",
        "description": (
            "One-shot read-only flow: validate patient, list upcoming appts, "
            "and optionally find open slots. Prefer this when the caller wants "
            "to check or move a visit. Does NOT book, reschedule, or cancel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "last_name": {"type": "string"},
                "first_name": {"type": "string"},
                "dob": {"type": "string", "description": "YYYY-MM-DD"},
                "phone": {"type": "string"},
                "mrn": {"type": "string"},
                "appt_type_id": {"type": "integer"},
                "duration": {"type": "integer"},
                "time_of_day": {"type": "string"},
                "days_ahead": {"type": "integer"},
                "slot_limit": {"type": "integer"},
            },
            "required": [],
        },
    },
]

_WRITE_TOOLS = frozenset({
    "book_appointment",
    "reschedule_appointment",
    "cancel_appointment",
})


def voice_tools_enabled() -> bool:
    """EMA tools on voice by default when not explicitly disabled."""
    raw = os.environ.get("EMA_VOICE_TOOLS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


@lru_cache(maxsize=1)
def _get_flow():
    from liora_tools.auth.session_manager import get_ema_client
    from liora_tools.modmed.scheduling_flow import SchedulingFlow

    return SchedulingFlow(get_ema_client())


def clear_flow_cache() -> None:
    _get_flow.cache_clear()


def _compact_json(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":"))


def _writes_disabled_payload(name: str, detail: str) -> dict:
    return {
        "status": "writes_disabled",
        "error": "writes_disabled",
        "tool": name,
        "detail": detail,
        "writes_enabled": False,
        "booking_available": False,
        "message": (
            "Scheduling changes are not enabled right now; offer staff callback. "
            "Do not say the visit was booked, moved, or cancelled."
        ),
    }


def handle_ema_tool(name: str, arguments: dict) -> str:
    """Execute EMA voice tool (read + gated single-write mutations)."""
    from liora_tools.exceptions import WriteGatedError
    from liora_tools.modmed.write_gate import ema_writes_enabled, is_confirmed

    known = {t["name"] for t in EMA_TOOL_DEFINITIONS} | set(_WRITE_TOOLS)
    if name not in known:
        return _compact_json({"error": "unknown_tool", "name": name})

    arguments = dict(arguments or {})

    # Voice-layer short-circuit: never touch EMA without confirm on writes.
    if name in _WRITE_TOOLS and not is_confirmed(arguments.get("confirmed")):
        pending = {"tool": name, **{
            k: arguments.get(k)
            for k in (
                "appointment_id",
                "patient_id",
                "provider_id",
                "facility_id",
                "appointment_type_id",
                "scheduled_start",
                "new_start",
                "duration",
            )
            if arguments.get(k) is not None
        }}
        return _compact_json({
            "status": "needs_confirmation",
            "error": "needs_confirmation",
            "action": name,
            "message": (
                "Repeat the proposed change in plain speech and ask a yes/no "
                "question. Only on a clear spoken yes call again with confirmed=true. "
                "One write per confirm — do not batch cancel+book."
            ),
            "pending_write": pending,
            "writes_enabled": ema_writes_enabled(),
            "booking_available": ema_writes_enabled(),
            "confirm_policy": "one_write_per_confirm",
        })

    try:
        flow = _get_flow()
    except Exception as e:
        logger.exception("EMA client unavailable")
        return _compact_json(
            {
                "error": "ema_unavailable",
                "detail": str(e),
                "hint": "Session cookies missing or expired. Re-extract via Kernel Liora profile.",
            }
        )

    try:
        if name == "lookup_patient":
            result = flow.validate_patient(
                last_name=arguments.get("last_name"),
                first_name=arguments.get("first_name"),
                dob=arguments.get("dob"),
                phone=arguments.get("phone"),
                mrn=arguments.get("mrn"),
            )
        elif name == "list_upcoming_appointments":
            pid = arguments.get("patient_id")
            if pid is None:
                return _compact_json({"error": "patient_id_required"})
            result = flow.list_upcoming_appointments(
                pid,
                days_ahead=int(arguments.get("days_ahead") or 90),
            )
        elif name == "list_visit_types":
            types = flow.list_visit_types()
            result = {"count": len(types), "types": types[:40]}
        elif name == "find_open_slots":
            tid = arguments.get("appt_type_id")
            if tid is None:
                return _compact_json({"error": "appt_type_id_required"})
            result = flow.find_open_slots(
                tid,
                duration=int(arguments.get("duration") or 15),
                time_of_day=arguments.get("time_of_day") or "ANYTIME",
                specific_date=arguments.get("specific_date"),
                limit=int(arguments.get("limit") or 5),
            )
        elif name == "schedule_lookup":
            result = flow.lookup(
                last_name=arguments.get("last_name"),
                first_name=arguments.get("first_name"),
                dob=arguments.get("dob"),
                phone=arguments.get("phone"),
                mrn=arguments.get("mrn"),
                days_ahead=int(arguments.get("days_ahead") or 90),
                appt_type_id=arguments.get("appt_type_id"),
                duration=int(arguments.get("duration") or 15),
                time_of_day=arguments.get("time_of_day") or "ANYTIME",
                slot_limit=int(arguments.get("slot_limit") or 5),
            )
        elif name == "book_appointment":
            result = flow.book_appointment(
                patient_id=arguments.get("patient_id"),
                provider_id=arguments.get("provider_id"),
                facility_id=arguments.get("facility_id"),
                appointment_type_id=arguments.get("appointment_type_id"),
                scheduled_start=arguments.get("scheduled_start"),
                duration=int(arguments.get("duration") or 15),
                notes=arguments.get("notes") or "Booked via Liora voice",
                new_patient=bool(arguments.get("new_patient") or False),
                confirmed=True,  # already validated via is_confirmed
            )
        elif name == "reschedule_appointment":
            result = flow.reschedule_appointment(
                appointment_id=arguments.get("appointment_id"),
                new_start=arguments.get("new_start"),
                new_duration=arguments.get("new_duration"),
                provider_id=arguments.get("provider_id"),
                reason=arguments.get("reason") or "PATIENT_RESCHEDULE",
                confirmed=True,
            )
        elif name == "cancel_appointment":
            result = flow.cancel_appointment(
                appointment_id=arguments.get("appointment_id"),
                reason=arguments.get("reason") or "PATIENT_CANCELLED",
                notes=arguments.get("notes") or "Cancelled via Liora voice",
                confirmed=True,
            )
        else:
            return _compact_json({"error": "unknown_tool", "name": name})

        if isinstance(result, dict):
            we = ema_writes_enabled()
            if "writes_enabled" not in result:
                result = {**result, "writes_enabled": we}
            if "booking_available" not in result:
                result = {**result, "booking_available": we}
        return _compact_json(result)
    except WriteGatedError as e:
        return _compact_json(_writes_disabled_payload(name, str(e)))
    except Exception as e:
        logger.exception("EMA tool %s failed", name)
        return _compact_json({"error": "ema_tool_failed", "tool": name, "detail": str(e)})


TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    t["name"]: (lambda args, n=t["name"]: handle_ema_tool(n, args))
    for t in EMA_TOOL_DEFINITIONS
}
