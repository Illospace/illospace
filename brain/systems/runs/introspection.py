"""Shared metadata helpers for questions that require hidden tool-backed context."""
from __future__ import annotations

import re

from brain.systems.runs.tool_catalog.registry import get_tool_registration

_PERSON_ACTIVITY_PATTERNS = (
    re.compile(
        r"\bwhat(?:'s| is| are| has| have| was| were| did| does)\s+"
        r"[^?]{1,80}\s+(?:working on|been working on|doing|up to)\b"
    ),
    re.compile(
        r"\bwho\s+(?:is|are|has|have|was|were)\s+[^?]{0,60}\b"
        r"(?:working|active)\b"
    ),
)
_WORKSPACE_ACTIVITY_PHRASES = (
    "active cortex",
    "active runs",
    "active thoughts",
    "latest activity",
    "latest shared activity",
    "latest signals",
    "recent activity",
    "team activity",
    "teammate activity",
    "workspace activity",
)
_WORKSPACE_OVERVIEW_PHRASES = (
    "finish setting up this workspace",
    "help me understand this workspace",
    "help me understand what you can do to help me",
    "help me finish setting up this workspace",
    "introduce yourself",
    "what can you do to help me",
    "what can you see",
    "what do you know about this workspace",
    "what you should know about the team",
    "what is this workspace",
    "workspace overview",
    "workspace setup",
)
_PROJECT_CONTEXT_PHRASES = (
    "connected repo",
    "connected repos",
    "connected docs",
    "project context",
    "project contexts",
    "what projects",
)
_WORKSPACE_RECORD_PHRASES = (
    "domain records",
    "structured records",
    "team database",
    "workspace records",
)


def _normalized_text(message: str | None) -> str:
    return " ".join((message or "").strip().lower().split())


def _looks_like_workspace_activity_question(message: str | None) -> bool:
    text = _normalized_text(message)
    if not text:
        return False
    if any(pattern.search(text) for pattern in _PERSON_ACTIVITY_PATTERNS):
        return True
    return any(phrase in text for phrase in _WORKSPACE_ACTIVITY_PHRASES)


def _looks_like_any_phrase(message: str | None, phrases: tuple[str, ...]) -> bool:
    text = _normalized_text(message)
    if not text:
        return False
    return any(phrase in text for phrase in phrases)


def required_introspection_tool(
    message: str | None = None,
    *,
    explicit_tool: str | None = None,
) -> tuple[str | None, str | None]:
    """Return a mandatory hidden-context tool from explicit routing metadata.

    Explicit metadata is the preferred path. The small heuristic below is a guardrail
    for high-risk freshness questions where answering from memory produces stale
    workspace status.
    """
    tool = _normalized_text(explicit_tool)
    registration = get_tool_registration(tool) if tool else None
    if registration and registration.context_route is not None:
        route = registration.context_route
        return tool, f"This question requires {tool}. {route.description}"
    if _looks_like_any_phrase(message, _WORKSPACE_OVERVIEW_PHRASES):
        tool = "read_workspace_overview"
        registration = get_tool_registration(tool)
        if registration and registration.context_route is not None:
            route = registration.context_route
            return (
                tool,
                f"This question asks for a current workspace overview or setup status. Use {tool} "
                f"before answering from memory. {route.description}",
            )
    if _looks_like_any_phrase(message, _PROJECT_CONTEXT_PHRASES):
        tool = "read_project_contexts"
        registration = get_tool_registration(tool)
        if registration and registration.context_route is not None:
            route = registration.context_route
            return (
                tool,
                f"This question asks what project context is connected. Use {tool} "
                f"before answering from memory. {route.description}",
            )
    if _looks_like_any_phrase(message, _WORKSPACE_RECORD_PHRASES):
        tool = "read_workspace_records"
        registration = get_tool_registration(tool)
        if registration and registration.context_route is not None:
            route = registration.context_route
            return (
                tool,
                f"This question asks about structured workspace records. Use {tool} "
                f"before answering from memory. {route.description}",
            )
    if _looks_like_workspace_activity_question(message):
        tool = "read_team_activity"
        registration = get_tool_registration(tool)
        if registration and registration.context_route is not None:
            route = registration.context_route
            return (
                tool,
                f"This question asks for current workspace/team activity. Use {tool} "
                f"with a recent time window and person filter when relevant before "
                f"answering from memory. {route.description}",
            )
    return None, None
