"""Skill bundle registry models.

These tables separate portable skill package versions from tenant-local runtime
state. The existing ``skills`` table remains the fast runtime projection.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brain.platform.db.base import ArchivableMixin, Base, CreatedAtMixin, TimestampMixin

__all__ = [
    "SkillBundle",
    "SkillBundleVersion",
    "SkillAsset",
    "SkillInstallation",
    "SkillOverlay",
]


class SkillBundle(Base, TimestampMixin, ArchivableMixin):
    """Stable package identity for a portable skill bundle."""

    __tablename__ = "skill_bundles"
    __table_args__ = (
        UniqueConstraint("namespace", "name", name="uq_skill_bundles_namespace_name"),
        Index("ix_skill_bundles_trust_level", "trust_level"),
        Index("ix_skill_bundles_visibility", "visibility"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace: Mapped[str] = mapped_column(
        String(80), nullable=False, server_default="local", default="local"
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    owner_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="private_local", default="private_local"
    )
    source_kind: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="local", default="local"
    )
    trust_level: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="private_local", default="private_local"
    )
    latest_approved_version_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    versions: Mapped[list["SkillBundleVersion"]] = relationship(
        "SkillBundleVersion",
        back_populates="bundle",
        foreign_keys="SkillBundleVersion.bundle_id",
        lazy="noload",
    )
    installations: Mapped[list["SkillInstallation"]] = relationship(
        "SkillInstallation", back_populates="bundle", lazy="noload"
    )


class SkillBundleVersion(Base, CreatedAtMixin):
    """Immutable published or draft package version."""

    __tablename__ = "skill_bundle_versions"
    __table_args__ = (
        UniqueConstraint("bundle_id", "semver", name="uq_skill_bundle_versions_bundle_semver"),
        UniqueConstraint("bundle_id", "content_digest", name="uq_skill_bundle_versions_bundle_digest"),
        Index("ix_skill_bundle_versions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bundle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill_bundles.id", ondelete="CASCADE"), nullable=False
    )
    semver: Mapped[str] = mapped_column(String(40), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(96), nullable=False)
    manifest: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    asset_root: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    routing_card: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    permissions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    compatibility: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    eval_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="draft", default="draft"
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    bundle: Mapped["SkillBundle"] = relationship(
        "SkillBundle",
        back_populates="versions",
        foreign_keys=[bundle_id],
        lazy="noload",
    )
    assets: Mapped[list["SkillAsset"]] = relationship(
        "SkillAsset", back_populates="bundle_version", lazy="noload"
    )


class SkillAsset(Base, CreatedAtMixin):
    """Loadable asset within a bundle version."""

    __tablename__ = "skill_assets"
    __table_args__ = (
        UniqueConstraint("bundle_version_id", "path", name="uq_skill_assets_version_path"),
        Index("ix_skill_assets_bundle_version", "bundle_version_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bundle_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("skill_bundle_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    asset_kind: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="reference", default="reference"
    )
    mime_type: Mapped[str] = mapped_column(
        String(120), nullable=False, server_default="text/plain", default="text/plain"
    )
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_digest: Mapped[str] = mapped_column(String(96), nullable=False)
    storage_kind: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="inline", default="inline"
    )
    storage_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    loading_budget_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    bundle_version: Mapped["SkillBundleVersion"] = relationship(
        "SkillBundleVersion", back_populates="assets", lazy="noload"
    )


class SkillInstallation(Base, TimestampMixin, ArchivableMixin):
    """Tenant/user scoped installed view of a bundle version."""

    __tablename__ = "skill_installations"
    __table_args__ = (
        Index(
            "uq_skill_installations_active_scope",
            "bundle_id",
            "org_id",
            "user_id",
            "enabled_scope",
            unique=True,
        ),
        Index("ix_skill_installations_org_user", "org_id", "user_id"),
        Index("ix_skill_installations_review_status", "review_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bundle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill_bundles.id", ondelete="CASCADE"), nullable=False
    )
    bundle_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill_bundle_versions.id"), nullable=False
    )
    skill_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("skills.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    installed_by_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    enabled_scope: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="user", default="user"
    )
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE"), default=True
    )
    update_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="manual", default="manual"
    )
    installed_digest: Mapped[str] = mapped_column(String(96), nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="approved", default="approved"
    )
    permission_grants: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    disabled_sections: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    loading_budgets: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    rollback_bundle_version_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("skill_bundle_versions.id"), nullable=True
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    bundle: Mapped["SkillBundle"] = relationship(
        "SkillBundle", back_populates="installations", lazy="noload"
    )
    bundle_version: Mapped["SkillBundleVersion"] = relationship(
        "SkillBundleVersion",
        foreign_keys=[bundle_version_id],
        lazy="noload",
    )
    overlays: Mapped[list["SkillOverlay"]] = relationship(
        "SkillOverlay", back_populates="installation", lazy="noload"
    )


class SkillOverlay(Base, TimestampMixin):
    """Local patch over an installed bundle version."""

    __tablename__ = "skill_overlays"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "overlay_revision",
            name="uq_skill_overlays_installation_revision",
        ),
        Index("ix_skill_overlays_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill_installations.id", ondelete="CASCADE"), nullable=False
    )
    base_bundle_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill_bundle_versions.id"), nullable=False
    )
    overlay_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="draft", default="draft"
    )
    patch: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    overlay_digest: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    effective_digest: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    author_user_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    promoted_bundle_version_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("skill_bundle_versions.id"), nullable=True
    )

    installation: Mapped["SkillInstallation"] = relationship(
        "SkillInstallation", back_populates="overlays", lazy="noload"
    )
