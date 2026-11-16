# A2P 10DLC registration: what to submit in Twilio

Status: draft for the owner to submit (OPERATING_PLAN §9 and §11 item 3). Nothing here has been
submitted. US carriers block or filter application texts from a 10-digit long code until the brand
and a campaign are registered, so text (P2) can't go live without this.

Fill in every `‹…›` before submitting. The wording below follows the CTIA guidelines and what The
Campaign Registry (TCR) reviewers look for: a named program, how people opt in, how often they get
texts, "Msg & data rates may apply", HELP and STOP, and links to terms and privacy.

## 1. Before you start

| Need | Notes |
|---|---|
| Legal business name, EIN, business address, website | Must match the IRS record exactly (EIN letter, CP-575 or 147C). A mismatch is the most common rejection |
| Authorised contact | Name, title, email on the company domain, phone |
| Website with SMS terms and a privacy policy | Section 6 has the text to publish. Reviewers open these links |
| Twilio number | +1 312 755 8589 (the shared sandbox line, already SMS-capable) |
| BAA | Twilio BAA signed 15 Nov 2026 ✅ |

## 2. Brand (Twilio Console → Messaging → Regulatory Compliance → A2P 10DLC)

1. **Customer profile** (Trust Hub, "Primary Business Profile"): business details above.
2. **Brand type: Standard** (not Sole Proprietor; Standard allows more than one campaign and higher
   throughput). Choose **secondary vetting** if offered: it raises throughput limits.

| Field | Value |
|---|---|
| Legal company name | ‹CarePlix legal name, exactly as on the EIN› |
| Brand / DBA name | CarePlix |
| Company type | Private for-profit |
| Tax ID (EIN) | ‹EIN› |
| Vertical / industry | Healthcare |
| Website | ‹https://careplix.com› |
| Stock symbol | none |

**Customer brands later.** CarePlix sends on behalf of practices and agencies (an ISV). For labs and
the first pilot one CarePlix brand is enough. Before production, each customer (starting with
Absolute Health Group) should be registered as its own brand under CarePlix's ISV account, with its
own campaign and number, so texts say who they are from and one customer's traffic can't affect
another's. The code already supports a sender per workspace (Studio → Text messages).

## 3. Messaging Service

Create a Messaging Service named **CarePlix Patient Messaging**, then:

- **Sender pool**: add +1 312 755 8589.
- **Integration → Incoming messages**: "Send a webhook", URL
  `‹careplix-webhooks URL›/webhooks/twilio/sms`, HTTP POST. (`deploy-labs.sh deploy` prints the
  URL.) Delivery status callbacks are set per message by the code; leave the service's status
  callback empty.
- **Opt-out management (Advanced Opt-Out)**: keep it on, and set the replies to the text in §5.
  The app records STOP, START and HELP itself and does not send its own replies while
  `SMS_KEYWORD_REPLIES=twilio` (the default), so people never get two.
- Copy the service SID (`MG…`) into `.env` as `TWILIO_MESSAGING_SERVICE_SID` once the campaign is
  approved, then run deploy. Until then texts send `From` the number directly.

## 4. Campaign

**Use case: Low Volume Mixed** for labs and the pilot (under 2,000 segments a day; cheaper and
quicker to vet), covering Customer Care and Account Notifications. Move to a **Mixed** standard
campaign when volume grows. Do not select Marketing: there is no promotional content.

### Campaign description

> CarePlix sends appointment and scheduling texts on behalf of healthcare practices to their own
> patients, and shift-scheduling texts on behalf of home care agencies to their own caregivers.
> Messages include appointment reminders and confirmations, offers of an earlier appointment when
> one opens (the recipient replies YES or NO), call-back notices after a missed call, and
> open-shift offers for caregivers (reply YES or NO). Recipients opted in with the practice or
> agency. Messages contain no medical details, no marketing and no promotions. Recipients can reply
> HELP for help and STOP to opt out at any time.

### Message flow (how people opt in)

> Patients opt in with their healthcare practice: on the practice's new-patient or annual intake
> form (paper or online), which includes an unchecked box with this wording: "I agree to receive
> appointment and scheduling text messages from ‹Practice name› sent by CarePlix at the mobile
> number I provided. Message frequency varies (typically 1–6 a month). Msg & data rates may apply.
> Reply HELP for help, STOP to opt out. Consent is not a condition of care. Terms:
> ‹https://careplix.com/sms-terms› Privacy: ‹https://careplix.com/privacy›." Front-desk staff may
> also record a verbal opt-in given in person or on a recorded call, reading the same wording.
> Caregivers opt in the same way on their agency's onboarding form. Every opt-in is stored with its
> source and date in CarePlix, and no text is sent to a number without one. The first text a person
> receives repeats how to opt out. A person can also text START to ‹+1 312 755 8589› to opt in.

