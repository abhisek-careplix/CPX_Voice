# CarePlix operating plan: two product lines on one platform, and how every open item closes

Owner's brief (15 Nov 2026): automate the front office, back office, clinical support and customer
experience of a practice, each to a defined point of completion. Add a Phoebe-class AI teammate for
home health agencies. Close every open item. This document is the product plan for all of it.

Revised 16 Nov 2026 after a head-of-product review against the code on `main`: §3 now separates what
works from what is partly built, §4 sets out the platform decisions that make end-to-end automation
possible, §8 names a launch set per line, and §10 replaces the four waves with phases.

## 1. The product in one paragraph

CarePlix sells **outcomes, not calls**. A practice or agency hands us a piece of work (a cancelled slot,
a denied claim, a missed clock-in, a refill request). An agent finishes it across phone, text, email,
web portals and the EHR. The work closes only when its **completion contract** (§2) is met. Two product
lines share one platform, one codebase and one Studio:

- **CarePlix Practice**, for ambulatory clinics: the four areas in §5.
- **CarePlix Home Health**, for home care and home health agencies: the Phoebe-class line in §6.

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

## 6. CarePlix Home Health: the Phoebe-class teammate, and more

Phoebe (phoebe.work) runs six roles for home care agencies over voice, text and team chat, inside
home-care EHRs, with "it assists, you approve" rules. We match each role and add what a clinical-grade
platform can do.

| Role | Completion | Today | Build | Reuses |
|---|---|---|---|---|
| **Receptionist** | Call resolved or handed off with a summary | Partial | Home-care tools: call-outs, clock-in help, schedule questions, urgent triage | Voice receptionist, guardrails |
| **Shift filler** | Caregiver assigned in the EHR, client told, others told it's filled, scheduler summary | Partial | Match on skills, distance, client fit, history; contact all qualified caregivers at once by their preferred channel; first confirmed yes wins | Backfill ranking and atomic claim (never double-books) |
| **Confirmations** | Each shift confirmed or re-opened in time to fill | ❌ | Day-before and hour-before check-ins | Reminder case pattern |
| **Timekeeper (EVV)** | Missed clock-in or clock-out resolved with evidence, so EVV and payroll are clean | ❌ | Exception feed from the EHR or EVV aggregator; call or text the caregiver; staff approve corrections | Write intents |
| **Memory** | Profiles current and used by every agent | ❌ | Caregivers and clients: availability, preferences, reliability, do-not-contact | – |
| **Engagement** | Availability refreshed; dormant caregivers re-engaged | ❌ | Regular availability check-ins; re-activation outreach | – |

Beyond Phoebe:

| Workflow | Today | Reuses |
|---|---|---|
| Referral intake to start of care (eligibility, Medicare home-health period check, authorization) | ❌ as a flow | Stedi eligibility including Medicaid, Medicare (HETS) planner, prior-auth status. Our main edge |
| Medicaid waiver hour tracking | ❌ | Rules engine from care opportunities |
| Caregiver credential expiry (licence, TB, CPR) | ❌ | Rules engine from care opportunities |
| Family updates | ❌ | Needs text and proxy access (P2, P3) |
| Clinician documentation drafts | ❌, last | Needs the clinician review queue (P6) |

Integrations, in order: one EHR with a real API and a design-partner agency first (candidates:
HHAeXchange, AxisCare, AlayaCare, WellSky), then the EVV aggregators. API access is usually limited to
vendor partners, so the design partner and its EHR are the longest-lead item in this plan; where there
is no API, start from the agency's CSV exports and the browser agent. Our edge over Phoebe: eligibility
and authorization, the Voice Studio gate, red-team guardrails, the browser agent for portals without
APIs, and the completion contract as proof of work.

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
| Home Health | Caregiver call-out to shift filled | Inbound call or text; EHR shift change | Phoebe's core; reuses backfill |
| Home Health | Shift confirmations | Schedule | Prevents missed shifts |
| Home Health | Missed clock-in or clock-out | EVV exception | Clean payroll and Medicaid billing |

Referral intake to start of care follows the home-health launch set as the first workflow Phoebe can't do.

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
| **3: Home Health launch set** | The home-care connector contract on the design partner's EHR (CSV and browser agent if no API); the three Home Health lead workflows (§8) | A shift filled end to end in a sandbox agency |
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
2. **Home health**: one design-partner agency and its EHR's API access.
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
