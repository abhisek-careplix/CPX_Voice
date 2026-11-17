"""Secure links: issue one for a case, open it with its token, take the patient's answers.

- The token is 32 random bytes; only an HMAC of it is stored, so a database read can't open forms.
- A link expires (``LINK_TTL``), works for one submission, and is revoked when a newer one is sent.
- The page shows the practice's name, the questions and nothing else about the patient.
- Answers are sealed at rest. Questionnaire answers become the case's scripted answers (red flags
  escalate at once); intake answers stay sealed on the link for the intake playbook.

Labs serves the form from the Studio (behind IAP), so only signed-in testers can open a link; a
patient-facing forms host is the production step (see docs/solution/CLINICAL_SUPPORT.md).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import careplix_api.cases as cases
from careplix_api.cases import contract
from careplix_api.clinical import scripts
from careplix_api.db import as_utc, utcnow
from careplix_api.errors import ApiError
from careplix_api.forms.models import FormLink
from careplix_api.models import AuditEvent, Case
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

KINDS = ("questionnaire", "intake")
LINK_TTL = timedelta(days=7)
MAX_IMAGE_BYTES = 1_500_000
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic")

INTAKE_FIELDS: list[dict[str, Any]] = [
    # Demographics (proposed to the EHR as a patient update)
    {"key": "first_name", "label": "First name", "section": "about", "required": True},
    {"key": "last_name", "label": "Last name", "section": "about", "required": True},
    {
        "key": "date_of_birth",
        "label": "Date of birth",
        "section": "about",
        "kind": "date",
        "required": True,
    },
    {
        "key": "sex",
        "label": "Sex",
        "section": "about",
        "kind": "select",
        "options": ["female", "male", "other"],
        "required": False,
    },
    {"key": "email", "label": "Email", "section": "about", "kind": "email", "required": False},
    {"key": "phone", "label": "Mobile phone", "section": "about", "kind": "tel", "required": True},
    {"key": "address_line1", "label": "Street address", "section": "about", "required": True},
    {"key": "city", "label": "City", "section": "about", "required": True},
    {"key": "state", "label": "State", "section": "about", "required": True},
    {"key": "postal_code", "label": "ZIP code", "section": "about", "required": True},
    {
        "key": "preferred_language",
        "label": "Preferred language",
        "section": "about",
        "required": False,
    },
    # Coverage (proposed to the EHR as a coverage record), as printed on the card
    {"key": "payer_name", "label": "Insurance company", "section": "coverage", "required": False},
    {"key": "member_id", "label": "Member ID", "section": "coverage", "required": False},
    {"key": "group_number", "label": "Group number", "section": "coverage", "required": False},
    {
        "key": "subscriber_name",
        "label": "Policy holder's name",
        "section": "coverage",
        "required": False,
    },
    {
        "key": "relationship",
        "label": "Your relationship to the policy holder",
        "section": "coverage",
        "kind": "select",
        "options": ["self", "spouse", "child", "other"],
        "required": False,
    },
    {
        "key": "card_front",
        "label": "Insurance card, front",
        "section": "coverage",
        "kind": "image",
        "required": False,
    },
    {
        "key": "card_back",
        "label": "Insurance card, back",
        "section": "coverage",
        "kind": "image",
        "required": False,
    },
    {
        "key": "no_insurance",
        "label": "I don't have insurance",
        "section": "coverage",
        "kind": "checkbox",
        "required": False,
    },
    {
        "key": "confirm",
        "label": "The information above is correct",
        "section": "confirm",
        "kind": "checkbox",
        "required": True,
    },
]
INTAKE_KEYS = {f["key"]: f for f in INTAKE_FIELDS}


def _hash(settings: Settings, token: str) -> str:
    return hmac.new(settings.signing_key().encode(), token.encode(), hashlib.sha256).hexdigest()


def url_for(settings: Settings, token: str) -> str:
    base = (settings.studio_url or "").rstrip("/")
    return f"{base}/f/{token}"


async def issue(
    session: AsyncSession, settings: Settings, case: Case, kind: str, actor: str
) -> tuple[FormLink, str]:
    """A new link for the case (older open links for the same form are revoked)."""
    if kind not in KINDS:
        raise ApiError(422, "bad_form", f"Form is one of {', '.join(KINDS)}.")
    now = utcnow()
    await session.execute(
        update(FormLink)
        .where(
            FormLink.case_id == case.id,
            FormLink.kind == kind,
            FormLink.submitted_at.is_(None),
            FormLink.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    token = secrets.token_urlsafe(32)
    link = FormLink(
        workspace_id=case.workspace_id,
        case_id=case.id,
        kind=kind,
        token_hash=_hash(settings, token),
        expires_at=now + LINK_TTL,
        created_by=actor,
    )
    session.add(link)
    await session.flush()
    return link, url_for(settings, token)


async def by_token(session: AsyncSession, settings: Settings, token: str) -> FormLink:
    from careplix_api import tenancy

    with tenancy.unscoped():
        link = await session.scalar(
            select(FormLink).where(FormLink.token_hash == _hash(settings, token[:200]))
        )
    if link is None or link.revoked_at is not None:
        raise ApiError(
            404, "link_not_found", "This link isn't valid. Ask the practice for a new one."
        )
    tenancy.set_tenant(link.workspace_id)  # the rest of the request is this workspace only
    if as_utc(link.expires_at) < utcnow() and link.submitted_at is None:
        raise ApiError(
            410, "link_expired", "This link has expired. Ask the practice for a new one."
        )
    return link


async def latest(session: AsyncSession, case: Case, kind: str) -> FormLink | None:
    return await session.scalar(
        select(FormLink)
        .where(FormLink.case_id == case.id, FormLink.kind == kind)
        .order_by(FormLink.created_at.desc())
        .limit(1)
    )


def definition(case: Case, link: FormLink) -> dict[str, Any]:
    facts = cases.context(case)
    base = {
        "kind": link.kind,
        "practice": facts.get("practice_name") or "Your practice",
        "practice_phone": facts.get("practice_phone"),
        "submitted": link.submitted_at is not None,
        "expires_at": as_utc(link.expires_at),
    }
    if link.kind == "questionnaire":
        s = scripts.script(cases.type_id(case)) or {}
        return {
            **base,
            "title": s.get("name", "Questionnaire"),
            "intro": s.get("intro"),
            "emergency": "If you have an emergency, call 911 now. This form isn't checked "
            "around the clock.",
            "questions": [
                {k: q[k] for k in ("key", "text", "kind", "required")}
                for q in scripts.questions(cases.type_id(case))
            ],
        }
    return {
        **base,
        "title": "Registration",
        "intro": "Please complete your details before your visit. Photos of your insurance card "
        "help us check your coverage.",
        "fields": [{"kind": "text", **f} for f in INTAKE_FIELDS],
    }


def _image(value: Any) -> str | None:
    """A data URL of an accepted image type and size, or None."""
    if not value:
        return None
    if not isinstance(value, str) or not value.startswith("data:") or ";base64," not in value:
        raise ApiError(422, "bad_image", "Upload the card as a photo.")
    head, data = value.split(";base64,", 1)
    if head[5:] not in IMAGE_TYPES:
        raise ApiError(422, "bad_image", "Upload a JPEG, PNG, WebP or HEIC photo.")
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise ApiError(422, "bad_image", "That photo couldn't be read.") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ApiError(413, "image_too_large", "Use a photo under 1.5 MB.")
    return value


def clean_intake(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, field in INTAKE_KEYS.items():
        val = values.get(key)
        kind = field.get("kind")
        if kind == "image":
            val = _image(val)
        elif kind == "checkbox":
            val = bool(val)
        elif val is not None:
            val = str(val).strip()[:200] or None
            if kind == "select" and val and val not in field["options"]:
                raise ApiError(
                    422, "bad_choice", f"Choose one of the options for {field['label']}."
                )
        if field["required"] and not val:
            raise ApiError(422, "missing", f"{field['label']} is required.")
        out[key] = val
    has_card = out.get("member_id") or out.get("card_front")
    if not has_card and not out.get("no_insurance"):
        raise ApiError(
            422, "coverage_missing", "Add your insurance details, or tick 'I don't have insurance'."
        )
    return out


async def submit(
    session: AsyncSession, settings: Settings, link: FormLink, values: dict[str, Any]
) -> dict[str, Any]:
    from careplix_api.clinical import answers

    if link.submitted_at is not None:
        raise ApiError(409, "already_submitted", "This form was already sent. Thank you.")
    case = await session.get(Case, link.case_id)
    if case is None or (case.status or "open") in contract.TERMINAL:
        raise ApiError(410, "closed", "This request is closed. Please call the practice.")
    box = PhiBox(settings)
    out: dict[str, Any] = {"state": "received"}
    if link.kind == "questionnaire":
        given = []
        questions = scripts.by_key(cases.type_id(case))
        raw = values.get("answers") or {}
        if not isinstance(raw, dict):
            raise ApiError(422, "bad_answers", "Answers are by question.")
        for key, item in raw.items():
            q = questions.get(key)
            if q is None or not isinstance(item, dict):
                raise ApiError(422, "unknown_question", "That isn't one of the questions.")
            answer = item.get("answer")
            detail = (item.get("detail") or "").strip()[:300] or None
            if q["kind"] == scripts.TEXT:
                if not detail:
                    continue
                answer = "text"
            given.append({"question": key, "answer": answer, "detail": detail})
        missing = [
            q["key"]
            for q in questions.values()
            if q["required"] and q["key"] not in {g["question"] for g in given}
        ]
        if missing:
            raise ApiError(422, "missing", "Please answer every question.", {"missing": missing})
        result = await answers.record(
            session, settings, case, given, source="form", actor="patient_form", ref=link.id
        )
        if result.get("red_flag"):
            # The page shows the same words the agent would read.
            out["say"] = result.get("say")
        link.answers = box.seal({"answers": given})
    else:
        clean = clean_intake(values)
        link.answers = box.seal(clean)
        case.data = {
            **(case.data or {}),
            "intake": {
                "submitted_at": utcnow().isoformat(),
                "link_id": link.id,
                "has_card_photo": bool(clean.get("card_front")),
                "no_insurance": bool(clean.get("no_insurance")),
            },
        }
        contract.add_link(
            session,
            case,
            part="evidence",
            kind="note",
            ref=link.id,
            label="Registration form submitted by secure link",
            actor="patient_form",
        )
        from careplix_api.clinical.answers import _wake

        await _wake(session, case)
    link.submitted_at = utcnow()
    contract.add_link(
        session,
        case,
        part="informed",
        kind="message",
        ref=f"form:{link.id}",
        label="Patient saw the form's confirmation page",
        audience="patient",
        channel="portal",
        delivery="delivered",
        actor="patient_form",
    )
    session.add(
        AuditEvent(
            workspace_id=link.workspace_id,
            actor="patient_form",
            action="form.submitted",
            target=case.id,
            detail={"kind": link.kind, "link": link.id},
        )
    )
    return out


def intake_answers(settings: Settings, link: FormLink | None) -> dict[str, Any]:
    return PhiBox(settings).open(link.answers) if link and link.answers else {}


def view(link: FormLink | None) -> dict[str, Any] | None:
    if link is None:
        return None
    return {
        "id": link.id,
        "kind": link.kind,
        "sent_at": link.created_at,
        "expires_at": as_utc(link.expires_at),
        "opened_at": link.opened_at,
        "submitted_at": link.submitted_at,
        "revoked": link.revoked_at is not None,
    }
