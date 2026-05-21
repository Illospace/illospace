"""Team router - org members and user profile."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.team import (
    CortexColorRead,
    TeamMemberRead,
    TeamTokenAnalyticsRead,
    TeamTokenUsageRead,
    UserProfileUpdate,
)
from brain.platform.db.models.org import User
from brain.platform.db.repositories.team import TeamRepository
from brain.systems.runs.token_usage import async_summarize_member_token_usage

router = APIRouter(
    prefix="/api",
    tags=["team"],
    dependencies=[Depends(rate_limit)],
)

_PROFILE_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _should_skip_org_lookup(user: dict[str, Any]) -> bool:
    return bool(user.get("internal") or user.get("principal_type") == "service")


async def _async_org_id_for_user(db: AsyncSession, user: dict[str, Any]) -> str | None:
    org_id = user.get("org_id") or None
    if not org_id and _should_skip_org_lookup(user):
        return None
    if not org_id and user.get("id") != "system":
        db_user = await db.get(User, user["id"])
        org_id = str(db_user.org_id) if db_user else None
    return org_id


def _normalize_profile_color(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    color = value.strip()
    if not _PROFILE_COLOR_RE.fullmatch(color):
        return None
    if len(color) == 4:
        color = f"#{color[1]}{color[1]}{color[2]}{color[2]}{color[3]}{color[3]}"
    return color.lower()


async def _async_profile_value_taken(
    db: AsyncSession,
    *,
    org_id: str,
    current_user_id: str,
    column: Any,
    value: str,
) -> bool:
    stmt = (
        select(User.id)
        .where(
            User.org_id == org_id,
            User.id != current_user_id,
            func.lower(column) == value.lower(),
        )
        .limit(1)
    )
    value = await db.scalar(stmt)
    return value is not None


async def async_list_members_payload(
    db: AsyncSession,
    user: dict[str, Any],
) -> list[TeamMemberRead]:
    org_id = await _async_org_id_for_user(db, user)
    if not org_id:
        return []
    members = await TeamRepository(db).a_list_by_org(org_id)
    return list(members)


@router.get("/team/members", response_model=list[TeamMemberRead])
async def list_members(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await async_list_members_payload(db=db, user=user)


@router.get("/team/colors", response_model=list[CortexColorRead])
async def list_cortex_colors(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Lightweight endpoint returning team members with their cortex colors."""
    org_id = await _async_org_id_for_user(db, user)
    if not org_id:
        return []
    members = await TeamRepository(db).a_list_by_org(org_id)
    return [
        CortexColorRead(id=str(member.id), name=member.name, cortex_color=member.color)
        for member in members
    ]


def _empty_token_usage(user_id: str | None = None) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "runs": 0,
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "estimated_cost": 0.0,
        "last_used_at": None,
    }


def _token_usage_from_summary(user_id: str | None, summary: dict[str, Any]) -> dict[str, Any]:
    input_tokens = int(summary.get("tokens_input") or 0)
    output_tokens = int(summary.get("tokens_output") or 0)
    return {
        "user_id": user_id,
        "runs": int(summary.get("runs") or 0),
        "api_calls": int(summary.get("api_calls") or 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cache_read": int(summary.get("cache_read") or 0),
        "cache_write": int(summary.get("cache_write") or 0),
        "estimated_cost": round(float(summary.get("estimated_cost") or 0.0), 6),
        "last_used_at": summary.get("last_used_at"),
    }


def _add_token_usage(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in (
        "runs",
        "api_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read",
        "cache_write",
    ):
        target[key] += int(source.get(key) or 0)
    target["estimated_cost"] = round(
        float(target.get("estimated_cost") or 0.0) + float(source.get("estimated_cost") or 0.0),
        6,
    )
    last_used_at = source.get("last_used_at")
    if last_used_at and (not target.get("last_used_at") or last_used_at > target["last_used_at"]):
        target["last_used_at"] = last_used_at


async def async_token_analytics_payload(
    *,
    days: int,
    db: AsyncSession,
    user: dict[str, Any],
) -> dict[str, Any]:
    """Async token usage by workspace member."""
    generated_at = datetime.now(timezone.utc)
    org_id = await _async_org_id_for_user(db, user)
    if not org_id:
        empty = _empty_token_usage()
        return {
            "window_days": days,
            "generated_at": generated_at,
            "members": [],
            "unattributed": empty,
            "totals": empty,
        }

    cutoff = generated_at - timedelta(days=days)
    members = list(await TeamRepository(db).a_list_by_org(org_id))
    usage_by_user = {str(member.id): _empty_token_usage(str(member.id)) for member in members}
    unattributed = _empty_token_usage(None)
    raw_usage = await async_summarize_member_token_usage(db, org_id=org_id, since=cutoff)
    for raw_user_id, summary in raw_usage.items():
        user_id = str(raw_user_id) if raw_user_id else None
        item = _token_usage_from_summary(user_id, summary)
        if user_id:
            usage_by_user[user_id] = item
        else:
            unattributed = item

    member_rows = sorted(
        usage_by_user.values(),
        key=lambda item: (int(item.get("total_tokens") or 0), int(item.get("api_calls") or 0)),
        reverse=True,
    )
    totals = _empty_token_usage(None)
    for item in member_rows:
        _add_token_usage(totals, item)
    _add_token_usage(totals, unattributed)

    return {
        "window_days": days,
        "generated_at": generated_at,
        "members": [TeamTokenUsageRead(**item) for item in member_rows],
        "unattributed": TeamTokenUsageRead(**unattributed),
        "totals": TeamTokenUsageRead(**totals),
    }


@router.get("/team/token-analytics", response_model=TeamTokenAnalyticsRead)
async def get_team_token_analytics(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await async_token_analytics_payload(days=days, db=db, user=user)


@router.patch("/users/me", response_model=dict)
async def update_profile_route(
    body: UserProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await async_update_profile(body=body, db=db, user=user)


async def async_update_profile(
    body: UserProfileUpdate,
    db: AsyncSession,
    user: dict[str, Any],
):
    repo = TeamRepository(db)
    u = await repo.a_get(user["id"])
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    updates = body.model_dump(exclude_unset=True)
    if "name" in updates:
        name = (updates.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        if await _async_profile_value_taken(
            db,
            org_id=u.org_id,
            current_user_id=str(u.id),
            column=User.name,
            value=name,
        ):
            raise HTTPException(status_code=409, detail="name is already taken in this workspace")
        updates["name"] = name
    if "color" in updates:
        color = _normalize_profile_color(updates.get("color"))
        if not color:
            raise HTTPException(status_code=400, detail="color must be a hex color")
        if await _async_profile_value_taken(
            db,
            org_id=u.org_id,
            current_user_id=str(u.id),
            column=User.color,
            value=color,
        ):
            raise HTTPException(status_code=409, detail="color is already taken in this workspace")
        updates["color"] = color
    for key, value in updates.items():
        setattr(u, key, value)
    await db.flush()
    return {"updated": True}
