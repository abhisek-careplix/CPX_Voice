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
| **FO-3 AI receptionist on the EHR** (A1, A5) | Voice tools: `find_patient`, `list_appointments`, `find_slots`, `book_appointment`, `reschedule_appointment`, `cancel_appointment`, `leave_message` over NexHealth and OpenEMR. **Built:** see §5 | ✅ inbound number live; Browser Test on a synthetic practice |
| **FO-4 Outreach channel** (D1, E1, E2, B2, B6, A2, A3) | Twilio or Telnyx SIP trunk + SMS, consent store, contact rules, campaigns | Needs the carrier decision + 10DLC |
| **FO-5 Forms and intake** (D2, D3, A4) | Form schemas and responses, coverage create, patient update | Needs NexHealth beta access |

**Asks to start now, because they have lead time:**
1. **Carrier decision and account** (Twilio recommended). Then start BAA and 10DLC registration.
2. **NexHealth:** request v3.1 beta partner access (forms, coverage create, patient update) and confirm the webhook signing method.
3. **Stedi:** a production key when ready, for discovery, MBI lookup, COB and batch eligibility.
4. **A pilot practice** on athena, eCW, ModMed or NextGen Office, to validate medical-EHR writes.


---

## 5. FO-3 as built: the AI receptionist

**Template:** *Front desk receptionist* (`front-desk-receptionist`, agent "Riley"). It is an inbound template, so it answers a connected number. It can also be tried with **Browser Test**, and the demo workspace seeds it as a draft agent.

**Call flow:**
1. Greet → why are you calling? Emergencies → "hang up and call 911". No clinical advice.
2. **Verify** with `find_patient` (first name, last name, date of birth against the encrypted mirror). Exactly one active match passes. Two people with the same name and birth date do not pass, because they can't be told apart by voice. The caller gets two tries. The result never says whether someone is a patient. Nothing about appointments is read or changed before verification.
3. **Book:** `find_slots` asks the EHR for open times (NexHealth `available_slots`; OpenEMR clinic hours minus booked visits). It offers up to six, at most two per day, as `S1`, `S2`… `book_appointment` accepts only those ids, so the model can't invent a time or book for someone else.
4. **Reschedule:** `find_slots(appointment_id)` keeps the visit type and provider. The new time is booked first; the old visit is released only once the booking is accepted. A failed booking never releases anything.
5. **Cancel:** the patient chooses a reason from a fixed list (no free text, so no clinical detail). By default the cancellation goes to **Practice → EHR write queue** for one click (L3 in the EMR design); the agent tells the caller it isn't cancelled until the front desk confirms. With `RECEPTIONIST_CANCEL_MODE=auto` it is cancelled at once, and the freed time is picked up by **backfill** (FO-2) on the next pass.
6. **Messages:** `leave_message` covers new patients, refills, records, billing and anything else the agent can't do. It works without verification. Messages appear in **Practice → Front desk messages**: the list shows topic, initials and whether a callback is wanted; opening one reveals the text and the caller's number, and that is audited.

**Safety:**
- Every EHR change is a write intent: `source = receptionist`, approved by `policy:receptionist-L2`.
- The slot is re-checked with the EHR immediately before booking. A time taken in the meantime gets "slot_taken" and the agent offers another.
- At most 3 changes and 3 messages per call.
- Write tools claim their call id before calling the EHR, so a worker retry can't book twice. An unexpected error ends as "outcome unknown, front desk to check", never as a second attempt.
- A Browser Test can write only to a synthetic practice (local OpenEMR, NexHealth sandbox). Against a production practice, writes are refused.
- The call timeline shows outcomes ("Booking: booked in the EHR"), never names or times.

**Try it:** `make openemr-up && make openemr-setup`, start the API and Studio, publish *Riley Front Desk*, then use **Browser Test** as one of the synthetic patients. Or connect a number to it in **Connections → Inbound**.

**Next:**
- New-patient registration on the call (NexHealth `POST /patients`; OpenEMR also needs sex at birth), which today goes to a message.
- SMS confirmation after booking (FO-4).
- Handoff to a person (`request_handoff`, release P).
