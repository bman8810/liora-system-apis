# Voice-safe billing answer patterns (Genie)

**Card:** `t_1329aa10`  
**Date:** 2026-07-28  
**Depends on:** `discovery/billing-readonly-surfaces.md` (`t_2f658a9e`)  
**Scope:** Design only — answer templates, tool contracts, PCI deny list, refusal paths. **No code ship, no charge, no card capture.**

---

## 1. Product rule (one line)

After patient ID (phone + DOB), Genie may speak **amount due** and offer a **pay path** (existing unpaid Weave link if any; otherwise staff/SMS/transfer). Genie **never** takes, repeats, or logs card/bank data.

---

## 2. Surfaces → what voice may use

| Caller ask | Source (map) | Voice payload | Do not |
|------------|--------------|---------------|--------|
| “What do I owe?” / balance | EMA `GET /charges?where=patient=={id}` → sum `patientResponsibleBalance` (>0, `CHARGED`) | `amount_due`, `currency=USD`, optional high-level open item labels + service dates | Full ledger dump, claim EDI, adjustments dump |
| “What’s on my statement?” | Same charges list (no eStatement API) | Open **count**, total due, 1–3 plain-language lines (desc + $ + date) | PDF bytes, line-item CPT laundry list unless asked for “details” then cap at 3 + transfer |
| “How do I pay?” / pay link | Weave unpaid invoice `uniqueLink` → `https://app.getweave.com/pay/{uniqueLink}` **if found**; else staff path | `has_pay_link`, optional `pay_url` + invoice amount; or transfer/SMS handoff speak | Create invoice, TTP POST, card-over-phone |
| Visit-day balance / copay | EMA `appointments-finance-info` | `balance`, `paid_copay` for that visit | Treat as full AR |
| “Pay with card now” | **None** (out of scope) | Refuse + offer link/staff | Any PAN/CVV capture tool |

**Identity gate (required):** same as scheduling — outbound dialed phone + DOB; inbound caller ID + DOB. No balance/pay tools before match. Do not speak MRN or full DOB back after verify.

**Weave ↔ EMA person crosswalk:** not validated. Until it is, `get_weave_pay_link` may miss unpaid invoices; fallback = transfer / staff SMS link, not invent URL.

**Practice reality (2026-07-28 map):** Weave has only 3 invoices, all `PAID`. Open AR SoT = **EMA charges**. Expect `has_pay_link=false` often.

---

## 3. Proposed tools (read-only; design contract)

Mirror ops tools: JSON + `speak` / `message`; strip PCI before model; no POST payments.

### 3.1 `get_patient_balance`

```text
args: { patient_id }   # required; after lookup_patient
out:
  status: ok | zero_balance | lookup_failed | patient_id_required | session_expired
  amount_due: number     # dollars, 2dp; 0 if none
  currency: "USD"
  open_charge_count: int
  open_items: [ { description?, amount, service_date? } ]  # max 5; high-level desc only
  as_of: ISO-8601
  source: "ema_charges"
  speak: <template §4>
```

- Selector limited to non-PCI charge fields (map §2).  
- Never return nested patient DOB/MRN/SSN in tool JSON.  
- Paginate charges client-side; always `patient==`.

### 3.2 `get_visit_finance` (optional P3+)

```text
args: { appointment_id }  # or patient_id + matched upcoming appt
out:
  status: ok | not_found | lookup_failed
  balance, paid_copay
  source: "ema_finance_info"
  speak: <template §4>
```

Use only when caller asks about **today’s visit / check-in balance**, not full statement.

### 3.3 `get_weave_pay_link`

```text
args: { weave_person_id? } or { phone? } after Weave person resolve
out:
  status: found | none | lookup_failed | person_unresolved
  found: bool
  amount_due?: number   # cents→dollars from billedAmount
  currency?: "USD"
  pay_url?: "https://app.getweave.com/pay/{uniqueLink}"
  status_invoice?: "UNPAID" | ...   # only active unpaid
  speak: <template §4>
```

