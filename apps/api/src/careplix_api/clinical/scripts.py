"""Scripted questions the agent asks word for word, by voice or on a secure form.

Server-owned and versioned, like case types: an agent can't reword, add or drop a question. The
red-flag rule is the protocol's, not the agent's judgement: a ``yes`` (or ``unsure``) to a
red-flag question goes to a clinician at once. "Attention" questions are surfaced first in the
summary a clinician reads; nothing here interprets an answer.

These scripts are starting points for the practice's clinical lead to approve before go-live
(labs runs them on synthetic patients only).
"""

from __future__ import annotations

from typing import Any

VERSION = 1
YES_NO = "yes_no"
TEXT = "text"
FLAG_ANSWERS = ("yes", "unsure")


def _q(
    key: str,
    text: str,
    kind: str = YES_NO,
    *,
    red_flag: bool = False,
    attention: bool = False,
    required: bool = True,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "text": text,
        "kind": kind,
        "red_flag": red_flag,
        "attention": attention,
        "required": required,
        "label": label or key.replace("_", " ").capitalize(),
    }


QUESTION_FOR_TEAM = _q(
    "question_for_team",
    "Is there anything you'd like us to pass on to your care team?",
    TEXT,
    required=False,
    label="Question for the care team",
)

SCRIPTS: dict[str, dict[str, Any]] = {
    "discharge_followup": {
        "name": "Discharge follow-up check",
        "intro": "Your care team asked me to check in after your hospital stay.",
        "questions": [
            _q(
                "feeling",
                "Since you got home from the hospital, how are you feeling overall?",
                TEXT,
                label="How they feel",
            ),
            _q(
                "chest_pain",
                "Have you had any chest pain or chest pressure since you got home?",
                red_flag=True,
                label="Chest pain or pressure",
            ),
            _q(
                "breathing",
                "Are you more short of breath now than when you left the hospital?",
                red_flag=True,
                label="More short of breath",
            ),
            _q(
                "fever",
                "Have you had a fever of 100.4 degrees or higher?",
                red_flag=True,
                label="Fever 100.4 or higher",
            ),
            _q(
                "bleeding_or_wound",
                "Have you had any new bleeding, or a wound that is red, swollen or draining?",
                red_flag=True,
                label="New bleeding or wound problem",
            ),
            _q(
                "fall_or_confusion",
                "Have you had a fall, fainted, or felt newly confused?",
                red_flag=True,
                label="Fall, fainting or new confusion",
            ),
            _q(
                "has_medicines",
                "Do you have all the medicines listed on your discharge papers?",
                attention=True,
                label="Has their discharge medicines",
            ),
            _q(
                "understands_medicines",
                "Do you know how and when to take each of them?",
                attention=True,
                label="Knows how to take them",
            ),
            _q(
                "follow_up_booked",
                "Do you have a follow-up appointment booked with us?",
                attention=True,
                label="Follow-up visit booked",
            ),
            QUESTION_FOR_TEAM,
        ],
    },
    "previsit_prep": {
        "name": "Pre-visit questionnaire",
        "intro": "A few questions so your clinician can prepare for your visit.",
        "questions": [
            _q(
                "urgent_now",
                "Are you having chest pain, trouble breathing, or any symptom that feels like an "
                "emergency right now?",
                red_flag=True,
                label="Emergency symptoms now",
            ),
            _q(
                "visit_reason",
                "What would you like to talk about at this visit?",
                TEXT,
                label="What they want to discuss",
            ),
            _q(
                "new_symptoms",
                "Do you have any new symptoms since your last visit?",
                attention=True,
                label="New symptoms",
            ),
            _q(
                "medicine_changes",
                "Have any of your medicines changed, or have you stopped taking any?",
                attention=True,
                label="Medicine changes",
            ),
            _q(
                "outside_care",
                "Have you seen another doctor, or been to a hospital or urgent care, since your "
                "last visit?",
                attention=True,
                label="Care elsewhere since last visit",
            ),
            _q(
                "refills_needed",
                "Will you need any refills at this visit?",
                label="Refills needed",
            ),
            QUESTION_FOR_TEAM,
        ],
    },
    "refill_request": {
        "name": "Refill request details",
        "intro": "I'm calling to confirm the details of your refill request.",
        "questions": [
            _q("medication", "Which medicine do you need refilled?", TEXT, label="Medicine"),
            _q(
                "dose",
                "What dose is written on the label?",
                TEXT,
                required=False,
                label="Dose on the label",
            ),
            _q(
                "pharmacy",
                "Which pharmacy should it go to?",
                TEXT,
                label="Pharmacy",
            ),
            _q(
                "days_left",
                "About how many days of this medicine do you have left?",
                TEXT,
                required=False,
                label="Days left",
            ),
            QUESTION_FOR_TEAM,
        ],
    },
}


def script(case_type: str) -> dict[str, Any] | None:
    return SCRIPTS.get(case_type)


def questions(case_type: str) -> list[dict[str, Any]]:
    s = script(case_type)
    return list(s["questions"]) if s else []


def by_key(case_type: str) -> dict[str, dict[str, Any]]:
    return {q["key"]: q for q in questions(case_type)}


def spoken(case_type: str) -> str:
    """The questions as the call brief gives them: key, then the exact words."""
    return " ".join(f'[{q["key"]}] "{q["text"]}"' for q in questions(case_type))


def is_red_flag(question: dict[str, Any], answer: str) -> bool:
    return bool(question.get("red_flag")) and answer in FLAG_ANSWERS


def view() -> dict[str, Any]:
    return {"version": VERSION, "scripts": SCRIPTS}
