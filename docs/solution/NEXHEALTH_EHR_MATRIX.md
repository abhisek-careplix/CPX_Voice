# EHR coverage matrix: what CarePlix can automate, per EHR

Sources: NexHealth "Supported health record systems", "Testing writes, per system", "Onboard a location" and
"Go-live checklist" (read 1 Nov 2026); OpenEMR verified live on the local stack (31 Oct – 1 Nov 2026).

## 1. Status at a glance

| Path | EHRs | Status today |
|---|---|---|
| **Direct: OpenEMR** | OpenEMR 7 | ✅ **Verified end to end** (sync, eligibility, backfill booking, release by status via the CarePlix Connector module) on a synthetic clinic |
| **NexHealth sandbox** | Dentrix-like demo location (no sync) | ✅ Reads and accepted writes verified; writes never reach an EHR, by design |
| **NexHealth production** | 15 systems below | 🔑 Account upgraded. **Each EHR needs a connected practice** before anything can be verified; the key alone reaches no EHR |

A production key reaches only the institutions you create (or that approve your access request) in the
Developer Portal. There is no production "test EHR": end-to-end on a given EHR means a practice on that EHR,
or the free Open Dental demo synced to the sandbox (§4).

## 2. The 15 NexHealth systems × CarePlix automations

Every system supports the **read mirror** (practice, providers, visit types, patients, coverage, appointments),
so **eligibility autopilot** (Stedi) runs on all of them. Writes differ:

| EHR | Type | Connects by | Backfill: book | Backfill: release (cancel) | Confirm (reminders) | Check-in | Reschedule (time/note) | Documents (e.g. 271 PDF) | Payments / adjustments |
|---|---|---|---|---|---|---|---|---|---|
| athenahealth | Medical | Cloud: dedicated user + Chrome extension | ✅ | ✅ | ✅ | — | — | Ask NexHealth | — |
| eClinicalWorks (v11.53+, cloud) | Medical | Cloud: dedicated user + Chrome extension | ✅ | ✅ | ✅ | — | — | Ask NexHealth | — |
| ModMed | Medical | Cloud: dedicated user + Chrome extension | ✅ | ✅ | ✅ | ✅ | — | — | — |
| NextGen Office | Medical | Cloud: dedicated user + Chrome extension | ✅ | ✅ | ✅ | ✅ | — | — | — |
| Dentrix (G6.2+) | Dental | Server: Synchronizer + product key | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (ledger sync) |
| Dentrix Enterprise | Dental | Server: NexHealth installs | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (ledger sync) |
| Dentrix Ascend | Dental | Cloud | ✅ | ✅ | ✅ | — | — | ✅ | — |
| Eaglesoft (18+) | Dental | Server: Synchronizer + product key | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (ledger sync) |
| Open Dental (V17+) | Dental | Server: Synchronizer + product key | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (ledger sync) |
| Denticon | Dental | Cloud | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Curve Hero | Dental | Cloud | ✅ existing patients | ✅ | ✅ | — | — | ✅ | — |
| PracticeWorks | Dental | Server: NexHealth installs | ✅ existing patients | ❌ staff cancels | ✅ | — | — | — | — |
| Cloud9 | Ortho | Cloud | ✅ | ✅ | ✅ | — | — | — | — |
| Dolphin | Ortho | Server: NexHealth installs | ✅ existing patients | ✅ | ✅ | — | — | — | — |
| OrthoTrac | Ortho | Server: NexHealth installs | ✅ | ❌ staff cancels | ✅ | — | — | Ask NexHealth | — |

Notes that change how automations behave:
- **Booking reaches the EHR only when the location has `insert_appt_client` enabled** (NexHealth setting).
- **Cloud EHRs sync only while someone at the office is signed in** to the EHR and the NexHealth Chrome
  extension. Writes queued after hours land the next morning.
- **New patients** reach the EHR only with their first booked appointment; Curve Hero, Dolphin and
  PracticeWorks can't take new patients at all. Backfill offers only existing patients, so it's unaffected.
  The receptionist registers a new patient (`POST /patients` with `return_existing_if_match`) and books them
  in one step where the catalog allows it (`patient.create`); **Dentrix and Eaglesoft need the patient's
  gender** (NexHealth stores `Female` otherwise), so the receptionist asks for it there.
- **Placeholders:** a patient created through the API is deleted by NexHealth once the office's real chart
  links to it (`_np` foreign ids on Dentrix and Denticon). The mirror marks it deleted and moves its visits to
  the real chart.
- **Chart alerts** (eligibility "coverage inactive") aren't in NexHealth's per-system table: verify per EHR.
- **Insurance writes and communication logs** are v3.1 beta (Dentrix, Eaglesoft, Open Dental; logs Open Dental
  only) and need NexHealth to enable beta access for the organization.
- CarePlix backfill releases by **cancel, then book**, never by reschedule, so the reschedule column matters
  only for receptionist reschedules (FO-3). There, Denticon, Dentrix, Dentrix Enterprise, Eaglesoft and Open
  Dental move the visit in place (`PATCH start_time/end_time`, once the visit has a `foreign_id`); every other
  EHR books the new time, then cancels the old one.
- **Every `PATCH /appointments` write** (cancel, confirm, check-in, time change) is confirmed only by
  `appointment_updated` naming the field in `previous_attributes`; `.failed`, `.timed_out` and `.incomplete`
  send it to the write queue for a person. Reading the visit back never counts as confirmation.
- **Not used (v3.1 beta only):** forms and form responses, patient insertion, insurance writes. They need
  NexHealth to enable beta access for the organization.

## 3. Making a NexHealth EHR live (per practice)

1. Developer Portal (production mode) → **Institutions → Create Institution** (or **Request access** if the
   practice already uses NexHealth: they approve by email, nothing to install).
2. **Create sync** and choose the EHR:
   - Dentrix / Eaglesoft / Open Dental: the practice runs the Synchronizer with the portal's product key.
   - Other server systems: schedule NexHealth's install team from the portal.
   - Cloud systems: submit the EHR URL plus a dedicated login the practice creates (up to 72 business hours),
     then the practice installs the Chrome extension on scheduling workstations.
3. `make nexhealth-inventory`: the location shows its EHR, and read/write sync turn green.
4. **G4 PHI gate**: BAA with NexHealth (and Stedi, Google Cloud, LiveKit, the carrier), production project.
   Only then does the app switch from the sandbox key to the production key for that practice.
5. Go-live test from NexHealth's checklist: book one appointment for the practice's test patient, wait for
   `appointment_insertion.complete`, have the office confirm it, then cancel it.

## 4. Verifying writes before a pilot practice

- **Open Dental demo on the sandbox** (free; needs a Windows VM): sandbox institution → *Create new sync* →
  Open Dental → run the Synchronizer with the product key. Writes then really land in an EHR and fire
  `*_insertion` webhooks, with synthetic data. This is the only NexHealth EHR testable without a practice.
- **OpenEMR** (direct) already covers the full loop on synthetic data, locally and as the hosted demo.
