# Read-only EMA scheduling flow

Runbook for Genie agents: validate patient → list upcoming appointments → find open slots.
**Writes are hard-gated** unless `EMA_WRITES_ENABLED=true`.

## Flow steps

1. **Validate patient** — search by last/first name, DOB, phone (client-side digits filter), MRN.
   Statuses: `matched` | `none` | `ambiguous` | `inactive`.
2. **Upcoming appointments** — for a matched patient ID, open statuses only
   (`PENDING`, `CONFIRMED`, `SCHEDULED`, `ARRIVED`, `CHECKED_IN`, `CHANGED`, `PRESENT`).
3. **Open slots** — optional `appt_type_id`; flattens EMA finder groups into a simple slot list.
4. **Combined lookup** — runs 1→2→3 and returns `next_actions` hints for the agent
   (`confirm_existing`, `offer_slots`, `handoff_ambiguous`, …).

## HTTP endpoints

App prefix: `/api/v1/ema` (API key required). Scheduling router: `/scheduling`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/ema/scheduling/flow/validate` | Patient match classification |
| GET | `/api/v1/ema/scheduling/flow/patients/{patient_id}/upcoming` | Upcoming open appts |
| GET | `/api/v1/ema/scheduling/flow/lookup` | Full read-only flow |
| GET | `/api/v1/ema/scheduling/flow/visit-types` | Simplified visit types |

Existing write routes (`POST .../scheduling/reschedule/{id}`, `POST .../scheduling/cancel/{id}`)
remain but raise **403** `ema_writes_disabled` when the gate is off.

## CLI

```bash
python -m liora_tools ema validate-patient --last-name Doe --dob 1980-01-01
python -m liora_tools ema upcoming --patient-id 12345 --days-ahead 90
python -m liora_tools ema schedule-lookup --last-name Doe --type-id 99
python -m liora_tools ema visit-types
```

Cancel/reschedule CLI commands also call the write gate before contacting EMA.

## Write gate

| Env | Effect |
|-----|--------|
| unset / `false` | All EMA mutations blocked (`WriteGatedError`) |
| `EMA_WRITES_ENABLED=true` (or `1`/`yes`/`on`) | Mutations allowed |

Gated actions: `send_portal_email`, `create_appointment`, `update_appointment`,
`reschedule`, `cancel_appointment`.

Python:

```python
from liora_tools.modmed import SchedulingFlow, EmaClient
from liora_tools.modmed.write_gate import ema_writes_enabled, require_ema_writes

flow = SchedulingFlow(client)
result = flow.lookup(last_name="Doe", dob="1980-01-01", appt_type_id=99)
```

## Next phase

- Wire voice-agent tools to these read-only flow endpoints.
- Enable writes only after explicit product approval and monitoring.
