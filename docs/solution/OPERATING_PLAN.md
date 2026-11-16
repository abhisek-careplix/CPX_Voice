# CarePlix operating plan: two product lines on one platform, and how every open item closes

Owner's brief (15 Nov 2026): automate the front office, back office, clinical support and customer
experience of a practice, each to a defined point of completion. Add a Phoebe-class AI teammate for
home health agencies. Close every open item. This document is the product plan for all of it.

Revised 16 Nov 2026 (later): §6 now maps the owner's home agency blueprint onto the platform.
Revised 16 Nov 2026 after a head-of-product review against the code on `main`: §3 now separates what
works from what is partly built, §4 sets out the platform decisions that make end-to-end automation
possible, §8 names a launch set per line, and §10 replaces the four waves with phases.

## 1. The product in one paragraph

CarePlix sells **outcomes, not calls**. A practice or agency hands us a piece of work (a cancelled slot,
a denied claim, a missed clock-in, a refill request). An agent finishes it across phone, text, email,
web portals and the EHR. The work closes only when its **completion contract** (§2) is met. Two product
lines share one platform, one codebase and one Studio:

- **CarePlix Practice**, for ambulatory clinics: the four areas in §5.
- **CarePlix Home Health**, for home care and home health agencies: the agency's whole lifecycle in §6
  (non-medical home care first, then Medicaid personal care, then skilled home health).

A line is a **pack** on the shared platform (§4): its case types, agent templates, guardrail packs,
connectors, menus and measures. Everything else is built once and used by both.

## 2. The completion contract (one definition, every area)

Every work item is a **Case**. An agent may not mark a case done unless all four hold:

| Part | What it means | Stored as |
|---|---|---|
| **Evidence** | What the agent saw or was told: transcript lines, portal screenshot, 271/277/835 response, EHR record | Evidence links on the case |
| **System of record updated** | The EHR, clearinghouse or payer acknowledged the change: appointment id, claim status, write-intent result | The acknowledgement id |
| **People informed** | The patient (or caregiver, or client) and the staff owner got the right message in their channel | Message ids, with delivery status |
| **Owned next action** | Either "nothing left", or a named owner, a due date and the next step | Owner, due date, next step |

Clinical work adds a fifth part: **clinician reviewed**. The agent prepares, a named clinician signs off,
and only then does anything reach the patient.

Status values are `done`, `handed_off` (a person owns the next step), `blocked` (with the reason) and
`failed`. A case that misses its due date escalates to its owner.

Today a case has `open`, `in_progress`, `done` and `closed`, and a reviewer's outcome moves it. None of
the four parts is recorded yet; that is Cases v2 (P4).

## 3. Where we stand (16 Nov 2026)

Everything below runs in careplix-labs on synthetic data, approved test numbers, the NexHealth sandbox,
Stedi test mode and the CarePlix Payer Sandbox. Nothing runs on real patients yet.

| Works end to end | Notes |
|---|---|
| Voice agents (inbound, outbound, phone menus, hold, live monitor, emergency stop) | Gemini Live on LiveKit; Twilio SIP trunk; approved numbers only; inbound only on the shared sandbox line by workspace code |
| Agent builder, personas, guardrail packs | Drafts and published versions |
| Voice Studio: scenarios, simulated other side, AI judge, regression and red-team suites, shadow mode, sandbox line | The publish gate |
| EHR hub: NexHealth, OpenEMR, write intents with approval, practice mirror | 16 EHR capability profiles |
| Front office: AI receptionist (verify, slots, book, reschedule, new patient), backfill engine, eligibility (Stedi test mode), Medicare benefits planner, care opportunities, Practice page | Backfill offers are made by staff, not by the agent |
| Cases (five types) and tasks started from a case | Agent, number, facts and guardrails come from the case type |
| Browser agent B1 (runtime, live view, sign-ins, take-over) and B3 (Gemini and Claude computer use, approval cards, Approvals inbox) | Mode A, skill library and skills-as-tools are still to build |
| Identity foundations: workspaces, memberships, roles, IAP check | Sign-in is dev-login behind IAP; no customer sign-in yet |

| Partly built | What is missing |
|---|---|
| Appointment reminders, claim status calls, eligibility calls, care outreach | Case types and the general agent exist; nothing creates the cases or runs them on a schedule |
| Prior-authorization status | Works against the synthetic PAS payer sandbox and by payer call; no real payer endpoint |
| Cases | Added by hand or as samples only; approved care opportunities, backfill openings, worklists and inbound calls don't create them |

