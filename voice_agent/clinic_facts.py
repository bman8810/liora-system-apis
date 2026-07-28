"""Config-sourced clinic facts for static FAQ (hours / address / parking).

Defaults live in clinic_facts.json next to this module. Override path with
LIORA_CLINIC_FACTS_PATH (JSON). Never invent clinical, insurance, or results content.
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().with_name("clinic_facts.json")

# Built-in fallback if JSON missing (must match public practice facts).
_BUILTIN: dict[str, Any] = {
    "name": "Liora Dermatology & Aesthetics",
    "address_line1": "110 E 60th Street",
    "address_line2": "Suite 800",
    "city": "New York",
    "state": "NY",
    "postal_code": "10022",
    "address_speak": "110 East 60th Street, Suite 800, New York, New York 10022",
    "phone": "212-433-4569",
    "phone_speak": "212-433-4569, that's 212-433-GLOW",
    "email": "hello@lioradermatology.com",
    "timezone": "America/New_York",
    "hours": {
        "Mon-Thu": "9:00 AM – 6:00 PM",
        "Fri": "9:00 AM – 4:00 PM",
        "Sat": "10:00 AM – 4:00 PM",
        "Sun": "Closed",
    },
    "hours_speak": (
        "Monday through Thursday 9 AM to 6 PM, Friday 9 AM to 4 PM, "
        "and Saturday 10 AM to 4 PM. We're closed Sunday."
    ),
    "parking_speak": (
        "There is street parking and nearby public garages around East 60th. "
        "We don't validate parking or guarantee a specific garage. "
        "We're about two blocks from the 4, 5, and 6 trains at 59th Street."
    ),
    "transit_speak": "About two blocks from the 4, 5, and 6 at 59th Street.",
    "source": "builtin fallback",
    "as_of": "2026-07-28",
    "barric_confirmed": False,
}

FAQ_TOPICS = frozenset({"hours", "address", "parking", "all"})


def clinic_facts_path() -> Path:
    raw = (os.environ.get("LIORA_CLINIC_FACTS_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _DEFAULT_PATH


def clear_facts_cache() -> None:
    load_clinic_facts.cache_clear()


@lru_cache(maxsize=1)
def load_clinic_facts() -> dict[str, Any]:
    """Load clinic facts from config JSON (or builtin defaults)."""
    path = clinic_facts_path()
    facts = deepcopy(_BUILTIN)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("clinic facts root must be a JSON object")
            facts.update(data)
            facts["_loaded_from"] = str(path)
        except Exception as e:
            logger.warning("Failed to load clinic facts from %s: %s — using builtin", path, e)
            facts["_loaded_from"] = f"builtin(after_error:{path})"
    else:
        facts["_loaded_from"] = "builtin"
    return facts


def address_string(facts: dict[str, Any] | None = None) -> str:
    f = facts or load_clinic_facts()
    if f.get("address_speak"):
        return str(f["address_speak"])
    parts = [
        f.get("address_line1") or "",
        f.get("address_line2") or "",
        ", ".join(
            p
            for p in (
                f.get("city") or "",
                f.get("state") or "",
            )
            if p
        ),
        f.get("postal_code") or "",
    ]
    return ", ".join(p for p in parts if p).replace(" ,", ",")


def hours_speak(facts: dict[str, Any] | None = None) -> str:
    f = facts or load_clinic_facts()
    if f.get("hours_speak"):
        return str(f["hours_speak"])
    h = f.get("hours") or {}
    return (
        f"Monday through Thursday {h.get('Mon-Thu', '9 AM to 6 PM')}, "
        f"Friday {h.get('Fri', '9 AM to 4 PM')}, "
        f"Saturday {h.get('Sat', '10 AM to 4 PM')}. "
        f"Sunday {h.get('Sun', 'Closed')}."
    )


def parking_speak(facts: dict[str, Any] | None = None) -> str:
    f = facts or load_clinic_facts()
    return str(f.get("parking_speak") or "Street and nearby garage parking; we do not validate parking.")


def topic_payload(topic: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Return patient-safe FAQ payload for hours | address | parking | all."""
    facts = load_clinic_facts()
    t = (topic or "all").strip().lower()
    if t not in FAQ_TOPICS:
        msg = (
            "I can help with office hours, our address, or parking. "
            "Which of those would you like?"
        )
        return {
            "status": "unknown_topic",
            "topic": topic,
            "allowed_topics": sorted(FAQ_TOPICS),
            "dry_run": bool(dry_run),
            "writes_performed": False,
            "message": msg,
            "speak": msg,
        }

    base_meta = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "writes_performed": False,
        "read_only": True,
        "facts_source": facts.get("_loaded_from") or facts.get("source"),
        "facts_as_of": facts.get("as_of"),
        "barric_confirmed": bool(facts.get("barric_confirmed")),
        "timezone": facts.get("timezone") or "America/New_York",
        # Explicit non-goals — keep models from inventing these via this tool
        "scope": ["hours", "address", "parking"],
        "excluded": ["clinical", "insurance", "results", "billing"],
    }

    if t == "hours":
        msg = hours_speak(facts)
        return {
            **base_meta,
            "topic": "hours",
            "hours": facts.get("hours"),
            "message": msg,
            "speak": msg,
        }

    if t == "address":
        addr = address_string(facts)
        msg = f"We're at {addr}."
        return {
            **base_meta,
            "topic": "address",
            "address": addr,
            "address_line1": facts.get("address_line1"),
            "address_line2": facts.get("address_line2"),
            "city": facts.get("city"),
            "state": facts.get("state"),
            "postal_code": facts.get("postal_code"),
            "message": msg,
            "speak": msg,
        }

    if t == "parking":
        msg = parking_speak(facts)
        return {
            **base_meta,
            "topic": "parking",
            "parking_speak": msg,
            "transit_speak": facts.get("transit_speak"),
            "message": msg,
            "speak": msg,
        }

    # all — still only hours/address/parking (no clinical/insurance)
    addr = address_string(facts)
    h = hours_speak(facts)
    p = parking_speak(facts)
    msg = f"We're at {addr}. Hours: {h} Parking: {p}"
    return {
        **base_meta,
        "topic": "all",
        "name": facts.get("name"),
        "address": addr,
        "hours": facts.get("hours"),
        "parking_speak": p,
        "transit_speak": facts.get("transit_speak"),
        "message": msg,
        "speak": msg,
    }
