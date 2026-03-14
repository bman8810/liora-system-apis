# ModMed EMA API Reference

> Reverse-engineered from `lioraderm.ema.md` (EMA v7.13.1.5, revision 9587b402ed)
> Generated: 2026-03-12

## Base URL

```
https://lioraderm.ema.md/ema/ws/
```

Two API versions are in use: `v2` (older, supports writes) and `v3` (newer, mostly read-only).

## Authentication

- **Session-based**: httpOnly session cookie (not visible to JS)
- **Load balancer cookies**: `AWSALBAPP-0` through `AWSALBAPP-3` (AWS ALB sticky session)
- **No CSRF tokens** observed in requests
- All API calls require an active EMA session (login via browser sets the cookie)
- **FHIR API** exists at `/ema/fhir/r4/` but returns `403` — requires separate OAuth registration via [ModMed API Portal](https://portal.api.modmed.com/reference/getting-started-3)

### Login Flow (Keycloak OIDC + PKCE)

The login is a **multi-step OIDC Authorization Code flow with PKCE**:

```
1. GET https://lioraderm.ema.md/ema/Login.action
   → Role selection page: "Continue as Practice Staff" or "Continue as Patient"

2. Click "Continue as Practice Staff"
   → 302 redirect to Keycloak SSO:
   GET https://sso.ema.md/auth/realms/Modmed/protocol/openid-connect/auth
   Params: response_type=code, client_id=ema, scope=openid profile email,
           state, nonce, redirect_uri, code_challenge (S256), login_hint (base64 firm metadata)

3. POST https://sso.ema.md/auth/realms/Modmed/login-actions/authenticate
   Form fields: firm (hidden, "lioraderm.ema.md"), username, password
   → 302 redirect to redirect_uri with ?code=<auth_code>&state=<state>

4. GET https://lioraderm.ema.md/ema/login/oauth2/callback/modmed?code=...&state=...
   → Server exchanges code for tokens, sets JSESSIONID cookie
   → 302 redirect to dashboard
```

### Session Cookies (17 total after login)
| Cookie | Domain | Purpose |
|--------|--------|---------|
| `JSESSIONID` | lioraderm.ema.md | **Primary session cookie** (httpOnly) |
| `KEYCLOAK_SESSION` | sso.ema.md | SSO session |
| `AUTH_SESSION_ID` | sso.ema.md | Keycloak auth session |
| `AWSALBAPP-0..3` | lioraderm.ema.md | AWS ALB sticky session |
| `AWSALB`, `AWSALBCORS` | sso.ema.md | ALB cookies for SSO |
| `__cf_bm` | ema.md | Cloudflare bot management |
| `CSID`, `gdpr`, `X-Qlik-Session-EMA` | lioraderm.ema.md | App state |

### Programmatic Auth with Playwright (Verified Working)

> **CRITICAL**: You must use `keyboard.type()` — Keycloak rejects `page.fill()` as bot input.

```python
from playwright.sync_api import sync_playwright
import requests

def get_ema_session(username, password):
    """Login to EMA and return a requests.Session with all cookies."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # Keycloak may reject headless
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # Step 1: Navigate to role selection page
        page.goto("https://lioraderm.ema.md/ema/Login.action", wait_until="networkidle")

        # Step 2: Click "Continue as Practice Staff"
        page.wait_for_selector("text=Continue as Practice Staff", timeout=15000)
        page.click("text=Continue as Practice Staff")

        # Step 3: Fill credentials using keyboard.type() (NOT page.fill!)
        page.wait_for_selector("#username", timeout=15000)
        page.click("#username")
        page.keyboard.type(username, delay=80)
        page.click("#password")
        page.keyboard.type(password, delay=80)
        page.keyboard.press("Enter")

        # Step 4: Wait for redirect to EMA
        page.wait_for_url("**/practice/staff/**", timeout=30000)

        # Step 5: Extract all cookies (including httpOnly)
        cookies = context.cookies()
        browser.close()

        # Step 6: Build a requests session
        session = requests.Session()
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
        return session

# Usage
session = get_ema_session("YOUR_USERNAME", "YOUR_PASSWORD")
patients = session.get(
    "https://lioraderm.ema.md/ema/ws/v3/patients",
    params={
        "selector": "lastName,firstName,mrn,dateOfBirth,phoneNumbers,email",
        "where": 'fn=patientStatus="\\"ACTIVE\\""',
        "paging.pageSize": "25",
        "sorting.sortBy": "lastName",
        "sorting.sortOrder": "ASC",
    },
).json()
```

### Session Refresh Strategy (Verified Working)

Three tiers of session management, from cheapest to most expensive:

| Scenario | Strategy | Credentials needed? | Cost |
|----------|----------|-------------------|------|
| EMA session alive | Reuse saved `JSESSIONID` cookie via `requests` | No | ~0ms, no browser |
| EMA session expired, Keycloak alive | Inject SSO cookies into Playwright → navigate to Login.action → click "Continue as Practice Staff" → auto-redirects, no login form | No | ~3s, browser launch |
| Both sessions expired | Full Playwright login with `keyboard.type()` | Yes | ~8s, browser + typing |

**Cookie persistence file:** Save all 17 cookies to `ema_cookies.json` after login. Load on next run.

**SSO cookies for refresh (inject these to skip login):**
- `AUTH_SESSION_ID` / `AUTH_SESSION_ID_LEGACY` (domain: `sso.ema.md`)
- `KEYCLOAK_IDENTITY` / `KEYCLOAK_IDENTITY_LEGACY` (domain: `sso.ema.md`)
- `KEYCLOAK_SESSION` / `KEYCLOAK_SESSION_LEGACY` (domain: `sso.ema.md`)
- `AWSALB` / `AWSALBCORS` (domain: `sso.ema.md`)

**Detection logic for an agent:**
```python
def ensure_session(session):
    """Check if session is alive, refresh if needed."""
    r = session.get(f"{BASE}/ema/ws/v3/facilities",
                    params={"paging.pageSize": "1"},
                    allow_redirects=False)
    if r.status_code == 200:
        return session  # still good
    # Try SSO refresh (no credentials)
    cookies = load_cookies()
    new_cookies = refresh_via_keycloak(cookies)
    if new_cookies:
        save_cookies(new_cookies)
        return make_session(new_cookies)
    # Full re-login needed
    new_cookies = login_fresh()
    save_cookies(new_cookies)
    return make_session(new_cookies)
```

**Session timeouts (observed):**
- EMA `JSESSIONID`: ~30-60 min idle timeout
- Keycloak SSO session: longer-lived (hours), survives EMA session expiry
- The PKCE flow means you **cannot** replay the login via raw HTTP requests alone — must use Playwright

## Query Language

EMA uses a custom query language across its `v3` endpoints:

### `selector` Parameter
Controls which fields are returned (like GraphQL field selection):
```
selector=lastName,firstName,mrn,dateOfBirth
```
Nested objects use parentheses:
```
selector=patient(id,lastName,firstName,mrn),provider(id,name),facility(id,name)
```

### `where` Parameter
Filter expressions with these operators:
| Operator | Example | Notes |
|----------|---------|-------|
| `==` | `lastName=="Smith"` | Exact match |
| `>=` / `<=` | `scheduledStartDateLd>="2026-03-12"` | Date range |
| `=in=()` | `status=in=("ARRIVED","PENDING")` | Set membership |
| `=null=` | `facility=null="true"` | Null check |
| `fn=` | `fn=patientStatus="\"ACTIVE\""` | Function-style filter |
| `and` / `or` | `(facility=in=("2040") or facility=null="true") and provider=in=("123")` | Logical |
| `;` | `lastName=="Smith";firstName=="John"` | AND (semicolon shorthand) |

### Paging & Sorting
```
paging.pageSize=25
paging.pageNumber=1
sorting.sortBy=lastName
sorting.sortOrder=ASC
```

---

## Patient APIs

### List / Search Patients

```
GET /ema/ws/v3/patients
```

| Parameter | Example |
|-----------|---------|
| `selector` | `lastName,firstName,mrn,pmsId,dateOfBirth,nickName,preferredPhoneNumber,phoneNumbers,email,establishedPatient,dateLastVisit,encryptedId,statementNumber` |
| `where` | `lastName=="Smith";fn=patientStatus="\"ACTIVE\""` |
| `paging.pageSize` | `25` |
| `paging.pageNumber` | `1` |
| `sorting.sortBy` | `lastName` |
| `sorting.sortOrder` | `ASC` |
| `showInactive` | `false` |

**Search examples:**
```
# By last name
where=lastName=="Smith";fn=patientStatus="\"ACTIVE\""

# By first name
where=firstName=="John";fn=patientStatus="\"ACTIVE\""

# Combined
where=lastName=="Smith";firstName=="John";fn=patientStatus="\"ACTIVE\""

# By ID
where=id==5778462
```

**Response:** JSON array of patient objects
```json
[
  {
    "id": 5778462,
    "lastName": "Smith",
    "firstName": "John",
    "fullName": "Smith, John",
    "name": "Smith, John",
    "mrn": "MM0000002044",
    "pmsId": "113983PAT000002050",
    "pmsIdType": "string",
    "dateOfBirth": "1997-09-06T00:00:00.000+0000",
    "displayDateOfBirth": "09/06/1997",
    "gender": "Male",
    "sex": "MALE",
    "email": "patient@example.com",
    "username": "string",
    "encryptedId": "string",
    "establishedPatient": true,
    "testUser": false,
    "phoneNumbers": [
      {
        "id": 46960285,
        "phoneNumber": "7039891824",
        "formattedPhoneNumber": "(703) 989-1824",
        "formattedPhoneNumberWithParens": "(703) 989-1824",
        "formattedPhoneNumberWithExtension": "(703) 989-1824",
        "phoneNumberType": "MOBILE"
      }
    ]
  }
]
```

### Get Single Patient

```
GET /ema/ws/v3/patients/{id}
```

Supports `selector` parameter. Without selector, returns a minimal set of fields.

### Patient Status Enum
`ACTIVE` | `INACTIVE` | `INACTIVE_DUPLICATE` | `DISCHARGED` | `DECEASED`

---

## Appointment APIs

### List Appointments (v3 - read-only)

```
GET /ema/ws/v3/appointments
```
Methods allowed: `HEAD`, `GET`, `OPTIONS`

| Parameter | Example |
|-----------|---------|
| `selector` | `id,scheduledStartDate,scheduledEndDate,scheduledDuration,appointmentTypeName,status,patient(id,lastName,firstName,mrn,dateOfBirth),provider(id,name),facility(id,name)` |
| `where` | `scheduledStartDateLd>="2026-03-12";scheduledStartDateLd<="2026-03-14"` |
| `paging.pageSize` | `50` |
| `sorting.sortBy` | `scheduledStartDate` |
| `sorting.sortOrder` | `ASC` |

**Response schema:**
```
id                    number
scheduledStartDate    string (ISO 8601)
scheduledEndDate      string (ISO 8601)
scheduledStartDateLd  string (date only: "2026-03-12")
scheduledEndDateLd    string
scheduledDuration     number (minutes)
displayStartDate      string
displayStartTime      string
displayEndDate        string
appointmentTypeName   string
status                string (enum)
statusValue           string
newPatient            boolean
notes                 string
reason                string
reasonForVisit        string
reportableReason      string
origin                string
paymentMethod         string
editable              boolean
objectLockVersion     number
pmsId                 string
pmsIdType             string
facilityTimeZone      string
appointmentDateCreated string
declinedReferringProvider boolean
patientPcpAbsent      boolean

appointmentType       object
  .id                 number
  .name               string
  .abbreviation       string
  .defaultDuration    number
  .status             string
  .predefined         boolean
  .includeGeneralAvailability boolean
  .doNotDisplayInPortal boolean
  .doNotRequireVisitCreation boolean

patient               object
  .id                 number
  .lastName           string
  .firstName          string
  .fullName           string
  .mrn                string
  .dateOfBirth        string
  .gender             string
  .sex                string
  .phoneNumbers       array
  .testUser           boolean

provider              object
  .id                 number
  .name               string
  .fullName           string
  .firstName          string
  .lastName           string
  .username           string
  .isRcmUser          boolean
  .testUser           boolean

facility              object
  .id                 number
  .facilityId         number
  .name               string
  .timeZone           string
  .visible            boolean
  .primaryFacility    boolean
```

### Scheduler Appointments (richer view)

```
GET /ema/ws/v3/scheduler/appointments
```

Same query structure but supports additional nested selectors:
```
selector=reservations(facilityResource),appointmentType,provider,facility,
  patient(dateArchived,phoneNumbers,preferredPhone,allActiveInsurancePolicies,
  hasActivePregnancy,activeInsurances),latestReminderActivity,
  previousAppointmentInfo(insurances(position,insurancePolicy)),
  treatmentCase(insurancePolicy),authorizationPrimary,authorizationPrimaryVision,
  mavAppointmentPinnedCalendarPreference(mavCalendarPreference)
```

The `where` clause uses date strings with timezone:
```
where=(facility=in=("2040") or facility=null="true")
  and provider=in=("8327689")
  and scheduledStartDate>="Mon Mar 09 2026 00:00:00 GMT-0400"
  and scheduledEndDate<="Sun Mar 15 2026 23:59:59 GMT-0400"
  and status=in=("ARRIVED","PENDING","CHECKED_IN","CHECKED_OUT","CONFIRMED")
```

### Create Appointment

```
POST /ema/ws/v2/appointment
```

Methods allowed: `HEAD`, `POST`, `GET`, `OPTIONS`

Request body: JSON object (exact required fields TBD — empty body returns 500).
Likely requires: `patient.id`, `provider.id`, `facility.id`, `appointmentType.id`, `scheduledStartDate`, `scheduledDuration`.

### Update Appointment

```
PUT /ema/ws/v2/appointment/{id}
```

Methods allowed: `HEAD`, `POST`, `GET`, `OPTIONS`, `PUT`

### Appointment Status Enum

| Status | Description |
|--------|-------------|
| `SCHEDULED` | Scheduled |
| `CHANGED` | Changed |
| `CANCELED` | Canceled |
| `RESCHEDULED` | Rescheduled |
| `NO_SHOW` | No Show |
| `PRESENT` | Present |
| `ARRIVED` | Arrived |
| `CHECKED_IN` | Checked In |
| `DISCONTINUED` | Discontinued |
| `COMPLETED` | Completed |
| `CHECKED_OUT` | Checked Out |

---

## Appointment Finder (Slot Search)

```
GET /ema/ws/v2/appointment/finder
```

Find available appointment slots.

| Parameter | Type | Values |
|-----------|------|--------|
| `apptTypeId` | number | Required. Appointment type ID |
| `duration` | number | Minutes (e.g., `15`) |
| `timeOfDay` | string | `ANYTIME`, `MORNING`, `AFTERNOON`, `EVENING`, `OVERNIGHT` |
| `timeFrame` | string | `FIRST_AVAILABLE`, `SPECIFIC_DATE`, `TIME_FRAME`, `DATE_RANGE` |
| `display` | string | `BY_PROVIDER`, `BY_TIME` |
| `specificDate` | string | ISO 8601 (e.g., `2026-03-13T00:00:00.000Z`) |
| `facilityId` | number | Optional. Filter by facility |
| `providerId` | number | Optional. Filter by provider |
| `alternateDurationsIncluded` | boolean | Include alternate durations |
| `canOverrideDefaults` | boolean | Allow overriding defaults |
| `afterFirstAppointment` | boolean | Search after first available |
| `timeBuffer` | number | Buffer time in minutes |

**Response:**
```json
[
  {
    "provider": {
      "id": 8327689,
      "firstName": "Jane",
      "lastName": "Doe",
      "addSupervisorToRx": false
    },
    "facility": {
      "id": 2040,
      "name": "Liora Dermatology & Aesthetics",
      "timeZone": "America/New_York"
    },
    "isMoreAvailable": true,
    "appointments": [
      {
        "scheduledStartDate": "2026-03-13T13:00:00.000+0000",
        "scheduledEndDate": "2026-03-13T13:15:00.000+0000",
        "scheduledDuration": 15,
        "overrideAllowed": false,
        "numRemainingOccurrences": 1,
        "totalOccurences": 1,
        "prefInstanceIds": {},
        "timeZoneId": "America/New_York"
      }
    ]
  }
]
```

---

## Calendar Events

```
GET /ema/ws/v3/scheduler/calendar-events
```

Time blocks, availability blocks, office closures.

| Parameter | Example |
|-----------|---------|
| `facilityIds` | `2040` |
| `providerIds` | `18490903` |
| `from` | `2026-03-09T04:00:00.000Z` |
| `to` | `2026-03-16T03:59:59.999Z` |
| `deleted` | `false` |
| `selector` | `providers,facility,createdBy,modifiedBy,instances,facilities(facility)` |

**Response schema:**
```
id                number
fromTimeInMinutes number
toTimeInMinutes   number
notes             string
visible           boolean
blocking          boolean
dateCreated       string
dateModified      string
createdBy         object (user)
instances         array
providers         array
facilities        array
```

---

## Reference Data

### Appointment Types

```
GET /ema/ws/v3/appointmentType?paging.pageSize=100
```

```
id                          number
name                        string    (e.g., "Follow-up", "Surgery", "New Patient")
abbreviation                string
defaultDuration             number    (minutes)
status                      string
predefined                  boolean
defaultAsNewPatient         boolean
doNotDisplayInPortal        boolean
doNotRequireVisitCreation   boolean
includeGeneralAvailability  boolean
childAppointmentTypeCount   number
parentAppointmentTypeCount  number
alternateDurationsCount     number
```

### Facilities

```
GET /ema/ws/v3/facilities?paging.pageSize=100
```

```
id               number
facilityId       number
name             string
timeZone         string    (e.g., "America/New_York")
visible          boolean
primaryFacility  boolean
```

### Enums

```
GET /ema/ws/enum?enumClassName={fully.qualified.class}
```

Known enum classes:
| Class | Values |
|-------|--------|
| `com.m2.domain.enums.AppointmentStatus` | SCHEDULED, CHANGED, CANCELED, RESCHEDULED, NO_SHOW, PRESENT, ARRIVED, CHECKED_IN, DISCONTINUED, COMPLETED, CHECKED_OUT |
| `com.m2.domain.enums.PatientStatus` | ACTIVE, INACTIVE, INACTIVE_DUPLICATE, DISCHARGED, DECEASED |
| `com.m2.domain.enums.Gender` | (Male, Female, etc.) |

---

## Scheduler UI Support

### Save/Apply Scheduler Filter

```
POST /ema/ws/v2/scheduler/filter
```

```json
{
  "providerIds": [8327689],
  "facilityIds": [2040],
  "resourceIds": [],
  "weekViewProviderIds": [18490903],
  "weekViewFacilityIds": [2040],
  "view": "week",
  "calendarIncrement": 5,
  "autoPopulateProviders": false
}
```

### Quick Filters

```
GET /ema/ws/v3/quick-filters
GET /ema/ws/v3/quick-filters/groups
```

### Appointment Holds

```
GET /ema/ws/v3/appointment/hold
```

### Calendar Preferences

```
GET /ema/ws/v3/calendarPreferences/all-day-instances
    ?startDate=...&endDate=...&facilityId=...&providerId=...
```

### Appointment Finance Info

```
GET /ema/ws/v3/scheduler/appointments-finance-info
```
Same `where` clause as scheduler/appointments.

### Check-in / Appt Flow

```
POST /ema/ws/v3/checkin/appt-count-by-date
```
Body: JSON array (of date strings or appointment IDs).

---

## Patient Portal Activation

### Send Portal Access Email

```
POST /ema/ws/v3/patients/{patientId}/portal
```

Sends the patient an email with instructions to access their patient portal. Works for both initial activation (portal inactive → active) and resending access to an already-active portal — **the same endpoint is used in both cases**.

**Request body:**
```json
{
  "username": "patient.email@example.com",
  "email": "patient.email@example.com",
  "cellPhone": {
    "phoneNumberType": "MOBILE",
    "phoneNumber": "330-206-7819",
    "id": 46764536
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `username` | string | Patient's portal username (typically their email) |
| `email` | string | Email address to send the portal access instructions to |
| `cellPhone.phoneNumberType` | string | Always `"MOBILE"` |
| `cellPhone.phoneNumber` | string | Patient's mobile phone number (formatted: `"330-206-7819"`) |
| `cellPhone.id` | number | Internal phone number record ID (from the patient's `phoneNumbers` array) |

**Response:** `200 OK` with empty body on success.

**Notes:**
- The `cellPhone.id` is the ID of the patient's existing phone record — retrieve it from `GET /ema/ws/v3/patients/{id}` with `selector=phoneNumbers`
- The `username` and `email` may differ (username is used for login, email is where the portal invite is sent)
- Idempotent — calling multiple times just resends the access email
- No observed difference in endpoint or payload between activating an inactive portal and resending to an active one

---

## Other Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/ema/ws/v3/user/permissions` | GET | Current user's permissions |
| `/ema/ws/v3/featureflag/{flag}` | GET | Feature flag status |
| `/ema/ws/v3/stickies` | GET/POST | UI state persistence |
| `/ema/ws/v3/firm/integration/settings/pms` | GET | PMS integration settings |
| `/ema/ws/v3/taxpayerIdentificationNumber/viewable/{id}` | GET | TIN visibility check |
| `/ema/ws/v3/alert/sets` | POST | Alert notifications |

---

## Known Facility & Provider IDs (Liora Dermatology)

- **Facility ID**: `2040` (Liora Dermatology & Aesthetics)
- **Provider IDs observed**: `8327689`, `18490903`, `18993106`

## FHIR API

ModMed offers a FHIR R4 API but it requires separate OAuth registration:
- **Documentation**: https://portal.api.modmed.com/reference/getting-started-3
- **Base path**: `/ema/fhir/r4/` (returns 403 without proper OAuth token)
- The FHIR API is distinct from the internal EMA REST API documented above

---

## Next Steps for Productionizing

### 1. Auth Module
- [ ] Extract the Playwright login + cookie persistence + SSO refresh into a standalone `ema_auth.py` module
- [ ] Store credentials in a secrets manager (not env vars) — AWS Secrets Manager, 1Password CLI, or a local `.env` file excluded from git
- [ ] Add retry logic with exponential backoff for transient failures
- [ ] Add lockout detection — if Keycloak returns "account locked", alert instead of retrying
- [ ] Measure actual session timeouts (JSESSIONID and Keycloak) to optimize refresh intervals

### 2. API Client Library
- [ ] Build a typed Python client (`EmaClient`) wrapping the discovered endpoints
- [ ] Methods: `search_patients()`, `get_patient()`, `list_appointments()`, `find_slots()`, `create_appointment()`, `update_appointment()`
- [ ] Handle pagination automatically (follow `paging.pageNumber` until results are empty)
- [ ] Add session health check on every request — auto-refresh on 401/302

### 3. Patient Portal API (Partially Complete)
- [x] Captured `POST /ema/ws/v3/patients/{id}/portal` endpoint and request body
- [x] Verified it works for resending portal access to already-active patients
- [ ] Test with a truly inactive portal patient to confirm the same endpoint handles initial activation (both test patients had active portals)
- [ ] Check if there's a `DELETE` or status-change endpoint for deactivating portals

### 4. Appointment Creation (Incomplete)
- [ ] Reverse-engineer the `POST /ema/ws/v2/appointment` payload by capturing a real appointment creation in the browser (we confirmed the endpoint accepts POST but didn't capture the required fields)
- [ ] Same for `PUT /ema/ws/v2/appointment/{id}` — capture an update to document the payload
- [ ] Test appointment status transitions (e.g., SCHEDULED → CHECKED_IN → CHECKED_OUT)

### 5. Headless Operation
- [ ] Investigate running Playwright fully headless — the `keyboard.type()` workaround works in headed mode; test if it also works headless with the anti-detection flags
- [ ] If headless fails, consider running a persistent headed Chromium via Xvfb (virtual framebuffer) on Linux, or use `playwright`'s persistent context to keep a browser alive between sessions
- [ ] Alternative: use `undetected-chromedriver` or `playwright-stealth` plugin

### 6. FHIR API Registration
- [ ] Register for ModMed's official FHIR API via https://portal.api.modmed.com
- [ ] This would provide OAuth2 token-based auth (no Playwright needed) with documented, supported endpoints
- [ ] Compare FHIR API coverage vs the internal REST API — FHIR may not expose scheduling/slot-finding

### 7. Monitoring & Resilience
- [ ] Log all API calls and response times for observability
- [ ] Set up alerts for auth failures, session expiry spikes, or unexpected 500s
- [ ] Rate limiting — unknown what EMA's limits are; start conservative and monitor for 429s
- [ ] Handle the `objectLockVersion` field on appointment updates (optimistic locking — read before write)

### 8. Security
- [ ] Store `ema_cookies.json` encrypted at rest (contains session tokens)
- [ ] Rotate the bot account password on a schedule
- [ ] Audit log all write operations (appointment create/update) for compliance
- [ ] Ensure the bot account has minimum necessary permissions in EMA
