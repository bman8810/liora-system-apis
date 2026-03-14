# Zocdoc Provider API Reference

> Reverse-engineered from `zocdoc.com/provider` (Zocdoc Provider Dashboard)
> Generated: 2026-03-13

## Architecture

- **GraphQL** backend at `api2.zocdoc.com/provider/v1/gql` — main provider API
- **REST** endpoints at `www.zocdoc.com/provider/api/...` — legacy actions (messaging)
- **REST** microservices at `api2.zocdoc.com/intake/...`, `api2.zocdoc.com/insurance-eligibility/...`
- Cookie-based auth (`.ASPXAUTH`, `ZDAUTH`, `JWTv2`)
- DataDome bot protection — blocks Python `requests` on `www.zocdoc.com` REST endpoints; `api2.zocdoc.com` GraphQL works with `x-datadome-clientid` header
- **REST calls must go through browser** (Playwright `page.evaluate(fetch(...))`) to bypass DataDome on `www.zocdoc.com`

## Authentication

### Login Flow (Two-Step)

```
1. POST www.zocdoc.com/auth/v1/practice/login/start
   Body: {"email": "user@example.com"}
   Response: {"login_type": "password", "login_url": null}

2. POST www.zocdoc.com/accounts/v2/authentication/authenticate-password
   Body: {"username": "user@example.com", "password": "..."}
   Response: 200 (sets auth cookies)

3. Redirect to /practice/{practiceId}/dashboard
```

### Auth Cookies

All API calls require these cookies (set automatically after login):

| Cookie | Domain | Purpose |
|--------|--------|---------|
| `.ASPXAUTH` | www.zocdoc.com | ASP.NET auth (httpOnly, secure) |
| `ZDAUTH` | .zocdoc.com | Zocdoc auth (httpOnly, secure) |
| `JWTv2` | .zocdoc.com | JWT RS256 token (httpOnly, secure) |
| `xsrf` | .zocdoc.com | XSRF token |
| `CurrentPracticeId` | .www.zocdoc.com | Current practice context |

### Session Refresh

```
POST /auth/user/v1/refresh
Response: {"expiry_in_seconds": 1800}
```

### Key IDs (Liora Dermatology)

| ID | Value |
|----|-------|
| Practice ID | `pt_FMyrNSVN50CbgjEI0NcL9h` |
| Provider ID | `pr_eTTyn6m-e0y7oL1yjr9JQB` |
| Location ID | `lo_U3iTFPUvAEWDYsbb3HxqBB` |
| Entity ID | `practicestaff~ZXV58yYGUUyjc6Zps52FrQ` |
| Provider NPI | `1265738884` |

---

## GraphQL Endpoint

```
POST https://api2.zocdoc.com/provider/v1/gql
Content-Type: application/json
```

All GraphQL operations use this single endpoint. Request body:
```json
{
  "operationName": "operationName",
  "variables": { ... },
  "query": "query operationName(...) { ... }"
}
```

---

## Inbox/Appointments

### List Inbox Bookings — `getInboxRows`

Lists all bookings with patient info, intake status, and insurance.

**Variables:**
```json
{
  "practiceId": "pt_FMyrNSVN50CbgjEI0NcL9h",
  "pageNumber": 1,
  "pageSize": 20,
  "fromAppointmentTime": "2026-03-13T06:00:00-04:00",
  "tableAppointmentStatuses": [
    "UNCONFIRMED", "PATIENT_RESCHEDULED", "PATIENT_CANCELLED",
    "CONFIRMED", "PROVIDER_RESCHEDULED",
    "SYNC_CONFIRMED", "SYNC_PATIENT_RESCHEDULED"
  ],
  "statusCountStatuses": [
    "UNCONFIRMED", "PATIENT_RESCHEDULED", "PATIENT_CANCELLED",
    "SYNC_CONFIRMED", "SYNC_PATIENT_RESCHEDULED"
  ],
  "intakeSubmissionsFilterStatuses": [
    "UNCONFIRMED", "PATIENT_RESCHEDULED", "PATIENT_CANCELLED",
    "CONFIRMED", "PROVIDER_RESCHEDULED",
    "SYNC_CONFIRMED", "SYNC_PATIENT_RESCHEDULED"
  ],
  "productType": "INBOX",
  "sortType": "DESCENDING",
  "sortByField": "LAST_UPDATED_BY_PATIENT",
  "providerIdsFilter": [],
  "locationIdsFilter": [],
  "appointmentSources": ["MARKETPLACE", "API", "MANUAL_INTAKE"],
  "patientName": "",
  "shouldFetchIntake": true,
  "intakeReviewStates": null
}
```

