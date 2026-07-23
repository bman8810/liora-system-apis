# Zocdoc new-booking job — production notes

Productionized job for the Zocdoc NEW-patient path with Genies Bottle
`correlation_id` end-to-end. Implements
[zocdoc-new-patient-processing.md](./zocdoc-new-patient-processing.md).

## Entrypoint

```bash
cd /opt/data/workspace/liora/liora-system-apis
source .venv/bin/activate   # if present

# Staged / safe (no call-request, SMS, portal, or mutating GB writes)
python -m liora_tools run zocdoc-new-booking --dry-run --lookback-minutes=90

# Live (requires auth + GENIE_BOTTLE_API_KEY)
python -m liora_tools run zocdoc-new-booking --lookback-minutes=90

# Caps + force re-evaluate completed gate (still skips done steps)
python -m liora_tools run zocdoc-new-booking --max-patients=1 --force
```

Module form: `python -m liora_tools.scripts.zocdoc_new_booking --dry-run`

## Flow (retained)

1. Report **running** → Genies Bottle (`correlation_id`)
2. **Call-the-office** on Zocdoc (`requestId`, $100 fee avoidance ≤24h)
3. **GB checkpoint** (running + steps) so resume sees call done if later crash
4. **EMA portal** activate (omit `cellPhone`)
5. **Weave SMS** — template "Genie - New Zocdoc Patient" only (`correlation_id` in `relatedIds`, not body)
6. Report **completed** / **failed** — **same** `correlation_id` (upsert)

## correlation_id

Canonical field name (coordinate across GB + job + Weave metadata): always
**`correlation_id`**.

```
zocdoc-{appointmentId}
```

Fallback if appointment id missing: `zocdoc-{mrn}-{appt_date}`.

Never put PHI into `correlation_id`. Never put `correlation_id` in the
patient-facing SMS body.

Job validates loudly via `validate_correlation_id` before side effects:
must be non-blank, start with `zocdoc-`, length ≥ 8.

GB webhook upserts on `correlation_id`, so running → completed updates one
execution.

### Ops: query by correlation_id

```python
rows = gb.query_executions(
    task_slug="zocdoc-new-booking",
    correlation_id="zocdoc-app_…",
)
# Each row includes (after GB deploy): steps, appointment, metadata,
# started_at / completed_at / updated_at, plus status / error_message / patient.
```

Use `steps` on the prior execution for step-level resume (call / portal / SMS).

### Activity action names (step audit trail)

Best-effort `log_activity` after each side effect. Payload is **corr + step
status only** (no phone/body/email):

| Action | When | payload keys |
|--------|------|--------------|
| `zocdoc_call_request` | after call-request done/skipped | `correlation_id`, `step=call_request`, `status` |
| `ema_portal` | after portal done/failed/skipped | `correlation_id`, `step=ema_portal`, `status` |
| `weave_sms` | after SMS done/skipped | `correlation_id`, `step=weave_sms`, `status`, optional Weave id keys only |
| `zocdoc_new_patient_processed` | on full success | `correlation_id` |

### SMS relatedIds / metadata path

`WeaveClient.send_message(..., correlation_id=cid)` appends to Weave
`relatedIds`:

```json
{"type": "correlation_id", "id": "zocdoc-app_…"}
```

No correlation id in SMS body. On success, GB metadata may store Weave
`smsId` / `threadId` / `personId` keys only (no phone).

## Idempotency / retry

| Layer | Behavior |
|-------|----------|
| File lock | `~/.liora/locks/zocdoc-new-booking.lock` (fcntl); overlapping ticks exit `status=locked` |
| GB completed | Skip patient entirely |
| Step resume | If prior execution has call/portal/SMS `done`/`skipped`, do not re-send |
| Call checkpoint | After call_request success, GB `running`+steps before portal/SMS |
| Weave search | SMS skipped if $100 template fingerprint already present |
| Portal | Skipped if EMA `username` already set |

Re-runs after **failed** mid-flight resume remaining steps only — no double
call-request or double SMS when prior steps were recorded on GB.

## PHI / secrets

- Logs mask name/phone/email; errors redact emails and long digit runs
- GB patient payload: `mrn`, `name`, optional `phone_last4` — not full phone/email
- SMS body not written to logs
- No passwords/API keys in repo; use env + Kernel Managed Auth bridge

## Auth

Uses `get_client("zocdoc"|"weave"|"ema")` → local credentials, then Kernel
Liora Managed Auth sync (`discovery/kernel-auth-bridge.md`).

Required env:

- `GENIE_BOTTLE_API_KEY` (required for **live** runs; dry-run continues with empty GB gates if unset)
- Kernel: `KERNEL_API_KEY`, `KERNEL_PROJECT` (Liora), profile **Liora**
- Or refreshed files under `LIORA_CREDENTIALS_DIR` (~/.liora/credentials)

## Dry-run expectations

Dry-run **will**:

- Init clients (auth must work)
- List Zocdoc bookings and apply NEW / lookback filters
- Fetch booking details + EMA lookup
- Query GB for prior executions
- Print masked plan lines + JSON summary

Dry-run **will not**:

- `send_call_request`
- `send_message` / portal email
- `report_process` running/completed/failed
- Take the job lock (lock only on live runs)

Exit codes: `0` ok, `2` auth, `3` booking list failure.

## Failure surfacing

On per-patient error (live), **`process_one` owns reporting** (with current
`steps`) and returns `"error"` so main does not double-report:

1. `gb.report_process(..., status="failed", correlation_id=..., steps=steps, error_message=redacted)`
2. `gb.request_feedback(..., priority="high", bot_context.correlation_id=...)`
3. Main increments `errors` and continues to next candidate

Main only calls `report_failure` for exceptions raised outside that path
(e.g. before steps started). Scan-level list failures open high-priority
feedback without patient PHI.

## Cron (separate task)

Schedule every 30 minutes is **not** installed by this change — see kanban
child **Add 30m cron for zocdoc-new-booking job**. Suggested:

```text
*/30 * * * *  cd …/liora-system-apis && .venv/bin/python -m liora_tools run zocdoc-new-booking >>logs/zocdoc-new-booking.log 2>&1
```

Lock prevents overlap if a run exceeds 30m.

## SMS template

- Id: `00914ffc-ae68-49c8-a76d-a0d78a5d5d21`
- Fingerprint required in body: `booking cost of $100`
- Hardcoded full runbook body is default; Weave templator used only if it
  returns the same fingerprint + `{{FIRST_NAME}}`

## Tests

```bash
.venv/bin/python -m pytest tests/test_zocdoc_new_booking.py -q
```

Pure unit tests — no live portal/SMS.