- **GET search only.** Strip entire `payment` / `payments` / `paymentDetails` trees before JSON to model.  
- Prefer returning `pay_url` host+path only (no secrets in query today).  
- If `none`: do **not** POST create invoice; escalate per §5.

### 3.4 Explicitly **not** proposed

| Tool / behavior | Why |
|-----------------|-----|
| `take_card_payment` / collect PAN/CVV/expiry | PCI-DSS; out of scope |
| `create_invoice` / `send_text_to_pay` | Weave POST write; product+PCI review |
| `read_payment_method_on_file` (card brand+last4) | Speech/log of brand+last4 deny |
| `get_statement_pdf` / attachment bytes | PHI + identifiers; not voice |
| `post_ema_payment` / refunds | Charge path |
| Speaking processor `confirmationCode` / `ch_…` | PCI-adjacent / useless on phone |

### 3.5 Prompt addendum (when tools ship)

```text
BILLING (read-only):
- Tools: get_patient_balance, get_weave_pay_link [, get_visit_finance].
- After ID only. Speak tool "speak" lines; never invent balances or pay URLs.
- Safe to say: amount due, short item labels, how to pay (link or staff).
- NEVER ask for or repeat card numbers, CVV, expiry, bank/routing, last four,
  card brand+last4, cardholder name on tender, or confirmation codes.
- If they want to pay by card on this call → refuse; offer pay link or transfer_to_staff.
- No pay link / complex dispute / refund / collections → transfer_to_staff
  (reason=billing, active_intents includes billing).
- Do not claim you charged a card or sent a pay text unless a confirmed write tool exists
  (none today).
```

---

## 4. Speak templates (examples)

Style: short phone turns; dollars as “one hundred fifty dollars” or “$150” consistently; use tool `speak` as source of truth.

### 4.1 Balance / amount due

| Case | `speak` (agent says this) |
|------|---------------------------|
| Open balance | “I see a balance of {amount_speak} on your account.” |
| Zero | “I don’t see an open balance on your account right now.” |
| Open + one clear label | “I see {amount_speak} open — looks like a {description_plain} from {date_speak}.” |
| Open + many items | “I see {amount_speak} total across {n} open charges. I can give a quick breakdown or help with how to pay.” |
| Lookup fail / session | “I can’t pull your balance right this second. I can connect you with billing, or you can try the patient portal.” |
| Not identified | “I need to confirm who I’m speaking with first — date of birth is perfect.” |

**Example (ok):**  
Caller: “How much do I owe?”  
→ `get_patient_balance` →  
Genie: “I see a balance of one hundred fifty dollars on your account. Want options to pay?”

**Example (refuse invent):**  
Tool fail → do not guess $0 or prior call memory as fact.

### 4.2 Statement summary

| Case | `speak` |
|------|---------|
| Summary | “You have {n} open items totaling {amount_speak}. Latest is {desc} on {date} for {item_amount}.” |
| Detail request (cap 3) | “First, {a}. Second, {b}. Third, {c}. For the full statement, billing or the portal is better — want me to connect you?” |
| No eStatement API | Never claim “I emailed your statement” without a real tool. |

### 4.3 How to pay

| Case | `speak` |
|------|---------|
| Unpaid Weave link found | “You can pay online — I can give you the secure pay link. It’s also best if we text it so you can tap it. The amount on that invoice is {amount_speak}.” |
| Link found, voice-only URL | Prefer SMS path when product allows; if speaking URL: slow, chunked hostname+token only if caller insists. Prefer: “I’ll have the team text you the pay link” + `transfer_to_staff` / future confirmed SMS tool. |
| No unpaid Weave invoice (common) | “I don’t see an active online invoice I can send from here. Billing can text a pay link or take care of it — want me to connect you?” |
| Portal / QuickPay (URL unknown) | “You can also pay through the patient portal when that’s set up. I don’t have a public pay page URL on file yet — billing can confirm the best link.” |
| In-office | “You can pay at the front desk at your visit — card and the usual options.” |
| CareCredit / financing catalog | Only high-level if asked: “The office offers several pay options including CareCredit — billing can walk you through those.” Do **not** open applications on the call. |

