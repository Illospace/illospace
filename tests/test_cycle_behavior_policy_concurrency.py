"""PostgreSQL proof for Cycle policy locking and optimistic concurrency."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.cycle import (
    BehaviorChangeAudit,
    Cycle,
    CycleGuidance,
    CycleRevision,
)
from brain.platform.db.models.org import Org, User
from brain.systems.cycles import behavior_policy
from brain.systems.cycles.access import CycleActor
from brain.systems.cycles.behavior_policy import (
    CyclePolicyApplied,
    CyclePolicyConflict,
    CyclePolicyPatch,
    async_apply_cycle_policy_change,
    async_preview_cycle_policy_change,
)
from tests.conftest import TEST_DB_URL
from tests.db_engine_utils import create_async_test_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_db]

LOCK_WAIT_TIMEOUT_SECONDS = 15.0
RACE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class _PostgresPolicyWorkspace:
    engine: object
    factory: object
    app_name: str
    cycle_id: int
    actor: CycleActor

    @asynccontextmanager
    async def held_cycle_lock(self):
        connection = await self.engine.connect()
        transaction = await connection.begin()
        try:
            await connection.execute(
                text("SELECT id FROM cycles WHERE id = :cycle_id FOR UPDATE"),
                {"cycle_id": self.cycle_id},
            )
            yield
        finally:
            await transaction.commit()
            await connection.close()

    async def await_lock_waiters(self, expected: int) -> None:
        connection = await (await self.engine.connect()).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            deadline = asyncio.get_running_loop().time() + LOCK_WAIT_TIMEOUT_SECONDS
            blocked = 0
            while asyncio.get_running_loop().time() < deadline:
                blocked = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname = current_database() "
                                "AND application_name = :app_name "
                                "AND cardinality(pg_blocking_pids(pid)) > 0"
                            ),
                            {"app_name": self.app_name},
                        )
                    ).scalar_one()
                )
                if blocked >= expected:
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(
                f"expected {expected} policy editor(s) waiting on the Cycle lock; "
                f"saw {blocked}"
            )
        finally:
            await connection.close()


@pytest.fixture
async def postgres_policy_workspace(monkeypatch):
    schema = f"policy_conc_{uuid4().hex[:12]}"
    app_name = f"policy-conc-{uuid4().hex[:8]}"
    admin_engine = create_async_test_engine(TEST_DB_URL)
    admin = await (await admin_engine.connect()).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    await admin.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_test_engine(
        TEST_DB_URL,
        connect_args={
            "server_settings": {
                "search_path": f'"{schema}",public',
                "application_name": app_name,
            }
        },
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    org_id = str(uuid4())
    user_id = str(uuid4())
    cycle_id = None
    monkeypatch.setattr(behavior_policy, "publish_cycle_change", lambda **_kwargs: None)
    try:
        async with engine.begin() as connection:
            for table in (
                Org.__table__,
                User.__table__,
                Cycle.__table__,
                CycleRevision.__table__,
                CycleGuidance.__table__,
                BehaviorChangeAudit.__table__,
            ):
                await connection.execute(CreateTable(table))

        async with factory.begin() as session:
            session.add(
                Org(
                    id=org_id,
                    name="Policy concurrency org",
                    slug=f"policy-concurrency-{org_id[:8]}",
                )
            )
            session.add(
                User(
                    id=user_id,
                    org_id=org_id,
                    name="Policy editor",
                    email=f"policy-editor-{user_id[:8]}@example.com",
                )
            )
            await session.flush()
            cycle = Cycle(
                user_id=user_id,
                org_id=org_id,
                name="Concurrent policy",
                prompt="Review concurrency.",
                schedule_expr="0 9 * * *",
                timezone="UTC",
                enabled=True,
                max_concurrency=1,
                retry_policy={},
                execution_mode="reuse_same_idea",
                reopen_archived=True,
            )
            session.add(cycle)
            await session.flush()
            cycle_id = cycle.id
            session.add(
                CycleRevision(
                    cycle_id=cycle.id,
                    revision_number=1,
                    source_type="user",
                    source_id=user_id,
                    rationale="Initial definition.",
                    name=cycle.name,
                    prompt=cycle.prompt,
                    schedule_expr=cycle.schedule_expr,
                    timezone=cycle.timezone,
                    enabled=cycle.enabled,
                    model_override=None,
                    thinking_override=None,
                    execution_policy_key=None,
                    target_idea_id=None,
                    context_policy={"workspace_id": org_id},
                )
            )

        yield _PostgresPolicyWorkspace(
            engine=engine,
            factory=factory,
            app_name=app_name,
            cycle_id=cycle_id,
            actor=CycleActor(user_id=user_id, org_id=org_id),
        )
    finally:
        await engine.dispose()
        await admin.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.close()
        await admin_engine.dispose()


async def test_two_reviewed_editors_serialize_and_second_gets_latest_conflict(
    postgres_policy_workspace,
):
    workspace = postgres_policy_workspace
    first_patch = CyclePolicyPatch(name="First reviewed edit")
    second_patch = CyclePolicyPatch(name="Second reviewed edit")
    async with workspace.factory() as session:
        first_preview = await async_preview_cycle_policy_change(
            session,
            actor=workspace.actor,
            cycle_id=workspace.cycle_id,
            proposal=first_patch,
        )
        second_preview = await async_preview_cycle_policy_change(
            session,
            actor=workspace.actor,
            cycle_id=workspace.cycle_id,
            proposal=second_patch,
        )

    async def apply(patch, preview, editor):
        async with workspace.factory.begin() as session:
            return await async_apply_cycle_policy_change(
                session,
                actor=workspace.actor,
                cycle_id=workspace.cycle_id,
                proposal=patch,
                expected_version=preview.before.version,
                preview_digest=preview.preview_digest,
                rationale=f"{editor} reviewed this edit.",
                source_reference=f"editor:{editor}",
            )

    async with workspace.held_cycle_lock():
        first_task = asyncio.create_task(apply(first_patch, first_preview, "first"))
        second_task = asyncio.create_task(apply(second_patch, second_preview, "second"))
        await workspace.await_lock_waiters(2)
        assert not first_task.done() and not second_task.done()

    results = await asyncio.wait_for(
        asyncio.gather(first_task, second_task),
        RACE_TIMEOUT_SECONDS,
    )
    applied = [result for result in results if isinstance(result, CyclePolicyApplied)]
    conflicts = [result for result in results if isinstance(result, CyclePolicyConflict)]
    assert len(applied) == 1
    assert len(conflicts) == 1
    assert conflicts[0].reason == "stale_version"
    assert conflicts[0].latest_effective_policy.version == 1
    assert conflicts[0].latest_effective_policy.snapshot["name"] in {
        "First reviewed edit",
        "Second reviewed edit",
    }

    async with workspace.factory() as session:
        changes = list(
            (
                await session.scalars(
                    select(BehaviorChangeAudit).order_by(
                        BehaviorChangeAudit.version.asc()
                    )
                )
            ).all()
        )
    assert [(change.version, change.target_id) for change in changes] == [
        (1, str(workspace.cycle_id))
    ]