Attach a screenshot or PDF of the intake-form consent box (a mock-up with "‹Practice name›" is
fine for labs). Reviewers reject flows they can't see.

### Campaign attributes

| Question | Answer |
|---|---|
| Subscriber opt-in | Yes |
| Subscriber opt-out | Yes |
| Subscriber help | Yes |
| Number pooling | No |
| Direct lending or loan arrangement | No |
| Embedded links | No (change to Yes, with a sample, when secure form links ship) |
| Embedded phone numbers | Yes (the practice's help number in HELP replies) |
| Age-gated content | No |
| Message volume | ‹expected texts per day; under 2,000 for Low Volume Mixed› |

### Sample messages (five; brand name and STOP wording in each)

1. `Riverside Family Medicine: Hi Sam, a reminder of your appointment on Tue Nov 18 at 9:30 AM.
   Reply YES to confirm or NO if you need to change it. Reply STOP to opt out.`
2. `Riverside Family Medicine: Hi Sam, an earlier appointment opened on Thu Nov 20 at 2:15 PM.
   Reply YES to take it or NO to keep your current time. First to reply gets it. Reply STOP to opt
   out.`
3. `Riverside Family Medicine: You're confirmed for Thu Nov 20 at 2:15 PM. Thank you!`
4. `Riverside Family Medicine: Sorry we missed you on the phone. The front desk will call you back
   today during office hours. Reply STOP to opt out.`
5. `Lakeside Home Care: Open shift Sat Nov 22, 8 AM–2 PM near 60614. Reply YES to take it or NO to
   pass. First confirmed yes gets it. Reply STOP to opt out.`

These match `careplix_api.channels.content.TEMPLATES`; keep the two in step.

## 5. Keywords and replies (set in Advanced Opt-Out)

| Keywords | Reply |
|---|---|
| Opt-out: STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, REVOKE, OPTOUT | `CarePlix: You're unsubscribed and won't get more texts. Reply START to resubscribe.` |
| Opt-in: START, UNSTOP (and YES from someone opted out) | `CarePlix: You're resubscribed to appointment and scheduling texts. Msg & data rates may apply. Reply HELP for help, STOP to opt out.` |
| Help: HELP, INFO | `CarePlix: Appointment and scheduling texts for your healthcare provider. For help call your provider's office or ‹support phone›. Msg & data rates may apply. Reply STOP to opt out.` |

The opt-in confirmation text for a new subscriber (sent by the practice's playbook after a
recorded opt-in, or by Twilio for START):
`CarePlix: You're signed up for appointment and scheduling texts from ‹Practice name›. Up to 6
msgs/month. Msg & data rates may apply. Reply HELP for help, STOP to opt out.`

## 6. Publish on the website before submitting

**SMS terms** (`‹https://careplix.com/sms-terms›`):

> CarePlix patient and caregiver messaging. Healthcare practices and home care agencies use
> CarePlix to send appointment, scheduling and shift texts to people who opted in with them.
> Message frequency varies (typically 1–6 a month). Message and data rates may apply. Reply HELP
> for help or STOP to cancel at any time; you'll get one confirmation and no further texts. Carriers
> are not liable for delayed or undelivered messages. Questions: ‹support email / phone›.

**Privacy policy** must include, word for word or close to it:

> We do not sell, rent or share mobile phone numbers or SMS opt-in consent with third parties or
> affiliates for marketing or promotional purposes. Text messaging originator opt-in data and
> consent are not shared with any third parties, except service providers who deliver the messages
> on our behalf.

## 7. After approval

1. `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_MESSAGING_SERVICE_SID`.
2. `./infra/gcp/deploy-labs.sh setup` (stores the Auth Token in Secret Manager), then build and
   deploy.
3. Studio → Text messages: record consent for your own approved test phone, send a test, reply
   YES, STOP, START and HELP, and check each shows in the log.

## What carriers will check in our traffic

- Every text matches the campaign's use case and samples (no marketing, no other topics).
- Opt-out works immediately: the app blocks any later send to a number that replied STOP, and
  Twilio's error 21610 (recipient unsubscribed) is recorded as an opt-out.
- Texts go out only between 8:00 and 20:00 in the workspace's time zone (held otherwise).
- No PHI beyond the minimum: the app refuses bodies with dates of birth, member or record numbers,
  SSNs or clinical words.
