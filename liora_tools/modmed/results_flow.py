"""Lab / results request triage for Genie voice.

Policy (non-negotiable):
- NEVER return raw lab/result values to the patient via this path.
- Route to message-MD or staff callback queue only.
- No clinical advice, no billing invent, no card PAN capture.
- Explicit release/disclosure policy is out of scope (env flag stays off by default).

Dry-run / writes-off: build intended queue payload, log it, skip side effects.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from liora_tools.modmed.staff_message_queue import StaffMessageQueue, voice_dry_run
from liora_tools.modmed.write_gate import ema_writes_enabled

logger = logging.getLogger(__name__)

Route = Literal["message_md", "callback"]

# Keys that must never appear with clinical content on the patient-facing payload
_FORBIDDEN_RESULT_KEYS = frozenset(
    {
        "result_values",
        "lab_values",
        "results_content",
        "lab_results",
        "raw_results",
        "value",
        "values",
        "observation",
        "observations",
        "abnormal_flags",
        "reference_range",
        "panel_results",
    }
)


def lab_results_disclose_enabled() -> bool:
    """Explicit release path — default OFF. Out of scope for this stub."""
    return os.environ.get("LIORA_LAB_RESULTS_DISCLOSE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def strip_result_disclosure(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove any accidental clinical result keys from a tool response."""
    if lab_results_disclose_enabled():
        # Still no auto-fetch; flag only — stub never attaches values.
        out = dict(payload)
        out["disclosure_policy"] = "explicit_release_flag_on_but_stub_has_no_values"
        out["clinical_results_disclosed"] = False
        return out

    cleaned = {k: v for k, v in payload.items() if k not in _FORBIDDEN_RESULT_KEYS}
    cleaned["clinical_results_disclosed"] = False
    cleaned["disclosure_policy"] = "no_raw_results"
    return cleaned


def _normalize_route(route: str | None) -> Route:
    r = (route or "message_md").strip().lower().replace("-", "_").replace(" ", "_")
    if r in {"callback", "call_back", "call_back_queue", "staff_callback", "fd_callback"}:
        return "callback"
    if r in {"message_md", "message_md_queue", "md", "provider", "doctor", "message"}:
        return "message_md"
    # default safe path: provider message
    return "message_md"