Not built anywhere yet: **text, email, team chat, warm transfer, person memory, contacting many people
at once, triggers that start work without a person, forms, a clinician review queue, home-health EHRs,
customer billing**.

### How it is built, and what that means for end-to-end automation

| Today | Consequence |
|---|---|
| A **Case** is the unit of work, typed by a server-owned case type | The right base; it needs the §2 contract |
| A **Task** is one phone call to one number | Shift filling, backfill and records requests need one case reaching many people, by text or voice, first yes wins |
| **Every task is started by a person** in the Studio | Nothing turns a cancellation, call-out, referral or denial into a case and works it. This is the largest gap |
| A **Workspace** has no product line | The Studio cannot show an agency different menus, case types and agents from a clinic |
| The **connector contract** is shaped around patients, appointments and slots | Home care runs on caregivers, clients, shifts, visits and EVV; it needs its own operations, not stretched appointments |
| **Three approval places**: result review, EHR write intents, browser approvals | Clinician sign-off would be a fourth; staff need one inbox |
| **Voice is the only channel** | Caregivers and patients answer by text; every lead workflow in both lines depends on it |

## 4. One platform, two lines: the decisions

| # | Decision | Why |
|---|---|---|
| **D1** | **Playbooks.** A playbook is: trigger (an EHR event, a schedule, an inbound call or text, an import) → case → steps across channels → completion check → hand-off to a person when a step fails or a policy says so | This is what "end to end" means; without it every workflow waits for someone to press Start |
| **D2** | **A task is an attempt on any channel** (call, text, email, fax, browser), and one case may **contact many people at once**, first confirmed yes wins, the rest are told it's filled | Shift filler, backfill, confirmations and records collection all need it |
| **D3** | **Product line on the workspace.** The line selects its pack: case types, templates, guardrail packs, connectors, menus, measures. One codebase, one Studio | Two products without two platforms |
| **D4** | **Workforce in person memory.** Caregivers are people with credentials, skills, availability, distance, client fit and reliability, alongside patients, clients, families, proxies and payers. Home-care EHRs get a **home-care connector contract** (caregivers, clients, shifts, visits, EVV exceptions) beside the clinic one | Home health is a workforce problem as much as a patient one |
| **D5** | **One review inbox** for result review, EHR write intents, browser approvals and clinician sign-off, under one approval policy (what an agent may decide alone, what a person approves, what a clinician signs) | Staff work one queue; sign-off becomes evidence |
| **D6** | **Launch with three lead workflows per line** (§8), each a playbook with a Voice Studio suite, then widen | Depth that a pilot can buy beats forty half-built rows |

## 5. CarePlix Practice: the four areas

Each row says what we automate, what exists, what we build, and which platform pieces (§7) it needs.

### 5.1 Front office

Completion: the correct appointment and information are confirmed in the EHR, and the patient is told.

| Workflow | Today | To build | Needs |
|---|---|---|---|
| Answering | Partial (receptionist; sandbox line only) | A practice number per workspace; warm transfer with a spoken and written summary; after-hours rules | P2, P8 |
| Patient verification | Partial (find patient by name and birth date) | A verification policy per workspace (two identifiers, step-up for sensitive asks), logged as evidence | P3 |
| Caregiver and proxy access | ❌ | Authorised representatives on file (who, scope, expiry); the agent checks before sharing | P3 |
| Specialty scheduling | Partial (slots, booking) | Specialty rules: visit types, referral or auth required, provider preference, prep instructions, duration | P5 |
| Rescheduling | ✅ by voice | Two-way by text | P2 |
| Waitlists | Partial (backfill engine; staff make the offers) | Offers by the agent by text and voice, first yes wins | P2, P4 |
| Reminders | Partial (case type and agent; nothing schedules them) | 7-day, 72-hour and 24-hour reminders with confirm, cancel and reschedule replies, created from the EHR schedule | P2, P4 |
| Registration and intake | ❌ | Forms by secure link; insurance card capture; demographics and coverage written to the EHR | P2, P5 (NexHealth v3.1 beta) |

### 5.2 Back office

Completion: an evidence-backed result, an acknowledged submission or update, and an owned next action.

