"""Conservative run target resolution and binding persistence."""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.environment import (
    RunTargetBinding,
    EnvironmentBinding,
    EnvironmentCommand,
    EnvironmentService,
    TargetRegistry,
)

_TARGET_KEYS = ("repo", "workspace", "branch", "app")
_TARGET_ALIASES = {
    "repository": "repo",
    "repo_name": "repo",
    "workspace_name": "workspace",
    "workspace_path": "workspace",
    "application": "app",
}


@dataclass(frozen=True)
class TargetBindingResolution:
    """Structured resolution result before it is written back to the DB."""

    status: str
    target_registry_id: int | None = None
    environment_binding_id: int | None = None
    resolved_workspace_root: str | None = None
    resolved_branch: str | None = None
    resolved_service_set: list[dict[str, Any]] = field(default_factory=list)
    resolution_notes: dict[str, Any] = field(default_factory=dict)
    resolution_confidence: float = 0.0


def _normalize_target_payload(raw_target_metadata: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Return a normalized target payload plus any validation notes."""
    if not isinstance(raw_target_metadata, dict):
        return {}, []

    if "target" in raw_target_metadata and isinstance(raw_target_metadata.get("target"), dict):
        explicit = raw_target_metadata.get("target")
        normalized: dict[str, Any] = {}
        errors: list[str] = []

        for raw_key, raw_value in explicit.items():
            key = _TARGET_ALIASES.get(str(raw_key), str(raw_key))
            if key not in _TARGET_KEYS:
                errors.append(f"metadata.target contains unsupported field `{raw_key}`.")
                continue

            if key == "workspace" and isinstance(raw_value, dict):
                workspace: dict[str, str] = {}
                for child_key in ("name", "path"):
                    child_value = raw_value.get(child_key)
                    if child_value is None:
                        continue
                    if not isinstance(child_value, str) or not child_value.strip():
                        errors.append(f"metadata.target.workspace.{child_key} must be a non-empty string.")
                        continue
                    workspace[child_key] = child_value.strip()
                if workspace:
                    normalized[key] = workspace
                continue

            if not isinstance(raw_value, str) or not raw_value.strip():
                errors.append(f"metadata.target.{key} must be a non-empty string.")
                continue
            normalized[key] = raw_value.strip()

        if not normalized and not errors:
            errors.append("metadata.target must include at least one of repo/workspace/branch/app.")
        return normalized, errors

    normalized: dict[str, Any] = {}
    for key in _TARGET_KEYS:
        value = raw_target_metadata.get(key)
        if value is None:
            for alias, canonical in _TARGET_ALIASES.items():
                if canonical == key and alias in raw_target_metadata:
                    value = raw_target_metadata.get(alias)
                    break
        if value is None:
            continue
        if key == "workspace" and isinstance(value, dict):
            workspace: dict[str, str] = {}
            for workspace_key in ("name", "path"):
                workspace_value = value.get(workspace_key)
                if isinstance(workspace_value, str) and workspace_value.strip():
                    workspace[workspace_key] = workspace_value.strip()
            if workspace:
                normalized[key] = workspace
            continue
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    return normalized, []


def _display_name_matches(target_value: str | None, candidate: str | None) -> bool:
    if not target_value or not candidate:
        return False
    return target_value.strip().lower() == candidate.strip().lower()


def _last_url_segment(value: str | None) -> str | None:
    if not value:
        return None
    segment = value.rstrip("/").rsplit("/", 1)[-1]
    if segment.endswith(".git"):
        segment = segment[:-4]
    return segment or None


def _registry_matches_target(registry: TargetRegistry, target: dict[str, Any]) -> tuple[bool, list[str]]:
    matched_on: list[str] = []
    repo = target.get("repo")
    app = target.get("app")

    if isinstance(repo, str) and repo.strip():
        repo_value = repo.strip().lower()
        slug = (registry.slug or "").strip().lower()
        if repo_value == slug:
            matched_on.append("repo.slug")
        elif repo_value == (_last_url_segment(registry.repo_url) or "").lower():
            matched_on.append("repo.repo_url")
        elif repo_value == (_last_url_segment(registry.canonical_path) or "").lower():
            matched_on.append("repo.canonical_path")

    if isinstance(app, str) and app.strip():
        app_value = app.strip().lower()
        if app_value == (registry.slug or "").strip().lower():
            matched_on.append("app.slug")
        elif _display_name_matches(app, registry.display_name):
            matched_on.append("app.display_name")

    return bool(matched_on), matched_on


def _workspace_matches(binding: EnvironmentBinding, target: dict[str, Any]) -> bool:
    workspace = target.get("workspace")
    if not isinstance(workspace, dict):
        return False
    path = workspace.get("path")
    if not isinstance(path, str) or not path.strip():
        return False
    if not binding.workspace_root:
        return False
    return path.strip() == binding.workspace_root.strip()


def _branch_matches(binding: EnvironmentBinding, registry: TargetRegistry, target: dict[str, Any]) -> bool:
    branch = target.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        return False
    branch_value = branch.strip()
    if binding.branch_pattern and fnmatchcase(branch_value, binding.branch_pattern):
        return True
    if binding.branch_pattern and branch_value == binding.branch_pattern.strip():
        return True
    if registry.default_branch and branch_value == registry.default_branch.strip():
        return True
    return False


def _binding_matches_target(
    binding: EnvironmentBinding,
    registry: TargetRegistry,
    target: dict[str, Any],
    *,
    org_id: str | None,
) -> tuple[bool, list[str]]:
    matched_on: list[str] = []

    if org_id and binding.org_id and binding.org_id != org_id:
        return False, matched_on

    repo_or_app_present = any(isinstance(target.get(key), str) and target.get(key).strip() for key in ("repo", "app"))
    workspace_present = _workspace_matches(binding, target)
    branch_present = _branch_matches(binding, registry, target)
    registry_present, registry_matches = _registry_matches_target(registry, target)

    if workspace_present:
        matched_on.append("workspace.path")
    if branch_present:
        matched_on.append("branch")
    if registry_present:
        matched_on.extend(registry_matches)

    if repo_or_app_present and not registry_present:
        return False, matched_on

    if not workspace_present and not branch_present:
        return False, matched_on

    return True, matched_on


def _serialise_service(service: EnvironmentService) -> dict[str, Any]:
    return {
        "id": service.id,
        "service_name": service.service_name,
        "service_type": service.service_type,
        "base_path": service.base_path,
        "healthcheck": service.healthcheck,
        "test_command_id": service.test_command_id,
        "verify_contract": service.verify_contract or {},
    }


def _serialise_command(command: EnvironmentCommand) -> dict[str, Any]:
    return {
        "id": command.id,
        "binding_id": command.binding_id,
        "command_name": command.command_name,
        "command": command.command,
        "cwd": command.cwd,
        "purpose": command.purpose,
        "cost_class": command.cost_class,
        "safe_default": bool(command.safe_default),
        "metadata": command.metadata_ or {},
    }


def _binding_resolution_confidence(binding: dict[str, Any] | None) -> float:
    if not isinstance(binding, dict):
        return 0.0
    try:
        return float(binding.get("resolution_confidence") or (binding.get("resolution_notes") or {}).get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _unique_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for command in commands:
        command_id = command.get("id")
        if isinstance(command_id, int):
            if command_id in seen_ids:
                continue
            seen_ids.add(command_id)
        unique.append(command)
    return unique


def select_execution_workspace_hint(context: dict[str, Any] | None) -> str | None:
    """Return a workspace root hint only when the binding is low-ambiguity."""
    if not isinstance(context, dict):
        return None

    binding = context.get("binding") or {}
    if not isinstance(binding, dict):
        return None
    if binding.get("resolution_status") != "resolved":
        return None
    if _binding_resolution_confidence(binding) < 0.75:
        return None

    workspace_root = binding.get("resolved_workspace_root")
    if isinstance(workspace_root, str) and workspace_root.strip():
        return workspace_root.strip()

    environment_binding = context.get("environment_binding") or {}
    if isinstance(environment_binding, dict):
        binding_workspace_root = environment_binding.get("workspace_root")
        if isinstance(binding_workspace_root, str) and binding_workspace_root.strip():
            return binding_workspace_root.strip()
    return None


def select_safe_command_default(context: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return one curated command only when the selection is unambiguous."""
    if not isinstance(context, dict):
        return None

    binding = context.get("binding") or {}
    if not isinstance(binding, dict):
        return None
    if binding.get("resolution_status") != "resolved":
        return None
    if _binding_resolution_confidence(binding) < 0.75:
        return None

    safe_commands = _unique_commands([command for command in (context.get("safe_commands") or []) if isinstance(command, dict)])
    if len(safe_commands) == 1:
        return safe_commands[0]

    service_test_commands = _unique_commands([command for command in (context.get("service_test_commands") or []) if isinstance(command, dict)])
    if not safe_commands and len(service_test_commands) == 1:
        return service_test_commands[0]

    return None


def build_execution_defaults(context: dict[str, Any] | None) -> dict[str, Any]:
    """Derive low-ambiguity execution defaults from a resolved target context."""
    binding = (context or {}).get("binding") if isinstance(context, dict) else {}
    binding_is_low_ambiguity = (
        isinstance(binding, dict)
        and binding.get("resolution_status") == "resolved"
        and _binding_resolution_confidence(binding) >= 0.75
    )
    workspace_root = select_execution_workspace_hint(context)
    safe_command = select_safe_command_default(context)
    command_selection_status = "unavailable"
    if safe_command is not None:
        command_selection_status = "selected"
    elif binding_is_low_ambiguity:
        safe_count = len(_unique_commands([command for command in (context or {}).get("safe_commands", []) if isinstance(command, dict)]))
        service_count = len(_unique_commands([command for command in (context or {}).get("service_test_commands", []) if isinstance(command, dict)]))
        if safe_count or service_count:
            command_selection_status = "ambiguous"

    defaults: dict[str, Any] = {
        "workspace_root": workspace_root,
        "workspace_hint": workspace_root,
        "safe_command": safe_command,
        "command_selection_status": command_selection_status,
    }
    if workspace_root is not None:
        defaults["verifier_workspace_root"] = workspace_root
    return defaults


def load_run_target_context(session: Session, run_id: int) -> dict[str, Any] | None:
    """Load a resolved run target plus catalog details for read surfaces."""
    binding = get_run_target_binding(session, run_id)
    if binding is None:
        return None

    try:
        registry = session.get(TargetRegistry, binding.target_registry_id) if binding.target_registry_id is not None else None
    except Exception:
        registry = None
    try:
        environment_binding = (
            session.get(EnvironmentBinding, binding.environment_binding_id)
            if binding.environment_binding_id is not None
            else None
        )
    except Exception:
        environment_binding = None

    command_rows = []
    service_rows = []
    if environment_binding is not None:
        try:
            command_rows = session.scalars(
                select(EnvironmentCommand).where(EnvironmentCommand.binding_id == environment_binding.id)
            ).all()
        except Exception:
            command_rows = []
        try:
            service_rows = session.scalars(
                select(EnvironmentService).where(EnvironmentService.binding_id == environment_binding.id)
            ).all()
        except Exception:
            service_rows = []

    command_lookup = {command.id: command for command in command_rows}
    safe_commands = [
        _serialise_command(command)
        for command in command_rows
        if bool(command.safe_default)
    ]
    service_test_commands = [
        _serialise_command(command_lookup[service.test_command_id])
        for service in service_rows
        if service.test_command_id is not None and service.test_command_id in command_lookup
    ]

    binding_payload = serialize_run_target_binding(binding, session=None) or {}
    if registry is not None:
        binding_payload["target_registry"] = {
            "id": getattr(registry, "id", None),
            "target_kind": getattr(registry, "target_kind", None),
            "slug": getattr(registry, "slug", None),
            "display_name": getattr(registry, "display_name", None),
            "owner_team": getattr(registry, "owner_team", None),
            "repo_url": getattr(registry, "repo_url", None),
            "canonical_path": getattr(registry, "canonical_path", None),
            "default_branch": getattr(registry, "default_branch", None),
            "metadata": getattr(registry, "metadata_", None) or {},
            "active": getattr(registry, "active", None),
        }
    if environment_binding is not None:
        binding_payload["environment_binding"] = {
            "id": getattr(environment_binding, "id", None),
            "target_registry_id": getattr(environment_binding, "target_registry_id", None),
            "env_name": getattr(environment_binding, "env_name", None),
            "branch_pattern": getattr(environment_binding, "branch_pattern", None),
            "workspace_root": getattr(environment_binding, "workspace_root", None),
            "deploy_target": getattr(environment_binding, "deploy_target", None),
            "org_id": getattr(environment_binding, "org_id", None),
            "metadata": getattr(environment_binding, "metadata_", None) or {},
        }
    binding_payload["catalog_summary"] = {
        "command_count": len(command_rows),
        "safe_command_count": len(safe_commands),
        "service_count": len(service_rows),
        "service_test_command_count": len(service_test_commands),
    }
    binding_payload["execution_defaults"] = build_execution_defaults({
        "binding": binding_payload,
        "safe_commands": safe_commands,
        "service_test_commands": service_test_commands,
    })

    return {
        "binding": binding_payload,
        "registry": binding_payload.get("target_registry"),
        "environment_binding": binding_payload.get("environment_binding"),
        "catalog_summary": binding_payload["catalog_summary"],
        "execution_defaults": binding_payload["execution_defaults"],
        "services": [_serialise_service(service) for service in service_rows],
        "commands": [_serialise_command(command) for command in command_rows],
        "safe_commands": safe_commands,
        "service_test_commands": service_test_commands,
    }


def render_run_target_context(
    context: dict[str, Any] | None,
    *,
    include_commands: bool = True,
    include_services: bool = True,
) -> str:
    """Render a conservative prompt/debug summary for a resolved run target."""
    if not context:
        return ""

    binding = context.get("binding") or {}
    registry = context.get("registry") or binding.get("target_registry") or {}
    environment_binding = context.get("environment_binding") or binding.get("environment_binding") or {}
    notes = binding.get("resolution_notes") or {}
    messages = notes.get("messages") or []
    safe_commands = context.get("safe_commands") or []
    service_test_commands = context.get("service_test_commands") or []
    services = context.get("services") or []
    execution_defaults = context.get("execution_defaults") or binding.get("execution_defaults") or {}

    parts = [
        "## Run Target Binding",
        f"Resolution status: {binding.get('resolution_status', 'unknown')}",
        f"Resolution confidence: {float(binding.get('resolution_confidence') or notes.get('confidence') or 0.0):.2f}",
    ]

    if binding.get("resolved_workspace_root"):
        parts.append(f"Resolved workspace root: {binding['resolved_workspace_root']}")
    if binding.get("resolved_branch"):
        parts.append(f"Resolved branch hint: {binding['resolved_branch']}")
    if registry:
        parts.append(
            f"Registry: {registry.get('display_name') or registry.get('slug')} "
            f"({registry.get('target_kind') or 'unknown'})"
        )
    if environment_binding:
        parts.append(
            f"Environment binding: {environment_binding.get('env_name')} "
            f"({environment_binding.get('deploy_target') or 'no deploy target'})"
        )

    if messages:
        parts.append("### Resolution Notes")
        for message in messages[:5]:
            parts.append(f"- {message}")

    if include_services and services:
        parts.append("### Known Services")
        for service in services[:5]:
            summary = f"- {service.get('service_name')} [{service.get('service_type')}]"
            if service.get("base_path"):
                summary += f" @ {service['base_path']}"
            if service.get("healthcheck"):
                summary += f" | healthcheck: {service['healthcheck']}"
            parts.append(summary)

    if include_commands:
        command_rows = []
        seen_commands: set[int] = set()
        for command in [*safe_commands, *service_test_commands]:
            command_id = command.get("id")
            if isinstance(command_id, int) and command_id in seen_commands:
                continue
            if isinstance(command_id, int):
                seen_commands.add(command_id)
            command_rows.append(command)
        if command_rows:
            parts.append("### Safe Commands")
            for command in command_rows[:5]:
                summary = f"- {command.get('command_name')}: {command.get('command')}"
                if command.get("cwd"):
                    summary += f" (cwd: {command['cwd']})"
                if command.get("purpose"):
                    summary += f" | {command['purpose']}"
                parts.append(summary)

    project_snapshot = None
    raw_target_metadata = binding.get("raw_target_metadata")
    if isinstance(raw_target_metadata, dict):
        project_snapshot = raw_target_metadata.get("project_context_snapshot")
    if not isinstance(project_snapshot, dict):
        run_target_metadata = context.get("run_target_metadata")
        if isinstance(run_target_metadata, dict):
            project_snapshot = run_target_metadata.get("project_context_snapshot")
    if isinstance(project_snapshot, dict):
        resources = project_snapshot.get("resources")
        if isinstance(resources, list):
            parts.append("### Project Context Snapshot")
            project_name = project_snapshot.get("name")
            if isinstance(project_name, str) and project_name.strip():
                parts.append(f"Project: {project_name.strip()}")
            status = project_snapshot.get("status")
            if isinstance(status, str) and status.strip():
                parts.append(f"Status: {status.strip()}")
            errors = project_snapshot.get("validation_errors")
            if isinstance(errors, list) and errors:
                parts.append("Validation errors: " + "; ".join(str(error) for error in errors[:3]))
            parts.append(f"Resources: {len(resources)}")
            for resource in resources[:5]:
                if not isinstance(resource, dict):
                    continue
                label = resource.get("name") or resource.get("path") or resource.get("uri") or resource.get("id")
                summary = f"- {resource.get('kind', 'resource')}"
                if label:
                    summary += f": {label}"
                if resource.get("path"):
                    summary += f" @ {resource['path']}"
                elif resource.get("uri"):
                    summary += f" <{resource['uri']}>"
                uploaded_files = resource.get("uploaded_files")
                if isinstance(uploaded_files, list) and uploaded_files:
                    summary += f" | uploaded {len(uploaded_files)} file{'s' if len(uploaded_files) != 1 else ''}"
                elif resource.get("uploaded_file_count"):
                    summary += f" | uploaded {resource['uploaded_file_count']} files"
                git = resource.get("git")
                if isinstance(git, dict):
                    commit = str(git.get("commit") or "")[:12]
                    summary += f" | git {git.get('branch') or 'detached'}"
                    if commit:
                        summary += f"@{commit}"
                    if git.get("dirty"):
                        summary += f" dirty({git.get('changed_file_count', 0)})"
                parts.append(summary)

    if execution_defaults:
        parts.append("### Execution Defaults")
        workspace_hint = execution_defaults.get("workspace_hint") or execution_defaults.get("workspace_root")
        if workspace_hint:
            parts.append(f"Preferred workspace root: {workspace_hint}")
        safe_command = execution_defaults.get("safe_command")
        if isinstance(safe_command, dict):
            summary = f"Selected command: {safe_command.get('command_name')}: {safe_command.get('command')}"
            if safe_command.get("cwd"):
                summary += f" (cwd: {safe_command['cwd']})"
            parts.append(summary)
        elif execution_defaults.get("command_selection_status") == "ambiguous":
            parts.append("Selected command: ambiguous; keep command execution explicit.")

    confidence = float(binding.get("resolution_confidence") or notes.get("confidence") or 0.0)

    if confidence < 0.75:
        parts.append(
            "### Guidance\n"
            "Treat this binding as advisory, not authoritative. "
            "Do not override explicit user metadata or guess missing target details."
        )
    else:
        parts.append(
            "### Guidance\n"
            "Resolved bindings may refine workspace, branch, and safe catalog choices, "
            "but they must not silently override explicit user intent."
        )

    return "\n".join(parts)


def _resolution_notes(
    *,
    target: dict[str, Any],
    validation_errors: list[str],
    matched_on: list[str],
    registry_candidates: list[TargetRegistry],
    binding_candidates: list[EnvironmentBinding],
    status: str,
) -> dict[str, Any]:
    notes: dict[str, Any] = {
        "messages": [],
        "match_basis": matched_on,
        "registry_candidate_count": len(registry_candidates),
        "binding_candidate_count": len(binding_candidates),
    }
    if status == "resolved":
        notes["confidence"] = 0.95 if matched_on else 0.9
    elif status == "partial":
        notes["confidence"] = 0.55 if (binding_candidates or registry_candidates) else 0.0
    else:
        notes["confidence"] = 0.0
    if validation_errors:
        notes["validation_errors"] = validation_errors
    if registry_candidates:
        notes["registry_candidates"] = [
            {
                "id": registry.id,
                "target_kind": registry.target_kind,
                "slug": registry.slug,
                "display_name": registry.display_name,
            }
            for registry in registry_candidates[:5]
        ]
    if binding_candidates:
        notes["binding_candidates"] = [
            {
                "id": binding.id,
                "env_name": binding.env_name,
                "workspace_root": binding.workspace_root,
                "branch_pattern": binding.branch_pattern,
                "target_registry_id": binding.target_registry_id,
            }
            for binding in binding_candidates[:5]
        ]

    if status == "resolved":
        notes["messages"].append("Exact curated binding matched the explicit target metadata.")
    elif status == "partial":
        if binding_candidates:
            notes["messages"].append("More than one curated candidate matched; keeping the binding partial.")
        elif registry_candidates:
            notes["messages"].append("Registry matched, but there was not enough explicit data to bind a specific environment.")
        else:
            notes["messages"].append("Some explicit target metadata was present, but no curated binding could be confirmed.")
    else:
        notes["messages"].append("No curated target binding could be confirmed from the available metadata.")

    if not target:
        notes["messages"].append("No explicit target metadata was provided.")

    return notes


def _project_context_resources(raw_target_metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(raw_target_metadata, dict):
        return []
    snapshot = raw_target_metadata.get("project_context_snapshot")
    if not isinstance(snapshot, dict):
        return []
    if str(snapshot.get("status") or "").strip().lower() == "invalid":
        return []
    resources = snapshot.get("resources")
    if not isinstance(resources, list):
        return []
    return [resource for resource in resources if isinstance(resource, dict)]


def _project_context_resolution(
    *,
    raw_target_metadata: dict[str, Any] | None,
    target: dict[str, Any],
    validation_errors: list[str],
) -> TargetBindingResolution | None:
    """Resolve a binding from a validated materialized Project Context snapshot.

    Project Context resources are already validated and permission-scoped before
    materialization. Once a single resource has a concrete local path, it is a
    better binding than leaving the run target unknown simply because no
    curated registry row exists yet.
    """
    resources = _project_context_resources(raw_target_metadata)
    if not resources:
        return None

    path_candidates = [
        resource
        for resource in resources
        if isinstance(resource.get("path"), str) and resource.get("path", "").strip()
    ]
    candidate_summaries = []
    for resource in resources[:5]:
        git = resource.get("git") if isinstance(resource.get("git"), dict) else {}
        candidate_summaries.append({
            "kind": resource.get("kind"),
            "name": resource.get("name"),
            "repo": resource.get("repo"),
            "branch": resource.get("branch") or git.get("branch"),
            "path": resource.get("path"),
            "uri": resource.get("uri"),
        })
    notes: dict[str, Any] = {
        "messages": [],
        "match_basis": ["project_context_snapshot"],
        "registry_candidate_count": 0,
        "binding_candidate_count": 0,
        "project_context_resource_count": len(resources),
        "project_context_candidates": candidate_summaries,
    }
    if validation_errors:
        notes["validation_errors"] = validation_errors

    if len(path_candidates) == 1:
        resource = path_candidates[0]
        git = resource.get("git") if isinstance(resource.get("git"), dict) else {}
        branch = resource.get("branch") or git.get("branch") or target.get("branch")
        path = str(resource.get("path") or "").strip()
        notes["confidence"] = 0.82
        notes["match_basis"] = [
            "project_context_snapshot.resource.path",
            *(
                ["project_context_snapshot.resource.repo"]
                if resource.get("repo") or resource.get("name") or resource.get("uri")
                else []
            ),
        ]
        notes["messages"].append("Resolved target from a single materialized Project Context resource.")
        return TargetBindingResolution(
            status="resolved",
            target_registry_id=None,
            environment_binding_id=None,
            resolved_workspace_root=path,
            resolved_branch=str(branch).strip() if isinstance(branch, str) and branch.strip() else None,
            resolved_service_set=[],
            resolution_notes=notes,
            resolution_confidence=0.82,
        )

    notes["confidence"] = 0.55 if path_candidates else 0.35
    if path_candidates:
        notes["messages"].append("Multiple Project Context resources were materialized; keeping target binding partial.")
    else:
        notes["messages"].append("Project Context was present but no materialized workspace path was available yet.")
    return TargetBindingResolution(
        status="partial",
        target_registry_id=None,
        environment_binding_id=None,
        resolved_workspace_root=None,
        resolved_branch=None,
        resolved_service_set=[],
        resolution_notes=notes,
        resolution_confidence=float(notes["confidence"]),
    )


def _build_target_resolution(
    *,
    target: dict[str, Any],
    validation_errors: list[str],
    registries: list[TargetRegistry],
    bindings: list[EnvironmentBinding],
    services: list[EnvironmentService],
    org_id: str | None,
    raw_target_metadata: dict[str, Any] | None = None,
) -> TargetBindingResolution:
    registry_candidates: list[TargetRegistry] = []
    binding_candidates: list[tuple[EnvironmentBinding, TargetRegistry, list[str]]] = []

    for registry in registries:
        registry_match, _ = _registry_matches_target(registry, target)
        if registry_match:
            registry_candidates.append(registry)

    for binding in bindings:
        registry = next((candidate for candidate in registries if candidate.id == binding.target_registry_id), None)
        if registry is None:
            continue
        binding_match, matched_on = _binding_matches_target(binding, registry, target, org_id=org_id)
        if binding_match:
            binding_candidates.append((binding, registry, matched_on))

    if len(binding_candidates) == 1:
        binding, registry, matched_on = binding_candidates[0]
        branch = target.get("branch")
        resolved_branch = branch.strip() if isinstance(branch, str) and branch.strip() else None
        return TargetBindingResolution(
            status="resolved",
            target_registry_id=registry.id,
            environment_binding_id=binding.id,
            resolved_workspace_root=binding.workspace_root,
            resolved_branch=resolved_branch,
            resolved_service_set=[
                _serialise_service(service)
                for service in services
                if service.binding_id == binding.id
            ],
            resolution_notes=_resolution_notes(
                target=target,
                validation_errors=validation_errors,
                matched_on=matched_on,
                registry_candidates=registry_candidates or [registry],
                binding_candidates=[binding],
                status="resolved",
            ),
            resolution_confidence=0.95 if matched_on else 0.9,
        )

    if binding_candidates:
        candidate_bindings = [candidate[0] for candidate in binding_candidates]
        candidate_registries = [candidate[1] for candidate in binding_candidates]
        return TargetBindingResolution(
            status="partial",
            target_registry_id=candidate_registries[0].id if len({registry.id for registry in candidate_registries}) == 1 else None,
            environment_binding_id=None,
            resolved_workspace_root=None,
            resolved_branch=None,
            resolved_service_set=[],
            resolution_notes=_resolution_notes(
                target=target,
                validation_errors=validation_errors,
                matched_on=sorted({basis for _, _, bases in binding_candidates for basis in bases}),
                registry_candidates=candidate_registries,
                binding_candidates=candidate_bindings,
                status="partial",
            ),
            resolution_confidence=0.55,
        )

    project_context_resolution = _project_context_resolution(
        raw_target_metadata=raw_target_metadata,
        target=target,
        validation_errors=validation_errors,
    )
    if project_context_resolution is not None:
        return project_context_resolution

    if registry_candidates:
        registry = registry_candidates[0]
        return TargetBindingResolution(
            status="partial",
            target_registry_id=registry.id,
            environment_binding_id=None,
            resolved_workspace_root=None,
            resolved_branch=None,
            resolved_service_set=[],
            resolution_notes=_resolution_notes(
                target=target,
                validation_errors=validation_errors,
                matched_on=sorted({basis for registry in registry_candidates for basis in _registry_matches_target(registry, target)[1]}),
                registry_candidates=registry_candidates,
                binding_candidates=[],
                status="partial",
            ),
            resolution_confidence=0.55,
        )

    return TargetBindingResolution(
        status="unknown",
        target_registry_id=None,
        environment_binding_id=None,
        resolved_workspace_root=None,
        resolved_branch=None,
        resolved_service_set=[],
        resolution_notes=_resolution_notes(
            target=target,
            validation_errors=validation_errors,
            matched_on=[],
            registry_candidates=[],
            binding_candidates=[],
            status="unknown",
        ),
        resolution_confidence=0.0,
    )


def _lookup_run_org_id(session: Session, run: AgentRun) -> str | None:
    if not run.idea_id:
        return None
    idea = session.get(Idea, run.idea_id)
    return getattr(idea, "org_id", None) if idea else None


def get_run_target_binding(session: Session, run_id: int) -> RunTargetBinding | None:
    """Return the persisted binding row for a run, if any."""
    stmt = select(RunTargetBinding).where(RunTargetBinding.run_id == run_id)
    return session.scalars(stmt).first()


def resolve_run_target_binding(
    session: Session,
    run_id: int,
    raw_target_metadata: dict[str, Any] | None = None,
) -> RunTargetBinding | None:
    """Resolve a run target conservatively and persist the binding row."""
    run = session.get(AgentRun, run_id)
    if run is None:
        return None

    raw_target = raw_target_metadata if raw_target_metadata is not None else (run.target_metadata or {})
    target, validation_errors = _normalize_target_payload(raw_target)

    registries = session.scalars(select(TargetRegistry).where(TargetRegistry.active.is_(True))).all()
    bindings = session.scalars(select(EnvironmentBinding)).all()
    services = session.scalars(select(EnvironmentService)).all()
    org_id = _lookup_run_org_id(session, run)

    resolution = _build_target_resolution(
        target=target,
        validation_errors=validation_errors,
        registries=registries,
        bindings=bindings,
        services=services,
        org_id=org_id,
        raw_target_metadata=raw_target if isinstance(raw_target, dict) else None,
    )

    binding = get_run_target_binding(session, run_id)
    if binding is None:
        binding = RunTargetBinding(run_id=run_id)
        session.add(binding)

    binding.raw_target_metadata = raw_target if isinstance(raw_target, dict) else {}
    binding.resolution_status = resolution.status
    binding.target_registry_id = resolution.target_registry_id
    binding.environment_binding_id = resolution.environment_binding_id
    binding.resolved_workspace_root = resolution.resolved_workspace_root
    binding.resolved_branch = resolution.resolved_branch
    binding.resolved_service_set = resolution.resolved_service_set
    binding.resolution_notes = resolution.resolution_notes
    return binding


def serialize_run_target_binding(
    binding: RunTargetBinding | None,
    *,
    session: Session | None = None,
) -> dict[str, Any] | None:
    """Serialize a binding row for debug/read surfaces."""
    if binding is None:
        return None

    payload: dict[str, Any] = {
        "id": binding.id,
        "run_id": binding.run_id,
        "raw_target_metadata": binding.raw_target_metadata or {},
        "resolution_status": binding.resolution_status,
        "resolution_confidence": float((binding.resolution_notes or {}).get("confidence") or 0.0),
        "target_registry_id": binding.target_registry_id,
        "environment_binding_id": binding.environment_binding_id,
        "resolved_workspace_root": binding.resolved_workspace_root,
        "resolved_branch": binding.resolved_branch,
        "resolved_service_set": binding.resolved_service_set or [],
        "resolution_notes": binding.resolution_notes or {},
    }

    if session is None:
        return payload

    if binding.target_registry_id is not None:
        try:
            registry = session.get(TargetRegistry, binding.target_registry_id)
        except Exception:
            registry = None
        if registry is not None:
            payload["target_registry"] = {
                "id": getattr(registry, "id", None),
                "target_kind": getattr(registry, "target_kind", None),
                "slug": getattr(registry, "slug", None),
                "display_name": getattr(registry, "display_name", None),
                "owner_team": getattr(registry, "owner_team", None),
                "repo_url": getattr(registry, "repo_url", None),
                "canonical_path": getattr(registry, "canonical_path", None),
                "default_branch": getattr(registry, "default_branch", None),
                "metadata": getattr(registry, "metadata_", None) or {},
                "active": getattr(registry, "active", None),
            }

    if binding.environment_binding_id is not None:
        try:
            environment_binding = session.get(EnvironmentBinding, binding.environment_binding_id)
        except Exception:
            environment_binding = None
        if environment_binding is not None:
            payload["environment_binding"] = {
                "id": getattr(environment_binding, "id", None),
                "target_registry_id": getattr(environment_binding, "target_registry_id", None),
                "env_name": getattr(environment_binding, "env_name", None),
                "branch_pattern": getattr(environment_binding, "branch_pattern", None),
                "workspace_root": getattr(environment_binding, "workspace_root", None),
                "deploy_target": getattr(environment_binding, "deploy_target", None),
                "org_id": getattr(environment_binding, "org_id", None),
                "metadata": getattr(environment_binding, "metadata_", None) or {},
            }
        try:
            command_rows = session.scalars(
                select(EnvironmentCommand).where(EnvironmentCommand.binding_id == binding.environment_binding_id)
            ).all()
        except Exception:
            command_rows = []
        try:
            service_rows = session.scalars(
                select(EnvironmentService).where(EnvironmentService.binding_id == binding.environment_binding_id)
            ).all()
        except Exception:
            service_rows = []
        payload["catalog_summary"] = {
            "command_count": len(command_rows),
            "safe_command_count": sum(1 for command in command_rows if bool(command.safe_default)),
            "service_count": len(service_rows),
            "service_test_command_count": sum(1 for service in service_rows if service.test_command_id is not None),
        }

    return payload
