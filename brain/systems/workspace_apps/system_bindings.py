"""Gateway for workspace-app system data bindings."""
from __future__ import annotations

from typing import Any


async def query_system_binding_source(
    *,
    source: str,
    query: str | None,
    search: str | None,
    time_window: str,
    start_at: str | None,
    end_at: str | None,
    limit: int,
    idea_id: str | None,
    domain_id: int | None,
    object_key: str | None,
    include_archived: bool,
    user_id: str | None,
    org_id: str,
) -> dict[str, Any]:
    """Query workspace data for runtime app bindings.

    This keeps the app runtime broker dependent on a product-facing gateway,
    while the existing agent tool continues to own the current query plumbing.
    """
    from brain.systems.runs.tool_catalog.handlers.workspace_data import query_workspace_data

    return await query_workspace_data(
        sources=[source],
        query=query,
        search=search,
        time_window=time_window,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        idea_id=idea_id,
        domain_id=domain_id,
        object_key=object_key,
        include_archived=include_archived,
        user_id=user_id,
        org_id=org_id,
    )
