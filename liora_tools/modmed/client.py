"""EmaClient — client for the ModMed EMA API (patients, appointments, scheduling)."""

from __future__ import annotations

import requests

from liora_tools.config import EmaConfig
from liora_tools.exceptions import (
    AuthenticationError,
    LioraAPIError,
    OptimisticLockError,
    RateLimitError,
)


class EmaClient:
    """Client for the ModMed EMA API.

    Auth: Session cookies via Keycloak SSO. EMA returns 302 (not 401) on
    expired sessions — all requests use allow_redirects=False.
    """

    def __init__(self, session: requests.Session, config: EmaConfig = None):
        self._s = session
        self._cfg = config or EmaConfig()

    @classmethod
    def from_cookies(cls, cookies: list, config: EmaConfig = None):
        """Create an EmaClient from a list of cookie dicts."""
        config = config or EmaConfig()
        session = requests.Session()
        for c in cookies:
            session.cookies.set(
                c["name"], c["value"],
                domain=c["domain"], path=c.get("path", "/"),
            )
        return cls(session, config)

    @classmethod
    def connect(cls, config: EmaConfig = None):
        """Create an EmaClient with automatic auth (saved cookies -> SSO refresh -> browser login)."""
        from liora_tools.auth.ema import ensure_session
        config = config or EmaConfig()
        session, _cookies = ensure_session(config=config)
        return cls(session, config)

    # -- Internal helpers --

    def _get(self, path: str, params: dict = None) -> requests.Response:
        r = self._s.get(
            f"{self._cfg.base_url}{path}",
            params=params,
            allow_redirects=False,
        )
        self._check_response(r)
        return r

    def _post(self, path: str, json: dict = None) -> requests.Response:
        r = self._s.post(
            f"{self._cfg.base_url}{path}",
            json=json,
            allow_redirects=False,
        )
        self._check_response(r)
        return r

    def _put(self, path: str, json: dict = None) -> requests.Response:
        r = self._s.put(
            f"{self._cfg.base_url}{path}",
            json=json,
            allow_redirects=False,
        )
        self._check_response(r)
        return r

    def _check_response(self, r: requests.Response) -> None:
        if r.status_code == 302:
            location = r.headers.get("Location", "")
            raise AuthenticationError(
                f"EMA session expired (302 redirect to {location})",
                status_code=302, response=r,
            )
        if r.status_code == 429:
            raise RateLimitError("EMA rate limit exceeded", status_code=429, response=r)
        if r.status_code == 409:
            raise OptimisticLockError(
                "objectLockVersion mismatch — appointment was modified concurrently",
                status_code=409, response=r,
            )
        if not r.ok:
            raise LioraAPIError(
                f"EMA API error: {r.status_code} {r.text[:200]}",
                status_code=r.status_code, response=r,
            )

    # -- Session --

    def check_session(self) -> bool:
        """Test if the current session is alive by hitting /v3/facilities."""
        try:
            self._get("/ema/ws/v3/facilities", {"paging.pageSize": "1"})
            return True
        except AuthenticationError:
            return False

    # -- Patients --

    def list_patients(self, where: str = None, page_size: int = 25,
                      page_number: int = 1, selector: str = None,
                      sort_by: str = "lastName") -> list:
        params = {
            "paging.pageSize": str(page_size),
            "paging.pageNumber": str(page_number),
            "sorting.sortBy": sort_by,
            "sorting.sortOrder": "ASC",
        }
        if where:
            params["where"] = where
        if selector:
            params["selector"] = selector
        return self._get("/ema/ws/v3/patients", params).json()

    def search_patients(self, last_name: str = None, first_name: str = None,
                        status: str = None, page_size: int = 25) -> list:
        """Search patients by name and optionally status."""
        clauses = []
        if last_name:
            clauses.append(f'lastName=="{last_name}"')
        if first_name:
            clauses.append(f'firstName=="{first_name}"')
        if status:
            clauses.append(f'patientStatus=="{status}"')

        where = ";".join(clauses) if clauses else None
        return self.list_patients(
            where=where,
            page_size=page_size,
            selector="lastName,firstName,mrn,id,dateOfBirth,email,cellPhone,patientStatus",
        )

    def get_patient(self, patient_id: str, selector: str = None) -> dict:
        params = {}
        if selector:
            params["selector"] = selector
        return self._get(f"/ema/ws/v3/patients/{patient_id}", params).json()

    def send_portal_email(self, patient_id: str, username: str,
                          email: str) -> None:
        """Activate or resend patient portal access email.

        NOTE: Do NOT include cellPhone — the EMA API returns 500 when it's present.
        """
        self._post(f"/ema/ws/v3/patients/{patient_id}/portal", {
            "username": username,
            "email": email,
        })

    # -- Appointments --

    def list_appointments(self, start_date: str = None, end_date: str = None,
                          selector: str = None, where: str = None,
                          page_size: int = 50) -> list:
        params = {"paging.pageSize": str(page_size)}
        if selector:
            params["selector"] = selector

        clauses = []
        if where:
            clauses.append(where)
        if start_date:
            clauses.append(f'scheduledStartDateLd>="{start_date}"')
        if end_date:
            clauses.append(f'scheduledStartDateLd<="{end_date}"')
        if clauses:
            params["where"] = ";".join(clauses)

        return self._get("/ema/ws/v3/appointments", params).json()

    def get_appointment(self, appointment_id: str, selector: str = None) -> dict:
        params = {}
        if selector:
            params["selector"] = selector
        return self._get(f"/ema/ws/v3/appointments/{appointment_id}", params).json()

    def create_appointment(self, payload: dict) -> dict:
        """Create an appointment (v2 endpoint)."""
        return self._post("/ema/ws/v2/appointment", payload).json()

    def update_appointment(self, appointment_id: str, payload: dict) -> dict:
        """Update an appointment (v2 endpoint)."""
        return self._put(f"/ema/ws/v2/appointment/{appointment_id}", payload).json()

    def reschedule(
        self,
        appointment_id: str | int,
        new_start: str,
        new_duration: int = None,
        provider_id: int = None,
        reason: str = "PATIENT_RESCHEDULE",
    ) -> dict:
        """Reschedule an appointment to a new date/time.

        Uses a read-before-write pattern: fetches the current appointment
        (including objectLockVersion), updates the time fields, and POSTs
        back to the v2 endpoint.

        Args:
            appointment_id: EMA appointment ID.
            new_start: New start time in ISO 8601 UTC (e.g. "2026-03-16T13:00:00.000Z").
            new_duration: Duration in minutes. None = keep existing.
            provider_id: New provider ID. None = keep existing.
            reason: Reschedule reason enum — "PATIENT_RESCHEDULE" or "OFFICE_EDIT".

        Returns:
            The updated appointment dict from the API.

        Raises:
            OptimisticLockError: If the appointment was modified concurrently.
        """
        from datetime import datetime, timedelta

        # Step 1: Fetch current appointment state (v2 gives us the full object)
        current = self._get(
            f"/ema/ws/v2/appointment/{appointment_id}",
            {"mapId": "CHECK_IN"},
        ).json()

        # Step 2: Compute new end time
        duration = new_duration if new_duration is not None else current.get("scheduledDuration", 10)
        start_dt = datetime.fromisoformat(new_start.replace("Z", "+00:00"))
        end_dt = start_dt + timedelta(minutes=duration)
        new_end = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Step 3: Update time fields in the current object
        current["scheduledStartDate"] = new_start
        current["scheduledEndDate"] = new_end
        current["scheduledDuration"] = duration
        current["rescheduleReason"] = reason
        current["overrideAllowed"] = True

        if provider_id is not None:
            current.setdefault("provider", {})["id"] = provider_id

        # Step 4: POST to the reschedule endpoint
        return self._post(
            f"/ema/ws/v2/appointment?id={appointment_id}&mapId=APPOINTMENT_DETAILS",
            current,
        ).json()

    def cancel_appointment(
        self,
        appointment_id: str | int,
        reason: str = "PATIENT_CANCELLED",
        notes: str = "",
    ) -> dict:
        """Cancel an appointment.

        Args:
            appointment_id: EMA appointment ID.
            reason: Cancel reason enum (e.g. "PATIENT_CANCELLED", "OFFICE_RESCHEDULED",
                    "OFFICE_SCHEDULING_ERROR", "PATIENT_SCHEDULE_CHANGE").
            notes: Optional free-text cancellation notes.

        Returns:
            The cancelled appointment dict from the API.
        """
        # Step 1: Look up the cancel reason ID from the reason name/enum
        reasons = self._get("/ema/ws/v3/appointment/cancel-reason", {
            "where": 'active=="true"',
            "sorting.sortBy": "name",
            "sorting.sortOrder": "asc",
            "paging.pageSize": "20",
        }).json()

        # Match by reasonId or name (case-insensitive)
        reason_upper = reason.upper().replace(" ", "_")
        cancel_reason = None
        for r in reasons:
            if r.get("reasonId", "").upper() == reason_upper:
                cancel_reason = r
                break
            # Also match against the name with underscores (e.g. "PATIENT_CANCELLED" -> "Patient Cancelled")
            name_normalized = r.get("name", "").upper().replace(" ", "_")
            if name_normalized == reason_upper:
                cancel_reason = r
                break

        if cancel_reason is None:
            available = [r["name"] for r in reasons]
            raise LioraAPIError(
                f"Unknown cancel reason '{reason}'. Available: {available}"
            )

        # Step 2: POST to the cancel endpoint
        return self._post(
            f"/ema/ws/v3/appointments/{appointment_id}/cancel"
            f"?cancelReason={cancel_reason['name'].upper().replace(' ', '_')}"
            f"&customCancelReasonId={cancel_reason['id']}"
            f"&cancelNotes={notes}",
        ).json()

    def list_cancel_reasons(self) -> list:
        """List available appointment cancellation reasons."""
        return self._get("/ema/ws/v3/appointment/cancel-reason", {
            "where": 'active=="true"',
            "sorting.sortBy": "name",
            "sorting.sortOrder": "asc",
            "paging.pageSize": "20",
        }).json()

    def find_slots(self, appt_type_id: str, duration: int = 15,
                   time_of_day: str = "ANYTIME", specific_date: str = None,
                   time_frame: str = "FIRST_AVAILABLE",
                   display: str = "BY_PROVIDER") -> list:
        """Find available appointment slots."""
        params = {
            "apptTypeId": str(appt_type_id),
            "duration": str(duration),
            "timeOfDay": time_of_day,
            "timeFrame": time_frame,
            "display": display,
            "alternateDurationsIncluded": "true",
            "canOverrideDefaults": "true",
            "afterFirstAppointment": "false",
            "timeBuffer": "0",
        }
        if specific_date:
            params["specificDate"] = f"{specific_date}T00:00:00.000Z"
        return self._get("/ema/ws/v2/appointment/finder", params).json()

    # -- Reference Data --

    def list_appointment_types(self, page_size: int = 100) -> list:
        return self._get("/ema/ws/v3/appointmentType", {
            "paging.pageSize": str(page_size),
        }).json()

    def list_facilities(self, page_size: int = 100) -> list:
        return self._get("/ema/ws/v3/facilities", {
            "paging.pageSize": str(page_size),
        }).json()
