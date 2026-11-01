# CarePlix Connect: the EHR integration hub for every CarePlix product

**Goal.** One layer that connects any practice's EHR once and lets every CarePlix product (Voice, CardiacPro
and the next ones) read the schedule and chart, write back safely, and drive AI agents through the same tools.
Products plug in through an API, agent tools and events; none of them talks to an EHR or NexHealth directly.

**Starting point (built and verified).** The Voice practice layer already has the hard parts: a PHI-sealed
mirror, a sync engine, a write-intent engine with approvals and confirmation, webhook ingress, eligibility
autopilot and backfill, with two connectors, **OpenEMR (direct)** and **NexHealth (hub to 15 EHRs)**.
CarePlix Connect generalizes that code, it doesn't restart it.

## 1. What changes

| Today (Voice-only practice layer) | CarePlix Connect |
|---|---|
| `if kind == "openemr" … else …` inside sync, writes and backfill | A **connector SDK**: each EHR path is one class behind one contract |
| Capability notes in docs | A **capability catalog** as data (16 EHRs × operations), enforced at write time |
| Tables keyed by Voice workspace | **Tenancy**: organization → practice → location, with per-product access scopes |
| Studio pages call internal endpoints | **Hub API v1** + **agent tools (MCP)** + **events**, used by every product |
| Practice set up by hand in .env | **Onboarding**: catalog → add practice → hosted EHR setup → probe → go-live test |
| OpenEMR is one EHR | OpenEMR is also the **EHR emulator**: run any EHR's capability profile on it for demos and tests |

## 2. Architecture

