"""Pure policy: Genie remote-refill visit lapse (no EMA / network / eRx).

Remote refill triage may proceed only when the patient has a completed visit
within the configured window.

Policy rules
------------
- Default threshold is DEFAULT_REFILL_LAPSE_DAYS (365) ≈ 12 months; override via
  ``threshold_days``.
- No visit on file (None / missing) → LAPSED: allowed=False, reason="no_visit",
  offer_booking=True.
- age_days > threshold_days → LAPSED: allowed=False, reason="lapsed",
  offer_booking=True.
- age_days <= threshold_days (including *exactly* threshold_days) → IN WINDOW:
  allowed=True, reason="in_window", offer_booking=False.
  Boundary: exactly 12 months / threshold is still allowed (not "older than").
- Age is calendar-day difference using date parts only (strip timezones; for
  datetime use ``.date()``).
- Accepts ISO date/datetime strings.
- Invalid threshold_days (< 1) → raise ValueError.
- No side effects. Never call eRx or EMA.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

DEFAULT_REFILL_LAPSE_DAYS = 365  # ~12 months; configurable via threshold_days

_COMPLETED_STATUSES = frozenset({"CHECKED_OUT", "COMPLETED"})


@dataclass(frozen=True)
class RefillLapseDecision:
    """Outcome of the remote-refill visit-lapse check."""

    allowed: bool  # True = remote refill triage may proceed
    reason: str  # machine code: "in_window" | "lapsed" | "no_visit"
    offer_booking: bool  # True when remote refill refused due to lapse/no visit
    last_completed_visit_at: datetime | date | None
    now: datetime | date
    threshold_days: int
    age_days: int | None  # None when no visit
    message: str  # short human-readable for tools/prompts


def _to_date(value: datetime | date | str) -> date:
    """Normalize datetime/date/ISO string to a calendar date (tz stripped)."""
    if isinstance(value, datetime):
        # Compare date parts only; drop tz before taking .date() if aware.
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        # datetime.fromisoformat handles date-only and most ISO forms;
        # tolerate trailing Z.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # date-only fallback
            return date.fromisoformat(s[:10])
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt.date()
    raise TypeError(f"unsupported visit/now type: {type(value)!r}")


def _resolve_now(now: datetime | date | str | None) -> date:
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None).date()
    return _to_date(now)


def evaluate_refill_visit_lapse(
    last_completed_visit_at: datetime | date | str | None,
    *,
    now: datetime | date | str | None = None,
    threshold_days: int = DEFAULT_REFILL_LAPSE_DAYS,
) -> RefillLapseDecision:
    """Decide whether remote refill triage may proceed given last visit age."""
    if threshold_days < 1:
        raise ValueError(f"threshold_days must be >= 1, got {threshold_days}")

    now_resolved = _resolve_now(now)
    # Preserve original datetime/date when provided; normalize strings / default.
    now_out: datetime | date = (
        now if isinstance(now, (datetime, date)) else now_resolved
    )

    if last_completed_visit_at is None:
        return RefillLapseDecision(
            allowed=False,
            reason="no_visit",
            offer_booking=True,
            last_completed_visit_at=None,
            now=now_out,
            threshold_days=threshold_days,
            age_days=None,
            message="No completed visit on file; remote refill refused — offer booking.",
        )

    visit_date = _to_date(last_completed_visit_at)
    # Keep original non-str value on the decision; normalize strings to date.
    visit_out: datetime | date | None
    if isinstance(last_completed_visit_at, str):
        visit_out = visit_date
    else:
        visit_out = last_completed_visit_at

    age_days = (now_resolved - visit_date).days
    # age_days <= threshold → still in window (exact boundary allowed).
    if age_days > threshold_days:
        return RefillLapseDecision(
            allowed=False,
            reason="lapsed",
            offer_booking=True,
            last_completed_visit_at=visit_out,
            now=now_out,
            threshold_days=threshold_days,
            age_days=age_days,
            message=(
                f"Last completed visit was {age_days} days ago "
                f"(threshold {threshold_days}); remote refill refused — offer booking."
            ),
        )

    return RefillLapseDecision(
        allowed=True,
        reason="in_window",
        offer_booking=False,
        last_completed_visit_at=visit_out,
        now=now_out,
        threshold_days=threshold_days,
        age_days=age_days,
        message=(
            f"Last completed visit was {age_days} days ago "
            f"(threshold {threshold_days}); remote refill triage may proceed."
        ),
    )


def is_refill_lapsed(
    last_completed_visit_at: datetime | date | str | None,
    *,
    now: datetime | date | str | None = None,
    threshold_days: int = DEFAULT_REFILL_LAPSE_DAYS,
) -> bool:
    """True when remote refill must be refused (lapsed or no visit)."""
    return not evaluate_refill_visit_lapse(
        last_completed_visit_at,
        now=now,
        threshold_days=threshold_days,
    ).allowed


def _appt_start(appt: dict[str, Any]) -> datetime | date | None:
    raw = (
        appt.get("scheduledStartDate")
        or appt.get("start_date")
        or appt.get("start")
    )
    if raw is None:
        return None
    if isinstance(raw, (datetime, date)) and not isinstance(raw, str):
        return raw
    try:
        return _to_date(raw)
    except (TypeError, ValueError):
        return None


def last_completed_visit_from_appointments(
    appointments: list[dict],
) -> datetime | date | None:
    """Most recent completed visit start from appointment dicts (pure, no I/O).

    Completed statuses: CHECKED_OUT, COMPLETED (case-insensitive).
    Start keys: scheduledStartDate / start_date / start.
    """
    best: datetime | date | None = None
    best_day: date | None = None
    for appt in appointments or []:
        status = str(appt.get("status") or "").strip().upper()
        if status not in _COMPLETED_STATUSES:
            continue
        start = _appt_start(appt)
        if start is None:
            continue
        day = _to_date(start)
        if best_day is None or day > best_day:
            best = start
            best_day = day
    return best
