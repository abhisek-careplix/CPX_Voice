# How to onboard a clinic

For the owner and CarePlix staff. Covers every medical EHR NexHealth connects (athenahealth,
eClinicalWorks cloud v11.53+, ModMed, NextGen Office) and the 11 dental/ortho systems, which use the
same steps. Code: `apps/api/src/careplix_api/practice/{onboarding,golive,automation,setup_api}.py`,
Studio: Settings → EHR hub.

## Before you start

| Need | Where it comes from |
| --- | --- |
| The clinic has authorised CarePlix in NexHealth | NexHealth sends the practice an authorisation request for our app; their admin approves it. Until NexHealth approves our v3.1 onboarding link, this is done by email with NexHealth. |
| The practice's **subdomain** and **location id** | NexHealth Developer Portal → the practice → its locations. |
| NexHealth key for the env | Sandbox: `NEXHEALTH_API_KEY`. Production: `NEXHEALTH_PROD_API_KEY`. A practice only ever reads its own env's key; there is no fallback between them. careplix-labs is sandbox and demo only and never stores a production key, so there the form offers Sandbox only. Real clinics will be added the same way in the separate production GCP project. |
| `WEBHOOK_PUBLIC_URL` | The public `careplix-webhooks` URL. Without it a practice can't be confirmed, so it can't go live. |
| A **test patient** in the clinic's EHR | Ask the clinic to create a chart named e.g. "Pat Test" and give you its NexHealth patient id. The go-live test only accepts a patient whose name contains "Test". |
| For cloud EHRs (athena, eCW, ModMed, NextGen) | A dedicated EHR user for NexHealth, and the NexHealth Chrome extension signed in at the front desk. They sync only while the office is signed in. |
| In NexHealth, for the location | **Insert appointments** turned on (`insert_appt_client`). Without it bookings stay in NexHealth and never reach the EHR. |

## Steps

1. **Workspace.** Platform → create the clinic's workspace (product line Practice) and invite its
   admin. A new workspace starts with no lab EHR.
2. **Add the practice.** EHR hub → **Add practice** (or pick the EHR in the catalog → *Add a … practice*):
   EHR, subdomain, location id, environment. Sandbox practices can be added by the workspace admin;
   **production practices only by CarePlix staff** (the subdomain alone doesn't prove the practice
   belongs to that workspace, and our production key reaches every practice that authorised it).
   One NexHealth location can belong to only one workspace.
3. **Watch setup.** The practice's *Setup* sheet shows: added → NexHealth reaches the location →
   signed webhook registered → first full sync (runs in the background; large practices take a while).
   A failed step says why; fix it (e.g. the location id) and press *Verify and register again*.
   If NexHealth reports a different EHR from the one chosen, the checklist flags it.
4. **Go-live checklist** (same sheet). Production practices run **no automation** (receptionist
   bookings/cancels/reschedules, cancellation backfill, eligibility autopilot) until every item is
   green:
   - Connected (location found under that subdomain)
   - Appointment insertion is on
   - Read sync green, write sync green
   - Webhooks registered and delivering
   - First full sync finished
   - **Test writes**: enter the test patient's id and *Run the go-live test*. CarePlix books a visit at
     least a day out, confirms it, then cancels it, waiting each time for the EHR to confirm through
     NexHealth's webhooks. On cloud EHRs each step waits for the office to be signed in, so start it
     during office hours. Where an EHR can't take a step (e.g. cancel on OrthoTrac), it becomes a
     staff task and the note says so.

   When the last item passes the practice shows **Live** and automations start. Manual actions in the
   Studio (approving a write, *Check now*) always work. Sandbox practices show the checklist but don't
   need it. To demo it in labs, add a NexHealth sandbox practice and run the test on a sandbox
   patient whose name contains "Test": the sync items read "not applicable" and each write counts once
   NexHealth accepts it, since there is no EHR behind the sandbox.
5. **Automation settings** (EHR hub card, per workspace; the env values are the defaults):
   cancellations by approval or automatic, cancellation backfill, eligibility autopilot, default sync
   interval (each practice can set its own in its Setup sheet), clearinghouse test or production
   (production needs `STEDI_PROD_API_KEY` in that environment).

## What to expect per EHR

| EHR | Book | New patient | Cancel | Confirm | Check-in | Change time | Writes confirmed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| athenahealth | yes | yes | yes | yes | staff task | book new + cancel old | office hours |
| eClinicalWorks (cloud) | yes | yes | yes | yes | staff task | book new + cancel old | office hours |
| ModMed | yes | yes | yes | yes | yes | book new + cancel old | office hours |
| NextGen Office | yes | yes | yes | yes | yes | book new + cancel old | office hours |

A cloud EHR's write that isn't confirmed after two **office hours** (weekdays 8:00–18:00 in the
practice's time zone) goes to a person; a write sent Friday evening is not flagged over the weekend.
The full catalog is in `NEXHEALTH_EHR_MATRIX.md`.

## If something fails

- *"This environment has no NexHealth production key"*: the production key isn't deployed there.
- *Verify fails*: wrong subdomain or location id, or the practice hasn't authorised CarePlix yet.
- *Read/write sync not green*: the Synchronizer (server EHRs) or Chrome extension (cloud EHRs) isn't
  running; the clinic's NexHealth contact fixes it.
- *Test write stuck on "confirm"/"cancel"*: the office isn't signed in to the EHR and extension, or
  webhooks aren't delivering (check the Setup sheet's webhook item).
- Worker logs:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="careplix-worker"' --project careplix-labs --freshness=1h --limit 100 --format='value(timestamp,severity,textPayload,jsonPayload.message)'
```

Not covered yet: document upload, charges/claims/payments mirror, chart alerts.
