"""Team router — org members and user profile."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.team import CortexColorRead, TeamMemberRead, UserProfileUpdate
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User
from brain.platform.db.repositories.team import TeamRepository

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
    org_id = user.get("org_id") or None
    if not org_id and _should_skip_org_lookup(user):
        return []
    if not org_id and user.get("id") != "system":
        # Fallback: look up org_id from the user's DB record
        from brain.systems.auth.users import get_user_by_id
        db_user = get_user_by_id(user["id"])
        org_id = db_user.get("org_id") if db_user else None
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
    org_id = user.get("org_id") or None
    if not org_id and _should_skip_org_lookup(user):
        return []
    if not org_id and user.get("id") != "system":
        from brain.systems.auth.users import get_user_by_id
        db_user = get_user_by_id(user["id"])
        org_id = db_user.get("org_id") if db_user else None
    if not org_id:
        return []
    repo = TeamRepository(db)
    members = repo.list_by_org(org_id)
    return [
        CortexColorRead(id=str(m.id), name=m.name, cortex_color=m.color)
        for m in members
    ]


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
