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
)
from brain.systems.runs.tool_catalog.handlers.common import _agent_context


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


def _handle_read_capabilities(
    query: str | None = None,
    capability_key: str | None = None,
    category: str | None = None,
    include_setup_guide: bool = False,
) -> str:
    """Read machine-readable capability manifests available in this run."""

    manifests = merge_capability_manifests([
        *builtin_capability_manifests(),
        *custom_capability_manifests(*_context_containers()),
    ])
    matches = filter_capability_manifests(
        manifests,
        query=query,
        capability_key=capability_key,
        category=category,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "source": "runtime_capability_registry",
        "query": query,
        "capability_key": capability_key,
        "category": category,
        "count": len(matches),
        "capabilities": [manifest.to_payload() for manifest in matches],
        "answering_guidance": [
            "Use capability manifests and tool schemas as the authority for what Illo can inspect, do, or guide.",
            "For setup requests, match the capability, inspect its status_check tool when present, then use its setup guide when available.",
            "For custom capabilities, rely on the provided manifest fields instead of general model assumptions.",
        ],
    }
    if include_setup_guide:
        guide_targets = matches if capability_key or len(matches) == 1 else []
        payload["setup_guides"] = [
            guide
            for guide in (load_setup_guide(manifest) for manifest in guide_targets)
            if guide is not None
        ]
    return json.dumps(payload, default=str)


__all__ = ["_handle_read_capabilities"]
