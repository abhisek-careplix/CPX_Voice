# Demo Integration Plan — Healthcare Prior Authorization

**Goal:** a demo that runs end to end on real, reachable systems wherever possible, with synthetic data and clearly labelled stand-ins for systems that aren't publicly reachable.
**Inputs:** [HEALTHCARE_INTEGRATION_LAYER.md](HEALTHCARE_INTEGRATION_LAYER.md) and the integration research of 31 Oct 2026 (sources inline).
**Rule:** synthetic data only. Public test servers (HAPI, SMART launcher) are world-readable.

---

## 1. Demo storyline (what the audience sees)

1. **Case intake.** The operator opens synthetic case *SYN-CASE-0001* (outpatient physical therapy, Northwind Health Plan).
2. **Data checks, before any call.** CarePlix automatically:
   - validates the **provider NPI** against the live **NPPES NPI Registry**;
   - validates the **diagnosis (ICD-10-CM)** and any **HCPCS** codes against **NLM Clinical Tables**;
   - pulls the relevant **Medicare coverage policy (NCD/LCD)** from the **CMS Coverage API**, so the brief lists likely documentation requirements;
   - checks **eligibility and whether prior auth is required** (X12 270/271 via **Stedi test mode**, `authOrCertIndicator`);
   - asks the **payer's Prior Authorization API** (Da Vinci PAS `Claim/$inquire`) for status. In the demo this is the **CarePlix Payer Sandbox**.
3. **The call, only if needed.** The status is pended, so the voice agent calls the payer line (live LiveKit + Gemini Live): it navigates the phone menu, waits on hold, gets the missing-documentation request, and reads back the references.
4. **Validation after the call.** Call findings are compared with the payer record. A match is marked *payer system verified*; a mismatch is marked *conflicting* and routed to review.
5. **Next action.** The agent identifies the requested PT notes in the EHR sandbox (**Oracle Health open FHIR sandbox**: DocumentReference/Observation) and prepares the documentation packet. Sending it (fax or X12 275) is shown as an *approval-gated* step, not executed.
6. **Results.** The reviewer sees every fact with its source: NPPES, NLM, CMS, Stedi, the PAS payer record, or the call transcript. Then they record the business outcome.

---

## 2. Integration catalogue and endpoints

### 2.1 Live in the demo (no signup)

| # | System | Purpose | Endpoint(s) | Auth | CarePlix capability |
|---|---|---|---|---|---|
| 1 | **NPPES NPI Registry** (CMS) | Provider identity, taxonomy, practice address and fax | `GET https://npiregistry.cms.hhs.gov/api/?version=2.1&number={npi}` | None | `provider.npi.validate` (validate) |
| 2 | **NLM Clinical Tables: ICD-10-CM** | Diagnosis code validity and display | `GET https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search?sf=code,name&terms={code}` | None | `code.icd10.validate` |
| 3 | **NLM Clinical Tables: HCPCS Level II** | DME and supply code validity, obsolete flag | `GET https://clinicaltables.nlm.nih.gov/api/hcpcs/v3/search?terms={code}` | None | `code.hcpcs.validate` |
| 4 | **CMS Coverage API** | Medicare NCD/LCD criteria and documentation requirements | `GET https://api.coverage.cms.gov/v1/data/ncd/…`, `/v1/data/lcd/…`; LCD needs a 1-hour token from `/v1/metadata/license-agreement/` (AMA/ADA/AHA terms) | None + licence token | `coverage.policy.lookup` (read) |
| 5 | **Oracle Health open FHIR sandbox** | Clinical documentation (PT notes) for the packet | `https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/` (read-only) | None (open sandbox) | `ehr.documents.find` (read) |
| 6 | **CarePlix Payer Sandbox** (ours) | Payer PA API stand-in (Da Vinci PAS) | `POST {base}/Claim/$inquire`, `$submit`; `GET {base}/metadata` | Google ID token (Cloud) / none (local) | `pa.status.inquire` (validate), **built** |

### 2.2 Live once you create the account (free, self-serve)

| # | System | Purpose | Endpoint | Auth | Notes |
|---|---|---|---|---|---|
| 7 | **Stedi** (test mode) | 270/271 eligibility; prior-auth-required flag (`authOrCertIndicator` Y/N/U) | `POST https://healthcare.us.stedi.com/2026-06-01/eligibility-check` | `Authorization: Key {test key}` | Sandbox: mock payers only (Aetna, Cigna, UHC, CMS). **No 276/277 or 278 in test mode.** |
| 8 | **Availity Demo plan** | Canned 278 Service Reviews and 276/277 claim status | OAuth2 client_credentials (`scope=hipaa`), then the Service Reviews / Claim Statuses APIs | OAuth2 | Auto-approved, 3 TPS / 500 calls per day, canned responses; token path to confirm on signup |
| 9 | **Epic sandbox** (Backend Systems) | Second EHR, signed-JWT client_credentials | `https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4` | SMART Backend Services (our client already supports RS384 assertions) | Key propagation can take hours; a JWKS/JKU URL is required |

