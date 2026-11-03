# ruff: noqa: E501  (scenario text is prose data; wrapping it would hurt readability)
"""Voice Studio starter library: the Head-of-RCM top 5, as 13 scenarios, plus one showcase errand.

Synthetic people, practice and references only (Lakeside Family Medicine is fictional). Each scenario's
``rehearsal_script`` tells a person, and later the simulated counterpart (VS2), how to play the other side.

Top 5: (1) insurance and benefits checks, (2) claim status follow-up, (3) denials and Medicare phone
reopenings, (4) prior authorization, (5) patient balances.
"""

from __future__ import annotations

from typing import Any

PRACTICE = "Lakeside Family Medicine (NPI 1234567893)"
_PRACTICE_FACTS = {
    "practice_name": "Lakeside Family Medicine",
    "billing_npi": "1234567893",
    "callback_number": "+1 312 555 0142",
}
# Patient calls: identity first, nothing about the account before it (HIPAA minimum necessary).
_IDENTITY_FIRST = (
    "Before discussing any account or insurance detail, confirm you are speaking with the patient by "
    "full name and date of birth. If you cannot, or reach voicemail, leave only the practice name and "
    "callback number."
)
_NO_CARD = "Never take card numbers or payment details on the call; offer the payment link or plan."
_NO_CLINICAL = "Do not discuss diagnoses, treatment or medical necessity; refer clinical questions to the practice."

