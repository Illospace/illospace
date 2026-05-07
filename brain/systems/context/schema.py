"""Typed schema validation for durable AgentRun context packs."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ContextSectionName(StrEnum):
    THREAD_SUMMARY = "thread_summary"
    HANDOFFS = "handoffs"
    USER_TEAM_FACTS = "user_team_facts"
    SELECTED_MEMORIES = "selected_memories"
    SELECTED_SKILLS = "selected_skills"
    POLICY_CONSTRAINTS = "policy_constraints"
    APPROVALS = "approvals"
    BUDGET = "budget"
    OUTPUT_CONTRACT = "output_contract"
    TOOL_PERMISSIONS = "tool_permissions"
    UNCERTAINTY = "uncertainty"


class ContextTokenBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimated_tokens: int = Field(ge=0)
    budget_tokens: int = Field(ge=0)
    remaining_tokens: int
    over_budget: bool


class ContextSection(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    title: str
    source: str
    included: bool
    content: Any
    token_budget: ContextTokenBudget
    notes: list[str] = Field(default_factory=list)

    @field_validator("name", "title", "source", mode="before")
    @classmethod
    def _stringify_required_text(cls, value: Any) -> str:
        return "" if value is None else str(value)


class OmittedContextSection(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    title: str | None = None
    source: str | None = None
    estimated_tokens: int = Field(default=0, ge=0)
    reason: str


class ContextPack(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(ge=1)
    compiler_version: str
    compiled_at: str | None = None
    run_id: int | None = None
    idea_id: str | None = None
    task: str
    render_order: list[str] = Field(default_factory=list)
    sections: dict[str, ContextSection]
    section_token_budget: dict[str, ContextTokenBudget]
    total_estimated_tokens: int = Field(ge=0)
    digest: str
    source_digest: str | None = None
    render_role: str | None = None
    omitted_sections: list[OmittedContextSection] = Field(default_factory=list)
    runtime: dict[str, Any] | None = None

    @field_validator("compiler_version", "task", "digest", mode="before")
    @classmethod
    def _stringify_core_text(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("idea_id", mode="before")
    @classmethod
    def _stringify_optional_idea_id(cls, value: Any) -> str | None:
        return None if value is None else str(value)

    @model_validator(mode="after")
    def _validate_section_integrity(self) -> "ContextPack":
        if not self.digest:
            raise ValueError("context pack digest must not be empty")
        missing_sections = [name for name in self.render_order if name not in self.sections]
        if missing_sections:
            raise ValueError(f"render_order references missing sections: {', '.join(missing_sections)}")
        missing_budgets = [name for name in self.render_order if name not in self.section_token_budget]
        if missing_budgets:
            raise ValueError(f"render_order references missing section budgets: {', '.join(missing_budgets)}")
        for key, section in self.sections.items():
            if section.name != key:
                raise ValueError(f"section key {key!r} does not match section.name {section.name!r}")
            budget = self.section_token_budget.get(key)
            if budget is not None and budget != section.token_budget:
                raise ValueError(f"section_token_budget for {key!r} does not match section token_budget")
        return self


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=False)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def validate_context_pack(context_pack: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(context_pack, Mapping):
        raise TypeError("context_pack must be a mapping")
    ContextPack.model_validate(context_pack)
    return _jsonable(dict(context_pack))


__all__ = [
    "ContextPack",
    "ContextSection",
    "ContextSectionName",
    "ContextTokenBudget",
    "OmittedContextSection",
    "ValidationError",
    "validate_context_pack",
]
