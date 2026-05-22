"""Lifecycle cleanup for thread-owned Project draft workspaces."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import shutil

from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.idea import Idea
from brain.systems.cortex.project_context.drafts import plan_draft_publish
from brain.systems.cortex.project_context.repo_publish import repo_draft_status
from brain.systems.cortex.project_context.runtime_context import project_runtime_context_from_payloads
from brain.systems.cortex.project_context.workspace_manifest import PROJECT_CONTEXT_DIR


PROJECT_DRAFT_CLEANUP_METADATA_KEY = "project_draft_cleanup"
PROJECT_DRAFT_UNPUBLISHED_RETENTION = timedelta(days=7)
PROJECT_DRAFT_CLEANUP_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProjectDraftCleanupPath:
    project_context_dir: str
    status: str
    has_unpublished_changes: bool
    reason: str
    cleanup_after: str | None = None
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "project_context_dir": self.project_context_dir,
            "status": self.status,
            "has_unpublished_changes": self.has_unpublished_changes,
            "reason": self.reason,
            "errors": self.errors,
            "details": self.details,
        }
        if self.cleanup_after:
            payload["cleanup_after"] = self.cleanup_after
        return payload


@dataclass(frozen=True)
class ProjectDraftCleanupResult:
    status: str
    archived_at: str | None
    cleanup_after: str | None = None
    deleted_count: int = 0
    retained_count: int = 0
    skipped_count: int = 0
    paths: list[ProjectDraftCleanupPath] = field(default_factory=list)

    @property
    def has_unpublished_changes(self) -> bool:
        return any(path.has_unpublished_changes for path in self.paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_DRAFT_CLEANUP_SCHEMA_VERSION,
            "status": self.status,
            "archived_at": self.archived_at,
            "cleanup_after": self.cleanup_after,
            "deleted_count": self.deleted_count,
            "retained_count": self.retained_count,
            "skipped_count": self.skipped_count,
            "has_unpublished_changes": self.has_unpublished_changes,
            "retention": {
                "clean": "immediate",
                "unpublished_seconds": int(PROJECT_DRAFT_UNPUBLISHED_RETENTION.total_seconds()),
            },
            "paths": [path.to_dict() for path in self.paths],
        }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _iso_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_aware(value).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return _ensure_aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _payloads_from_run(run: Any) -> list[Mapping[str, Any]]:
    payloads: list[Mapping[str, Any]] = []
    for value in (
        getattr(run, "workspace_ref", None),
        getattr(run, "target_ref", None),
        getattr(run, "metadata_", None),
        getattr(run, "metadata", None),
    ):
        payload = _as_mapping(value)
        if payload and not any(payload is existing for existing in payloads):
            payloads.append(payload)
    return payloads


def _project_context_dir_for_path(value: Any) -> Path | None:
    text = _clean_text(value)
    if not text:
        return None
    path = Path(text).expanduser()
    parts = path.parts
    for index, part in enumerate(parts):
        if part == PROJECT_CONTEXT_DIR:
            return Path(*parts[: index + 1])
    return None


def _mounts_from_manifest_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    mounts = payload.get("mounts")
    if not isinstance(mounts, list):
        return []
    return [dict(mount) for mount in mounts if isinstance(mount, Mapping)]


def _project_manifest_payloads(run: Any) -> list[Mapping[str, Any]]:
    manifests: list[Mapping[str, Any]] = []
    for payload in _payloads_from_run(run):
        runtime = project_runtime_context_from_payloads(payload)
        runtime_manifest = runtime.get("project_workspace_manifest")
        if runtime_manifest:
            manifests.append(runtime_manifest)
        manifest = payload.get("project_workspace_manifest")
        if isinstance(manifest, Mapping):
            manifests.append(manifest)
        runtime_materialization = runtime.get("project_context_materialization")
        if isinstance(runtime_materialization, Mapping):
            nested = runtime_materialization.get("workspace_manifest")
            if isinstance(nested, Mapping):
                manifests.append(nested)
        materialization = payload.get("project_context_materialization")
        if isinstance(materialization, Mapping):
            nested = materialization.get("workspace_manifest")
            if isinstance(nested, Mapping):
                manifests.append(nested)
    return manifests


def _workspace_entries_from_payloads(run: Any) -> list[Mapping[str, Any] | str]:
    entries: list[Mapping[str, Any] | str] = []
    for payload in _payloads_from_run(run):
        workspaces = payload.get("workspaces")
        if isinstance(workspaces, list):
            entries.extend(item for item in workspaces if isinstance(item, (Mapping, str)))
        for key in ("workspace_root", "resolved_workspace_root"):
            value = _clean_text(payload.get(key))
            if value:
                entries.append({"path": value})
    return entries


def _run_project_mounts(run: Any) -> list[dict[str, Any]]:
    mounts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for manifest in _project_manifest_payloads(run):
        for mount in _mounts_from_manifest_payload(manifest):
            materialization = _as_mapping(_as_mapping(mount.get("metadata")).get("materialization"))
            workspace_path = _clean_text(mount.get("workspace_path") or mount.get("path"))
            resource_path = _clean_text(mount.get("resource_path")) or workspace_path
            source_path = _clean_text(mount.get("source_path")) or _clean_text(materialization.get("source_path"))
            repo = _clean_text(mount.get("repo")) or _clean_text(materialization.get("repo"))
            key = (workspace_path or "", resource_path or "", source_path or repo or "")
            if not workspace_path or key in seen:
                continue
            seen.add(key)
            mounts.append({
                "kind": _clean_text(mount.get("kind")) or "workspace",
                "workspace_path": workspace_path,
                "resource_path": resource_path,
                "source_path": source_path,
                "repo": repo,
                "mount_path": _clean_text(mount.get("mount_path")) or _clean_text(mount.get("id")),
            })
    return mounts


def _run_project_context_dirs(run: Any) -> list[Path]:
    paths: list[Path] = []
    for mount in _run_project_mounts(run):
        for key in ("workspace_path", "resource_path"):
            project_dir = _project_context_dir_for_path(mount.get(key))
            if project_dir:
                paths.append(project_dir)
    for entry in _workspace_entries_from_payloads(run):
        path = entry if isinstance(entry, str) else entry.get("path")
        project_dir = _project_context_dir_for_path(path)
        if project_dir:
            paths.append(project_dir)
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        text = str(path)
        if text not in seen:
            seen.add(text)
            deduped.append(path)
    return deduped


def _local_mount_has_unpublished_changes(mount: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any], list[str]]:
    source_path = _clean_text(mount.get("source_path"))
    workspace_path = _clean_text(mount.get("workspace_path"))
    if not source_path or not workspace_path:
        return True, "local_publish_target_unknown", {}, []
    source = Path(source_path).expanduser()
    workspace = Path(workspace_path).expanduser()
    if not source.exists() or not workspace.exists():
        return True, "local_publish_target_missing", {}, []
    try:
        plan = plan_draft_publish(source, workspace)
    except Exception as exc:
        return True, "local_publish_plan_failed", {}, [str(exc)]
    details = {
        "created": plan.created,
        "modified": plan.modified,
        "deleted": plan.deleted,
        "conflicted": plan.conflicted,
    }
    dirty = bool(plan.created or plan.modified or plan.deleted or plan.conflicted)
    return dirty, "unpublished_local_changes" if dirty else "local_draft_clean", details, []


def _repo_mount_has_unpublished_changes(mount: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any], list[str]]:
    workspace_path = _clean_text(mount.get("workspace_path"))
    if not workspace_path:
        return True, "repo_workspace_unknown", {}, []
    workspace = Path(workspace_path).expanduser()
    if not workspace.exists():
        return False, "repo_workspace_missing", {}, []
    try:
        status = repo_draft_status(workspace)
    except Exception as exc:
        return True, "repo_status_failed", {}, [str(exc)]
    errors = [str(error) for error in status.errors if str(error)]
    details = {
        "changed_paths": status.changed_paths,
        "unmerged_paths": status.unmerged_paths,
    }
    dirty = bool(status.changed_paths or status.unmerged_paths or errors)
    return dirty, "unpublished_repo_changes" if dirty else "repo_draft_clean", details, errors


def _mount_has_unpublished_changes(mount: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any], list[str]]:
    if _clean_text(mount.get("source_path")):
        return _local_mount_has_unpublished_changes(mount)
    kind = (_clean_text(mount.get("kind")) or "").lower()
    if _clean_text(mount.get("repo")) or kind == "repo":
        return _repo_mount_has_unpublished_changes(mount)
    return True, "project_resource_publish_target_unknown", {}, []


def _path_is_safe_project_context_dir(path: Path) -> bool:
    try:
        return path.expanduser().name == PROJECT_CONTEXT_DIR
    except Exception:
        return False


def _delete_project_context_dir(path: Path) -> list[str]:
    if not _path_is_safe_project_context_dir(path):
        return [f"Refusing to delete non-Project context directory: {path}"]
    if not path.exists():
        return []
    try:
        shutil.rmtree(path)
        return []
    except Exception as exc:
        return [str(exc)]


def _project_context_dir_dirty_state(run: Any, project_context_dir: Path) -> tuple[bool, list[dict[str, Any]], list[str]]:
    dirty = False
    details: list[dict[str, Any]] = []
    errors: list[str] = []
    matched = False
    for mount in _run_project_mounts(run):
        workspace_dir = _project_context_dir_for_path(mount.get("workspace_path"))
        resource_dir = _project_context_dir_for_path(mount.get("resource_path"))
        if project_context_dir not in {workspace_dir, resource_dir}:
            continue
        matched = True
        mount_dirty, reason, mount_details, mount_errors = _mount_has_unpublished_changes(mount)
        dirty = dirty or mount_dirty
        errors.extend(mount_errors)
        details.append({
            "mount_path": mount.get("mount_path"),
            "workspace_path": mount.get("workspace_path"),
            "resource_path": mount.get("resource_path"),
            "source_path": mount.get("source_path"),
            "repo": mount.get("repo"),
            "reason": reason,
            "has_unpublished_changes": mount_dirty,
            **mount_details,
        })
    if not matched:
        dirty = True
        details.append({
            "reason": "project_context_directory_not_in_manifest",
            "has_unpublished_changes": True,
        })
    return dirty, details, errors


def _project_context_dir_dirty_state_for_runs(
    runs: Sequence[Any],
    project_context_dir: Path,
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    dirty = False
    details: list[dict[str, Any]] = []
    errors: list[str] = []
    for run in runs:
        run_dirty, run_details, run_errors = _project_context_dir_dirty_state(run, project_context_dir)
        dirty = dirty or run_dirty
        errors.extend(run_errors)
        details.append({
            "run_id": str(getattr(run, "id", "")) if getattr(run, "id", None) is not None else None,
            "thread_id": str(getattr(run, "thread_id", "")) if getattr(run, "thread_id", None) is not None else None,
            "has_unpublished_changes": run_dirty,
            "resources": run_details,
        })
    return dirty, details, errors


def _cleanup_status_for_counts(*, deleted_count: int, retained_count: int, skipped_count: int) -> str:
    if retained_count:
        return "retained_unpublished"
    if skipped_count:
        return "partial"
    if deleted_count:
        return "deleted"
    return "no_project_draft"


def _cleanup_result_from_paths(
    *,
    archived_at: datetime,
    cleanup_after: str,
    paths: list[ProjectDraftCleanupPath],
    skipped_count: int = 0,
) -> ProjectDraftCleanupResult:
    deleted_count = sum(1 for path in paths if path.status == "deleted")
    retained_count = sum(1 for path in paths if path.status == "retained_until_cleanup_after")
    skipped_count += sum(1 for path in paths if path.status == "delete_failed")
    return ProjectDraftCleanupResult(
        status=_cleanup_status_for_counts(
            deleted_count=deleted_count,
            retained_count=retained_count,
            skipped_count=skipped_count,
        ),
        archived_at=_iso_timestamp(archived_at),
        cleanup_after=cleanup_after if retained_count else None,
        deleted_count=deleted_count,
        retained_count=retained_count,
        skipped_count=skipped_count,
        paths=paths,
    )


def cleanup_project_drafts_for_runs(
    runs: Sequence[Any],
    *,
    archived_at: datetime | None = None,
    now: datetime | None = None,
    force_expired: bool = False,
) -> list[ProjectDraftCleanupResult]:
    """Apply cleanup once per shared Project context directory, then stamp each run."""

    now = _ensure_aware(now or _utc_now())
    archived_at = _ensure_aware(archived_at or now)
    cleanup_after_dt = archived_at + PROJECT_DRAFT_UNPUBLISHED_RETENTION
    cleanup_after = _iso_timestamp(cleanup_after_dt)
    run_dirs = [(run, _run_project_context_dirs(run)) for run in runs]
    directory_keys: list[str] = []
    directories_by_key: dict[str, Path] = {}
    for _, project_context_dirs in run_dirs:
        for project_context_dir in project_context_dirs:
            key = str(project_context_dir)
            if key in directories_by_key:
                continue
            directories_by_key[key] = project_context_dir
            directory_keys.append(key)

    decisions: dict[str, ProjectDraftCleanupPath] = {}
    for key in directory_keys:
        project_context_dir = directories_by_key[key]
        referencing_runs = [
            run
            for run, project_context_dirs in run_dirs
            if any(str(candidate) == key for candidate in project_context_dirs)
        ]
        has_changes, details, errors = _project_context_dir_dirty_state_for_runs(
            referencing_runs,
            project_context_dir,
        )
        if has_changes and not force_expired:
            decisions[key] = ProjectDraftCleanupPath(
                project_context_dir=str(project_context_dir),
                status="retained_until_cleanup_after",
                has_unpublished_changes=True,
                reason="unpublished_project_draft_changes",
                cleanup_after=cleanup_after,
                errors=errors,
                details={"runs": details},
            )
            continue

        delete_errors = _delete_project_context_dir(project_context_dir)
        if delete_errors:
            decisions[key] = ProjectDraftCleanupPath(
                project_context_dir=str(project_context_dir),
                status="delete_failed",
                has_unpublished_changes=has_changes,
                reason="project_draft_delete_failed",
                cleanup_after=cleanup_after if has_changes else None,
                errors=[*errors, *delete_errors],
                details={"runs": details},
            )
        else:
            decisions[key] = ProjectDraftCleanupPath(
                project_context_dir=str(project_context_dir),
                status="deleted",
                has_unpublished_changes=has_changes,
                reason="expired_unpublished_project_draft" if has_changes else "clean_project_draft_archived",
                details={"runs": details},
            )

    results: list[ProjectDraftCleanupResult] = []
    for run, project_context_dirs in run_dirs:
        if not project_context_dirs:
            result = ProjectDraftCleanupResult(
                status="no_project_draft",
                archived_at=_iso_timestamp(archived_at),
                skipped_count=1,
            )
        else:
            paths = [
                decisions[str(project_context_dir)]
                for project_context_dir in project_context_dirs
                if str(project_context_dir) in decisions
            ]
            result = _cleanup_result_from_paths(
                archived_at=archived_at,
                cleanup_after=cleanup_after,
                paths=paths,
            )
        _set_run_cleanup_metadata(run, result)
        results.append(result)
    return results


def cleanup_project_draft_for_run(
    run: Any,
    *,
    archived_at: datetime | None = None,
    now: datetime | None = None,
    force_expired: bool = False,
) -> ProjectDraftCleanupResult:
    """Apply archive cleanup for one AgentRun's Project draft workspace."""

    now = _ensure_aware(now or _utc_now())
    archived_at = _ensure_aware(archived_at or now)
    cleanup_after_dt = archived_at + PROJECT_DRAFT_UNPUBLISHED_RETENTION
    cleanup_after = _iso_timestamp(cleanup_after_dt)
    paths: list[ProjectDraftCleanupPath] = []
    deleted_count = 0
    retained_count = 0
    skipped_count = 0

    project_context_dirs = _run_project_context_dirs(run)
    if not project_context_dirs:
        result = ProjectDraftCleanupResult(
            status="no_project_draft",
            archived_at=_iso_timestamp(archived_at),
            skipped_count=1,
        )
        _set_run_cleanup_metadata(run, result)
        return result

    for project_context_dir in project_context_dirs:
        has_changes, details, errors = _project_context_dir_dirty_state(run, project_context_dir)
        if has_changes and not force_expired:
            retained_count += 1
            paths.append(ProjectDraftCleanupPath(
                project_context_dir=str(project_context_dir),
                status="retained_until_cleanup_after",
                has_unpublished_changes=True,
                reason="unpublished_project_draft_changes",
                cleanup_after=cleanup_after,
                errors=errors,
                details={"resources": details},
            ))
            continue

        delete_errors = _delete_project_context_dir(project_context_dir)
        if delete_errors:
            skipped_count += 1
            paths.append(ProjectDraftCleanupPath(
                project_context_dir=str(project_context_dir),
                status="delete_failed",
                has_unpublished_changes=has_changes,
                reason="project_draft_delete_failed",
                cleanup_after=cleanup_after if has_changes else None,
                errors=[*errors, *delete_errors],
                details={"resources": details},
            ))
        else:
            deleted_count += 1
            paths.append(ProjectDraftCleanupPath(
                project_context_dir=str(project_context_dir),
                status="deleted",
                has_unpublished_changes=has_changes,
                reason="expired_unpublished_project_draft" if has_changes else "clean_project_draft_archived",
                details={"resources": details},
            ))

    result = ProjectDraftCleanupResult(
        status=_cleanup_status_for_counts(
            deleted_count=deleted_count,
            retained_count=retained_count,
            skipped_count=skipped_count,
        ),
        archived_at=_iso_timestamp(archived_at),
        cleanup_after=cleanup_after if retained_count else None,
        deleted_count=deleted_count,
        retained_count=retained_count,
        skipped_count=skipped_count,
        paths=paths,
    )
    _set_run_cleanup_metadata(run, result)
    return result


