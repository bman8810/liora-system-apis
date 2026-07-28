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

_PATIENT_SELECTOR = (
    "lastName,firstName,mrn,id,dateOfBirth,email,cellPhone,phoneNumbers,patientStatus"
)

_APPT_SELECTOR = (
    "id,scheduledStartDate,scheduledEndDate,scheduledDuration,"
    "appointmentTypeName,status,patient(id,lastName,firstName,mrn),"
    "provider(id,name),facility(id,name)"
)


def _phone_digits(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return digits or None


def _phone_matches(patient: dict, phone_digits: str) -> bool:
    cell = patient.get("cellPhone") or {}
    if str(cell.get("phoneNumber", "")).replace("-", "") == phone_digits:
        return True
    for pn in patient.get("phoneNumbers") or []:
        if str(pn.get("phoneNumber", "")).replace("-", "") == phone_digits:
            return True
    return False


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
    """Parse EMA ISO timestamps (Z / +0000 / +00:00) to aware datetime, or None."""
    if not val:
        return None
    s = str(val).strip()
    try:
        from datetime import datetime, timezone

        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # +0000 -> +00:00 (EMA often omits the colon)
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
    return at.get("name") if isinstance(at, dict) else None


def _appt_summary(a: dict) -> dict:
    """Patient-facing appointment row: internal start kept; speech uses speak_as only."""
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
    """Read-only patient validation + upcoming appts + open slots."""

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
    ) -> dict:
        """Search patients and classify match quality."""
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

        results = results[:page_size]
        candidates = [_patient_summary(p) for p in results]
        match_count = len(results)

        if match_count == 0:
            return {
                "status": "none",
                "match_count": 0,
                "patient": None,
                "candidates": [],
                "message": "No patients matched the given criteria.",
            }

        # EMA often omits patientStatus on list/get; treat missing/empty as schedulable.
        def _is_active(p: dict) -> bool:
            st = (p.get("patientStatus") or "").strip().upper()
            if not st:
                return True
            return st == "ACTIVE"

        active = [p for p in results if _is_active(p)]
        if len(active) == 1:
            return {
                "status": "matched",
                "match_count": match_count,
                "patient": _patient_summary(active[0]),
                "candidates": candidates,
                "message": "Single active patient matched.",
            }
        if len(active) > 1:
            return {
                "status": "ambiguous",
                "match_count": match_count,
                "patient": None,
                "candidates": candidates,
                "message": f"{len(active)} active patients matched; need more criteria.",
            }

        # Zero ACTIVE — only inactive / other statuses
        if match_count == 1:
            return {
                "status": "inactive",
                "match_count": 1,
                "patient": _patient_summary(results[0]),
                "candidates": candidates,
                "message": "Patient found but is not ACTIVE.",
            }

        return {
            "status": "ambiguous",
            "match_count": match_count,
            "patient": None,
            "candidates": candidates,
            "message": f"{match_count} non-active patients matched; need more criteria.",
        }

    def list_upcoming_appointments(
        self,
        patient_id,
        *,
        days_ahead: int = 90,
        page_size: int = 50,
    ) -> dict:
        """List open-status appointments for a patient in [today, today+days_ahead]."""
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
        for a in raw:
            status = (a.get("status") or "").upper()
            if status not in self._open_statuses:
                continue
            appointments.append(_appt_summary(a))

        return {
            "patient_id": patient_id,
            "start_date": start_s,
            "end_date": end_s,
            "count": len(appointments),
            "appointments": appointments,
            "timezone": "America/New_York",
            "speak_hint": "Read speak_as aloud; never convert start/start_utc yourself.",
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

            for appt in group.get("appointments") or []:
                slot = {
                    "start": appt.get("scheduledStartDate"),
                    "end": appt.get("scheduledEndDate"),
                    "duration": appt.get("scheduledDuration"),
                    "provider_id": provider_id,
                    "provider_name": provider_name,
                    "facility_id": facility_id,
                    "facility_name": facility_name,
                    # Presentation contract for Genie — practice is always Eastern.
                    "time_zone": "America/New_York",
                }
                slot.update(_to_ny_fields(appt.get("scheduledStartDate")))
                slots.append(slot)
                if len(slots) >= limit:
                    break
            if len(slots) >= limit:
                break

        return {
            "appt_type_id": appt_type_id,
            "count": len(slots),
            "slots": slots[:limit],
            "timezone": "America/New_York",
            "speak_hint": "Read speak_as aloud; never convert start/start_utc yourself.",
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
        appt_type_id=None,
        duration: int = 15,
        time_of_day: str = "ANYTIME",
        specific_date: str = None,
        slot_limit: int = 5,
    ) -> dict:
        """Full read-only flow: validate → upcoming → optional open slots."""
        patient_result = self.validate_patient(
            last_name=last_name,
            first_name=first_name,
            dob=dob,
            phone=phone,
            mrn=mrn,
        )

        appointments = None
        slots = None
        next_actions: list[str] = []

        status = patient_result["status"]
        if status == "matched":
            pid = patient_result["patient"]["id"]
            appointments = self.list_upcoming_appointments(
                pid, days_ahead=days_ahead,
            )
            if appointments["count"] > 0:
                next_actions.append("confirm_existing")
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
            "slots": slots,
            "writes_enabled": ema_writes_enabled(),
            "next_actions": next_actions,
        }
