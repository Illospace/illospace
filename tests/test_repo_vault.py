"""VaultRepository tests using in-memory SQLite."""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

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
def engine():
    eng = create_engine("sqlite://", echo=False)
    event.listen(eng, "connect", _register_sqlite_functions)
    Org.__table__.create(eng, checkfirst=True)
    User.__table__.create(eng, checkfirst=True)
    Secret.__table__.create(eng, checkfirst=True)
    VaultAccessLog.__table__.create(eng, checkfirst=True)
    VaultShare.__table__.create(eng, checkfirst=True)
    VaultMissingRequest.__table__.create(eng, checkfirst=True)
    return eng


@pytest.fixture
def session(engine):
    s = Session(engine)
    org = Org(id=ORG_ID, name="Test Org", slug="test-org")
    s.add(org)
    user = User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@test.com")
    s.add(user)
    user2 = User(id=USER2_ID, org_id=ORG_ID, name="Bob", email="bob@test.com")
    s.add(user2)
    s.flush()
    yield s
    s.close()


@pytest.fixture
def repo(session):
    return VaultRepository(session)


@pytest.fixture
def share_repo(session):
    return VaultShareRepository(session)


@pytest.fixture
def log_repo(session):
    return VaultAccessLogRepository(session)


def _make_secret(session, **kwargs):
    defaults = {
        "key_name": "API_KEY",
        "encrypted_value": b"encrypted",
        "user_id": USER_ID,
    }
    defaults.update(kwargs)
    s = Secret(**defaults)
    session.add(s)
    session.flush()
    return s


class TestVaultRepository:
    def test_model_assignment(self):
        assert VaultRepository.model is Secret

    def test_list_by_user(self, repo, session):
        _make_secret(session, key_name="KEY_A")
        _make_secret(session, key_name="KEY_B", user_id=USER2_ID)
        result = repo.list_by_user(USER_ID)
        assert len(result) == 1

    def test_list_by_user_and_category(self, repo, session):
        _make_secret(session, key_name="K1", category="api")
        _make_secret(session, key_name="K2", category="db")
        result = repo.list_by_user_and_category(USER_ID, "api")
        assert len(result) == 1

    def test_get_by_key(self, repo, session):
        _make_secret(session, key_name="MY_KEY")
        found = repo.get_by_key(USER_ID, "MY_KEY")
        assert found is not None
        assert found.key_name == "MY_KEY"

    def test_get_by_key_not_found(self, repo):
        assert repo.get_by_key(USER_ID, "MISSING") is None


class TestVaultShareRepository:
    def test_model_assignment(self):
        assert VaultShareRepository.model is VaultShare

    def test_list_by_secret(self, share_repo, session):
        secret = _make_secret(session, key_name="SHARED")
        share = VaultShare(
            secret_id=secret.id,
            shared_with_user_id=USER2_ID,
            shared_by_user_id=USER_ID,
        )
        session.add(share)
        session.flush()
        result = share_repo.list_by_secret(secret.id)
        assert len(result) == 1

    def test_list_shared_with_user(self, share_repo, session):
        secret = _make_secret(session, key_name="S2")
        share = VaultShare(
            secret_id=secret.id,
            shared_with_user_id=USER2_ID,
            shared_by_user_id=USER_ID,
        )
        session.add(share)
        session.flush()
        result = share_repo.list_shared_with_user(USER2_ID)
        assert len(result) == 1


class TestVaultAccessLogRepository:
    def test_model_assignment(self):
        assert VaultAccessLogRepository.model is VaultAccessLog

    def test_log_access(self, log_repo, session):
        secret = _make_secret(session)
        entry = log_repo.log_access(USER_ID, secret.id, "API_KEY", "read")
        session.flush()
        assert entry.action == "read"

    def test_list_recent(self, log_repo, session):
        secret = _make_secret(session)
        log_repo.log_access(USER_ID, secret.id, "API_KEY", "read")
        session.flush()
        result = log_repo.list_recent()
        assert len(result) == 1
