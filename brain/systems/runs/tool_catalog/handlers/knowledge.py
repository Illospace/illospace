"""AgentRun handler for the knowledge index primitive."""

from __future__ import annotations

import json
from typing import Any

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.knowledge.search import (
    normalize_knowledge_search_limit,
    search_knowledge,
)
from brain.systems.runs.tool_catalog.handlers.common import _agent_context


async def _handle_search_knowledge(
    query: str,
    sources: list[str] | None = None,
    kinds: list[str] | None = None,
    limit: int = 10,
) -> str:
    clean_query = str(query or "").strip()
    if not clean_query:
        return json.dumps({"error": "search_knowledge requires query"})
    org_id = str(getattr(_agent_context, "org_id", "") or "").strip()
    if not org_id:
        return json.dumps({"error": "search_knowledge requires workspace context"})
    async with UnitOfWork() as uow:
        result = await search_knowledge(
            uow.session,
            clean_query,
            org_id=org_id,
            sources=sources,
            kinds=kinds,
            limit=normalize_knowledge_search_limit(limit),
        )
    return json.dumps(result, default=str)


__all__ = ["_handle_search_knowledge"]
