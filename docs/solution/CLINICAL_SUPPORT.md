# Clinical support and front-office completion

Built 16 Nov 2026 on Cases v2 and playbooks (#47), the text channel (#46) and person memory (#43).
Labs only, synthetic patients only.

**The rule that doesn't bend: the agent never makes a clinical decision.** It gathers, prepares,
routes, and delivers clinician-approved words. Every decision is a named clinician's, recorded as
evidence on the case (the completion contract's fifth part).

## Who signs

- New role **clinician** (Members). It can view, review results and sign clinical work.
- A member with another role (a physician who is also the admin) can be marked **Clinician** on
  Members. Only these people have `clinical_signoff`; the generic `review` permission no longer signs.
- Clinician-only: refill decisions, red-flag acknowledgements, reviewing discharge answers and
  pre-visit briefs, approving or retiring instruction wording, approving care opportunities.
- The demo administrator (dev seed) is marked clinician so one tester can walk every flow. In
  careplix-labs, tick **Clinician** on your own row in Members first.

## Approved instructions library (Clinical → Approved instructions)

- Starter wording arrives as **drafts**. Nothing reaches a patient until a clinician approves it.
- Delivered exactly as written; the only fill-ins are `{first_name}` and `{practice_phone}`.
- Editing approved words makes a new draft version.
- Text wording must pass the text channel's minimum-necessary check (no medicines, doses,
  diagnoses or identifiers), so it never bounces at send time. Clinical detail is call-only.
- A decision without approved words ("Other") goes to a person to tell the patient.

## Refills (Clinical → Refills)

1. Capture: the receptionist's `request_refill` tool (verified callers only), staff entry, or the
   playbook trigger API.
2. The protocol facts are read from the EHR mirror and imported facts: last visit (12-month
   rule), next visit, whether the medicine is on the list, last fill, and whether it is a
   controlled substance. Facts are shown, never decided on.
3. The patient is texted that the care team has the request (administrative wording).
4. The clinician queue: the review inbox shows **Decide: refill request**. The clinician picks the
   outcome and the approved words the patient will hear.
5. The words go out by text, else a call that reads them word for word. If neither reaches the
   patient, a person tells them.
6. The decision is the system-of-record acknowledgement. **No prescribing:** the clinician sends
   any prescription from the EHR. A prescribing partner comes later.

## Discharge follow-up (Clinical → Discharge follow-up)

- Notices come in by staff entry, CSV import, or a `discharge` clinical fact (Care opportunities
  import; trigger `discharge_notice`, on by default). The ADT feed comes later.
- The call-by date is **two business days** after discharge (weekends skipped; practice holidays
  later). The case is due at 5 pm local that day.
- The clinical-check agent calls with the scripted check (`clinical/scripts.py`) and records each
  answer with `record_check_answers`. A yes or unsure to a red-flag question (chest pain,
  breathlessness, fever, bleeding or a wound problem, a fall or confusion) escalates **at once**:
  a **Red flag** item in the review inbox, a staff alert, and the agent reads the approved red-flag
  words (the platform's emergency wording until a clinician approves the practice's own).
- A nurse who called by hand records the answers on the case; the playbook picks up from there.
- The clinician acknowledges any red flag (what they did), then signs off the answers.

## Pre-visit (Clinical → Pre-visit)

- Start from an upcoming visit (Prepare), by hand, or turn on the opt-in trigger `previsit`
  (booked visits 2 to 5 days ahead) in `PLAYBOOK_TRIGGERS`.
- The questionnaire goes by secure text link, else a call. Red flags escalate like discharge.
- The chart-prep brief: problems, medicines, open care gaps, outside records (and discharges),
  last and next visit, and the answers with patient-flagged items first. The clinician marks it
  reviewed.

## Intake and registration (Front Office → Intake & registration)

- A secure link to the registration form: demographics, coverage as printed on the card, and card
  photos (front and back, 1.5 MB each, sealed at rest).
- Demographics and coverage become two write intents (`patient_update`, `coverage_create`) that an
  administrator approves. They need NexHealth's v3.1 beta (owner ask §11.7); until then the hub
  marks them **manual** and the front desk enters them, with the photos on the case.

## Reminders

The schedule trigger now opens a reminder case **7 days** out. The cadence sends at 7 days, 72 hours
and 24 hours until the patient answers. **YES** confirms (EHR confirmation auto-approved), **R** or
**NO** hands the reschedule to the front desk and tells the patient, **C** proposes the EHR
cancellation for staff approval and tells the patient. `CANCEL` alone stays a carrier opt-out
keyword (CTIA). No answer by 2 hours before the visit: the front desk owns a call-back.

## Secure links

A link is 32 random bytes; only its HMAC is stored. It expires in 7 days, works once, and is
revoked when a newer one is sent. The page names the practice and nothing about the patient.
Answers are sealed at rest. **Labs serves the form from the Studio, behind IAP**, so only signed-in
testers can open a link. Before real patients, forms need a patient-facing host (no IAP, its own
rate limits and security review).

## Voice Studio

`POST /api/scenarios/examples/clinical` (Voice Studio → clinical suite) creates the
**clinical-check** agent and 12 scenarios: refill details, delivering an approved decision,
discharge checks (routine, chest pain, unsure breathing), pre-visit by call, an emergency, and five
red-team callers who try to get dose advice, reassurance, a promised approval, a decision change
from someone claiming to be a doctor, or an explanation of the decision. The red-team generator adds
three clinical attacks for clinical-check agents. The agent must pass its suite before publishing.

## Tables (all with `workspace_id`)

`approved_instructions`, `form_links`; new nullable column `memberships.clinician`.
