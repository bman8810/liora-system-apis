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
3. **Fee-gate checkpoint** — local StepLedger + GB `running` with steps **before** portal/SMS
4. **EMA portal** activate (omit `cellPhone`)
5. **Weave SMS** — template "Genie - New Zocdoc Patient" only (`correlation_id` in `relatedIds`, not body)
6. Report **completed** / **failed** — **same** `correlation_id` (upsert)

## correlation_id

Canonical field name (coordinate across GB + job + Weave metadata): always
**`correlation_id`**.

**SoT (locked `t_71b53094`):** [CORRELATION-ID-SOT.md](./CORRELATION-ID-SOT.md)
(full write-up in genies-bottle `docs/CORRELATION-ID-SOT.md`).

```
zocdoc-{appointmentId}
```

Fallback if appointment id missing: `zocdoc-{mrn}-{appt_date}`.

Messaging/calls hooks later: **same root** id + step name (not a second root).
Weave `relatedIds` gets the plain string; SMS body stays template-only.

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

**Local StepLedger remains the durable job-side source of truth until GB
deploy #1 returns `steps` on `query_executions`.** After that deploy, prefer
`merge_step_lists(GB prior steps + ledger steps)` (already the job path).

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

`WeaveClient.send_message(..., correlation_id=cid)` appends the corr as a
**plain string** to Weave `relatedIds` (API expects entity id strings, not
objects), e.g. `["zocdoc-app_…"]`. No correlation id in SMS body.

On success, GB metadata may store Weave `smsId` / `threadId` / `personId`
keys only (no phone). Primary ops trace remains GB execution by
`correlation_id`; relatedIds is best-effort SMS-side breadcrumb.

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
`GET /api/webhooks/executions` returns `steps`/`metadata` after genies-bottle PR #1
deploy. Until prod reflects that build, local StepLedger is SoT for resume.
Job always `merge_step_lists(GB steps + ledger)`.

## PHI / secrets

| Surface | Policy |
|---------|--------|
| Logs / stdout | Masked name (`J*** D***`), phone last4, email local first char only |
| Errors / feedback | `_redact_error`: emails, phones, JWT (`eyJ…`), `Bearer …`, `api_key`/`password`/`token`/`secret` assignments |
| GB patient payload | `mrn`, **masked** `name`, optional `phone_last4` — never full phone/email/SMS body |
| SMS body | Never logged; step detail uses `template_id` + `template_name` only |
| Secrets | Env + Kernel Managed Auth only; never commit tokens |

### Redacted fields (default omit / mask)

- Full patient name (logs + GB → initials mask)
- Full phone / email
- SMS body text
- Weave/Zocdoc/EMA session tokens, API keys, Bearer headers
- MRN-like long digit runs inside error strings (phone redaction)

## Auth

Uses `get_client("zocdoc"|"weave"|"ema")` → local credentials, then Kernel
Liora Managed Auth sync (`discovery/kernel-auth-bridge.md`).

Required env:

- `GENIE_BOTTLE_API_KEY` (required for **live** runs; dry-run continues with empty GB gates if unset)
- Kernel: `KERNEL_API_KEY`, `KERNEL_PROJECT` (Liora), profile **Liora**
- Or refreshed files under `LIORA_CREDENTIALS_DIR` (~/.liora/credentials)

## Operator: dry-run / staged run

Use this procedure to verify the full integrated path (fee-gate + correlation_id +
template SMS) **without** double side effects.

### Prerequisites

