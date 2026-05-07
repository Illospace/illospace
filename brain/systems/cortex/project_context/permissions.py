"""Project Context permission and provenance helpers.

These helpers are intentionally pure/portable so admission, queue pickup,
artifact normalization, and tests can share the same fail-closed rules without
requiring a live workspace checkout. They do not replace OS/container isolation;
they produce the in-repo contract that workers and verifiers can enforce.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_PERMISSION_MODES = {"read", "write", "admin", "read_write"}
_DEFAULT_PERMISSION_MODE = "read_write"


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def normalize_project_path(path: str | None) -> str | None:
    """Normalize project paths for lexical scope comparison.

    Absolute paths remain absolute-like, relative paths remain relative-like, and
    traversal segments are collapsed. Paths that attempt to escape above their
    root are rejected by returning ``None``.
    """

    raw = _clean_text(path)
    if raw is None:
        return None
    raw = raw.replace("\\", "/")
    is_abs = raw.startswith("/")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    normalized = "/".join(parts)
    if is_abs:
        normalized = "/" + normalized
    return normalized or ("/" if is_abs else ".")


def _path_is_within(path: str, root: str) -> bool:
    normalized_path = normalize_project_path(path)
    normalized_root = normalize_project_path(root)
    if normalized_path is None or normalized_root is None:
        return False
    if normalized_root in ("", ".", "/"):
        return True
    return normalized_path == normalized_root or normalized_path.startswith(normalized_root.rstrip("/") + "/")


def _resource_scope_roots(resource: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    denied: list[str] = []
    for key in ("allowed_paths", "files", "folders"):
        for item in _as_list(resource.get(key)):
            text = _clean_text(item)
            if text:
                allowed.append(text)
    permissions = resource.get("permissions")
    if isinstance(permissions, Mapping):
        for key in ("allowed_paths", "read", "write", "read_write"):
            for item in _as_list(permissions.get(key)):
                text = _clean_text(item)
                if text:
                    allowed.append(text)
        for key in ("forbidden_paths", "deny", "denied_paths"):
            for item in _as_list(permissions.get(key)):
                text = _clean_text(item)
                if text:
                    denied.append(text)
    scope = resource.get("scope")
    if isinstance(scope, Mapping):
        for key in ("allowed_paths", "files", "folders"):
            for item in _as_list(scope.get(key)):
                text = _clean_text(item)
                if text:
                    allowed.append(text)
        for key in ("forbidden_paths", "deny", "denied_paths"):
            for item in _as_list(scope.get(key)):
                text = _clean_text(item)
                if text:
                    denied.append(text)
    for key in ("forbidden_paths", "denied_paths"):
        for item in _as_list(resource.get(key)):
            text = _clean_text(item)
            if text:
                denied.append(text)
    return allowed, denied


def derive_project_permission_scope(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a worker/verifier ownership scope from a project snapshot."""

    resources = snapshot.get("resources") if isinstance(snapshot, Mapping) else None
    allowed_paths: list[str] = []
    forbidden_paths: list[str] = []
    modes: set[str] = set()
    resource_ids: list[str] = []
    for resource in resources if isinstance(resources, list) else []:
        if not isinstance(resource, Mapping):
            continue
        resource_id = _clean_text(resource.get("id"))
        if resource_id:
            resource_ids.append(resource_id)
        mode = _clean_text(resource.get("mode")) or _clean_text(resource.get("permission"))
        permissions = resource.get("permissions")
        if isinstance(permissions, Mapping):
            mode = mode or _clean_text(permissions.get("mode"))
        if mode:
            modes.add(mode.lower())
        root = _clean_text(resource.get("path"))
        resource_allowed, resource_denied = _resource_scope_roots(resource)
        if root and not resource_allowed:
            allowed_paths.append(root)
        if root:
            for item in resource_allowed:
                allowed_paths.append(item if item.startswith("/") else f"{root.rstrip('/')}/{item}")
            for item in resource_denied:
                forbidden_paths.append(item if item.startswith("/") else f"{root.rstrip('/')}/{item}")
        else:
            allowed_paths.extend(resource_allowed)
            forbidden_paths.extend(resource_denied)

    def dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = normalize_project_path(value)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    normalized_modes = sorted(mode for mode in modes if mode in _PERMISSION_MODES)
    return {
        "schema_version": 1,
        "mode": "enforce",
        "permission_mode": normalized_modes[0] if len(normalized_modes) == 1 else (_DEFAULT_PERMISSION_MODE if not normalized_modes else "mixed"),
        "resource_ids": resource_ids[:100],
        "allowed_paths": dedupe(allowed_paths),
        "forbidden_paths": dedupe(forbidden_paths),
    }


def validate_path_permission(
    path: str,
    snapshot: Mapping[str, Any] | None,
    *,
    operation: str = "read",
) -> tuple[bool, str | None, dict[str, Any]]:
    """Validate a path against a Project Context snapshot scope.

    Returns ``(allowed, reason, scope)``. Missing/invalid snapshots are treated as
    not enforceable and return allowed=True with an explanatory reason; callers
    should use snapshot validation for fail-closed admission.
    """

    normalized_path = normalize_project_path(path)
    scope = derive_project_permission_scope(snapshot)
    if normalized_path is None:
        return False, "path escapes the project context root", scope
    status = snapshot.get("status") if isinstance(snapshot, Mapping) else None
    if status == "invalid":
        return False, "project context snapshot is invalid", scope
    allowed_paths = scope.get("allowed_paths") if isinstance(scope.get("allowed_paths"), list) else []
    forbidden_paths = scope.get("forbidden_paths") if isinstance(scope.get("forbidden_paths"), list) else []
    for denied in forbidden_paths:
        if _path_is_within(normalized_path, str(denied)):
            return False, f"path is inside forbidden project context path `{denied}`", scope
    if allowed_paths and not any(_path_is_within(normalized_path, str(root)) for root in allowed_paths):
        return False, "path is outside allowed Project Context resources", scope
    permission_mode = str(scope.get("permission_mode") or _DEFAULT_PERMISSION_MODE)
    if operation in {"write", "edit", "delete"} and permission_mode == "read":
        return False, "project context is read-only", scope
    return True, None, scope


def attach_project_provenance(
    artifact: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach project-context provenance and path permission status to an artifact."""

    payload = dict(artifact)
    if not isinstance(snapshot, Mapping):
        return payload
    path = _clean_text(payload.get("path") or payload.get("relative_path") or payload.get("absolute_path"))
    provenance = dict(payload.get("provenance") or {}) if isinstance(payload.get("provenance"), Mapping) else {}
    project_ref = {
        "project_context_id": snapshot.get("id"),
        "project_context_name": snapshot.get("name"),
        "project_context_status": snapshot.get("status"),
    }
    if path:
        operation = str(payload.get("operation") or payload.get("status") or "read").lower()
        allowed, reason, scope = validate_path_permission(path, snapshot, operation=operation)
        project_ref["path_permission"] = {
            "allowed": allowed,
            "reason": reason,
            "scope": scope,
        }
    provenance["project_context"] = {key: value for key, value in project_ref.items() if value not in (None, "", {}, [])}
    payload["provenance"] = provenance
    return payload