### 2.3 Shown as designed, not executed in the demo

| System | Why not live | Demo treatment |
|---|---|---|
| Payer PAS/CRD/DTR production APIs | Mandated from 1 Jan 2027; no public endpoints | CarePlix Payer Sandbox; Inferno PAS kit for conformance (Docker) |
| HL7 Da Vinci CRD/DTR reference implementations | Self-hosted Docker stack | Phase 2 |
| Fax (Sinch Fax API v3, Documo) / X12 275 / CMS esMD | BAA and production accounts needed; esMD needs HIH certification | An approval-gated "Send documentation" step that produces a packet preview only |
| CPT lookups | AMA-licensed | Local stub list for the synthetic cases |

---

## 3. CarePlix capability contract (one row per demo capability)

| Capability | Kind | Inputs (from the case) | Output stored | Evidence |
|---|---|---|---|---|
| `provider.npi.validate` | validate | provider NPI | `confirmed` / `conflicting` / `unverifiable` + name, taxonomy, fax | NPPES response hash + call ID |
| `code.icd10.validate` | validate | ICD-10-CM code(s) | valid/invalid + display name | NLM response |
| `code.hcpcs.validate` | validate | HCPCS code(s) | valid/obsolete + display name | NLM response |
| `coverage.policy.lookup` | read | service keywords / HCPCS | matching NCD/LCD titles and IDs | CMS response |
| `eligibility.check` | validate | member, payer, provider NPI, service type | active coverage, prior-auth-required flag | Stedi 271 |
| `pa.status.inquire` | validate | case (PAS bundle) | status, review action, reference | PAS ClaimResponse (**built**) |
| `ehr.documents.find` | read | patient (sandbox ID), document type | document list (title, date, type) | FHIR Bundle |
| `docs.packet.prepare` | act (L1) | requested docs | packet manifest (no send) | manifest hash |

Every capability runs through the integration gateway: per-workspace connection, timeouts, audit with outcome only (no PHI payloads), and results attached to the task as *system evidence*.

---

## 4. Build sequence

| Step | Scope | Status |
|---|---|---|
| D0 | PAS connector + Payer Sandbox + pre/post-call validation + Studio Integrations | **Done** (local), deploying to careplix-labs |
| D1 | Public reference connectors: NPPES, NLM ICD-10/HCPCS, CMS Coverage, run as a **pre-call "case readiness" check** with results on the task | Next |
| D2 | Oracle Health open-sandbox document finder + a packet-preparation step gated on approval | Next |
| D3 | Stedi eligibility connector (test key in Secret Manager) | Needs a Stedi account |
| D4 | Availity 278/276 connector (Demo plan) | Needs an Availity account |
| D5 | Epic Backend Systems connector | Needs an Epic app registration and JWKS hosting |
| D6 | Inferno PAS conformance in CI; CRD/DTR reference stack | Needs Docker |

## 5. Accounts needed from CarePlix (to unlock D3–D5)

1. **Stedi**: sign up with a work email (free sandbox, test key). I store the key in Secret Manager.
2. **Availity Developer Portal**: Demo plan (auto-approved). Provides client ID and secret.
3. **Epic on FHIR**: register a Backend Systems app; CarePlix hosts a JWKS URL (the Studio service can serve it).

## 6. Risks

- Public sandboxes (Oracle open, HAPI) can be slow, rate-limited or wiped, so the demo falls back to cached synthetic responses labelled *cached*.
- Availity and Stedi responses are canned and may not match real payer behaviour; label them clearly.
- CMS LCD content carries AMA/ADA/AHA licence terms; show summaries and links, not full text.
- CPT licensing: no live CPT validation.

## Sources

- NPPES API — https://npiregistry.cms.hhs.gov/api-page
- NLM Clinical Tables ICD-10-CM — https://clinicaltables.nlm.nih.gov/apidoc/icd10cm/v3/doc.html
- NLM Clinical Tables HCPCS — https://clinicaltables.nlm.nih.gov/apidoc/hcpcs/v3/doc.html
- CMS Coverage API — https://api.coverage.cms.gov/docs/
- Stedi sandbox — https://www.stedi.com/docs/healthcare/sandbox-account ; prior-auth flag — https://www.stedi.com/blog/how-to-check-for-prior-authorization-requirements-in-a-271-eligibility-response
- Availity developer support — https://developer.availity.com/partner/support
- Oracle Health service root URLs — https://docs.oracle.com/en/industries/health/millennium-platform-apis/mfrap/srv_root_url.html
- Epic SMART sandboxes overview — https://good-neighbor.smarthealthit.org/sandboxes/
- CMS esMD — https://www.cms.gov/data-research/computer-data-systems/esmd/esmd-health-information-handlers
