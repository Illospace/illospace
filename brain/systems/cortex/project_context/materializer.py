"""Materialize Project Context resources into run-readable workspaces."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import inspect
import os
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from brain.platform.async_io import check_output_sync, run_subprocess_sync
from brain.platform.db.models.run import AgentRun
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.project_context.github import parse_github_repo_slug
from brain.systems.cortex.project_context.permissions import derive_project_permission_scope
from brain.systems.cortex.project_context.snapshot import snapshot_from_project_context
from brain.systems.vault import get_secret, list_secrets


_REPO_RESOURCE_KINDS = {
    "repo",
    "repository",
    "github",
    "github_repo",
    "github_repository",
}


@dataclass
class ProjectContextMaterializationResult:
    workspaces: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    resources_checked: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.workspaces) and not self.errors

    def fail(self, message: str) -> "ProjectContextMaterializationResult":
        self.errors.append(message)
        return self


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _resource_kind(resource: dict[str, Any]) -> str:
    return (_clean_text(resource.get("kind") or resource.get("type") or resource.get("resource_type")) or "").lower()


def _looks_like_github_remote(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.startswith("git@github.com:")
        or lowered.startswith("https://github.com/")
        or lowered.startswith("http://github.com/")
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


def project_context_has_materializable_resources(context: dict[str, Any] | None) -> bool:
    resources = context.get("resources") if isinstance(context, dict) else None
    if not isinstance(resources, list):
        return False
    return any(
        isinstance(resource, dict) and _github_slug_from_resource(resource)
        for resource in resources
    )


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
    try:
        candidates = await _maybe_await(list_secrets(user_id, category="github", org_id=org_id))
    except Exception:
        candidates = []
    names: list[str] = []
    seen: set[str] = set()
    for secret in candidates:
        name = _clean_text(secret.get("key_name") if isinstance(secret, dict) else None)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names[:5]


async def _token_candidates(resource: dict[str, Any], user_id: str | None, org_id: str | None) -> list[tuple[str | None, str | None]]:
    explicit_key = _vault_key_from_resource(resource)
    candidates: list[tuple[str | None, str | None]] = [] if explicit_key else [(None, None)]
    if not user_id:
        return candidates or [(None, None)]

    key_names = []
    if explicit_key:
        key_names.append(explicit_key)
    else:
        key_names.extend(await _github_secret_names(user_id, org_id))

    seen: set[str] = set()
    for key_name in key_names:
        if key_name in seen:
            continue
        seen.add(key_name)
        try:
            token = await _maybe_await(get_secret(
                key_name,
                user_id=user_id,
                org_id=org_id,
                accessed_by="api",
            ))
        except Exception:
            token = None
        if token:
            candidates.append((key_name, token.strip()))
    if explicit_key:
        candidates.append((None, None))
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


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(parent.expanduser().resolve())
        return True
    except Exception:
        return False


def _is_managed_project_context_path(path: Path) -> bool:
    return ".illo-project-context" in path.expanduser().parts


def _should_use_existing_resource_path(existing_path: str | None, workspace_root: Path) -> Path | None:
    if not existing_path:
        return None
    path = Path(existing_path).expanduser()
    if not path.exists() or not path.is_dir():
        return None
    if _path_is_relative_to(path, workspace_root):
        return path
    if _is_managed_project_context_path(path):
        return None
    return path


def _existing_git_checkout(destination: Path, slug: str, branch: str | None) -> dict[str, str] | None:
    """Return metadata for an existing checkout without mutating local state."""

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


def _clone_github_repo(
    slug: str,
    destination: Path,
    *,
    token: str | None,
    branch: str | None,
) -> dict[str, str]:
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
            command = ["git", "clone", "--depth", "1"]
            if selected_branch:
                command.extend(["--branch", selected_branch])
            command.extend([f"https://github.com/{slug}.git", str(destination)])
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            proc = run_subprocess_sync(
                command,
                capture_output=True,
                text=True,
                timeout=180,
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


async def _materialize_resource(
    resource: dict[str, Any],
    *,
    workspace_root: Path,
    user_id: str | None,
    org_id: str | None,
) -> tuple[dict[str, str] | None, str | None]:
    slug = _github_slug_from_resource(resource)
    if not slug:
        return None, None
    existing_path = _should_use_existing_resource_path(_clean_text(resource.get("path")), workspace_root)
    if existing_path:
        return {"name": slug, "path": str(existing_path)}, None

    destination = _safe_repo_destination(workspace_root, slug)
    branch = _clean_text(resource.get("branch") or resource.get("default_branch") or resource.get("branch_hint"))
    existing_checkout = _existing_git_checkout(destination, slug, branch)
    if existing_checkout:
        clone = existing_checkout
        resource["path"] = clone["path"]
        resource["repo"] = slug
        resource["materialization"] = {
            "status": "ready",
            "provider": "github",
            "repo": slug,
            "branch": clone.get("branch") or branch,
            "commit": clone.get("commit") or None,
            "credential": "existing",
            "reused": True,
        }
        return {"name": slug, "path": clone["path"]}, None
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
    for key_name, token in await _token_candidates(resource, user_id, org_id):
        try:
            clone = _clone_github_repo(slug, destination, token=token, branch=branch)
        except Exception as exc:
            prefix = f"Vault key {key_name}: " if key_name else "public clone: "
            errors.append(prefix + _sanitize_git_error(str(exc)))
            continue
        resource["path"] = clone["path"]
        resource["repo"] = slug
        resource["materialization"] = {
            "status": "ready",
            "provider": "github",
            "repo": slug,
            "branch": clone.get("branch") or branch,
            "commit": clone.get("commit") or None,
            "credential": "vault" if key_name else "public",
        }
        if key_name and "credential_ref" not in resource:
            resource["credential_ref"] = {
                "type": "vault_secret",
                "provider": "github",
                "key_name": key_name,
            }
        return {"name": slug, "path": clone["path"]}, None

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
        snapshot = _snapshot_from_run(run)
        if not isinstance(snapshot, dict):
            return result.fail("Project Context snapshot is missing.")
        snapshot = dict(snapshot)
        resources = [dict(item) for item in snapshot.get("resources") or [] if isinstance(item, dict)]
        if not resources:
            return result.fail("Project Context has no resources to materialize.")
        snapshot["resources"] = resources
        user_id = user_id or (str(run.user_id) if getattr(run, "user_id", None) else None)
        org_id = org_id or _clean_text(getattr(run, "org_id", None)) or _clean_text(metadata.get("org_id"))

    workspaces: list[dict[str, str]] = []
    for resource in resources:
        slug = _github_slug_from_resource(resource)
        if not slug:
            continue
        result.resources_checked += 1
        workspace, error = await _materialize_resource(
            resource,
            workspace_root=root,
            user_id=user_id,
            org_id=org_id,
        )
        if workspace:
            workspaces.append(workspace)
        if error:
            result.errors.append(error)

    if not result.resources_checked:
        return result.fail("Project Context resources do not include a supported repository.")

    result.workspaces = workspaces
    status = "materialized" if not result.errors else "failed"
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

        current_target_payload["project_context_snapshot"] = snapshot
        current_target_payload["project_context_permission_scope"] = permission_scope
        current_workspace_payload["project_context_snapshot"] = snapshot
        current_workspace_payload["project_context_permission_scope"] = permission_scope
        current_workspace_payload["workspaces"] = _merge_workspaces(current_workspace_payload.get("workspaces"), workspaces)
        if workspaces:
            default_workspace = workspaces[0]["path"]
            current_workspace_payload["workspace_root"] = default_workspace
            current_workspace_payload["resolved_workspace_root"] = default_workspace
        current_workspace_payload["project_context_materialization"] = {
            "status": status,
            "workspaces": workspaces,
            "errors": result.errors[:10],
        }

        current_metadata["workspaces"] = _merge_workspaces(current_metadata.get("workspaces"), workspaces)
        current_metadata["project_context_materialization"] = {
            "status": status,
            "workspaces": workspaces,
            "errors": result.errors[:10],
        }
        run.metadata_ = current_metadata
        _set_run_target_payload(run, current_target_payload)
        _set_run_workspace_payload(run, current_workspace_payload)
        if result.errors and hasattr(run, "target_status"):
            run.target_status = "invalid"
            run.target_validation_error = "; ".join(result.errors)[:1000]

    return result
