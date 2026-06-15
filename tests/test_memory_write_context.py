"""Tests for explicit memory write context and canonical repository inserts."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.platform.db.repositories.memory_write_context import (
    MemoryWriteContext,
    MemoryWriteContextError,
    dangerously_build_dev_test_memory_write_context,
)


def test_memory_write_context_requires_user_and_org_for_shared_visibility():
    with pytest.raises(MemoryWriteContextError):
        MemoryWriteContext(user_id="", source="test")

    with pytest.raises(MemoryWriteContextError):
        MemoryWriteContext(user_id="user-1", visibility="org", source="test")

    context = MemoryWriteContext(
        user_id="user-1",
        org_id="org-1",
        visibility="org",
        source="run_memory",
        idea_id="idea-1",
        run_id=42,
        session_id="session-1",
        confidence=2.0,
    )

    assert context.confidence == 1.0
    assert context.source_ref() == "run:42;idea:idea-1;session:session-1"


@pytest.mark.asyncio
async def test_dev_test_compatibility_context_is_disabled_in_production(monkeypatch):
    monkeypatch.setenv("ILLO_ENV", "production")

    with pytest.raises(MemoryWriteContextError):
        await dangerously_build_dev_test_memory_write_context(source="compatibility")


@pytest.mark.asyncio
async def test_dev_test_compatibility_context_warns_loudly(monkeypatch):
    monkeypatch.setenv("ILLO_ENV", "test")
    monkeypatch.setenv("ILLO_DEV_MEMORY_USER_ID", "user-1")
    monkeypatch.setenv("ILLO_DEV_MEMORY_ORG_ID", "org-1")

    with pytest.warns(RuntimeWarning, match="DEV/TEST COMPATIBILITY ONLY"):
        context = await dangerously_build_dev_test_memory_write_context(
            source="compatibility",
            visibility="org",
        )

    assert context.user_id == "user-1"
    assert context.org_id == "org-1"
    assert context.visibility == "org"


async def test_add_memory_uses_repository_insert_with_context():
    from brain.app.cli.memory import add_memory

    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.memories.insert_memory = AsyncMock(return_value={
        "id": 55,
        "type": "lesson",
        "salience": 6.0,
        "visibility": "private",
    })
    context = MemoryWriteContext(user_id="user-1", source="cli_test")

    with patch("brain.app.cli.memory.UnitOfWork", return_value=mock_uow), \
         patch("brain.app.cli.memory.check_quality", return_value=SimpleNamespace(passed=True, adjusted_salience=None)):
        result = await add_memory(
            content="This is a durable lesson worth recording in tests",
            memory_type="lesson",
            salience=6.0,
            write_context=context,
        )

    assert result["id"] == 55
    kwargs = mock_uow.memories.insert_memory.call_args.kwargs
    assert kwargs["context"].user_id == context.user_id
    assert kwargs["context"].source == context.source
    assert kwargs["context"].confidence == 0.6
    assert kwargs["content"].startswith("This is a durable lesson")
