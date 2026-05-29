"""Shared metadata helpers for questions that require hidden tool-backed context."""
from __future__ import annotations

import re

from brain.systems.runs.capabilities import (
    builtin_capability_manifests,
    custom_capability_manifests,
    registry_capability_manifests,
)
from brain.systems.runs.execution_context import _agent_context
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
    "help me finish setting up this workspace",
    "introduce yourself",
    "what can you see",
    "what do you know about this workspace",
    "what you should know about the team",
    "what is this workspace",
    "workspace overview",
    "workspace setup",
)
_SELF_CONTEXT_PATTERNS = (
    re.compile(r"\b(?:who are you|what is illo|what is illospace|what are you)\b"),
    re.compile(r"\bwhere\b[^?]{0,100}\b(?:installed|running|hosted|source|code|repo|repository)\b"),
    re.compile(r"\b(?:open[- ]source repo|github repo|source code|your code|own code|inspect yourself)\b"),
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
_CAPABILITY_SETUP_PATTERNS = (
    re.compile(
        r"\b(?:set\s+(?:you|illo|it|this|that|me|us|them)\s+up|set\s*up|setup|connect|integrate|install|configure|enable|add)\b"
    ),
    re.compile(
        r"\b(?:set\s*up|setup|connect|integrate|install|configure|enable|add)\b"
        r"[^?]{0,100}\b(?:you|illo|agent|integration|connector|capability|tool|plugin|app|mcp)\b"
    ),
    re.compile(
        r"\b(?:you|illo|agent|integration|connector|capability|tool|plugin|app|mcp)\b"
        r"[^?]{0,100}\b(?:set\s*up|setup|connect|integrate|install|configure|enable|add)\b"
    ),
    re.compile(r"\b(?:what|which)\b[^?]{0,80}\b(?:capabilities|integrations|connectors|plugins|tools)\b"),
    re.compile(r"\b(?:what|which)\b[^?]{0,80}\b(?:can|could)\b[^?]{0,40}\b(?:you|illo)\b[^?]{0,40}\b(?:do|help)\b"),
    re.compile(r"\b(?:what|which)\b[^?]{0,40}\b(?:you|illo)\b[^?]{0,40}\b(?:can|could)\b[^?]{0,40}\b(?:do|help)\b"),
    re.compile(r"\bhelp\b[^?]{0,80}\b(?:what|which)\b[^?]{0,40}\b(?:can|could)\b[^?]{0,40}\b(?:you|illo)\b[^?]{0,40}\b(?:do|help)\b"),
    re.compile(r"\bhelp\b[^?]{0,80}\b(?:what|which)\b[^?]{0,40}\b(?:you|illo)\b[^?]{0,40}\b(?:can|could)\b[^?]{0,40}\b(?:do|help)\b"),
)
_CAPABILITY_CONTEXT_PATTERN_COUNT = 3


def _normalized_text(message: str | None) -> str:
    return " ".join((message or "").strip().lower().split())


def _looks_like_workspace_activity_question(message: str | None) -> bool:
    text = _normalized_text(message)
    if not text:
        return False
    if any(pattern.search(text) for pattern in _PERSON_ACTIVITY_PATTERNS):
        return True
    return any(phrase in text for phrase in _WORKSPACE_ACTIVITY_PHRASES)


def _looks_like_capability_setup_question(message: str | None) -> bool:
    text = _normalized_text(message)
    if not text:
        return False
    setup_verb = bool(_CAPABILITY_SETUP_PATTERNS[0].search(text))
    context_patterns = _CAPABILITY_SETUP_PATTERNS[1:_CAPABILITY_CONTEXT_PATTERN_COUNT]
    if any(pattern.search(text) for pattern in context_patterns):
        return True
    if any(pattern.search(text) for pattern in _CAPABILITY_SETUP_PATTERNS[_CAPABILITY_CONTEXT_PATTERN_COUNT:]):
        return True
    return setup_verb and _mentions_known_capability(text)


def _looks_like_self_context_question(message: str | None) -> bool:
    text = _normalized_text(message)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SELF_CONTEXT_PATTERNS)


def _agent_context_containers() -> list[dict]:
    containers: list[dict] = []
    for attr in ("target_ref", "workspace_ref"):
        value = getattr(_agent_context, attr, None)
        if isinstance(value, dict):
            containers.append(value)
    metadata = getattr(_agent_context, "execution_metadata", None)
    if isinstance(metadata, dict):
        containers.append(metadata)
        for key in ("target_ref", "workspace_ref"):
            value = metadata.get(key)
            if isinstance(value, dict):
                containers.append(value)
    return containers


def _known_capability_terms() -> tuple[str, ...]:
    manifests = [
        *registry_capability_manifests(),
        *builtin_capability_manifests(),
        *custom_capability_manifests(*_agent_context_containers()),
    ]
    terms: set[str] = set()
    for manifest in manifests:
        terms.update((manifest.key.lower(), manifest.name.lower()))
        terms.update(alias.lower() for alias in manifest.aliases)
    return tuple(sorted(term for term in terms if len(term) >= 2))


def _mentions_known_capability(text: str) -> bool:
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text)
        for term in _known_capability_terms()
    )


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
    if _looks_like_self_context_question(message):
        tool = "read_self_context"
        registration = get_tool_registration(tool)
        if registration and registration.context_route is not None:
            route = registration.context_route
            return (
                tool,
                f"This question asks for Illo's verified identity/source/runtime context. Use {tool} "
                f"before answering from memory. {route.description}",
            )
    if _looks_like_capability_setup_question(message):
        tool = "read_capabilities"
        registration = get_tool_registration(tool)
        if registration and registration.context_route is not None:
            route = registration.context_route
            return (
                tool,
                f"This question asks for Illo's current capability/setup context. Use {tool} "
                f"before answering from memory. {route.description}",
            )
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
