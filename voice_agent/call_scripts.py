"""Caller-facing after-hours and provider-unavailable scripts for Genie voice.

Tools return structured next-best actions + speak paths. Aligns with multi-intent
parking: parked secondary intents are preserved and re-offered after the primary
path resolves (or when the caller declines all primary options).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .clinic_hours import (
    CLINIC_NAME,
    CLINIC_PHONE_SPEAK,
    TZ,
    check_hours,
    hours_speak,
)

logger = logging.getLogger(__name__)

# Preferred real providers for alternates (never offer zzz* lab/test providers aloud).
PREFERRED_ALTERNATES = (
    "Dr. Rhee",
    "a medical provider on the schedule",
)

PROVIDER_UNAVAILABLE_REASONS = frozenset(
    {
        "no_slots",
        "provider_off",
        "teaching_day",
        "booked_out",
        "not_on_schedule",
        "requested_unavailable",
        "other",
    }
)

SCRIPT_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "check_office_hours",
        "description": (
            "Check whether the Liora front desk is open right now (America/New_York). "
            "Call at the start of inbound after-hours handling, or when the caller asks "
            "if anyone is in / office hours. Returns is_open, after_hours, next_open_speak."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "as_of": {
                    "type": "string",
                    "description": "Optional ISO datetime override (tests); default now Eastern",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "after_hours_script",
        "description": (
            "Get the after-hours caller script and next-best actions when outside "
            "front-desk hours (or staff unavailable). Offers: leave message, "
            "schedule callback for next open, FAQ hours, optional urgent guidance. "
            "Pass parked_intents so secondary requests are not dropped. "
            "Does not give clinical advice. Staff transfer is usually unavailable after hours."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "caller_goal": {
                    "type": "string",
                    "description": "Primary thing the caller wants (book, Rx, results, etc.)",
                },
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id if already matched",
                },
                "callback_number": {
                    "type": "string",
                    "description": "Best callback number if known",
                },
                "parked_intents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Secondary intents parked for later in this call",
                },
                "preferred_action": {
                    "type": "string",
                    "description": (
                        "Optional: leave_message | schedule_callback | hours_only | "
                        "none — when caller already chose"
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "True only after verbal yes to queue leave_message or schedule_callback"
                    ),
                },
                "message_summary": {
                    "type": "string",
                    "description": "Short note to staff when leaving a message / callback",
                },
                "as_of": {
                    "type": "string",
                    "description": "Optional ISO datetime override (tests)",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "provider_unavailable_script",
        "description": (
            "Script when the requested provider has no slots, is off, on a teaching day, "
            "or otherwise unavailable. Offers: alternate provider, other times, "
            "staff callback, leave message, transfer/hold to staff when office is open. "
            "Pass parked_intents so multi-intent secondaries are re-offered. "
            "Never invent clinical coverage or same-day MD promises after hours."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requested_provider": {
                    "type": "string",
                    "description": "Name the caller asked for (e.g. Dr. Rhee)",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "no_slots | provider_off | teaching_day | booked_out | "
                        "not_on_schedule | requested_unavailable | other"
                    ),
                },
                "visit_type": {
                    "type": "string",
                    "description": "Visit type if known",
                },
                "patient_id": {"type": "integer"},
                "callback_number": {"type": "string"},
                "alternate_providers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Real alternates from slot tools only; never invent",
                },
                "parked_intents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Secondary intents still pending this call",
                },
                "preferred_action": {
                    "type": "string",
                    "description": (
                        "alternate_provider | other_times | schedule_callback | "
                        "leave_message | transfer_to_staff | none"
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "True after verbal yes to queue callback/message/transfer note",
                },
                "message_summary": {"type": "string"},
                "as_of": {
                    "type": "string",
                    "description": "Optional ISO datetime override (tests)",
                },
            },
            "required": [],
        },
    },
]

SCRIPT_TOOL_NAMES = frozenset(t["name"] for t in SCRIPT_TOOL_DEFINITIONS)


def _compact_json(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":"))


def _normalize_parked(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if item is None:
                continue
            s = str(item).strip()
            if s and s not in out:
                out.append(s)
    return out


def _filter_alternates(raw: Any) -> list[str]:
    items = _normalize_parked(raw)
    clean: list[str] = []
    for name in items:
        low = name.lower().replace(" ", "")
        if low.startswith("zzz") or "phreesia" in low or "test" == low:
            continue
        if name not in clean:
            clean.append(name)
    return clean


def _parse_as_of(raw: str | None) -> datetime | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _staff_enqueue(
    kind: str,
    *,
    patient_id: Any = None,
    summary: str = "",
    payload: dict | None = None,
) -> dict[str, Any]:
    """Best-effort staff queue; never fails the script path."""
    try:
        from .staff_queue import enqueue as staff_enqueue

        return staff_enqueue(
            kind,
            patient_id=patient_id,
            summary=summary,
            payload=payload or {},
        )
    except Exception as e:
        logger.warning("staff_queue unavailable: %s", e)
        # Fallback local JSONL under /tmp so lab still has an artifact
        path = Path(os.environ.get("LIORA_STAFF_QUEUE_PATH") or "/tmp/liora-staff-queue.jsonl")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": datetime.now(TZ).isoformat(),
                "kind": kind,
                "patient_id": patient_id,
                "summary": summary,
                "payload": payload or {},
                "source": "voice_scripts",
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
            return {"queued": True, "path": str(path), "record": record}
        except OSError as e2:
            return {"queued": False, "error": str(e2)}


def _reoffer_parked(parked: list[str]) -> dict[str, Any]:
    if not parked:
        return {
            "parked_intents": [],
            "reoffer_speak": "",
            "preserve_parked": True,
        }
    if len(parked) == 1:
        reoffer = (
            f"After we wrap this, I still have your other request: {parked[0]}. "
            "Want to handle that next?"
        )
    else:
        joined = "; ".join(parked)
        reoffer = (
            f"After we wrap this, I still have your other requests: {joined}. "
            "Which should we do next?"
        )
    return {
        "parked_intents": parked,
        "reoffer_speak": reoffer,
        "preserve_parked": True,
    }


def build_after_hours_script(
    *,
    caller_goal: str = "",
    patient_id: Any = None,
    callback_number: str = "",
    parked_intents: list[str] | None = None,
    preferred_action: str = "",
    confirmed: bool = False,
    message_summary: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Structured after-hours path for the voice agent."""
    status = check_hours(now)
    parked = _normalize_parked(parked_intents)
    park_block = _reoffer_parked(parked)

    # If somehow called while open, still return coherent guidance
    if status.is_open and (preferred_action or "").strip().lower() in {"", "none"}:
        speak = (
            "The front desk is open right now. I can keep helping, or connect you "
            "with staff if you need a person."
        )
        return {
            "status": "office_open",
            "script": "after_hours",
            "hours": status.as_dict(),
            "options": [
                {"id": "continue_self_serve", "label": "Keep helping on this call"},
                {"id": "transfer_to_staff", "label": "Hold/transfer to staff when allowed"},
            ],
            "message": speak,
            "speak": speak,
            **park_block,
            "next_step": "continue_or_transfer",
            "transfer_allowed": True,
        }

    next_open = status.next_open_speak or "our next business day"
    goal = (caller_goal or "").strip()
    goal_bit = f" about {goal}" if goal else ""

    options = [
        {
            "id": "leave_message",
            "label": "Leave a message for the team",
            "speak_hint": "I can leave a note for the team to call you back.",
        },
        {
            "id": "schedule_callback",
            "label": "Callback after we open",
            "speak_hint": f"I can have someone call you back after we open — next open is {next_open}.",
        },
        {
            "id": "hours_only",
            "label": "Hear office hours",
            "speak_hint": hours_speak(),
        },
    ]

    openers = (
        f"You've reached {CLINIC_NAME} after hours. "
        f"Our team is back {next_open}. "
    )
    if goal:
        openers += f"I noted you called{goal_bit}. "

    base_speak = (
        openers
        + "I can leave a message for a callback, or note a time for someone to call you "
        f"once we're open. Our phone is {CLINIC_PHONE_SPEAK}. "
        "I can't give clinical advice or promise a same-day doctor response after hours."
    )

    action = (preferred_action or "").strip().lower()
    queued = None
    speak = base_speak

    if action in {"leave_message", "schedule_callback"}:
        if not confirmed:
            if action == "leave_message":
                speak = (
                    f"Happy to leave a message{goal_bit}. "
                    "I'll note your number and what you need, and the team will call back "
                    f"after we open ({next_open}). Does that work?"
                )
            else:
                speak = (
                    f"I can schedule a callback{goal_bit} for after we open "
                    f"({next_open}). Should I put that in for you?"
                )
            return {
                "status": "needs_confirmation",
                "script": "after_hours",
                "hours": status.as_dict(),
                "options": options,
                "preferred_action": action,
                "message": speak,
                "speak": speak,
                **park_block,
                "next_step": "confirm_then_retry",
                "transfer_allowed": False,
            }

        kind = "after_hours_message" if action == "leave_message" else "after_hours_callback"
        summary = (message_summary or goal or "After-hours caller request").strip()
        payload = {
            "caller_goal": goal,
            "callback_number": (callback_number or "").strip(),
            "parked_intents": parked,
            "next_open_speak": status.next_open_speak,
            "action": action,
        }
        queued = _staff_enqueue(
            kind,
            patient_id=patient_id,
            summary=summary,
            payload=payload,
        )
        if action == "leave_message":
            speak = (
                f"Got it — I've left a message for the team{goal_bit}. "
                f"Someone will follow up after we open ({next_open})."
            )
        else:
            speak = (
                f"You're set for a callback{goal_bit} after we open ({next_open}). "
                "We'll use the number on file unless you gave a different one."
            )
        if park_block["reoffer_speak"]:
            speak = f"{speak} {park_block['reoffer_speak']}"
        return {
            "status": "queued" if queued.get("queued") else "accepted",
            "script": "after_hours",
            "hours": status.as_dict(),
            "options": options,
            "preferred_action": action,
            "queued": queued,
            "message": speak,
            "speak": speak,
            **park_block,
            "next_step": "reoffer_parked_or_close",
            "transfer_allowed": False,
        }

    if action == "hours_only":
        speak = hours_speak()
        if park_block["reoffer_speak"]:
            speak = f"{speak} {park_block['reoffer_speak']}"
        return {
            "status": "ok",
            "script": "after_hours",
            "hours": status.as_dict(),
            "options": options,
            "preferred_action": action,
            "message": speak,
            "speak": speak,
            **park_block,
            "next_step": "reoffer_parked_or_close",
            "transfer_allowed": False,
        }

    # Default: present menu (short)
    speak = base_speak
    if park_block["reoffer_speak"]:
        speak = (
            f"{speak} I'll also keep your other request on this call so we don't lose it."
        )

    return {
        "status": "ok",
        "script": "after_hours",
        "hours": status.as_dict(),
        "options": options,
        "message": speak,
        "speak": speak,
        **park_block,
        "next_step": "offer_options",
        "transfer_allowed": False,
        "policy": {
            "no_clinical_advice": True,
            "no_same_day_md_promise": True,
            "staff_transfer_after_hours": False,
        },
    }


