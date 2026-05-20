"""VaultRepository tests using in-memory SQLite."""
import uuid
from datetime import datetime, timezone

import pytest

from brain.platform.db.models.org import Org, User
from brain.platform.db.models.vault import Secret, VaultAccessLog, VaultMissingRequest
from brain.platform.db.repositories.vault import VaultAccessLogRepository, VaultRepository


USER_ID = "aaaaaaaa-0000-4000-8000-000000000001"
USER2_ID = "aaaaaaaa-0000-4000-8000-000000000002"
ORG_ID = "bbbbbbbb-0000-4000-8000-000000000001"
OTHER_ORG_ID = "bbbbbbbb-0000-4000-8000-000000000002"


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
            VaultMissingRequest.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    s.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    s.add(Org(id=OTHER_ORG_ID, name="Other Org", slug="other-org"))
    s.add(User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@test.com"))
    s.add(User(id=USER2_ID, org_id=ORG_ID, name="Bob", email="bob@test.com"))
    await s.flush()
    return s


@pytest.fixture
def repo(session):
    return VaultRepository(session)


@pytest.fixture
def log_repo(session):
    return VaultAccessLogRepository(session)


async def _make_secret(session, **kwargs):
    defaults = {
        "key_name": "API_KEY",
        "encrypted_value": b"encrypted",
        "org_id": ORG_ID,
        "created_by_user_id": USER_ID,
        "updated_by_user_id": USER_ID,
    }
    defaults.update(kwargs)
    secret = Secret(**defaults)
    session.add(secret)
    await session.flush()
    return secret


class TestVaultRepository:
    def test_model_assignment(self):
        assert VaultRepository.model is Secret

    async def test_list_by_org(self, repo, session):
        await _make_secret(session, key_name="KEY_A")
        await _make_secret(session, key_name="KEY_B", org_id=OTHER_ORG_ID)

        result = await repo.a_list_by_org(ORG_ID)

        assert [secret.key_name for secret in result] == ["KEY_A"]

    async def test_list_by_org_and_category(self, repo, session):
        await _make_secret(session, key_name="K1", category="api")
        await _make_secret(session, key_name="K2", category="db")

        result = await repo.a_list_by_org_and_category(ORG_ID, "api")

        assert [secret.key_name for secret in result] == ["K1"]

    async def test_get_by_key(self, repo, session):
        await _make_secret(session, key_name="MY_KEY")

        found = await repo.a_get_by_key(ORG_ID, "MY_KEY")

        assert found is not None
        assert found.key_name == "MY_KEY"

    async def test_get_by_key_not_found(self, repo):
        assert await repo.a_get_by_key(ORG_ID, "MISSING") is None

    async def test_list_missing_requests(self, repo, session):
        session.add(
            VaultMissingRequest(
                key_name="MISSING",
                org_id=ORG_ID,
                actor_user_id=USER_ID,
                resolved=False,
            )
        )
        session.add(
            VaultMissingRequest(
                key_name="RESOLVED",
                org_id=ORG_ID,
                actor_user_id=USER_ID,
                resolved=True,
            )
        )
        await session.flush()

        result = await repo.list_missing_requests(org_id=ORG_ID)

        assert [row.key_name for row in result] == ["MISSING"]


class TestVaultAccessLogRepository:
    def test_model_assignment(self):
        assert VaultAccessLogRepository.model is VaultAccessLog

    async def test_log_access(self, log_repo, session):
        secret = await _make_secret(session)
        entry = await log_repo.a_log_access(
            org_id=ORG_ID,
            actor_user_id=USER_ID,
            secret_id=secret.id,
            key_name="API_KEY",
            action="read",
        )
        await session.flush()

        assert entry.action == "read"
        assert entry.org_id == ORG_ID
        assert entry.actor_user_id == USER_ID

    async def test_list_recent_for_org(self, log_repo, session):
        secret = await _make_secret(session)
        await log_repo.a_log_access(
            org_id=ORG_ID,
            actor_user_id=USER_ID,
            secret_id=secret.id,
            key_name="API_KEY",
            action="read",
        )
        await session.flush()

        result = await log_repo.list_recent_for_org(ORG_ID)

        assert len(result) == 1
