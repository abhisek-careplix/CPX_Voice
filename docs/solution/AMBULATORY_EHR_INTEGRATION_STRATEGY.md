# Ambulatory EHR / PM Integration Strategy — Small and Mid-size Practices

**Question:** how do CarePlix agents see, validate and act on practice data for prior authorization when the customers are small and mid-size practices, not Epic or Oracle health systems?
**Date:** 31 Oct 2026. Sources are inline. Market-share figures for small practices are not reliably published, so this document ranks systems by segment and specialty, not by unverified percentages.

---

## 1. The market we are actually selling into

The ambulatory market is fragmented: eClinicalWorks, athenahealth, NextGen, Veradigm, Greenway, and dozens of specialty vendors [[Synodus](https://synodus.com/blog/healthcare/electronic-medical-health-record-companies/)]. In one survey, 72% of 1–5 physician practices said they were considering vendors beyond the best-known names [[Healthcare IT News](https://www.healthcareitnews.com/news/physicians-widen-net-their-search-emrs)].

| Tier | Systems (EHR + PM) | Typical practice | Notes for prior auth |
|---|---|---|---|
| **Mid-tier, multi-specialty** | athenahealth (athenaOne), eClinicalWorks, NextGen (Enterprise / Office), Greenway (Intergy, Prime Suite), Veradigm (Practice Fusion, Allscripts PM) | 5–200 providers, often with an in-house billing team | Mature APIs and partner programs; write-back is contract-gated |
| **Specialty-led** | ModMed (derm, ortho, ophtho, GI, uro, plastics), Nextech (ophtho, plastics, derm), Compulink (eye care), WebPT / Raintree (PT/OT/rehab) | Specialty groups, **heavy prior-auth users** (ortho, imaging, PT, pain, GI) | High prior-auth volume per practice: the best early-customer fit |
| **Small / low-cost** | Tebra (Kareo), AdvancedMD, DrChrono, CureMD, Elation (primary care), CharmHealth, Office Ally (free PM) | 1–10 providers, often an **outsourced billing company** | APIs are thinner or partner-only; outsourced billers often do the prior-auth work |
| **Behavioral / cash-heavy** | SimplePractice, TherapyNotes, Healthie, Jane | Solo and small groups | Lower prior-auth volume (outpatient behavioral auths only); Healthie runs an open developer program [[Healthie](https://partner-program-directory.partnerfleet.io/partners/healthie-platform-partner-program)] |

**Key insight:** in small and mid practices, prior-auth data lives mostly in the **practice-management (PM) / billing side** (orders and referrals, scheduled services, insurance, the authorization number and dates on the visit), not in clinical notes. Often it isn't in software at all: it's a staff spreadsheet or the billing company's worklist.

---

## 2. What the agent needs from the practice

| Need | Data | Direction |
|---|---|---|
| **What needs an auth** | Upcoming appointments and orders (CPT/HCPCS, ICD-10, date, rendering provider, location) | Read |
| **Who and which plan** | Patient demographics, member ID, payer, plan | Read |
| **Evidence for the payer** | Visit notes, imaging/PT notes, conservative-therapy history | Read (documents) |
| **Result** | Auth number, status, approved units, valid dates, missing requirements, call reference | **Write back** to PM (auth fields or visit), plus a task or note for staff |
| **Packet** | The call summary as a PDF in the patient chart | Write (document) |

Reading is widely standardised. **Writing back is the hard part:** it costs about 40–60% more than reading, and many ambulatory EHRs restrict third-party writes [[Thinkitive](https://www.thinkitive.com/blog/ehr-integration-cost-guide/), [Commure](https://www.commure.com/blog-scribe/ehr-integrations)].

---

## 3. Integration options, ranked for this market

| # | Approach | Covers | Read / Write | Effort per practice | Verdict |
|---|---|---|---|---|---|
| **A** | **Worklist in, results out (no EHR integration):** CSV/SFTP export from the PM, or a simple REST API; results back as CSV, PDF and webhook; email/fax to staff | **Every** EHR and PM, and billing companies | Read via export; staff or biller keys in results | Hours | **Start here.** This is how SuperDial onboards (CSV, API, integrations) [[SuperDial FAQ](https://www.superdial.com/faq)] |
| **B** | **ONC (g)(10) standard FHIR API** (US Core, SMART Backend Services, Bulk Data). Every certified EHR must provide it [[ONC](https://www.healthit.gov/sites/default/files/page2/2020-03/APICertificationCriterion.pdf), [Nirmitee](https://nirmitee.io/blog/onc-ehr-certification/)] | athena, eCW, NextGen, Greenway, Veradigm, ModMed, DrChrono, AdvancedMD, Elation (all certified) | **Read-only** by rule | Practice admin authorises the app; vendor endpoint registration | **One build, many EHRs**, for reading demographics, coverage, conditions, documents. Our FHIR client already supports SMART Backend Services |
| **C** | **Vendor partner APIs** (proprietary + FHIR write) | Per vendor: athenahealth (Certified APIs; non-certified need a Platform Services contract), NextGen (API Distributor Program, fees), eCW (write APIs contracted via interop@eclinicalworks.com, recent patch levels) [[athena/NextGen/eCW programs](https://nirmitee.io/blog/ehr-integration-api-comparison-epic-oracle-athenahealth-eclinicalworks/), [NextGen](https://www.nextgen.com/partner-program), [eCW](https://nirmitee.io/healthcare-interoperability/eclinicalworks-integration/)] | **Read + write**: auth fields, appointments, documents | Partner contract once per vendor, then a per-practice switch-on | **Write-back for the top 2–3 vendors** in our pipeline |
| **D** | **Unified integration API** (Redox, now allied with Kno2) | athenahealth, eCW, NextGen, Greenway, DrChrono and 100+ more, via FHIR, HL7 and proprietary APIs, with a write-back accelerator [[Redox athena](https://www.redoxengine.com/connect/ehrs-supported/athenahealth/), [Redox–Kno2](https://www.rfp.wiki/specialty-industries/healthcare-life-sciences/healthcare/health-data-management-platforms/redox/kno2)] | Read + write, depth varies by EHR and version | Commercial per-connection fees (not published) | **Buy instead of build** once customers on a vendor we don't support are paying; avoids N partner contracts |
| **E** | **HL7 v2 interfaces** (inbound scheduling/orders/admit messages; outbound documents/charges) via an interface engine (Mirth / NextGen Connect) | Most mid-tier EHRs sell HL7 interfaces | Read + write (documents, notes) | Vendor interface fee plus setup per practice | For larger mid-tier groups that already run interfaces |
| **F** | **Clinical-records networks** (Carequality / CommonWell / TEFCA via Health Gorilla, Particle, Metriport) | Records across almost any EHR, **without integrating with the practice's EHR** | Read (documents) | Network onboarding once | Strong for **building documentation packets**. Confirm the allowed exchange purposes (payment / operations) before use **[verify]** |
| **G** | **Billing company / clearinghouse layer** (Availity, Waystar, Office Ally, the practice's RCM company) | Where small practices already send claims | X12 270/271, 276/277, 278 | Clearinghouse enrolment | Pair with A: **sell through RCM companies**, which serve many practices across many EHRs (SuperDial's largest segment) |
| **H** | **Browser automation** (agent-driven UI automation of the EHR/PM) | Anything with a screen | Read + write | High upkeep; needs a credential vault and BAA | **Last resort** for the long tail; human approval on every write |

### Regulatory tailwind to plan for
**HTI-4** (effective 1 Oct 2025) adds certification criteria so EHRs themselves can do electronic prior auth: **(g)(31) CRD, (g)(32) DTR, (g)(33) PAS** [[ASTP/ONC HTI-4](https://healthit.gov/resources/hti-4-overview-and-key-dates-for-certification-program/)]. Test kits are expected around Q4 2026, and reports say MIPS providers will need these capabilities in 2027 **[verify]** [[Drummond](https://www.drummondgroup.com/wp-content/uploads/2025/09/Drummond-Webinar-HTI-4-Unpacked-092325.pdf)].

- **Mid-tier EHRs will ship electronic submission.** CarePlix should not compete on submission.
- **CarePlix covers the gaps instead:** payers not yet on the APIs, *pended* cases that need phone follow-up, missing-document chases, and validating what payers said against the record. Our Da Vinci PAS client lets us read the same status those EHRs will write.

---

## 4. Recommended plan

| Phase | Build | Customer value | CarePlix components |
|---|---|---|---|
| **P1 — Universal intake (weeks 1–3)** | Worklist import (CSV, SFTP drop, REST `POST /worklists`), mapping templates for common PM exports (athena, eCW, NextGen, ModMed, Tebra, AdvancedMD), bulk tasks with scheduling/retries; results as CSV and PDF, signed webhooks, daily email digest | Works for **any** practice or RCM company on day one | New `worklist_import` connector; reuses tasks, voice, readiness, PAS checks |
| **P2 — Standard FHIR read (weeks 3–6)** | (g)(10) connector using SMART Backend Services: Patient, Coverage, Condition, ServiceRequest, Appointment, DocumentReference; an endpoint directory per vendor; per-practice authorisation flow in Connections | Pull the case context automatically; fewer CSVs | Extends `integrations/fhir.py` (Backend Services already built) |
| **P3 — Write-back, top vendors (weeks 6–12)** | Partner programmes for **athenahealth** and **eClinicalWorks** first, then **ModMed** or **NextGen**, chosen by pipeline; write the auth number, status and dates to the PM; upload the call-summary PDF; create a staff task | "The auth number appears in my PM" | Write tools at **L1 approval** by default; verify-after-write; reconciliation of uncertain writes (existing patterns) |
| **P4 — Documentation packets** | Records-network retrieval (Health Gorilla/Particle) + (g)(10) DocumentReference; packet assembly; approval-gated fax (Sinch Fax / Documo under BAA) or 275 | Close "missing documentation" pends without staff chasing | Builds on the readiness checks and the CMS Coverage requirements |
| **P5 — Long tail** | Redox for vendors outside P3 when a paying customer needs them; browser automation only with approval | Coverage of the remaining market | Unified API adapter |

**Go-to-market fit:** lead with **specialty groups that have heavy prior-auth volume** (orthopedics, imaging, PT, pain, GI, cardiology), which are mostly on ModMed, NextGen, athenahealth and eClinicalWorks, and with **RCM companies** serving many small practices on Tebra, AdvancedMD, DrChrono and Office Ally.

---

## 4a. Decision: one integration hub to go live (31 Oct 2026)

**Goal:** a single read/write integration across the ambulatory EHRs our target practices use, instead of N vendor partnerships.

| Hub | Medical ambulatory EHRs (documented) | Read | **Write** | Pricing | Time to live | Fit |
|---|---|---|---|---|---|---|
| **NexHealth Synchronizer** | **athenahealth, eClinicalWorks (cloud v11.53+), ModMed, NextGen Office** (+11 dental) [[supported systems](https://docs.nexhealth.com/docs/supported-health-record-systems)] | Patients, appointments, slots, insurance/eligibility, charges, claims, documents | **Appointments (book/reschedule/cancel), patient documents/forms (PDF to eCW and athena), payments** [[what's possible](https://docs.nexhealth.com/docs/whats-possible-with-nexhealth), [eCW docs](https://docs.nexhealth.com/changelog/june-2023-release)] | **Usage-based, $0.10 per API call, free sandbox** [[Synchronizer](https://synchronizer.nexhealth.com/)] | Days: the practice creates a dedicated user and NexHealth syncs | ✅ **Recommended primary** |
| Redox (+Kno2) | Widest: athena, eCW, NextGen, Greenway, DrChrono, 100+ [[Redox](https://www.redoxengine.com/connect/ehrs-supported/athenahealth/)] | Broad | Broad (vendor APIs, HL7, write-back accelerator) | Custom annual contracts, not published [[aggregator](https://getpulsesignal.com/pricing/redoxengine)] | Weeks per site; each site configures | Long tail and enterprise, once a customer pays for it |
| Metriport (open source) | athena, eCW, Elation, Practice Fusion, Healthie, Canvas, Epic [[Metriport](https://metriport.com/networks/ehrs)] | Clinical record + **national networks** (Carequality / CommonWell) | Clinical items (conditions, medications, allergies, immunizations); "support differs by EHR"; **not documents or appointments** | Not published | Turnkey per EHR | Best for **documentation packets**, not auth write-back |
| Keragon | athena, ModMed, Elation, DrChrono, Healthie [[Keragon](https://keragon.com/software/ehr)] | Via workflows | Unclear per EHR | From ~$99/month (third-party listing), BAA on paid plans [[Capterra](https://www.capterra.com/p/10018678/Keragon/)] | Fast (no code) | Workflow glue, not an API foundation |
| Particle / Health Gorilla | Network-wide records (see 4b) | Documents via networks | Network contribution only, **no EHR write-back** [[comparison](https://nirmitee.io/blog/particle-health-vs-health-gorilla-vs-redox-vs-direct-ehr-integration/)] | Contracted | Network onboarding | Records only |

**Recommendation: NexHealth Synchronizer as the single integration for go-live.**
- **Covers the core prior-auth specialty segment:** ModMed (ortho, derm, ophtho, GI) plus athenahealth, eClinicalWorks and NextGen Office.
- **Cheapest path to production:** pay per call, free sandbox, no annual minimum published.
- **Writes back what prior auth needs:** the call summary and auth result as a **PDF in the patient chart**, and appointment updates. Reads the scheduled services and insurance that create the work.

**Prior-auth flow on NexHealth**
1. A webhook or daily pull of upcoming appointments (type, provider, date).
2. Read the patient and insurance coverage. CarePlix creates a task when the appointment type or service needs auth.
3. Readiness checks, payer API check, and a call if needed (existing).
4. Write the result PDF to the patient's documents, plus an appointment note/flag where the EHR supports it **[verify per EHR]**.
5. Staff see it in their own EHR, so no new screen is required.

**Indicative cost:** roughly 5–10 API calls per authorization (appointments page, patient, coverage, document upload, plus webhook handling) is **~$0.50–$1.00 per auth** at $0.10 per call. Verify against real call patterns in the sandbox.

**Gaps and how we cover them**
- AdvancedMD, Tebra, DrChrono, Elation and Greenway are not on NexHealth's documented list. Cover them with **P1 worklist import** (CSV/SFTP/API), then Redox for paying customers.
- No documented write to a dedicated *authorization number* field. Write a structured PDF plus a note now; confirm field-level write per EHR with NexHealth.
- **SOC 2 Type I** (not Type II) on the vendor page. A **BAA** must be executed before any PHI.

**Confirm with NexHealth before committing:** a BAA; volume pricing; per-EHR write depth (appointment notes, custom fields, document categories); webhook coverage for medical EHRs; and the practice onboarding steps for athenahealth, eCW, ModMed and NextGen Office.

## 4b. The read side: records networks (Particle, Health Gorilla, Metriport)

NexHealth covers **the practice's own chart** (read and write). Records networks cover **everything outside it**: what other hospitals, specialists, labs and pharmacies hold about the same patient. Prior auth needs both, because payers ask for history the ordering practice often doesn't have.

### What the networks are, and where the data actually comes from

All three are on-ramps to the same national exchange fabric. None of them is a data source itself:

| Source behind the network | What it typically returns |
|---|---|
| **Carequality, CommonWell, eHealth Exchange** (document query: XCPD patient match, then XCA retrieve) | **C-CDA documents** from responding organisations: CCD summaries, progress notes, discharge summaries, consult and referral notes, results. Sometimes PDFs. |
| **TEFCA QHINs** (federal framework, live since Dec 2023) | The same document exchange under one governance model; the scope is USCDI (labs, notes, problems, meds, allergies, procedures, vitals). |
| **ADT feeds** (state HIEs, hospital partners) | Admit, discharge and transfer events, near real time. |
| **Pharmacy (Surescripts)** | Medication **fill history**, the strongest evidence for step therapy. |
| **Lab networks** | Results from reference labs and hospital labs. |

Each vendor then **normalises the C-CDAs into FHIR R4** (Patient, Encounter, Condition, MedicationRequest/Statement, AllergyIntolerance, Immunization, Observation, DiagnosticReport, Procedure, DocumentReference), deduplicates them and keeps a pointer to the original document.

**What the networks do not give you:**
- Payer data: eligibility, coverage, auth status or claims. That stays with Stedi/Availity/PAS.
- Imaging pixels (DICOM): you get reports only.
- Records from organisations that don't respond. Epic and the large health systems respond well; small practices on non-participating EHRs often don't.
- Clean data. Expect duplicates, local codes, missing dates and a minutes-long async query.

### The three vendors (31 Oct 2026)

| | **Particle Health** | **Health Gorilla** | **Metriport** |
|---|---|---|---|
| Networks reached | Carequality, CommonWell, eHealth Exchange, TEFCA partners, state HIEs (e.g. Healthix, Manifest MedEx), **Surescripts**, ADT partners [[Particle](https://particlehealth.com/blog/particle-health-expands-capabilities-to-allow-health-platform-companies-to-offer-interoperability-at-scale), [press kit](https://go.particlehealth.com/press-kit)] | **Designated QHIN** under TEFCA, plus national networks; diagnostic/lab ordering network [[QHIN](https://healthgorilla.com/blog/health-gorilla-achieves-federal-designation-as-a-qualified-health-information-network), [docs](https://apidocs.healthgorilla.com/getting-started/national-networks/qhin)] | Carequality (implementer), CommonWell (service adopter), HIEs; **Candidate QHIN** in pre-production testing, not yet designated [[Metriport](https://metriport.com/medical), [directory](https://healthcarediscovery.ai/companies/metriport/)] |
| Output formats | FHIR R4, C-CDA, **FLAT** (tabular), **Deltas** (what changed), HL7v2 for ADT [[Signal docs](https://docs.particlehealth.com/docs/particle-signal)] | FHIR R4 (recommended; synchronous) and STU3 (async); `$p360-retrieve` returns a normalised, deduplicated longitudinal record; `$export-ccda`; originals via DocumentReference/Binary [[Patient360](https://developer.healthgorilla.com/docs/patient360)] | Consolidated FHIR R4 after C-CDA conversion; raw documents downloadable; webhooks signed with HMAC (`x-metriport-signature`) [[webhooks](https://docs.metriport.com/medical-api/handling-data/webhooks-with-nq)] |
| Event triggers | **Signal**: ADT alerts as CloudEvents webhooks on subscribed patients (needs a contract with the ADT partner) [[Signal alerts](https://docs.particlehealth.com/docs/signal-alerts)] | ADT feeds and referrals in the API set [[report card](https://supergood.ai/api-report-card/health-gorilla)] | ADT among its sources (verify the product depth) |
| Purposes of use | Treatment-centred. History: Epic's 2024 complaint led Carequality to bar two Particle customers for non-treatment queries, and Carequality tightened its treatment criteria; Particle's antitrust suit against Epic continues [[Fierce](https://www.fiercehealthcare.com/health-tech/particle-healths-antitrust-lawsuit-against-epic-moves-forward-after-judge-dismisses), [Becker's](https://www.beckershospitalreview.com/ehrs/carequality-releases-findings-in-epic-startup-dispute-12-notes.html)] | States support for Treatment, Individual Access, **Payment**, **Health Care Operations**, Public Health and Government Benefits, and enforces TEFCA purpose rules in request processing [[compliance](https://apidocs.healthgorilla.com/support/security-practices/regulatory-compliance)] | Production access reportedly **scoped to Treatment** (third-party listing; confirm with Metriport) |
| EHR write-back | None (contributes documents to the network) | None documented | Limited clinical items on some EHRs (see 4a) |
| Pricing | Contracted, not published | Contracted, not published; BAA and case-by-case vetting before production [[report card](https://supergood.ai/api-report-card/health-gorilla)] | Sales-led hosted plans by patient volume; **open-source code** [[plans](https://apis.io/plans/metriport/metriport-plans-pricing/)] |
| Best at | Breadth, **med fill history**, ADT triggers, change feeds | **Compliance posture** (QHIN, multiple purposes), normalisation with provenance, labs | **Cost and developer speed**, open source, also has EHR connectors |

### The critical question: are we allowed to query for prior auth?

- **HIPAA puts prior auth under *payment*.** The definition of payment in 45 CFR 164.501 includes utilization review, precertification and preauthorization.
- **The networks are built around *treatment*.**
  - TEFCA requires a response for Treatment and Individual Access only. Payment and Health Care Operations are authorized purposes, but a response is optional [[RCE FAQ](https://rce.sequoiaproject.org/rce/faqs/), [Exchange Purposes SOP v4.1](https://rce.sequoiaproject.org/wp-content/uploads/2025/12/SOP-Exchange-Purposes-v4.1.pdf)].
  - TEFCA's July 2026 Treatment SOP v2.0 widened the required-response tier to all vetted covered-entity providers and their delegates [[Centauri](https://www.centaurihs.com/tefca-just-opened-the-door-wider-on-treatment-exchange/)].
  - Prior auth is still listed as a "coming soon" payment use case by at least one large vendor [[Epic OEC06](https://vendorservices.epic.com/Resources/OEC06)].
- **Misstating the purpose is the failure mode that got Particle customers removed from Carequality.**

**What this means for CarePlix (to confirm with counsel):**
1. **Query under Treatment only when there is a real treating relationship.** CarePlix is the business associate of the ordering practice, a covered-entity provider. The query is tied to an active order or referral in that practice's EHR, for example "chart prep for the ordered MRI or infusion", and the requesting clinician's NPI is recorded.
2. **Re-use, don't re-query.** Once records are lawfully in the practice's possession for treatment, the practice may use them for its own payment activity, which includes assembling the PA packet. We never fire a query whose only purpose is building a packet.
3. **Use the Payment purpose where it is supported,** for example through Health Gorilla's QHIN, accepting that responses are optional and yield will be lower.
4. **Enforce this in code, not just in policy.** A network query is refused unless the task has a source order or encounter from the EHR, a treating-provider NPI and a recorded purpose code. Every query is audited with the purpose and outcome, with no payloads.

### What network data buys prior auth (where it changes the outcome)

| Payer requirement | Network evidence |
|---|---|
| **Step therapy / failed conservative care** (e.g. biologics, GLP-1, spine imaging) | Surescripts fill history; MedicationStatements from other providers; PT/chiro notes |
| Prior imaging and results | Outside radiology reports (DiagnosticReport) |
| Lab thresholds (A1c, BMI, LDL, sleep study AHI) | Observations from outside labs |
| Specialist evaluation | Consult notes from other organisations |
| Recent hospitalisation (post-acute, home health, DME) | ADT events and discharge summaries |
| Diagnosis history and duration | Problem lists (Conditions) across sources |

This complements the CMS Coverage connector: the **LCD/NCD gives the checklist**, the **EHR (NexHealth) and the networks give the evidence**, and a text agent (Claude) maps each criterion to a cited source document. Missing items become the documentation request *before* the call, not after the payer denies.

### How it plugs into CarePlix

```
EHR order/appointment (NexHealth) ──► Task
        │ demographics + treating NPI + order ref (purpose guard)
        ▼
records_network connector (one adapter per vendor)
   1. match patient (name, DOB, sex, address)  ── async ──►  network query
   2. webhook  ──► careplix-webhooks (public Cloud Run, signature-verified,
                   no IAP; writes to the outbox only)
   3. fetch consolidated FHIR + DocumentReference list
        ▼
Evidence extractor (Claude, cited) ──► criteria checklist (from CMS Coverage / payer policy)
        ▼
Readiness card + call brief + packet manifest (approval-gated send)
```

- **New connection kind `records_network`.** The adapter is selected by `vendor` (particle | health_gorilla | metriport) and reuses the IntegrationWorker phases: `pre_call` for the query, `post_call` for gap checks.
- **Webhooks need a public ingress,** because the Studio sits behind IAP. Add a small `careplix-webhooks` service that only verifies signatures (Metriport HMAC; Particle CloudEvents with JWT) and enqueues. Polling is the fallback (Health Gorilla STU3 supports a polling location).
- **Store derived facts with provenance, not whole records.** Each fact keeps the source organisation, document ID, date and a hash, and maps to `Finding.source = "network"` with the `system_verified` evidence level. Raw documents are fetched on demand for the packet.
- **PHI gate.** This is the first connector that brings real clinical content. It cannot go past sandbox until G4: BAA with the vendor, Carequality/TEFCA onboarding under the practice's participation, purpose guard in place, and retention policy set.

### Recommendation

1. **Go-live stays on NexHealth alone.** The practice's own chart already holds most PA documentation for the ordering clinician.
2. **Phase 2: add one records network, chosen on quotes.**
   - **Health Gorilla**, if compliance footing matters most. It is a designated QHIN, explicitly supports Payment and HCO purposes, and has strong normalisation and provenance. It is the safest choice for a payment-adjacent product.
   - **Metriport**, if cost matters most. It is open source and has the lowest integration effort, but it is treatment-scoped and not yet a designated QHIN.
   - **Particle**, if med fill history (step therapy) and ADT triggers are the main value. Its purpose-of-use history brings extra scrutiny.
3. **Ask each vendor before signing:**
   - purposes of use allowed for our model (BA of the ordering provider);
   - typical response and match rates for small-practice patients;
   - per-patient or per-query price;
   - sandbox with synthetic patients;
   - BAA and SOC 2 Type II;
   - whether Surescripts med history is included;
   - webhook signing method.

## 5. How it fits the existing architecture

```
Integration Hub (existing gateway: per-workspace connections, audit, evidence)
├── worklist_import      CSV / SFTP / REST            → tasks            (P1)
├── fhir_g10_read        SMART Backend Services        → case context     (P2)  [client built]
├── vendor_api           athena / eCW / ModMed / NG    → read + write     (P3)
├── hl7v2_interface      ADT/SIU/ORM in, MDM/DFT out   → read + write     (P3–P5)
├── records_network      Health Gorilla / Particle     → documents        (P4)
├── unified_api          Redox (+Kno2)                 → read + write     (P5)
├── clearinghouse        Stedi / Availity              → 270/271/276/278  (parallel)
└── payer_pas            Da Vinci PAS                  → status/submit    [built]
```

Every connector implements the same **see → validate → act → verify** contract. Writes default to **L1 (human approval)**, are idempotent, and are confirmed by reading back.

## 6. Decisions and risks

1. **Which two EHR vendors to partner with first.** Decide from the first 10 target customers' systems; I recommend asking prospects before committing to partner fees.
2. **Partner fees and contracts:** athenahealth non-certified APIs, NextGen Distributor, eCW write contracts. Pricing is not published; budget time for business development.
3. **Per-practice authorisation of (g)(10) apps:** the practice admin must approve, and quality and coverage vary by vendor.
4. **Records-network purposes of use:** confirm that prior-authorization use (payment / operations) is permitted before relying on networks **[verify]**.
5. **BAAs before any of this touches real patients** (G4 gate). Browser automation also needs credential custody and vendor terms review.
6. **HTI-4:** EHR-native electronic prior auth reduces the value of "submission". Our differentiation is follow-up, validation and voice.
