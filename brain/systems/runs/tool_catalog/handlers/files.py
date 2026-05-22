"""Files orchestration tool handlers."""

from __future__ import annotations

from collections.abc import Mapping
import pathlib
import re as _re
import shlex as _shlex

from brain.systems.cortex.project_context.drafts import IGNORED_DRAFT_DIRS
from brain.systems.cortex.project_context.workspace_manifest import (
    ProjectWorkspaceManifest,
    normalize_project_workspace_manifest,
)
from brain.systems.runs.tool_catalog.handlers.common import *
from brain.systems.runs.project_execution_env import (
    annotate_project_execution_result as _annotate_project_execution_result,
    prepare_project_execution_env as _prepare_project_execution_env,
    redact_sensitive_output as _redact_sensitive_output,
)
from brain.platform.async_io import run_subprocess_sync


def _path_is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _is_project_internal_relative_path(path: pathlib.Path) -> bool:
    return any(part in IGNORED_DRAFT_DIRS for part in path.parts)


def _block_project_internal_path(path: str) -> None:
    if any(part in IGNORED_DRAFT_DIRS for part in pathlib.Path(path).parts):
        raise ValueError("Project draft metadata is internal and is not accessible through file tools.")


def _context_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _run_mapping_attr(name: str) -> Mapping[str, object]:
    run = getattr(_agent_context, "run", None)
    return _context_mapping(getattr(run, name, None))


def _metadata_mapping() -> Mapping[str, object]:
    run = getattr(_agent_context, "run", None)
    metadata = getattr(run, "metadata_", None) or getattr(run, "metadata", None)
    if callable(metadata):
        try:
            metadata = metadata()
        except Exception:
            metadata = None
    return _context_mapping(metadata)


def _agent_context_payloads() -> list[Mapping[str, object]]:
    payloads: list[Mapping[str, object]] = []
    for value in (
        getattr(_agent_context, "workspace_ref", None),
        getattr(_agent_context, "target_ref", None),
        _run_mapping_attr("workspace_ref"),
        _run_mapping_attr("target_ref"),
        _metadata_mapping(),
        getattr(_agent_context, "execution_metadata", None),
    ):
        payload = _context_mapping(value)
        if payload and not any(payload is existing for existing in payloads):
            payloads.append(payload)
    return payloads


def _manifest_from_project_workspace_payload(
    payload: Mapping[str, object],
    *,
    include_workspace_entries: bool = True,
) -> ProjectWorkspaceManifest | None:
    mounts = payload.get("mounts")
    if isinstance(mounts, list) and mounts:
        resources = []
        for index, raw_mount in enumerate(mounts):
            mount = _context_mapping(raw_mount)
            workspace_path = mount.get("workspace_path") or mount.get("path")
            mount_path = mount.get("mount_path") or mount.get("name") or mount.get("id")
            if not isinstance(workspace_path, str) or not workspace_path.strip():
                continue
            if not isinstance(mount_path, str) or not mount_path.strip():
                continue
            resource_path = mount.get("resource_path") or workspace_path
            source_path = mount.get("source_path")
            materialization = _context_mapping(mount.get("metadata")).get("materialization")
            materialization_payload = dict(_context_mapping(materialization))
            materialization_payload.update({
                "workspace_path": workspace_path,
                "path": resource_path,
            })
            if isinstance(source_path, str) and source_path.strip():
                materialization_payload["source_path"] = source_path
            resources.append({
                "id": mount.get("resource_id") or mount.get("id") or f"resource-{index + 1}",
                "kind": mount.get("kind") or "workspace",
                "mount_path": mount_path,
                "name": mount.get("label") or mount_path,
                "path": resource_path,
                "source_path": source_path,
                "materialization": materialization_payload,
            })
        if resources:
            return normalize_project_workspace_manifest(
                {
                    "id": payload.get("project_id") or payload.get("project_key"),
                    "resources": resources,
                    "workspace_root": payload.get("workspace_root"),
                    "resolved_workspace_root": payload.get("resolved_workspace_root"),
                },
                workspaces=payload.get("workspaces") if isinstance(payload.get("workspaces"), list) else None,
            )

    workspaces = payload.get("workspaces")
    if include_workspace_entries and isinstance(workspaces, list) and workspaces:
        return normalize_project_workspace_manifest(payload, workspaces=workspaces)
    return None


