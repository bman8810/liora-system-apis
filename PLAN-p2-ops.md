# P2 ops/clinical tools (t_6b739350)

## Goal
Add safe, policy-bound voice tools for frequent non-scheduling intents from Weave n=110 sample.

## Tools (Grok Realtime function schemas + handlers)

1. **`triage_lab_results`** — Results/labs status triage → staff MD/callback queue.
   - NEVER return clinical result values/content unless env `LIORA_LAB_RESULTS_DISCLOSE=1` (default off).
   - Params: patient_id (opt), reason (caller words), preferred_callback, confirmed (bool for enqueue).
   - Success: queued + speak path "we'll have the doctor/office call you back about results".
   - Fail: needs_confirmation / queue error / unmatched patient.

2. **`forms_intake_nudge`** — Portal/forms help.
   - Read: if patient_id, try EMA `get_patient` for portal username/email presence (no PHI dump).
   - Spoken path: ModMed portal email; fill before visit.
   - Optional write: resend portal invite only if `confirmed=true` AND `EMA_WRITES_ENABLED` + email/username available; else status `writes_disabled` or `needs_confirmation` with verbal-only nudge.
   - Never invent portal URLs beyond "email from ModMed".

3. **`flag_running_late`** — Same-day running-late flag for FD/MA.
   - Prefer patient_id + optional appointment_id; if missing, list today's upcoming via SchedulingFlow and match.
   - Enqueue staff note (queue_kind=running_late) with eta_minutes if given.
   - Require confirmed=true before enqueue (verbal yes).
   - Success spoken: "I've let the front desk know you're running late."

4. **`clinic_faq`** — Hours / address / parking / phone only from grounded `CLINIC_FACTS` in code (from live site JSON-LD 2026-07-28).
   - Topics: hours | address | parking | phone | all
   - Never invent other clinics or after-hours clinical advice.
   - Parking: only "street/garage options near 60th; 2 blocks from 4/5/6 at 59th — we don't validate parking" (no false garage name).

5. **`get_insurance_on_file`** — Read insurance summary if EMA exposes it.
   - Sanitize: strip card numbers / PAN-like digit runs (4+ groups or 13–19 consecutive digits).
   - Never invent eligibility, copay, or "you're covered".
   - Always include speak prompts: bring insurance card; ask if referral needed for medical visits.
   - No card capture params ever.

## Staff queue
- Module `voice_agent/staff_queue.py`: append-only JSONL under env `LIORA_STAFF_QUEUE_PATH` default `cache/voice-staff-queue.jsonl` relative to package parent or `/tmp/liora-staff-queue.jsonl`.
- Fields: ts, kind, patient_id, appointment_id, summary, payload, source=voice_ops.
- No network required for unit tests.

## Integration
- Extend `voice_agent/ema_tools.py` OR new `ops_tools.py` merged into tool list in `grok_bridge.py`.
- Prefer: `ops_tools.py` with `OPS_TOOL_DEFINITIONS` + `handle_ops_tool`; `ema_tools.EMA_TOOL_DEFINITIONS` remains; grok_bridge registers both and routes by name.
- Update `SYSTEM_INSTRUCTIONS_SCHEDULING` in `config.py` with OPS section: labs never read results; insurance no eligibility invent; late/forms/FAQ tools; confirm before staff queue writes.

## Clinic facts (grounded)
```
name: Liora Dermatology & Aesthetics
address: 110 E 60th Street, Suite 800, New York, NY 10022
phone_speak: 212-433-4569 (212-433-GLOW)
email: hello@lioradermatology.com
hours:
  Mon-Thu: 9:00 AM – 6:00 PM
  Fri: 9:00 AM – 4:00 PM
  Sat: 10:00 AM – 4:00 PM
  Sun: Closed
timezone: America/New_York
transit: 2 blocks from 4/5/6 at 59th Street
parking_note: Nearby street and garage parking; office does not guarantee a specific garage.
```

## Tests (fixture / no live EMA)
`tests/test_ops_tools.py`:
- clinic_faq returns hours/address; no hallucinated fields
- triage_lab_results never includes result_values key with clinical content; needs_confirmation without confirmed; queues when confirmed
- get_insurance_on_file strips PAN-like numbers from mock patient
- flag_running_late needs confirm; queues with kind running_late
- forms_intake_nudge verbal path; writes_disabled when gate off
- policy: LIORA_LAB_RESULTS_DISCLOSE default denies content

## Non-goals
Billing balance/pay-link, new-patient intake depth, telehealth, ASR training.

## DoD
- Each tool success/fail spoken path in tool JSON (`message` / `speak`)
- Labs no clinical disclosure default
- Insurance read-only safe
- EMA side-effects (portal resend) gated + confirmed
- pytest green
