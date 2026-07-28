# Zocdoc new-booking — 30m cron schedule (Hermes)

**Status:** wired, **DISABLED** until Barric explicit go-live.  
**Kanban:** `t_fa0580f2`  
**Do not** re-enable genie `HEARTBEAT.md` Zocdoc monitor (paused 2026-03-23).

## What runs

| | |
|--|--|
| Schedule | `*/30 * * * *` (every 30 minutes, UTC tick; job uses UTC booking times) |
| Hermes job name | `liora-zocdoc-new-booking` |
| Hermes job id (this host) | `846f536e3dbd` |
| Mode | `no_agent` — script stdout delivered verbatim; **empty stdout = silent** |
| Script | `/opt/data/scripts/zocdoc-new-booking-cron.sh` (basename `zocdoc-new-booking-cron.sh`) |
| Entrypoint | `python -m liora_tools run zocdoc-new-booking --lookback-minutes=90` |
| Repo cwd | `/opt/data/workspace/liora/liora-system-apis` |
| Overlap | Job `JobLock` → `~/.liora/locks/zocdoc-new-booking.lock` (fcntl). Overlap tick exits `status=locked` (healthy, silent). |
| Lookback | **90 minutes** (default) so a missed tick still catches NEW bookings within the 30m product goal |

Related job docs: [zocdoc-new-booking-job.md](./zocdoc-new-booking-job.md) · runbook [zocdoc-new-patient-processing.md](./zocdoc-new-patient-processing.md).

Legacy Claude `/loop 30m` prompt in [zocdoc-cron-config.md](./zocdoc-cron-config.md) is **superseded** by this Hermes script path.

## Double enable gate (reversible)

Live ticks require **both**:

1. Hermes cron job **resumed** (not paused)
2. Enable flag file content in `{1,true,yes,on,enabled}`:

```text
/opt/data/cron/state/zocdoc-new-booking.enabled
```

If the flag is `0` / missing / `false`, the script exits **0 with empty stdout** (silent no-op) even if the cron job is active. This prevents accidental `hermes cron resume` from starting patient SMS.

### Enable (Barric go-live only)

Prerequisites:

- `GENIE_BOTTLE_API_KEY` in `/opt/data/.env`
- Kernel project **Liora** auth healthy (Zocdoc + Weave + EMA) — not `bot_detected`
- Explicit Barric OK for live call-request + SMS

```bash
export PATH="/opt/hermes/bin:$PATH"
export HERMES_HOME=/opt/data

# 1) Flag
printf '1\n' > /opt/data/cron/state/zocdoc-new-booking.enabled

# 2) Resume Hermes job
hermes cron resume 846f536e3dbd   # name: liora-zocdoc-new-booking

# 3) Optional one-shot dry tick (no side effects)
ZOCDOC_NEW_BOOKING_CRON_FORCE_DRY_RUN=1 ZOCDOC_NEW_BOOKING_CRON_ENABLED=1 \
  ZOCDOC_CRON_VERBOSE=1 bash /opt/data/scripts/zocdoc-new-booking-cron.sh
```

### Disable / reverse

```bash
# Immediate stop of side effects (preferred)
printf '0\n' > /opt/data/cron/state/zocdoc-new-booking.enabled

# Also pause scheduler tick
export PATH="/opt/hermes/bin:$PATH"
hermes cron pause 846f536e3dbd
```

Either gate alone is enough to stop live work; use **both** for belt-and-suspenders.

Job id may change if recreated — `hermes cron list | rg zocdoc`.

### One-shot env override (ops)

```bash
ZOCDOC_NEW_BOOKING_CRON_ENABLED=1 bash /opt/data/scripts/zocdoc-new-booking-cron.sh
ZOCDOC_NEW_BOOKING_CRON_FORCE_DRY_RUN=1   # force --dry-run
ZOCDOC_NEW_BOOKING_LOOKBACK_MINUTES=120
ZOCDOC_NEW_BOOKING_MAX_PATIENTS=1
ZOCDOC_CRON_VERBOSE=1                    # print ok summary line
```

## Create job (idempotent ops)

```bash
export PATH="/opt/hermes/bin:$PATH"
export HERMES_HOME=/opt/data

# Only if missing:
hermes cron create "*/30 * * * *" unused \
  --name liora-zocdoc-new-booking \
  --script zocdoc-new-booking-cron.sh \
  --no-agent \
  --deliver local

hermes cron pause 846f536e3dbd   # stay parked until go-live
printf '0\n' > /opt/data/cron/state/zocdoc-new-booking.enabled
```

Current job id on this host: **`846f536e3dbd`** (paused, flag=0).

`--deliver local` keeps healthy runs off Telegram. Failures still surface via:

- non-empty script stdout when consecutive fail streak ≥ 2
- `cron-fail-watch` on `last_status=error` (every 30m, deliver origin)
- Genies Bottle `failed` + high-priority feedback per patient (job path)
- State: `/opt/data/cron/state/zocdoc-new-booking-cron.json`
- Rotating logs: `/opt/data/cron/output/zocdoc-new-booking/run-*.log` (PHI-masked job output)

## Observability (no PHI / secrets)

| Signal | Where |
|--------|--------|
| Per-run counters | state `last_summary` (`processed`, `skipped`, `errors`, `candidates`) |
| correlation_id list | state `last_summary.correlation_ids` (≤10, ids only) |
| Consecutive failures | state `consecutive_failures`; stdout alert at streak ≥ 2 |
| Overlap | `status=locked` → healthy; not a fail |
| GB | executions by `task_slug=zocdoc-new-booking` + `correlation_id=zocdoc-{appointmentId}` |
| Logs | `run-*.log` — job already masks names/phones; do not paste raw logs into chat |

Never enable verbose delivery of full logs to Telegram.

## Failure behavior

| Condition | Script | Cron status |
|-----------|--------|-------------|
| Flag disabled | exit 0 silent | ok |
| Lock held | exit 0 silent | ok |
| Auth fail (exit 2) | fail streak++; alert at ≥2 | error |
| List fail (exit 3) | fail streak++; alert at ≥2 | error |
| Per-patient errors > 0 | fail streak++; alert at ≥2 | error |
| Missing `GENIE_BOTTLE_API_KEY` on live | refuse; fail | error |
| Healthy scan | exit 0 silent | ok |

Job still reports each patient failure to GB with redacted `error_message` and `bot_context.correlation_id`.

## Acceptance checklist

- [x] ~30m schedule registered on Hermes
- [x] Overlap protection via job file lock
- [x] Schedule config documented + reversible (flag + pause)
- [x] Failed runs observable without PHI/secrets dump
- [ ] **Live enable** — Barric go-live only (flag=1 + resume)

## Related

- Production job notes: `discovery/zocdoc-new-booking-job.md`
- Hermes host scripts convention: `hermes-kanban-ops` → `references/cron-no-agent-scripts.md`
