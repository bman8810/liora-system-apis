"""Structured staff/provider message queue for voice triage.

Voice never e-prescribes. Product/retail and Rx refill requests land here as
structured messages for front desk / inventory / provider review.

Delivery backends (in order when writes enabled):
1. Optional EMA staff-message hook on EmaClient (if implemented)
2. Durable JSONL queue file (always when writes enabled)
3. Optional Genies Bottle process report (best-effort)

When EMA_WRITES_ENABLED is off, enqueue is blocked (same gate as other writes).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from liora_tools.modmed.write_gate import ema_writes_enabled, require_ema_writes


def default_queue_path() -> Path:
    raw = (os.environ.get("LIORA_STAFF_MESSAGE_QUEUE") or "").strip()
    if raw:
        return Path(raw)
    creds = (os.environ.get("LIORA_CREDENTIALS_DIR") or "").strip()
    if creds:
        return Path(creds) / "staff_message_queue.jsonl"
    return Path("/opt/data/workspace/liora/cache/staff_messages/queue.jsonl")


def speak_hint_for(*, kind: str, audience: str) -> str:
    """Audience-specific spoken guidance. Never claim Rx/eRx was sent."""
    aud = (audience or "").strip().lower()
    k = (kind or "").strip().lower()

    if aud == "inventory" or k == "product_refill":
        return (
            "I left a note for the front desk about that product. "
            "Someone will check stock and call you back."
        )

    if aud == "provider" or k == "rx_refill":
        return (
            "I messaged the provider team about this. They usually review "
            "by the next business day — never an automatic prescription."
        )

    # staff / general
    return (
        "I left a note for the office team. Someone will follow up — "
        "usually by the next business day."
    )


class StaffMessageQueue:
    """Persist structured staff messages; never claim Rx was sent."""

    def __init__(
        self,
        client=None,
        *,
        queue_path: str | Path | None = None,
    ):
        self._client = client
        self._queue_path = Path(queue_path) if queue_path else default_queue_path()

    def enqueue(
        self,
        *,
        kind: str,
        patient_id=None,
        subject: str,
        body: str,
        payload: dict | None = None,
        audience: str = "provider",
        source: str = "voice_agent",
    ) -> dict[str, Any]:
        """Queue a staff/provider message. Gated by EMA_WRITES_ENABLED.

        kind: product_refill | rx_refill | general
        audience: provider | staff | inventory
        """
        require_ema_writes(f"staff_message:{kind}")

        now = datetime.now(timezone.utc)
        message_id = f"smq_{uuid.uuid4().hex[:16]}"
        record = {
            "id": message_id,
            "kind": kind,
            "audience": audience,
            "patient_id": patient_id,
            "subject": subject,
            "body": body,
            "payload": payload or {},
            "source": source,
            "created_at": now.isoformat(),
            "status": "queued",
            "erx": False,
            "prescription_written": False,
        }

        delivery: dict[str, Any] = {"jsonl": False, "ema": None, "genies_bottle": None}

        # 1) Optional live EMA API if client exposes it
        if self._client is not None and hasattr(self._client, "send_staff_message"):
            try:
                ema_result = self._client.send_staff_message(record)
                delivery["ema"] = {"ok": True, "result": ema_result}
                record["status"] = "submitted_ema"
            except Exception as e:
                delivery["ema"] = {"ok": False, "error": str(e)[:300]}

        # 2) Always durable JSONL when writes allowed
        self._append_jsonl(record)
        delivery["jsonl"] = True
        delivery["path"] = str(self._queue_path)

        # 3) Best-effort Bottle so ops can see the request
        delivery["genies_bottle"] = self._try_bottle(record)

        return {
            "status": record["status"],
            "message_id": message_id,
            "kind": kind,
            "audience": audience,
            "patient_id": patient_id,
            "subject": subject,
            "erx": False,
            "prescription_written": False,
            "writes_enabled": ema_writes_enabled(),
            "delivery": delivery,
            "speak_hint": speak_hint_for(kind=kind, audience=audience),
        }

    def _append_jsonl(self, record: dict) -> None:
        self._queue_path.parent.mkdir(parents=True, exist_ok=True)
        with self._queue_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def _try_bottle(self, record: dict) -> dict | None:
        if os.environ.get("LIORA_STAFF_MESSAGE_BOTTLE", "0").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return None
        try:
            from liora_tools.genies_bottle.client import GenieBottleClient

            gb = GenieBottleClient.connect()
            kwargs: dict[str, Any] = {
                "task_slug": "voice-staff-message",
                "status": "completed",
                "correlation_id": record["id"],
                "trigger_type": "voice",
                "trigger_source": record.get("source") or "voice_agent",
                "outcome_summary": str(
                    record.get("subject") or record.get("kind") or "staff message"
                ),
                "metadata": {
                    "kind": record.get("kind"),
                    "audience": record.get("audience"),
                    "body": (record.get("body") or "")[:500],
                },
            }
            if record.get("patient_id") is not None:
                kwargs["patient"] = {"id": record["patient_id"]}
            gb.report_process(**kwargs)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
