"""Tests for task-scoped agent vault grants."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from brain.platform.db.models.run import AgentRun  # noqa: F401 - resolves vault grant FK target
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.vault import Secret, VaultAgentGrant, VaultMissingRequest, VaultShare


USER_ID = "aaaaaaaa-0000-4000-8000-000000000001"
OWNER_ID = "aaaaaaaa-0000-4000-8000-000000000002"
ORG_ID = "bbbbbbbb-0000-4000-8000-000000000001"


def _register_sqlite_functions(dbapi_conn, connection_record):
    dbapi_conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


pytestmark = pytest.mark.asyncio


class _TestUoW:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            await self.session.rollback()
        else:
            await self.session.commit()
        return False


@pytest.fixture
async def session(async_sqlite_session_factory):
    s = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Secret.__table__,
            VaultShare.__table__,
            VaultAgentGrant.__table__,
            VaultMissingRequest.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    s.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    s.add(User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@test.com"))
    s.add(User(id=OWNER_ID, org_id=ORG_ID, name="Sam", email="sam@test.com"))
    await s.commit()
    return s


@pytest.fixture
def patch_uow(session, monkeypatch):
    monkeypatch.setattr("brain.systems.vault.UnitOfWork", lambda: _TestUoW(session))


async def _secret(session, key_name: str, *, user_id=USER_ID, access_level="ask") -> Secret:
    secret = Secret(
        key_name=key_name,
        encrypted_value=b"encrypted-test-secret",
        user_id=user_id,
        agent_access_level=access_level,
    )
    session.add(secret)
    await session.flush()
    return secret


async def _share_secret(session, secret: Secret, *, with_user_id=USER_ID, by_user_id=OWNER_ID) -> VaultShare:
    share = VaultShare(
        secret_id=secret.id,
        shared_with_user_id=with_user_id,
        shared_by_user_id=by_user_id,
    )
    session.add(share)
    await session.flush()
    return share


async def test_agent_secret_read_without_run_is_denied(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    await _secret(session, "OPENAI_API_KEY")

    result = await authorize_agent_secret_read(
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


async def test_agent_secret_read_missing_key_records_request_without_grant(patch_uow, session):
    from sqlalchemy import func, select

    from brain.systems.vault import authorize_agent_secret_read

    result = await authorize_agent_secret_read(
        "ILLO_AGENT_GITHUB_TOKEN",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need GitHub access for this project task",
        requested_by="coordinator",
    )

    assert result["allowed"] is False
    assert result["status"] == "missing"
    assert "not found" in result["reason"]
    grant_count = int(await session.scalar(select(func.count()).select_from(VaultAgentGrant)) or 0)
    assert grant_count == 0
    missing = (
        await session.scalars(
            select(VaultMissingRequest).where(VaultMissingRequest.key_name == "ILLO_AGENT_GITHUB_TOKEN")
        )
    ).first()
    assert missing is not None
    assert missing.request_count == 1


async def test_stale_pending_grants_are_hidden_and_not_approvable(patch_uow, session):
    from brain.systems.vault import approve_agent_grant, list_agent_grants

    missing_grant = VaultAgentGrant(
        key_name="ILLO_AGENT_GITHUB_TOKEN",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=199,
        requested_by="agent",
        reason="Need GitHub access for this project task",
        status="pending",
    )
    session.add(missing_grant)
    available = await _secret(session, "GITHUB_TOKEN", access_level="available")
    stale_available_grant = VaultAgentGrant(
        key_name=available.key_name,
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=200,
        requested_by="agent",
        reason="Need GitHub access for this project task",
        status="pending",
    )
    session.add(stale_available_grant)
    await session.flush()

    grants = await list_agent_grants(USER_ID, org_id=ORG_ID, statuses=["pending"])

    assert grants == []
    assert await approve_agent_grant(
        missing_grant.id,
        approved_by_user_id=USER_ID,
        org_id=ORG_ID,
    ) is None
    assert await approve_agent_grant(
        stale_available_grant.id,
        approved_by_user_id=USER_ID,
        org_id=ORG_ID,
    ) is None


async def test_agent_grant_list_overfetches_past_stale_pending_rows(patch_uow, session):
    from brain.systems.vault import list_agent_grants

    now = datetime.now(timezone.utc)
    actionable_secret = await _secret(session, "OPENAI_API_KEY")
    session.add(
        VaultAgentGrant(
            key_name=actionable_secret.key_name,
            user_id=USER_ID,
            org_id=ORG_ID,
            run_id=198,
            requested_by="agent",
            reason="Need provider key for active task",
            status="pending",
            requested_at=now - timedelta(minutes=3),
        )
    )
    available_secret = await _secret(session, "GITHUB_TOKEN", access_level="available")
    for index, key_name in enumerate(("MISSING_TOKEN", available_secret.key_name), start=1):
        session.add(
            VaultAgentGrant(
                key_name=key_name,
                user_id=USER_ID,
                org_id=ORG_ID,
                run_id=200 + index,
                requested_by="agent",
                reason="Need GitHub access for this project task",
                status="pending",
                requested_at=now - timedelta(minutes=index),
            )
        )
    await session.flush()

    grants = await list_agent_grants(USER_ID, org_id=ORG_ID, statuses=["pending"], limit=1)

    assert [grant["key_name"] for grant in grants] == ["OPENAI_API_KEY"]


async def test_agent_secret_read_creates_pending_grant(patch_uow, session):
    from brain.systems.vault import authorize_agent_secret_read

    await _secret(session, "OPENAI_API_KEY")

    result = await authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
        requested_by="coordinator",
    )

    assert result["allowed"] is False
    assert result["status"] == "pending"
    grant = await session.get(VaultAgentGrant, result["grant"]["id"])
    assert grant is not None
    assert grant.key_name == "OPENAI_API_KEY"
    assert grant.reason == "Need provider key for active task"
    assert grant.requested_by == "coordinator"


async def test_approved_grant_is_one_use(patch_uow, session):
    from brain.systems.vault import approve_agent_grant, authorize_agent_secret_read

    await _secret(session, "OPENAI_API_KEY")

    pending = await authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
    )
    approved = await approve_agent_grant(
        pending["grant"]["id"],
        approved_by_user_id=USER_ID,
        org_id=ORG_ID,
        ttl_minutes=15,
        max_reads=1,
    )
    assert approved is not None

    first = await authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
    )
    assert first["allowed"] is True
    used = await session.get(VaultAgentGrant, pending["grant"]["id"])
    assert used.status == "used"
    assert used.read_count == 1
    assert await approve_agent_grant(
        pending["grant"]["id"],
        approved_by_user_id=USER_ID,
        org_id=ORG_ID,
        ttl_minutes=15,
        max_reads=1,
    ) is None

    second = await authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task again",
    )
    assert second["allowed"] is False
    assert second["status"] == "pending"
    assert second["grant"]["id"] != pending["grant"]["id"]


async def test_shared_agent_secret_grant_can_be_approved_and_consumed(patch_uow, session):
    from sqlalchemy import func, select

    from brain.systems.vault import approve_agent_grant, authorize_agent_secret_read

    owner_secret = await _secret(session, "SHARED_OPENAI_API_KEY", user_id=OWNER_ID)
    await _share_secret(session, owner_secret)

    pending = await authorize_agent_secret_read(
        "SHARED_OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
    )

    assert pending["allowed"] is False
    assert pending["status"] == "pending"
    missing_count = int(await session.scalar(select(func.count()).select_from(VaultMissingRequest)) or 0)
    assert missing_count == 0
    assert await approve_agent_grant(
        pending["grant"]["id"],
        approved_by_user_id=USER_ID,
        org_id=ORG_ID,
        ttl_minutes=15,
        max_reads=1,
    ) is not None

    consumed = await authorize_agent_secret_read(
        "SHARED_OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
    )

    assert consumed["allowed"] is True
    assert consumed["status"] == "approved"


async def test_grant_requires_exact_org_scope(patch_uow, session):
    from brain.systems.vault import approve_agent_grant, authorize_agent_secret_read

    await _secret(session, "OPENAI_API_KEY")

    pending = await authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=ORG_ID,
        run_id=42,
        reason="Need provider key for active task",
    )
    approved = await approve_agent_grant(
        pending["grant"]["id"],
        approved_by_user_id=USER_ID,
        org_id=ORG_ID,
        ttl_minutes=15,
        max_reads=1,
    )
    assert approved is not None

    result = await authorize_agent_secret_read(
        "OPENAI_API_KEY",
        user_id=USER_ID,
        org_id=None,
        run_id=42,
        reason="Need provider key for active task",
    )

    assert result["allowed"] is False
    assert result["status"] == "pending"
    assert result["grant"]["id"] != pending["grant"]["id"]
    original = await session.get(VaultAgentGrant, pending["grant"]["id"])
    assert original.status == "approved"
    assert original.read_count == 0
