# G4 PHI gate: production for real clinics

**Status (16 Nov 2026):** careplix-labs is, and stays, sandbox and demo only: synthetic data,
sandbox and test keys, no real patients. Real clinics will run later in a separate Google Cloud
project that does not exist yet. This document is the checklist and the owner's commands for that
project; nothing here is run until the owner decides, and not before every **Blocking** row below
is done.

Production will be its own project (its id goes in `PROJECT=` at the top of
`infra/gcp/deploy-prod.sh`, the one place it is named; the script refuses to run while it is empty)
with its own Cloud SQL, Secret Manager, service accounts, data key,
LiveKit project and settings file (`.env.production`). It runs the same services as labs (Studio,
internal API, voice worker, browser runtime, webhook ingress) and nothing synthetic: no payer
sandbox, no OpenEMR or EHR emulation, no shared trial line, no demo data, no clearinghouse test mode.

## 1. Business associate agreements

| Vendor | What it handles | BAA |
|---|---|---|
| Anthropic | Claude: AI judge, browser agent | Signed |
| Google Cloud | Cloud Run, Cloud SQL, Secret Manager, Vertex AI (Gemini Live, judge), Cloud Logging | Signed |
| Stedi | Clearinghouse: eligibility, claim status | Signed |
| NexHealth | EHR read and write | Signed |
| LiveKit | Call media and SIP | Signed |
| Twilio | Carrier SIP trunk | Signed |

Any new vendor that would see PHI (email provider for anything beyond sign-in codes, fax, team chat,
payments) needs its own BAA first. Resend sends only sign-in codes and invitations, which carry no
PHI; keep it that way.

## 2. What the code enforces (done in this change)

`CAREPLIX_ENV` names the deployment: `dev`, `labs`, `staging` or `production`. Anything else is
refused at startup, so a typo can't skip the production rules (`careplix_api/environment.py`).

**Sandbox deployments** (`dev`, `labs`, `staging`) can never have a production workspace or
connection: `Settings.production_allowed` is false there, and `environment.refuse_production_here`
refuses a production NexHealth connection or clearinghouse production mode (409
`production_not_allowed`). Synthetic shortcuts (payer sandbox, OpenEMR and EHR emulation, Stedi
test mode, the trial line, demo data) stay available. Only a `production` deployment may hold
production workspaces and connections.

**Production refuses to start** with: demo seeding, a payer sandbox URL (cleared explicitly; the
default points at the local sandbox), OpenEMR, the NexHealth sandbox, clearinghouse test mode or
test keys, the shared trial line, a missing session secret or data key (no derived key), DEBUG
logging, and `PLATFORM_ADMINS` (staff must use their careplix.com account).

**Labs refuses to start** with a production NexHealth key, clearinghouse production mode or a
production clearinghouse key. `deploy-labs.sh` keeps its own refusal before touching anything.

**At runtime in production:**

- Every change of state of an EHR write intent (proposed, approved, sending, accepted, written,
  failed, needs review) writes an audit event `production.write_intent` with the actor, kind,
  level, source and previous state. No payload, no patient details.
- Synthetic claims (samples, or a claim not marked `synthetic=False`), synthetic connections and EHR
  emulation are refused when saved (409 `not_in_production`).
- CarePlix staff can't sign in with an email code (403 `mfa_required`); they use their careplix.com
  Google account, where 2-Step Verification is enforced, or IAP.
- Staff are not added to a workspace automatically on first sign-in (in labs they join the lab
  workspace). They join a clinic's workspace on purpose through Platform → Join, which is audited.
- Other code asks `environment.is_production()` / `refuse_in_production("…")` instead of reading the
  setting (for example per-connection sandbox keys in Practice).

**Retention job** (`careplix_api/retention.py`, daily in the Studio): deletes email sign-in codes a
day after they expire, and call transcripts older than `TRANSCRIPT_RETENTION_DAYS` once that is set
(0, the default, keeps them until the policy below is decided).

**Logging without PHI:** `careplix_common.safe_logging` already writes IDs and categories only,
scrubs emails, phone numbers and SSN-shaped values, drops unknown fields, logs exception types
without messages, and hides httpx request lines below 400. Production adds: no DEBUG level.

