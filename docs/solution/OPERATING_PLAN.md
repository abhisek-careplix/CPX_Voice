# CarePlix operating plan: the full scope, the home-health line, and how every open item closes

Owner's brief (15 Nov 2026): automate the front office, back office, clinical support and customer
experience of a practice, each to a defined point of completion. Add a Phoebe-class AI teammate for
home health agencies. Close every open item. This document is the product plan for all of it.

## 1. The product in one paragraph

CarePlix sells **outcomes, not calls**. A practice or agency hands us a piece of work (a cancelled slot,
a denied claim, a missed clock-in, a refill request). An agent finishes it across phone, text, email,
web portals and the EHR. The work closes only when its **completion contract** (§2) is met. Two product
lines share one platform:

- **CarePlix Practice**, for ambulatory clinics: the four areas in §4.
- **CarePlix Home Health**, for home care and home health agencies: the Phoebe-class line in §5.

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

## 3. Where we stand (15 Nov 2026)

| Built and merged | Notes |
|---|---|
| Voice agents (inbound, outbound, phone menus, hold, live monitor, emergency stop) | Gemini Live on LiveKit; Twilio SIP trunk; approved numbers only |
| Agent builder, personas, guardrail packs | Drafts and published versions |
| Voice Studio: scenarios, simulated other side, AI judge, regression and red-team suites, shadow mode, sandbox line | The publish gate |
| EHR hub: NexHealth, OpenEMR, write intents with approval, practice mirror | 16 EHR capability profiles |
| Front office: AI receptionist, waitlist backfill, eligibility (Stedi test mode), Medicare benefits planner, care opportunities, Practice page | |
| Cases (five types) and tasks started from a case | The base for §2 |
| Browser agent B1 (runtime, live view, sign-ins, take-over) and B3 (Gemini and Claude computer use, approval cards, Approvals inbox) | Mode A, skill library and skills-as-tools are still to build |
| Identity foundations: workspaces, memberships, roles, IAP check | Sign-in is dev-login behind IAP; no customer sign-in yet |

Not built anywhere yet: **text, email, team chat, warm transfer, person memory, contacting many people
at once, forms, a clinician review queue, home-health EHRs, customer billing**.

## 4. CarePlix Practice: the four areas

Each row says what we automate, what exists, what we build, and which platform pieces (§6) it needs.

### 4.1 Front office

Completion: the correct appointment and information are confirmed in the EHR, and the patient is told.

| Workflow | Today | To build | Needs |
|---|---|---|---|
| Answering | ✅ receptionist | Warm transfer with a spoken and written summary; after-hours rules | P2 |
| Patient verification | Partial (find patient by name and birth date) | A verification policy per workspace (two identifiers, step-up for sensitive asks), logged as evidence | P3 |
| Caregiver and proxy access | ❌ | Authorised representatives on file (who, scope, expiry); the agent checks before sharing | P3 |
| Specialty scheduling | Partial (slots, booking) | Specialty rules: visit types, referral or auth required, provider preference, prep instructions, duration | P5 |
| Rescheduling | ✅ | Two-way by text | P2 |
| Waitlists | ✅ backfill engine | Offers by text, first yes wins | P2, P4 |
| Reminders | ❌ | 7-day, 72-hour and 24-hour reminders with confirm, cancel and reschedule replies | P2 |
| Registration and intake | ❌ | Forms by secure link; insurance card capture; demographics and coverage written to the EHR | P2, P5 (NexHealth v3.1 beta) |

### 4.2 Back office

Completion: an evidence-backed result, an acknowledged submission or update, and an owned next action.

| Workflow | Today | To build | Needs |
|---|---|---|---|
| Eligibility and benefits | ✅ test mode, Medicare planner | Production key; discovery, MBI lookup, COB (RC4) | Stedi production |
| Authorization preparation and status | ❌ | Rules for when auth is needed; packet assembly from the chart; status by portal (browser agent) and payer call; electronic 278 where the clearinghouse supports it | P5, browser B5 |
| Records collection | ❌ | Request records from other providers by fax and portal; track until received; attach to the chart | Fax channel (P2) |
| Claims follow-up | Partial (payer-call scenarios) | 276/277 on a schedule; call the payer when electronic status is stale (RC6) | RC1–RC6 |
| Denials and appeals | ❌ | 835 denial codes to work queues; appeal letters drafted from evidence; staff approve before sending (RC8) | RC7–RC8 |
| Provider enrollment | Research only | NPPES, PECOS and CAQH checks; enrollment status per payer; expiry alerts; portal work by browser agent | browser B5 |

