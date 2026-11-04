# CarePlix platform roadmap: what is built, what is pending, and the credentialing opportunity

Owner: Head of Solution Design and Product Innovation · 4 Nov 2026 · Status: **plan, no code**

This document does three things:

1. It takes stock of every plan we have approved: what is live in careplix-labs and what is still to build.
2. It sets out the browser agent's path from nothing to production.
3. It maps CareLumi's product (AI credentialing and compliance) onto our voice, browser and hub stack, as a new product line.

It ends with one combined build order and the decisions the owner needs to make.

---

## 1. Where we stand

### Built and deployed (careplix-labs, `main` at `87cb389`)

| Area | What works today |
|---|---|
| **Voice platform** | Gemini Live voice agents on LiveKit. Inbound number, outbound calls via the Twilio SIP trunk to approved numbers only, browser test calls, live monitoring, emergency stop. Hold mode: the agent stays silent on hold and its talk and hold time are budgeted. Phone-menu navigation with keypad presses. |
| **Agents** | Agent builder with drafts and published versions, tools with per-tool timeouts, a "call on my behalf" errand agent. |
| **Voice Studio** | VS1: scenarios, a library of 13 RCM scenarios plus a showcase, rehearse, live runs and a measured scorecard. VS2: a simulated other side (phone menu, hold music, a rep), so a scenario runs with no person on the line. |
| **Integration hub** | NexHealth and OpenEMR connectors, capability catalog (16 EHR profiles), write intents with approval, synced practice mirror. |
| **Front office** | FO-0 foundation, FO-1 eligibility autopilot (mock payers), FO-2 backfill engine, FO-3 AI receptionist on the EHR. |
| **Payer sandbox** | A mock payer line for safe outbound tests. |

### Planned but not built

| Plan | Pending milestones | Blocker or dependency |
|---|---|---|
| Voice Studio | **VS3** AI-judged scorecards, suites, publish gate · **VS4** personas, guardrail packs, red-team · **VS5** knowledge (RAG) · **VS6** caller memory, showcase | None; can start now |
| Clearinghouse RCM | **RC1–RC9**: gateway and white-label, enrollment wizard, hub billing pull, eligibility to production, claims, status, ERA, denials, appeals, balances, prior auth, reporting | Clearinghouse enrollment for Absolute Health Group (NGS) for 837P, 835 and eligibility; production key at the end |
| Browser agent | **B1–B6**: none built yet (see §2) | A first target site whose terms allow automation; GKE and KMS setup; computer-use models enabled on Vertex |
| Front office | **FO-4** outreach (SMS, consent, campaigns) · **FO-5** forms and intake | 10DLC registration for SMS; NexHealth v3.1 beta access |
| Open issue | Silent audio in browser rehearsal: diagnostics shipped, root cause not confirmed | Owner's test after deploying `87cb389` |

---

## 2. Browser agent: from zero to production

This section is the "browser function for the agent". Nothing is built yet. The plan (`BROWSER_AGENT_PLAN.md`) is approved: our own runtime, Gemini and Claude computer use selectable, Mode A (learn by watching) first, then Mode B (defined task with a pre-flight check).

| Step | Delivers | How the owner tests it |
|---|---|---|
| **B1 Runtime** | One isolated Chrome per session on GKE Autopilot. Live view in the Studio, staff sign-in, take-over switch, saved sessions encrypted with KMS, audit trail with screenshots. | Open **Studio → Browser**, sign in to a test site, close it, and come back still signed in. |
| **B2 Mode A, teach** | Record a person doing a case: steps, pages, fields, result. The agent confirms the purpose of each step and drafts a skill; staff edit it. | Teach "look up a provider in NPPES" once and see the drafted skill. |
| **B3 Models and Mode B** | Gemini and Claude adapters, model picker, pre-flight check (site allowed, signed in, inputs complete, permission, minimum data). The agent runs a taught skill step by step with approval. | Run the same lookup for a new NPI with each model; compare. |
| **B4 Maturity** | Skill library. Ladder: watched → assisted → supervised → autonomous. Scorecards; automatic demotion after a failure. | A skill is promoted after 10 clean runs and demoted after one failure. |
| **B5 Skills as tools** | Voice agents and RCM workflows call browser skills ("check the portal while I'm on the phone"); a Browser connector in the hub with approved writes. | During a simulated payer call, the agent checks a portal result. |
| **B6 Production skills** | The first two production skills on permitted sites. | A real task on synthetic or test data. |

**Good first targets** (public, built for lookup, no prohibited bots): **NPPES NPI Registry**, **OIG exclusions (LEIE)**, **SAM.gov exclusions**. Each has an API or bulk download, so the browser skill can be checked against the API answer. That makes them ideal for proving B2 and B3, and they feed straight into credentialing (§3).

