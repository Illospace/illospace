from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.external_agent import (
    ExternalAgentConnectionRow,
    ExternalAgentConnectionTokenRow,
)
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.db.models.org import Org, User

if TYPE_CHECKING:
    from brain.systems.external_agents import service as external_agents


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
CONNECTION_ID = "33333333-3333-4333-8333-333333333333"
TOKEN_ID = "44444444-4444-4444-8444-444444444444"
RAW_TOKEN = "illo_conn_test_webhook_token"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    for name in ("visit_VECTOR", "visit_Vector"):
        if not hasattr(SQLiteTypeCompiler, name):
            setattr(SQLiteTypeCompiler, name, lambda self, type_, **kw: "TEXT")

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_inbound_preservation_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._inbound_preservation_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ExternalAgentConnectionRow.__table__,
            ExternalAgentConnectionTokenRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


async def _seed_connection(session) -> external_agents.AgentBridgePrincipal:
    from brain.systems.external_agents import service as external_agents

    if await session.get(Org, ORG_ID) is None:
        session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    if await session.get(User, USER_ID) is None:
        session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    session.add_all(
        [
            ExternalAgentConnectionRow(
                id=CONNECTION_ID,
                org_id=ORG_ID,
                owner_user_id=USER_ID,
                display_name="Codex",
                agent_kind="codex",
                transport="mcp",
                status="online",
                remote_agent_card={},
                capabilities={
                    "illo_submit": True,
                    "illo_read": True,
                    "illo_act": True,
                    "illo_get_result": True,
                },
                auth_metadata={},
                metadata_={},
            ),
            ExternalAgentConnectionTokenRow(
                id=TOKEN_ID,
                connection_id=CONNECTION_ID,
                org_id=ORG_ID,
                owner_user_id=USER_ID,
                token_hash=external_agents.hash_connection_token(RAW_TOKEN),
                token_prefix=external_agents.token_prefix(RAW_TOKEN),
                name="MCP token",
                scopes=[external_agents.SCOPE_SIGNAL_SUBMIT],
            ),
        ]
    )
    await session.flush()
    return external_agents.AgentBridgePrincipal(
        connection_id=CONNECTION_ID,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        token_id=TOKEN_ID,
        scopes=frozenset([external_agents.SCOPE_SIGNAL_SUBMIT]),
        connection_display_name="Codex",
        agent_kind="codex",
    )


async def _assert_queued_submission(session, outcome: dict) -> dict:
    assert outcome["operation"] == "queued"
    assert outcome["message"] == "Submission accepted and queued for Illo handling."
    handling = outcome["handling"]
    assert handling["status"] == "queued"
    assert handling["event_id"]
    assert handling["run_id"]

    run = await session.get(AgentRunRow, handling["run_id"])
    assert run is not None
    assert run.thread_id == f"inbound:{CONNECTION_ID}:{handling['event_id']}"
    assert run.status == "queued"
    assert run.metadata_["producer"] == "inbound"
    assert run.target_ref["kind"] == "inbound_submission"
    assert run.target_ref["event_id"] == handling["event_id"]
    assert run.metadata_["submission"]["message"]
    return handling
