"""Team router — org members and user profile."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.team import (
    CortexColorRead,
    TeamMemberRead,
    TeamTokenAnalyticsRead,
    TeamTokenUsageRead,
    UserProfileUpdate,
)
from brain.platform.db.models.agent import AgentApiCall
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User
from brain.platform.db.repositories.team import TeamRepository
from brain.systems.runs.modeling import calculate_cost

router = APIRouter(
    prefix="/api",
    tags=["team"],
    dependencies=[Depends(rate_limit)],
)

_PROFILE_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _team_activity_skill_name(run: AgentRunRow) -> str | None:
    metadata = run.metadata_ if isinstance(run.metadata_, dict) else {}
    routing = metadata.get("routing") if isinstance(metadata.get("routing"), dict) else {}
    skill_name = routing.get("selected_skill")
    return str(skill_name) if skill_name else None


def _should_skip_org_lookup(user: dict[str, Any]) -> bool:
    return bool(user.get("internal") or user.get("principal_type") == "service")


def _org_id_for_user(db: Session, user: dict[str, Any]) -> str | None:
    org_id = user.get("org_id") or None
    if not org_id and _should_skip_org_lookup(user):
        return None
    if not org_id and user.get("id") != "system":
        from brain.systems.auth.users import get_user_by_id
        db_user = get_user_by_id(user["id"])
        org_id = db_user.get("org_id") if db_user else None
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


def _profile_value_taken(
    db: Session,
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
    return db.scalar(stmt) is not None


def list_members_payload(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> list[TeamMemberRead]:
    org_id = _org_id_for_user(db, user)
    if not org_id:
        return []
    repo = TeamRepository(db)
    return repo.list_by_org(org_id)


@router.get("/team/members", response_model=list[TeamMemberRead])
def list_members(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return list_members_payload(db=db, user=user)


@router.get("/team/colors", response_model=list[CortexColorRead])
def list_cortex_colors(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Lightweight endpoint returning team members with their cortex colors.

    Used by the frontend to position and color user suns in the solar system view.
    """
    org_id = _org_id_for_user(db, user)
    if not org_id:
        return []
    repo = TeamRepository(db)
    members = repo.list_by_org(org_id)
    return [
        CortexColorRead(id=str(m.id), name=m.name, cortex_color=m.color)
        for m in members
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


@router.get("/team/token-analytics", response_model=TeamTokenAnalyticsRead)
def get_team_token_analytics(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Token usage by workspace member, derived from run-linked API-call telemetry."""
    generated_at = datetime.now(timezone.utc)
    org_id = _org_id_for_user(db, user)
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
    members = TeamRepository(db).list_by_org(org_id)
    usage_by_user = {str(member.id): _empty_token_usage(str(member.id)) for member in members}
    unattributed = _empty_token_usage(None)

    usage_stmt = (
        select(
            AgentRunRow.user_id.label("user_id"),
            func.count(func.distinct(AgentRunRow.id)).label("runs"),
            func.count(AgentApiCall.id).label("api_calls"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("input_tokens"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("output_tokens"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
            func.max(AgentApiCall.created_at).label("last_used_at"),
        )
        .join(AgentRunRow, AgentRunRow.id == AgentApiCall.run_id)
        .where(
            AgentRunRow.org_id == org_id,
            AgentApiCall.created_at >= cutoff,
        )
        .group_by(AgentRunRow.user_id)
    )

    for row in db.execute(usage_stmt).mappings():
        user_id = str(row["user_id"]) if row["user_id"] else None
        target = usage_by_user.setdefault(user_id, _empty_token_usage(user_id)) if user_id else unattributed
        target["runs"] = int(row["runs"] or 0)
        target["api_calls"] = int(row["api_calls"] or 0)
        target["input_tokens"] = int(row["input_tokens"] or 0)
        target["output_tokens"] = int(row["output_tokens"] or 0)
        target["total_tokens"] = target["input_tokens"] + target["output_tokens"]
        target["cache_read"] = int(row["cache_read"] or 0)
        target["cache_write"] = int(row["cache_write"] or 0)
        target["last_used_at"] = row["last_used_at"]

    cost_stmt = (
        select(
            AgentRunRow.user_id.label("user_id"),
            AgentApiCall.model.label("model"),
            func.coalesce(func.sum(AgentApiCall.tokens_input), 0).label("input_tokens"),
            func.coalesce(func.sum(AgentApiCall.tokens_output), 0).label("output_tokens"),
            func.coalesce(func.sum(AgentApiCall.cache_read), 0).label("cache_read"),
            func.coalesce(func.sum(AgentApiCall.cache_write), 0).label("cache_write"),
        )
        .join(AgentRunRow, AgentRunRow.id == AgentApiCall.run_id)
        .where(
            AgentRunRow.org_id == org_id,
            AgentApiCall.created_at >= cutoff,
        )
        .group_by(AgentRunRow.user_id, AgentApiCall.model)
    )

    for row in db.execute(cost_stmt).mappings():
        user_id = str(row["user_id"]) if row["user_id"] else None
        target = usage_by_user.setdefault(user_id, _empty_token_usage(user_id)) if user_id else unattributed
        try:
            target["estimated_cost"] += calculate_cost(
                str(row["model"] or ""),
                int(row["input_tokens"] or 0),
                int(row["output_tokens"] or 0),
                cache_read=int(row["cache_read"] or 0),
                cache_write=int(row["cache_write"] or 0),
            )
        except Exception:
            continue
        target["estimated_cost"] = round(float(target["estimated_cost"] or 0.0), 6)

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


@router.get("/team/activity")
def get_team_activity(
    hours: int = 48,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Recent team activity — skills used, ideas created, etc."""
    org_id = user.get("org_id")
    if not org_id:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    # Query recent runs + thread messages as team activity
    try:
        stmt = (
            select(
                AgentRunRow,
                func.coalesce(Idea.display_title, Idea.title).label("idea_title"),
                User.name.label("user_name"),
            )
            .join(Idea, cast(Idea.id, String) == AgentRunRow.thread_id)
            .outerjoin(User, User.id == AgentRunRow.user_id)
            .where(
                Idea.org_id == org_id,
                AgentRunRow.created_at >= cutoff,
            )
            .order_by(AgentRunRow.created_at.desc())
            .limit(50)
        )
        rows = db.execute(stmt).all()
        return [
            {
                "user_id": run.user_id,
                "user_name": user_name,
                "skill_name": _team_activity_skill_name(run),
                "status": run.status,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "idea_title": idea_title,
                "type": "run",
            }
            for run, idea_title, user_name in rows
        ]
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("team_activity_error: %s", e)
        return []


@router.patch("/users/me", response_model=dict)
def update_profile(
    body: UserProfileUpdate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    repo = TeamRepository(db)
    u = repo.get(user["id"])
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    updates = body.model_dump(exclude_unset=True)
    if "name" in updates:
        name = (updates.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        if _profile_value_taken(
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
        if _profile_value_taken(
            db,
            org_id=u.org_id,
            current_user_id=str(u.id),
            column=User.color,
            value=color,
        ):
            raise HTTPException(status_code=409, detail="color is already taken in this workspace")
        updates["color"] = color
    provider = updates.get("default_provider")
    if provider is not None:
        provider = provider.strip().lower() or None
        if provider not in (None, "anthropic", "openai"):
            raise HTTPException(status_code=400, detail="default_provider must be anthropic or openai")
        updates["default_provider"] = provider
    for key, value in updates.items():
        setattr(u, key, value)
    db.flush()
    return {"updated": True}
