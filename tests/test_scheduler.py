"""Scheduler catalog and due-run materialization tests."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.orm import Session

from brain.platform.db.models.scheduler import SchedulerJob, SchedulerLease, SchedulerRun, SchedulerRunStep
from brain.app.scheduler.catalog import list_scheduler_jobs, sync_scheduler_catalog
from brain.app.scheduler.contracts import (
    extract_declared_actions,
    normalize_recurring_task_contract,
    validate_declared_actions,
    validate_recurring_task_contract,
)
from brain.app.scheduler.daemon import scheduler_daemon_tick, scheduler_health_snapshot
from brain.app.scheduler.executor import (
    execute_scheduler_run,
    run_scheduler_run,
    set_scheduler_job_load_shed,
    set_scheduler_job_owner_mode,
    set_scheduler_job_paused,
)
from brain.app.scheduler.planner import materialize_due_runs
from brain.app.scheduler.programs import build_scheduler_step_plan
from brain.app.scheduler.runtime import (
    RUN_STATUS_EXPIRED,
    RUN_STATUS_SETTLED_SUCCESS,
    RUN_STATUS_SHELVED,
    claim_next_due_run,
    ensure_run_steps,
    reclaim_expired_leases,
    update_run_step,
)


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
        SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "TEXT"

    original = SQLiteDDLCompiler.get_column_default_string

    def _patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = re.sub(r"::text\[\]", "", result)
        return result

    SQLiteDDLCompiler.get_column_default_string = _patched


def _register_sqlite_functions(dbapi_conn, connection_record):
    dbapi_conn.create_function("NOW", 0, lambda: datetime.utcnow().isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


def _register_sqlite_adapters():
    import sqlite3

    sqlite3.register_adapter(list, lambda val: json.dumps(val))
    sqlite3.register_adapter(dict, lambda val: json.dumps(val))


@pytest.fixture
def engine():
    _patch_sqlite_for_pg_types()
    _register_sqlite_adapters()
    eng = create_engine("sqlite://", echo=False)
    event.listen(eng, "connect", _register_sqlite_functions)
    SchedulerJob.__table__.create(eng, checkfirst=True)
    SchedulerRun.__table__.create(eng, checkfirst=True)
    SchedulerLease.__table__.create(eng, checkfirst=True)
    SchedulerRunStep.__table__.create(eng, checkfirst=True)
    return eng


@pytest.fixture
def session(engine):
    s = Session(engine)
    yield s
    s.close()


def _make_scheduler_job(**overrides):
    defaults = {
        "job_key": "nightly_sleep",
        "family": "nightly_sleep",
        "program_key": "nightly_sleep",
        "handler_kind": "scheduler_builtin",
        "handler_ref": "brain.app.scheduler.programs:nightly_sleep",
        "cron_expr": "0 3 * * *",
        "timezone": "UTC",
        "enabled": True,
        "owner_mode": "scheduler",
        "priority": 100,
        "max_concurrency": 1,
        "default_payload": {"name": "Nightly Sleep"},
        "task_contract": {
            "owner_user_id": "user-1",
            "org_id": "org-1",
            "memory_scope": {"visibility": "private", "user_id": "user-1"},
            "allowed_actions": ["scheduler.run"],
            "success_criteria": ["Nightly work completes"],
        },
        "next_run_at": datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return SchedulerJob(**defaults)


def test_sync_scheduler_catalog_seeds_scheduler_jobs_without_cron_table(session):
    now = datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc)

    result = sync_scheduler_catalog(session, timezone_name="UTC", now=now)
    session.flush()

    assert result == {"upserted": 2}
    jobs = {job["job_key"]: job for job in list_scheduler_jobs(session)}
    assert set(jobs) == {"curiosity_cron", "nightly_sleep"}
    assert jobs["nightly_sleep"]["owner_mode"] == "scheduler"
    assert jobs["nightly_sleep"]["handler_kind"] == "scheduler_builtin"
    assert jobs["nightly_sleep"]["default_payload"]["scheduler_split_steps"] is True
    assert jobs["nightly_sleep"]["next_run_at"].startswith("2026-04-21T03:00:00")
    for job in jobs.values():
        assert job["handler_kind"] == "scheduler_builtin"
        assert not job["handler_ref"].endswith(".sh")
        assert "script_path" not in job["default_payload"]
        assert "legacy_cron_retired" not in job["default_payload"]


def test_due_run_materialization_records_scheduler_jobs(session):
    job = _make_scheduler_job()
    session.add(job)
    session.flush()

    created = materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )
    session.refresh(job)

    assert len(created) == 1
    run = created[0]
    assert run.status == "recorded"
    assert run.idempotency_key.startswith("nightly_sleep:2026-04-21T03:00:00")
    assert run.task_contract["owner_user_id"] == "user-1"
    assert run.task_contract["org_id"] == "org-1"
    assert run.task_contract["schedule"]["job_key"] == "nightly_sleep"
    assert run.task_contract["success_criteria"] == ["Nightly work completes"]
    assert job.next_run_at.isoformat().startswith("2026-04-22T03:00:00")


def test_recurring_task_contract_normalization_and_validation():
    job = _make_scheduler_job(
        cron_expr="*/15 * * * *",
        timezone="America/Toronto",
        retry_policy={"max_attempts": 4, "backoff_seconds": 30},
        task_contract={
            "owner": {"user_id": "user-owner", "org_id": "org-owner"},
            "allowed_actions": ["scheduler.run", "memory.write"],
            "memory_scope": {"visibility": "org", "org_id": "org-owner"},
            "output_channel": "cortex",
            "success_criteria": ["Send a summary"],
        },
    )

    contract = normalize_recurring_task_contract(job)

    assert contract["owner_user_id"] == "user-owner"
    assert contract["org_id"] == "org-owner"
    assert contract["schedule"]["cron_expr"] == "*/15 * * * *"
    assert contract["retry_policy"] == {"max_attempts": 4, "backoff_seconds": 30}
    assert validate_recurring_task_contract(contract) == []


def test_declared_actions_must_fit_contract_scope(session):
    job = _make_scheduler_job(
        default_payload={
            "name": "Nightly Sleep",
            "action_manifest": {"tool_name": "vault.delete_secret"},
        },
        task_contract={
            "owner_user_id": "user-1",
            "org_id": "org-1",
            "memory_scope": {"visibility": "private", "user_id": "user-1"},
            "allowed_actions": ["scheduler.run"],
            "success_criteria": ["Nightly work completes"],
        },
    )
    session.add(job)
    session.flush()

    contract = normalize_recurring_task_contract(job)

    assert extract_declared_actions(job.default_payload) == ("vault.delete_secret",)
    assert validate_declared_actions(contract, job.default_payload) == [
        "Action(s) outside recurring task contract: vault.delete_secret"
    ]

    created = materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )

    assert len(created) == 1
    assert created[0].status == RUN_STATUS_SHELVED
    assert created[0].result_summary["reason"] == "contract_invalid"
    assert "vault.delete_secret" in created[0].result_summary["contract_errors"][0]


def test_run_scheduler_run_blocks_invalid_recurring_contract(session):
    job = _make_scheduler_job(
        owner_mode="scheduler",
        task_contract={
            "org_id": "org-1",
            "memory_scope": {"visibility": "private"},
            "success_criteria": ["Nightly work completes"],
        },
    )
    session.add(job)
    session.flush()

    run = SchedulerRun(
        job_id=job.id,
        scheduled_for=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
        window_start=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
        status="recorded",
        attempt=1,
        idempotency_key="nightly_sleep:invalid-contract",
        payload={},
    )
    session.add(run)
    session.flush()

    result = run_scheduler_run(
        session,
        run.id,
        owner_id="tester",
        runner=lambda command, *, cwd=None: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
        now=datetime(2026, 4, 21, 3, 2, tzinfo=timezone.utc),
    )

    assert result.status == "blocked"
    assert result.result_summary["reason"] == "contract_invalid"
    assert "memory_scope.user_id" in result.error_text


def test_due_run_materialization_skip_misfire_drops_backlog(session):
    job = _make_scheduler_job(
        misfire_policy="skip",
        next_run_at=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
    )
    session.add(job)
    session.flush()

    created = materialize_due_runs(
        session,
        now=datetime(2026, 4, 23, 3, 1, tzinfo=timezone.utc),
    )
    session.refresh(job)

    assert created == []
    assert session.scalar(select(func.count()).select_from(SchedulerRun)) == 0
    assert job.next_run_at.isoformat().startswith("2026-04-24T03:00:00")


def test_due_run_materialization_catch_up_records_missed_fires(session):
    job = _make_scheduler_job(
        misfire_policy="catch_up",
        next_run_at=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
    )
    session.add(job)
    session.flush()

    created = materialize_due_runs(
        session,
        now=datetime(2026, 4, 23, 3, 1, tzinfo=timezone.utc),
    )
    session.refresh(job)

    assert [run.scheduled_for.isoformat() for run in created] == [
        "2026-04-21T03:00:00+00:00",
        "2026-04-22T03:00:00+00:00",
        "2026-04-23T03:00:00+00:00",
    ]
    assert all(run.status == "recorded" for run in created)
    assert job.next_run_at.isoformat().startswith("2026-04-24T03:00:00")


def test_due_run_materialization_is_idempotent_after_restart(session):
    job = _make_scheduler_job()
    session.add(job)
    session.flush()

    first = materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )
    job.next_run_at = datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc)
    session.flush()

    second = materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )
    session.refresh(job)

    assert len(first) == 1
    assert second == []
    assert session.scalar(select(func.count()).select_from(SchedulerRun)) == 1
    assert job.next_run_at.isoformat().startswith("2026-04-22T03:00:00")


def test_load_shed_pause_new_runs_shelves_due_run(session):
    job = _make_scheduler_job(
        load_shed_policy={"pause_new_runs": True, "reason": "operator_pause"},
    )
    session.add(job)
    session.flush()

    created = materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )

    assert len(created) == 1
    assert created[0].status == RUN_STATUS_SHELVED
    assert created[0].result_summary["reason"] == "pause_new_runs"
    assert (
        claim_next_due_run(
            session,
            owner_id="scheduler-worker-a",
            now=datetime(2026, 4, 21, 3, 2, tzinfo=timezone.utc),
        )
        is None
    )


def test_max_concurrency_counts_active_leases(session):
    job = _make_scheduler_job(
        owner_mode="scheduler",
        next_run_at=datetime(2026, 4, 21, 4, 0, tzinfo=timezone.utc),
    )
    session.add(job)
    session.flush()
    active_run = SchedulerRun(
        job_id=job.id,
        scheduled_for=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
        window_start=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
        status="running",
        attempt=1,
        idempotency_key="nightly_sleep:active",
        payload={},
    )
    session.add(active_run)
    session.flush()
    active_lease = SchedulerLease(
        run_id=active_run.id,
        owner_id="scheduler-worker-a",
        owner_host="test",
        owner_pid=123,
        acquired_at=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
        heartbeat_at=datetime(2026, 4, 21, 3, 30, tzinfo=timezone.utc),
        expires_at=datetime(2026, 4, 21, 5, 0, tzinfo=timezone.utc),
    )
    session.add(active_lease)
    session.flush()
    active_run.lease_id = active_lease.id
    session.flush()

    created = materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 4, 1, tzinfo=timezone.utc),
        allowed_owner_modes=("scheduler",),
    )

    assert len(created) == 1
    assert created[0].status == RUN_STATUS_SHELVED
    assert created[0].result_summary["reason"] == "max_concurrency"
    assert created[0].result_summary["active_leases"] == 1


def test_due_run_materialization_skips_legacy_owner_modes_by_default(session):
    job = _make_scheduler_job(
        owner_mode="mirror",
        next_run_at=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
    )
    session.add(job)
    session.flush()

    created = materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )
    session.refresh(job)

    assert created == []
    assert session.scalar(select(func.count()).select_from(SchedulerRun)) == 0
    assert job.next_run_at.isoformat().startswith("2026-04-21T03:00:00")


def test_scheduler_cutover_materializes_and_persists_split_nightly_steps(session):
    job = _make_scheduler_job(
        owner_mode="scheduler",
        default_payload={"name": "Nightly Sleep", "scheduler_split_steps": True},
    )
    session.add(job)
    session.flush()

    created = materialize_due_runs(
        session,
        now=datetime(2026, 4, 22, 3, 1, tzinfo=timezone.utc),
        allowed_owner_modes=("scheduler",),
    )
    session.refresh(job)

    assert len(created) == 1
    assert created[0].status == "recorded"
    step_plan = build_scheduler_step_plan(job)
    assert len(step_plan) == 15
    assert "skill_quality" not in {step["step_key"] for step in step_plan}
    phase_steps = [step for step in step_plan if step["kind"] == "phase"]
    assert phase_steps[0]["payload"]["night_budget"]["work_type"] == "memory_conflict_resolution"
    assert {step["payload"]["night_budget"]["work_type"] for step in phase_steps} >= {
        "context_policy_eval",
        "memory_conflict_resolution",
        "reflection_dream",
        "repo_summary_refresh",
        "skill_eval",
    }
    assert job.next_run_at.isoformat().startswith("2026-04-22T03:00:00")


def test_nightly_step_plan_can_accept_budget_allowed_step_subset(session):
    job = _make_scheduler_job(
        owner_mode="scheduler",
        default_payload={
            "name": "Nightly Sleep",
            "scheduler_split_steps": True,
            "night_budget_allowed_steps": ["memory_consolidation", "reflection"],
        },
    )

    step_plan = build_scheduler_step_plan(job)

    assert [step["step_key"] for step in step_plan] == [
        "nightly_wrapper",
        "memory_consolidation",
        "reflection",
    ]
    assert step_plan[1]["payload"]["night_budget"]["work_type"] == "memory_conflict_resolution"
    assert step_plan[2]["payload"]["night_budget"]["work_type"] == "reflection_dream"


def test_scheduler_lease_claim_and_reclaim(session):
    job = _make_scheduler_job(
        owner_mode="scheduler",
        default_payload={"name": "Nightly Sleep"},
    )
    session.add(job)
    session.flush()

    materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
        allowed_owner_modes=("scheduler",),
    )

    claimed = claim_next_due_run(
        session,
        owner_id="scheduler-worker-a",
        allowed_owner_modes=("scheduler",),
        lease_ttl_seconds=30,
        now=datetime(2026, 4, 21, 3, 2, tzinfo=timezone.utc),
    )
    assert claimed is not None
    claimed_run, claimed_lease = claimed
    assert claimed_run.status == "claimed"
    assert claimed_run.lease_id == claimed_lease.id

    reclaimed = reclaim_expired_leases(
        session,
        now=datetime(2026, 4, 21, 3, 2, 31, tzinfo=timezone.utc),
    )
    session.refresh(claimed_run)

    assert len(reclaimed) == 1
    assert reclaimed[0].id == claimed_run.id
    assert claimed_run.status == RUN_STATUS_EXPIRED
    assert claimed_run.attempt == 1

    reclaimed_again = claim_next_due_run(
        session,
        owner_id="scheduler-worker-b",
        allowed_owner_modes=("scheduler",),
        lease_ttl_seconds=30,
        now=datetime(2026, 4, 21, 3, 2, 32, tzinfo=timezone.utc),
    )
    assert reclaimed_again is not None
    reclaimed_run, reclaimed_lease = reclaimed_again
    assert reclaimed_run.id == claimed_run.id
    assert reclaimed_run.status == "claimed"
    assert reclaimed_run.attempt == 2
    assert reclaimed_run.lease_id == reclaimed_lease.id


def test_scheduler_rejects_legacy_owner_mode_cutback(session):
    job = _make_scheduler_job(default_payload={"name": "Nightly Sleep"})
    session.add(job)
    session.flush()

    with pytest.raises(ValueError, match="Legacy cron/mirror owner modes are retired"):
        set_scheduler_job_owner_mode(session, job.job_key, owner_mode="mirror")


def test_scheduler_health_snapshot_reports_lag_and_paused_jobs(session):
    due_job = _make_scheduler_job(
        job_key="scheduler_due",
        family="scheduler_due",
        program_key="scheduler_due",
        handler_kind="command",
        handler_ref='python3 -c "print(\\"due\\")"',
        owner_mode="scheduler",
        next_run_at=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
        default_payload={"name": "Scheduler Due"},
    )
    paused_job = _make_scheduler_job(
        job_key="scheduler_paused",
        family="scheduler_paused",
        program_key="scheduler_paused",
        handler_kind="command",
        handler_ref='python3 -c "print(\\"paused\\")"',
        owner_mode="scheduler",
        enabled=False,
        pause_reason="manual_pause",
        next_run_at=datetime(2026, 4, 21, 4, 0, tzinfo=timezone.utc),
        default_payload={"name": "Scheduler Paused"},
    )
    session.add_all([due_job, paused_job])
    session.flush()

    snapshot = scheduler_health_snapshot(
        session,
        owner_mode="scheduler",
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )

    assert snapshot["health"]["status"] == "degraded"
    assert snapshot["summary"]["jobs_in_scope"] == 2
    assert snapshot["summary"]["lagging_jobs"] == 1
    assert snapshot["lag"]["lag_seconds"] == 60
    assert snapshot["pause"]["paused_job_keys"] == ["scheduler_paused"]
    assert snapshot["pause"]["global_pause"] is False


def test_scheduler_health_snapshot_reports_fully_paused_scope(session):
    job = _make_scheduler_job(
        job_key="scheduler_paused_only",
        family="scheduler_paused_only",
        program_key="scheduler_paused_only",
        handler_kind="command",
        handler_ref='python3 -c "print(\\"paused\\")"',
        owner_mode="scheduler",
        enabled=False,
        pause_reason="manual_pause",
        next_run_at=datetime(2026, 4, 21, 4, 0, tzinfo=timezone.utc),
        default_payload={"name": "Scheduler Paused Only"},
    )
    session.add(job)
    session.flush()

    snapshot = scheduler_health_snapshot(
        session,
        owner_mode="scheduler",
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )

    assert snapshot["health"]["status"] == "paused"
    assert snapshot["pause"]["global_pause"] is True
    assert snapshot["pause"]["paused_job_keys"] == ["scheduler_paused_only"]


def test_scheduler_daemon_tick_executes_due_scheduler_job(session):
    job = _make_scheduler_job(
        job_key="scheduler_tick",
        family="scheduler_tick",
        program_key="scheduler_tick",
        handler_kind="command",
        handler_ref='python3 -c "print(\\"tick\\")"',
        owner_mode="scheduler",
        next_run_at=datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc),
        default_payload={"name": "Scheduler Tick"},
    )
    session.add(job)
    session.flush()

    result = scheduler_daemon_tick(
        session,
        owner_mode="scheduler",
        max_runs=5,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )
    session.refresh(job)

    assert result["ok"] is True
    assert result["reclaimed"] == 0
    assert result["drain"]["executed"] == 1
    assert result["snapshot"]["health"]["status"] == "healthy"
    run = session.scalar(select(SchedulerRun).where(SchedulerRun.job_id == job.id))
    assert run is not None
    assert run.status == "settled_success"
    assert run.finished_at is not None


def test_scheduler_job_controls_pause_resume_cutover_and_load_shed(session):
    job = _make_scheduler_job(
        job_key="scheduler_controls",
        family="scheduler_controls",
        program_key="scheduler_controls",
        handler_kind="command",
        handler_ref='python3 -c "print(\\"control\\")"',
        owner_mode="mirror",
        default_payload={"name": "Scheduler Controls"},
    )
    session.add(job)
    session.flush()

    paused = set_scheduler_job_paused(session, job.job_key, paused=True, reason="manual_pause")
    assert paused.enabled is False
    assert paused.pause_reason == "manual_pause"

    resumed = set_scheduler_job_paused(session, job.job_key, paused=False)
    assert resumed.enabled is True
    assert resumed.pause_reason is None

    cutover = set_scheduler_job_owner_mode(session, job.job_key, owner_mode="scheduler")
    assert cutover.owner_mode == "scheduler"

    load_shed = set_scheduler_job_load_shed(
        session,
        job.job_key,
        max_concurrency=2,
        pause_new_runs=True,
        reason="backlog",
    )
    assert load_shed.max_concurrency == 2
    assert load_shed.load_shed_policy["pause_new_runs"] is True
    assert load_shed.load_shed_policy["reason"] == "backlog"


def test_step_persistence_upserts_and_updates(session):
    job = _make_scheduler_job()
    session.add(job)
    session.flush()

    materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
    )
    run = session.scalar(select(SchedulerRun).order_by(SchedulerRun.id.asc()))
    assert run is not None

    steps = ensure_run_steps(
        session,
        run,
        [
            {"step_key": "nightly_wrapper", "sequence_no": 1},
            {"step_key": "phase_a", "sequence_no": 2},
        ],
    )
    assert [step.step_key for step in steps] == ["nightly_wrapper", "phase_a"]
    assert session.scalar(
        select(func.count()).select_from(SchedulerRunStep).where(SchedulerRunStep.run_id == run.id)
    ) == 2

    steps_again = ensure_run_steps(
        session,
        run,
        [
            {"step_key": "nightly_wrapper", "sequence_no": 1},
            {"step_key": "phase_a", "sequence_no": 2},
        ],
    )
    assert len(steps_again) == 2

    updated = update_run_step(
        session,
        steps[0],
        status=RUN_STATUS_SETTLED_SUCCESS,
        result_summary={"ok": True},
        finished_at=datetime(2026, 4, 21, 3, 2, tzinfo=timezone.utc),
    )
    session.refresh(updated)
    assert updated.status == RUN_STATUS_SETTLED_SUCCESS
    assert updated.result_summary == {"ok": True}


def test_run_scheduler_run_persists_step_failures_and_resumes(session):
    job = _make_scheduler_job(
        owner_mode="scheduler",
        default_payload={
            "name": "Nightly Sleep",
            "step_plan": [
                {"step_key": "step_a", "sequence_no": 1, "command": "python3 -m brain.jobs.pipelines.consolidate --phase all"},
                {"step_key": "step_b", "sequence_no": 2, "command": "python3 -m brain.app.cli.skills evolve"},
            ],
        },
    )
    session.add(job)
    session.flush()

    created = materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
        allowed_owner_modes=("scheduler",),
    )
    run = created[0]

    calls: list[str] = []

    def runner(command, *, cwd=None):
        rendered = " ".join(command)
        calls.append(rendered)
        if "brain.app.cli.skills" in rendered:
            return SimpleNamespace(returncode=1, stdout="step ok", stderr="step failed")
        return SimpleNamespace(returncode=0, stdout="step ok", stderr="")

    first = run_scheduler_run(
        session,
        run.id,
        owner_id="tester",
        runner=runner,
        now=datetime(2026, 4, 21, 3, 2, tzinfo=timezone.utc),
    )
    session.refresh(first)

    assert first.status == "retryable"
    steps = session.scalars(
        select(SchedulerRunStep).where(SchedulerRunStep.run_id == run.id).order_by(SchedulerRunStep.sequence_no.asc())
    ).all()
    assert steps[0].status == RUN_STATUS_SETTLED_SUCCESS
    assert steps[1].status == "retryable"
    assert any("brain.jobs.pipelines.consolidate" in call for call in calls)

    calls.clear()

    resumed = run_scheduler_run(
        session,
        run.id,
        owner_id="tester",
        runner=lambda command, *, cwd=None: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
        now=datetime(2026, 4, 21, 3, 10, tzinfo=timezone.utc),
    )
    session.refresh(resumed)

    assert resumed.status == "settled_success"
    steps = session.scalars(
        select(SchedulerRunStep).where(SchedulerRunStep.run_id == run.id).order_by(SchedulerRunStep.sequence_no.asc())
    ).all()
    assert all(step.status == RUN_STATUS_SETTLED_SUCCESS for step in steps)
    assert all("brain.jobs.pipelines.consolidate" not in call for call in calls)


def test_retry_policy_backoff_controls_automatic_reclaim(session):
    job = _make_scheduler_job(
        owner_mode="scheduler",
        retry_policy={"max_attempts": 2, "backoff_seconds": 300},
        default_payload={
            "name": "Retry Backoff",
            "step_plan": [
                {"step_key": "step_a", "sequence_no": 1, "command": "python3 -m brain.app.cli.skills evolve"},
            ],
        },
    )
    session.add(job)
    session.flush()
    run = materialize_due_runs(
        session,
        now=datetime(2026, 4, 21, 3, 1, tzinfo=timezone.utc),
        allowed_owner_modes=("scheduler",),
    )[0]

    failed = run_scheduler_run(
        session,
        run.id,
        owner_id="tester",
        runner=lambda command, *, cwd=None: SimpleNamespace(returncode=1, stdout="", stderr="failed"),
        now=datetime(2026, 4, 21, 3, 2, tzinfo=timezone.utc),
    )
    session.refresh(failed)

    assert failed.status == "retryable"
    assert failed.attempt == 1
    assert failed.result_summary["next_retry_at"] == "2026-04-21T03:07:00+00:00"
    assert (
        claim_next_due_run(
            session,
            owner_id="scheduler-worker-a",
            allowed_owner_modes=("scheduler",),
            now=datetime(2026, 4, 21, 3, 6, 59, tzinfo=timezone.utc),
        )
        is None
    )

    retry_claim = claim_next_due_run(
        session,
        owner_id="scheduler-worker-a",
        allowed_owner_modes=("scheduler",),
        now=datetime(2026, 4, 21, 3, 7, tzinfo=timezone.utc),
    )

    assert retry_claim is not None
    retry_run, retry_lease = retry_claim
    assert retry_run.id == failed.id
    assert retry_run.attempt == 2
    assert retry_run.lease_id == retry_lease.id


def test_execute_scheduler_run_blocks_invalid_callable_private_contract(session, monkeypatch):
    job = _make_scheduler_job(
        handler_kind="python_callable",
        handler_ref="tests.test_scheduler:should_not_run",
        task_contract={
            "org_id": "org-1",
            "memory_scope": {"visibility": "private"},
            "allowed_actions": ["scheduler.run"],
            "success_criteria": ["Callable work completes"],
        },
    )
    session.add(job)
    session.flush()

    run = SchedulerRun(
        job_id=job.id,
        scheduled_for=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        window_start=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        status="recorded",
        attempt=1,
        idempotency_key="callable:invalid-private-contract",
        payload={},
    )
    session.add(run)
    session.flush()

    monkeypatch.setattr(
        "brain.app.scheduler.executor._resolve_handler",
        lambda handler_ref: pytest.fail("invalid callable contract should not resolve handler"),
    )

    executed = execute_scheduler_run(
        session,
        run.id,
        owner_id="callable-worker",
        now=datetime(2026, 4, 21, 12, 5, tzinfo=timezone.utc),
    )
    session.refresh(executed)

    assert executed.status == "blocked"
    assert executed.lease_id is None
    assert executed.result_summary["reason"] == "contract_invalid"
    assert executed.result_summary["task_contract"] == executed.task_contract
    assert "memory_scope.user_id" in executed.error_text
    step_count = session.scalar(
        select(func.count()).select_from(SchedulerRunStep).where(SchedulerRunStep.run_id == run.id)
    )
    assert step_count == 0


def test_execute_scheduler_run_blocks_callable_action_outside_contract(session, monkeypatch):
    job = _make_scheduler_job(
        handler_kind="python_callable",
        handler_ref="tests.test_scheduler:should_not_run",
        default_payload={
            "name": "Callable Action Scope",
            "action_manifest": {"tool_name": "vault.delete_secret"},
        },
        task_contract={
            "owner_user_id": "user-1",
            "org_id": "org-1",
            "memory_scope": {"visibility": "private", "user_id": "user-1"},
            "allowed_actions": ["scheduler.run"],
            "success_criteria": ["Callable work completes"],
        },
    )
    session.add(job)
    session.flush()

    run = SchedulerRun(
        job_id=job.id,
        scheduled_for=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        window_start=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        status="recorded",
        attempt=1,
        idempotency_key="callable:invalid-action-contract",
        payload=job.default_payload,
    )
    session.add(run)
    session.flush()

    monkeypatch.setattr(
        "brain.app.scheduler.executor._resolve_handler",
        lambda handler_ref: pytest.fail("invalid callable contract should not resolve handler"),
    )

    executed = execute_scheduler_run(
        session,
        run.id,
        owner_id="callable-worker",
        now=datetime(2026, 4, 21, 12, 5, tzinfo=timezone.utc),
    )
    session.refresh(executed)

    assert executed.status == "blocked"
    assert executed.lease_id is None
    assert executed.result_summary["reason"] == "contract_invalid"
    assert executed.result_summary["task_contract"] == executed.task_contract
    assert "vault.delete_secret" in executed.error_text
    step_count = session.scalar(
        select(func.count()).select_from(SchedulerRunStep).where(SchedulerRunStep.run_id == run.id)
    )
    assert step_count == 0


def test_execute_scheduler_run_valid_callable_stores_normalized_contract(session, monkeypatch):
    calls: list[dict] = []

    def handler(payload, *, now=None):
        calls.append(dict(payload))
        return {"status": "recorded", "value": "ok"}

    job = _make_scheduler_job(
        handler_kind="python_callable",
        handler_ref="tests.test_scheduler:callable_handler",
        default_payload={
            "name": "Callable Valid",
            "user_id": "user-from-payload",
            "org_id": "org-from-payload",
            "action_manifest": {"tool_name": "scheduler.run"},
        },
        task_contract={
            "memory_scope": {"visibility": "private"},
            "allowed_actions": ["scheduler.run"],
            "success_criteria": ["Callable work completes"],
        },
    )
    session.add(job)
    session.flush()

    run = SchedulerRun(
        job_id=job.id,
        scheduled_for=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        window_start=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        status="recorded",
        attempt=1,
        idempotency_key="callable:valid-contract",
        payload=job.default_payload,
    )
    session.add(run)
    session.flush()

    monkeypatch.setattr("brain.app.scheduler.executor._resolve_handler", lambda handler_ref: handler)

    executed = execute_scheduler_run(
        session,
        run.id,
        owner_id="callable-worker",
        now=datetime(2026, 4, 21, 12, 5, tzinfo=timezone.utc),
    )
    session.refresh(executed)

    assert executed.status == RUN_STATUS_SETTLED_SUCCESS
    assert calls == [job.default_payload]
    assert executed.task_contract["owner_user_id"] == "user-from-payload"
    assert executed.task_contract["org_id"] == "org-from-payload"
    assert executed.task_contract["memory_scope"]["user_id"] == "user-from-payload"
    assert executed.task_contract["allowed_actions"] == ["scheduler.run"]
    assert executed.result_summary["handler_result"]["status"] == "recorded"
    assert executed.result_summary["execution"]["owner_id"] == "callable-worker"


def test_scheduler_executor_settles_bounded_agency_handoff(session):
    job = _make_scheduler_job(
        handler_kind="python_callable",
        handler_ref="brain.systems.agency.handoff:run_candidate",
        default_payload={
            "candidate_id": 1,
            "candidate_key": "candidate-1",
            "proposal_kind": "curiosity_followup",
            "source_type": "curiosity_reading",
            "source_refs": [{"kind": "reading_source", "url": "https://example.com"}],
            "proposed_run_payload": {
                "item_title": "Example",
                "item_url": "https://example.com",
                "concrete_application": "Review the pattern",
            },
            "budget_snapshot": {"auto_execute_enabled": True},
            "policy_snapshot": {"drive_type": "curiosity"},
            "decision": "approve",
            "execution_class": "read_only",
        },
    )
    session.add(job)
    session.flush()

    run = SchedulerRun(
        job_id=job.id,
        scheduled_for=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        window_start=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
        status="recorded",
        attempt=1,
        idempotency_key="agency:handoff:1",
        payload=job.default_payload,
    )
    session.add(run)
    session.flush()

    executed = execute_scheduler_run(
        session,
        run.id,
        owner_id="agency",
        now=datetime(2026, 4, 21, 12, 5, tzinfo=timezone.utc),
    )
    session.refresh(job)
    session.refresh(executed)

    assert executed.status == "settled_success"
    assert executed.lease_id is not None
    assert executed.result_summary["handler_result"]["status"] == "recorded"
    assert executed.result_summary["execution"]["owner_id"] == "agency"
    assert job.last_started_at is not None
    assert job.last_finished_at is not None

    lease = session.get(SchedulerLease, executed.lease_id)
    assert lease is not None
    assert lease.released_at is not None
    assert lease.release_reason == "run_settled_success"

    steps = session.scalars(select(SchedulerRunStep).where(SchedulerRunStep.run_id == executed.id)).all()
    assert len(steps) == 1
    assert steps[0].status == "completed"
    assert steps[0].result_summary["handler_result"]["status"] == "recorded"
