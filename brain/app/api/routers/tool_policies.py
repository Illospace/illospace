"""Member-readable tool policies. Only human administrators can write them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import has_role, require_org_context
from brain.app.api.deps import get_db, rate_limit
from brain.platform.db.repositories.skills import SkillRepository
from brain.systems.runs.tool_catalog.registry import all_tool_registrations
from brain.systems.runs.tool_policy_store import (
    MAX_DISABLED_TOOLS,
    ToolPolicyConflict,
    apply_tool_policy_change,
    list_tool_policy_history,
    read_tool_policy,
    validate_disabled_tools,
)

router = APIRouter(
    prefix="/api/tool-policies",
    tags=["tool-policies"],
    dependencies=[Depends(rate_limit)],
)


class ToolPolicyUpdate(BaseModel):
    disabled_tools: list[str] = Field(max_length=MAX_DISABLED_TOOLS)
    expected_version: int = Field(ge=0, strict=True)

    @field_validator("disabled_tools")
    @classmethod
    def validate_names(cls, value):
        return validate_disabled_tools(value)


class ToolCatalogEntry(BaseModel):
    name: str
    description: str


class ToolPolicyWarning(BaseModel):
    name: str
    message: str


class ToolPolicyRead(BaseModel):
    disabled_tools: list[str]
    version: int
    updated_at: datetime | None
    updated_by: str | None
    warnings: list[ToolPolicyWarning]
    tools: list[ToolCatalogEntry]


class ToolPolicyAuditRead(BaseModel):
    id: int
    scope_type: str
    scope_key: str
    version: int
    actor_id: str
    actor_type: str
    before_disabled_tools: list[str]
    after_disabled_tools: list[str]
    changed_at: datetime


def tool_catalog() -> list[ToolCatalogEntry]:
    # The registry owns every model-visible built-in and workspace tool.
    # External tool names remain valid even before their connection exists.
    return [
        ToolCatalogEntry(
            name=name, description=registration.description.split("\n", 1)[0][:240]
        )
        for name, registration in sorted(all_tool_registrations().items())
    ]


def _response(policy) -> ToolPolicyRead:
    catalog = tool_catalog()
    known = {tool.name for tool in catalog}
    disabled = sorted(policy.disabled_tools) if policy else []
    return ToolPolicyRead(
        disabled_tools=disabled,
        version=policy.version if policy else 0,
        updated_at=policy.updated_at if policy else None,
        updated_by=policy.updated_by if policy else None,
        tools=catalog,
        warnings=[
            ToolPolicyWarning(
                name=name,
                message="Unknown tool. Illo saves this name and blocks the tool if it connects later.",
            )
            for name in disabled
            if name not in known
        ],
    )


def _human_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("principal_type") != "human" or not has_role(user, "owner", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Only a human organization admin can change this policy.",
        )
    require_org_context(user)
    return user


async def _visible_skill(db, user, skill_name):
    skill = await SkillRepository(db).a_get_visible(
        org_id=require_org_context(user),
        user_id=str(user["id"]),
        name=skill_name,
    )
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")


async def _read(db, user, scope_type, scope_key=""):
    return _response(
        await read_tool_policy(
            db,
            org_id=require_org_context(user),
            scope_type=scope_type,
            scope_key=scope_key,
        )
    )


async def _put(db, user, payload, scope_type, scope_key=""):
    try:
        policy = await apply_tool_policy_change(
            db,
            org_id=require_org_context(user),
            scope_type=scope_type,
            scope_key=scope_key,
            disabled_tools=payload.disabled_tools,
            expected_version=payload.expected_version,
            actor_id=str(user["id"]),
            actor_type="human",
        )
    except ToolPolicyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _response(policy)


async def _history(db, user, scope_type, scope_key="", limit=100):
    rows = await list_tool_policy_history(
        db,
        org_id=require_org_context(user),
        scope_type=scope_type,
        scope_key=scope_key,
        limit=limit,
    )
    return [
        ToolPolicyAuditRead.model_validate(row, from_attributes=True) for row in rows
    ]


@router.get("/organization", response_model=ToolPolicyRead)
async def read_organization_policy(
    user: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return await _read(db, user, "organization")


@router.put("/organization", response_model=ToolPolicyRead)
async def put_organization_policy(
    payload: ToolPolicyUpdate,
    user: dict = Depends(_human_admin),
    db: AsyncSession = Depends(get_db),
):
    return await _put(db, user, payload, "organization")


@router.get("/organization/history", response_model=list[ToolPolicyAuditRead])
async def organization_policy_history(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
):
    return await _history(db, user, "organization", limit=limit)


@router.get("/skills/{skill_name}", response_model=ToolPolicyRead)
async def read_skill_policy(
    skill_name: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _visible_skill(db, user, skill_name)
    return await _read(db, user, "skill", skill_name)


@router.put("/skills/{skill_name}", response_model=ToolPolicyRead)
async def put_skill_policy(
    skill_name: str,
    payload: ToolPolicyUpdate,
    user: dict = Depends(_human_admin),
    db: AsyncSession = Depends(get_db),
):
    await _visible_skill(db, user, skill_name)
    return await _put(db, user, payload, "skill", skill_name)


@router.get("/skills/{skill_name}/history", response_model=list[ToolPolicyAuditRead])
async def skill_policy_history(
    skill_name: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
):
    await _visible_skill(db, user, skill_name)
    return await _history(db, user, "skill", skill_name, limit)
