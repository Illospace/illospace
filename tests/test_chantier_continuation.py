"""Regression coverage for issue #399's chantier fan-out stall."""

from __future__ import annotations

from sqlalchemy import select

from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.systems.runs.chantier_continuation import (
    CONTINUATION_QUEUED_EVENT,
    queue_chantier_continuation_for_terminal_run,
)
from brain.systems.runs.domain import AgentRunRequest, RunRecipe
from brain.systems.runs.engine import AsyncAgentRunEngine
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore


async def _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory(
        [
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
        ]
    )


def _slack_target() -> dict:
    slack_trigger = {
        "team_id": "T399",
        "channel_id": "CCHANTIER",
        "message_ts": "1784567890.000100",
        "thread_ts": "1784567890.000100",
        "response_target": {
            "channel_id": "CCHANTIER",
            "thread_ts": "1784567890.000100",
        },
    }
    return {
        "kind": "slack_message",
        "team_id": "T399",
        "channel_id": "CCHANTIER",
        "message_ts": "1784567890.000100",
        "thread_ts": "1784567890.000100",
        "slack_thread_id": "slack:T399:CCHANTIER:1784567890.000100",
        "slack_trigger": slack_trigger,
    }


async def _completed_anchor(store: AsyncAgentRunStore):
    target = _slack_target()
    anchor = await store.create_run(
        AgentRunRequest(
            org_id="org-399",
            user_id="user-399",
            thread_id=target["slack_thread_id"],
            message="Coordinate the chantier and fan out verification.",
            target_ref=target,
            metadata={
                "slack_trigger": target["slack_trigger"],
                "slack_thread_id": target["slack_thread_id"],
                "chantier_declare": {
                    "domain_id": 1,
                    "record_id": 399,
                    "record_ref": "domain_record:399",
                },
            },
        )
    )
    await store.set_status(anchor.id, RunStatus.STARTING)
    await store.set_status(anchor.id, RunStatus.RUNNING)
    await store.set_status(anchor.id, RunStatus.COMPLETED)
    return anchor


async def _spawn_worker(
    store: AsyncAgentRunStore,
    anchor,
    *,
    step: str,
    role: str,
):
    child = await store.create_child_run(
        anchor,
        recipe=RunRecipe.WORKER,
        message=f"Run {role} verification.",
        step_key=f"spawn_worker:{step}",
        metadata={
            "origin": "spawn_worker",
            "spawned_by_tool": True,
            "worker_role": role,
        },
        initial_status=RunStatus.STARTING,
    )
    await store.set_status(child.id, RunStatus.RUNNING)
    return child


async def test_last_completed_child_immediately_queues_one_bound_continuation(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    store = AsyncAgentRunStore(session)
    anchor = await _completed_anchor(store)
    schema_worker = await _spawn_worker(
        store,
        anchor,
        step="schema",
        role="schema verifier",
    )
    runtime_worker = await _spawn_worker(
        store,
        anchor,
        step="runtime",
        role="runtime verifier",
    )
    engine = AsyncAgentRunEngine(session, recipes={})

    await engine.complete(schema_worker.id, output="Schema verification passed.")
    assert await _continuations(session) == []

    await engine.complete(runtime_worker.id, output="Runtime verification passed.")
    continuations = await _continuations(session)

    assert len(continuations) == 1
    continuation = continuations[0]
    assert continuation.status == RunStatus.QUEUED.value
    assert continuation.thread_id == anchor.thread_id
    assert continuation.parent_run_id is None
    assert continuation.target_ref["chantier"] == {
        "record_id": 399,
        "domain_id": 1,
        "anchor_thread_id": anchor.thread_id,
        "completed_fanout_run_id": anchor.id,
    }
    assert continuation.metadata_["chantier_continuation"]["worker_run_ids"] == [
        schema_worker.id,
        runtime_worker.id,
    ]
    assert "Schema verification passed." in continuation.input_message
    assert "Runtime verification passed." in continuation.input_message
    assert "No human follow-up triggered this run." in continuation.input_message

    events = (
        await session.scalars(
            select(AgentRunEventRow).where(
                AgentRunEventRow.run_id == anchor.id,
                AgentRunEventRow.event_type == CONTINUATION_QUEUED_EVENT,
            )
        )
    ).all()
    assert len(events) == 1
    assert events[0].payload["continuation_run_id"] == continuation.id
    assert events[0].payload["worker_run_ids"] == [schema_worker.id, runtime_worker.id]

    # Terminal replay and a duplicate completion notification both reuse the
    # same source-idempotent continuation.
    await engine.complete(runtime_worker.id, output="duplicate terminal delivery")
    replay_id = await queue_chantier_continuation_for_terminal_run(
        session,
        terminal_run_id=runtime_worker.id,
    )
    assert replay_id == continuation.id
    assert len(await _continuations(session)) == 1


async def test_parent_completion_closes_race_when_workers_finished_first(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    store = AsyncAgentRunStore(session)
    target = _slack_target()
    anchor = await store.create_run(
        AgentRunRequest(
            org_id="org-399",
            user_id="user-399",
            thread_id=target["slack_thread_id"],
            message="Fan out then finish.",
            target_ref=target,
            metadata={
                "slack_trigger": target["slack_trigger"],
                "chantier_continuation": {"domain_id": 1, "record_id": 399},
            },
        )
    )
    await store.set_status(anchor.id, RunStatus.STARTING)
    await store.set_status(anchor.id, RunStatus.RUNNING)
    worker = await _spawn_worker(store, anchor, step="fast", role="fast verifier")
    engine = AsyncAgentRunEngine(session, recipes={})

    await engine.complete(worker.id, output="Finished before the coordinator parent.")
    assert await _continuations(session) == []

    await engine.complete(anchor.id, output="Coordinator parent has now ended.")
    continuations = await _continuations(session)

    assert len(continuations) == 1
    assert continuations[0].metadata_["chantier_continuation"][
        "completed_fanout_run_id"
    ] == anchor.id
    assert "Finished before the coordinator parent." in continuations[0].input_message


async def _continuations(session) -> list[AgentRunRow]:
    return list(
        (
            await session.scalars(
                select(AgentRunRow)
                .where(AgentRunRow.source_idempotency_key.like("chantier:continuation:%"))
                .order_by(AgentRunRow.id.asc())
            )
        ).all()
    )
