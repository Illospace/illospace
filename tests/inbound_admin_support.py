"""Shared setup and helpers for inbound administration tests."""
from __future__ import annotations

import json
import re

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.domain import (
    Domain,
    DomainEvent,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
    DomainRelation,
    DomainRelationType,
)
from brain.platform.db.models.external_agent import (
    ExternalAgentConnectionRow,
    ExternalAgentConnectionTokenRow,
)
from brain.platform.db.models.inbound import (
    InboundDecisionReceiptRow,
    InboundDomainProjectionKeyRow,
    InboundDomainProjectionRow,
    InboundEventRow,
    InboundSourcePolicyRow,
)
from brain.platform.db.models.org import Org, User
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound import service as inbound_service
from brain.systems.user_domains.service import AsyncDomainService


ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"

    original = SQLiteDDLCompiler.get_column_default_string

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

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
            Domain.__table__,
            DomainObjectType.__table__,
            DomainFieldDefinition.__table__,
            DomainRelationType.__table__,
            DomainRecord.__table__,
            DomainRelation.__table__,
            DomainEvent.__table__,
            InboundSourcePolicyRow.__table__,
            InboundDomainProjectionRow.__table__,
            InboundDomainProjectionKeyRow.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


@pytest.fixture
async def seeded_session(session):
    session.add_all(
        [
            Org(id=ORG_ID, name="Uwear", slug="uwear"),
            User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com", approved=True),
        ]
    )
    await session.flush()
    return session


@pytest.fixture
def patch_unit_of_work(monkeypatch, seeded_session):
    class _SessionUnitOfWork:
        async def __aenter__(self):
            self.session = seeded_session
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                await seeded_session.flush()
            return False

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        _SessionUnitOfWork,
    )


def _decode(result: str) -> dict:
    return json.loads(result)


async def _create_issue_domain(session) -> Domain:
    return await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="Incoming Jira Issues",
        objects=[
            {
                "key": "issue",
                "name": "Issue",
                "title_field": "summary",
                "fields": [
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "summary", "field_type": "text", "required": True},
                    {"key": "status", "field_type": "text"},
                ],
            }
        ],
        actor_id=USER_ID,
    )


def _bridge_connection(connection: dict) -> dict:
    return {
        "connection_id": connection["id"],
        "org_id": ORG_ID,
        "owner_user_id": USER_ID,
        "display_name": connection["display_name"],
        "agent_kind": connection["agent_kind"],
        "scopes": [external_agents.SCOPE_SIGNAL_SUBMIT],
    }


async def _submit_issue_signal(
    session,
    connection: dict,
    *,
    origin: str,
    issue: dict,
    idempotency_key: str,
) -> dict:
    return await inbound_service.submit_inbound_envelope(
        session,
        connection=_bridge_connection(connection),
        envelope={
            "kind": "signal",
            "origin": origin,
            "payload": {"issue": issue},
            "idempotency_key": idempotency_key,
        },
        ingress_context={"surface": "webhook"},
    )
