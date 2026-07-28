"""Read-only ModMed EMA scheduling orchestration for Genie agents."""

from __future__ import annotations

import os
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

# Dr. Libby Rhee — default medical primary. Override via EMA_PRIMARY_PROVIDER_IDS.
DEFAULT_PRIMARY_PROVIDER_IDS = frozenset({8327689})
PRIMARY_PROVIDER_NAME_TOKENS = ("rhee",)
PLACEHOLDER_PROVIDER_PREFIXES = (
    "zzz",
    "test",
    "training",
    "phreesia",
    "placeholder",
)
AESTHETIC_NAME_TOKENS = ("aesthetic", "injector", "laser")
SLOT_RANKING_LABEL = "non_zzz_then_rhee_first_then_start"

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


def primary_provider_ids() -> frozenset[int]:
    """Configured primary clinician ids (default Rhee). Comma-separated env override."""
    raw = (os.environ.get("EMA_PRIMARY_PROVIDER_IDS") or "").strip()
    if not raw:
        return DEFAULT_PRIMARY_PROVIDER_IDS
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids) if ids else DEFAULT_PRIMARY_PROVIDER_IDS


def _normalize_provider_name(name: str | None) -> str:
    return (name or "").strip().lower()


def is_placeholder_provider(name: str | None) -> bool:
    """True for lab/test fillers (zzz*, test*, training*, phreesia*, placeholder*)."""
    n = _normalize_provider_name(name)
    if not n:
        return False
    return any(n.startswith(p) for p in PLACEHOLDER_PROVIDER_PREFIXES)


def _provider_id_key(provider_id) -> int:
    try:
        return int(provider_id)
    except (TypeError, ValueError):
        return 0


def slot_rank_key(slot: dict) -> tuple:
    """Deterministic sort key for open slots (lower is better).

    Order:
      1. real clinicians before placeholder/zzz/test providers
      2. configured primary (Rhee) before other clinicians
      3. general clinicians before aesthetic-leaning names
      4. earlier start time
      5. provider id / name (stable tie-break)
    """
    name = slot.get("provider_name")
    n = _normalize_provider_name(name)
    pid = _provider_id_key(slot.get("provider_id"))
    placeholder = 1 if is_placeholder_provider(name) else 0

    if not placeholder and (
        pid in primary_provider_ids()
        or any(tok in n for tok in PRIMARY_PROVIDER_NAME_TOKENS)
    ):
        tier = 0
    elif any(tok in n for tok in AESTHETIC_NAME_TOKENS):
        tier = 2
    else:
        tier = 1

    start = slot.get("start") or ""
    return (placeholder, tier, start, pid, n)


def rank_open_slots(slots: list[dict], *, limit: int | None = None) -> list[dict]:
    """Stable deterministic ranking; does not drop placeholder capacity."""
    ranked = sorted(slots, key=slot_rank_key)
    if limit is not None:
        return ranked[: max(0, int(limit))]
    return ranked


def _round_robin_slots(per_group: list[list[dict]], *, limit: int) -> list[dict]:
    """Interleave provider queues so the first EMA group cannot fill the whole limit."""
    if limit <= 0:
        return []
    queues = [list(row) for row in per_group if row]
    # Prefer starting RR at higher-ranked providers (Rhee before zzz).
    queues.sort(key=lambda row: slot_rank_key(row[0]))
    picked: list[dict] = []
    while len(picked) < limit and queues:
        next_queues: list[list[dict]] = []
        progressed = False
        for row in queues:
            if not row:
                continue
            picked.append(row.pop(0))
            progressed = True
            if row:
                next_queues.append(row)
            if len(picked) >= limit:
                # Preserve remaining capacity only matters before final rank+slice;
                # stop once we have enough candidates.
                break
        if not progressed:
            break
        # If we hit limit mid-pass, still drop empty queues; unused tails discarded.
        if len(picked) >= limit:
            break
        queues = next_queues
    return picked


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
        """Flatten EMA finder groups into ranked slots (Rhee-first, non-zzz).

        EMA often returns placeholder providers (zzz*) first. Collect per-provider
        queues, round-robin so the first group cannot starve others, then apply a
        deterministic rank: real clinicians → primary (Rhee) → others → placeholders,
        then start time. Placeholders stay available; they are only deprioritized.
        """
        groups = self._client.find_slots(
            appt_type_id=str(appt_type_id),
            duration=duration,
            time_of_day=time_of_day,
            specific_date=specific_date,
            time_frame=time_frame,
            display=display,
        )

        per_group: list[list[dict]] = []
        for group in groups or []:
            provider = group.get("provider") or {}
            facility = group.get("facility") or {}
            provider_id = provider.get("id")
            provider_name = _provider_name(provider)
            facility_id = facility.get("id")
            facility_name = facility.get("name")
            time_zone = facility.get("timeZone")
            row: list[dict] = []
            for appt in group.get("appointments") or []:
                row.append({
                    "start": appt.get("scheduledStartDate"),
                    "end": appt.get("scheduledEndDate"),
                    "duration": appt.get("scheduledDuration"),
                    "provider_id": provider_id,
                    "provider_name": provider_name,
                    "facility_id": facility_id,
                    "facility_name": facility_name,
                    "time_zone": appt.get("timeZoneId") or time_zone,
                })
            if row:
                per_group.append(row)

        # Over-collect slightly so ranking can still surface primary after RR.
        pool_limit = max(int(limit), 0) * max(len(per_group), 1)
        if limit > 0:
            pool_limit = max(pool_limit, int(limit))
        picked = _round_robin_slots(per_group, limit=pool_limit if limit > 0 else 0)
        slots = rank_open_slots(picked, limit=limit)

        return {
            "appt_type_id": appt_type_id,
            "count": len(slots),
            "slots": slots,
            "ranking": SLOT_RANKING_LABEL,
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
