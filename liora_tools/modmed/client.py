"""EmaClient — client for the ModMed EMA API (patients, appointments, scheduling)."""

import requests

from liora_tools.config import EmaConfig
from liora_tools.exceptions import AuthenticationError, LioraAPIError, RateLimitError


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
                        status: str = "ACTIVE", page_size: int = 25) -> list:
        """Search patients by name and status."""
        clauses = []
        if last_name:
            clauses.append(f'lastName=="{last_name}"')
        if first_name:
            clauses.append(f'firstName=="{first_name}"')
        if status:
            clauses.append(f'fn=patientStatus="\"{status}\""')

        where = ";".join(clauses) if clauses else None
        return self.list_patients(
            where=where,
            page_size=page_size,
            selector="lastName,firstName,mrn,id,dateOfBirth,email,cellPhone",
        )

    def get_patient(self, patient_id: str, selector: str = None) -> dict:
        params = {}
        if selector:
            params["selector"] = selector
        return self._get(f"/ema/ws/v3/patients/{patient_id}", params).json()

    def send_portal_email(self, patient_id: str, username: str,
                          email: str, cell_phone: str) -> None:
        """Activate or resend patient portal access email."""
        self._post(f"/ema/ws/v3/patients/{patient_id}/portal", {
            "username": username,
            "email": email,
            "cellPhone": cell_phone,
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
