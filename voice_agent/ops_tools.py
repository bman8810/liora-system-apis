"""P2 ops/clinical voice tools — policy-bound, spoken paths on every result.

Labs never disclose clinical result content unless LIORA_LAB_RESULTS_DISCLOSE=1.
Insurance is read-only sanitized; no eligibility invent; no card capture.
EMA portal resend and staff queue enqueues require confirmed=true (+ write gate for EMA).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from functools import lru_cache
from typing import Any, Callable

from .clinic_facts import get_topic
from .staff_queue import enqueue as staff_enqueue

logger = logging.getLogger(__name__)

OPS_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "triage_lab_results",
        "description": (
            "Triage a caller asking about lab/results status. Queues MD/callback "
            "staff follow-up. NEVER read or invent clinical result values. "
            "Require confirmed=true before enqueueing the staff note."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id if already validated",
                },
                "reason": {
                    "type": "string",
                    "description": "Caller words about why they are calling",
                },
                "preferred_callback": {
                    "type": "string",
                    "description": "Preferred callback number or window",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "True only after verbal yes to queue a callback",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "forms_intake_nudge",
        "description": (
            "Help with ModMed patient portal / intake forms. Speaks portal email "
            "path only (no invented URLs). Optional resend portal invite only when "
            "confirmed=true and EMA writes are enabled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id if known",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "True to attempt portal email resend",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "flag_running_late",
        "description": (
            "Flag that the patient is running late for a same-day visit. "
            "Notifies front desk/MA via staff queue. Require confirmed=true."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id",
                },
                "appointment_id": {
                    "type": "integer",
                    "description": "Specific appointment id if known",
                },
                "eta_minutes": {
                    "type": "integer",
                    "description": "Estimated minutes until arrival",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "True after verbal yes to notify front desk",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "clinic_faq",
        "description": (
            "Answer hours, address, parking, or phone from grounded clinic facts only. "
            "Topics: hours | address | parking | phone | all. Never invent other clinics."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "hours | address | parking | phone | all",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_insurance_on_file",
        "description": (
            "Read insurance summary on file if EMA exposes it. Never invent eligibility "
            "or copay. Card numbers are stripped. No card capture parameters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id from lookup_patient",
                },
            },
            "required": ["patient_id"],
        },
    },
    {
        "type": "function",
        "name": "transfer_to_staff",
        "description": (
            "Warm handoff / hold for human staff. ALWAYS call before saying you are "
            "connecting them or putting them on hold. Persists structured note "
            "(reason, patient, callbacks, summary, intents). No SIP dial — returns "
            "handoff + speak text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the caller needs a human",
                },
                "call_summary": {
                    "type": "string",
                    "description": "Short summary of the call so far for staff",
                },
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id if already matched",
                },
                "callback_numbers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Callback phone numbers",
                },
                "callback_windows": {
                    "description": "Preferred callback window(s) as string or array of strings",
                },
                "active_intents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Active caller intents",
                },
                "parked_intents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Parked/deferred intents",
                },
                "mode": {
                    "type": "string",
                    "enum": ["transfer", "hold"],
                    "description": "transfer (default) or hold",
                },
            },
            "required": ["reason", "call_summary"],
        },
    },
]


def lab_results_disclose_enabled() -> bool:
    return os.environ.get("LIORA_LAB_RESULTS_DISCLOSE", "").strip() in {"1", "true", "yes", "on"}


def _compact_json(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":"))


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return val != 0
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _speak_result(
    *,
    status: str,
    message: str,
    speak: str | None = None,
    **extra: Any,
) -> str:
    out = {"status": status, "message": message, "speak": speak if speak is not None else message}
    out.update(extra)
    return _compact_json(out)


def _as_str_list(val: Any) -> list[str]:
    """Normalize optional string-or-array args to a list of non-empty strings."""
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, (list, tuple)):
        out: list[str] = []
        for item in val:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    s = str(val).strip()
    return [s] if s else []


@lru_cache(maxsize=1)
def _get_client():
    from liora_tools.auth.session_manager import get_ema_client

    return get_ema_client()


@lru_cache(maxsize=1)
def _get_flow():
    from liora_tools.modmed.scheduling_flow import SchedulingFlow

    return SchedulingFlow(_get_client())


def clear_ops_caches() -> None:
    _get_client.cache_clear()
    _get_flow.cache_clear()


# ── PAN / card number sanitization ───────────────────────────────────────────

# 13–19 consecutive digits (PAN-like)
_RE_LONG_DIGITS = re.compile(r"\d{13,19}")
# 4+ groups of 3–4 digits separated by space/dash (e.g. 4111-1111-1111-1111)
_RE_GROUPED_PAN = re.compile(r"(?:\d{3,4}[\s\-]){3,}\d{3,4}")


def strip_pan_like(text: str) -> str:
    """Strip PAN-like digit runs from free text. Leaves short IDs alone."""
    if not text:
        return text
    out = _RE_GROUPED_PAN.sub("[card redacted]", text)
    out = _RE_LONG_DIGITS.sub("[card redacted]", out)
    return out


def _sanitize_value(val: Any) -> Any:
    if isinstance(val, str):
        return strip_pan_like(val)
    if isinstance(val, dict):
        return {k: _sanitize_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_sanitize_value(v) for v in val]
    return val


def _insurance_from_patient(patient: dict) -> dict[str, Any]:
    """Extract a safe insurance summary from EMA patient payload if present."""
    # Common EMA field names — only surface what exists; never invent eligibility.
    candidates: list[Any] = []
    for key in (
        "insurance",
        "insurances",
        "primaryInsurance",
        "patientInsurances",
        "coverage",
        "payors",
        "guarantor",
    ):
        if key in patient and patient[key] not in (None, "", [], {}):
            candidates.append(patient[key])

    # Nested under related objects
    for key in ("primaryInsuranceCompany", "insuranceCompany", "payer"):
        if key in patient and patient[key] not in (None, "", [], {}):
            candidates.append({key: patient[key]})

    if not candidates:
        return {"on_file": False, "summary": None}

    sanitized = _sanitize_value(candidates if len(candidates) > 1 else candidates[0])
    return {"on_file": True, "summary": sanitized}


# ── Tool handlers ────────────────────────────────────────────────────────────


def triage_lab_results(arguments: dict) -> str:
    """Queue MD/callback for labs; never return clinical result content by default."""
    patient_id = arguments.get("patient_id")
    reason = (arguments.get("reason") or "").strip()
    preferred_callback = (arguments.get("preferred_callback") or "").strip()
    confirmed = _truthy(arguments.get("confirmed"))

    # Hard policy: never include clinical result content unless explicitly enabled.
    disclose = lab_results_disclose_enabled()

    if not confirmed:
        msg = (
            "I can have the doctor or office call you back about your results. "
            "Should I put that request through?"
        )
        return _speak_result(
            status="needs_confirmation",
            message=msg,
            patient_id=patient_id,
            reason=reason or None,
            preferred_callback=preferred_callback or None,
            lab_content_disclosed=False,
        )

    try:
        q = staff_enqueue(
            "lab_results_callback",
            patient_id=patient_id,
            summary=reason or "Caller asking about lab/results status",
            payload={
                "reason": reason,
                "preferred_callback": preferred_callback,
                # Never store clinical result values here
            },
        )
    except OSError as e:
        logger.exception("staff queue failed")
        msg = (
            "I'm having trouble sending that to the office right now. "
            "Please try calling the front desk directly."
        )
        return _speak_result(
            status="queue_error",
            message=msg,
            detail=str(e),
            lab_content_disclosed=False,
        )

    msg = (
        "I've put in a request — we'll have the doctor or office call you back "
        "about your results."
    )
    result_extra: dict[str, Any] = {
        "queued": True,
        "queue_kind": "lab_results_callback",
        "patient_id": patient_id,
        "preferred_callback": preferred_callback or None,
        "lab_content_disclosed": False,
    }
    # Even when disclose env is on, this tool does not fetch or return result values.
    # Explicitly omit any result_values key unless disclose is enabled AND content exists
    # (we never fetch clinical content in this path).
    if disclose:
        # Policy gate open, but we still do not invent/fetch values.
        result_extra["disclose_env"] = True
    return _speak_result(status="queued", message=msg, **result_extra)


def forms_intake_nudge(arguments: dict) -> str:
    """Portal/forms help; optional gated portal resend."""
    from liora_tools.modmed.write_gate import ema_writes_enabled

    patient_id = arguments.get("patient_id")
    confirmed = _truthy(arguments.get("confirmed"))

    verbal = (
        "We send an email from ModMed for the patient portal — "
        "please fill out your forms on there before your visit. "
        "If you can't find it, I can try to resend the invite."
    )

    portal_username = None
    portal_email = None
    has_portal_hint = False

    if patient_id is not None:
        try:
            client = _get_client()
            patient = client.get_patient(
                str(patient_id),
                selector="id,email,username,portalUsername,patientPortalUsername",
            )
            portal_email = (
                patient.get("email")
                or (patient.get("emailAddress") if isinstance(patient.get("emailAddress"), str) else None)
            )
            if isinstance(patient.get("email"), dict):
                portal_email = patient["email"].get("address") or patient["email"].get("email")
            portal_username = (
                patient.get("username")
                or patient.get("portalUsername")
                or patient.get("patientPortalUsername")
                or portal_email
            )
            has_portal_hint = bool(portal_email or portal_username)
        except Exception as e:
            logger.warning("get_patient for forms nudge failed: %s", e)
            # Continue with verbal-only path
            return _speak_result(
                status="verbal_only",
                message=verbal,
                patient_id=patient_id,
                portal_lookup_error=str(e),
                has_portal_contact=False,
            )

    if not confirmed:
        status = "needs_confirmation" if has_portal_hint else "verbal_only"
        msg = verbal
        if has_portal_hint:
            msg = (
                verbal + " I see contact info on file — want me to resend the portal invite?"
            )
        return _speak_result(
            status=status,
            message=msg,
            patient_id=patient_id,
            has_portal_contact=has_portal_hint,
            # No PHI dump — only booleans
            resend_attempted=False,
        )

    # confirmed=true → attempt write only if gate + contact available
    if not ema_writes_enabled():
        msg = (
            "I can't resend the portal email from here right now, but check your inbox "
            "for an email from ModMed and fill out the forms before your visit. "
            "Staff can resend it if needed."
        )
        return _speak_result(
            status="writes_disabled",
            message=msg,
            patient_id=patient_id,
            has_portal_contact=has_portal_hint,
            resend_attempted=False,
        )

    if not portal_email or not portal_username:
        msg = (
            "I don't have a portal email or username on file to resend. "
            "Please check for an email from ModMed, or the front desk can help set one up."
        )
        return _speak_result(
            status="missing_portal_contact",
            message=msg,
            patient_id=patient_id,
            has_portal_contact=False,
            resend_attempted=False,
        )

    try:
        client = _get_client()
        client.send_portal_email(str(patient_id), str(portal_username), str(portal_email))
    except Exception as e:
        logger.exception("send_portal_email failed")
        msg = (
            "I couldn't resend the portal invite just now. "
            "Please check for an email from ModMed, or call the office if you still don't see it."
        )
        return _speak_result(
            status="resend_failed",
            message=msg,
            detail=str(e),
            patient_id=patient_id,
            resend_attempted=True,
        )

    msg = (
        "I've resent the ModMed patient portal invite. "
        "Check your email and fill out the forms before your visit."
    )
    return _speak_result(
        status="resent",
        message=msg,
        patient_id=patient_id,
        has_portal_contact=True,
        resend_attempted=True,
    )


def flag_running_late(arguments: dict) -> str:
    """Same-day running-late flag for FD/MA via staff queue."""
    patient_id = arguments.get("patient_id")
    appointment_id = arguments.get("appointment_id")
    eta_minutes = arguments.get("eta_minutes")
    confirmed = _truthy(arguments.get("confirmed"))

    if not confirmed:
        msg = (
            "I can let the front desk know you're running late. "
            "Should I go ahead and notify them?"
        )
        return _speak_result(
            status="needs_confirmation",
            message=msg,
            patient_id=patient_id,
            appointment_id=appointment_id,
            eta_minutes=eta_minutes,
        )

    # Resolve appointment if missing and patient_id present
    matched_appt = None
    if appointment_id is None and patient_id is not None:
        try:
            flow = _get_flow()
            upcoming = flow.list_upcoming_appointments(patient_id, days_ahead=1)
            today_s = date.today().isoformat()
            today_appts = []
            for a in upcoming.get("appointments") or []:
                start = str(a.get("start") or "")
                # Match today's local calendar date prefix in ISO start
                if start.startswith(today_s) or today_s in start[:10]:
                    today_appts.append(a)
                else:
                    # scheduledStartDate may be UTC; also accept any open appt same calendar day
                    # via date prefix of start field
                    if len(start) >= 10 and start[:10] == today_s:
                        today_appts.append(a)
            if len(today_appts) == 1:
                matched_appt = today_appts[0]
                appointment_id = matched_appt.get("id")
            elif len(today_appts) > 1:
                # Prefer earliest; still enqueue with patient_id
                today_appts.sort(key=lambda x: str(x.get("start") or ""))
                matched_appt = today_appts[0]
                appointment_id = matched_appt.get("id")
            elif (upcoming.get("appointments") or []):
                # Fall back to soonest upcoming today-window list
                appts = sorted(
                    upcoming["appointments"],
                    key=lambda x: str(x.get("start") or ""),
                )
                matched_appt = appts[0]
                appointment_id = matched_appt.get("id")
        except Exception as e:
            logger.warning("list_upcoming for running_late failed: %s", e)

    try:
        q = staff_enqueue(
            "running_late",
            patient_id=patient_id,
            appointment_id=appointment_id,
            summary="Patient running late (voice)",
            payload={
                "eta_minutes": eta_minutes,
                "matched_start": (matched_appt or {}).get("start"),
            },
        )
    except OSError as e:
        logger.exception("staff queue failed")
        msg = (
            "I'm having trouble reaching the front desk system. "
            "Please call the office directly to say you're running late."
        )
        return _speak_result(
            status="queue_error",
            message=msg,
            detail=str(e),
        )

    msg = "I've let the front desk know you're running late."
    return _speak_result(
        status="queued",
        message=msg,
        queued=True,
        queue_kind="running_late",
        patient_id=patient_id,
        appointment_id=appointment_id,
        eta_minutes=eta_minutes,
        queue_path=q.get("path"),
    )


def clinic_faq(arguments: dict) -> str:
    topic = arguments.get("topic") or "all"
    result = get_topic(str(topic))
    return _compact_json(result)


def get_insurance_on_file(arguments: dict) -> str:
    """Read insurance summary; strip PAN; never invent eligibility."""
    patient_id = arguments.get("patient_id")
    if patient_id is None:
        msg = "I need the patient on file first before I can check insurance."
        return _speak_result(status="patient_id_required", message=msg)

    try:
        client = _get_client()
        # Broad selector; we sanitize whatever comes back
        patient = client.get_patient(str(patient_id))
    except Exception as e:
        logger.exception("get_patient insurance failed")
        msg = (
            "I couldn't pull insurance on file right now. "
            "Please bring your insurance card to the visit, and ask if a referral is needed "
            "for medical visits."
        )
        return _speak_result(
            status="lookup_failed",
            message=msg,
            detail=str(e),
            patient_id=patient_id,
        )

    info = _insurance_from_patient(patient if isinstance(patient, dict) else {})
    bring_card = (
        "Please bring your insurance card to the visit, "
        "and ask if a referral is needed for medical visits."
    )

    if not info["on_file"]:
        msg = (
            "I don't see insurance details on file that I can read back. " + bring_card
        )
        return _speak_result(
            status="none_on_file",
            message=msg,
            patient_id=patient_id,
            on_file=False,
            insurance=None,
            eligibility_checked=False,
            # Never invent coverage
        )

    # Speak a high-level line only — no card numbers
    summary = info["summary"]
    payer_hint = None
    if isinstance(summary, dict):
        for k in ("name", "payerName", "companyName", "insuranceCompany", "planName", "carrier"):
            if summary.get(k):
                payer_hint = strip_pan_like(str(summary[k]))
                break
        # Nested company
        for nest in ("primaryInsuranceCompany", "insuranceCompany", "payer"):
            nested = summary.get(nest)
            if isinstance(nested, dict) and nested.get("name"):
                payer_hint = strip_pan_like(str(nested["name"]))
                break
            if isinstance(nested, str) and nested.strip():
                payer_hint = strip_pan_like(nested)
                break
    elif isinstance(summary, list) and summary:
        first = summary[0]
        if isinstance(first, dict):
            for k in ("name", "payerName", "companyName", "planName"):
                if first.get(k):
                    payer_hint = strip_pan_like(str(first[k]))
                    break

    if payer_hint:
        msg = f"I see insurance on file that looks like {payer_hint}. " + bring_card
    else:
        msg = "I see some insurance information on file. " + bring_card

    return _speak_result(
        status="ok",
        message=msg,
        patient_id=patient_id,
        on_file=True,
        insurance=_sanitize_value(summary),
        eligibility_checked=False,
        # Explicit: we do not assert coverage
        coverage_asserted=False,
    )


_TRANSFER_SPEAK = (
    "I'm connecting you with someone on our team who can help with that. "
    "I've left them a note so you won't need to repeat everything."
)
_HOLD_SPEAK = (
    "I'm going to put you on a brief hold while I get a team member. "
    "I've written down what you need so they have it."
)


def transfer_to_staff(arguments: dict) -> str:
    """Warm handoff / hold — always persist structured staff note (no confirmed gate)."""
    reason = (arguments.get("reason") or "").strip() if arguments.get("reason") is not None else ""
    call_summary = (
        (arguments.get("call_summary") or "").strip()
        if arguments.get("call_summary") is not None
        else ""
    )

    if not reason or not call_summary:
        msg = (
            "I need a quick reason and a short summary of what you need "
            "before I connect you — what should I tell the team?"
        )
        return _speak_result(
            status="needs_fields",
            message=msg,
            handoff=False,
        )

    mode_raw = (arguments.get("mode") or "transfer")
    mode = str(mode_raw).strip().lower() if mode_raw is not None else "transfer"
    if mode not in ("transfer", "hold"):
        mode = "transfer"

    patient_id = arguments.get("patient_id")
    callback_numbers = _as_str_list(arguments.get("callback_numbers"))
    callback_windows_raw = arguments.get("callback_windows")
    # Keep string as string, list as list for payload fidelity; normalize empty
    if callback_windows_raw is None or callback_windows_raw == "":
        callback_windows: Any = None
    elif isinstance(callback_windows_raw, str):
        callback_windows = callback_windows_raw.strip() or None
    else:
        callback_windows = _as_str_list(callback_windows_raw) or None

    active_intents = _as_str_list(arguments.get("active_intents"))
    parked_intents = _as_str_list(arguments.get("parked_intents"))

    warm = _HOLD_SPEAK if mode == "hold" else _TRANSFER_SPEAK

    payload = {
        "reason": reason,
        "callback_numbers": callback_numbers,
        "callback_windows": callback_windows,
        "active_intents": active_intents,
        "parked_intents": parked_intents,
        "mode": mode,
        "warm_handoff_copy": warm,
    }
    extra = {
        "reason": reason,
        "call_summary": call_summary,
        "callback_numbers": callback_numbers,
        "callback_windows": callback_windows,
        "active_intents": active_intents,
        "parked_intents": parked_intents,
        "mode": mode,
        "warm_handoff_copy": warm,
        "status": "queued",
    }

    try:
        q = staff_enqueue(
            "transfer_to_staff",
            patient_id=patient_id,
            summary=call_summary,
            payload=payload,
            extra=extra,
        )
    except OSError as e:
        logger.exception("staff queue failed for transfer_to_staff")
        msg = (
            "I'm having trouble connecting you right now. "
            "Please stay on the line or call the front desk directly."
        )
        return _speak_result(
            status="queue_error",
            message=msg,
            handoff=False,
            detail=str(e),
        )

    record = q.get("record") or {}
    note_id = record.get("id")
    note_path = q.get("path")

    artifact = {
        "reason": reason,
        "patient_id": patient_id,
        "callback_numbers": callback_numbers,
        "call_summary": call_summary,
        "active_intents": active_intents,
        "parked_intents": parked_intents,
        "kind": "transfer_to_staff",
        "schema_version": record.get("schema_version", 1),
        "id": note_id,
        "mode": mode,
        "callback_windows": callback_windows,
        "status": "queued",
        "warm_handoff_copy": warm,
    }

    return _speak_result(
        status="queued",
        message="Connecting you with our team.",
        speak=warm,
        handoff=True,
        mode=mode,
        note_id=note_id,
        note_path=note_path,
        artifact=artifact,
        next_step="speak_warm_handoff_then_hold",
    )


_HANDLERS: dict[str, Callable[[dict], str]] = {
    "triage_lab_results": triage_lab_results,
    "forms_intake_nudge": forms_intake_nudge,
    "flag_running_late": flag_running_late,
    "clinic_faq": clinic_faq,
    "get_insurance_on_file": get_insurance_on_file,
    "transfer_to_staff": transfer_to_staff,
}


def handle_ops_tool(name: str, arguments: dict) -> str:
    """Execute an ops tool; return JSON string for Grok."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _speak_result(
            status="unknown_tool",
            message="I can't do that from here.",
            error="unknown_tool",
            name=name,
        )
    try:
        return handler(arguments or {})
    except Exception as e:
        logger.exception("ops tool %s failed", name)
        return _speak_result(
            status="tool_failed",
            message="Something went wrong on my end — let me have someone call you back.",
            error="ops_tool_failed",
            tool=name,
            detail=str(e),
        )


OPS_TOOL_NAMES = frozenset(_HANDLERS.keys())
