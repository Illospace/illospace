from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow
from brain.systems.runs.domain import AgentRunRequest, RunRecipe
from brain.systems.runs.engine import AsyncAgentRunEngine
from brain.systems.runs.events import run_event
from brain.systems.runs.failure_diagnostic import RunFailureStage, read_run_failure_diagnostic
from brain.systems.runs.failures import RunFailureCategory
from brain.systems.runs.recipes.fast import FastRecipe
from brain.systems.runs.recipes.workers import WorkerRecipe
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore
from tests.inbound_preservation_support import (
    _assert_queued_submission,
    _seed_connection,
    session,
)


pytestmark = pytest.mark.asyncio


def _engine(session, monkeypatch, recipe, invoke):
    module = f"brain.systems.runs.recipes.{'workers' if recipe == 'worker' else 'fast'}"
    monkeypatch.setattr(f"{module}.build_agent_tools", lambda _role: [])
    monkeypatch.setattr(f"{module}.build_tool_handlers", lambda **_kwargs: {})
    monkeypatch.setattr(f"{module}.invoke_direct_agent_async", invoke)
    if recipe == "worker":
        async def no_usage(*_args, **_kwargs):
            return None
        monkeypatch.setattr(f"{module}.async_summarize_run_usage_in_savepoint", no_usage)
    return AsyncAgentRunEngine(
        session, recipes={recipe: WorkerRecipe() if recipe == "worker" else FastRecipe()}
    )


def _request(recipe="fast", **metadata):
    return AgentRunRequest(
        org_id="org-1",
        thread_id="thread-connect-error",
        message="Preserve this result.",
        recipe=RunRecipe(recipe),
        model_policy={"model": "openai/gpt-5.6-sol", "thinking": "high"},
        metadata={
            "submission": {"preservation": {"requires_durable_evidence": True}},
            **metadata,
        },
    )


async def _event_types(session, run_id):
    return list(await session.scalars(
        select(AgentRunEventRow.event_type).where(AgentRunEventRow.run_id == run_id)
    ))


@pytest.mark.parametrize("recipe", ["fast", "worker"])
async def test_agent_start_connect_error_requeues_and_really_runs_again(
    session, monkeypatch, recipe,
):
    attempts = 0

    async def invoke(_spec):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection unavailable")
        return SimpleNamespace(success=True, output="done")

    engine = _engine(session, monkeypatch, recipe, invoke)
    queued = await engine.run(_request(recipe))
    assert queued.status == RunStatus.QUEUED
    await session.commit()

    # Observe the committed queue in another session, then use the real claimer.
    async with AsyncSession(bind=session.bind, expire_on_commit=False) as retry_session:
        store = AsyncAgentRunStore(retry_session)
        row = await store.require_run(queued.id)
        diagnostic = await read_run_failure_diagnostic(retry_session, run=row)
        assert diagnostic.as_payload() == {
            "stage": "agent_execution",
            "stage_state": "known",
            "exception_class": "ConnectError",
            "exception_class_state": "known",
            "tool_execution_started": False,
            "terminal": False,
            "retry_scheduled": True,
        }
        assert row.execution_token is None
        events = await _event_types(retry_session, queued.id)
        assert events.count("run.requeued") == 1
        assert "run.failed" not in events
        assert "run.text_completed" not in events
        assert not list(await retry_session.scalars(
            select(AgentRunArtifactRow).where(
                AgentRunArtifactRow.run_id == queued.id,
                AgentRunArtifactRow.artifact_type.in_(("worker_result", "final_answer")),
            )
        ))
        claimed = await store.claim_next()
        assert claimed.id == queued.id
        assert await read_run_failure_diagnostic(retry_session, run=row) is None
        completed = await _engine(retry_session, monkeypatch, recipe, invoke).run_existing(claimed.id)
        assert completed.status == RunStatus.COMPLETED
        assert completed.id == queued.id
        assert attempts == 2
        assert (await store.require_run(queued.id)).execution_attempt == 2
        assert await read_run_failure_diagnostic(retry_session, run=row) is None


@pytest.mark.parametrize("recipe", ["fast", "worker"])
@pytest.mark.parametrize("event_type", ["run.tool_started", "run.tool_completed", "run.tool_failed"])
async def test_connect_error_after_tool_execution_does_not_requeue(
    session, monkeypatch, recipe, event_type,
):
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_request(recipe))

    async def invoke(_spec):
        await store.append_event(run_event(run.id, event_type, {"tool_name": "memory_ingest_source"}))
        raise httpx.ConnectError("connection unavailable after a tool")

    failed = await _engine(session, monkeypatch, recipe, invoke).run_existing(run.id)
    assert failed.status == RunStatus.FAILED
    diagnostic = await read_run_failure_diagnostic(session, run=await store.require_run(run.id))
    assert diagnostic.tool_execution_started is True
    assert diagnostic.retry_scheduled is False
    assert diagnostic.terminal is True
    assert "run.requeued" not in await _event_types(session, run.id)


