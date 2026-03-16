# Zocdoc New Patient Processing — Cron Config

**Schedule**: `*/30 * * * *` (every 30 minutes)
**Recurring**: true
**Auto-expires**: 3 days (session-only)

## Prompt

Refresh auths & process new Zocdoc patients. GENIE_BOTTLE_API_KEY is in the cwd .env file. Full runbook: discovery/zocdoc-new-patient-processing.md

**Step 0 — Log Run Start**
Log to Genies Bottle immediately: `gb.log_activity("zocdoc_routine_check", "Routine new patient check started", source="zocdoc-cron")`

**Step 1 — Refresh Auths**
Refresh sessions for all three services. Auto-refresh expired sessions:
- **EMA**: try SSO refresh via `refresh_via_keycloak()`, fall back to `login_browser()`
- **Weave**: refresh via persistent Playwright profile at `/tmp/weave-token-profile`, update `.env`
- **Zocdoc**: `login_browser()`, save cookies

If any session still fails after refresh, stop and alert me.

**Step 2 — Find Unprocessed Zocdoc Bookings**
Scan **Zocdoc bookings** (not EMA appointments). The goal is to catch new bookings within 30 minutes.

1. `zoc.list_bookings()` — data at `data.appointments.appointments`, sorted by `LAST_UPDATED_BY_PATIENT` descending
2. For each booking with `patientType == "NEW"` (skip `PATIENT_CANCELLED`):
   - `zoc.get_booking(appointmentId)` to get full details including numeric `requestId` and `bookingTimeUtc`
   - **All three checks must pass before processing:**
     - Check GB: `gb.query_executions(task_slug="zocdoc-new-booking", patient_mrn=mrn, status="completed")` — skip if non-empty
     - Check Weave: `weave.search_messages(f"{first} {last}")` — skip if `numResults > 0` (already messaged)
     - Check EMA portal: `ema.get_patient(pid)` — skip if `username` field exists (portal already active)
   - Cross-reference EMA by patient name to get EMA patient ID, email, phone

**Important:** `SYNC_CONFIRMED` does NOT mean confirmed with the practice — it's an auto-sync status. Process all new bookings regardless of Zocdoc status except `PATIENT_CANCELLED`.

**Step 3 — Process Each Patient (sequentially, ~1s between)**
For each unprocessed patient:

1. **Report start** → `gb.report_process("zocdoc-new-booking", "running", correlation_id=f"zocdoc-{mrn}-{appt_date}", trigger_type="cron", trigger_source="zocdoc", patient={"mrn": mrn, "name": name}, steps=[{"step": 1, "action": "Pulled appointment from ZocDoc", "status": "done"}])`
2. **Message on Zocdoc** → `send_call_request_browser(requestId, reasons=["Other"])` — use the **numeric `requestId`** from `get_booking()`, NOT the `appointmentId` string. This asks the patient to call the office so the practice can cancel the Zocdoc booking (avoids $100 fee).
3. **Activate portal in ModMed** → `ema.send_portal_email(pid, username=email, email=email)` — **omit cellPhone** (causes 500). Skip if portal already active (`username` field exists).
4. **Send Genie SMS in Weave** → `weave.send_message(phone, body=template_text)` using the "Genie - New Zocdoc Patient" template with `{{FIRST_NAME}}` replaced.
5. **Report completion** → `gb.report_process("zocdoc-new-booking", "completed", correlation_id=f"zocdoc-{mrn}-{appt_date}", ...)` with steps in format: `[{"step": 1, "action": "Pulled appointment from ZocDoc", "status": "done"}, {"step": 2, "action": "Sent call office request on ZocDoc", "status": "done"}, {"step": 3, "action": "Activated patient portal in ModMed", "status": "done"}, {"step": 4, "action": "Sent Genie SMS via Weave", "status": "done"}]` — **same correlation_id** so it updates the existing execution
6. **Log activity** → `gb.log_activity("zocdoc_new_patient_processed", ...)`

**Step format:** Each step must include `step` (number), `action` (human-readable description), and `status` ("done" or "failed"). Example: `{"step": 2, "action": "Sent call office request on ZocDoc", "status": "failed", "detail": "error message"}`

**On failure** for any patient: report to GB as `"failed"` with `error_message`, AND `gb.request_feedback(title=..., priority="high", patient=...)`. Then continue to the next patient.

**Step 4 — Summary**
Print a summary: how many bookings found, how many processed, how many skipped (and why), any failures.

## Usage

To re-create this loop in a new Claude session:

```
/loop 30m <paste prompt above>
```
