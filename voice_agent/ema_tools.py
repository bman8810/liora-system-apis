"""Read-only EMA scheduling tools for Grok Realtime voice agent.

Writes are never exposed here. Mutations stay behind EMA_WRITES_ENABLED on EmaClient.
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
            "Find a patient in ModMed EMA. REQUIRED for outbound: dob (YYYY-MM-DD) "
            "plus phone (10-digit number dialed). Do NOT pass last_name/first_name "
            "on the first try when phone is known — names often fail. "
            "Only add last_name after phone+dob returns none or ambiguous. "
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
    """Inject dialed number when model omits phone on outbound calls."""
    args = dict(arguments or {})
    if not args.get("phone"):
        dial = (os.environ.get("OUTBOUND_DIAL_PHONE") or "").strip()
        if dial:
            args["phone"] = dial
    return args


def _prefer_phone_dob(arguments: dict) -> dict:
    """Strip names when phone+dob present so bad ASR names cannot zero results."""
    args = dict(arguments or {})
    phone = (args.get("phone") or "").strip()
    dob = (args.get("dob") or "").strip()
    if phone and dob:
        args["last_name"] = None
        args["first_name"] = None
    return args


def handle_ema_tool(name: str, arguments: dict) -> str:
    """Execute a read-only EMA tool; return JSON string for Grok."""
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
            arguments = _prefer_phone_dob(_with_outbound_phone(arguments))
            logger.info(
                "lookup_patient keys=%s phone=%s dob=%s name=%s/%s",
                sorted(k for k, v in arguments.items() if v),
                bool(arguments.get("phone")),
                arguments.get("dob"),
                arguments.get("first_name"),
                arguments.get("last_name"),
            )
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
            arguments = _prefer_phone_dob(_with_outbound_phone(arguments))
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
        else:
            return _compact_json({"error": "unknown_tool", "name": name})

        # Never claim write capability
        if isinstance(result, dict):
            result = {**result, "writes_enabled": False, "booking_available": False}
        return _compact_json(result)
    except Exception as e:
        logger.exception("EMA tool %s failed", name)
        return _compact_json({"error": "ema_tool_failed", "tool": name, "detail": str(e)})


TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    t["name"]: (lambda args, n=t["name"]: handle_ema_tool(n, args))
    for t in EMA_TOOL_DEFINITIONS
}
