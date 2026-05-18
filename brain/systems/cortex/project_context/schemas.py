"""Project Context API transport contracts."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from brain.systems.cortex.project_context.access import PROJECT_VISIBILITY_PRIVATE
from brain.systems.cortex.project_context.resources import ProjectResource, normalize_project_resource


class ProjectProfileCreate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=120)
    name: str = Field(..., min_length=1)
    description: str | None = None
    project_context: dict[str, Any]
    visibility: str = Field(default=PROJECT_VISIBILITY_PRIVATE)
    shared_usernames: list[str] = Field(default_factory=list)
    default_environment_binding_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectProfileUpdate(BaseModel):
    slug: str | None = Field(default=None, min_length=1, max_length=120)
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    project_context: dict[str, Any] | None = None
    visibility: str | None = None
    shared_usernames: list[str] | None = None
    default_environment_binding_id: int | None = None
    active: bool | None = None
    metadata: dict[str, Any] | None = None


class ProjectProfileAccessRead(BaseModel):
    user_id: str
    name: str
    email: str | None = None
    shared_by_user_id: str | None = None
    created_at: datetime | None = None


class ProjectProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str | None = None
    user_id: str | None = None
    slug: str
    name: str
    description: str | None = None
    project_context: dict[str, Any]
    visibility: str = PROJECT_VISIBILITY_PRIVATE
    access: list[ProjectProfileAccessRead] = Field(default_factory=list)
    default_environment_binding_id: int | None = None
    active: bool = True
    metadata_: dict[str, Any] | None = Field(default=None, alias="metadata")
    created_at: datetime | None = None


class ProjectResourcesCreate(BaseModel):
    resources: list[dict[str, Any]] = Field(..., min_length=1)


class ProjectResourceUpdate(BaseModel):
    resource: dict[str, Any]
    replace: bool = False


class ProjectResourcesReorder(BaseModel):
    resource_ids: list[str] = Field(..., min_length=1)


class IdeaProjectAttachmentCreate(BaseModel):
    project_profile_id: str | None = None
    project_context: dict[str, Any] | None = None
    environment_binding_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IdeaProjectAttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    idea_id: str
    project_profile_id: str | None = None
    snapshot: dict[str, Any]
    permission_scope: dict[str, Any] | None = None
    status: str
    validation_errors: list[Any] | None = None
    environment_binding_id: int | None = None
    metadata_: dict[str, Any] | None = Field(default=None, alias="metadata")
    created_at: datetime | None = None


class GitHubVaultTokenRequest(BaseModel):
    vault_key: str = Field(..., min_length=1, max_length=255)


class GitHubRepoSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=255)
    vault_key: str | None = Field(default=None, max_length=255)


class GitHubProjectTokenBindRequest(BaseModel):
    vault_key: str = Field(..., min_length=1, max_length=255)
    repo: str = Field(..., min_length=1, max_length=255)
    env_name: str = Field(default="GH_TOKEN", min_length=1, max_length=128)


class GitHubRepoRead(BaseModel):
    full_name: str
    html_url: str
    description: str | None = None
    default_branch: str | None = None
    language: str | None = None
    topics: list[str] = Field(default_factory=list)
    private: bool = False
    permissions: dict[str, Any] = Field(default_factory=dict)


class GitHubConnectRead(BaseModel):
    login: str | None = None
    repos: list[GitHubRepoRead] = Field(default_factory=list)


class GitHubRepoSearchRead(BaseModel):
    repos: list[GitHubRepoRead] = Field(default_factory=list)
    matched_exact: bool = False


class GitHubProjectTokenBindRead(BaseModel):
    project_slug: str
    env_name: str
    binding: dict[str, Any]
    repo: GitHubRepoRead
    write_access: bool = False
