# Browser agents: learn a task by watching staff, then take it over

**Status:** approved direction (owner, 3 Nov 2026). **Parallel track** to Voice Studio
([VOICE_STUDIO_PLAN.md](VOICE_STUDIO_PLAN.md)) and RCM ([RCM_CLEARINGHOUSE_PLAN.md](RCM_CLEARINGHOUSE_PLAN.md)).
**Decisions:** both computer-use models are supported (Gemini and Claude); the person picks the model before a
run, with a workspace default in settings. Browser infrastructure is **built by us** on our Google Cloud.

## 1. Why

Many systems a practice depends on have no API: EHRs the hub doesn't reach yet, payer portals, credentialing
sites, state Medicaid portals. Staff sign in to a secure browser inside our Studio; the agent first watches,
then works with staff watching, then on its own. Each mastered task becomes a **skill**: a tool that voice
agents, the RCM engine and the integration hub can call like an API.

## 2. How it works for staff

1. **Open a browser** in the Studio: a live, isolated Chrome on our side, shown in a panel.
2. **Sign in yourself**, including any two-factor code. The agent never sees or types passwords. The signed-in
   session is kept, encrypted, per practice and user, until it expires or is revoked.
3. **Choose the model** for the run (Gemini or Claude), or keep the workspace default.
4. **Teach**: do the task once while narrating ("check this claim on the UHC portal"). We record clicks, typed
   fields (never passwords), pages and screenshots.
