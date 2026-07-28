# Weave inbound classify + route

Card: `t_e8bdd62d` · package `liora_tools.messaging`

## Purpose

Pure deterministic classifier over normalized inbound Weave SMS. Feeds the
Phase 2 messaging worker (poll → **classify** → template outbound / escalate).

## API

```python
from liora_tools.messaging import (
    NormalizedInboundMessage,
    classify_inbound,
    decision_log_dict,
    ROUTE_ZOCDOC_NP,
    ROUTE_SCHEDULE,
    ROUTE_REFILL,
    ROUTE_ESCALATE,
)

msg = NormalizedInboundMessage(
    message_id="…",
    thread_id="…",
    body=patient_text,  # classify only — never log
    prior_outbound_fingerprints=("booking cost of $100",),  # optional NP prior
)
decision = classify_inbound(msg)
# decision.route_key, .confidence, .reason, .handler_hint, .staff_escalation_required
log = decision_log_dict(decision, msg)  # PHI-safe: ids + body_len/fp, no body
```

Also accepts raw `str` body or a mapping with `body` / `text`.

## Routes (extensible map `DEFAULT_ROUTES`)

| `route_key` | Handler hint | Notes |
|-------------|--------------|--------|
| `zocdoc_np` | `handler.zocdoc_np_reply` | Portal / registration / $100 / Zocdoc language; NP outbound fingerprint soft prior |
| `schedule` | `handler.schedule_question` | Book / reschedule / cancel / availability |
| `refill` | `handler.refill_triage` | Rx + product refill keywords |
| `escalate_to_staff` | `handler.staff_queue` | **Safe default** — unknown, ack-only, multi-intent, explicit human/callback/urgent |

## Policy

1. **Rules first** — regex keyword map; no AI in v1 (AI must never be the sole gate against escalation).
2. **Low confidence / no match → escalate** (`MIN_ROUTE_CONFIDENCE = 0.55`).
3. **Multi-intent** (two+ strong non-escalate routes) → escalate with `secondary_routes`.
4. **PHI** — never put `body` in logs/Telegram/GB; use `decision_log_dict`.
5. Aligns with Genie NP template fingerprint `booking cost of $100` (`zocdoc_new_booking.SMS_FINGERPRINT`).

## Tests

```bash
.venv/bin/python -m pytest tests/test_inbound_classify.py -q
```

Table-driven fixtures cover NP, schedule, refill, escalate, multi-intent, PHI log scrub. No live Weave.

## Downstream

- Inbound poll module emits `NormalizedInboundMessage`.
- Outbound sender keys templates off `route_key` / `handler_hint`.
- Worker loop (`t_facec990`) composes poll → classify → send/escalate.
