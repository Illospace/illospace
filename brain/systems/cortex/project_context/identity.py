"""Stable Project identity helpers."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


PROJECT_IDENTITY_FIELDS = (
    "project_key",
    "profile_id",
    "project_id",
    "selected_profile_id",
    "id",
    "profile_slug",
    "selected_profile_slug",
    "slug",
)


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def project_profile_context_identity(
    *,
    profile_id: Any,
    slug: Any = None,
    name: Any = None,
    description: Any = None,
) -> dict[str, str]:
    """Return canonical identity fields for a persisted Project profile."""

    profile_id_text = _clean_text(str(profile_id)) if profile_id is not None else None
    slug_text = _clean_text(slug)
    name_text = _clean_text(name)
    description_text = _clean_text(description)
    identity: dict[str, str] = {}
    if profile_id_text:
        identity.update({
            "id": profile_id_text,
            "project_id": profile_id_text,
            "profile_id": profile_id_text,
            "selected_profile_id": profile_id_text,
            "project_key": profile_id_text,
        })
    if slug_text:
        identity.update({
            "slug": slug_text,
            "profile_slug": slug_text,
            "selected_profile_slug": slug_text,
        })
    if name_text:
        identity["name"] = name_text
    if description_text:
        identity["description"] = description_text
    return identity


def stamp_project_profile_identity(
    project_context: Mapping[str, Any] | None,
    *,
    profile_id: Any,
    slug: Any = None,
    name: Any = None,
    description: Any = None,
) -> dict[str, Any]:
    """Stamp a Project profile context with its durable profile identity."""

    context = dict(project_context or {})
    context.update(
        project_profile_context_identity(
            profile_id=profile_id,
            slug=slug,
            name=name,
            description=description,
        )
    )
    return context


__all__ = [
    "PROJECT_IDENTITY_FIELDS",
    "project_profile_context_identity",
    "stamp_project_profile_identity",
]
