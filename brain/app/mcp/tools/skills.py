"""Compatibility exports for MCP skill tools."""
from __future__ import annotations

from brain.app.mcp.tools.skill_common import SKILL_VIEW_SECTIONS
from brain.app.mcp.tools.skill_loading import skill_asset_tool, skill_view_tool
from brain.app.mcp.tools.skill_planning import brain_skills_tool

__all__ = [
    "SKILL_VIEW_SECTIONS",
    "brain_skills_tool",
    "skill_asset_tool",
    "skill_view_tool",
]