### 4.4 Visit finance

| Case | `speak` |
|------|---------|
| Visit balance > 0 | “For that visit I’m seeing a balance of {amount_speak}.” |
| Copay already paid | “It looks like a copay of {amount_speak} was already paid for that visit.” |
| Zero | “I’m not seeing a balance on that visit.” |

### 4.5 Card-on-call / PCI pressure

| Caller | `speak` |
|--------|---------|
| “I’ll give you my card number” | “I can’t take card numbers over this line. Safest is the online pay link or our front desk. Want the link path or a person?” |
| Reads PAN unsolicited | Do **not** repeat digits. Interrupt: “Please don’t share the card number here — it won’t go through on this call. Let’s use the pay link or billing.” |
| “What’s the last four on file?” | “I can’t read back card details. Billing can help if you need to update a card on file.” |
| “Did my payment go through? Confirmation code?” | “I can’t see processor confirmation codes from here. If you paid online, check your email receipt, or I can connect billing.” |

### 4.6 Transfer (billing complex)

Use existing `transfer_to_staff`:

```text
reason: "billing — {short}"
call_summary: "Verified patient. Balance spoken: {amount or n/a}. Pay link: {yes/no}. Caller wants: {dispute|refund|payment plan|card on file|...}."
active_intents: ["billing"]
mode: transfer | hold
```

`speak` from tool (already shipped): warm handoff copy — do not claim transfer without tool.

---

## 5. Refusal / fallback matrix

| Condition | Agent action |
|-----------|--------------|
| No `patient_id` | ID flow first; no balance tools |
| EMA 302 / charges fail | Apology + `transfer_to_staff` or callback; no invented $ |
| `amount_due == 0` but caller insists bill | Soft: “I don’t see open charges; billing can double-check” → transfer if push |
| Caller disputes charge / wants write-off | Transfer — Genie does not adjust AR |
| Refund / collections / payment plan setup | Transfer |
| Wants pay link, Weave `none`, no QuickPay URL | Transfer or “billing will text a link” (staff write) — **no auto POST** |
| Wants to pay by card now | Refuse capture (§4.5); link or staff |
| Insurance **eligibility / “am I covered for X”** | Not billing balance tool — existing insurance tool + no eligibility invention; clinical coverage → staff |
| Guarantor / someone else’s bill | Re-verify identity; do not discuss third-party balances without clear policy — default transfer |
| Partial tool data (balance ok, person unresolved for Weave) | Speak balance; pay path via staff |

---

## 6. Never say / never capture / never log

### 6.1 Hard deny (speech, tool args, tool results to model, staff notes, Telegram, WAV side-channel transcripts if exported)

| Class | Examples |
|-------|----------|
| Full PAN | 13–19 digit card numbers; grouped `####-####-####-####` |
| CVV / CVC / PIN | any |
| Expiry as tender data | “exp 09/27” collected for payment |
| Track / magstripe / chip cryptograms | any |
| Bank account + routing (full) | Weave `paymentDetails.bank*` |
| Card **last4** and **brand** in speech | even if API returns them |
| Cardholder name on tender | `cardholderName` |
| Processor ids | `confirmationCode`, `ch_…`, Stripe charge ids |
| Payment PDF / filesession bytes | statement attachments |
| SSN / TIN | EMA taxpayer endpoints — do not call from voice |
| Stored payment method tokens | if discovered later |
| Creating charge / capturing payment | any write payment API |

Reuse `strip_pan_like` (ops_tools) on any free-text billing fields and on `call_summary` before staff queue.

### 6.2 Do not ask the caller for

- Card number, CVV, expiry, ZIP-for-AVS as payment  
- Bank account / routing  
- “Read the numbers on your card”  
- Screenshots of cards  

OK to ask: DOB (verify), preferred callback, whether they want a **texted** pay link (not the card).

