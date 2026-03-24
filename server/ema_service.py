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


def _bootstrap_cookies():
    """If EMA_COOKIES_B64 env var is set, decode it into the credential store.

    This enables serverless deployments (Vercel) where there's no persistent
    filesystem — cookies are injected via env var on each cold start.
    """
    b64 = os.environ.get("EMA_COOKIES_B64")
    if not b64:
        return
    try:
        data = json.loads(base64.b64decode(b64))
        cookies = data if isinstance(data, list) else data.get("cookies", data)
        save_credentials("ema", {"cookies": cookies})
    except Exception:
        pass


def _get_client() -> EmaClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _bootstrap_cookies()
                _client = get_ema_client()
    return _client


def clear_client():
    global _client
    _client = None


# -- Patients --

async def search_patients(last_name: str = None, first_name: str = None,
                          page_size: int = 25) -> list:
    def _call():
        return _get_client().search_patients(
            last_name=last_name, first_name=first_name, page_size=page_size,
        )
    return await asyncio.to_thread(_call)


async def get_patient(patient_id: str, selector: str = None) -> dict:
    def _call():
        return _get_client().get_patient(patient_id, selector=selector)
    return await asyncio.to_thread(_call)


async def send_portal_email(patient_id: str, username: str, email: str) -> None:
    def _call():
        _get_client().send_portal_email(patient_id, username, email)
    await asyncio.to_thread(_call)


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


async def create_appointment(payload: dict) -> dict:
    def _call():
        return _get_client().create_appointment(payload)
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
