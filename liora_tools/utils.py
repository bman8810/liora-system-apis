"""Shared utilities for liora_tools."""

import re
from typing import Set

from liora_tools.exceptions import SafetyGuardError


def normalize_phone_e164(phone: str, country_code: str = "1") -> str:
    """Normalize a phone number to E.164 format (+1XXXXXXXXXX)."""
    digits = re.sub(r"[^\d]", "", phone)
    if digits.startswith(country_code) and len(digits) == len(country_code) + 10:
        digits = digits[len(country_code):]
    if len(digits) != 10:
        raise ValueError(f"Expected 10-digit phone number, got {len(digits)} digits from '{phone}'")
    return f"+{country_code}{digits}"


def check_safety_guard(phone_e164: str, allowed_phones: Set[str], action: str) -> None:
    """Raise SafetyGuardError if phone is not in the allowlist."""
    if phone_e164 not in allowed_phones:
        raise SafetyGuardError(
            f"SAFETY: Refusing to {action} {phone_e164}. "
            f"Allowed numbers: {allowed_phones}"
        )
