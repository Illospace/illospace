"""Canonical runtime Project context payloads."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


PROJECT_RUNTIME_CONTEXT_KEY = "project_runtime_context"


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def build_project_runtime_context(
    *,
    snapshot: Mapping[str, Any],
    permission_scope: Mapping[str, Any],
    workspace_manifest: Mapping[str, Any],
    materialization: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the canonical Project runtime payload stored on ``workspace_ref``."""

    return {
        "schema_version": 1,
        "project_context_snapshot": dict(snapshot),
        "permission_scope": dict(permission_scope),
        "project_workspace_manifest": dict(workspace_manifest),
        "project_context_materialization": dict(materialization),
    }


def project_runtime_context_from_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the canonical runtime context from a run payload, if present."""

    mapped = _as_mapping(payload)
    return _as_mapping(mapped.get(PROJECT_RUNTIME_CONTEXT_KEY))


def project_runtime_context_from_payloads(*payloads: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the best Project runtime context from canonical or compatibility payloads."""

    mapped_payloads = [_as_mapping(payload) for payload in payloads]
    for payload in mapped_payloads:
        runtime = project_runtime_context_from_payload(payload)
        if runtime:
            return runtime

    snapshot = {}
    manifest = {}
    materialization = {}
    permission_scope = {}
    for payload in mapped_payloads:
        if not snapshot:
            snapshot = _as_mapping(payload.get("project_context_snapshot"))
        if not manifest:
            manifest = _as_mapping(payload.get("project_workspace_manifest"))
        if not materialization:
            materialization = _as_mapping(payload.get("project_context_materialization"))
        if not permission_scope:
            permission_scope = _as_mapping(payload.get("project_context_permission_scope"))
        if not manifest:
            manifest = _as_mapping(materialization.get("workspace_manifest"))
    if not any((snapshot, manifest, materialization, permission_scope)):
        return {}
    return build_project_runtime_context(
        snapshot=snapshot,
        permission_scope=permission_scope,
        workspace_manifest=manifest,
        materialization=materialization,
    )


def project_runtime_snapshot(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    runtime = project_runtime_context_from_payload(payload)
    return _as_mapping(runtime.get("project_context_snapshot"))


def project_runtime_manifest(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    runtime = project_runtime_context_from_payload(payload)
    return _as_mapping(runtime.get("project_workspace_manifest"))


def project_runtime_materialization(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    runtime = project_runtime_context_from_payload(payload)
    return _as_mapping(runtime.get("project_context_materialization"))


__all__ = [
    "PROJECT_RUNTIME_CONTEXT_KEY",
    "build_project_runtime_context",
    "project_runtime_context_from_payload",
    "project_runtime_context_from_payloads",
    "project_runtime_manifest",
    "project_runtime_materialization",
    "project_runtime_snapshot",
]
