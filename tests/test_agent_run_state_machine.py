"""Restart-safety tests for the AgentRun state machine."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, AsyncIterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from brain.systems.runs.domain import AgentRunRequest as _AgentRunRequest, RunRecipe
from brain.systems.runs.engine import AsyncAgentRunEngine, RunRecipeResult, RunRuntime, StaticAnswerRecipe
from brain.systems.runs.status import (
    ALLOWED_RUN_TRANSITIONS,
    RunStatus,
    RunTransitionError,
    ensure_run_transition,
)
from brain.systems.runs.store import AsyncAgentRunStore
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow


def _run_request(**kwargs) -> _AgentRunRequest:
    return _AgentRunRequest(org_id=kwargs.pop("org_id", "org-1"), **kwargs)


@pytest.fixture
async def session_factory() -> AsyncIterator[Callable[[], AsyncSession]]:
    pytest.importorskip("aiosqlite")
    _patch_sqlite_for_agent_run_tables()
    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as connection:
        for table in [
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
        ]:
            await connection.execute(CreateTable(table, if_not_exists=True))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _file_session_factory(database):
    _patch_sqlite_for_agent_run_tables()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 5},
    )
    async with engine.begin() as connection:
        for table in [
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
        ]:
            await connection.execute(CreateTable(table, if_not_exists=True))
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


class _SessionUoW:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            await self.session.flush()
        return False


@pytest.mark.parametrize(
    "from_status",
    (RunStatus.STARTING, RunStatus.RUNNING, RunStatus.VERIFYING),
)
def test_interrupted_requeue_transition_is_declared_but_scoped(from_status: RunStatus):
    assert RunStatus.QUEUED in ALLOWED_RUN_TRANSITIONS[from_status]
    assert ensure_run_transition(
        from_status,
        RunStatus.QUEUED,
        allow_interrupted_requeue=True,
    ) == (from_status, RunStatus.QUEUED)
    with pytest.raises(RunTransitionError, match="outside interrupted requeue"):
        ensure_run_transition(from_status, RunStatus.QUEUED)


async def test_concurrent_event_appends_are_sequential_and_leave_session_usable(session_factory):
    from brain.systems.runs.events import run_event

    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_run_request(thread_id="thread-event-race", message="run"))
    baseline = int(
        await session.scalar(
            select(func.max(AgentRunEventRow.sequence_no)).where(
                AgentRunEventRow.run_id == run.id
            )
        )
        or 0
    )
    active_writers = 0
    max_active_writers = 0
    original_acquire = store._acquire_agent_run_locks

    async def observed_acquire(*args, **kwargs):
        nonlocal active_writers, max_active_writers
        active_writers += 1
        max_active_writers = max(max_active_writers, active_writers)
        try:
            await asyncio.sleep(0)
            return await original_acquire(*args, **kwargs)
        finally:
            active_writers -= 1

    store._acquire_agent_run_locks = observed_acquire
    first, second = await asyncio.gather(
        store.append_event(run_event(run.id, "run.concurrent_first")),
        store.append_event(run_event(run.id, "run.concurrent_second")),
    )

    assert max_active_writers == 1
    assert sorted((first.sequence_no, second.sequence_no)) == [baseline + 1, baseline + 2]

    third = await store.append_event(run_event(run.id, "run.after_concurrent_append"))
    assert third.sequence_no == baseline + 3
    assert await session.scalar(select(func.count()).select_from(AgentRunEventRow)) >= 4


async def test_inline_child_is_atomically_owned_before_parent_executes_it(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session)
    root = await store.create_run(_run_request(thread_id="thread-1", message="root"))
    await store.set_status(root.id, RunStatus.STARTING)
    await store.set_status(root.id, RunStatus.RUNNING)

    inline = await store.create_child_run(
        root,
        recipe=RunRecipe.WORKER,
        message="inline child",
        step_key="node:inline",
        initial_status=RunStatus.STARTING,
    )
    repeated = await store.create_child_run(
        root,
        recipe=RunRecipe.WORKER,
        message="inline child",
        step_key="node:inline",
        initial_status=RunStatus.STARTING,
    )
    queued = await store.create_child_run(
        root,
        recipe=RunRecipe.WORKER,
        message="queued child",
        step_key="spawn_worker:queued",
    )
    await session.commit()

    competing_session = session_factory()
    competing_store = AsyncAgentRunStore(competing_session)
    assert repeated.id == inline.id
    assert inline.status == RunStatus.STARTING
    assert inline.started_at is not None
    assert await competing_store.claim_run(inline.id) is None
    claimed = await competing_store.claim_next()
    assert claimed is not None
    assert claimed.id == queued.id

    completed = await AsyncAgentRunEngine(
        competing_session,
        recipes={RunRecipe.WORKER.value: StaticAnswerRecipe("done")},
    ).run_existing(inline.id)
    assert completed.status == RunStatus.COMPLETED


async def test_child_does_not_inherit_root_source_idempotency(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session)
    source_metadata = {
        "idempotency_key": "slack:C123:1712345678.000100",
        "work_intake": {"source": "slack"},
    }
    root = await store.create_run(
        _run_request(
            thread_id="thread-slack",
            message="root",
            metadata=source_metadata,
        )
    )
    child = await store.create_child_run(
        root,
        recipe=RunRecipe.WORKER,
        message="child",
        step_key="node:slack-child",
        metadata=source_metadata,
        initial_status=RunStatus.STARTING,
    )

    assert child.id != root.id
    assert child.parent_run_id == root.id


async def test_new_run_persists_a_deadline_from_the_runtime_limit(
    session_factory,
    monkeypatch,
):
    monkeypatch.setenv("AGENT_RUN_DEADLINE_SECONDS", "120")
    session = session_factory()
    before = datetime.now(timezone.utc)

    run = await AsyncAgentRunStore(session).create_run(
        _run_request(thread_id="thread-deadline", message="bounded work")
    )

    assert run.deadline_at is not None
    assert before + timedelta(seconds=119) <= run.deadline_at
    assert run.deadline_at <= datetime.now(timezone.utc) + timedelta(seconds=120)


async def test_child_run_cannot_outlive_its_parent_deadline(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=3)
    root = await store.create_run(
        _run_request(
            thread_id="thread-parent-deadline",
            message="root",
            deadline_at=deadline,
        )
    )

    child = await store.create_child_run(
        root,
        recipe=RunRecipe.WORKER,
        message="child",
        step_key="node:bounded-child",
    )

    assert child.deadline_at == deadline


async def test_deadline_sweep_requests_one_graceful_closeout_before_expiring(
    session_factory,
):
    from brain.systems.runs.deadlines import sweep_agent_run_deadlines
    from brain.systems.runs.events import run_event

    session = session_factory()
    store = AsyncAgentRunStore(session)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    run = await store.create_run(
        _run_request(
            thread_id="thread-closeout",
            message="finish safely",
            deadline_at=now - timedelta(seconds=1),
        )
    )
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    tool_event = await store.append_event(
        run_event(
            run.id,
            "run.tool_completed",
            {"tool": "write_file", "side_effect": "write", "is_write": True},
        )
    )
    tool_event.created_at = now - timedelta(minutes=5)
    await session.flush()

    first = await sweep_agent_run_deadlines(
        session,
        now=now,
        grace_seconds=90,
    )
    second = await sweep_agent_run_deadlines(
        session,
        now=now + timedelta(seconds=30),
        grace_seconds=90,
    )

    row = await session.get(AgentRunRow, run.id)
    assert first.closeout_requested == 1
    assert first.expired == 0
    assert second.closeout_requested == 0
    assert second.expired == 0
    assert row is not None
    assert row.status == RunStatus.RUNNING.value
    assert row.closeout_expires_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=90)
    closeout_events = (
        await session.scalars(
            select(AgentRunEventRow).where(
                AgentRunEventRow.run_id == run.id,
                AgentRunEventRow.event_type == "run.deadline_closeout_requested",
            )
        )
    ).all()
    steering_events = (
        await session.scalars(
            select(AgentRunEventRow).where(
                AgentRunEventRow.run_id == run.id,
                AgentRunEventRow.event_type == "run.steering_submitted",
            )
        )
    ).all()
    assert len(closeout_events) == 1
    assert len(steering_events) == 1
    assert closeout_events[0].payload["seconds_since_last_state_change"] == 300
    assert "last state-changing tool call completed 5 minutes ago" in steering_events[0].payload["content"]
    assert "close-out window ends in 90 seconds" in steering_events[0].payload["content"]


async def test_deadline_sweep_expires_after_grace_and_preserves_partial_state(
    session_factory,
):
    from brain.systems.runs.deadlines import sweep_agent_run_deadlines
    from brain.systems.runs.events import run_event

    session = session_factory()
    store = AsyncAgentRunStore(session)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    run = await store.create_run(
        _run_request(
            thread_id="thread-expire",
            message="bounded work",
            deadline_at=now - timedelta(minutes=2),
            metadata={"partial_result": {"saved": True}},
        )
    )
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    row = await session.get(AgentRunRow, run.id)
    assert row is not None
    row.closeout_expires_at = now - timedelta(seconds=1)
    for index in range(3):
        read_event = await store.append_event(
            run_event(
                run.id,
                "run.tool_completed",
                {"tool": "read_file", "side_effect": "read_only", "is_write": False},
            )
        )
        read_event.created_at = now - timedelta(seconds=30 - index)
    await session.flush()

    with patch(
        "brain.systems.runs.chantier_continuation.queue_chantier_continuation_for_terminal_run",
        return_value=None,
    ):
        result = await sweep_agent_run_deadlines(session, now=now)

    row = await session.get(AgentRunRow, run.id)
    final_answers = (
        await session.scalars(
            select(AgentRunArtifactRow).where(
                AgentRunArtifactRow.run_id == run.id,
                AgentRunArtifactRow.artifact_type == "final_answer",
            )
        )
    ).all()
    assert result.expired == 1
    assert result.expired_run_ids == (run.id,)
    assert row is not None
    assert row.status == RunStatus.EXPIRED.value
    assert row.expired_at.replace(tzinfo=timezone.utc) == now
    assert row.metadata_["partial_result"] == {"saved": True}
    assert len(final_answers) == 1
    assert "timed out" in str(final_answers[0].text)
    assert (await _event_types(session, run.id)).count("run.expired") == 1


async def test_interruption_requeue_cap_expires_instead_of_looping_forever(
    session_factory,
    monkeypatch,
):
    monkeypatch.setenv("AGENT_RUN_MAX_INTERRUPTION_REQUEUES", "2")
    session = session_factory()
    store = AsyncAgentRunStore(session, auto_commit=True)
    run = await store.create_run(
        _run_request(
            thread_id="thread-interruption-cap",
            message="bounded restart",
            metadata={
                "interruption_count": 2,
                "partial_result": {"saved": True},
            },
        )
    )
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    running = await store.require_run(run.id)
    assert running.status == RunStatus.RUNNING.value
    assert running.metadata_["interruption_count"] == 2

    with patch(
        "brain.systems.runs.chantier_continuation.queue_chantier_continuation_for_terminal_run",
        return_value=None,
    ):
        expired, changed = await store.interrupt_and_requeue(run.id)

    row = await store.require_run(run.id)
    assert changed is True
    assert expired.status == RunStatus.EXPIRED
    assert row.status == RunStatus.EXPIRED.value
    assert row.metadata_["interruption_count"] == 3
    assert row.metadata_["partial_result"] == {"saved": True}
    assert row.metadata_["interruption"]["requeued"] is False
    assert "run.interruption_limit_exhausted" in await _event_types(session, run.id)
    assert "timed out" in await store.latest_artifact_text(run.id)


async def test_child_creation_and_parent_event_are_atomic_and_repairable(tmp_path, monkeypatch):
    database_engine, factory = await _file_session_factory(tmp_path / "child-event-atomicity.sqlite3")
    try:
        async with factory() as setup_session:
            store = AsyncAgentRunStore(setup_session, auto_commit=True)
            root = await store.create_run(
                _run_request(thread_id="thread-child-event", message="root")
            )
            root_id = root.id

            original_append = store._append_child_created_once

            async def fail_parent_event(*_args, **_kwargs):
                raise RuntimeError("parent event failed")

            monkeypatch.setattr(store, "_append_child_created_once", fail_parent_event)
            with pytest.raises(RuntimeError, match="parent event failed"):
                await store.create_child_run(
                    root,
                    recipe=RunRecipe.WORKER,
                    message="child",
                    step_key="node:atomic-child",
                    initial_status=RunStatus.STARTING,
                )

            monkeypatch.setattr(store, "_append_child_created_once", original_append)

        async with factory() as creation_session:
            creation_store = AsyncAgentRunStore(creation_session, auto_commit=True)
            root_row = await creation_store.require_run(root_id)
            child, created = await creation_store.create_child_run_with_result(
                root_row,
                recipe=RunRecipe.WORKER,
                message="child",
                step_key="node:atomic-child",
                initial_status=RunStatus.STARTING,
            )
            assert created is True
            child_id = child.id

        async with factory() as repair_session:
            await repair_session.execute(
                AgentRunEventRow.__table__.delete().where(
                    AgentRunEventRow.run_id == root_id,
                    AgentRunEventRow.event_type == "run.child_created",
                )
            )
            await repair_session.commit()
            repair_store = AsyncAgentRunStore(repair_session, auto_commit=True)
            root_row = await repair_store.require_run(root_id)
            replayed, created = await repair_store.create_child_run_with_result(
                root_row,
                recipe=RunRecipe.WORKER,
                message="child",
                step_key="node:atomic-child",
                initial_status=RunStatus.STARTING,
            )
            assert replayed.id == child_id
            assert created is False

        async with factory() as inspection_session:
            children = (
                await inspection_session.scalars(
                    select(AgentRunRow).where(AgentRunRow.parent_run_id == root_id)
                )
            ).all()
            assert [row.id for row in children] == [child_id]
            child_events = (
                await inspection_session.scalars(
                    select(AgentRunEventRow).where(
                        AgentRunEventRow.run_id == root_id,
                        AgentRunEventRow.event_type == "run.child_created",
                    )
                )
            ).all()
            assert len(child_events) == 1
            assert child_events[0].payload["child_run_id"] == child_id
    finally:
        await database_engine.dispose()


async def test_concurrent_inline_child_creation_and_execution_is_exactly_once(tmp_path):
    _patch_sqlite_for_agent_run_tables()
    database = tmp_path / "concurrent-agent-runs.sqlite3"
    database_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 5},
    )
    async with database_engine.begin() as connection:
        for table in [
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
        ]:
            await connection.execute(CreateTable(table, if_not_exists=True))
    factory = async_sessionmaker(bind=database_engine, expire_on_commit=False)
    try:
        async with factory() as setup_session:
            setup_store = AsyncAgentRunStore(setup_session)
            root = await setup_store.create_run(
                _run_request(thread_id="thread-concurrent", message="root")
            )
            await setup_store.set_status(root.id, RunStatus.STARTING)
            await setup_store.set_status(root.id, RunStatus.RUNNING)
            await setup_session.commit()

        async def create_inline_child() -> int:
            async with factory() as session:
                child = await AsyncAgentRunStore(session).create_child_run(
                    root,
                    recipe=RunRecipe.WORKER,
                    message="same logical child",
                    step_key="node:concurrent",
                    initial_status=RunStatus.STARTING,
                )
                return child.id

        child_ids = await asyncio.gather(create_inline_child(), create_inline_child())
        assert child_ids[0] == child_ids[1]

        async with factory() as inspection_session:
            children = (
                await inspection_session.scalars(
                    select(AgentRunRow).where(AgentRunRow.parent_run_id == root.id)
                )
            ).all()
            assert [child.id for child in children] == [child_ids[0]]
            assert children[0].parent_step_key_hash is not None

        recipe_calls = 0

        class SideEffectRecipe:
            async def execute(self, _runtime: RunRuntime) -> RunRecipeResult:
                nonlocal recipe_calls
                recipe_calls += 1
                await asyncio.sleep(0.05)
                return RunRecipeResult(output="done")

        first_session = factory()
        second_session = factory()
        await first_session.get(AgentRunRow, child_ids[0])
        await second_session.get(AgentRunRow, child_ids[0])
        first, second = await asyncio.gather(
            AsyncAgentRunEngine(
                first_session,
                recipes={RunRecipe.WORKER.value: SideEffectRecipe()},
            ).run_existing(child_ids[0]),
            AsyncAgentRunEngine(
                second_session,
                recipes={RunRecipe.WORKER.value: SideEffectRecipe()},
            ).run_existing(child_ids[0]),
        )
        await first_session.close()
        await second_session.close()
        assert first.status == RunStatus.COMPLETED
        assert second.status == RunStatus.COMPLETED
        assert recipe_calls == 1

        async with factory() as setup_session:
            paused_child = await AsyncAgentRunStore(setup_session).create_child_run(
                root,
                recipe=RunRecipe.WORKER,
                message="pause once",
                step_key="node:paused",
                initial_status=RunStatus.STARTING,
            )

        pause_calls = 0

        class PauseRecipe:
            async def execute(self, _runtime: RunRuntime) -> RunRecipeResult:
                nonlocal pause_calls
                pause_calls += 1
                await asyncio.sleep(0.05)
                return RunRecipeResult(output="needs input", status=RunStatus.PAUSED)

        first_pause_session = factory()
        second_pause_session = factory()
        await first_pause_session.get(AgentRunRow, paused_child.id)
        await second_pause_session.get(AgentRunRow, paused_child.id)
        paused_first, paused_second = await asyncio.gather(
            AsyncAgentRunEngine(
                first_pause_session,
                recipes={RunRecipe.WORKER.value: PauseRecipe()},
            ).run_existing(paused_child.id),
            AsyncAgentRunEngine(
                second_pause_session,
                recipes={RunRecipe.WORKER.value: PauseRecipe()},
            ).run_existing(paused_child.id),
        )
        await first_pause_session.close()
        await second_pause_session.close()
        assert paused_first.status == RunStatus.PAUSED
        assert paused_second.status == RunStatus.PAUSED
        assert pause_calls == 1
    finally:
        await database_engine.dispose()


async def test_execution_claim_prevents_canceled_run_from_owner_completion(tmp_path):
    database_engine, factory = await _file_session_factory(tmp_path / "cancel-fence.sqlite3")
    recipe_started = asyncio.Event()
    finish_recipe = asyncio.Event()
    calls = 0

    class BlockedRecipe:
        async def execute(self, _runtime: RunRuntime) -> RunRecipeResult:
            nonlocal calls
            calls += 1
            recipe_started.set()
            await finish_recipe.wait()
            return RunRecipeResult(output="must not publish")

    try:
        async with factory() as setup_session:
            store = AsyncAgentRunStore(setup_session)
            run = await store.create_run(
                _run_request(thread_id="thread-cancel-fence", message="race")
            )
            await store.set_status(run.id, RunStatus.STARTING)
            await setup_session.commit()
            run_id = run.id

        owner_session = factory()
        owner_task = asyncio.create_task(
            AsyncAgentRunEngine(
                owner_session,
                recipes={RunRecipe.FAST.value: BlockedRecipe()},
            ).run_existing(run_id)
        )
        await asyncio.wait_for(recipe_started.wait(), timeout=1)

        async with factory() as cancel_session:
            canceled = await AsyncAgentRunStore(cancel_session).set_status(
                run_id,
                RunStatus.CANCELED,
                reason="user_canceled",
            )
            await cancel_session.commit()
            assert canceled.status == RunStatus.CANCELED

        finish_recipe.set()
        result = await asyncio.wait_for(owner_task, timeout=2)
        await owner_session.close()

        assert result.status == RunStatus.CANCELED
        assert calls == 1
        async with factory() as inspection_session:
            row = await inspection_session.get(AgentRunRow, run_id)
            assert row is not None
            assert row.status == RunStatus.CANCELED.value
            assert row.execution_token is None
            artifacts = (
                await inspection_session.scalars(
                    select(AgentRunArtifactRow).where(AgentRunArtifactRow.run_id == run_id)
                )
            ).all()
            assert not [artifact for artifact in artifacts if artifact.text == "must not publish"]
            assert "run.completed" not in await _event_types(inspection_session, run_id)
    finally:
        await database_engine.dispose()


async def test_stale_cancel_cannot_overwrite_or_emit_after_completed_execution(tmp_path):
    from brain.app.api.routers.cortex._run import _cancel_run_with_event

    database_engine, factory = await _file_session_factory(tmp_path / "stale-cancel.sqlite3")
    recipe_started = asyncio.Event()
    finish_recipe = asyncio.Event()

    class BlockedRecipe:
        async def execute(self, _runtime: RunRuntime) -> RunRecipeResult:
            recipe_started.set()
            await finish_recipe.wait()
            return RunRecipeResult(output="done")

    try:
        async with factory() as setup_session:
            store = AsyncAgentRunStore(setup_session)
            run = await store.create_run(
                _run_request(thread_id="thread-stale-cancel", message="race")
            )
            await store.set_status(run.id, RunStatus.STARTING)
            await setup_session.commit()
            run_id = run.id

        owner_session = factory()
        owner_task = asyncio.create_task(
            AsyncAgentRunEngine(
                owner_session,
                recipes={RunRecipe.FAST.value: BlockedRecipe()},
            ).run_existing(run_id)
        )
        await asyncio.wait_for(recipe_started.wait(), timeout=1)

        stale_cancel_session = factory()
        stale_row = await stale_cancel_session.get(AgentRunRow, run_id)
        assert stale_row is not None and stale_row.status == RunStatus.RUNNING.value

        finish_recipe.set()
        completed = await asyncio.wait_for(owner_task, timeout=2)
        await owner_session.close()
        assert completed.status == RunStatus.COMPLETED

        await _cancel_run_with_event(
            AsyncAgentRunStore(stale_cancel_session),
            run_id,
            reason="late_cancel",
        )
        await stale_cancel_session.commit()
        await stale_cancel_session.close()

        async with factory() as inspection_session:
            row = await inspection_session.get(AgentRunRow, run_id)
            assert row is not None and row.status == RunStatus.COMPLETED.value
            assert "run.canceled" not in await _event_types(inspection_session, run_id)
    finally:
        await database_engine.dispose()


async def test_auto_commit_terminal_output_failure_rolls_back_terminal_status(tmp_path, monkeypatch):
    database_engine, factory = await _file_session_factory(tmp_path / "terminal-atomicity.sqlite3")
    try:
        async with factory() as setup_session:
            store = AsyncAgentRunStore(setup_session)
            run = await store.create_run(
                _run_request(thread_id="thread-terminal-atomicity", message="finish atomically")
            )
            await store.set_status(run.id, RunStatus.STARTING)
            await setup_session.commit()
            run_id = run.id

        owner_session = factory()
        engine = AsyncAgentRunEngine(
            owner_session,
            recipes={RunRecipe.FAST.value: StaticAnswerRecipe("final output")},
            auto_commit_events=True,
        )

        async def fail_final_answer(*_args, **_kwargs):
            raise RuntimeError("artifact write failed")

        monkeypatch.setattr(engine.store, "append_final_answer_once", fail_final_answer)
        with pytest.raises(RuntimeError, match="artifact write failed"):
            await engine.run_existing(run_id)
        await owner_session.close()

        async with factory() as inspection_session:
            row = await inspection_session.get(AgentRunRow, run_id)
            assert row is not None
            assert row.status == RunStatus.RUNNING.value
            assert row.execution_token is None
            assert "run.completed" not in await _event_types(inspection_session, run_id)
            artifacts = (
                await inspection_session.scalars(
                    select(AgentRunArtifactRow).where(AgentRunArtifactRow.run_id == run_id)
                )
            ).all()
            assert not [artifact for artifact in artifacts if artifact.artifact_type == "final_answer"]
    finally:
        await database_engine.dispose()


async def test_auto_commit_claim_event_failure_rolls_back_claim(tmp_path, monkeypatch):
    database_engine, factory = await _file_session_factory(tmp_path / "claim-atomicity.sqlite3")
    try:
        async with factory() as setup_session:
            store = AsyncAgentRunStore(setup_session)
            run = await store.create_run(
                _run_request(thread_id="thread-claim-atomicity", message="start atomically")
            )
            await store.set_status(run.id, RunStatus.STARTING)
            await setup_session.commit()
            run_id = run.id

        owner_session = factory()
        engine = AsyncAgentRunEngine(
            owner_session,
            recipes={RunRecipe.FAST.value: StaticAnswerRecipe("must not run")},
            auto_commit_events=True,
        )
        original_append = engine.store.append_event

        async def fail_started_event(event):
            if event.event_type == "run.started":
                raise RuntimeError("started event failed")
            return await original_append(event)

        monkeypatch.setattr(engine.store, "append_event", fail_started_event)
        with pytest.raises(RuntimeError, match="started event failed"):
            await engine.run_existing(run_id)
        await owner_session.close()

        async with factory() as inspection_session:
            row = await inspection_session.get(AgentRunRow, run_id)
            assert row is not None
            assert row.status == RunStatus.STARTING.value
            assert row.execution_token is None
            assert row.execution_attempt == 0
            assert "run.started" not in await _event_types(inspection_session, run_id)
    finally:
        await database_engine.dispose()


async def test_active_execution_claim_is_not_stolen_from_live_owner(tmp_path):
    database_engine, factory = await _file_session_factory(tmp_path / "active-claim.sqlite3")
    try:
        async with factory() as setup_session:
            store = AsyncAgentRunStore(setup_session)
            run = await store.create_run(
                _run_request(thread_id="thread-expired-claim", message="recover")
            )
            await store.set_status(run.id, RunStatus.STARTING)
            await store.set_status(run.id, RunStatus.RUNNING)
            row = await store.require_run(run.id)
            row.execution_token = "live-owner"
            row.execution_attempt = 7
            await setup_session.commit()
            run_id = run.id

        async with factory() as contender_session:
            contender_store = AsyncAgentRunStore(contender_session)
            row = await contender_store.refresh_run(run_id)
            claim = await contender_store._try_acquire_execution_claim(
                row,
                token="contender",
            )
            assert claim is None

        async with factory() as inspection_session:
            row = await inspection_session.get(AgentRunRow, run_id)
            assert row is not None
            assert row.status == RunStatus.RUNNING.value
            assert row.execution_attempt == 7
            assert row.execution_token == "live-owner"
    finally:
        await database_engine.dispose()


async def test_stale_status_cas_preserves_live_owner_and_prior_batch_changes(tmp_path):
    database_engine, factory = await _file_session_factory(tmp_path / "stale-status-cas.sqlite3")
    old = datetime.now(timezone.utc) - timedelta(seconds=300)
    now = datetime.now(timezone.utc)
    try:
        async with factory() as setup_session:
            store = AsyncAgentRunStore(setup_session)
            runs = []
            for index in range(2):
                run = await store.create_run(
                    _run_request(thread_id=f"thread-stale-cas-{index}", message="stale")
                )
                await store.set_status(run.id, RunStatus.STARTING)
                await store.set_status(run.id, RunStatus.RUNNING)
                row = await store.require_run(run.id)
                row.execution_token = f"owner-{index}"
                row.execution_attempt = index + 1
                row.updated_at = old
                runs.append(run.id)
            await setup_session.commit()

        stale_session = factory()
        stale_store = AsyncAgentRunStore(stale_session)
        stale_rows = [await stale_store.refresh_run(run_id) for run_id in runs]
        expected = [
            (row.updated_at, row.execution_token, int(row.execution_attempt or 0))
            for row in stale_rows
        ]

        async with factory() as live_session:
            live_store = AsyncAgentRunStore(live_session)
            assert await live_store.heartbeat_run(runs[1], now=now, reason="owner_is_alive")
            await live_session.commit()

        _first, first_changed = await stale_store.set_status_with_result(
            runs[0],
            RunStatus.FAILED,
            reason="runner_heartbeat_stale",
            expected_updated_at=expected[0][0],
            expected_execution_token=expected[0][1],
            expected_execution_attempt=expected[0][2],
            rollback_on_conflict=False,
        )
        second, second_changed = await stale_store.set_status_with_result(
            runs[1],
            RunStatus.FAILED,
            reason="runner_heartbeat_stale",
            expected_updated_at=expected[1][0],
            expected_execution_token=expected[1][1],
            expected_execution_attempt=expected[1][2],
            rollback_on_conflict=False,
        )
        assert first_changed is True
        assert second_changed is False
        assert second.status == RunStatus.RUNNING
        await stale_session.commit()
        await stale_session.close()

        async with factory() as inspection_session:
            first_row = await inspection_session.get(AgentRunRow, runs[0])
            second_row = await inspection_session.get(AgentRunRow, runs[1])
            assert first_row is not None and first_row.status == RunStatus.FAILED.value
            assert first_row.execution_token is None
            assert second_row is not None and second_row.status == RunStatus.RUNNING.value
            assert second_row.execution_token == "owner-1"
            assert second_row.metadata_["runner_heartbeat"]["reason"] == "owner_is_alive"
    finally:
        await database_engine.dispose()


async def test_canceled_run_is_not_entered_by_recipe(session_factory):
    calls = 0

    class UnexpectedRecipe:
        async def execute(self, _runtime: RunRuntime) -> RunRecipeResult:
            nonlocal calls
            calls += 1
            return RunRecipeResult(output="unexpected")

    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(
        _run_request(thread_id="thread-canceled-before-entry", message="do not run")
    )
    await store.set_status(run.id, RunStatus.CANCELED)
    await session.commit()

    result = await AsyncAgentRunEngine(
        session,
        recipes={RunRecipe.FAST.value: UnexpectedRecipe()},
    ).run_existing(run.id)

    assert result.status == RunStatus.CANCELED
    assert calls == 0


@pytest.mark.requires_db
async def test_postgres_execution_token_allows_one_recipe_attempt(db_engine):
    import uuid

    from brain.platform.db.models.org import Org

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    org_id = str(uuid.uuid4())
    slug = f"lease-{uuid.uuid4().hex[:12]}"
    root_id = None
    child_id = None
    try:
        async with factory() as setup_session:
            setup_session.add(Org(id=org_id, name="Lease Test", slug=slug))
            await setup_session.flush()
            setup_store = AsyncAgentRunStore(setup_session)
            root = await setup_store.create_run(
                _run_request(
                    org_id=org_id,
                    thread_id=f"thread-{uuid.uuid4().hex}",
                    message="root",
                )
            )
            root_id = root.id
            await setup_store.set_status(root.id, RunStatus.STARTING)
            await setup_store.set_status(root.id, RunStatus.RUNNING)
            child = await setup_store.create_child_run(
                root,
                recipe=RunRecipe.WORKER,
                message="child",
                step_key="node:postgres-exactly-once",
                initial_status=RunStatus.STARTING,
            )
            child_id = child.id

        calls = 0

        class Recipe:
            async def execute(self, _runtime: RunRuntime) -> RunRecipeResult:
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.05)
                return RunRecipeResult(output="done")

        first_session = factory()
        second_session = factory()
        await first_session.get(AgentRunRow, child_id)
        await second_session.get(AgentRunRow, child_id)
        first, second = await asyncio.gather(
            AsyncAgentRunEngine(
                first_session,
                recipes={RunRecipe.WORKER.value: Recipe()},
            ).run_existing(child_id),
            AsyncAgentRunEngine(
                second_session,
                recipes={RunRecipe.WORKER.value: Recipe()},
            ).run_existing(child_id),
        )
        await first_session.close()
        await second_session.close()

        assert first.status == RunStatus.COMPLETED
        assert second.status == RunStatus.COMPLETED
        assert calls == 1
    finally:
        async with factory() as cleanup_session:
            if root_id is not None:
                run_ids = [value for value in (root_id, child_id) if value is not None]
                await cleanup_session.execute(
                    AgentRunArtifactRow.__table__.delete().where(
                        AgentRunArtifactRow.run_id.in_(run_ids)
                    )
                )
                await cleanup_session.execute(
                    AgentRunEventRow.__table__.delete().where(
                        AgentRunEventRow.run_id.in_(run_ids)
                    )
                )
                await cleanup_session.execute(
                    AgentRunRow.__table__.delete().where(AgentRunRow.id.in_(run_ids))
                )
            await cleanup_session.execute(Org.__table__.delete().where(Org.id == org_id))
            await cleanup_session.commit()


@pytest.mark.requires_db
async def test_postgres_event_boundary_releases_parent_for_isolated_child_uow(db_engine):
    import uuid

    from brain.platform.db.models.org import Org
    from brain.systems.runs.events import run_event

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    org_id = str(uuid.uuid4())
    root_id = None
    child_id = None
    try:
        async with factory() as setup_session:
            setup_session.add(
                Org(
                    id=org_id,
                    name="Tool Boundary Test",
                    slug=f"tool-boundary-{uuid.uuid4().hex[:12]}",
                )
            )
            await setup_session.flush()
            root = await AsyncAgentRunStore(setup_session).create_run(
                _run_request(
                    org_id=org_id,
                    thread_id=f"thread-{uuid.uuid4().hex}",
                    message="root",
                )
            )
            root_id = root.id
            await setup_session.commit()

        async with factory() as parent_session, factory() as child_session:
            parent_store = AsyncAgentRunStore(parent_session)
            await parent_store.append_event(
                run_event(root_id, "run.tool_started", {"tool_name": "spawn_worker"})
            )
            await parent_store.commit_event_boundary(root_id)

            child_store = AsyncAgentRunStore(child_session)
            parent = await child_store.require_run(root_id)
            child = await asyncio.wait_for(
                child_store.create_child_run(
                    parent,
                    recipe=RunRecipe.WORKER,
                    message="child",
                    step_key="spawn_worker:tool-boundary",
                ),
                timeout=1,
            )
            child_id = child.id
            await child_session.commit()

        async with factory() as inspection_session:
            stored_child = await inspection_session.get(AgentRunRow, child_id)
            assert stored_child is not None
            assert stored_child.parent_run_id == root_id
    finally:
        async with factory() as cleanup_session:
            run_ids = [value for value in (root_id, child_id) if value is not None]
            if run_ids:
                await cleanup_session.execute(
                    AgentRunArtifactRow.__table__.delete().where(
                        AgentRunArtifactRow.run_id.in_(run_ids)
                    )
                )
                await cleanup_session.execute(
                    AgentRunEventRow.__table__.delete().where(
                        AgentRunEventRow.run_id.in_(run_ids)
                    )
                )
                await cleanup_session.execute(
                    AgentRunRow.__table__.delete().where(AgentRunRow.id.in_(run_ids))
                )
            await cleanup_session.execute(Org.__table__.delete().where(Org.id == org_id))
            await cleanup_session.commit()


@pytest.mark.requires_db
async def test_postgres_waiting_event_session_does_not_block_advisory_lock_owner(db_engine):
    """A DB advisory-lock waiter must not own another session's Python lock."""

    import uuid

    from brain.platform.db.models.org import Org
    from brain.systems.runs.events import run_event

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    org_id = str(uuid.uuid4())
    run_id = None
    try:
        async with factory() as setup_session:
            setup_session.add(
                Org(
                    id=org_id,
                    name="Event Session Lock Test",
                    slug=f"event-session-lock-{uuid.uuid4().hex[:12]}",
                )
            )
            await setup_session.flush()
            run = await AsyncAgentRunStore(setup_session).create_run(
                _run_request(
                    org_id=org_id,
                    thread_id=f"thread-{uuid.uuid4().hex}",
                    message="event session lock",
                )
            )
            run_id = run.id
            await setup_session.commit()

        async with factory() as owner_session, factory() as waiter_session:
            owner_store = AsyncAgentRunStore(owner_session)
            waiter_store = AsyncAgentRunStore(waiter_session)
            first = await owner_store.append_event(
                run_event(run_id, "run.owner_first")
            )

            waiter_entered_advisory_lock = asyncio.Event()
            original_waiter_lock = waiter_store.lock_event_stream

            async def observed_waiter_lock(locked_run_id: int):
                waiter_entered_advisory_lock.set()
                await original_waiter_lock(locked_run_id)

            waiter_store.lock_event_stream = observed_waiter_lock
            waiter_task = asyncio.create_task(
                waiter_store.append_event(run_event(run_id, "run.waiter"))
            )
            try:
                await asyncio.wait_for(waiter_entered_advisory_lock.wait(), timeout=1)
                await asyncio.sleep(0.05)
                assert not waiter_task.done()

                # The owner retains PostgreSQL's transaction-scoped advisory
                # lock. It must still be able to take its own session lock and
                # append; a process-wide run lock deadlocks at this point.
                second = await asyncio.wait_for(
                    owner_store.append_event(run_event(run_id, "run.owner_second")),
                    timeout=1,
                )
                await owner_session.commit()
                waited = await asyncio.wait_for(waiter_task, timeout=1)
                await waiter_session.commit()
            finally:
                waiter_task.cancel()
                await asyncio.gather(waiter_task, return_exceptions=True)
                await owner_session.rollback()
                await waiter_session.rollback()

            assert second.sequence_no == first.sequence_no + 1
            assert waited.sequence_no == second.sequence_no + 1
    finally:
        async with factory() as cleanup_session:
            if run_id is not None:
                await cleanup_session.execute(
                    AgentRunArtifactRow.__table__.delete().where(
                        AgentRunArtifactRow.run_id == run_id
                    )
                )
                await cleanup_session.execute(
                    AgentRunEventRow.__table__.delete().where(
                        AgentRunEventRow.run_id == run_id
                    )
                )
                await cleanup_session.execute(
                    AgentRunRow.__table__.delete().where(AgentRunRow.id == run_id)
                )
            await cleanup_session.execute(Org.__table__.delete().where(Org.id == org_id))
            await cleanup_session.commit()


