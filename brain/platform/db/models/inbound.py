"""Inbound coordination models for external source signals."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin
from brain.platform.db.constraints import check_in_constraint
from brain.platform.status_contracts import INBOUND_EVENT_STATUS_VALUES

JSONVariant = JSONB().with_variant(JSON(), "sqlite")
UUIDString = UUID(as_uuid=False).with_variant(String, "sqlite")

__all__ = [
    "InboundDecisionReceiptRow",
    "InboundDomainProjectionRow",
    "InboundDomainProjectionKeyRow",
    "InboundEventRow",
    "InboundSourcePolicyRow",
]


class InboundSourcePolicyRow(Base, TimestampMixin):
    """Deterministic handling policy for one external source connection."""

    __tablename__ = "inbound_source_policies"
    __table_args__ = (
        Index(
            "ix_inbound_source_policies_connection_priority",
            "connection_id",
            "enabled",
            "priority",
        ),
        Index("ix_inbound_source_policies_org_enabled", "org_id", "enabled"),
    )

    id: Mapped[str] = mapped_column(UUIDString, primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("TRUE"),
        default=True,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"), default=100)
    origin_patterns: Mapped[list[Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )
    envelope_kinds: Mapped[list[Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'[\"signal\"]'::jsonb"),
        default=lambda: ["signal"],
    )
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_config: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    allowed_actions: Mapped[list[Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )
    auto_execute_actions: Mapped[list[Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )
    auto_execute_min_confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        server_default=text("0.85"),
        default=0.85,
    )
    review_mode: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        server_default="review_required",
        default="review_required",
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )


class InboundDomainProjectionRow(Base, TimestampMixin):
    """Configured deterministic projection from source payloads into Domains."""

    __tablename__ = "inbound_domain_projections"
    __table_args__ = (
        Index("ix_inbound_domain_projections_policy", "policy_id", "enabled"),
        Index("ix_inbound_domain_projections_domain", "domain_id", "object_key"),
    )

    id: Mapped[str] = mapped_column(UUIDString, primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    policy_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("inbound_source_policies.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    domain_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    object_key: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("TRUE"),
        default=True,
    )
    external_id_path: Mapped[str] = mapped_column(Text, nullable=False)
    external_id_field: Mapped[str] = mapped_column(String(80), nullable=False)
    field_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    title_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    upsert_mode: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        server_default="upsert",
        default="upsert",
    )
    validation_failure_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        server_default="review_required",
        default="review_required",
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )


class InboundDomainProjectionKeyRow(Base, TimestampMixin):
    """Unique external-id claim for a configured Domain Projection."""

    __tablename__ = "inbound_domain_projection_keys"
    __table_args__ = (
        UniqueConstraint(
            "projection_id",
            "external_id",
            name="uq_inbound_projection_keys_projection_external",
        ),
        Index("ix_inbound_projection_keys_org_projection", "org_id", "projection_id"),
        Index("ix_inbound_projection_keys_record", "record_id"),
    )

    id: Mapped[str] = mapped_column(UUIDString, primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    projection_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("inbound_domain_projections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    record_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("domain_records.id", ondelete="SET NULL"),
        nullable=True,
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)


class InboundEventRow(Base, CreatedAtMixin):
    """Raw and normalized external signal event with processing state."""

    __tablename__ = "inbound_events"
    __table_args__ = (
        CheckConstraint(
            check_in_constraint("status", INBOUND_EVENT_STATUS_VALUES),
            name="ck_inbound_events_status",
        ),
        UniqueConstraint("connection_id", "idempotency_key", name="uq_inbound_events_connection_idempotency"),
        Index("ix_inbound_events_connection_created", "connection_id", "created_at"),
        Index("ix_inbound_events_origin_created", "org_id", "origin", "created_at"),
        Index("ix_inbound_events_status_created", "org_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(UUIDString, primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_connection_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    policy_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("inbound_source_policies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    domain_projection_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("inbound_domain_projections.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, server_default="signal", default="signal")
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    envelope: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    ingress_context: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    source_actor: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    authority_user_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        server_default="received",
        default="received",
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    action_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    action_result: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InboundDecisionReceiptRow(Base, CreatedAtMixin):
    """Durable receipt for deterministic or Illo-handled inbound decisions."""

    __tablename__ = "inbound_decision_receipts"
    __table_args__ = (
        Index("ix_inbound_decision_receipts_event", "event_id", "created_at"),
        Index("ix_inbound_decision_receipts_org_created", "org_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(UUIDString, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("inbound_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    org_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[str] = mapped_column(
        UUIDString,
        ForeignKey("external_agent_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_id: Mapped[str | None] = mapped_column(
        UUIDString,
        ForeignKey("inbound_source_policies.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    outcome: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    target: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    tool_use: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    reasoning_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reusable_pattern_candidate: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
