"""
Tests for multiplayer brain: agent layer per-user context.

Tests cover:
- Per-user system prompt injection
- Cross-user intelligence attribution
- brain_encode with user_id and visibility
- Run user_id passthrough

After the ORM migration:
- _add_attribution takes a SQLAlchemy session, not a cursor
- tool_brain_encode uses UnitOfWork
- enqueue uses UnitOfWork
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

USER_A = {
    "id": "aaaa0000-0000-0000-0000-000000000001",
    "email": "alice@example.test", "name": "Alice", "role": "owner",
    "color": "#6366f1",
    "org_id": "org00000-0000-0000-0000-000000000001",
    "org_name": "Example", "org_slug": "example", "attribution_enabled": True,
}

USER_B = {
    "id": "bbbb0000-0000-0000-0000-000000000002",
    "email": "bob@example.test", "name": "Bob", "role": "member",
    "color": "#ed8936",
    "org_id": "org00000-0000-0000-0000-000000000001",
    "org_name": "Example", "org_slug": "example", "attribution_enabled": False,
}


# -- Cross-user attribution -----------------------------------------------

class TestCrossUserAttribution:

    def test_attribution_enabled_shows_name(self):
        """When attribution_enabled=True, cross-user memories show the author's name."""
        from brain.app.mcp.server import _add_attribution
        mock_session = MagicMock()
        mock_session.execute.return_value.mappings.return_value.all.return_value = [
            {"id": 1, "user_id": USER_B["id"], "name": "Bob", "attribution_enabled": True},
        ]
        memories = [{"id": 1, "content": "test", "type": "lesson"}]
        result = _add_attribution(mock_session, memories, USER_A["id"])
        assert result[0]["attributed_to"] == "Bob"

    def test_attribution_disabled_anonymizes(self):
        """When attribution_enabled=False, cross-user memories show 'A teammate'."""
        from brain.app.mcp.server import _add_attribution
        mock_session = MagicMock()
        mock_session.execute.return_value.mappings.return_value.all.return_value = [
            {"id": 1, "user_id": USER_B["id"], "name": "Bob", "attribution_enabled": False},
        ]
        memories = [{"id": 1, "content": "test", "type": "lesson"}]
        result = _add_attribution(mock_session, memories, USER_A["id"])
        assert result[0]["attributed_to"] == "A teammate"

    def test_own_memories_no_attribution(self):
        """Own memories should not get attribution tag."""
        from brain.app.mcp.server import _add_attribution
        mock_session = MagicMock()
        mock_session.execute.return_value.mappings.return_value.all.return_value = [
            {"id": 1, "user_id": USER_A["id"], "name": "Alice", "attribution_enabled": True},
        ]
        memories = [{"id": 1, "content": "test", "type": "lesson"}]
        result = _add_attribution(mock_session, memories, USER_A["id"])
        assert "attributed_to" not in result[0]


# -- brain_encode with user scoping ----------------------------------------

class TestBrainEncodeUserScoped:

    def test_encode_stores_user_id_and_visibility(self):
        """brain_encode should store user_id, org_id, and visibility."""
        from brain.app.mcp.server import tool_brain_encode

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.memories.insert_memory.return_value = {
            "id": 42,
            "type": "episode",
            "salience": 5.0,
            "visibility": "org",
        }

        with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
             patch("brain.systems.memory.embeddings.embed_document", return_value=[0.1] * 2000):
            result = tool_brain_encode(
                content="This is a test memory with enough content",
                user_id=USER_A["id"],
                org_id=USER_A["org_id"],
                visibility="org",
            )

        assert result["id"] == 42
        assert result["visibility"] == "org"
        context = mock_uow.memories.insert_memory.call_args.kwargs["context"]
        assert context.user_id == USER_A["id"]
        assert context.org_id == USER_A["org_id"]
        assert context.visibility == "org"

    def test_encode_defaults_to_private(self):
        """brain_encode should default to private visibility."""
        from brain.app.mcp.server import tool_brain_encode

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.memories.insert_memory.return_value = {
            "id": 43,
            "type": "episode",
            "salience": 5.0,
            "visibility": "private",
        }

        with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
             patch("brain.systems.memory.embeddings.embed_document", return_value=[0.1] * 2000):
            result = tool_brain_encode(
                content="Another test memory long enough to pass",
                user_id=USER_A["id"],
            )

        assert result["visibility"] == "private"
        context = mock_uow.memories.insert_memory.call_args.kwargs["context"]
        assert context.visibility == "private"


# -- Run user passthrough ----------------------------------------------

class TestRunUserPassthrough:

    def test_admit_run_stores_user_id(self):
        """admit_run() should store user_id in agent_runs."""
        from types import SimpleNamespace

        from brain.systems.runs.cortex import RunAdmissionRequest, admit_run

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.session.get.return_value = SimpleNamespace(
            id="idea-123",
            org_id=USER_A["org_id"],
            user_id=USER_A["id"],
            title="Idea",
        )
        mock_uow.session.get_bind.return_value.dialect.name = "sqlite"
        mock_uow.session.scalar.return_value = 0

        added_objects = []
        def _track_add(obj):
            added_objects.append(obj)
        mock_uow.session.add.side_effect = _track_add
        def _flush():
            if not added_objects:
                return
            obj = added_objects[-1]
            if getattr(obj, "id", None) is None:
                obj.id = 100 if obj.__class__.__name__ == "AgentRunRow" else 1
        mock_uow.session.flush.side_effect = _flush

        with patch("brain.systems.runs.cortex.UnitOfWork", return_value=mock_uow):
            result = admit_run(
                RunAdmissionRequest(
                    idea_id="idea-123",
                    event="thread_reply",
                    message="test message",
                    user_id=USER_A["id"],
                )
            )

        assert result.run_id == 100
        run_objects = [obj for obj in added_objects if obj.__class__.__name__ == "AgentRunRow"]
        assert len(run_objects) == 1
        run_obj = run_objects[0]
        assert run_obj.user_id == USER_A["id"]
        assert run_obj.thread_id == "idea-123"
        assert run_obj.target_ref["event"] == "thread_reply"
