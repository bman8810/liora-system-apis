# EMA / Weave billing read-only surfaces (Genie voice)

**Card:** `t_2f658a9e`  
**Date:** 2026-07-28  
**Scope:** Inventory only. **No charge, refund, invoice-create, text-to-pay send, or card-capture paths exercised.**  
**Auth used:** Weave JWT (`weave_token.json`); EMA session cookies via Kernel Liora Managed Auth (`modmedapp.com`) → `~/.liora/credentials/ema_cookies.json` with `base_url=https://lioraderm.modmedapp.com`.

---

## Executive map

| Need | Best source today | Read-only? | Voice-safe payload | Notes |
|------|-------------------|------------|--------------------|--------|
| **Balance (amount due)** | **EMA** `GET /ema/ws/v3/charges?where=patient=={id}` → sum `patientResponsibleBalance` where > 0 | Yes (GET) | Dollar amount + optional charge labels (e.g. “no-show fee”) | No single “account balance” field on patient; aggregate open charge balances. |
| **Appt-day balance / copay** | **EMA** `GET /ema/ws/v3/scheduler/appointments-finance-info` | Yes (GET) | `balance`, `paidCopay` per `appointmentId` | Needs scheduler-style `where` (facility + date window). Not a full AR ledger. |
| **Statement summary** | **EMA** charges list (+ optional claim status); patient `statementNumber` selector documented but **null** in live samples | Partial | Count of open items, total due, service dates, CPT/desc | No staff WS “eStatement PDF/list” endpoint found. Paper/portal statements = ModMed Pay product surface, not reverse-engineered here. |
| **Pay link (sendable URL)** | **Weave** invoice `uniqueLink` → `https://app.getweave.com/pay/{uniqueLink}` | Retrieve = yes; **create/send = write** | URL + amount due only | Practice has **3 historical Weave invoices, all PAID**. Online bill pay **enabled**. Creating invoices / TTP is POST — out of scope. |
| **Pay link (ModMed)** | ModMed Pay QuickPay / eStatements / Patient Portal (product) | N/A — **no staff API URL discovered** | Static QuickPay URL once Jenny provides it | Genie memory (2026-02): ModMed Pay active; QuickPay website URL still **waiting on practice**. |

**Voice Genie recommendation (read path):** prefer **EMA open-balance aggregate** for “what do I owe?”; offer **Weave pay URL only if an active unpaid invoice exists** for that person; otherwise transfer billing or SMS a human-generated link. Do **not** collect or read back card data on the call.

---

## 1. Weave Payments

### Auth
- Base: `https://api.weaveconnect.com`
- Headers: `Authorization: Bearer <localStorage.token JWT>`, `Location-Id: <location UUID>`
- Location (Liora): `d8508d79-c71c-4678-b139-eaedb19c2159`
- Token lifetime ~hours; refresh via Kernel Liora profile → `app.getweave.com`

### Surfaces probed (GET only)

| Method | Path | Status | Latency (lab) | Role |
|--------|------|--------|---------------|------|
| GET | `/payments/views/service/locations/{locationId}/feature-settings` | 200 | ~80ms | Feature flags |
| GET | `/payments/v1/search/invoices` | 200 | ~50–60ms | Search invoices (`limit`, `skip`, `locationIds`, `personid`, `status`, `active`) |
| GET | `/payments/v1/invoices` | 200 | ~50ms | List invoices |
| GET | `/payments/v1/invoices/{id}` | 200 | ~50ms | Invoice detail |
| GET | `/payments/requests/{id}/filesessions` | 200 | — | Attachment bytes (PDF statement/receipt) |
| GET | `/payments/requests/{id}` or `/{uniqueLink}` | **410** | — | Gone for paid samples |
| POST/PUT/PATCH/DELETE | (CORS allow on invoices) | **not called** | — | **Write / charge path — do not use in voice tools** |

Public pay page (no staff auth): `https://app.getweave.com/pay/{uniqueLink}` → **200** (SPA). Amount is not in initial HTML (client fetch after load).

### Feature settings (live)
```
onlineBillPayEnabled: true
ttpAuthEnabled: true          # text-to-pay auth flag
autoReceiptEnabled: true
surchargingEnabled: false
onlineBillPayPatientBirthdateEnabled: false
onlineBillPayPatientIdentifierEnabled: false
*WritebacksEnabled: all false for dental/PM bridges (EMA not in list)
```

### Invoice fields (staff API)

| Field | Type | Notes |
|-------|------|--------|
| `id` | UUID | Invoice id |
| `billedAmount` | int | **Cents** (12586 → $125.86) |
| `status` | string | Live sample set: only `PAID` (3 invoices total) |
| `isActive` | bool | `false` when paid |
| `uniqueLink` | string | Short token for public pay URL |
| `billedAt` | ISO time | |
| `hasAttachment` | bool | PDF via filesessions |
| `person.id` / `pmid` / `name` / `mobilePhone` / `emailAddress` / `birthdate` | | Person join |
| `payment` / `payments[]` | object | Present when paid — **PCI-sensitive subtree** |
| `payment.paymentDetails.lastFour` | string | Card last4 |
| `payment.paymentDetails.brand` | string | e.g. AMEX |
| `payment.paymentDetails.cardholderName` | string | |
| `payment.confirmationCode` | string | Stripe-style `ch_…` |
| `payment.paymentType` | string | e.g. `CARD` |
| `payment.processorType` | string | e.g. `STRIPE_PROCESSOR` |
| `links.payment` | URL | API path under `/payments/requests/{uniqueLink}` |
| `links.attachment` | URL | filesessions |
| summary (search) | | `gross`, `fees`, `net`, `refunds`, `invoicesCount` (location rollup) |

