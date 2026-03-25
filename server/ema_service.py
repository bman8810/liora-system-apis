"""Singleton EmaClient manager with async wrappers via asyncio.to_thread."""

import asyncio
import base64
import json
import os
import threading

from liora_tools.auth.session_manager import get_ema_client, save_credentials
from liora_tools.modmed.client import EmaClient

_client: EmaClient | None = None
_lock = threading.Lock()


def _client_from_env() -> EmaClient | None:
    """Try to create an EmaClient directly from the EMA_COOKIES_B64 env var.

    Falls back to writing credentials to disk for the standard get_ema_client()
    flow if the filesystem is writable.
    """
    b64 = os.environ.get("EMA_COOKIES_B64")
    if not b64:
        return None
    try:
        data = json.loads(base64.b64decode(b64))
        cookies = data if isinstance(data, list) else data.get("cookies", data)

        # Try direct construction first (no filesystem needed)
        client = EmaClient.from_cookies(cookies)
        if client.check_session():
            return client

        # If session check fails, try saving to disk for the full refresh flow
        try:
            save_credentials("ema", {"cookies": cookies})
        except OSError:
            pass
    except Exception:
        pass
    return None


def _get_client() -> EmaClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                # Try env-var bootstrap first (serverless), then standard flow
                _client = _client_from_env() or get_ema_client()
    return _client


def clear_client():
    global _client
    _client = None


# -- Patients --

async def search_patients(last_name: str = None, first_name: str = None,
                          phone: str = None, dob: str = None,
                          mrn: str = None, page_size: int = 25) -> list:
    def _call():
        clauses = []
        if last_name:
            clauses.append(f'lastName=="{last_name}"')
        if first_name:
            clauses.append(f'firstName=="{first_name}"')
        if mrn:
            clauses.append(f'mrn=="{mrn}"')
        if dob:
            # EMA needs full timestamp format: 1980-01-08T00:00:00.000+0000
            dob_ts = dob if "T" in dob else f"{dob}T00:00:00.000+0000"
            clauses.append(f'dateOfBirth=="{dob_ts}"')

        # Phone isn't queryable server-side — fetch results then filter client-side.
        # Best used alongside name/DOB to narrow the server-side result set.
        phone_digits = None
        if phone:
            phone_digits = "".join(c for c in phone if c.isdigit())

        where = ";".join(clauses) if clauses else None
        client = _get_client()
        selector = "lastName,firstName,mrn,id,dateOfBirth,email,cellPhone,phoneNumbers,patientStatus"

        # When filtering by phone, fetch a larger batch for client-side matching
        fetch_size = 100 if phone_digits else page_size
        results = client.list_patients(
            where=where, page_size=fetch_size, selector=selector,
        )

        if phone_digits:
            filtered = []
            for p in results:
                # Check cellPhone
                cell = p.get("cellPhone") or {}
                if cell.get("phoneNumber", "").replace("-", "") == phone_digits:
                    filtered.append(p)
                    continue
                # Check phoneNumbers array
                for pn in p.get("phoneNumbers") or []:
                    if pn.get("phoneNumber", "").replace("-", "") == phone_digits:
                        filtered.append(p)
                        break
            return filtered[:page_size]

        return results
    return await asyncio.to_thread(_call)


async def get_patient(patient_id: str, selector: str = None) -> dict:
    def _call():
        return _get_client().get_patient(patient_id, selector=selector)
    return await asyncio.to_thread(_call)


async def send_portal_email(patient_id: str, username: str, email: str) -> None:
    def _call():
        _get_client().send_portal_email(patient_id, username, email)
    await asyncio.to_thread(_call)


async def get_patient_appointments(patient_id: str, start_date: str = None,
                                   end_date: str = None, page_size: int = 50) -> list:
    def _call():
        where = f'patient=={patient_id}'
        return _get_client().list_appointments(
            start_date=start_date, end_date=end_date,
            where=where, page_size=page_size,
            selector="id,scheduledStartDate,scheduledEndDate,scheduledDuration,"
                     "appointmentTypeName,status,patient(id,lastName,firstName,mrn),"
                     "provider(id,name),facility(id,name)",
        )
    return await asyncio.to_thread(_call)


# -- Appointments --

async def list_appointments(start_date: str = None, end_date: str = None,
                            page_size: int = 50) -> list:
    def _call():
        return _get_client().list_appointments(
            start_date=start_date, end_date=end_date, page_size=page_size,
        )
    return await asyncio.to_thread(_call)


