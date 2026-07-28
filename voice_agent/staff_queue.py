"""Append-only staff callback / ops note queue for voice tools.

Default path: LIORA_STAFF_QUEUE_PATH, else
  <repo>/cache/voice-staff-queue.jsonl, else /tmp/liora-staff-queue.jsonl.
No network — unit tests can point at a temp file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _default_queue_path() -> Path:
    env = (os.environ.get("LIORA_STAFF_QUEUE_PATH") or "").strip()
    if env:
        return Path(env).expanduser()
    repo_cache = Path(__file__).resolve().parent.parent / "cache" / "voice-staff-queue.jsonl"
    try:
        repo_cache.parent.mkdir(parents=True, exist_ok=True)
        return repo_cache
    except OSError:
        return Path("/tmp/liora-staff-queue.jsonl")


def queue_path() -> Path:
    return _default_queue_path()


def ops_dry_run(arguments: dict | None = None) -> bool:
    """True when tool/env asks for no side effects."""
    args = arguments or {}
    if args.get("dry_run") is True:
        return True
    raw = os.environ.get("LIORA_OPS_DRY_RUN", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    # Treat writes-off gate as dry for staff-queue side effects too
    writes = os.environ.get("EMA_WRITES_ENABLED", "").strip().lower()
    if writes in {"0", "false", "no", "off"}:
        # Default EMA writes is already off when unset — only force dry when
        # explicitly disabled. Unset means staff queue still allowed with confirm.
        pass
    raw_off = os.environ.get("LIORA_OPS_WRITES", "1").strip().lower()
    if raw_off in {"0", "false", "no", "off"}:
        return True
    return False


def enqueue(
    kind: str,
    *,
    summary: str,
    patient_id: Any = None,
    appointment_id: Any = None,
    payload: dict | None = None,
    source: str = "voice_ops",
    dry_run: bool = False,
) -> dict:
    """Append one JSONL record. dry_run logs intent only (no file write)."""
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": kind,
        "patient_id": patient_id,
        "appointment_id": appointment_id,
        "summary": summary,
        "payload": payload or {},
        "source": source,
    }
    if dry_run:
        logger.info("staff_queue dry-run kind=%s summary=%s", kind, summary)
        return {
            "queued": False,
            "dry_run": True,
            "intended": record,
            "path": str(queue_path()),
        }

    path = queue_path()
    line = json.dumps(record, default=str, separators=(",", ":"))
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    logger.info("staff_queue enqueued kind=%s path=%s", kind, path)
    return {
        "queued": True,
        "dry_run": False,
        "record": record,
        "path": str(path),
    }
