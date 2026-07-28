"""Rx + product refill triage for Genie voice.

Safety rules (practice FD policy from Weave n=110 sample):
- Voice NEVER e-prescribes or claims a script was sent.
- Rx refill → structured staff/provider message only (after verbal confirm + writes gate).
- ~12 month visit lapse (no completed/checked-out visit in window) → refuse remote
  refill and offer booking handoff.
- Product/retail (shampoo etc.) → separate inventory/staff path (no eRx).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from liora_tools.modmed.staff_message_queue import StaffMessageQueue
from liora_tools.modmed.write_gate import ema_writes_enabled

# ~12 calendar months; env override for tests
DEFAULT_LAPSE_DAYS = 365

# Statuses that count as a real in-office (or completed) visit for refill eligibility
_COMPLETED_STATUSES = frozenset({
    "CHECKED_OUT",
    "COMPLETED",
    "CHECKOUT",
    "SEEN",
    "FINALIZED",
})


def lapse_days() -> int:
    import os

    raw = (os.environ.get("LIORA_REFILL_LAPSE_DAYS") or "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return DEFAULT_LAPSE_DAYS


def _parse_date(val) -> date | None:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "T" in s:
            # normalize +0000
            if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
                s = s[:-2] + ":" + s[-2:]
            dt = datetime.fromisoformat(s)
            return dt.date()
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def evaluate_lapse(
    appointments: list[dict],
    *,
    as_of: date | None = None,
    window_days: int | None = None,
) -> dict[str, Any]:
    """Pure lapse policy over appointment summaries.

    Prefers CHECKED_OUT/completed statuses; if none, falls back to any non-cancelled
    past visit date (conservative: still counts as 'seen' if chart shows a visit).
    """
    today = as_of or date.today()
    days = window_days if window_days is not None else lapse_days()
    cutoff = today - timedelta(days=days)

    completed: list[tuple[date, dict]] = []
    any_visit: list[tuple[date, dict]] = []

    for a in appointments or []:
        st = (a.get("status") or "").upper()
        if st == "CANCELLED":
            continue
        d = (
            _parse_date(a.get("local_date"))
            or _parse_date(a.get("start_date"))
            or _parse_date(a.get("start"))
            or _parse_date(a.get("scheduledStartDate"))
        )
        if not d or d > today:
            continue
        any_visit.append((d, a))
        if st in _COMPLETED_STATUSES or not st:
            # blank status on historical list often means completed in EMA exports
            if st in _COMPLETED_STATUSES or st == "":
                completed.append((d, a))

    pool = completed if completed else any_visit
    if not pool:
        return {
            "eligible": False,
            "reason": "no_visit_history",
            "status": "no_visit_history",
            "window_days": days,
            "cutoff_date": cutoff.isoformat(),
            "last_visit_date": None,
            "last_visit": None,
            "days_since_last_visit": None,
            "speak_hint": (
                "I don't see a recent visit on file. For prescriptions we need you "
                "to come in — I can help book something soon."
            ),
            "next_action": "offer_book",
        }

    pool.sort(key=lambda x: x[0], reverse=True)
    last_d, last_a = pool[0]
    days_since = (today - last_d).days

    if last_d < cutoff:
        return {
            "eligible": False,
            "reason": "lapsed",
            "status": "lapsed",
            "window_days": days,
            "cutoff_date": cutoff.isoformat(),
            "last_visit_date": last_d.isoformat(),
            "last_visit": {
                "id": last_a.get("id"),
                "date": last_d.isoformat(),
                "status": last_a.get("status"),
                "type_name": last_a.get("type_name") or last_a.get("appointmentTypeName"),
            },
            "days_since_last_visit": days_since,
            "speak_hint": (
                f"It's been over a year since your last visit"
                f"{' on ' + last_d.strftime('%B %d, %Y') if last_d else ''}. "
                "We can't send a remote refill without seeing you — "
                "I can book the soonest appointment."
            ),
            "next_action": "offer_book",
        }

    return {
        "eligible": True,
        "reason": "in_window",
        "status": "eligible",
        "window_days": days,
        "cutoff_date": cutoff.isoformat(),
        "last_visit_date": last_d.isoformat(),
        "last_visit": {
            "id": last_a.get("id"),
            "date": last_d.isoformat(),
            "status": last_a.get("status"),
            "type_name": last_a.get("type_name") or last_a.get("appointmentTypeName"),
        },
        "days_since_last_visit": days_since,
        "speak_hint": (
            "You're eligible for a refill request. I'll message the provider team — "
            "they review these; it is not an automatic prescription."
        ),
        "next_action": "queue_message",
    }


class RefillFlow:
    """Orchestrate lapse check + staff message queue for refills."""

    def __init__(self, client, scheduling_flow=None, message_queue: StaffMessageQueue | None = None):
        self._client = client
        if scheduling_flow is None:
            from liora_tools.modmed.scheduling_flow import SchedulingFlow

            scheduling_flow = SchedulingFlow(client)
        self._flow = scheduling_flow
        self._queue = message_queue or StaffMessageQueue(client)

    def check_visit_lapse(
        self,
        patient_id,
        *,
        window_days: int | None = None,
        days_back: int | None = None,
    ) -> dict[str, Any]:
        """Load past appointments and evaluate ~12mo policy."""
        days = window_days if window_days is not None else lapse_days()
        lookback = days_back if days_back is not None else max(days + 30, 400)
        past = self._flow.list_past_appointments(
            patient_id,
            days_back=lookback,
            limit=25,
            include_cancelled=False,
        )
        result = evaluate_lapse(
            past.get("appointments") or [],
            window_days=days,
        )
        result["patient_id"] = patient_id
        result["past_count"] = past.get("count")
        result["writes_enabled"] = ema_writes_enabled()
        result["erx"] = False
        return result

    def request_rx_refill(
        self,
        *,
        patient_id,
        medication: str,
        pharmacy: str | None = None,
        notes: str = "",
        provider_name: str | None = None,
        confirmed: bool = False,
        window_days: int | None = None,
        skip_lapse_check: bool = False,
    ) -> dict[str, Any]:
        """Rx refill → staff/provider message only. Never eRx."""
        med = (medication or "").strip()
        if not med:
            return {
                "status": "need_medication",
                "message": "Ask which medication they need refilled.",
                "erx": False,
                "prescription_written": False,
                "writes_enabled": ema_writes_enabled(),
            }

        if not skip_lapse_check:
            lapse = self.check_visit_lapse(patient_id, window_days=window_days)
            if not lapse.get("eligible"):
                return {
                    "status": lapse.get("status") or "lapsed",
                    "reason": lapse.get("reason"),
                    "lapse": lapse,
                    "erx": False,
                    "prescription_written": False,
                    "message_queued": False,
                    "next_action": "offer_book",
                    "speak_hint": lapse.get("speak_hint"),
                    "writes_enabled": ema_writes_enabled(),
                }

        if not confirmed:
            return {
                "status": "needs_confirmation",
                "message": (
                    f"Confirm you should message the provider about a refill for {med}"
                    + (f" at {pharmacy}" if pharmacy else "")
                    + ". This does not send a prescription — only a staff message."
                ),
                "medication": med,
                "pharmacy": pharmacy,
                "erx": False,
                "prescription_written": False,
                "writes_enabled": ema_writes_enabled(),
            }

        body_lines = [
            f"Voice refill request for patient_id={patient_id}.",
            f"Medication: {med}",
        ]
        if pharmacy:
            body_lines.append(f"Pharmacy: {pharmacy}")
        if provider_name:
            body_lines.append(f"Preferred provider: {provider_name}")
        if notes:
            body_lines.append(f"Notes: {notes}")
        body_lines.append("Do NOT auto-prescribe from voice. Review chart and act in EMA.")

        try:
            queued = self._queue.enqueue(
                kind="rx_refill",
                patient_id=patient_id,
                subject=f"Rx refill request: {med}",
                body="\n".join(body_lines),
                payload={
                    "medication": med,
                    "pharmacy": pharmacy,
                    "provider_name": provider_name,
                    "notes": notes,
                    "channel": "voice",
                },
                audience="provider",
            )
        except Exception as e:
            from liora_tools.exceptions import WriteGatedError

            if isinstance(e, WriteGatedError):
                return {
                    "status": "writes_disabled",
                    "error": "writes_disabled",
                    "detail": str(e),
                    "erx": False,
                    "prescription_written": False,
                    "message_queued": False,
                    "message": (
                        "Cannot queue the provider message right now. "
                        "Offer a staff callback — never claim the refill was sent."
                    ),
                    "writes_enabled": False,
                }
            raise

        return {
            "status": "message_queued",
            "kind": "rx_refill",
            "medication": med,
            "pharmacy": pharmacy,
            "erx": False,
            "prescription_written": False,
            "message_queued": True,
            "queue": queued,
            "speak_hint": queued.get("speak_hint"),
            "writes_enabled": True,
            "next_action": "close_or_other_intent",
        }

    def request_product_refill(
        self,
        *,
        product_name: str,
        patient_id=None,
        quantity: str | None = None,
        notes: str = "",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Office retail/product (shampoo etc.) → inventory/staff message. Not eRx."""
        product = (product_name or "").strip()
        if not product:
            return {
                "status": "need_product",
                "message": "Ask which office product they need (e.g. dandruff shampoo).",
                "erx": False,
                "writes_enabled": ema_writes_enabled(),
            }

        if not confirmed:
            return {
                "status": "needs_confirmation",
                "message": (
                    f"Confirm messaging the office about product '{product}'"
                    + (f" qty {quantity}" if quantity else "")
                    + ". Staff will check inventory and call back."
                ),
                "product_name": product,
                "quantity": quantity,
                "erx": False,
                "writes_enabled": ema_writes_enabled(),
            }

        body_lines = [
            f"Voice product/retail request patient_id={patient_id}.",
            f"Product: {product}",
        ]
        if quantity:
            body_lines.append(f"Quantity: {quantity}")
        if notes:
            body_lines.append(f"Notes: {notes}")
        body_lines.append("Office inventory / front desk — not a prescription.")

        try:
            queued = self._queue.enqueue(
                kind="product_refill",
                patient_id=patient_id,
                subject=f"Product/office stock: {product}",
                body="\n".join(body_lines),
                payload={
                    "product_name": product,
                    "quantity": quantity,
                    "notes": notes,
                    "channel": "voice",
                },
                audience="inventory",
            )
        except Exception as e:
            from liora_tools.exceptions import WriteGatedError

            if isinstance(e, WriteGatedError):
                return {
                    "status": "writes_disabled",
                    "error": "writes_disabled",
                    "detail": str(e),
                    "erx": False,
                    "message_queued": False,
                    "message": (
                        "Cannot queue inventory message right now. "
                        "Offer a staff callback."
                    ),
                    "writes_enabled": False,
                }
            raise

        return {
            "status": "message_queued",
            "kind": "product_refill",
            "product_name": product,
            "quantity": quantity,
            "erx": False,
            "prescription_written": False,
            "message_queued": True,
            "queue": queued,
            "speak_hint": (
                "I left a note for the front desk about that product. "
                "Someone will check stock and get back to you."
            ),
            "writes_enabled": True,
            "next_action": "close_or_other_intent",
        }
