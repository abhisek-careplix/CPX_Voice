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
    # --- claims we build and submit (RC3–RC8); None on claims we only follow -----------------
    # draft | ready | blocked | submitted | accepted | rejected | paid | partially_paid | denied
    # | void. ``submitted_on`` stays "" until the clearinghouse takes the claim.
    stage: Mapped[str | None] = mapped_column(String(16), index=True)
    place_of_service: Mapped[str | None] = mapped_column(String(2))
    diagnoses: Mapped[list[str] | None] = mapped_column(JSON)  # ICD-10-CM, no dots, first = primary
    # [{line_id, cpt, modifiers, units, charge, dx (1-based pointers), date_of_service, charge_id}]
    lines: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    frequency: Mapped[str | None] = mapped_column(String(12))  # original | replacement
    replaces_claim_id: Mapped[str | None] = mapped_column(String(36))
    # Test mode only: which remittance the clearinghouse's test payer sends (paid | partial |
    # denied); it picks the published member-ID prefix.
    test_outcome: Mapped[str | None] = mapped_column(String(10))
    scrub: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # {clean, findings, checked_at}
    submit_mode: Mapped[str | None] = mapped_column(String(12))  # test | production
    submission_id: Mapped[str | None] = mapped_column(String(60))
    clearinghouse_claim_id: Mapped[str | None] = mapped_column(String(60))
    submitted_at: Mapped[datetime | None] = mapped_column()
    ack_status: Mapped[str | None] = mapped_column(String(12))  # accepted | rejected
    ack_at: Mapped[datetime | None] = mapped_column()
    paid_total: Mapped[str | None] = mapped_column(String(16))
    patient_responsibility: Mapped[str | None] = mapped_column(String(16))
    era_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ClaimCharge(IdMixin, Base):
    """One billed service from the EHR (or a synthetic one): the raw material of a claim (RC3).

    NexHealth charges carry the procedure code, fee, date and provider but no diagnoses, units or
    modifiers: those are added before the claim is scrubbed, and the scrub refuses a claim without
    them."""

    __tablename__ = "claim_charges"
    __table_args__ = (UniqueConstraint("workspace_id", "source", "source_ref"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source: Mapped[str] = mapped_column(String(12))  # sample | ehr
    source_ref: Mapped[str] = mapped_column(String(60))
    ehr_connection_id: Mapped[str | None] = mapped_column(String(36))
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    patient_ref: Mapped[str | None] = mapped_column(String(40), index=True)
    # Sealed {display_name, first_name, last_name, date_of_birth, member_id, gender, address}.
    patient: Mapped[str | None] = mapped_column(Text)
    payer_id: Mapped[str | None] = mapped_column(String(40))
    payer_name: Mapped[str | None] = mapped_column(String(160))
    plan_kind: Mapped[str | None] = mapped_column(String(24))
    provider_ref: Mapped[str | None] = mapped_column(String(40))
    provider_name: Mapped[str | None] = mapped_column(String(160))
    provider_npi: Mapped[str | None] = mapped_column(String(10))
    date_of_service: Mapped[str] = mapped_column(String(10))
    cpt_code: Mapped[str] = mapped_column(String(10))
    description: Mapped[str | None] = mapped_column(String(160))
    modifiers: Mapped[list[str]] = mapped_column(JSON, default=list)
    units: Mapped[str] = mapped_column(String(8), default="1")
    amount: Mapped[str] = mapped_column(String(16))
    diagnoses: Mapped[list[str]] = mapped_column(JSON, default=list)
    place_of_service: Mapped[str | None] = mapped_column(String(2))
    test_outcome: Mapped[str | None] = mapped_column(String(10))  # synthetic visits only
    claim_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BillingProfile(IdMixin, Base):
    """Who bills: the billing provider, tax ID and submitter on every claim (one per workspace).

    Test claims to the clearinghouse's test payer must use the NPI and tax ID the provider was
    enrolled with for remittances (docs/solution/RCM_TEST_MODE_WALKTHROUGH.md)."""

    __tablename__ = "billing_profiles"
    __table_args__ = (UniqueConstraint("workspace_id"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    organization: Mapped[str] = mapped_column(String(60))
    npi: Mapped[str] = mapped_column(String(10))
    tax_id: Mapped[str] = mapped_column(String(9))  # EIN, digits only
    taxonomy: Mapped[str | None] = mapped_column(String(10))
    address1: Mapped[str] = mapped_column(String(55))
    city: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(2))
    postal_code: Mapped[str] = mapped_column(String(9))
    submitter_name: Mapped[str] = mapped_column(String(60))
    contact_phone: Mapped[str] = mapped_column(String(10))
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
