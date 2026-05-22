"""Stable Project identity helpers."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


PROJECT_IDENTITY_FIELDS = (
    "project_key",
    "project_id",
    "slug",
)


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def project_context_identity(
    *,
    project_id: Any,
    slug: Any = None,
    name: Any = None,
    description: Any = None,
) -> dict[str, str]:
    """Return canonical identity fields for a persisted Project."""

    project_id_text = _clean_text(str(project_id)) if project_id is not None else None
    slug_text = _clean_text(slug)
    name_text = _clean_text(name)
    description_text = _clean_text(description)
    identity: dict[str, str] = {}
    if project_id_text:
        identity.update({
            "project_id": project_id_text,
            "project_key": project_id_text,
        })
    if slug_text:
        identity["slug"] = slug_text
    if name_text:
        identity["name"] = name_text
    if description_text:
        identity["description"] = description_text
    return identity


def stamp_project_identity(
    project_context: Mapping[str, Any] | None,
    *,
    project_id: Any,
    slug: Any = None,
    name: Any = None,
    description: Any = None,
) -> dict[str, Any]:
    """Stamp a Project context with its durable Project identity."""

    context = dict(project_context or {})
    context.update(
        project_context_identity(
            project_id=project_id,
            slug=slug,
            name=name,
            description=description,
        )
    )
    return context


def stamped_project_context(
    project: Any,
    project_context: Mapping[str, Any] | None = None,
    *,
    project_id: Any = None,
) -> dict[str, Any]:
    """Return a Project context stamped with its durable Project identity."""

    source = project_context if isinstance(project_context, Mapping) else getattr(project, "project_context", None)
    context = dict(source) if isinstance(source, Mapping) else {}
    return stamp_project_identity(
        context,
        project_id=project_id if project_id is not None else getattr(project, "id", None),
        slug=getattr(project, "slug", None),
        name=getattr(project, "name", None),
        description=getattr(project, "description", None),
    )


__all__ = [
    "PROJECT_IDENTITY_FIELDS",
    "project_context_identity",
    "stamp_project_identity",
    "stamped_project_context",
]
