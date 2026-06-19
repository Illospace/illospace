"""
Tests for multiplayer brain: agent layer per-user context.

Tests cover:
- Per-user system prompt injection
- Cross-user intelligence attribution
- brain_encode with user_id and visibility
- Run user_id passthrough

After the ORM migration:
- _add_attribution takes a SQLAlchemy session, not a cursor
- async_tool_brain_encode uses UnitOfWork
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


# -- brain_encode with user scoping ----------------------------------------

class TestBrainEncodeUserScoped:

    async def test_encode_stores_user_id_and_visibility(self):
        """brain_encode should store user_id, org_id, and visibility."""
        from brain.app.mcp.server import async_tool_brain_encode

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        ingested = MagicMock()
        ingested.to_dict.return_value = {
            "memory_system": "reconstructive",
            "source_id": 7,
            "content_node_id": 42,
        }

        with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
             patch("brain.systems.reconstructive_memory.ingestion.ingest_memory_source", new=AsyncMock(return_value=ingested)) as ingest:
            result = await async_tool_brain_encode(
                content="This is a test memory with enough content",
                user_id=USER_A["id"],
                org_id=USER_A["org_id"],
                visibility="org",
            )

        assert result["content_node_id"] == 42
        assert result["compatibility_alias"] == "brain_encode"
        kwargs = ingest.call_args.kwargs
        assert kwargs["user_id"] == USER_A["id"]
        assert kwargs["org_id"] == USER_A["org_id"]
        assert kwargs["visibility"] == "org"

    async def test_encode_defaults_to_private(self):
        """brain_encode should default to private visibility."""
        from brain.app.mcp.server import async_tool_brain_encode

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        ingested = MagicMock()
        ingested.to_dict.return_value = {
            "memory_system": "reconstructive",
            "source_id": 8,
            "content_node_id": 43,
        }

        with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
             patch("brain.systems.reconstructive_memory.ingestion.ingest_memory_source", new=AsyncMock(return_value=ingested)) as ingest:
            result = await async_tool_brain_encode(
                content="Another test memory long enough to pass",
                user_id=USER_A["id"],
            )

        assert result["content_node_id"] == 43
        assert result["compatibility_alias"] == "brain_encode"
        assert ingest.call_args.kwargs["visibility"] == "private"


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