**Deploy safety** (`infra/gcp/deploy-prod.sh`): reads `.env.production` only and refuses the same
synthetic settings before any gcloud command, plus the labs LiveKit project (a worker registered on
the wrong project would take the other environment's calls). Builds only a clean commit that is on
`origin/main`. Deploys only the tag `build` recorded, only if its images exist, never a `-dirty`
build, and only after the owner types the project id.

**Infrastructure in `setup`:** one service account per service with only the roles and secrets it
uses (the internal API has its own identity; no service-account keys); Cloud SQL with high
availability, daily backups kept 30 days, point-in-time recovery (7 days), encrypted connections
only, deletion protection and `max_connections=100`, with a per-service connection budget whose
rollout peak (60) leaves headroom; a production data key generated in the project, never copied from
labs; the production NexHealth and clearinghouse keys under their own names (`NEXHEALTH_PROD_API_KEY`
from `cpx-nexhealth-prod-api-key`, `STEDI_PROD_API_KEY` from `cpx-stedi-prod-api-key`; the sandbox
`NEXHEALTH_API_KEY` is refused);
Data Access audit logs for all services; Cloud Logging retention 400 days; Studio behind IAP unless
`STUDIO_PUBLIC=true` opens it to clinic staff with app sign-in.

## 3. What remains

Owner is the CarePlix owner unless named. Dates are proposed; change them in this table.

| Item | What done looks like | Owner | Target | Blocking |
|---|---|---|---|---|
| Penetration test | External test of the Studio, webhook ingress and sign-in on the production project before real data; findings fixed or accepted in writing | Owner + chosen firm | 11 Dec 2026 | Yes |
| MFA for CarePlix staff | Google Workspace admin: 2-Step Verification enforced for careplix.com (code already refuses staff email codes and extra admin addresses) | Owner | 20 Nov 2026 | Yes |
| MFA for clinic staff | Decide: clinic signs in with its Microsoft or Google tenant (MFA by its own policy, recorded in the onboarding form), or CarePlix adds a second factor to email codes | Owner with the clinic | 27 Nov 2026 | Yes |
| Retention policy | Written periods for transcripts, call events, findings, practice mirror, eligibility and claim records, audit log (HIPAA documentation: 6 years); then set `TRANSCRIPT_RETENTION_DAYS` and extend `retention.py` to the other tables | Owner | 27 Nov 2026 | Yes |
| Backups and restore test | Restore the production Cloud SQL backup to a scratch instance, open it with the Studio, record time to restore; repeat quarterly | Owner | Before first real patient | Yes |
| Incident runbook | Who is paged, how to stop calls (workspace emergency stop), revoke keys (Secret Manager versions), notify the clinic within the BAA's window, and breach assessment steps | Owner | 27 Nov 2026 | Yes |
| Access review | List of everyone with IAM roles on the production project, IAP access and Studio memberships (Platform → workspaces); reviewed and signed monthly; owner/editor roles held by named people only | Owner | Before first real patient, then monthly | Yes |
| Logging without PHI review | After the first pilot week, sample production logs (Logs Explorer, all services) and confirm no names, DOBs, member IDs or transcript text; widen `safe_logging` if anything shows | Owner + engineering | First pilot week | No (follow-up) |
| Database migrations and row-level security | `db.py` still uses `create_all`; Alembic migrations and Postgres RLS (ADR13) before schema changes reach production data | Engineering | Before the second production schema change | No |
| Gemini settings review | Confirm the Vertex AI endpoints and region used are under the Google BAA and that data caching/retention is off (BUILD_PLAN G2) | Owner | 27 Nov 2026 | Yes |
| Recording and disclosure | Decide whether calls are recorded, and the disclosure each agent says at the start of a patient call | Owner with the clinic | 27 Nov 2026 | Yes |
| Calling policy | Which destinations production may dial (the labs allowlist is for synthetic tests) and contact-hours rules for patient outreach | Owner with the clinic | 27 Nov 2026 | Yes |
| Production Twilio trunk and number | A production SIP trunk and number in the BAA-covered Twilio account, connected to the production LiveKit project in Studio → Connections (credentials pass to LiveKit; CarePlix doesn't store them) | Owner | Before first live call | Yes |
| Clinic onboarding | Pilot workspace, NexHealth production connection and its practice IDs, clinic admins invited | Owner + onboarding (W1) | Pilot start | Yes |

## 4. Owner instructions (from your Mac)

Not now: careplix-labs stays sandbox and demo only. These are for the day the owner starts the
separate production project. Every command is safe to re-run.

**1. Choose the project id and name it in the script (a reviewed PR).** Set `PROJECT=` at the top
of `infra/gcp/deploy-prod.sh` to the new id (for example `careplix-prod`, if it is free). Until
that PR merges the script refuses every command. Below, `<PROJECT_ID>` is that id.

**2. Create the project and link billing:**

```bash
gcloud projects create <PROJECT_ID> --name "CarePlix Voice production"
```

```bash
gcloud billing projects link <PROJECT_ID> --billing-account <BILLING_ACCOUNT_ID>
```

Confirm the Google Cloud BAA covers the new project (it covers the account's projects when accepted
at the organization or billing level; check Admin Console → Legal and compliance).

**3. Make the production settings file** (never commit it; keep it on your Mac only):

```bash
cd ~/Desktop/CarePlix_Voice && cp infra/gcp/env.production.example .env.production && chmod 600 .env.production
```

Fill it in with the production LiveKit project, the NexHealth production key and practice IDs, the
production clearinghouse key, and the sign-in settings. Then check it (read-only, no gcloud):

```bash
cd ~/Desktop/CarePlix_Voice && ./infra/gcp/deploy-prod.sh preflight
```

**4. Set up the project** (APIs, registry, service accounts, Cloud SQL, secrets, audit logs).
Cloud SQL creation takes about 10 minutes:

```bash
cd ~/Desktop/CarePlix_Voice && git switch main && git pull && ./infra/gcp/deploy-prod.sh setup
```

**5. Build and deploy** (builds only merged code; deploy asks you to type the project id):

```bash
cd ~/Desktop/CarePlix_Voice && git switch main && git pull && ./infra/gcp/deploy-prod.sh build && ./infra/gcp/deploy-prod.sh deploy
```

The build ends with `built <short sha>`; deploy prints `deploying <short sha>` and must match.

**6. Verify:**

```bash
cd ~/Desktop/CarePlix_Voice && ./infra/gcp/deploy-prod.sh status
```

Every service shows the same build and `production`. Then open the Studio URL the deploy printed,
sign in with your careplix.com Google account (an email code must be refused for staff), and check
that Practice shows no demo workspace, no sample claims and no emulation option that saves.
Production worker logs:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="careplix-worker"' --project <PROJECT_ID> --freshness=1h --limit 100 --format='value(timestamp,severity,textPayload,jsonPayload.message)'
```

A service that refuses its settings fails to start; its log says `CAREPLIX_ENV=production refuses:`
followed by each rule it broke.
