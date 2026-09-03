from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from brain.systems.workspace_reclamation import (
    manage_headless_worker_workspaces,
    reclaim_headless_worker_workspaces,
)
from brain.systems.runs.headless_worker_identity import (
    build_headless_worker_thread_id,
    headless_worker_directory_name,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
DIGEST = "abcdef1234567890"


class _Rows:
    def __init__(self, statuses: dict[int, str]) -> None:
        self._statuses = statuses

    def all(self) -> list[tuple[int, str]]:
        return list(self._statuses.items())


class _Session:
    def __init__(
        self,
        statuses: dict[int, str],
        *,
        retention_hours: int = 48,
        automatic_reclamation_allowed: bool = False,
    ) -> None:
        self._statuses = statuses
        self._retention_hours = retention_hours
        self._automatic_reclamation_allowed = automatic_reclamation_allowed

    async def scalar(self, _statement):
        return SimpleNamespace(
            finished_workspace_retention_hours=self._retention_hours,
            project_draft_retention_hours=168,
            canvas_quiet_hours=24,
            capacity_warn_percent=80,
            capacity_critical_percent=90,
            automatic_reclamation_allowed=self._automatic_reclamation_allowed,
        )

    async def execute(self, _statement) -> _Rows:
        return _Rows(self._statuses)


def _worker_directory_name(parent_run_id: int) -> str:
    thread_id = build_headless_worker_thread_id(parent_run_id, DIGEST)
    directory_name = headless_worker_directory_name(thread_id)
    assert directory_name is not None
    return directory_name


def _worker_workspace(
    workspace_root: Path,
    parent_run_id: int,
    *,
    age: timedelta,
    payload: bytes = b"workspace data",
) -> Path:
    path = workspace_root / "ideas" / _worker_directory_name(parent_run_id)
    path.mkdir(parents=True)
    (path / "payload.bin").write_bytes(payload)
    timestamp = (NOW - age).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


async def test_terminal_workspace_past_retention_is_deleted_and_reports_bytes(
    tmp_path,
    monkeypatch,
):
    workspace_root = tmp_path / "workspaces"
    payload = b"old worker bytes"
    workspace = _worker_workspace(
        workspace_root,
        101,
        age=timedelta(hours=49),
        payload=payload,
    )

    from brain.systems.workspace_inventory import inventory_workspace

    inventoried = []

    def recording_inventory(path):
        inventoried.append(path)
        return inventory_workspace(path)

    monkeypatch.setattr(
        "brain.systems.workspace_reclamation.inventory_workspace",
        recording_inventory,
    )

    result = await reclaim_headless_worker_workspaces(
        _Session({101: "completed"}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert not workspace.exists()
    assert result["directories_reclaimed"] == 1
    assert result["bytes_reclaimed"] == len(payload)
    # The reclaim path measures once, then re-confirms the complete inventory
    # immediately before deletion.
    assert inventoried == [workspace, workspace]


async def test_incomplete_inventory_prevents_workspace_reclamation(
    tmp_path,
    monkeypatch,
):
    workspace_root = tmp_path / "workspaces"
    workspace = _worker_workspace(
        workspace_root,
        109,
        age=timedelta(hours=49),
    )
    real_scandir = os.scandir

    def fake_scandir(path):
        if Path(path) == workspace:
            raise PermissionError("blocked for test")
        return real_scandir(path)

    monkeypatch.setattr("brain.systems.workspace_inventory.os.scandir", fake_scandir)

    result = await reclaim_headless_worker_workspaces(
        _Session({109: "completed"}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert workspace.is_dir()
    assert result["directories_reclaimed"] == 0
    assert result["errors"] == 1
    assert result["workspaces"][0]["disposition"] == "incomplete_inventory"
    assert result["workspaces"][0]["inventory_complete"] is False


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


async def test_runtime_policy_can_extend_finished_workspace_retention(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = _worker_workspace(workspace_root, 108, age=timedelta(hours=49))

    result = await reclaim_headless_worker_workspaces(
        _Session({108: "completed"}, retention_hours=72),
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
    assert result["workspaces"][0]["disposition"] == "parent_run_non_terminal"


async def test_reclamation_reconfirms_parent_status_after_inventory(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = _worker_workspace(
        workspace_root,
        115,
        age=timedelta(days=7),
    )

    class _ChangingSession(_Session):
        status_reads = 0

        async def execute(self, _statement) -> _Rows:
            self.status_reads += 1
            status = "completed" if self.status_reads == 1 else "running"
            return _Rows({115: status})

    result = await reclaim_headless_worker_workspaces(
        _ChangingSession({}),
        workspace_root=workspace_root,
        now=NOW,
    )

    assert workspace.is_dir()
    assert result["directories_reclaimed"] == 0
    assert result["bytes_reclaimed"] == 0
    assert result["workspaces"][0]["disposition"] == "parent_run_non_terminal"


async def test_inventory_action_reports_workspace_sizes_without_reclaiming(tmp_path):
    workspace_root = tmp_path / "workspaces"
    terminal_payload = b"terminal workspace"
    running_payload = b"running workspace"
    terminal = _worker_workspace(
        workspace_root,
        110,
        age=timedelta(days=7),
        payload=terminal_payload,
    )
    running = _worker_workspace(
        workspace_root,
        111,
        age=timedelta(days=7),
        payload=running_payload,
    )

    result = await manage_headless_worker_workspaces(
        _Session({110: "completed", 111: "running"}),
        action="inventory",
        workspace_root=workspace_root,
        now=NOW,
    )

    assert terminal.is_dir()
    assert running.is_dir()
    assert result["directories_reclaimed"] == 0
    assert result["bytes_reclaimable"] == len(terminal_payload)
    assert {
        (item["parent_run_id"], item["bytes_used"], item["disposition"])
        for item in result["workspaces"]
    } == {
        (110, len(terminal_payload), "reclaimable"),
        (111, len(running_payload), "parent_run_non_terminal"),
    }


async def test_illo_tool_handler_uses_shared_workspace_service(monkeypatch, tmp_path):
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    captured = {}

    class FakeUnitOfWork:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_manage(session, **kwargs):
        captured.update(session=session, **kwargs)
        return {"action": kwargs["action"], "bytes_reclaimed": 0}

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        FakeUnitOfWork,
    )
    monkeypatch.setattr(
        "brain.systems.workspace_reclamation.manage_headless_worker_workspaces",
        fake_manage,
    )
    monkeypatch.setattr(
        "brain.kernel.config.resolve_workspace_root",
        lambda: tmp_path,
    )

    result = await _get_tool_handlers()["manage_workspace_reclamation"](
        action="inventory",
        limit=12,
        max_reclaims=4,
    )

    assert result == {"action": "inventory", "bytes_reclaimed": 0}
    assert captured == {
        "session": FakeUnitOfWork.session,
        "action": "inventory",
        "workspace_root": tmp_path,
        "max_reclaims": 4,
        "report_limit": 12,
    }


async def test_automatic_reclamation_reads_permission_from_policy(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = _worker_workspace(
        workspace_root,
        112,
        age=timedelta(days=7),
    )

    disabled = await reclaim_headless_worker_workspaces(
        _Session({112: "completed"}, automatic_reclamation_allowed=False),
        workspace_root=workspace_root,
        now=NOW,
        automatic=True,
    )

    assert workspace.is_dir()
    assert disabled["directories_scanned"] == 1
    assert disabled["directories_reclaimable"] == 1
    assert disabled["bytes_reclaimable"] == len(b"workspace data")
    assert disabled["directories_reclaimed"] == 0
    assert disabled["bytes_reclaimed"] == 0
    assert disabled["reclamation_skipped_reason"] == (
        "automatic_reclamation_disabled_by_policy"
    )

    enabled = await reclaim_headless_worker_workspaces(
        _Session({112: "completed"}, automatic_reclamation_allowed=True),
        workspace_root=workspace_root,
        now=NOW,
        automatic=True,
    )

    assert not workspace.exists()
    assert enabled["directories_reclaimed"] == 1
    assert enabled["bytes_reclaimed"] == len(b"workspace data")


async def test_reclamation_batch_is_bounded_and_reports_exact_freed_bytes(tmp_path):
    workspace_root = tmp_path / "workspaces"
    oldest_payload = b"oldest"
    newer_payload = b"newer but still reclaimable"
    oldest = _worker_workspace(
        workspace_root,
        113,
        age=timedelta(days=8),
        payload=oldest_payload,
    )
    newer = _worker_workspace(
        workspace_root,
        114,
        age=timedelta(days=7),
        payload=newer_payload,
    )

    result = await reclaim_headless_worker_workspaces(
        _Session({113: "completed", 114: "completed"}),
        workspace_root=workspace_root,
        now=NOW,
        max_reclaims=1,
    )

    assert not oldest.exists()
    assert newer.is_dir()
    assert result["max_reclaims"] == 1
    assert result["directories_reclaimable"] == 2
    assert result["directories_reclaimed"] == 1
    assert result["bytes_reclaimed"] == len(oldest_payload)
    assert any(
        item["parent_run_id"] == 114
        and item["disposition"] == "reclaim_limit_reached"
        for item in result["workspaces"]
    )


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
    link = ideas / _worker_directory_name(105)
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
    project_root = workspace_root / "project-roots" / _worker_directory_name(106)
    sibling = workspace_root / "other" / _worker_directory_name(107)
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
