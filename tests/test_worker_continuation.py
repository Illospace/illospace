"""Generic worker fan-out continuation coverage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.models.cycle import CycleRun
from brain.systems.runs.chantier_continuation import (
    CONTINUATION_QUEUED_EVENT,
    GENERIC_CONTINUATION_QUEUED_EVENT,
    GENERIC_CONTINUATION_SOURCE,
    queue_worker_continuation_for_terminal_run,
)
from brain.systems.runs.domain import AgentRunRequest, RunRecipe
from brain.systems.runs.engine import AsyncAgentRunEngine
from brain.systems.runs.evidence_health import (
    WorkerEvidenceFailure,
    WorkerEvidenceReceipt,
    record_parent_evidence_failures,
)
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.runs.tool_catalog.definitions.workers import WORKER_SPAWN_TOOLS


def test_worker_evidence_receipt_deduplicates_by_full_canonical_identity():
    execution_failure = WorkerEvidenceFailure(
        worker_run_id=49,
        worker_role="GitHub reader",
        shard="github:Illospace/illospace",
        stage="worker_execution",
        status="failed",
        error="The worker failed.",
    )
    distinct_failure_for_same_worker = WorkerEvidenceFailure(
        worker_run_id=49,
        worker_role="GitHub reader",
        shard="github:Illospace/illospace",
        stage="project_context_materialization",
        error="The repository could not be materialized.",
    )

    receipt, first_added = WorkerEvidenceReceipt().with_failures(
        [execution_failure]
    )
    receipt, replay_added = receipt.with_failures(
        [execution_failure, distinct_failure_for_same_worker]
    )

    assert first_added == (execution_failure,)
    assert replay_added == (distinct_failure_for_same_worker,)
    assert receipt.failures == (
        execution_failure,
        distinct_failure_for_same_worker,
    )


def test_worker_evidence_receipt_keeps_identity_past_presentation_limit():
    failures = tuple(
        WorkerEvidenceFailure(
            worker_run_id=worker_id,
            worker_role="repository reader",
            shard=f"github:example/repo-{worker_id}",
            stage="worker_execution",
            status="failed",
            error="The worker failed.",
        )
        for worker_id in range(1, 22)
    )

    receipt, added = WorkerEvidenceReceipt().with_failures(failures)
    payload = receipt.to_payload()
    replayed = WorkerEvidenceReceipt.from_payload(payload)
    replayed, replay_added = replayed.with_failures([failures[20]])

    assert added == failures
    assert len(payload["failures"]) == 20
    assert payload["failure_count"] == 21
    assert replay_added == ()
    assert replayed.to_payload()["failure_count"] == 21


async def _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory(
        [
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
        ]
    )


def _parent_target() -> dict:
    return {
        "kind": "custom_parent_surface",
        "thread_id": "thread-generic-join",
        "resource_id": "resource-482",
    }


def _slack_target() -> dict:
    slack_trigger = {
        "team_id": "T482",
        "channel_id": "CGENERIC",
        "message_ts": "1785000000.000100",
        "thread_ts": "1785000000.000100",
    }
    return {
        "kind": "slack_message",
        **slack_trigger,
        "slack_thread_id": "slack:T482:CGENERIC:1785000000.000100",
        "slack_trigger": slack_trigger,
    }


async def _anchor(
    store: AsyncAgentRunStore,
    *,
    target: dict | None = None,
    metadata: dict | None = None,
    terminal: bool = True,
):
    target = dict(target or _parent_target())
    anchor = await store.create_run(
        AgentRunRequest(
            org_id="org-482",
            user_id="user-482",
            thread_id=str(target.get("thread_id") or target.get("slack_thread_id")),
            message="Coordinate the fan-out and synthesize its results.",
            target_ref=target,
            workspace_ref={"workspace_root": "/tmp/worker-join"},
            model_policy={"model": "openai/gpt-5.5", "thinking": "high"},
            metadata=dict(metadata or {}),
        )
    )
    await store.set_status(anchor.id, RunStatus.STARTING)
    await store.set_status(anchor.id, RunStatus.RUNNING)
    if terminal:
        await store.set_status(anchor.id, RunStatus.COMPLETED)
    return anchor


async def _worker(
    store: AsyncAgentRunStore,
    anchor,
    *,
    step: str,
    role: str,
    join_parent: bool | None,
    shard: str | None = None,
):
    metadata = {
        "origin": "spawn_worker",
        "spawned_by_tool": True,
        "worker_role": role,
    }
    if join_parent is not None:
        metadata["join_parent"] = join_parent
    if shard:
        metadata["shard"] = shard
    child = await store.create_child_run(
        anchor,
        recipe=RunRecipe.WORKER,
        message=f"Complete the {role} slice.",
        step_key=f"spawn_worker:{step}",
        metadata=metadata,
        initial_status=RunStatus.STARTING,
    )
    await store.set_status(child.id, RunStatus.RUNNING)
    return child


async def _generic_continuations(session) -> list[AgentRunRow]:
    return list(
        (
            await session.scalars(
                select(AgentRunRow)
                .where(
                    AgentRunRow.source_idempotency_key.like(
                        "worker:continuation:%"
                    )
                )
                .order_by(AgentRunRow.id.asc())
            )
        ).all()
    )


async def test_all_children_terminal_queues_one_generic_continuation_on_parent_thread(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    store = AsyncAgentRunStore(session)
    anchor = await _anchor(store)
    reader = await _worker(
        store,
        anchor,
        step="reader",
        role="repository reader",
        join_parent=True,
    )
    verifier = await _worker(
        store,
        anchor,
        step="verifier",
        role="independent verifier",
        join_parent=True,
    )
    engine = AsyncAgentRunEngine(session, recipes={})

    await engine.complete(reader.id, output="Reader found the relevant contract.")
    assert await _generic_continuations(session) == []

    await engine.complete(verifier.id, output="Verifier confirmed the contract.")
    continuations = await _generic_continuations(session)

    assert len(continuations) == 1
    continuation = continuations[0]
    assert continuation.thread_id == anchor.thread_id
    assert continuation.target_ref == anchor.target_ref
    assert continuation.workspace_ref == anchor.workspace_ref
    assert continuation.model_policy == anchor.model_policy
    assert continuation.source_idempotency_scope == GENERIC_CONTINUATION_SOURCE
    assert continuation.metadata_["worker_continuation"] == {
        "anchor_run_id": anchor.id,
        "anchor_thread_id": anchor.thread_id,
        "completed_fanout_run_id": anchor.id,
        "worker_run_ids": [reader.id, verifier.id],
    }
    assert continuation.metadata_["evidence_health"] == {
        "status": "ok",
        "completeness": "complete",
        "worker_shards": ["repository reader", "independent verifier"],
    }
    assert "Evidence health: ok" in continuation.input_message
    assert "Reader found the relevant contract." in continuation.input_message
    assert "Verifier confirmed the contract." in continuation.input_message

    replay_id = await queue_worker_continuation_for_terminal_run(
        session,
        terminal_run_id=verifier.id,
    )
    assert replay_id == continuation.id
    assert len(await _generic_continuations(session)) == 1

    events = (
        await session.scalars(
            select(AgentRunEventRow).where(
                AgentRunEventRow.run_id == anchor.id,
                AgentRunEventRow.event_type
                == GENERIC_CONTINUATION_QUEUED_EVENT,
            )
        )
    ).all()
    assert len(events) == 1
    assert events[0].payload["continuation_run_id"] == continuation.id


async def test_failed_spawned_reader_marks_parent_and_continuation_evidence_degraded(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    store = AsyncAgentRunStore(session)
    anchor = await _anchor(store)
    failed_reader = await _worker(
        store,
        anchor,
        step="github-reader",
        role="GitHub reader",
        join_parent=True,
        shard="github:Illospace/illospace",
    )
    healthy_reader = await _worker(
        store,
        anchor,
        step="domain-reader",
        role="Domain reader",
        join_parent=True,
        shard="domain:engineering-tickets",
    )
    engine = AsyncAgentRunEngine(session, recipes={})

    await engine.fail(
        failed_reader.id,
        "upstream_provider_error: overloaded_error",
        failure_category="upstream",
    )
    await engine.complete(
        healthy_reader.id,
        output="Domain evidence completed.",
    )

    refreshed_anchor = await session.get(AgentRunRow, anchor.id)
    health = refreshed_anchor.metadata_["evidence_health"]
    assert health["status"] == "degraded"
    assert health["completeness"] == "unavailable"
    assert health["missing_shards"] == ["github:Illospace/illospace"]
    assert health["failures"] == [
        {
            "kind": "worker_tool_failure",
            "tool": "spawn_worker",
            "child_run_id": failed_reader.id,
            "worker_run_id": failed_reader.id,
            "worker_role": "GitHub reader",
            "shard": "github:Illospace/illospace",
            "stage": "worker_execution",
            "status": "failed",
            "error": (
                "I hit a temporary upstream problem on this and it is still open "
                "— I will come back."
            ),
            "failure_category": "upstream",
        }
    ]

    continuation = (await _generic_continuations(session))[0]
    assert continuation.metadata_["evidence_health"] == health
    assert "Evidence health: degraded" in continuation.input_message
    assert "github:Illospace/illospace" in continuation.input_message
    assert "Do not report a normal sweep" in continuation.input_message


async def test_non_spawn_worker_child_failure_does_not_change_parent_evidence_health(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    store = AsyncAgentRunStore(session)
    anchor = await _anchor(store)
    ordinary_child = await store.create_child_run(
        anchor,
        recipe=RunRecipe.FAST,
        message="Run an ordinary child task.",
        step_key="ordinary-child",
        metadata={"origin": "scheduler"},
        initial_status=RunStatus.STARTING,
    )
    await store.set_status(ordinary_child.id, RunStatus.RUNNING)

    await AsyncAgentRunEngine(session, recipes={}).fail(
        ordinary_child.id,
        "ordinary internal failure",
        failure_category="internal",
    )

    refreshed_anchor = await session.get(AgentRunRow, anchor.id)
    assert "evidence_health" not in refreshed_anchor.metadata_
    assert await _generic_continuations(session) == []


async def test_generic_continuation_waits_for_terminal_parent(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    store = AsyncAgentRunStore(session)
    anchor = await _anchor(store, terminal=False)
    worker = await _worker(
        store,
        anchor,
        step="early",
        role="early finisher",
        join_parent=True,
    )
    engine = AsyncAgentRunEngine(session, recipes={})

    await engine.complete(worker.id, output="Worker finished before its parent.")
    assert await _generic_continuations(session) == []
    assert "evidence_health" not in (
        (await session.get(AgentRunRow, anchor.id)).metadata_
    )

    await engine.complete(anchor.id, output="Parent has now reached terminal state.")
    continuations = await _generic_continuations(session)
    assert len(continuations) == 1
    assert "Worker finished before its parent." in continuations[0].input_message
    assert continuations[0].metadata_["evidence_health"]["status"] == "ok"


async def test_spawn_worker_without_join_flag_remains_fire_and_forget(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    store = AsyncAgentRunStore(session)
    anchor = await _anchor(store)
    worker = await _worker(
        store,
        anchor,
        step="fire-and-forget",
        role="background reporter",
        join_parent=None,
    )

    await AsyncAgentRunEngine(session, recipes={}).complete(
        worker.id,
        output="Background report complete.",
    )

    assert await _generic_continuations(session) == []
    assert not await store.has_event_type(
        anchor.id,
        GENERIC_CONTINUATION_QUEUED_EVENT,
    )


async def test_chantier_scope_keeps_original_continuation_contract_when_join_opted_in(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    store = AsyncAgentRunStore(session)
    target = _slack_target()
    anchor = await _anchor(
        store,
        target=target,
        metadata={
            "slack_trigger": target["slack_trigger"],
            "chantier_declare": {
                "domain_id": 48,
                "record_id": 482,
                "record_ref": "domain_record:482",
            },
        },
    )
    worker = await _worker(
        store,
        anchor,
        step="chantier",
        role="chantier verifier",
        join_parent=True,
    )

    await AsyncAgentRunEngine(session, recipes={}).complete(
        worker.id,
        output="Chantier verification complete.",
    )

    chantier = list(
        (
            await session.scalars(
                select(AgentRunRow).where(
                    AgentRunRow.source_idempotency_key
                    == f"chantier:continuation:{anchor.id}"
                )
            )
        ).all()
    )
    assert len(chantier) == 1
    assert await _generic_continuations(session) == []
    assert chantier[0].target_ref["chantier"] == {
        "record_id": 482,
        "domain_id": 48,
        "anchor_thread_id": anchor.thread_id,
        "completed_fanout_run_id": anchor.id,
    }
    assert "worker_continuation" not in chantier[0].metadata_
    assert await store.has_event_type(anchor.id, CONTINUATION_QUEUED_EVENT)
    assert not await store.has_event_type(
        anchor.id,
        GENERIC_CONTINUATION_QUEUED_EVENT,
    )


async def test_concurrent_terminal_children_admit_only_one_generic_continuation(
    tmp_path,
    sqlite_postgres_ddl_patch,
):
    database = tmp_path / "generic-worker-continuation-race.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 5},
    )
    async with engine.begin() as connection:
        for table in (
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
        ):
            await connection.execute(CreateTable(table, if_not_exists=True))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as setup_session:
            setup_store = AsyncAgentRunStore(setup_session)
            anchor = await _anchor(setup_store)
            first = await _worker(
                setup_store,
                anchor,
                step="race-a",
                role="race worker A",
                join_parent=True,
            )
            second = await _worker(
                setup_store,
                anchor,
                step="race-b",
                role="race worker B",
                join_parent=True,
            )
            await setup_session.commit()
            anchor_id = anchor.id
            first_id = first.id
            second_id = second.id

        first_session = factory()
        second_session = factory()
        try:
            await asyncio.gather(
                AsyncAgentRunEngine(
                    first_session,
                    recipes={},
                    auto_commit_events=True,
                ).complete(first_id, output="Race result A."),
                AsyncAgentRunEngine(
                    second_session,
                    recipes={},
                    auto_commit_events=True,
                ).complete(second_id, output="Race result B."),
            )
        finally:
            await first_session.close()
            await second_session.close()

        async with factory() as inspection_session:
            continuations = await _generic_continuations(inspection_session)
            assert len(continuations) == 1
            assert continuations[0].source_idempotency_key == (
                f"worker:continuation:{anchor_id}"
            )
            events = (
                await inspection_session.scalars(
                    select(AgentRunEventRow).where(
                        AgentRunEventRow.run_id == anchor_id,
                        AgentRunEventRow.event_type
                        == GENERIC_CONTINUATION_QUEUED_EVENT,
                    )
                )
            ).all()
            assert len(events) == 1
    finally:
        await engine.dispose()


def test_spawn_worker_schema_exposes_opt_in_join_flag():
    spawn_worker = next(
        tool for tool in WORKER_SPAWN_TOOLS if tool["name"] == "spawn_worker"
    )
    join_parent = spawn_worker["input_schema"]["properties"]["join_parent"]

    assert join_parent["type"] == "boolean"
    assert join_parent["default"] is False


async def test_evidence_locks_refresh_preloaded_parent_and_cycle_after_concurrent_commit(
    tmp_path,
    sqlite_postgres_ddl_patch,
):
    database = tmp_path / "evidence-lock-freshness.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        for table in (
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            CycleRun.__table__,
        ):
            await connection.execute(CreateTable(table, if_not_exists=True))

    try:
        async with factory() as setup_session:
            cycle_run = CycleRun(
                cycle_id=7,
                scheduled_for=datetime.now(UTC),
                status="running",
                prompt_snapshot="Inspect the repository.",
                context_snapshot={
                    "evidence_health": {"status": "pending"},
                    "setup_cycle_marker": True,
                },
            )
            setup_session.add(cycle_run)
            await setup_session.flush()
            anchor = await _anchor(
                AsyncAgentRunStore(setup_session),
                metadata={
                    "source": "cycle",
                    "cycle_run_id": cycle_run.id,
                    "evidence_health": {"status": "pending"},
                    "setup_parent_marker": True,
                },
            )
            await setup_session.commit()
            parent_run_id = anchor.id
            cycle_run_id = cycle_run.id

        async with factory() as stale_session:
            stale_parent = await stale_session.get(AgentRunRow, parent_run_id)
            stale_cycle = await stale_session.get(CycleRun, cycle_run_id)
            assert "concurrent_parent_marker" not in stale_parent.metadata_
            assert "concurrent_cycle_marker" not in stale_cycle.context_snapshot

            async with factory() as writer_session:
                writer_parent = await writer_session.get(
                    AgentRunRow,
                    parent_run_id,
                )
                writer_parent.metadata_ = {
                    **writer_parent.metadata_,
                    "concurrent_parent_marker": "committed while lock waited",
                }
                writer_cycle = await writer_session.get(CycleRun, cycle_run_id)
                writer_cycle.context_snapshot = {
                    **writer_cycle.context_snapshot,
                    "concurrent_cycle_marker": "committed while lock waited",
                }
                await writer_session.commit()

            failure = WorkerEvidenceFailure(
                worker_run_id=49,
                worker_role="repository reader",
                shard="github:example/repository",
                stage="worker_execution",
                status="failed",
                error="The worker failed.",
            )
            await record_parent_evidence_failures(
                stale_session,
                parent_run_id=parent_run_id,
                failures=[failure],
            )
            await stale_session.commit()

        async with factory() as inspection_session:
            persisted_parent = await inspection_session.get(
                AgentRunRow,
                parent_run_id,
            )
            persisted_cycle = await inspection_session.get(
                CycleRun,
                cycle_run_id,
            )
            assert persisted_parent.metadata_["concurrent_parent_marker"] == (
                "committed while lock waited"
            )
            assert persisted_cycle.context_snapshot[
                "concurrent_cycle_marker"
            ] == "committed while lock waited"
            assert persisted_parent.metadata_["evidence_health"][
                "failure_count"
            ] == 1
            assert persisted_cycle.context_snapshot["evidence_health"][
                "failure_count"
            ] == 1
    finally:
        await engine.dispose()
