"""Who to contact for an open shift or slot, best first (OPERATING_PLAN §6 shift filler, D2).

Workstream B's contact-many calls :func:`rank` with a :class:`Need` and contacts the eligible
people in order; this module never contacts anyone. For each person it either gives a score with
the reasons behind it, or the one reason they were left out:

- **Hard rules** (excluded): inactive, do-not-contact, no channel they may be reached on, a
  required skill missing, a required credential missing or expired by the end of the shift, any
  expired compliance credential (licence, CPR, TB), outside their availability, beyond travel
  distance, or declined by the client.
- **Score** (0-100): reliability 40, distance 30, client preference 15, language 10, explicit
  availability 5.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.db import utcnow
from careplix_api.people import policy
from careplix_api.people.models import Credential, Person, Relationship
from careplix_api.phi import PhiBox
from careplix_api.settings import Settings

DEFAULT_MAX_KM = 40.0


@dataclass(frozen=True)
class Need:
    starts_at: datetime
    ends_at: datetime
    kinds: tuple[str, ...] = ("caregiver",)  # ("patient",) for a cancelled slot's waitlist
    skills: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()
    client_id: str | None = None  # the client's home and caregiver fit come from their profile
    location: tuple[float, float] | None = None  # (lat, lon) when there is no client
    max_km: float | None = None
    language: str | None = None
    contact_at: datetime | None = None  # when outreach would start (default: now)
    exclude: tuple[str, ...] = ()
    limit: int = 50


@dataclass
class Candidate:
    person_id: str
    kind: str
    score: float
    channel: str
    contact_now: bool
    contact_reason: str
    distance_km: float | None
    reliability: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Exclusion:
    person_id: str
    reason: str
    detail: str | None = None


@dataclass
class Ranking:
    eligible: list[Candidate]
    excluded: list[Exclusion]


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in kilometres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def reliability_score(stats: dict[str, int] | None) -> float:
    """0-1, smoothed so a new caregiver starts in the middle rather than at either end."""
    s = stats or {}
    offered, accepted = int(s.get("offered", 0)), int(s.get("accepted", 0))
    completed, no_shows, late = (
        int(s.get("completed", 0)),
        int(s.get("no_shows", 0)),
        int(s.get("late", 0)),
    )
    acceptance = (accepted + 1) / (offered + 2)
    showing = (completed + 1) / (completed + no_shows + 2)
    punctual = 1 - min(1.0, late / (completed + 1))
    return round(0.4 * acceptance + 0.5 * showing + 0.1 * punctual, 3)


def _home(pii: dict[str, Any]) -> tuple[float, float] | None:
    try:
        return float(pii["lat"]), float(pii["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def _channel(person: Person, at: datetime) -> tuple[str | None, bool, str]:
    """The channel to use and whether contact may start at ``at``.

    Preferred channel first, then text, then voice. ``None`` when no channel is ever allowed."""
    order = list(dict.fromkeys([person.preferred_channel, "sms", "voice"]))
    later = None
    for channel in order:
        if channel == "email":
            continue  # no email channel yet (P2)
        decision = policy.can_contact(person, channel, at)
        if decision.allowed:
            return channel, True, "ok"
        if decision.reason == "outside_window" and later is None:
            later = channel
    if later:
        return later, False, "outside_window"
    return None, False, "cannot_contact"


def score(
    people: list[Person],
    credentials: dict[str, list[Credential]],
    homes: dict[str, tuple[float, float] | None],
    fit: dict[str, str],
    need: Need,
    origin: tuple[float, float] | None,
    now: datetime,
) -> Ranking:
    """The pure ranking: no database, so tests can feed it rows directly."""
    eligible: list[Candidate] = []
    excluded: list[Exclusion] = []
    shift_end: date = need.ends_at.date()
    contact_at = need.contact_at or now
    for person in people:
        out = lambda reason, detail=None, p=person: excluded.append(  # noqa: E731
            Exclusion(p.id, reason, detail)
        )
        if person.id in need.exclude or person.kind not in need.kinds:
            continue
        if person.status != "active":
            out("inactive")
            continue
        if person.do_not_contact:
            out("do_not_contact")
            continue
        channel, contact_now, contact_reason = _channel(person, contact_at)
        if channel is None:
            out("cannot_contact")
            continue
        if fit.get(person.id) == "declined_caregiver":
            out("declined_by_client")
            continue
        missing = [s for s in need.skills if s not in (person.skills or [])]
        if missing:
            out("missing_skill", ", ".join(missing))
            continue
        held = {c.kind: c for c in credentials.get(person.id, [])}
        lacking = [c for c in need.credentials if c not in held]
        if lacking:
            out("missing_credential", ", ".join(lacking))
            continue
        expired = sorted(
            c.kind
            for c in held.values()
            if (c.kind in need.credentials or c.kind in policy.COMPLIANCE_CREDENTIALS)
            and policy.credential_state(c.expires_on, shift_end, 0) == "expired"
        )
        if expired:
            out("expired_credential", ", ".join(expired))
            continue
        windows = person.availability or []
        tz = policy.zone(person.timezone)
        if windows and not policy.within(windows, need.starts_at, need.ends_at, tz):
            out("unavailable")
            continue
        if not windows and "caregiver" in need.kinds and person.kind == "caregiver":
            out("unavailable", "no availability on file")
            continue
        home = homes.get(person.id)
        distance = round(km(home, origin), 1) if home and origin else None
        limit = min(v for v in (need.max_km, person.max_travel_km, DEFAULT_MAX_KM) if v is not None)
        if distance is not None and distance > limit:
            out("too_far", f"{distance} km")
            continue

        reasons: list[str] = []
        rel = reliability_score(person.reliability)
        total = 40 * rel
        reasons.append(f"reliability {round(rel * 100)}%")
        if distance is None:
            total += 10
        else:
            total += 30 * max(0.0, 1 - distance / limit)
            reasons.append(f"{distance} km away")
        if fit.get(person.id) == "preferred_caregiver":
            total += 15
            reasons.append("client's preferred caregiver")
        if need.language and policy.norm(person.language)[:2] == policy.norm(need.language)[:2]:
            total += 10
            reasons.append(f"speaks {need.language}")
        if windows:
            total += 5
            reasons.append("available then")
        if need.skills:
            reasons.append("has " + ", ".join(s.replace("_", " ") for s in need.skills))
        eligible.append(
            Candidate(
                person_id=person.id,
                kind=person.kind,
                score=round(total, 1),
                channel=channel,
                contact_now=contact_now,
                contact_reason=contact_reason,
                distance_km=distance,
                reliability=rel,
                reasons=reasons,
            )
        )
    eligible.sort(key=lambda c: (-c.score, c.distance_km or 0.0, c.person_id))
    return Ranking(eligible[: need.limit], excluded)


async def rank(
    session: AsyncSession,
    workspace_id: str,
    settings: Settings,
    need: Need,
    now: datetime | None = None,
) -> Ranking:
    """Eligible people for ``need`` in this workspace, best first, and who was left out."""
    if need.ends_at <= need.starts_at:
        raise ValueError("A need must end after it starts.")
    box = PhiBox(settings)
    people = list(
        await session.scalars(
            select(Person).where(Person.workspace_id == workspace_id, Person.kind.in_(need.kinds))
        )
    )
    ids = [p.id for p in people]
    credentials: dict[str, list[Credential]] = defaultdict(list)
    if ids:
        for c in await session.scalars(
            select(Credential).where(
                Credential.workspace_id == workspace_id, Credential.person_id.in_(ids)
            )
        ):
            credentials[c.person_id].append(c)
    origin = need.location
    fit: dict[str, str] = {}
    if need.client_id:
        client = await session.get(Person, need.client_id)
        if client is None or client.workspace_id != workspace_id:
            raise LookupError("client_not_found")
        origin = origin or _home(box.open(client.pii))
        for r in await session.scalars(
            select(Relationship).where(
                Relationship.workspace_id == workspace_id,
                Relationship.subject_id == client.id,
                Relationship.relation.in_(policy.FIT_RELATIONS),
                Relationship.revoked_at.is_(None),
            )
        ):
            # A decline outranks a preference if both were ever recorded.
            if fit.get(r.related_id) != "declined_caregiver":
                fit[r.related_id] = r.relation
    homes = {p.id: _home(box.open(p.pii)) for p in people}
    return score(people, credentials, homes, fit, need, origin, now or utcnow())


def view(ranking: Ranking, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "eligible": [
            {
                "person_id": c.person_id,
                "name": labels.get(c.person_id),
                "kind": c.kind,
                "score": c.score,
                "channel": c.channel,
                "contact_now": c.contact_now,
                "contact_reason": c.contact_reason,
                "distance_km": c.distance_km,
                "reliability": c.reliability,
                "reasons": c.reasons,
            }
            for c in ranking.eligible
        ],
        "excluded": [
            {
                "person_id": e.person_id,
                "name": labels.get(e.person_id),
                "reason": e.reason,
                "detail": e.detail,
            }
            for e in ranking.excluded
        ],
    }
