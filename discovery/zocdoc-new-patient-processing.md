# Zocdoc New Patient Processing — Runbook & Learnings

Documented 2026-03-15 after processing 9 weekend Zocdoc patients and auditing 7 more.

## Workflow

For each new Zocdoc patient:

1. **Report start** → Genies Bottle (with `correlation_id`)
2. **Message on Zocdoc** → Send "call the office" request (patients must call within 24h of booking so the practice can cancel without the $100 Zocdoc booking fee)
3. **Activate portal** (if needed) → EMA
4. **Send Genie SMS** → Weave
5. **Report completion** → Genies Bottle (same `correlation_id` to update, not create new)

**Why the Zocdoc call request matters:** Each Zocdoc booking costs the practice $100. By having the patient call the office directly, the practice can cancel the Zocdoc booking and rebook directly, avoiding the fee. This must happen within 24 hours of the booking time.

**Important:** Always pass a `correlation_id` on both the "running" and "completed" calls so the second call **updates** the existing execution rather than creating a new one.

```python
correlation_id = f"zocdoc-{mrn}-{appt_date}"

gb.report_process("zocdoc-new-booking", "running",
    correlation_id=correlation_id,
    trigger_type="manual", trigger_source="zocdoc",
    patient={"mrn": mrn, "name": name},
    steps=[{"step": 1, "action": "Pulled appointment from ZocDoc", "status": "done"}])

# ... call request + portal + SMS ...

gb.report_process("zocdoc-new-booking", "completed",
    correlation_id=correlation_id,  # same ID = update
    patient={"mrn": mrn, "name": name},
    outcome_summary="Call request sent, portal activated, Genie SMS sent",
    steps=[
        {"step": 1, "action": "Pulled appointment from ZocDoc", "status": "done"},
        {"step": 2, "action": "Sent call office request on ZocDoc", "status": "done"},
        {"step": 3, "action": "Activated patient portal in ModMed", "status": "done"},
        {"step": 4, "action": "Sent Genie SMS via Weave", "status": "done"},
    ])
```

### Genie SMS Template

- **Name:** "Genie - New Zocdoc Patient"
- **ID:** `00914ffc-ae68-49c8-a76d-a0d78a5d5d21`
- **Variable:** `{{FIRST_NAME}}`
- **Body:**

```
Hello {{FIRST_NAME}} ,

Thanks for scheduling with us at Liora.

In order to confirm your appointment, please log into the portal (link just sent) and complete the registration, including adding a credit card on file (securely encrypted).

Because appointments scheduled through Zocdoc reserve dedicated provider time and incur a booking cost of $100 to the practice, we require all new patients to complete registration and maintain a card on file prior to confirming the visit. If the registration is not completed, we may need to release the appointment so it can be offered to another patient in need of care.

Please let us know if you need the portal link resent or if we can assist you in any way.

We look forward to hearing from you soon!
```

### Finding Unprocessed Patients

**Primary source: Zocdoc bookings by booking time.** The cron runs every 30 min and must catch new bookings within 30 min of being placed. Scan Zocdoc `list_bookings()` sorted by `LAST_UPDATED_BY_PATIENT` descending, filter to `patientType == "NEW"`, and check `bookingTimeUtc` to find recent bookings.

1. **Get recent Zocdoc bookings** — `zoc.list_bookings()` returns data at `data.appointments.appointments`. For each booking:
   - Check `patientType == "NEW"`
   - Use `get_booking(appointmentId)` to get full details including `requestId` (needed for call request) and `bookingTimeUtc`
   - The `requestId` field (numeric) is needed for `send_call_request_browser()`, NOT the `appointmentId`

2. **Check Genies Bottle for prior processing** — use the bot-facing query endpoint:
   ```python
   existing = gb.query_executions(task_slug="zocdoc-new-booking", patient_mrn=mrn, status="completed")
   if existing:  # non-empty list = already processed, skip
       continue
   ```

3. **Check Weave for prior messaging** — use `search_messages()` (NOT `list_threads` which is capped at ~100):
   ```python
   results = weave.search_messages(f"{first_name} {last_name}")
   if results.get('numResults', 0) > 0:
       # Already messaged, skip
       continue
   ```
   This uses `GET /sms/search/v2` which is backed by a search index and bypasses the Firestore thread listing limitation.

4. **Cross-reference EMA** — search by patient name to get EMA patient ID, then:
   - **Check portal** — `ema.get_patient(pid)` — portal is active if `username` field exists
   - Get patient email for portal activation

**All three checks (GB, Weave search, EMA portal) must pass before taking any action on a patient.**

**Note on Zocdoc statuses:** `SYNC_CONFIRMED` does NOT mean the patient is confirmed with the practice. It's an auto-sync status. All new bookings need processing regardless of Zocdoc status (except `PATIENT_CANCELLED`).

---

## API Gotchas & Solutions

### EMA (ModMed)

| Issue | Cause | Solution |
|-------|-------|----------|
| `send_portal_email()` returns 500 | Including `cellPhone` in payload | **Omit `cellPhone`** — just send `username` and `email` |
| `search_patients()` with `patientStatus` filter returns 500 | EMA's `fn=` syntax broken for this field | Don't filter by status — filter client-side |
| `list_appointments()` with complex `where` (e.g. `!=`, nested `appointmentType.name==`) returns 500 | EMA doesn't support these operators | Query by date range only, filter client-side |
| Session returns 302 instead of 401 on expiry | EMA's auth pattern | Client uses `allow_redirects=False` and checks for 302 |
| Paginating appointments | No easy page_number on list_appointments | Query single days in a loop |

