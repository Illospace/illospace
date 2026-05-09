"""Onboarding handoff endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import rate_limit
from brain.app.triggers.adapters.internal import build_cortex_notify_trigger
from brain.app.triggers.router import route_trigger
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.idea import Idea
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.services.runtime_introspection import get_provider_auth_status
from brain.systems.runs.status import RunStatus

router = APIRouter(
    prefix="/api/onboarding",
    tags=["onboarding"],
    dependencies=[Depends(rate_limit)],
)

INTRO_ORIGIN = "onboarding"
INTRO_ORIGIN_REF_PREFIX = "runtime-ready-intro"
INTRO_TITLE = "Illo, help me finish setting up this workspace."
INTRO_DISPLAY_TITLE = "Welcome to Illo"
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

INTRO_PROMPT = "Hi Illo, what can you help me with?"


def _intro_ref(user_id: str) -> str:
    return f"{INTRO_ORIGIN_REF_PREFIX}:{user_id}"


def _find_existing_intro(session: Any, *, org_id: str, user_id: str) -> Idea | None:
    stmt = (
        select(Idea)
        .where(
            Idea.org_id == org_id,
            Idea.user_id == user_id,
            Idea.origin == INTRO_ORIGIN,
            Idea.origin_ref == _intro_ref(user_id),
            Idea.archived_at.is_(None),
        )
        .order_by(Idea.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _latest_intro_run_status(session: Any, *, idea_id: str) -> str | None:
    stmt = (
        select(AgentRunRow)
        .where(AgentRunRow.thread_id == str(idea_id))
        .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
        .limit(1)
    )
    row = session.scalars(stmt).first()
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
        "model_tier": "high",
        "thinking_tier": "high",
        "required_response": "introduce_and_continue_setup",
    }


def _route_intro_run(session: Any, *, idea: Idea, user: dict[str, Any]) -> Any:
    trigger = build_cortex_notify_trigger(
        event="idea_created",
        idea_id=str(idea.id),
        idea=idea,
        user=user,
        thread_message=INTRO_PROMPT,
        metadata=_intro_metadata(),
        priority=1,
    )
    return route_trigger(trigger, session=session)


@router.post("/runtime-ready-intro-draft")
def runtime_ready_intro_draft(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Return the visible composer draft for the runtime-ready intro flow."""
    user_id = str(user.get("id") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    org_id = require_org_context(user)
    status = get_provider_auth_status(user_id=user_id, org_id=org_id, provider="openai")
    if not status.get("runtime_key_available"):
        raise HTTPException(status_code=409, detail="OpenAI runtime is not connected yet.")

    with UnitOfWork() as uow:
        existing = _find_existing_intro(uow.session, org_id=org_id, user_id=user_id)
        return {
            "ok": True,
            "idea_id": str(existing.id) if existing is not None else None,
            "should_play": existing is None,
            "prompt": INTRO_PROMPT,
            "title": INTRO_TITLE,
            "display_title": INTRO_DISPLAY_TITLE,
            "origin": INTRO_ORIGIN,
            "origin_ref": _intro_ref(user_id),
            "run_metadata": _intro_metadata("visible_composer"),
        }


@router.post("/runtime-ready-intro")
def start_runtime_ready_intro(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Create or reuse the Cortex intro thread after model runtime setup."""
    user_id = str(user.get("id") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    org_id = require_org_context(user)
    status = get_provider_auth_status(user_id=user_id, org_id=org_id, provider="openai")
    if not status.get("runtime_key_available"):
        raise HTTPException(status_code=409, detail="OpenAI runtime is not connected yet.")

    with UnitOfWork() as uow:
        existing = _find_existing_intro(uow.session, org_id=org_id, user_id=user_id)
        if existing is not None:
            latest_status = _latest_intro_run_status(uow.session, idea_id=str(existing.id))
            if latest_status not in INTRO_RUN_SETTLED_STATUSES:
                result = _route_intro_run(uow.session, idea=existing, user=user)
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
            display_title=INTRO_DISPLAY_TITLE,
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
        uow.session.add(idea)
        uow.session.flush()

        result = _route_intro_run(uow.session, idea=idea, user=user)

        return {
            "ok": True,
            "idea_id": str(idea.id),
            "created": True,
            "run_id": result.run_id,
            "route": result.route,
            "skipped_reason": result.skipped_reason,
        }
