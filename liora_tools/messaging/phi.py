"""Shared PHI-safe helpers for messaging outbound / worker paths.

Extracted patterns from ``zocdoc_new_booking`` private helpers. Pure and
reusable — no I/O, no AI.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# Keys safe to include in structured ops logs (no body, full phone, name, email).
_LOG_ALLOWLIST = frozenset({
    "route",
    "template_id",
    "template_name",
    "template_version",
    "status",
    "mode",
    "idempotency_key",
    "correlation_id",
    "smsId",
    "threadId",
    "personId",
    "reason",
    "phone_masked",
    "body_len",
    "draft_preview_redacted",
})


def mask_phone(phone: str | None) -> str:
    """Mask phone to last-4 only (***1234)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "(none)" if not digits else "****"
    return f"***{digits[-4:]}"


def mask_email(email: str | None) -> str:
    """Mask email local-part (a***@domain)."""
    if not email or "@" not in email:
        return "(none)"
    local, _, domain = email.partition("@")
    if not local:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def mask_name(name: str | None) -> str:
    """Mask each name token to first letter + ***."""
    name = (name or "").strip()
    if not name:
        return "(unknown)"
    parts = name.split()
    return " ".join((p[0] + "***") if p else "" for p in parts)


def redact_error(err: BaseException | str) -> str:
    """Strip tokens/secrets/long digit runs from error text for logs."""
    text = str(err)
    # Secrets before PII so token-like digit runs inside JWTs are gone first
    text = re.sub(
        r"Bearer\s+\S+",
        "Bearer [redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9._-]+",
        "[token]",
        text,
    )
    text = re.sub(
        r"(api[_-]?key|secret|password|token)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = re.sub(r"\+?\d[\d\s().-]{8,}\d", "[phone]", text)
    return text[:500]


def summarize_for_log(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a log-safe dict with only allowlisted non-PHI keys.

    Phone values under common keys are masked to last-4. Never includes body,
    full phone, name, email, DOB, or MRN free text.
    """
    if not data:
        return {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in ("phone", "person_phone", "personPhone", "to"):
            out["phone_masked"] = mask_phone(str(value) if value is not None else None)
            continue
        if key == "phone_masked":
            out["phone_masked"] = str(value) if value is not None else "(none)"
            continue
        if key in ("smsId", "threadId", "personId") and value is not None:
            out[key] = value
            continue
        if key in _LOG_ALLOWLIST and value is not None:
            out[key] = value
            continue
        # Nested weave_ids
        if key == "weave_ids" and isinstance(value, Mapping):
            for wk in ("smsId", "threadId", "personId"):
                if value.get(wk) is not None:
                    out[wk] = value[wk]
    return out
