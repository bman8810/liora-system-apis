# Process definition: Zocdoc new-patient (`zocdoc-new-booking`)

**Card:** `t_6408ae20`  
**Architecture:** Extend Bottle (see `DECISION-extend-bottle-vs-thin-flow-t_fb77e5ca.md`)  
**Date:** 2026-07-28  

| Layer | Role |
|-------|------|
| **Authoritative definition** | This repo: job `FLOW_DEFINITION` in `liora_tools/scripts/zocdoc_new_booking.py` + this doc |
| **Machine copy (repo)** | [`zocdoc-new-booking.flow.json`](./zocdoc-new-booking.flow.json) |
| **Ops / UI copy** | Genies Bottle `TaskDefinition.flow_definition` via seed + `POST /api/webhooks/register-flow` |
| **Executor** | `python -m liora_tools run zocdoc-new-booking` (not Bottle) |

Hooks for **messaging** and **calls** are contracts only — later workers attach without changing the NP critical path.

---

## Flow diagram

```
filters: recent NEW bookings, not cancelled
    → gates: GB completed? Weave fingerprint? portal username? call already?
        → 1 get_booking_details (+ GB running)
        → 2 send_call_request          [$100 fee gate — checkpoint before 3/4]
        → 3 activate_portal            [non-fatal if fails]
        → 4 send_welcome_sms           [template only; relatedIds corr]
        → 5 report_completed           [GB completed | failed | dead_letter]
              ⋮
   hooks (later): messaging_inbound · calls_outbound · calls_inbound
              (same correlation_id; activity/optional step; no fee/SMS redo)
```

---

## correlation_id

```
zocdoc-{appointmentId}
```

Fallback: `zocdoc-{mrn}-{appt_date}`. Field name always `correlation_id`. Same value on every `report_process` upsert. Weave: plain string in `relatedIds` only — never SMS body. Hooks use the **same root id** + hook/step name (not a second root).

---

## Runtime steps (job ↔ Bottle)

Canonical report step object (decision contract):

```json
{
  "name": "send_call_request",
  "status": "done",
  "detail": "optional",
  "error": null,
  "at": "2026-07-28T00:00:00Z",
  "attempt": 1
}
```

Job today also emits `step` (int) + `action` (human label) for merge/UI aliases. `step_done` matches any of `name` / `action` / `step` against alias lists below.

| # | `name` (def) | Job `action` label | Aliases for resume | Service | Critical |
|---|--------------|--------------------|--------------------|---------|----------|
| 1 | `get_booking_details` | Pulled appointment from ZocDoc | pull, init | zocdoc | yes (start) |
| 2 | `send_call_request` | Sent call office request on ZocDoc | call, call_request | zocdoc | **yes — fee** |
| 3 | `activate_portal` | Activated patient portal in ModMed | portal, send_portal, ema_portal | ema | no |
| 4 | `send_welcome_sms` | Sent Genie SMS via Weave | sms, welcome, weave, send_welcome | weave | yes |
| 5 | `report_completed` | (terminal report) | complete | genies-bottle | yes |

### Per-step I/O (summary)

| Step | In | Out / side effects | Fail → |
|------|----|--------------------|--------|
| 1 | `appointmentId` | `requestId`, corr, masked patient; GB `running` | `failed`, retry |
| 2 | numeric `requestId` | Zocdoc call-request; **ledger + GB checkpoint before 3/4** | `failed`; DL if no requestId permanently |
| 3 | ema id + email; **omit cellPhone** | portal email; may `failed` without failing job | continue to SMS |
| 4 | phone + template vars | SMS; `metadata.weave` ids only | `failed`; DL if no phone after ops |
| 5 | merged steps; call_ok + sms_ok | GB terminal status | re-report on re-entry |

SMS template: id `00914ffc-ae68-49c8-a76d-a0d78a5d5d21` · name **Genie - New Zocdoc Patient** · vars `FIRST_NAME` only.

Activity actions (no PHI): `zocdoc_call_request`, `ema_portal`, `weave_sms`, `zocdoc_new_patient_processed`.

---

## Extension hooks (messaging / calls)

Not implemented inside the NP job. Contracts so Phase 4 messaging/calls workers attach cleanly.

### 1. `messaging_inbound`

| | |
|--|--|
| **When** | Inbound Weave SMS on thread tied to NP welcome |
| **Correlate** | `relatedIds` contains corr → else `metadata.weave.threadId`/`personId` → weak phone_last4 |
| **Write** | `log_activity` `weave_inbound_correlated` and/or optional step `messaging_inbound_observed` |
| **GB allows** | corr, threadId, smsId, direction, observed_at |
| **GB forbids** | message body, full phone |
| **Must not** | Re-send welcome SMS or re-hit call-request |