def _set_run_cleanup_metadata(run: Any, result: ProjectDraftCleanupResult) -> None:
    metadata = dict(_as_mapping(getattr(run, "metadata_", None) or getattr(run, "metadata", None)))
    metadata[PROJECT_DRAFT_CLEANUP_METADATA_KEY] = result.to_dict()
    if hasattr(run, "metadata_"):
        run.metadata_ = metadata
    elif hasattr(run, "metadata"):
        run.metadata = metadata


def _summary_from_results(results: Sequence[ProjectDraftCleanupResult]) -> dict[str, Any]:
    unique_paths: dict[str, ProjectDraftCleanupPath] = {}
    for result in results:
        for path in result.paths:
            unique_paths.setdefault(path.project_context_dir, path)
    if unique_paths:
        deleted_count = sum(1 for path in unique_paths.values() if path.status == "deleted")
        retained_count = sum(1 for path in unique_paths.values() if path.status == "retained_until_cleanup_after")
        skipped_count = sum(1 for path in unique_paths.values() if path.status == "delete_failed")
        skipped_count += sum(result.skipped_count for result in results if not result.paths)
    else:
        deleted_count = sum(result.deleted_count for result in results)
        retained_count = sum(result.retained_count for result in results)
        skipped_count = sum(result.skipped_count for result in results)
    return {
        "run_count": len(results),
        "deleted_count": deleted_count,
        "retained_count": retained_count,
        "skipped_count": skipped_count,
        "has_unpublished_changes": any(result.has_unpublished_changes for result in results),
        "runs": [result.to_dict() for result in results],
    }


