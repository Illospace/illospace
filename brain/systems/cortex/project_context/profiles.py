"""Project Context persistence read-model adapters."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brain.kernel import config
from brain.systems.cortex.project_context.access import project_profile_visibility
from brain.systems.cortex.project_context.drafts import IGNORED_DRAFT_DIRS
from brain.systems.cortex.project_context.identity import stamped_project_context
from brain.systems.cortex.project_context.project_root import project_key_from_context, project_root_path
from brain.systems.cortex.project_context.schemas import IdeaProjectAttachmentRead, ProjectProfileRead
from brain.systems.cortex.project_context.workspace_manifest import PROJECT_CONTEXT_DIR
from brain.platform.db.models.idea import IdeaProjectAttachment, ProjectProfile


PROFILE_CONTENT_FILE_COUNT_LIMIT = 1_000
_PROFILE_CONTENT_INTERNAL_DIRS = {*IGNORED_DRAFT_DIRS, PROJECT_CONTEXT_DIR, ".git"}
_REPO_RESOURCE_KINDS = {"github", "github_repo", "github_repository", "repo", "repository"}


def _profile_context_for_read(profile: ProjectProfile) -> dict[str, Any]:
    return stamped_project_context(profile)


def _profile_resources(project_context: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in (project_context.get("resources") or []) if isinstance(item, Mapping)]


def _is_repo_resource(resource: Mapping[str, Any]) -> bool:
    kind = str(resource.get("kind") or resource.get("type") or resource.get("resource_type") or "").strip().lower()
    source = str(resource.get("source") or resource.get("provider") or "").strip().lower()
    uri = str(resource.get("uri") or resource.get("url") or resource.get("repo_url") or "").strip().lower()
    return (
        kind in _REPO_RESOURCE_KINDS
        or source in {"github", "git"}
        or bool(resource.get("repo"))
        or uri.startswith(("git@", "https://github.com/", "http://github.com/"))
        or uri.endswith(".git")
    )


def _count_project_root_files(root: Path, *, limit: int = PROFILE_CONTENT_FILE_COUNT_LIMIT) -> tuple[int, bool]:
    if not root.exists() or not root.is_dir():
        return 0, True

    count = 0
    try:
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if any(part in _PROFILE_CONTENT_INTERNAL_DIRS for part in relative.parts):
                continue
            if path.is_symlink():
                continue
            if path.is_file():
                count += 1
                if count >= limit:
                    return count, False
    except OSError:
        return count, False
    return count, True


def _profile_content_summary(profile: ProjectProfile, project_context: Mapping[str, Any]) -> dict[str, Any]:
    resources = _profile_resources(project_context)
    project_key = project_key_from_context(
        project_context,
        resources=resources,
        fallback=getattr(profile, "slug", None) or getattr(profile, "id", None),
    )
    workspace_root = config.resolve_workspace_root(default=config.BRAIN_DIR.parent).expanduser()
    root = project_root_path(workspace_root, project_key)
    file_count, file_count_exact = _count_project_root_files(root)
    return {
        "root_exists": root.exists() and root.is_dir(),
        "file_count": file_count,
        "file_count_exact": file_count_exact,
        "repo_count": sum(1 for resource in resources if _is_repo_resource(resource)),
        "resource_count": len(resources),
    }


def profile_to_read(profile: ProjectProfile, access: list[dict[str, Any]] | None = None) -> ProjectProfileRead:
    project_context = _profile_context_for_read(profile)
    return ProjectProfileRead.model_validate({
        "id": profile.id,
        "org_id": profile.org_id,
        "user_id": profile.user_id,
        "slug": profile.slug,
        "name": profile.name,
        "description": profile.description,
        "project_context": project_context,
        "visibility": project_profile_visibility(profile),
        "access": access or [],
        "default_environment_binding_id": profile.default_environment_binding_id,
        "active": True if profile.active is None else profile.active,
        "content_summary": _profile_content_summary(profile, project_context),
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