### 2. `calls_outbound`

| | |
|--|--|
| **When** | Outbound Weave/Genie dial for NP follow-up |
| **Correlate** | Caller passes `correlation_id`; else personId + last4 |
| **Write** | activity `weave_call_outbound` / step `calls_outbound_observed` |
| **GB allows** | corr, call_record_id, disposition, duration_sec |
| **Must not** | Bottle dial orchestration; store recordings in Bottle |

### 3. `calls_inbound`

| | |
|--|--|
| **When** | Patient calls after “call the office” (fee path) |
| **Correlate** | Caller-ID last10 vs open/recent NP exec; or agent-supplied corr |
| **Write** | activity `weave_call_inbound` / step `calls_inbound_observed` |
| **Must not** | Invent corr; auto-cancel Zocdoc without a dedicated policy job |

Full JSON contracts: [`zocdoc-new-booking.flow.json`](./zocdoc-new-booking.flow.json) → `hooks[]`.

---

## Failure → retry vs dead-letter

| Condition | Execution status | Operator |
|-----------|------------------|----------|
| Transient auth / 5xx on critical step | `failed` | **Retry** — same `correlation_id`; job skips `done`/`skipped` |
| Missing `requestId` / fee step hard-fail, still fixable | `failed` | Retry after Zocdoc/chart fix |
| Unrecoverable fee step / no phone after ops exhausted | `dead_letter` (or `failed` + `metadata.dead_letter=true` until UI enum) | **Dead-letter** — human; no auto storm |
| Portal only failed | steps show failed; job may still **completed** | Optional retry portal on re-entry |
| Already `completed` | skip (unless `--force`, still no double side effects) | No-op |
| Hook unmatched inbound | leave execution unchanged | Do not guess corr |

**Retry engine = job re-entry**, not Bottle. Bump `metadata.retry_count` / `last_retry_at` on report. CLI:

```bash
python -m liora_tools run zocdoc-new-booking --force --max-patients=1
```

---

## Staged / mock path (enough to exercise Bottle)

1. **Unit:** `pytest tests/test_zocdoc_new_booking.py` in liora-system-apis  
2. **Dry-run:** `python -m liora_tools run zocdoc-new-booking --dry-run` (no mutating side effects)  
3. **Register def (API key):**

```bash
curl -sS -X POST "$PORTAL_URL/api/webhooks/register-flow" \
  -H "X-API-Key: $GENIE_BOTTLE_API_KEY" -H "Content-Type: application/json" \
  -d @- <<'EOF'
{"task_slug":"zocdoc-new-booking","flow_definition": <contents of flows/zocdoc-new-booking.flow.json> }
EOF
```

4. **Mock execution** (no live Zocdoc) — same `correlation_id` twice:

```json
{
  "task_slug": "zocdoc-new-booking",
  "correlation_id": "zocdoc-app_STAGED_EXAMPLE",
  "status": "running",
  "trigger_source": "staged-mock",
  "patient": {"mrn": "STAGED", "name": "S*** T***"},
  "steps": [
    {"step": 1, "name": "get_booking_details", "action": "Pulled appointment from ZocDoc", "status": "done"}
  ]
}
```

Then `status: completed` with steps 1–4 terminal. Query: `GET /api/webhooks/executions?correlation_id=zocdoc-app_STAGED_EXAMPLE`.

Live SMS/call-request/cron still require Barric go-live + `GENIE_BOTTLE_API_KEY` + Kernel auth.

---

## Seed / UI

Empty DB seed loads a UI-oriented subset of this flow (filters, gates, steps, hooks) on slug `zocdoc-new-booking`. Prefer job `register-flow` at live start so Bottle stays aligned with executor constants.

`FlowVisualization` shows `filters` → `gates` → `steps` (`name`, `service`, `description`). Hooks are stored on the definition for operators/docs; UI may ignore unknown keys until `t_0c1b4cdc`.

---

## Non-goals

- Bottle executing Zocdoc/Weave/EMA steps  
- Thin flow microservice  
- OpenClaw heartbeat as process engine  
- Full messaging/calls workers in this card (hooks only)  
- PHI-rich payloads in Bottle  

---

## Links

- JSON: `discovery/zocdoc-new-booking.flow.json`  
- Decision: genies-bottle `docs/DECISION-extend-bottle-vs-thin-flow-t_fb77e5ca.md`  
- Job: `liora_tools/scripts/zocdoc_new_booking.py`  
- Ops: `discovery/zocdoc-new-booking-job.md`  
- Sibling cards: `t_71b53094` corr · `t_0c1b4cdc` UI · `t_2509bc36` E2E  
