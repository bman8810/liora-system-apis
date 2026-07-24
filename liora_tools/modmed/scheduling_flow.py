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

        active = [p for p in results if (p.get("patientStatus") or "").upper() == "ACTIVE"]
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
            provider = a.get("provider") or {}
            facility = a.get("facility") or {}
            appointments.append({
                "id": a.get("id"),
                "start": a.get("scheduledStartDate"),
                "end": a.get("scheduledEndDate"),
                "duration": a.get("scheduledDuration"),
                "type_name": a.get("appointmentTypeName"),
                "status": a.get("status"),
                "provider_name": _provider_name(provider),
                "facility_name": facility.get("name"),
            })

        return {
            "patient_id": patient_id,
            "start_date": start_s,
            "end_date": end_s,
            "count": len(appointments),
            "appointments": appointments,
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
                slots.append({
                    "start": appt.get("scheduledStartDate"),
                    "end": appt.get("scheduledEndDate"),
                    "duration": appt.get("scheduledDuration"),
                    "provider_id": provider_id,
                    "provider_name": provider_name,
                    "facility_id": facility_id,
                    "facility_name": facility_name,
                    "time_zone": appt.get("timeZoneId") or time_zone,
                })
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
