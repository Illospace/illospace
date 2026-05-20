"""VaultRepository tests using in-memory SQLite."""
import uuid
from datetime import datetime, timezone

import pytest

from brain.platform.db.base import Base
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.vault import Secret, VaultAccessLog, VaultMissingRequest, VaultShare
from brain.platform.db.repositories.vault import (
    VaultAccessLogRepository,
    VaultRepository,
    VaultShareRepository,
)


USER_ID = "aaaaaaaa-0000-4000-8000-000000000001"
USER2_ID = "aaaaaaaa-0000-4000-8000-000000000002"
ORG_ID = "bbbbbbbb-0000-4000-8000-000000000001"


def _register_sqlite_functions(dbapi_conn, connection_record):
    dbapi_conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


@pytest.fixture
async def session(async_sqlite_session_factory):
    s = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Secret.__table__,
            VaultAccessLog.__table__,
            VaultShare.__table__,
            VaultMissingRequest.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    org = Org(id=ORG_ID, name="Test Org", slug="test-org")
    s.add(org)
    user = User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@test.com")
    s.add(user)
    user2 = User(id=USER2_ID, org_id=ORG_ID, name="Bob", email="bob@test.com")
    s.add(user2)
    await s.flush()
    return s


@pytest.fixture
def repo(session):
    return VaultRepository(session)


@pytest.fixture
def share_repo(session):
    return VaultShareRepository(session)


@pytest.fixture
def log_repo(session):
    return VaultAccessLogRepository(session)


async def _make_secret(session, **kwargs):
    defaults = {
        "key_name": "API_KEY",
        "encrypted_value": b"encrypted",
        "user_id": USER_ID,
    }
    defaults.update(kwargs)
    s = Secret(**defaults)
    session.add(s)
    await session.flush()
    return s


class TestVaultRepository:
    def test_model_assignment(self):
        assert VaultRepository.model is Secret

    async def test_list_by_user(self, repo, session):
        await _make_secret(session, key_name="KEY_A")
        await _make_secret(session, key_name="KEY_B", user_id=USER2_ID)
        result = await repo.a_list_by_user(USER_ID)
        assert len(result) == 1

    async def test_list_by_user_and_category(self, repo, session):
        await _make_secret(session, key_name="K1", category="api")
        await _make_secret(session, key_name="K2", category="db")
        result = await repo.a_list_by_user_and_category(USER_ID, "api")
        assert len(result) == 1

    async def test_get_by_key(self, repo, session):
        await _make_secret(session, key_name="MY_KEY")
        found = await repo.a_get_by_key(USER_ID, "MY_KEY")
        assert found is not None
        assert found.key_name == "MY_KEY"

    async def test_get_by_key_prefers_org_scope(self, repo, session):
        legacy = await _make_secret(session, key_name="SHARED_KEY", user_id=USER2_ID)
        org_secret = await _make_secret(session, key_name="SHARED_KEY", org_id=ORG_ID)

        found = await repo.a_get_by_key(USER2_ID, "SHARED_KEY", org_id=ORG_ID)

        assert found is not None
        assert found.id == org_secret.id
        assert found.id != legacy.id

    async def test_get_by_key_not_found(self, repo):
        assert await repo.a_get_by_key(USER_ID, "MISSING") is None


class TestVaultShareRepository:
    def test_model_assignment(self):
        assert VaultShareRepository.model is VaultShare

    async def test_list_by_secret(self, share_repo, session):
        secret = await _make_secret(session, key_name="SHARED")
        share = VaultShare(
            secret_id=secret.id,
            shared_with_user_id=USER2_ID,
            shared_by_user_id=USER_ID,
        )
        session.add(share)
        await session.flush()
        result = await share_repo.a_list_by_secret(secret.id)
        assert len(result) == 1

    async def test_list_shared_with_user(self, share_repo, session):
        secret = await _make_secret(session, key_name="S2")
        share = VaultShare(
            secret_id=secret.id,
            shared_with_user_id=USER2_ID,
            shared_by_user_id=USER_ID,
        )
        session.add(share)
        await session.flush()
        result = await share_repo.a_list_shared_with_user(USER2_ID)
        assert len(result) == 1


class TestVaultAccessLogRepository:
    def test_model_assignment(self):
        assert VaultAccessLogRepository.model is VaultAccessLog

    async def test_log_access(self, log_repo, session):
        secret = await _make_secret(session)
        entry = await log_repo.a_log_access(USER_ID, secret.id, "API_KEY", "read")
        await session.flush()
        assert entry.action == "read"

    async def test_list_recent(self, log_repo, session):
        secret = await _make_secret(session)
        await log_repo.a_log_access(USER_ID, secret.id, "API_KEY", "read")
        await session.flush()
        result = await log_repo.a_list_recent()
        assert len(result) == 1
