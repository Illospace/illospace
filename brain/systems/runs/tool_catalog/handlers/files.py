"""Files orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.common import *
from brain.systems.runs.project_execution_env import (
    annotate_project_execution_result as _annotate_project_execution_result,
    prepare_project_execution_env as _prepare_project_execution_env,
    redact_sensitive_output as _redact_sensitive_output,
)
from brain.platform.async_io import run_subprocess_sync


def _resolve_path(path: str, working_dir: str | None = None) -> str:
    """Resolve a path relative to workspace root. Enforces containment.

    Args:
        path: The path to resolve (relative or absolute).
        working_dir: Override workspace root (used by worktree isolation).
    """
    base = os.path.realpath(working_dir or _patched_workspace_root())
    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(base, path))

    # Path containment: must stay within workspace
    if not resolved.startswith(base + os.sep) and resolved != base:
        raise ValueError(f"Path escapes workspace: {path} → {resolved} (workspace: {base})")
    return resolved


import re as _re
import shlex as _shlex

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
        resolved = _resolve_path(path, _workspace)
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
        resolved = _resolve_path(path, _workspace)
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
    cmd = ["grep", "-rn", "--include", glob or "*", "-E", pattern, search_path]

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
    import pathlib

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
        matches = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
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
