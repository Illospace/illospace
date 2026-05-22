"""Project-bound environment setup for agent-owned child processes."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
import os
import re
from types import SimpleNamespace

from brain.systems.cortex.project_context.runtime_context import project_runtime_context_from_payloads
from brain.systems.runs.execution_context import _agent_context

logger = logging.getLogger("agent")

_GITHUB_TOKEN_ENV_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")


@dataclass(frozen=True)
class ProjectExecutionEnv:
    env: dict[str, str] | None
    injected_env: list[str]
    git_auth_hosts: list[str]
    sensitive_values: list[str]


def _current_context_run_id() -> int | str | None:
    run = getattr(_agent_context, "run", None)
    return getattr(run, "run_id", None) or getattr(run, "id", None) or getattr(_agent_context, "run_id", None)


def _run_payload_context(run: object) -> dict | None:
    target_payload = dict(
        getattr(run, "target_metadata", None)
        or getattr(run, "target_ref", None)
        or {}
    )
    workspace_payload = dict(getattr(run, "workspace_ref", None) or {})
    runtime = project_runtime_context_from_payloads(workspace_payload, target_payload)
    snapshot = (
        runtime.get("project_context_snapshot")
        or target_payload.get("project_context_snapshot")
        or workspace_payload.get("project_context_snapshot")
    )
    if isinstance(snapshot, dict):
        target_payload["project_context_snapshot"] = snapshot

    workspace_root = (
        (runtime.get("project_workspace_manifest") or {}).get("workspace_root")
        or workspace_payload.get("resolved_workspace_root")
        or workspace_payload.get("workspace_root")
    )
    workspaces = (runtime.get("project_workspace_manifest") or {}).get("workspaces") or workspace_payload.get("workspaces")
    if (not isinstance(workspace_root, str) or not workspace_root.strip()) and isinstance(workspaces, list):
        workspace_root = next(
            (
                item["path"].strip()
                for item in workspaces
                if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].strip()
            ),
            None,
        )

    if not target_payload and not workspace_root:
        return None

    workspace_root = workspace_root.strip() if isinstance(workspace_root, str) and workspace_root.strip() else None
    defaults = {"workspace_root": workspace_root, "workspace_hint": workspace_root} if workspace_root else {}
    return {"binding": {"raw_target_metadata": target_payload}, "execution_defaults": defaults}


def _threadlocal_payload_context() -> dict | None:
    run = getattr(_agent_context, "run", None)
    if run is not None:
        context = _run_payload_context(run)
        if context:
            return context

    target_payload = dict(getattr(_agent_context, "target_ref", None) or {})
    workspace_payload = dict(getattr(_agent_context, "workspace_ref", None) or {})
    if not target_payload and not workspace_payload:
        return None
    return _run_payload_context(SimpleNamespace(target_ref=target_payload, workspace_ref=workspace_payload))


def _current_run_target_context() -> dict | None:
    """Load the current run target context, if the tool call is running inside one."""
    return _threadlocal_payload_context()


async def _async_current_run_target_context() -> dict | None:
    """Load the current run target context with native async DB access when needed."""
    context = _threadlocal_payload_context()
    if context:
        return context

    run_id = _current_context_run_id()
    if not run_id:
        return None
    try:
        from brain.platform.db.models.run import AgentRun
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        async with UnitOfWork() as uow:
            run = await uow.session.get(AgentRun, int(run_id))
            if run:
                return _run_payload_context(run)
    except Exception:
        return None
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


async def _async_current_workspace_root_hint() -> str | None:
    """Return the safest current workspace root hint using async context loading."""
    workspace_root = getattr(_agent_context, "workspace_root", None)
    if isinstance(workspace_root, str) and workspace_root.strip():
        return workspace_root

    context = await _async_current_run_target_context()
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


async def _async_current_project_token_context() -> dict:
    """Return the project identity used for project-bound vault tokens."""
    context = await _async_current_run_target_context() or {}
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

    workspace_root = getattr(_agent_context, "workspace_root", None) or await _async_current_workspace_root_hint()
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


def current_project_bound_env() -> dict[str, str]:
    """Resolve project-bound vault tokens for command env injection."""
    preloaded = getattr(_agent_context, "project_bound_env", None)
    if isinstance(preloaded, dict):
        return {str(key): str(value) for key, value in preloaded.items() if key and value is not None}
    logger.debug("project_bound_vault_env_skipped_sync_runtime")
    return {}


async def async_current_project_bound_env() -> dict[str, str]:
    """Resolve project-bound vault tokens for command env injection."""
    user_id = getattr(_agent_context, "user_id", None)
    if not user_id:
        return {}
    project_context = await _async_current_project_token_context()
    if not project_context.get("project_slug") and project_context.get("target_registry_id") is None:
        return {}
    try:
        from brain.systems.vault import async_resolve_project_bound_env_tokens

        resolved = await async_resolve_project_bound_env_tokens(
            actor_user_id=user_id,
            org_id=getattr(_agent_context, "org_id", None),
            project_slug=project_context.get("project_slug"),
            project_slugs=project_context.get("project_slugs"),
            target_registry_id=project_context.get("target_registry_id"),
        )
        return {str(key): str(value) for key, value in resolved.items() if key and value is not None}
    except Exception:
        logger.debug("project_bound_vault_env_resolution_failed", exc_info=True)
        return {}


def prepare_project_execution_env() -> ProjectExecutionEnv:
    project_env = current_project_bound_env()
    if not project_env:
        return ProjectExecutionEnv(
            env=None,
            injected_env=[],
            git_auth_hosts=[],
            sensitive_values=[],
        )

    run_env = os.environ.copy()
    run_env.update(project_env)
    git_auth_hosts, git_sensitive_values = _configure_project_bound_git_auth(run_env, project_env)
    if git_auth_hosts:
        _configure_project_bound_git_identity(run_env)
    return ProjectExecutionEnv(
        env=run_env,
        injected_env=sorted(project_env),
        git_auth_hosts=git_auth_hosts,
        sensitive_values=list(project_env.values()) + git_sensitive_values,
    )


async def async_prepare_project_execution_env() -> ProjectExecutionEnv:
    project_env = await async_current_project_bound_env()
    if not project_env:
        return ProjectExecutionEnv(
            env=None,
            injected_env=[],
            git_auth_hosts=[],
            sensitive_values=[],
        )

    run_env = os.environ.copy()
    run_env.update(project_env)
    git_auth_hosts, git_sensitive_values = _configure_project_bound_git_auth(run_env, project_env)
    if git_auth_hosts:
        _configure_project_bound_git_identity(run_env)
    return ProjectExecutionEnv(
        env=run_env,
        injected_env=sorted(project_env),
        git_auth_hosts=git_auth_hosts,
        sensitive_values=list(project_env.values()) + git_sensitive_values,
    )


def annotate_project_execution_result(result: dict, project_execution: ProjectExecutionEnv) -> None:
    if project_execution.injected_env:
        result["injected_env"] = project_execution.injected_env
    if project_execution.git_auth_hosts:
        result["git_auth_configured"] = project_execution.git_auth_hosts


def redact_sensitive_output(text: str, sensitive_values: list[str]) -> str:
    redacted = text
    values = sorted(
        {value for value in sensitive_values if value},
        key=len,
        reverse=True,
    )
    for value in values:
        redacted = redacted.replace(value, "[secret redacted]")
    return redacted


def _github_token_from_project_env(project_env: dict[str, str]) -> str | None:
    for env_name in _GITHUB_TOKEN_ENV_NAMES:
        token = (project_env.get(env_name) or "").strip()
        if token:
            return token
    return None


def _append_git_config_env(env: dict[str, str], key: str, value: str) -> None:
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        count = 0
    env[f"GIT_CONFIG_KEY_{count}"] = key
    env[f"GIT_CONFIG_VALUE_{count}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + 1)


def _clean_git_identity_text(value: object, *, limit: int = 120) -> str | None:
    text = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    return text[:limit] if text else None


def _fallback_git_email(user_id: str) -> str:
    local = re.sub(r"[^a-zA-Z0-9._-]+", "-", user_id).strip("._-").lower()
    return f"{local or 'unknown'}@users.noreply.illospace.local"


def _context_metadata_candidates() -> list[dict]:
    candidates: list[dict] = []
    for attr in ("execution_metadata", "metadata"):
        value = getattr(_agent_context, attr, None)
        if isinstance(value, dict):
            candidates.append(value)
    run = getattr(_agent_context, "run", None)
    for attr in ("metadata", "metadata_"):
        value = getattr(run, attr, None) if run is not None else None
        if isinstance(value, dict):
            candidates.append(value)
    return candidates


def _nested_dict(payload: dict, path: tuple[str, ...]) -> dict | None:
    current: object = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _metadata_actor(payload: dict) -> dict | None:
    for path in (
        ("actor",),
        ("illo_trigger", "actor"),
        ("work_intake", "actor"),
        ("execution_provenance", "actor"),
    ):
        actor = _nested_dict(payload, path)
        if actor:
            return actor
    return None


def _current_git_actor_identity() -> tuple[str, str] | None:
    user_id = _clean_git_identity_text(getattr(_agent_context, "user_id", None))
    org_id = _clean_git_identity_text(getattr(_agent_context, "org_id", None))
    name = (
        _clean_git_identity_text(getattr(_agent_context, "git_author_name", None))
        or _clean_git_identity_text(getattr(_agent_context, "actor_name", None))
        or _clean_git_identity_text(getattr(_agent_context, "user_name", None))
    )
    email = (
        _clean_git_identity_text(getattr(_agent_context, "git_author_email", None))
        or _clean_git_identity_text(getattr(_agent_context, "actor_email", None))
        or _clean_git_identity_text(getattr(_agent_context, "user_email", None))
    )

    for metadata in _context_metadata_candidates():
        actor = _metadata_actor(metadata)
        if not actor or actor.get("internal") is True:
            continue
        actor_org_id = _clean_git_identity_text(actor.get("org_id"))
        if org_id and actor_org_id and actor_org_id != org_id:
            continue
        actor_user_id = _clean_git_identity_text(actor.get("id") or actor.get("user_id"))
        if user_id and actor_user_id and actor_user_id != user_id:
            continue
        user_id = user_id or actor_user_id
        name = name or _clean_git_identity_text(actor.get("name") or actor.get("display_name"))
        email = email or _clean_git_identity_text(actor.get("email"))
        if name and email:
            break

    if not user_id and not name:
        return None
    name = name or user_id or "Illo user"
    if not email or "@" not in email:
        email = _fallback_git_email(user_id or name)
    return name, email


def _configure_project_bound_git_identity(env: dict[str, str]) -> bool:
    identity = _current_git_actor_identity()
    if not identity:
        return False
    name, email = identity
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = name
    env["GIT_COMMITTER_EMAIL"] = email
    return True


def _configure_project_bound_git_auth(
    env: dict[str, str],
    project_env: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Let plain git use a project-bound GitHub token without persisting config."""
    token = _github_token_from_project_env(project_env)
    if not token:
        return [], []

    credential = f"x-access-token:{token}"
    encoded = base64.b64encode(credential.encode("utf-8")).decode("ascii")
    auth_header = f"AUTHORIZATION: basic {encoded}"
    _append_git_config_env(env, "http.https://github.com/.extraheader", auth_header)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "never")
    return ["github.com"], [token, credential, encoded, auth_header]
