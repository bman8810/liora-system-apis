"""Gate ModMed EMA write operations behind EMA_WRITES_ENABLED + verbal confirm."""

from __future__ import annotations

import os
from typing import Any


def ema_writes_enabled() -> bool:
    return os.environ.get("EMA_WRITES_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def require_ema_writes(action: str) -> None:
    if not ema_writes_enabled():
        from liora_tools.exceptions import WriteGatedError
        raise WriteGatedError(
            f"EMA write blocked ({action}). Set EMA_WRITES_ENABLED=true to allow "
            "ModMed mutations. Default is read-only."
        )


def is_confirmed(value: Any) -> bool:
    """Strict truthiness for voice/tool confirmed flags.

    Grok often sends JSON booleans, but string \"false\" must NOT count as true
    (bool(\"false\") is True in Python). Only explicit affirmatives pass.
    """
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return False


def needs_confirmation_result(
    action: str,
    *,
    message: str,
    pending: dict | None = None,
) -> dict:
    """Structured pre-write block — never mutates EMA."""
    out = {
        "status": "needs_confirmation",
        "error": "needs_confirmation",
        "action": action,
        "message": message,
        "writes_enabled": ema_writes_enabled(),
        "booking_available": ema_writes_enabled(),
        # Multi-step policy: one write per confirm; no silent batching.
        "confirm_policy": "one_write_per_confirm",
    }
    if pending is not None:
        out["pending_write"] = pending
    return out
