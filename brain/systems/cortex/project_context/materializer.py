"""Materialize Project Context resources into run-readable workspaces."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import inspect
import os
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from brain.contracts.github import parse_github_repo_slug
from brain.platform.async_io import check_output_sync, run_blocking, run_subprocess_sync
from brain.platform.db.models.run import AgentRun
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.project_context.drafts import load_draft_metadata, sync_draft_from_root
from brain.systems.cortex.project_context.identity import durable_project_id_from_context
from brain.systems.cortex.project_context.permissions import derive_project_permission_scope
from brain.systems.cortex.project_context.project_root import is_synthetic_project_root_resource, project_key_from_context
from brain.systems.cortex.project_context.resource_imports import (
    ResourcePathAccess,
    backend_readable_resource_path,
    path_is_relative_to,
    runtime_workspace_path,
    should_use_existing_resource_path,
)
from brain.systems.cortex.project_context.root_materializer import materialize_project_native_root
from brain.systems.cortex.project_context.runtime_context import (
    PROJECT_RUNTIME_CONTEXT_KEY,
    build_project_runtime_context,
)
from brain.systems.cortex.project_context.snapshot import snapshot_from_project_context
from brain.systems.cortex.project_context.workspace_manifest import (
    ThreadDraftIdentity,
    normalize_project_workspace_manifest,
)
from brain.systems.vault import async_get_secret, async_resolve_project_bound_env_tokens, list_secrets


_REPO_RESOURCE_KINDS = {
    "repo",
    "repository",
    "github",
    "github_repo",
    "github_repository",
}
_LOCAL_FIRST_RESOURCE_KINDS = {"file", "folder", "directory", "workspace", "docs", "doc"}
_DEFAULT_GIT_CLONE_TIMEOUT_SECONDS = 600
_MIN_GIT_CLONE_TIMEOUT_SECONDS = 30
_MAX_GIT_CLONE_TIMEOUT_SECONDS = 1800


@dataclass
class ProjectContextMaterializationResult:
    workspaces: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    degraded_resources: list[dict[str, str]] = field(default_factory=list)
    failed_resources: list[dict[str, str]] = field(default_factory=list)
    resources_checked: int = 0
    empty_project: bool = False

    @property
    def ready(self) -> bool:
        return (bool(self.workspaces) or self.empty_project) and not self.errors

    @property
    def ok(self) -> bool:
        return self.ready

    @property
    def status(self) -> str:
        if not self.ready:
            return "failed"
        if self.warnings:
            return "degraded"
        return "materialized"

    @property
    def evidence_health(self) -> dict[str, Any]:
        if self.status == "degraded":
            return {
                "status": "degraded",
                "warnings": self.warnings[:10],
                "degraded_resources": self.degraded_resources[:10],
            }
        if self.status == "failed":
            return {
                "status": "unavailable",
                "errors": self.errors[:10],
                "failed_resources": self.failed_resources[:10],
            }
        return {"status": "ok"}

    def fail(
        self,
        message: str,
        *,
        resource: dict[str, str] | None = None,
    ) -> "ProjectContextMaterializationResult":
        self.errors.append(message)
        if resource:
            self.failed_resources.append(resource)
        return self

    def degrade(
        self,
        message: str,
        *,
        resource: dict[str, str],
    ) -> "ProjectContextMaterializationResult":
        self.warnings.append(message)
        self.degraded_resources.append(resource)
        return self

    def fail_if_no_usable_workspace(self, usable_workspace_count: int) -> None:
        if usable_workspace_count or not self.warnings:
            return
        self.errors.extend(self.warnings)
        self.failed_resources.extend(self.degraded_resources)
        self.warnings.clear()
        self.degraded_resources.clear()


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _resource_kind(resource: dict[str, Any]) -> str:
    return (_clean_text(resource.get("kind") or resource.get("type") or resource.get("resource_type")) or "resource").lower()


def _looks_like_github_remote(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.startswith("git@github.com:")
        or lowered.startswith("https://github.com/")
        or lowered.startswith("http://github.com/")
        or lowered.startswith("github://")
        or lowered.startswith("github.com/")
    )


def _github_slug_from_resource(resource: dict[str, Any]) -> str | None:
    explicit_repo = _clean_text(resource.get("repo"))
    if explicit_repo:
        slug = parse_github_repo_slug(explicit_repo)
        if slug:
            return slug

    kind = _resource_kind(resource)
    source = (_clean_text(resource.get("source")) or "").lower()
    is_repo_resource = kind in _REPO_RESOURCE_KINDS or source in _REPO_RESOURCE_KINDS

    for key in ("uri", "url", "remote", "repo_url"):
        value = _clean_text(resource.get(key))
        if value and _looks_like_github_remote(value):
            slug = parse_github_repo_slug(value)
            if slug:
                return slug

    if is_repo_resource:
        for key in ("name",):
            value = _clean_text(resource.get(key))
            if not value:
                continue
            slug = parse_github_repo_slug(value)
            if slug:
                return slug
    return None


def _resource_failure_is_soft(resource: dict[str, Any]) -> bool:
    return bool(_github_slug_from_resource(resource)) and resource.get("required") is not True


def _degraded_resource_payload(resource: dict[str, Any], error: str) -> dict[str, str]:
    payload = {"kind": _resource_kind(resource), "error": error}
    for key, value in (
        ("id", _clean_text(resource.get("id"))),
        ("name", _clean_text(resource.get("name") or resource.get("label"))),
        ("repo", _github_slug_from_resource(resource)),
    ):
        if value:
            payload[key] = value
    return payload


def _normalise_repo_subpath(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    path = PurePosixPath(text.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix() or None


def _github_uri_subpath(value: Any) -> str | None:
    text = _clean_text(value)
    if not text or not text.lower().startswith("github://"):
        return None
    body = text.split("://", 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [part for part in body.split("/") if part]
    if len(parts) <= 2:
        return None
    return _normalise_repo_subpath("/".join(parts[2:]))


def _github_subpath_from_resource(resource: dict[str, Any]) -> str | None:
    for key in ("subpath", "path_in_repo", "relative_path"):
        subpath = _normalise_repo_subpath(resource.get(key))
        if subpath:
            return subpath
    for key in ("uri", "url"):
        subpath = _github_uri_subpath(resource.get(key))
        if subpath:
            return subpath
    return None


def project_context_has_materializable_resources(context: dict[str, Any] | None) -> bool:
    resources = context.get("resources") if isinstance(context, dict) else None
    if isinstance(resources, list):
        return True
    if isinstance(context, dict) and context.get("project_context_snapshot"):
        snapshot = context.get("project_context_snapshot")
        return isinstance(snapshot, dict) and isinstance(snapshot.get("resources"), list)
    return False


def _vault_key_from_resource(resource: dict[str, Any]) -> str | None:
    for key in ("vault_key", "github_vault_key"):
        value = _clean_text(resource.get(key))
        if value:
            return value
    for key in ("credential_ref", "credentials"):
        ref = resource.get(key)
        if not isinstance(ref, dict):
            continue
        provider = _clean_text(ref.get("provider"))
        ref_type = _clean_text(ref.get("type"))
        if provider and provider.lower() != "github":
            continue
        if ref_type and ref_type.lower() not in {"vault", "vault_secret", "secret"}:
            continue
        value = _clean_text(ref.get("key_name") or ref.get("vault_key") or ref.get("name"))
        if value:
            return value
    return None


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _github_secret_names(user_id: str, org_id: str | None) -> list[str]:
    def append_names(secrets: Any, *, github_like_only: bool = False) -> None:
        for secret in (secrets if isinstance(secrets, list) else []):
            name = _clean_text(secret.get("key_name") if isinstance(secret, dict) else None)
            if not name or name in seen:
                continue
            if github_like_only and "github" not in name.lower() and name.upper() not in {"GH_TOKEN"}:
                continue
            seen.add(name)
            names.append(name)

    names: list[str] = []
    seen: set[str] = set()
    try:
        candidates = await _maybe_await(list_secrets(actor_user_id=user_id, category="github", org_id=org_id))
    except Exception:
        candidates = []
    append_names(candidates)
    try:
        general_candidates = await _maybe_await(list_secrets(actor_user_id=user_id, org_id=org_id))
    except Exception:
        general_candidates = []
    append_names(general_candidates, github_like_only=True)
    return names[:5]


def _vault_read_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


async def _token_candidates(
    resource: dict[str, Any],
    user_id: str | None,
    org_id: str | None,
) -> list[tuple[str | None, str | None, str | None, str]]:
    explicit_key = _vault_key_from_resource(resource)
    candidates: list[tuple[str | None, str | None, str | None, str]] = []
    if not user_id:
        if explicit_key:
            return [(explicit_key, None, "Vault credential requires a run user_id context.", "vault")]
        return [(None, None, None, "public")]

    key_names: list[str] = []
    if explicit_key:
        key_names.append(explicit_key)

    seen_keys: set[str] = set()
    seen_tokens: set[str] = set()

    async def add_vault_keys() -> None:
        for key_name in key_names:
            if key_name in seen_keys:
                continue
            seen_keys.add(key_name)
            try:
                token = await async_get_secret(
                    key_name,
                    actor_user_id=user_id,
                    org_id=org_id,
                    accessed_by="github_runtime_tool",
                )
            except Exception as exc:
                candidates.append((key_name, None, _vault_read_error_message(exc), "vault"))
                continue
            if token:
                value = token.strip()
                if value and value not in seen_tokens:
                    seen_tokens.add(value)
                    candidates.append((key_name, value, None, "vault"))
            else:
                candidates.append((key_name, None, "Vault secret was not found or is empty.", "vault"))

    await add_vault_keys()

    slug = _github_slug_from_resource(resource)
    if slug and org_id:
        try:
            bound_env = await async_resolve_project_bound_env_tokens(
                actor_user_id=user_id,
                org_id=org_id,
                project_slug=slug,
            )
        except Exception:
            bound_env = {}
        if not isinstance(bound_env, dict):
            bound_env = {}
        preferred_names = ("GITHUB_TOKEN", "GH_TOKEN")
        ordered_names = [
            *[name for name in preferred_names if name in bound_env],
            *sorted(
                name
                for name in bound_env
                if name not in preferred_names
                and not ("__" in name.upper() and name.upper().endswith("_LEGACY"))
            ),
        ]
        for env_name in ordered_names:
            token = _clean_text(bound_env.get(env_name))
            if not token or token in seen_tokens:
                continue
            seen_tokens.add(token)
            candidates.append((None, token, None, "project_binding"))

    if not explicit_key:
        key_names.extend(await _github_secret_names(user_id, org_id))
        await add_vault_keys()

    if not explicit_key:
        candidates.append((None, None, None, "public"))
    return candidates


def _safe_repo_destination(root: Path, slug: str) -> Path:
    owner, repo = slug.split("/", 1)
    owner = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in owner)[:80]
    repo = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in repo)[:120]
    return root / ".illo-project-context" / "github" / owner / repo


def _git_output(cwd: Path, *args: str) -> str | None:
    try:
        return check_output_sync(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip() or None
    except Exception:
        return None


def _existing_git_checkout(destination: Path, slug: str, branch: str | None) -> dict[str, str] | None:
    """Return metadata for an existing checkout without mutating local state.

    BLOCKING: reaches ``check_output_sync`` via ``_git_output``. Async callers
    MUST go through ``run_blocking`` (see #860).
    """

    if not destination.exists():
        return None
    top_level = _git_output(destination, "rev-parse", "--show-toplevel")
    if not top_level or Path(top_level).resolve() != destination.resolve():
        return None
    actual_branch = _git_output(destination, "branch", "--show-current") or ""
    remote = _git_output(destination, "remote", "get-url", "origin") or ""
    if remote and parse_github_repo_slug(remote) != slug:
        return None
    return {
        "path": str(destination),
        "branch": actual_branch or branch or "",
        "commit": _git_output(destination, "rev-parse", "HEAD") or "",
    }


def _askpass_script(parent: Path) -> Path:
    handle = tempfile.NamedTemporaryFile("w", dir=str(parent), prefix=".github-askpass-", delete=False)
    with handle:
        handle.write(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
            "  *) printf '%s\\n' \"$ILLO_GITHUB_PROJECT_CONTEXT_TOKEN\" ;;\n"
            "esac\n"
        )
    path = Path(handle.name)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def _git_clone_timeout_seconds() -> int:
    raw = os.getenv("ILLO_PROJECT_CONTEXT_GIT_CLONE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_GIT_CLONE_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_GIT_CLONE_TIMEOUT_SECONDS
    return max(_MIN_GIT_CLONE_TIMEOUT_SECONDS, min(value, _MAX_GIT_CLONE_TIMEOUT_SECONDS))


def _clone_github_repo(
    slug: str,
    destination: Path,
    *,
    token: str | None,
    branch: str | None,
) -> dict[str, str]:
    """Clone a GitHub repo. BLOCKING: rmtree, mkdir, git subprocess, git reads.

    Async callers MUST go through ``run_blocking``. Calling this directly from a
    coroutine freezes the whole worker for the clone's duration, which trips the
    queue-stall watchdog and restart-loops the process (see #860).
    """
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    askpass = _askpass_script(destination.parent) if token else None
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token and askpass:
        env["GIT_ASKPASS"] = str(askpass)
        env["ILLO_GITHUB_PROJECT_CONTEXT_TOKEN"] = token

    branches = [branch] if branch else []
    branches.append(None)
    try:
        last_error = ""
        for selected_branch in dict.fromkeys(branches):
            command = [
                "git",
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--no-tags",
                "--filter",
                "blob:none",
            ]
            if selected_branch:
                command.extend(["--branch", selected_branch])
            command.extend([f"https://github.com/{slug}.git", str(destination)])
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            proc = run_subprocess_sync(
                command,
                capture_output=True,
                text=True,
                timeout=_git_clone_timeout_seconds(),
                env=env,
            )
            if proc.returncode == 0:
                return {
                    "path": str(destination),
                    "branch": _git_output(destination, "branch", "--show-current") or selected_branch or "",
                    "commit": _git_output(destination, "rev-parse", "HEAD") or "",
                }
            last_error = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(_sanitize_git_error(last_error) or "git clone failed")
    finally:
        if askpass:
            try:
                askpass.unlink()
            except OSError:
                pass
        env.pop("ILLO_GITHUB_PROJECT_CONTEXT_TOKEN", None)


def _sanitize_git_error(error: str) -> str:
    cleaned = " ".join((error or "").split())
    if not cleaned:
        return ""
    return cleaned.replace("x-access-token", "***")[:500]


def _merge_workspaces(existing: Any, additions: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for item in [*(existing if isinstance(existing, list) else []), *additions]:
        if isinstance(item, str):
            path = item.strip()
            name = Path(path).name
        elif isinstance(item, dict):
            path = _clean_text(item.get("path")) or ""
            name = _clean_text(item.get("name")) or Path(path).name
        else:
            continue
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        merged.append({"name": name, "path": path})
    return merged


def _resource_workspace_name(resource: dict[str, Any], path: Path) -> str:
    return (
        _clean_text(resource.get("mount_path"))
        or _clean_text(resource.get("project_path"))
        or _clean_text(resource.get("name"))
        or _clean_text(resource.get("label"))
        or _clean_text(resource.get("repo"))
        or path.name
        or str(path)
    )


def _workspace_entry_from_resource(resource: dict[str, Any], path: Path | str) -> dict[str, str]:
    workspace_path = Path(path)
    return {"name": _resource_workspace_name(resource, workspace_path), "path": str(workspace_path)}


def _default_workspace_path(
    workspaces: list[dict[str, str]],
    *,
    root_workspace: dict[str, str],
    root_empty: bool,
) -> str | None:
    root_path = _clean_text(root_workspace.get("path"))
    if root_empty:
        for workspace in workspaces:
            path = _clean_text(workspace.get("path"))
            if path and path != root_path:
                return path
    return root_path or next(
        (_clean_text(workspace.get("path")) for workspace in workspaces if _clean_text(workspace.get("path"))),
        None,
    )


def _repo_workspace_path(resource: dict[str, Any], repo_path: Path) -> tuple[Path | None, str | None]:
    subpath = _github_subpath_from_resource(resource)
    if not subpath:
        return repo_path, None
    workspace_path = (repo_path / subpath).expanduser()
    if not path_is_relative_to(workspace_path, repo_path):
        return None, f"GitHub Project subpath escapes repository root: {subpath}"
    if not workspace_path.exists():
        return None, f"GitHub Project subpath does not exist in {resource.get('repo') or repo_path.name}: {subpath}"
    return workspace_path, subpath


def _mark_github_materialized(
    resource: dict[str, Any],
    *,
    slug: str,
    repo_path: Path,
    workspace_path: Path,
    branch: str | None,
    commit: str | None,
    credential: str,
    subpath: str | None = None,
    reused: bool = False,
) -> dict[str, str]:
    resource["path"] = str(workspace_path)
    resource["repo"] = slug
    materialization = {
        "status": "ready",
        "provider": "github",
        "repo": slug,
        "branch": branch,
        "commit": commit,
        "credential": credential,
    }
    if subpath:
        materialization["path"] = str(workspace_path)
        materialization["workspace_path"] = str(workspace_path)
        materialization["repo_path"] = str(repo_path)
        materialization["subpath"] = subpath
    if reused:
        materialization["reused"] = True
    resource["materialization"] = materialization
    return _workspace_entry_from_resource(resource, workspace_path)


def _draft_equivalent_path(value: Any, source_path: Path, draft_path: Path) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        return text
    try:
        if source_path.is_file():
            if path.resolve() == source_path.resolve():
                return str(draft_path)
            return text
        relative = path.resolve().relative_to(source_path.resolve())
    except Exception:
        return text
    return str(draft_path / relative)


def _rewrite_scope_paths_to_draft(resource: dict[str, Any], source_path: Path, draft_path: Path) -> None:
    def rewrite_list(values: Any) -> Any:
        if not isinstance(values, list):
            return values
        rewritten = []
        for value in values:
            mapped = _draft_equivalent_path(value, source_path, draft_path)
            rewritten.append(mapped if mapped is not None else value)
        return rewritten

    for key in ("allowed_paths", "files", "folders", "forbidden_paths", "denied_paths"):
        if key in resource:
            resource[key] = rewrite_list(resource.get(key))

    path_keys = (
        "allowed_paths",
        "read",
        "write",
        "read_write",
        "files",
        "folders",
        "forbidden_paths",
        "deny",
        "denied_paths",
    )
    for key in ("permissions", "scope"):
        nested = resource.get(key)
        if not isinstance(nested, dict):
            continue
        for path_key in path_keys:
            if path_key in nested:
                nested[path_key] = rewrite_list(nested.get(path_key))


def _sync_resource_to_draft(source_path: Path, draft_path: Path) -> dict[str, list[str]]:
    draft_workspace_path = draft_path.parent if source_path.is_file() else draft_path
    had_base_manifest = bool(load_draft_metadata(draft_workspace_path).get("base_manifest"))
    result = sync_draft_from_root(source_path, draft_workspace_path)
    if not had_base_manifest:
        return {"updated_from_root": [], "removed_from_root": [], "conflicts": [], "out_of_date": []}
    return {
        "updated_from_root": result.copied,
        "removed_from_root": result.removed,
        "conflicts": result.conflicts,
        "out_of_date": result.out_of_date,
    }


def _is_project_root_resource(resource: dict[str, Any]) -> bool:
    return is_synthetic_project_root_resource(resource)


def _is_project_native_seed_resource(resource: dict[str, Any]) -> bool:
    if _is_project_root_resource(resource) or _github_slug_from_resource(resource):
        return False
    return _resource_kind(resource) in _LOCAL_FIRST_RESOURCE_KINDS


def _materialize_backend_readable_resource(
    resource: dict[str, Any],
    *,
    workspace_root: Path,
    project_context: dict[str, Any] | None = None,
) -> tuple[dict[str, str] | None, str | None, bool]:
    materialization = resource.get("materialization") if isinstance(resource.get("materialization"), dict) else {}
    previous_source = should_use_existing_resource_path(
        _clean_text(materialization.get("source_path")),
        workspace_root,
        access=ResourcePathAccess.READ_ONLY,
    )
    previous_draft = should_use_existing_resource_path(
        _clean_text(resource.get("path") or materialization.get("path")),
        workspace_root,
        access=ResourcePathAccess.WRITE,
    )
    if previous_source and previous_draft and path_is_relative_to(previous_draft, workspace_root):
        existing_path = previous_draft
        source_path = previous_source
        checked = True
    else:
        existing_path, checked = backend_readable_resource_path(resource, workspace_root=workspace_root)
        source_path = existing_path
    if not existing_path:
        return None, None, checked

    if source_path is None:
        source_path = existing_path
    draft = False
    draft_status: dict[str, list[str]] | None = None
    draft_identity: ThreadDraftIdentity | None = None
    if not path_is_relative_to(existing_path, workspace_root):
        draft_identity = ThreadDraftIdentity.from_project_resource(
            {**resource, "path": str(existing_path)},
            thread_workspace_root=workspace_root,
            project_context=project_context,
        )
        draft_path = Path(draft_identity.draft_resource_path)
        try:
            draft_status = _sync_resource_to_draft(existing_path, draft_path)
        except Exception as exc:
            message = f"Could not create Project draft workspace for {existing_path}: {exc}"
            resource["materialization"] = {
                "status": "failed",
                "provider": "local",
                "kind": _resource_kind(resource),
                "source_path": str(existing_path),
                "error": message,
            }
            return None, message, True
        existing_path = draft_path
        draft = True
        _rewrite_scope_paths_to_draft(resource, source_path, draft_path)
    elif previous_source and source_path != existing_path:
        try:
            draft_status = _sync_resource_to_draft(source_path, existing_path)
        except Exception as exc:
            message = f"Could not refresh Project draft workspace for {existing_path}: {exc}"
            resource["materialization"] = {
                "status": "failed",
                "provider": "local",
                "kind": _resource_kind(resource),
                "source_path": str(source_path),
                "path": str(existing_path),
                "error": message,
            }
            return None, message, True
        draft = True
        _rewrite_scope_paths_to_draft(resource, source_path, existing_path)

    workspace_path = runtime_workspace_path(existing_path)
    kind = _resource_kind(resource)
    resource["path"] = str(existing_path)
    materialization = {
        "status": "ready",
        "provider": "local",
        "kind": kind,
        "path": str(existing_path),
        "workspace_path": str(workspace_path),
    }
    if draft:
        materialization["source_path"] = str(source_path)
        materialization["draft"] = True
        if draft_identity and draft_identity.project_key:
            materialization["project_key"] = draft_identity.project_key
    if draft_status and any(draft_status.values()):
        materialization["draft_status"] = draft_status
    resource["materialization"] = materialization
    return _workspace_entry_from_resource(resource, workspace_path), None, True


async def _materialize_resource(
    resource: dict[str, Any],
    *,
    workspace_root: Path,
    user_id: str | None,
    org_id: str | None,
    project_context: dict[str, Any] | None = None,
) -> tuple[dict[str, str] | None, str | None]:
    if _resource_kind(resource) in _LOCAL_FIRST_RESOURCE_KINDS:
        workspace, error, checked = _materialize_backend_readable_resource(
            resource,
            workspace_root=workspace_root,
            project_context=project_context,
        )
        if checked:
            return workspace, error

    slug = _github_slug_from_resource(resource)
    if not slug:
        workspace, error, _checked = _materialize_backend_readable_resource(
            resource,
            workspace_root=workspace_root,
            project_context=project_context,
        )
        return workspace, error
    existing_path = should_use_existing_resource_path(
        _clean_text(resource.get("path")),
        workspace_root,
        access=ResourcePathAccess.WRITE,
    )
    if existing_path and path_is_relative_to(existing_path, workspace_root):
        return _workspace_entry_from_resource(resource, existing_path), None

    destination = _safe_repo_destination(workspace_root, slug)
    branch = _clean_text(resource.get("branch") or resource.get("default_branch") or resource.get("branch_hint"))
    existing_checkout = await run_blocking(_existing_git_checkout, destination, slug, branch)
    if existing_checkout:
        clone = existing_checkout
        repo_path = Path(clone["path"])
        workspace_path, subpath = _repo_workspace_path(resource, repo_path)
        if workspace_path is None:
            message = subpath or f"Could not resolve GitHub Project workspace for {slug}."
            resource["materialization"] = {
                "status": "failed",
                "provider": "github",
                "repo": slug,
                "error": message,
            }
            return None, message
        return _mark_github_materialized(
            resource,
            slug=slug,
            repo_path=repo_path,
            workspace_path=workspace_path,
            branch=clone.get("branch") or branch,
            commit=clone.get("commit") or None,
            credential="existing",
            subpath=subpath,
            reused=True,
        ), None
    if destination.exists() and any(destination.iterdir()):
        message = (
            f"Project context destination {destination} already exists but is not a matching GitHub "
            f"checkout for {slug}; refusing to overwrite live workspace state."
        )
        resource["materialization"] = {
            "status": "failed",
            "provider": "github",
            "repo": slug,
            "error": message,
        }
        return None, message

    errors: list[str] = []
    for candidate in await _token_candidates(resource, user_id, org_id):
        key_name, token = candidate[:2]
        token_error = candidate[2] if len(candidate) > 2 else None
        credential = candidate[3] if len(candidate) > 3 else ("vault" if key_name else "public")
        if token_error:
            prefix = f"Vault key {key_name}: " if key_name else "GitHub credential: "
            errors.append(prefix + token_error)
            continue
        try:
            clone = await run_blocking(_clone_github_repo, slug, destination, token=token, branch=branch)
        except Exception as exc:
            prefix = f"Vault key {key_name}: " if key_name else "public clone: "
            errors.append(prefix + _sanitize_git_error(str(exc)))
            continue
        repo_path = Path(clone["path"])
        workspace_path, subpath = _repo_workspace_path(resource, repo_path)
        if workspace_path is None:
            errors.append(subpath or f"Could not resolve GitHub Project workspace for {slug}.")
            continue
        if key_name and "credential_ref" not in resource:
            resource["credential_ref"] = {
                "type": "vault_secret",
                "provider": "github",
                "key_name": key_name,
            }
        return _mark_github_materialized(
            resource,
            slug=slug,
            repo_path=repo_path,
            workspace_path=workspace_path,
            branch=clone.get("branch") or branch,
            commit=clone.get("commit") or None,
            credential=credential,
            subpath=subpath,
        ), None

    resource["materialization"] = {
        "status": "failed",
        "provider": "github",
        "repo": slug,
        "error": errors[-1] if errors else "No usable GitHub credential found.",
    }
    return None, f"Could not materialize GitHub repository {slug}: {resource['materialization']['error']}"


def _run_target_payload(run: Any) -> dict[str, Any]:
    if hasattr(run, "target_metadata"):
        return dict(getattr(run, "target_metadata") or {})
    return dict(getattr(run, "target_ref", None) or {})


def _set_run_target_payload(run: Any, payload: dict[str, Any]) -> None:
    if hasattr(run, "target_metadata"):
        run.target_metadata = payload
    else:
        run.target_ref = payload


def _run_workspace_payload(run: Any) -> dict[str, Any]:
    return dict(getattr(run, "workspace_ref", None) or {})


def _set_run_workspace_payload(run: Any, payload: dict[str, Any]) -> None:
    if hasattr(run, "workspace_ref"):
        run.workspace_ref = payload


def _snapshot_from_run(run: Any) -> dict[str, Any] | None:
    target_payload = _run_target_payload(run)
    workspace_payload = _run_workspace_payload(run)
    snapshot = target_payload.get("project_context_snapshot") or workspace_payload.get("project_context_snapshot")
    if isinstance(snapshot, dict):
        return dict(snapshot)
    if isinstance(workspace_payload.get("resources"), list) and workspace_payload.get("resources"):
        return snapshot_from_project_context(workspace_payload, validate_local_paths=False)
    return None


def _identity_sources_from_run(
    *,
    snapshot: dict[str, Any],
    target_payload: dict[str, Any],
    workspace_payload: dict[str, Any],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = [snapshot, target_payload, workspace_payload]
    for payload in (target_payload, workspace_payload, metadata):
        for key in ("project_context", "project_context_snapshot"):
            value = payload.get(key)
            if isinstance(value, dict):
                sources.append(value)
    return sources


def _stamp_materialization_identity(
    snapshot: dict[str, Any],
    *,
    target_payload: dict[str, Any],
    workspace_payload: dict[str, Any],
    metadata: dict[str, Any],
    resources: list[dict[str, Any]],
    fallback: str,
) -> str:
    for source in _identity_sources_from_run(
        snapshot=snapshot,
        target_payload=target_payload,
        workspace_payload=workspace_payload,
        metadata=metadata,
    ):
        durable_project_id = durable_project_id_from_context(source)
        if durable_project_id:
            snapshot["project_id"] = durable_project_id
            snapshot["project_key"] = durable_project_id
            return durable_project_id
    project_key = project_key_from_context(snapshot, resources=resources, fallback=fallback)
    snapshot["project_key"] = project_key
    return project_key


async def materialize_project_context_workspaces(
    run_id: int | None,
    *,
    workspace_root: str | None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> ProjectContextMaterializationResult:
    """Clone Project Context repos and expose them through AgentRun.workspace_ref."""

    result = ProjectContextMaterializationResult()
    if not run_id:
        return result.fail("Project Context materialization requires a run id.")
    if not workspace_root:
        return result.fail("Project Context materialization requires a workspace root.")

    root = Path(workspace_root).expanduser()
    async with UnitOfWork() as uow:
        run = await uow.session.get(AgentRun, run_id)
        if not run:
            return result.fail(f"Agent run {run_id} was not found.")
        metadata = dict(getattr(run, "metadata_", None) or {})
        target_payload = _run_target_payload(run)
        workspace_payload = _run_workspace_payload(run)
        snapshot = _snapshot_from_run(run)
        if not isinstance(snapshot, dict):
            return result.fail("Project Context snapshot is missing.")
        snapshot = dict(snapshot)
        resources = [dict(item) for item in snapshot.get("resources") or [] if isinstance(item, dict)]
        _stamp_materialization_identity(
            snapshot,
            target_payload=target_payload,
            workspace_payload=workspace_payload,
            metadata=metadata,
            resources=resources,
            fallback=f"run-{run_id}",
        )
        user_id = user_id or (str(run.user_id) if getattr(run, "user_id", None) else None)
        org_id = org_id or _clean_text(getattr(run, "org_id", None)) or _clean_text(metadata.get("org_id"))

    materialized_resources: list[dict[str, Any]] = []
    workspaces: list[dict[str, str]] = []
    root_resource, root_workspace = materialize_project_native_root(
        snapshot,
        resources,
        workspace_root=root,
        run_id=run_id,
        actor_id=user_id,
        org_id=org_id,
        is_project_native_seed=_is_project_native_seed_resource,
        workspace_entry_from_resource=_workspace_entry_from_resource,
    )
    materialized_resources.append(root_resource)
    workspaces.append(root_workspace)
    result.resources_checked += 1
    root_materialization = root_resource.get("materialization") if isinstance(root_resource.get("materialization"), dict) else {}
    root_empty = bool(root_materialization.get("root_empty", True))
    result.empty_project = root_empty
    usable_workspace_count = 0 if root_empty else 1

    for resource in resources:
        if _is_project_root_resource(resource):
            continue
        if _is_project_native_seed_resource(resource):
            continue
        workspace, error = await _materialize_resource(
            resource,
            workspace_root=root,
            user_id=user_id,
            org_id=org_id,
            project_context=snapshot,
        )
        if workspace or error or isinstance(resource.get("materialization"), dict):
            result.resources_checked += 1
            materialized_resources.append(resource)
        if workspace:
            workspaces.append(workspace)
            usable_workspace_count += 1
        if error:
            if _resource_failure_is_soft(resource):
                result.degrade(error, resource=_degraded_resource_payload(resource, error))
            else:
                result.fail(error, resource=_degraded_resource_payload(resource, error))

    result.fail_if_no_usable_workspace(usable_workspace_count)

    snapshot["resources"] = materialized_resources

    workspace_manifest = normalize_project_workspace_manifest(
        snapshot,
        workspaces=workspaces,
        thread_workspace_root=root,
    )
    workspace_manifest_payload = workspace_manifest.to_dict()
    default_workspace = _default_workspace_path(
        workspaces,
        root_workspace=root_workspace,
        root_empty=root_empty,
    )
    if default_workspace:
        workspace_manifest_payload["workspace_root"] = default_workspace
        workspace_manifest_payload["resolved_workspace_root"] = default_workspace
    snapshot["project_workspace_manifest"] = workspace_manifest_payload
    result.workspaces = workspaces
    project_context_materialization = {
        "status": result.status,
        "workspaces": workspaces,
        "workspace_manifest": workspace_manifest_payload,
        "errors": result.errors[:10],
        "warnings": result.warnings[:10],
        "degraded_resources": result.degraded_resources[:10],
        "failed_resources": result.failed_resources[:10],
        "evidence_health": result.evidence_health,
        "empty_project": result.empty_project,
        "seed_resource_count": len(resources),
        "project_root_path_count": int(root_materialization.get("root_path_count") or 0),
        "project_root_file_count": int(root_materialization.get("root_file_count") or 0),
        "project_draft_path_count": int(root_materialization.get("draft_path_count") or 0),
        "project_draft_file_count": int(root_materialization.get("draft_file_count") or 0),
    }
    snapshot["permission_scope"] = derive_project_permission_scope(snapshot)
    if result.errors:
        existing_errors = snapshot.get("validation_errors") if isinstance(snapshot.get("validation_errors"), list) else []
        snapshot["validation_errors"] = [*existing_errors, *result.errors]
        snapshot["status"] = "invalid"

    async with UnitOfWork() as uow:
        run = await uow.session.get(AgentRun, run_id)
        if not run:
            return result
        current_metadata = dict(getattr(run, "metadata_", None) or {})
        current_target_payload = _run_target_payload(run)
        current_workspace_payload = _run_workspace_payload(run)
        permission_scope = derive_project_permission_scope(snapshot)
        project_runtime_context = build_project_runtime_context(
            snapshot=snapshot,
            permission_scope=permission_scope,
            workspace_manifest=workspace_manifest_payload,
            materialization=project_context_materialization,
        )

        current_target_payload["project_context_snapshot"] = snapshot
        current_target_payload["project_context_permission_scope"] = permission_scope
        current_workspace_payload[PROJECT_RUNTIME_CONTEXT_KEY] = project_runtime_context
        current_workspace_payload["project_context_snapshot"] = snapshot
        current_workspace_payload["project_context_permission_scope"] = permission_scope
        current_workspace_payload["project_workspace_manifest"] = workspace_manifest_payload
        current_workspace_payload["workspaces"] = _merge_workspaces(current_workspace_payload.get("workspaces"), workspaces)
        if default_workspace:
            current_workspace_payload["workspace_root"] = default_workspace
            current_workspace_payload["resolved_workspace_root"] = default_workspace
        current_workspace_payload["project_context_materialization"] = project_context_materialization

        current_metadata["workspaces"] = _merge_workspaces(current_metadata.get("workspaces"), workspaces)
        current_metadata["project_workspace_manifest"] = workspace_manifest_payload
        current_metadata["project_context_materialization"] = project_context_materialization
        run.metadata_ = current_metadata
        _set_run_target_payload(run, current_target_payload)
        _set_run_workspace_payload(run, current_workspace_payload)
        if result.errors and hasattr(run, "target_status"):
            run.target_status = "invalid"
            run.target_validation_error = "; ".join(result.errors)[:1000]

    return result
