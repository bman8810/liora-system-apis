"""Genie P2 ops tools (staff queue + grounded helpers).

This module currently ships ``flag_running_late`` (kanban t_0659fd57).
Sibling ops tools land in parallel cards; keep definitions additive.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .staff_queue import enqueue, ops_dry_run

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

OPS_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "flag_running_late",
        "description": (
            "Mark that a patient is running late for a SAME-DAY appointment and "
            "notify the front desk / MA via the staff queue. "
            "Requires patient_id (from lookup_patient). Optional appointment_id; "
            "if omitted, picks today's open appointment (errors if none or multiple). "
            "Optional eta_minutes. "
            "MUST set confirmed=true after the caller agrees before the desk is notified. "
            "Does not change clinical chart advice, billing, or invent new appointment times. "
            "dry_run=true logs the intended notify without writing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id from lookup_patient",
                },
                "appointment_id": {
                    "type": "integer",
                    "description": "Optional EMA appointment id (must be same calendar day ET)",
                },
                "eta_minutes": {
                    "type": "integer",
                    "description": "Optional minutes until arrival",
                },
                "note": {
                    "type": "string",
                    "description": "Optional short free-text note for staff (no clinical advice)",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "True only after patient confirms you may notify the desk",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, do not write the staff queue; return intended payload",
                },
            },
            "required": ["patient_id"],
        },
    },
]


def _compact_json(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":"))


def _speak_payload(message: str, **extra: Any) -> dict:
    out = {"message": message, "speak": message, **extra}
    return out


def _to_ny_fields(start_iso: str | None) -> dict:
    """UTC/offset start → Eastern speak fields."""
    if not start_iso:
        return {"start": None, "local_date": None, "speak_as": None}
    raw = str(start_iso).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return {"start": start_iso, "local_date": None, "speak_as": start_iso}
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    local = dt.astimezone(_NY)
    # Portable hour without leading zero (e.g. "2:10 PM")
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    speak = f"{hour}:{local.minute:02d} {ampm}"
    return {
        "start": start_iso,
        "local_date": local.date().isoformat(),
        "local_time": local.strftime("%H:%M"),
        "speak_as": speak,
        "timezone": "America/New_York",
    }


def _today_ny() -> date:
    return datetime.now(_NY).date()


def _is_same_day_ny(start_iso: str | None, day: date | None = None) -> bool:
    fields = _to_ny_fields(start_iso)
    target = (day or _today_ny()).isoformat()
    return fields.get("local_date") == target


@lru_cache(maxsize=1)
def _get_flow():
    from liora_tools.auth.session_manager import get_ema_client
    from liora_tools.modmed.scheduling_flow import SchedulingFlow

    return SchedulingFlow(get_ema_client())


def clear_ops_flow_cache() -> None:
    _get_flow.cache_clear()


def _truthy(val: Any) -> bool:
    if val is True:
        return True
    if isinstance(val, str) and val.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return False


def flag_running_late(
    arguments: dict,
    *,
    flow=None,
    enqueue_fn=None,
) -> dict:
    """Identify same-day appt and queue a running_late staff note.

    Injectable flow/enqueue_fn for unit tests (no live EMA).
    """
    enqueue_fn = enqueue_fn or enqueue
    pid = arguments.get("patient_id")
    if pid is None:
        return _speak_payload(
            "I need the patient on file first before I can flag running late.",
            status="error",
            error="patient_id_required",
        )

    confirmed = _truthy(arguments.get("confirmed"))
    dry = ops_dry_run(arguments) or _truthy(arguments.get("dry_run"))
    appt_id = arguments.get("appointment_id")
    eta = arguments.get("eta_minutes")
    note = (arguments.get("note") or "").strip()
    if note and len(note) > 280:
        note = note[:277] + "..."

    # Resolve appointments (today only)
    try:
        if flow is None:
            flow = _get_flow()
        upcoming = flow.list_upcoming_appointments(pid, days_ahead=1)
    except Exception as e:
        logger.exception("flag_running_late list_upcoming failed")
        return _speak_payload(
            "I couldn't reach the schedule right now. I can transfer you to the front desk.",
            status="error",
            error="ema_unavailable",
            detail=str(e),
        )

    appts = list(upcoming.get("appointments") or [])
    today = _today_ny()
    same_day = [a for a in appts if _is_same_day_ny(a.get("start"), today)]

    # If caller passed appointment_id, restrict further and validate same-day
    if appt_id is not None:
        try:
            appt_id_int = int(appt_id)
        except (TypeError, ValueError):
            return _speak_payload(
                "That appointment id doesn't look valid.",
                status="error",
                error="invalid_appointment_id",
            )
        matched = [a for a in same_day if int(a.get("id") or -1) == appt_id_int]
        if not matched:
            # Maybe they passed an id that isn't today
            any_match = [a for a in appts if int(a.get("id") or -1) == appt_id_int]
            if any_match and not _is_same_day_ny(any_match[0].get("start"), today):
                return _speak_payload(
                    "I can only flag running late for today's appointments.",
                    status="error",
                    error="not_same_day",
                    appointment_id=appt_id_int,
                )
            return _speak_payload(
                "I couldn't find that appointment on today's schedule for this patient.",
                status="error",
                error="appointment_not_found_today",
                appointment_id=appt_id_int,
            )
        same_day = matched

    if not same_day:
        return _speak_payload(
            "I don't see an open appointment for them today, so I can't flag running late.",
            status="error",
            error="no_same_day_appointment",
            patient_id=pid,
            local_date=today.isoformat(),
        )

    if len(same_day) > 1 and appt_id is None:
        options = []
        for a in same_day:
            fields = _to_ny_fields(a.get("start"))
            options.append(
                {
                    "id": a.get("id"),
                    "speak_as": fields.get("speak_as"),
                    "type_name": a.get("type_name"),
                    "provider_name": a.get("provider_name"),
                    "status": a.get("status"),
                }
            )
        return _speak_payload(
            "They have more than one visit today — which time should I flag as running late?",
            status="ambiguous",
            error="multiple_same_day",
            patient_id=pid,
            appointments=options,
        )

    appt = same_day[0]
    fields = _to_ny_fields(appt.get("start"))
    speak_time = fields.get("speak_as") or "their appointment time"
    conf = {
        "patient_id": pid,
        "appointment_id": appt.get("id"),
        "appointment_time": fields.get("speak_as"),
        "local_date": fields.get("local_date"),
        "type_name": appt.get("type_name"),
        "provider_name": appt.get("provider_name"),
        "status": appt.get("status"),
        "eta_minutes": eta,
        "flag": "running_late",
    }

    if not confirmed and not dry:
        return _speak_payload(
            f"Just to confirm — should I let the front desk know you're running late for "
            f"today's {speak_time} appointment?",
            status="needs_confirmation",
            confirmation=conf,
            dry_run=False,
        )

    summary_bits = [
        f"Running late — patient_id={pid}",
        f"appt_id={appt.get('id')}",
        f"time_et={fields.get('speak_as')}",
    ]
    if eta is not None:
        summary_bits.append(f"eta_min={eta}")
    if note:
        summary_bits.append(f"note={note}")
    summary = "; ".join(summary_bits)

    payload = {
        **conf,
        "note": note or None,
        "no_clinical_advice": True,
    }

    # dry_run path: either explicit dry or confirmed with dry env
    if dry:
        q = enqueue_fn(
            "running_late",
            summary=summary,
            patient_id=pid,
            appointment_id=appt.get("id"),
            payload=payload,
            dry_run=True,
        )
        return _speak_payload(
            f"Dry run only — I would notify the front desk that you're running late for "
            f"today's {speak_time} visit. No message was sent.",
            status="dry_run",
            confirmation=conf,
            queue=q,
            flag_applied=False,
        )

    # confirmed write
    q = enqueue_fn(
        "running_late",
        summary=summary,
        patient_id=pid,
        appointment_id=appt.get("id"),
        payload=payload,
        dry_run=False,
    )
    eta_bit = f" About {int(eta)} minutes." if eta is not None else ""
    return _speak_payload(
        f"I've let the front desk know you're running late for today's {speak_time} appointment."
        f"{eta_bit}",
        status="ok",
        confirmation=conf,
        queue=q,
        flag_applied=True,
    )


def handle_ops_tool(name: str, arguments: dict) -> str:
    """Dispatch an ops tool; return JSON string for Grok."""
    try:
        args = arguments or {}
        if name == "flag_running_late":
            result = flag_running_late(args)
        else:
            result = _speak_payload(
                "That ops tool isn't available.",
                status="error",
                error="unknown_tool",
                name=name,
            )
        return _compact_json(result)
    except Exception as e:
        logger.exception("ops tool %s failed", name)
        return _compact_json(
            _speak_payload(
                "Something went wrong flagging that for the desk.",
                status="error",
                error="ops_tool_failed",
                tool=name,
                detail=str(e),
            )
        )


OPS_TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    t["name"]: (lambda args, n=t["name"]: handle_ops_tool(n, args))
    for t in OPS_TOOL_DEFINITIONS
}


def is_ops_tool(name: str) -> bool:
    return name in OPS_TOOL_HANDLERS
