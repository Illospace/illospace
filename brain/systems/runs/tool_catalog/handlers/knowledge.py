"""AgentRun handler for the knowledge index primitive."""

from __future__ import annotations

import json
from typing import Any

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.knowledge.search import search_knowledge


async def _handle_search_knowledge(
    query: str,
    sources: list[str] | None = None,
    kinds: list[str] | None = None,
    limit: int = 10,
) -> str:
    clean_query = str(query or "").strip()
    if not clean_query:
        return json.dumps({"error": "search_knowledge requires query"})
    async with UnitOfWork() as uow:
        result = await search_knowledge(
            uow.session,
            clean_query,
            sources=sources,
            kinds=kinds,
            limit=max(1, min(int(limit or 10), 50)),
        )
    return json.dumps(result, default=str)


__all__ = ["_handle_search_knowledge"]
