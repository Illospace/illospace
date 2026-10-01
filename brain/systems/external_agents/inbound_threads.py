"""Thread-shaped reads for headless inbound submissions (which have no Idea row)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
from brain.systems.inbound.results import (
    InboundSubmissionResultState,
    project_inbound_submission_result,
    read_inbound_submission_result,
)
from brain.systems.runs.cortex.read_models import (
    public_failures_for_run_ids,
    serialize_public_run_artifact,
)

if TYPE_CHECKING:
    from brain.systems.external_agents.service import AgentBridgePrincipal


async def get_inbound_thread(
    session: AsyncSession,
    principal: AgentBridgePrincipal,
    *,
    connection_id: str,
    event_id: str,
    limit: int,
) -> dict[str, Any]:
    if connection_id != str(principal.connection_id):
        return {
            "event_id": event_id,
            "state": InboundSubmissionResultState.NOT_VISIBLE_TO_CONNECTION.value,
            "owned_by_another_connection": True,
        }
    result = await read_inbound_submission_result(
        session,
        org_id=principal.org_id,
        connection_id=principal.connection_id,
        event_id=event_id,
    )
    if result.mutated_inbound:
        await session.commit()
    if result.state is InboundSubmissionResultState.NOT_FOUND:
        raise ValueError("Inbound event not found")
    if result.state is InboundSubmissionResultState.NOT_VISIBLE_TO_CONNECTION:
        return {
            "event_id": event_id,
            "state": result.state.value,
            "owned_by_another_connection": True,
        }
    assert result.payload is not None
    payload = project_inbound_submission_result(result.payload)
    event = payload["event"]
    submission = event.get("normalized_payload") or event.get("raw_payload") or {}
    thread_id = f"inbound:{connection_id}:{event_id}"
    title = submission.get("task_title") or "Inbound submission"
    message_limit = max(1, min(int(limit), 100))
    messages = [{
        "id": event_id,
        "idea_id": thread_id,
        "role": "user",
        "content": submission.get("message") or "",
        "attachments": submission.get("attachments") or [],
        "created_at": event.get("created_at"),
    }]
    # Retries share this correlation key. Join the run to retain the org boundary.
    artifacts = (await session.scalars(
        select(AgentRunArtifactRow)
        .join(AgentRunRow, AgentRunRow.id == AgentRunArtifactRow.run_id)
        .where(
            AgentRunRow.org_id == principal.org_id,
            AgentRunRow.thread_id == thread_id,
            AgentRunArtifactRow.visibility == "public",
        )
        .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
        .limit(message_limit)
    )).all()
    failures = await public_failures_for_run_ids(session, {row.run_id for row in artifacts})
    for row in reversed(artifacts):
        artifact = serialize_public_run_artifact(row, failures.get(row.run_id))
        messages.append({
            "id": f"run-artifact:{row.id}",
            "idea_id": thread_id,
            "role": "assistant",
            "content": artifact["text"] or artifact["title"] or "",
            "attachments": [] if row.artifact_type == "final_answer" else [artifact],
            "metadata": {"run_id": row.run_id},
            "created_at": artifact["created_at"],
        })
    # The reconciled event can also supply an answer when no answer artifact is available.
    if payload.get("final_answer") and not any(
        row.artifact_type == "final_answer" for row in artifacts
    ):
        messages.append({
            "id": f"inbound-answer:{event_id}",
            "idea_id": thread_id,
            "role": "assistant",
            "content": payload["final_answer"],
            "attachments": [],
            "metadata": {"run_id": payload.get("run_id")},
            "created_at": payload.get("completed_at") or event.get("processed_at"),
        })
    messages.sort(key=lambda message: message["created_at"] or "")
    return {
        "idea": {
            "id": thread_id,
            "thread_id": thread_id,
            "title": title,
            "description": submission.get("message"),
            "status": payload.get("handling_status") or event.get("status"),
            "created_at": event.get("created_at"),
            "updated_at": event.get("processed_at"),
        },
        "thread_reference": {
            "type": "thread_reference",
            "object_type": "thread",
            "object_id": thread_id,
            "thread_id": thread_id,
            "title": title,
            "status": "available",
        },
        "messages": messages[-message_limit:],
    }