@pytest.mark.requires_db
async def test_postgres_child_event_key_share_does_not_block_parent_mutation(db_engine):
    import uuid

    from brain.platform.db.models.org import Org
    from brain.systems.runs.events import run_event

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    org_id = str(uuid.uuid4())
    root_id = None
    child_id = None
    try:
        async with factory() as setup_session:
            setup_session.add(
                Org(
                    id=org_id,
                    name="Child Event Lock Test",
                    slug=f"child-event-lock-{uuid.uuid4().hex[:12]}",
                )
            )
            await setup_session.flush()
            store = AsyncAgentRunStore(setup_session)
            root = await store.create_run(
                _run_request(
                    org_id=org_id,
                    thread_id=f"thread-{uuid.uuid4().hex}",
                    message="root",
                )
            )
            root_id = root.id
            child = await store.create_child_run(
                root,
                recipe=RunRecipe.WORKER,
                message="child",
                step_key="spawn_worker:child-event-lock",
            )
            child_id = child.id
            await setup_session.commit()

        async with factory() as child_session, factory() as parent_session:
            await AsyncAgentRunStore(child_session).append_event(
                run_event(
                    child_id,
                    "run.activity",
                    {"label": "Preparing project context"},
                    root_run_id=root_id,
                )
            )

            locked_parent = await asyncio.wait_for(
                AsyncAgentRunStore(parent_session)._locked_run(
                    root_id,
                    root_run_id=root_id,
                ),
                timeout=1,
            )
            assert locked_parent.id == root_id
            await parent_session.rollback()
            await child_session.rollback()
    finally:
        async with factory() as cleanup_session:
            run_ids = [value for value in (root_id, child_id) if value is not None]
            if run_ids:
                await cleanup_session.execute(
                    AgentRunArtifactRow.__table__.delete().where(
                        AgentRunArtifactRow.run_id.in_(run_ids)
                    )
                )
                await cleanup_session.execute(
                    AgentRunEventRow.__table__.delete().where(
                        AgentRunEventRow.run_id.in_(run_ids)
                    )
                )
                await cleanup_session.execute(
                    AgentRunRow.__table__.delete().where(AgentRunRow.id.in_(run_ids))
                )
            await cleanup_session.execute(Org.__table__.delete().where(Org.id == org_id))
            await cleanup_session.commit()


