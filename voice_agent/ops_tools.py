"""P2 ops glue tools for Genie voice (forms/portal first).

Writes that hit EMA (portal resend) require confirmed=true AND EMA_WRITES_ENABLED.
Dry-run / writes-off never side-effect; they report what would be sent.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Verbal scripts — no invented portal URLs; no clinical/billing content.
SPEAK_PORTAL_INACTIVE = (
    "We sent an email from ModMed with a link to create your patient portal account. "
    "Once you're in, the intake forms are right there — please fill them out before "
    "your visit so check-in goes smoothly."
)
SPEAK_PORTAL_ACTIVE_FORMS = (
    "Your ModMed patient portal should already be set up. Please log in and complete "
    "any remaining intake forms before your visit. If you can't find the email or "
    "reset link, I can resend the portal invite."
)
SPEAK_NO_EMAIL = (
    "I don't have a good email on file to send the portal invite. Please check spam "
    "for a message from ModMed, or call the office so front desk can update your email "
    "and resend the link."
)
SPEAK_RESENT = (
    "I've resent the ModMed patient portal invite to the email we have on file. "
    "Check inbox and spam, then finish the forms before your visit."
)
SPEAK_WOULD_RESEND = (
    "I can resend the ModMed portal invite to the email on file once that's confirmed. "
    "For now, please look for the ModMed email and complete your forms before the visit."
)
SPEAK_NEED_CONFIRM = (
    "I can resend the portal invite — just say yes if you'd like me to send it again."
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


OPS_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "forms_intake_nudge",
        "description": (
            "Check whether the patient's ModMed portal/intake looks incomplete and "
            "optionally resend the portal invite email. Use when the caller asks about "
            "forms, paperwork, portal link, or pre-visit intake. "
            "Does NOT give clinical advice or discuss balances/billing. "
            "Resend only when confirmed=true (and writes are enabled); otherwise returns "
            "verbal instructions the agent can speak."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id from lookup_patient (preferred)",
                },
                "resend": {
                    "type": "boolean",
                    "description": (
                        "If true, attempt portal invite resend (still needs confirmed=true). "
                        "Default false = status check + verbal nudge only."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Caller verbally confirmed they want the portal invite resent. "
                        "Required for any actual resend write."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "If true, never call EMA write APIs; report what would be sent. "
                        "Also honored when EMA_WRITES_ENABLED is off."
                    ),
                },
                "email_override": {
                    "type": "string",
                    "description": (
                        "Optional email if chart email is missing and caller just gave one. "
                        "Not stored permanently by this tool — used only for this resend."
                    ),
                },
            },
            "required": [],
        },
    },
]


def ops_tools_enabled() -> bool:
    raw = os.environ.get("EMA_VOICE_TOOLS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _compact_json(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":"))


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _mask_email(email: Optional[str]) -> Optional[str]:
    """Mask local part for logs/tool payload (keep domain for staff clarity)."""
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked = "*" * len(local)
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked}@{domain}"


def _pick_email(patient: dict, override: Optional[str] = None) -> Optional[str]:
    if override and _EMAIL_RE.match(override.strip()):
        return override.strip()
    for key in ("email", "preferredEmail", "portalEmail"):
        val = patient.get(key)
        if isinstance(val, str) and _EMAIL_RE.match(val.strip()):
            return val.strip()
        if isinstance(val, dict):
            inner = val.get("email") or val.get("address")
            if isinstance(inner, str) and _EMAIL_RE.match(inner.strip()):
                return inner.strip()
    return None


def _pick_username(patient: dict, email: Optional[str]) -> Optional[str]:
    username = patient.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip()
    # Practice convention: portal username is typically the email
    return email


def assess_portal_forms(patient: dict) -> dict[str, Any]:
    """Derive actionable portal/forms status from an EMA patient payload.

    EMA exposes portal activation via ``username`` (present ⇒ portal account exists).
    Form *completion* is not a reliable public field on get_patient — when portal is
    active we report forms_status=unknown and still give the fill-before-visit nudge.
    """
    username_raw = patient.get("username")
    has_username = isinstance(username_raw, str) and bool(username_raw.strip())
    email = _pick_email(patient)
    has_email = email is not None

    if has_username:
        portal_status = "active"
        # No trusted completion flag → treat as "may still need forms"
        forms_status = "unknown"
        incomplete = True  # still nudge; practice wants pre-visit forms
        actionable = "nudge_complete_forms"
        speak = SPEAK_PORTAL_ACTIVE_FORMS
    else:
        portal_status = "inactive"
        forms_status = "incomplete"
        incomplete = True
        actionable = "activate_portal_and_complete_forms"
        speak = SPEAK_PORTAL_INACTIVE if has_email else SPEAK_NO_EMAIL

    return {
        "portal_status": portal_status,
        "forms_status": forms_status,
        "incomplete": incomplete,
        "has_username": has_username,
        "has_email": has_email,
        "email_masked": _mask_email(email),
        "actionable": actionable,
        "speak": speak,
        "message": speak,
    }


def _get_ema_client():
    from liora_tools.auth.session_manager import get_ema_client

    return get_ema_client()


def forms_intake_nudge(
    *,
    patient_id: Any = None,
    resend: Any = False,
    confirmed: Any = False,
    dry_run: Any = False,
    email_override: Optional[str] = None,
    client=None,
) -> dict[str, Any]:
    """Check portal/forms incomplete status; optionally resend portal invite.

    Returns a dict with speak/message on every path. Never invents clinical advice
    or billing content.
    """
    want_resend = _truthy(resend)
    is_confirmed = _truthy(confirmed)
    is_dry = _truthy(dry_run)

    from liora_tools.modmed.write_gate import ema_writes_enabled

    writes_on = ema_writes_enabled()

    base: dict[str, Any] = {
        "tool": "forms_intake_nudge",
        "patient_id": patient_id,
        "resend_requested": want_resend,
        "confirmed": is_confirmed,
        "dry_run": is_dry,
        "writes_enabled": writes_on,
        "clinical_advice": False,
        "billing": False,
    }

    patient: dict[str, Any] = {}
    if patient_id is not None and str(patient_id).strip() != "":
        try:
            ema = client if client is not None else _get_ema_client()
            patient = ema.get_patient(
                str(patient_id),
                selector="id,firstName,lastName,email,username,mrn",
            ) or {}
        except Exception as e:
            logger.exception("forms_intake_nudge get_patient failed")
            return {
                **base,
                "status": "ema_error",
                "error": "ema_unavailable",
                "detail": str(e),
                "incomplete": True,
                "actionable": "verbal_only",
                "speak": SPEAK_PORTAL_INACTIVE,
                "message": SPEAK_PORTAL_INACTIVE,
                "hint": "Could not read chart; give verbal portal instructions only.",
            }
    else:
        # No patient id — still return verbal path (actionable for agent)
        assessment = {
            "portal_status": "unknown",
            "forms_status": "unknown",
            "incomplete": True,
            "has_username": False,
            "has_email": bool(email_override and _EMAIL_RE.match(str(email_override).strip())),
            "email_masked": _mask_email(email_override) if email_override else None,
            "actionable": "verbal_only_no_patient",
            "speak": SPEAK_PORTAL_INACTIVE,
            "message": SPEAK_PORTAL_INACTIVE,
        }
        out = {**base, "status": "check_ok", **assessment}
        if want_resend:
            out["status"] = "patient_id_required"
            out["speak"] = (
                "I need to pull up your chart before I can resend the portal invite. "
                "Can I get your date of birth?"
            )
            out["message"] = out["speak"]
            out["error"] = "patient_id_required"
        return out

    assessment = assess_portal_forms(patient)
    email = _pick_email(patient, email_override)
    username = _pick_username(patient, email)
    # Recompute mask if override supplied
    if email_override:
        assessment["has_email"] = email is not None
        assessment["email_masked"] = _mask_email(email)
        if not assessment["has_username"] and assessment["has_email"]:
            assessment["speak"] = SPEAK_PORTAL_INACTIVE
            assessment["message"] = SPEAK_PORTAL_INACTIVE

    out = {**base, "status": "check_ok", **assessment}

    if not want_resend:
        return out

    # --- Resend path ---
    if not email or not username:
        out["status"] = "missing_email"
        out["resend"] = {"attempted": False, "reason": "missing_email_or_username"}
        out["speak"] = SPEAK_NO_EMAIL
        out["message"] = SPEAK_NO_EMAIL
        out["actionable"] = "update_email_then_resend"
        return out

    would_send = {
        "endpoint": f"POST /ema/ws/v3/patients/{patient_id}/portal",
        "username": username if username == email else _mask_email(username) or username,
        "email_masked": _mask_email(email),
        # never echo full email in tool output when it came from chart — override is caller-given
        "note": "cellPhone omitted (EMA 500 if present)",
    }
    # Prefer not leaking username when it's a full email
    if "@" in str(username):
        would_send["username"] = _mask_email(str(username))

    if not is_confirmed:
        out["status"] = "needs_confirmation"
        out["resend"] = {
            "attempted": False,
            "reason": "needs_confirmation",
            "would_send": would_send,
        }
        out["speak"] = SPEAK_NEED_CONFIRM
        out["message"] = SPEAK_NEED_CONFIRM
        out["actionable"] = "confirm_then_resend"
        return out

    if is_dry or not writes_on:
        out["status"] = "would_resend" if is_dry else "writes_disabled"
        out["resend"] = {
            "attempted": False,
            "reason": "dry_run" if is_dry else "writes_disabled",
            "would_send": would_send,
        }
        out["speak"] = SPEAK_WOULD_RESEND
        out["message"] = SPEAK_WOULD_RESEND
        out["actionable"] = "dry_run_report" if is_dry else "enable_writes_to_resend"
        return out

    # Live resend
    try:
        ema = client if client is not None else _get_ema_client()
        ema.send_portal_email(str(patient_id), username, email)
    except Exception as e:
        # WriteGatedError should be rare here (we pre-checked), but handle any API error
        from liora_tools.exceptions import WriteGatedError

        if isinstance(e, WriteGatedError):
            out["status"] = "writes_disabled"
            out["resend"] = {
                "attempted": False,
                "reason": "writes_disabled",
                "would_send": would_send,
                "detail": str(e),
            }
            out["speak"] = SPEAK_WOULD_RESEND
            out["message"] = SPEAK_WOULD_RESEND
            return out
        logger.exception("send_portal_email failed")
        out["status"] = "resend_failed"
        out["error"] = "resend_failed"
        out["detail"] = str(e)
        out["resend"] = {"attempted": True, "ok": False, "would_send": would_send}
        out["speak"] = (
            "I wasn't able to resend that just now. Please check spam for ModMed, "
            "or the front desk can resend the portal link."
        )
        out["message"] = out["speak"]
        out["actionable"] = "verbal_fallback_after_error"
        return out

    out["status"] = "resent"
    out["resend"] = {
        "attempted": True,
        "ok": True,
        "email_masked": _mask_email(email),
    }
    out["speak"] = SPEAK_RESENT
    out["message"] = SPEAK_RESENT
    out["actionable"] = "forms_after_resend"
    out["incomplete"] = True
    return out


def handle_ops_tool(name: str, arguments: dict) -> str:
    """Execute an ops tool; return JSON string for Grok."""
    args = arguments or {}
    try:
        if name == "forms_intake_nudge":
            result = forms_intake_nudge(
                patient_id=args.get("patient_id"),
                resend=args.get("resend", False),
                confirmed=args.get("confirmed", False),
                dry_run=args.get("dry_run", False),
                email_override=args.get("email_override"),
            )
        else:
            return _compact_json({"error": "unknown_ops_tool", "name": name})
        return _compact_json(result)
    except Exception as e:
        logger.exception("ops tool %s failed", name)
        return _compact_json(
            {
                "error": "ops_tool_failed",
                "tool": name,
                "detail": str(e),
                "speak": SPEAK_PORTAL_INACTIVE,
                "message": SPEAK_PORTAL_INACTIVE,
            }
        )


TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    t["name"]: (lambda args, n=t["name"]: handle_ops_tool(n, args))
    for t in OPS_TOOL_DEFINITIONS
}
