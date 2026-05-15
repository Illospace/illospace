from __future__ import annotations

from types import SimpleNamespace

import pytest

from brain.systems import vault


class _VaultRepo:
    def __init__(self, secret=None) -> None:
        self.secret = secret
        self.keys: list[tuple[str, str]] = []

    async def get_by_key(self, user_id: str, key_name: str):
        self.keys.append((user_id, key_name))
        return self.secret


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
        if self.added and getattr(self.added[-1], "id", None) is None:
            self.added[-1].id = 11


class _UoW:
    def __init__(self, repo: _VaultRepo, session: _Session) -> None:
        self.vault = repo
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_async_set_secret_creates_secret(monkeypatch):
    repo = _VaultRepo()
    session = _Session()
    audit = []

    monkeypatch.setattr(vault, "UnitOfWork", lambda: _UoW(repo, session))
    monkeypatch.setattr(vault, "_encrypt", lambda value: b"ciphertext")

    async def fake_log(*args, **kwargs):
        audit.append((args, kwargs))

    monkeypatch.setattr(vault, "_async_log_access", fake_log)

    await vault.async_set_secret("OPENAI_API_KEY", "sk-test", "user-1")

    assert repo.keys == [("user-1", "OPENAI_API_KEY")]
    assert session.flushes == 1
    assert session.added[0].key_name == "OPENAI_API_KEY"
    assert session.added[0].encrypted_value == b"ciphertext"
    assert audit[0][0][:4] == ("user-1", 11, "OPENAI_API_KEY", "write")


@pytest.mark.asyncio
async def test_async_get_secret_reads_and_audits(monkeypatch):
    secret = SimpleNamespace(
        id=9,
        encrypted_value=b"ciphertext",
        last_accessed_at=None,
        access_count=0,
    )
    repo = _VaultRepo(secret=secret)
    session = _Session()
    audit = []

    monkeypatch.setattr(vault, "UnitOfWork", lambda: _UoW(repo, session))
    monkeypatch.setattr(vault, "_decrypt", lambda value: "plain")

    async def fake_log(*args, **kwargs):
        audit.append((args, kwargs))

    monkeypatch.setattr(vault, "_async_log_access", fake_log)

    value = await vault.async_get_secret("OPENAI_API_KEY", "user-1")

    assert value == "plain"
    assert secret.access_count == 1
    assert secret.last_accessed_at is not None
    assert audit[0][0][:4] == ("user-1", 9, "OPENAI_API_KEY", "read")


@pytest.mark.asyncio
async def test_async_delete_secret_uses_native_async_uow(monkeypatch):
    secret = SimpleNamespace(id=12)
    repo = _VaultRepo(secret=secret)
    session = _Session()
    audit = []

    monkeypatch.setattr(vault, "UnitOfWork", lambda: _UoW(repo, session))

    async def fake_log(*args, **kwargs):
        audit.append((args, kwargs))

    monkeypatch.setattr(vault, "_async_log_access", fake_log)

    deleted = await vault.async_delete_secret("OPENAI_API_KEY", "user-1")

    assert deleted is True
    assert repo.keys == [("user-1", "OPENAI_API_KEY")]
    assert session.deleted == [secret]
    assert audit[0][0][:4] == ("user-1", 12, "OPENAI_API_KEY", "delete")