def _project_workspace_manifest_payloads(payloads: list[Mapping[str, object]]) -> list[Mapping[str, object]]:
    manifests: list[Mapping[str, object]] = []
    for payload in payloads:
        workspace_manifest = payload.get("project_workspace_manifest")
        if isinstance(workspace_manifest, Mapping):
            manifests.append(workspace_manifest)
        materialization = payload.get("project_context_materialization")
        if isinstance(materialization, Mapping):
            materialized_manifest = materialization.get("workspace_manifest")
            if isinstance(materialized_manifest, Mapping):
                manifests.append(materialized_manifest)
    return manifests


def _first_project_workspace_manifest(
    payloads: list[Mapping[str, object]],
    *,
    include_workspace_entries: bool,
) -> ProjectWorkspaceManifest | None:
    for workspace_manifest in _project_workspace_manifest_payloads(payloads):
        manifest = _manifest_from_project_workspace_payload(
            workspace_manifest,
            include_workspace_entries=include_workspace_entries,
        )
        if manifest and manifest.mounts:
            return manifest
    return None


def _current_project_workspace_manifest() -> ProjectWorkspaceManifest | None:
    workspace_payloads = _agent_context_payloads()
    manifest = _first_project_workspace_manifest(workspace_payloads, include_workspace_entries=False)
    if manifest:
        return manifest

    workspaces = None
    for payload in workspace_payloads:
        if isinstance(payload.get("workspaces"), list):
            workspaces = payload.get("workspaces")
            break
    for payload in workspace_payloads:
        snapshot = payload.get("project_context_snapshot")
        if isinstance(snapshot, Mapping):
            manifest = normalize_project_workspace_manifest(snapshot, workspaces=workspaces)
            if manifest.mounts:
                return manifest

    return _first_project_workspace_manifest(workspace_payloads, include_workspace_entries=True)


def _project_mount_resolution(path: str) -> tuple[str, object] | None:
    manifest = _current_project_workspace_manifest()
    if manifest is None:
        return None
    mount = manifest.mount_for_agent_path(path)
    if mount is None:
        return None
    resolved = mount.resolve_agent_path(path)
    if not resolved:
        return None
    resolved_real = os.path.realpath(resolved)
    workspace_real = os.path.realpath(mount.workspace_path)
    if not _path_is_within(resolved_real, workspace_real):
        raise ValueError(
            f"Path escapes Project draft workspace: {path} → {resolved_real} "
            f"(workspace: {workspace_real})"
        )
    return resolved_real, mount


def _block_project_source_write(resolved: str) -> None:
    manifest = _current_project_workspace_manifest()
    if manifest is None:
        return
    resolved_real = os.path.realpath(resolved)
    for mount in manifest.mounts:
        if not mount.source_path:
            continue
        source_real = os.path.realpath(mount.source_path)
        workspace_real = os.path.realpath(mount.workspace_path)
        if _path_is_within(resolved_real, source_real) and not _path_is_within(resolved_real, workspace_real):
            raise ValueError(
                f"Blocked write to Project source path: {resolved_real}. "
                f"Use the draft mount {mount.mount_path} instead."
            )


def _project_source_mounts() -> list[object]:
    manifest = _current_project_workspace_manifest()
    if manifest is None:
        return []
    return [mount for mount in manifest.mounts if mount.source_path]


_PROJECT_SOURCE_WRITE_PATTERNS = [
    _re.compile(
        r"(^|\s)"
        r"(rm|mv|cp|touch|mkdir|rmdir|chmod|chown|truncate|tee|sed|perl|python|python3|node|npm|pnpm|yarn|git)\b"
    ),
    _re.compile(r"(^|\s)(>|>>|2>|&>)"),
    _re.compile(r"\b(open|write_text|write_bytes|unlink|remove|rmtree|rename|replace|mkdir)\s*\("),
]


