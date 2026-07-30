# Read-only EMA scheduling flow

Runbook for Genie agents: validate patient → list upcoming appointments → find open slots.
**Writes are hard-gated** unless `EMA_WRITES_ENABLED=true`.

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
   Missing `patientStatus` is treated as schedulable (EMA often omits it).
2. **Upcoming appointments** — open statuses only.
3. **Open slots** — optional `appt_type_id`.
4. **Combined lookup** — `next_actions` for the agent.

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
| `GROK_VOICE_MODEL` | `grok-voice-think-fast-2.0` |
| `EMA_WRITES_ENABLED` | off |

Tools registered on `session.update`: `lookup_patient`, `list_upcoming_appointments`, `list_visit_types`, `find_open_slots`, `schedule_lookup`.  
Handler: `voice_agent/ema_tools.py` → `SchedulingFlow`. Function events: `response.function_call_arguments.done` → `function_call_output` → debounced `response.create`.

## Write gate

Unset / false blocks: portal email, create, update, reschedule, cancel.

## Next

- Live phone smoke with tools mid-call
- Unlock writes only with explicit product OK
