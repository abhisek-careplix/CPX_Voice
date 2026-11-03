# End-to-end RCM on the clearinghouse

**Status:** approved direction (owner, 3 Nov 2026). Built after Voice Studio
([VOICE_STUDIO_PLAN.md](VOICE_STUDIO_PLAN.md)); the owner switches to production keys once both are done.
**Goal:** small and medium practices bill entirely through CarePlix: insurance checks, claims, follow-up,
payment posting, denials and patient balances, automated, with voice where electronic transactions stop.

## 1. Principles

1. **Data comes from the integration hub.** Patients, coverage, appointments, charges, claims, payments and
   balances are read from the practice's EHR through the hub (NexHealth now; Practice OS later through the
   same connector contract). CarePlix keeps no separate ledger.
2. **The EHR stays the system of record.** Everything we change there (payments, adjustments, notes,
   documents) is a write intent: proposed, approved per autonomy level, sent once, confirmed.
3. **The clearinghouse is white-labelled.** One vendor-neutral gateway in front of Stedi; the product says
   "Clearinghouse" everywhere. Only payer-signed enrollment forms carry the vendor's legal name (shown as "your
   clearinghouse enrollment form"). The contract must allow white-label use (owner to confirm with Stedi).
4. **Electronic first, voice second.** A transaction answers in seconds for cents; the voice agent calls when
   a payer has no electronic route or the answer is stuck.

## 2. What the clearinghouse covers (verified 3 Nov 2026)

| Capability | Status | Notes |
|---|---|---|
| Eligibility 270/271 (real-time, batch) | ✅ | CMS needs enrollment; never test patients against CMS |
| Insurance discovery, COB | ✅ | COB not for Medicare / Medicare Advantage |
| MBI lookup | ✅ | Always needs CMS enrollment |
| Claims 837P/I/D with edits | ✅ | Medicare MACs need enrollment |
| 277CA acknowledgements | ✅ | Free |
| Claim status 276/277 | ✅ commercial and Medicare Advantage | ❌ **not for traditional Medicare** (no MAC supports it): use 277CA + 835 + voice |
| 835 ERA | ✅ | Enrollment always; one ERA receiver per payer |
| 275 attachments | ✅ per payer | |
| Transaction enrollment, payer directory, webhooks | ✅ API | Drive onboarding from our Studio |
| 278 prior authorization | ⚠️ unconfirmed | Ask the vendor; meanwhile FHIR PAS + voice + portal task |

Not available in the vendor's test mode: claim status, discovery, COB, enrollment, 278. Those are proven in
production with the pilot practice.

## 3. Hub extensions (data in, write-back out)

| Hub resource | NexHealth | OpenEMR (test clinic) | Use |
|---|---|---|---|
| Charges (CPT, ICD-10, modifiers, units, amounts, provider, DOS) | `GET /charges` | billing table via API | Claim build, scrub |
| Claims (EHR-created) | `GET /claims` | — | Claim build, tracking |
| Payments, adjustments | `GET /payments`; `POST /payments`, `POST /adjustments` | — | ERA posting write-back (L3) |
| Guarantor balances | `GET /guarantor_balances` | — | Patient balance outreach |
| Documents | `POST /patients/{id}/documents` | messages | 271 summary, ERA summary, appeal letters |

The connector contract gains `pull_billing(since)` and write kinds `payment_post`, `adjustment_post`,
`document_upload`; the capability catalog decides per EHR what is allowed (some EHRs take no payments).

## 4. Workflows

| # | Workflow | Flow | Voice |
|---|---|---|---|
| R1 | Eligibility autopilot (production) | At booking, 72 h, 24 h, nightly batch → discovery if inactive → COB → MBI lookup for Medicare | Receptionist quotes coverage and copay; calls the patient about a coverage change |
| R2 | Claim build and scrub | Hub charges and claims → 837 with our checks (coverage rules via CMS, code pairs, modifiers, timely filing) + clearinghouse edits | — |
| R3 | Submit and acknowledge | 837 → 999/277CA by webhook → rejection work queue with auto-fix rules | — |
| R4 | Claim follow-up | 276/277 schedule by claim age (commercial, MA); traditional Medicare by 277CA + 835 age | Payer calls for stuck claims; NGS phone line once its terms allow automated assistants |
| R5 | Payment posting | 835 → posting proposals → payments and adjustments written to the EHR via the hub (L3, batch approval); underpayment flags against the EHR fee schedule | — |
| R6 | Denials and appeals | CARC/RARC classification → appeal draft (L1) → 275 attachment from EHR documents → deadline tracking | Payer calls to check appeals |
| R7 | Patient balances | Hub guarantor balances → reminders, payment-plan requests | Balance calls and SMS (campaigns) |
| R8 | Prior authorization | 271 "auth required" flag → PA case → FHIR PAS where offered, else voice and a portal task | Payer calls (built, with hold mode) |
| R9 | Reporting | A/R aging, days to payment, clean-claim and denial rates per payer, monthly practice report | — |

## 5. Clearinghouse administration (Studio → Admin → Clearinghouse)

Credentials and environment (Secret Manager), payer directory (our names and ids, refreshed from the
network), providers (NPI, tax ID, PTAN as a secret), **enrollment wizard** per practice with status per payer,
webhooks registered and rotated from our side on our domain, usage and costs, audit. All PDFs are rendered by
CarePlix.

## 6. Milestones

| # | Delivers |
|---|---|
| **RC1** | Gateway, white-label rename of every visible vendor mention, Admin → Clearinghouse, payer directory |
| **RC2** | Practice onboarding and enrollment wizard (providers, enrollments, status) |
| **RC3** | Hub billing pull (charges, claims, payments, balances) and write kinds for posting |
| **RC4** | R1 production-ready: discovery, COB, MBI lookup, receptionist eligibility answers |
| **RC5** | R2 + R3: claim build, scrub, submit, 277CA tracking, rejection queue |
| **RC6** | R4: claim status schedule, payer-call hand-off, overdue Medicare queue |
| **RC7** | R5: ERA posting proposals and EHR write-back |
| **RC8** | R6 + R7 + campaigns: denials, appeals, patient balances |
| **RC9** | R8 prior-auth wiring and R9 reporting |

## 7. Going live (after all milestones, owner switches to production)

Pilot: **Absolute Health Group** (group NPI 1306734819, Medicare Part B via NGS, Illinois), currently with a
third-party biller.

1. Confirm the practice's EHR is on the hub (NexHealth-supported) and connect it.
2. Enrollments through the wizard: CMS eligibility, CMS MBI lookup, NGS Part B claims and ERA, top Medicare
   Advantage and commercial plans.
3. Test ladder: real CMS eligibility for one Medicare patient → real claim status on one Medicare Advantage
   claim → one claim submitted through us → 277CA → 835 → posting proposal written to the EHR.
4. Cutover from the third-party biller on a set date (new claims through us, old claims run out with the old
   biller, ERA switched the same day), then 2–4 weeks of side-by-side checks. Medicare keeps paying the
   practice's bank by EFT; CarePlix acts as billing agent and never receives Medicare funds.
