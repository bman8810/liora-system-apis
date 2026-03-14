# liora_tools

Python client library for Liora Dermatology's healthcare platform APIs — **Weave**, **ModMed EMA**, and **Zocdoc**.

Extracted from reverse-engineered discovery scripts into clean, importable client classes with safety guards, separated auth, and sensible defaults.

## Quick Start

```python
from liora_tools import WeaveClient, EmaClient, ZocdocClient

# Weave — from a JWT token
client = WeaveClient.from_token(token)
threads = client.list_threads(page_size=5)

# EMA — from saved cookies
from liora_tools.auth.ema import load_cookies
client = EmaClient.from_cookies(load_cookies())
patients = client.search_patients(last_name="Smith")

# Zocdoc — from saved cookies
from liora_tools.auth.zocdoc import load_cookies
client = ZocdocClient.from_cookies(load_cookies())
counts = client.get_status_counts()
```

## Structure

```
liora_tools/
├── __init__.py              # Exports WeaveClient, EmaClient, ZocdocClient
├── config.py                # Dataclass configs with Liora Derm defaults
├── exceptions.py            # LioraAPIError hierarchy
├── utils.py                 # Phone normalization, safety guards
├── auth/
│   ├── weave.py             # JWT login via browser, session builder
│   ├── ema.py               # Keycloak login, SSO refresh, cookie persistence
│   └── zocdoc.py            # Browser login, cookie management, DataDome bypass
├── weave/
│   └── client.py            # WeaveClient
├── modmed/
│   └── client.py            # EmaClient
└── zocdoc/
    ├── client.py            # ZocdocClient
    └── queries.py           # GraphQL query constants
```

## Dependencies

- `requests` — HTTP client (required)
- `playwright` — browser automation for auth flows (optional, only needed for login)

---

## Authentication

Auth is fully separated from client classes. Playwright imports are lazy (inside functions) so clients work without Playwright installed — you only need it when logging in fresh.

### Weave

Weave uses JWT Bearer tokens stored in the browser's localStorage.

```python
from liora_tools.auth.weave import login_browser, get_session

# Option 1: Browser login (opens Chromium, log in manually)
token = login_browser()

# Option 2: Use an existing token (from env, .env, etc.)
token = os.environ["WEAVE_TOKEN"]

# Build a client
client = WeaveClient.from_token(token)
# — or build a session manually —
session = get_session(token)
client = WeaveClient(session)
```

### ModMed EMA

EMA uses session cookies via Keycloak SSO. Three-tier persistence strategy:

1. **Reuse saved cookies** — instant, no browser
2. **SSO refresh** — ~3s browser launch, no credentials needed if Keycloak session is alive
3. **Fresh login** — ~8s with Playwright, needs `EMA_USER` / `EMA_PASS` env vars

```python
from liora_tools.auth.ema import ensure_session, load_cookies, save_cookies

# Automatic — tries all three tiers
session, cookies = ensure_session()
client = EmaClient(session)

# Manual — from saved cookies
cookies = load_cookies()  # reads ema_cookies.json
client = EmaClient.from_cookies(cookies)
if not client.check_session():
    print("Session expired — need to re-authenticate")
```

### Zocdoc

Zocdoc uses cookie-based auth with a DataDome anti-bot header. Login requires `ZOCDOC_EMAIL` / `ZOCDOC_PASSWORD` env vars.

```python
from liora_tools.auth.zocdoc import login_browser, load_cookies, save_cookies

# Browser login (reads credentials from env vars)
cookies = login_browser()
save_cookies(cookies)

# From saved cookies
cookies = load_cookies()  # reads zocdoc_cookies.json
client = ZocdocClient.from_cookies(cookies)
```

**Important:** REST endpoints on `www.zocdoc.com` are blocked by DataDome for Python requests. Use `send_call_request_browser()` from `liora_tools.auth.zocdoc` for those calls — it makes the request via Playwright's `page.evaluate()`.

---

## Configuration

Each platform has a config dataclass with Liora Dermatology defaults. Override any field for different practices:

```python
from liora_tools.config import WeaveConfig, EmaConfig, ZocdocConfig

# Use defaults
client = WeaveClient.from_token(token)

# Override specific fields
config = WeaveConfig(location_id="different-uuid", from_name="Other Practice")
client = WeaveClient.from_token(token, config=config)
```

### WeaveConfig

