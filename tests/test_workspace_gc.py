from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brain.jobs.pipelines.workspace_gc import reclaim_headless_worker_workspaces


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


class _Rows:
    def __init__(self, statuses: dict[int, str]) -> None:
        self._statuses = statuses

    def all(self) -> list[tuple[int, str]]:
        return list(self._statuses.items())


class _Session:
    def __init__(self, statuses: dict[int, str]) -> None:
        self._statuses = statuses

    async def execute(self, _statement) -> _Rows:
        return _Rows(self._statuses)


def _worker_workspace(
    workspace_root: Path,
    parent_run_id: int,
    *,
    age: timedelta,
    payload: bytes = b"workspace data",
) -> Path:
    path = workspace_root / "ideas" / f"headless-worker-{parent_run_id}-abcdef1234567890"
    path.mkdir(parents=True)
    (path / "payload.bin").write_bytes(payload)
    timestamp = (NOW - age).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


async def test_terminal_workspace_past_retention_is_deleted_and_reports_bytes(tmp_path):
    workspace_root = tmp_path / "workspaces"
    payload = b"old worker bytes"
    workspace = _worker_workspace(
        workspace_root,
        101,
        age=timedelta(hours=49),
        payload=payload,
    )

    result = await reclaim_headless_worker_workspaces(
        _Session({101: "completed"}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert not workspace.exists()
    assert result["directories_reclaimed"] == 1
    assert result["bytes_reclaimed"] == len(payload)


async def test_terminal_workspace_inside_retention_is_kept(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = _worker_workspace(workspace_root, 102, age=timedelta(hours=47))

    result = await reclaim_headless_worker_workspaces(
        _Session({102: "completed"}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert workspace.is_dir()
    assert result["directories_recent"] == 1
    assert result["directories_reclaimed"] == 0


async def test_non_terminal_workspace_is_kept_past_retention(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = _worker_workspace(workspace_root, 103, age=timedelta(days=7))

    result = await reclaim_headless_worker_workspaces(
        _Session({103: "running"}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert workspace.is_dir()
    assert result["directories_non_terminal"] == 1
    assert result["directories_reclaimed"] == 0


async def test_missing_parent_run_is_treated_as_terminal_past_retention(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = _worker_workspace(workspace_root, 104, age=timedelta(days=7))

    result = await reclaim_headless_worker_workspaces(
        _Session({}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert not workspace.exists()
    assert result["directories_reclaimed"] == 1


async def test_uuid_named_idea_workspace_is_kept(tmp_path):
    workspace_root = tmp_path / "workspaces"
    idea_workspace = workspace_root / "ideas" / "55e655bf-664a-4b67-82c8-43ca688c9084"
    idea_workspace.mkdir(parents=True)
    (idea_workspace / "draft.md").write_text("keep", encoding="utf-8")

    result = await reclaim_headless_worker_workspaces(
        _Session({}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert idea_workspace.is_dir()
    assert result["directories_scanned"] == 0
    assert result["directories_reclaimed"] == 0


async def test_headless_worker_symlink_outside_root_is_refused(tmp_path):
    workspace_root = tmp_path / "workspaces"
    ideas = workspace_root / "ideas"
    ideas.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    link = ideas / "headless-worker-105-abcdef1234567890"
    link.symlink_to(outside, target_is_directory=True)

    result = await reclaim_headless_worker_workspaces(
        _Session({}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert link.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert result["directories_refused"] == 1
    assert result["directories_reclaimed"] == 0


async def test_project_roots_and_siblings_of_ideas_are_untouched(tmp_path):
    workspace_root = tmp_path / "workspaces"
    ideas = workspace_root / "ideas"
    ideas.mkdir(parents=True)
    project_root = (
        workspace_root / "project-roots" / "headless-worker-106-abcdef1234567890"
    )
    sibling = workspace_root / "other" / "headless-worker-107-abcdef1234567890"
    project_root.mkdir(parents=True)
    sibling.mkdir(parents=True)

    result = await reclaim_headless_worker_workspaces(
        _Session({106: "completed", 107: "completed"}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert project_root.is_dir()
    assert sibling.is_dir()
    assert result["directories_scanned"] == 0
    assert result["directories_reclaimed"] == 0
