"""Capability discovery handlers for Illo's runtime self model."""

from __future__ import annotations

import json
from typing import Any

from brain.systems.runs.capabilities import (
    builtin_capability_manifests,
    custom_capability_manifests,
    filter_capability_manifests,
    load_setup_guide,
    merge_capability_manifests,
    normalize_capability_manifests,
    registry_capability_manifests,
)
from brain.systems.runs.tool_policy import disabled_tool_names_from_metadata
from brain.systems.runs.tool_catalog.handlers.common import _agent_context


_FILTER_FIELDS = ("capability_key", "category")


def _context_containers() -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
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


def _available_registry_tool_names() -> set[str] | None:
    metadata = getattr(_agent_context, "execution_metadata", None)
    disabled = disabled_tool_names_from_metadata(metadata)
    if not disabled:
        return None
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations

    return set(all_tool_registrations()) - disabled


def _registered_registry_tool_names() -> set[str]:
    from brain.systems.runs.tool_catalog.registry import all_tool_registrations

    return set(all_tool_registrations())


def _resolved_detail_level(
    requested: str | None,
    *,
    matches: list,
    capability_key: str | None,
    include_setup_guide: bool,
) -> str:
    detail = str(requested or "auto").strip().lower()
    if detail in {"summary", "tools", "full"}:
        return detail
    if capability_key or include_setup_guide or len(matches) == 1:
        return "full"
    return "summary"


def _filter_with_fallbacks(
    manifests: list,
    *,
    query: str | None,
    capability_key: str | None,
    category: str | None,
) -> tuple[list, list[str]]:
    matches = filter_capability_manifests(
        manifests,
        query=query,
        capability_key=capability_key,
        category=category,
    )
    if matches:
        return matches, []

    ignored: list[str] = []
    if category:
        matches = filter_capability_manifests(
            manifests,
            query=query,
            capability_key=capability_key,
        )
        if matches:
            ignored.append("category")
            return matches, ignored

    if capability_key:
        matches = filter_capability_manifests(
            manifests,
            query=query,
            category=category,
        )
        if matches:
            ignored.append("capability_key")
            return matches, ignored

    if query:
        matches = filter_capability_manifests(manifests, query=query)
        if matches:
            ignored.extend(
                field for field, value in (
                    ("capability_key", capability_key),
                    ("category", category),
                )
                if value
            )
            return matches, ignored

    return matches, ignored


def _handle_read_capabilities(
    query: str | None = None,
    capability_key: str | None = None,
    category: str | None = None,
    include_setup_guide: bool = False,
    detail_level: str | None = "auto",
) -> str:
    """Read machine-readable capability manifests available in this run."""

    available_tools = _available_registry_tool_names()
    registered_tools = _registered_registry_tool_names()
    manifests = merge_capability_manifests([
        *registry_capability_manifests(available_tool_names=available_tools),
        *normalize_capability_manifests(
            [
                *builtin_capability_manifests(),
                *custom_capability_manifests(*_context_containers()),
            ],
            available_tool_names=available_tools,
            registered_tool_names=registered_tools,
        ),
    ])
    matches, ignored_filters = _filter_with_fallbacks(
        manifests,
        query=query,
        capability_key=capability_key,
        category=category,
    )
    resolved_detail = _resolved_detail_level(
        detail_level,
        matches=matches,
        capability_key=capability_key,
        include_setup_guide=include_setup_guide,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "source": "runtime_capability_registry",
        "query": query,
        "detail_level": resolved_detail,
        "count": len(matches),
        "ignored_filters": ignored_filters,
        "capabilities": [
            manifest.to_payload(detail_level=resolved_detail)
            for manifest in matches
        ],
        "answering_guidance": [
            "Use capability manifests and tool schemas as the authority for what Illo can inspect, do, or guide.",
            "Use summary results as a capability index; single matches include setup/status fields when they exist.",
            "Setup guides are optional extra documentation; when no setup guide is returned, use the capability setup/status fields.",
            "For custom capabilities, rely on the provided manifest fields instead of general model assumptions.",
        ],
    }
    for field in _FILTER_FIELDS:
        payload[f"{field}_ignored"] = field in ignored_filters
    if include_setup_guide:
        guide_targets = matches if capability_key or len(matches) == 1 else []
        setup_guides = [
            guide
            for guide in (load_setup_guide(manifest) for manifest in guide_targets)
            if guide is not None
        ]
        if setup_guides:
            payload["setup_guides"] = setup_guides
    return json.dumps(payload, default=str)


__all__ = ["_handle_read_capabilities"]
