"""Exercise content-node conflict recovery with real PostgreSQL transactions.

Each test owns a private schema with committed parent rows. Separate sessions
hold separate connections: A leaves its insert uncommitted, and B must reach a
real PostgreSQL lock wait before A commits or rolls back. The unique violation
must roll back only B's savepoint, leaving its outer transaction usable.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.org import Org, User
from brain.platform.db.models.reconstructive_memory import MemoryNode
from brain.platform.db.repositories.reconstructive_memory import (
    MemoryNodeRepository,
    NodeDraft,
)
from tests.conftest import TEST_DB_URL
from tests.db_engine_utils import create_async_test_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_db]

BASE_KEY = "weekly update."
SCOPE_KEY = "content-concurrency"
RACE_TIMEOUT_SECONDS = 30.0
LOCK_WAIT_TIMEOUT_SECONDS = 15.0
_SCHEMA_TABLES = (Org.__table__, User.__table__, MemoryNode.__table__)


@dataclass
class _MemoryWorkspace:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    app_name: str
    org_id: str
    user_id: str
    other_user_id: str

    @asynccontextmanager
    async def transactions(self):
        # Session close rolls back unfinished work, including failed assertions.
        async with asyncio.timeout(RACE_TIMEOUT_SECONDS):
            async with self.sessions() as a, self.sessions() as b:
                await a.begin()
                await b.begin()
                yield a, b

    async def create_content(self, session, *, user_id=None, visibility="team"):
        return await MemoryNodeRepository(session).get_or_create_content_node(
            draft=NodeDraft(
                node_kind="content",
                canonical_label="Weekly update.",
                normalized_key=BASE_KEY,
                scope_key=SCOPE_KEY,
                text="Weekly update. The launch is scheduled for Monday.",
            ),
            org_id=self.org_id,
            user_id=user_id or self.user_id,
            visibility=visibility,
        )

    def plain_node(self, *, key=BASE_KEY, kind="content"):
        return MemoryNode(
            org_id=self.org_id,
            user_id=self.user_id,
            node_kind=kind,
            scope_key=SCOPE_KEY,
            normalized_key=key,
            canonical_label="Weekly update.",
            text="Weekly update. The launch is scheduled for Monday.",
            visibility="team",
        )

    async def await_lock_waiters(self, *, waiter_pid, blocker_pid, pending):
        """Observe B's INSERT blocked by A, using fresh statistics snapshots.

        Like the wake concurrency tests, use application_name and blocking PIDs
        to exclude unrelated backends. Polling database state supplies the
        ordering; no elapsed sleep is used as evidence of contention.
        """
        async with asyncio.timeout(LOCK_WAIT_TIMEOUT_SECONDS):
            async with self.engine.connect() as connection:
                connection = await connection.execution_options(
                    isolation_level="AUTOCOMMIT"
                )
                while True:
                    blocked = await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND application_name = :app_name AND pid = :waiter "
                            "AND wait_event_type = 'Lock' "
                            "AND :blocker = ANY(pg_blocking_pids(pid)) "
                            "AND query ILIKE 'INSERT INTO memory_nodes%')"
                        ),
                        {
                            "app_name": self.app_name,
                            "waiter": waiter_pid,
                            "blocker": blocker_pid,
                        },
                    )
                    if blocked:
                        return
                    if pending.done():
                        # Surface an unexpected database error without waiting
                        # for the polling deadline to hide it.
                        pending.result()
                        pytest.fail("B finished before its INSERT blocked on A")

    async def release_after_insert_blocks(self, a, b, insert, *, rollback=False):
        a_pid = await a.scalar(text("SELECT pg_backend_pid()"))
        b_pid = await b.scalar(text("SELECT pg_backend_pid()"))
        assert a_pid != b_pid
        pending = asyncio.create_task(insert(b))
        try:
            await self.await_lock_waiters(
                waiter_pid=b_pid, blocker_pid=a_pid, pending=pending,
            )
            if rollback:
                await a.rollback()
            else:
                await a.commit()
            return await pending
        finally:
            # Join B before its session closes, even if polling or A failed.
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def content_rows(self):
        async with asyncio.timeout(RACE_TIMEOUT_SECONDS):
            async with self.sessions() as session:
                return list(await session.scalars(select(MemoryNode).where(
                    MemoryNode.org_id == self.org_id,
                    MemoryNode.node_kind == "content",
                    MemoryNode.scope_key == SCOPE_KEY,
                ).order_by(MemoryNode.id)))


@pytest.fixture
async def memory_workspace():
    schema = f"memory_conc_{uuid4().hex[:12]}"
    app_name = f"memory-conc-{uuid4().hex[:8]}"
    admin_engine = create_async_test_engine(TEST_DB_URL)
    engine = create_async_test_engine(
        TEST_DB_URL,
        connect_args={
            "timeout": 10,
            "server_settings": {
                "search_path": f'"{schema}",public',
                "application_name": app_name,
                "statement_timeout": "25000",
            },
        },
    )
    workspace = _MemoryWorkspace(
        engine=engine,
        sessions=async_sessionmaker(engine, expire_on_commit=False),
        app_name=app_name,
        org_id=str(uuid4()),
        user_id=str(uuid4()),
        other_user_id=str(uuid4()),
    )
    try:
        async with admin_engine.connect() as admin:
            admin = await admin.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'CREATE SCHEMA "{schema}"'))
            try:
                async with engine.begin() as connection:
                    for table in _SCHEMA_TABLES:
                        await connection.execute(CreateTable(table))
                    # Verify the constraint in this schema, not a public table.
                    assert await connection.scalar(text(
                        "SELECT count(*) FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "JOIN pg_namespace n ON n.oid = t.relnamespace "
                        "WHERE n.nspname = :schema AND t.relname = 'memory_nodes' "
                        "AND c.contype = 'u' "
                        "AND c.conname = 'uq_memory_nodes_scope_key'"
                    ), {"schema": schema}) == 1
                async with workspace.sessions.begin() as session:
                    session.add(Org(
                        id=workspace.org_id,
                        name="Memory Concurrency Org",
                        slug=schema,
                    ))
                    await session.flush()
                    for user_id in (workspace.user_id, workspace.other_user_id):
                        session.add(User(
                            id=user_id,
                            org_id=workspace.org_id,
                            name="Memory Concurrency Owner",
                            email=f"{user_id}@example.com",
                            approved=True,
                        ))
                yield workspace
            finally:
                await admin.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_both_miss_a_commits_b_reuses(memory_workspace):
    ws = memory_workspace
    async with ws.transactions() as (a, b):
        first, reused = await ws.create_content(a)
        assert reused is False
        second, reused = await ws.release_after_insert_blocks(a, b, ws.create_content)
        assert second.id == first.id
        assert reused is True
        # A successful statement AND commit prove the outer transaction survived.
        assert await b.scalar(text("SELECT 1")) == 1
        await b.commit()

    rows = await ws.content_rows()
    assert [(node.id, node.normalized_key) for node in rows] == [(first.id, BASE_KEY)]


async def test_earlier_work_survives_savepoint_rollback(memory_workspace):
    ws = memory_workspace
    async with ws.transactions() as (a, b):
        earlier = ws.plain_node(key="earlier cue", kind="cue")
        b.add(earlier)
        await b.flush()
        earlier_id = earlier.id
        first, reused = await ws.create_content(a)
        assert reused is False
        second, reused = await ws.release_after_insert_blocks(a, b, ws.create_content)
        assert second.id == first.id
        assert reused is True
        assert await b.scalar(text("SELECT 1")) == 1
        await b.commit()

    rows = await ws.content_rows()
    assert [(node.id, node.normalized_key) for node in rows] == [(first.id, BASE_KEY)]
    async with ws.sessions() as session:
        saved = await session.get(MemoryNode, earlier_id)
        assert saved is not None
        assert (saved.node_kind, saved.normalized_key) == ("cue", "earlier cue")


async def test_private_winner_is_not_reused_by_another_user(memory_workspace):
    ws = memory_workspace
    async with ws.transactions() as (a, b):
        first, reused = await ws.create_content(a, visibility="private")
        assert reused is False
        original = {
            column.name: getattr(first, column.name)
            for column in MemoryNode.__table__.columns
        }

        async def other_user_insert(session):
            return await ws.create_content(session, user_id=ws.other_user_id)

        second, reused = await ws.release_after_insert_blocks(a, b, other_user_insert)
        assert reused is False
        assert second.id != first.id
        assert second.normalized_key == f"{BASE_KEY}:{ws.other_user_id}:team"
        assert second.visibility == "team"
        assert second.user_id == ws.other_user_id
        await b.commit()

    rows = await ws.content_rows()
    assert len(rows) == 2
    saved_first = next(node for node in rows if node.id == first.id)
    assert {
        column.name: getattr(saved_first, column.name)
        for column in MemoryNode.__table__.columns
    } == original
    saved_second = next(node for node in rows if node.id == second.id)
    assert (saved_second.normalized_key, saved_second.visibility, saved_second.user_id) == (
        f"{BASE_KEY}:{ws.other_user_id}:team", "team", ws.other_user_id,
    )


async def test_a_rolls_back_b_inserts_base_key(memory_workspace):
    ws = memory_workspace
    async with ws.transactions() as (a, b):
        first, reused = await ws.create_content(a)
        assert reused is False
        first_id = first.id  # Rollback expires A's ORM object.
        second, reused = await ws.release_after_insert_blocks(
            a, b, ws.create_content, rollback=True,
        )
        assert reused is False
        assert second.id != first_id
        assert second.normalized_key == BASE_KEY
        assert second.user_id == ws.user_id
        await b.commit()

    rows = await ws.content_rows()
    assert [(node.id, node.normalized_key) for node in rows] == [(second.id, BASE_KEY)]


async def test_asyncpg_reports_expected_unique_constraint_name(memory_workspace):
    ws = memory_workspace
    assert ws.engine.dialect.name == "postgresql"
    assert ws.engine.dialect.driver == "asyncpg"
    async with ws.transactions() as (a, b):
        first = ws.plain_node()
        a.add(first)
        await a.flush()

        async def duplicate_insert(session):
            async with session.begin_nested():
                session.add(ws.plain_node())
                await session.flush()

        with pytest.raises(IntegrityError) as caught:
            await ws.release_after_insert_blocks(a, b, duplicate_insert)
        assert '"uq_memory_nodes_scope_key"' in str(caught.value.orig)
        assert await b.scalar(text("SELECT 1")) == 1
        await b.commit()

    rows = await ws.content_rows()
    assert [(node.id, node.normalized_key) for node in rows] == [(first.id, BASE_KEY)]