class ResultsFlow:
    """Orchestrate results-request → MD message or callback queue."""

    def __init__(self, client=None, message_queue: StaffMessageQueue | None = None):
        self._client = client
        self._queue = message_queue or StaffMessageQueue(client)

    def request_results_triage(
        self,
        *,
        patient_id=None,
        reason: str = "",
        preferred_callback: str | None = None,
        route: str | None = "message_md",
        notes: str = "",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Accept results request intent; never return lab contents.

        route:
          - message_md → provider audience, kind=results
          - callback → staff audience, kind=results_callback
        """
        chosen = _normalize_route(route)
        reason_clean = (reason or "").strip()
        notes_clean = (notes or "").strip()
        callback = (preferred_callback or "").strip() or None

        base = {
            "tool": "triage_lab_results",
            "route": chosen,
            "patient_id": patient_id,
            "reason": reason_clean or None,
            "preferred_callback": callback,
            "erx": False,
            "prescription_written": False,
            "billing_invented": False,
            "pan_captured": False,
            "clinical_advice": False,
            "writes_enabled": ema_writes_enabled(),
            "dry_run": voice_dry_run(),
        }

        # Never fetch or attach lab panels — stub has no results API.
        base = strip_result_disclosure(base)

        if not confirmed:
            speak = (
                "I can leave a note for the doctor about your results, "
                "or have the office call you back — I won't read any results on this call. "
                "Which do you prefer?"
                if not reason_clean
                else (
                    f"Just to confirm: I'll route a results request"
                    f"{' for ' + reason_clean if reason_clean else ''}"
                    f" via {'a message to the doctor' if chosen == 'message_md' else 'a staff callback'}"
                    f"{' at ' + callback if callback else ''}. "
                    "I will not read any lab values to you. Is that OK?"
                )
            )
            return strip_result_disclosure(
                {
                    **base,
                    "status": "needs_confirmation",
                    "message": speak,
                    "speak": speak,
                    "speak_hint": speak,
                    "message_queued": False,
                    "next_action": "confirm_then_retry",
                }
            )

        if chosen == "callback":
            kind = "results_callback"
            audience = "staff"
            subject = "Results request — staff callback"
            speak_success = (
                "I've asked the office to call you back about your results. "
                "I can't share lab details on this line."
            )
        else:
            kind = "results"
            audience = "provider"
            subject = "Results request — message MD"
            speak_success = (
                "I've messaged the doctor about your results request. "
                "Someone from the office will follow up — I can't read results on this call."
            )

        body_lines = [
            f"Voice results request patient_id={patient_id}.",
            f"Route: {chosen} ({kind}).",
            "POLICY: Do not disclose raw results via voice agent. Staff/MD review only.",
        ]
        if reason_clean:
            body_lines.append(f"Patient reason (their words): {reason_clean}")
        if callback:
            body_lines.append(f"Preferred callback: {callback}")
        if notes_clean:
            body_lines.append(f"Notes: {notes_clean}")
        body_lines.append("No lab values attached. No clinical advice from voice.")

        intended = self._queue.build_record(
            kind=kind,
            patient_id=patient_id,
            subject=subject,
            body="\n".join(body_lines),
            payload={
                "route": chosen,
                "reason": reason_clean or None,
                "preferred_callback": callback,
                "notes": notes_clean or None,
                "channel": "voice",
                "no_raw_results": True,
            },
            audience=audience,
            source="voice_ops",
        )

        # Dry-run or writes-off: log intended, skip side effects
        if voice_dry_run() or not ema_writes_enabled():
            logger.info(
                "results_triage dry-run/writes-off intended_queue=%s",
                {
                    "id": intended.get("id"),
                    "kind": kind,
                    "audience": audience,
                    "patient_id": patient_id,
                    "subject": subject,
                    "route": chosen,
                },
            )
            status = "dry_run" if voice_dry_run() else "writes_disabled"
            msg = (
                "Queue write skipped (dry-run or writes off). "
                "Tell the patient you noted the request and staff will follow up — "
                "never claim results were reviewed or shared. Never read lab values."
            )
            return strip_result_disclosure(
                {
                    **base,
                    "status": status,
                    "error": status,
                    "message": msg,
                    "speak": (
                        "I've noted your results request for the office. "
                        "Someone will follow up — I can't share lab details on this call."
                    ),
                    "speak_hint": (
                        "I've noted your results request for the office. "
                        "Someone will follow up — I can't share lab details on this call."
                    ),
                    "message_queued": False,
                    "intended_queue": {
                        "id": intended.get("id"),
                        "kind": kind,
                        "audience": audience,
                        "subject": subject,
                        "patient_id": patient_id,
                        "route": chosen,
                        "body_preview": (intended.get("body") or "")[:240],
                    },
                    "next_action": "close_or_other_intent",
                }
            )

        try:
            queued = self._queue.enqueue(
                kind=kind,
                patient_id=patient_id,
                subject=subject,
                body="\n".join(body_lines),
                payload={
                    "route": chosen,
                    "reason": reason_clean or None,
                    "preferred_callback": callback,
                    "notes": notes_clean or None,
                    "channel": "voice",
                    "no_raw_results": True,
                },
                audience=audience,
                source="voice_ops",
            )
        except Exception as e:
            from liora_tools.exceptions import WriteGatedError

            if isinstance(e, WriteGatedError):
                logger.info(
                    "results_triage gated intended kind=%s patient_id=%s: %s",
                    kind,
                    patient_id,
                    e,
                )
                return strip_result_disclosure(
                    {
                        **base,
                        "status": "writes_disabled",
                        "error": "writes_disabled",
                        "detail": str(e),
                        "message_queued": False,
                        "intended_queue": {
                            "kind": kind,
                            "audience": audience,
                            "subject": subject,
                            "patient_id": patient_id,
                            "route": chosen,
                        },
                        "message": (
                            "Cannot queue the results request right now. "
                            "Offer a staff callback — never read results."
                        ),
                        "speak": (
                            "I wasn't able to file that electronically. "
                            "I'll have the office call you back about your results."
                        ),
                        "speak_hint": (
                            "I wasn't able to file that electronically. "
                            "I'll have the office call you back about your results."
                        ),
                        "writes_enabled": False,
                        "next_action": "offer_callback_verbal",
                    }
                )
            raise

        return strip_result_disclosure(
            {
                **base,
                "status": "message_queued",
                "kind": kind,
                "audience": audience,
                "message_queued": True,
                "queue": {
                    "message_id": queued.get("message_id"),
                    "status": queued.get("status"),
                    "kind": queued.get("kind"),
                    "audience": queued.get("audience"),
                    "delivery": queued.get("delivery"),
                },
                "message": speak_success,
                "speak": speak_success,
                "speak_hint": speak_success,
                "writes_enabled": True,
                "dry_run": False,
                "next_action": "close_or_other_intent",
            }
        )