**Response:**
```json
{
  "data": {
    "statusCounts": [
      {"status": "UNCONFIRMED", "count": 0},
      {"status": "PATIENT_CANCELLED", "count": 4},
      {"status": "SYNC_CONFIRMED", "count": 10},
      {"status": "SYNC_PATIENT_RESCHEDULED", "count": 2}
    ],
    "practice": {"timeZone": "America/Indiana/Indianapolis"},
    "appointments": {
      "pagesCount": 1,
      "totalAppointmentsCount": 20,
      "appointments": [
        {
          "appointmentId": "app_b85ac662-aa4e-46af-b21c-e45f5add8467",
          "appointmentSource": "MARKETPLACE",
          "appointmentStatus": "SYNC_CONFIRMED",
          "appointmentTimeUtc": "2026-03-13T13:40:00",
          "bookingTimeUtc": "2026-03-12T21:32:37Z",
          "lastUpdatedUtc": "2026-03-12T21:32:39Z",
          "lastUpdatedByPatientUtc": "2026-03-12T21:32:39Z",
          "patientType": "NEW",
          "isInNetwork": true,
          "intake": {
            "hasIdCard": false,
            "hasInNetworkInsurance": true,
            "isIdCardRequired": true,
            "isInsuranceRequired": true,
            "isInsuranceSelfPay": false,
            "isInsuranceSubmitted": true,
            "numberOfCompletedForms": 0,
            "numberOfRequiredForms": 0,
            "patientConfirmationStatus": "PENDING"
          },
          "intakeReviewState": "HAS_UNSEEN",
          "intakeCompletionStatus": "IN_PROGRESS",
          "patient": {
            "firstName": "Jessica",
            "lastName": "Torres"
          },
          "procedure": {
            "name": "Paronychia (Nail Infection)"
          },
          "provider": {
            "fullName": "Dr. Libby Rhee, DO",
            "imageUrl": "//d1k13df5m14swc.cloudfront.net/...",
            "isResource": false
          },
          "insurance": {
            "carrier": {"id": "ic_440", "name": "1199SEIU"},
            "plan": {"id": "ip_4005", "name": "National Benefit Fund"}
          }
        }
      ]
    }
  }
}
```

**Filtering by status:** Set `tableAppointmentStatuses` to only the statuses you want:
- New bookings: `["SYNC_CONFIRMED"]`
- Reschedules: `["SYNC_PATIENT_RESCHEDULED", "PATIENT_RESCHEDULED"]`
- Cancellations: `["PATIENT_CANCELLED"]`

**Patient name search:** Set `patientName` to search string.

---

### Get Appointment Detail — `getPhiAppointmentDetails`

Returns full appointment details including patient PII.

**Variables:**
```json
{
  "practiceId": "pt_FMyrNSVN50CbgjEI0NcL9h",
  "appointmentId": "app_b85ac662-aa4e-46af-b21c-e45f5add8467",
  "appointmentSource": "MARKETPLACE",
  "shouldFetchIntake": true
}
```