@pytest.mark.requires_db
async def test_postgres_terminal_child_releases_then_locks_parent_first(db_engine):
    """A terminal child must not hold child while waiting on its parent."""

    import uuid

    from brain.platform.db.models.org import Org

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    org_id = str(uuid.uuid4())
    root_id = None
    child_id = None
    try:
        async with factory() as setup_session:
            setup_session.add(
                Org(
                    id=org_id,
                    name="Terminal Lock Order Test",
                    slug=f"terminal-lock-order-{uuid.uuid4().hex[:12]}",
                )
            )
            await setup_session.flush()
            store = AsyncAgentRunStore(setup_session)
            root = await store.create_run(
                _run_request(
                    org_id=org_id,
                    thread_id=f"thread-{uuid.uuid4().hex}",
                    message="root",
                )
            )
            root_id = root.id
            await store.set_status(root_id, RunStatus.STARTING)
            await store.set_status(root_id, RunStatus.RUNNING)
            child = await store.create_child_run(
                root,
                recipe=RunRecipe.WORKER,
                message="child",
                step_key="spawn_worker:terminal-lock-order",
                metadata={"spawned_by_tool": True},
            )
            child_id = child.id
            await store.set_status(child_id, RunStatus.STARTING)
            await store.set_status(child_id, RunStatus.RUNNING)
            await setup_session.commit()

        async with factory() as child_session, factory() as parent_session:
            child_store = AsyncAgentRunStore(child_session)
            parent_store = AsyncAgentRunStore(parent_session)

            # Mirror the runner transaction immediately before completion: it
            # has already mutated the child and therefore holds the child row.
            await child_store._locked_run(child_id, root_run_id=root_id)

            parent_locked = asyncio.Event()

            async def mutate_parent_then_child():
                await parent_store._locked_run(root_id, root_run_id=root_id)
                parent_locked.set()
                await parent_store._locked_run(child_id, root_run_id=root_id)
                await parent_session.commit()

            parent_task = asyncio.create_task(mutate_parent_then_child())
            await asyncio.wait_for(parent_locked.wait(), timeout=1)

            # Completion must publish the prior child transaction first. That
            # lets the parent transaction finish before completion reacquires
            # both rows as parent -> child and invokes the continuation hook.
            child_task = asyncio.create_task(
                AsyncAgentRunEngine(child_session, recipes={}).complete(
                    child_id,
                    output="done",
                )
            )
            try:
                completed = await asyncio.wait_for(asyncio.shield(child_task), timeout=3)
                await child_session.commit()
                await asyncio.wait_for(parent_task, timeout=1)
                assert completed.status == RunStatus.COMPLETED
            finally:
                child_task.cancel()
                parent_task.cancel()
                await child_session.rollback()
                await parent_session.rollback()
                await asyncio.gather(child_task, parent_task, return_exceptions=True)

        async with factory() as inspection_session:
            stored_child = await inspection_session.get(AgentRunRow, child_id)
            assert stored_child is not None
            assert stored_child.status == RunStatus.COMPLETED.value
    finally:
        async with factory() as cleanup_session:
            run_ids = [value for value in (root_id, child_id) if value is not None]
            if run_ids:
                await cleanup_session.execute(
                    AgentRunArtifactRow.__table__.delete().where(
                        AgentRunArtifactRow.run_id.in_(run_ids)
                    )
                )
                await cleanup_session.execute(
                    AgentRunEventRow.__table__.delete().where(
                        AgentRunEventRow.run_id.in_(run_ids)
                    )
                )
                await cleanup_session.execute(
                    AgentRunRow.__table__.delete().where(AgentRunRow.id.in_(run_ids))
                )
            await cleanup_session.execute(Org.__table__.delete().where(Org.id == org_id))
            await cleanup_session.commit()


