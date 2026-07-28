"""Static clinic FAQ tool for Grok Realtime (hours / address / parking only).

Read-only by nature. Supports dry_run for uniform tool contract with write tools.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .clinic_facts import FAQ_TOPICS, topic_payload

logger = logging.getLogger(__name__)

FAQ_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "clinic_faq",
        "description": (
            "Answer practice hours, office address, or parking from grounded clinic "
            "config only. Topics: hours | address | parking | all. "
            "Do NOT use for clinical advice, insurance, lab results, or billing. "
            "dry_run is accepted for contract parity (always read-only; same answers)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "hours | address | parking | all (default all)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "Uniform dry-run flag. FAQ is always read-only; when true, "
                        "response includes dry_run=true and writes_performed=false."
                    ),
                },
            },
            "required": [],
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


def clinic_faq(arguments: dict | None = None) -> str:
    """Return JSON string for Grok: hours / address / parking only."""
    args = dict(arguments or {})
    topic = args.get("topic") or "all"
    dry_run = _truthy(args.get("dry_run"))
    # Reject / ignore out-of-scope keys that models may invent
    result = topic_payload(str(topic), dry_run=dry_run)
    # Belt-and-suspenders: never attach clinical/insurance/results fields
    for banned in (
        "clinical",
        "diagnosis",
        "lab_results",
        "results",
        "insurance",
        "eligibility",
        "balance",
        "copay",
        "card_number",
        "pan",
    ):
        result.pop(banned, None)
    return _compact_json(result)


def handle_faq_tool(name: str, arguments: dict | None = None) -> str:
    if name == "clinic_faq":
        try:
            return clinic_faq(arguments)
        except Exception as e:
            logger.exception("clinic_faq failed")
            msg = "I couldn't pull that office info just now — one moment."
            return _compact_json(
                {
                    "status": "tool_failed",
                    "error": "faq_tool_failed",
                    "detail": str(e),
                    "dry_run": _truthy((arguments or {}).get("dry_run")),
                    "writes_performed": False,
                    "message": msg,
                    "speak": msg,
                }
            )
    msg = "I can only answer hours, address, or parking with that tool."
    return _compact_json(
        {
            "status": "unknown_tool",
            "name": name,
            "allowed_topics": sorted(FAQ_TOPICS),
            "dry_run": _truthy((arguments or {}).get("dry_run")),
            "writes_performed": False,
            "message": msg,
            "speak": msg,
        }
    )


FAQ_TOOL_NAMES = frozenset(t["name"] for t in FAQ_TOOL_DEFINITIONS)

TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    "clinic_faq": clinic_faq,
}
