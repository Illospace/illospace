"""Persisted Cycle authority survives joined continuation lineage (#928)."""
from datetime import datetime, timezone

import pytest

from brain.platform.db.models.agent_run import AgentRunRow, AgentRunEventRow, AgentRunArtifactRow
from brain.platform.db.models.cycle import CycleRun
from brain.systems.cycles.exception_ping import cycle_exception_ping_context
from brain.systems.runs.chantier_continuation import queue_worker_continuation_for_terminal_run
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore
from tests.test_worker_continuation import _anchor, _worker, _slack_target


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, {}, [], "nope"])
def test_invalid_cycle_occurrence_handles_fail_closed(value):
    assert cycle_exception_ping_context({"cycle_run_id": value, "launch_context": {"run_kind": "scheduled_digest"}}) is None


@pytest.mark.parametrize("chantier", [False, True])
async def test_persisted_cycle_binding_survives_two_joined_continuations(
    async_sqlite_session_factory, sqlite_postgres_ddl_patch, chantier
):
    session = await async_sqlite_session_factory([
        AgentRunRow.__table__, AgentRunEventRow.__table__, AgentRunArtifactRow.__table__, CycleRun.__table__
    ])
    store = AsyncAgentRunStore(session)
    launch = {"run_kind": "scheduled_digest", "source": "schedule"}
    metadata = {"cycle_run_id": 42, "launch_envelope": {"launch_context": launch, "cycle_run_id": 42}}
    if chantier:
        metadata["chantier_declare"] = {"domain_id": 1, "record_id": 928, "record_ref": "domain_record:928"}
    anchor = await _anchor(store, target=_slack_target() if chantier else None, metadata=metadata)
    session.add(CycleRun(id=42, cycle_id=2, scheduled_for=datetime.now(timezone.utc),
                         run_id=anchor.id, prompt_snapshot="Cycle mission",
                         context_snapshot={"launch_context": launch}))
    await session.flush()
    for hop in range(2):
        worker = await _worker(store, anchor, step=f"reader-{hop}", role="reader", join_parent=True)
        await store.set_status(worker.id, RunStatus.COMPLETED)
        continuation_id = await queue_worker_continuation_for_terminal_run(session, terminal_run_id=worker.id)
        continuation = await session.get(AgentRunRow, continuation_id)
        assert cycle_exception_ping_context(continuation.metadata_) == {"cycle_run_id": 42, "run_kind": "scheduled_digest"}
        assert continuation.metadata_["launch_envelope"] == metadata["launch_envelope"]
        await store.set_status(continuation.id, RunStatus.STARTING)
        await store.set_status(continuation.id, RunStatus.RUNNING)
        await store.set_status(continuation.id, RunStatus.COMPLETED)
        anchor = continuation


async def test_unrelated_run_cannot_inherit_cycle_authority(
    async_sqlite_session_factory, sqlite_postgres_ddl_patch
):
    from brain.systems.runs.chantier_continuation import _inherited_cycle_context

    session = await async_sqlite_session_factory([
        AgentRunRow.__table__, AgentRunEventRow.__table__, AgentRunArtifactRow.__table__, CycleRun.__table__
    ])
    store = AsyncAgentRunStore(session)
    launch = {"run_kind": "scheduled_digest"}
    metadata = {"cycle_run_id": 42, "launch_context": launch}
    original = await _anchor(store, metadata=metadata)
    session.add(CycleRun(id=42, cycle_id=2, scheduled_for=datetime.now(timezone.utc),
                         run_id=original.id, prompt_snapshot="Cycle mission",
                         context_snapshot={"launch_context": launch}))
    unrelated = await _anchor(store, metadata=metadata)
    await session.flush()
    assert await _inherited_cycle_context(session, await session.get(AgentRunRow, unrelated.id)) == {}
    assert await _inherited_cycle_context(session, await session.get(AgentRunRow, original.id)) == metadata
