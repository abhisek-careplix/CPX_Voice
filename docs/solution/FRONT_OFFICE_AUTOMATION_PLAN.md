# Front-Office Automation Plan

**Date:** 31 Oct 2026  **Status:** proposed  **Builds on:** [EMR_LAYER_SOLUTION_DESIGN.md](EMR_LAYER_SOLUTION_DESIGN.md)
**Focus:** the front office first: scheduling, eligibility, cancellation backfill, reminders and forms. Back office (claims, ERAs, denials) waits until this works.
**Rule:** synthetic data only until G4. NexHealth sandbox data is NexHealth's own demo practice.

---

## 1. What we can use today

| Integration | Status | What it gives the front office |
|---|---|---|
| **NexHealth** (sandbox connected) | ✅ live reads | appointments (incl. `cancelled`, `patient_missed`, **`sooner_if_possible`**), **available slots**, appointment types, working hours, providers, operatories, patients, coverages, plans (with `payer_id`), patient alerts, documents |
| | ✅ live writes | `POST /appointments` (book), `PATCH /appointments` (cancel in sandbox), `POST /patients`, patient alerts |
| | 🔒 beta access needed | patient update, coverage create, form schemas and responses, communication logs |
| | ✅ webhooks | `appointment_created/updated/requested`, `appointment_insertion.*`, `patient_created/updated`, `patient_insurance_*`, `form_request_completed`, `sync_status_*` |
| **Stedi** (test key connected) | ✅ live | real-time eligibility (mock payers), payer search |
| | ✅ production key | batch eligibility, **insurance discovery**, **MBI lookup**, **coordination of benefits** |
| **LiveKit + Gemini Live** | ✅ inbound | +1 312 879 1434 answers with the CarePlix agent; server-authorized tools; caller verification |
| | ⛔ outbound | **No carrier trunk.** This blocks every outreach call. |
| **SMS** | ⛔ none | Needed for reminders, form links and fast backfill offers |
| Reference APIs (NPPES, NLM, CMS) | ✅ live | provider and code checks |

**Biggest unblock: one carrier for outbound calls and SMS.**
- **Recommendation: Twilio.** It offers a BAA on eligible products, an Elastic SIP Trunk for LiveKit outbound, and SMS from the same account.
- **Telnyx** is the alternative. Its main draw is lower per-minute cost.
- **Start A2P 10DLC (or toll-free) SMS registration now.** It takes days to weeks before any patient text is delivered.

---

## 2. Front-office automations

Readiness key:
- ✅ buildable and testable now.
- 🟡 buildable now, but outreach waits for outbound/SMS.
- 🔒 needs NexHealth beta access or a pilot practice.

### A. Scheduling and access

| # | Automation | How it works | Integrations | Ready |
|---|---|---|---|---|
| A1 | **AI receptionist (inbound)** | Answer → verify the patient (name and DOB, matched against NexHealth) → book, reschedule or cancel from **available slots** → confirm by voice; new callers become new patients; FAQs (hours, address, parking, accepted insurance); hand off to staff with a summary | LiveKit inbound, NexHealth patients/slots/appointments | ✅ |
| A2 | **Online booking request follow-up** | `appointment_requested` → call or text to confirm and collect insurance → book | NexHealth webhook, voice/SMS | 🟡 |
| A3 | **Provider out / schedule change** | Working hours removed, or a block of cancellations for one provider → call each affected patient → rebook with the same or another provider | NexHealth working hours/appointments, voice | 🟡 |
| A4 | **New-patient intake call** | After a new patient books → collect demographics, reason for visit and insurance card details → create coverage → run eligibility (B1) | NexHealth patient/coverage, Stedi | 🔒 coverage create |
| A5 | **After-hours line** | Same agent as A1, plus message capture for refills, records, billing → patient alert and staff work item. **No clinical advice; emergencies go to 911/on-call.** | LiveKit, NexHealth alerts | ✅ |

