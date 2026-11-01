# EMR Layer + Revenue Loop — Solution Design and Build Plan

**Date:** 31 Oct 2026  **Status:** proposed  **Builds on:** [AMBULATORY_EHR_INTEGRATION_STRATEGY.md](AMBULATORY_EHR_INTEGRATION_STRATEGY.md) (NexHealth decision, records networks), [HEALTHCARE_INTEGRATION_LAYER.md](HEALTHCARE_INTEGRATION_LAYER.md) (PAS), [DEMO_INTEGRATION_PLAN.md](DEMO_INTEGRATION_PLAN.md).
**Rule:** synthetic data only until the G4 PHI gate. That gate needs BAAs with NexHealth and Stedi.

---

## 0. Summary

CarePlix becomes the automation layer that sits between a practice's EHR and its payers:

```
          Practice EHR / PM                    CarePlix                         Payers
 (athena · eCW · ModMed · NextGen Office)                                  (Aetna, UHC, BCBS, …)
                │                                                                 ▲
     NexHealth Synchronizer  ◄──read/write──►  EMR layer  ◄──X12──►  Stedi  ──────┤ 270/271 · 837 · 276/277
                                                   │                                │ 277CA · 835 · 275
                                       Automation engine (tasks, policies)          │
                                                   │                                │
                          Voice (LiveKit + Gemini Live) · SMS · fax  ──────────────►┘ calls, when no API answers
                                                   │
                                Public reference: NPPES · NLM · CMS Coverage · payer PAS (2027)
```

- **NexHealth** is the *only* EHR integration. It gives us the practice's schedule, patients, coverage, charges, claims, balances and documents. It writes appointments, documents, alerts, payments and adjustments.
- **Stedi** is the *only* clearinghouse. It covers eligibility, insurance discovery, COB, MBI lookup, claims, acknowledgments, claim status, ERAs, attachments and payer enrollment.
- **The CarePlix voice agent** covers the gaps where no API answers: prior auth (Stedi has **no 278**), claim follow-ups the 276 can't resolve, payer callbacks, and patient calls.
- **Every write is an approved, tracked and confirmed intent.** Nothing writes silently, and nothing counts as "done" until the EHR confirms it.

---

## 1. What the two platforms actually give us (verified from their docs, 31 Oct 2026)

### 1.1 NexHealth (base `https://nexhealth.info`; same URL for sandbox and production; key → `POST /authenticates` → 1-hour JWT; `Nex-Api-Version` header; `subdomain` + `location_id` on most calls) [[sandbox](https://docs.nexhealth.com/docs/sandbox-and-test-mode.md), [auth](https://docs.nexhealth.com/v3.1.0/reference/authentication-1), [model](https://docs.nexhealth.com/docs/institutions-syncs-and-locations.md)]

| Domain | Read | Write | Webhooks |
|---|---|---|---|
| Practice | institutions, locations, providers, operatories, procedures | — | — |
| Patients | `GET /patients`, `/patients/{id}` | `POST /patients`; `PATCH` (v3.1 beta) | `patient_created/updated` |
| Scheduling | appointments, appointment types, **available slots**, working hours, descriptors | **`POST /appointments`**, `PATCH` (reschedule/cancel), appointment types, working hours | `appointment_created/updated/requested`, `appointment_insertion.complete/failed/timed_out` |
| Insurance | insurance plans, **coverages** | coverage/plan create (v3.1 beta) | `patient_insurance_created/updated` |
| Financials | charges, **claims**, payments, adjustments, guarantor and insurance balances, fee schedules, payment plans | **`POST /payments`**, **`POST /adjustments`** | charge/claim/payment/adjustment created/updated/deleted, `*_insertion` |
| Clinical records | clinical notes, document types, **patient documents**, recalls, treatment plans, medical history (beta) | **`POST /patients/{id}/documents`**, **patient alerts** (create/edit), medical history items (beta) | `document_insertion` |
| Forms and comms | form schemas and responses (beta), communication logs (beta) | form responses, communication logs (beta) | `form_request_completed`, `form_response_insertion` |
| Sync | **`GET /sync_status`** (read and write health per sync) | — | `sync_status_read_change`, `sync_status_write_change` |