def _command_or_script_may_write(text: str) -> bool:
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in _PROJECT_SOURCE_WRITE_PATTERNS)


def _block_project_source_command_write(text: str, *, operation: str, cwd: str | None = None) -> None:
    if not text or not _command_or_script_may_write(text):
        return
    cwd_real = os.path.realpath(cwd) if cwd else None
    for mount in _project_source_mounts():
        source_path = getattr(mount, "source_path", None)
        if not isinstance(source_path, str) or not source_path.strip():
            continue
        source_real = os.path.realpath(source_path)
        workspace_real = os.path.realpath(getattr(mount, "workspace_path", "") or "")
        names_source_path = source_path in text or source_real in text
        runs_in_source_path = bool(cwd_real and _path_is_within(cwd_real, source_real))
        runs_in_draft_path = bool(cwd_real and workspace_real and _path_is_within(cwd_real, workspace_real))
        if names_source_path or (runs_in_source_path and not runs_in_draft_path):
            raise ValueError(
                f"Blocked {operation} that may write to Project source path: {source_real}. "
                f"Use the draft mount {getattr(mount, 'mount_path', '<project mount>')} instead."
            )


def _resolve_path(path: str, working_dir: str | None = None, *, for_write: bool = False) -> str:
    """Resolve a path relative to workspace root. Enforces containment.

    Args:
        path: The path to resolve (relative or absolute).
        working_dir: Override workspace root (used by worktree isolation).
    """
    base = os.path.realpath(working_dir or _patched_workspace_root())
    project_resolution = _project_mount_resolution(path)
    if project_resolution is not None:
        resolved, _mount = project_resolution
        _block_project_internal_path(resolved)
        if for_write:
            _block_project_source_write(resolved)
        return resolved

    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(base, path))

    if for_write:
        _block_project_source_write(resolved)
    _block_project_internal_path(resolved)

    # Path containment: must stay within workspace
    if not _path_is_within(resolved, base):
        raise ValueError(f"Path escapes workspace: {path} → {resolved} (workspace: {base})")
    return resolved

_BLOCKED_PATTERNS = [
    _re.compile(r"rm\s+-[a-z]*r[a-z]*f?\s+/"),  # rm -rf /
    _re.compile(r"mkfs"),
    _re.compile(r"dd\s+if="),
    _re.compile(r">\s*/dev/sd"),
    _re.compile(r"chmod\s+-R\s+777\s+/"),
    _re.compile(r":\(\)\s*\{\s*:\|:&\s*\}"),  # fork bomb
]