**Practice snapshot:** `invoicesCount=3`, all `PAID`, no `UNPAID`/`OPEN` rows. Weave is **not** currently the live AR system of record for open patient balances (EMA is).

### UI routes (app host, not REST)
From Weave SPA bundle: `/payments/invoices`, `/payments/online-bill-pay`, `/payments/payment-plans`, `/payments/refunds`, `/payments/payouts`, settings/*, terminals — staff UI only.

### Weave: what Genie should expose
- **OK to read:** unpaid/active invoice → `billedAmount/100`, `status`, `https://app.getweave.com/pay/{uniqueLink}`, billed date.
- **OK to filter:** `personid` after Weave person lookup (phone/name).
- **Do not expose in voice/logs:** entire `payment` / `payments` / `paymentDetails`, last4, brand, cardholderName, confirmationCode, bank fields, attachment PDF bytes, raw email if avoidable.
- **Do not call:** invoice create, charge, refund, terminal, payment-plan create, or any POST under `/payments/*`.

---

## 2. EMA / ModMed (staff WS)

### Auth
- Base: **`https://lioraderm.modmedapp.com`** (not `*.ema.md` — mid-call JSON errors if wrong host)
- Session cookie `JSESSIONID` + ALB + Keycloak SSO cookies
- Refresh: Kernel project **Liora** / Managed Auth domain **modmedapp.com** (credential `liora-ema-modmed`)
- All probes: GET + `allow_redirects=False`; 302 = dead session

### Balance / finance surfaces

| Method | Path | Status | Latency | Role |
|--------|------|--------|---------|------|
| GET | `/ema/ws/v3/charges` | 200 | ~100–850ms/page | Charge lines + **`patientResponsibleBalance`** |
| GET | `/ema/ws/v3/charges/{id}` | 200 | ~80ms | Single charge (+ nested `patient`, `adjustments` via selector) |
| GET | `/ema/ws/v3/scheduler/appointments-finance-info` | 200* | ~170–350ms | Per-appointment `balance`, `paidCopay` |
| GET | `/ema/ws/v3/paymentMethods` | 200 | ~200ms | Method catalog (Cash, Card, CareCredit, Papaya, ALLE, Cherry, …) — **config, not patient instruments** |
| GET | `/ema/ws/v3/claims` | 200 | ~300ms | Claim headers (insurance); weak patient $ without richer selector |
| GET | `/ema/ws/v3/patients` + `statementNumber` | 200 | ~100ms | Field listed in discovery selector; **live values null** on samples |
| GET | `/ema/fhir/r4/*` ChargeItem/Account/… | **403** | — | Needs ModMed API Portal OAuth — not staff session |
| POST | payment / charge posting | **not called** | — | Write / PCI path |

\* `appointments-finance-info` returns **500** with simple `scheduledStartDateLd` filters; works with **scheduler-style** `where` including `facility=in=("2040")` and `scheduledStartDate`/`End` GMT strings (same family as `scheduler/appointments`).

### Charge schema (voice-relevant)

| Field | Example shape | Voice? |
|-------|---------------|--------|
| `id` | number | internal only |
| `patientResponsibleBalance` | `150.00` (dollars, decimal) | **Yes — primary due amount** |
| `actualAmount` / `originalAmount` | dollars | optional context |
| `status` | `CHARGED`, `CANCELED`, … | yes (filter to open) |
| `resolved` | bool | yes (`false` + bal>0 ≈ open) |
| `description` | CPT or “NO SHOW FEE MEDICAL” | careful — OK high-level |
| `serviceDateLd` / `postDateLd` / `chargeCreatedDate` | dates | yes (dates only) |
| `patient` (via selector) | id, name, mrn, dob, … | id for join; **don’t speak MRN/DOB back** beyond verify flow |
| `taxAmount` / rates | | usually skip |
| `adjustments` | array on detail | skip unless needed |
| `units` | | skip |

**Open balance recipe (read-only):**
```
GET /ema/ws/v3/charges
  ?where=patient=={emaPatientId}
  &selector=id,patient(id),patientResponsibleBalance,actualAmount,status,description,resolved,serviceDateLd
  &paging.pageSize=100
  &paging.pageNumber=1..N

amount_due = sum(patientResponsibleBalance for row if balance > 0 and status == "CHARGED")
```
Verified pattern: `where=patient=={id}` works (same idiom as appointments). Client-side sum required — no `patientResponsibleBalance>0` server filter (500).

### appointments-finance-info schema
```json
{
  "appointmentId": 123,
  "balance": 0.0,
  "paidCopay": 0,
  "firmHasSingleBusinessUnit": true
}
```
Use for check-in / same-day “balance on this visit,” not full statement history. Negative balances observed (credits).

### Not found / blocked (staff session)
- `/ema/ws/v3/payments`, `/patientPayments`, `/statements`, `/patientStatements`, `/balances`, `/ledger`, `/invoices`, `/quick-pay`, `/modmedPay/*` → 404 or empty
- `/ema/ws/v3/paymentBatch` → **401** (permission)
- Featureflag probes `MODMED_PAY`, `QUICK_PAY`, etc. → empty `[]` (not informative)
- FHIR financial resources → **403** without portal OAuth
- Static QuickPay path on firm host → not publicly mapped (`/quickpay` 404)

### ModMed Pay (product — outside staff WS map)
From ModMed public docs (not live-integrated here):
- **Patient Portal** — view statements / pay  
- **Online QuickPay** — practice website link (Genie still waiting on URL from practice)  
- **eStatements / text-to-pay** — staff-sent SMS payment links  
- In-office terminal / card-on-file / autopay  

Staff-initiated “send pay link” is almost certainly a **write** UX path; treat as human/tooling phase-2, not voice auto-send without explicit product decision.

---

## 3. PCI / never-speak / never-log (voice tools)

**Hard deny (do not request, store in transcripts, TTS, Telegram, or tool JSON returned to the model beyond redacted summaries):**

| Class | Examples seen or likely |
|-------|-------------------------|
| Full PAN | never present in these GETs; still forbid any card-entry tool |
| CVV / CVC / PIN | forbid |
| Track/mag data | forbid |
| Bank account / routing full numbers | Weave `paymentDetails.bank*` fields |
| Card last4 + brand together in speech | Weave `lastFour`, `brand` — **log redaction required even if API returns them** |
| Cardholder name on tender | `cardholderName` |
| Processor charge ids | `confirmationCode` (`ch_…`), Stripe ids |
| Payment attachment PDFs | may contain statement line items + identifiers |
| Guarantor SSN / TIN | `/taxpayerIdentificationNumber/*` exists in EMA ref — do not call from voice |
| Full MRN + full DOB readback | verify privately; don’t recite |
| Stored payment method tokens | if discovered later under paymentMethods patient APIs |

**Safe to speak (after patient verify):**
- Total amount due (currency, two decimals)
- High-level reason (“outstanding no-show fee”, “balance from your last visit”) without full claim dumps
- Pay-by-link URL or “I’ll text you a pay link” (link generation may be human/Weave write — separate gate)
- Appointment copay / visit balance from finance-info when relevant

**Safe to log (structured tool result):**
- `amount_due`, `currency=USD`, `open_charge_count`, `as_of` timestamp  
- `has_pay_link` bool + hostname only, or full pay URL if product insists (prefer short link, no query secrets)  
- EMA `patient_id` internal, Weave `person_id` internal  
- **Strip** nested `payment`, emails, phones from tool output unless required for SMS send path (SMS send = separate confirmed write)

---

## 4. Latency / reliability notes

| Call | Typical | Risk |
|------|---------|------|
| Weave invoice search | &lt;100ms | Token expiry → 401 |
| EMA charges by patient | ~100–300ms | Session 302; paginate if &gt;100 lines |
| EMA finance-info week window | ~200–400ms | Wrong `where` → 500 |
| EMA charges full scan | hundreds ms × pages | Don’t full-scan in voice path — always `patient==` |

---

## 5. Gaps / follow-ups (out of this card)

1. **ModMed QuickPay public URL** — still unknown; needed for static “pay online at …” without Weave invoice.  
2. **Staff API to generate ModMed eStatement / text-to-pay link** — not reverse-engineered (likely UI + write).  
3. **Weave invoice create + TTP send** — OPTIONS allows POST; **do not implement** until PCI + product review.  
4. **FHIR ChargeItem/Account** — clean official read API if practice enables ModMed API Portal OAuth.  
5. **Crosswalk Weave `person.pmid` ↔ EMA patient id** — needed before auto pay-link match; not validated here.  
6. Parent card `t_a5a9f1ba` still owns voice UX design + ship/defer decision.

---

## 6. Explicit non-actions this run

- No POST/PUT/PATCH/DELETE to Weave `/payments/*`  
- No EMA payment posting, refund, statement batch, or portal pay  
- No card capture, terminal, or Stripe Checkout interaction  
- No patient-identifying dumps committed; this file uses field names and aggregates only  

---

## 7. Suggested Genie tool shapes (design only)

```text
get_patient_balance(patient_id) -> { amount_due, currency, open_items: [{description, amount, service_date}], source: "ema_charges" }
get_visit_finance(appointment_id) -> { balance, paid_copay, source: "ema_finance_info" }
get_weave_pay_link(person_id) -> { found, amount_due?, pay_url?, status? }  # GET search only; empty if none active
```

All three: read-only; strip PCI subtrees; require existing voice ID gate (phone+DOB).