@pytest.mark.parametrize("recipe", ["fast", "worker"])
@pytest.mark.parametrize("kind", ["redacted", "unknown", "other"])
async def test_unidentified_or_other_exception_does_not_requeue(
    session, monkeypatch, recipe, kind,
):
    class ConnectError(Exception):
        pass

    async def invoke(_spec):
        if kind == "unknown":
            return SimpleNamespace(success=False, output="", error="ConnectError")
        if kind == "redacted":
            raise ConnectError("local exception type")
        raise RuntimeError("different failure")

    engine = _engine(session, monkeypatch, recipe, invoke)
    failed = await engine.run(_request(recipe))
    assert failed.status == RunStatus.FAILED
    assert failed.metadata["failure"]["category"] == "preservation_setup"
    diagnostic = await read_run_failure_diagnostic(session, run=await engine.store.require_run(failed.id))
    assert diagnostic.exception_class_state.value == {
        "redacted": "redacted", "unknown": "unknown", "other": "known",
    }[kind]
    assert diagnostic.tool_execution_started is False
    assert diagnostic.retry_scheduled is False
    assert diagnostic.terminal is True
    assert "run.requeued" not in await _event_types(session, failed.id)


async def test_get_result_reports_committed_retry_without_failing_preservation(
    session, monkeypatch,
):
    from brain.app.api.routers.agent_mcp import _tool_get_result
    from brain.systems.inbound import service as inbound

    principal = await _seed_connection(session)
    submitted = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.memory",
            "desired_outcome": "preserve_knowledge",
            "message": "Preserve the reusable finding.",
            "source": {"source_tool": "codex"},
            "idempotency_key": "codex:connect-error:retry",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, submitted["ilo_outcome"])

    async def invoke(_spec):
        raise httpx.ConnectError("connection unavailable")

    engine = _engine(session, monkeypatch, "fast", invoke)
    run = await engine.run_existing(int(handling["run_id"]))
    assert run.status == RunStatus.QUEUED
    payload = await _tool_get_result(session, principal, {"event_id": submitted["event_id"]})
    assert payload["run_id"] == run.id
    assert payload["run_status"] == "queued"
    assert payload["evidence_status"] == "pending"
    assert payload["failure"]["status"] == "queued"
    assert payload["failure"]["diagnostic"]["retry_scheduled"] is True
    assert payload["failure"]["diagnostic"]["terminal"] is False


async def test_connect_error_retry_cap_reports_expired_without_a_pending_retry(
    session, monkeypatch,
):
    monkeypatch.setenv("AGENT_RUN_MAX_INTERRUPTION_REQUEUES", "2")

    async def invoke(_spec):
        raise httpx.ConnectError("connection unavailable")

    engine = _engine(session, monkeypatch, "fast", invoke)
    run = await engine.run(_request(interruption_count=2))
    assert run.status == RunStatus.EXPIRED
    diagnostic = await read_run_failure_diagnostic(session, run=await engine.store.require_run(run.id))
    assert diagnostic.exception_class == "ConnectError"
    assert diagnostic.retry_scheduled is False
    assert diagnostic.terminal is True
    events = await _event_types(session, run.id)
    assert "run.requeued" not in events
    assert "run.interruption_limit_exhausted" in events


async def test_failed_requeue_transaction_does_not_advertise_a_retry(session, monkeypatch):
    async def invoke(_spec):
        raise httpx.ConnectError("connection unavailable")

    engine = _engine(session, monkeypatch, "fast", invoke)
    run = await engine.store.create_run(_request())
    interrupt = engine.store.interrupt_and_requeue

    async def interrupt_then_fail(*args, **kwargs):
        await interrupt(*args, **kwargs)
        raise RuntimeError("transaction failed before commit")

    monkeypatch.setattr(engine.store, "interrupt_and_requeue", interrupt_then_fail)
    with pytest.raises(RuntimeError, match="transaction failed before commit"):
        await engine.run_existing(run.id)

    async with AsyncSession(bind=session.bind, expire_on_commit=False) as observer:
        row = await AsyncAgentRunStore(observer).require_run(run.id)
        assert row.status == "running"
        assert "interruption" not in row.metadata_
        assert "run.requeued" not in await _event_types(observer, run.id)
        assert await read_run_failure_diagnostic(observer, run=row) is None


async def test_stale_execution_owner_cannot_schedule_a_retry(session, monkeypatch):
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_request())

    async def invoke(_spec):
        row = await store.require_run(run.id)
        row.execution_token = "replacement-owner"
        await session.commit()
        raise httpx.ConnectError("connection unavailable")

    result = await _engine(session, monkeypatch, "fast", invoke).run_existing(run.id)
    row = await store.require_run(run.id)
    assert result.status == RunStatus.RUNNING
    assert row.execution_token == "replacement-owner"
    assert "interruption" not in row.metadata_
    assert "run.requeued" not in await _event_types(session, run.id)
    assert await read_run_failure_diagnostic(session, run=row) is None


async def test_retryable_failure_shape_alone_does_not_advertise_a_retry(session):
    store = AsyncAgentRunStore(session)
    run = await store.create_run(_request())
    await store.set_status(run.id, RunStatus.STARTING)
    await store.set_status(run.id, RunStatus.RUNNING)
    await store.fail_run(
        run.id,
        category=RunFailureCategory.PRESERVATION_SETUP,
        stage=RunFailureStage.AGENT_EXECUTION,
        exception_type=httpx.ConnectError,
    )
    diagnostic = await read_run_failure_diagnostic(session, run=await store.require_run(run.id))
    assert diagnostic.exception_class == "ConnectError"
    assert diagnostic.tool_execution_started is False
    assert diagnostic.retry_scheduled is False
    assert diagnostic.terminal is True
    assert "run.requeued" not in await _event_types(session, run.id)
