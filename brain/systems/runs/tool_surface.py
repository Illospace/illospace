"""AgentRun-owned model tool surface."""

from __future__ import annotations

from typing import Any


def build_agent_tools(role: str) -> list[dict]:
    """Compose model-visible tools for an AgentRun role.

    Deep orchestration is recipe-owned state, so this surface only contains
    normal product and workspace tools.
    """
    from brain.systems.runs.direct_agent import COORDINATOR_TOOLS, WORKER_TOOLS, get_tools_with_extended

    base = COORDINATOR_TOOLS if role == "coordinator" else WORKER_TOOLS
    return get_tools_with_extended(base)


def build_tool_handlers(
    *,
    workspace_root: str | None,
    allowed_workspaces: list[str | dict] | None = None,
    reader_policy: dict[str, Any] | None = None,
) -> dict:
    """Build runtime tool handlers without exposing harness-control tools."""
    from brain.systems.runs.direct_agent import _get_tool_handlers

    handlers = _get_tool_handlers(
        workspace_root=workspace_root,
        allowed_workspaces=allowed_workspaces,
        reader_policy=reader_policy,
    )
    return dict(handlers)


__all__ = ["build_agent_tools", "build_tool_handlers"]
