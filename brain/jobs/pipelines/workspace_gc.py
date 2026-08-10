#!/usr/bin/env python3
"""Reclaim retained headless-worker workspaces after their parent runs finish."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel import config as brain_config
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.runs.status import (
    TERMINAL_RUN_STATUSES,
    coerce_run_status,
    project_run_status_value,
)

log = logging.getLogger(__name__)

HEADLESS_WORKER_PREFIX = "headless-worker-"
HEADLESS_WORKER_WORKSPACE_RETENTION = timedelta(hours=48)


def _empty_result() -> dict[str, int]:
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
    if not path.name.startswith(HEADLESS_WORKER_PREFIX):
        return None
    parent_run_id, separator, digest = path.name.removeprefix(
        HEADLESS_WORKER_PREFIX
    ).partition("-")
    if not separator or not parent_run_id.isdigit() or not digest:
        return None
    parsed = int(parent_run_id)
    return parsed if parsed > 0 else None


def _path_is_safe_headless_worker_workspace(
    path: Path,
    *,
    workspace_root: Path,
) -> bool:
    """Accept only a real, direct child of the configured ideas directory."""

    try:
        root = workspace_root.expanduser().resolve(strict=True)
        ideas = (workspace_root.expanduser() / "ideas").resolve(strict=True)
        if path.is_symlink() or not stat.S_ISDIR(
            path.stat(follow_symlinks=False).st_mode
        ):
            return False
        resolved = path.resolve(strict=True)
        return (
            ideas.is_relative_to(root)
            and ideas != root
            and path.parent.resolve(strict=True) == ideas
            and resolved.parent == ideas
            and resolved.is_relative_to(ideas)
            and _parent_run_id(path) is not None
        )
    except (OSError, RuntimeError):
        return False


def _directory_size_bytes(path: Path) -> int:
    """Return regular-file bytes without following links inside the workspace."""

    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(entry_stat.st_mode):
                    total += entry_stat.st_size
                elif stat.S_ISDIR(entry_stat.st_mode):
                    pending.append(Path(entry.path))
    return total


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
    retention: timedelta = HEADLESS_WORKER_WORKSPACE_RETENTION,
) -> dict[str, int]:
    """Delete old worker workspaces whose parent run is terminal or absent."""

    result = _empty_result()
    cutoff = now or datetime.now(timezone.utc)
    cutoff = cutoff.astimezone(timezone.utc) - retention
    ideas = workspace_root.expanduser() / "ideas"
    if not ideas.exists():
        log.info("Workspace GC complete: %s", result)
        return result

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
        if not path.name.startswith(HEADLESS_WORKER_PREFIX):
            continue
        result["directories_scanned"] += 1
        parent_run_id = _parent_run_id(path)
        if parent_run_id is None or not _path_is_safe_headless_worker_workspace(
            path,
            workspace_root=workspace_root,
        ):
            result["directories_refused"] += 1
            log.warning("Workspace GC refused unsafe candidate: %s", path)
            continue
        try:
            modified_at = datetime.fromtimestamp(
                path.stat(follow_symlinks=False).st_mtime,
                tz=timezone.utc,
            )
        except OSError as exc:
            result["errors"] += 1
            log.warning("Workspace GC could not stat %s: %s", path, exc)
            continue
        if modified_at > cutoff:
            result["directories_recent"] += 1
            continue
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
            if not _path_is_safe_headless_worker_workspace(
                path,
                workspace_root=workspace_root,
            ):
                result["directories_refused"] += 1
                log.warning("Workspace GC refused candidate before deletion: %s", path)
                continue
            modified_at = datetime.fromtimestamp(
                path.stat(follow_symlinks=False).st_mtime,
                tz=timezone.utc,
            )
            if modified_at > cutoff:
                result["directories_recent"] += 1
                continue
            size = _directory_size_bytes(path)
            shutil.rmtree(path)
        except OSError as exc:
            result["errors"] += 1
            log.warning("Workspace GC could not delete %s: %s", path, exc)
            continue

        result["directories_reclaimed"] += 1
        result["bytes_reclaimed"] += size

    log.info("Workspace GC complete: %s", result)
    return result


async def run() -> dict[str, int]:
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
