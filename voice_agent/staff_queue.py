"""Append-only staff queue (JSONL) for voice side-effects.

No network. Path from LIORA_STAFF_QUEUE_PATH or defaults under package parent cache
or /tmp/liora-staff-queue.jsonl.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_queue_path() -> Path:
    """Resolve staff queue path without requiring network or live EMA."""
    env = os.environ.get("LIORA_STAFF_QUEUE_PATH", "").strip()
    if env:
        return Path(env).expanduser()

    pkg_parent = Path(__file__).resolve().parent.parent
    cache_dir = pkg_parent / "cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        probe = cache_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return cache_dir / "voice-staff-queue.jsonl"
    except OSError:
        return Path(tempfile.gettempdir()) / "liora-staff-queue.jsonl"


def enqueue(
    kind: str,
    *,
    patient_id: Any = None,
    appointment_id: Any = None,
    summary: str = "",
    payload: dict | None = None,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Append one JSONL record. Returns the written record metadata."""
    queue_path = Path(path) if path else default_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "patient_id": patient_id,
        "appointment_id": appointment_id,
        "summary": summary or "",
        "payload": payload or {},
        "source": "voice_ops",
    }

    line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
    with open(queue_path, "a", encoding="utf-8") as f:
        f.write(line)

    return {
        "queued": True,
        "path": str(queue_path),
        "record": record,
    }