| Workflow | Today | To build | Needs |
|---|---|---|---|
| Eligibility and benefits | ✅ test mode, Medicare planner; eligibility case type | Production key; discovery, MBI lookup, COB (RC4); checks before every visit | Stedi production, P4 |
| Authorization status | Partial (PAS payer sandbox, payer calls) | Status by portal (browser agent); electronic 278 where the clearinghouse supports it | P5, browser B5 |
| Authorization preparation | ❌ | Rules for when auth is needed; packet assembly from the chart | P5 |
| Records collection | ❌ | Request records from other providers by fax and portal; track until received; attach to the chart | Fax channel (P2) |
| Claims follow-up | Partial (claim status case type, payer calls) | 276/277 on a schedule; call the payer when electronic status is stale (RC6) | RC1–RC6, P4 |
| Denials and appeals | ❌ | 835 denial codes to work queues; appeal letters drafted from evidence; staff approve before sending (RC8) | RC7–RC8 |
| Provider enrollment | Early (NPI lookup, enrollment records) | NPPES, PECOS and CAQH checks; enrollment status per payer; expiry alerts; portal work by browser agent | browser B5 |

### 5.3 Clinical support

Completion: information a clinician has reviewed, and an acknowledged follow-up or escalation.
**The agent never makes a clinical decision.** It gathers, prepares, routes and delivers approved words.

| Workflow | To build |
|---|---|
| Pre-visit questionnaires | Questionnaire by text link or call; answers summarised for the clinician |
| Chart preparation | A pre-visit brief: problems, medicines, open care gaps, outside records received, questionnaire answers |
| Refill-request routing | Capture the request (drug, dose, pharmacy), check the protocol facts, route to the clinician queue; tell the patient when it is decided |
| Approved instructions | A library of clinician-approved instructions, delivered word for word; anything else goes to a person |
| Discharge follow-up | Hospital discharge notice in (ADT feed or staff entry); call within two business days; escalate red-flag answers at once |
| Care-gap outreach | Partial: care opportunities are approved by a clinician and a care outreach case type exists; approval should create the case, then add text and booking |
| Care navigation | Referrals, community resources, transport; track until the patient is seen |

Needs the **clinician review queue**, part of the one review inbox (D5, P6), with sign-off as evidence.

### 5.4 Customer experience

Completion: patients and staff know what happened, what is left and who owns it.

| Workflow | Today | To build |
|---|---|---|
| Consistent communication | ❌ | One thread per person across channels; workspace tone and templates |
| Human transfers | ❌ | Warm transfer with a summary, or a call-back promise that becomes a case |
| Status updates | ❌ | Proactive updates whenever a case changes state |
| Complaint resolution | ❌ | A complaint case type with acknowledgement, an owner, a due date and a resolution note |
| Customer onboarding | Partial (shadow mode gives the parity report) | A go-live wizard: connect the EHR, billing NPI, payers, numbers, agents, Voice Studio gate, shadow mode, go live |
| Support | ❌ | In-app help, plus a support agent that answers from our docs |
| Outcome reporting | ❌ | Per area: volume, completion rate, time to complete, hand-off rate, revenue recovered |

## 6. CarePlix Home Health: the agency's whole lifecycle on the same platform

Source: the owner's research, [HOME_AGENCY_BLUEPRINT.md](HOME_AGENCY_BLUEPRINT.md) (16 Nov 2026). It
inventories 84 public Phoebe features and 37 Sage Care features (Phoebe acquired Sage on 2 Nov 2026),
the 21-stage agency lifecycle (L01–L21) and 18 proposed workflows (W01–W18). This section decides how
each lands on CarePlix. **Home Health is not a second product built beside Practice. It is a pack on
the platform phase 1 built.** Every agency workflow is a playbook, every piece of work is a case under
the same completion contract, and every approval goes to the same review inbox. The pack adds the
agency's case types, playbooks, guardrails, connector and Studio sections.

### 6.1 One platform, many capabilities: the blueprint mapped to what exists