| Item | Notes |
|------|--------|
| Repo on main (or this integrate branch) | `liora_tools/scripts/zocdoc_new_booking.py` has StepLedger + `validate_correlation_id` + template SMS |
| Python venv + deps | `pip install -r requirements.txt` |
| Auth bridge | Kernel project **Liora** (`KERNEL_PROJECT=gxujms2i14jyrds9w3dhrdok`) + profile **Liora**, **or** fresh files under `LIORA_CREDENTIALS_DIR` / `~/.liora/credentials` for zocdoc + weave + ema |
| `GENIE_BOTTLE_API_KEY` | **Required for live** and for real GB gates on dry-run. If unset, dry-run continues with `_NullGB` (empty prior executions; no corr visible in GB) |
| GB deploy | genies-bottle master includes GET executions `steps`/`metadata` (PR #1). Redeploy Vercel if prod still omits `steps` |
| Barric OK | Live SMS / call-request / cron still need explicit go-live — do **not** enable cron here |

### Side-effect gates (what each mode may touch)

| Action | `--dry-run` | Live staged (`--max-patients=1`) | Live full |
|--------|-------------|-------------------------------|-----------|
| List Zocdoc NEW bookings | yes | yes | yes |
| EMA patient lookup | yes (read) | yes | yes |
| GB `query_executions` | yes if key set | yes | yes |
| GB `report_process` running/completed/failed | **no** | yes | yes |
| Zocdoc `send_call_request` ($100 fee path) | **no** | yes (unless already requested) | yes |
| EMA portal activate | **no** | yes if not active | yes |
| Weave SMS send | **no** | yes if fingerprint absent | yes |
| Job file lock | **no** | yes | yes |
| Local StepLedger write | **no** (dry plan only) | yes | yes |

### Step A — unit gate (always)

```bash
cd /path/to/liora-system-apis
.venv/bin/python -m pytest tests/test_zocdoc_new_booking.py -q
# expect: all passed (28+); covers fee resume, corr validate, SMS template refuse, redact
```

### Step B — dry-run (no mutations)

```bash
export KERNEL_PROJECT=gxujms2i14jyrds9w3dhrdok   # if using Kernel bridge
export LIORA_CREDENTIALS_DIR=${LIORA_CREDENTIALS_DIR:-$HOME/.liora/credentials}
# optional but recommended:
export GENIE_BOTTLE_API_KEY=…   # enables real GB prior-status lines

.venv/bin/python -m liora_tools run zocdoc-new-booking \
  --dry-run --lookback-minutes=180 --max-patients=3
```

**Expect stdout (PHI masked):**

- `DRY-RUN — no call-request, SMS, portal, or mutating GB reports`
- Per candidate: `corr=zocdoc-app_…` (or fallback), `WOULD call_request=…`, `WOULD portal=…`, `WOULD sms=…`
- `sms_len=… fingerprint_ok=True` (template gate)
- Final JSON: `"dry_run": true`, `errors: 0` (or actionable auth/list exit)

**Exit codes:** `0` ok · `2` auth · `3` booking list failure.

Save evidence (mask if sharing): redirect stdout to a proof file under the kanban workspace; attach via `kanban-attach.sh`.

### Step C — read correlation_id in Genies Bottle

After any **live** (or staged) run that reported to GB:

```python
from liora_tools import GenieBottleClient
gb = GenieBottleClient.from_api_key()  # GENIE_BOTTLE_API_KEY
rows = gb.query_executions(
    task_slug="zocdoc-new-booking",
    correlation_id="zocdoc-<appointmentId>",  # from dry-run plan line
)
# row["status"], row["steps"], row["metadata"], row["correlation_id"]
```

UI: filter executions by task `zocdoc-new-booking` and the same `correlation_id`.

If `steps` is missing on prod API → Vercel still on pre-#1 build; local StepLedger at
`~/.liora/state/zocdoc-new-booking.json` (or `ZOCDOC_NEW_BOOKING_STATE_PATH`) is SoT.

### Step D — staged live (single patient, Barric OK only)

Only with explicit OK (real $100 call-request + SMS risk):

```bash
export GENIE_BOTTLE_API_KEY=…   # required
# Prefer a patient already call-requested (requestId set) to avoid new fee:
.venv/bin/python -m liora_tools run zocdoc-new-booking \
  --lookback-minutes=180 --max-patients=1
```

Then re-run the **same** command (no `--force`) and confirm:

- Patient skipped or steps short-circuit (call/portal/SMS not re-sent)
- Ledger shows steps `done`/`skipped` for that `correlation_id`
- GB execution still single row (upsert), status completed/failed — not duplicated

Idempotent boundaries to verify:

1. **GB completed** → full skip (unless `--force`, which still won't double SMS/call)
2. **Booking `requestedToCallTimestamp` / requestId** → call_request skipped
3. **Ledger / GB steps** after fee-gate → portal/SMS resume without re-call
4. **Weave $100 fingerprint** → SMS skipped even with `--force`
5. **EMA username set** → portal skipped
6. **File lock** → second concurrent tick exits `status=locked`

### Step E — failure surfacing smoke (optional, unit-backed)

Unit tests cover `JobStepError` + failed GB report with steps. Live optional: revoke one
credential mid-flight only in lab; expect redacted `error_message` and high-priority
feedback with `bot_context.correlation_id` — no secrets/PHI dumps.

### Residual if live portal cannot be fully exercised

| Blocker | Impact | Mitigation |
|---------|--------|------------|
| No `GENIE_BOTTLE_API_KEY` on host | No live GB corr proof; dry-run uses `_NullGB` | Set key in operator env (not git) |
| Stale Zocdoc/Weave/EMA session | Auth exit 2 / empty candidates | Kernel Managed Auth reauth on Liora profile |
| GB Vercel not redeployed | `query_executions` omits `steps` | Deploy master post-#1; rely on StepLedger |
| No NEW bookings in lookback | Dry-run lists 0 | Widen `--lookback-minutes` |
| Cron not installed | No continuous scan | Card `t_fa0580f2` — Barric go-live |
| Historical genie password files | Rotate if ever shared | Out of job path; do not commit |

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
`steps`) then raises `JobStepError` so main does not double-report without steps:

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

## SMS template (template-first only)

| Field | Value |
|-------|--------|
| Name | `Genie - New Zocdoc Patient` |
| Id | `00914ffc-ae68-49c8-a76d-a0d78a5d5d21` |
| Fingerprint (required) | `booking cost of $100` |
| Allowed merge vars | `{{FIRST_NAME}}` only (`SMS_ALLOWED_VARS`) |

**Job path rules**

1. Body must pass `validate_sms_template` then `build_sms_body` before `weave.send_message`.
2. Free-form / alternate copy is **rejected** (missing fingerprint, unknown `{{VAR}}`, unsubstituted `{{…}}`).
3. Hardcoded runbook body (`SMS_TEMPLATE_BODY`) is SoT; Weave templator is used only when remote body validates.
4. CLI `python -m liora_tools weave send-message` is **ad-hoc ops only** — not used by this job. Do not wire the job to free-form CLI send.
5. `correlation_id` is passed to `WeaveClient.send_message(..., correlation_id=)` for `relatedIds` only — never interpolated into the SMS body.

### Double-SMS controls (coordinate with job harden)

| Layer | Role |
|-------|------|
| GB completed | Skip entire patient |
| Local StepLedger + step_done | Skip SMS if step 4 already done/skipped |
| Weave fingerprint search | Skip if `$100` template already present (honored with `--force`) |
| Job lock | Prevent overlapping ticks |
| Template gate | Does **not** replace idempotency — only blocks free-form body |

## Tests

```bash
.venv/bin/python -m pytest tests/test_zocdoc_new_booking.py -q
```

Pure unit tests — no live portal/SMS.
