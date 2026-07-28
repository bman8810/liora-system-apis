"""P2 ops voice tools — insurance-on-file prompt (read-only).

Insurance: read high-level coverage on file if EMA exposes it; otherwise
scripted ask to bring cards/referral. Never invent eligibility/copay/balance.
Never accept or store card PAN. No insurance writes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Prefer nested insurance selectors documented on scheduler patient views.
_INSURANCE_SELECTOR = (
    "id,lastName,firstName,"
    "allActiveInsurancePolicies,activeInsurances,"
    "primaryInsurance,insurances,insurance"
)

BRING_CARDS_REFERRAL = (
    "Please bring your insurance cards and any referral to the visit."
)

OPS_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_insurance_on_file",
        "description": (
            "Read insurance-on-file summary if EMA exposes it (payer name / on-file "
            "yes-no only). Never invent eligibility, copay, or balance. Never capture "
            "or read back card numbers. If nothing on file or lookup fails, instruct "
            "the patient to bring insurance cards and referral. Read-only; no writes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id from lookup_patient",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "If true, still performs the read (no writes exist) and "
                        "marks the response dry_run=true. Default false."
                    ),
                },
            },
            "required": ["patient_id"],
        },
    },
]


def _compact_json(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":"))


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return val != 0
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _speak_result(
    *,
    status: str,
    message: str,
    speak: str | None = None,
    **extra: Any,
) -> str:
    out: dict[str, Any] = {
        "status": status,
        "message": message,
        "speak": speak if speak is not None else message,
        # Insurance tool never writes chart or card data
        "writes_attempted": False,
        "writes_enabled": False,
        "eligibility_checked": False,
        "coverage_asserted": False,
        "balance_asserted": False,
    }
    out.update(extra)
    return _compact_json(out)


@lru_cache(maxsize=1)
def _get_client():
    from liora_tools.auth.session_manager import get_ema_client

    return get_ema_client()


def clear_ops_caches() -> None:
    _get_client.cache_clear()


# ── PAN / card number sanitization ───────────────────────────────────────────

# 13–19 consecutive digits (PAN-like)
_RE_LONG_DIGITS = re.compile(r"\d{13,19}")
# 4+ groups of 3–4 digits separated by space/dash
_RE_GROUPED_PAN = re.compile(r"(?:\d{3,4}[\s\-]){3,}\d{3,4}")

# Keys that must never leave this tool (card / member identifiers)
_SENSITIVE_KEY_RE = re.compile(
    r"(pan|card|member|policy|subscriber|ssn|account|token|number|id|group)",
    re.I,
)
_PAYER_NAME_KEYS = (
    "payerName",
    "payer_name",
    "companyName",
    "company_name",
    "insuranceCompanyName",
    "carrierName",
    "carrier",
    "planName",
    "plan_name",
    "name",
    "displayName",
    "description",
)


def strip_pan_like(text: str) -> str:
    """Strip PAN-like digit runs from free text. Leaves short IDs alone."""
    if not text:
        return text
    out = _RE_GROUPED_PAN.sub("[card redacted]", text)
    out = _RE_LONG_DIGITS.sub("[card redacted]", out)
    return out


def _is_sensitive_key(key: str) -> bool:
    k = key or ""
    # Keep high-level name-ish keys even if they contain "name"
    if k.lower() in {
        "name",
        "displayname",
        "payername",
        "companyname",
        "planname",
        "carrier",
        "carriername",
        "position",
        "priority",
        "type",
        "status",
        "active",
        "primary",
        "relationship",
    }:
        return False
    return bool(_SENSITIVE_KEY_RE.search(k))


def _sanitize_value(val: Any) -> Any:
    if isinstance(val, str):
        return strip_pan_like(val)
    if isinstance(val, dict):
        out = {}
        for k, v in val.items():
            if _is_sensitive_key(str(k)):
                # Drop member/card/policy identifiers entirely (not even redacted)
                continue
            out[k] = _sanitize_value(v)
        return out
    if isinstance(val, list):
        return [_sanitize_value(v) for v in val]
    return val


def _iter_insurance_blobs(patient: dict) -> list[Any]:
    """Collect insurance-shaped objects from known EMA / generic keys."""
    blobs: list[Any] = []
    for key in (
        "allActiveInsurancePolicies",
        "activeInsurances",
        "primaryInsurance",
        "insurances",
        "insurance",
        "patientInsurances",
        "coverage",
        "payors",
        "insurancePolicies",
        "primaryInsuranceCompany",
        "insuranceCompany",
        "payer",
    ):
        if key not in patient:
            continue
        val = patient[key]
        if val in (None, "", [], {}):
            continue
        blobs.append(val)
    return blobs


def _payer_name_from_obj(obj: Any) -> str | None:
    if isinstance(obj, str):
        s = strip_pan_like(obj.strip())
        return s or None
    if not isinstance(obj, dict):
        return None

    for k in _PAYER_NAME_KEYS:
        if obj.get(k):
            return strip_pan_like(str(obj[k]).strip()) or None

    for nest in (
        "insurancePolicy",
        "insuranceCompany",
        "primaryInsuranceCompany",
        "payer",
        "payor",
        "company",
        "carrier",
        "plan",
    ):
        nested = obj.get(nest)
        name = _payer_name_from_obj(nested)
        if name:
            return name
    return None


def summarize_insurance_on_file(patient: dict) -> dict[str, Any]:
    """High-level insurance summary: on_file + payer names only (no PAN/member ids)."""
    blobs = _iter_insurance_blobs(patient if isinstance(patient, dict) else {})
    if not blobs:
        return {"on_file": False, "payers": [], "count": 0}

    payers: list[str] = []
    seen: set[str] = set()

    def _add(name: str | None) -> None:
        if not name:
            return
        key = name.casefold()
        if key in seen:
            return
        seen.add(key)
        payers.append(name)

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if isinstance(node, dict):
            _add(_payer_name_from_obj(node))
            # EMA nested: { position, insurancePolicy: {...} }
            if "insurancePolicy" in node:
                _walk(node.get("insurancePolicy"))
            return
        if isinstance(node, str):
            _add(strip_pan_like(node.strip()) or None)

    for b in blobs:
        _walk(b)

    return {
        "on_file": True,
        "payers": payers,
        "count": len(payers) if payers else 1,  # on file even if name unknown
    }


def get_insurance_on_file(arguments: dict) -> str:
    """Read insurance summary; strip/drop PAN; never invent eligibility."""
    patient_id = arguments.get("patient_id")
    dry_run = _truthy(arguments.get("dry_run"))

    if patient_id is None:
        return _speak_result(
            status="patient_id_required",
            message="I need the patient on file first before I can check insurance.",
            dry_run=dry_run,
            on_file=False,
        )

    try:
        client = _get_client()
        try:
            patient = client.get_patient(str(patient_id), selector=_INSURANCE_SELECTOR)
        except TypeError:
            # Older client signature without selector kw
            patient = client.get_patient(str(patient_id))
        except Exception as sel_err:
            # Selector may be rejected by EMA; retry bare get
            logger.info("insurance selector get failed (%s); retry bare", sel_err)
            patient = client.get_patient(str(patient_id))
    except Exception as e:
        logger.exception("get_patient insurance failed")
        msg = (
            "I couldn't pull insurance on file right now. " + BRING_CARDS_REFERRAL
        )
        return _speak_result(
            status="lookup_failed",
            message=msg,
            detail=str(e),
            patient_id=patient_id,
            dry_run=dry_run,
            on_file=False,
            fallback="bring_cards_and_referral",
        )

    info = summarize_insurance_on_file(patient if isinstance(patient, dict) else {})
    # Defensive: never pass raw patient blobs through
    payers = [strip_pan_like(p) for p in (info.get("payers") or [])]
    payers = [p for p in payers if p and "[card redacted]" not in p]

    if not info.get("on_file"):
        msg = (
            "I don't see insurance details on file that I can read back. "
            + BRING_CARDS_REFERRAL
        )
        return _speak_result(
            status="none_on_file",
            message=msg,
            patient_id=patient_id,
            dry_run=dry_run,
            on_file=False,
            payers=[],
            fallback="bring_cards_and_referral",
        )

    if payers:
        if len(payers) == 1:
            msg = f"I see {payers[0]} on file. " + BRING_CARDS_REFERRAL
        else:
            listed = ", ".join(payers[:3])
            msg = f"I see insurance on file: {listed}. " + BRING_CARDS_REFERRAL
    else:
        msg = "I see insurance on file. " + BRING_CARDS_REFERRAL

    return _speak_result(
        status="ok",
        message=msg,
        patient_id=patient_id,
        dry_run=dry_run,
        on_file=True,
        payers=payers,
        # High-level only — no member ids / PAN / full policy dump
        insurance={"on_file": True, "payers": payers},
        fallback="bring_cards_and_referral",
    )


_HANDLERS: dict[str, Callable[[dict], str]] = {
    "get_insurance_on_file": get_insurance_on_file,
}


def handle_ops_tool(name: str, arguments: dict) -> str:
    """Execute an ops tool; return JSON string for Grok."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _speak_result(
            status="unknown_tool",
            message="I can't do that from here.",
            error="unknown_tool",
            name=name,
        )
    try:
        return handler(arguments or {})
    except Exception as e:
        logger.exception("ops tool %s failed", name)
        return _speak_result(
            status="tool_failed",
            message=(
                "Something went wrong checking insurance. " + BRING_CARDS_REFERRAL
            ),
            error="ops_tool_failed",
            tool=name,
            detail=str(e),
            fallback="bring_cards_and_referral",
        )


OPS_TOOL_NAMES = frozenset(_HANDLERS.keys())
