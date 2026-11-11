"""Persona library table. Only the personas package reads or writes it."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from careplix_api.db import Base, IdMixin, utcnow


class Persona(IdMixin, Base):
    """A reusable persona in a workspace's library: the spoken name, role, voice and greeting, and
    the style the agent keeps. Applying one copies it into an agent's draft config, so published
    versions stay immutable and a changed persona goes through the suite like any other edit."""

    __tablename__ = "personas"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))  # library label, e.g. "Warm patient coordinator"
    description: Mapped[str | None] = mapped_column(String(240))
    agent_name: Mapped[str] = mapped_column(String(60))  # what the agent calls itself
    role: Mapped[str] = mapped_column(String(120))
    voice: Mapped[str] = mapped_column(String(30))
    greeting: Mapped[str] = mapped_column(Text)
    warmth: Mapped[str] = mapped_column(String(10), default="neutral")
    formality: Mapped[str] = mapped_column(String(10), default="neutral")
    pace: Mapped[str] = mapped_column(String(10), default="normal")
    empathy: Mapped[str] = mapped_column(String(10), default="standard")
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    avoid: Mapped[list[str]] = mapped_column(JSON, default=list)
    signature_phrases: Mapped[list[str]] = mapped_column(JSON, default=list)
    character: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