| Blueprint concept | Platform piece | On `main` |
|---|---|---|
| A workflow (W01–W18) with a trigger, steps, completion and exceptions | **Playbook**: trigger → case → steps across channels → completion check → hand-off (`cases/playbooks.py`) | ✅ engine; 3 clinic playbooks |
| "Completion" for each workflow; "sending a notification is insufficient" | **Completion contract**: evidence, system of record acknowledged, people informed, owned next action, plus clinician sign-off (`cases/contract.py`) | ✅ |
| Automation levels A0 capture, A1 propose, A2 execute approved, A3 bounded policy | **Approval policy** (`cases/policy.py`): A0 = `agent` logs only; A1 and A2 = `staff`, `admin` or `clinician` approves in the inbox; A3 = a `playbook.*` rule set to `agent` for a named action | ✅ (add home-care rules) |
| Approval screen: exact change, evidence, impact, approve/edit/reject/escalate | **One review inbox** (`cases/inbox.py`) | ✅ |
| Agency memory, people, relationships, authority, credentials | **Person memory** (`people/`): Person (client, family, caregiver, staff), Relationship (proxy authority), Credential (expiry), Observation (memory item with source) | ✅ |
| Explained caregiver shortlist, hard filters then ranking (P-F09, P-F48) | `people/ranking.py`: credentials, skills, availability, travel, declined pairs as hard filters; reliability, distance, preference, language as scored reasons. No protected traits | ✅ |
| Grouped outreach, first confirmed yes, closing messages to the rest (P-F10, P-F43) | **Contact many at once** (`cases/attempts.py`), atomic winner claim | ✅ voice; text in C (#46) |
| SMS, quiet hours, consent, language, after-hours rules (P-F27, P-F55, P-F56) | **Channels** (workstream C) using Person consent, contact windows and language | 🔶 #46 open |
| Unknown caller asks about a client; family authority; revocation | Verification and proxy policy (`people/policy.py`, VerificationPolicy) | ✅ |
| Tenant isolation; "Tenant A requests Tenant B's data" test | `tenancy.py`, `test_tenancy.py` leak sweep | ✅ |
| Test/live separation, preview calls (P-F73, P-F74), shadow and replay (A23) | Sandbox line, Browser Test, Voice Studio suites, red team, shadow mode | ✅ |
| Malicious instruction in a transcript (§15) | Red-team suite and guardrail packs | ✅ |
| Durable workflow engine (blueprint suggests Temporal) | Playbook engine with state in Postgres; steps resume after restarts | ✅ Keep it. Revisit Temporal only if volume needs it |
| Action contract (§10.4): authority, preconditions, approval, idempotency, read-back, audit, recovery | Write intents (approval, audit) plus the completion contract | 🔶 Missing: idempotency keys and read-back reconciliation (§6.4) |
| Secure browser-assisted exceptions (A28) | Supervised browser agent with risk-tiered approval cards | ✅ B1, B3 |
| Eligibility and payer checks (L07) | Stedi eligibility, Medicaid and Medicare (HETS) planner, claim status | ✅ test mode |

### 6.2 Three service models inside one line

The blueprint is right that these are different businesses. They stay **one product line**, and the
workspace gains a `service_models` setting (one or more) that extends the Home Health pack:

| Service model | Adds to the pack | When |
|---|---|---|
| **Non-medical home care** (private pay, ADLs, companionship) | Intake, shift filling, confirmations, attendance exceptions, family updates, private-pay invoices | Launch (phase 3a) |
| **Medicaid personal care** | Authorization units (requested, approved, used, reserved, remaining), EVV evidence and exceptions, state/payer submission | Phase 3b |
| **Skilled home health** (nursing, therapy) | Orders and practitioner signatures, assessments and OASIS (instrument chosen by date: OASIS-E2 from 1 Apr 2026), certification and recertification, Notice of Admission (5 days), medication reconciliation, QAPI | Separate validated track (3c), with named clinical and revenue-cycle leads |
| Assisted living (later) | Facility locations, staffing patterns, escalation ownership | Not planned |

### 6.3 The workflows as playbooks

Each row becomes a playbook in the Home Health pack: its own case type, Voice Studio suite and
completion check. The approver column is the approval-policy default.

| W | Playbook | Trigger | System of record write | Completion (contract) | Human decision | Model | Phase |
|---|---|---|---|---|---|---|---|
| W01 | Agency setup | Platform staff create the workspace | Workspace, branches, policies, connector test | Readiness checklist with evidence; automation limits named | Owner enables each workflow | All | 3a |
| W02 | Referral source outreach | Campaign, new referral partner | Referral CRM record | Inquiry has owner, source, next action, deadline | Campaign approval (staff) | All | 4 |
| W03 | Inquiry to intake | Inbound call, text, web form, referral | Client and contacts in the agency system | Intake record, assessment booked or owned callback, confirmation delivered | Urgent or ambiguous identity → hand-off | All | **3a** |
| W04 | Referral packet and eligibility | Documents arrive (fax, email, portal) | Referral record, missing-items list, coverage response | Reconciled referral; authorization case where needed | Clinical appropriateness (clinician) | Medicaid, skilled | 3b / 3c |
| W05 | Assessment documentation | Assessment visit | Draft assessment with evidence per field | Signed assessment version | Assessor or clinician signs | All | 3b |
| W06 | Plan approval and activation | Assessment reviewed | Approved plan published to the agency system | Signed agreement, plan version, feasible first schedule | Plan approval; start of service | All | 3b |
| W07 | Workforce credentials | Applicant, expiring credential | Worker status in scheduling | Verified credentials; expired ones block assignment | Hiring and adverse actions (HR) | All | 3b |
| W08 | **Call-out to confirmed replacement** | Caregiver call-out (call or text), unassigned visit, new plan | Assignment in the agency system, read back | One eligible caregiver confirmed in the schedule; client and other respondents told; scheduler summary | Assignment: staff first, then A3 by policy | All | **3a** |
| W09 | **Confirmations and missed arrival** | Before each visit; missing clock-in | Confirmation status; attendance correction proposal | Confirmed, documented delay, verified correction, or owned replacement case | Disputed attendance, welfare concern | All | **3a** |
| W10 | Visit documentation | Visit starts | Visit note draft, tasks performed/refused/omitted | Reviewed visit record linked to attendance and plan version | Clinical notes signed by a clinician | All | 3b |
| W11 | Care change and device event | Caregiver or family report; validated device event | Care event case | A named person acknowledged and closed it; reassessment if decided | Clinician or supervisor | All | 3d |
| W12 | Visits to payroll and billing | Visit complete, payroll or billing cutoff | Payroll/billing export; invoice or claim | Approved payable/billable record or owned exception | Finance | All | 3b |
| W13 | Skilled episode | Skilled referral accepted | Start of care, OASIS, NOA, certification | Each deadline met or owned | Clinical assessment, OASIS, certification | Skilled | 3c |
| W14 | Quality, incidents, discharge | Incident, complaint, discharge | Corrective action; transition package | Authorized disposition | Quality lead; clinician | All | 4 |
| W15 | Medications and supplies | Admission, transition, discrepancy | Reconciled list; supply tasks | Clinician-approved list | Clinician | Skilled | 3c |
| W16 | Practitioner orders | Admission, change, unsigned order | Order request; signed order matched | Authenticated order or owned case | Practitioner signs | Skilled | 3c |
| W17 | Authorizations, denials, collections | Units near exhaustion; denial | Authorization request or appeal draft; unit ledger | Verified payer decision or owned case | Clinical justification, write-offs | Medicaid, skilled | 3b (shares prior-auth and claims work) |
| W18 | Clinical follow-up and transitions | Lab or specialist request, hospital transfer | Referral and result tracking | Receiving party acknowledged; next plan documented | Clinician | Skilled | 3c |

### 6.4 What the platform must add for agencies

New platform pieces, each built once and used by both lines where it fits:

1. **Home-care connector contract and mirror** (D4): clients, caregivers, visits with the source record
   version, recurrence and time zone, availability, attendance and EVV exceptions, authorizations,
   care-plan fields. It sits beside the clinic contract in the hub, as a home-care mirror like the
   practice mirror. First connector: the design partner's platform. AxisCare's public API is the first
   to evaluate; WellSky goes through a partner route. Where there's no API, start from CSV import
   with a review screen; the browser agent is a supervised fallback, never the base for unattended
   writes. Complete the blueprint §11.2 capability matrix before committing to a connector.
2. **Safe writes for connectors**: an idempotency key on every write intent, an offer bound to an exact
   visit version, a short reservation where the system supports it, read-back after every write, and
   reconciliation on a timeout. A timeout is never taken to mean "nothing happened". This closes
   blueprint W08's critical failures, and clinic writes get it too.
3. **Branches inside a workspace**: a branch on people, visits and cases; coordinator-specific views
   and notification routing (P-F52, P-F53). It extends P1 and the tenancy layer.
4. **Document intake**: secure email and fax in, classification, extraction with page references,
   duplicate and conflict checks. It serves W04 here and records collection in §5.2.
5. **Assessment and plan records**: versioned, every proposed field linked to its source, missing items
   left unanswered, sign-off as contract evidence. This is the same clinician review queue §5.3 needs.
6. **Authorization unit ledger**: requested, approved, used, reserved and remaining units per service
   and period, reserved against future visits (A10). It's shared with the prior-auth workstream.
7. **Attendance evidence**: reported time, device time, entered time, editor, reason and approval kept
   apart. EVV is a compliance record, not a text saying "I arrived".
8. **Outreach configuration**: editable sequences, templates with variables, deferral of calls while a
   caregiver is on a shift, client exclusion lists, pause and resume (P-F34, P-F40, P-F42, P-F54,
   P-F57). It extends playbook steps and channels.
9. **Field and family surfaces (later)**: start with text messages and secure web links. A caregiver
   mobile app and a family portal come after the pilot proves the workflows.
10. **Connected-care events (3d)**: CarePlix devices (CareSight, Health Hub, Reflekt, Loop Ring,
    CardiacPro) send validated events into a care-event case with an acknowledgment deadline and a
    backup roster. Hardware is introduced only when an event has a named recipient and a protocol.

### 6.5 The Studio for an agency

The blueprint's agency navigation (§13) maps onto Studio sections in the Home Health pack
(`packs.py`). Work that needs attention stays the default view.

| Agency view | Studio section | Status |
|---|---|---|
| Today and exceptions | Home and the review inbox | ✅ shared |
| Intake and referral pipeline | `intake` (cases of the intake types, by stage) | New |
| Clients and authorized contacts | `people` (clients, family, proxies) | ✅ shared |
| Assessments and care plans | `care_plans` | New (3b) |
| Workforce and credentials | `people` filtered to caregivers, credential expiry | ✅ base; `workforce` view new |
| Schedule and open coverage | `schedule` (home-care mirror, open visits, outreach in progress) | New (3a) |
| Conversations and calls | `calls`, plus text threads from C | ✅ / 🔶 |
| Visits and documentation | `visits` | New (3b) |
| Authorizations and revenue | `claims`, plus authorization units | Add `claims` to the Home Health pack |
| Quality and incidents | `cases` by type | ✅ shared |
| Policies, approvals, integration health | approval policy, `connections`, `integrations` | ✅ shared |

Clinic pages (Practice, Backfill, Care opportunities) stay out of the Home Health pack.

### 6.6 Why one platform beats Phoebe and Sage

- **One workflow from inquiry to plan to schedule.** Phoebe and Sage merged on 2 Nov 2026 but haven't
  shown one integrated data path. Here the approved plan's needs (transfer assistance, language,
  hours, time window) are the shift-filling filters, because both live in the same person memory
  and case records.