```
 Products ── Voice agent ── CardiacPro ── future products ── partner apps
     │  REST v1 (OpenAPI SDKs)   │  Agent tools (MCP / JSON schema)   │  Events (Pub/Sub, signed webhooks)
 ┌───┴──────────────────────────────────────────────────────────────────────────────────────┐
 │ CarePlix Connect                                                                          │
 │  Access: product clients, scopes, practice consent, autonomy levels (L0 read … L3 human)  │
 │  Engines: sync (webhook-first + reconcile) · write intents (approve → send → confirm)     │
 │           · eligibility (Stedi) · events out · audit · PHI sealing                        │
 │  Canonical model: FHIR R4 subset (Patient, Practitioner, Location, Organization,          │
 │           Coverage, Appointment, Slot, DocumentReference, Flag) + CarePlix extensions     │
 │  Capability catalog: EHR profile × operation → supported | unsupported(fallback) | verify │
 │  Onboarding: catalog, practice setup links, progress, probe, go-live test                 │
 │  Connector SDK ──┬── OpenEMR (direct REST/FHIR + CarePlix Connector module)               │
 │                  ├── NexHealth (15 EHRs; Synchronizer / cloud sync / Chrome extension)    │
 │                  ├── next: athenahealth direct, SMART-on-FHIR (Epic, Oracle, eCW)          │
 │                  └── read-only: records networks (Particle / Health Gorilla / Metriport)  │
 └───────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Connector SDK (the "synchronizer" contract)

Every EHR path implements the same small interface; engines never branch on the EHR:

| Method | Purpose |
|---|---|
| `profile()` | Which catalog profile applies (e.g. `nexhealth:athenahealth`, `openemr`) |
| `probe()` | Live health: reads that work, sync read/write status, writes confirmable |
| `pull(resources, since)` | Yields canonical records (full or incremental) for the mirror |
| `available_slots(query)` | Bookable times (provider, visit type, window) |
| `write(op)` | Book, cancel, confirm, check-in, reschedule, chart note, document, payment → **receipt** (`accepted` or `written`) |
| `confirm(receipt)` | Read-back or insertion webhook → `written`, `failed` or `not_reported` |
| `parse_webhook(req)` | Verified change events → which records to re-read |

Adding an EHR path = one connector + its catalog rows + a fake for tests. The existing OpenEMR and NexHealth
code moves behind this contract unchanged in behaviour.

### 2.2 Capability catalog and EHR emulation

- **Catalog rows** come from vendor docs (see `NEXHEALTH_EHR_MATRIX.md`) and are overridden by each
  connection's live probe. Example: `nexhealth:orthotrac` → `appointment.cancel = unsupported(fallback: staff task)`.
- **The hub enforces the catalog**: an unsupported write never leaves the hub. It becomes a staff task with the
  reason, and products see the same answer through the API and the tools.
- **Emulation on OpenEMR**: a connection can run OpenEMR under another EHR's profile
  (`emulate: nexhealth:athenahealth`). The hub then allows only that EHR's operations and mimics its
  confirmation behaviour (accepted → written after a delay; cloud EHRs confirm only in office hours).
  One OpenEMR clinic can show all 16 EHRs side by side, clearly labelled "emulated". This is how we demo and
  regression-test every supported EHR before a real practice exists.

### 2.3 Tenancy, access and safety

- **Organization → practice → location**, each practice with one or more connections.
- **Product clients** (Voice, CardiacPro, …) get scopes per practice: `schedule.read`, `appointments.write`,
  `coverage.read`, `chart.write`, `payments.write`. A practice consents per product.
- **Autonomy levels** stay as today: L0 reads automatic; L2 non-financial writes automatic after pilot sign-off;
  L3 financial or payer-facing writes need a human approval. Tools return `pending_approval` for L3.
- **PHI**: field-level sealing (Fernet today, Cloud KMS per tenant next), minimum-necessary mirror, audit of
  every read of contact details, no payloads in logs. **No EHR passwords are ever held by CarePlix**: NexHealth's
  hosted setup collects them (2.5); direct connectors use OAuth clients stored in Secret Manager.
- **G4 PHI gate** before any real practice: BAAs (NexHealth, Stedi, Google Cloud, LiveKit, carrier),
  production project, pen test of the public webhook and API surfaces.

### 2.4 Interfaces for product groups

1. **REST API v1** (OpenAPI; Python and TypeScript SDKs generated):
   `GET /v1/catalog` · `POST /v1/practices` · `POST /v1/practices/{id}/onboardings` · `GET /v1/practices/{id}/health`
   · `GET /v1/practices/{id}/patients:search` · `GET …/slots` · `POST …/appointments` · `PATCH …/appointments/{id}`
   (confirm, cancel, check-in) · `POST …/coverage/{id}/eligibility` · `POST …/chart-notes` · `GET /v1/intents/{id}`.
   Service-to-service auth: Google-signed ID tokens per product service account.
2. **Agent tools** (same operations, JSON schemas, served over MCP): `find_patient`, `find_slots`,
   `book_appointment`, `cancel_appointment`, `confirm_appointment`, `check_eligibility`, `add_chart_note`,
   `practice_info`. The Voice receptionist (FO-3) is the first consumer; any product's agent can mount them.
3. **Events**: one Pub/Sub topic plus optional signed webhooks:
   `appointment.created|cancelled|confirmed`, `slot.opened`, `eligibility.changed`, `write.written|failed`,
   `practice.sync_degraded`. Backfill and reminders subscribe instead of polling.

### 2.5 Practice onboarding (how a real practice goes live)

1. Staff pick the practice's EHR from the catalog (16 entries, each showing what CarePlix can do there).
2. **NexHealth EHRs**: the hub calls NexHealth's onboarding API (`POST /onboardings`, v3.1 beta, needs
   NexHealth approval) and gets a **setup link**. The practice opens it and enters their EHR credentials or runs
   the installer **inside NexHealth's flow**. The hub tracks progress (`ehr_selected → credentials_entered →
   initial_read_complete → installing_chrome_extension → complete`) and shows errors with the responsible party.
   Until the beta is approved, the same steps run in the Developer Portal and staff paste the subdomain and location.
3. **Direct EHRs** (OpenEMR now; athena / SMART-on-FHIR next): admin registers the OAuth client; secrets go
   to Secret Manager.
4. **Probe**: reads, sync status, writes confirmable; catalog overrides recorded.
5. **Go-live test** (NexHealth's checklist): book one appointment for the practice's test patient, wait for
   confirmation, office confirms, cancel it. The practice is then live for the scopes it approved.

## 3. Deployment shape

- **Now**: a Python package inside this repo (`careplix_connect`), used by the Voice API. No behaviour change.
- **Then**: its own Cloud Run service `careplix-connect` (own Cloud SQL schema, own public webhook ingress,
  IAP for the admin UI), with Voice as the first client over the API. Other products integrate through the
  SDK, MCP tools and Pub/Sub, never by importing code.

## 4. Build plan

| Phase | Delivers | Proof |
|---|---|---|
| **H1 Connector SDK + catalog + emulation** | OpenEMR and NexHealth behind one contract; the 16-EHR catalog as data and enforced; OpenEMR emulation of any profile; Studio **Integrations hub** page (catalog, each EHR's status: live / emulated / sandbox / needs practice) | Existing 133 tests pass unchanged; new tests per profile (e.g. OrthoTrac cancel → staff task) |
| **H2 Hub API v1 + agent tools** | REST v1, MCP tool server, product scopes; Voice receptionist books and cancels through the tools | Voice agent books into OpenEMR (and an emulated athena) on a call |
| **H3 Onboarding** | Catalog → add practice → NexHealth setup link → progress → probe → go-live test | Open Dental demo on the sandbox, then the first pilot practice |
| **H4 Standalone service + events** | `careplix-connect` on Cloud Run, Pub/Sub events, SDKs; a second product group integrated | CardiacPro (or the next product) reads a schedule through the SDK |
| **H5 Direct connectors** | athenahealth direct (full reschedule), SMART-on-FHIR backend (Epic, Oracle Health, eCW) | Vendor sandboxes |

## 5. Needed from outside engineering

- **NexHealth**: beta access for onboardings and v3.1 (email info@nexhealth.com) and `insert_appt_client` on pilot locations.
- **A pilot practice** on one supported EHR, plus BAAs (G4).
- **Product owners**: which product integrates second, and the scopes it needs.
- Optional: Windows VM for the Open Dental demo sync (real EHR writes on synthetic data).
