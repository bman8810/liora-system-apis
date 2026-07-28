"""Structured staff/provider message queue for voice triage.

Voice never discloses lab values or e-prescribes. Requests land here as
structured messages for front desk / provider review.

Delivery backends (when writes enabled and not dry-run):
1. Optional EMA staff-message hook on EmaClient (if implemented)
2. Durable JSONL queue file
3. Optional Genies Bottle process report (best-effort)

When EMA_WRITES_ENABLED is off or LIORA_VOICE_DRY_RUN is on, enqueue is blocked
and callers should log intended payload without side effects.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from liora_tools.modmed.write_gate import ema_writes_enabled, require_ema_writes

logger = logging.getLogger(__name__)


def voice_dry_run() -> bool:
    """True when voice side-effects must be skipped (lab / dry-run mode)."""
    return os.environ.get("LIORA_VOICE_DRY_RUN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def default_queue_path() -> Path:
    raw = (os.environ.get("LIORA_STAFF_MESSAGE_QUEUE") or "").strip()
    if raw:
        return Path(raw)
    # Prefer explicit staff queue path alias used by ops plan
    alt = (os.environ.get("LIORA_STAFF_QUEUE_PATH") or "").strip()
    if alt:
        return Path(alt)
    creds = (os.environ.get("LIORA_CREDENTIALS_DIR") or "").strip()
    if creds:
        return Path(creds) / "staff_message_queue.jsonl"
    return Path("/opt/data/workspace/liora/cache/staff_messages/queue.jsonl")


class StaffMessageQueue:
    """Persist structured staff messages; never claim clinical content was released."""

    def __init__(
        self,
        client=None,
        *,
        queue_path: str | Path | None = None,
    ):
        self._client = client
        self._queue_path = Path(queue_path) if queue_path else default_queue_path()

    def build_record(
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
        """Build queue record without writing (for dry-run / intended logging)."""
        now = datetime.now(timezone.utc)
        message_id = f"smq_{uuid.uuid4().hex[:16]}"
        return {
            "id": message_id,
            "kind": kind,
            "audience": audience,
            "patient_id": patient_id,
            "subject": subject,
            "body": body,
            "payload": payload or {},
            "source": source,
            "created_at": now.isoformat(),
            "status": "intended",
            "erx": False,
            "prescription_written": False,
            "clinical_results_disclosed": False,
        }

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
        """Queue a staff/provider message. Gated by EMA_WRITES_ENABLED + dry-run.

        kind: rx_refill | product_refill | general | results | results_callback | late | transfer
        audience: provider | staff | inventory
        """
        if voice_dry_run():
            from liora_tools.exceptions import WriteGatedError

            raise WriteGatedError(
                f"Voice dry-run active (staff_message:{kind}). "
                "Set LIORA_VOICE_DRY_RUN=0 and EMA_WRITES_ENABLED=true to allow queue writes."
            )

        require_ema_writes(f"staff_message:{kind}")

        record = self.build_record(
            kind=kind,
            patient_id=patient_id,
            subject=subject,
            body=body,
            payload=payload,
            audience=audience,
            source=source,
        )
        record["status"] = "queued"

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

        logger.info(
            "staff_message queued id=%s kind=%s audience=%s patient_id=%s",
            record["id"],
            kind,
            audience,
            patient_id,
        )

        return {
            "status": record["status"],
            "message_id": record["id"],
            "kind": kind,
            "audience": audience,
            "patient_id": patient_id,
            "subject": subject,
            "erx": False,
            "prescription_written": False,
            "clinical_results_disclosed": False,
            "writes_enabled": ema_writes_enabled(),
            "dry_run": False,
            "delivery": delivery,
            "speak_hint": (
                "I messaged the care team about this. They usually review "
                "by the next business day — not a same-day guarantee."
            ),
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
