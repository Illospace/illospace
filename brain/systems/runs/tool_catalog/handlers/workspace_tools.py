"""Workspace tool installer handlers."""

from __future__ import annotations

import json
from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import *


def _workspace_tools_context() -> tuple[str | None, str | None]:
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    org_id = getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id")
    user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")
    return (str(org_id) if org_id else None, str(user_id) if user_id else None)


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def _error(message: str) -> str:
    return _dump({"error": message})


async def _handle_manage_workspace_tools(
    action: str = "list",
    operation: str | None = None,
    bundle_id: str | None = None,
) -> str:
    action = str(action or "list").strip().lower()
    if action in {"help", "schema"}:
        return _manage_tool_guide("manage_workspace_tools", operation)

    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.runtime_settings.workspace_tools import (
        async_check_workspace_tool,
        async_get_workspace_tools_status,
        async_install_workspace_tool,
        workspace_tool_catalog,
    )

    if action == "catalog":
        return _dump({
            "action": action,
            "catalog": [bundle.model_dump(mode="json") for bundle in workspace_tool_catalog()],
        })

    org_id, user_id = _workspace_tools_context()
    if not org_id:
        return _error("manage_workspace_tools could not access this workspace context")

    try:
        async with UnitOfWork() as uow:
            session = uow.session
            if action in {"list", "status"}:
                status = await async_get_workspace_tools_status(
                    session,
                    org_id=org_id,
                    bundle_id=bundle_id,
                )
                return _dump({"action": action, **status.model_dump(mode="json")})

            if action == "install":
                if not user_id:
                    return _error("install requires authenticated user context")
                if not bundle_id:
                    return _error("install requires: bundle_id")
                status = await async_install_workspace_tool(
                    session,
                    org_id=org_id,
                    bundle_id=bundle_id,
                    requested_by=user_id,
                )
                return _dump({"action": action, **status.model_dump(mode="json")})

            if action == "check":
                if not bundle_id:
                    return _error("check requires: bundle_id")
                status = await async_check_workspace_tool(
                    session,
                    org_id=org_id,
                    bundle_id=bundle_id,
                )
                return _dump({"action": action, **status.model_dump(mode="json")})
    except Exception as exc:
        logger.exception("manage_workspace_tools failed: %s", exc)
        return _error(str(exc))

    return _error(f"Unknown manage_workspace_tools action: {action}")


__all__ = ["_handle_manage_workspace_tools"]
