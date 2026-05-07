"""Tests for explicit memory write context and canonical repository inserts."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from brain.platform.db.repositories.memories import MemoryRepository
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


def test_dev_test_compatibility_context_is_disabled_in_production(monkeypatch):
    monkeypatch.setenv("ILLO_ENV", "production")

    with pytest.raises(MemoryWriteContextError):
        dangerously_build_dev_test_memory_write_context(source="legacy")


def test_dev_test_compatibility_context_warns_loudly(monkeypatch):
    monkeypatch.setenv("ILLO_ENV", "test")
    monkeypatch.setenv("ILLO_DEV_MEMORY_USER_ID", "user-1")
    monkeypatch.setenv("ILLO_DEV_MEMORY_ORG_ID", "org-1")

    with pytest.warns(RuntimeWarning, match="DEV/TEST COMPATIBILITY ONLY"):
        context = dangerously_build_dev_test_memory_write_context(
            source="legacy",
            visibility="org",
        )

    assert context.user_id == "user-1"
    assert context.org_id == "org-1"
    assert context.visibility == "org"


def test_memory_repository_create_is_disabled():
    repo = MemoryRepository(MagicMock())

    with pytest.raises(MemoryWriteContextError):
        repo.create(content="nope", memory_type="fact")


def test_repository_insert_memory_persists_context_columns():
    session = MagicMock()
    session.get_bind.side_effect = RuntimeError("no bind")
    session.bind = None
    session.execute.return_value.scalar_one.return_value = 101
    repo = MemoryRepository(session)
    context = MemoryWriteContext(
        user_id="user-1",
        org_id="org-1",
        visibility="org",
        source="brain_encode",
        conversation_id="conversation-1",
        idea_id="idea-1",
        run_id=42,
        session_id="session-1",
        confidence=0.77,
        evidence={"kind": "test"},
    )

    result = repo.insert_memory(
        content="A useful memory with enough content",
        memory_type="lesson",
        salience=7.0,
        emotion_label="neutral",
        tags=["test"],
        context=context,
    )

    assert result["id"] == 101
    assert result["visibility"] == "org"
    insert_sql, params = session.execute.call_args.args
    sql_text = str(insert_sql)
    assert "user_id" in sql_text
    assert "org_id" in sql_text
    assert "visibility" in sql_text
    assert "source_ref" in sql_text
    assert params["user_id"] == "user-1"
    assert params["org_id"] == "org-1"
    assert params["visibility"] == "org"
    assert params["source"] == "brain_encode"
    assert params["source_session"] == "session-1"
    assert params["source_ref"] == "run:42;idea:idea-1;conversation:conversation-1;session:session-1"
    assert params["confidence"] == 0.77


def test_add_memory_uses_repository_insert_with_context():
    from brain.app.cli.memory import add_memory

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.memories.insert_memory.return_value = {
        "id": 55,
        "type": "lesson",
        "salience": 6.0,
        "visibility": "private",
    }
    context = MemoryWriteContext(user_id="user-1", source="cli_test")

    with patch("brain.app.cli.memory.UnitOfWork", return_value=mock_uow), \
         patch("brain.app.cli.memory.check_quality", return_value=SimpleNamespace(passed=True, adjusted_salience=None)), \
         patch("brain.app.cli.memory.embed_document", return_value=[0.1] * 3), \
         patch("brain.app.cli.memory.make_emotional_embedding", return_value=[0.0] * 3):
        result = add_memory(
            content="This is a durable lesson worth recording in tests",
            memory_type="lesson",
            salience=6.0,
            write_context=context,
        )

    assert result["id"] == 55
    kwargs = mock_uow.memories.insert_memory.call_args.kwargs
    assert kwargs["context"] is context
    assert kwargs["content"].startswith("This is a durable lesson")