async def test_runtime_fails_legacy_run_without_workspace_org_id(session_factory):
    class UnexpectedRecipe:
        async def execute(self, _runtime: RunRuntime) -> RunRecipeResult:
            raise AssertionError("runs without workspace context must not execute")

    session = session_factory()
    row = AgentRunRow(
        thread_id="thread-1",
        profile=RunRecipe.FAST.value,
        recipe=RunRecipe.FAST.value,
        status=RunStatus.QUEUED.value,
        input_message="legacy queued run",
        target_ref={},
        workspace_ref={},
        model_policy={},
        metadata_={},
    )
    session.add(row)
    await session.flush()
    row.root_run_id = row.id
    await session.flush()

    result = await AsyncAgentRunEngine(session, recipes={"fast": UnexpectedRecipe()}).run_existing(row.id)

    assert result.status == RunStatus.FAILED
    row = await session.get(AgentRunRow, result.id)
    assert row is not None
    assert row.status == RunStatus.FAILED.value
    status_events = (
        await session.scalars(
            select(AgentRunEventRow)
            .where(
                AgentRunEventRow.run_id == result.id,
                AgentRunEventRow.event_type == "run.status_changed",
            )
            .order_by(AgentRunEventRow.sequence_no.asc())
        )
    ).all()
    assert status_events[-1].payload["reason"] == "AgentRun missing workspace org_id"