### 4.3 Clinical support

Completion: information a clinician has reviewed, and an acknowledged follow-up or escalation.
**The agent never makes a clinical decision.** It gathers, prepares, routes and delivers approved words.

| Workflow | To build |
|---|---|
| Pre-visit questionnaires | Questionnaire by text link or call; answers summarised for the clinician |
| Chart preparation | A pre-visit brief: problems, medicines, open care gaps, outside records received, questionnaire answers |
| Refill-request routing | Capture the request (drug, dose, pharmacy), check the protocol facts, route to the clinician queue; tell the patient when it is decided |
| Approved instructions | A library of clinician-approved instructions, delivered word for word; anything else goes to a person |
| Discharge follow-up | Hospital discharge notice in (ADT feed or staff entry); call within two business days; escalate red-flag answers at once |
| Care-gap outreach | ✅ care opportunities; add text and booking |
| Care navigation | Referrals, community resources, transport; track until the patient is seen |

New platform piece: the **clinician review queue** (P6), with sign-off as evidence.

### 4.4 Customer experience

Completion: patients and staff know what happened, what is left and who owns it.

| Workflow | To build |
|---|---|
| Consistent communication | One thread per person across channels; workspace tone and templates |
| Human transfers | Warm transfer with a summary, or a call-back promise that becomes a case |
| Status updates | Proactive updates whenever a case changes state |
| Complaint resolution | A complaint case type with acknowledgement, an owner, a due date and a resolution note |
| Customer onboarding | A go-live wizard: connect the EHR, billing NPI, payers, numbers, agents, Voice Studio gate, shadow mode, go live |
| Support | In-app help, plus a support agent that answers from our docs |
| Outcome reporting | Per area: volume, completion rate, time to complete, hand-off rate, revenue recovered |

## 5. CarePlix Home Health: the Phoebe-class teammate, and more

Phoebe (phoebe.work) runs six roles for home care agencies over voice, text and team chat, inside
home-care EHRs, with "it assists, you approve" rules. We match each role and add what a clinical-grade
platform can do.

| Role | Completion | Build |
|---|---|---|
| **Receptionist** | Call resolved or handed off with a summary | Our receptionist with home-care tools: call-outs, clock-in help, schedule questions, urgent triage |
| **Shift filler** | Caregiver assigned in the EHR, client told, others told it's filled, scheduler summary | Match on skills, distance, client fit, history; contact all qualified caregivers at once by their preferred channel; first confirmed yes wins |
| **Confirmations** | Each shift confirmed or re-opened in time to fill | Day-before and hour-before check-ins |
| **Timekeeper (EVV)** | Missed clock-in or clock-out resolved with evidence, so EVV and payroll are clean | Exception feed from the EHR or EVV aggregator; call or text the caregiver; staff approve corrections |
| **Memory** | — | Profiles of caregivers and clients: availability, preferences, reliability, do-not-contact |
| **Engagement** | Availability refreshed; dormant caregivers re-engaged | Regular availability check-ins; re-activation outreach |
| **Beyond Phoebe** | | Referral intake to start of care (eligibility, Medicare home-health period check, authorization); Medicaid waiver hour tracking; caregiver credential expiry (licence, TB, CPR); family updates; clinician documentation drafts (clinician reviewed) |

Integrations, in order: one EHR with a real API and a design-partner agency first (candidates:
HHAeXchange, AxisCare, AlayaCare, WellSky), then the EVV aggregators. Our edge over Phoebe: eligibility
and authorization, the Voice Studio gate, red-team guardrails, the browser agent for portals without
APIs, and the completion contract as proof of work.

## 6. Shared platform pieces

| # | Piece | Scope |
|---|---|---|
| **P1** | Identity and tenancy | Google and Microsoft sign-in from any domain, email one-time codes, one workspace per customer, invitations and roles, tenant isolation enforced on every query, audit |
| **P2** | Channels | Text (Twilio, with consent, STOP and HELP, quiet hours), email (a vendor that signs a BAA, or no-PHI notices with a secure link), fax, staff alerts in Slack or Teams (minimum necessary, link back), warm transfer, a preferred channel per person |
| **P3** | Person memory | Profiles for patients, caregivers, clients, staff and payers; consent and contact rules; authorised representatives; used by every agent |
| **P4** | Outcome engine (Cases v2) | The §2 contract, due dates and escalation, owners, contact-many-at-once with first-yes-wins, status updates |
| **P5** | Systems of record | EHR hub (more clinic EHRs via a pilot), clearinghouse (RC1–RC9), browser agent (B2, B4, B5, B6), home-care EHRs |
| **P6** | Trust | Approval policy (what agents decide alone), clinician review queue, guardrails, Voice Studio gate, PHI controls, BAAs |
| **P7** | Reporting | Outcome reporting per area and per customer |
| **P8** | Commerce | Phone number purchase at cost plus 100%, usage metering, invoices (Stripe) |