### B. Eligibility and benefits

| # | Automation | How it works | Integrations | Ready |
|---|---|---|---|---|
| B1 | **Eligibility autopilot** | At booking, and again at **72 h** and **24 h** before the visit: map the NexHealth plan `payer_id` to the Stedi payer → 270/271 → active? copay, coinsurance, deductible remaining, out-of-pocket → note on the chart (alert/document) | NexHealth coverages/plans, Stedi | ✅ (mock payers) |
| B2 | **Inactive coverage recovery** | 271 inactive or terminated → **insurance discovery** → found a plan? flag it for staff and ask the patient to confirm. Not found? call the patient: "Do you have a new insurance card?" → capture the new member ID → re-verify | Stedi discovery, voice | 🟡 / 🔒 |
| B3 | **Medicare ID repair** | Medicare patient without a valid MBI → **MBI lookup** → re-verify | Stedi | ✅ (prod key) |
| B4 | **Secondary coverage check** | **COB** check → flag which plan is primary | Stedi | ✅ (prod key) |
| B5 | **Prior-auth needed flag** | Auth indicator in the 271 + appointment-type rules → start the existing auth workflow | Stedi, CarePlix auth agent | ✅ |
| B6 | **Patient cost heads-up** | Copay/deductible from the 271 → reminder call/SMS "expected copay $40" | Stedi, voice/SMS | 🟡 |

### C. Cancellation backfill (the core revenue win)

**Trigger:**
- `appointment_updated` with `cancelled=true`;
- a deleted appointment;
- a no-show (`patient_missed`) on a same-day slot.

**Should we fill it?** Only if:
- the slot starts **at least 2 h from now** (configurable);
- it is inside working hours;
- the appointment type is backfillable (the practice chooses which).

**Candidate pool, ranked:**
1. **ASAP list:** patients whose future appointment has `sooner_if_possible = true` and the same appointment type (or a compatible one), with the same provider, or any provider if the practice allows it.
2. **Move-up:** patients booked later with the same type and provider.
3. **Overdue recall / unscheduled follow-up:** no future appointment, and the last visit is older than the practice's interval.
4. **Recent cancellers** who never rebooked.

**Filter the candidates:**
- **Coverage still active:** the B1 eligibility result is fresh (≤ 7 days) or re-run.
- **Reachable:** a phone number exists, the patient hasn't opted out, it's within contact hours (8 am–8 pm local), and they haven't been contacted more than the frequency cap.

**Outreach:**
- **SMS (when available):** offer the slot to the top 3 at once; first "YES" wins.
- **Voice:** call candidates one at a time, best first.
- **Speed:** stop at the first acceptance. Each candidate gets about 10 minutes before we move on.

**Booking safely:**
1. Lock the slot in CarePlix.
2. Re-check NexHealth **available slots** right before booking.
3. `POST /appointments`. For a move-up, also cancel the old appointment (`PATCH cancelled`).
4. Confirm on `appointment_insertion.complete`.
5. If the write fails, apologise and offer the next slot. A slot is **never double-booked**.

**Staff view:** the slot timeline (cancelled → offered to 3 → accepted by X → written to EHR) and the recovered value (from the fee schedule).

**Metrics:**
- fill rate;
- minutes from cancellation to fill;
- recovered visits and revenue;
- patient opt-outs.

**Ready:**
- The engine (detection, ranking, slot lock, booking) is ✅ testable now on the sandbox. Calls can go through the Browser Test or approved test numbers.
- Real outreach is 🟡 until we have outbound calling and SMS.

### D. Visit readiness

