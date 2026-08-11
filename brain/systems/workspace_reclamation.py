"""Inventory and reclaim retained headless-worker workspaces."""

from __future__ import annotations

import logging
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.async_io import run_blocking
from brain.platform.db.models.agent_run import AgentRunRow
from brain.systems.runs.headless_worker_identity import (
    is_headless_worker_directory_candidate,
    parse_headless_worker_directory_name,
)
from brain.systems.runs.status import (
    TERMINAL_RUN_STATUSES,
    coerce_run_status,
    project_run_status_value,
)
from brain.systems.storage_policy import async_get_storage_policy
from brain.systems.workspace_inventory import WorkspaceScanError, inventory_workspace

log = logging.getLogger(__name__)

WorkspaceGCAction = Literal["inventory", "reclaim"]
WorkspaceDisposition = Literal[
    "recent",
    "parent_run_non_terminal",
    "incomplete_inventory",
    "reclaimable",
    "reclaim_limit_reached",
    "reclaimed",
    "refused_before_reclamation",
]

# One hourly scheduler tick cannot start an unbounded deletion batch. At this
# rate, a full month can safely drain far more workspaces than the 833-directory
# production cleanup that established the policy.
MAX_RECLAIMS_PER_RUN = 100
MAX_WORKSPACE_REPORT_LIMIT = 200
_DEFAULT_WORKSPACE_REPORT_LIMIT = 100
_SCAN_ERRORS_PER_WORKSPACE = 3
_PARENT_STATUS_BATCH_SIZE = 500


class WorkspaceGCWorkspace(TypedDict):
    path: str
    parent_run_id: int
    parent_run_status: str | None
    modified_at: str
    bytes_used: int
    inventory_complete: bool
    scan_error_count: int
    scan_errors: list[WorkspaceScanError]
    reclaimable: bool
    disposition: WorkspaceDisposition


class WorkspaceGCResult(TypedDict):
    action: WorkspaceGCAction
    automatic: bool
    automatic_reclamation_allowed: bool
    retention_hours: int
    max_reclaims: int
    reclamation_skipped_reason: str | None
    directories_scanned: int
    directories_reclaimable: int
    bytes_reclaimable: int
    directories_reclaimed: int
    bytes_reclaimed: int
    directories_recent: int
    directories_non_terminal: int
    directories_refused: int
    errors: int
    workspaces: list[WorkspaceGCWorkspace]
    workspaces_truncated: bool


