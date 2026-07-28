"""Product/retail + thin Rx refill triage for Genie voice.

Safety rules:
- Voice NEVER e-prescribes or claims a script was sent.
- Product/retail (shampoo etc.) → inventory/staff path (office stock, no eRx).
- Rx refill (thin here) → provider message only; sibling owns full 12mo lapse.
"""

from __future__ import annotations

from typing import Any

from liora_tools.modmed.staff_message_queue import StaffMessageQueue, speak_hint_for
from liora_tools.modmed.write_gate import ema_writes_enabled


class RefillFlow:
    """Orchestrate staff message queue for product + thin Rx refill paths."""

    def __init__(self, client, scheduling_flow=None, message_queue: StaffMessageQueue | None = None):
        self._client = client
        self._flow = scheduling_flow  # optional; product path never needs it
        self._queue = message_queue or StaffMessageQueue(client)

    def request_product_refill(
        self,
        *,
        product_name: str,
        patient_id=None,
        quantity: str | None = None,
        notes: str = "",
        confirmed: bool = False,
        pharmacy=None,  # ignored — product path is office inventory, not pharmacy eRx
        **_ignored,
    ) -> dict[str, Any]:
        """Office retail/product (shampoo etc.) → inventory/staff message. Not eRx.

        No visit lapse check. Never includes pharmacy on the product path.
        """
        # Explicitly ignore pharmacy even if passed (routing contrast vs Rx path)
        _ = pharmacy

        product = (product_name or "").strip()
        if not product:
            return {
                "status": "need_product",
                "message": "Ask which office product they need (e.g. dandruff shampoo).",
                "erx": False,
                "prescription_written": False,
                "writes_enabled": ema_writes_enabled(),
            }

        if not confirmed:
            return {
                "status": "needs_confirmation",
                "message": (
                    f"Confirm messaging the front desk about product '{product}'"
                    + (f" qty {quantity}" if quantity else "")
                    + ". Staff will check inventory/stock and call back."
                ),
                "product_name": product,
                "quantity": quantity,
                "erx": False,
                "prescription_written": False,
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

        # Never put pharmacy on product payload
        payload = {
            "product_name": product,
            "quantity": quantity,
            "notes": notes,
            "channel": "voice",
        }

        try:
            queued = self._queue.enqueue(
                kind="product_refill",
                patient_id=patient_id,
                subject=f"Product/office stock: {product}",
                body="\n".join(body_lines),
                payload=payload,
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
                    "prescription_written": False,
                    "message_queued": False,
                    "message": (
                        "Cannot queue inventory message right now. "
                        "Offer a staff callback."
                    ),
                    "writes_enabled": False,
                }
            raise

        hint = queued.get("speak_hint") or speak_hint_for(
            kind="product_refill", audience="inventory"
        )
        return {
            "status": "message_queued",
            "kind": "product_refill",
            "audience": "inventory",
            "product_name": product,
            "quantity": quantity,
            "erx": False,
            "prescription_written": False,
            "message_queued": True,
            "queue": queued,
            "speak_hint": hint,
            "writes_enabled": True,
            "next_action": "close_or_other_intent",
        }

    def request_rx_refill(
        self,
        *,
        patient_id,
        medication: str,
        pharmacy: str | None = None,
        notes: str = "",
        provider_name: str | None = None,
        confirmed: bool = False,
        skip_lapse_check: bool = True,
        **_ignored,
    ) -> dict[str, Any]:
        """Thin Rx refill → provider MESSAGE only. Never eRx.

        For routing contrast with product path. Sibling owns full 12mo lapse;
        when skip_lapse_check=True (default here), queue after confirm only.
        """
        med = (medication or "").strip()
        if not med:
            return {
                "status": "need_medication",
                "message": "Ask which medication they need refilled.",
                "erx": False,
                "prescription_written": False,
                "writes_enabled": ema_writes_enabled(),
            }

        # Thin path: no lapse evaluation when skip_lapse_check (default True).
        # Sibling worktree owns the full 12mo policy when skip_lapse_check=False.
        if not skip_lapse_check and self._flow is not None:
            # Optional hook if a fuller scheduling flow is injected later.
            check = getattr(self._flow, "check_visit_lapse", None) or getattr(
                self, "check_visit_lapse", None
            )
            if callable(check):
                lapse = check(patient_id)
                if not lapse.get("eligible", True):
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
                    f"Confirm you should message the provider team about a refill for {med}"
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

        hint = queued.get("speak_hint") or speak_hint_for(
            kind="rx_refill", audience="provider"
        )
        return {
            "status": "message_queued",
            "kind": "rx_refill",
            "audience": "provider",
            "medication": med,
            "pharmacy": pharmacy,
            "erx": False,
            "prescription_written": False,
            "message_queued": True,
            "queue": queued,
            "speak_hint": hint,
            "writes_enabled": True,
            "next_action": "close_or_other_intent",
        }
