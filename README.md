# CarePlix Voice

A CarePlix-owned platform for task-specific voice agents over browser, inbound and outbound phone,
with an AI Control Studio, an operator workspace and evidence-backed results.

- Requirements baseline: `01_…docx` to `08_…docx` (v1.0, 31 Oct 2026)
- Architecture and delivery plan: [BUILD_PLAN.md](BUILD_PLAN.md)
- Current gate: **G0 Foundation**. See the [G0 runbook](docs/g0/G0_RUNBOOK.md).

> Synthetic data only. No PHI may enter this repository, its fixtures, prompts, screenshots or
> evidence until the G4 PHI gate (doc 07) is passed.

## Layout

| Path | What |
|---|---|
| `apps/api` | FastAPI control plane. G0: `/healthz`, `/readiness`, readiness and browser-session probes |
| `apps/worker` | Generic LiveKit agent worker. G0: Gemini Live spike + Vertex probe |
| `packages/contracts` | JSON Schemas (source of truth) + tool registry + validators |
| `packages/common` | PHI-safe logging, evidence records |
| `fixtures/synthetic` | 10 synthetic prior-auth cases, 3 payer simulator scripts |
| `infra/terraform` | GCP environment (dev skeleton) |
| `infra/livekit` | SIP trunk / dispatch rule templates, worker deployment notes |
| `evidence/` | Local evidence records (git-ignored; doc 08 format) |

## Quick start

```bash
cp .env.example .env
make setup
make check            # ruff + pytest; no external services required
make api              # http://localhost:8080/healthz
```

Probes that need real accounts are listed in the [G0 runbook](docs/g0/G0_RUNBOOK.md#3-probes-run-each-and-keep-the-evidence-record).
