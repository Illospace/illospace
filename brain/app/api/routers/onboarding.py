"""Onboarding handoff endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db, rate_limit
from brain.app.triggers.adapters.internal import build_cortex_notify_trigger
from brain.app.triggers.router import async_route_trigger
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.idea import Idea
from brain.systems.cortex.title_generation import generate_and_store_idea_display_title
from brain.systems.services.runtime_introspection import async_get_provider_auth_status
from brain.systems.runs.status import RunStatus

router = APIRouter(
    prefix="/api/onboarding",
    tags=["onboarding"],
    dependencies=[Depends(rate_limit)],
)

INTRO_ORIGIN = "onboarding"
INTRO_ORIGIN_REF_PREFIX = "runtime-ready-intro"
INTRO_PROMPT = "Hey Illo, help me understand what you can do to help me."
INTRO_TITLE = INTRO_PROMPT
INTRO_DESCRIPTION = "Runtime is connected. Illo can finish setup from here."
INTRO_MODEL = "openai/gpt-5.5"
INTRO_RUN_SETTLED_STATUSES = {
    RunStatus.QUEUED.value,
    RunStatus.STARTING.value,
    RunStatus.RUNNING.value,
    RunStatus.PAUSED.value,
    RunStatus.VERIFYING.value,
    RunStatus.COMPLETED.value,
}

def _intro_ref(user_id: str) -> str:
    return f"{INTRO_ORIGIN_REF_PREFIX}:{user_id}"


def _has_personal_openai_connection(status: dict[str, Any]) -> bool:
    return bool(
        status.get("runtime_key_available")
        and status.get("runtime_key_source") == "codex_subscription"
    )


async def _require_personal_openai_connection(db: AsyncSession, *, user_id: str, org_id: str) -> None:
    status = await async_get_provider_auth_status(db, user_id=user_id, org_id=org_id, provider="openai")
    if not _has_personal_openai_connection(status):
        raise HTTPException(
            status_code=409,
            detail="Connect a personal OpenAI account before starting onboarding.",
        )


async def _find_existing_intro(session: Any, *, org_id: str, user_id: str) -> Idea | None:
    intro_ref = _intro_ref(user_id)
    stmt = (
        select(Idea)
        .where(
            Idea.org_id == org_id,
            Idea.user_id == user_id,
            or_(
                Idea.origin_ref == intro_ref,
                (Idea.origin == INTRO_ORIGIN) & (Idea.title == INTRO_TITLE),
            ),
        )
        .order_by(Idea.archived_at.is_not(None), Idea.created_at.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


def _intro_is_archived(idea: Idea) -> bool:
    return getattr(idea, "archived_at", None) is not None


async def _latest_intro_run_status(session: Any, *, idea_id: str) -> str | None:
    stmt = (
        select(AgentRunRow)
        .where(AgentRunRow.thread_id == str(idea_id))
        .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
        .limit(1)
    )
    row = (await session.scalars(stmt)).first()
    status = getattr(row, "status", None)
    return str(status) if status else None


def _intro_metadata(prompt_visibility: str = "hidden") -> dict[str, str]:
    return {
        "origin": "onboarding",
        "onboarding_step": "runtime_ready_intro",
        "prompt_visibility": prompt_visibility,
        "execution_profile": "fast",
        "provider": "openai",
        "model": INTRO_MODEL,
        "thinking_tier": "high",
    }


async def _route_intro_run(session: Any, *, idea: Idea, user: dict[str, Any]) -> Any:
    trigger = build_cortex_notify_trigger(
        event="idea_created",
        idea_id=str(idea.id),
        idea=idea,
        user=user,
        thread_message=INTRO_PROMPT,
        metadata=_intro_metadata(),
        priority=1,
    )
    return await async_route_trigger(trigger, session=session)


def _queue_intro_display_title(
    background_tasks: BackgroundTasks,
    *,
    idea_id: str,
    user_id: str,
    org_id: str,
    raw_title: str,
) -> None:
    background_tasks.add_task(
        generate_and_store_idea_display_title,
        idea_id,
        raw_title=raw_title,
        user_id=user_id,
        org_id=org_id,
    )


@router.post("/runtime-ready-intro-draft")
async def runtime_ready_intro_draft(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the visible composer draft for the runtime-ready intro flow."""
    user_id = str(user.get("id") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    org_id = require_org_context(user)
    await _require_personal_openai_connection(db, user_id=user_id, org_id=org_id)

    existing = await _find_existing_intro(db, org_id=org_id, user_id=user_id)
    existing_active_id = (
        str(existing.id)
        if existing is not None and not _intro_is_archived(existing)
        else None
    )
    return {
        "ok": True,
        "idea_id": existing_active_id,
        "should_play": existing is None,
        "prompt": INTRO_PROMPT,
        "origin": INTRO_ORIGIN,
        "origin_ref": _intro_ref(user_id),
        "run_metadata": _intro_metadata("visible_composer"),
    }


@router.post("/runtime-ready-intro")
async def start_runtime_ready_intro(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
    background_tasks: BackgroundTasks = None,
) -> dict[str, Any]:
    """Create or reuse the Cortex intro thread after model runtime setup."""
    user_id = str(user.get("id") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    org_id = require_org_context(user)
    await _require_personal_openai_connection(db, user_id=user_id, org_id=org_id)

    existing = await _find_existing_intro(db, org_id=org_id, user_id=user_id)
    if existing is not None:
        if _intro_is_archived(existing):
            return {
                "ok": True,
                "idea_id": str(existing.id),
                "created": False,
                "run_id": None,
            }
        latest_status = await _latest_intro_run_status(db, idea_id=str(existing.id))
        if latest_status not in INTRO_RUN_SETTLED_STATUSES:
            result = await _route_intro_run(db, idea=existing, user=user)
            await db.commit()
            return {
                "ok": True,
                "idea_id": str(existing.id),
                "created": False,
                "run_id": result.run_id,
                "route": result.route,
                "skipped_reason": result.skipped_reason,
            }
        return {
            "ok": True,
            "idea_id": str(existing.id),
            "created": False,
            "run_id": None,
        }

    idea = Idea(
        title=INTRO_TITLE,
        description=INTRO_DESCRIPTION,
        status="active",
        origin=INTRO_ORIGIN,
        origin_ref=_intro_ref(user_id),
        user_id=user_id,
        org_id=org_id,
        position_x=0,
        position_y=0,
        agent_details={
            "onboarding": {
                "source": "runtime_ready",
                "intro": True,
            },
        },
    )
    db.add(idea)
    await db.flush()

    result = await _route_intro_run(db, idea=idea, user=user)
    if background_tasks is not None:
        _queue_intro_display_title(
            background_tasks,
            idea_id=str(idea.id),
            user_id=user_id,
            org_id=org_id,
            raw_title=idea.title,
        )
    await db.commit()

    return {
        "ok": True,
        "idea_id": str(idea.id),
        "created": True,
        "run_id": result.run_id,
        "route": result.route,
        "skipped_reason": result.skipped_reason,
    }