- **Clinics and agencies on the same platform.** A clinic's discharge or home-care referral case can
  become an agency's intake case (with consent), and the agency's updates flow back. This is
  closed-loop referral (A34), which neither competitor can offer.
- **Eligibility, authorization and claims built in**: Stedi, Medicaid and Medicare checks, the
  prior-auth and claims workstreams.
- **Proof of work.** Every outcome carries evidence and an acknowledgement under the completion
  contract. Every agent passes Voice Studio suites and red-team tests, and runs in shadow mode before
  it acts.
- **Connected care later**: CarePlix devices feed the same case and acknowledgment loop.

### 6.7 Agency guardrails

| Action | Default (approval policy) |
|---|---|
| Summarize, classify, find missing fields | `agent` |
| Routine reminders and confirmations to opted-in contacts | `agent`, within approved templates and contact windows |
| Update a caregiver's stated availability | `agent`, after identity and conflict checks |
| Assign an ordinary eligible shift | `staff`; becomes `agent` per workspace only after the pilot shows zero invalid assignments |
| Rates, bonuses, recurring commitments, attendance corrections | `admin` |
| Delete or merge records | `admin`, with preview |
| Care plans, orders, assessments, clinical notes | `clinician` |
| Hiring, rejection, discipline | Never automated |
| Clinical emergency | Route to the agency's human protocol; never automatic dispatch |

