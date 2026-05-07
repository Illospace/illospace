"""Target registry and environment binding models."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base

__all__ = [
    "TargetRegistry",
    "EnvironmentBinding",
    "EnvironmentService",
    "EnvironmentCommand",
    "RunTargetBinding",
]


class TargetRegistry(Base):
    """A curated target record for a repo, app, service, or deploy surface."""

    __tablename__ = "target_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_team: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    repo_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    canonical_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    default_branch: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), default=dict)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), default=True)

    __table_args__ = (
        UniqueConstraint("target_kind", "slug", name="uq_target_registry_kind_slug"),
    )


class EnvironmentBinding(Base):
    """A deployment/runtime binding for a curated target."""

    __tablename__ = "environment_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_registry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("target_registry.id", ondelete="CASCADE"), nullable=False
    )
    env_name: Mapped[str] = mapped_column(String(80), nullable=False)
    branch_pattern: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    workspace_root: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deploy_target: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), default=dict)

    __table_args__ = (
        UniqueConstraint(
            "target_registry_id",
            "env_name",
            "org_id",
            name="uq_environment_bindings_registry_env_org",
        ),
    )


class EnvironmentCommand(Base):
    """A curated command for a specific environment binding."""

    __tablename__ = "environment_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("environment_bindings.id", ondelete="CASCADE"), nullable=False
    )
    command_name: Mapped[str] = mapped_column(String(120), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    cwd: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    purpose: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cost_class: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    safe_default: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), default=False)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), default=dict)

    __table_args__ = (
        UniqueConstraint("binding_id", "command_name", name="uq_environment_commands_binding_name"),
    )


class EnvironmentService(Base):
    """A service exposed by a curated environment binding."""

    __tablename__ = "environment_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("environment_bindings.id", ondelete="CASCADE"), nullable=False
    )
    service_name: Mapped[str] = mapped_column(String(120), nullable=False)
    service_type: Mapped[str] = mapped_column(String(80), nullable=False)
    base_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    healthcheck: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    test_command_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("environment_commands.id"), nullable=True
    )
    verify_contract: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        UniqueConstraint("binding_id", "service_name", name="uq_environment_services_binding_name"),
    )


class RunTargetBinding(Base):
    """Resolved target binding for a run."""

    __tablename__ = "run_target_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    raw_target_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )
    resolution_status: Mapped[str] = mapped_column(
        String(20), server_default="unknown", default="unknown", nullable=False
    )
    target_registry_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("target_registry.id"), nullable=True
    )
    environment_binding_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("environment_bindings.id"), nullable=True
    )
    resolved_workspace_root: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_branch: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_service_set: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    resolution_notes: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_run_target_bindings_run"),
    )
