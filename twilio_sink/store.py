"""JSON call/stream artifact store (no secrets, no PHI dumps)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from twilio_sink.config import settings

_lock = threading.Lock()


def _calls_dir() -> Path:
    root = settings.ensure_artifact_dir()
    return root / "calls"


def _path_for(call_sid: str) -> Path:
    safe = "".join(c for c in call_sid if c.isalnum() or c in ("-", "_")) or "unknown"
    return _calls_dir() / f"{safe}.json"


def upsert_call(call_sid: str, **fields: Any) -> dict[str, Any]:
    if not call_sid:
        call_sid = f"unknown-{int(time.time())}"
    path = _path_for(call_sid)
    with _lock:
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                data = {}
        data.setdefault("call_sid", call_sid)
        data.setdefault("created_at", time.time())
        data["updated_at"] = time.time()
        events = data.setdefault("events", [])
        event_name = fields.pop("event", None)
        if event_name:
            events.append({"at": time.time(), "event": event_name, **{k: v for k, v in fields.items() if k.startswith("_") is False}})
            # also merge top-level non-meta fields
            for k, v in list(fields.items()):
                if not k.startswith("_"):
                    # keep last-known values at top level too
                    if k not in ("event",):
                        data[k] = v
        else:
            data.update(fields)
        path.write_text(json.dumps(data, indent=2, default=str))
        return data


def get_call(call_sid: str) -> dict[str, Any] | None:
    path = _path_for(call_sid)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_calls(limit: int = 50) -> list[dict[str, Any]]:
    root = _calls_dir()
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for p in files[:limit]:
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    return out


def append_stream_meta(call_sid: str, **fields: Any) -> None:
    upsert_call(call_sid, event="stream", **fields)


def mark_recording(call_sid: str, **fields: Any) -> None:
    upsert_call(call_sid, event="recording", **fields)
