# Rollup: P3 Genie billing surface + call-intel refresh

**Card:** `t_a5a9f1ba`  
**Date:** 2026-07-28  
**Epic:** `t_7a6ca785`  
**Children:** `t_2f658a9e` · `t_1329aa10` · `t_90aaf48d` · `t_3b0abca1`

## Outcomes (all closed)

| Outcome | Result | Artifact |
|---------|--------|----------|
| Map EMA/Weave balance / statements / pay link | **Done** | `discovery/billing-readonly-surfaces.md` |
| Voice-safe billing answers (amount + link only; no PAN) | **Done** | `discovery/billing-voice-safe-patterns.md` |
| Optional Call Intel / better ASR for next sample | **Defer Call Intel**; optional ASR on next ~100 only | attachments `t_90aaf48d` |
| Decision ship vs defer | **SHIP** read-only billing tools; **DEFER** Call Intel; **NEVER** live charge/PCI | `discovery/DECISION-p3-billing-ship-vs-defer.md` |

## Decision (card result)

**SHIP** Genie read-only billing voice surface next:

- `get_patient_balance` (EMA charges → sum `patientResponsibleBalance`)
- `get_weave_pay_link` (GET invoices only; often none at Liora today)
- optional `get_visit_finance` (appointments-finance-info)

**DEFER** Weave Call Intel import (tenant: null insights / `AI_CALL_CREDIT_STATUS_NOT_APPLICABLE` on 1500 records).

**Optional non-blocking:** on next deliberate ~100-call sample cut, upgrade faster-whisper `tiny.en` → `small.en` + LLM multi-label. Does **not** gate billing tools.

**NEVER this phase:** live charge, card capture, invoice create, text-to-pay POST, statement PDF bytes, speech of brand+last4 / confirmation codes / PAN/CVV.

## Readiness snapshot

| Leg | Status |
|-----|--------|
| EMA open balance | Green (live GET proven) |
| Visit finance-info | Green |
| Weave pay link | Amber (3 invoices, all PAID; expect no link → transfer) |
| ModMed QuickPay URL | Missing (practice) |
| Weave↔EMA person crosswalk | Unvalidated |
| Payment writes | Red / out of scope |

## PCI / voice tools verification (this card)

- Design + map list explicit deny fields and forbidden tools.
- Current `voice_agent/`: **no** `billing_tools.py`; **no** charge/card/payment POST tools found.
- Existing safeguards: `strip_pan_like` in `ops_tools.py`; `transfer_to_staff` for billing handoff.
- Implementation must keep: phone+DOB identity gate, strip `payment*` trees before model, tool-only pay URLs, never echo caller-spoken PAN.

## Follow-up (not done here)

Implement code + unit tests per design §3–6 → child card under this rollup / epic. Lab smoke on allowlisted chart after ship. Confirmed SMS pay-link send = separate product/PCI card later.

## One line

P3 discovery closed: ship read-only balance + optional pay-link tools; defer Call Intel; no PCI capture or live charge.
