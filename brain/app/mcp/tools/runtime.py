"""Runtime settings MCP tool implementation."""
from __future__ import annotations

from typing import Any


async def runtime_settings_tool(
    provider: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    *,
    unit_of_work_cls: Any,
) -> dict:
    """Inspect active runtime/provider/auth settings for the current user."""
    from brain.systems.services.runtime_introspection import async_get_runtime_settings_snapshot

    async with unit_of_work_cls() as uow:
        return await async_get_runtime_settings_snapshot(
            uow.session,
            user_id=user_id,
            org_id=org_id,
            provider=provider,
        )