async def test_claim_and_completion_are_idempotent(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session)
    first = await store.create_run(_run_request(thread_id="thread-1", message="one"))
    second = await store.create_run(_run_request(thread_id="thread-1", message="two"))

    claimed = await store.claim_next()
    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status == RunStatus.STARTING
    assert await store.claim_run(first.id) is None
    assert (await store.claim_next()).id == second.id

    engine = AsyncAgentRunEngine(session, recipes={"fast": StaticAnswerRecipe("done")})
    completed = await engine.run_existing(first.id)
    assert completed.status == RunStatus.COMPLETED
    await engine.complete(first.id, output="done again")

    events = await _event_types(session, first.id)
    assert events.count("run.completed") == 1


async def test_cycle_final_answer_store_never_persists_raw_provider_error(session_factory):
    from brain.systems.runs.artifacts import final_answer_artifact

    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(
        _run_request(
            thread_id="thread-1",
            message="scheduled mission",
            metadata={
                "source": "cycle",
                "cycle_run_id": 12,
                "contract": {"result": {"kind": "autonomous_cycle_run_result"}},
            },
        )
    )
    raw_provider_error = (
        "An error occurred while processing your request. Contact help.openai.com with request ID "
        "req-store-guard. | server_error | server_error"
    )

    artifact = await store.append_final_answer_once(run.id, raw_provider_error)
    direct_artifact = await store.append_artifact(final_answer_artifact(run.id, raw_provider_error))

    assert artifact is not None
    assert artifact.text == "upstream_provider_error: server_error"
    assert "help.openai.com" not in artifact.text
    assert direct_artifact.text == "upstream_provider_error: server_error"
    assert "help.openai.com" not in direct_artifact.text