| # | Automation | How it works | Integrations | Ready |
|---|---|---|---|---|
| D1 | **Reminders and confirmations** | T-72 h and T-24 h by voice/SMS → confirm, reschedule (A1 tools) or cancel (which triggers C) | NexHealth, voice/SMS | 🟡 |
| D2 | **Forms completion** | Missing intake or consent forms → send the form link → `form_request_completed` → chart. Nudge at T-48 h; voice-assisted completion for patients who can't do it online | NexHealth forms (beta), SMS/voice | 🔒 |
| D3 | **Demographics and insurance refresh** | Annual or new-plan-year check → confirm address, phone and insurance by call/SMS → update | NexHealth patient update (beta), Stedi | 🔒 |
| D4 | **Daily huddle sheet** | Tomorrow's schedule with eligibility status, auth status, missing forms, balances and backfill opportunities, as a Studio page (later email) | NexHealth, Stedi, CarePlix | ✅ |

### E. No-shows, recall and reactivation

| # | Automation | How it works | Ready |
|---|---|---|---|
| E1 | **No-show rebook** | `patient_missed` → same-day call → rebook | 🟡 |
| E2 | **Recall / follow-up due** | No future appointment and the follow-up interval has passed (annual wellness, chronic follow-ups, post-procedure) → call/SMS → book | 🟡 |
| E3 | **Reactivation** | No visit in 18+ months → outreach campaign with practice approval | 🟡 |

---

## 3. Guardrails (all front-office outreach)

**Consent and contact rules:**
- **TCPA.** Healthcare appointment and reminder calls to mobiles have specific exemptions (free to the patient, limited frequency, immediate opt-out). Confirm the exact rules with counsel and record consent and opt-outs per patient.
- **Contact hours:** 8 am–8 pm in the patient's local time.
- **Frequency caps:** per patient, per week.

**Identity and disclosure:**
- **Identity before disclosure.** Voice calls state only the practice and a callback number until the patient is verified. The existing `verify_caller` pattern covers this.
- **Voicemail:** no appointment details or PHI, only a callback request.

**What the agent never does:**
- No clinical advice.
- No emergency handling beyond "call 911".
- Hand off to staff on request.

**System safety:**
- **Every EHR write is a tracked intent.** Backfill bookings are L2 (automatic once the practice approves the playbook); cancellations made for move-ups are confirmed with the patient on the call first.
- **Kill switch:** the existing workspace emergency stop halts all outreach.

---

## 4. Build order (front office first)

| Step | Scope | Testable now? |
|---|---|---|
| **FO-0 Foundation** | NexHealth + Stedi adapters; encrypted practice mirror; sync (full + incremental); webhook ingest; write intents; capability probe; Studio **Practice** page | ✅ sandbox |
| **FO-1 Eligibility autopilot** (B1, B5, D4) | Batch at booking, 72 h and 24 h; plan → Stedi payer mapping; results on the appointment; huddle sheet | ✅ mock payers. In test mode, demo patients are mapped to Stedi's mock members and labelled *mock*. |
| **FO-2 Backfill engine** (C) | Cancellation detection, ranking, slot lock, re-check, booking, move-up, timeline, metrics | ✅ engine; outreach through Browser Test / approved numbers |
| **FO-3 AI receptionist on the EHR** (A1, A5) | Voice tools: `find_patient`, `find_slots`, `book`, `reschedule`, `cancel`, `leave_message` over NexHealth | ✅ inbound number live |
| **FO-4 Outreach channel** (D1, E1, E2, B2, B6, A2, A3) | Twilio or Telnyx SIP trunk + SMS, consent store, contact rules, campaigns | Needs the carrier decision + 10DLC |
| **FO-5 Forms and intake** (D2, D3, A4) | Form schemas and responses, coverage create, patient update | Needs NexHealth beta access |

**Asks to start now, because they have lead time:**
1. **Carrier decision and account** (Twilio recommended). Then start BAA and 10DLC registration.
2. **NexHealth:** request v3.1 beta partner access (forms, coverage create, patient update) and confirm the webhook signing method.
3. **Stedi:** a production key when ready, for discovery, MBI lookup, COB and batch eligibility.
4. **A pilot practice** on athena, eCW, ModMed or NextGen Office, to validate medical-EHR writes.