## 7. Closing every open item

| Open item | What closes it | Who | Wave |
|---|---|---|---|
| Deploy of `749b35e` | Run build and deploy; check services and logs | Owner runs (gcloud login expired on this Mac); Claude verifies | 0 |
| Accounts and access | P1 | Claude | 1 |
| Buying phone numbers | P8 in Stripe test mode | Owner creates the Stripe account; Claude builds | 3 |
| Billing work (RCM) | RC1–RC9 in Stedi test mode, Blue Button 2.0 in the CMS sandbox | Claude; owner gets clearinghouse enrollment and the production key | 1–2 |
| Text messaging | P2 code | Claude | 1 |
| A2P 10DLC registration | Brand and campaign registration in Twilio | Owner submits; Claude drafts the text | 0 |
| Browser agent | B2, B4, B5, then B6 under supervision | Claude; owner confirms Vertex computer-use access | 2 |
| BAAs with LiveKit and Twilio | Signed BAAs (Twilio needs a HIPAA-eligible edition; ask LiveKit sales) | Owner | 0 |
| Receptionist config | Published receptionists pick up the new tools | Claude | 0 |
| Sandbox inbound routing | Twilio trunk origination points to the LiveKit SIP endpoint | Owner in Twilio, or Claude with approval | 0 |

## 8. Order of work

| Wave | Contents | Exit criteria |
|---|---|---|
| **0 (this week)** | Deploy; receptionist config; sandbox inbound; owner starts every long-lead item in §9 | Services on the latest `main`; asks submitted |
| **1** | Four parallel workstreams: **P1** identity and tenancy · **P2** channels and consent · **P3 + P4** memory and outcome engine · **RC1–RC5** | Each merged with tests and a Voice Studio suite where relevant |
| **2** | Front office completion (reminders, intake, specialty rules, verification and proxies) · clinical support with the review queue · browser B2, B4, B5 · RC6–RC9 | Every §4 workflow runs end to end in labs against the completion contract |
| **3** | CarePlix Home Health with the design partner's EHR · customer experience (onboarding wizard, complaints, reporting) · P8 commerce | A shift filled end to end in a sandbox agency |
| **4** | Production readiness: all BAAs, penetration test, SOC 2 preparation, runbooks; pilot with Absolute Health Group | First real-data go-live under shadow mode, then supervised |

Workstreams in a wave run as separate sessions on separate branches, each following the repo's ship
flow. P1 touches every query, so it lands first in Wave 1 and the others rebase onto it.

## 9. Decisions and asks for the owner (start now, they have lead time)

1. **BAAs**: Twilio (HIPAA-eligible edition) and LiveKit, then each new vendor (email, fax, chat) before it carries PHI.
2. **10DLC**: register the brand and the campaigns in Twilio; Claude drafts the use-case text and samples.
3. **Stripe**: create the account; test mode is enough to build.
4. **Clearinghouse**: enrollment for Absolute Health Group (837P, 835, eligibility); the Stedi production key at the end.
5. **NexHealth**: v3.1 beta access (forms, coverage create, patient update).
6. **Home health**: one design-partner agency and its EHR's API access.
7. **Vertex**: confirm both computer-use models are enabled for careplix-labs.

## 10. Measures

| Area | Headline measure |
|---|---|
| Front office | Share of calls resolved without staff; slot fill rate; no-show rate |
| Back office | Days in A/R; first-pass claim rate; denials overturned; auths decided before the visit |
| Clinical support | Refill turnaround; discharge calls within two business days; care gaps closed |
| Customer experience | Cases closed within due date; complaints resolved; time to go live |
| Home health | Shift fill time; missed-shift rate; EVV exceptions open at payroll |

## 11. Rules that do not bend

- No clinical decision by an agent; clinical words come from the approved library or a clinician.
- Nothing is done without evidence; nothing is sent to a patient that a policy hasn't allowed.
- Minimum necessary PHI in every notification; no PHI to a vendor without a BAA.
- Every agent passes its Voice Studio suite before publishing; new workflows start in shadow mode.
- Labs uses synthetic data only, and calls only approved destinations.