def build_provider_unavailable_script(
    *,
    requested_provider: str = "",
    reason: str = "requested_unavailable",
    visit_type: str = "",
    patient_id: Any = None,
    callback_number: str = "",
    alternate_providers: list[str] | None = None,
    parked_intents: list[str] | None = None,
    preferred_action: str = "",
    confirmed: bool = False,
    message_summary: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Structured path when the requested provider cannot take the booking."""
    status = check_hours(now)
    parked = _normalize_parked(parked_intents)
    park_block = _reoffer_parked(parked)
    provider = (requested_provider or "that provider").strip() or "that provider"
    reason_key = (reason or "other").strip().lower()
    if reason_key not in PROVIDER_UNAVAILABLE_REASONS:
        reason_key = "other"

    reason_speak = {
        "no_slots": f"I don't see open times with {provider} in that window",
        "provider_off": f"{provider} isn't on the schedule then",
        "teaching_day": f"{provider} is in teaching / not seeing patients then",
        "booked_out": f"{provider} is booked out for what you need",
        "not_on_schedule": f"{provider} isn't on the schedule for that visit type",
        "requested_unavailable": f"{provider} isn't available for that",
        "other": f"{provider} isn't available for that",
    }[reason_key]

    alts = _filter_alternates(alternate_providers)
    if not alts:
        # Soft defaults only as *labels* — agent must still verify slots via tools
        alts = list(PREFERRED_ALTERNATES)

    options: list[dict[str, str]] = [
        {
            "id": "alternate_provider",
            "label": "Offer another provider",
            "speak_hint": f"I can check times with {alts[0]} instead.",
        },
        {
            "id": "other_times",
            "label": "Other times with same provider",
            "speak_hint": f"I can look further out for {provider}.",
        },
        {
            "id": "schedule_callback",
            "label": "Staff callback",
            "speak_hint": "I can have the team call you back to place you.",
        },
        {
            "id": "leave_message",
            "label": "Leave a message",
            "speak_hint": "I can leave a note for the scheduling team.",
        },
    ]
    if status.is_open:
        options.append(
            {
                "id": "transfer_to_staff",
                "label": "Hold/transfer to staff",
                "speak_hint": "I can put you through to the front desk with a short summary.",
            }
        )

    visit_bit = f" for {visit_type}" if (visit_type or "").strip() else ""
    base_speak = (
        f"{reason_speak}{visit_bit}. "
        "I can check another provider, look at other times, leave a message, "
        "or set a callback"
        + (", or connect you with the front desk" if status.is_open else "")
        + "."
    )
    if not status.is_open:
        base_speak += (
            " We're outside front-desk hours, so I won't promise a same-day doctor "
            "response — message and callback are best."
        )

    action = (preferred_action or "").strip().lower()
    queued = None
    speak = base_speak

    if action in {"schedule_callback", "leave_message", "transfer_to_staff"}:
        if action == "transfer_to_staff" and not status.is_open:
            speak = (
                "Staff transfer isn't available after hours. "
                "I can leave a message or set a callback for when we open instead."
            )
            return {
                "status": "transfer_unavailable_after_hours",
                "script": "provider_unavailable",
                "reason": reason_key,
                "requested_provider": provider,
                "hours": status.as_dict(),
                "alternate_providers": alts,
                "options": [o for o in options if o["id"] != "transfer_to_staff"],
                "message": speak,
                "speak": speak,
                **park_block,
                "next_step": "offer_message_or_callback",
                "transfer_allowed": False,
            }

        if not confirmed:
            if action == "transfer_to_staff":
                speak = (
                    f"I can hold for the front desk and pass along that {provider} "
                    f"wasn't available{visit_bit}. Want me to connect you?"
                )
            elif action == "leave_message":
                speak = (
                    f"I'll leave a note that you need {provider}{visit_bit}. "
                    "Should I send that to the team?"
                )
            else:
                speak = (
                    f"I can have scheduling call you back about {provider}{visit_bit}. "
                    "Want me to set that up?"
                )
            return {
                "status": "needs_confirmation",
                "script": "provider_unavailable",
                "reason": reason_key,
                "requested_provider": provider,
                "hours": status.as_dict(),
                "alternate_providers": alts,
                "options": options,
                "preferred_action": action,
                "message": speak,
                "speak": speak,
                **park_block,
                "next_step": "confirm_then_retry",
                "transfer_allowed": status.is_open,
            }

        kind_map = {
            "leave_message": "provider_unavailable_message",
            "schedule_callback": "provider_unavailable_callback",
            "transfer_to_staff": "provider_unavailable_transfer",
        }
        kind = kind_map[action]
        summary = (
            message_summary
            or f"Provider unavailable: {provider} ({reason_key}){visit_bit}"
        ).strip()
        payload = {
            "requested_provider": provider,
            "reason": reason_key,
            "visit_type": (visit_type or "").strip(),
            "callback_number": (callback_number or "").strip(),
            "alternate_providers": alts,
            "parked_intents": parked,
            "action": action,
        }
        queued = _staff_enqueue(
            kind,
            patient_id=patient_id,
            summary=summary,
            payload=payload,
        )
        if action == "transfer_to_staff":
            speak = (
                "Okay — I'm putting you through with a short note for the desk. "
                "Please hold."
            )
        elif action == "leave_message":
            speak = "Got it — I've left that note for the scheduling team."
        else:
            speak = "You're set — scheduling will call you back to place the visit."
        if park_block["reoffer_speak"] and action != "transfer_to_staff":
            speak = f"{speak} {park_block['reoffer_speak']}"
        elif park_block["parked_intents"] and action == "transfer_to_staff":
            # Handoff must carry parked intents in the note (already in payload)
            speak = (
                f"{speak} I included your other requests in the note for staff."
            )

        return {
            "status": "queued" if queued.get("queued") else "accepted",
            "script": "provider_unavailable",
            "reason": reason_key,
            "requested_provider": provider,
            "hours": status.as_dict(),
            "alternate_providers": alts,
            "options": options,
            "preferred_action": action,
            "queued": queued,
            "message": speak,
            "speak": speak,
            **park_block,
            "next_step": (
                "transfer_handoff"
                if action == "transfer_to_staff"
                else "reoffer_parked_or_close"
            ),
            "transfer_allowed": status.is_open,
        }

    if action == "alternate_provider":
        alt_list = ", ".join(alts[:3])
        speak = (
            f"Sure — I can look at {alt_list}. "
            "I'll only offer real open times from the schedule."
        )
        if park_block["reoffer_speak"]:
            speak = f"{speak} We'll still get to your other request after this."
        return {
            "status": "ok",
            "script": "provider_unavailable",
            "reason": reason_key,
            "requested_provider": provider,
            "hours": status.as_dict(),
            "alternate_providers": alts,
            "options": options,
            "preferred_action": action,
            "message": speak,
            "speak": speak,
            **park_block,
            "next_step": "find_open_slots_alternate",
            "transfer_allowed": status.is_open,
        }

    if action == "other_times":
        speak = (
            f"Okay — I'll look further out for {provider}. "
            "Give me a second while I check the schedule."
        )
        if park_block["reoffer_speak"]:
            speak = f"{speak} I'll keep your other request parked."
        return {
            "status": "ok",
            "script": "provider_unavailable",
            "reason": reason_key,
            "requested_provider": provider,
            "hours": status.as_dict(),
            "alternate_providers": alts,
            "options": options,
            "preferred_action": action,
            "message": speak,
            "speak": speak,
            **park_block,
            "next_step": "find_open_slots_same_provider",
            "transfer_allowed": status.is_open,
        }

    # Default menu
    speak = base_speak
    if parked:
        speak = (
            f"{speak} I still have your other request saved so we won't lose it."
        )

    return {
        "status": "ok",
        "script": "provider_unavailable",
        "reason": reason_key,
        "requested_provider": provider,
        "visit_type": (visit_type or "").strip() or None,
        "hours": status.as_dict(),
        "alternate_providers": alts,
        "options": options,
        "message": speak,
        "speak": speak,
        **park_block,
        "next_step": "offer_options",
        "transfer_allowed": status.is_open,
        "policy": {
            "no_invented_slots": True,
            "no_zzz_providers": True,
            "no_same_day_md_promise_after_hours": not status.is_open,
            "reoffer_parked_after_primary": True,
        },
    }


def handle_script_tool(name: str, arguments: dict | None) -> str:
    """Dispatch after-hours / provider-unavailable tools → JSON string for Grok."""
    args = arguments or {}
    now = _parse_as_of(args.get("as_of"))

    try:
        if name == "check_office_hours":
            result = check_hours(now).as_dict()
            result["hours_speak"] = hours_speak()
            result["message"] = (
                f"We're open now. {hours_speak()}"
                if result["is_open"]
                else (
                    f"We're currently closed. Next open: {result.get('next_open_speak')}. "
                    f"{hours_speak()}"
                )
            )
            result["speak"] = result["message"]
            return _compact_json(result)

        if name == "after_hours_script":
            result = build_after_hours_script(
                caller_goal=str(args.get("caller_goal") or ""),
                patient_id=args.get("patient_id"),
                callback_number=str(args.get("callback_number") or ""),
                parked_intents=args.get("parked_intents"),
                preferred_action=str(args.get("preferred_action") or ""),
                confirmed=bool(args.get("confirmed")),
                message_summary=str(args.get("message_summary") or ""),
                now=now,
            )
            return _compact_json(result)

        if name == "provider_unavailable_script":
            result = build_provider_unavailable_script(
                requested_provider=str(args.get("requested_provider") or ""),
                reason=str(args.get("reason") or "requested_unavailable"),
                visit_type=str(args.get("visit_type") or ""),
                patient_id=args.get("patient_id"),
                callback_number=str(args.get("callback_number") or ""),
                alternate_providers=args.get("alternate_providers"),
                parked_intents=args.get("parked_intents"),
                preferred_action=str(args.get("preferred_action") or ""),
                confirmed=bool(args.get("confirmed")),
                message_summary=str(args.get("message_summary") or ""),
                now=now,
            )
            return _compact_json(result)

        return _compact_json({"error": "unknown_tool", "name": name})
    except Exception as e:
        logger.exception("script tool %s failed", name)
        return _compact_json(
            {"error": "script_tool_failed", "tool": name, "detail": str(e)}
        )


TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    t["name"]: (lambda args, n=t["name"]: handle_script_tool(n, args))
    for t in SCRIPT_TOOL_DEFINITIONS
}