async def apply_project_draft_cleanup_for_thread(
    session: Any,
    thread_id: str,
    *,
    archived_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete clean archived-thread Project drafts and retain dirty drafts with a grace deadline."""

    stmt = select(AgentRunRow).where(AgentRunRow.thread_id == str(thread_id))
    runs = (await session.scalars(stmt)).all()
    results = cleanup_project_drafts_for_runs(runs, archived_at=archived_at, now=now)
    await session.flush()
    return _summary_from_results(results)


async def cleanup_expired_project_draft_workspaces(
    session: Any,
    *,
    now: datetime | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Delete retained Project drafts whose archived-thread grace period has expired."""

    now = _ensure_aware(now or _utc_now())
    stmt = (
        select(AgentRunRow)
        .join(Idea, AgentRunRow.thread_id == Idea.id)
        .where(Idea.archived_at.is_not(None))
        .order_by(AgentRunRow.updated_at.desc())
        .limit(max(1, min(int(limit or 500), 2000)))
    )
    runs = (await session.scalars(stmt)).all()
    eligible_runs: list[Any] = []
    archived_times: list[datetime] = []
    for run in runs:
        cleanup = _as_mapping(_as_mapping(getattr(run, "metadata_", None)).get(PROJECT_DRAFT_CLEANUP_METADATA_KEY))
        if cleanup.get("status") == "deleted":
            continue
        cleanup_after = _parse_timestamp(cleanup.get("cleanup_after"))
        if cleanup_after is None or cleanup_after > now:
            continue
        eligible_runs.append(run)
        archived_times.append(_parse_timestamp(cleanup.get("archived_at")) or now)
    archived_at = min(archived_times) if archived_times else now
    results = cleanup_project_drafts_for_runs(
        eligible_runs,
        archived_at=archived_at,
        now=now,
        force_expired=True,
    )
    if results:
        await session.flush()
    return _summary_from_results(results)


__all__ = [
    "PROJECT_DRAFT_CLEANUP_METADATA_KEY",
    "PROJECT_DRAFT_UNPUBLISHED_RETENTION",
    "apply_project_draft_cleanup_for_thread",
    "cleanup_expired_project_draft_workspaces",
    "cleanup_project_draft_for_run",
    "cleanup_project_drafts_for_runs",
]
