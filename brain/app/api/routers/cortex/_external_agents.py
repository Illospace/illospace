"""Cortex endpoints for delegating work to connected personal agents."""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.db_utils import run_db
from brain.app.api.deps import get_db
from brain.app.api.routers.external_agent_errors import raise_external_agent_http_error
from brain.app.api.routers.cortex._router import router
from brain.app.api.routers.ws import ws_manager
from brain.app.api.schemas.external_agents import CortexExternalAgentTaskCreate
from brain.platform.db.models.external_agent import ExternalAgentTaskRow
from brain.systems.external_agents import service as external_agents

@router.post("/ideas/{idea_id}/external-agent-tasks", status_code=201)
async def create_cortex_external_agent_task(
    idea_id: str,
    payload: CortexExternalAgentTaskCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    user_id = str(user.get("id"))

    def _create(sync_db):
        try:
            task, thread_message = external_agents.create_external_task_for_idea(
                sync_db,
                org_id=org_id,
                user_id=user_id,
                idea_id=idea_id,
                connection_id=payload.connection_id,
                instructions=payload.instructions,
                title=payload.title,
                include_thread_context=payload.include_thread_context,
                include_project_context=payload.include_project_context,
                metadata=payload.metadata,
                idempotency_key=payload.idempotency_key,
            )
            return {
                "task": external_agents.serialize_task(task, include_events=True, session=sync_db),
                "thread_message": external_agents.serialize_thread_message(thread_message),
            }
        except Exception as exc:
            raise_external_agent_http_error(exc)

    result = await run_db(db, _create)
    await db.commit()
    thread_message = result.get("thread_message") if isinstance(result, dict) else None
    if isinstance(thread_message, dict):
        await ws_manager.broadcast_product_event(
            "thread_message",
            {"idea_id": idea_id, "message": thread_message},
            org_id=org_id,
        )
    await ws_manager.broadcast_product_event(
        "status_change",
        {"idea_id": idea_id, "new_status": "working"},
        org_id=org_id,
    )
    return result


@router.get("/ideas/{idea_id}/external-agent-tasks")
async def list_cortex_external_agent_tasks(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)

    def _list(sync_db):
        try:
            external_agents.require_idea_for_org(sync_db, idea_id=idea_id, org_id=org_id)
            stmt = (
                select(ExternalAgentTaskRow)
                .where(ExternalAgentTaskRow.source_idea_id == str(idea_id), ExternalAgentTaskRow.org_id == str(org_id))
                .order_by(ExternalAgentTaskRow.created_at.desc(), ExternalAgentTaskRow.id.desc())
            )
            return {
                "tasks": [
                    external_agents.serialize_task(row, include_events=True, include_artifacts=True, session=sync_db)
                    for row in sync_db.scalars(stmt).all()
                ]
            }
        except Exception as exc:
            raise_external_agent_http_error(exc)

    return await run_db(db, _list)
