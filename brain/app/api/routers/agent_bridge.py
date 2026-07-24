"""Scoped bridge API for personal agents such as Hermes and OpenClaw."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.deps import get_db, rate_limit
from brain.app.api.routers.external_agent_errors import raise_external_agent_http_error
from brain.app.api.routers.ws import ws_manager
from brain.app.api.schemas.external_agents import (
    BridgeArtifactCreate,
    BridgeAskIlloRequest,
    BridgeClaimTasksRequest,
    BridgeCompleteTaskRequest,
    BridgeFailTaskRequest,
    BridgeHeartbeatRequest,
    BridgeTaskEventRequest,
    BridgeThreadCreateRequest,
    BridgeThreadMessageCreateRequest,
    BridgeWorkspaceSearchRequest,
)
from brain.app.mentions import classify_mention_intent
from brain.platform.db.models.idea import Idea
from brain.systems.cortex.thread_links import thread_link_payload
from brain.systems.external_agents import service as external_agents


router = APIRouter(
    prefix="/api/agent-bridge",
    tags=["agent-bridge"],
    dependencies=[Depends(rate_limit)],
)


def _bearer_token(request: Request, x_illo_bridge_token: str | None) -> str:
    if x_illo_bridge_token:
        return x_illo_bridge_token.strip()
    auth = request.headers.get("Authorization", "")
    if isinstance(auth, str) and auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def require_bridge_scope(required_scope: str) -> Callable[..., Any]:
    async def _dependency(
        request: Request,
        x_illo_bridge_token: str | None = Header(default=None, alias="X-Illo-Bridge-Token"),
        db: AsyncSession = Depends(get_db),
    ) -> external_agents.AgentBridgePrincipal:
        token = _bearer_token(request, x_illo_bridge_token)

        try:
            return await external_agents.authenticate_bridge_token(
                db,
                token,
                required_scope=required_scope,
            )
        except Exception as exc:
            raise_external_agent_http_error(exc)

    return _dependency


def _thread_payload(idea: Idea, message: Any, notified_user_ids: list[str]) -> dict[str, Any]:
    links = thread_link_payload(idea.id)
    display_title = getattr(idea, "display_title", None)
    title = getattr(idea, "title", None)
    preview_updated_at = getattr(idea, "preview_updated_at", None)
    thread_reference = {
        "type": "thread_reference",
        "object_type": "thread",
        "object_id": str(idea.id),
        "thread_id": str(idea.id),
        "status": "available",
        "title": display_title or title,
        "preview_summary": getattr(idea, "preview_summary", None),
        "preview_source": getattr(idea, "preview_source", None),
        "preview_updated_at": (
            preview_updated_at.isoformat()
            if preview_updated_at
            else None
        ),
        **links,
    }
    return {
        "idea": {
            "id": str(idea.id),
            "thread_id": str(idea.id),
            "title": title,
            "display_title": display_title,
            "status": getattr(idea, "status", None),
            "origin": getattr(idea, "origin", None),
            "origin_ref": getattr(idea, "origin_ref", None),
            "preview_summary": getattr(idea, "preview_summary", None),
            "preview_source": getattr(idea, "preview_source", None),
            **links,
            "created_at": message.created_at.isoformat() if getattr(message, "created_at", None) else None,
        },
        "thread_reference": thread_reference,
        "thread_id": str(idea.id),
        "thread_url": links["thread_url"],
        "thread_route": links["thread_route"],
        "url": links["thread_url"],
        "message": external_agents.serialize_thread_message(message),
        "notified_user_ids": notified_user_ids,
    }


async def _commit_for_live_fanout(db: AsyncSession) -> None:
    await db.commit()


async def _broadcast_thread_message(result: dict[str, Any], *, org_id: str | None) -> None:
    message = result.get("thread_message") or result.get("message")
    if not isinstance(message, dict):
        return
    idea_id = str(message.get("idea_id") or "")
    if not idea_id:
        return
    await ws_manager.broadcast_product_event(
        "thread_message",
        {"idea_id": idea_id, "message": message},
        org_id=org_id,
    )


async def _broadcast_status_if_present(result: dict[str, Any], *, org_id: str | None) -> None:
    idea = result.get("idea")
    if not isinstance(idea, dict) or not idea.get("id") or not idea.get("status"):
        return
    await ws_manager.broadcast_product_event(
        "status_change",
        {"idea_id": str(idea["id"]), "new_status": str(idea["status"])},
        org_id=org_id,
    )


async def _broadcast_thread_result(result: dict[str, Any], *, org_id: str | None) -> None:
    await _broadcast_thread_message(result, org_id=org_id)
    await _broadcast_status_if_present(result, org_id=org_id)


async def _run_trigger_if_requested(
    db: AsyncSession,
    *,
    idea: Idea,
    body: str,
    metadata: dict[str, Any],
    principal: external_agents.AgentBridgePrincipal,
):
    if not metadata.get("trigger_illo"):
        return None
    metadata = dict(metadata)
    metadata.setdefault(
        "request_source",
        external_agents.request_source_context(
            principal,
            surface="mcp_personal_agent" if metadata.get("mcp_tool") else "personal_agent_bridge",
            visibility="visible_team_thread",
            permission="visible_coordination_trigger",
            tool_name=str(metadata.get("mcp_tool") or "") or None,
        ),
    )
    from brain.app.triggers.adapters.internal import build_cortex_notify_trigger
    from brain.app.triggers.router import async_route_trigger

    user = {
        "id": principal.owner_user_id,
        "org_id": principal.org_id,
        "role": "member",
        "name": "Personal agent",
        "principal_type": "human",
    }
    trigger = build_cortex_notify_trigger(
        event="thread_reply",
        idea_id=str(idea.id),
        idea=idea,
        user=user,
        thread_message=body,
        metadata=metadata,
        effective_metadata=metadata,
        priority=0,
    )
    return (await async_route_trigger(trigger, session=db)).to_response()


@router.post("/heartbeat")
async def heartbeat(
    payload: BridgeHeartbeatRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_CONNECTION_HEARTBEAT)
    ),
):
    connection = await external_agents.record_heartbeat(
        db,
        principal,
        status=payload.status,
        capabilities=payload.capabilities,
        metadata=payload.metadata,
    )
    return {"ok": True, "connection": external_agents.serialize_connection(connection)}


@router.post("/tasks/claim")
async def claim_tasks(
    payload: BridgeClaimTasksRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_TASK_CLAIM)
    ),
):
    rows = await external_agents.claim_tasks(db, principal, max_tasks=payload.max_tasks)
    return {"tasks": [await external_agents.serialize_task(row) for row in rows]}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_TASK_CLAIM)
    ),
):
    try:
        task = await external_agents.require_task_for_principal(db, principal, task_id)
        return await external_agents.serialize_task(
            task,
            include_events=True,
            include_artifacts=True,
            session=db,
        )
    except Exception as exc:
        raise_external_agent_http_error(exc)


@router.post("/tasks/{task_id}/events")
async def append_task_event(
    task_id: str,
    payload: BridgeTaskEventRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_TASK_UPDATE)
    ),
):
    try:
        event = await external_agents.update_task_event(
            db,
            principal,
            task_id=task_id,
            event_type=payload.event_type,
            status=payload.status,
            message=payload.message,
            payload=payload.payload,
            remote_event_id=payload.remote_event_id,
        )
        return {"event": external_agents.serialize_event(event)}
    except Exception as exc:
        raise_external_agent_http_error(exc)


@router.post("/tasks/{task_id}/artifacts")
async def append_task_artifact(
    task_id: str,
    payload: BridgeArtifactCreate,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_ARTIFACT_WRITE)
    ),
):
    try:
        artifact = await external_agents.append_artifact(
            db,
            principal,
            task_id=task_id,
            kind=payload.kind,
            title=payload.title,
            mime_type=payload.mime_type,
            content_text=payload.content_text,
            content_json=payload.content_json,
            uri=payload.uri,
            upload_id=payload.upload_id,
            metadata=payload.metadata,
        )
        return {"artifact": external_agents.serialize_artifact(artifact)}
    except Exception as exc:
        raise_external_agent_http_error(exc)


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: str,
    payload: BridgeCompleteTaskRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_TASK_COMPLETE)
    ),
):
    try:
        task, message = await external_agents.complete_task(
            db,
            principal,
            task_id=task_id,
            result_summary=payload.result_summary,
            artifacts=[artifact.model_dump() for artifact in payload.artifacts],
            payload=payload.payload,
        )
        result = {
            "task": await external_agents.serialize_task(task, include_artifacts=True, session=db),
            "thread_message": external_agents.serialize_thread_message(message) if message else None,
        }
    except Exception as exc:
        raise_external_agent_http_error(exc)

    await _commit_for_live_fanout(db)
    await _broadcast_thread_result(result, org_id=principal.org_id)
    return result


@router.post("/tasks/{task_id}/fail")
async def fail_task(
    task_id: str,
    payload: BridgeFailTaskRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_TASK_COMPLETE)
    ),
):
    try:
        task, message = await external_agents.fail_task(
            db,
            principal,
            task_id=task_id,
            error=payload.error,
            payload=payload.payload,
        )
        result = {
            "task": await external_agents.serialize_task(task),
            "thread_message": external_agents.serialize_thread_message(message) if message else None,
        }
    except Exception as exc:
        raise_external_agent_http_error(exc)

    await _commit_for_live_fanout(db)
    await _broadcast_thread_result(result, org_id=principal.org_id)
    return result


@router.post("/workspace/search")
async def bridge_search_workspace(
    payload: BridgeWorkspaceSearchRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_WORKSPACE_READ)
    ),
):
    return await external_agents.search_workspace(
        db,
        principal,
        query=payload.query,
        limit=payload.limit,
    )


@router.get("/workspace/threads/{idea_id}")
async def bridge_get_thread(
    idea_id: str,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_WORKSPACE_READ)
    ),
):
    try:
        return await external_agents.get_thread(db, principal, idea_id=idea_id, limit=limit)
    except Exception as exc:
        raise_external_agent_http_error(exc)


@router.get("/workspace/team-members")
async def bridge_get_team_members(
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_WORKSPACE_READ)
    ),
):
    return await external_agents.get_team_members(db, principal)


@router.post("/illo/ask", status_code=202)
async def ask_illo(
    payload: BridgeAskIlloRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_ILLO_ASK)
    ),
):
    task = await external_agents.create_headless_ask(
        db,
        principal,
        question=payload.question,
        context=payload.context,
        metadata=payload.metadata,
        effort=payload.effort,
    )
    return await external_agents.serialize_task(task, include_events=True, session=db)


@router.get("/illo/ask/{ask_id}")
async def get_illo_ask(
    ask_id: str,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_ILLO_ASK)
    ),
):
    try:
        return await external_agents.get_headless_ask(db, principal, ask_id=ask_id)
    except Exception as exc:
        raise_external_agent_http_error(exc)


@router.post("/illo/threads", status_code=201)
async def create_illo_thread(
    payload: BridgeThreadCreateRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_ILLO_THREAD_CREATE)
    ),
):
    should_trigger = bool(payload.trigger_illo or classify_mention_intent(payload.body).should_invoke_illo)
    try:
        metadata = {**payload.metadata, "trigger_illo": should_trigger}
        idea, message, notified = await external_agents.create_thread_from_agent(
            db,
            principal,
            title=payload.title,
            body=payload.body,
            teammate_user_ids=payload.teammate_user_ids,
            artifacts=payload.artifacts,
            trigger_illo=should_trigger,
            metadata=metadata,
        )
        result = _thread_payload(idea, message, notified)
        result["_trigger_idea"] = idea
    except Exception as exc:
        raise_external_agent_http_error(exc)

    trigger_idea = result.pop("_trigger_idea", None)
    if trigger_idea is not None:
        result["trigger"] = await _run_trigger_if_requested(
            db,
            idea=trigger_idea,
            body=payload.body,
            metadata={**payload.metadata, "trigger_illo": should_trigger},
            principal=principal,
        )
    await _commit_for_live_fanout(db)
    idea = result.get("idea") if isinstance(result, dict) else None
    if isinstance(idea, dict):
        await ws_manager.broadcast_product_event(
            "idea_created",
            {"idea_id": idea.get("id"), "title": idea.get("title")},
            org_id=principal.org_id,
        )
    await _broadcast_thread_result(result, org_id=principal.org_id)
    return result


@router.post("/illo/threads/{idea_id}/messages", status_code=201)
async def post_illo_thread_message(
    idea_id: str,
    payload: BridgeThreadMessageCreateRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_ILLO_THREAD_WRITE)
    ),
):
    should_trigger = bool(payload.trigger_illo or classify_mention_intent(payload.body).should_invoke_illo)
    try:
        metadata = {**payload.metadata, "trigger_illo": should_trigger}
        idea, message, notified = await external_agents.post_thread_message_from_agent(
            db,
            principal,
            idea_id=idea_id,
            body=payload.body,
            teammate_user_ids=payload.teammate_user_ids,
            artifacts=payload.artifacts,
            trigger_illo=should_trigger,
            metadata=metadata,
        )
        result = _thread_payload(idea, message, notified)
        result["_trigger_idea"] = idea
    except Exception as exc:
        raise_external_agent_http_error(exc)

    trigger_idea = result.pop("_trigger_idea", None)
    if trigger_idea is not None:
        result["trigger"] = await _run_trigger_if_requested(
            db,
            idea=trigger_idea,
            body=payload.body,
            metadata={**payload.metadata, "trigger_illo": should_trigger},
            principal=principal,
        )
    await _commit_for_live_fanout(db)
    await _broadcast_thread_result(result, org_id=principal.org_id)
    return result