async def get_appointment(appointment_id: str, selector: str = None) -> dict:
    def _call():
        return _get_client().get_appointment(appointment_id, selector=selector)
    return await asyncio.to_thread(_call)


async def create_appointment(patient_id: int, provider_id: int,
                             facility_id: int, appointment_type_id: int,
                             scheduled_start: str, duration: int = 15,
                             reason: str = "", notes: str = "",
                             new_patient: bool = False) -> dict:
    def _call():
        from datetime import datetime, timedelta

        # EMA requires .000Z format for create
        start_dt = datetime.fromisoformat(scheduled_start.replace("Z", "+00:00"))
        end_dt = start_dt + timedelta(minutes=duration)
        start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        client = _get_client()

        # EMA v2 create requires full patient, provider, facility, and
        # appointmentType objects — not just IDs. Fetch them all.
        patient = client.get_patient(str(patient_id))

        # Get full provider + facility from an existing appointment or facilities list
        facilities = client.list_facilities()
        facility_obj = next((f for f in facilities if f["id"] == facility_id), {"id": facility_id})

        # Get full appointment type object
        appt_types = client.list_appointment_types()
        appt_type = next((t for t in appt_types if t["id"] == appointment_type_id), {"id": appointment_type_id})

        # Get full provider object from a recent appointment
        recent = client.list_appointments(page_size=1)
        if recent:
            recent_full = client._get(
                f"/ema/ws/v2/appointment/{recent[0]['id']}", {"mapId": "CHECK_IN"},
            ).json()
            provider_obj = recent_full["provider"]
            # Override with requested provider if different
            if provider_obj.get("id") != provider_id:
                provider_obj = {"id": provider_id}
        else:
            provider_obj = {"id": provider_id}

        payload = {
            "status": "PENDING",
            "scheduledStartDate": start_str,
            "scheduledEndDate": end_str,
            "scheduledDuration": duration,
            "provider": provider_obj,
            "facility": facility_obj,
            "facilityTimeZone": facility_obj.get("timeZone", "US/Eastern"),
            "patient": patient,
            "newPatient": new_patient,
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
        if reason:
            payload["notes"] = reason
        if notes:
            payload["notes"] = notes

        # Use mapId=APPOINTMENT_DETAILS — required for v2 create
        return client._post(
            "/ema/ws/v2/appointment?mapId=APPOINTMENT_DETAILS", payload,
        ).json()
    return await asyncio.to_thread(_call)


async def update_appointment(appointment_id: str, payload: dict) -> dict:
    def _call():
        return _get_client().update_appointment(appointment_id, payload)
    return await asyncio.to_thread(_call)


# -- Scheduling --

async def find_slots(appt_type_id: str, duration: int = 15,
                     time_of_day: str = "ANYTIME", specific_date: str = None,
                     time_frame: str = "FIRST_AVAILABLE",
                     display: str = "BY_PROVIDER") -> list:
    def _call():
        return _get_client().find_slots(
            appt_type_id=appt_type_id, duration=duration,
            time_of_day=time_of_day, specific_date=specific_date,
            time_frame=time_frame, display=display,
        )
    return await asyncio.to_thread(_call)


async def reschedule(appointment_id: str, new_start: str,
                     new_duration: int = None, provider_id: int = None,
                     reason: str = "PATIENT_RESCHEDULE") -> dict:
    def _call():
        return _get_client().reschedule(
            appointment_id=appointment_id, new_start=new_start,
            new_duration=new_duration, provider_id=provider_id, reason=reason,
        )
    return await asyncio.to_thread(_call)


async def cancel_appointment(appointment_id: str, reason: str = "PATIENT_CANCELLED",
                             notes: str = "") -> dict:
    def _call():
        return _get_client().cancel_appointment(
            appointment_id=appointment_id, reason=reason, notes=notes,
        )
    return await asyncio.to_thread(_call)


async def list_cancel_reasons() -> list:
    def _call():
        return _get_client().list_cancel_reasons()
    return await asyncio.to_thread(_call)


# -- Reference --

async def list_appointment_types() -> list:
    def _call():
        return _get_client().list_appointment_types()
    return await asyncio.to_thread(_call)


async def list_facilities() -> list:
    def _call():
        return _get_client().list_facilities()
    return await asyncio.to_thread(_call)


# -- Health --

async def check_session() -> bool:
    def _call():
        return _get_client().check_session()
    return await asyncio.to_thread(_call)
