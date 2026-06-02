"""Runtime self-context handler for Illo's source and identity grounding."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tomllib
from typing import Any

from brain.platform.async_io import run_subprocess_sync
from brain.systems.runs.tool_catalog.handlers.common import _agent_context
from brain.systems.runs.tool_catalog.registry import all_tool_registrations
from brain.systems.runs.tool_policy import disabled_tool_names_from_metadata


_REPO_ROOT = Path(__file__).resolve().parents[5]
_OPEN_SOURCE_REPO_URL = "https://github.com/Illospace/illospace"
_SOURCE_INSPECTION_TOOLS = (
    "read_file",
    "search_files",
    "list_files",
    "project_context",
    "exec_command",
    "run_script",
)


def _safe_git(*args: str) -> str | None:
    try:
        result = run_subprocess_sync(
            ["git", "-C", str(_REPO_ROOT), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _project_version() -> str | None:
    pyproject = _REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return str(version) if version else None


def _path_payload(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "is_file": path.is_file(),
    }


def _docs_payload() -> list[dict[str, Any]]:
    docs = (
        "README.md",
        "docs/server-setup.md",
        "docs/deployment.md",
        "docs/configuration.md",
        "docs/security-model.md",
        "docs/prd-universal-thread-context-ingress.md",
        "docs/prd-slack-teammate-ingress.md",
    )
    return [
        {
            "path": item,
            "exists": (_REPO_ROOT / item).exists(),
        }
        for item in docs
    ]


def _disabled_tools() -> set[str]:
    metadata = getattr(_agent_context, "execution_metadata", None)
    return disabled_tool_names_from_metadata(metadata)


def _source_inspection_payload() -> dict[str, Any]:
    registrations = all_tool_registrations()
    disabled = _disabled_tools()
    tools: dict[str, dict[str, Any]] = {}
    for name in _SOURCE_INSPECTION_TOOLS:
        registration = registrations.get(name)
        tools[name] = {
            "registered": registration is not None,
            "available_after_policy": registration is not None and name not in disabled,
            "permission": registration.permission.value if registration is not None else None,
            "side_effect_class": registration.side_effect_class.value if registration is not None else None,
        }
    can_read_source = bool(
        tools.get("read_file", {}).get("available_after_policy")
        and tools.get("search_files", {}).get("available_after_policy")
    )
    return {
        "can_read_source": can_read_source,
        "tools": tools,
        "disabled_by_policy": sorted(disabled & set(_SOURCE_INSPECTION_TOOLS)),
    }


def _runtime_scope_payload() -> dict[str, Any]:
    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    metadata = execution_metadata if isinstance(execution_metadata, dict) else {}
    return {
        "workspace_bound": bool(getattr(_agent_context, "org_id", None) or metadata.get("org_id")),
        "user_bound": bool(getattr(_agent_context, "user_id", None) or metadata.get("user_id")),
        "thread_bound": bool(getattr(_agent_context, "idea_id", None) or metadata.get("idea_id")),
        "has_target_ref": isinstance(getattr(_agent_context, "target_ref", None), dict)
        or isinstance(metadata.get("target_ref"), dict),
        "has_workspace_ref": isinstance(getattr(_agent_context, "workspace_ref", None), dict)
        or isinstance(metadata.get("workspace_ref"), dict),
    }


def _handle_read_self_context(
    include_paths: bool = True,
    include_git: bool = True,
) -> str:
    """Read verified identity, source, and runtime context for this Illo run."""

    payload: dict[str, Any] = {
        "ok": True,
        "source": "runtime_self_context",
        "identity": {
            "agent_name": "Illo",
            "workspace_product": "Illospace",
            "relationship": "Illo is the agent that operates inside an Illospace workspace.",
        },
        "open_source": {
            "repository_url": _OPEN_SOURCE_REPO_URL,
            "repository_url_source": "static product identity",
        },
        "runtime_scope": _runtime_scope_payload(),
        "source_inspection": _source_inspection_payload(),
        "project_version": _project_version(),
        "answering_guidance": [
            "Use read_self_context for Illo/Illospace identity, source-root, install, and source-inspection facts.",
            "Use read_capabilities for what Illo can inspect, do, guide, or set up in the current run.",
            "When source_inspection.can_read_source is true, inspect local docs or code before claiming Illospace-specific setup paths.",
        ],
    }
    if include_paths:
        env_project_root = os.environ.get("ILLO_PROJECT_ROOT")
        payload["installation"] = {
            "source_root": _path_payload(_REPO_ROOT),
            "current_working_directory": _path_payload(Path.cwd()),
            "env_project_root": _path_payload(Path(env_project_root)) if env_project_root else None,
            "source_roots": [
                _path_payload(_REPO_ROOT / item)
                for item in ("brain", "docs", "deploy", "frontend")
            ],
            "docs": _docs_payload(),
        }
    if include_git:
        payload["git"] = {
            "commit": _safe_git("rev-parse", "HEAD"),
            "short_commit": _safe_git("rev-parse", "--short", "HEAD"),
            "branch": _safe_git("branch", "--show-current"),
            "remote_origin_url": _safe_git("config", "--get", "remote.origin.url"),
        }
    return json.dumps(payload, default=str)


__all__ = ["_handle_read_self_context"]
