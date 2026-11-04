# Voice Studio: build, test and run any voice agent

**Status:** approved direction (owner, 3 Nov 2026). Build order: Voice Studio first (VS1–VS6), then
end-to-end RCM on the clearinghouse ([RCM_CLEARINGHOUSE_PLAN.md](RCM_CLEARINGHOUSE_PLAN.md)), then the
switch to production keys.
**Rule:** every clinical and RCM fact an agent uses comes from the integration hub (NexHealth now, Practice
OS later) or from what the person running a scenario enters. Voice Studio adds no second source of truth.

## 1. What changes

Today each agent is a fixed template (prior-auth caller, receptionist, inbound line). Voice Studio makes an
agent something you **assemble, prove in a test lab, then deploy**, for any phone task.

| Part | What the Studio sets | Enforced by |
|---|---|---|
| **Persona** | Name, voice, style (warm/brisk, formal/casual), pace, languages, greeting, phrases to avoid | Agent config (L2), within template bounds |
| **Procedure** | A template's procedure (L1); the *errand* template takes its goal and limits from a scenario | Template, server-owned |
| **Knowledge** | Documents, FAQs and pages the agent may answer from, with citations, owner approval and expiry | `search_knowledge` tool, server-side retrieval |
| **Memory** | Per-caller summaries across calls (preferences, open items), sealed, with retention and opt-out | Written after a call, read at claim time |
| **Skills** | Tools: EHR via hub, clearinghouse checks, SMS, transfer… each with an autonomy level (L0–L3) | Tool registry + API authorization |
| **Guardrails** | Allowed and forbidden topics, disclosures (identity first), never-say, escalation, compliance packs | L0 policy + tool policy + post-call judge |

Templates remain the authority boundary: a persona or scenario can never add a tool or disclosure the
template does not allow (SEC06 stays intact).

## 2. Test Lab

- **Scenario**: a reusable test case or real errand: who to call, goal, facts the agent may share, limits,
  success checks, and a script for the other side.
- **Counterpart**: a simulated other party (payer rep, pharmacy tech, airline agent, impatient patient, a rep
  who refuses bots), with its own persona, a phone menu and hold music if the scenario wants them.
- **Run modes**
  1. *Rehearse*: the agent calls the browser and a person plays the other side.
  2. *Simulate*: the agent and an AI counterpart in the same LiveKit room, no phone, many runs in parallel.
  3. *Test line*: the agent dials our own inbound number, answered by the counterpart (full telephony path).
  4. *Live*: a real call to an approved destination.
- **Scorecard** per run
  - *Measured* (from events and findings, never from the model's say-so): goal outcome, time to a person,
    menu presses, holds and hold time, refused actions, duration, response delay, interruptions.
  - *Judged* (AI grader with a rubric): goal achieved, facts accurate (against scenario facts and knowledge),
    guardrails kept, persona kept, conversation quality. Each verdict cites transcript segments.
- **Suites and the publish gate**: a suite is a set of scenarios with thresholds; a new version cannot be
  published if it scores below the current one. Every agent gets a red-team pack (prompt injection, fishing
  for patient data, social engineering, off-topic bait).
- **Showcase mode**: a read-only page (behind IAP) with the live transcript, what the agent did and the
  scorecard, for customer demos.

## 3. Architecture

```
Studio ─ Agents (persona, knowledge, skills, guardrails) ─ Scenarios ─ Suites ─ Runs/Scorecards ─ Showcase
   │                                                                       ▲
   ▼ API: scenarios, runs, judge, knowledge, memory                        │ events, findings, transcript
LiveKit room ── agent worker (role=agent) ⇄ audio ⇄ worker (role=counterpart, hold music, IVR, DTMF)
   │                                                     or browser person, or test line, or real phone
   └── tools → API → hub (EHR), clearinghouse, SMS …  (model proposes, API disposes)
```

- **Counterpart worker**: the same worker image, dispatched with `role=counterpart` and the scenario's
  counterpart persona. It plays a hold-music asset during simulated holds, speaks its menu, and receives the
  agent's DTMF as LiveKit SIP/data events.
- **AI grader**: Gemini on Vertex AI from the API service account (needs `roles/aiplatform.user`; added to
  `deploy-labs.sh setup`). Runs after a call ends; stores the rubric verdicts with evidence ids.
- **Knowledge**: chunks and embeddings (Vertex text embeddings) in Cloud SQL with `pgvector`; SQLite in dev
  uses an in-process cosine search. Content needs an owner, an approval and an expiry, like MediaOS Proof.
- **Memory**: `contact_memories` keyed by a verified identity (hub patient ref or a hashed phone), written by
  a post-call summarizer, read into the call brief as data.

## 4. Milestones (each merged, deployable, with test steps)

| # | Delivers | Proof |
|---|---|---|
| **VS1** | Voice Studio page; scenarios (CRUD, example library); *errand* "Call on my behalf" agent; rehearse and live runs; measured scorecard | Rehearse "move a flight" in the browser; place it live to an approved number; scorecard shows outcome, menu presses, hold |
| **VS2** | Counterpart simulator (agent vs AI in one room) with phone menu, DTMF and hold music; test line on our inbound number | Run "payer: menu → 3 min hold → rep" with no human; hold mode verified end to end |
| **VS3** | AI-judged scorecards (**built 4 Nov 2026**: auto-scored runs, evidence per check and limit, coaching), then suites, publish gate, version comparison | A suite of 10 scenarios runs in parallel; a worse draft is blocked from publishing |
| **VS4** | Persona editor (style, pace, languages, avoid list); guardrail packs; red-team suite | Same scenarios, two personas side by side; injection attempts refused and logged |
| **VS5** | Knowledge (RAG with citations) and `search_knowledge`; accuracy scoring | Upload an FAQ; agent answers only from it; accuracy score per run |
| **VS6** | Caller memory; showcase mode | A second call picks up the first call's open item; a shareable demo page |

Campaigns (batch outbound) arrive with RCM, where they are first needed (balances, reminders).

## 5. Decisions

- AI grader: Gemini (already connected). Claude can be added later as a second grader.
- Avatar: voice only first; a visual talking head for the web widget after VS6.
- Builders: CarePlix staff first; the data model keeps workspace isolation so practices can self-serve later.
