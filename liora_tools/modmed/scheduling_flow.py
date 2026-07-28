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



def _looks_like_test_patient(patient: dict) -> bool:
    blob = " ".join(
        str(patient.get(k) or "")
        for k in ("lastName", "firstName", "mrn")
    ).upper()
    return any(
        tok in blob
        for tok in (
            "TEST",
            "PHREESIA",
            "TRAINING",
            "GALATIQ",
            "ZZTEST",
            "DUMMY",
            "FAKE",
        )
    )

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
        if len(s) >= 5 and s[-5] in "+-" and len(s) >= 3 and s[-3] != ":":
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

        # When multiple hits (common on shared lab phones), drop obvious test charts
        if len(results) > 1:
            real = [p for p in results if not _looks_like_test_patient(p)]
            if real:
                results = real

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

        def _is_schedulable(p: dict) -> bool:
            st = (p.get("patientStatus") or "").strip().upper()
            # EMA often omits status on list/search — treat blank as OK
            if not st:
                return True
            if st in {"INACTIVE", "DECEASED", "MERGED", "DELETED"}:
                return False
            return st == "ACTIVE"

        # Prefer single phone-filtered hit even before status games
        if match_count == 1 and _is_schedulable(results[0]):
            return {
                "status": "matched",
                "match_count": 1,
                "patient": _patient_summary(results[0]),
                "candidates": candidates,
                "message": "Single patient matched.",
            }

        active = [p for p in results if _is_schedulable(p)]
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
        """Recent past appointments (most recent first). Default excludes cancelled."""
        end = date.today()
        start = end - timedelta(days=days_back)
        start_s = start.isoformat()
        end_s = end.isoformat()

        raw = self._client.list_appointments(
            start_date=start_s,
            end_date=end_s,
            where=f"patient=={patient_id}",
            page_size=page_size,
        )

        skip = {"CANCELLED"} if not include_cancelled else set()
        items = []
        for a in raw:
            st = (a.get("status") or "").upper()
            if st in skip:
                continue
            items.append(_appt_summary(a))

        items.sort(
            key=lambda x: str(x.get("start_date") or x.get("start") or ""),
            reverse=True,
        )
        items = items[: max(1, int(limit or 5))]
        return {
            "patient_id": patient_id,
            "start_date": start_s,
            "end_date": end_s,
            "count": len(items),
            "appointments": items,
            "latest": items[0] if items else None,
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

        # Round-robin across providers so lab/test providers don't starve Rhee/etc.
        per_group = []
        for group in groups or []:
            provider = group.get("provider") or {}
            facility = group.get("facility") or {}
            provider_id = provider.get("id")
            provider_name = _provider_name(provider)
            facility_id = facility.get("id")
            facility_name = facility.get("name")
            time_zone = facility.get("timeZone")
            row = []
            for appt in group.get("appointments") or []:
                slot = {
                    "start": appt.get("scheduledStartDate"),
                    "end": appt.get("scheduledEndDate"),
                    "duration": appt.get("scheduledDuration"),
                    "provider_id": provider_id,
                    "provider_name": provider_name,
                    "facility_id": facility_id,
                    "facility_name": facility_name,
                    "time_zone": "America/New_York",
                }
                slot.update(_to_ny_fields(appt.get("scheduledStartDate")))
                row.append(slot)
            if row:
                per_group.append(row)

        slots = []
        idx = 0
        while len(slots) < limit and per_group:
            progressed = False
            for row in list(per_group):
                if not row:
                    continue
                slots.append(row.pop(0))
                progressed = True
                if len(slots) >= limit:
                    break
            per_group = [r for r in per_group if r]
            if not progressed:
                break
            idx += 1

        # Prefer real clinicians over zzz* lab providers when presenting
        def _rank(s):
            name = (s.get("provider_name") or "").lower()
            zzz = 1 if name.startswith("zzz") else 0
            return (zzz, s.get("start") or "")
        slots.sort(key=_rank)

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


    def book_appointment(
        self,
        *,
        patient_id,
        provider_id,
        facility_id,
        appointment_type_id,
        scheduled_start: str,
        duration: int = 15,
        notes: str = "",
        new_patient: bool = False,
        confirmed: bool = False,
    ) -> dict:
        """Create appointment. Requires EMA_WRITES_ENABLED + confirmed=True."""
        from liora_tools.modmed.write_gate import ema_writes_enabled, require_ema_writes
        from datetime import datetime, timedelta

        if not confirmed:
            return {
                "status": "needs_confirmation",
                "message": "Caller must verbally confirm the slot before booking.",
                "writes_enabled": ema_writes_enabled(),
            }
        require_ema_writes("book_appointment")

        start_dt = datetime.fromisoformat(scheduled_start.replace("Z", "+00:00"))
        end_dt = start_dt + timedelta(minutes=int(duration))
        start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        patient = self._client.get_patient(str(patient_id))
        facilities = self._client.list_facilities()
        facility_obj = next((f for f in facilities if f.get("id") == int(facility_id)), {"id": int(facility_id)})
        appt_types = self._client.list_appointment_types()
        appt_type = next((x for x in appt_types if x.get("id") == int(appointment_type_id)), {"id": int(appointment_type_id)})

        # Prefer full provider object from open slots / recent appt
        provider_obj = {"id": int(provider_id)}
        try:
            recent = self._client.list_appointments(page_size=3)
            if recent:
                full = self._client._get(
                    f"/ema/ws/v2/appointment/{recent[0]['id']}", {"mapId": "CHECK_IN"}
                ).json()
                if (full.get("provider") or {}).get("id") == int(provider_id):
                    provider_obj = full["provider"]
                elif full.get("provider"):
                    # keep id only if different
                    provider_obj = {"id": int(provider_id)}
        except Exception:
            pass

        payload = {
            "status": "PENDING",
            "scheduledStartDate": start_str,
            "scheduledEndDate": end_str,
            "scheduledDuration": int(duration),
            "provider": provider_obj,
            "facility": facility_obj,
            "facilityTimeZone": facility_obj.get("timeZone", "US/Eastern"),
            "patient": patient,
            "newPatient": bool(new_patient),
            "appointmentType": appt_type,
            "paymentMethod": "MEDICAL",
            "reportableReason": "MEDICAL_NON_EMERGENCY",
            "patientPcpAbsent": False,
            "overrideAllowed": True,
            "additionalProviders": [],
            "reservations": [],
            "treatmentCaseAuthorization": None,
            "treatmentCase": None,
            "recall": None,
        }
        if notes:
            payload["notes"] = notes

        created = self._client._post(
            "/ema/ws/v2/appointment?mapId=APPOINTMENT_DETAILS", payload
        ).json()
        # client.create_appointment also gated — use _post after require already called
        return {
            "status": "booked",
            "appointment": _appt_summary(created) if isinstance(created, dict) else created,
            "raw_id": (created or {}).get("id") if isinstance(created, dict) else None,
            "writes_enabled": True,
        }

    def reschedule_appointment(
        self,
        *,
        appointment_id,
        new_start: str,
        new_duration: int | None = None,
        provider_id: int | None = None,
        reason: str = "PATIENT_RESCHEDULE",
        confirmed: bool = False,
    ) -> dict:
        from liora_tools.modmed.write_gate import ema_writes_enabled, require_ema_writes
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "message": "Caller must verbally confirm the new time before reschedule.",
                "writes_enabled": ema_writes_enabled(),
            }
        require_ema_writes("reschedule_appointment")
        updated = self._client.reschedule(
            appointment_id=appointment_id,
            new_start=new_start,
            new_duration=new_duration,
            provider_id=provider_id,
            reason=reason,
        )
        return {
            "status": "rescheduled",
            "appointment": _appt_summary(updated) if isinstance(updated, dict) else updated,
            "writes_enabled": True,
        }

    def cancel_appointment(
        self,
        *,
        appointment_id,
        reason: str = "PATIENT_CANCELLED",
        notes: str = "Cancelled via Liora voice agent",
        confirmed: bool = False,
    ) -> dict:
        from liora_tools.modmed.write_gate import ema_writes_enabled, require_ema_writes
        if not confirmed:
            return {
                "status": "needs_confirmation",
                "message": "Caller must verbally confirm cancellation.",
                "writes_enabled": ema_writes_enabled(),
            }
        require_ema_writes("cancel_appointment")
        cancelled = self._client.cancel_appointment(
            appointment_id=appointment_id, reason=reason, notes=notes
        )
        return {
            "status": "cancelled",
            "appointment": _appt_summary(cancelled) if isinstance(cancelled, dict) else cancelled,
            "writes_enabled": True,
        }

