"""Ops glue voice tools (P2): results triage stub and future late/forms/FAQ/insurance.

Separate from ema_tools scheduling. Grok bridge merges OPS_TOOL_DEFINITIONS when
EMA voice tools are enabled.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

OPS_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "triage_lab_results",
        "description": (
            "Patient wants lab/test RESULTS status or to hear results. "
            "NEVER read or invent result values. Routes to message-MD or staff callback queue only. "
            "Get verbal confirm (confirmed=true) before queueing. "
            "route=message_md (default) or callback. "
            "No clinical advice, billing, or card numbers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id from lookup_patient when known",
                },
                "reason": {
                    "type": "string",
                    "description": "What they said (e.g. 'biopsy from last week') — not result values",
                },
                "preferred_callback": {
                    "type": "string",
                    "description": "Phone or time window if they want a callback",
                },
                "route": {
                    "type": "string",
                    "description": "message_md | callback",
                    "enum": ["message_md", "callback"],
                },
                "notes": {"type": "string"},
                "confirmed": {
                    "type": "boolean",
                    "description": "true only after caller agrees to MD message or callback (not reading results)",
                },
            },
            "required": ["confirmed"],
        },
    },
]


def ops_tools_enabled() -> bool:
    """Ops tools ride with EMA voice tools unless explicitly disabled."""
    raw = os.environ.get("EMA_VOICE_OPS_TOOLS", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    # default: same gate as EMA voice tools
    try:
        from .ema_tools import voice_tools_enabled

        return voice_tools_enabled()
    except Exception:
        return os.environ.get("EMA_VOICE_TOOLS", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }


@lru_cache(maxsize=1)
def _get_results_flow():
    from liora_tools.modmed.results_flow import ResultsFlow

    client = None
    try:
        from liora_tools.auth.session_manager import get_ema_client

        client = get_ema_client()
    except Exception:
        logger.debug("EMA client unavailable for results flow; queue-only mode", exc_info=True)
    return ResultsFlow(client)


def clear_ops_cache() -> None:
    _get_results_flow.cache_clear()


def _compact_json(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":"))


def handle_ops_tool(name: str, arguments: dict) -> str:
    """Execute ops voice tool; return JSON string for Grok."""
    args = dict(arguments or {})
    try:
        if name == "triage_lab_results":
            flow = _get_results_flow()
            confirmed = bool(args.get("confirmed"))
            result = flow.request_results_triage(
                patient_id=args.get("patient_id"),
                reason=args.get("reason") or "",
                preferred_callback=args.get("preferred_callback"),
                route=args.get("route") or "message_md",
                notes=args.get("notes") or "",
                confirmed=confirmed,
            )
            return _compact_json(result)

        return _compact_json({"error": "unknown_ops_tool", "name": name})
    except Exception as e:
        logger.exception("ops tool %s failed", name)
        return _compact_json(
            {
                "error": "ops_tool_failed",
                "tool": name,
                "detail": str(e),
                "clinical_results_disclosed": False,
                "message": "Offer a staff callback. Never read lab results.",
            }
        )


def is_ops_tool(name: str) -> bool:
    return name in {t["name"] for t in OPS_TOOL_DEFINITIONS}
