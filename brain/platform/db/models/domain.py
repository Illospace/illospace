"""Org-wide custom domain models.

Domains are user-defined operational data spaces: object definitions, typed
records, record relations, and append-only change events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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

from brain.platform.db.base import Base, CreatedAtMixin, TimestampMixin
from brain.platform.db.models.agent_run import ActionManifestRow as _ActionManifestRow  # noqa: F401

__all__ = [
    "Domain",
    "DomainObjectType",
    "DomainFieldDefinition",
    "DomainRelationType",
    "DomainRecord",
    "DomainRelation",
    "DomainEvent",
]


class Domain(Base, TimestampMixin):
    """An org-wide custom operational data space."""

    __tablename__ = "domains"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_domains_org_slug"),
        Index("ix_domains_org_archived", "org_id", "archived_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class DomainObjectType(Base, TimestampMixin):
    """A type of record inside a domain."""

    __tablename__ = "domain_object_types"
    __table_args__ = (
        UniqueConstraint("domain_id", "key", name="uq_domain_object_types_domain_key"),
        Index("ix_domain_object_types_domain", "domain_id", "archived_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title_field: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class DomainFieldDefinition(Base, TimestampMixin):
    """A typed field declared on one domain object type."""

    __tablename__ = "domain_field_definitions"
    __table_args__ = (
        UniqueConstraint("object_type_id", "key", name="uq_domain_field_defs_object_key"),
        Index("ix_domain_field_defs_domain_object", "domain_id", "object_type_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    object_type_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("domain_object_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    field_type: Mapped[str] = mapped_column(String(30), nullable=False)
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE"), default=False
    )
    options: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    default_value: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    validation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    searchable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    sortable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class DomainRelationType(Base, TimestampMixin):
    """A typed edge between two domain object types."""

    __tablename__ = "domain_relation_types"
    __table_args__ = (
        UniqueConstraint("domain_id", "key", name="uq_domain_relation_types_domain_key"),
        Index("ix_domain_relation_types_domain", "domain_id", "archived_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_object_type_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("domain_object_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_object_type_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("domain_object_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cardinality: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="many_to_many", default="many_to_many"
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class DomainRecord(Base, TimestampMixin):
    """One user-created record in a domain object type."""

    __tablename__ = "domain_records"
    __table_args__ = (
        Index("ix_domain_records_domain_object", "domain_id", "object_type_id"),
        Index("ix_domain_records_org_archived", "org_id", "archived_at"),
        Index("ix_domain_records_search_text", "search_text"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    object_type_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("domain_object_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    search_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class DomainRelation(Base, TimestampMixin):
    """One typed relation between two domain records."""

    __tablename__ = "domain_relations"
    __table_args__ = (
        Index("ix_domain_relations_domain_type", "domain_id", "relation_type_id"),
        Index("ix_domain_relations_source", "source_record_id"),
        Index("ix_domain_relations_target", "target_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("domain_relation_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_record_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("domain_records.id", ondelete="CASCADE"), nullable=False
    )
    target_record_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("domain_records.id", ondelete="CASCADE"), nullable=False
    )
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class DomainEvent(Base, CreatedAtMixin):
    """Append-only event for domain schema and data changes."""

    __tablename__ = "domain_events"
    __table_args__ = (
        Index("ix_domain_events_domain_created", "domain_id", "created_at"),
        Index("ix_domain_events_record_created", "record_id", "created_at"),
        Index("ix_domain_events_org_created", "org_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    domain_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("domains.id", ondelete="CASCADE"), nullable=False, index=True
    )
    record_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("domain_records.id", ondelete="SET NULL"), nullable=True
    )
    relation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("domain_relations.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_kind: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="human", default="human"
    )
    actor_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    idea_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ideas.id", ondelete="SET NULL"), nullable=True
    )
    action_manifest_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("action_manifests.id", ondelete="SET NULL"), nullable=True
    )
    before: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    after: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    patch: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
