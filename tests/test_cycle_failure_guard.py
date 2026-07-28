"""Cycle failure-guard persistence, evaluation, and delivery tests."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from brain.platform.db.models.cycle import Cycle, CycleRun, CycleRunEvaluation
from brain.systems.cycles import memory as cycle_memory
import brain.systems.failure_guard as shared_failure_guard


def test_cycle_model_exposes_failure_guard_state():
    columns = Cycle.__table__.columns

    assert columns["failure_signature"].type.length == 64
    assert columns["consecutive_failure_count"].nullable is False
    assert columns["failure_alerted_at"].nullable is True
    assert columns["last_failure_error"].nullable is True


def test_cycle_failure_guard_migration_round_trips(monkeypatch):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0046_cycle_failure_guard"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE cycles (id INTEGER PRIMARY KEY)"))
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column["name"]: column
            for column in sa.inspect(connection).get_columns("cycles")
        }
        assert set(columns) == {
            "id",
            "failure_signature",
            "consecutive_failure_count",
            "failure_alerted_at",
            "last_failure_error",
        }
        assert columns["consecutive_failure_count"]["nullable"] is False

        migration.downgrade()
        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("cycles")
        } == {"id"}


async def test_terminal_cycle_failures_post_one_named_alert_without_prompt_cooperation(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    session = await async_sqlite_session_factory(
        [
            Cycle.__table__,
            CycleRun.__table__,
            CycleRunEvaluation.__table__,
        ]
    )
    cycle = Cycle(
        id=2,
        user_id=str(uuid4()),
        org_id=None,
        name="Uwear Ticket Coordinator Check-ins",
        prompt="Coordinate tickets.",
        schedule_expr="0 * * * *",
        timezone="UTC",
        enabled=True,
    )
    session.add(cycle)
    await session.flush()

    calls: list[dict[str, str]] = []

    class FakeSlackClient:
        async def conversations_list(self, **kwargs):
            return {"channels": [{"id": "C_ALERTS", "name": "alerts"}]}

        async def post_message(self, *, channel, text):
            calls.append({"channel": channel, "text": text})
            return {"ok": True, "message": {"text": text}}

    async def fake_client_from_runtime(*, requested_by, reason):
        assert requested_by == "scheduler_failure_alert"
        assert reason == "Deliver a repeated scheduler job failure alert to the team."
        return FakeSlackClient()

    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "#alerts")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    monkeypatch.setattr(
        shared_failure_guard,
        "slack_web_client_from_runtime",
        fake_client_from_runtime,
    )
    base = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    for offset in range(3):
        scheduled_for = base + timedelta(hours=offset)
        run = CycleRun(
            cycle_id=cycle.id,
            scheduled_for=scheduled_for,
            status="running",
            prompt_snapshot=cycle.prompt,
        )
        session.add(run)
        await session.flush()
        await cycle_memory.finalize_cycle_run(
            run,
            cycle,
            session=session,
            status="failed",
            error="RuntimeError: coordinator stopped",
        )

    assert cycle.consecutive_failure_count == 3
    assert cycle.failure_alerted_at is not None
    assert cycle.last_failure_error == "RuntimeError: coordinator stopped"
    assert calls == [
        {
            "channel": "C_ALERTS",
            "text": (
                "Cycle repeated failure\n"
                "Cycle: Uwear Ticket Coordinator Check-ins (#2)\n"
                "Failure count: 3\n"
                "Window: 3 consecutive scheduled runs\n"
                "Error: RuntimeError: coordinator stopped\n"
                "Cycle: <https://illo.example.com/cycles?cycle_id=2"
                f"&run_id={run.id}|open cycle state>"
            ),
        }
    ]


async def test_auth_blocked_alerts_with_reconnect_action_then_completion_resets(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    session = await async_sqlite_session_factory(
        [
            Cycle.__table__,
            CycleRun.__table__,
            CycleRunEvaluation.__table__,
        ]
    )
    cycle = Cycle(
        id=7,
        user_id=str(uuid4()),
        org_id=None,
        name="OpenAI Coordinator",
        prompt="Coordinate with OpenAI.",
        schedule_expr="*/15 * * * *",
        timezone="UTC",
        enabled=True,
    )
    blocked_run = CycleRun(
        cycle_id=cycle.id,
        scheduled_for=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
        status="running",
        prompt_snapshot=cycle.prompt,
    )
    session.add_all([cycle, blocked_run])
    await session.flush()

    calls: list[dict[str, str]] = []

    class FakeSlackClient:
        async def post_message(self, *, channel, text):
            calls.append({"channel": channel, "text": text})
            return {"ok": True, "message": {"text": text}}

    async def fake_client_from_runtime(*, requested_by, reason):
        return FakeSlackClient()

    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "C_ALERTS")
    monkeypatch.setattr(
        shared_failure_guard,
        "slack_web_client_from_runtime",
        fake_client_from_runtime,
    )
    blocked_error = (
        "OpenAI Codex / ChatGPT access expired. Open Settings > Access."
    )

    await cycle_memory.finalize_cycle_run(
        blocked_run,
        cycle,
        session=session,
        status="auth_blocked",
        error=blocked_error,
    )
    first_count = cycle.consecutive_failure_count
    first_alerted_at = cycle.failure_alerted_at

    await cycle_memory.finalize_cycle_run(
        blocked_run,
        cycle,
        session=session,
        status="auth_blocked",
        error=blocked_error,
    )

    assert first_count == 1
    assert cycle.consecutive_failure_count == first_count
    assert cycle.failure_alerted_at == first_alerted_at
    assert len(calls) == 1
    assert calls[0]["channel"] == "C_ALERTS"
    assert calls[0]["text"].startswith(
        "Cycle authentication blocked\n"
        "Cycle: OpenAI Coordinator (#7)\n"
        "Failure count: 1\n"
        "Window: 1 scheduled interval\n"
        "Action: reconnect OpenAI in Settings > Access\n"
    )

    completed_run = CycleRun(
        cycle_id=cycle.id,
        scheduled_for=datetime(2026, 7, 26, 12, 15, tzinfo=timezone.utc),
        status="running",
        prompt_snapshot=cycle.prompt,
    )
    session.add(completed_run)
    await session.flush()
    await cycle_memory.finalize_cycle_run(
        completed_run,
        cycle,
        session=session,
        status="completed",
    )

    assert cycle.failure_signature is None
    assert cycle.consecutive_failure_count == 0
    assert cycle.failure_alerted_at is None
    assert cycle.last_failure_error is None
    assert len(calls) == 1


async def test_cycle_two_production_outcome_sequence_crosses_the_guard(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    session = await async_sqlite_session_factory(
        [
            Cycle.__table__,
            CycleRun.__table__,
            CycleRunEvaluation.__table__,
        ]
    )
    cycle = Cycle(
        id=2,
        user_id=str(uuid4()),
        org_id=None,
        name="Uwear Ticket Coordinator Check-ins",
        prompt="Coordinate tickets.",
        schedule_expr="0 * * * *",
        timezone="UTC",
        enabled=True,
    )
    session.add(cycle)
    await session.flush()

    delivered: list[str] = []

    class FakeSlackClient:
        async def post_message(self, *, channel, text):
            assert channel == "C_ALERTS"
            delivered.append(text)
            return {"ok": True, "message": {"text": text}}

    async def fake_client_from_runtime(*, requested_by, reason):
        return FakeSlackClient()

    monkeypatch.setenv("SCHEDULER_FAILURE_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "C_ALERTS")
    monkeypatch.setattr(
        shared_failure_guard,
        "slack_web_client_from_runtime",
        fake_client_from_runtime,
    )
    outcomes = [
        "failed",
        "skipped",
        "failed",
        "failed",
        *(["auth_blocked"] * 5),
        "failed",
        "degraded",
        "degraded",
    ]
    error_by_status = {
        "failed": "RuntimeError: coordinator stopped",
        "skipped": None,
        "auth_blocked": "OpenAI access expired. Open Settings > Access.",
        "degraded": "mission_contract_failed: coordinator digest incomplete",
    }
    base = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    for offset, status in enumerate(outcomes):
        run = CycleRun(
            cycle_id=cycle.id,
            scheduled_for=base + timedelta(hours=offset * 8),
            status="running",
            prompt_snapshot=cycle.prompt,
        )
        session.add(run)
        await session.flush()
        await cycle_memory.finalize_cycle_run(
            run,
            cycle,
            session=session,
            status=status,
            error=error_by_status[status],
            skip_reason="previous_run_active" if status == "skipped" else None,
        )

    assert delivered
    assert any(text.startswith("Cycle repeated failure") for text in delivered)
    assert any(text.startswith("Cycle authentication blocked") for text in delivered)