### 6.3 Safe to speak (after verify)

- Total **amount due** (USD, two decimals)  
- High-level charge labels (“no-show fee”, “balance from your last visit”)  
- Open item **count** and service **dates**  
- Visit `balance` / `paid_copay`  
- That online bill pay exists; **pay URL only** from tool `found` path  
- “Pay at front desk”; transfer to billing  

### 6.4 Safe structured log / tool JSON

```text
amount_due, currency, open_charge_count, as_of,
has_pay_link, pay_url (or hostname-only),
ema patient_id, weave person_id (internal),
status enums, source tags
```

Strip: `payment`, `payments`, `paymentDetails`, emails/phones unless required for a **separate** confirmed SMS send tool.

---

## 7. Worked call scripts (happy + edge)

### A. Balance + no pay link (typical Liora today)

1. ID: phone + DOB → `lookup_patient`  
2. “What do I owe?” → `get_patient_balance` → speak amount  
3. `get_weave_pay_link` → `none`  
4. Speak: no active online invoice; offer transfer for texted link / front desk  
5. If yes → `transfer_to_staff` (billing)

### B. Balance + Weave unpaid link (future / other tenants)

1. ID → balance → pay link `found`  
2. Speak amount + offer secure link; prefer staff/SMS to deliver URL  
3. Never ask for card on call  

### C. “Here’s my Visa …”

1. Stop collection immediately (§4.5)  
2. Do not read back any digits heard  
3. Route to link or transfer  

### D. Statement detail spiral

1. Cap verbal line items at 3  
2. Offer portal/billing for full statement  
3. Transfer if dispute  

---

## 8. Verification checklist (design)

| Check | Status |
|-------|--------|
| Patterns tied to EMA charges + finance-info + Weave pay URL map | Yes (§2–4) |
| Outputs constrained to amount due + pay path (+ harmless labels/dates) | Yes |
| Explicit never-say/capture list | Yes (§6) |
| Refusal when missing data or PCI would be required | Yes (§4.5, §5) |
| No PCI-sensitive **capture** in proposed tools | Yes — read-only GETs; no card tools (§3.4) |
| No payment POST in design | Yes |
| Weave `payment*` subtree stripped in contract | Yes (§3.3, §6.4) |
| Identity gate before billing tools | Yes (§2) |
| Complex billing → existing `transfer_to_staff` | Yes (§4.6) |
| Implementation deferred to ship decision (`t_3b0abca1`) | Yes — this card is design only |

**Residual risks (for decision card):** EMA session lag mid-call; Weave person crosswalk missing; long charge lists; ASR may pick up caller-spoken PAN → must not echo (prompt + `strip_pan_like` on notes); speaking long URLs is UX-poor → prefer SMS/staff.

---

## 9. Implementation sketch (not this card)

When/if ship:

1. `voice_agent/billing_tools.py` (or ops_tools section): three handlers + PCI strip unit tests (mirror `test_ops_tools` PAN tests).  
2. Wire schemas next to ops tool defs; gate on patient match.  
3. Extend `SYSTEM_INSTRUCTIONS_SCHEDULING` with §3.5.  
4. Tests: zero balance, open balance sum, strip last4 from mock Weave invoice, refuse missing patient_id, no payment keys in JSON.  
5. Do **not** enable invoice create or card tools.

---

## 10. Related

| Doc / code | Role |
|------------|------|
| `discovery/billing-readonly-surfaces.md` | Surface map + field-level PCI |
| `voice_agent/ops_tools.py` `strip_pan_like` | Existing PAN redaction |
| `voice_agent/ops_tools.py` `transfer_to_staff` | Billing complex handoff |
| `voice_agent/ops_tools.py` `get_insurance_on_file` | Not eligibility; separate from AR |
| `voice_agent/config.py` SYSTEM_INSTRUCTIONS_* | Prompt home for §3.5 |
| Parent epic outcomes | `t_a5a9f1ba` ship-vs-defer via `t_3b0abca1` |
