"""Prior authorization tables. Each carries ``workspace_id`` so the tenant wall scopes it."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class PriorAuthRule(IdMixin, Base):
    """A payer rule: this service needs prior authorization with this payer (or with any payer).

    Matches an appointment by its EHR appointment type, and names the service it stands for (CPT,
    description, usual diagnoses) so a case opened from the schedule carries the codes the payer
    needs. A 271 "auth required" flag opens a case without a rule; a rule fills in the codes.
    """

    __tablename__ = "prior_auth_rules"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    payer: Mapped[str] = mapped_column(String(120), default="*")  # payer name or payer id; * = any
    appointment_type_ref: Mapped[str | None] = mapped_column(String(40))  # EHR appointment type id
    appointment_type_name: Mapped[str | None] = mapped_column(String(120))  # or match by name
    cpt_code: Mapped[str] = mapped_column(String(10))
    service_description: Mapped[str] = mapped_column(String(160))
    icd_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    lead_days: Mapped[int] = mapped_column(default=14)  # start this long before the visit
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "payer": self.payer,
            "appointment_type_ref": self.appointment_type_ref,
            "appointment_type_name": self.appointment_type_name,
            "cpt_code": self.cpt_code,
            "service_description": self.service_description,
            "icd_codes": list(self.icd_codes or []),
            "lead_days": self.lead_days,
            "enabled": self.enabled,
            "note": self.note,
        }