def _handle_exec_command(
    command: str,
    working_dir: str | None = None,
    timeout: int = 60,
    _workspace: str | None = None,
) -> dict:
    """Execute a shell command with safety limits."""
    import subprocess

    # Block destructive command patterns via regex
    cmd_lower = command.lower().strip()
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(cmd_lower):
            result = {"exit_code": -1, "stdout": "", "stderr": "Blocked: dangerous command pattern", "blocked": True}
            return result

    timeout = min(timeout, 300)  # Cap at 5 minutes
    if _workspace and os.path.exists(_workspace) and not os.path.isdir(_workspace):
        _workspace = None
    if _workspace:
        try:
            cwd = _resolve_path(working_dir, _workspace) if working_dir else _workspace
        except ValueError as e:
            result = {"exit_code": -1, "stdout": "", "stderr": str(e), "error": str(e)}
            return result
    else:
        cwd = working_dir or _patched_workspace_root()
    try:
        _block_project_source_command_write(command, operation="command", cwd=cwd)
    except ValueError as e:
        result = {"exit_code": -1, "stdout": "", "stderr": str(e), "error": str(e), "blocked": True}
        return result

    # Use shell=True only when the command requires shell features (pipes, redirects, &&).
    # Otherwise split into a list for safer execution.
    _SHELL_CHARS = {'|', '>', '<', '&&', '||', ';', '`', '$(' }
    needs_shell = any(ch in command for ch in _SHELL_CHARS)
    project_execution = _prepare_project_execution_env()

    try:
        if needs_shell:
            proc = run_subprocess_sync(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=project_execution.env,
            )
        else:
            proc = run_subprocess_sync(
                _shlex.split(command),
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=project_execution.env,
            )
        stdout_raw = _redact_sensitive_output(proc.stdout or "", project_execution.sensitive_values)
        stderr_raw = _redact_sensitive_output(proc.stderr or "", project_execution.sensitive_values)
        stdout = stdout_raw[:_MAX_RESULT_CHARS] if stdout_raw else ""
        stderr = stderr_raw[:_MAX_RESULT_CHARS] if stderr_raw else ""

        if len(stdout_raw) > _MAX_RESULT_CHARS:
            stdout += f"\n... (truncated, {len(proc.stdout):,} chars total)"
        if len(stderr_raw) > _MAX_RESULT_CHARS:
            stderr += f"\n... (truncated, {len(proc.stderr):,} chars total)"

        result = {
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
        _annotate_project_execution_result(result, project_execution)
        # Only semantic execution outcomes belong in execution_artifacts. Generic
        # command transcripts are too noisy for provenance tools such as my_activity.
        _patched_private(
            "_record_execution_artifacts",
            _record_execution_artifacts,
        )(command, result)
        return result
    except subprocess.TimeoutExpired:
        result = {"exit_code": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s", "error": "timeout"}
        _annotate_project_execution_result(result, project_execution)
        return result
    except Exception as e:
        error = _redact_sensitive_output(str(e), project_execution.sensitive_values)
        result = {"exit_code": -1, "stdout": "", "stderr": error, "error": error}
        return result


def _handle_run_script(script: str, description: str | None = None, timeout: int = 60, _workspace: str | None = None) -> dict:
    """Write a Python script to a tempfile and execute it."""
    import subprocess
    import tempfile

    timeout = min(timeout, 300)
    if _workspace and os.path.exists(_workspace) and not os.path.isdir(_workspace):
        _workspace = None
    cwd = _workspace or _patched_workspace_root()
    project_execution = _prepare_project_execution_env()

    try:
        _block_project_source_command_write(script, operation="script", cwd=cwd)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, prefix="_illo_script_") as f:
            f.write(script)
            script_path = f.name

        try:
            proc = run_subprocess_sync(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=project_execution.env,
            )
            stdout_raw = _redact_sensitive_output(proc.stdout or "", project_execution.sensitive_values)
            stderr_raw = _redact_sensitive_output(proc.stderr or "", project_execution.sensitive_values)
            stdout = stdout_raw[:_MAX_RESULT_CHARS] if stdout_raw else ""
            stderr = stderr_raw[:_MAX_RESULT_CHARS] if stderr_raw else ""

            if len(stdout_raw) > _MAX_RESULT_CHARS:
                stdout += f"\n... (truncated, {len(proc.stdout):,} chars total)"
            if len(stderr_raw) > _MAX_RESULT_CHARS:
                stderr += f"\n... (truncated, {len(proc.stderr):,} chars total)"

            result = {
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
            _annotate_project_execution_result(result, project_execution)
            _record_tool_evidence(
                "run_script",
                {"script": script, "description": description, "timeout": timeout, "workspace": _workspace},
                result,
            )
            _patched_private(
                "_record_execution_artifacts",
                _record_execution_artifacts,
            )(f"python {os.path.basename(script_path)}", result)
            return result
        finally:
            try:
                os.remove(script_path)
            except OSError:
                pass
    except subprocess.TimeoutExpired:
        result = {"exit_code": -1, "stdout": "", "stderr": f"Script timed out after {timeout}s", "error": "timeout"}
        _annotate_project_execution_result(result, project_execution)
        _record_tool_evidence(
            "run_script",
            {"script": script, "description": description, "timeout": timeout, "workspace": _workspace},
            result,
        )
        return result
    except Exception as e:
        error = _redact_sensitive_output(str(e), project_execution.sensitive_values)
        result = {"exit_code": -1, "stdout": "", "stderr": error, "error": error}
        _annotate_project_execution_result(result, project_execution)
        _record_tool_evidence(
            "run_script",
            {"script": script, "description": description, "timeout": timeout, "workspace": _workspace},
            result,
        )
        return result


def _handle_read_file(path: str, start_line: int | None = None, end_line: int | None = None, _workspace: str | None = None) -> dict:
    """Read a file with optional line range."""
    try:
        resolved = _resolve_path(path, _workspace)
    except ValueError as e:
        result = {"error": str(e), "path": path}
        _record_tool_evidence(
            "read_file",
            {"path": path, "start_line": start_line, "end_line": end_line, "workspace": _workspace},
            result,
        )
        return result

    try:
        with open(resolved, "r", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        requested_start = start_line
        requested_end = end_line

        if start_line or end_line:
            start = max(1, start_line or 1)
            end = min(total_lines, end_line or total_lines)
            lines = lines[start - 1:end]
            offset = start
        else:
            offset = 1

        # Format with line numbers
        numbered = []
        for i, line in enumerate(lines, offset):
            numbered.append(f"{i:>6}\t{line.rstrip()}")

        content = "\n".join(numbered)
        if len(content) > _MAX_RESULT_CHARS:
            content = content[:_MAX_RESULT_CHARS] + f"\n... (truncated, {total_lines} lines total)"

        _record_file_artifact(
            resolved,
            "read",
            observation={
                "operation": "read",
                "partial_read": bool(requested_start or requested_end),
                "start_line": requested_start,
                "end_line": requested_end,
                "total_lines": total_lines,
            },
            workspace_root=_workspace,
        )
        result = {"content": content, "total_lines": total_lines, "path": resolved}
        _record_tool_evidence(
            "read_file",
            {"path": path, "start_line": start_line, "end_line": end_line, "workspace": _workspace},
            result,
        )
        return result
    except FileNotFoundError:
        result = {"error": f"File not found: {resolved}", "path": resolved}
        _record_tool_evidence(
            "read_file",
            {"path": path, "start_line": start_line, "end_line": end_line, "workspace": _workspace},
            result,
        )
        return result
    except Exception as e:
        result = {"error": str(e), "path": resolved}
        _record_tool_evidence(
            "read_file",
            {"path": path, "start_line": start_line, "end_line": end_line, "workspace": _workspace},
            result,
        )
        return result


def _handle_write_file(path: str, content: str, _workspace: str | None = None) -> dict:
    """Write content to a file, creating directories as needed."""
    try:
        resolved = _resolve_path(path, _workspace, for_write=True)
    except ValueError as e:
        result = {"error": str(e), "path": path}
        _record_tool_evidence("write_file", {"path": path, "content": content, "workspace": _workspace}, result)
        return result

    try:
        pre_meta = _file_snapshot(resolved)
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "w") as f:
            f.write(content)
        post_meta = _file_snapshot(resolved)
        result = {"written": True, "path": resolved, "bytes": len(content.encode())}
        _record_file_artifact(
            resolved,
            "written",
            observation={
                "operation": "write",
                "pre_sha256": pre_meta.get("sha256"),
                "post_sha256": post_meta.get("sha256"),
                "existed_before": bool(pre_meta.get("exists")),
                "bytes_written": len(content.encode()),
            },
            workspace_root=_workspace,
        )
        _record_tool_evidence("write_file", {"path": path, "content": content, "workspace": _workspace}, result)
        return result
    except Exception as e:
        result = {"error": str(e), "path": resolved}
        _record_tool_evidence("write_file", {"path": path, "content": content, "workspace": _workspace}, result)
        return result


def _handle_edit_file(path: str, old_text: str, new_text: str, _workspace: str | None = None) -> dict:
    """Surgical string replacement in a file."""
    try:
        resolved = _resolve_path(path, _workspace, for_write=True)
    except ValueError as e:
        result = {"error": str(e), "path": path}
        _record_tool_evidence(
            "edit_file",
            {"path": path, "old_text": old_text, "new_text": new_text, "workspace": _workspace},
            result,
        )
        return result

    try:
        pre_meta = _file_snapshot(resolved)
        with open(resolved, "r") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            result = {"error": "old_text not found in file", "path": resolved}
            _record_tool_evidence(
                "edit_file",
                {"path": path, "old_text": old_text, "new_text": new_text, "workspace": _workspace},
                result,
            )
            return result
        if count > 1:
            result = {"error": f"old_text matches {count} locations — must be unique. Add more context.", "path": resolved}
            _record_tool_evidence(
                "edit_file",
                {"path": path, "old_text": old_text, "new_text": new_text, "workspace": _workspace},
                result,
            )
            return result

        new_content = content.replace(old_text, new_text, 1)
        with open(resolved, "w") as f:
            f.write(new_content)
        post_meta = _file_snapshot(resolved)

        result = {"edited": True, "path": resolved}
        _record_file_artifact(
            resolved,
            "edited",
            observation={
                "operation": "edit",
                "pre_sha256": pre_meta.get("sha256"),
                "post_sha256": post_meta.get("sha256"),
                "old_text_bytes": len(old_text.encode()),
                "new_text_bytes": len(new_text.encode()),
            },
            workspace_root=_workspace,
        )
        _record_tool_evidence(
            "edit_file",
            {"path": path, "old_text": old_text, "new_text": new_text, "workspace": _workspace},
            result,
        )
        return result
    except FileNotFoundError:
        result = {"error": f"File not found: {resolved}", "path": resolved}
        _record_tool_evidence(
            "edit_file",
            {"path": path, "old_text": old_text, "new_text": new_text, "workspace": _workspace},
            result,
        )
        return result
    except Exception as e:
        result = {"error": str(e), "path": resolved}
        _record_tool_evidence(
            "edit_file",
            {"path": path, "old_text": old_text, "new_text": new_text, "workspace": _workspace},
            result,
        )
        return result


def _file_snapshot(path: str) -> dict:
    if not os.path.exists(path):
        return {"exists": False}
    try:
        with open(path, "rb") as f:
            data = f.read()
        stat = os.stat(path)
        return {
            "exists": True,
            "sha256": sha256(data).hexdigest(),
            "mtime": stat.st_mtime,
            "size_bytes": stat.st_size,
        }
    except Exception:
        return {"exists": True}


def _relative_artifact_path(path: str, workspace_root: str | None = None) -> str:
    base = os.path.realpath(
        workspace_root
        or getattr(_agent_context, "workspace_root", None)
        or _patched_workspace_root()
    )
    resolved = os.path.realpath(path)
    try:
        return os.path.relpath(resolved, base)
    except Exception:
        return resolved


def _execution_artifact_context() -> dict:
    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    return {
        "run_id": getattr(run, "run_id", None) or execution_metadata.get("run_id"),
        "execution_id": execution_metadata.get("execution_id"),
        "worker_id": execution_metadata.get("worker_id"),
        "node_id": execution_metadata.get("node_id") or execution_metadata.get("step_id"),
        "skill": execution_metadata.get("skill_name"),
        "session_id": execution_metadata.get("session_id") or getattr(_agent_context, "session_id", None),
        "ownership_scope": execution_metadata.get("ownership_scope") or {},
    }


def _record_file_artifact(
    path: str,
    status: str,
    *,
    observation: dict | None = None,
    workspace_root: str | None = None,
) -> None:
    """Persist file-level execution artifacts for contract verification."""
    context = _execution_artifact_context()
    relative_path = _relative_artifact_path(path, workspace_root)
    operation = (observation or {}).get("operation") or status
    provenance = {
        "source": "file_tool",
        "operation": operation,
        "path": relative_path,
        **{key: value for key, value in context.items() if value not in (None, "", {})},
    }
    artifacts = []
    if status != "read":
        artifacts.append({
            "type": "file",
            "path": path,
            "relative_path": relative_path,
            "status": status,
            "provenance": provenance,
        })
    if observation:
        snapshot = _file_snapshot(path)
        artifacts.append({
            "type": "file_observation",
            "schema_version": 1,
            "operation": operation,
            "path": relative_path,
            "absolute_path": path,
            "sha256": snapshot.get("sha256"),
            "mtime": snapshot.get("mtime"),
            "size_bytes": snapshot.get("size_bytes"),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "provenance": provenance,
            **{key: value for key, value in context.items() if value not in (None, "", {})},
            **{key: value for key, value in observation.items() if value is not None},
        })
    _patched_private(
        "_persist_execution_artifacts",
        _persist_execution_artifacts,
    )(artifacts, run_id=context.get("run_id"))


def _handle_search_files(pattern: str, path: str | None = None, glob: str | None = None, _workspace: str | None = None) -> dict:
    """Search file contents using grep."""
    import subprocess

    try:
        search_path = _resolve_path(path, _workspace) if path else (_workspace or _patched_workspace_root())
    except ValueError as e:
        result = {"error": str(e)}
        _record_tool_evidence(
            "search_files",
            {"pattern": pattern, "path": path, "glob": glob, "workspace": _workspace},
            result,
        )
        return result
    cmd = [
        "grep",
        "-rn",
        *[f"--exclude-dir={name}" for name in sorted(IGNORED_DRAFT_DIRS)],
        "--include",
        glob or "*",
        "-E",
        pattern,
        search_path,
    ]

    try:
        proc = run_subprocess_sync(
            cmd, capture_output=True, text=True, timeout=30,
        )
        output = proc.stdout[:_MAX_RESULT_CHARS]
        lines = output.strip().split("\n") if output.strip() else []

        if len(proc.stdout or "") > _MAX_RESULT_CHARS:
            output += f"\n... (truncated)"

        result = {"matches": output, "count": len(lines)}
        _record_tool_evidence(
            "search_files",
            {"pattern": pattern, "path": path, "glob": glob, "workspace": _workspace},
            result,
        )
        return result
    except subprocess.TimeoutExpired:
        result = {"error": "Search timed out after 30s"}
        _record_tool_evidence(
            "search_files",
            {"pattern": pattern, "path": path, "glob": glob, "workspace": _workspace},
            result,
        )
        return result
    except Exception as e:
        result = {"error": str(e)}
        _record_tool_evidence(
            "search_files",
            {"pattern": pattern, "path": path, "glob": glob, "workspace": _workspace},
            result,
        )
        return result


def _handle_list_files(pattern: str, path: str | None = None, _workspace: str | None = None) -> dict:
    """List files matching a glob pattern."""

    try:
        base = pathlib.Path(_resolve_path(path, _workspace) if path else (_workspace or _patched_workspace_root()))
    except ValueError as e:
        result = {"error": str(e)}
        _record_tool_evidence(
            "list_files",
            {"pattern": pattern, "path": path, "workspace": _workspace},
            result,
        )
        return result

    try:
        matches = [
            match
            for match in sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
            if not _is_project_internal_relative_path(match.relative_to(base))
        ]
        # Limit results
        paths = [str(m.relative_to(base)) for m in matches[:100]]
        total = len(matches)

        result_text = "\n".join(paths)
        if len(result_text) > _MAX_RESULT_CHARS:
            result_text = result_text[:_MAX_RESULT_CHARS]

        result = {"files": paths, "total": total, "truncated": total > 100}
        _record_tool_evidence(
            "list_files",
            {"pattern": pattern, "path": path, "workspace": _workspace},
            result,
        )
        return result
    except Exception as e:
        result = {"error": str(e)}
        _record_tool_evidence(
            "list_files",
            {"pattern": pattern, "path": path, "workspace": _workspace},
            result,
        )
        return result

__all__ = [name for name in globals() if not name.startswith("__")]
