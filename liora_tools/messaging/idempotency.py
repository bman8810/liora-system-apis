"""File-backed idempotency store for outbound SMS.

JSON under ``~/.liora/state/`` by default; path injectable for tests.
Atomic-ish writes via temp file + rename.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

DEFAULT_STORE_PATH = os.path.expanduser(
    "~/.liora/state/messaging-outbound-idempotency.json"
)
DEFAULT_RETENTION_SECONDS = 14 * 24 * 3600  # 14 days


def make_idempotency_key(
    *,
    route: str,
    template_id: str,
    template_version: str,
    thread_id: str | None = None,
    message_id: str | None = None,
    correlation_id: str | None = None,
    person_phone_e164: str | None = None,
) -> str:
    """Stable SHA256 hex over normalized identity parts.

    Prefer message_id / thread_id / correlation_id. Phone is last resort and
    is hashed into the material (never stored raw as the key itself).
    """
    parts: list[str] = [
        "v1",
        (route or "").strip().lower(),
        (template_id or "").strip(),
        (template_version or "").strip(),
    ]
    mid = (message_id or "").strip()
    tid = (thread_id or "").strip()
    cid = (correlation_id or "").strip()
    if mid:
        parts.append(f"msg:{mid}")
    elif tid:
        parts.append(f"thread:{tid}")
    elif cid:
        parts.append(f"corr:{cid}")
    else:
        phone = (person_phone_e164 or "").strip()
        if phone:
            phone_hash = hashlib.sha256(phone.encode("utf-8")).hexdigest()[:32]
            parts.append(f"phone:{phone_hash}")
        else:
            parts.append("anon")
    material = "|".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """JSON file store of sent-message keys with redacted metadata."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    ):
        self.path = Path(path) if path else Path(DEFAULT_STORE_PATH)
        self.retention_seconds = max(0, int(retention_seconds))
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if not self.path.exists():
            self._data = {"entries": {}}
            return self._data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {"entries": {}}
            entries = raw.get("entries")
            if not isinstance(entries, dict):
                raw["entries"] = {}
            self._data = raw
        except (OSError, json.JSONDecodeError):
            self._data = {"entries": {}}
        return self._data

    def _save(self) -> None:
        data = self._load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".idem-",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def seen(self, key: str) -> bool:
        """True if *key* was previously recorded as sent."""
        if not key:
            return False
        self.prune()
        entries = self._load().get("entries") or {}
        return key in entries

    def get(self, key: str) -> dict | None:
        """Return redacted meta for *key*, or None."""
        if not key:
            return None
        entries = self._load().get("entries") or {}
        entry = entries.get(key)
        if entry is None:
            return None
        return dict(entry) if isinstance(entry, dict) else {"value": entry}

    def record(self, key: str, meta_redacted: Mapping[str, Any] | None = None) -> None:
        """Record *key* with redacted metadata (overwrites prior entry)."""
        if not key:
            raise ValueError("idempotency key required")
        data = self._load()
        entries = data.setdefault("entries", {})
        meta = dict(meta_redacted or {})
        meta.setdefault("recorded_at", time.time())
        entries[key] = meta
        self._save()

    def prune(self, *, now: float | None = None) -> int:
        """Drop entries older than retention. Returns count removed."""
        if self.retention_seconds <= 0:
            return 0
        data = self._load()
        entries = data.get("entries") or {}
        if not entries:
            return 0
        cutoff = (now if now is not None else time.time()) - self.retention_seconds
        stale = [
            k
            for k, v in entries.items()
            if isinstance(v, dict) and float(v.get("recorded_at") or 0) < cutoff
        ]
        if not stale:
            return 0
        for k in stale:
            del entries[k]
        self._save()
        return len(stale)
