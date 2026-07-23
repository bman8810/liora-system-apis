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
3. **EMA portal** activate (omit `cellPhone`)
4. **Weave SMS** — template "Genie - New Zocdoc Patient" only
5. Report **completed** / **failed** — **same** `correlation_id` (upsert)

## correlation_id

```
zocdoc-{appointmentId}
```

Fallback if appointment id missing: `zocdoc-{mrn}-{appt_date}`.

GB webhook upserts on `correlation_id`, so running → completed updates one
execution. Ops can filter:

```python
gb.query_executions(task_slug="zocdoc-new-booking", correlation_id="zocdoc-app_…")
```

## Idempotency / retry

| Layer | Behavior |
|-------|----------|
| File lock | `~/.liora/locks/zocdoc-new-booking.lock` (fcntl); overlapping ticks exit `status=locked` |
| Local StepLedger | `~/.liora/state/zocdoc-new-booking.json` (override `ZOCDOC_NEW_BOOKING_STATE_PATH`); keyed by `correlation_id`; survives GB omitting `steps` |
| GB completed | Skip patient entirely (unless `--force`) |
| Step resume | Merge GB prior steps + ledger; if call/portal/SMS `done`/`skipped`, do not re-send |
| Booking timestamp | `patient.requestedToCallTimestamp` ⇒ treat call-request as already done |
| Fee-gate checkpoint | After call-request success/skip, ledger + GB `running` are written **before** portal/SMS |
| Weave search | SMS skipped if $100 template fingerprint already present (honored even with `--force`) |
| Portal | Skipped if EMA `username` already set |

`--force` ignores only the GB **completed** gate. It still honors ledger,
`step_done`, booking call-already, and Weave fingerprint — never double-charge
or double-SMS.

Re-runs after **failed** mid-flight resume remaining steps only. GB webhook
`GET /api/webhooks/executions` currently omits `steps`; the local ledger is the
durable job-side source of truth for step resume.

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

On per-patient error (live):

1. `gb.report_process(..., status="failed", correlation_id=..., error_message=redacted)`
2. `gb.request_feedback(..., priority="high", bot_context.correlation_id=...)`
3. Continue to next candidate

Scan-level list failures open high-priority feedback without patient PHI.

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