def _empty_result(
    *,
    action: WorkspaceGCAction,
    automatic: bool,
    automatic_reclamation_allowed: bool,
    retention: timedelta,
    max_reclaims: int,
) -> WorkspaceGCResult:
    return {
        "action": action,
        "automatic": automatic,
        "automatic_reclamation_allowed": automatic_reclamation_allowed,
        "retention_hours": int(retention.total_seconds() // 3600),
        "max_reclaims": max_reclaims,
        "reclamation_skipped_reason": None,
        "directories_scanned": 0,
        "directories_reclaimable": 0,
        "bytes_reclaimable": 0,
        "directories_reclaimed": 0,
        "bytes_reclaimed": 0,
        "directories_recent": 0,
        "directories_non_terminal": 0,
        "directories_refused": 0,
        "errors": 0,
        "workspaces": [],
        "workspaces_truncated": False,
    }


def _parent_run_id(path: Path) -> int | None:
    identity = parse_headless_worker_directory_name(path.name)
    return identity[0] if identity is not None else None


_ValidationCounter = Literal[
    "directories_recent",
    "directories_refused",
    "errors",
]


class _WorkspaceValidation(NamedTuple):
    parent_run_id: int | None
    modified_at: datetime | None
    counter: _ValidationCounter | None
    error: OSError | None = None


def _validate_headless_worker_workspace(
    path: Path,
    *,
    workspace_root: Path,
    cutoff: datetime,
) -> _WorkspaceValidation:
    """Validate that a candidate is a safe, parseable workspace now."""

    try:
        root = workspace_root.expanduser().resolve(strict=True)
        ideas = (workspace_root.expanduser() / "ideas").resolve(strict=True)
        if path.is_symlink() or not stat.S_ISDIR(
            path.stat(follow_symlinks=False).st_mode
        ):
            return _WorkspaceValidation(None, None, "directories_refused")
        resolved = path.resolve(strict=True)
        parent_run_id = _parent_run_id(path)
        if not (
            ideas.is_relative_to(root)
            and ideas != root
            and path.parent.resolve(strict=True) == ideas
            and resolved.parent == ideas
            and resolved.is_relative_to(ideas)
            and parent_run_id is not None
        ):
            return _WorkspaceValidation(None, None, "directories_refused")
    except (OSError, RuntimeError):
        return _WorkspaceValidation(None, None, "directories_refused")

    try:
        modified_at = datetime.fromtimestamp(
            path.stat(follow_symlinks=False).st_mtime,
            tz=timezone.utc,
        )
    except OSError as exc:
        return _WorkspaceValidation(parent_run_id, None, "errors", exc)
    if modified_at > cutoff:
        return _WorkspaceValidation(
            parent_run_id,
            modified_at,
            "directories_recent",
        )
    return _WorkspaceValidation(parent_run_id, modified_at, None)


async def _parent_run_statuses(
    session: AsyncSession,
    parent_run_ids: set[int],
) -> dict[int, str]:
    if not parent_run_ids:
        return {}
    statuses: dict[int, str] = {}
    ordered_ids = sorted(parent_run_ids)
    for offset in range(0, len(ordered_ids), _PARENT_STATUS_BATCH_SIZE):
        batch = ordered_ids[offset : offset + _PARENT_STATUS_BATCH_SIZE]
        rows = (
            await session.execute(
                select(AgentRunRow.id, AgentRunRow.status).where(
                    AgentRunRow.id.in_(batch)
                )
            )
        ).all()
        statuses.update(
            {int(run_id): str(status) for run_id, status in rows}
        )
    return statuses


def _is_non_terminal(status: str | None) -> bool:
    return status is not None and coerce_run_status(
        project_run_status_value(status)
    ) not in TERMINAL_RUN_STATUSES


@dataclass(slots=True)
class _MeasuredWorkspace:
    path: Path
    parent_run_id: int
    parent_run_status: str | None
    modified_at: datetime
    bytes_used: int
    inventory_complete: bool
    scan_errors: tuple[WorkspaceScanError, ...]
    reclaimable: bool
    disposition: WorkspaceDisposition

    def payload(self, workspace_root: Path) -> WorkspaceGCWorkspace:
        return {
            "path": str(self.path.relative_to(workspace_root)),
            "parent_run_id": self.parent_run_id,
            "parent_run_status": self.parent_run_status,
            "modified_at": self.modified_at.isoformat(),
            "bytes_used": self.bytes_used,
            "inventory_complete": self.inventory_complete,
            "scan_error_count": len(self.scan_errors),
            "scan_errors": [
                dict(error) for error in self.scan_errors[:_SCAN_ERRORS_PER_WORKSPACE]
            ],
            "reclaimable": self.reclaimable,
            "disposition": self.disposition,
        }


def _log_scan_errors(path: Path, errors: tuple[WorkspaceScanError, ...]) -> None:
    for error in errors[:_SCAN_ERRORS_PER_WORKSPACE]:
        log.warning(
            "Workspace GC could not %s %s while inventorying %s: %s",
            error["operation"],
            error["path"],
            path,
            error["message"],
        )
    remaining = len(errors) - _SCAN_ERRORS_PER_WORKSPACE
    if remaining > 0:
        log.warning(
            "Workspace GC omitted %s additional inventory errors for %s",
            remaining,
            path,
        )


def _bounded_workspace_payloads(
    measured: list[_MeasuredWorkspace],
    *,
    workspace_root: Path,
    limit: int,
) -> tuple[list[WorkspaceGCWorkspace], bool]:
    ordered = sorted(
        measured,
        key=lambda item: (
            0 if item.disposition == "reclaimed" else 1,
            0 if item.reclaimable else 1,
            -item.bytes_used,
            str(item.path),
        ),
    )
    return (
        [item.payload(workspace_root) for item in ordered[:limit]],
        len(ordered) > limit,
    )


async def manage_headless_worker_workspaces(
    session: AsyncSession,
    *,
    action: WorkspaceGCAction,
    workspace_root: Path,
    now: datetime | None = None,
    retention: timedelta | None = None,
    automatic: bool = False,
    max_reclaims: int = MAX_RECLAIMS_PER_RUN,
    report_limit: int = _DEFAULT_WORKSPACE_REPORT_LIMIT,
) -> WorkspaceGCResult:
    """Inventory or reclaim policy-eligible headless-worker workspaces.

    Both the scheduled job and the Illo tool use this function. Reclamation is
    bounded and re-confirms path, run status, and inventory immediately before
    each deletion.
    """

    if action not in {"inventory", "reclaim"}:
        raise ValueError("action must be 'inventory' or 'reclaim'")
    normalized_max_reclaims = max(1, min(int(max_reclaims), MAX_RECLAIMS_PER_RUN))
    normalized_report_limit = max(
        1,
        min(int(report_limit), MAX_WORKSPACE_REPORT_LIMIT),
    )
    policy = await async_get_storage_policy(session)
    active_retention = (
        policy.finished_workspace_retention if retention is None else retention
    )
    if active_retention.total_seconds() <= 0:
        raise ValueError("retention must be positive")
    result = _empty_result(
        action=action,
        automatic=automatic,
        automatic_reclamation_allowed=policy.automatic_reclamation_allowed,
        retention=active_retention,
        max_reclaims=normalized_max_reclaims,
    )
    if action == "reclaim" and automatic and not policy.automatic_reclamation_allowed:
        result["reclamation_skipped_reason"] = (
            "automatic_reclamation_disabled_by_policy"
        )
        log.info("Workspace GC skipped because automatic reclamation is disabled")
        return result

    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = observed_at - active_retention
    root_input = workspace_root.expanduser()
    ideas = root_input / "ideas"
    if not ideas.exists():
        log.info("Workspace GC complete: %s", result)
        return result

    try:
        root = root_input.resolve(strict=True)
        resolved_ideas = ideas.resolve(strict=True)
        if not resolved_ideas.is_relative_to(root) or resolved_ideas == root:
            result["directories_refused"] += 1
            log.error(
                "Workspace GC refused unsafe ideas directory: root=%s ideas=%s",
                root,
                resolved_ideas,
            )
            return result
        entries = sorted(resolved_ideas.iterdir(), key=lambda entry: entry.name)
    except (OSError, RuntimeError) as exc:
        result["errors"] += 1
        log.exception("Workspace GC could not inspect %s: %s", ideas, exc)
        return result

    validated: list[tuple[Path, _WorkspaceValidation]] = []
    for path in entries:
        if not is_headless_worker_directory_candidate(path.name):
            continue
        result["directories_scanned"] += 1
        validation = _validate_headless_worker_workspace(
            path,
            workspace_root=root,
            cutoff=cutoff,
        )
        if validation.counter == "directories_refused":
            result["directories_refused"] += 1
            log.warning("Workspace GC refused unsafe candidate: %s", path)
            continue
        if validation.counter == "errors":
            result["errors"] += 1
            log.warning("Workspace GC could not stat %s: %s", path, validation.error)
            continue
        assert validation.parent_run_id is not None
        assert validation.modified_at is not None
        validated.append((path, validation))

    statuses = await _parent_run_statuses(
        session,
        {
            validation.parent_run_id
            for _, validation in validated
            if validation.parent_run_id
        },
    )
    measured: list[_MeasuredWorkspace] = []
    for path, validation in validated:
        parent_run_id = validation.parent_run_id
        modified_at = validation.modified_at
        assert parent_run_id is not None
        assert modified_at is not None
        status = statuses.get(parent_run_id)
        inventory = await run_blocking(inventory_workspace, path)
        if not inventory.complete:
            result["errors"] += len(inventory.scan_errors)
            _log_scan_errors(path, inventory.scan_errors)

        if validation.counter == "directories_recent":
            result["directories_recent"] += 1
            disposition: WorkspaceDisposition = "recent"
        elif _is_non_terminal(status):
            result["directories_non_terminal"] += 1
            disposition = "parent_run_non_terminal"
        elif not inventory.complete:
            disposition = "incomplete_inventory"
        else:
            result["directories_reclaimable"] += 1
            result["bytes_reclaimable"] += inventory.bytes_used
            disposition = "reclaimable"

        measured.append(
            _MeasuredWorkspace(
                path=path,
                parent_run_id=parent_run_id,
                parent_run_status=status,
                modified_at=modified_at,
                bytes_used=inventory.bytes_used,
                inventory_complete=inventory.complete,
                scan_errors=inventory.scan_errors,
                reclaimable=disposition == "reclaimable",
                disposition=disposition,
            )
        )

    if action == "reclaim":
        reclaimable = sorted(
            (item for item in measured if item.reclaimable),
            key=lambda item: (item.modified_at, str(item.path)),
        )
        for item in reclaimable[normalized_max_reclaims:]:
            item.disposition = "reclaim_limit_reached"

        for item in reclaimable[:normalized_max_reclaims]:
            inventory = await run_blocking(inventory_workspace, item.path)
            if not inventory.complete:
                item.bytes_used = inventory.bytes_used
                item.inventory_complete = False
                item.scan_errors = inventory.scan_errors
                item.reclaimable = False
                item.disposition = "incomplete_inventory"
                result["errors"] += len(inventory.scan_errors)
                _log_scan_errors(item.path, inventory.scan_errors)
                continue

            # Re-confirm the path and parent status after the potentially
            # expensive walk, immediately before deletion.
            validation = _validate_headless_worker_workspace(
                item.path,
                workspace_root=root,
                cutoff=cutoff,
            )
            if validation.counter is not None:
                item.reclaimable = False
                if validation.counter == "directories_recent":
                    item.disposition = "recent"
                    result["directories_recent"] += 1
                elif validation.counter == "directories_refused":
                    item.disposition = "refused_before_reclamation"
                    result["directories_refused"] += 1
                    log.warning(
                        "Workspace GC refused candidate before deletion: %s",
                        item.path,
                    )
                else:
                    item.disposition = "refused_before_reclamation"
                    result["errors"] += 1
                    log.warning(
                        "Workspace GC could not revalidate %s: %s",
                        item.path,
                        validation.error,
                    )
                continue

            current_status = (
                await _parent_run_statuses(session, {item.parent_run_id})
            ).get(item.parent_run_id)
            item.parent_run_status = current_status
            if _is_non_terminal(current_status):
                item.reclaimable = False
                item.disposition = "parent_run_non_terminal"
                result["directories_non_terminal"] += 1
                continue

            try:
                await run_blocking(shutil.rmtree, item.path)
            except OSError as exc:
                item.reclaimable = False
                item.disposition = "refused_before_reclamation"
                result["errors"] += 1
                log.warning("Workspace GC could not delete %s: %s", item.path, exc)
                continue

            item.bytes_used = inventory.bytes_used
            item.inventory_complete = True
            item.scan_errors = ()
            item.reclaimable = False
            item.disposition = "reclaimed"
            result["directories_reclaimed"] += 1
            result["bytes_reclaimed"] += inventory.bytes_used

    result["workspaces"], result["workspaces_truncated"] = (
        _bounded_workspace_payloads(
            measured,
            workspace_root=root,
            limit=normalized_report_limit,
        )
    )
    log.info(
        "Workspace GC complete: action=%s scanned=%s reclaimed=%s bytes_reclaimed=%s",
        action,
        result["directories_scanned"],
        result["directories_reclaimed"],
        result["bytes_reclaimed"],
    )
    return result


async def reclaim_headless_worker_workspaces(
    session: AsyncSession,
    *,
    workspace_root: Path,
    now: datetime | None = None,
    retention: timedelta | None = None,
    automatic: bool = False,
    max_reclaims: int = MAX_RECLAIMS_PER_RUN,
    report_limit: int = _DEFAULT_WORKSPACE_REPORT_LIMIT,
) -> WorkspaceGCResult:
    """Reclaim old worker workspaces through the shared maintenance service."""

    return await manage_headless_worker_workspaces(
        session,
        action="reclaim",
        workspace_root=workspace_root,
        now=now,
        retention=retention,
        automatic=automatic,
        max_reclaims=max_reclaims,
        report_limit=report_limit,
    )