| Field | Default | Description |
|-------|---------|-------------|
| `api_base` | `https://api.weaveconnect.com` | API base URL |
| `location_id` | Liora's UUID | Location identifier |
| `tenant_id` | Liora's UUID | Tenant identifier |
| `user_id` | Liora's UUID | User identifier |
| `location_phone` | `+12124334569` | Office phone number |
| `softphone_id` | Liora's UUID | Softphone device ID |
| `sip_profile_id` | Liora's UUID | SIP profile ID |
| `from_number` | `2124334569` | Outbound caller ID (10 digits) |
| `from_name` | `Liora Dermatology & Aesthetics` | Outbound caller name |
| `allowed_send_phones` | `{+13302067819, ...}` | SMS safety allowlist |
| `allowed_dial_phones` | `{+13302067819, ...}` | Dial safety allowlist |

### EmaConfig

| Field | Default | Description |
|-------|---------|-------------|
| `base_url` | `https://lioraderm.ema.md` | EMA instance URL |
| `cookie_file` | `ema_cookies.json` | Cookie persistence path |
| `facility_id` | `2040` | Facility ID |

### ZocdocConfig

| Field | Default | Description |
|-------|---------|-------------|
| `gql_url` | `https://api2.zocdoc.com/provider/v1/gql` | GraphQL endpoint |
| `rest_base` | `https://www.zocdoc.com` | REST base URL |
| `practice_id` | `pt_FMyrNSVN50CbgjEI0NcL9h` | Practice identifier |
| `provider_id` | `pr_eTTyn6m-e0y7oL1yjr9JQB` | Provider identifier |
| `cookie_file` | `zocdoc_cookies.json` | Cookie persistence path |

---

## Safety Guards

Methods that send messages or place calls are **safety-guarded** — they will only target phone numbers in the configured allowlist. Attempting to reach an unlisted number raises `SafetyGuardError`.

```python
# This works (number is in allowed_send_phones)
client.send_message("330-206-7819", "Hello!")

# This raises SafetyGuardError
client.send_message("555-000-0000", "Hello!")
```

To add numbers to the allowlist, pass a custom config:

```python
config = WeaveConfig(
    allowed_send_phones={"+13302067819", "+15550001234"},
    allowed_dial_phones={"+13302067819", "+15550001234"},
)
client = WeaveClient.from_token(token, config=config)
```

---

## Exceptions

All exceptions inherit from `LioraAPIError`:

| Exception | When |
|-----------|------|
| `LioraAPIError` | Base — any API error (has `status_code`, `response`) |
| `AuthenticationError` | 401, expired session, or 302 redirect (EMA) |
| `SafetyGuardError` | Phone number not in allowlist |
| `GraphQLError` | GraphQL response contained errors (has `errors` list) |
| `RateLimitError` | 429 Too Many Requests |
| `OptimisticLockError` | 409 Conflict — `objectLockVersion` mismatch (appointment modified concurrently) |

```python
from liora_tools.exceptions import AuthenticationError, SafetyGuardError

try:
    client.list_threads()
except AuthenticationError:
    # Token expired — need to re-authenticate
    token = login_browser()
    client = WeaveClient.from_token(token)
```

---

## API Reference

### WeaveClient

#### Messaging

| Method | Description |
|--------|-------------|
| `list_threads(page_size=25)` | List inbox threads |
| `get_thread(thread_id, page_size=25)` | Get messages in a thread |
| `send_message(person_phone, body, person_id=None)` | Send SMS (**safety-guarded**) |
| `save_draft(thread_id, body, person_phone)` | Save a draft message |
| `get_draft(thread_id)` | Get draft for a thread |
| `indicate_typing(thread_id, person_phone, is_typing=True)` | Send typing indicator |

#### Contacts

| Method | Description |
|--------|-------------|
| `search_persons(query, page_size=25)` | Search contacts by name or phone |
| `lookup_by_phone(phone)` | Look up a person by phone number |
| `get_person(person_id)` | Get full person details |

#### Call Records

| Method | Description |
|--------|-------------|
| `list_call_records(page_size=25)` | List call history |
| `list_hydrated_call_records(page_size=10)` | Call history with person data |
| `get_call_records_by_person(person_ids)` | Call records for specific persons |

#### Voicemail

| Method | Description |
|--------|-------------|
| `list_voicemails(page_size=25)` | List voicemails with person data |
| `list_voicemail_messages(page_size=25)` | List raw voicemail messages |
| `count_unread_voicemails()` | Count unread per mailbox |
| `list_voicemail_boxes()` | List voicemail boxes |

#### SIP / Phone

