"""Grounded clinic facts for voice FAQ (no invented content).

Source: live site JSON-LD snapshot 2026-07-28 (PLAN-p2-ops).
"""

from __future__ import annotations

from typing import Any

CLINIC_FACTS: dict[str, Any] = {
    "name": "Liora Dermatology & Aesthetics",
    "address": "110 E 60th Street, Suite 800, New York, NY 10022",
    "phone_speak": "212-433-4569 (212-433-GLOW)",
    "phone": "212-433-4569",
    "email": "hello@lioradermatology.com",
    "hours": {
        "Mon-Thu": "9:00 AM – 6:00 PM",
        "Fri": "9:00 AM – 4:00 PM",
        "Sat": "10:00 AM – 4:00 PM",
        "Sun": "Closed",
    },
    "timezone": "America/New_York",
    "transit": "2 blocks from 4/5/6 at 59th Street",
    "parking_note": (
        "Nearby street and garage parking near 60th; "
        "2 blocks from 4/5/6 at 59th — we don't validate parking. "
        "The office does not guarantee a specific garage."
    ),
}

_TOPIC_KEYS = frozenset({"hours", "address", "parking", "phone", "all"})


def hours_speak() -> str:
    h = CLINIC_FACTS["hours"]
    return (
        f"We're open Monday through Thursday {h['Mon-Thu']}, "
        f"Friday {h['Fri']}, Saturday {h['Sat']}. Sunday we're closed."
    )


def address_speak() -> str:
    return f"We're at {CLINIC_FACTS['address']}."


def parking_speak() -> str:
    return CLINIC_FACTS["parking_note"]


def phone_speak() -> str:
    return f"You can reach us at {CLINIC_FACTS['phone_speak']}."


def get_topic(topic: str) -> dict[str, Any]:
    """Return grounded fact payload for a FAQ topic.

    Unknown topics yield status=unknown_topic (never invent fields).
    """
    t = (topic or "all").strip().lower()
    if t not in _TOPIC_KEYS:
        return {
            "status": "unknown_topic",
            "topic": topic,
            "allowed_topics": sorted(_TOPIC_KEYS),
            "message": (
                "I only have hours, address, parking, and phone on file. "
                "What would you like?"
            ),
            "speak": (
                "I only have hours, address, parking, and phone on file. "
                "What would you like?"
            ),
        }

    facts = CLINIC_FACTS
    if t == "hours":
        msg = hours_speak()
        return {
            "status": "ok",
            "topic": "hours",
            "hours": facts["hours"],
            "timezone": facts["timezone"],
            "message": msg,
            "speak": msg,
        }
    if t == "address":
        msg = address_speak()
        return {
            "status": "ok",
            "topic": "address",
            "address": facts["address"],
            "message": msg,
            "speak": msg,
        }
    if t == "parking":
        msg = parking_speak()
        return {
            "status": "ok",
            "topic": "parking",
            "parking_note": facts["parking_note"],
            "transit": facts["transit"],
            "message": msg,
            "speak": msg,
        }
    if t == "phone":
        msg = phone_speak()
        return {
            "status": "ok",
            "topic": "phone",
            "phone": facts["phone"],
            "phone_speak": facts["phone_speak"],
            "message": msg,
            "speak": msg,
        }

    # all
    msg = (
        f"{facts['name']}. {address_speak()} {hours_speak()} "
        f"{phone_speak()} {parking_speak()}"
    )
    return {
        "status": "ok",
        "topic": "all",
        "name": facts["name"],
        "address": facts["address"],
        "hours": facts["hours"],
        "timezone": facts["timezone"],
        "phone": facts["phone"],
        "phone_speak": facts["phone_speak"],
        "email": facts["email"],
        "transit": facts["transit"],
        "parking_note": facts["parking_note"],
        "message": msg,
        "speak": msg,
    }
