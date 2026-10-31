# G0 Foundation Runbook

**Goal (doc 08 G0):** get account/project access, model, routes and synthetic fixtures in place,
with readiness checks and a connection inventory backed by evidence.
**Exit:** every item below has an owner and an evidence record, and the engineering estimate for
G1–G3 is reissued from what G0 found.

Status key: ☐ not started · ◐ in progress · ☑ done (with evidence path)

## 1. Decisions (owners from doc 01 / doc 08)

| ID | Decision | Owner | Status | Answer / evidence |
|---|---|---|---|---|
| OD01 | SIP carrier, caller-number ownership, STIR/SHAKEN attestation, DTMF (RFC 2833), BAA availability for P | Telephony lead | ☐ | |
| OD02 | D inbound route: LiveKit number (fast, inbound-only) vs carrier DID | Product + telephony | ☐ | |
| OD03 | LiveKit plan, quotas, agent hosting, regions, telephony noise cancellation, BAA eligibility (L8) | Platform lead | ☐ | |
| OD04 | Vertex project/region, Gemini Live model ID + quota, **worker credential method** | Cloud lead | ☐ | |
| OD05 | Owners: verification design, recording, retention, incidents, legal review of AI-voice outbound calls | Product owner | ☐ | |
| OD06 | Identity provider; existing CarePlix platform / case-system integration | Engineering lead | ☐ | |
| — | Confirm managed demo (ADR01) vs hybrid workers on GCP | Engineering lead | ☐ | |

## 2. Environment setup

```bash
cp .env.example .env          # fill in; never commit
make setup                    # uv sync
make check                    # lint + tests (no external services needed)
make db-up                    # local Postgres (requires Docker)
```

GCP (per environment project):

```bash
cd infra/terraform/envs/dev
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init && terraform plan
```

Add secret values out of band, never through Terraform:

```bash
printf '%s' "$VALUE" | gcloud secrets versions add careplix-voice-dev-livekit-api-secret --data-file=-
```

## 3. Probes: run each and keep the evidence record

| # | Command | Proves | Evidence |
|---|---|---|---|
| P1 | `make probe-gemini` from the **worker's credential environment** | Vertex Gemini Live connects and speaks; credential path works (R2) | `evidence/g0/G0-GEMINI-LIVE-*.json` |
| P2 | `make probe-gemini ARGS="--soak-seconds 900"`, then 3600 | Session/GoAway limits under silent "hold" streaming; resumption handles issued (R1, ADR10) | same; record `soak.*` |
| P3 | `make probe-readiness` | LiveKit credentials, outbound trunk, inbound dispatch rule, DB, model config | `evidence/g0/G0-READINESS-*.json` |
| P4 | `make worker-dev`, then in another shell `make browser-session`; join with the printed URL and token (e.g. LiveKit Agents Playground custom connection) | Explicit dispatch → worker → Gemini → two-way browser audio; interruption behaviour by ear | Write a `G0-BROWSER` record by hand from `evidence/template.json` with the room and dispatch IDs |
| P5 | Deploy the worker to LiveKit Cloud, repeat P1 (via job logs) and P4 | Same chain from **hosted** workers, which is the deployment that matters | `G0-HOSTED-WORKER` record |

P1 must run from the same credential path the hosted worker will use. A pass from a laptop with
`gcloud auth application-default login` proves model access, **not** the hosted credential path.

## 4. Questions G0 must answer (write the answers here)

- Exact Gemini Live model ID on Vertex, its region, and its GA/preview status:
- Measured connect time and first-audio time (P1):
- Session limit / GoAway timing during silent streaming (P2):
- Session resumption: are handles issued, and does resume work across a reconnect?
- Hosted-worker credential method that works (federation or rotated key):
- LiveKit agent deployment regions, and the co-location with the Vertex region:
- Carrier selected, test destinations approved, caller number ownership confirmed:
- Inbound route chosen for D:

## 5. Out of scope for G0

No real outbound dialing (G2), no PHI anywhere, no recording of real calls, and no UI beyond what
the probes need. The Mock Payer Line and the counterpart simulator start in G1/G2 and use
`fixtures/synthetic/payer_scripts/`.
