"""Cycle failure-guard persistence, evaluation, and delivery tests."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.cycle import (
    Cycle,
    CycleFailureGuardLatch,
    CycleFailureGuardObservation,
    CycleRun,
    CycleRunEvaluation,
)
from brain.systems.cycles import memory as cycle_memory
import brain.systems.cycles.cycle_failure_guard as cycle_failure_guard
from brain.systems.cycles.status import CYCLE_RUN_TERMINAL_STATUSES


FAILURE_GUARD_TABLES = (
    Cycle.__table__,
    CycleRun.__table__,
    CycleFailureGuardLatch.__table__,
    CycleFailureGuardObservation.__table__,
    CycleRunEvaluation.__table__,
)


async def _cycle_latches(session, cycle_id: int):
    result = await session.scalars(
        select(CycleFailureGuardLatch).where(
            CycleFailureGuardLatch.cycle_id == cycle_id
        )
    )
    return {latch.trigger_kind: latch for latch in result.all()}


def test_cycle_model_exposes_failure_guard_state():
    columns = Cycle.__table__.columns

    assert columns["failure_signature"].type.length == 64
    assert columns["consecutive_failure_count"].nullable is False
    assert columns["last_failure_error"].nullable is True
    assert "failure_alerted_at" not in columns
    assert [
        column.name
        for column in CycleFailureGuardLatch.__table__.primary_key.columns
    ] == ["cycle_id", "trigger_kind"]
    assert [
        column.name
        for column in CycleFailureGuardObservation.__table__.primary_key.columns
    ] == ["cycle_run_id"]


def test_cycle_terminal_policy_is_total_for_canonical_terminal_statuses():
    assert (
        set(cycle_failure_guard.CYCLE_TERMINAL_POLICIES)
        == CYCLE_RUN_TERMINAL_STATUSES
    )
    assert {
        status: policy.action
        for status, policy in cycle_failure_guard.CYCLE_TERMINAL_POLICIES.items()
    } == {
        "completed": cycle_failure_guard.CycleTerminalAction.RESET,
        "failed": cycle_failure_guard.CycleTerminalAction.RECORD_FAILURE,
        "skipped": cycle_failure_guard.CycleTerminalAction.IGNORE,
        "degraded": cycle_failure_guard.CycleTerminalAction.RECORD_FAILURE,
        "auth_blocked": cycle_failure_guard.CycleTerminalAction.RECORD_FAILURE,
    }


def test_cycle_failure_guard_migration_round_trips(monkeypatch):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0046_cycle_failure_guard"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE cycles (id INTEGER PRIMARY KEY)"))
        connection.execute(
            sa.text("CREATE TABLE cycle_runs (id INTEGER PRIMARY KEY)")
        )
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
            "last_failure_error",
        }
        assert columns["consecutive_failure_count"]["nullable"] is False
        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns(
                "cycle_failure_guard_latches"
            )
        } == {"cycle_id", "trigger_kind", "alerted_at"}
        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns(
                "cycle_failure_guard_observations"
            )
        } == {"cycle_run_id", "observed_at"}

        migration.downgrade()
        migration.downgrade()
        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("cycles")
        } == {"id"}
        assert {
            "cycle_failure_guard_latches",
            "cycle_failure_guard_observations",
        }.isdisjoint(sa.inspect(connection).get_table_names())


async def test_terminal_cycle_failures_post_one_named_alert_without_prompt_cooperation(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    session = await async_sqlite_session_factory(FAILURE_GUARD_TABLES)
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
        assert requested_by == "cycle_failure_alert"
        assert reason == "Deliver a repeated cycle failure alert to the team."
        return FakeSlackClient()

    monkeypatch.setenv("CYCLE_FAILURE_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("ILLO_CYCLE_FAILURE_ALERT_CHANNEL", "#alerts")
    monkeypatch.setenv("ILLO_PUBLIC_URL", "https://illo.example.com")
    monkeypatch.setattr(
        cycle_failure_guard,
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
    assert (await _cycle_latches(session, cycle.id))["consecutive"].alerted_at
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
    session = await async_sqlite_session_factory(FAILURE_GUARD_TABLES)
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

    monkeypatch.setenv("CYCLE_FAILURE_ALERT_THRESHOLD", "99")
    monkeypatch.setenv("ILLO_CYCLE_FAILURE_ALERT_CHANNEL", "C_ALERTS")
    monkeypatch.setattr(
        cycle_failure_guard,
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
    first_alerted_at = (
        await _cycle_latches(session, cycle.id)
    )["consecutive"].alerted_at

    await cycle_memory.finalize_cycle_run(
        blocked_run,
        cycle,
        session=session,
        status="auth_blocked",
        error=blocked_error,
    )

    assert first_count == 1
    assert cycle.consecutive_failure_count == first_count
    assert (
        await _cycle_latches(session, cycle.id)
    )["consecutive"].alerted_at == first_alerted_at
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
    assert await _cycle_latches(session, cycle.id) == {}
    assert cycle.last_failure_error is None
    assert len(calls) == 1


async def test_cycle_two_production_outcome_sequence_crosses_the_guard(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    session = await async_sqlite_session_factory(FAILURE_GUARD_TABLES)
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

    monkeypatch.setenv("CYCLE_FAILURE_ALERT_THRESHOLD", "3")
    monkeypatch.setenv("ILLO_CYCLE_FAILURE_ALERT_CHANNEL", "C_ALERTS")
    monkeypatch.setattr(
        cycle_failure_guard,
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


async def test_terminal_observation_claim_is_idempotent_across_two_sessions(
    tmp_path,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    monkeypatch.setenv("CYCLE_FAILURE_ALERT_THRESHOLD", "99")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'cycle-guard.db'}",
        echo=False,
    )
    async with engine.begin() as connection:
        for table in FAILURE_GUARD_TABLES:
            await connection.execute(CreateTable(table, if_not_exists=True))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    scheduled_for = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

    try:
        async with factory() as first_session:
            cycle = Cycle(
                id=41,
                user_id=str(uuid4()),
                org_id=None,
                name="Two-worker cycle",
                prompt="Prove terminal idempotency.",
                schedule_expr="0 * * * *",
                timezone="UTC",
                enabled=True,
            )
            run = CycleRun(
                id=73,
                cycle_id=cycle.id,
                scheduled_for=scheduled_for,
                status="running",
                prompt_snapshot=cycle.prompt,
            )
            first_session.add_all([cycle, run])
            await first_session.commit()
            await cycle_memory.finalize_cycle_run(
                run,
                cycle,
                session=first_session,
                status="failed",
                error="RuntimeError: one terminal outcome",
            )
            await first_session.commit()

        async with factory() as second_session:
            second_cycle = await second_session.get(Cycle, 41)
            second_run = await second_session.get(CycleRun, 73)
            assert second_cycle is not None
            assert second_run is not None
            await cycle_memory.finalize_cycle_run(
                second_run,
                second_cycle,
                session=second_session,
                status="failed",
                error="RuntimeError: one terminal outcome",
            )
            await second_session.commit()
            await second_session.refresh(second_cycle)

            observation_count = await second_session.scalar(
                select(func.count()).select_from(
                    CycleFailureGuardObservation
                )
            )
            assert observation_count == 1
            assert second_cycle.consecutive_failure_count == 1
    finally:
        await engine.dispose()


async def test_terminal_observation_claim_reraises_unrelated_integrity_error():
    class NestedTransaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FailingSession:
        def begin_nested(self):
            return NestedTransaction()

        def add(self, value):
            pass

        async def flush(self):
            raise IntegrityError(
                "insert observation",
                {},
                RuntimeError("unrelated constraint"),
            )

        async def get(self, model, primary_key):
            return None

    store = cycle_failure_guard.CycleFailureGuardStore(
        session=FailingSession(),
        cycle_id=1,
    )

    with pytest.raises(IntegrityError, match="unrelated constraint"):
        await store.claim_observation(
            cycle_run_id=73,
            observed_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        )
