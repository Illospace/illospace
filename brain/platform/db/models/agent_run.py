"""Agent-run persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin
from brain.platform.db.constraints import check_in_constraint
from brain.contracts.statuses import AGENT_RUN_DB_STATUS_VALUES

__all__ = [
    "ActionManifestRow",
    "AgentRunArtifactRow",
    "AgentRunEventRow",
    "AgentRunRow",
]


class AgentRunRow(Base, TimestampMixin):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            check_in_constraint("status", AGENT_RUN_DB_STATUS_VALUES),
            name="ck_agent_runs_status",
        ),
        Index("ix_agent_runs_org_created", "org_id", "created_at"),
        Index("ix_agent_runs_thread_created", "thread_id", "created_at"),
        Index("ix_agent_runs_status_created", "status", "created_at"),
        Index("ix_agent_runs_root_run_id", "root_run_id"),
        Index("ix_agent_runs_parent_run_id", "parent_run_id"),
        Index("ix_agent_runs_parent_created", "parent_run_id", "created_at", "id"),
        Index("ix_agent_runs_trace_id", "trace_id"),
        UniqueConstraint(
            "org_id",
            "source_idempotency_scope",
            "source_idempotency_key",
            name="uq_agent_runs_org_source_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    org_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False).with_variant(String, "sqlite"),
        ForeignKey("orgs.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False).with_variant(String, "sqlite"),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    parent_run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    root_run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    recipe: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=sql_text("'queued'"), default="queued")
    input_message: Mapped[str] = mapped_column(Text, nullable=False)
    target_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"), default=dict)
    workspace_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"), default=dict)
    model_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"), default=dict)
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"), default=dict)
    source_idempotency_scope: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def adaptations(self) -> list[dict[str, Any]]:
        metadata = self.metadata_ if isinstance(self.metadata_, dict) else {}
        value = metadata.get("adaptations")
        return list(value) if isinstance(value, list) else []

    @adaptations.setter
    def adaptations(self, value: list[dict[str, Any]]) -> None:
        metadata = dict(self.metadata_ or {})
        metadata["adaptations"] = list(value or [])
        self.metadata_ = metadata


class ActionManifestRow(Base, CreatedAtMixin):
    """Run-owned audit row for a side-effecting tool action."""

    __tablename__ = "action_manifests"
    __table_args__ = (
        Index("ix_action_manifests_run_id", "run_id"),
        Index("ix_action_manifests_trace_id", "trace_id"),
        Index("ix_action_manifests_org_id", "org_id"),
        Index("ix_action_manifests_tool_created_at", "tool_name", "created_at"),
        Index("ix_action_manifests_idempotency_key", "idempotency_key"),
        Index("ix_action_manifests_policy_result", "policy_result"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'agent'"), default="agent"
    )
    org_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False).with_variant(String, "sqlite"),
        ForeignKey("orgs.id", ondelete="SET NULL"),
        nullable=True,
    )
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"), default=dict
    )
    risk: Mapped[str] = mapped_column(Text, nullable=False)
    reversibility: Mapped[str] = mapped_column(Text, nullable=False)
    expected_effect: Mapped[str] = mapped_column(Text, nullable=False)
    approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("FALSE"), default=False
    )
    approval_requirement: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=sql_text("'not_required_permissive_audit'"),
        default="not_required_permissive_audit",
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_result: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=sql_text("'allowed_audit_only'"),
        default="allowed_audit_only",
    )
    policy_mode: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=sql_text("'permissive_audit'"),
        default="permissive_audit",
    )
    outcome_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'started'"), default="started"
    )
    outcome_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sql_text("'{}'::jsonb"),
        default=dict,
    )

    @classmethod
    def from_create(cls, manifest: Any) -> "ActionManifestRow":
        values = manifest.to_db_values() if hasattr(manifest, "to_db_values") else dict(manifest)
        return cls(**values)


class AgentRunEventRow(Base):
    __tablename__ = "agent_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_agent_run_events_run_sequence"),
        Index("ix_agent_run_events_root_sequence", "root_run_id", "sequence_no"),
        Index("ix_agent_run_events_type_created", "event_type", "created_at"),
        Index("ix_agent_run_events_run_created", "run_id", "created_at"),
        Index("ix_agent_run_events_run_type_sequence", "run_id", "event_type", "sequence_no", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    root_run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"), default=dict)
    producer: Mapped[str] = mapped_column(String(120), nullable=False, server_default=sql_text("'agent_runtime'"), default="agent_runtime")
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, server_default=sql_text("'public'"), default="public")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("NOW()")
    )


class AgentRunArtifactRow(Base):
    __tablename__ = "agent_run_artifacts"
    __table_args__ = (
        Index("ix_agent_run_artifacts_run_type_created", "run_id", "artifact_type", "created_at"),
        Index("ix_agent_run_artifacts_root_created", "root_run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    root_run_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"), default=dict)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, server_default=sql_text("'public'"), default="public")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("NOW()")
    )
