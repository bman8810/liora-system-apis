"""Read-only ModMed EMA scheduling orchestration for Genie agents."""

from __future__ import annotations

from datetime import date, timedelta

from liora_tools.modmed.client import EmaClient
from liora_tools.modmed.write_gate import ema_writes_enabled

DEFAULT_OPEN_STATUSES = frozenset({
    "PENDING",
    "CONFIRMED",
    "SCHEDULED",
    "ARRIVED",
    "CHECKED_IN",
    "CHANGED",
    "PRESENT",
})

# Name/MRN tokens treated as non-clinical noise on multi-match phone lookups.
_TEST_NOISE_TOKENS = (
    "TEST",
    "PHREESIA",
    "TRAINING",
    "GALATIQ",
    "ZZTEST",
    "DUMMY",
    "FAKE",
)

_PATIENT_SELECTOR = (
    "lastName,firstName,mrn,id,dateOfBirth,email,cellPhone,phoneNumbers,patientStatus"
)

_APPT_SELECTOR = (
    "id,scheduledStartDate,scheduledEndDate,scheduledDuration,"
    "appointmentTypeName,status,patient(id,lastName,firstName,mrn),"
    "provider(id,name),facility(id,name)"
)


def _phone_digits(phone: str | None) -> str | None:
    """Normalize to last 10 digits when possible (E.164 / +1 safe)."""
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return None
    return digits[-10:] if len(digits) >= 10 else digits


