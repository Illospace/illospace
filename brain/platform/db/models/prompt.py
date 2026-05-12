"""Prompt template and brain prompt models.

Matches: prompt_templates, prompt_template_outcomes, brain_prompts tables.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin

__all__ = ["PromptTemplate", "PromptTemplateOutcome", "BrainPrompt"]


class PromptTemplate(Base):
    """A versioned prompt template with quality tracking."""

    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
    avg_quality_score: Mapped[float] = mapped_column(
        Float, server_default=text("0.0"), default=0.0
    )
    use_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    last_used: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW()")
    )

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_prompt_templates_name_version"),
    )


class PromptTemplateOutcome(Base, CreatedAtMixin):
    """Quality outcome for a prompt template usage."""

    __tablename__ = "prompt_template_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_name: Mapped[str] = mapped_column(Text, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)


class BrainPrompt(Base, CreatedAtMixin):
    """A brain-generated prompt/nudge."""

    __tablename__ = "brain_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"), default=dict
    )
    dismissed_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
