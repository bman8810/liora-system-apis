# Decision: P3 billing read-only voice surface — ship vs defer

**Card:** `t_3b0abca1`  
**Parent epic:** `t_a5a9f1ba` (P3 Genie billing + call-intel) · backlog epic `t_7a6ca785`  
**Date:** 2026-07-28  
**Inputs:**  
- Surface map — `discovery/billing-readonly-surfaces.md` (`t_2f658a9e`)  
- Voice-safe design — `discovery/billing-voice-safe-patterns.md` (`t_1329aa10`)  
- Call Intel / ASR — `DECISION-call-intel-asr-refresh-t_90aaf48d.md` (`t_90aaf48d`)

**Scope of this decision:** whether to **implement** Genie read-only billing tools next. Not live charge. Not PCI capture. Not Call Intel import as a gate.

---

## Decision line

| Track | Decision | One-line why |
|-------|----------|--------------|
| **Billing voice surface (read-only)** | **SHIP** | EMA balance path is proven; design + PCI deny-list are done; FD calls ask “what do I owe?” often; pay-path gaps already fall to existing `transfer_to_staff`. |
| **Live charge / card-on-call / invoice create / TTP POST** | **NEVER in this phase (hard defer)** | Out of scope; PCI. No payment POST tools. |
| **Call Intel import** | **DEFER** (reaffirm `t_90aaf48d`) | Tenant has null insights / `AI_CALL_CREDIT_STATUS_NOT_APPLICABLE` on 1500 records; no JSON import path. Do not gate P3 or tool ship. |
| **ASR / label refresh (next ~100)** | **Optional, non-blocking** | When deliberately cutting a new sample: `tiny.en` → `small.en` + LLM multi-label. Not a prerequisite for billing tools. |

**Primary recommendation: ship the read-only billing surface now. Defer Call Intel. No live charge. No PCI-sensitive voice capture.**

---

## Billing surface readiness

### Ready (green)

| Capability | Evidence |
|------------|----------|
| Open balance (amount due) | EMA `GET /ema/ws/v3/charges?where=patient=={id}` → sum `patientResponsibleBalance` (>0, `CHARGED`). Live GET verified. |
| Visit balance / copay | EMA `GET …/appointments-finance-info` (scheduler-style `where`). Live GET verified. |
| PCI field inventory | Map + design: PAN/CVV/last4/brand/cardholder/confirmationCode/bank/PDF/SSN-TIN deny list explicit. |
| Voice contracts | Design proposes `get_patient_balance`, `get_weave_pay_link`, optional `get_visit_finance` — JSON + `speak`, ID gate phone+DOB, strip PCI before model. |
| Refusal / complex path | Design matrix + existing `transfer_to_staff` (reason=billing) + `strip_pan_like` already in ops tools. |
| Identity gate | Same as scheduling: outbound dialed phone + DOB; inbound CID + DOB. No billing tools before match. |

### Weak / residual (amber — ship anyway with fallbacks)

| Gap | Impact | Mitigation in ship |
|-----|--------|--------------------|
| Weave pay link inventory | Practice has **3 invoices, all PAID**. Open AR SoT = **EMA**, not Weave. Expect `has_pay_link=false` often. | Speak balance; pay path → transfer / “billing can text a link”; never invent URL. |
| ModMed QuickPay public URL | Still unknown (waiting on practice). | Prompt: no static pay-page claim; portal/billing only. |
| Weave person ↔ EMA patient crosswalk | Not validated. | `get_weave_pay_link` may miss; fallback transfer — do not block balance ship. |
| Speaking long URLs | Poor phone UX. | Prefer staff/SMS delivery language; only speak URL if tool `found` and caller insists. |
| EMA session mid-call | 302 / lag. | `session_expired` speak + transfer; never invent $0. |
| Caller speaks PAN unsolicited | ASR may capture digits. | Prompt interrupt + never echo; `strip_pan_like` on notes/summaries. |

### Not ready (red — do not ship)

| Path | Status |
|------|--------|
| Card capture / take payment on call | Forbidden |
| Weave invoice create / text-to-pay POST | Write + product/PCI review — out of scope |
| EMA payment post / refund / statement PDF bytes | Out of scope |
| FHIR financial resources | 403 without ModMed API Portal OAuth |
| “I charged your card” / “I texted a pay link” without confirmed write tools | Prompt forbid (no such tools today) |

---

## What “ship” means (implementation envelope)

Implement **only** the design in `billing-voice-safe-patterns.md` §3–6:

1. **`get_patient_balance`** — EMA charges aggregate; primary P3 tool.  
2. **`get_weave_pay_link`** — GET search only; strip entire `payment*` trees; usually `none` at Liora today — still ship for future unpaid invoices.  
3. **`get_visit_finance`** — optional same PR or immediate follow-up; visit-day only.  
4. Prompt addendum (design §3.5) on scheduling/ops system instructions.  
5. Unit tests: zero/open balance, mock Weave strip last4, missing `patient_id`, no payment keys in tool JSON, PAN strip on free text.  
6. Wire after existing patient match; reuse `transfer_to_staff` for disputes/refunds/plans/card-on-file/no-link pay.

