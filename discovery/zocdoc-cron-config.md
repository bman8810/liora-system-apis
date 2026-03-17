# Zocdoc New Patient Processing — Cron Config

**Schedule**: `*/30 * * * *` (every 30 minutes)
**Recurring**: true
**Auto-expires**: 3 days (session-only)

## Prompt

Process new Zocdoc patients. GENIE_BOTTLE_API_KEY is in the cwd .env file. Full runbook: discovery/zocdoc-new-patient-processing.md

**Step 0 — Log Run Start**
Log to Genies Bottle immediately: `gb.log_activity("zocdoc_routine_check", "Starting scan", source="zocdoc-cron")`

**Step 1 — Find Unprocessed Zocdoc Bookings**
Scan Zocdoc bookings for new patients to process.

1. `zoc.list_bookings()` — data at `data["data"]["appointments"]["appointments"]`
2. Filter candidates: booked within the last 60 minutes (`now - bookingTimeUtc ≤ 60m`), exclude patients booked > 60m ago. Must also have `patientType == "NEW"` and status != `PATIENT_CANCELLED`.
3. For each candidate, **both gates must pass before processing:**
   - **Gate 1** — Check GB: `gb.query_executions(task_slug="zocdoc-new-booking", patient_mrn=mrn, status="completed")` — skip if non-empty
   - **Gate 2** — Check Weave: `weave.search_messages(f"{first} {last}")` — skip if `numResults > 0` (already messaged)

**Important:** `SYNC_CONFIRMED` does NOT mean confirmed with the practice — it's an auto-sync status. Process all new bookings regardless of Zocdoc status except `PATIENT_CANCELLED`.

**Step 2 — Process Each Patient (sequentially, ~1s between)**
For each unprocessed patient:

1. **Get booking details** → `zoc.get_booking(appointmentId)` — extract numeric `requestId`, patient details, phone
2. **Report start** → `gb.report_process("zocdoc-new-booking", "running", correlation_id=f"zocdoc-{mrn}-{appt_date}", trigger_type="cron", trigger_source="zocdoc", patient={"mrn": mrn, "name": name}, steps=[{"step": 1, "action": "Pulled appointment from ZocDoc", "status": "done"}])`
3. **Message on Zocdoc** → `zoc.send_call_request(requestId, reasons=["Other"])` — use the **numeric `requestId`** from `get_booking()`, NOT the `appointmentId` string. This asks the patient to call the office so the practice can cancel the Zocdoc booking (avoids $100 fee).
4. **Send Genie SMS in Weave** → `weave.send_message(phone, body=template_text)` using the "Genie - New Zocdoc Patient" template with `{{FIRST_NAME}}` replaced.
5. **Report completion** → `gb.report_process("zocdoc-new-booking", "completed", correlation_id=f"zocdoc-{mrn}-{appt_date}", ...)` with steps in format: `[{"step": 1, "action": "Pulled appointment from ZocDoc", "status": "done"}, {"step": 2, "action": "Sent call office request on ZocDoc", "status": "done"}, {"step": 3, "action": "Sent Genie SMS via Weave", "status": "done"}]` — **same correlation_id** so it updates the existing execution
6. **Log activity** → `gb.log_activity("zocdoc_new_patient_processed", ...)`

**Step format:** Each step must include `step` (number), `action` (human-readable description), and `status` ("done" or "failed"). Example: `{"step": 2, "action": "Sent call office request on ZocDoc", "status": "failed", "detail": "error message"}`

**On failure** for any patient: report to GB as `"failed"` with `error_message`, AND `gb.request_feedback(title=..., priority="high", patient=...)`. Then continue to the next patient.

**Step 3 — Summary**
Print a summary: how many bookings found, how many processed, how many skipped (and why), any failures.

## Usage

To re-create this loop in a new Claude session:

```
/loop 30m <paste prompt above>
```