| Method | Description |
|--------|-------------|
| `get_softphone_settings()` | Get softphone configuration |
| `fetch_sip_credentials()` | Extract SIP creds (username, password, domain, proxy, extension) |
| `list_sip_profiles()` | List SIP profiles for the tenant |
| `get_tenants()` | Get tenant info |
| `dial(destination)` | Place outbound call (**safety-guarded**) |
| `list_call_queues()` | List call queues |
| `get_call_queue_metrics()` | Get call queue metrics |
| `check_registration()` | Check SIP profile registration status |

---

### EmaClient

#### Session

| Method | Description |
|--------|-------------|
| `check_session()` | Test if session is alive → `bool` |

#### Patients

| Method | Description |
|--------|-------------|
| `list_patients(where, page_size, page_number, selector, sort_by)` | List patients with EMA query language |
| `search_patients(last_name, first_name, status, page_size)` | Search by name/status (builds `where` clause) |
| `get_patient(patient_id, selector)` | Get patient by ID |
| `send_portal_email(patient_id, username, email, cell_phone)` | Send/resend patient portal activation email |

#### Appointments

| Method | Description |
|--------|-------------|
| `list_appointments(start_date, end_date, selector, where, page_size)` | List appointments with date filtering |
| `get_appointment(appointment_id, selector)` | Get appointment by ID |
| `create_appointment(payload)` | Create appointment (v2) |
| `update_appointment(appointment_id, payload)` | Update appointment (v2) |
| `reschedule(appointment_id, new_start, new_duration, provider_id, reason)` | Reschedule appointment to a new date/time (read-before-write with optimistic locking) |
| `cancel_appointment(appointment_id, reason, notes)` | Cancel an appointment with a reason |
| `list_cancel_reasons()` | List available cancellation reasons |
| `find_slots(appt_type_id, duration, time_of_day, specific_date, ...)` | Find available appointment slots |

#### Reference Data

| Method | Description |
|--------|-------------|
| `list_appointment_types(page_size=100)` | List appointment types |
| `list_facilities(page_size=100)` | List facilities |

**EMA Query Language:** The `where` parameter supports operators like `==`, `>=`, `<=`, `=in=()`, `=null=`, `fn=`, with `;` as AND separator. The `selector` parameter controls which fields are returned (similar to GraphQL field selection). See `discovery/modmed/ema-api-reference.md` for full documentation.

---

### ZocdocClient

#### Inbox / Bookings

| Method | Description |
|--------|-------------|
| `list_bookings(page_number, page_size, statuses, patient_name)` | List inbox appointments with pagination and filtering |
| `get_booking(appointment_id, appointment_source)` | Get full appointment detail including patient PHI |
| `get_status_counts()` | Get appointment status counts (inbox tab badges) |

#### Messaging

| Method | Description |
|--------|-------------|
| `mark_as_read(appointment_id, appointment_source)` | Mark appointment as read/confirmed |
| `send_call_request(request_id, reasons)` | Send "call the office" request (DataDome-blocked — use browser version) |

#### Session

| Method | Description |
|--------|-------------|
| `refresh_session()` | Refresh auth session |

#### GraphQL

| Method | Description |
|--------|-------------|
| `gql(operation, variables, query)` | Execute any GraphQL operation |

**Browser-based calls:** For REST endpoints blocked by DataDome, use the auth module directly:

```python
from liora_tools.auth.zocdoc import send_call_request_browser

result = send_call_request_browser(request_id=12345, reasons=["Insurance"])
```

**Custom GraphQL:** Use `gql()` with query strings from `liora_tools.zocdoc.queries` or your own:

```python
from liora_tools.zocdoc.queries import GET_INBOX_ROWS

result = client.gql("getInboxRows", variables={...}, query=GET_INBOX_ROWS)
```

Available query constants: `GET_INBOX_ROWS`, `GET_PHI_APPOINTMENT_DETAILS`, `GET_STATUS_AGGREGATES`, `MARK_INBOX_APPOINTMENT_STATUS`.

**Important:** The `requestId` (numeric, from `get_booking()` response) is used for REST messaging — not the `appointmentId` (UUID format `app_xxx`).

---

## Environment Variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `WEAVE_TOKEN` | Weave | JWT token (alternative to browser login) |
| `EMA_USER` | EMA | Keycloak username |
| `EMA_PASS` | EMA | Keycloak password |
| `ZOCDOC_EMAIL` | Zocdoc | Login email |
| `ZOCDOC_PASSWORD` | Zocdoc | Login password |
