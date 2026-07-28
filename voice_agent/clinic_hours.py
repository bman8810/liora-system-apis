"""Grounded clinic hours + open/closed check (America/New_York).

Hours source: live site JSON-LD snapshot used by P2 ops (2026-07-28).
No invented clinics or clinical after-hours advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

# weekday: Mon=0 … Sun=6 → (open, close) or None if closed
_WEEKLY_WINDOWS: dict[int, tuple[time, time] | None] = {
    0: (time(9, 0), time(18, 0)),   # Mon
    1: (time(9, 0), time(18, 0)),   # Tue
    2: (time(9, 0), time(18, 0)),   # Wed
    3: (time(9, 0), time(18, 0)),   # Thu
    4: (time(9, 0), time(16, 0)),   # Fri
    5: (time(10, 0), time(16, 0)),  # Sat
    6: None,                        # Sun
}

HOURS_SPEAK = {
    "Mon-Thu": "9:00 AM – 6:00 PM",
    "Fri": "9:00 AM – 4:00 PM",
    "Sat": "10:00 AM – 4:00 PM",
    "Sun": "Closed",
}

CLINIC_NAME = "Liora Dermatology & Aesthetics"
CLINIC_ADDRESS = "110 E 60th Street, Suite 800, New York, NY 10022"
CLINIC_PHONE_SPEAK = "212-433-4569 (212-433-GLOW)"

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


@dataclass(frozen=True)
class HoursStatus:
    is_open: bool
    after_hours: bool
    local_now_iso: str
    local_now_speak: str
    weekday: str
    window: str | None
    next_open_iso: str | None
    next_open_speak: str | None
    hours: dict[str, str]
    timezone: str = "America/New_York"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "is_open": self.is_open,
            "after_hours": self.after_hours,
            "local_now_iso": self.local_now_iso,
            "local_now_speak": self.local_now_speak,
            "weekday": self.weekday,
            "window": self.window,
            "next_open_iso": self.next_open_iso,
            "next_open_speak": self.next_open_speak,
            "hours": dict(self.hours),
            "timezone": self.timezone,
            "name": CLINIC_NAME,
            "address": CLINIC_ADDRESS,
            "phone_speak": CLINIC_PHONE_SPEAK,
        }


def _fmt_time(t: time) -> str:
    h = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    if t.minute:
        return f"{h}:{t.minute:02d} {ampm}"
    return f"{h} {ampm}"


def _fmt_dt(dt: datetime) -> str:
    return f"{_WEEKDAY_NAMES[dt.weekday()]}, {dt.strftime('%B')} {dt.day} at {_fmt_time(dt.time())} Eastern"


def hours_speak() -> str:
    h = HOURS_SPEAK
    return (
        f"We're open Monday through Thursday {h['Mon-Thu']}, "
        f"Friday {h['Fri']}, Saturday {h['Sat']}. Sunday we're closed."
    )


def next_open_after(when: datetime) -> datetime | None:
    """Return next opening datetime (America/New_York) strictly after `when` if closed,
    or the start of the current window if currently open? — always next *opening*.
    If currently open, returns start of *next* business day open (not used for after-hours).
    Prefer for closed/after-hours messaging.
    """
    local = when.astimezone(TZ)
    # Search up to 8 days ahead
    for day_offset in range(0, 8):
        day = (local + timedelta(days=day_offset)).date()
        window = _WEEKLY_WINDOWS[day.weekday()]
        if window is None:
            continue
        open_t, _close_t = window
        candidate = datetime.combine(day, open_t, tzinfo=TZ)
        if candidate > local:
            return candidate
        # same day but already past open — if still before close and day_offset==0, skip to next open
        if day_offset == 0:
            continue
    return None


def check_hours(now: datetime | None = None) -> HoursStatus:
    """Return open/closed status for Liora front desk hours."""
    local = (now or datetime.now(TZ)).astimezone(TZ)
    window = _WEEKLY_WINDOWS[local.weekday()]
    weekday = _WEEKDAY_NAMES[local.weekday()]

    if window is None:
        is_open = False
        window_speak = "Closed"
    else:
        open_t, close_t = window
        is_open = open_t <= local.time() < close_t
        window_speak = f"{_fmt_time(open_t)} – {_fmt_time(close_t)}"

    nxt = None if is_open else next_open_after(local)
    return HoursStatus(
        is_open=is_open,
        after_hours=not is_open,
        local_now_iso=local.isoformat(),
        local_now_speak=_fmt_dt(local),
        weekday=weekday,
        window=window_speak,
        next_open_iso=nxt.isoformat() if nxt else None,
        next_open_speak=_fmt_dt(nxt) if nxt else None,
        hours=dict(HOURS_SPEAK),
    )