Matching never uses protected characteristics. A client's documented care requirement (for example,
same-gender personal care) is a client requirement on the plan, not a caregiver score. AI voice
outreach follows TCPA consent rules, and EVV follows state and payer rules.

### 6.8 Phasing for Home Health

| Phase | Scope | Exit (from the blueprint's gates) |
|---|---|---|
| **3a Non-medical launch** | W01 agency setup, **W03 inquiry to intake**, **W08 call-out to replacement**, **W09 confirmations and missed arrival**; connector #1 on the design partner's platform; safe writes (§6.4 #2); `schedule` and `intake` sections | Sandbox demo: an inquiry becomes a reviewed, synced client record, and an open shift becomes one verified assignment with the right notifications and a full audit trail. Two accept at once, a visit edited during outreach, "yes but an hour late", a timeout after a write, an expired credential and an opted-out caregiver all pass |
| **3b Plans, workforce, Medicaid** | W05, W06, W07, W10, W12; authorization units and EVV (W17 for units) | Assessor acceptance; correct write-back; no assignment over authorized units |
| **3c Skilled track** | W04, W13, W15, W16, W18 with clinical governance | Clinical leadership sign-off; OASIS version selection and NOA deadlines tested |
| **3d Connected care** | W11 with validated device events | False-alert testing; acknowledgment and backup path proven |
| 4 | W02 referral CRM, W14 quality, family portal, caregiver app | Per the blueprint's phase 5 |

Order against Practice is unchanged (§10): the first paying pilot is a clinic, so Practice goes first,
and phase 3 starts when the design partner and its system access are in hand.

## 7. Shared platform pieces

| # | Piece | Scope |
|---|---|---|
| **P1** | Identity, tenancy and product line | Google and Microsoft sign-in from any domain, email one-time codes, one workspace per customer, invitations and roles, tenant isolation enforced on every query, audit; the workspace's product line selects its pack (D3) |
| **P2** | Channels | Text (Twilio, with consent, STOP and HELP, quiet hours), email (a vendor that signs a BAA, or no-PHI notices with a secure link), fax, staff alerts in Slack or Teams (minimum necessary, link back), warm transfer, a preferred channel per person; a task is an attempt on any channel (D2) |
| **P3** | Person memory | Profiles for patients, clients, families, caregivers (credentials, skills, availability, reliability), staff and payers; consent and contact rules; authorised representatives; used by every agent (D4) |
| **P4** | Outcome engine: Cases v2 and playbooks | The §2 contract, due dates and escalation, owners, status updates; contact-many-at-once with first-yes-wins (D2); playbooks with triggers from EHR events, schedules, inbound calls and texts, and imports (D1) |
| **P5** | Systems of record | EHR hub (more clinic EHRs via a pilot), clearinghouse (RC1–RC9), browser agent (B2, B4, B5, B6), the home-care connector contract and its first EHR (D4) |
| **P6** | Trust | Approval policy (what agents decide alone), the one review inbox with clinician sign-off (D5), guardrails, Voice Studio gate, PHI controls, BAAs |
| **P7** | Reporting | Outcome reporting per area, per line and per customer |
| **P8** | Commerce | Phone number purchase at cost plus 100%, usage metering, invoices (Stripe) |

## 8. Launch set: three lead workflows per line

Each is a playbook (D1) with a Voice Studio suite, measured against §2.

| Line | Lead workflow | Trigger | Why first |
|---|---|---|---|
| Practice | Reminders and cancellation backfill, by text and voice | EHR schedule; cancellation webhook | Revenue: fewer no-shows, filled slots; the engine exists |
| Practice | Eligibility and authorization before the visit | Appointment booked | Prevents denials; Stedi, Medicare and PAS pieces exist |
| Practice | Claim status follow-up | Claim age; stale 277 | Cash; claim status case type exists |
| Home Health | Caregiver call-out to confirmed replacement (W08) | Inbound call or text; unassigned visit in the agency system | Phoebe's core; reuses ranking and contact-many |
| Home Health | Inquiry to intake (W03) | Inbound call, text, web form, referral | Sage's core; no lost inquiries; feeds the plan that drives matching |
| Home Health | Confirmations and missed arrival (W09) | Schedule; missing clock-in | Prevents missed visits; clean attendance and payroll |

Assessment to approved plan (W05, W06) follows in 3b. Closed-loop referral from a CarePlix clinic to an
agency (§6.6) is the first workflow neither Phoebe nor Sage can offer.

## 9. Closing every open item

| Open item | What closes it | Who | Phase |
|---|---|---|---|
| Deploy of the latest `main` | Run build and deploy; check services and logs | Owner runs (gcloud login expired on this Mac); Claude verifies | 0 |
| Accounts and access | P1 | Claude | 1 |
| Buying phone numbers | P8 in Stripe test mode | Owner creates the Stripe account; Claude builds | 3 |
| Billing work (RCM) | RC1–RC9 in Stedi test mode, Blue Button 2.0 in the CMS sandbox | Claude; owner gets clearinghouse enrollment and the production key | 2–4 |
| Text messaging | P2 code | Claude | 1 |
| A2P 10DLC registration | Brand and campaign registration in Twilio | Owner submits; Claude drafts the text | 0 |
| Browser agent | B2, B4, B5, then B6 under supervision | Claude; owner confirms Vertex computer-use access | 2–4 |
| BAAs with LiveKit and Twilio | ✅ **Closed 15 Nov 2026**: signed. Every vendor that touches PHI today (Anthropic, Google, Stedi, NexHealth, LiveKit, Twilio) has a BAA | Owner | Done |
| Receptionist config | Published receptionists pick up the new tools | Claude | 0 |
| Sandbox inbound routing | Twilio trunk origination points to the LiveKit SIP endpoint | Owner in Twilio, or Claude with approval | 0 |
| Home-health design partner | One agency and its EHR's API access | Owner | 0 |

## 10. Order of work

| Phase | Contents | Exit criteria |
|---|---|---|
| **0 (this week)** | Deploy; receptionist config; sandbox inbound; owner starts every long-lead item in §11 | Services on the latest `main`; asks submitted |
| **1: shared spine** | Four parallel workstreams: **A** P1 identity, tenancy and product line (lands first; the others rebase onto it) · **B** P4 Cases v2, playbooks and the one review inbox (P6) · **C** P2 text with consent, contact-many, warm transfer · **D** P3 person memory including the workforce | A synthetic event creates a case; a playbook works it across voice and text; it closes only with evidence; staff approve in one inbox |
| **2: Practice launch set** | The three Practice lead workflows (§8); RC1–RC5; practice menus from the line pack | Each runs end to end in labs as a playbook, with a Voice Studio suite |
| **3: Home Health** | 3a to 3d in §6.8: non-medical launch (W01, W03, W08, W09, connector #1, safe writes), then plans, workforce and Medicaid, then the skilled track, then connected care | §6.8 exits; first: an inquiry becomes a synced client record and an open shift one verified assignment |
| **4: widen and differentiate** | Referral intake to start of care, Medicaid hours, credential expiry, family updates; clinical support behind the review queue; the rest of §5.1; RC6–RC9 and denials; browser B2, B4, B5; customer experience (onboarding wizard, complaints, reporting); P8 commerce | Every §5 and §6 workflow runs end to end in labs against the completion contract |
| **5: production readiness** | BAAs for any new PHI vendors, MFA, retention, penetration test, SOC 2 preparation, runbooks; Absolute Health Group pilot under shadow mode | First real-data go-live under shadow mode, then supervised |

**Decided 16 Nov 2026: the first paying pilot, Absolute Health Group, is a clinic.** CarePlix Practice
goes first: the Practice launch set (phase 2) is the pilot's scope, and the phase 5 go-live is
Absolute Health Group under shadow mode. Home Health (phase 3) follows; its long-lead asks (a design
partner and its EHR's API access) still start now so phase 3 isn't waiting on them.
Workstreams in a phase run as separate sessions on separate branches, each following the repo's ship
flow.

## 11. Decisions and asks for the owner (start now, they have lead time)

1. **First pilot line**: ✅ decided 16 Nov 2026. Absolute Health Group is a clinic, so CarePlix Practice goes first (phase 2 before phase 3).
   Still needed: which EHR and practice-management system Absolute Health Group runs, to confirm the connector path (NexHealth or direct) before phase 2.
2. **Home health**: one design-partner agency (non-medical home care first), its agency platform and an
   administrator with API access (AxisCare or WellSky preferred), and a home-care operations expert for
   the pilot. Skilled home health later needs named clinical and revenue-cycle leads.
3. **10DLC**: register the brand and the campaigns in Twilio; Claude drafts the use-case text and samples. Text blocks every lead workflow.
4. **BAAs**: ✅ all six current vendors signed (15 Nov 2026). Each new vendor (email, fax, team chat) needs one before it carries PHI.
5. **Stripe**: create the account; test mode is enough to build.
6. **Clearinghouse**: enrollment for Absolute Health Group (837P, 835, eligibility); the Stedi production key at the end.
7. **NexHealth**: v3.1 beta access (forms, coverage create, patient update).
8. **Vertex**: confirm both computer-use models are enabled for careplix-labs.

## 12. Measures

| Area | Headline measure |
|---|---|
| Front office | Share of calls resolved without staff; slot fill rate; no-show rate |
| Back office | Days in A/R; first-pass claim rate; denials overturned; auths decided before the visit |
| Clinical support | Refill turnaround; discharge calls within two business days; care gaps closed |
| Customer experience | Cases closed within due date; complaints resolved; time to go live |
| Home health | Shift fill time; missed-shift rate; EVV exceptions open at payroll |
| Platform | Share of cases started by a trigger, not a person; share closed without hand-off |

## 13. Rules that do not bend

- No clinical decision by an agent; clinical words come from the approved library or a clinician.
- Nothing is done without evidence; nothing is sent to a patient that a policy hasn't allowed.
- Minimum necessary PHI in every notification; no PHI to a vendor without a BAA.
- Every agent passes its Voice Studio suite before publishing; new workflows start in shadow mode.
- Labs uses synthetic data only, and calls only approved destinations.
