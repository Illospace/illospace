"""Project context snapshot helpers for run/scout context surfaces.

A Project Context can include several resources (repos, folders, docs, apps,
uploaded files). This module captures a small immutable-ish run snapshot
from the explicit metadata and augments local git-backed resources with
provenance. Git is provenance for change tracking/rollback; it is not treated as
Project Context's only source of truth.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from urllib.parse import urlsplit, urlunsplit
from typing import Any

from brain.systems.cortex.project_context.permissions import (
    derive_project_permission_scope,
    normalize_project_path,
)
from brain.systems.cortex.project_context.resources import normalize_project_resource

_GIT_RESOURCE_KINDS = {"repo", "repository", "folder", "workspace", "docs", "doc"}
_RESOURCE_LOCATOR_KEYS = ("path", "uri", "name")
_BROWSER_ONLY_URI_SCHEMES = {"browser", "browser-file", "browser-folder"}


def _clean_scalar(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None



def _redact_remote(remote: str | None) -> str | None:
    if not remote:
        return None
    try:
        parsed = urlsplit(remote)
    except Exception:
        parsed = None
    if parsed and parsed.scheme in {"http", "https", "ssh"} and "@" in parsed.netloc:
        host = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, f"***@{host}", parsed.path, parsed.query, parsed.fragment))
    # SCP-style SSH remotes (git@github.com:org/repo.git) do not parse with a
    # netloc, but still include a user component that is unnecessary in worker
    # context. Keep the host/path identity while redacting the username/token.
    if "://" not in remote and "@" in remote and ":" in remote.split("@", 1)[-1]:
        return f"***@{remote.split('@', 1)[-1]}"
    return remote

def _run_git(path: str, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=path,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip()
    except Exception:
        return None


def _git_provenance(path: str) -> dict[str, Any] | None:
    root = _run_git(path, "rev-parse", "--show-toplevel")
    if not root:
        return None

    branch = _run_git(root, "branch", "--show-current")
    commit = _run_git(root, "rev-parse", "HEAD")
    status = _run_git(root, "status", "--short") or ""
    changed_files = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        status_code = line[:2]
        path_part = line[3:] if line[2:3] == " " else line[2:]
        if "->" in path_part and status_code.strip().startswith("R"):
            path_part = path_part.rsplit("->", 1)[-1].strip()
        changed_files.append(path_part.strip())
    remote = _run_git(root, "remote", "get-url", "origin")

    return {
        "root": root,
        "branch": branch or None,
        "commit": commit or None,
        "dirty": bool(status.strip()),
        "changed_files": changed_files[:100],
        "changed_file_count": len(changed_files),
        "remote": _redact_remote(remote),
    }


def _normalise_resource(raw: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    resource = normalize_project_resource(raw, index=index)
    kind = _clean_scalar(resource.get("kind")) or "resource"
    path = _clean_scalar(resource.get("path"))
    access = _clean_scalar(resource.get("access"))
    if access and "mode" not in resource:
        resource["mode"] = "read_write" if access == "write" else access

    if path and kind.lower() in _GIT_RESOURCE_KINDS:
        git = _git_provenance(path)
        if git:
            resource["git"] = git
    return resource


def _resources_from_target(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    explicit_resources = target.get("resources")
    if isinstance(explicit_resources, list):
        for item in explicit_resources:
            if isinstance(item, Mapping):
                resources.append(_normalise_resource(item, index=len(resources)))

    repo = target.get("repo") or target.get("repository") or target.get("repo_name")
    workspace = target.get("workspace") or target.get("workspace_path")
    if isinstance(workspace, Mapping):
        workspace_path = workspace.get("path")
        workspace_name = workspace.get("name")
    else:
        workspace_path = workspace
        workspace_name = None

    if repo or workspace_path or workspace_name:
        resource: dict[str, Any] = {"kind": "repo" if repo else "workspace"}
        if isinstance(repo, str):
            resource["name"] = repo
        if isinstance(workspace_name, str):
            resource.setdefault("name", workspace_name)
        if isinstance(workspace_path, str):
            resource["path"] = workspace_path
        if target.get("branch"):
            resource["branch_hint"] = target.get("branch")
        resources.insert(0, _normalise_resource(resource, index=0))
        for index, resource in enumerate(resources):
            resource["id"] = resource.get("id") or f"resource-{index + 1}"
    return resources


def _local_path_access_error(path: str) -> str | None:
    try:
        if Path(path).expanduser().exists():
            return None
    except OSError:
        pass
    return f"path does not exist or is not accessible: `{path}`."


def _local_uri_access_error(uri: str) -> str | None:
    parsed = urlsplit(uri)
    if parsed.scheme != "file":
        return None
    return _local_path_access_error(parsed.path)


def validate_project_context_snapshot(
    snapshot: Mapping[str, Any] | None,
    *,
    validate_local_paths: bool = False,
) -> tuple[str, list[str]]:
    """Return a compact validation status for a worker-visible snapshot.

    This is deliberately fail-closed for explicit Project Context metadata: if a
    caller says a project is attached, workers should see whether it is usable
    instead of silently receiving an empty/ambiguous scope. Path permission scope
    is derived here for in-repo worker/verifier enforcement; OS/container
    isolation remains a separate runtime layer.
    """
    if not isinstance(snapshot, Mapping):
        return "missing", ["project_context_snapshot is missing."]
    resources = snapshot.get("resources")
    if not isinstance(resources, list):
        return "invalid", ["project_context_snapshot.resources must be a list."]
    if not resources:
        if _clean_scalar(snapshot.get("name")) or _clean_scalar(snapshot.get("id")):
            return "validated", []
        return "invalid", ["project_context_snapshot.resources must contain at least one resource unless the project has a name or id."]

    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, resource in enumerate(resources):
        label = f"resources[{index}]"
        if not isinstance(resource, Mapping):
            errors.append(f"{label} must be an object.")
            continue
        resource_id = _clean_scalar(resource.get("id"))
        if resource_id:
            if resource_id in seen_ids:
                errors.append(f"{label}.id duplicates `{resource_id}`.")
            seen_ids.add(resource_id)
        if not _clean_scalar(resource.get("kind")):
            errors.append(f"{label}.kind must be a non-empty string.")
        if not any(_clean_scalar(resource.get(key)) for key in _RESOURCE_LOCATOR_KEYS):
            errors.append(f"{label} must include at least one locator: path, uri, or name.")
        path = _clean_scalar(resource.get("path"))
        if path and normalize_project_path(path) is None:
            errors.append(f"{label}.path escapes its root: `{path}`.")
        if validate_local_paths and path:
            access_error = _local_path_access_error(path)
            if access_error:
                errors.append(f"{label}.{access_error}")
        uri = _clean_scalar(resource.get("uri"))
        if uri and urlsplit(uri).scheme in _BROWSER_ONLY_URI_SCHEMES and not (
            _clean_scalar(resource.get("path")) or resource.get("uploaded_files") or resource.get("allowed_paths")
        ):
            errors.append(
                f"{label}.uri uses a browser-only picker reference; upload the file/folder or attach a backend-readable path."
            )
        if validate_local_paths and uri:
            access_error = _local_uri_access_error(uri)
            if access_error:
                errors.append(f"{label}.{access_error}")
        for path_key in ("allowed_paths", "forbidden_paths", "denied_paths"):
            paths = resource.get(path_key)
            if paths is not None and not (
                isinstance(paths, list) and all(isinstance(item, str) and item.strip() for item in paths)
            ):
                errors.append(f"{label}.{path_key} must be a list of non-empty strings when provided.")
            if isinstance(paths, list):
                for scoped_path in paths:
                    if normalize_project_path(scoped_path) is None:
                        errors.append(f"{label}.{path_key} contains a path that escapes its root: `{scoped_path}`.")
                        continue
                    if validate_local_paths and path_key == "allowed_paths":
                        candidate = scoped_path
                        if path and not Path(scoped_path).expanduser().is_absolute():
                            candidate = str(Path(path).expanduser() / scoped_path)
                        access_error = _local_path_access_error(candidate)
                        if access_error:
                            errors.append(f"{label}.{path_key}.{access_error}")
        mode = resource.get("mode")
        permissions = resource.get("permissions")
        if isinstance(permissions, Mapping) and mode is None:
            mode = permissions.get("mode")
        if mode is not None and str(mode).strip().lower() not in {"read", "write", "admin", "read_write"}:
            errors.append(f"{label}.mode must be one of read, write, read_write, or admin when provided.")
    return ("invalid" if errors else "validated", errors)


def build_project_context_snapshot(
    metadata: Mapping[str, Any] | None,
    *,
    run_id: int | None = None,
    captured_at: str | None = None,
    validate_local_paths: bool = False,
) -> dict[str, Any] | None:
    """Build a serializable Project Context snapshot from run metadata.

    Supported metadata shapes:
    - {"project_context": {"resources": [...]}}
    - {"project": {"resources": [...]}}
    - legacy/simple {"target": {"repo": ..., "workspace": {"path": ...}}}
    """
    if not isinstance(metadata, Mapping):
        return None

    project = metadata.get("project_context") or metadata.get("project")
    if isinstance(project, Mapping):
        resources = []
        raw_resources = project.get("resources")
        if isinstance(raw_resources, list):
            for item in raw_resources:
                if isinstance(item, Mapping):
                    resources.append(_normalise_resource(item, index=len(resources)))
        snapshot: dict[str, Any] = {
            "schema_version": 1,
            "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
            "source": "metadata.project_context" if "project_context" in metadata else "metadata.project",
            "resources": resources,
        }
        for key in ("id", "name", "description", "permissions", "mode"):
            value = project.get(key)
            if value is not None:
                snapshot[key] = value
        if run_id is not None:
            snapshot["run_id"] = run_id
        if resources or any(k in snapshot for k in ("id", "name")):
            status, errors = validate_project_context_snapshot(snapshot, validate_local_paths=validate_local_paths)
            snapshot["status"] = status
            if errors:
                snapshot["validation_errors"] = errors
            snapshot["permission_scope"] = derive_project_permission_scope(snapshot)
            return snapshot
        return None

    target = metadata.get("target")
    if isinstance(target, Mapping):
        resources = _resources_from_target(target)
        if resources:
            snapshot = {
                "schema_version": 1,
                "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
                "source": "metadata.target",
                "resources": resources,
            }
            if run_id is not None:
                snapshot["run_id"] = run_id
            status, errors = validate_project_context_snapshot(snapshot, validate_local_paths=validate_local_paths)
            snapshot["status"] = status
            if errors:
                snapshot["validation_errors"] = errors
            snapshot["permission_scope"] = derive_project_permission_scope(snapshot)
            return snapshot
    return None


def snapshot_from_project_context(
    project_context: Mapping[str, Any],
    *,
    validate_local_paths: bool = True,
) -> dict[str, Any]:
    """Build and validate a snapshot from a persisted profile/attachment payload."""

    snapshot = build_project_context_snapshot(
        {"project_context": project_context},
        validate_local_paths=validate_local_paths,
    )
    if snapshot is None:
        snapshot = dict(project_context)
        status, errors = validate_project_context_snapshot(
            snapshot,
            validate_local_paths=validate_local_paths,
        )
        snapshot["status"] = status
        if errors:
            snapshot["validation_errors"] = errors
    snapshot["permission_scope"] = derive_project_permission_scope(snapshot)
    return snapshot


def attach_project_context_snapshot(
    target_metadata: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    *,
    run_id: int | None = None,
    validate_local_paths: bool = False,
) -> dict[str, Any]:
    """Return target metadata with project_context_snapshot attached when available."""
    payload = dict(target_metadata or {})
    snapshot = build_project_context_snapshot(
        metadata,
        run_id=run_id,
        validate_local_paths=validate_local_paths,
    )
    if snapshot is None and isinstance(metadata, Mapping) and isinstance(metadata.get("project_context_snapshot"), Mapping):
        snapshot = dict(metadata["project_context_snapshot"])
        if "status" not in snapshot:
            status, errors = validate_project_context_snapshot(
                snapshot,
                validate_local_paths=validate_local_paths,
            )
            snapshot["status"] = status
            if errors:
                snapshot["validation_errors"] = errors
    if snapshot:
        payload["project_context_snapshot"] = snapshot
    return payload
