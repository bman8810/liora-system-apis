# Refill triage + 12-month lapse (Genie voice)

## Policy (from Weave FD sample n=110)

1. **Rx refill** — message provider/staff only. Voice never e-prescribes.
2. **~12 months no visit** — refuse remote refill; offer book.
3. **Product/retail** (office shampoo etc.) — inventory/staff message path, not Rx.

## Tools

| Tool | Writes? | Behavior |
|------|---------|----------|
| `check_visit_lapse` | no | Past visits → eligible / lapsed / no_visit_history |
| `request_rx_refill` | yes (message queue) | Lapse gate → confirm → staff message JSONL |
| `request_product_refill` | yes (message queue) | Confirm → inventory message (no lapse gate) |

Gates: `confirmed=true` + `EMA_WRITES_ENABLED=true`. Default writes **off**.

## Queue

- Path: `LIORA_STAFF_MESSAGE_QUEUE` or `$LIORA_CREDENTIALS_DIR/staff_message_queue.jsonl`
- Fallback: `/opt/data/workspace/liora/cache/staff_messages/queue.jsonl`
- Optional EMA hook: `EmaClient.send_staff_message` if implemented later
- Optional Bottle: `LIORA_STAFF_MESSAGE_BOTTLE=1`

Every record sets `erx=false` and `prescription_written=false`.

## Lapse window

- Default **365** days (`LIORA_REFILL_LAPSE_DAYS`)
- Prefers `CHECKED_OUT` / completed statuses; else any non-cancelled past visit

## Tests

```bash
cd liora-system-apis
.venv/bin/pytest tests/test_refill_flow.py -q
```

## Smoke dialogue (writes off)

1. Active patient refill → `needs_confirmation` then `writes_disabled` (no queue)
2. Lapsed → `lapsed` + offer book (no queue)
3. Product → `needs_confirmation` / `writes_disabled`

Allowlisted phones only for live voice.
