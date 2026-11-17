# Prior authorization, end to end (CarePlix Practice)

**Status:** built on the Cases v2 / playbooks framework, synthetic data only (careplix-labs).
Code: `apps/api/src/careplix_api/priorauth/`. Studio: **Prior auth** (pack section `prior_auth`).

## 1. What the playbook does

| Step | What happens | Where it can stop |
|---|---|---|
| **Detect** (trigger `auth_required`) | An upcoming visit (next 30 days) whose latest eligibility check (271) flags a service as needing authorization, or whose appointment type a **payer rule** names, opens one `prior_authorization` case per visit. It carries patient, coverage (member ID, payer), CPT/ICD-10 (from the rule), ordering provider and the date of service. The 271 or the rule is the first evidence. | — |
| **Prepare** (`prepare_packet`) | Checklist: the case's required facts plus diagnosis codes, ordering provider, date of service; and the documents from the EHR mirror (signed order, recent clinical notes, supporting documents). | Missing items → the case is **blocked**, owned, and shows in the review inbox as **Prior auth packet**. Staff fill facts on the case and attach documents by EHR id on the Prior auth page; a complete packet resumes the playbook. |
| **Submit** (`submit_auth`) | First route that works: payer **PAS API** (`Claim/$submit`), **portal** (browser channel), **payer call** (auth-status agent), **fax**. | No route → hand-off with each route's reason. |
| **Track** (`track_auth`) | PAS `Claim/$inquire` (first check after a minute, then daily), or the reviewed call's findings. | **Pended** → check again; **stale** (72 h without a decision) or "contact payer" → a follow-up call (two at most, then hand-off). **More documents requested** or **denied** → hand-off with the payer's reason. **Visit under 24 h and undecided** → hand-off. Resume picks tracking back up. |
| **Record** (`record_auth`, `await_record`) | Auth number, status and dates go on the case; the number goes back to the EHR as a chart note write intent (an administrator approves it in the review inbox; EHR writes are `admin` in the approval policy). | EHR that takes no notes → the payer's number is the record and staff own entering it. A failed write → hand-off. |
| **Tell** (`tell_patient`, `inform_staff`, `close`) | When the visit is affected (denied, undecided close to the visit, or the approval doesn't cover the visit date) a one-way text through the channel hook asks the patient to call; no clinical or insurance detail in it. Staff are told; the completion contract decides `done`. | Contract unmet → hand-off naming the missing part. |

**Completion contract (§2):** evidence (the 271/rule, every change in the payer's PAS answer as a
`fhir` link to the audited integration call, the reviewed call task), system of record (the EHR's
acknowledgement of the chart note, else the payer's auth number), staff informed, and an owned
next action (e.g. "Move the visit inside the approved dates").

**Synthetic payer sandbox:** `$submit` answers pended (A4); the first `$inquire` decides by CPT
(`fixtures/synthetic/payer_sandbox.json` → `submit_outcomes`): 72148 and anything unlisted
approved for 90 days, 95810 stays pended, 97161 denied, 93306 asks for documents, 73721 "contact
payer".

**Hooks left open:** portal = the `browser` channel (a browser-agent playbook registers it),
fax = the `fax` channel (no vendor yet). Both report "not available yet" and are skipped.
Documents: the NexHealth mirror has no orders/notes/documents; when a connector syncs them as
`EhrRecord` resources `order`/`service_request`, `note`/`clinical_note`/`encounter`,
`document`/`document_reference`, the checklist finds them without staff attaching anything.

## 2. Partner note: electronic 278 submission (owner to choose)

Our clearinghouse (Stedi) exposes only the 271 "auth required" indicators; it has **no 278**. Today
the playbook submits by FHIR PAS where a payer has an endpoint (sandbox only), otherwise by a payer
call. Two ways to add electronic submission for payers without a FHIR endpoint:

| | **A. 278-capable clearinghouse or PA platform** | **B. Payer FHIR PAS under CMS-0057-F** |
|---|---|---|
| What it is | A vendor that sends X12 278 requests (and often portal automation) to many payers through one API. Examples to evaluate: Availity (Service Reviews API; a free Demo plan with canned 278s), Optum/Change Healthcare medical network, Waystar, Experian Health, Rhyme. | Each impacted payer's own Prior Authorization API (HL7 Da Vinci CRD/DTR/PAS over FHIR), which our `integrations/pas.py` client and playbook already speak. |
| Coverage | Broad today, including commercial plans; 278 response quality varies by payer (many still pend to a portal or fax). | Required for Medicare Advantage, Medicaid, CHIP and federal-exchange QHP payers by **1 January 2027**; commercial plans outside those programs aren't required. Endpoints, onboarding and auth differ per payer. |
| Time to first live request | Weeks: contract, **BAA**, enrollment per provider/payer. | Not before payers go live (2026–2027); then per-payer registration. |
| Cost | Per transaction or subscription. | No transaction fee; engineering per payer (endpoint discovery, auth, conformance; test with the Inferno PAS kit). |
| Fit with what's built | New connector behind the existing route list (`submit_auth` routes), a `278` evidence kind, response mapping to the same statuses. | Already the first route; needs a per-payer connection instead of the sandbox's `*`. |
| Risk | Vendor lock-in; another PHI vendor (needs a BAA before any real data). | Payer readiness and IG versions vary; HHS lets payers that run an all-FHIR PA API skip X12 278, so some payers may offer FHIR only. |

**Suggested path (for the owner to decide):** keep B as the long-term route (it's the regulated one
and the code exists); for the pilot clinic, ask its current clearinghouse/RCM partner whether it
offers 278 for the clinic's top payers, and evaluate **one** of A (Availity's Demo plan is the
cheapest way to prove the 278 mapping with canned responses) only if the pilot's payer mix needs it
before 2027. Nothing has been signed up for; any vendor that sees real patient data needs a BAA
first.

Sources to verify before deciding: CMS-0057-F final rule (compliance dates, impacted payers);
HHS enforcement discretion on X12 278 for FHIR PA APIs; each vendor's current payer list for 278.