**Explicit non-goals of ship:**

- No `take_card_payment`, `create_invoice`, `send_text_to_pay`, `read_card_on_file_last4`, `get_statement_pdf`, `post_ema_payment`.  
- No live charge.  
- No PCI-sensitive voice capture or speech of brand+last4 / confirmation codes.

Suggested code home (from design): `voice_agent/billing_tools.py` (or ops_tools section) + schemas next to ops defs.

---

## Why ship now (not defer implementation)

1. **Exploration is complete.** Map + design closed the P3 discovery loop; holding implementation only delays a high-frequency FD intent.  
2. **Balance alone is valuable.** Even when pay link is missing (typical today), “I see $X on your account — want billing?” beats inventing amounts or dead air.  
3. **Risk is bounded.** Read-only GETs already exercised; tools return redacted summaries; charge path is design-forbidden and testable.  
4. **Handoff already exists.** Warm `transfer_to_staff` landed; billing complex cases have a path without building write payments.  
5. **Call Intel / ASR do not block.** Prior decision: defer Call Intel; optional ASR only on next sample cut. Billing safety does not need a new labeled batch.  
6. **P2 ops stack is in place** (labs, forms, late, FAQ, insurance, transfer) — billing is the natural read-only companion for money questions already co-mentioned with those intents.

---

## Why not “defer until QuickPay / unpaid Weave”

Deferring for a better pay path would optimize the less common leg (auto pay URL) while withholding the ready leg (EMA amount due). Design already treats no-link as the **common** Liora path and routes to staff. Waiting on practice QuickPay or AR process change is unbounded and not required for safe ship.

If product later wants **SMS pay link without human**, that is a **separate** confirmed-write card (Weave TTP or ModMed eStatement send) — not a reason to hold read-only balance.

---

## Call-intel / ASR stance (folded in)

Reaffirm `t_90aaf48d`:

| Item | Stance |
|------|--------|
| Call Intel import | **Defer.** No payload on tenant; entitlement/plan blocked. |
| Gate tool ship on Call Intel? | **No.** |
| Next ~100 sample | Optional: faster-whisper **`small.en`** + LLM constrained multi-label + 15–20 human spot-check. |
| Gate P3 billing on ASR refresh? | **No.** |

Continuous training loop stays **local recording → better ASR/labels when we choose**, not vendor Call Intel until Weave UI shows real insights on this location.

---

## Residual risks after ship

| Risk | Severity | Mitigation |
|------|----------|------------|
| Wrong/stale balance if charges pagination incomplete | Med | Always `patient==`; page until empty; unit + lab smoke on known chart |
| Patient hears balance before full ID | High | Hard gate tools on matched patient_id |
| Model invents pay URL | High | Prompt + tool-only URLs; tests |
| PAN in transcript / staff note | High | Never echo; strip_pan_like; refuse card-on-call script |
| Over-transfer when balance is enough | Low | Prompt: answer amount first, then offer pay path |
| EMA cookie death mid-call | Med | Clear status + transfer; Kernel reauth ops unchanged |

---

## Follow-ups (do **not** expand this decision card)

| Priority | Work | Notes |
|----------|------|--------|
| **Next (implement)** | Code + tests for billing tools per design | Child/implement card under epic when this decision lands |
| Soon | Lab smoke: verified chart balance speak + no-link → transfer | Barric allowlist / writes still off |
| When practice provides | Static ModMed QuickPay URL in config/speak | Unblocks “pay online at …” without Weave invoice |
| When needed | Weave↔EMA person crosswalk validation | Improves pay-link hit rate |
| Separate product | Confirmed SMS pay-link send (write) | PCI/product review; not P3 |
| Never this phase | Card capture / payment POST | Hard line |
| Optional training | ASR small.en + LLM labels on next sample100 | Non-blocking |
| Blocked external | Call Intel when Weave SKU shows insights | See `t_90aaf48d` steps |

---

## Acceptance checklist (this card)

| Criterion | Met |
|-----------|-----|
| Written ship vs defer with clear why | **SHIP** read-only billing; **DEFER** Call Intel; **NEVER** live charge/PCI capture |
| Billing surface readiness called out | Green balance/finance-info; amber pay-link/QuickPay/crosswalk; red writes |
| Call-intel/ASR choice mentioned | Defer Intel; optional ASR on next sample; no gate |
| Reaffirm no live charge | Yes — entire § “Not ready” + non-goals |
| Reaffirm no PCI-sensitive voice capture | Yes — deny list + no card tools |

---

## One-paragraph handoff for epic `t_a5a9f1ba`

P3 discovery is closed: EMA is SoT for open balance; Weave pay links are retrieve-only and usually empty at Liora; voice-safe patterns and PCI never-lists are written. **Ship** read-only Genie tools (`get_patient_balance`, `get_weave_pay_link`, optional visit finance) with phone+DOB gate, PCI strip, and transfer-to-staff for pay/dispute — **no** charge, invoice create, or card capture. **Defer** Call Intel import (tenant N/A); optional ASR upgrade only when cutting the next labeled sample. Implementation is the remaining epic checkbox after this decision.
