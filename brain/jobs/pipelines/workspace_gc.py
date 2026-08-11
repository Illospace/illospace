#!/usr/bin/env python3
"""Reclaim retained headless-worker workspaces after their parent runs finish."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel import config as brain_config
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork
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
from brain.systems.workspace_inventory import inventory_workspace

log = logging.getLogger(__name__)


class WorkspaceGCResult(TypedDict):
    directories_scanned: int
    directories_reclaimed: int
    bytes_reclaimed: int
    directories_recent: int
    directories_non_terminal: int
    directories_refused: int
    errors: int


def _empty_result() -> WorkspaceGCResult:
    return {
        "directories_scanned": 0,
        "directories_reclaimed": 0,
        "bytes_reclaimed": 0,
        "directories_recent": 0,
        "directories_non_terminal": 0,
        "directories_refused": 0,
        "errors": 0,
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
    counter: _ValidationCounter | None
    error: OSError | None = None


def _validate_headless_worker_workspace(
    path: Path,
    *,
    workspace_root: Path,
    cutoff: datetime,
) -> _WorkspaceValidation:
    """Validate that a candidate is a safe, parseable, old workspace now."""

    try:
        root = workspace_root.expanduser().resolve(strict=True)
        ideas = (workspace_root.expanduser() / "ideas").resolve(strict=True)
        if path.is_symlink() or not stat.S_ISDIR(
            path.stat(follow_symlinks=False).st_mode
        ):
            return _WorkspaceValidation(None, "directories_refused")
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
            return _WorkspaceValidation(None, "directories_refused")
    except (OSError, RuntimeError):
        return _WorkspaceValidation(None, "directories_refused")

    try:
        modified_at = datetime.fromtimestamp(
            path.stat(follow_symlinks=False).st_mtime,
            tz=timezone.utc,
        )
    except OSError as exc:
        return _WorkspaceValidation(None, "errors", exc)
    if modified_at > cutoff:
        return _WorkspaceValidation(parent_run_id, "directories_recent")
    return _WorkspaceValidation(parent_run_id, None)


async def _parent_run_statuses(
    session: AsyncSession,
    parent_run_ids: set[int],
) -> dict[int, str]:
    if not parent_run_ids:
        return {}
    rows = (
        await session.execute(
            select(AgentRunRow.id, AgentRunRow.status).where(
                AgentRunRow.id.in_(sorted(parent_run_ids))
            )
        )
    ).all()
    return {int(run_id): str(status) for run_id, status in rows}


async def reclaim_headless_worker_workspaces(
    session: AsyncSession,
    *,
    workspace_root: Path,
    now: datetime | None = None,
    retention: timedelta | None = None,
) -> WorkspaceGCResult:
    """Delete old worker workspaces whose parent run is terminal or absent."""

    result = _empty_result()
    ideas = workspace_root.expanduser() / "ideas"
    if not ideas.exists():
        log.info("Workspace GC complete: %s", result)
        return result
    if retention is None:
        policy = await async_get_storage_policy(session)
        retention = policy.finished_workspace_retention
    if retention.total_seconds() <= 0:
        raise ValueError("retention must be positive")
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - retention

    try:
        root = workspace_root.expanduser().resolve(strict=True)
        resolved_ideas = ideas.resolve(strict=True)
        if not resolved_ideas.is_relative_to(root) or resolved_ideas == root:
            result["directories_refused"] += 1
            log.error(
                "Workspace GC refused unsafe ideas directory: root=%s ideas=%s",
                root,
                resolved_ideas,
            )
            log.info("Workspace GC complete: %s", result)
            return result
        entries = sorted(ideas.iterdir(), key=lambda entry: entry.name)
    except (OSError, RuntimeError) as exc:
        result["errors"] += 1
        log.exception("Workspace GC could not inspect %s: %s", ideas, exc)
        log.info("Workspace GC complete: %s", result)
        return result

    candidates: list[tuple[Path, int]] = []
    for path in entries:
        if not is_headless_worker_directory_candidate(path.name):
            continue
        result["directories_scanned"] += 1
        validation = _validate_headless_worker_workspace(
            path,
            workspace_root=workspace_root,
            cutoff=cutoff,
        )
        if validation.counter is not None:
            result[validation.counter] += 1
            if validation.counter == "directories_refused":
                log.warning("Workspace GC refused unsafe candidate: %s", path)
            elif validation.counter == "errors":
                log.warning("Workspace GC could not stat %s: %s", path, validation.error)
            continue
        parent_run_id = validation.parent_run_id
        assert parent_run_id is not None
        candidates.append((path, parent_run_id))

    statuses = await _parent_run_statuses(
        session,
        {parent_run_id for _, parent_run_id in candidates},
    )
    for path, parent_run_id in candidates:
        status = statuses.get(parent_run_id)
        if status is not None and coerce_run_status(
            project_run_status_value(status)
        ) not in TERMINAL_RUN_STATUSES:
            result["directories_non_terminal"] += 1
            continue

        try:
            validation = _validate_headless_worker_workspace(
                path,
                workspace_root=workspace_root,
                cutoff=cutoff,
            )
            if validation.counter is not None:
                result[validation.counter] += 1
                if validation.counter == "directories_refused":
                    log.warning(
                        "Workspace GC refused candidate before deletion: %s", path
                    )
                elif validation.counter == "errors":
                    log.warning(
                        "Workspace GC could not delete %s: %s", path, validation.error
                    )
                continue
            inventory = inventory_workspace(path)
            if not inventory.complete:
                result["errors"] += len(inventory.scan_errors)
                for error in inventory.scan_errors:
                    log.warning(
                        "Workspace GC could not %s %s: %s",
                        error["operation"],
                        error["path"],
                        error["message"],
                    )
                continue
            size = inventory.bytes_used
            shutil.rmtree(path)
        except OSError as exc:
            result["errors"] += 1
            log.warning("Workspace GC could not delete %s: %s", path, exc)
            continue

        result["directories_reclaimed"] += 1
        result["bytes_reclaimed"] += size

    log.info("Workspace GC complete: %s", result)
    return result


async def run() -> WorkspaceGCResult:
    async with UnitOfWork() as uow:
        return await reclaim_headless_worker_workspaces(
            uow.session,  # type: ignore[arg-type]
            workspace_root=brain_config.resolve_workspace_root(),
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [workspace_gc] %(message)s",
    )
    print(json.dumps(asyncio.run(run()), sort_keys=True))


if __name__ == "__main__":
    main()
