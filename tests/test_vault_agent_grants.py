"""Tests for task-scoped agent vault grants."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from brain.platform.db.models.run import AgentRun  # noqa: F401 - resolves vault grant FK target
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.vault import VaultAgentGrant


USER_ID = "aaaaaaaa-0000-4000-8000-000000000001"
ORG_ID = "bbbbbbbb-0000-4000-8000-000000000001"


def _register_sqlite_functions(dbapi_conn, connection_record):
    dbapi_conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


class _TestUoW:
    def __init__(self, session: Session):
        self.session = session

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.session.rollback()
        else:
            self.session.commit()
        return False


@pytest.fixture
def session():
    engine = create_engine("sqlite://", echo=False)
    event.listen(engine, "connect", _register_sqlite_functions)
    Org.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    VaultAgentGrant.__table__.create(engine, checkfirst=True)
    s = Session(engine)
    s.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    s.add(User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@test.com"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def patch_uow(session, monkeypatch):
    monkeypatch.setattr("brain.systems.vault.UnitOfWork", lambda: _TestUoW(session))


def test_agent_secret_read_without_run_is_denied(patch_uow):
    from brain.systems.vault import authorize_agent_secret_read

    result = authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=None,
        reason="Need provider key for active task",
    )

    assert result == {
        "allowed": False,
        "status": "denied",
        "reason": "run-scoped grant required",
    }


def test_agent_secret_read_creates_pending_grant(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    result = authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
        requested_by="coordinator",
    )

    assert result["allowed"] is False
    assert result["status"] == "pending"
    grant = session.get(VaultAgentGrant, result["grant"]["id"])
    assert grant is not None
    assert grant.key_name == "OPENAI_API_KEY"
    assert grant.reason == "Need provider key for active task"
    assert grant.requested_by == "coordinator"


def test_approved_grant_is_one_use(patch_uow, session):
    from brain.systems.vault import approve_agent_grant, authorize_agent_secret_read

    pending = authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
    )
    approved = approve_agent_grant(
        pending["grant"]["id"],
        approved_by_user_id=USER_ID,
        org_id=ORG_ID,
        ttl_minutes=15,
        max_reads=1,
    )
    assert approved is not None

    first = authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
    )
    assert first["allowed"] is True
    used = session.get(VaultAgentGrant, pending["grant"]["id"])
    assert used.status == "used"
    assert used.read_count == 1
    assert approve_agent_grant(
        pending["grant"]["id"],
        approved_by_user_id=USER_ID,
        org_id=ORG_ID,
        ttl_minutes=15,
        max_reads=1,
    ) is None

    second = authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task again",
    )
    assert second["allowed"] is False
    assert second["status"] == "pending"
    assert second["grant"]["id"] != pending["grant"]["id"]


def test_grant_requires_exact_org_scope(patch_uow, session):
    from brain.systems.vault import approve_agent_grant, authorize_agent_secret_read

    pending = authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
    )
    approved = approve_agent_grant(
        pending["grant"]["id"],
        approved_by_user_id=USER_ID,
        org_id=ORG_ID,
        ttl_minutes=15,
        max_reads=1,
    )
    assert approved is not None

    result = authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=None,
        run_id=42,
        reason="Need provider key for active task",
    )

    assert result["allowed"] is False
    assert result["status"] == "pending"
    assert result["grant"]["id"] != pending["grant"]["id"]
    original = session.get(VaultAgentGrant, pending["grant"]["id"])
    assert original.status == "approved"
    assert original.read_count == 0
