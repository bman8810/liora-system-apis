"""Gate ModMed EMA write operations behind EMA_WRITES_ENABLED."""

from __future__ import annotations

import os


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
