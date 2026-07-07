"""Shared metadata helpers for questions that require hidden tool-backed context."""
from __future__ import annotations

from brain.systems.runs.message_metadata import (
    INTROSPECTION_MESSAGE_METADATA_KEYS,
    extract_latest_user_intent,
)
from brain.systems.runs.tool_catalog.registry import get_tool_registration


def _normalized_text(message: str | None) -> str:
    return " ".join((message or "").strip().lower().split())


def message_for_required_introspection(
    message: str | None,
    metadata: dict | None = None,
) -> str | None:
    """Prefer the original human text when a trigger decorates the run message."""
    if isinstance(metadata, dict):
        for key in INTROSPECTION_MESSAGE_METADATA_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return extract_latest_user_intent(message)


def required_introspection_tool(
    message: str | None = None,
    *,
    explicit_tool: str | None = None,
) -> tuple[str | None, str | None]:
    """Return a mandatory hidden-context tool ONLY from an explicit routing directive.

    There is deliberately NO heuristic/keyword forcing. A competent model calls the
    context tools it needs — read_self_context, read_capabilities, read_team_activity,
    read_workspace_overview, read_project_contexts, read_workspace_records — voluntarily,
    because they are in its registry and its tool descriptions point at them.

    End-of-turn forcing of *guessed* tools was the source of the recurring
    "introspection hijack": a regex/keyword match on an ordinary work request (e.g.
    "set … up", "where … source", "what is X working on") force-injected a tool whose
    output then replaced the model's completed answer with the wrong thing (issue #249
    and ~30% of production runs). Only an explicit ``required_introspection_tool``
    metadata directive is honored here; nothing in the app currently sets one, so in
    practice this never forces. The ``message`` argument is retained for call-site
    compatibility.
    """
    tool = _normalized_text(explicit_tool)
    if not tool:
        return None, None
    registration = get_tool_registration(tool)
    if registration and registration.context_route is not None:
        route = registration.context_route
        return tool, f"This question requires {tool}. {route.description}"
    return None, None
