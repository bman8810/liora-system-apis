# EMA scheduling flow (voice P0)

Runbook for Genie agents: validate patient → upcoming/past → slots → gated book/reschedule/cancel.

**Writes are hard-gated** unless `EMA_WRITES_ENABLED=true` **and** voice tools pass `confirmed=true` after verbal yes.

## Live auth (Kernel Liora)

1. Project `gxujms2i14jyrds9w3dhrdok`, profile **Liora**, Managed Auth domain `modmedapp.com` (AUTHENTICATED).
2. Create short-TTL browser → `https://lioraderm.ema.md/ema/practice/staff/` (lands on `lioraderm.modmedapp.com`).
3. Playwright `page.context().cookies()` (httpOnly JSESSIONID).
4. Save EMA/SSO cookies only to `$LIORA_CREDENTIALS_DIR/ema_cookies.json`:

```json
{
  "base_url": "https://lioraderm.modmedapp.com",
  "cookies": [ {"name":"JSESSIONID","value":"…","domain":"lioraderm.modmedapp.com","path":"/"}, … ]
}
```

5. Always `kernel browsers delete $SID`.

Default API host is **`https://lioraderm.modmedapp.com`** (not bare `.ema.md`).

## Flow steps

1. **Validate patient** — name / DOB / phone / MRN. Statuses: `matched` | `none` | `ambiguous` | `inactive`.
   - Phone match uses last 10 digits.
   - Missing `patientStatus` is treated as schedulable.
   - When multiple hits, drop TEST / PHREESIA / TRAINING / GALATIQ / ZZTEST / DUMMY / FAKE charts.
2. **Upcoming appointments** — open statuses only; each item has `speak_as` / `local_time` (America/New_York).
3. **Past appointments** — recent history; excludes CANCELLED/CANCELED by default; `latest` shortcut.
4. **Open slots** — round-robin providers, then rank **non-zzz → Rhee first → start**.
5. **Book / reschedule / cancel** — require `confirmed=True` then `EMA_WRITES_ENABLED`.
6. **Combined lookup** — `next_actions` for the agent (reads only).

## HTTP

| Method | Path |
|--------|------|
| GET | `/api/v1/ema/scheduling/flow/validate` |
| GET | `/api/v1/ema/scheduling/flow/patients/{patient_id}/upcoming` |
| GET | `/api/v1/ema/scheduling/flow/lookup` |
| GET | `/api/v1/ema/scheduling/flow/visit-types` |

Writes (`reschedule`/`cancel`/create/portal) → **403** `ema_writes_disabled` when gated.

## CLI

```bash
export LIORA_CREDENTIALS_DIR=~/.liora/credentials
python -m liora_tools ema validate-patient --last-name Doe --dob 1980-01-01
python -m liora_tools ema upcoming --patient-id 12345
python -m liora_tools ema schedule-lookup --last-name Doe --type-id 6188
python -m liora_tools ema visit-types
```

## Voice agent (Grok tools)

| Env | Default |
|-----|---------|
| `EMA_VOICE_TOOLS` | `1` (set `0` to disable tools / use scripted prompt) |
| `AI_BACKEND` | `grok` |
| `GROK_VOICE_MODEL` | `grok-voice-latest` |
| `EMA_WRITES_ENABLED` | off |
| `OUTBOUND_DIAL_PHONE` | set by CallManager from dialed number or inbound ANI |

Tools on `session.update`:

- Reads: `lookup_patient`, `list_upcoming_appointments`, `list_past_appointments`, `list_visit_types`, `find_open_slots`, `schedule_lookup`
- Gated writes: `book_appointment`, `reschedule_appointment`, `cancel_appointment` (`confirmed` required)

Handler: `voice_agent/ema_tools.py` → `SchedulingFlow`.  
Identity: outbound phone+DOB; inbound ANI+DOB.  
Timezone: **speak `speak_as` only** (never model-convert UTC).  
Reschedule fallback policy: cancel-then-book with **two** verbal confirms.

## Write gate

Unset / false blocks: portal email, create, update, reschedule, cancel, and voice write tools (returns `writes_disabled` / `needs_confirmation`).

## Tests

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_scheduling_flow.py -q
```

## Safety

- Smoke dials: allowlisted E.164 only (Barric smoke + Twilio sink C + manual staff).
- No live patient writes until Barric greenlight + `EMA_WRITES_ENABLED=true`.