**Never automated:** NGSConnex and Availity; their terms forbid it. These stay voice- or human-only.

---

## 3. CareLumi: what they do, and how we do it better with voice and browser

### 3.1 What CareLumi is

Research was through web search only. This session cannot reach carelumi.com, so the details come from search excerpts of their site and third-party listings.

- **Positioning:** AI-native credentialing and compliance for outpatient providers; "the verification layer for healthcare AI". Their AI agent is called **CARL**. Every action is logged and audited, and a human signs off on every determination. Chicago; $2.5M pre-seed.
- **Functions:** licensing, credentialing, privileging (facility and telehealth), payer enrollment, ongoing monitoring (licenses, sanctions, exclusions), CAQH profile and 120-day re-attestation, DEA registrations, compliance status.
- **Segments:** solo providers, group practices, staffing and locum agencies, health systems (committee-ready files, privileging, re-credentialing), and MSOs, IPAs and physician alliances (multi-TIN rosters, CAQH re-attestation, network status monitoring, panel participation, lapse alerts). Specialties include behavioral health, ABA and therapy, ambulatory surgery centers, urgent care, home health and hospice.
- **Integrations claimed:** CAQH, state boards, payer portals, EHRs (athenahealth, CentralReach, TherapyNotes). SOC 2 Type 1, ISO 27001, HIPAA.
- **What they appear to lack:** voice. Most of the waiting in payer enrollment happens on the phone (status checks, missing-document chases, effective-date confirmation), and that is our strength.

### 3.2 Why it fits CarePlix

Credentialing and payer enrollment come before billing: a provider who isn't enrolled can't be paid. We are already building billing (RC1–RC9), and the RC2 enrollment wizard is the seed of this. Our differentiators:

1. **Voice:** the agent calls payers and boards, waits on hold, works the phone menu and gets the status.
2. **Browser:** taught skills on portals that allow automation.
3. **Hub:** provider and practice data comes from the EHR and practice management system, so there's no re-keying.
4. **One platform:** credentialing feeds billing, so the clearinghouse enrollment and the payer enrollment are tracked in the same place.

### 3.3 Feature map: theirs, and how we build it

| Function | How CarePlix does it | Data source and channel | Feasibility |
|---|---|---|---|
| **Provider profile and document vault** | One verified record per provider: licenses, DEA, board certification, malpractice, education, work history, CV. Documents with expiry dates; extract fields from uploads. | Hub (EHR roster) plus uploads | ✅ build |
| **NPI verification** | Automatic check on add and monthly | NPPES API (public) | ✅ build |
| **Exclusion and sanction monitoring** | Monthly OIG LEIE and SAM.gov checks, state Medicaid exclusion lists, alerts | LEIE download, SAM.gov API; state lists via browser skills | ✅ / 🟡 per state |
| **License primary-source verification** | Per-state verification and expiry tracking | State board look-ups: API where offered (e.g. Nursys for nurses), otherwise a browser skill **only where terms allow**, otherwise the voice agent calls the board | 🟡 per state |
| **DEA verification** | Registration status and expiry | DEA lookup needs a paid data subscription | 🔒 subscription |
| **Board certification** | Verify and track recertification | ABMS, ABIM and others: subscriptions or browser where allowed | 🔒 / 🟡 |
| **NPDB** | Continuous query enrollment | NPDB registration as a querying entity, needs eligibility review | 🔒 legal |
| **CAQH ProView** | Profile completeness, 120-day re-attestation reminders, roster | CAQH API needs a partner agreement; until then re-attestation is reminder-led and done by the provider | 🔒 partner |
| **Medicare enrollment (PECOS, CMS-855)** | Prepare the application packet from the record; staff submit in PECOS; agent tracks status | PECOS: human submits (identity and access rules); voice calls the MAC enrollment line for status | 🟡 assisted |
| **Commercial and Medicaid payer enrollment** | Enrollment tracker per provider × payer × TIN × location: applied → in review → missing info → approved → effective date. Application packets pre-filled. | Voice agent calls provider-relations lines for status and missing items; email and fax packets; portals via browser where allowed | ✅ voice-led, our edge |
| **Clearinghouse enrollment** | 837P, 835 and eligibility enrollments per payer (already RC2) | Clearinghouse API | ✅ RC2 |
| **Privileging (facility, telehealth)** | Request packet, committee-ready file, delineation of privileges, expiry | Documents plus workflow | ✅ build |
| **Re-credentialing** | 36-month cycle tracker, auto-assembled file | Workflow | ✅ build |
| **Monitoring and alerts** | Expiry calendar (licenses, DEA, malpractice, CAQH, board), 120/90/60/30-day reminders, lapse alerts | Scheduler plus outreach (SMS and voice need FO-4) | ✅ |
| **Rosters (MSOs, IPAs, groups)** | Multi-TIN, multi-location roster; payer roster exports | Hub plus exports | ✅ |
| **Audit layer ("verification of AI")** | We already log every tool call, keep the transcript, record write intents and require approval. Package it as an evidence report per determination. | Existing audit plus browser screenshots | ✅ extends what exists |
| **Compliance dashboard** | Readiness per provider ("can bill payer X at location Y from date Z"), gaps, upcoming expiries | Combines everything above | ✅ |