5. **The agent drafts the skill**: steps, inputs (member ID, date of service), checks ("the page shows
   *Claim details*") and outputs (status, amount paid). Staff review and edit it.
6. **Practise with a person watching**, step by step at first, then whole runs. Staff can take over at any
   moment.
7. **Graduate**: after enough clean runs a supervisor promotes the skill. Next time anyone signs in, the
   skill is ready to use.

"Learning" here is **procedure memory**, not retraining: demonstrations, the written skill, examples and every
human correction, refined after each run. It is reliable, explainable and auditable.

## 2a. Two ways a task starts (owner, 3 Nov 2026)

**Mode A: learn by watching (the first, safest way).** A person does the real task for a real case in the
Studio browser, start to finish. The agent only observes: it records the steps, the pages, the fields used and
the result, and asks the person to confirm what each step was for. From one or more such cases it drafts the
skill. Next time the same kind of case comes up, the agent offers to do it, first step by step with approval
(Shadow), then under supervision. Nothing is ever done by the agent in Mode A until a person has done it first.

**Mode B: do a defined task.** A person (or a workflow, later) asks for a task in plain words with its inputs,
for example "check the status of claim 12345 on the Aetna provider portal". Before touching the browser the
agent runs a **pre-flight check** and shows the result:

| Check | Passes when |
|---|---|
| Human supervision | The **Under human supervision** box is ticked and the named supervisor has the live view open (see §6a) |
| Signed in | A valid saved session exists for this practice and user (otherwise: "please sign in first") |
| Information complete | Every input the task needs is present (member ID, date of service, claim number…), taken from the hub or entered by the person; nothing is guessed |
| Permission | The person's role may run this task; the skill's maturity level allows this mode (read-only vs. write) |
| Minimum data | Only the fields the task needs are passed to the model |
| Model | The chosen model (Gemini or Claude) is approved for this workspace |

If a check fails, the agent says exactly what is missing and stops. If all pass, it proceeds in the browser
under the skill's current level: step-by-step approval if the skill is new or unknown, watched if it is
Supervised, alone for read-only tasks once Autonomous, and always stopping before any submit or save.

**Where to start:** Mode A on a **read-only** task (look up eligibility or a claim status on a payer portal),
then Mode B for the same task once a few cases have been taught.

## 3. Maturity ladder (per skill)

| Level | Who acts | To move up |
|---|---|---|
| **Taught** | Human demonstrates; agent drafts the skill | Human approves the draft |
| **Shadow** | Agent proposes each step; human approves each | ~5 runs with no corrections |
| **Supervised** | Agent runs; human watches and can take over | ~10 runs above the success threshold, no critical errors |
| **Autonomous, read-only** | Agent alone (lookups, reading results) | Supervisor sign-off; ongoing scorecards |
| **Autonomous with approval** | Agent prepares writes and **stops before submit** for one-click approval | Writes always approved (L2/L3, as for EHR writes) |

A skill **drops a level automatically** when its error rate rises or the site changes (pages no longer match
the skill's checks).

## 4. Where it plugs in

- **Integration hub**: a third connector type, **Browser**, beside NexHealth and OpenEMR. An EHR without an
  API appears in the hub with the operations its skills cover. Writes use the same intent pipeline (propose →
  approve → send → confirm), confirmed by reading the result back on screen.
- **RCM**: payer portals (prior auth, attachments, appeals), credentialing portals, and Medicare portal tasks
  **only where the site's terms allow automation**.
- **Voice Studio**: skills are tools a voice agent can call mid-call; scenarios and scorecards apply to browser
  runs.
- **Studio UI**: **Skills** area: live browser panel (Teach / Watch / Take over), model picker, skill library
  with each skill's level, run history with screenshots, scorecards.

## 5. Technical design

| Part | Design |
|---|---|
| **Browser runtime (ours)** | One isolated Chrome container per session (Playwright, Chrome DevTools Protocol) on **GKE Autopilot** in careplix-labs (sessions last hours; Cloud Run requests do not). Egress restricted per skill to its allowed domains. |
| **Live view and take-over** | CDP screencast streamed to the Studio over a websocket; staff input (mouse, keyboard) sent back the same way; one controller at a time with a visible "Agent / You" switch. |
| **Model adapter** | One interface, two implementations: **Gemini Computer Use** and **Claude computer use**, both called through **Vertex AI** (already under the Google Cloud BAA). The adapter turns screenshots and page state into the model's input and its actions (click, type, scroll, key, navigate, wait) into Playwright calls. Exact model IDs are configured, not hard-coded, and verified at B2. |
| **Model choice** | Workspace default in **Settings → Computer use** (approved models only); the person may switch per run; a skill can **pin** the model it was validated on. Every run records which model acted. |
| **Hybrid execution** | Replay recorded steps directly when the page matches (fast, cheap, deterministic); hand control to the model only when something differs; record the model's fix as a proposed skill update. |
| **Saved sessions** | Browser profiles encrypted at rest (Cloud KMS), per practice and user, with expiry and revocation. Passwords and 2FA codes are never stored or typed by the agent. |
| **Evidence** | Every action logged with a screenshot. Screenshots contain PHI: encrypted, retention-limited, redacted in list views, opened on demand with an audit entry. |

## 6. Safety rules

- **Any site, under human supervision** (owner decision, 4 Nov 2026; replaces the earlier per-site allowlist).
  No site is blocked. Every run on a site carries the supervision rules in §6a.
- **Page content is data, never instructions** (prompt-injection defence); the agent cannot leave a skill's
  approved domains; no downloads or uploads beyond the files a skill is allowed to handle.
- **No payments, no signatures on legal forms, no account or password changes** without a person.
- **Kill switch, take-over and the workspace emergency stop** cover browser runs; every run is audited.

## 6a. Under human supervision (owner decision, 4 Nov 2026)

Every browser run starts with a checkbox, **"Under human supervision"**, ticked by a named person who takes
responsibility for the run. While it is on:

| Rule | How it is enforced |
|---|---|
| **A person is really watching** | The supervisor's live view must be open and active. If it is closed, hidden or idle for more than 60 s, the agent pauses at the next step and waits. |
| **The person can act at any moment** | Pause, take over and stop are always one click away; take-over hands mouse and keyboard to the person. |
| **Stops before anything that changes data** | Submit, save, send, upload, delete and attest each need the supervisor's click. Read-only steps run without asking once the skill is past Shadow. |
| **Human pace, one account, one session** | No parallel sessions on one login and no bulk loops faster than a person would work; the practice's own login only, never shared credentials. |
| **People handle the gates** | Sign-in, two-factor codes and CAPTCHAs are done by the person. If a site shows a bot check or warning, the agent stops and hands over. |
| **Recorded** | Supervisor, practice, site, model, every action and screenshot, approvals and take-overs go in the audit trail. |

### 6b. The approval experience (owner, 4 Nov 2026: "like how Claude asks for approval")

The agent works the way Claude Code asks permission: **it proposes, the person approves, the agent does it.**
The person never has to drive the mouse; they read a short card and click.

**1. Plan first.** Before the first click the agent shows its plan in plain words, for example:
"1. Open Claims → Search. 2. Enter member ID ••••4821 and date of service 10/14. 3. Read the claim status.
4. Copy status and paid amount back to the claim in CarePlix." The person can **approve the plan**,
**edit** it (change a step or an input), or **cancel**.

**2. An approval card for each action that needs one.** The card sits beside the live view:

| On the card | Example |
|---|---|
| What the agent will do | "Click **Submit reconsideration**" |
| Where | Screenshot with the target outlined, plus the page title and site |
| With what data | Fields and values it will type, editable before approving (PHI masked until the person reveals it) |
| Why | The plan step it belongs to and what it expects to happen next |
| Risk | **Read**, **Enter data** or **Commit** (see below) |

The buttons:
- **Approve** (Enter);
- **Approve all similar for this run**, such as all further page reads or "Next" clicks;
- **Always allow for this skill on this site**, for admins and for Read and Enter-data actions only;
- **Edit and approve**;
- **Deny and tell the agent why** (Esc), so the agent re-plans from the feedback;
- **Take over**.

**3. Risk tiers decide what asks.**

| Tier | Examples | Asks? |
|---|---|---|
| **Read** | Open a page, search, scroll, read a result | Asks while the skill is new. Can be allowed for the run or for the skill. |
| **Enter data** | Type into a field, choose a dropdown, tick a box (nothing saved yet) | Asks with the values shown. Can be allowed for the run. |
| **Commit** | Submit, save, send, upload, delete, attest, sign, pay | **Always asks, every time.** No "always allow". Pay, sign and attest stay person-only (§6). |

**4. Never stuck, never silent.**
- If no one answers within 2 minutes, the run pauses and the approval moves to the **Approvals inbox**.
- The inbox is one queue in the Studio, with push and SMS notifications. It also holds voice-agent write
  intents and RCM approvals, so staff approve everything in one place.
- Approving from a phone works for Read and Enter-data actions. Commit actions need the live view.

**5. After the run.**
- A summary lists what was done, what was approved by whom, the result, and the evidence screenshots.
- Each denial and edit becomes a proposed improvement to the skill. Approvals feed the maturity ladder: a step
  that has been approved unchanged many times is offered as "allow for this skill".

The same card-and-inbox pattern is used for voice-agent actions (write intents) and clearinghouse submissions,
so the platform has one consistent way to ask a person.

**Unsupervised runs** (the *Autonomous* rung of the ladder) are only offered on sites an admin has marked
**"automation permitted"** with a note of where the terms say so. Everything else stays supervised, however
mature the skill.

**Risk the owner accepts.** Some portal terms (for example Availity's and NGSConnex's) prohibit automated
access whether or not a person watches. Supervision greatly lowers the risk but does not remove it: the worst
case is the portal suspending the **practice's** account. So, the first time a practice uses the agent on a
site, its admin sees the site's terms note and acknowledges it once; the acknowledgement is audited. The
agent never helps evade a site's controls (no CAPTCHA solving, no disguising, no rate-limit workarounds).

## 7. Milestones (parallel track)

| # | Delivers |
|---|---|
| **B1** | Browser runtime on GKE, live view, staff sign-in, saved sessions, take-over, audit |
| **B2** | **Mode A**: record a person doing a real case (steps, pages, fields, result), confirm each step's purpose, draft the skill; staff edit it |
| **B3** | Model adapter (Gemini + Claude via Vertex), model picker, **Mode B** with the pre-flight check, plan approval, approval cards with risk tiers (§6b), and the Approvals inbox |
| **B4** | Maturity ladder, skill library, scorecards, automatic demotion |
| **B5** | Skills as tools for voice agents and RCM; Browser connector in the hub with approved writes |
| **B6** | First production skills, under human supervision: NGSConnex Medicare claim status and eligibility for Absolute Health Group (closes the gap the clearinghouse can't cover), then a commercial payer portal |

## 8. Open items

- First target: NGSConnex claim status (read-only), under human supervision, with the practice admin's
  one-time acknowledgement.
- Confirm both computer-use models are enabled for the careplix-labs Vertex project.
- GKE Autopilot cluster and a KMS key are new infrastructure: added to `deploy-labs.sh setup` at B1.