[[endpoint index](https://docs.nexhealth.com/reference/llms.txt), [webhook events](https://docs.nexhealth.com/reference/webhook-subscriptions.md)]

**Behaviour the design must respect** [[write lifecycle](https://docs.nexhealth.com/docs/how-a-write-reaches-the-ehr.md)]:
1. **A write is accepted, then later written.** A `2xx` means only "accepted". The record is in the EHR when `foreign_id` is set and the `*_insertion.complete` webhook arrives.
2. **NexHealth never retries a failed write.** Before re-sending, read the record and check that `foreign_id` is still `null`.
3. **Reads lag the EHR by minutes.** Check `sync_status` before offering slots.
4. **Cloud EHRs (athena, eCW, ModMed, NextGen Office) sync only while someone at the office is signed in to the NexHealth Chrome extension and the EHR.** Writes queued after hours land the next morning. Overnight automations must *prepare* work, not promise it is in the chart.
5. **The v3.1 beta endpoints are limited to approved partner institutions:** patient PATCH, coverage create, form responses, communication logs. Request access early.
6. **The sandbox is a Dentrix-like demo location with no sync behind it.**
   - Reads and accepted writes work.
   - `*_insertion` webhooks never fire, and payments and adjustments never complete.
   - Confirmed writes need the free **Open Dental demo** connected to the sandbox.
   - **Medical-EHR behaviour can only be verified with a pilot practice.**
7. **Cost:** $0.10 per call. Production includes 10,000 free calls and 10,000 webhook deliveries. A repeated `POST /patients` within a few seconds can create a duplicate.

### 1.2 Stedi (test key = mock payers) [[API index](https://www.stedi.com/docs/llms.txt), [test mode](https://www.stedi.com/docs/healthcare/test-mode)]

| Capability | X12 | Test mode |
|---|---|---|
| Real-time eligibility (JSON / raw X12, 271 PDF) | 270/271 | ✅ mock payers (Aetna, Cigna, UHC, CMS) |
| **Batch eligibility** (overnight refresh) | 270/271 | ⚠️ verify |
| MBI lookup (Medicare ID from demographics) | 270/271 | ✅ mock |
| Insurance discovery (find coverage from demographics) | — | ❌ |
| Coordination of benefits | — | ❌ |
| Claims: professional (CMS-1500 JSON + validate), institutional, dental | 837P/I/D | ✅ test claims, **production account only** |
| Acknowledgments and remits | 277CA, 835 (+ ERA PDF) | ✅ via the *Stedi Test Payer* |
| Real-time claim status | 276/277 | ❌ |
| Claim attachments | 275 (≤ 6 MB per API call) | ✅ with test claims |
| Transaction enrollment (ERA, claims, eligibility) | — | ❌ |
| Payers list and search | — | ✅ |
| Event destinations (thin events, Standard Webhooks signing) | — | ✅ test events |
| **Prior auth (278)** | — | **Not offered.** Handled by CarePlix voice, payer portals and PAS (2027). |

Events [[types](https://www.stedi.com/docs/healthcare/event-destinations-event-types)]: `transaction.processed` (837/835/277), `file.processed/failed`, and `enrollment.activated/rejected/updated` plus task events. They arrive with `webhook-id`, `webhook-timestamp` and `webhook-signature` headers. **Payloads are thin**, so we fetch the detail by ID.

---

## 2. Architecture of the EMR layer

### 2.1 Components (in the existing codebase)

| Component | Where | Responsibility |
|---|---|---|
| **Connection registry** | `PayerConnection` gains kinds: `ehr` (provider `nexhealth`), `clearinghouse` (provider `stedi`) | Per-workspace credentials (Secret Manager refs), `subdomain`, location IDs, verified capability map |
| **Adapters** | `integrations/nexhealth.py`, `integrations/stedi.py` | Typed clients: token cache (1 h), pagination, `Nex-Api-Version`, retry and backoff, a call-cost meter, audit (outcome only, no payloads) |
| **Practice graph** (canonical store) | New tables (2.2) | A normalised mirror of only what automations need, keyed by `(source, source_id)` and FHIR-shaped |
| **Sync engine** | `IntegrationWorker` + new `SyncWorker` | Webhook-first ingest, with a periodic incremental reconcile as the backstop. Pauses on `sync_status` red. |
| **Write-intent engine** | New `WriteIntent` table + outbox | Every write: approve → send → accepted → confirmed (insertion webhook + read-back) → or human task |
| **Webhook ingress** | New Cloud Run service `careplix-webhooks` (public, **no IAP**) | Verify signatures (Stedi: Standard Webhooks; NexHealth: method **[verify]**), dedupe on event ID, enqueue to the outbox, return 200 fast. Holds no business logic and no PHI at rest. |
| **Automation engine** | Extends Tasks / Cases | Triggers (webhook, schedule, call outcome), playbooks (section 3), autonomy level per step, SLA timers |
| **Capability probe** | Extends readiness | Per location: which resources return data, which writes confirm. Stored as the location's capability map; the UI and playbooks read it. |
| **Studio** | New pages: *Practices*, *Work queue*, *Revenue* | Sync health, intents awaiting approval, failures, per-automation metrics |

### 2.2 Practice graph (canonical model, FHIR-aligned)

| CarePlix table | FHIR shape | Source | Key fields (minimum necessary) |
|---|---|---|---|
| `practice_location` | Location / Organization | NexHealth locations | subdomain, location_id, EHR type, tz, sync state |
| `provider` | Practitioner | NexHealth providers + NPPES | NPI (validated), name, taxonomy |
| `patient_ref` | Patient | NexHealth patients | NexHealth id, `foreign_id`, name, DOB, sex, phone. **Encrypted at the field level.** |
| `coverage` | Coverage | NexHealth coverages ⇄ Stedi 271 | payer ID (Stedi), member ID, plan, active dates, last 271 summary |
| `appointment` | Appointment | NexHealth | type, provider, start, status, `foreign_id` |
| `service_request` | ServiceRequest | Derived (appointment type / procedure codes → CPT/HCPCS) | codes, needs-auth flag, policy reference |
| `charge` / `claim` | ChargeItem / Claim | NexHealth charges and claims ⇄ Stedi 837 | amounts, codes, claim ID, PCN |
| `claim_event` | ClaimResponse | Stedi 277CA / 277 / 835 | status category, CARC/RARC, paid amount |
| `remit` | ExplanationOfBenefit | Stedi 835 | payer check/EFT, line adjustments |
| `ehr_document` | DocumentReference | NexHealth documents | type, date, our intent ID |
| `write_intent` | (Task) | CarePlix | resource, payload hash, level, approver, status, `foreign_id` |

**Rules:**
- **Mirror only what a playbook uses.** We do not replicate the whole EHR.
- **Every row keeps provenance:** source, source ID and last seen. Findings cite these rows as `system_verified` evidence, as the PAS check does today.

### 2.3 Read path

```
NexHealth webhook ─► careplix-webhooks ─► outbox ─► SyncWorker: fetch by id ─► upsert practice graph ─► fire triggers
                     (verify, dedupe)                    ▲
Every 15 min: incremental reconcile (updated_since) ─────┘  (catches missed webhooks)
Daily: locations, providers, appointment types, document types, payers (cached; saves $0.10 calls)
```

### 2.4 Write path (the same for every resource)

```
playbook proposes ─► WriteIntent(draft) ─► policy: auto (L2 allowed) | needs approval (L3) ─► approved
   ─► check sync_status(write) green? ── no ─► hold, alert at threshold
   ─► POST (Idempotency: intent id stored in our row; NexHealth id saved)
   ─► accepted ─► wait for *_insertion.complete  ─► read back: foreign_id set ─► CONFIRMED (evidence)
                └─ failed / timed_out / no webhook in SLA ─► read: foreign_id null? ─► retry once, else human task
```

**Autonomy levels** (extending the current L-scale):

| Level | Examples | Default |
|---|---|---|
| L0 read | any read, eligibility, claim status | Auto |
| L1 prepare | draft packet, draft appeal, posting suggestion | Auto |
| L2 non-financial write | result PDF to chart, patient alert, appointment confirm or reschedule *requested by the patient* | Auto after a pilot sign-off, per location |
| L3 financial / payer-facing | submit claim, post payment or adjustment, send attachment, cancel appointment | **Human approval** (one click in Work queue), policy can batch-approve |

---

## 3. Automation catalogue

Each playbook is listed as trigger → reads → actions → writes. Autonomy level and success metric follow in brackets.

### 3.1 Front office

| # | Playbook | How it runs | Writes to EHR | Metric |
|---|---|---|---|---|
| F1 | **Insurance verification** before every visit | `appointment_created`, plus a batch at 72 h and 24 h → coverage → **Stedi 270/271** (batch) → inactive? **insurance discovery** → Medicare without MBI? **MBI lookup** → secondary? **COB** | Result PDF (271 summary: active, copay, coinsurance, deductible remaining) + **patient alert** "coverage inactive" / "copay $40" (L2) | % visits verified ≥ 24 h prior; eligibility denials ↓ |
| F2 | **Prior-auth required?** | F1 result (`authOrCertIndicator`) + service rules from appointment type/CPT + CMS Coverage policy | Alert "Auth needed by {date}" → opens B1 | Auth-required visits caught before DOS |
| F3 | **Voice receptionist** (inbound, the existing LiveKit number) | Caller verified (`verify_caller`) → `available_slots` → book / reschedule / cancel | `POST/PATCH /appointments` (L2), confirmed on `appointment_insertion.complete`; SMS confirmation | Calls handled without staff; booking conversion |
| F4 | **Reminders, confirmations, waitlist fill** | T-72 h / T-24 h; `appointment_updated` cancelled → call or SMS the waitlist | Confirmation status; new appointment (L2) | No-show rate ↓, backfilled slots |
| F5 | **Digital intake** | New patient → form request → `form_request_completed` → insurance card → coverage create (beta) → re-run F1 | Form response, coverage (L2) | Intake complete before arrival |
| F6 | **Patient cost estimate** | F1 benefits + fee schedule → estimate | Document (estimate) (L2) | Point-of-service collections ↑ |
| F7 | **Recall / gap outreach** | Patient recalls / last-visit age → call or SMS | Appointment (L2), communication log (beta) | Recall bookings |

### 3.2 Back office (revenue cycle)

| # | Playbook | How it runs | Writes | Metric |
|---|---|---|---|---|
| B1 | **Prior authorization end to end** (built core) | Task from F2 → readiness (NPI, ICD-10, HCPCS, LCD/NCD) → payer PAS where it exists (2027) → **voice call to payer** to submit or check status → packet (C2) → follow-up timers until decision | Result PDF + alert "Auth #… approved 12 visits through 31 Jan" (L2); denial → B4-style appeal draft (L1) | Auths secured before DOS; staff minutes per auth |
| B2 | **Claim scrub and submit** *(opt-in per practice)* | `charge_created` → build CMS-1500 JSON → **Stedi validate** → fix list or submit **837P** | Claim reference document (L3 submit) | First-pass acceptance (277CA) |
| B3 | **Claim status follow-up** | Claims open > N days (NexHealth claims) → **Stedi 276/277** → "no record / pended / needs info" → **voice call to payer** | Status note document + alert (L2) | A/R > 30/60/90 days ↓ |
| B4 | **Rejections and denials** | 277CA rejects, 835 CARC/RARC → Claude classifies (fixable / appeal / write-off) → corrected claim or appeal letter + **275 attachment** | Corrected claim / appeal (L3) | Denial overturn rate, days to resolve |
| B5 | **ERA posting** | **835** → match to claim and charges → posting proposal | `POST /payments` + `POST /adjustments` (L3, batch-approve), confirmed via `*_insertion` | Posting lag; unposted ERA $ |
| B6 | **Patient balances** | Guarantor balances > $X → SMS or voice reminder with pay link | Payment (L3 if card-on-file), communication log | Patient A/R ↓ |
| B7 | **Payer enrollment** | New provider or payer → **Stedi transaction enrollment** (835/claims/eligibility) → track tasks | — | Days to enrolment |

### 3.3 Clinical operations (administrative support, never clinical decisions)

| # | Playbook | How it runs | Writes |
|---|---|---|---|
| C1 | **Documentation readiness** for auths | LCD/NCD + payer criteria → checklist → search clinical notes and documents (read) → mark met / missing, with citations | Alert to the clinician "Needs: 6 weeks failed PT note" (L2) |
| C2 | **Documentation packet** | Selected documents + cover sheet → packet | Packet record (L1); send by fax / 275 / portal (L3) |
| C3 | **Referral loop closure** | Outbound referral needing auth plus a specialist appointment → voice call to the specialist's office | Alert / document with appointment details (L2) |
| C4 | **Auth utilisation tracking** | Approved visits vs scheduled appointments | Alert "2 of 12 visits left; auth ends 31 Jan" (L2) → B1 extension |
| C5 *(phase 2)* | **Outside records** | Records network (Health Gorilla / Metriport) under the treatment purpose (strategy §4b) | Cited facts into C1 |

---

## 4. The closed loop: one patient, end to end

```
 1 Book (F3)            NexHealth POST /appointments ─► insertion.complete
 2 Verify (F1)          Stedi 270/271 (+ discovery/MBI/COB) ─► PDF + alert to chart
 3 Auth (F2→B1, C1–C2)  rules + CMS ─► PAS or voice call ─► approval # ─► PDF + alert
 4 Remind (F4)          SMS/voice T-72/T-24
 5 Visit                (practice)
 6 Charge (B2)          charge_created ─► Stedi validate ─► 837P (approved)
 7 Ack                  277CA via Stedi event ─► reject? ─► B4
 8 Follow up (B3)       Stedi 276/277 ─► voice call if stuck
 9 Remit (B5)           835 ─► payment + adjustment proposals ─► approved ─► NexHealth POST
10 Patient balance (B6) guarantor balance ─► reminder ─► payment
```

Every step leaves **system evidence** on the case: the EHR record ID, the Stedi transaction ID or the call transcript reference. The case timeline in Studio shows the whole loop.

---

## 5. Safety, compliance and cost controls

| Concern | Control |
|---|---|
| **Duplicate claims and ERA conflicts.** athena and many PMs already submit claims through their own clearinghouse, and ERA enrolment is usually exclusive to one clearinghouse per payer. | B2 and B5 are **off by default**. The onboarding questionnaire records who submits claims and receives ERAs, per payer. B3 and B4 work regardless, because 276/277 doesn't need us to be the submitter. |
| Writing into the chart | Only allow-listed document types and alerts; a "CarePlix" document category; every write traced to an intent and approver |
| PHI | BAAs with NexHealth and Stedi before G4; field-level encryption for patient identifiers; payloads never logged; Stedi and NexHealth payloads fetched on demand and not stored unless a playbook needs them; retention policy |
| Webhook ingress exposure | Signature + timestamp check, replay window, event-ID dedupe, rate limit, no PHI in queue messages (IDs only) |
| After-hours sync gaps | Write SLAs measured from the next sync-online window; `sync_status_write_change` alerts |
| Cost | Call meter per playbook and per practice. Cache static resources daily, prefer webhooks to polling, and budget **~5–10 NexHealth calls per auth / visit**. Stedi priced per transaction (confirm the contract). |
| Model safety | Claude drafts and classifies; deterministic code submits. No model writes directly to NexHealth or Stedi; it proposes intents (the SEC06 pattern already in the codebase). |

---

## 6. Build plan

| Phase | Scope | Proves | Needs |
|---|---|---|---|
| **E0 Foundations** (≈ 1.5 wks) | NexHealth and Stedi adapters; connection kinds; secrets; practice-graph tables + migrations; SyncWorker (reconcile + webhook handler); `careplix-webhooks` service; write-intent engine; capability probe; Studio *Practices* page (sync health, capability map) | The demo practice's schedule, patients and coverage visible in Studio; a document write accepted and tracked | NexHealth sandbox key, Stedi test key |
| **E1 Verify + auth detection** (≈ 1 wk) | F1 (Stedi mock payers mapped to synthetic coverages), F2, result PDF + alert intents | Appointment → eligibility → "auth needed" alert, all automatic | — |
| **E2 Prior-auth loop on the EHR** (≈ 1.5 wks) | B1 wired to EHR appointments; C1 checklist; C2 packet (L1); C4 tracking; Work queue page | Appointment-driven auth task → readiness → call → result written back | — |
| **E3 Voice receptionist + reminders** (≈ 1.5 wks) | F3 tools (`find_slots`, `book`, `reschedule`, `cancel`) over NexHealth; F4 outbound reminders; SMS | Inbound call books an appointment; confirmation after insertion | Open Dental demo for confirmed writes; SMS provider |
| **E4 Revenue loop** (≈ 2 wks) | B3 (276/277 + call), B4 classification, B2 validate/submit and B5 posting proposals against the **Stedi Test Payer** (277CA + 835) | Charge → claim → 277CA → 835 → posting proposal → approved write | **Stedi production account** (test claims need it) |
| **E5 Medical pilot** (G4) | One athena or ModMed practice: capability matrix, write confirmation, after-hours behaviour, BAAs | Real medical EHR read/write confirmed | Pilot practice, BAAs, v3.1 beta access |
| **E6 Phase 2** | Records network (C5), Redox for EHRs NexHealth doesn't support, payer PAS endpoints as they go live (Jan 2027) | — | Vendor contracts |

**Definition of done for E0–E4:**
- Contract tests against recorded sandbox responses.
- A replay test for every webhook type.
- Write intents proven idempotent: duplicate webhooks and resends.
- A cost report per playbook.
- No PHI in logs; checked automatically.
- Deployed to careplix-labs behind IAP; the webhook service is the only public endpoint.

---

## 7. Open items to confirm (owner: CarePlix, with the vendors)

1. **NexHealth:**
   - webhook signature method;
   - v3.1 beta partner access;
   - per-EHR write depth for medical EHRs (documents, alerts, appointment PATCH, payments);
   - which document types exist in athena, eCW, ModMed and NextGen Office;
   - BAA and volume pricing.
2. **Stedi:**
   - production-account upgrade, needed for test claims;
   - per-transaction pricing;
   - BAA;
   - batch eligibility in test mode;
   - payer coverage for 276/277 and 275 among our target payers.
3. **Practice onboarding questionnaire:**
   - who submits claims and receives ERAs, per payer;
   - which appointment types need auth;
   - who approves L3 actions;
   - office hours, for sync windows.

## Sources

NexHealth:
- [Endpoint index](https://docs.nexhealth.com/reference/llms.txt)
- [Write lifecycle](https://docs.nexhealth.com/docs/how-a-write-reaches-the-ehr.md)
- [Sandbox](https://docs.nexhealth.com/docs/sandbox-and-test-mode.md)
- [Institutions/syncs/locations](https://docs.nexhealth.com/docs/institutions-syncs-and-locations.md)
- [Webhook subscriptions](https://docs.nexhealth.com/reference/webhook-subscriptions.md)
- [Supported systems](https://docs.nexhealth.com/docs/supported-health-record-systems)
- [Authentication](https://docs.nexhealth.com/v3.1.0/reference/authentication-1)

Stedi:
- [API index](https://www.stedi.com/docs/llms.txt)
- [Products](https://www.stedi.com/docs/healthcare)
- [Test mode](https://www.stedi.com/docs/healthcare/test-mode)
- [Event types](https://www.stedi.com/docs/healthcare/event-destinations-event-types)
- [Claim attachments](https://stedi.com/docs/healthcare/submit-claim-attachments)
