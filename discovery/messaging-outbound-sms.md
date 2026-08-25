# Messaging outbound SMS (template-first)

Reusable Weave SMS sender for the future messaging worker. **Not** Twilio DID.
Module: `liora_tools.messaging.outbound.OutboundSmsSender`.

## Safety defaults

| Env flag | Default | Meaning |
|----------|---------|---------|
| `LIORA_SMS_DRY_RUN` | `true` | Log intent only; **no** Weave send; does **not** mark idempotency as sent |
| `LIORA_SMS_GO_LIVE` | `false` | Hard gate — live `weave.send_message` only if go-live **and** dry-run off |
| `LIORA_SMS_STAGED_MOCK` | `false` | When dry-run is off and go-live is off, pretend success (records idempotency) |

**Live send requires both:**

```bash
LIORA_SMS_GO_LIVE=true
LIORA_SMS_DRY_RUN=false
```

## Modes (resolved in order)

1. **dry_run** (default) — redacted log of intent; skip if already sent; do not record sent
2. **staged_mock** — mock success IDs; record sent (no Weave call)
3. **blocked_no_go_live** — dry-run off, go-live off, staged mock off
4. **live** — `weave.send_message(phone, body, person_id=…, correlation_id=…)`  
   (`correlation_id` goes in Weave `relatedIds`, **never** in the SMS body)

## Template-first

- Routes registered in `liora_tools.messaging.templates` (e.g. `zocdoc_new_patient`)
- Unknown route → `escalate_to_staff`, never free-form send
- AI draft (if allowed + PHI-safe) never auto-sends; staff escalation only

## PHI

Logs use `summarize_for_log` / `mask_phone` only. Example log shape:

```text
route=zocdoc_new_patient status=dry_run phone_masked=***4567 template_id=00914ffc-…
```

Do not put full phone, name, email, DOB, MRN, or SMS body in ops logs or docs.

## Idempotency

File store default: `~/.liora/state/messaging-outbound-idempotency.json`  
Key material prefers `message_id` / `thread_id` / `correlation_id`; phone hashed only as last resort.
