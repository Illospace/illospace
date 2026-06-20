"""Pydantic schemas for Skill serialization and validation."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.systems.skills.bundles import (
    SkillBundleAssetType,
    SkillBundleInstallStatus,
    SkillBundleManifest,
    SkillBundlePermissionGrantSpec,
    SkillBundlePermissionSpec,
    SkillBundleReviewStatus,
    SkillBundleRoutingSpec,
    SkillBundleRuntimeSpec,
    SkillBundleSourceKind,
    SkillBundleTrustLevel,
    SkillBundleUpdatePolicy,
)


_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}


class SkillExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    task_description: str | None = None
    outcome: str | None = None
    duration_sec: float | None = None
    started_at: datetime | None = None
    rework_rounds: int = 0
    flagged: bool = False


class SkillDepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str | None = None
    rel: str | None = None
    order: int | None = None


class SkillRead(BaseModel):
    """Full skill for dashboard display."""
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)
    id: int
    name: str
    description: str | None = None
    procedure: str | None = None
    version: int = 1
    skill_type: str = "skill"
    maturity: str = "emerging"
    confidence: float = 0.3
    use_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    partial_count: int = 0
    avg_duration_sec: float | None = None
    last_used: datetime | None = None
    pitfalls: list = []
    refinements: list = []
    triggers: list = []
    guardrails: list = []
    auto_emerged: bool = False
    thinking_tier: str = "medium"
    success_rate: float = 0.0
    children: list[SkillDepRead] = []
    executions: list[SkillExecutionRead] = []
    skill_installation_id: int | None = None
    bundle_version_id: int | None = None
    bundle_digest: str | None = None
    overlay_revision: int | None = None
    effective_digest: str | None = None
    source_kind: SkillBundleSourceKind = SkillBundleSourceKind.LEGACY_DB
    trust_level: SkillBundleTrustLevel = SkillBundleTrustLevel.PRIVATE_LOCAL


class SkillCreate(BaseModel):
    """Input validation for skill creation."""
    name: str
    description: str = ""
    procedure: str
    thinking_tier: str = "medium"
    pitfalls: list = Field(default_factory=list)
    refinements: list = Field(default_factory=list)
    triggers: list = Field(default_factory=list)
    guardrails: list = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v.strip()

    @field_validator("thinking_tier")
    @classmethod
    def valid_thinking_tier(cls, v: str) -> str:
        if v not in ("high", "medium", "low", "none", "xhigh"):
            raise ValueError(f"Invalid thinking_tier: {v}")
        return v


class SkillUpdate(BaseModel):
    """Input validation for skill updates. All fields optional."""
    name: str | None = None
    description: str | None = None
    procedure: str | None = None
    thinking_tier: str | None = None
    pitfalls: list | None = None
    refinements: list | None = None
    triggers: list | None = None
    guardrails: list | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("name must not be empty")
        return v.strip() if v else v

    @field_validator("thinking_tier")
    @classmethod
    def valid_thinking_tier(cls, v: str | None) -> str | None:
        if v is not None and v not in ("high", "medium", "low", "none", "xhigh"):
            raise ValueError(f"Invalid thinking_tier: {v}")
        return v


class SkillExport(BaseModel):
    """Portable format for import/export."""
    model_config = ConfigDict(from_attributes=True)
    name: str
    description: str | None = None
    procedure: str
    version: int = 1
    level: str = "cognitive"
    maturity: str = "emerging"
    confidence: float = 0.3
    pitfalls: list = []
    refinements: list = []
    triggers: list = []
    guardrails: list = []
    thinking_tier: str = "medium"
    auto_emerged: bool = False
    builtin: bool = False


class SkillAssetRead(BaseModel):
    """Metadata for a loadable asset in a portable skill package."""
    id: int | None = None
    path: str
    asset_kind: str = "reference"
    mime_type: str = "text/plain"
    size_bytes: int | None = None
    content_digest: str | None = None
    storage_kind: str = "inline"
    storage_uri: str | None = None
    loading_budget_tokens: int | None = None
    has_inline_content: bool = False


class SkillAssetContentRead(SkillAssetRead):
    """A specific asset loaded through the Level 2 progressive loading surface."""
    content: str | None = None
    truncated: bool = False


class SkillPackageRead(BaseModel):
    """Portable package/install metadata behind a runtime skill projection."""
    package_kind: str = "legacy_db"
    is_bundle_backed: bool = False
    namespace: str | None = None
    package_name: str | None = None
    display_name: str | None = None
    description: str | None = None
    source_kind: str = "legacy_db"
    trust_level: str = "private_local"
    visibility: str | None = None
    bundle_id: int | None = None
    bundle_version_id: int | None = None
    semver: str | None = None
    bundle_digest: str | None = None
    effective_digest: str | None = None
    overlay_revision: int | None = None
    installation_id: int | None = None
    enabled: bool | None = None
    enabled_scope: str | None = None
    pinned: bool | None = None
    update_policy: str | None = None
    review_status: str | None = None
    rollback_bundle_version_id: int | None = None
    asset_count: int = 0
    asset_counts: dict[str, int] = Field(default_factory=dict)
    assets: list[SkillAssetRead] = Field(default_factory=list)
    permissions: dict = Field(default_factory=dict)
    routing_card: dict = Field(default_factory=dict)
    compatibility: dict = Field(default_factory=dict)
    eval_summary: dict = Field(default_factory=dict)
    manifest: dict = Field(default_factory=dict)


class SkillProgressiveLoadingRead(BaseModel):
    """How the agent should progressively load this skill."""
    level0: str = "catalog"
    level1_tool: str = "skill_view"
    level2_tool: str = "skill_asset"
    loaded_sections: list[str] = Field(default_factory=lambda: ["catalog"])
    available_sections: list[str] = Field(default_factory=list)
    available_assets: list[str] = Field(default_factory=list)
    load_tools: dict = Field(default_factory=dict)


class SkillEnhancedRead(BaseModel):
    """Skill management view that joins runtime projection and package state."""
    skill: SkillRead
    package: SkillPackageRead
    progressive_loading: SkillProgressiveLoadingRead
    needs_attention: bool = False
    editable: bool = True
    convert_to_bundle_available: bool = False
