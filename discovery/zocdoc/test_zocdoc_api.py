"""
Zocdoc Provider API client — authenticate via Playwright, then exercise inbox/messaging APIs.
Launches a visible Chrome browser — credentials are entered automatically, cookies extracted.

Usage:
    python3 test_zocdoc_api.py                         # login + run all tests
    python3 test_zocdoc_api.py --cookies zocdoc_cookies.json  # skip login, use saved cookies
    python3 test_zocdoc_api.py --send                  # also send a test "call request" message

SAFETY: Send messages ONLY target the first inbox appointment — review before using --send.
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone, timedelta

import requests as req

# --- Constants ---
GQL_URL = "https://api2.zocdoc.com/provider/v1/gql"
REST_BASE = "https://www.zocdoc.com"
PRACTICE_ID = "pt_FMyrNSVN50CbgjEI0NcL9h"
PROVIDER_ID = "pr_eTTyn6m-e0y7oL1yjr9JQB"

# Appointment statuses for inbox queries
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


def get_session(cookies: list) -> req.Session:
    """Create a requests.Session with Zocdoc auth cookies."""
    s = req.Session()

    # Build cookie header manually — requests' cookie jar has domain-matching
    # issues with subdomains. All zocdoc cookies should be sent to all *.zocdoc.com.
    cookie_dict = {c["name"]: c["value"] for c in cookies}

    # Extract datadome client ID for the anti-bot header
    datadome_id = cookie_dict.get("datadome", "")

    s.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.zocdoc.com",
        "Referer": f"https://www.zocdoc.com/provider/inbox/{PRACTICE_ID}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "x-datadome-clientid": datadome_id,
    })

    # Set cookies using the cookie jar with proper domains
    for c in cookies:
        domain = c.get("domain", ".zocdoc.com")
        # Ensure leading dot for domain cookies
        if not domain.startswith(".") and not domain.startswith("www"):
            domain = "." + domain
        s.cookies.set(
            c["name"], c["value"],
            domain=domain,
            path=c.get("path", "/"),
        )

    return s


# --- Auth ---
def login_browser() -> list:
    """Login to Zocdoc via Playwright and return cookies."""
    from playwright.sync_api import sync_playwright

    profile = os.path.expanduser("~/.zocdoc-discovery-profile")
    print("[1] Launching Chrome with persistent profile...")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        print("[2] Navigating to Zocdoc sign-in...")
        page.goto("https://www.zocdoc.com/signin?provider=1", wait_until="networkidle")
        time.sleep(2)

        # Check if already logged in
        if "/practice/" in page.url or "/provider/" in page.url:
            print(f"    Already logged in! URL: {page.url}")
        else:
            # Step 1: Email
            print("[3] Entering email...")
            email_input = page.wait_for_selector(
                'input[type="email"], input[name="email"]', timeout=10000
            )
            email_input.fill("barric@galatiq.ai")
            time.sleep(0.5)
            page.click('button[type="submit"]')
            time.sleep(3)

            # Step 2: Password
            print("[4] Entering password...")
            pass_input = page.wait_for_selector('input[type="password"]:visible', timeout=10000)
            pass_input.fill("Mynewpass1@")
            time.sleep(0.5)
            page.click('button[type="submit"]')

            print("[5] Waiting for login...")
            page.wait_for_url("**/practice/**", timeout=30000)
            print(f"    Login successful! URL: {page.url}")

        print("[6] Extracting cookies...")
        cookies = ctx.cookies()
        zocdoc_cookies = [c for c in cookies if "zocdoc" in c.get("domain", "")]
        print(f"    Got {len(zocdoc_cookies)} Zocdoc cookies")

        # Save for reuse
        with open("zocdoc_cookies.json", "w") as f:
            json.dump(zocdoc_cookies, f, indent=2)
        print("    Saved to zocdoc_cookies.json")

        ctx.close()
        return zocdoc_cookies


# --- GraphQL helpers ---
def gql(session: req.Session, operation: str, variables: dict, query: str) -> dict:
    """Execute a GraphQL operation."""
    r = session.post(GQL_URL, json={
        "operationName": operation,
        "variables": variables,
        "query": query,
    })
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise Exception(f"GraphQL errors: {json.dumps(data['errors'])}")
    return data


# --- Inbox APIs ---
def list_bookings(session: req.Session, page_number: int = 1, page_size: int = 20,
                  statuses: list = None, patient_name: str = "") -> dict:
    """List inbox bookings (who scheduled + when)."""
    now = datetime.now(timezone(timedelta(hours=-4)))
    from_time = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Format timezone as -04:00 instead of -0400
    from_time = from_time[:-2] + ":" + from_time[-2:]

    query = """query getInboxRows($practiceId: String!, $fromAppointmentTime: OffsetDateTime!, $tableAppointmentStatuses: [AppointmentStatus!]!, $statusCountStatuses: [AppointmentStatus!]!, $intakeReviewStates: [IntakeReviewState!], $productType: ProviderProduct, $pageSize: Int!, $pageNumber: Int!, $sortType: AppointmentSortType, $sortByField: AppointmentSortByField, $providerIdsFilter: [String!], $locationIdsFilter: [String!], $appointmentSources: [AppointmentSource!], $patientName: String!) {
  statusCounts: appointmentStatusAggregates(request: {practiceId: $practiceId, appointmentStatuses: $statusCountStatuses, fromAppointmentTime: $fromAppointmentTime, productType: $productType, appointmentSources: $appointmentSources}) {
    status
    count
    __typename
  }
  practice(practiceId: $practiceId) {
    timeZone
    __typename
  }
  appointments(request: {practiceId: $practiceId, appointmentStatuses: $tableAppointmentStatuses, fromAppointmentTime: $fromAppointmentTime, productType: $productType, pageSize: $pageSize, pageNumber: $pageNumber, sortType: $sortType, sortByField: $sortByField, providerIdsFilter: $providerIdsFilter, locationIdsFilter: $locationIdsFilter, appointmentSources: $appointmentSources, patientName: $patientName, intakeReviewStates: $intakeReviewStates}) {
    pagesCount
    totalAppointmentsCount
    appointments {
      appointmentId
      appointmentSource
      appointmentStatus
      appointmentTimeUtc
      bookingTimeUtc
      lastUpdatedUtc
      lastUpdatedByPatientUtc
      patientType
      isInNetwork
      patient {
        firstName
        lastName
        __typename
      }
      procedure {
        name
        __typename
      }
      provider {
        fullName
        __typename
      }
      insurance {
        carrier {
          id
          name
          __typename
        }
        plan {
          id
          name
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}"""

    variables = {
        "practiceId": PRACTICE_ID,
        "pageNumber": page_number,
        "pageSize": page_size,
        "fromAppointmentTime": from_time,
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

    return gql(session, "getInboxRows", variables, query)


def get_booking(session: req.Session, appointment_id: str,
                appointment_source: str = "MARKETPLACE") -> dict:
    """Get full appointment details including patient PHI."""
    query = """query getPhiAppointmentDetails($practiceId: String, $appointmentId: String, $appointmentSource: AppointmentSource, $shouldFetchIntake: Boolean!) {
  appointmentDetails(request: {practiceId: $practiceId, appointmentIdentifier: {appointmentId: $appointmentId, appointmentSource: $appointmentSource}}) {
    appointmentIdentifier {
      appointmentId
      appointmentSource
      __typename
    }
    appointmentStatus
    practiceId
    requestId
    appointmentTimeUtc
    bookingTimeUtc
    lastUpdatedUtc
    patientType
    bookedBy
    isInNetwork
    duration
    procedure {
      id
      name
      __typename
    }
    practice {
      id
      name
      timeZone
      __typename
    }
    provider {
      id
      fullName
      npi
      __typename
    }
    location {
      id
      address1
      address2
      city
      state
      zipCode
      phoneNumber
      practiceFacingName
      __typename
    }
    insurance {
      card {
        memberId
        frontImageUrl
        backImageUrl
        __typename
      }
      carrier {
        id
        name
        __typename
      }
      plan {
        id
        name
        networkType
        programType
        __typename
      }
      __typename
    }
    patient {
      id
      firstName
      lastName
      email
      dob
      sex
      phoneNumber
      note
      cloudId
      requestedToCallTimestamp
      address {
        address1
        address2
        city
        state
        zipCode
        __typename
      }
      __typename
    }
    appointmentHistory {
      cancellationDetails {
        cancellationReason
        cancelledAtTimestamp
        __typename
      }
      reschedulingDetails {
        previousAppointmentTime
        rescheduledAtTimestamp
        reason
        __typename
      }
      __typename
    }
    intake @include(if: $shouldFetchIntake) {
      intakePatientId
      intakeCompletionStatus
      forms {
        formId
        formType
        isRequired
        __typename
      }
      cards {
        cardId
        cardType
        hasImage
        carrierName
        memberId
        __typename
      }
      __typename
    }
    __typename
  }
}"""

    variables = {
        "practiceId": PRACTICE_ID,
        "appointmentId": appointment_id,
        "appointmentSource": appointment_source,
        "shouldFetchIntake": True,
    }

    return gql(session, "getPhiAppointmentDetails", variables, query)


def send_call_request(session: req.Session, request_id: str,
                      reasons: list = None) -> dict:
    """
    Send "Request patient to call the office" message.

    NOTE: The REST endpoint at www.zocdoc.com is protected by DataDome which
    blocks Python requests. Use send_call_request_browser() instead, which
    makes the call via Playwright's page.evaluate() to bypass DataDome.

    Args:
        request_id: The numeric requestId from getPhiAppointmentDetails
                    (NOT the appointmentId UUID)
        reasons: List of reason strings. Options: "Insurance", "VisitReason", "Other"
    """
    if reasons is None:
        reasons = ["Other"]

    r = session.post(
        f"{REST_BASE}/provider/api/appointments/RequestPatientCall",
        json={
            "apptId": str(request_id),
            "requestedInformation": reasons,
        },
    )
    r.raise_for_status()
    return {"status": r.status_code, "body": r.text}


def send_call_request_browser(request_id: str, reasons: list = None) -> dict:
    """
    Send "Request patient to call the office" via browser fetch.

    Uses Playwright to make the REST call from within the browser context,
    which bypasses DataDome bot protection on www.zocdoc.com.

    Args:
        request_id: The numeric requestId from getPhiAppointmentDetails
        reasons: List of reason strings. Options: "Insurance", "VisitReason", "Other"
    """
    from playwright.sync_api import sync_playwright

    if reasons is None:
        reasons = ["Other"]

    profile = os.path.expanduser("~/.zocdoc-discovery-profile")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # Navigate to zocdoc (need same-origin context for fetch)
        page.goto(
            f"https://www.zocdoc.com/provider/inbox/{PRACTICE_ID}",
            wait_until="networkidle",
        )
        time.sleep(2)

        # Handle login if needed
        if "signin" in page.url:
            email_input = page.wait_for_selector(
                'input[type="email"], input[name="email"]', timeout=10000
            )
            email_input.fill("barric@galatiq.ai")
            time.sleep(0.5)
            page.click('button[type="submit"]')
            time.sleep(3)
            pass_input = page.wait_for_selector(
                'input[type="password"]:visible', timeout=10000
            )
            pass_input.fill("Mynewpass1@")
            time.sleep(0.5)
            page.click('button[type="submit"]')
            page.wait_for_url("**/practice/**", timeout=30000)
            page.goto(
                f"https://www.zocdoc.com/provider/inbox/{PRACTICE_ID}",
                wait_until="networkidle",
            )
            time.sleep(2)

        # Make the REST call via browser's fetch
        result = page.evaluate("""
            async (args) => {
                const [requestId, reasons] = args;
                try {
                    const response = await fetch(
                        '/provider/api/appointments/RequestPatientCall',
                        {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json',
                            },
                            body: JSON.stringify({
                                apptId: requestId,
                                requestedInformation: reasons
                            }),
                            credentials: 'include'
                        }
                    );
                    const text = await response.text();
                    return { status: response.status, body: text };
                } catch (e) {
                    return { error: e.message };
                }
            }
        """, [str(request_id), reasons])

        # Save fresh cookies while we have the browser open
        cookies = ctx.cookies()
        zocdoc_cookies = [c for c in cookies if "zocdoc" in c.get("domain", "")]
        with open("zocdoc_cookies.json", "w") as f:
            json.dump(zocdoc_cookies, f, indent=2)

        ctx.close()

    if result.get("error"):
        raise Exception(f"Browser fetch failed: {result['error']}")
    if result["status"] != 200:
        raise Exception(f"RequestPatientCall returned {result['status']}: {result['body']}")

    return result


def get_status_counts(session: req.Session) -> dict:
    """Get appointment status counts (inbox tab badges)."""
    now = datetime.now(timezone(timedelta(hours=-4)))
    from_time = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    from_time = from_time[:-2] + ":" + from_time[-2:]

    query = """query getAppointmentStatusAggregates($practiceId: String!, $fromAppointmentTime: OffsetDateTime!, $appointmentStatuses: [AppointmentStatus!]!, $productType: ProviderProduct, $appointmentSources: [AppointmentSource!]) {
  appointmentStatusAggregates(request: {practiceId: $practiceId, fromAppointmentTime: $fromAppointmentTime, appointmentStatuses: $appointmentStatuses, productType: $productType, appointmentSources: $appointmentSources}) {
    status
    count
    __typename
  }
}"""

    variables = {
        "practiceId": PRACTICE_ID,
        "fromAppointmentTime": from_time,
        "appointmentStatuses": COUNT_STATUSES,
        "productType": "INBOX",
        "appointmentSources": APPOINTMENT_SOURCES + ["APPOINTMENT_LIST"],
    }

    return gql(session, "getAppointmentStatusAggregates", variables, query)


# --- Test Runner ---
def run_tests(session: req.Session, do_send: bool = False):
    print("\n" + "=" * 60)
    print("ZOCDOC PROVIDER API TEST SUITE")
    print("=" * 60)

    # Test 1: Status counts
    print("\n[TEST 1] Get appointment status counts")
    try:
        data = get_status_counts(session)
        counts = data.get("data", {}).get("appointmentStatusAggregates", [])
        print(f"  OK — {len(counts)} statuses:")
        for c in counts:
            print(f"    {c['status']}: {c['count']}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 2: List bookings
    print("\n[TEST 2] List inbox bookings (page 1)")
    first_appointment_id = None
    first_appointment_source = None
    try:
        data = list_bookings(session, page_size=5)
        appts_data = data.get("data", {}).get("appointments", {})
        appts = appts_data.get("appointments", [])
        total = appts_data.get("totalAppointmentsCount", "?")
        pages = appts_data.get("pagesCount", "?")
        print(f"  OK — {len(appts)} appointments (total: {total}, pages: {pages})")
        for a in appts[:5]:
            patient = a.get("patient", {})
            name = f"{patient.get('firstName', '?')} {patient.get('lastName', '?')}"
            status = a.get("appointmentStatus", "?")
            appt_time = a.get("appointmentTimeUtc", "?")
            procedure = a.get("procedure", {}).get("name", "?")
            print(f"    - {name} | {procedure} | {appt_time} | {status}")
            if not first_appointment_id:
                first_appointment_id = a.get("appointmentId")
                first_appointment_source = a.get("appointmentSource", "MARKETPLACE")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 3: Get booking detail
    print("\n[TEST 3] Get appointment detail")
    request_id = None
    if first_appointment_id:
        try:
            data = get_booking(session, first_appointment_id, first_appointment_source)
            details = data.get("data", {}).get("appointmentDetails", {})
            patient = details.get("patient", {})
            request_id = details.get("requestId")
            print(f"  OK — Appointment detail:")
            print(f"    Patient: {patient.get('firstName')} {patient.get('lastName')}")
            print(f"    Email: {patient.get('email')}")
            print(f"    Phone: {patient.get('phoneNumber')}")
            print(f"    DOB: {patient.get('dob')}")
            print(f"    Visit: {details.get('procedure', {}).get('name')}")
            print(f"    Provider: {details.get('provider', {}).get('fullName')}")
            print(f"    Location: {details.get('location', {}).get('address1')}, {details.get('location', {}).get('city')}")
            print(f"    RequestId: {request_id} (used for messaging)")
            print(f"    Call requested: {patient.get('requestedToCallTimestamp', 'never')}")
            ins = details.get("insurance", {})
            if ins:
                carrier = ins.get("carrier", {}).get("name", "?")
                plan = ins.get("plan", {}).get("name", "?")
                print(f"    Insurance: {carrier} / {plan}")
        except Exception as e:
            print(f"  FAILED: {e}")
    else:
        print("  SKIP — no appointment from Test 2")

    # Test 4: Search by patient name
    print("\n[TEST 4] Search by patient name")
    try:
        data = list_bookings(session, patient_name="Torres", page_size=5)
        appts = data.get("data", {}).get("appointments", {}).get("appointments", [])
        print(f"  OK — {len(appts)} results for 'Torres':")
        for a in appts[:3]:
            patient = a.get("patient", {})
            print(f"    - {patient.get('firstName')} {patient.get('lastName')}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 5: Send call request (via browser to bypass DataDome)
    print("\n[TEST 5] Send 'call the office' request (via browser)")
    if not do_send:
        print("  SKIP — pass --send flag to actually send a call request")
    elif not request_id:
        print("  SKIP — no requestId available")
    else:
        try:
            result = send_call_request_browser(request_id, reasons=["Other"])
            print(f"  OK — status={result['status']}, body='{result['body']}'")
        except Exception as e:
            print(f"  FAILED: {e}")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Test Zocdoc Provider API client")
    parser.add_argument("--cookies", help="Path to cookies JSON file (skip browser login)")
    parser.add_argument("--send", action="store_true",
                        help="Actually send a 'call the office' request to first patient")
    args = parser.parse_args()

    if args.cookies:
        print(f"Loading cookies from {args.cookies}...")
        with open(args.cookies) as f:
            cookies = json.load(f)
        print(f"Loaded {len(cookies)} cookies")
    else:
        cookies = login_browser()

    session = get_session(cookies)
    run_tests(session, do_send=args.send)


if __name__ == "__main__":
    main()
