"""ZocdocClient — client for the Zocdoc Provider API (inbox, bookings, messaging).

All API calls are executed via a Playwright browser context to bypass
DataDome bot detection. The browser reuses the persistent profile at
~/.zocdoc-discovery-profile (same profile used by auth refresh).
"""

import json
from datetime import datetime, timezone, timedelta

from liora_tools.config import ZocdocConfig
from liora_tools.exceptions import (
    AuthenticationError, GraphQLError, LioraAPIError, RateLimitError,
)
from liora_tools.zocdoc.browser_transport import BrowserTransport
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

    Auth: Persistent Playwright browser profile with session cookies.
    All requests go through browser fetch() to bypass DataDome.
    """

    def __init__(self, transport: BrowserTransport = None,
                 config: ZocdocConfig = None):
        self._cfg = config or ZocdocConfig()
        self._transport = transport or BrowserTransport()

    @classmethod
    def from_profile(cls, config: ZocdocConfig = None):
        """Create a ZocdocClient using the persistent browser profile."""
        config = config or ZocdocConfig()
        transport = BrowserTransport()
        return cls(transport, config)

    def close(self):
        """Close the browser transport."""
        self._transport.stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- Internal helpers --

    def _now_offset(self) -> str:
        """Current time as OffsetDateTime string for GQL variables."""
        now = datetime.now(timezone(timedelta(hours=-4)))
        s = now.strftime("%Y-%m-%dT%H:%M:%S%z")
        return s[:-2] + ":" + s[-2:]

    # -- GraphQL --

    def gql(self, operation: str, variables: dict, query: str) -> dict:
        """Execute a GraphQL operation via browser transport."""
        payload = {
            "operationName": operation,
            "variables": variables,
            "query": query,
        }
        try:
            data = self._transport.gql(self._cfg.gql_url, payload)
        except RuntimeError as e:
            msg = str(e)
            if "401" in msg:
                raise AuthenticationError(
                    "Zocdoc session expired", status_code=401)
            if "403" in msg:
                raise LioraAPIError(
                    f"Zocdoc API error: {msg}", status_code=403)
            raise LioraAPIError(f"Zocdoc API error: {msg}")

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
        """Send 'call the office' request via REST (through browser)."""
        reasons = reasons or ["Other"]
        url = f"{self._cfg.rest_base}/provider/api/appointments/RequestPatientCall"
        payload = {
            "apptId": str(request_id),
            "requestedInformation": reasons,
        }
        result = self._transport.post(url, payload)
        if result["status"] != 200:
            raise LioraAPIError(
                f"RequestPatientCall failed: {result['status']} {result['body'][:200]}",
                status_code=result["status"],
            )
        return result

    # -- Session --

    def refresh_session(self) -> dict:
        """Refresh auth session via browser."""
        url = f"{self._cfg.rest_base}/auth/user/v1/refresh"
        result = self._transport.post(url, {})
        if result["status"] not in (200, 204):
            raise AuthenticationError(
                f"Session refresh failed: {result['status']}",
                status_code=result["status"],
            )
        return json.loads(result["body"]) if result["body"] else {"status": result["status"]}
