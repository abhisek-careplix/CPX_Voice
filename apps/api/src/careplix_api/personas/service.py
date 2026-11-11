"""Voice personas (VS4): a workspace library of how agents sound, and what applying one means.

A persona is style only: the agent's spoken name, role, voice, greeting, warmth, formality, pace,
empathy, the languages it may switch to, words it must never say and phrases it likes to use. It
never touches tools, disclosures or the automated-identity disclosure, so a template's bounds hold
(SEC06). The Studio applies a persona by copying ``config_block`` into an agent's draft; the draft
then goes through the regression suite and the publish gate like any other edit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.auth import Principal
from careplix_api.db import utcnow
from careplix_api.errors import ApiError
from careplix_api.models import AuditEvent
from careplix_api.personas.models import Persona

# Gemini Live native-audio voices offered for agents.
VOICES = (
    "Puck",
    "Charon",
    "Kore",
    "Fenrir",
    "Aoede",
    "Leda",
    "Orus",
    "Zephyr",
    "Sulafat",
    "Achird",
)
LANGUAGES = {
    "en-US": "English",
    "es-US": "Spanish",
    "zh-CN": "Mandarin Chinese",
    "vi-VN": "Vietnamese",
    "ko-KR": "Korean",
    "hi-IN": "Hindi",
    "pt-BR": "Portuguese",
    "fr-FR": "French",
    "ru-RU": "Russian",
}
WARMTH = ("warm", "neutral", "brisk")
FORMALITY = ("formal", "neutral", "casual")
PACE = ("slow", "normal", "fast")
EMPATHY = ("standard", "high")
MAX_PERSONAS = 50

STARTERS: list[dict[str, Any]] = [
    {
        "name": "Warm patient coordinator",
        "description": "Patient calls: reminders, scheduling, follow-ups. Patient and reassuring.",
        "agent_name": "Ava",
        "role": "Patient care coordinator",
        "voice": "Aoede",
        "greeting": "Hi, this is Ava, an automated assistant from your care team.",
        "warmth": "warm",
        "formality": "neutral",
        "pace": "slow",
        "empathy": "high",
        "languages": ["en-US", "es-US"],
        "avoid": ["Calm down", "That's not my problem", "Per my last call"],
        "signature_phrases": ["Take your time.", "I'm glad you told me."],
        "character": "Calm and unhurried. Checks understanding, repeats key details back, and "
        "never rushes someone who sounds unsure or upset.",
    },
    {
        "name": "Efficient payer caller",
        "description": "Calls to insurers: benefits, prior auth and claim status. Brisk, precise.",
        "agent_name": "Avery",
        "role": "Benefits and authorization coordinator",
        "voice": "Charon",
        "greeting": "Hi, this is Avery, an automated assistant calling for the provider.",
        "warmth": "neutral",
        "formality": "formal",
        "pace": "normal",
        "empathy": "standard",
        "languages": ["en-US"],
        "avoid": ["I think", "Probably"],
        "signature_phrases": ["Let me read that back to confirm."],
        "character": "Courteous and to the point. Has every identifier ready, spells reference "
        "numbers back, and asks for the representative's name and a call reference before ending.",
    },
    {
        "name": "Bilingual front desk",
        "description": "Inbound calls for a practice. Switches to Spanish when the caller prefers.",
        "agent_name": "Sofia",
        "role": "Front desk assistant",
        "voice": "Leda",
        "greeting": "Thank you for calling, this is Sofia, the practice's automated assistant.",
        "warmth": "warm",
        "formality": "neutral",
        "pace": "normal",
        "empathy": "high",
        "languages": ["en-US", "es-US"],
        "avoid": [],
        "signature_phrases": ["Is there anything else I can help you with today?"],
        "character": "Friendly and organised. Offers Spanish as soon as the caller uses it, and "
        "keeps the call moving without sounding hurried.",
    },
]


def _clean_list(values: Any, *, limit: int, size: int) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = " ".join(str(value).split())[:size]
        if text and text not in out:
            out.append(text)
    return out[:limit]


def _choice(value: Any, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise ApiError(422, f"bad_{field}", f"Choose one of: {', '.join(allowed)}.")
    return value


def apply(persona: Persona, data: dict[str, Any]) -> None:
    """Validate and copy the editable fields onto ``persona``."""
    name = " ".join(str(data.get("name") or "").split())[:80]
    agent_name = " ".join(str(data.get("agent_name") or "").split())[:60]
    greeting = " ".join(str(data.get("greeting") or "").split())[:400]
    if not name or not agent_name or not greeting:
        raise ApiError(
            422, "missing_fields", "A persona needs a name, a spoken name and a greeting."
        )
    languages = [lang for lang in data.get("languages") or ["en-US"] if lang in LANGUAGES]
    if not languages:
        raise ApiError(422, "bad_languages", "Choose at least one supported language.")
    persona.name = name
    persona.description = (" ".join(str(data.get("description") or "").split())[:240]) or None
    persona.agent_name = agent_name
    persona.role = " ".join(str(data.get("role") or "Assistant").split())[:120]
    persona.voice = _choice(data.get("voice") or "Puck", VOICES, "voice")
    persona.greeting = greeting
    persona.warmth = _choice(data.get("warmth") or "neutral", WARMTH, "warmth")
    persona.formality = _choice(data.get("formality") or "neutral", FORMALITY, "formality")
    persona.pace = _choice(data.get("pace") or "normal", PACE, "pace")
    persona.empathy = _choice(data.get("empathy") or "standard", EMPATHY, "empathy")
    persona.languages = list(dict.fromkeys(languages))
    persona.avoid = _clean_list(data.get("avoid"), limit=20, size=120)
    persona.signature_phrases = _clean_list(data.get("signature_phrases"), limit=10, size=160)
    persona.character = (" ".join(str(data.get("character") or "").split())[:600]) or None
    persona.updated_at = utcnow()


def view(persona: Persona) -> dict[str, Any]:
    return {
        "id": persona.id,
        "name": persona.name,
        "description": persona.description,
        "agent_name": persona.agent_name,
        "role": persona.role,
        "voice": persona.voice,
        "greeting": persona.greeting,
        "warmth": persona.warmth,
        "formality": persona.formality,
        "pace": persona.pace,
        "empathy": persona.empathy,
        "languages": persona.languages or ["en-US"],
        "avoid": persona.avoid or [],
        "signature_phrases": persona.signature_phrases or [],
        "character": persona.character,
        "config": config_block(persona),
        "updated_at": persona.updated_at,
    }


def config_block(persona: Persona) -> dict[str, Any]:
    """What applying the persona writes into an agent config: identity name, role and greeting,
    the conversation voice, and the ``persona`` block. Disclosure, tools and authority untouched."""
    block: dict[str, Any] = {
        "source_id": persona.id,
        "source_name": persona.name,
        "warmth": persona.warmth,
        "formality": persona.formality,
        "pace": persona.pace,
        "empathy": persona.empathy,
        "languages": persona.languages or ["en-US"],
        "avoid": persona.avoid or [],
        "signature_phrases": persona.signature_phrases or [],
    }
    if persona.character:
        block["character"] = persona.character
    return {
        "identity": {
            "name": persona.agent_name,
            "role": persona.role,
            "greeting": persona.greeting,
        },
        "voice": persona.voice,
        "persona": block,
    }


def prompt_lines(persona: dict[str, Any] | None) -> list[str]:
    """How the persona reads to the voice model. Shared by the worker contract and the judge."""
    if not persona:
        return []
    lines = []
    style = {
        "warm": "warm and personable",
        "neutral": "friendly and even",
        "brisk": "brisk and efficient",
    }.get(persona.get("warmth", ""))
    tone = {"formal": "formal", "neutral": "plain", "casual": "casual"}.get(
        persona.get("formality", "")
    )
    if style or tone:
        lines.append(f"Tone: {', '.join(x for x in (style, tone and f'{tone} language') if x)}.")
    pace = {
        "slow": "Speak slowly and leave pauses; give one piece of information at a time.",
        "fast": "Keep a quick pace and short sentences, without sounding rushed.",
    }.get(persona.get("pace", ""))
    if pace:
        lines.append(pace)
    if persona.get("empathy") == "high":
        lines.append(
            "Acknowledge feelings before facts when the other person sounds worried, confused or "
            "upset."
        )
    langs = [LANGUAGES[c] for c in persona.get("languages") or [] if c in LANGUAGES]
    if len(langs) > 1:
        lines.append(
            f"Start in {langs[0]}. If the other person prefers {', '.join(langs[1:])}, switch and "
            "stay in that language for the rest of the call."
        )
    if persona.get("character"):
        lines.append(f"Character: {persona['character']}")
    if persona.get("signature_phrases"):
        lines.append(
            "Phrases you may use naturally: "
            + "; ".join(f'"{p}"' for p in persona["signature_phrases"])
        )
    if persona.get("avoid"):
        lines.append(
            "Never say any of these: " + "; ".join(f'"{p}"' for p in persona["avoid"]) + "."
        )
    return lines


async def get(session: AsyncSession, principal: Principal, persona_id: str) -> Persona:
    persona = await session.get(Persona, persona_id)
    if persona is None or persona.workspace_id != principal.workspace_id:
        raise ApiError(404, "not_found", "Persona not found.")
    return persona


async def list_all(session: AsyncSession, principal: Principal) -> list[Persona]:
    return list(
        await session.scalars(
            select(Persona)
            .where(Persona.workspace_id == principal.workspace_id)
            .order_by(Persona.name)
        )
    )


async def create(session: AsyncSession, principal: Principal, data: dict[str, Any]) -> Persona:
    if len(await list_all(session, principal)) >= MAX_PERSONAS:
        raise ApiError(
            422, "too_many_personas", f"A workspace keeps at most {MAX_PERSONAS} personas."
        )
    persona = Persona(workspace_id=principal.workspace_id, created_by=principal.user_id)
    apply(persona, data)
    session.add(persona)
    await session.flush()
    audit(session, principal, "persona.created", persona)
    return persona


async def add_starters(session: AsyncSession, principal: Principal) -> int:
    have = {p.name for p in await list_all(session, principal)}
    added = 0
    for starter in STARTERS:
        if starter["name"] not in have:
            await create(session, principal, starter)
            added += 1
    return added


def audit(session: AsyncSession, principal: Principal, action: str, persona: Persona) -> None:
    session.add(
        AuditEvent(
            workspace_id=principal.workspace_id,
            actor=principal.user_id,
            action=action,
            target=persona.id,
            detail={"name": persona.name},
        )
    )
