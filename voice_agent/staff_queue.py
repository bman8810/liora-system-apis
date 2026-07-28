"""Append-only staff queue (JSONL) for voice ops side-effects.

No network. Path from LIORA_STAFF_QUEUE_PATH or defaults under package parent cache
or /tmp/liora-staff-queue.jsonl.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CORE_KEYS = frozenset(
    {
        "ts",
        "kind",
        "patient_id",
        "appointment_id",
        "summary",
        "payload",
        "source",
        "id",
        "schema_version",
    }
)


def default_queue_path() -> Path:
    """Resolve staff queue path without requiring network or live EMA."""
    env = os.environ.get("LIORA_STAFF_QUEUE_PATH", "").strip()
    if env:
        return Path(env).expanduser()

    # Prefer package-parent/cache when writable
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


def _make_id(kind: str) -> str:
    hex8 = secrets.token_hex(4)
    if kind == "transfer_to_staff":
        return f"xfer_{hex8}"
    return f"note_{hex8}"


def enqueue(
    kind: str,
    *,
    patient_id: Any = None,
    appointment_id: Any = None,
    summary: str = "",
    payload: dict | None = None,
    path: Path | str | None = None,
    extra: dict | None = None,
) -> dict[str, Any]:
    """Append one JSONL record. Returns the written record metadata."""
    queue_path = Path(path) if path else default_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    record: dict[str, Any] = {
        "id": _make_id(kind),
        "schema_version": 1,
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "patient_id": patient_id,
        "appointment_id": appointment_id,
        "summary": summary or "",
        "payload": payload or {},
        "source": "voice_ops",
    }

    if extra:
        for key, value in extra.items():
            if key not in _CORE_KEYS:
                record[key] = value

    line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
    with open(queue_path, "a", encoding="utf-8") as f:
        f.write(line)

    return {
        "queued": True,
        "path": str(queue_path),
        "record": record,
    }


def read_notes(
    path: Path | str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read recent JSONL notes (for tests/inspection). Newest last; caps at limit."""
    queue_path = Path(path) if path else default_queue_path()
    if not queue_path.exists():
        return []
    notes: list[dict[str, Any]] = []
    with open(queue_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                notes.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if limit > 0 and len(notes) > limit:
        return notes[-limit:]
    return notes
