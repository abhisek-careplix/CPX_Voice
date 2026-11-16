"""The payer directory (RC1): our payer IDs and names, and which transactions each answers.

Every workspace starts with a built-in list of common payers; "Refresh from the network" searches
the clearinghouse's payer network and updates (or adds) rows. Support values are the network's
(``SUPPORTED``, ``NOT_SUPPORTED``, ``ENROLLMENT_REQUIRED``); ``None`` means not known yet, and a
claim status check is then tried.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from careplix_api.claims.models import ClearinghousePayer
from careplix_api.db import as_utc, utcnow
from careplix_api.integrations import clearinghouse
from careplix_api.integrations.clearinghouse import NOT_SUPPORTED, SUPPORTED
from careplix_api.models import PayerConnection
from careplix_api.settings import Settings

_COMMERCIAL = {"eligibility": SUPPORTED, "claim_status": None, "claims": SUPPORTED, "era": None}
_MAC = {"eligibility": None, "claim_status": NOT_SUPPORTED, "claims": SUPPORTED, "era": None}
# (payer ID, name, programs, support). Medicare Administrative Contractors never answer 276/277.
BUILTIN: tuple[tuple[str, str, list[str], dict[str, Any]], ...] = (
    ("60054", "Aetna", ["COMMERCIAL"], _COMMERCIAL),
    ("87726", "UnitedHealthcare", ["COMMERCIAL", "MEDICARE_ADVANTAGE"], _COMMERCIAL),
    ("62308", "Cigna", ["COMMERCIAL"], _COMMERCIAL),
    ("61101", "Humana", ["COMMERCIAL", "MEDICARE_ADVANTAGE"], _COMMERCIAL),
    ("00621", "Blue Cross Blue Shield of Illinois", ["COMMERCIAL"], _COMMERCIAL),
    ("68069", "Ambetter", ["COMMERCIAL"], _COMMERCIAL),
    ("KSRCN", "Kaiser Permanente (Northern California)", ["COMMERCIAL"], _COMMERCIAL),
    (
        "CMS",
        "Medicare (eligibility, HETS)",
        ["MEDICARE"],
        {"eligibility": SUPPORTED, "claim_status": NOT_SUPPORTED, "claims": None, "era": None},
    ),
    ("06102", "Medicare Part B, Illinois (NGS, J6)", ["MEDICARE"], _MAC),
)


def view(row: ClearinghousePayer) -> dict[str, Any]:
    return {
        "id": row.id,
        "payer_id": row.payer_id,
        "name": row.name,
        "aliases": row.aliases or [],
        "programs": row.programs or [],
        "support": row.support or {},
        "phone": row.phone,
        "source": row.source,
        "claim_status_route": clearinghouse.claim_status_route(
            row.payer_id, (row.support or {}).get("claim_status")
        ),
        "refreshed_at": as_utc(row.refreshed_at).isoformat() if row.refreshed_at else None,
    }


async def ensure(session: AsyncSession, workspace_id: str) -> None:
    """Add the built-in payers a workspace doesn't have yet (never overwrites a row)."""
    have = set(
        await session.scalars(
            select(ClearinghousePayer.payer_id).where(
                ClearinghousePayer.workspace_id == workspace_id
            )
        )
    )
    for payer_id, name, programs, support in BUILTIN:
        if payer_id not in have:
            session.add(
                ClearinghousePayer(
                    workspace_id=workspace_id,
                    payer_id=payer_id,
                    name=name,
                    programs=list(programs),
                    support=dict(support),
                    aliases=[],
                    source="builtin",
                )
            )
    await session.flush()


async def listing(session: AsyncSession, workspace_id: str, q: str = "") -> list[dict[str, Any]]:
    await ensure(session, workspace_id)
    needle = q.strip().lower()
    rows = await session.scalars(
        select(ClearinghousePayer)
        .where(ClearinghousePayer.workspace_id == workspace_id)
        .order_by(ClearinghousePayer.name)
    )
    return [
        view(r)
        for r in rows
        if not needle
        or needle in r.name.lower()
        or needle == r.payer_id.lower()
        or needle in {a.lower() for a in r.aliases or []}
    ]


async def find(session: AsyncSession, workspace_id: str, payer_id: str | None):
    if not payer_id:
        return None
    return await session.scalar(
        select(ClearinghousePayer).where(
            ClearinghousePayer.workspace_id == workspace_id,
            ClearinghousePayer.payer_id == payer_id,
        )
    )


async def refresh(
    session: AsyncSession,
    workspace_id: str,
    conn: PayerConnection,
    settings: Settings,
    query: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Search the network and upsert what it returns. Names and phones a person set are kept."""
    gateway = clearinghouse.for_connection(conn, settings, transport)
    try:
        found = await gateway.search_payers(query)
    finally:
        await gateway.aclose()
    out = []
    for entry in found:
        if not entry["payer_id"]:
            continue
        row = await find(session, workspace_id, entry["payer_id"])
        if row is None:
            row = ClearinghousePayer(
                workspace_id=workspace_id,
                payer_id=entry["payer_id"],
                name=entry["name"][:160] or entry["payer_id"],
                source="network",
            )
            session.add(row)
        row.network_id = entry["network_id"]
        row.aliases, row.programs = entry["aliases"], entry["programs"]
        row.support = {**(row.support or {}), **{k: v for k, v in entry["support"].items() if v}}
        row.refreshed_at = utcnow()
        await session.flush()
        out.append(view(row))
    return out
