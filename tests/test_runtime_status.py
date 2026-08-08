from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.app.ops import runtime_status


@pytest.mark.parametrize(
    ("heartbeat_age", "queued", "stale_queue", "expected"),
    [
        (5, 0, False, "good"),
        (31, 0, False, "late"),
        (91, 0, False, "stalled"),
        (5, 1, True, "stalled"),
        (None, 0, False, "stalled"),
    ],
)
def test_worker_state_uses_durable_heartbeat_and_canonical_queue_health(
    heartbeat_age,
    queued,
    stale_queue,
    expected,
):
    state, _reason = runtime_status._worker_state(
        heartbeat_age_seconds=heartbeat_age,
        queue_health=SimpleNamespace(
            queued=queued,
            stale_queued_backlog=stale_queue,
        ),
    )
    assert state == expected


@pytest.mark.parametrize(
    ("tick_age", "lag", "expected"),
    [
        (30, 0, "good"),
        (121, 0, "late"),
        (30, 1, "late"),
        (901, 0, "stalled"),
        (30, 901, "stalled"),
        (None, 0, "stalled"),
    ],
)
def test_scheduler_state_uses_tick_and_job_lag(tick_age, lag, expected):
    state, _reason = runtime_status._scheduler_state(
        tick_age_seconds=tick_age,
        lag_seconds=lag,
    )
    assert state == expected


def test_deploy_evidence_requires_a_real_build_sha(monkeypatch):
    monkeypatch.setenv("ILLO_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("ILLO_BUILD_TIME", "2026-08-08T20:00:00Z")
    monkeypatch.setenv("ILLO_DEPLOY_TIME", "2026-08-08T20:05:00Z")
    evidence = runtime_status._deploy_evidence()
    assert evidence["state"] == "good"
    assert evidence["sha"] == "abc123"
    assert evidence["built_at"] == "2026-08-08T20:00:00+00:00"
    assert evidence["deployed_at"] == "2026-08-08T20:05:00+00:00"
    assert evidence["process_started_at"]

    monkeypatch.setenv("ILLO_BUILD_COMMIT", "unknown")
    assert runtime_status._deploy_evidence()["state"] == "stalled"


@pytest.mark.asyncio
async def test_runtime_snapshot_contains_all_six_evidence_rows(monkeypatch):
    now = datetime(2026, 8, 8, 20, 0, tzinfo=timezone.utc)
    session = MagicMock()
    session.scalar = AsyncMock(
        side_effect=[
            now - timedelta(minutes=1),  # last claim
            2,  # running
            now - timedelta(minutes=5),  # oldest running
            0,  # overdue deadlines
            3,  # enabled cycles
            now - timedelta(hours=1),  # last cycle fire
            1,  # overdue cycles
            now - timedelta(minutes=4),  # oldest overdue cycle
        ]
    )
    overdue_cycle = SimpleNamespace(
        id=7,
        name="Daily brief",
        next_run_at=now - timedelta(minutes=4),
        last_run_at=now - timedelta(days=1),
        last_status="completed",
    )
    cycle_result = MagicMock()
    cycle_result.all.return_value = [overdue_cycle]
    session.scalars = AsyncMock(return_value=cycle_result)
    usage_summary = AsyncMock(
        return_value={
            "estimated_cost": 0.375,
            "tokens_total": 3000,
            "runs": 2,
        }
    )

    monkeypatch.setenv("ILLO_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("ILLO_BUILD_TIME", "2026-08-08T19:55:00Z")
    monkeypatch.setenv("ILLO_DEPLOY_TIME", "2026-08-08T19:58:00Z")
    with (
        patch(
            "brain.app.ops.runtime_status.worker_liveness_checkpoint",
            AsyncMock(return_value=now - timedelta(seconds=20)),
        ),
        patch(
            "brain.app.ops.runtime_status.queued_backlog_health_snapshot_async",
            AsyncMock(
                return_value=SimpleNamespace(
                    queued=1,
                    oldest_queued_age_seconds=180,
                    stale_queued_backlog=False,
                )
            ),
        ),
        patch(
            "brain.app.ops.runtime_status.async_scheduler_health_snapshot",
            AsyncMock(
                return_value={
                    "lag": {"lag_seconds": 45, "lagging_jobs": [{"job_key": "demo"}]}
                }
            ),
        ),
        patch(
            "brain.app.ops.runtime_status.scheduler_liveness_checkpoint",
            AsyncMock(return_value=now - timedelta(seconds=30)),
        ),
        patch(
            "brain.app.ops.runtime_status.async_summarize_token_totals",
            usage_summary,
        ),
    ):
        snapshot = await runtime_status.async_runtime_status_snapshot(session, now=now)

    assert set(snapshot) == {
        "captured_at",
        "overall",
        "worker",
        "scheduler",
        "runs",
        "cycles",
        "spend",
        "deploy",
    }
    assert snapshot["worker"]["state"] == "good"
    assert snapshot["worker"]["heartbeat_age_seconds"] == 20
    assert snapshot["scheduler"]["state"] == "late"
    assert snapshot["runs"]["queued"] == 1
    assert snapshot["runs"]["running"] == 2
    assert snapshot["cycles"]["overdue"] == 1
    assert snapshot["cycles"]["items"][0]["name"] == "Daily brief"
    assert snapshot["cycles"]["items_truncated"] is False
    assert snapshot["spend"]["amount_usd"] == 0.375
    usage_summary.assert_awaited_once_with(
        session,
        since=now.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    assert snapshot["deploy"]["sha"] == "abc123"
    assert snapshot["overall"]["state"] == "late"


def test_deploy_wiring_passes_build_evidence_to_the_api_image():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "deploy/docker/api.Dockerfile").read_text(encoding="utf-8")
    compose = (root / "deploy/compose/docker-compose.yml").read_text(encoding="utf-8")
    upgrade = (root / "deploy/scripts/upgrade.sh").read_text(encoding="utf-8")
    launcher = (root / "illo").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/container-images.yml").read_text(
        encoding="utf-8"
    )

    for name in ("ILLO_BUILD_COMMIT", "ILLO_BUILD_TIME"):
        assert f"ARG {name}" in dockerfile
        assert name in compose
        assert f"export {name}" in upgrade
        assert f"export {name}" in launcher
        assert name in workflow
    assert "ILLO_DEPLOY_TIME" in compose
    assert "export ILLO_DEPLOY_TIME" in upgrade
    assert "export ILLO_DEPLOY_TIME" in launcher
    shared_backend_env = compose.split("x-illo-env: &illo-env", 1)[1].split(
        "x-backend-service:", 1
    )[0]
    assert "ILLO_AGENT_RUNNER_CONCURRENCY" in shared_backend_env


def test_runtime_panel_keeps_stale_and_detailed_evidence_visible():
    root = Path(__file__).resolve().parents[1]
    panel = (root / "frontend/src/routes/system/RuntimeStatusPanel.svelte").read_text(
        encoding="utf-8"
    )

    assert "The evidence below is stale." in panel
    assert "Snapshot captured" in panel
    assert "status.cycles.items" in panel
    assert "Showing the 20 most overdue cycles." in panel
    assert "status.deploy.built_at" in panel
    assert "status.deploy.process_started_at" in panel
