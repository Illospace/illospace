from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_serializer, model_validator

from brain.systems.memory.truth_maintenance import (
    build_truth_state,
    normalize_contradiction_data,
    normalize_memory_truth_data,
    normalize_review_data,
)


def _raw_model_data(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    return {}


class MemoryRead(BaseModel):
    id: int
    content: str
    memory_type: str
    memory_tier: str = "episodic"
    consolidated: bool = False
    archived: bool = False
    superseded_by: int | None = None
    salience: float
    source: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    truth_status: str = "unknown"
    review_status: str = "unreviewed"
    confidence: float = 0.5
    freshness_score: float = 0.5
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    policy_kind: str | None = None
    policy_scope: str | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | UUID | None = None
    demoted_at: datetime | None = None
    demotion_reason: str | None = None
    open_contradiction_count: int = 0
    resolved_contradiction_count: int = 0
    contradiction_status: str = "none"
    has_open_contradiction: bool = False
    is_reviewed_active: bool = False
    is_policy_effective: bool = False
    tags: list[str] | None = None
    access_count: int
    last_accessed: datetime | None = None
    created_at: datetime
    scope: str
    visibility: str
    user_id: str | UUID | None = None
    org_id: str | UUID | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: object) -> dict:
        data = _raw_model_data(value)
        data.update(normalize_memory_truth_data(value))
        data.setdefault("archived", False)
        data.setdefault("consolidated", False)
        data.setdefault("superseded_by", None)
        data.setdefault("memory_tier", "episodic")
        data.setdefault("truth_status", "unknown")
        data.setdefault("review_status", "unreviewed")
        data.setdefault("confidence", 0.5)
        data.setdefault("freshness_score", 0.5)
        data.setdefault("open_contradiction_count", 0)
        data.setdefault("resolved_contradiction_count", 0)
        data.setdefault("contradiction_status", "none")
        data.setdefault("has_open_contradiction", False)
        data.setdefault("is_reviewed_active", False)
        data.setdefault("is_policy_effective", False)
        return data

    @field_serializer("user_id", "org_id", "reviewed_by")
    @classmethod
    def serialize_uuid(cls, v: object) -> str | None:
        return str(v) if v is not None else None


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1)
    memory_type: str = "fact"
    tags: list[str] | None = None


class MemoryUpdate(BaseModel):
    content: str | None = None
    tags: list[str] | None = None
    scope: str | None = None
    visibility: str | None = None


class MemoryPromote(BaseModel):
    visibility: str


class MemoryTruthReviewRequest(BaseModel):
    action: Literal["promote", "demote", "quarantine", "review"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: dict = Field(default_factory=dict)
    rationale: str = Field(min_length=3, max_length=1000)
    to_tier: str | None = Field(default=None, max_length=20)
    truth_status: str | None = Field(default=None, max_length=20)
    valid_until: datetime | None = None


class EdgeRead(BaseModel):
    id: int
    source_id: int
    target_id: int
    relationship: str
    weight: float
    model_config = {"from_attributes": True}


class SimilarityEdge(BaseModel):
    source_id: int
    target_id: int
    similarity: float


class TruthStateRead(BaseModel):
    memory_tier: str = "episodic"
    truth_status: str = "unknown"
    review_status: str = "unreviewed"
    confidence: float = 0.5
    freshness_score: float = 0.5
    source_type: str | None = None
    source_ref: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    policy_kind: str | None = None
    policy_scope: str | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | UUID | None = None
    demoted_at: datetime | None = None
    demotion_reason: str | None = None
    is_active: bool = True
    is_quarantined: bool = False
    is_expired: bool = False
    is_superseded: bool = False
    is_archived: bool = False
    open_contradiction_count: int = 0
    resolved_contradiction_count: int = 0
    contradiction_status: str = "none"
    has_open_contradiction: bool = False
    is_reviewed_active: bool = False
    is_policy_effective: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: object) -> dict:
        data = _raw_model_data(value)
        data.update(build_truth_state(value))
        return data

    @field_serializer("reviewed_by")
    @classmethod
    def serialize_uuid(cls, v: object) -> str | None:
        return str(v) if v is not None else None


class MemoryContradictionRead(BaseModel):
    id: int
    left_memory_id: int
    right_memory_id: int
    detected_by: str | None = None
    contradiction_type: str
    evidence: dict = Field(default_factory=dict)
    severity: float = 0.5
    status: str = "open"
    resolution_memory_id: int | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | UUID | None = None
    is_open: bool = True
    is_resolved: bool = False

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: object) -> dict:
        return normalize_contradiction_data(value)

    @field_serializer("resolved_by")
    @classmethod
    def serialize_uuid(cls, v: object) -> str | None:
        return str(v) if v is not None else None


class MemoryReviewRead(BaseModel):
    id: int
    memory_id: int
    action: str
    from_tier: str
    to_tier: str
    reviewer_id: str | UUID | None = None
    rationale: str | None = None
    evidence: dict = Field(default_factory=dict)
    created_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: object) -> dict:
        return normalize_review_data(value)

    @field_serializer("reviewer_id")
    @classmethod
    def serialize_uuid(cls, v: object) -> str | None:
        return str(v) if v is not None else None


class MemoryTruthSnapshot(BaseModel):
    memory: MemoryRead
    state: TruthStateRead
    contradictions: list[MemoryContradictionRead] = Field(default_factory=list)
    reviews: list[MemoryReviewRead] = Field(default_factory=list)
    conservative_filter_enabled: bool = False


class GraphResponse(BaseModel):
    nodes: list[MemoryRead]
    edges: list[EdgeRead]


class SimilarityGraphResponse(BaseModel):
    nodes: list[MemoryRead]
    edges: list[EdgeRead]
    similarity_edges: list[SimilarityEdge]