async def test_runner_setup_failure_never_persists_diagnostic_as_public_output(
    monkeypatch,
    session_factory,
):
    from brain.systems.runs.cortex import runner
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE

    raw_diagnostic = "peer closed connection while cloning token=super-secret"
    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_run_request(thread_id="thread-1", message="materialize"))
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)

    async def settle(_session, run_id):
        return {"run_id": run_id}

    monkeypatch.setattr(runner, "_unit_of_work_factory", lambda: lambda: _SessionUoW(session))
    monkeypatch.setattr(runner, "_settle_terminal_root_run_async", settle)

    result = await runner._mark_run_failed_after_runner_error_async(
        run.id,
        raw_diagnostic,
        final_answer=raw_diagnostic,
    )

    row = await session.get(AgentRunRow, run.id)
    artifacts = list(
        (
            await session.scalars(
                select(AgentRunArtifactRow).where(AgentRunArtifactRow.run_id == run.id)
            )
        ).all()
    )
    text_events = list(
        (
            await session.scalars(
                select(AgentRunEventRow).where(
                    AgentRunEventRow.run_id == run.id,
                    AgentRunEventRow.event_type == "run.text_completed",
                )
            )
        ).all()
    )

    assert result == {"run_id": run.id}
    assert row is not None
    assert row.status == RunStatus.FAILED.value
    assert row.metadata_["failure"] == {"category": "upstream"}
    assert [artifact.text for artifact in artifacts] == [UPSTREAM_FAILED_RUN_MESSAGE]
    assert [event.payload for event in text_events] == [{"text": UPSTREAM_FAILED_RUN_MESSAGE}]
    assert all(raw_diagnostic not in str(value) for value in (artifacts, text_events))


async def test_engine_preserves_original_flush_exception_for_uow_owner(
    session_factory,
):
    from brain.systems.runs.execution_failure import RunExecutionFailure

    session = session_factory()
    armed = {"value": False}
    raised = {"value": False}

    def fail_first_armed_flush(_session, _flush_context, _instances):
        if armed["value"] and not raised["value"]:
            raised["value"] = True
            raise RuntimeError("original flush exploded")

    event.listen(session.sync_session, "before_flush", fail_first_armed_flush)

    class FlushFailureRecipe:
        async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
            armed["value"] = True
            await runtime.activity("Trigger the failing flush")
            raise AssertionError("the injected flush must fail")

    try:
        with pytest.raises(RunExecutionFailure) as captured:
            await AsyncAgentRunEngine(
                session,
                recipes={"fast": FlushFailureRecipe()},
            ).run(_run_request(thread_id="thread-1", message="flush failure"))
    finally:
        event.remove(session.sync_session, "before_flush", fail_first_armed_flush)

    failure = captured.value

    assert isinstance(failure.original, RuntimeError)
    assert str(failure.original) == "original flush exploded"
    assert str(failure) == (
        "run_execution_failed: RuntimeError: original flush exploded"
    )
    assert "PendingRollbackError" not in str(failure)


def test_run_execution_failure_survives_a_cleanup_exception():
    from brain.systems.runs.execution_failure import RunExecutionFailure

    primary = RunExecutionFailure(42, RuntimeError("primary exploded"))
    try:
        raise primary
    except RunExecutionFailure:
        try:
            raise RuntimeError("cleanup exploded")
        except RuntimeError as cleanup:
            captured = RunExecutionFailure.capture(42, cleanup)

    assert captured is primary


async def test_runner_commit_failure_rolls_back_then_settles_in_fresh_uow(
    monkeypatch,
    session_factory,
):
    from contextlib import asynccontextmanager

    from brain.systems.runs.cortex import runner

    setup_session = session_factory()
    store = AsyncAgentRunStore(setup_session)
    run = await store.create_run(
        _run_request(thread_id="thread-commit-failure", message="commit failure")
    )
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    await setup_session.commit()
    await setup_session.close()

    opened_uows = 0

    class CommitAwareUoW:
        def __init__(self):
            nonlocal opened_uows
            self.index = opened_uows
            opened_uows += 1
            self.session = session_factory()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, _exc, _tb):
            try:
                if exc_type is not None:
                    await self.session.rollback()
                elif self.index == 0:
                    await self.session.rollback()
                    raise RuntimeError("commit exploded")
                else:
                    await self.session.commit()
            finally:
                await self.session.close()

    class CompletingEngine:
        def __init__(self, session):
            self.session = session

        async def run_existing(self, run_id):
            row = await self.session.get(AgentRunRow, int(run_id))
            assert row is not None
            row.status = RunStatus.COMPLETED.value
            return SimpleNamespace(status=RunStatus.COMPLETED)

    @asynccontextmanager
    async def heartbeat(_run_id):
        yield

    async def materialize(_run_id):
        return True, None

    async def no_settlement(_session, _run_id):
        return None

    async def no_cycle_finalization(*_args, **_kwargs):
        return None

    async def no_contract_gate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner, "_unit_of_work_factory", lambda: CommitAwareUoW)
    monkeypatch.setattr(runner, "_engine_for_session", lambda session: CompletingEngine(session))
    monkeypatch.setattr(runner, "_run_heartbeat_async", heartbeat)
    monkeypatch.setattr(runner, "_async_materialize_project_context", materialize)
    monkeypatch.setattr(runner, "_settle_terminal_root_run_async", no_settlement)
    monkeypatch.setattr(runner, "_finalize_cycle_run_if_needed_async", no_cycle_finalization)
    monkeypatch.setattr(
        "brain.systems.cycles.contract_gate.async_prepare_cycle_run_visible_finalization",
        no_contract_gate,
    )

    processed = await runner._process_claimed_run_async(run.id)

    inspection_session = session_factory()
    row = await inspection_session.get(AgentRunRow, run.id)
    failed_event = (
        await inspection_session.scalars(
            select(AgentRunEventRow).where(
                AgentRunEventRow.run_id == run.id,
                AgentRunEventRow.event_type == "run.failed",
            )
        )
    ).one()
    await inspection_session.close()

    assert processed is False
    assert opened_uows == 2
    assert row is not None
    assert row.status == RunStatus.FAILED.value
    assert failed_event.payload["error"] == (
        "run_execution_failed: RuntimeError: commit exploded"
    )


async def test_oversized_system_admission_error_reaches_run_failed_event(
    monkeypatch,
    caplog,
    session_factory,
):
    from brain.systems.runs import direct_agent

    class FakeProvider:
        def __init__(self):
            self.requests = []

        def is_api_error(self, exc):
            return False

        def is_retryable_error(self, exc):
            return False

        def create(self, request):
            self.requests.append(request)
            raise AssertionError("admission failure must happen before provider sampling")

    provider = FakeProvider()
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", "4096")
    monkeypatch.setenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", "500")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_REASONING_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_TOOL_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", "0")
    monkeypatch.setattr(direct_agent, "get_provider", lambda _provider_name, _client: provider)
    resolved_llm = SimpleNamespace(
        provider="openai",
        client=object(),
        source="test",
        auth_mode="api_key",
        is_oauth=False,
        token_prefix="test-token",
        build_request_headers=lambda **_kwargs: {},
    )

    class OversizedSystemRecipe:
        async def execute(self, _runtime: RunRuntime) -> RunRecipeResult:
            agent_result = await direct_agent.run_agent_async(
                "start",
                system_prompt="oversized " + ("x" * 4000),
                session_id="oversized-event",
                model="openai/gpt-5.5",
                thinking="low",
                tools=[{"name": "read_file"}, {"name": "search_files"}],
                tool_handlers={},
                max_turns=20,
                persist_session=False,
                cache_system_prompt=False,
                resolved_llm=resolved_llm,
            )
            return RunRecipeResult(
                error=agent_result.error,
                status=RunStatus.FAILED,
            )

    session = session_factory()
    with caplog.at_level("ERROR", logger="agent"):
        result = await AsyncAgentRunEngine(
            session,
            recipes={"fast": OversizedSystemRecipe()},
        ).run(_run_request(thread_id="thread-1", message="oversized system"))

    failed_event = (
        await session.scalars(
            select(AgentRunEventRow).where(
                AgentRunEventRow.run_id == result.id,
                AgentRunEventRow.event_type == "run.failed",
            )
        )
    ).one()
    error = failed_event.payload["error"]

    assert result.status == RunStatus.FAILED
    assert error.startswith("context_floor_exceeds_budget:")
    assert "floor=" in error
    assert "ceiling=500" in error
    assert "tools=2" in error
    assert error in caplog.text
    assert provider.requests == []


