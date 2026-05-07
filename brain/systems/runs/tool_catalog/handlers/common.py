"""Tool handler implementations for the Illo agent loop.

Contains all _handle_* functions that execute tool calls,
plus _get_tool_handlers() which builds the run map.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
import re as _re
import sys
import time
import uuid

from brain.kernel import config as brain_config
from brain.systems.runs import actions as action_audit
from brain.systems.runs.evidence import normalize_tool_call_evidence
from brain.systems.runs.execution_artifacts import (
    append_execution_artifacts,
    append_run_execution_artifacts,
    load_execution_artifacts,
)
from brain.systems.runs.execution_context import (
    _agent_context,
    bind_agent_context,
)
from brain.systems.runs.tool_catalog.registry import (
    action_manifest_tool_names,
    parallel_safe_tool_names,
)

logger = logging.getLogger("agent")

_MODEL_TIERS = {"high", "medium", "low", "local"}
_MODEL_TIER_ALIASES: dict[str, str] = {}
_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}

# Workspace root — configurable, defaults to project root
WORKSPACE_ROOT = os.environ.get(
    "ILLO_WORKSPACE_ROOT",
    str(brain_config.BRAIN_DIR),
)

# Max output size for tool results (from budget config)
_MAX_RESULT_CHARS = int(os.environ.get("BUDGET_MAX_TOOL_RESULT_CHARS", "10000"))
_PARALLEL_BATCH_SAFE_TOOL_NAMES = parallel_safe_tool_names(scope="batch")
_MAX_PARALLEL_BATCH_OPERATIONS = int(os.environ.get("AGENT_PARALLEL_BATCH_MAX_OPS", "12"))
_MAX_PARALLEL_BATCH_WORKERS = int(os.environ.get("AGENT_PARALLEL_BATCH_MAX_WORKERS", "6"))
_ACTION_MANIFEST_TOOL_NAMES = action_manifest_tool_names()

_CONCRETE_BLOCKER_MARKERS = (
    "relation \"cycles\" does not exist",
    "undefinedtable",
    "table does not exist",
    "missing table",
    "missing schema",
    "schema blocker",
    "migration",
    "worker_unavailable",
    "unavailable service",
    "service unavailable",
    "startup timeout",
    "invalid x-api-key",
    "api key",
    "credential",
    "permission denied",
)

_BLOCKED_REPLY_MARKERS = (
    "blocked",
    "cannot",
    "can't",
    "couldn't",
    "could not",
    "failed",
    "error",
    "unavailable",
    "missing",
)


def _is_missing_cycle_schema_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "undefinedtable" in message
        or 'relation "cycles" does not exist' in message
        or "relation 'cycles' does not exist" in message
    )


def _cycle_schema_missing_payload(exc: Exception) -> dict:
    return {
        "error": str(exc),
        "terminal": True,
        "blocker": "cycles_db_schema_missing",
        "instruction": (
            "The cycles database schema is missing. Do not retry or continue investigating this request. "
            "Tell the user that the server needs the cycles migration applied before cycles can be listed, "
            "created, updated, deleted, or run."
        ),
    }


def _looks_like_concrete_blocker_reply(reply: str, execution_context: str | None = None) -> bool:
    text = f"{reply}\n{execution_context or ''}".lower()
    return (
        any(marker in text for marker in _BLOCKED_REPLY_MARKERS)
        and any(marker in text for marker in _CONCRETE_BLOCKER_MARKERS)
    )


def _current_manifest_context() -> dict:
    from brain.systems.runs.cortex.recording import trace_id_for_run_id

    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")
    worker_name = _get_current_worker_name()
    actor_kind = "user" if user_id else "agent"
    actor = str(user_id or worker_name or "agent")
    run_id = getattr(run, "run_id", None) or execution_metadata.get("run_id")
    return {
        "actor": actor,
        "actor_id": str(user_id) if user_id else None,
        "actor_kind": actor_kind,
        "org_id": getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id"),
        "run_id": run_id,
        "trace_id": execution_metadata.get("trace_id") or trace_id_for_run_id(run_id),
        "worker_name": worker_name,
        "idea_id": getattr(_agent_context, "idea_id", None) or execution_metadata.get("idea_id"),
    }


def _build_action_manifest(
    tool_name: str,
    args: tuple,
    kwargs: dict,
) -> action_audit.ActionManifestCreate | None:
    return action_audit.build_action_manifest(
        tool_name,
        args,
        kwargs,
        context=_patched_private("_current_manifest_context", _current_manifest_context)(),
    )


def _wrap_action_manifest_audit(tool_name: str, handler):
    return action_audit.wrap_action_manifest_audit(
        tool_name,
        handler,
        context_factory=lambda: _patched_private(
            "_current_manifest_context",
            _current_manifest_context,
        )(),
    )


def _wrap_brain_encode(original_fn):
    """Wrap brain_encode to handle the 'type' → 'memory_type' rename."""
    def wrapper(**kwargs):
        if "type" in kwargs:
            kwargs["memory_type"] = kwargs.pop("type")
        kwargs.setdefault("user_id", getattr(_agent_context, "user_id", None))
        kwargs.setdefault("org_id", getattr(_agent_context, "org_id", None))
        kwargs.setdefault("idea_id", getattr(_agent_context, "idea_id", None))
        kwargs.setdefault("session_id", getattr(_agent_context, "session_id", None))
        run = getattr(_agent_context, "run", None)
        execution_metadata = getattr(_agent_context, "execution_metadata", None)
        run_id = getattr(run, "run_id", None)
        if not run_id and isinstance(execution_metadata, dict):
            run_id = execution_metadata.get("run_id")
        kwargs.setdefault("run_id", run_id)
        if kwargs.get("user_id") and not kwargs.get("org_id"):
            try:
                from brain.platform.db.repositories.unit_of_work import UnitOfWork
                from brain.platform.db.models.org import User
                with UnitOfWork() as uow:
                    user = uow.session.get(User, kwargs["user_id"])
                    if user and getattr(user, "org_id", None):
                        kwargs["org_id"] = user.org_id
            except Exception:
                pass
        if "visibility" not in kwargs:
            kwargs["visibility"] = "org" if kwargs.get("org_id") else "private"
        return original_fn(**kwargs)
    return wrapper


def _wrap_brain_recall(original_fn):
    """Inject run viewer context into recall tool calls."""
    def wrapper(**kwargs):
        execution_metadata = getattr(_agent_context, "execution_metadata", None)
        if kwargs.get("user_id") is None:
            kwargs["user_id"] = getattr(_agent_context, "user_id", None)
        if kwargs.get("org_id") is None:
            kwargs["org_id"] = getattr(_agent_context, "org_id", None)
        if isinstance(execution_metadata, dict):
            if kwargs.get("user_id") is None:
                kwargs["user_id"] = execution_metadata.get("user_id")
            if kwargs.get("org_id") is None:
                kwargs["org_id"] = execution_metadata.get("org_id")
        result = original_fn(**kwargs)
        _patched_private("_record_tool_evidence", _record_tool_evidence)(
            "brain_recall",
            kwargs,
            result,
        )
        return result
    return wrapper


def _invoke_with_threadlocal_context(handler, args: dict, threadlocal_context: dict) -> object:
    """Invoke a handler in another thread while preserving AgentRun-local context."""
    with bind_agent_context(threadlocal_context):
        return handler(**args)


def _normalize_workspace_entry(entry) -> dict[str, str] | None:
    """Normalize a workspace entry into {name, path}."""
    if isinstance(entry, str) and entry.strip():
        expanded = os.path.realpath(os.path.expanduser(entry.strip()))
        return {"name": os.path.basename(expanded), "path": expanded}
    if isinstance(entry, dict):
        raw_path = str(entry.get("path", "")).strip()
        if not raw_path:
            return None
        expanded = os.path.realpath(os.path.expanduser(raw_path))
        name = str(entry.get("name") or os.path.basename(expanded)).strip()
        return {"name": name, "path": expanded}
    return None


def _build_workspace_registry(
    workspace_root: str | None = None,
    allowed_workspaces: list[str | dict] | None = None,
) -> list[dict[str, str]]:
    """Return a deduplicated list of accessible workspace roots."""
    registry: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    for raw in ([workspace_root] if workspace_root else []) + list(allowed_workspaces or []):
        item = _normalize_workspace_entry(raw)
        if not item or item["path"] in seen_paths:
            continue
        registry.append(item)
        seen_paths.add(item["path"])

    return registry


def _select_workspace(
    workspace: str | None,
    workspace_root: str | None = None,
    allowed_workspaces: list[str | dict] | None = None,
) -> str | None:
    """Resolve an explicit workspace selector against the allowed workspace set."""
    registry = _build_workspace_registry(workspace_root, allowed_workspaces)
    default_root = registry[0]["path"] if registry else workspace_root
    if not workspace:
        return default_root
    if not registry and not workspace_root:
        return None

    requested = workspace.strip()
    requested_path = os.path.realpath(os.path.expanduser(requested))
    path_matches = [item for item in registry if item["path"] == requested_path]
    if path_matches:
        return path_matches[0]["path"]

    name_matches = [item for item in registry if item["name"] == requested or os.path.basename(item["path"]) == requested]
    if len(name_matches) == 1:
        return name_matches[0]["path"]
    if len(name_matches) > 1:
        options = ", ".join(sorted({item["path"] for item in name_matches}))
        raise ValueError(f"Workspace selector '{workspace}' is ambiguous. Matches: {options}")

    options = ", ".join(sorted(item["name"] for item in registry)) or "(none)"
    raise ValueError(f"Workspace '{workspace}' is not accessible in this run. Available: {options}")


def _current_run_target_context() -> dict | None:
    """Load the current run target context, if the tool call is running inside one."""
    run = getattr(_agent_context, "run", None)
    run_id = getattr(run, "run_id", None)
    if not run_id:
        return None

    try:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.systems.environment import load_run_target_context

        with UnitOfWork() as uow:
            return load_run_target_context(uow.session, run_id)
    except Exception:
        return None


def _current_workspace_root_hint() -> str | None:
    """Return the safest current workspace root hint available to helper wrappers."""
    workspace_root = getattr(_agent_context, "workspace_root", None)
    if isinstance(workspace_root, str) and workspace_root.strip():
        return workspace_root

    context = _current_run_target_context()
    if not context:
        return None

    defaults = context.get("execution_defaults") or {}
    workspace_hint = defaults.get("workspace_root") or defaults.get("workspace_hint")
    if isinstance(workspace_hint, str) and workspace_hint.strip():
        return workspace_hint
    return None


def _canonical_project_token_slug(value: object, *, require_repo_like: bool = False) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        from brain.systems.cortex.project_context.github import parse_github_repo_slug

        github_slug = parse_github_repo_slug(raw)
    except Exception:
        github_slug = None
    if github_slug:
        return github_slug.lower()
    if require_repo_like:
        return None
    return raw.lower()


def _project_context_token_slugs(raw_target: dict) -> list[str]:
    snapshot = raw_target.get("project_context_snapshot")
    if not isinstance(snapshot, dict):
        return []
    resources = snapshot.get("resources")
    if not isinstance(resources, list):
        return []

    slugs: list[str] = []

    def add(value: object, *, require_repo_like: bool = False) -> None:
        slug = _canonical_project_token_slug(value, require_repo_like=require_repo_like)
        if slug and slug not in slugs:
            slugs.append(slug)

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        git = resource.get("git") if isinstance(resource.get("git"), dict) else {}
        add(resource.get("repo"))
        add(resource.get("name"), require_repo_like=not ("/" in str(resource.get("name") or "")))
        for key in ("uri", "url", "html_url", "remote", "remote_url"):
            add(resource.get(key), require_repo_like=True)
        for key in ("repo", "remote", "remote_url", "url"):
            add(git.get(key), require_repo_like=True)
    return slugs


def _current_project_token_context() -> dict:
    """Return the project identity used for project-bound vault tokens."""
    context = _current_run_target_context() or {}
    binding = context.get("binding") or {}
    registry = context.get("registry") or binding.get("target_registry") or {}
    raw_target = binding.get("raw_target_metadata") or {}

    target_registry_id = registry.get("id") or binding.get("target_registry_id")
    project_slugs: list[str] = []

    def add_slug(value: object, *, require_repo_like: bool = False) -> None:
        slug = _canonical_project_token_slug(value, require_repo_like=require_repo_like)
        if slug and slug not in project_slugs:
            project_slugs.append(slug)

    add_slug(registry.get("slug"))
    for key in ("project_slug", "slug", "repo", "name"):
        add_slug(raw_target.get(key))
    for slug in _project_context_token_slugs(raw_target):
        add_slug(slug)

    workspace_root = getattr(_agent_context, "workspace_root", None) or _current_workspace_root_hint()
    if isinstance(workspace_root, str) and workspace_root.strip():
        add_slug(os.path.basename(os.path.realpath(workspace_root)))

    try:
        target_registry_id = int(target_registry_id) if target_registry_id is not None else None
    except (TypeError, ValueError):
        target_registry_id = None

    return {
        "project_slug": project_slugs[0] if project_slugs else None,
        "project_slugs": project_slugs,
        "target_registry_id": target_registry_id,
    }


def _get_current_run_id() -> str | None:
    """Get the current run_id from AgentRun context."""
    run = getattr(_agent_context, "run", None)
    return run.run_id if run else None


def _get_current_worker_name() -> str:
    """Get the current worker/skill name from AgentRun context."""
    worker_name = getattr(_agent_context, "worker_name", None)
    if worker_name:
        return worker_name
    run = getattr(_agent_context, "run", None)
    return run.skill_used or "coordinator" if run else "unknown"


def _coerce_issue_number(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _attach_issue_number(artifact: dict, issue_number: int | None) -> dict:
    normalized = dict(artifact)
    if (
        issue_number is not None
        and normalized.get("issue_number") is None
        and normalized.get("type") in {"branch", "pr", "merge"}
    ):
        normalized["issue_number"] = issue_number
    return normalized


def _persist_execution_artifacts(artifacts: list[dict], run_id: int | None = None) -> None:
    """Persist normalized execution artifacts to AgentRun context and run state."""
    if not artifacts:
        return

    from brain.systems.runs.cortex.recording import trace_id_for_run_id

    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    run_id = run_id or getattr(run, "run_id", None) or execution_metadata.get("run_id")
    trace_id = execution_metadata.get("trace_id") or trace_id_for_run_id(run_id)

    normalized_artifacts = [dict(artifact) for artifact in artifacts]

    if run_id:
        append_run_execution_artifacts(run_id=run_id, artifacts=normalized_artifacts)

    existing = getattr(_agent_context, "execution_artifacts", None)
    if existing is None:
        _agent_context.execution_artifacts = []
        existing = _agent_context.execution_artifacts

    for artifact in normalized_artifacts:
        if artifact not in existing:
            existing.append(artifact)

    execution_id = execution_metadata.get("execution_id")
    if execution_id:
        provenance = {
            "execution_id": execution_id,
            "run_id": execution_metadata.get("run_id", run_id),
            "trace_id": trace_id,
            "session_id": execution_metadata.get("session_id"),
            "node_id": execution_metadata.get("node_id") or execution_metadata.get("step_id"),
        }
        append_execution_artifacts(
            execution_id=execution_id,
            provenance=provenance,
            artifacts=[
                {**artifact, "provenance": provenance}
                for artifact in normalized_artifacts
            ],
        )


def _current_evidence_provenance() -> dict:
    """Return compact execution provenance for evidence ledger records."""
    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    return {
        "run_id": getattr(run, "run_id", None) or execution_metadata.get("run_id"),
        "execution_id": execution_metadata.get("execution_id"),
        "worker_id": execution_metadata.get("worker_id"),
        "node_id": execution_metadata.get("node_id") or execution_metadata.get("step_id"),
        "skill": execution_metadata.get("skill_name"),
        "session_id": execution_metadata.get("session_id") or getattr(_agent_context, "session_id", None),
    }


def _record_tool_evidence(tool_name: str, args: dict | None, result: object) -> None:
    """Persist backend-owned evidence records for a tool call."""
    provenance = {
        key: value
        for key, value in _current_evidence_provenance().items()
        if value not in (None, "", {})
    }
    records = normalize_tool_call_evidence(
        tool_name,
        args if isinstance(args, dict) else {},
        result,
        provenance=provenance,
    )
    if not records:
        return
    _patched_private(
        "_persist_execution_artifacts",
        _persist_execution_artifacts,
    )(records, run_id=provenance.get("run_id"))


def _wrap_tool_evidence(tool_name: str, handler):
    """Wrap a handler so successful backend tool output becomes evidence."""
    def wrapper(*args, **kwargs):
        result = handler(*args, **kwargs)
        evidence_args = dict(kwargs)
        if args:
            evidence_args["_args"] = list(args)
        _patched_private("_record_tool_evidence", _record_tool_evidence)(
            tool_name,
            evidence_args,
            result,
        )
        return result
    return wrapper


def _record_execution_artifacts(command: str, result: dict) -> None:
    """Persist structured execution artifacts for provenance questions."""
    exit_code = result.get("exit_code")
    stdout = result.get("stdout", "") or ""
    artifacts: list[dict] = []

    if exit_code == 0:
        if m := _re.search(r"\bgit\s+checkout\s+-b\s+([^\s]+)", command):
            artifacts.append({"type": "branch", "branch": m.group(1), "status": "created"})
        if m := _re.search(r"\bgit\s+switch\s+-c\s+([^\s]+)", command):
            artifacts.append({"type": "branch", "branch": m.group(1), "status": "created"})
        if m := _re.search(r"\bgit\s+worktree\s+add\b.*?\s+-b\s+([^\s]+)", command):
            artifacts.append({"type": "branch", "branch": m.group(1), "status": "created"})
        if (
            _re.search(r"\bgit\s+commit\b", command)
            and (m := _re.search(r"\[([^\s]+)\s+([0-9a-f]{7,40})\]\s+([^\n]+)", stdout))
        ):
            artifacts.append({
                "type": "commit",
                "branch": m.group(1),
                "sha": m.group(2),
                "summary": m.group(3).strip(),
            })
        if m := _re.search(r"\bgit\s+push\s+origin\s+([^\s]+)", command):
            artifacts.append({"type": "push", "remote": "origin", "branch": m.group(1), "status": "pushed"})

        pr_match = _re.search(r"https://github\.com/[^\s]+/pull/(\d+)", stdout)
        if pr_match:
            if (
                _re.search(r"\bgh\s+pr\s+merge\b", command)
                or _re.search(r"\b(?:merged|merge(?:d)?)\s+(?:a\s+)?(?:pr|pull request)\b", stdout, _re.I)
            ):
                artifacts.append({
                    "type": "merge",
                    "url": pr_match.group(0),
                    "number": int(pr_match.group(1)),
                    "status": "merged",
                })
            elif (
                _re.search(r"\bgh\s+pr\s+create\b", command)
                or _re.search(r"\b(?:created?|opened|submitted|raised)\s+(?:a\s+)?(?:pr|pull request)\b", stdout, _re.I)
            ):
                artifacts.append({
                    "type": "pr",
                    "url": pr_match.group(0),
                    "number": int(pr_match.group(1)),
                    "status": "created",
                })

        issue_match = _re.search(r"https://github\.com/[^\s]+/issues/(\d+)", stdout)
        if issue_match and (
            _re.search(r"\bgh\s+issue\s+create\b", command)
            or _re.search(r"\b(?:created?|opened|filed)\s+(?:a\s+)?issue\b", stdout, _re.I)
        ):
            artifacts.append({
                "type": "issue",
                "url": issue_match.group(0),
                "number": int(issue_match.group(1)),
                "status": "created",
            })

        if _looks_like_test_command(command):
            artifacts.append({
                "type": "test_run",
                "command": command,
                "status": "passed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "summary": (stdout or result.get("stderr", "") or "")[:500],
            })

    _patched_private(
        "_persist_execution_artifacts",
        _persist_execution_artifacts,
    )(artifacts)


def _looks_like_test_command(command: str) -> bool:
    lowered = (command or "").lower()
    return any(
        hint in lowered
        for hint in (
            "pytest",
            "python -m pytest",
            "unittest",
            "npm test",
            "pnpm test",
            "yarn test",
            "go test",
            "cargo test",
            "make test",
        )
    )


def _require_run_id(tool_name: str) -> tuple[str | None, str | None]:
    """Return (run_id, error_json). If error_json is set, return it immediately."""
    run_id = _patched_private("_get_current_run_id", _get_current_run_id)()
    if not run_id:
        return None, json.dumps({"error": f"No active AgentRun — {tool_name} requires a run context"})
    return run_id, None


def _patched_private(name: str, default):
    """Resolve private patch points exposed by brain.systems.runs.tool_handlers."""
    facade = sys.modules.get("brain.systems.runs.tool_handlers")
    if facade is None:
        return default
    return getattr(facade, name, default)


def _patched_workspace_root() -> str:
    return _patched_private("WORKSPACE_ROOT", WORKSPACE_ROOT)


_VALID_SECTIONS = frozenset({"findings", "decisions", "open_questions", "resources", "handoffs"})


__all__ = [name for name in globals() if not name.startswith("__")]
