"""Compatibility shell for binding Cortex thoughts/messages to AgentRun requests."""

from __future__ import annotations

from typing import Any

from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.work_intake import build_cortex_agent_run_request


async def a_build_run_request(
    session: Any,
    *,
    idea_id: str,
    event: str,
    message: str,
    user_id: str | None,
    metadata: dict[str, Any] | None = None,
    priority: int = 0,
    source: str | None = None,
    producer: str | None = None,
    idempotency_key: str | None = None,
) -> AgentRunRequest:
    return await build_cortex_agent_run_request(
        session,
        idea_id=idea_id,
        event=event,
        message=message,
        user_id=user_id,
        metadata=metadata,
        priority=priority,
        source=source,
        producer=producer,
        idempotency_key=idempotency_key,
    )


__all__ = ["a_build_run_request"]