### Weave

| Issue | Cause | Solution |
|-------|-------|----------|
| `WeaveClient.from_token(None)` fails silently | `None` passed as token string | Always pass actual token: `os.environ['WEAVE_TOKEN']` |
| Template API (`/messaging/templator/v2/templates`) returns 401 | Different auth scope needed | Hardcode template text or fetch from Genies Bottle |
| SMS safety guard blocks production sends | Dev safeguard in `check_safety_guard()` | **Removed** in production — guard calls deleted from `send_message()` and `dial()` |
| **Token expires every ~4 hours** | JWT has short TTL | Refresh via Playwright persistent profile at `/tmp/weave-token-profile` — see memory `reference_weave_token.md` |
| **Thread listing caps at ~100 threads** | `/sms/data/v4/threads` pagination cycles back to the same set | Don't use thread listing to verify if a patient has been messaged |
| **Thread filter params are ignored** | `personId`, `personPhone`, `search` all return the same 100 threads | No workaround — API limitation |
| **No "lookup thread by phone" endpoint** | Thread list comes from Firestore (Firebase real-time), not REST. Tried `/threads/lookup`, UUID v5 construction (exhaustive brute force across all known namespaces), `/threads/search`, `/v3/thread?personPhone=` — none work | Use **EMA appointment status** (`statusValue` = Confirmed vs Pending) as source of truth for "has patient been contacted." Save `threadId` from `send_message()` response to Genies Bottle for future reference |
| **`send_message()` returns threadId** | Response includes `smsId`, `threadId`, `personId` | Save these to Genies Bottle activity payload for future thread lookups |

### Zocdoc

| Issue | Cause | Solution |
|-------|-------|----------|
| Session expires → 403 on `refresh_session()` | DataDome + cookie expiry | Must do full browser re-login: `login_browser()` (needs `ZOCDOC_EMAIL`/`ZOCDOC_PASSWORD` env vars) |
| `list_bookings()` response at wrong path | Response is `data.appointments.appointments`, not `data.inboxRows.inboxRows` | Use correct path |
| `list_bookings()` returns 0 rows but status counts show items | `fromAppointmentTime` filters future only; check `totalAppointmentsCount` | Paginate: check `pagesCount` field |
| `dotenv.load_dotenv()` fails in heredoc scripts | `find_dotenv()` can't find `.env` from stdin | Pass explicit path: `load_dotenv('/path/to/.env')` |
| `send_call_request_browser()` returns 500 | Wrong ID passed — `appointmentId` (string like `app_...`) instead of `requestId` (numeric) | Use `get_booking(appointmentId)` to get `requestId` (e.g. `82615864`), pass that to `send_call_request_browser()` |
| `SYNC_CONFIRMED` status misleading | Auto-sync status, does NOT mean patient confirmed with practice | Process all new bookings regardless of Zocdoc status (except `PATIENT_CANCELLED`) |

### Genies Bottle

| Issue | Cause | Solution |
|-------|-------|----------|
| Transient 500 errors | Server-side flakiness | Retry once, or use direct `requests.post()` as fallback |
| **Must use `correlation_id` on both calls** | Without it, "running" and "completed" create 2 separate executions | Generate a deterministic `correlation_id` (e.g. `zocdoc-{mrn}-{date}`) and pass it on both the "running" and "completed" `report_process()` calls. Portal now upserts by `correlation_id`. |
| `list_executions()` returns 401 with API key | JWT-only endpoint | Use `query_executions()` instead — hits `GET /api/webhooks/executions` which accepts X-API-Key auth. Supports `task_slug`, `status`, `patient_mrn`, `patient_name`, `correlation_id` filters. |

---

## Client Initialization Pattern

```python
import os
from dotenv import load_dotenv
load_dotenv('/path/to/.env')  # Always use explicit path

from liora_tools import WeaveClient, EmaClient, GenieBottleClient
from liora_tools.auth.ema import load_cookies

# EMA
cookies = load_cookies('discovery/modmed/ema_cookies.json')
ema = EmaClient.from_cookies(cookies)

# Weave — must pass actual token string
weave = WeaveClient.from_token(os.environ['WEAVE_TOKEN'])

# Genies Bottle — reads GENIE_BOTTLE_API_KEY from env
gb = GenieBottleClient.from_api_key()
```

## Processing Order

Process patients sequentially (avoid rate limits), prioritized by booking time (most recent first):
- **Primary scan: Zocdoc bookings** — check all bookings with `patientType == "NEW"`, sorted by last updated
- Skip `PATIENT_CANCELLED` bookings
- Skip patients already processed in Genies Bottle (`gb.query_executions(task_slug="zocdoc-new-booking", patient_mrn=mrn, status="completed")`)
- For the Zocdoc call request, use `requestId` (numeric) from `get_booking()` details, NOT `appointmentId`
- Goal: process new bookings within 30 minutes of being placed (cron runs every 30 min)