**Response (key fields):**
```json
{
  "data": {
    "appointmentDetails": {
      "appointmentIdentifier": {
        "appointmentId": "app_b85ac662-aa4e-46af-b21c-e45f5add8467",
        "appointmentSource": "MARKETPLACE"
      },
      "appointmentStatus": "SYNC_CONFIRMED",
      "practiceId": "pt_FMyrNSVN50CbgjEI0NcL9h",
      "requestId": 82532113,
      "appointmentTimeUtc": "2026-03-13T13:40:00",
      "bookingTimeUtc": "2026-03-12T21:32:37Z",
      "patientType": "NEW",
      "bookedBy": "PATIENT",
      "duration": 10,
      "procedure": {
        "id": "pc_sMufQL3wK0C97gVOyuPJjR",
        "monolithId": 5797,
        "name": "Paronychia (Nail Infection)"
      },
      "practice": {
        "id": "pt_FMyrNSVN50CbgjEI0NcL9h",
        "name": "Liora Dermatology & Aesthetics",
        "timeZone": "America/Indiana/Indianapolis"
      },
      "provider": {
        "id": "pr_eTTyn6m-e0y7oL1yjr9JQB",
        "fullName": "Dr. Libby Rhee, DO",
        "npi": "1265738884"
      },
      "location": {
        "id": "lo_U3iTFPUvAEWDYsbb3HxqBB",
        "address1": "110 E 60th St",
        "address2": "Ste 800",
        "city": "New York",
        "state": "NY",
        "zipCode": "10022",
        "phoneNumber": "2124334569"
      },
      "patient": {
        "id": "12486249",
        "firstName": "Jessica",
        "lastName": "Torres",
        "email": "hipolitaj1983@gmail.com",
        "dob": "1983-12-12",
        "sex": "FEMALE",
        "phoneNumber": "+16464982120",
        "note": "My fingernails and toenails...",
        "cloudId": "pa_eWFLhDclukiz_mj_oXiC_w",
        "requestedToCallTimestamp": null,
        "address": {
          "address1": "430 east 105th street",
          "address2": "6d",
          "city": "New york",
          "state": "NY",
          "zipCode": "10029"
        }
      },
      "insurance": {
        "card": {
          "memberId": "9008578432 01",
          "frontImageUrl": "/images/insurance/9abb6cef-.../38095683/",
          "backImageUrl": "/images/insurance/c29aec50-.../38095684/"
        },
        "carrier": {"id": "ic_440", "name": "1199SEIU"},
        "plan": {"id": "ip_4005", "name": "National Benefit Fund", "networkType": "PPO", "programType": "Commercial"}
      },
      "intake": {
        "intakePatientId": "intake_patient:YDk49xzYakuJRuSa8lzsNg",
        "intakeCompletionStatus": "IN_PROGRESS",
        "forms": [],
        "cards": [
          {
            "cardType": "ID",
            "hasImage": false,
            "isRequired": true
          },
          {
            "cardType": "MEDICAL_INSURANCE",
            "hasImage": true,
            "carrierId": "440",
            "carrierName": "1199SEIU",
            "memberId": "9008578432 01"
          }
        ]
      }
    }
  }
}
```

**Important:** The `requestId` (numeric, e.g., `82532113`) is used by the REST messaging API, NOT the `appointmentId` (UUID format `app_xxx`).

---

### Get Status Counts — `getAppointmentStatusAggregates`

Returns counts for each appointment status (used for inbox tab badges).

**Variables:**
```json
{
  "practiceId": "pt_FMyrNSVN50CbgjEI0NcL9h",
  "fromAppointmentTime": "2026-03-13T06:00:00-04:00",
  "appointmentStatuses": [
    "UNCONFIRMED", "PATIENT_RESCHEDULED", "PATIENT_CANCELLED",
    "SYNC_CONFIRMED", "SYNC_PATIENT_RESCHEDULED"
  ],
  "productType": "INBOX",
  "appointmentSources": ["MARKETPLACE", "API", "MANUAL_INTAKE", "APPOINTMENT_LIST"]
}
```

**Response:**
```json
{
  "data": {
    "appointmentStatusAggregates": [
      {"status": "UNCONFIRMED", "count": 0},
      {"status": "PATIENT_RESCHEDULED", "count": 0},
      {"status": "PATIENT_CANCELLED", "count": 4},
      {"status": "SYNC_CONFIRMED", "count": 10},
      {"status": "SYNC_PATIENT_RESCHEDULED", "count": 2}
    ]
  }
}
```

---

### Mark Appointment as Read — `markInboxAppointmentStatus` (mutation)

```json
{
  "operationName": "markInboxAppointmentStatus",
  "variables": {
    "practiceId": "pt_FMyrNSVN50CbgjEI0NcL9h",
    "appointments": [
      {"appointmentId": "app_b85ac662-...", "appointmentSource": "MARKETPLACE"}
    ],
    "productStatus": "READ_BY_PRACTICE"
  }
}
```

---

## Messaging

### Request Patient to Call Office

```
POST https://www.zocdoc.com/provider/api/appointments/RequestPatientCall
Content-Type: application/json
```

**Request Body:**
```json
{
  "apptId": "82532113",
  "requestedInformation": ["Other"]
}
```

**Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `apptId` | String (numeric) | The `requestId` from `getPhiAppointmentDetails` — NOT the `appointmentId` |
| `requestedInformation` | String[] | Reasons for the call. Values: `"Insurance"`, `"VisitReason"`, `"Other"` |

**Response:** `200 OK` (empty body)

**Notes:**
- After sending, the patient's `requestedToCallTimestamp` is set in appointment details
- Patient receives a text message asking them to call the office
- The `apptId` maps to the `requestId` field in the GraphQL response (numeric), not the `appointmentId` (UUID)
- Multiple reasons can be combined: `["Insurance", "Other"]`

---

## Other Endpoints

### Current User

```
GET https://www.zocdoc.com/api/2/user
```

**Response:**
```json
{
  "display_name": "Barric",
  "email": "barric@galatiq.ai",
  "last_name": "Reed",
  "login_state": 2,
  "has_password": true,
  "home_page_url": "/provider/dash/performance"
}
```

