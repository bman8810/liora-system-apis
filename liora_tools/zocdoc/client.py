"""ZocdocClient — client for the Zocdoc Provider API (inbox, bookings, messaging)."""

import json
from datetime import datetime, timezone, timedelta

import requests

from liora_tools.config import ZocdocConfig
from liora_tools.exceptions import (
    AuthenticationError, GraphQLError, LioraAPIError, RateLimitError,
)
from liora_tools.zocdoc.queries import (
    GET_INBOX_ROWS, GET_PHI_APPOINTMENT_DETAILS,
    GET_STATUS_AGGREGATES, MARK_INBOX_APPOINTMENT_STATUS,
)

ALL_STATUSES = [
    "UNCONFIRMED", "PATIENT_RESCHEDULED", "PATIENT_CANCELLED",
    "CONFIRMED", "PROVIDER_RESCHEDULED",
    "SYNC_CONFIRMED", "SYNC_PATIENT_RESCHEDULED",
]
COUNT_STATUSES = [
    "UNCONFIRMED", "PATIENT_RESCHEDULED", "PATIENT_CANCELLED",
    "SYNC_CONFIRMED", "SYNC_PATIENT_RESCHEDULED",
]
APPOINTMENT_SOURCES = ["MARKETPLACE", "API", "MANUAL_INTAKE"]


class ZocdocClient:
    """Client for the Zocdoc Provider API.

    Auth: Cookie-based + x-datadome-clientid header.
    GQL at api2.zocdoc.com works from Python; REST at www.zocdoc.com is
    blocked by DataDome — use browser-based methods for those calls.
    """

    def __init__(self, session: requests.Session, config: ZocdocConfig = None):
        self._s = session
        self._cfg = config or ZocdocConfig()

    @classmethod
    def from_cookies(cls, cookies: list, config: ZocdocConfig = None):
        """Create a ZocdocClient from a list of cookie dicts."""
        from liora_tools.auth.zocdoc import get_session
        config = config or ZocdocConfig()
        session = get_session(cookies, config)
        return cls(session, config)

    # -- Internal helpers --

    def _now_offset(self) -> str:
        """Current time as OffsetDateTime string for GQL variables."""
        now = datetime.now(timezone(timedelta(hours=-4)))
        s = now.strftime("%Y-%m-%dT%H:%M:%S%z")
        return s[:-2] + ":" + s[-2:]

    # -- GraphQL --

    def gql(self, operation: str, variables: dict, query: str) -> dict:
        """Execute a GraphQL operation."""
        r = self._s.post(self._cfg.gql_url, json={
            "operationName": operation,
            "variables": variables,
            "query": query,
        })
        if r.status_code == 401:
            raise AuthenticationError("Zocdoc session expired", status_code=401, response=r)
        if r.status_code == 429:
            raise RateLimitError("Zocdoc rate limit exceeded", status_code=429, response=r)
        if not r.ok:
            raise LioraAPIError(
                f"Zocdoc API error: {r.status_code} {r.text[:200]}",
                status_code=r.status_code, response=r,
            )
        data = r.json()
        if "errors" in data:
            raise GraphQLError(
                f"GraphQL errors: {json.dumps(data['errors'])}",
                errors=data["errors"],
            )
        return data

    # -- Inbox / Bookings --

    def list_bookings(self, page_number: int = 1, page_size: int = 20,
                      statuses: list = None, patient_name: str = "") -> dict:
        variables = {
            "practiceId": self._cfg.practice_id,
            "pageNumber": page_number,
            "pageSize": page_size,
            "fromAppointmentTime": self._now_offset(),
            "tableAppointmentStatuses": statuses or ALL_STATUSES,
            "statusCountStatuses": COUNT_STATUSES,
            "productType": "INBOX",
            "sortType": "DESCENDING",
            "sortByField": "LAST_UPDATED_BY_PATIENT",
            "providerIdsFilter": [],
            "locationIdsFilter": [],
            "appointmentSources": APPOINTMENT_SOURCES,
            "patientName": patient_name,
            "intakeReviewStates": None,
        }
        return self.gql("getInboxRows", variables, GET_INBOX_ROWS)

    def get_booking(self, appointment_id: str,
                    appointment_source: str = "MARKETPLACE") -> dict:
        variables = {
            "practiceId": self._cfg.practice_id,
            "appointmentId": appointment_id,
            "appointmentSource": appointment_source,
            "shouldFetchIntake": True,
        }
        return self.gql("getPhiAppointmentDetails", variables, GET_PHI_APPOINTMENT_DETAILS)

    def get_status_counts(self) -> dict:
        variables = {
            "practiceId": self._cfg.practice_id,
            "fromAppointmentTime": self._now_offset(),
            "appointmentStatuses": COUNT_STATUSES,
            "productType": "INBOX",
            "appointmentSources": APPOINTMENT_SOURCES + ["APPOINTMENT_LIST"],
        }
        return self.gql("getAppointmentStatusAggregates", variables, GET_STATUS_AGGREGATES)

    # -- Messaging --

    def mark_as_read(self, appointment_id: str,
                     appointment_source: str = "MARKETPLACE") -> dict:
        variables = {
            "practiceId": self._cfg.practice_id,
            "appointmentId": appointment_id,
            "appointmentSource": appointment_source,
            "status": "CONFIRMED",
        }
        return self.gql("markInboxAppointmentStatus", variables, MARK_INBOX_APPOINTMENT_STATUS)

    def send_call_request(self, request_id: str, reasons: list = None) -> dict:
        """Send 'call the office' request via REST.

        NOTE: This endpoint is blocked by DataDome for Python requests.
        Use liora_tools.auth.zocdoc.send_call_request_browser() instead.
        """
        reasons = reasons or ["Other"]
        r = self._s.post(
            f"{self._cfg.rest_base}/provider/api/appointments/RequestPatientCall",
            json={
                "apptId": str(request_id),
                "requestedInformation": reasons,
            },
        )
        if not r.ok:
            raise LioraAPIError(
                f"RequestPatientCall failed: {r.status_code} {r.text[:200]}",
                status_code=r.status_code, response=r,
            )
        return {"status": r.status_code, "body": r.text}

    # -- Session --

    def refresh_session(self) -> dict:
        """Refresh auth session."""
        r = self._s.post(f"{self._cfg.rest_base}/auth/user/v1/refresh")
        if not r.ok:
            raise AuthenticationError(
                f"Session refresh failed: {r.status_code}",
                status_code=r.status_code, response=r,
            )
        return r.json() if r.text else {"status": r.status_code}
