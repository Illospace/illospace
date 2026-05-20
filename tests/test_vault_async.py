from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from brain.platform.db.models.vault import Secret, VaultAccessLog
from brain.systems import vault

ORG_ID = "org-1"
ACTOR_ID = "user-1"


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


@pytest.mark.asyncio
async def test_async_set_secret_creates_secret_without_sync_uow_bridge(monkeypatch):
    session = _Session()
    _patch_uow(monkeypatch, session)
    _patch_secret_lookup(monkeypatch, None)
    monkeypatch.setattr(vault, "_encrypt", lambda value: b"ciphertext")

    async def fake_resolve_missing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(vault, "async_resolve_missing", fake_resolve_missing)

    await vault.async_set_secret("OPENAI_API_KEY", "sk-test", ACTOR_ID, org_id=ORG_ID)

    secret = next(obj for obj in session.added if isinstance(obj, Secret))
    audit = next(obj for obj in session.added if isinstance(obj, VaultAccessLog))
    assert secret.org_id == ORG_ID
    assert secret.created_by_user_id == ACTOR_ID
    assert session.flushes == 1
    assert audit.org_id == ORG_ID
    assert audit.actor_user_id == ACTOR_ID
    assert audit.action == "write"


@pytest.mark.asyncio
async def test_async_get_secret_reads_and_audits_actor(monkeypatch):
    secret = SimpleNamespace(
        id=9,
        encrypted_value=b"ciphertext",
        last_accessed_at=None,
        access_count=0,
    )
    session = _Session()
    _patch_uow(monkeypatch, session)
    _patch_secret_lookup(monkeypatch, secret)
    monkeypatch.setattr(vault, "_decrypt", lambda value: "plain")

    value = await vault.async_get_secret("OPENAI_API_KEY", ACTOR_ID, org_id=ORG_ID)

    audit = next(obj for obj in session.added if isinstance(obj, VaultAccessLog))
    assert value == "plain"
    assert secret.access_count == 1
    assert audit.org_id == ORG_ID
    assert audit.actor_user_id == ACTOR_ID
    assert audit.action == "read"


@pytest.mark.asyncio
async def test_async_delete_secret_uses_native_async_uow(monkeypatch):
    secret = SimpleNamespace(id=12)
    session = _Session()
    _patch_uow(monkeypatch, session)
    _patch_secret_lookup(monkeypatch, secret)

    deleted = await vault.async_delete_secret("OPENAI_API_KEY", ACTOR_ID, org_id=ORG_ID)

    audit = next(obj for obj in session.added if isinstance(obj, VaultAccessLog))
    assert deleted is True
    assert session.deleted == [secret]
    assert audit.org_id == ORG_ID
    assert audit.actor_user_id == ACTOR_ID
    assert audit.action == "delete"


def test_async_vault_entrypoints_do_not_use_sync_uow_bridges():
    assert not hasattr(vault, "run_unit_of_work_task")

    for name, fn in inspect.getmembers(vault, inspect.iscoroutinefunction):
        if not (name.startswith("async_") or name.startswith("_async_")):
            continue
        source = inspect.getsource(fn)
        assert "run_unit_of_work_task" not in source, name
        assert "open_unit_of_work" not in source, name
