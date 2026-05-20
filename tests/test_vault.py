"""Tests for the org-owned secret vault service."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from brain.platform.db.models.vault import Secret, VaultAccessLog
from brain.systems import vault

TEST_KEY = Fernet.generate_key().decode()
ORG_ID = "org-1"
ACTOR_ID = "user-1"


@pytest.fixture(autouse=True)
def set_vault_key(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", TEST_KEY)


class _Session:
    def __init__(self) -> None:
        self.added = []
        self.deleted = []
        self.flushes = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def delete(self, obj) -> None:
        self.deleted.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 11


class _UoW:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_uow(monkeypatch, session: _Session) -> None:
    monkeypatch.setattr(vault, "UnitOfWork", lambda: _UoW(session))


def _patch_secret_lookup(monkeypatch, secret) -> None:
    async def fake_secret_by_key(*_args, **_kwargs):
        return secret

    monkeypatch.setattr(vault, "_async_secret_by_key", fake_secret_by_key)


def test_encrypt_decrypt_roundtrip():
    plaintext = "super-secret-api-key-12345"
    encrypted = vault._encrypt(plaintext)
    assert isinstance(encrypted, bytes)
    assert vault._decrypt(encrypted) == plaintext


def test_encrypt_decrypt_special_chars():
    value = "p@$$w0rd! with spaces & symbols=+/"
    assert vault._decrypt(vault._encrypt(value)) == value


def test_missing_master_key_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
    env_file = tmp_path / ".env"

    class _MockPath:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __truediv__(self, value):
            return env_file if value == ".env" else tmp_path / value

    monkeypatch.setattr(vault, "Path", _MockPath)

    with pytest.raises(RuntimeError, match="VAULT_MASTER_KEY is required"):
        vault._get_fernet()


@pytest.mark.asyncio
async def test_set_secret_creates_org_secret_and_audit_log(monkeypatch):
    session = _Session()
    _patch_uow(monkeypatch, session)
    monkeypatch.setattr(vault, "_encrypt", lambda value: b"ciphertext")
    _patch_secret_lookup(monkeypatch, None)

    resolved_missing = []

    async def fake_resolve_missing(*args, **kwargs):
        resolved_missing.append((args, kwargs))

    monkeypatch.setattr(vault, "async_resolve_missing", fake_resolve_missing)

    await vault.async_set_secret(
        "OPENAI_API_KEY",
        "sk-test",
        ACTOR_ID,
        org_id=ORG_ID,
        description="OpenAI",
        category="api",
        agent_access_level=vault.VAULT_AGENT_ACCESS_AVAILABLE,
    )

    secret = next(obj for obj in session.added if isinstance(obj, Secret))
    audit = next(obj for obj in session.added if isinstance(obj, VaultAccessLog))
    assert secret.org_id == ORG_ID
    assert secret.created_by_user_id == ACTOR_ID
    assert secret.updated_by_user_id == ACTOR_ID
    assert secret.key_name == "OPENAI_API_KEY"
    assert secret.encrypted_value == b"ciphertext"
    assert secret.agent_access_level == vault.VAULT_AGENT_ACCESS_AVAILABLE
    assert audit.org_id == ORG_ID
    assert audit.actor_user_id == ACTOR_ID
    assert audit.key_name == "OPENAI_API_KEY"
    assert audit.action == "write"
    assert resolved_missing == [(("OPENAI_API_KEY",), {"actor_user_id": ACTOR_ID, "org_id": ORG_ID})]


@pytest.mark.asyncio
async def test_set_secret_updates_org_secret_in_place(monkeypatch):
    session = _Session()
    existing = SimpleNamespace(
        id=22,
        encrypted_value=b"old",
        description="old",
        category="old",
        org_id=ORG_ID,
        created_by_user_id="user-2",
        updated_by_user_id="user-2",
        agent_access_level=vault.VAULT_AGENT_ACCESS_ASK,
        updated_at=datetime.now(timezone.utc),
    )
    _patch_uow(monkeypatch, session)
    monkeypatch.setattr(vault, "_encrypt", lambda value: b"new")
    _patch_secret_lookup(monkeypatch, existing)

    async def fake_resolve_missing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(vault, "async_resolve_missing", fake_resolve_missing)

    await vault.async_set_secret("KEY", "value", ACTOR_ID, org_id=ORG_ID, category="api")

    assert existing.encrypted_value == b"new"
    assert existing.org_id == ORG_ID
    assert existing.created_by_user_id == "user-2"
    assert existing.updated_by_user_id == ACTOR_ID
    assert existing.category == "api"
    assert not any(isinstance(obj, Secret) for obj in session.added)


@pytest.mark.asyncio
async def test_get_secret_reads_org_secret_and_normalizes_integration_audit(monkeypatch):
    session = _Session()
    secret = SimpleNamespace(
        id=9,
        encrypted_value=b"ciphertext",
        last_accessed_at=None,
        access_count=0,
    )
    _patch_uow(monkeypatch, session)
    _patch_secret_lookup(monkeypatch, secret)
    monkeypatch.setattr(vault, "_decrypt", lambda value: "plain")

    value = await vault.async_get_secret(
        "GITHUB_TOKEN",
        ACTOR_ID,
        org_id=ORG_ID,
        accessed_by="github_connector",
    )

    audit = next(obj for obj in session.added if isinstance(obj, VaultAccessLog))
    assert value == "plain"
    assert secret.access_count == 1
    assert secret.last_accessed_at is not None
    assert audit.org_id == ORG_ID
    assert audit.actor_user_id == ACTOR_ID
    assert audit.accessed_by == "api"


@pytest.mark.asyncio
async def test_get_secret_missing_records_org_request(monkeypatch):
    session = _Session()
    _patch_uow(monkeypatch, session)
    _patch_secret_lookup(monkeypatch, None)
    recorded = []

    async def fake_record_missing(*args, **kwargs):
        recorded.append((args, kwargs))

    monkeypatch.setattr(vault, "_async_record_missing", fake_record_missing)

    result = await vault.async_get_secret("MISSING_KEY", ACTOR_ID, org_id=ORG_ID)

    assert result is None
    assert recorded == [(("MISSING_KEY",), {"actor_user_id": ACTOR_ID, "org_id": ORG_ID})]


@pytest.mark.asyncio
async def test_get_secret_env_fallback_does_not_create_missing_request(monkeypatch):
    session = _Session()
    _patch_uow(monkeypatch, session)
    _patch_secret_lookup(monkeypatch, None)
    monkeypatch.setenv("FALLBACK_KEY", "env-value")

    async def fail_record_missing(*_args, **_kwargs):
        raise AssertionError("env fallback should not record a missing vault secret")

    monkeypatch.setattr(vault, "_async_record_missing", fail_record_missing)

    assert (
        await vault.async_get_secret(
            "FALLBACK_KEY",
            ACTOR_ID,
            org_id=ORG_ID,
            allow_env_fallback=True,
        )
        == "env-value"
    )


@pytest.mark.asyncio
async def test_delete_secret_deletes_org_secret_and_audits(monkeypatch):
    session = _Session()
    secret = SimpleNamespace(id=12)
    _patch_uow(monkeypatch, session)
    _patch_secret_lookup(monkeypatch, secret)

    deleted = await vault.async_delete_secret("OPENAI_API_KEY", ACTOR_ID, org_id=ORG_ID)

    audit = next(obj for obj in session.added if isinstance(obj, VaultAccessLog))
    assert deleted is True
    assert session.deleted == [secret]
    assert audit.org_id == ORG_ID
    assert audit.actor_user_id == ACTOR_ID
    assert audit.action == "delete"


@pytest.mark.asyncio
async def test_delete_secret_returns_false_when_missing(monkeypatch):
    session = _Session()
    _patch_uow(monkeypatch, session)
    _patch_secret_lookup(monkeypatch, None)

    assert await vault.async_delete_secret("NOPE", ACTOR_ID, org_id=ORG_ID) is False
    assert session.added == []
    assert session.deleted == []


@pytest.mark.asyncio
async def test_vault_operations_require_org_and_real_actor():
    with pytest.raises(ValueError, match="org_id is required"):
        await vault.async_get_secret("KEY", ACTOR_ID, org_id=None)

    with pytest.raises(ValueError, match="actor_user_id is required"):
        await vault.async_set_secret("KEY", "value", "service:worker", org_id=ORG_ID)
