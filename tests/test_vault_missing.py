"""Tests for org-scoped missing vault secret tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from brain.platform.db.models.vault import VaultMissingRequest
from brain.systems import vault

ORG_ID = "org-1"
ACTOR_ID = "user-1"


class _ScalarResult:
    def __init__(self, first_value=None, all_values=None) -> None:
        self._first = first_value
        self._all = all_values if all_values is not None else []

    def first(self):
        return self._first

    def all(self):
        return self._all


class _Session:
    def __init__(self, *, first_value=None, all_values=None, fail_scalars: Exception | None = None) -> None:
        self.added = []
        self.first_value = first_value
        self.all_values = all_values if all_values is not None else []
        self.fail_scalars = fail_scalars

    def add(self, obj) -> None:
        self.added.append(obj)

    async def scalars(self, _stmt):
        if self.fail_scalars is not None:
            raise self.fail_scalars
        return _ScalarResult(self.first_value, self.all_values)

    async def flush(self) -> None:
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


@pytest.mark.asyncio
async def test_record_missing_creates_org_entry(monkeypatch):
    session = _Session(first_value=None)
    _patch_uow(monkeypatch, session)

    await vault._async_record_missing("TEST_KEY", actor_user_id=ACTOR_ID, org_id=ORG_ID)

    missing = next(obj for obj in session.added if isinstance(obj, VaultMissingRequest))
    assert missing.org_id == ORG_ID
    assert missing.actor_user_id == ACTOR_ID
    assert missing.key_name == "TEST_KEY"
    assert missing.request_count == 1
    assert missing.resolved is False


@pytest.mark.asyncio
async def test_record_missing_increments_existing_org_entry(monkeypatch):
    existing = SimpleNamespace(
        actor_user_id=None,
        request_count=1,
        last_requested=None,
        resolved=True,
    )
    session = _Session(first_value=existing)
    _patch_uow(monkeypatch, session)

    await vault._async_record_missing("TEST_KEY", actor_user_id=ACTOR_ID, org_id=ORG_ID)

    assert existing.actor_user_id == ACTOR_ID
    assert existing.request_count == 2
    assert existing.last_requested is not None
    assert existing.resolved is False
    assert session.added == []


@pytest.mark.asyncio
async def test_record_missing_ignores_calls_without_org(monkeypatch):
    session = _Session(first_value=None)
    _patch_uow(monkeypatch, session)

    await vault._async_record_missing("TEST_KEY", actor_user_id=ACTOR_ID, org_id=None)

    assert session.added == []


@pytest.mark.asyncio
async def test_get_missing_requests_returns_org_rows(monkeypatch):
    now = datetime.now(timezone.utc)
    entry = SimpleNamespace(
        key_name="MISSING_KEY",
        request_count=3,
        first_requested=now,
        last_requested=now,
        actor_user_id=ACTOR_ID,
        org_id=ORG_ID,
    )
    session = _Session(all_values=[entry])
    _patch_uow(monkeypatch, session)

    result = await vault.async_get_missing_requests(actor_user_id=ACTOR_ID, org_id=ORG_ID)

    assert result == [
        {
            "key_name": "MISSING_KEY",
            "request_count": 3,
            "first_requested": now,
            "last_requested": now,
            "actor_user_id": ACTOR_ID,
            "org_id": ORG_ID,
        }
    ]


@pytest.mark.asyncio
async def test_resolve_missing_marks_all_org_rows_resolved(monkeypatch):
    existing = SimpleNamespace(resolved=False)
    session = _Session(all_values=[existing])
    _patch_uow(monkeypatch, session)

    await vault.async_resolve_missing("TEST_KEY", actor_user_id=ACTOR_ID, org_id=ORG_ID)

    assert existing.resolved is True


@pytest.mark.asyncio
async def test_resolve_missing_is_best_effort_when_schema_drifts(monkeypatch):
    session = _Session(fail_scalars=RuntimeError("schema drift"))
    _patch_uow(monkeypatch, session)

    await vault.async_resolve_missing("TEST_KEY", actor_user_id=ACTOR_ID, org_id=ORG_ID)


@pytest.mark.asyncio
async def test_get_secret_records_missing_when_not_found(monkeypatch):
    session = _Session()
    _patch_uow(monkeypatch, session)

    async def fake_secret_by_key(*_args, **_kwargs):
        return None

    recorded = []

    async def fake_record_missing(*args, **kwargs):
        recorded.append((args, kwargs))

    monkeypatch.setattr(vault, "_async_secret_by_key", fake_secret_by_key)
    monkeypatch.setattr(vault, "_async_record_missing", fake_record_missing)

    result = await vault.async_get_secret("TEST_NOTFOUND_KEY", ACTOR_ID, org_id=ORG_ID)

    assert result is None
    assert recorded == [(("TEST_NOTFOUND_KEY",), {"actor_user_id": ACTOR_ID, "org_id": ORG_ID})]


@pytest.mark.asyncio
async def test_set_secret_resolves_org_missing_request(monkeypatch):
    session = _Session()
    _patch_uow(monkeypatch, session)

    async def fake_secret_by_key(*_args, **_kwargs):
        return None

    resolved = []

    async def fake_resolve_missing(*args, **kwargs):
        resolved.append((args, kwargs))

    monkeypatch.setattr(vault, "_async_secret_by_key", fake_secret_by_key)
    monkeypatch.setattr(vault, "_encrypt", lambda value: b"ciphertext")
    monkeypatch.setattr(vault, "async_resolve_missing", fake_resolve_missing)

    await vault.async_set_secret("TEST_KEY", "test_value", ACTOR_ID, org_id=ORG_ID)

    assert resolved == [(("TEST_KEY",), {"actor_user_id": ACTOR_ID, "org_id": ORG_ID})]