async def test_interactive_slack_transport_failure_never_persists_raw_error_as_final_answer(
    monkeypatch,
    session_factory,
):
    from brain.systems.runs.failures import UPSTREAM_FAILED_RUN_MESSAGE
    from brain.systems.runs.recipes.fast import FastRecipe

    raw_error = (
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )

    async def fake_invoke(_spec):
        return SimpleNamespace(output="", success=False, error=raw_error)

    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_agent_tools", lambda _role: [])
    monkeypatch.setattr("brain.systems.runs.recipes.fast.build_tool_handlers", lambda **_kwargs: {})
    monkeypatch.setattr("brain.systems.runs.recipes.fast.invoke_direct_agent_async", fake_invoke)

    session = session_factory()
    result = await AsyncAgentRunEngine(
        session,
        recipes={"fast": FastRecipe()},
    ).run(
        _run_request(
            thread_id="slack:T789:C456:1716900000.000100",
            message="Continue the design conversation",
            target_ref={"kind": "slack_message", "originating_surface": "slack"},
            model_policy={"model": "openai/gpt-5.4", "thinking": "high"},
            metadata={"origin": "slack_teammate", "originating_surface": "slack"},
        )
    )

    final_answers = list(
        (
            await session.scalars(
                select(AgentRunArtifactRow).where(
                    AgentRunArtifactRow.run_id == result.id,
                    AgentRunArtifactRow.artifact_type == "final_answer",
                )
            )
        ).all()
    )

    assert result.status == RunStatus.FAILED
    assert result.metadata["failure"] == {"category": "upstream"}
    assert [artifact.text for artifact in final_answers] == [UPSTREAM_FAILED_RUN_MESSAGE]
    assert all(raw_error not in str(artifact.text) for artifact in final_answers)

    events = list(
        (
            await session.scalars(
                select(AgentRunEventRow).where(AgentRunEventRow.run_id == result.id)
            )
        ).all()
    )
    failed_events = [event for event in events if event.event_type == "run.failed"]
    text_completed_events = [event for event in events if event.event_type == "run.text_completed"]

    assert failed_events[-1].payload["error"] == raw_error
    assert failed_events[-1].payload["failure_category"] == "upstream"
    assert text_completed_events[-1].payload["text"] == UPSTREAM_FAILED_RUN_MESSAGE
    assert all(raw_error not in str(event.payload) for event in text_completed_events)


async def test_failed_cycle_engine_events_do_not_surface_raw_provider_error(session_factory):
    raw_provider_error = (
        "An error occurred while processing your request. Contact help.openai.com with request ID "
        "req-engine-guard. | server_error | server_error"
    )

    class ProviderFailureRecipe:
        async def execute(self, _runtime: RunRuntime) -> RunRecipeResult:
            return RunRecipeResult(error=raw_provider_error, status=RunStatus.FAILED)

    session = session_factory()
    result = await AsyncAgentRunEngine(
        session,
        recipes={"fast": ProviderFailureRecipe()},
    ).run(
        _run_request(
            thread_id="thread-1",
            message="scheduled mission",
            metadata={
                "source": "cycle",
                "cycle_run_id": 12,
                "contract": {"result": {"kind": "autonomous_cycle_run_result"}},
            },
        )
    )

    artifacts = list(
        (
            await session.scalars(
                select(AgentRunArtifactRow).where(AgentRunArtifactRow.run_id == result.id)
            )
        ).all()
    )
    events = list(
        (
            await session.scalars(
                select(AgentRunEventRow).where(AgentRunEventRow.run_id == result.id)
            )
        ).all()
    )

    assert result.status == RunStatus.FAILED
    assert not [artifact for artifact in artifacts if artifact.artifact_type == "final_answer"]
    assert all("help.openai.com" not in str(event.payload) for event in events)


async def test_post_completion_tasks_run_after_terminal_completion(session_factory):
    session = session_factory()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    event_types_at_start: list[str] = []
    statuses_at_start: list[str | None] = []

    class PostCompletionRecipe:
        async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
            async def after_completion() -> None:
                event_types_at_start.extend(await _event_types(session, runtime.run.id))
                row = await session.get(AgentRunRow, runtime.run.id)
                statuses_at_start.append(row.status if row is not None else None)
                started.set()
                await release.wait()
                finished.set()

            return RunRecipeResult(output="done", post_completion_tasks=(after_completion,))

    run = await asyncio.wait_for(
        AsyncAgentRunEngine(session, recipes={"fast": PostCompletionRecipe()}).run(
            _run_request(thread_id="thread-1", message="hello")
        ),
        timeout=1,
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)

    assert run.status == RunStatus.COMPLETED
    assert "run.completed" in event_types_at_start
    assert statuses_at_start == [RunStatus.COMPLETED.value]


async def test_deferred_queued_run_waits_for_target_terminal(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session)
    active = await store.create_run(_run_request(thread_id="thread-1", message="active"))
    await store.set_status(active.id, RunStatus.STARTING)
    await store.set_status(active.id, RunStatus.RUNNING)
    deferred = await store.create_run(
        _run_request(
            thread_id="thread-1",
            message="queued next",
            metadata={"queued_after_run_id": active.id},
        )
    )

    assert await store.claim_next() is None

    await store.set_status(active.id, RunStatus.COMPLETED)

    claimed = await store.claim_next()
    assert claimed is not None
    assert claimed.id == deferred.id
    assert claimed.status == RunStatus.STARTING


async def test_deferred_queued_runs_stay_sequential_after_target_finishes(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session)
    active = await store.create_run(_run_request(thread_id="thread-1", message="active"))
    await store.set_status(active.id, RunStatus.STARTING)
    await store.set_status(active.id, RunStatus.RUNNING)
    first_deferred = await store.create_run(
        _run_request(
            thread_id="thread-1",
            message="queued first",
            metadata={"queued_after_run_id": active.id},
        )
    )
    second_deferred = await store.create_run(
        _run_request(
            thread_id="thread-1",
            message="queued second",
            metadata={"queued_after_run_id": active.id},
        )
    )

    await store.set_status(active.id, RunStatus.COMPLETED)

    first_claimed = await store.claim_next()
    assert first_claimed is not None
    assert first_claimed.id == first_deferred.id
    assert await store.claim_next() is None

    await store.set_status(first_deferred.id, RunStatus.RUNNING)
    await store.set_status(first_deferred.id, RunStatus.COMPLETED)

    second_claimed = await store.claim_next()
    assert second_claimed is not None
    assert second_claimed.id == second_deferred.id


async def test_blocked_deferred_run_does_not_starve_other_threads(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session)
    active = await store.create_run(_run_request(thread_id="thread-1", message="active"))
    await store.set_status(active.id, RunStatus.STARTING)
    await store.set_status(active.id, RunStatus.RUNNING)
    for index in range(30):
        await store.create_run(
            _run_request(
                thread_id="thread-1",
                message=f"queued next {index}",
                metadata={"queued_after_run_id": active.id},
            )
        )
    eligible = await store.create_run(_run_request(thread_id="thread-2", message="normal queued"))

    claimed = await store.claim_next()
    assert claimed is not None
    assert claimed.id == eligible.id


async def test_durable_steering_drains_once_from_run_events(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_run_request(thread_id="thread-1", message="listen"))
    event = await store.append_steering(
        run.id,
        "  Don't fetch everything.  ",
        user_id="user-1",
        thread_message_id=7,
    )

    first = await store.drain_steering(run.id)
    second = await store.drain_steering(run.id)

    assert [message.content for message in first] == ["Don't fetch everything."]
    assert first[0].user_id == "user-1"
    assert second == []
    row = await session.get(AgentRunRow, run.id)
    assert row is not None
    assert row.metadata_["steering_cursor_sequence_no"] == event.sequence_no
    event_payload = (await session.get(AgentRunEventRow, event.id)).payload
    assert event_payload["thread_message_id"] == 7


async def test_durable_steering_no_rows_autocommit_releases_transaction(session_factory, monkeypatch):
    session = session_factory()
    store = AsyncAgentRunStore(session, auto_commit=True)
    run = await store.create_run(_run_request(thread_id="thread-1", message="listen"))

    def fail_locked_run(_run_id):
        raise AssertionError("draining an empty steering inbox should not lock the run")

    monkeypatch.setattr(store, "_locked_run", fail_locked_run)

    assert await store.drain_steering(run.id) == []
    assert not session.in_transaction()


async def test_durable_steering_with_rows_autocommit_releases_transaction(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session, auto_commit=True)
    run = await store.create_run(_run_request(thread_id="thread-1", message="listen"))
    event = await store.append_steering(run.id, "keep going", user_id="user-1")

    messages = await store.drain_steering(run.id)

    assert [message.content for message in messages] == ["keep going"]
    assert messages[0].user_id == "user-1"
    assert not session.in_transaction()
    row = await session.get(AgentRunRow, run.id)
    assert row is not None
    assert row.metadata_["steering_cursor_sequence_no"] == event.sequence_no


async def test_run_heartbeat_updates_liveness_without_event_noise(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_run_request(thread_id="thread-1", message="heartbeat"))
    await store.set_status(run.id, RunStatus.STARTING)
    before_events = await _event_types(session, run.id)
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)

    assert await store.heartbeat_run(run.id, token="runner-1", reason="runner_running", now=now)

    row = await session.get(AgentRunRow, run.id)
    assert row is not None
    assert row.metadata_["runner_heartbeat"]["token"] == "runner-1"
    assert row.metadata_["runner_heartbeat"]["reason"] == "runner_running"
    assert row.metadata_["runner_heartbeat"]["at"] == now.isoformat()
    assert await _event_types(session, run.id) == before_events


async def test_runtime_cancel_token_stops_run_after_recipe_returns(session_factory):
    class SlowRecipe:
        async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
            assert runtime.cancel_event is not None
            return RunRecipeResult(output="should not complete")

    session = session_factory()
    result = await AsyncAgentRunEngine(
        session,
        recipes={"fast": SlowRecipe()},
        cancel_event_factory=lambda _run_id: SimpleNamespace(is_set=lambda: True),
    ).run(_run_request(thread_id="thread-1", message="cancel me"))

    assert result.status == RunStatus.CANCELED
    events = await _event_types(session, result.id)
    assert "run.canceled" in events
    assert "run.completed" not in events


async def test_auto_commit_store_finishes_event_transactions(session_factory):
    session = session_factory()
    store = AsyncAgentRunStore(session, auto_commit=True)

    with patch.object(session, "commit", wraps=session.commit) as commit:
        run = await store.create_run(_run_request(thread_id="thread-1", message="live"))
        await store.set_status(run.id, RunStatus.STARTING)

    assert commit.call_count >= 2