### Insurance Eligibility Report

```
GET https://api2.zocdoc.com/insurance-eligibility/v2/report/{practiceId}/{appointmentId}
```

**Response:**
```json
{
  "eligibility_check_progress": "Complete",
  "is_active_on_request_date": true,
  "is_carrier_supported": true,
  "patient_insurance_eligibility_details": {
    "carrier_name": "AETNA INC",
    "group_name": "IPSOS AMERICA, INC.",
    "group_number": "086990101100011",
    "plan_name": "HSA Aetna Choice POS II",
    "plan_status": "Active"
  }
}
```

### Insurance Card Images

```
GET https://www.zocdoc.com/images/insurance/{imageId}/{version}
```

### Intake Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/intake/v5/practice/{practiceId}/practice-config` | Practice intake configuration |
| GET | `/intake/v2/practice/{practiceId}/patient-card/{cardId}` | Patient intake card |
| POST | `/intake/v2/update-last-seen` | Mark intake items as seen |
| POST | `/intake/v4/mark-intake-tasks-as-seen-by-practice` | Mark intake tasks as seen |

### Provider Bookability

```
POST https://api.zocdoc.com/provider/bookability/v1/status:batchGet
Body: {"provider_ids": ["pr_eTTyn6m-e0y7oL1yjr9JQB"]}
```

### Dashboard GraphQL Operations

| Operation | Description |
|-----------|-------------|
| `GetApiBookingsTabData` | Booking statistics comparison (current vs previous period) |
| `getOrgTopMetricsData` | Top-level metrics (total bookings, patients seen, costs) |
| `getOrgTopVisitReasons` | Top visit reasons breakdown |
| `getOrgTopInsurances` | Top insurance carriers breakdown |
| `GetOrgBookingSourceData` | Booking channel breakdown over time |
| `getOrgBookingsBreakdown` | Booking type breakdown (new patient, video, high-value) |
| `getCurrentMonthCost` | Current month spend |
| `getSpendManagement` | Spend cap and lock status |
| `getCostPlanFee` | Cost plan fees per provider/location |
| `getPracticeRecommendations` | Practice improvement recommendations |
| `getSponsoredResults` | Sponsored results performance |
| `getPracticesFirstBookingTime` | First booking time for practices |
| `getPracticeAlertStatuses` | Practice alert/notification statuses |
| `reviewsCardQuery` | Provider reviews summary |
| `userMultiPracticesQuery` | List practices the user has access to |

---

## URL Structure

| Page | URL |
|------|-----|
| Login | `/signin?provider=1` |
| Dashboard | `/practice/{practiceId}/dashboard` |
| Performance | `/provider/dash/performance` |
| Inbox | `/provider/inbox/{practiceId}` |
| Settings | `/provider/config/settings/profile?providerId={providerId}` |

---

## Enum Values

### AppointmentStatus
- `UNCONFIRMED`
- `CONFIRMED`
- `SYNC_CONFIRMED`
- `PATIENT_RESCHEDULED`
- `SYNC_PATIENT_RESCHEDULED`
- `PROVIDER_RESCHEDULED`
- `PATIENT_CANCELLED`

### AppointmentSource
- `MARKETPLACE`
- `API`
- `MANUAL_INTAKE`
- `APPOINTMENT_LIST`

### AppointmentSortByField
- `LAST_UPDATED_BY_PATIENT`

### PatientType
- `NEW`

### IntakeCompletionStatus
- `IN_PROGRESS`

### IntakeReviewState
- `HAS_UNSEEN`

---

## Programmatic Auth with Playwright

> Cookies are httpOnly — must extract via browser context, not JavaScript.

```python
from playwright.sync_api import sync_playwright
import os

def get_zocdoc_cookies():
    """Login to Zocdoc and return auth cookies."""
    profile = os.path.expanduser("~/.zocdoc-profile")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0]
        page.goto("https://www.zocdoc.com/signin?provider=1", wait_until="networkidle")

        # Enter email
        page.fill('input[type="email"]', "barric@galatiq.ai")
        page.click('button[type="submit"]')
        page.wait_for_selector('input[type="password"]:visible')

        # Enter password
        page.fill('input[type="password"]', "Mynewpass1@")
        page.click('button[type="submit"]')
        page.wait_for_url("**/practice/**", timeout=30000)

        cookies = ctx.cookies()
        ctx.close()
        return {c["name"]: c["value"] for c in cookies if "zocdoc" in c.get("domain", "")}
```
