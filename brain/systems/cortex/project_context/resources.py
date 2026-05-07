"""Durable Project Context resource normalization."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _clean_scalar(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _copy_mapping(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _copy_list(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return list(value)
    return None


@dataclass(frozen=True)
class ProjectResource:
    """Normalized backend shape for every Project Context resource.

    GitHub repos, uploaded files/folders, and backend-local paths all flow
    through this abstraction before snapshots, permission scopes, and
    materialization touch them. Unknown metadata is preserved so connector
    surfaces can evolve without forcing a database migration for every hint.
    """

    id: str
    kind: str
    label: str | None = None
    name: str | None = None
    path: str | None = None
    uri: str | None = None
    repo: str | None = None
    branch: str | None = None
    default_branch: str | None = None
    access: str | None = None
    mode: str | None = None
    source: str | None = None
    private: bool | None = None
    credential_ref: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None
    scope: dict[str, Any] | None = None
    permissions: dict[str, Any] | None = None
    allowed_paths: list[Any] | None = None
    files: list[Any] | None = None
    folders: list[Any] | None = None
    forbidden_paths: list[Any] | None = None
    denied_paths: list[Any] | None = None
    file_manifest: list[Any] | None = None
    file_count: int | None = None
    size: int | None = None
    mime: str | None = None
    last_modified: int | float | None = None
    uploaded_files: list[Any] | None = None
    uploaded_file_count: int | None = None
    upload_id: str | None = None
    relative_path: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, index: int = 0) -> "ProjectResource":
        kind = _clean_scalar(raw.get("kind") or raw.get("type") or raw.get("resource_type")) or "resource"
        label = _clean_scalar(raw.get("label"))
        name = _clean_scalar(raw.get("name") or label or raw.get("repo") or raw.get("app"))
        path = _clean_scalar(raw.get("path") or raw.get("workspace_root") or raw.get("local_path"))
        uri = _clean_scalar(raw.get("uri") or raw.get("url") or raw.get("remote") or raw.get("repo_url"))
        known = {
            "access",
            "allowed_paths",
            "app",
            "branch",
            "credential_ref",
            "credentials",
            "default_branch",
            "denied_paths",
            "file_count",
            "file_manifest",
            "files",
            "folders",
            "forbidden_paths",
            "id",
            "kind",
            "label",
            "last_modified",
            "local_path",
            "mime",
            "mode",
            "name",
            "path",
            "permissions",
            "private",
            "relative_path",
            "remote",
            "repo",
            "repo_url",
            "resource_type",
            "scope",
            "size",
            "source",
            "type",
            "upload_id",
            "uploaded_file_count",
            "uploaded_files",
            "uri",
            "url",
            "workspace_root",
        }
        return cls(
            id=_clean_scalar(raw.get("id")) or f"resource-{index + 1}",
            kind=kind,
            label=label,
            name=name,
            path=path,
            uri=uri,
            repo=_clean_scalar(raw.get("repo")),
            branch=_clean_scalar(raw.get("branch")),
            default_branch=_clean_scalar(raw.get("default_branch")),
            access=_clean_scalar(raw.get("access")),
            mode=_clean_scalar(raw.get("mode")),
            source=_clean_scalar(raw.get("source")),
            private=bool(raw.get("private")) if raw.get("private") is not None else None,
            credential_ref=_copy_mapping(raw.get("credential_ref")),
            credentials=_copy_mapping(raw.get("credentials")),
            scope=_copy_mapping(raw.get("scope")),
            permissions=_copy_mapping(raw.get("permissions")),
            allowed_paths=_copy_list(raw.get("allowed_paths")),
            files=_copy_list(raw.get("files")),
            folders=_copy_list(raw.get("folders")),
            forbidden_paths=_copy_list(raw.get("forbidden_paths")),
            denied_paths=_copy_list(raw.get("denied_paths")),
            file_manifest=_copy_list(raw.get("file_manifest")),
            file_count=raw.get("file_count") if isinstance(raw.get("file_count"), int) else None,
            size=raw.get("size") if isinstance(raw.get("size"), int) else None,
            mime=_clean_scalar(raw.get("mime")),
            last_modified=raw.get("last_modified") if isinstance(raw.get("last_modified"), (int, float)) else None,
            uploaded_files=_copy_list(raw.get("uploaded_files")),
            uploaded_file_count=raw.get("uploaded_file_count") if isinstance(raw.get("uploaded_file_count"), int) else None,
            upload_id=_clean_scalar(raw.get("upload_id")),
            relative_path=_clean_scalar(raw.get("relative_path")),
            extra={str(key): value for key, value in raw.items() if key not in known and value not in (None, "", {}, [])},
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "name": self.name,
            "path": self.path,
            "uri": self.uri,
            "repo": self.repo,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "access": self.access,
            "mode": self.mode,
            "source": self.source,
            "private": self.private,
            "credential_ref": self.credential_ref,
            "credentials": self.credentials,
            "scope": self.scope,
            "permissions": self.permissions,
            "allowed_paths": self.allowed_paths,
            "files": self.files,
            "folders": self.folders,
            "forbidden_paths": self.forbidden_paths,
            "denied_paths": self.denied_paths,
            "file_manifest": self.file_manifest,
            "file_count": self.file_count,
            "size": self.size,
            "mime": self.mime,
            "last_modified": self.last_modified,
            "uploaded_files": self.uploaded_files,
            "uploaded_file_count": self.uploaded_file_count,
            "upload_id": self.upload_id,
            "relative_path": self.relative_path,
            **dict(self.extra or {}),
        }
        return {key: value for key, value in payload.items() if value not in (None, "", {}, [])}


def normalize_project_resource(raw: Mapping[str, Any], *, index: int = 0) -> dict[str, Any]:
    return ProjectResource.from_mapping(raw, index=index).to_dict()
