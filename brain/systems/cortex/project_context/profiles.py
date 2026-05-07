"""Project Context persistence read-model adapters."""
from __future__ import annotations

from brain.systems.cortex.project_context.schemas import IdeaProjectAttachmentRead, ProjectProfileRead
from brain.platform.db.models.idea import IdeaProjectAttachment, ProjectProfile


def profile_to_read(profile: ProjectProfile) -> ProjectProfileRead:
    return ProjectProfileRead.model_validate({
        "id": profile.id,
        "org_id": profile.org_id,
        "user_id": profile.user_id,
        "slug": profile.slug,
        "name": profile.name,
        "description": profile.description,
        "project_context": profile.project_context,
        "default_environment_binding_id": profile.default_environment_binding_id,
        "active": True if profile.active is None else profile.active,
        "metadata": profile.metadata_ or {},
        "created_at": profile.created_at,
    })


def attachment_to_read(attachment: IdeaProjectAttachment) -> IdeaProjectAttachmentRead:
    return IdeaProjectAttachmentRead.model_validate({
        "id": attachment.id,
        "idea_id": attachment.idea_id,
        "project_profile_id": attachment.project_profile_id,
        "snapshot": attachment.snapshot,
        "permission_scope": attachment.permission_scope or {},
        "status": attachment.status,
        "validation_errors": attachment.validation_errors or [],
        "environment_binding_id": attachment.environment_binding_id,
        "metadata": attachment.metadata_ or {},
        "created_at": attachment.created_at,
    })
