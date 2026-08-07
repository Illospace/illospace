"""Project Context payload merge helpers."""
from __future__ import annotations

import copy
import json
from typing import Any

from brain.contracts.github import parse_github_repo_slug


def project_resource_identity(resource: dict[str, Any]) -> str:
    for key in ("repo", "uri", "url", "remote"):
        value = resource.get(key)
        if isinstance(value, str):
            slug = parse_github_repo_slug(value)
            if slug:
                return f"github_repo:{slug.lower()}"
    for key in ("path", "uri", "repo", "name", "label"):
        value = resource.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}:{value.strip()}"
    return json.dumps(resource, sort_keys=True, default=str)


def merge_project_context_resources(
    base: dict[str, Any] | None,
    addition: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return base Project identity with additional resources appended.

    Project Context merges are intentionally asymmetric: the base context owns
    durable identity and top-level metadata, while the addition can contribute
    resource references gathered later in the thread.
    """

    if not base:
        return copy.deepcopy(addition) if addition else None
    if not addition:
        return copy.deepcopy(base)

    merged = copy.deepcopy(base)
    resources = [
        dict(resource)
        for resource in (merged.get("resources") or [])
        if isinstance(resource, dict)
    ]
    seen = {project_resource_identity(resource) for resource in resources}
    for resource in addition.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        key = project_resource_identity(resource)
        if key in seen:
            continue
        seen.add(key)
        resources.append(copy.deepcopy(resource))
    merged["resources"] = resources
    merged.setdefault("source", "cortex-thread-message")
    if addition.get("source"):
        sources = [
            part.strip()
            for item in [merged.get("source"), addition.get("source")]
            if isinstance(item, str) and item.strip()
            for part in item.split("+")
            if part.strip()
        ]
        if sources:
            merged["source"] = "+".join(dict.fromkeys(sources))
    return merged


__all__ = [
    "merge_project_context_resources",
    "project_resource_identity",
]
