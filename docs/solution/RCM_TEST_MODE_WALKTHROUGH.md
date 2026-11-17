# Charge to cash in clearinghouse test mode: owner walkthrough

**Scope:** RC3-lite, RC5, RC6 (test-mode signals), RC7, RC8 from
[RCM_CLEARINGHOUSE_PLAN.md](RCM_CLEARINGHOUSE_PLAN.md), running on careplix-labs against Stedi **test
mode only** with synthetic data. `infra/gcp/deploy-labs.sh` refuses production keys for labs.

## What test mode can and can't do (Stedi, checked 16 Nov 2026)

| Works in test mode | Not in test mode |
|---|---|
| Test 837P claims (needs a Stedi **production account** used with a **test API key**; sandbox accounts can't send test claims) | 276/277 real-time claim status |
| A test 277CA for every test claim | Transaction enrollment API, batch eligibility, insurance discovery, COB |
| Test 835s, but only for claims sent to the **Stedi Test Payer** (payer ID `STEDI`) and only after the provider is enrolled with it for 835s | 835s from any other payer |
| Unsolicited 275 attachments | |

The member-ID prefix on a claim to the test payer picks the remittance: `STEDI_PAID_` (paid in full),
`STEDI_PARTIALLY_PAID_` (60 % paid, CO-45 20 %, PR-2 20 %), `STEDI_DENIED_` (CO-31, nothing paid).
CarePlix adds the prefix itself from the claim's **Test payer outcome**; never type it.

## One-time owner steps (Stedi portal)

1. **Account and key.** The Stedi account must be a production account (upgrade if it's a sandbox).
   Create a **test** API key (Settings → API keys → Generate → Mode: Test). It goes in `.env` as
   `STEDI_TEST_API_KEY` (already the case if eligibility test mode works); `deploy-labs.sh setup` puts it in
   Secret Manager.
2. **Enroll the billing provider with the Stedi Test Payer for remittances (835).** Done in
   production mode (no test key, test-mode toggle off); it carries no PHI:
   1. In the Studio: **Claims → Clearinghouse → Billing profile**. Note the **Billing NPI** (default
      `1999999984`, Stedi's dummy NPI) and **Tax ID**. Change them first if you prefer; claims must carry
      the same values as the enrollment.
   2. In the Stedi portal, switch **Test mode off**. **Providers → New provider**: name
      `Riverside Family Medicine (synthetic)`, NPI and tax ID (EIN) from step 1.
   3. **Enrollments → New enrollment**: that provider, payer **Stedi Test Payer** (`STEDI`), transaction
      **Claim payment (835)**. Submit. It goes live on its own within about a minute.
3. **Webhook for acknowledgements and remittances (recommended).** Stedi portal, **Test mode on** (test
   events only go to test-mode destinations): **Webhooks / Event destinations → New destination**:
   URL `https://<careplix-webhooks service URL>/webhooks/stedi` (the deploy prints it as "Clearinghouse
   events"), event type `transaction.processed`.
   Copy the signing secret (`whsec_…`) into `.env` as `STEDI_WEBHOOK_SECRET` and run
   `./infra/gcp/deploy-labs.sh setup` before the deploy. Without a webhook, use **Check for responses**
   (polls the last three days) or paste a transaction ID under **Claims → Clearinghouse → Clearinghouse
   responses**.

## Studio walkthrough (careplix-labs, after the deploy)

1. **Claims → Charges → Add synthetic charges** (all sample visits), then **Build claims**. Seven draft
   claims appear on the Claims tab.
2. **Scrub catches problems.** Open each draft and press **Scrub**:
   - *no diagnosis* → `dx_missing` and `dx_pointer_missing`; add `J06.9`, point the line at `1`, **Save
     changes**, **Scrub** again → Ready to submit;
   - *modifier 25 on an ECG (93000)* → `mod_25_not_em`;
   - *200-day-old visit* → `timely_filing`.
3. **Submit the clean ones** (Aetna office visit → paid; UnitedHealthcare visit + ECG → partially paid;
   Cigna wellness → denied; Medicare Part B → acknowledgement only). Expect **Submitted**, and under
   *Clearinghouse transactions* a Validation and a Submission (reviewers can view the exact JSON sent).
   Pressing Submit again sends nothing (idempotency key `<control number>-<attempt>`).
4. **Acknowledgements (277CA).** Within a few minutes the webhook (or **Check for responses**) moves
   each claim to **Accepted** and stores the 277CA as evidence. A rejection would land in **Work queue**
   with the payer's codes, a category and what to fix; fix the claim and **Resubmit**.
5. **Remittances (835)** for the three test-payer claims:
   - **Remittances** tab: Aetna paid in full; UHC paid 60 % with CO-45 (contractual write-off proposed)
     and PR-2 (left on the patient's balance); Cigna denied. Synthetic claims have no EHR patient, so
     postings say **Post by hand** → **Mark posted**. Claims built from NexHealth sandbox charges (**Read
     EHR charges**) instead propose L3 write intents (`payment_post`, `adjustment_post`): **Approve and
     post** sends `POST /payments` and `POST /adjustments` to NexHealth.
   - **Work queue**: the Cigna denial, *CO-31 → Patient or coverage → Fix and resubmit*, due in 90 days.
     **Corrected claim** drafts a replacement (payer claim number as the original reference, test outcome
     *paid*); submit it, and its 835 closes the denial.
6. **Other denial routes** (switch the route on any denial): **Payer call** opens a *Claim denial* case
   for the calling agent; **Appeal** drafts a letter under **Appeals** (edit, **Approve**: with no fax or
   portal service it's *ready to send*; **Mark sent**, then **Overturned/Upheld**; **Attach records (275)**
   uploads a PDF through the clearinghouse and the corrected claim carries it); **Write-off** proposes an
   adjustment for approval.
7. **Follow-up.** The Medicare claim gets an acknowledgement but never an 835 in test mode; 30 days
   after submission the follow-up opens a *Claim status* payer call (no acknowledgement after 3 days does
   the same). Test mode never sends a 276 for these claims.

## If something fails

The claims loops (277CA/835 intake every minute, follow-up every 15 minutes) run in the Studio service;
webhook deliveries land in `careplix-webhooks`:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="careplix-studio" AND (textPayload:"claims" OR jsonPayload.logger="careplix.claims")' --project careplix-labs --freshness=1h --limit 100 --format='value(timestamp,severity,textPayload,jsonPayload.message)'
```

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="careplix-webhooks"' --project careplix-labs --freshness=1h --limit 50 --format='value(timestamp,severity,httpRequest.status,httpRequest.requestUrl)'
```

- *Validation errors from the clearinghouse* show on the claim's scrub with the clearinghouse's own text;
  the request JSON is under *Clearinghouse transactions → View* (reviewers).
- *403 on submit*: the key isn't a test key on a production account.
- *No 835 for a test-payer claim*: the enrollment (step 2) isn't live, or the claim's NPI/tax ID differ
  from the enrollment.
- *Webhook events not applied*: check `STEDI_WEBHOOK_SECRET` and that the destination is in test mode;
  **Check for responses** catches up.

## Not built yet (owner decisions or later milestones)

- Fax and payer-portal sending for appeals (no vendor; `claims.appeals.SENDERS` is the hook).
- Claim build from the EHR's own claims (`GET /claims`), guarantor balances and patient statements (R7).
- Payer-specific timely-filing and appeal windows (defaults: Medicare 365/120 days, commercial 90/180).
- NexHealth `POST /payments`/`/adjustments` bodies follow NexHealth's reference but haven't been sent to a
  practice with ledger sync yet; payment and adjustment type IDs aren't mapped (the EHR's defaults apply).
