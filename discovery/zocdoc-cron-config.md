# Zocdoc New Patient Processing — Cron Config

> **Prefer the production job**, not ad-hoc agent loops:
> `python -m liora_tools run zocdoc-new-booking`  
> Ops notes: [zocdoc-new-booking-job.md](./zocdoc-new-booking-job.md)  
> correlation_id SoT: `zocdoc-{appointmentId}` (see genies-bottle `docs/CORRELATION-ID-SOT.md`)

**Schedule**: `*/30 * * * *` (every 30 minutes) — Hermes cron when live  
**Live gate**: Barric explicit go-live; double-enable (flag + unpause)

## Prompt (legacy agent loop — keep aligned with job)

Process new Zocdoc patients via the hardened job when possible. If driving APIs by hand:

**correlation_id SoT (required):**

```python
# Primary — appointmentId from Zocdoc booking
correlation_id = f"zocdoc-{appointment_id}"
# Fallback ONLY if appointment id missing:
# correlation_id = f"zocdoc-{mrn}-{appt_date}"  # YYYY-MM-DD
# Never: name, phone, email, SMS body inside the id
```

Same `correlation_id` on every `report_process` (running → completed/failed).

**Step 0 — Log Run Start**  
`gb.log_activity("zocdoc_routine_check", "Starting scan", source="zocdoc-cron")`

**Step 1 — Find Unprocessed Zocdoc Bookings**

1. `zoc.list_bookings()` — data at `data["data"]["appointments"]["appointments"]`
2. Filter: booked within lookback, `patientType == "NEW"`, status != `PATIENT_CANCELLED`
3. Gates before processing:
   - **Gate 1** — `gb.query_executions(task_slug="zocdoc-new-booking", correlation_id=f"zocdoc-{appointmentId}", status="completed")` or MRN completed gate — skip if done
   - **Gate 2** — Weave template fingerprint / search — skip if already messaged

**Important:** `SYNC_CONFIRMED` does NOT mean confirmed with the practice. Process NEW bookings except `PATIENT_CANCELLED`.

**Step 2 — Process Each Patient**

1. **Get booking** → `zoc.get_booking(appointmentId)` — `requestId`, patient, phone  
2. **Report start** →  
   `gb.report_process("zocdoc-new-booking", "running", correlation_id=f"zocdoc-{appointmentId}", trigger_type="cron", trigger_source="zocdoc", patient={"mrn": mrn, "name": masked_name}, steps=[...])`  
3. **Zocdoc call request** → `zoc.send_call_request(requestId, …)` — numeric `requestId`, not appointmentId string  
4. **Fee-gate checkpoint** → GB `running` + steps before portal/SMS  
5. **EMA portal** if needed (omit cellPhone)  
6. **Weave SMS** → template "Genie - New Zocdoc Patient";  
   `weave.send_message(phone, body, correlation_id=correlation_id)` → `relatedIds` only, not body  
7. **Report completion** → same `correlation_id`, status `completed`  
8. **log_activity** → payload with `correlation_id` + step status only (no PHI)

**On failure:** `report_process(..., status="failed", correlation_id=same)` + `request_feedback` with `bot_context.correlation_id`.

## Usage

Production:

```bash
python -m liora_tools run zocdoc-new-booking --dry-run --lookback-minutes=90
# live only with Barric OK + GENIE_BOTTLE_API_KEY
python -m liora_tools run zocdoc-new-booking --lookback-minutes=90
```
