"""Postgres serializes first writes and keeps one audit row per version."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.org import Org, User
from brain.platform.db.models.tool_policy import ToolPolicy, ToolPolicyChangeAudit
from brain.systems.runs.tool_policy_store import (
    ToolPolicyConflict,
    apply_tool_policy_change,
)
from tests.conftest import TEST_DB_URL
from tests.db_engine_utils import create_async_test_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_db]


async def test_concurrent_initial_writes_have_one_winner():
    schema = f"tool_policy_{uuid4().hex}"
    admin_engine = create_async_test_engine(TEST_DB_URL)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_test_engine(
        TEST_DB_URL, connect_args={"server_settings": {"search_path": schema}}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            for table in (
                Org.__table__,
                User.__table__,
                ToolPolicy.__table__,
                ToolPolicyChangeAudit.__table__,
            ):
                await connection.execute(CreateTable(table))
        org_id, user_id = str(uuid4()), str(uuid4())
        async with factory.begin() as session:
            session.add_all(
                [
                    Org(id=org_id, name="Workspace", slug=org_id),
                    User(
                        id=user_id,
                        org_id=org_id,
                        name="Admin",
                        email=f"{user_id}@example.com",
                    ),
                ]
            )

        async def write(name):
            try:
                async with factory.begin() as session:
                    await apply_tool_policy_change(
                        session,
                        org_id=org_id,
                        scope_type="organization",
                        disabled_tools=[name],
                        expected_version=0,
                        actor_id=user_id,
                        actor_type="human",
                    )
                return "applied"
            except ToolPolicyConflict:
                return "conflict"

        assert sorted(
            await asyncio.wait_for(
                asyncio.gather(write("exec_command"), write("run_script")), timeout=15
            )
        ) == ["applied", "conflict"]
        async with factory() as session:
            policy = await session.scalar(select(ToolPolicy))
            audits = list(await session.scalars(select(ToolPolicyChangeAudit)))
            assert policy.version == 1
            assert len(audits) == 1
            assert audits[0].before_disabled_tools == []
            assert audits[0].after_disabled_tools == policy.disabled_tools
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()