### 3.4 Voice scenarios to add to the Voice Studio library (credentialing)

| # | Scenario | Counterpart |
|---|---|---|
| CR1 | Payer enrollment status check (provider × TIN) | Payer provider-relations line with a menu and hold |
| CR2 | Missing-document chase: what's missing, where to send it, by when | Enrollment analyst |
| CR3 | Effective-date and contract-loaded confirmation | Payer provider services |
| CR4 | Medicare enrollment status with the MAC | MAC provider enrollment line (IVR) |
| CR5 | State board license verification call | State board clerk |
| CR6 | Provider reminder: CAQH re-attestation or expiring license | Provider (our client) |
| CR7 | Hospital medical staff office: privileging application status | Medical staff coordinator |

Each runs first as a VS2 simulation, then live to approved numbers only.

### 3.5 Browser skills to teach (Mode A), on permitted sites only

1. NPPES lookup.
2. OIG and SAM exclusion search, cross-checked against the API.
3. State boards whose terms allow it, one at a time, legal-reviewed.
4. Payer portals that allow automation (to be named; Availity excluded).
5. PECOS: *assisted only*. The agent prepares, a person submits.

---

## 4. Combined build order

Four tracks run in parallel; each step is merged, deployed and testable.

| Wave | Voice | Browser | RCM | Credentialing (new, "CR") |
|---|---|---|---|---|
| **1 (now)** | VS3 AI judge, suites, publish gate | B1 runtime | RC1 gateway and white-label | CR-0 provider record, document vault, NPPES and exclusion checks (API) |
| **2** | VS4 personas and guardrails | B2 teach (NPPES as first skill) | RC2 enrollment wizard | CR-1 expiry calendar, alerts, readiness dashboard; CR1–CR7 scenarios in the library |
| **3** | VS5 knowledge | B3 models and Mode B | RC3 hub billing pull · RC4 eligibility | CR-2 payer enrollment tracker plus voice status calls (CR1–CR4 live) |
| **4** | VS6 memory and showcase | B4 maturity, skill library | RC5 claims · RC6 claim status | CR-3 privileging and re-credentialing; packet generation (CMS-855, payer applications) |
| **5** | FO-4 outreach (SMS, campaigns) | B5 skills as tools | RC7 ERA · RC8 denials and balances | CR-4 rosters for MSOs and groups; audit evidence reports |
| **6** | FO-5 forms | B6 production skills | RC9 prior auth and reporting | CR-5 CAQH API, DEA, NPDB (after the agreements below) |

**Pilot:** Absolute Health Group onboarding naturally exercises CR (its providers' NPI, licenses and Medicare enrollment) before RC (billing).

---

## 5. Decisions and asks for the owner

| # | Decision or ask | Why | Lead time |
|---|---|---|---|
| 1 | Approve **credentialing (CR)** as a product line in this order | Scope | Now |
| 2 | First browser targets: **NPPES, then OIG and SAM** (recommended), plus any payer or board site you know allows automation | Unblocks B1–B2 | Now |
| 3 | Confirm Gemini and Claude computer use are enabled in Vertex for careplix-labs | B3 | Days |
| 4 | **CAQH** partner/API application | CR-5 | Weeks to months |
| 5 | **DEA** verification data subscription | CR-5 | Weeks |
| 6 | **NPDB** querying-entity eligibility (legal) | CR-5 | Weeks |
| 7 | Legal review of each state board's and payer portal's terms before any browser skill | Safety rule | Per site |
| 8 | 10DLC registration for SMS reminders | FO-4, CR-1 alerts | 2–4 weeks |
| 9 | Test the deploy `87cb389` and report the silent-audio result | Closes the open issue | Now |

## 6. Principles that carry over

- All data is synthetic in dev. No test records go to CMS or production payers.
- The vendor is shown as "Clearinghouse" only.
- Calls go to approved numbers only.
- Browser automation runs only where the site's terms allow it.
- Every determination has human sign-off and an evidence trail.
