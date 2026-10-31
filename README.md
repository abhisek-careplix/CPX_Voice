# CarePlix Voice

A CarePlix-owned platform for task-specific voice agents over browser, inbound and outbound phone,
with an AI Control Studio, an operator workspace and evidence-backed results.

- Requirements baseline: `01_…docx` to `08_…docx` (v1.0, 31 Oct 2026)
- Architecture and delivery plan: [BUILD_PLAN.md](BUILD_PLAN.md)
- G0 checklist: [docs/g0/G0_RUNBOOK.md](docs/g0/G0_RUNBOOK.md)

> Synthetic data only. No PHI may enter this repository, its fixtures, prompts, screenshots or
> evidence until the G4 PHI gate (doc 07) is passed.

## What runs end to end

```
Studio (React) ──/api──▶ CarePlix API (FastAPI) ──rooms, dispatch, SIP──▶ LiveKit Cloud
      ▲   SSE timeline        ▲  claim · tools · events                    │
      │                       └──────────── Agent worker ◀── dispatch ──────┘
      └── browser audio (LiveKit WebRTC) ◀──▶ room ◀──▶ worker ◀──▶ Gemini Live (Vertex AI)
```

1. **Author** an agent in the Studio (instructions, voice, tools, guardrails) and **publish** an
   immutable version. Publishing blocks privilege increases the template does not allow and any
   model other than the approved one.
2. **Browser Test**: the API creates a sandbox attempt and a LiveKit room, explicitly dispatches
   `careplix-agent`, and gives the browser a 15-minute room-scoped token. The worker **claims** the
   attempt, receives the immutable call brief and pinned config, and talks through Gemini Live.
3. **Outbound call**: the operator starts a task, which is idempotent and limited to approved
   destinations. The API waits for the worker's claim and only then dials through the LiveKit SIP
   trunk.
4. **Inbound call**: a LiveKit dispatch rule sends the call to the worker. The worker claims by the
   *called* number, never the caller ID.
5. Every tool call (`send_dtmf`, `record_finding`, `verify_caller`, `end_call`…) is **authorised by
   the API**. Transcript segments, state and findings stream back as events. The Studio shows them
   live over SSE, and they survive a refresh.
6. A **reviewer** checks findings against their transcript evidence, corrects them (append-only),
   and records the business outcome. A finished call is never treated as a finished task.

## Run it

```bash
cp .env.example .env     # fill in the LiveKit, Vertex AI and approved-destination values
make setup               # uv sync + npm ci
make api                 # http://localhost:8080
make studio              # http://localhost:5173  → "Continue as Demo Administrator"
make worker-dev          # agent worker: registers with LiveKit Cloud, claims from the local API
```

The Studio runs without any external accounts: readiness shows what is missing, and actions that
need LiveKit or a phone route explain why they are unavailable. For real conversations you need:

| Setting | Needed for |
|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Browser Test, all calls |
| `GCP_PROJECT`, `VERTEX_LOCATION`, `GEMINI_LIVE_MODEL` (+ ADC or `GOOGLE_SERVICE_ACCOUNT_JSON` on the worker) | The agent's voice. Agents can only publish with this approved model. |
| `LIVEKIT_OUTBOUND_TRUNK_ID`, `APPROVED_TEST_DESTINATIONS` | Outbound calls |
| A LiveKit number with a dispatch rule to `careplix-agent`, added under **Connections & Routes** | Inbound calls |

A worker running locally reaches the API at `CAREPLIX_API_URL`. A worker hosted on LiveKit Cloud
needs a public URL for the API instead, such as a tunnel during development.

## Layout

| Path | What |
|---|---|
| `apps/api` | Control plane: agents and versions, tasks, calls, tools, events (SSE), results, readiness |
| `apps/worker` | Generic LiveKit agent runtime: claim, layered prompt, tool proxies, event reporter |
| `apps/web` | CarePlix Voice Studio (React + LiveKit components) |
| `packages/contracts` | JSON Schemas (source of truth) + versioned tool registry |
| `packages/common` | PHI-safe logging, evidence records |
| `fixtures/synthetic` | 10 synthetic prior-auth cases, 3 payer simulator scripts |
| `infra/terraform`, `infra/livekit` | GCP dev environment, SIP trunk and dispatch templates |

## Checks

```bash
make check               # ruff, pytest (contracts, API lifecycle, worker), web type-check + build
```
