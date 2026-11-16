"""Claims records: claims to follow, each claim status answer kept as evidence, the payer directory.

Every table carries ``workspace_id``; queries always filter by the principal's workspace.
Patient identifiers (name, date of birth, member ID) are sealed like the practice mirror (SEC08),
and so is the raw 277 answer. Summaries hold outcomes only: status, codes, dates and amounts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class Claim(IdMixin, Base):
    """One submitted claim to follow until the payer finalizes it (RC6, workflow R4)."""

    __tablename__ = "claims"
    __table_args__ = (UniqueConstraint("workspace_id", "source", "source_ref"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source: Mapped[str] = mapped_column(String(12))  # sample | ehr
    source_ref: Mapped[str] = mapped_column(String(60))
    ehr_connection_id: Mapped[str | None] = mapped_column(String(36))
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    claim_number: Mapped[str] = mapped_column(String(40))  # our patient control number (CLM01)
    payer_claim_number: Mapped[str | None] = mapped_column(String(60))
    payer_id: Mapped[str | None] = mapped_column(String(40))  # clearinghouse payer ID
    payer_name: Mapped[str] = mapped_column(String(160))
    payer_phone: Mapped[str | None] = mapped_column(String(20))
    plan_kind: Mapped[str | None] = mapped_column(String(24))  # medicare | medicare_advantage | …
    provider_name: Mapped[str] = mapped_column(String(160))
    provider_npi: Mapped[str] = mapped_column(String(10))
    patient_ref: Mapped[str | None] = mapped_column(String(40))
    # Sealed {display_name, first_name, last_name, date_of_birth, member_id, gender}.
    patient: Mapped[str | None] = mapped_column(Text)
    date_of_service: Mapped[str] = mapped_column(String(10))
    billed_amount: Mapped[str] = mapped_column(String(16))
    cpt_code: Mapped[str | None] = mapped_column(String(10))
    submitted_on: Mapped[str] = mapped_column(String(10))  # when the payer received the claim
    # Synthetic samples answer with a synthetic 277 for this scenario (claims.samples).
    sample_scenario: Mapped[str | None] = mapped_column(String(20))
    # The latest electronic status (claims.service.apply).
    status: Mapped[str | None] = mapped_column(String(16), index=True)
    status_label: Mapped[str | None] = mapped_column(String(60))
    status_since: Mapped[str | None] = mapped_column(String(10))  # payer's effective date
    status_mock: Mapped[bool | None] = mapped_column(Boolean)
    amount_paid: Mapped[str | None] = mapped_column(String(16))
    no_route: Mapped[str | None] = mapped_column(String(120))  # why there's no electronic status
    last_checked_at: Mapped[datetime | None] = mapped_column()
    last_check_id: Mapped[str | None] = mapped_column(String(36))
    next_check_at: Mapped[datetime | None] = mapped_column(index=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)  # connectivity errors in a row
    # The payer call, once electronic status is stale or unavailable (claims.follow_up).
    follow_up_case_id: Mapped[str | None] = mapped_column(String(36))
    follow_up_reason: Mapped[str | None] = mapped_column(String(160))
    follow_up_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ClaimStatusCheck(IdMixin, Base):
    """One claim status answer (276/277), kept as evidence with the payer's response."""

    __tablename__ = "claim_status_checks"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    clearinghouse_connection_id: Mapped[str | None] = mapped_column(String(36))
    integration_call_id: Mapped[str | None] = mapped_column(String(36))
    trigger: Mapped[str] = mapped_column(String(12))  # schedule | manual
    source: Mapped[str] = mapped_column(String(14))  # clearinghouse | sample | none
    mode: Mapped[str] = mapped_column(String(12))  # test | production | sample
    mock: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(12))  # done | failed | unavailable
    status: Mapped[str] = mapped_column(String(16))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw: Mapped[str | None] = mapped_column(Text)  # sealed 277 response (with the X12 text)
    message: Mapped[str | None] = mapped_column(String(200))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class ClearinghousePayer(IdMixin, Base):
    """The workspace's payer directory: our payer IDs and names, which transactions each payer
    answers (refreshed from the clearinghouse network), and the phone number for payer calls."""

    __tablename__ = "clearinghouse_payers"
    __table_args__ = (UniqueConstraint("workspace_id", "payer_id"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    payer_id: Mapped[str] = mapped_column(String(40))
    network_id: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(160))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    programs: Mapped[list[str]] = mapped_column(JSON, default=list)
    # {eligibility, claim_status, claims, era}: SUPPORTED | NOT_SUPPORTED | ENROLLMENT_REQUIRED
    support: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    phone: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(12), default="builtin")  # builtin | network
    refreshed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
