"""Canonical Project Context resolution for thread and run entrypoints."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.idea import IdeaProjectAttachment, IdeaThread, ProjectProfile
from brain.systems.cortex.project_context.identity import stamped_project_context
from brain.systems.cortex.project_context.merge import merge_project_context_resources
from brain.systems.cortex.project_context.snapshot import (
    ProjectContextValidationError,
    validated_project_context_snapshot,
)


@dataclass(frozen=True)
class ProjectContextResolution:
    project_context: dict[str, Any]
    snapshot: dict[str, Any] | None
    validation_errors: list[dict[str, Any]] = field(default_factory=list)


def project_context_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    for key in ("project_context", "project_context_snapshot"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            return dict(candidate)
    return {}


def project_context_from_idea(idea: Any) -> dict[str, Any]:
    details = getattr(idea, "agent_details", None)
    if not isinstance(details, dict):
        return {}
    return project_context_from_payload(details)


def snapshot_for_project_context(project_context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    if not project_context:
        return None, []
    try:
        return validated_project_context_snapshot(project_context, validate_local_paths=False), []
    except ProjectContextValidationError as exc:
        return None, exc.errors
    except Exception:
        return None, ["Project Context could not be validated."]


async def latest_attached_project_context(session: Any, idea_id: str) -> dict[str, Any]:
    if not hasattr(session, "scalars"):
        return {}
    try:
        result = await session.scalars(
            select(IdeaProjectAttachment)
            .where(
                IdeaProjectAttachment.idea_id == idea_id,
                IdeaProjectAttachment.status != "invalid",
            )
            .order_by(IdeaProjectAttachment.created_at.desc(), IdeaProjectAttachment.id.desc())
        )
        attachment = result.first()
    except Exception:
        return {}

    project_id = getattr(attachment, "project_profile_id", None)
    if project_id and hasattr(session, "get"):
        try:
            profile = await session.get(ProjectProfile, project_id)
        except Exception:
            profile = None
        if profile is not None and getattr(profile, "active", True) is not False:
            project_context = getattr(profile, "project_context", None)
            if isinstance(project_context, dict):
                return stamped_project_context(profile, project_context, project_id=project_id)

    snapshot = getattr(attachment, "snapshot", None)
    return dict(snapshot) if isinstance(snapshot, dict) else {}


async def latest_user_thread_metadata(session: Any, idea_id: str) -> dict[str, Any]:
    if not hasattr(session, "execute"):
        return {}
    result = await session.execute(
        select(IdeaThread.metadata_)
        .where(IdeaThread.idea_id == idea_id, IdeaThread.role == "user")
        .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    return dict(latest or {}) if isinstance(latest, dict) else {}


def merge_project_context_metadata(
    base_metadata: dict[str, Any],
    metadata: Any,
) -> dict[str, Any]:
    effective = dict(base_metadata)
    if not isinstance(metadata, dict):
        return effective
    for key, value in metadata.items():
        if value is None:
            continue
        if key == "project_context" and isinstance(value, dict):
            thread_project_context = effective.get("project_context")
            if isinstance(thread_project_context, dict):
                effective[key] = merge_project_context_resources(thread_project_context, value)
            else:
                effective[key] = value
        else:
            effective[key] = value
    return effective


async def resolve_effective_project_context(
    session: Any,
    *,
    idea: Any,
    idea_id: str,
    metadata: dict[str, Any],
) -> ProjectContextResolution:
    validation_errors: list[dict[str, Any]] = []
    metadata_candidate = project_context_from_payload(metadata)
    attachment_candidate = await latest_attached_project_context(session, idea_id)
    if metadata_candidate and attachment_candidate:
        candidate = merge_project_context_resources(attachment_candidate, metadata_candidate)
        snapshot, errors = snapshot_for_project_context(candidate or {})
        if snapshot and candidate:
            return ProjectContextResolution(candidate, snapshot, validation_errors)
        validation_errors.append({"source": "metadata+latest_attachment", "errors": errors})

    for source, candidate in (
        ("metadata", metadata_candidate),
        ("latest_attachment", attachment_candidate),
        ("idea", project_context_from_idea(idea)),
    ):
        if not candidate:
            continue
        snapshot, errors = snapshot_for_project_context(candidate)
        if snapshot:
            return ProjectContextResolution(candidate, snapshot, validation_errors)
        validation_errors.append({"source": source, "errors": errors})

    return ProjectContextResolution({}, None, validation_errors)


__all__ = [
    "ProjectContextResolution",
    "latest_attached_project_context",
    "latest_user_thread_metadata",
    "merge_project_context_metadata",
    "project_context_from_idea",
    "project_context_from_payload",
    "resolve_effective_project_context",
    "snapshot_for_project_context",
]
