"""Agent session and API call models.

Matches: agent_sessions, agent_api_calls tables.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, TimestampMixin, CreatedAtMixin

__all__ = ["AgentSession", "AgentApiCall"]


class AgentSession(Base, TimestampMixin):
    """A persistent agent conversation session."""

    __tablename__ = "agent_sessions"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    messages: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'"), default=list
    )
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    handoff_summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    handoff_message_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), default=0
    )
    handoff_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_input_tokens: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), default=0
    )
    total_output_tokens: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), default=0
    )
    total_cache_read: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), default=0
    )
    total_cache_creation: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), default=0
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )


class AgentApiCall(Base, CreatedAtMixin):
    """A single API call made during an agent session."""

    __tablename__ = "agent_api_calls"
    __table_args__ = (
        Index("ix_agent_api_calls_trace_id", "trace_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_input: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens_output: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_read: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_write: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    context_messages: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    system_prompt_chars: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stop_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
