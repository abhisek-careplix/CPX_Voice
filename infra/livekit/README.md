# LiveKit configuration (per environment)

Each environment (dev, staging, prod) uses its **own LiveKit Cloud project** (doc 06). These
files are templates; real trunk credentials live in the secret store and are never committed.

| File | Purpose | Gate |
|---|---|---|
| `outbound-trunk.json.example` | Carrier outbound trunk (OD01) | G2 |
| `inbound-trunk.json.example` | Authenticated carrier inbound trunk (pilot-preferred route) | G2/P |
| `dispatch-rule.json.example` | Routes an inbound number to `careplix-agent` with an opaque `route_id` | G2 |

Rules that apply to all of them:

- Explicit dispatch only (`agentName`). Room prefixes and metadata carry **opaque IDs only**: never
  names, phone numbers or case data (I4).
- The API resolves the workspace from the trusted route, never from the caller (I5). The
  `route_id` in dispatch metadata is cross-checked against the called number on claim (ADR09).
- LiveKit-purchased numbers are inbound-only and cannot forward/transfer (L1). Record that as the
  route's capability; do not expose transfer on that route.

## Commands (LiveKit CLI `lk`, after `lk cloud auth`)

```bash
lk sip outbound create outbound-trunk.json   # copy of the example with real values, outside git
lk sip inbound create inbound-trunk.json
lk sip dispatch create dispatch-rule.json
lk sip outbound list && lk sip dispatch list   # then: make probe-readiness
```

## Worker deployment (OD03 — confirm on the account during G0)

The worker image builds from the repository root with `apps/worker/Dockerfile`. Confirm during G0
how `lk agent create` / `lk agent deploy` take a Dockerfile in a monorepo (build context and
Dockerfile path), the regions available to the project, and how agent secrets are set
(`GEMINI_LIVE_MODEL`, `GCP_PROJECT`, `VERTEX_LOCATION`, `GOOGLE_SERVICE_ACCOUNT_JSON` if federation
is unavailable). Record the answers in `docs/g0/G0_RUNBOOK.md`.
