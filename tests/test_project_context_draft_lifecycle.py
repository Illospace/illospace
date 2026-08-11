from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _storage_policy_row(project_draft_retention_hours: int) -> SimpleNamespace:
    return SimpleNamespace(
        finished_workspace_retention_hours=48,
        project_draft_retention_hours=project_draft_retention_hours,
        canvas_quiet_hours=24,
        capacity_warn_percent=80,
        capacity_critical_percent=90,
        automatic_reclamation_allowed=False,
    )


def _run_with_local_project_draft(source_dir, draft_dir):
    resource = {
        "id": "reports",
        "kind": "folder",
        "mount_path": "/reports",
        "path": str(draft_dir),
        "source_path": str(source_dir),
        "materialization": {
            "provider": "local",
            "draft": True,
            "path": str(draft_dir),
            "workspace_path": str(draft_dir),
            "source_path": str(source_dir),
        },
    }
    manifest = {
        "mounts": [
            {
                "id": "/reports",
                "resource_id": "reports",
                "kind": "folder",
                "mount_path": "/reports",
                "workspace_path": str(draft_dir),
                "resource_path": str(draft_dir),
                "source_path": str(source_dir),
            }
        ],
        "workspaces": [{"name": "/reports", "path": str(draft_dir)}],
    }
    return SimpleNamespace(
        id=7,
        thread_id="idea-1",
        workspace_ref={
            "project_context_snapshot": {"resources": [resource]},
            "project_workspace_manifest": manifest,
            "workspaces": manifest["workspaces"],
        },
        target_ref={"project_context_snapshot": {"resources": [resource]}},
        metadata_={},
    )


def test_archived_clean_project_draft_is_deleted_immediately(tmp_path):
    from brain.systems.cortex.project_context.draft_lifecycle import cleanup_project_draft_for_run
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "root"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("Published\n")
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    sync_draft_from_root(source_dir, draft_dir)
    run = _run_with_local_project_draft(source_dir, draft_dir)

    result = cleanup_project_draft_for_run(
        run,
        workspace_retention=timedelta(hours=36),
        archived_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )

    assert result.status == "deleted"
    assert result.deleted_count == 1
    assert not (tmp_path / "thread" / ".illo-project-context").exists()
    assert run.metadata_["project_draft_cleanup"]["status"] == "deleted"


def test_archived_unpublished_project_draft_is_retained_with_grace_deadline(tmp_path):
    from brain.systems.cortex.project_context.draft_lifecycle import cleanup_project_draft_for_run
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "root"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("Published\n")
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("Unpublished draft\n")
    run = _run_with_local_project_draft(source_dir, draft_dir)
    archived_at = datetime(2026, 5, 21, tzinfo=timezone.utc)

    result = cleanup_project_draft_for_run(
        run,
        workspace_retention=timedelta(hours=36),
        archived_at=archived_at,
    )

    assert result.status == "retained_unpublished"
    assert result.deleted_count == 0
    assert result.retained_count == 1
    assert (tmp_path / "thread" / ".illo-project-context").exists()
    cleanup = run.metadata_["project_draft_cleanup"]
    assert cleanup["has_unpublished_changes"] is True
    assert cleanup["cleanup_after"] == (archived_at + timedelta(hours=36)).isoformat()
    assert cleanup["retention"]["unpublished_seconds"] == 36 * 60 * 60


def test_expired_unpublished_project_draft_is_deleted_after_grace_period(tmp_path):
    from brain.systems.cortex.project_context.draft_lifecycle import cleanup_project_draft_for_run
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "root"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("Published\n")
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("Unpublished draft\n")
    run = _run_with_local_project_draft(source_dir, draft_dir)
    archived_at = datetime(2026, 5, 21, tzinfo=timezone.utc)
    cleanup_project_draft_for_run(
        run,
        workspace_retention=timedelta(hours=36),
        archived_at=archived_at,
    )

    result = cleanup_project_draft_for_run(
        run,
        workspace_retention=timedelta(hours=36),
        archived_at=archived_at,
        now=archived_at + timedelta(days=8),
        force_expired=True,
    )

    assert result.status == "deleted"
    assert result.deleted_count == 1
    assert not (tmp_path / "thread" / ".illo-project-context").exists()
    assert result.paths[0].reason == "expired_unpublished_project_draft"


@pytest.mark.asyncio
async def test_thread_cleanup_updates_all_project_runs(tmp_path):
    from brain.systems.cortex.project_context.draft_lifecycle import apply_project_draft_cleanup_for_thread
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "root"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("Published\n")
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    sync_draft_from_root(source_dir, draft_dir)
    run = _run_with_local_project_draft(source_dir, draft_dir)

    result = MagicMock()
    result.all.return_value = [run]
    session = MagicMock()
    session.scalar = AsyncMock(return_value=_storage_policy_row(36))
    session.scalars = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    payload = await apply_project_draft_cleanup_for_thread(
        session,
        "idea-1",
        archived_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )

    assert payload["run_count"] == 1
    assert payload["deleted_count"] == 1
    assert run.metadata_["project_draft_cleanup"]["status"] == "deleted"
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_thread_cleanup_reads_runtime_workspace_retention(tmp_path):
    from brain.systems.cortex.project_context.draft_lifecycle import (
        apply_project_draft_cleanup_for_thread,
    )
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "root"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("Published\n")
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("Unpublished draft\n")
    run = _run_with_local_project_draft(source_dir, draft_dir)
    archived_at = datetime(2026, 5, 21, tzinfo=timezone.utc)

    result = MagicMock()
    result.all.return_value = [run]
    session = MagicMock()
    session.scalar = AsyncMock(return_value=_storage_policy_row(60))
    session.scalars = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    await apply_project_draft_cleanup_for_thread(
        session,
        "idea-1",
        archived_at=archived_at,
    )

    cleanup = run.metadata_["project_draft_cleanup"]
    assert cleanup["cleanup_after"] == (
        archived_at + timedelta(hours=60)
    ).isoformat()
    assert cleanup["retention"]["unpublished_seconds"] == 60 * 60 * 60


@pytest.mark.asyncio
async def test_expired_cleanup_reaps_retained_unpublished_project_drafts(tmp_path):
    from brain.systems.cortex.project_context.draft_lifecycle import (
        cleanup_expired_project_draft_workspaces,
        cleanup_project_draft_for_run,
    )
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    source_dir = tmp_path / "root"
    source_dir.mkdir()
    (source_dir / "brief.md").write_text("Published\n")
    draft_dir = tmp_path / "thread" / ".illo-project-context" / "local" / "reports"
    sync_draft_from_root(source_dir, draft_dir)
    (draft_dir / "brief.md").write_text("Unpublished draft\n")
    run = _run_with_local_project_draft(source_dir, draft_dir)
    archived_at = datetime(2026, 5, 21, tzinfo=timezone.utc)
    cleanup_project_draft_for_run(
        run,
        workspace_retention=timedelta(hours=36),
        archived_at=archived_at,
    )

    result = MagicMock()
    result.all.return_value = [run]
    session = MagicMock()
    session.scalar = AsyncMock(return_value=_storage_policy_row(36))
    session.scalars = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    payload = await cleanup_expired_project_draft_workspaces(
        session,
        now=archived_at + timedelta(days=8),
    )

    assert payload["deleted_count"] == 1
    assert run.metadata_["project_draft_cleanup"]["status"] == "deleted"
    assert not (tmp_path / "thread" / ".illo-project-context").exists()
    session.flush.assert_awaited_once()


def test_project_draft_cleanup_is_in_nightly_scheduler_steps():
    from brain.app.scheduler.programs import NIGHTLY_SLEEP_STEP_KEYS, get_step_specs

    assert "project_draft_cleanup" in NIGHTLY_SLEEP_STEP_KEYS

    job = SimpleNamespace(
        job_key="nightly_sleep",
        family="nightly_sleep",
        program_key="nightly_sleep",
        handler_ref="nightly",
        default_payload={},
        handler_kind="builtin",
        timezone="UTC",
    )
    run = SimpleNamespace(scheduled_for=datetime(2026, 5, 21, tzinfo=timezone.utc))
    steps = get_step_specs(job, run)

    assert any(step.step_key == "project_draft_cleanup" for step in steps)
