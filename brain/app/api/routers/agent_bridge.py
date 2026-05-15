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
from brain.platform.db.session_tasks import run_external_agent_db
from brain.platform.db.models.idea import Idea
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

        def _auth(sync_db):
            try:
                return external_agents.authenticate_bridge_token(
                    sync_db,
                    token,
                    required_scope=required_scope,
                )
            except Exception as exc:
                raise_external_agent_http_error(exc)

        return await run_external_agent_db(db, _auth)

    return _dependency


def _thread_payload(idea: Idea, message: Any, notified_user_ids: list[str]) -> dict[str, Any]:
    return {
        "idea": {
            "id": str(idea.id),
            "title": idea.title,
            "status": idea.status,
            "origin": idea.origin,
            "origin_ref": idea.origin_ref,
            "created_at": message.created_at.isoformat() if getattr(message, "created_at", None) else None,
        },
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
    def _heartbeat(sync_db):
        connection = external_agents.record_heartbeat(
            sync_db,
            principal,
            status=payload.status,
            capabilities=payload.capabilities,
            metadata=payload.metadata,
        )
        return {"ok": True, "connection": external_agents.serialize_connection(connection)}

    return await run_external_agent_db(db, _heartbeat)


@router.post("/tasks/claim")
async def claim_tasks(
    payload: BridgeClaimTasksRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_TASK_CLAIM)
    ),
):
    def _claim(sync_db):
        rows = external_agents.claim_tasks(sync_db, principal, max_tasks=payload.max_tasks)
        return {"tasks": [external_agents.serialize_task(row) for row in rows]}

    return await run_external_agent_db(db, _claim)


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_TASK_CLAIM)
    ),
):
    def _get(sync_db):
        try:
            task = external_agents.require_task_for_principal(sync_db, principal, task_id)
            return external_agents.serialize_task(
                task,
                include_events=True,
                include_artifacts=True,
                session=sync_db,
            )
        except Exception as exc:
            raise_external_agent_http_error(exc)

    return await run_external_agent_db(db, _get)


@router.post("/tasks/{task_id}/events")
async def append_task_event(
    task_id: str,
    payload: BridgeTaskEventRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_TASK_UPDATE)
    ),
):
    def _event(sync_db):
        try:
            event = external_agents.update_task_event(
                sync_db,
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

    return await run_external_agent_db(db, _event)


@router.post("/tasks/{task_id}/artifacts")
async def append_task_artifact(
    task_id: str,
    payload: BridgeArtifactCreate,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_ARTIFACT_WRITE)
    ),
):
    def _artifact(sync_db):
        try:
            artifact = external_agents.append_artifact(
                sync_db,
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

    return await run_external_agent_db(db, _artifact)


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: str,
    payload: BridgeCompleteTaskRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_TASK_COMPLETE)
    ),
):
    def _complete(sync_db):
        try:
            task, message = external_agents.complete_task(
                sync_db,
                principal,
                task_id=task_id,
                result_summary=payload.result_summary,
                artifacts=[artifact.model_dump() for artifact in payload.artifacts],
                payload=payload.payload,
            )
            return {
                "task": external_agents.serialize_task(task, include_artifacts=True, session=sync_db),
                "thread_message": external_agents.serialize_thread_message(message) if message else None,
            }
        except Exception as exc:
            raise_external_agent_http_error(exc)

    result = await run_external_agent_db(db, _complete)
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
    def _fail(sync_db):
        try:
            task, message = external_agents.fail_task(
                sync_db,
                principal,
                task_id=task_id,
                error=payload.error,
                payload=payload.payload,
            )
            return {
                "task": external_agents.serialize_task(task),
                "thread_message": external_agents.serialize_thread_message(message) if message else None,
            }
        except Exception as exc:
            raise_external_agent_http_error(exc)

    result = await run_external_agent_db(db, _fail)
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
    return await run_external_agent_db(
        db,
        lambda sync_db: external_agents.search_workspace(
            sync_db,
            principal,
            query=payload.query,
            limit=payload.limit,
        ),
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
    def _get(sync_db):
        try:
            return external_agents.get_thread(sync_db, principal, idea_id=idea_id, limit=limit)
        except Exception as exc:
            raise_external_agent_http_error(exc)

    return await run_external_agent_db(db, _get)


@router.get("/workspace/team-members")
async def bridge_get_team_members(
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_WORKSPACE_READ)
    ),
):
    return await run_external_agent_db(db, lambda sync_db: external_agents.get_team_members(sync_db, principal))


@router.post("/illo/ask", status_code=202)
async def ask_illo(
    payload: BridgeAskIlloRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_ILLO_ASK)
    ),
):
    def _ask(sync_db):
        task = external_agents.create_headless_ask(
            sync_db,
            principal,
            question=payload.question,
            context=payload.context,
            metadata=payload.metadata,
        )
        return external_agents.serialize_task(task, include_events=True, session=sync_db)

    return await run_external_agent_db(db, _ask)


@router.get("/illo/ask/{ask_id}")
async def get_illo_ask(
    ask_id: str,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_ILLO_ASK)
    ),
):
    def _get(sync_db):
        try:
            return external_agents.get_headless_ask(sync_db, principal, ask_id=ask_id)
        except Exception as exc:
            raise_external_agent_http_error(exc)

    return await run_external_agent_db(db, _get)


@router.post("/illo/threads", status_code=201)
async def create_illo_thread(
    payload: BridgeThreadCreateRequest,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(
        require_bridge_scope(external_agents.SCOPE_ILLO_THREAD_CREATE)
    ),
):
    should_trigger = bool(payload.trigger_illo or classify_mention_intent(payload.body).should_invoke_illo)

    def _create(sync_db):
        try:
            metadata = {**payload.metadata, "trigger_illo": should_trigger}
            idea, message, notified = external_agents.create_thread_from_agent(
                sync_db,
                principal,
                title=payload.title,
                body=payload.body,
                teammate_user_ids=payload.teammate_user_ids,
                artifacts=payload.artifacts,
                trigger_illo=should_trigger,
                metadata=metadata,
            )
            response = _thread_payload(idea, message, notified)
            response["_trigger_idea"] = idea
            return response
        except Exception as exc:
            raise_external_agent_http_error(exc)

    result = await run_external_agent_db(db, _create)
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

    def _post(sync_db):
        try:
            metadata = {**payload.metadata, "trigger_illo": should_trigger}
            idea, message, notified = external_agents.post_thread_message_from_agent(
                sync_db,
                principal,
                idea_id=idea_id,
                body=payload.body,
                teammate_user_ids=payload.teammate_user_ids,
                artifacts=payload.artifacts,
                trigger_illo=should_trigger,
                metadata=metadata,
            )
            response = _thread_payload(idea, message, notified)
            response["_trigger_idea"] = idea
            return response
        except Exception as exc:
            raise_external_agent_http_error(exc)

    result = await run_external_agent_db(db, _post)
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