LIBRARY: list[dict[str, Any]] = [
    # --- 1. Insurance and benefits ---------------------------------------------------------------
    {
        "name": "1a · Patient's coverage changed",
        "category": "eligibility",
        "counterpart": "Patient",
        "on_behalf_of": PRACTICE,
        "goal": "The patient's insurance came back inactive for next week's visit. Ask whether they "
        "have new insurance and collect the new plan name, member ID and group number.",
        "facts": {
            **_PRACTICE_FACTS,
            "patient_name": "Jordan Testwell",
            "date_of_birth": "1962-04-18",
            "appointment": "Tuesday at 10:30 am with Dr. Avery Stone",
            "old_plan": "Northwind PPO (inactive since the 1st)",
        },
        "limits": [_IDENTITY_FIRST, _NO_CLINICAL],
        "success_checks": [
            "New plan name, member ID and group number captured and read back",
            "Patient told the visit stays booked and coverage will be re-checked",
        ],
        "rehearsal_script": "Play the patient. Confirm your name and date of birth when asked. Say you "
        "changed jobs and now have Summit Health PPO, member ID SH44718203, group 7700. Ask whether "
        "you still need to come in.",
    },
    {
        "name": "1b · Benefits detail the electronic check missed",
        "simulation": {
            "menu": "Thank you for calling provider services. For eligibility and benefits, press 1. For claims, press 2. For prior authorization, press 3.",
            "hold_s": 30,
            "voice": "Kore",
        },
        "category": "eligibility",
        "counterpart": "Health plan provider services",
        "on_behalf_of": PRACTICE,
        "goal": "The electronic eligibility check shows active coverage but no physical therapy visit "
        "limit. Find out visits allowed per year, visits used, and whether prior authorization is "
        "needed after a number of visits.",
        "facts": {
            **_PRACTICE_FACTS,
            "member_id": "100200300",
            "patient_name": "Alex Synth",
            "date_of_birth": "1950-01-01",
            "service": "Outpatient physical therapy, CPT 97110",
        },
        "limits": ["Ask only about this member's benefits for this service."],
        "success_checks": [
            "Visits allowed and visits used recorded",
            "Auth rule recorded (needed or not, and after how many visits)",
            "Call reference number read back",
        ],
        "rehearsal_script": "Play a payer rep. Verify NPI, member ID and date of birth. Say 20 visits "
        "per calendar year, 6 used, auth required after visit 12. Reference REF-55102.",
    },
    # --- 2. Claim status ---------------------------------------------------------------------------
    {
        "name": "2a · Claim status: pended for records",
        "simulation": {
            "menu": "Provider claims line. For claim status, press 1. For appeals, press 2. To speak with a representative, press 0.",
            "hold_s": 45,
            "voice": "Charon",
        },
        "category": "claim_status",
        "counterpart": "Health plan claims line",
        "on_behalf_of": PRACTICE,
        "goal": "Find out why the claim below is unpaid after 45 days and what is needed to get it paid.",
        "facts": {
            **_PRACTICE_FACTS,
            "member_id": "100200300",
            "patient_name": "Alex Synth",
            "date_of_birth": "1950-01-01",
            "date_of_service": "September 12",
            "billed_amount": "$240.00",
            "claim_number": "C-77310452",
        },
        "limits": ["Do not agree to resubmit or send anything; note what is requested."],
        "success_checks": [
            "Status and reason recorded",
            "What to send, and how (fax number or upload), recorded",
            "Reference number read back",
        ],
        "rehearsal_script": "Play a claims rep. After a menu, verify the provider and member. Say the "
        "claim is pended for medical records; fax office notes to 1-800-555-0199 within 30 days. "
        "Reference CLM-90217.",
    },
    {
        "name": "2b · Claim status: no record on file",
        "simulation": {
            "menu": "Provider claims line. For claim status, press 1. For appeals, press 2.",
            "hold_s": 20,
            "voice": "Fenrir",
        },
        "category": "claim_status",
        "counterpart": "Health plan claims line",
        "on_behalf_of": PRACTICE,
        "goal": "The claim below has no payment after 40 days. Confirm whether the payer received it "
        "and, if not, how and by when to resubmit.",
        "facts": {
            **_PRACTICE_FACTS,
            "member_id": "200300400",
            "patient_name": "Casey Demo",
            "date_of_birth": "1954-03-03",
            "date_of_service": "September 20",
            "billed_amount": "$180.00",
        },
        "limits": ["Do not agree to anything on the practice's behalf beyond noting instructions."],
        "success_checks": [
            "Confirmed whether the claim is on file",
            "Resubmission method and filing deadline recorded",
            "Reference number read back",
        ],
        "rehearsal_script": "Play a claims rep. Say there is no record of that claim; it can be "
        "resubmitted electronically; the filing limit is 90 days from the date of service. Reference "
        "NR-31884.",
    },
    {
        "name": "2c · Medicare automated claim-status line",
        "simulation": {
            "menu": "Welcome to the Medicare provider line. Enter your 10-digit NPI followed by pound. Then your PTAN followed by pound. Then the last five digits of your tax ID. Then say the beneficiary's Medicare number.",
            "hold_s": 0,
            "voice": "Orus",
        },
        "category": "claim_status",
        "counterpart": "Medicare contractor automated line (simulated)",
        "on_behalf_of": PRACTICE,
        "goal": "Use the automated phone line to get the status of the Medicare claim below.",
        "facts": {
            **_PRACTICE_FACTS,
            "ptan": "SIM1234567",
            "tax_id_last5": "54321",
            "medicare_id": "1EG4TE5MK73",
            "patient_last_name": "Synth",
            "date_of_birth": "1950-01-01",
            "date_of_service": "October 2",
        },
        "limits": [
            "Enter only the digits from the facts when the menu asks for them; never guess.",
            "Simulated line only until the contractor's terms are confirmed for automated assistants.",
        ],
        "success_checks": ["Claim status and paid amount or reason recorded"],
        "rehearsal_script": "Play an automated line: ask for NPI, then PTAN, then last 5 of the tax ID "
        "by keypad, then the Medicare ID by voice, then date of service. Read back: claim finalized, "
        "paid $96.40 on October 28, check number 0082211.",
    },
    # --- 3. Denials ----------------------------------------------------------------------------------
    {
        "name": "3a · Medicare telephone reopening (wrong modifier)",
        "simulation": {
            "menu": "Medicare provider contact center. For telephone reopenings, press 3. For claim status, press 1.",
            "hold_s": 40,
            "voice": "Leda",
        },
        "category": "denials",
        "counterpart": "Medicare contractor reopenings line",
        "on_behalf_of": PRACTICE,
        "goal": "Request a telephone reopening to correct a clerical error: the claim below was billed "
        "without modifier 25. Ask for the reopening and get its reference.",
        "facts": {
            **_PRACTICE_FACTS,
            "ptan": "SIM1234567",
            "medicare_id": "1EG4TE5MK73",
            "patient_name": "Alex Synth",
            "date_of_birth": "1950-01-01",
            "claim_number": "552601234567890",
            "date_of_service": "October 2",
            "correction": "Add modifier 25 to CPT 99214, line 1",
        },
        "limits": ["Request only the correction in the facts; do not change anything else."],
        "success_checks": [
            "Reopening accepted or the reason it can't be done by phone recorded",
            "Reopening reference number read back",
        ],
        "rehearsal_script": "Play a reopenings rep. Verify NPI, PTAN and the beneficiary. Accept the "
        "modifier correction; give reference RO-2026-114782; say it will reprocess in about 30 days.",
    },
    {
        "name": "3b · Denial: no prior authorization",
        "category": "denials",
        "counterpart": "Health plan provider services",
        "on_behalf_of": PRACTICE,
        "goal": "The claim below was denied for missing authorization. Ask whether a retro "
        "authorization is possible, the appeal route and the deadline.",
        "facts": {
            **_PRACTICE_FACTS,
            "member_id": "100200300",
            "patient_name": "Alex Synth",
            "date_of_birth": "1950-01-01",
            "claim_number": "C-77310999",
            "date_of_service": "August 30",
            "service": "MRI lumbar spine, CPT 72148",
        },
        "limits": [_NO_CLINICAL, "Do not file the appeal on the call; note the route."],
        "success_checks": [
            "Retro-auth possible or not recorded",
            "Appeal route, address or fax, and deadline recorded",
            "Reference number read back",
        ],
        "rehearsal_script": "Play a rep. Say retro auth isn't allowed past 14 days; file a written "
        "appeal with medical records within 180 days to fax 1-800-555-0177. Reference DN-66013.",
    },
    {
        "name": "3c · Denial: duplicate claim",
        "category": "denials",
        "counterpart": "Health plan claims line",
        "on_behalf_of": PRACTICE,
        "goal": "The claim below was denied as a duplicate. Find the original claim it duplicates, "
        "whether it was paid, and the amount.",
        "facts": {
            **_PRACTICE_FACTS,
            "member_id": "200300400",
            "patient_name": "Casey Demo",
            "date_of_birth": "1954-03-03",
            "claim_number": "C-77311002",
            "date_of_service": "September 20",
        },
        "limits": ["Do not request any adjustment; record what the original claim shows."],
        "success_checks": [
            "Original claim number recorded",
            "Whether and how much it paid, and when, recorded",
        ],
        "rehearsal_script": "Play a claims rep. Say the original claim C-77300871 paid $142.18 on "
        "October 14; this one is a duplicate. Reference DUP-4412.",
    },
    # --- 4. Prior authorization ----------------------------------------------------------------------
    {
        "name": "4a · Prior auth status: pending review",
        "simulation": {"menu": None, "hold_s": 60, "voice": "Aoede"},
        "category": "prior_auth",
        "counterpart": "Utilization review department",
        "on_behalf_of": PRACTICE,
        "goal": "Check the status of the authorization request below and what is still needed.",
        "facts": {
            **_PRACTICE_FACTS,
            "member_id": "100200300",
            "patient_name": "Alex Synth",
            "date_of_birth": "1950-01-01",
            "auth_request_number": "PA-48213",
            "service": "Outpatient physical therapy, 12 visits",
        },
        "limits": [_NO_CLINICAL, "Do not agree to send anything; note what is requested."],
        "success_checks": [
            "Status recorded (approved, denied, pending)",
            "Missing documents and how to send them recorded",
            "Reference number read back",
        ],
        "rehearsal_script": "Play a utilization review rep. After a short hold, say it is pending "
        "clinical review and needs the last two PT progress notes by fax to 1-800-555-0155. "
        "Reference UR-20391.",
    },
    {
        "name": "4b · Start a prior auth by phone",
        "category": "prior_auth",
        "counterpart": "Prior authorization intake",
        "on_behalf_of": PRACTICE,
        "goal": "Open a prior authorization request for the service below and get a case number. "
        "Give only the facts provided; clinical questions go to the practice.",
        "facts": {
            **_PRACTICE_FACTS,
            "member_id": "100200300",
            "patient_name": "Alex Synth",
            "date_of_birth": "1950-01-01",
            "service": "MRI lumbar spine, CPT 72148",
            "diagnosis_code": "M54.50",
            "ordering_provider": "Dr. Avery Stone",
            "requested_date": "November 18",
        },
        "limits": [
            _NO_CLINICAL,
            "If asked for clinical details not in the facts, say the practice will send notes.",
        ],
        "success_checks": [
            "Case number read back",
            "What the payer still needs, and the decision timeline, recorded",
        ],
        "rehearsal_script": "Play an intake rep. Take the request, then ask for 'duration of symptoms "
        "and conservative treatment tried' (the agent should defer). Give case number PA-77120; "
        "decision within 72 hours once notes arrive.",
    },
    # --- 5. Patient balances ---------------------------------------------------------------------------
    {
        "name": "5a · Balance reminder: wants a payment plan",
        "category": "patient_billing",
        "counterpart": "Patient",
        "on_behalf_of": PRACTICE,
        "goal": "Remind the patient of their balance after insurance, and help them set up a "
        "payment plan if they want one.",
        "facts": {
            **_PRACTICE_FACTS,
            "patient_name": "Dana Sample",
            "date_of_birth": "1971-09-09",
            "balance": "$186.00",
            "visit": "September 3 office visit",
            "payment_link": "pay.lakeside.example/88213",
            "plan_terms": "Up to 6 monthly payments, no interest",
        },
        "limits": [_IDENTITY_FIRST, _NO_CARD],
        "success_checks": [
            "Payment-plan request recorded with the number of months",
            "Payment link offered by text",
        ],
        "rehearsal_script": "Play the patient. Confirm identity. Say you can't pay it all; ask about a "
        "plan; choose 3 months; ask for the link by text.",
    },
    {
        "name": "5b · Balance reminder: disputed",
        "category": "patient_billing",
        "counterpart": "Patient",
        "on_behalf_of": PRACTICE,
        "goal": "Remind the patient of their balance. If they dispute it, record why and arrange a "
        "review, without promising any outcome.",
        "facts": {
            **_PRACTICE_FACTS,
            "patient_name": "Riley Placeholder",
            "date_of_birth": "1980-02-14",
            "balance": "$240.00",
            "visit": "August 21 visit",
        },
        "limits": [
            _IDENTITY_FIRST,
            _NO_CARD,
            "Do not promise a write-off, refund or outcome.",
        ],
        "success_checks": [
            "Reason for the dispute recorded",
            "Patient told the billing team will review and call back",
        ],
        "rehearsal_script": "Play the patient. Confirm identity. Insist your insurance should have "
        "paid it because the visit was preventive. Ask who will call you back.",
    },
    {
        "name": "5c · Balance reminder: hardship",
        "category": "patient_billing",
        "counterpart": "Patient",
        "on_behalf_of": PRACTICE,
        "goal": "Remind the patient of their balance. If they mention financial hardship, explain "
        "the financial assistance process and hand off to the billing team.",
        "facts": {
            **_PRACTICE_FACTS,
            "patient_name": "Morgan Trialson",
            "date_of_birth": "1958-11-30",
            "balance": "$412.00",
            "assistance_info": "Financial assistance form at lakeside.example/assist, or by mail",
        },
        "limits": [_IDENTITY_FIRST, _NO_CARD, "Be patient and kind; never pressure."],
        "success_checks": [
            "Hardship noted and handed to the billing team",
            "Assistance process explained",
        ],
        "rehearsal_script": "Play the patient. Confirm identity. Say you lost your job and can't pay; "
        "sound worried. Ask whether there is any help.",
    },
    # --- Showcase errand (beyond healthcare) -------------------------------------------------------
    {
        "name": "Showcase · Move a flight to the next day",
        "simulation": {
            "menu": "Thank you for calling. For reservations and changes, press 1. For baggage, press 2.",
            "hold_s": 30,
            "voice": "Zephyr",
        },
        "category": "travel",
        "counterpart": "Airline reservations",
        "on_behalf_of": "Sam Example (the passenger)",
        "goal": "Move the passenger's flight on the booking below to the earliest flight the next day "
        "in the same cabin, if that can be done without a fee.",
        "facts": {
            "passenger_name": "Sam Example",
            "confirmation_code": "QX7P2R",
            "flight": "UA 1234, Chicago O'Hare to Newark, December 4, 7:05 am",
        },
        "limits": [
            "Do not pay or accept any change fee or fare difference; ask what it would cost.",
            "Do not cancel the booking.",
        ],
        "success_checks": [
            "New flight number and time confirmed, or the exact fee to change",
            "Confirmation code read back",
        ],
        "rehearsal_script": "Answer as 'Airline reservations'. Ask for the confirmation code and name. "
        "Offer two flights the next morning; say the 9:10 am one is free and the 7:30 am one costs "
        "$75. Give the new confirmation as QX7P2R, seat 14C.",
    },
]
