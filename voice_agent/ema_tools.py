"""EMA scheduling tools for Grok Realtime voice agent.

Reads always available. Mutations (book/reschedule/cancel) require verbal
confirmed=true AND EMA_WRITES_ENABLED on the server (default off).
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
            "(from lookup_patient). Each item has speak_as and local_time in Eastern — read speak_as aloud, never UTC."
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
        "name": "list_past_appointments",
        "description": (
            "List recent PAST appointments for patient_id (most recent first). "
            "Use for last visit / history. Default excludes cancelled. READ ONLY."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "integer"},
                "days_back": {"type": "integer"},
                "limit": {"type": "integer"},
                "include_cancelled": {"type": "boolean"},
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
            "Offer only 2-3 options. Read each slot speak_as (Eastern). READ ONLY — does not book."
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
            "Book a NEW appointment after patient is validated and a slot was chosen from find_open_slots. "
            "MUST get clear verbal confirmation first, then call with confirmed=true. "
            "Requires EMA_WRITES_ENABLED on the server. Do not invent provider/facility/type ids — use tool results."
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
                    "description": "ISO UTC start from find_open_slots, e.g. 2026-08-01T14:00:00.000Z",
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
            "Verbal confirm required (confirmed=true). Uses EMA reschedule API."
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
            "Cancel an upcoming appointment after clear verbal confirmation (confirmed=true)."
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
            "One-shot patient validate + upcoming + optional open slots. "
            "Does not write. For book/reschedule/cancel use dedicated tools after confirm."
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


def _with_outbound_phone(arguments: dict) -> dict:
    args = dict(arguments or {})
    if not args.get("phone"):
        dial = (os.environ.get("OUTBOUND_DIAL_PHONE") or "").strip()
        if dial:
            args["phone"] = dial
    return args


def handle_ema_tool(name: str, arguments: dict) -> str:
    """Execute EMA voice tool (read + gated writes); return JSON string for Grok."""
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
            arguments = _with_outbound_phone(arguments)
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
        elif name == "list_past_appointments":
            pid = arguments.get("patient_id")
            if pid is None:
                return _compact_json({"error": "patient_id_required"})
            result = flow.list_past_appointments(
                pid,
                days_back=int(arguments.get("days_back") or 365),
                limit=int(arguments.get("limit") or 5),
                include_cancelled=bool(arguments.get("include_cancelled") or False),
            )
        elif name == "list_visit_types":
            types = flow.list_visit_types()
            # Keep payload small for voice
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
            arguments = _with_outbound_phone(arguments)
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
            if not arguments.get("confirmed"):
                result = {
                    "status": "needs_confirmation",
                    "message": "Repeat the proposed day/time and ask them to confirm before booking.",
                    "writes_enabled": __import__("liora_tools.modmed.write_gate", fromlist=["ema_writes_enabled"]).ema_writes_enabled(),
                }
            else:
                result = flow.book_appointment(
                    patient_id=arguments.get("patient_id"),
                    provider_id=arguments.get("provider_id"),
                    facility_id=arguments.get("facility_id"),
                    appointment_type_id=arguments.get("appointment_type_id"),
                    scheduled_start=arguments.get("scheduled_start"),
                    duration=int(arguments.get("duration") or 15),
                    notes=arguments.get("notes") or "Booked via Liora voice",
                    new_patient=bool(arguments.get("new_patient") or False),
                    confirmed=True,
                )
        elif name == "reschedule_appointment":
            result = flow.reschedule_appointment(
                appointment_id=arguments.get("appointment_id"),
                new_start=arguments.get("new_start"),
                new_duration=arguments.get("new_duration"),
                provider_id=arguments.get("provider_id"),
                reason=arguments.get("reason") or "PATIENT_RESCHEDULE",
                confirmed=bool(arguments.get("confirmed")),
            )
        elif name == "cancel_appointment":
            result = flow.cancel_appointment(
                appointment_id=arguments.get("appointment_id"),
                reason=arguments.get("reason") or "PATIENT_CANCELLED",
                notes=arguments.get("notes") or "Cancelled via Liora voice",
                confirmed=bool(arguments.get("confirmed")),
            )
        else:
            return _compact_json({"error": "unknown_tool", "name": name})

        if isinstance(result, dict):
            from liora_tools.modmed.write_gate import ema_writes_enabled
            we = ema_writes_enabled()
            # Preserve explicit keys from write helpers; default read tools stay non-booking
            if "writes_enabled" not in result:
                result = {**result, "writes_enabled": we}
            if "booking_available" not in result:
                result = {**result, "booking_available": we}
        return _compact_json(result)
    except Exception as e:
        from liora_tools.exceptions import WriteGatedError
        if isinstance(e, WriteGatedError):
            return _compact_json({
                "status": "writes_disabled",
                "error": "writes_disabled",
                "tool": name,
                "detail": str(e),
                "writes_enabled": False,
                "booking_available": False,
                "message": "Scheduling changes are not enabled right now; offer staff callback.",
            })
        logger.exception("EMA tool %s failed", name)
        return _compact_json({"error": "ema_tool_failed", "tool": name, "detail": str(e)})


TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    t["name"]: (lambda args, n=t["name"]: handle_ema_tool(n, args))
    for t in EMA_TOOL_DEFINITIONS
}