@pytest.mark.asyncio
async def test_cancel_endpoint_helper_records_run_canceled_event(session_factory):
    from brain.app.api.routers.cortex._run import _cancel_run_with_event

    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_run_request(thread_id="thread-1", message="cancel me"))

    class _AsyncStore:
        async def require_run(self, run_id: int):
            return await store.require_run(run_id)

        async def append_event(self, event):
            return await store.append_event(event)

        async def set_status(self, run_id: int, status, *, reason: str | None = None):
            return await store.set_status(run_id, status, reason=reason)

    await _cancel_run_with_event(_AsyncStore(), run.id, reason="user_canceled")

    row = await session.get(AgentRunRow, run.id)
    assert row is not None
    assert row.status == RunStatus.CANCELED.value
    events = await _event_types(session, run.id)
    assert "run.canceled" in events
    assert events.count("run.canceled") == 1


async def test_cancel_runs_for_idea_records_run_canceled_event(session_factory):
    from brain.systems.runs.cortex import async_cancel_runs_for_idea

    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_run_request(thread_id="idea-1", message="cancel me"))
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)

    with patch("brain.systems.runs.cortex.UnitOfWork", return_value=_SessionUoW(session)):
        count = await async_cancel_runs_for_idea("idea-1")

    row = await session.get(AgentRunRow, run.id)
    assert count == 1
    assert row is not None
    assert row.status == RunStatus.CANCELED.value
    events = await _event_types(session, run.id)
    assert "run.canceled" in events
    assert events.count("run.canceled") == 1


async def test_stale_active_run_reaper_interrupts_requeues_and_retries_abandoned_runs(session_factory):
    from brain.systems.runs.cortex import runner

    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_run_request(thread_id="idea-1", message="stale"))
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(seconds=300)
    row = await session.get(AgentRunRow, run.id)
    assert row is not None
    row.created_at = old
    row.started_at = old
    row.paused_at = old
    row.updated_at = old
    row.execution_token = "abandoned-owner"
    row.execution_attempt = 3
    row.metadata_ = {
        "failure": {"category": "runner_failed"},
        "runner_heartbeat": {"at": old.isoformat(), "reason": "runner_running"},
    }
    for event in (await session.scalars(select(AgentRunEventRow).where(AgentRunEventRow.run_id == run.id))).all():
        event.created_at = old
    await session.commit()

    with (
        patch("brain.systems.runs.cortex.runner.UnitOfWork", return_value=_SessionUoW(session)),
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch("brain.systems.runs.cortex.runner._settle_idea_for_terminal_root_run_async", return_value=None),
        patch("brain.systems.runs.cortex.runner.notify_run_interruption", return_value=None),
    ):
        count = await runner.reap_stale_active_runs(now=now, stale_after_seconds=120)

    row = await session.get(AgentRunRow, run.id)
    assert count == 1
    assert row is not None
    assert row.status == RunStatus.QUEUED.value
    assert row.execution_token is None
    assert row.paused_at is None
    assert "failure" not in row.metadata_
    candidate_events = (await session.scalars(
        select(AgentRunEventRow).where(
            AgentRunEventRow.run_id == run.id,
            AgentRunEventRow.event_type.in_(
                ("run.interrupted", "run.status_changed", "run.requeued", "run.failed")
            ),
        )
        .order_by(AgentRunEventRow.sequence_no.asc())
    )).all()
    interruption_events = [
        event
        for event in candidate_events
        if event.event_type != "run.status_changed"
        or event.payload.get("to_status") == RunStatus.QUEUED.value
    ]
    assert [event.event_type for event in interruption_events] == [
        "run.interrupted",
        "run.status_changed",
        "run.requeued",
    ]
    assert interruption_events[0].payload["reason"] == "runner_heartbeat_stale"
    assert interruption_events[0].payload["requeued"] is True
    assert interruption_events[1].payload == {
        "from_status": RunStatus.RUNNING.value,
        "to_status": RunStatus.QUEUED.value,
        "reason": "runner_heartbeat_stale",
    }
    assert row.metadata_["interruption"]["from_status"] == RunStatus.RUNNING.value

    retried = await AsyncAgentRunStore(session).claim_next()
    assert retried is not None
    assert retried.id == run.id
    assert retried.status == RunStatus.STARTING


async def test_scheduler_reaps_abandoned_run_without_a_worker(session_factory, monkeypatch, capsys):
    from brain.app.cli import scheduler
    from brain.systems.runs.cortex import runner

    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_run_request(thread_id="idea-1", message="scheduler recovery"))
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    old = datetime.now(timezone.utc) - timedelta(seconds=600)
    row = await session.get(AgentRunRow, run.id)
    assert row is not None
    row.created_at = old
    row.started_at = old
    row.updated_at = old
    row.execution_token = "dead-worker"
    row.metadata_ = {"runner_heartbeat": {"at": old.isoformat(), "reason": "runner_running"}}
    for event in (await session.scalars(select(AgentRunEventRow).where(AgentRunEventRow.run_id == run.id))).all():
        event.created_at = old
    await session.commit()

    monkeypatch.setattr(scheduler, "_monotonic", lambda: 100.0)
    with (
        patch("brain.systems.runs.cortex.runner.UnitOfWork", return_value=_SessionUoW(session)),
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch("brain.systems.runs.cortex.runner._settle_idea_for_terminal_root_run_async", return_value=None),
        patch("brain.systems.runs.cortex.runner.notify_run_interruption", return_value=None),
    ):
        next_reap_at = await scheduler._reap_stale_active_runs_if_due(next_reap_at=0.0)

    row = await session.get(AgentRunRow, run.id)
    assert next_reap_at == 160.0
    assert row is not None
    assert row.status == RunStatus.QUEUED.value
    event = json.loads(capsys.readouterr().out)
    assert event == {"event": "agent_run_stale_reap", "ok": True, "reaped": 1}


async def test_stale_active_run_reaper_uses_recent_events_as_liveness(session_factory):
    from brain.systems.runs.events import run_event
    from brain.systems.runs.cortex import runner

    session = session_factory()
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_run_request(thread_id="idea-1", message="active events"))
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(seconds=300)
    recent = now - timedelta(seconds=30)
    row = await session.get(AgentRunRow, run.id)
    assert row is not None
    row.created_at = old
    row.started_at = old
    row.updated_at = old
    row.metadata_ = {"runner_heartbeat": {"at": old.isoformat(), "reason": "runner_running"}}
    for event in (await session.scalars(select(AgentRunEventRow).where(AgentRunEventRow.run_id == run.id))).all():
        event.created_at = old
    recent_event = await store.append_event(run_event(run.id, "run.tool_completed", {"tool": "read_file"}))
    recent_event.created_at = recent
    await session.commit()

    with (
        patch("brain.systems.runs.cortex.runner.UnitOfWork", return_value=_SessionUoW(session)),
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch("brain.systems.runs.cortex.runner._settle_idea_for_terminal_root_run_async", return_value=None),
    ):
        count = await runner.reap_stale_active_runs(now=now, stale_after_seconds=120)

    row = await session.get(AgentRunRow, run.id)
    assert count == 0
    assert row is not None
    assert row.status == RunStatus.RUNNING.value


async def test_stale_active_run_reaper_does_not_reap_child_while_root_active(session_factory):
    from brain.systems.runs.cortex import runner

    session = session_factory()
    store = AsyncAgentRunStore(session)
    root = await store.create_run(_run_request(thread_id="idea-1", message="root"))
    child = await store.create_child_run(root, recipe=RunRecipe.WORKER, message="child", step_key="node:investigate")
    await store.set_status(root.id, RunStatus.STARTING)
    await store.set_status(root.id, RunStatus.RUNNING)
    await store.set_status(child.id, RunStatus.STARTING)
    await store.set_status(child.id, RunStatus.RUNNING)
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(seconds=300)
    root_row = await session.get(AgentRunRow, root.id)
    child_row = await session.get(AgentRunRow, child.id)
    assert root_row is not None
    assert child_row is not None
    root_row.updated_at = now
    child_row.created_at = old
    child_row.started_at = old
    child_row.updated_at = old
    child_row.metadata_ = {"runner_heartbeat": {"at": old.isoformat(), "reason": "runner_started"}}
    for event in (await session.scalars(select(AgentRunEventRow).where(AgentRunEventRow.run_id == child.id))).all():
        event.created_at = old
    await session.commit()

    with (
        patch("brain.systems.runs.cortex.runner.UnitOfWork", return_value=_SessionUoW(session)),
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch("brain.systems.runs.cortex.runner._settle_idea_for_terminal_root_run_async", return_value=None),
    ):
        count = await runner.reap_stale_active_runs(now=now, stale_after_seconds=120)

    child_row = await session.get(AgentRunRow, child.id)
    assert count == 0
    assert child_row is not None
    assert child_row.status == RunStatus.RUNNING.value


async def _event_types(session: AsyncSession, run_id: int) -> list[str]:
    return list(
        await session.scalars(
            select(AgentRunEventRow.event_type)
            .where(AgentRunEventRow.run_id == int(run_id))
            .order_by(AgentRunEventRow.sequence_no.asc())
        )
    )


def _patch_sqlite_for_agent_run_tables() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    original = SQLiteDDLCompiler.get_column_default_string

    if getattr(original, "_agent_run_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._agent_run_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched
