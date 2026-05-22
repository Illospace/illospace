from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


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

    result = cleanup_project_draft_for_run(run, archived_at=archived_at)

    assert result.status == "retained_unpublished"
    assert result.deleted_count == 0
    assert result.retained_count == 1
    assert (tmp_path / "thread" / ".illo-project-context").exists()
    cleanup = run.metadata_["project_draft_cleanup"]
    assert cleanup["has_unpublished_changes"] is True
    assert cleanup["cleanup_after"] == (archived_at + timedelta(days=7)).isoformat()


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
    cleanup_project_draft_for_run(run, archived_at=archived_at)

    result = cleanup_project_draft_for_run(
        run,
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
async def test_thread_cleanup_retains_shared_project_context_when_any_run_is_dirty(tmp_path):
    from brain.systems.cortex.project_context.draft_lifecycle import apply_project_draft_cleanup_for_thread
    from brain.systems.cortex.project_context.drafts import sync_draft_from_root

    clean_source = tmp_path / "clean-root"
    dirty_source = tmp_path / "dirty-root"
    clean_source.mkdir()
    dirty_source.mkdir()
    (clean_source / "brief.md").write_text("Published clean\n")
    (dirty_source / "brief.md").write_text("Published dirty\n")
    project_context_dir = tmp_path / "thread" / ".illo-project-context"
    clean_draft = project_context_dir / "local" / "clean-reports"
    dirty_draft = project_context_dir / "local" / "dirty-reports"
    sync_draft_from_root(clean_source, clean_draft)
    sync_draft_from_root(dirty_source, dirty_draft)
    (dirty_draft / "brief.md").write_text("Unpublished dirty draft\n")
    clean_run = _run_with_local_project_draft(clean_source, clean_draft)
    dirty_run = _run_with_local_project_draft(dirty_source, dirty_draft)
    clean_run.id = 8
    dirty_run.id = 9

    result = MagicMock()
    result.all.return_value = [clean_run, dirty_run]
    session = MagicMock()
    session.scalars = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    payload = await apply_project_draft_cleanup_for_thread(
        session,
        "idea-1",
        archived_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )

    assert payload["run_count"] == 2
    assert payload["deleted_count"] == 0
    assert payload["retained_count"] == 1
    assert project_context_dir.exists()
    assert (dirty_draft / "brief.md").read_text(encoding="utf-8") == "Unpublished dirty draft\n"
    assert clean_run.metadata_["project_draft_cleanup"]["status"] == "retained_unpublished"
    assert dirty_run.metadata_["project_draft_cleanup"]["status"] == "retained_unpublished"
    session.flush.assert_awaited_once()


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
    cleanup_project_draft_for_run(run, archived_at=archived_at)

    result = MagicMock()
    result.all.return_value = [run]
    session = MagicMock()
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