def _norm_phone(val) -> str:
    digits = "".join(c for c in str(val or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _phone_matches(patient: dict, phone_digits: str) -> bool:
    want = _norm_phone(phone_digits)
    if not want:
        return False
    cell = patient.get("cellPhone") or {}
    if _norm_phone(cell.get("phoneNumber")) == want:
        return True
    for pn in patient.get("phoneNumbers") or []:
        if _norm_phone(pn.get("phoneNumber")) == want:
            return True
    return False


def looks_like_test_patient(patient: dict) -> bool:
    """True when last/first/MRN looks like TEST/PHREESIA/training noise."""
    blob = " ".join(
        str(patient.get(k) or "")
        for k in ("lastName", "firstName", "mrn")
    ).upper()
    return any(tok in blob for tok in _TEST_NOISE_TOKENS)


def _looks_like_test_patient(patient: dict) -> bool:
    return looks_like_test_patient(patient)


def _patient_summary(patient: dict) -> dict:
    return {
        "id": patient.get("id"),
        "last_name": patient.get("lastName"),
        "first_name": patient.get("firstName"),
        "date_of_birth": patient.get("dateOfBirth"),
        "mrn": patient.get("mrn"),
        "status": patient.get("patientStatus"),
    }


def _provider_name(provider: dict | None) -> str | None:
    if not provider:
        return None
    if provider.get("name"):
        return provider["name"]
    parts = [provider.get("firstName") or "", provider.get("lastName") or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or None


def _parse_dt(val: str | None):
    if not val:
        return None
    s = str(val).strip()
    try:
        from datetime import datetime, timezone

        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # +0000 -> +00:00
        if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
            s = s[:-2] + ":" + s[-2:]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _to_ny_fields(val: str | None) -> dict:
    """America/New_York display fields for voice (model must not convert UTC)."""
    from datetime import timezone
    from zoneinfo import ZoneInfo

    dt = _parse_dt(val)
    if not dt:
        return {
            "start_utc": val,
            "local_timezone": "America/New_York",
            "local_time": None,
            "local_date": None,
            "local_weekday": None,
            "speak_as": None,
        }
    ny = dt.astimezone(ZoneInfo("America/New_York"))
    hour12 = ny.hour % 12 or 12
    ampm = "AM" if ny.hour < 12 else "PM"
    local_time = f"{hour12}:{ny.strftime('%M')} {ampm}"
    speak = (
        f"{ny.strftime('%A')}, {ny.strftime('%B')} {ny.day} "
        f"at {local_time} Eastern"
    )
    return {
        "start_utc": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "local_timezone": "America/New_York",
        "local_time": local_time,
        "local_date": ny.strftime("%Y-%m-%d"),
        "local_weekday": ny.strftime("%A"),
        "speak_as": speak,
    }


def _appt_type_name(a: dict) -> str | None:
    if a.get("appointmentTypeName"):
        return a.get("appointmentTypeName")
    at = a.get("appointmentType") or {}
    return at.get("name")


def _appt_summary(a: dict) -> dict:
    provider = a.get("provider") or {}
    facility = a.get("facility") or {}
    start_raw = a.get("scheduledStartDate") or a.get("scheduledStartDateLd")
    out = {
        "id": a.get("id"),
        "start": start_raw,
        "start_date": str(a.get("scheduledStartDateLd") or "")[:10] or None,
        "end": a.get("scheduledEndDate"),
        "duration": a.get("scheduledDuration"),
        "type_name": _appt_type_name(a),
        "status": a.get("status"),
        "provider_name": _provider_name(provider),
        "facility_name": facility.get("name"),
    }
    out.update(_to_ny_fields(start_raw))
    if out.get("local_date"):
        out["start_date"] = out["local_date"]
    return out


class SchedulingFlow:
    """Patient validation + upcoming/past appts + open slots (read path)."""

    def __init__(
        self,
        client: EmaClient,
        *,
        facility_id: int | str | None = None,
        open_statuses: set[str] | None = None,
    ):
        self._client = client
        self._facility_id = facility_id
        self._open_statuses = set(open_statuses) if open_statuses else set(DEFAULT_OPEN_STATUSES)

    def validate_patient(
        self,
        *,
        last_name: str = None,
        first_name: str = None,
        dob: str = None,
        phone: str = None,
        mrn: str = None,
        page_size: int = 25,
        include_test_patients: bool = False,
    ) -> dict:
        """Search patients and classify match quality.

        When multiple charts share a phone (lab smoke numbers), drop obvious
        TEST/PHREESIA/training noise unless include_test_patients=True.
        """
        clauses = []
        if last_name:
            clauses.append(f'lastName=="{last_name}"')
        if first_name:
            clauses.append(f'firstName=="{first_name}"')
        if mrn:
            clauses.append(f'mrn=="{mrn}"')
        if dob:
            dob_ts = dob if "T" in dob else f"{dob}T00:00:00.000+0000"
            clauses.append(f'dateOfBirth=="{dob_ts}"')

        phone_digits = _phone_digits(phone)
        where = ";".join(clauses) if clauses else None
        fetch_size = 100 if phone_digits else page_size

        results = self._client.list_patients(
            where=where,
            page_size=fetch_size,
            selector=_PATIENT_SELECTOR,
        )

        if phone_digits:
            results = [p for p in results if _phone_matches(p, phone_digits)]

        noise_filtered = 0
        # Drop noise only when multi-match; keep a sole test chart if that is all we have.
        if not include_test_patients and len(results) > 1:
            real = [p for p in results if not _looks_like_test_patient(p)]
            if real:
                noise_filtered = len(results) - len(real)
                results = real

        results = results[:page_size]
        candidates = [_patient_summary(p) for p in results]
        match_count = len(results)

        def _payload(**extra) -> dict:
            base = {
                "status": extra.get("status"),
                "match_count": match_count,
                "patient": extra.get("patient"),
                "candidates": candidates,
                "message": extra.get("message"),
                "noise_filtered": noise_filtered,
            }
            return base

        if match_count == 0:
            return _payload(
                status="none",
                patient=None,
                message="No patients matched the given criteria.",
            )

        # EMA often omits patientStatus on list/get; treat missing/empty as schedulable.
        def _is_schedulable(p: dict) -> bool:
            st = (p.get("patientStatus") or "").strip().upper()
            if not st:
                return True
            if st in {"INACTIVE", "DECEASED", "MERGED", "DELETED"}:
                return False
            return st == "ACTIVE"

        # Prefer single phone-filtered hit even before status games
        if match_count == 1 and _is_schedulable(results[0]):
            return _payload(
                status="matched",
                patient=_patient_summary(results[0]),
                message="Single patient matched.",
            )

        active = [p for p in results if _is_schedulable(p)]
        if len(active) == 1:
            return _payload(
                status="matched",
                patient=_patient_summary(active[0]),
                message="Single active patient matched.",
            )
        if len(active) > 1:
            return _payload(
                status="ambiguous",
                patient=None,
                message=f"{len(active)} active patients matched; need more criteria.",
            )

        # Zero ACTIVE — only inactive / other statuses
        if match_count == 1:
            return _payload(
                status="inactive",
                patient=_patient_summary(results[0]),
                message="Patient found but is not ACTIVE.",
            )

        return _payload(
            status="ambiguous",
            patient=None,
            message=f"{match_count} non-active patients matched; need more criteria.",
        )

    def list_upcoming_appointments(
        self,
        patient_id,
        *,
        days_ahead: int = 90,
        page_size: int = 50,
    ) -> dict:
        """List open-status appointments for a patient in [today, today+days_ahead].

        Always returns a stable shape: patient_id, window, count, appointments[].
        Empty list is count=0 with appointments=[] (not null). Each item includes
        speak_as / local_* Eastern fields for voice.
        """
        try:
            days_ahead = max(1, int(days_ahead or 90))
        except (TypeError, ValueError):
            days_ahead = 90
        try:
            page_size = max(1, min(200, int(page_size or 50)))
        except (TypeError, ValueError):
            page_size = 50

        start = date.today()
        end = start + timedelta(days=days_ahead)
        start_s = start.isoformat()
        end_s = end.isoformat()

        raw = self._client.list_appointments(
            start_date=start_s,
            end_date=end_s,
            where=f"patient=={patient_id}",
            page_size=page_size,
            selector=_APPT_SELECTOR,
        )

        appointments = []
        for a in raw or []:
            status = (a.get("status") or "").upper()
            if status not in self._open_statuses:
                continue
            appointments.append(_appt_summary(a))

        # Chronological for confirm scripts
        appointments.sort(
            key=lambda x: str(x.get("start_date") or x.get("start") or ""),
        )

        return {
            "patient_id": patient_id,
            "start_date": start_s,
            "end_date": end_s,
            "count": len(appointments),
            "appointments": appointments,
            "empty": len(appointments) == 0,
            "message": (
                "No upcoming open appointments in window."
                if not appointments
                else f"{len(appointments)} upcoming appointment(s)."
            ),
        }

    def list_past_appointments(
        self,
        patient_id,
        *,
        days_back: int = 365,
        page_size: int = 50,
        limit: int = 5,
        include_cancelled: bool = False,
    ) -> dict:
        """Recent past appointments (most recent first). Default excludes cancelled.

        Stable shape: patient_id, window, count, appointments[], latest (or null).
        """
        try:
            days_back = max(1, int(days_back or 365))
        except (TypeError, ValueError):
            days_back = 365
        try:
            page_size = max(1, min(200, int(page_size or 50)))
        except (TypeError, ValueError):
            page_size = 50
        try:
            limit = max(1, min(50, int(limit or 5)))
        except (TypeError, ValueError):
            limit = 5

        end = date.today()
        start = end - timedelta(days=days_back)
        start_s = start.isoformat()
        end_s = end.isoformat()

        raw = self._client.list_appointments(
            start_date=start_s,
            end_date=end_s,
            where=f"patient=={patient_id}",
            page_size=page_size,
            selector=_APPT_SELECTOR,
        )

        skip = {"CANCELLED", "CANCELED", "NO_SHOW", "NOSHOW"} if not include_cancelled else set()
        # Past list is history — skip still-open future-ish statuses when present
        openish = self._open_statuses if not include_cancelled else set()
        items = []
        for a in raw or []:
            st = (a.get("status") or "").upper()
            if st in skip:
                continue
            # Keep checked-out / completed / arrived-history; drop pure open future
            # only when start is still ahead of today (defensive).
            summary = _appt_summary(a)
            if st in openish:
                local_date = summary.get("local_date") or summary.get("start_date") or ""
                if local_date >= end_s:
                    continue
            items.append(summary)

        items.sort(
            key=lambda x: str(x.get("start_date") or x.get("start") or ""),
            reverse=True,
        )
        items = items[:limit]
        return {
            "patient_id": patient_id,
            "start_date": start_s,
            "end_date": end_s,
            "count": len(items),
            "appointments": items,
            "latest": items[0] if items else None,
            "empty": len(items) == 0,
            "message": (
                "No past appointments in window."
                if not items
                else f"{len(items)} past appointment(s)."
            ),
        }

    def list_visit_types(self) -> list:
        """Simplified appointment type list."""
        types = self._client.list_appointment_types()
        out = []
        for t in types:
            out.append({
                "id": t.get("id"),
                "name": t.get("name"),
                "default_duration": t.get("defaultDuration"),
                "default_as_new_patient": t.get("defaultAsNewPatient"),
            })
        return out

    def find_open_slots(
        self,
        appt_type_id,
        *,
        duration: int = 15,
        time_of_day: str = "ANYTIME",
        specific_date: str = None,
        time_frame: str = "FIRST_AVAILABLE",
        display: str = "BY_PROVIDER",
        limit: int = 10,
    ) -> dict:
        """Flatten EMA appointment finder results into a simple slot list."""
        groups = self._client.find_slots(
            appt_type_id=str(appt_type_id),
            duration=duration,
            time_of_day=time_of_day,
            specific_date=specific_date,
            time_frame=time_frame,
            display=display,
        )

        slots = []
        for group in groups or []:
            provider = group.get("provider") or {}
            facility = group.get("facility") or {}
            provider_id = provider.get("id")
            provider_name = _provider_name(provider)
            facility_id = facility.get("id")
            facility_name = facility.get("name")
            time_zone = facility.get("timeZone")

            for appt in group.get("appointments") or []:
                start_raw = appt.get("scheduledStartDate")
                slot = {
                    "start": start_raw,
                    "end": appt.get("scheduledEndDate"),
                    "duration": appt.get("scheduledDuration"),
                    "provider_id": provider_id,
                    "provider_name": provider_name,
                    "facility_id": facility_id,
                    "facility_name": facility_name,
                    "time_zone": appt.get("timeZoneId") or time_zone,
                }
                slot.update(_to_ny_fields(start_raw))
                slots.append(slot)
                if len(slots) >= limit:
                    break
            if len(slots) >= limit:
                break

        return {
            "appt_type_id": appt_type_id,
            "count": len(slots),
            "slots": slots[:limit],
        }

    def lookup(
        self,
        *,
        last_name: str = None,
        first_name: str = None,
        dob: str = None,
        phone: str = None,
        mrn: str = None,
        days_ahead: int = 90,
        days_back: int = 365,
        include_past: bool = True,
        include_test_patients: bool = False,
        appt_type_id=None,
        duration: int = 15,
        time_of_day: str = "ANYTIME",
        specific_date: str = None,
        slot_limit: int = 5,
    ) -> dict:
        """Full read-only flow: validate → upcoming (+ past) → optional open slots."""
        patient_result = self.validate_patient(
            last_name=last_name,
            first_name=first_name,
            dob=dob,
            phone=phone,
            mrn=mrn,
            include_test_patients=include_test_patients,
        )

        appointments = None
        past_appointments = None
        slots = None
        next_actions: list[str] = []

        status = patient_result["status"]
        if status == "matched":
            pid = patient_result["patient"]["id"]
            appointments = self.list_upcoming_appointments(
                pid, days_ahead=days_ahead,
            )
            if include_past:
                past_appointments = self.list_past_appointments(
                    pid, days_back=days_back, limit=5,
                )
            if appointments["count"] > 0:
                next_actions.append("confirm_existing")
            elif past_appointments and past_appointments["count"] > 0:
                next_actions.append("confirm_last_visit")
            if appt_type_id is not None:
                slots = self.find_open_slots(
                    appt_type_id,
                    duration=duration,
                    time_of_day=time_of_day,
                    specific_date=specific_date,
                    limit=slot_limit,
                )
                if slots["count"] > 0:
                    next_actions.append("offer_slots")
                else:
                    next_actions.append("no_slots")
            elif appointments["count"] == 0:
                next_actions.append("ask_visit_type")
        elif status == "none":
            next_actions.append("handoff_no_match")
        elif status == "ambiguous":
            next_actions.append("handoff_ambiguous")
        elif status == "inactive":
            next_actions.append("handoff_inactive")

        if not next_actions:
            next_actions.append("review")

        return {
            "patient_result": patient_result,
            "appointments": appointments,
            "past_appointments": past_appointments,
            "slots": slots,
            "writes_enabled": ema_writes_enabled(),
            "next_actions": next_actions,
        }
