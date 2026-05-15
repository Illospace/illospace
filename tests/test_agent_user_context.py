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

    async def test_attribution_enabled_shows_name(self):
        """When attribution_enabled=True, cross-user memories show the author's name."""
        from brain.app.mcp.server import _add_attribution
        mock_session = MagicMock()
        result = MagicMock()
        result.mappings.return_value.all.return_value = [
            {"id": 1, "user_id": USER_B["id"], "name": "Bob", "attribution_enabled": True},
        ]
        mock_session.execute = AsyncMock(return_value=result)
        memories = [{"id": 1, "content": "test", "type": "lesson"}]
        result = await _add_attribution(mock_session, memories, USER_A["id"])
        assert result[0]["attributed_to"] == "Bob"

    async def test_attribution_disabled_anonymizes(self):
        """When attribution_enabled=False, cross-user memories show 'A teammate'."""
        from brain.app.mcp.server import _add_attribution
        mock_session = MagicMock()
        result = MagicMock()
        result.mappings.return_value.all.return_value = [
            {"id": 1, "user_id": USER_B["id"], "name": "Bob", "attribution_enabled": False},
        ]
        mock_session.execute = AsyncMock(return_value=result)
        memories = [{"id": 1, "content": "test", "type": "lesson"}]
        result = await _add_attribution(mock_session, memories, USER_A["id"])
        assert result[0]["attributed_to"] == "A teammate"

    async def test_own_memories_no_attribution(self):
        """Own memories should not get attribution tag."""
        from brain.app.mcp.server import _add_attribution
        mock_session = MagicMock()
        result = MagicMock()
        result.mappings.return_value.all.return_value = [
            {"id": 1, "user_id": USER_A["id"], "name": "Alice", "attribution_enabled": True},
        ]
        mock_session.execute = AsyncMock(return_value=result)
        memories = [{"id": 1, "content": "test", "type": "lesson"}]
        result = await _add_attribution(mock_session, memories, USER_A["id"])
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

    async def test_admit_run_stores_user_id(self):
        """Work Intake should pass the actor user_id into run request construction."""
        from types import SimpleNamespace

        from brain.platform.db.models.idea import Idea
        from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

        class FakeSession:
            async def get(self, model, key):
                if model is Idea and key == "idea-123":
                    return SimpleNamespace(
                        id="idea-123",
                        title="Test idea",
                        org_id=USER_A["org_id"],
                        user_id="owner-id",
                        agent_details=None,
                    )
                return None

            async def scalars(self, *_args, **_kwargs):
                return SimpleNamespace(first=lambda: None)

        request = await build_agent_run_request(
            FakeSession(),
            WorkIntakeEvent(
                source="cortex",
                event_type="cortex.thread_reply",
                org_id=USER_A["org_id"],
                actor={"id": USER_A["id"], "org_id": USER_A["org_id"]},
                target={"kind": "cortex_idea", "idea_id": "idea-123"},
                payload={"message": "test message"},
                policy={"run_event": "thread_reply"},
            )
        )

        assert request.user_id == USER_A["id"]
        assert request.thread_id == "idea-123"
        assert request.target_ref["event"] == "thread_reply"
        assert request.message == "test message"
