"""Tests for the create_skill agent tool handler."""

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.systems.runs.tool_handlers import _handle_create_skill, _handle_manage_skill


VALID_PROCEDURE = (
    "1. Check the logs for errors\n"
    "2. Trace the call stack backward\n"
    "3. Identify the root cause and apply the fix"
)


class TestCreateSkillGateEnforcement:
    """Test that create_skill enforces the live gate."""

    def test_invalid_name_rejected(self):
        result = _handle_create_skill(
            name="", description="Good description", procedure=VALID_PROCEDURE,
        )
        assert result["created"] is False
        assert "violations" in result

    def test_vague_procedure_rejected(self):
        result = _handle_create_skill(
            name="my-skill",
            description="Good description",
            procedure="When doing this task, just try hard and do your best. " * 3,
        )
        assert result["created"] is False
        assert any("Vague" in v for v in result["violations"])

    def test_short_procedure_rejected(self):
        result = _handle_create_skill(
            name="my-skill", description="Good description", procedure="do stuff",
        )
        assert result["created"] is False

    def test_user_requested_strict_validation(self):
        """User-requested skills require structured procedures."""
        unstructured = "This is a long procedure without any structure just a wall of text that goes on and on without numbered steps or bullets."
        result = _handle_create_skill(
            name="my-skill", description="Good description",
            procedure=unstructured, user_requested=True,
        )
        assert result["created"] is False
        assert any("lacks structure" in v for v in result["violations"])


def _make_uow(first_result=None):
    """Create a mock UnitOfWork with a mock session."""
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.session.execute.return_value.mappings.return_value.first.return_value = first_result
    return uow


class TestCreateSkillDBInsert:
    """Test the DB insertion path (mocked)."""

    @patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]")
    @patch("brain.systems.memory.embeddings.embed_document", return_value=[0.1] * 384)
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_user_requested_creates_non_provisional(self, MockUoW, mock_embed, mock_vec):
        MockUoW.return_value = _make_uow({"id": 42})

        result = _handle_create_skill(
            name="write-issues",
            description="Write well-structured GitHub issues with context and acceptance criteria",
            procedure=VALID_PROCEDURE,
            user_requested=True,
        )

        assert result["created"] is True
        assert result["skill_id"] == 42
        assert result["provisional"] is False
        assert result["status"] == "active"
        assert result["source_kind"] == "private_local"
        assert result["trust_level"] == "private_local"

    @patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]")
    @patch("brain.systems.memory.embeddings.embed_document", return_value=[0.1] * 384)
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_agent_initiated_creates_provisional(self, MockUoW, mock_embed, mock_vec):
        MockUoW.return_value = _make_uow({"id": 43})

        result = _handle_create_skill(
            name="write-issues",
            description="Write well-structured GitHub issues with context and acceptance criteria",
            procedure=VALID_PROCEDURE,
            user_requested=False,
        )

        assert result["created"] is True
        assert result["provisional"] is True
        assert "provisional" in result["status"]
        assert result["source_kind"] == "agent_draft"
        assert result["trust_level"] == "agent_draft"

    @patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]")
    @patch("brain.systems.memory.embeddings.embed_document", return_value=[0.1] * 384)
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_duplicate_skill_rejected(self, MockUoW, mock_embed, mock_vec):
        # ON CONFLICT DO NOTHING returns None for first()
        MockUoW.return_value = _make_uow(None)

        result = _handle_create_skill(
            name="write-issues",
            description="Write well-structured GitHub issues",
            procedure=VALID_PROCEDURE,
        )

        assert result["created"] is False
        assert "already exists" in result["error"]

    @patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]")
    @patch("brain.systems.memory.embeddings.embed_document", return_value=[0.1] * 384)
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_model_and_thinking_tier_passed(self, MockUoW, mock_embed, mock_vec):
        uow = _make_uow({"id": 44})
        MockUoW.return_value = uow

        result = _handle_create_skill(
            name="write-issues",
            description="Write well-structured GitHub issues with context",
            procedure=VALID_PROCEDURE,
            model_tier="high",
            thinking_tier="xhigh",
        )

        assert result["created"] is True
        assert result["model_tier"] == "high"
        assert result["thinking_tier"] == "xhigh"

        # Verify the SQL call includes the right tiers
        call_args = uow.session.execute.call_args
        params = call_args[0][1]
        assert params["model_tier"] == "high"
        assert params["thinking_tier"] == "xhigh"
        assert params["source_kind"] == "agent_draft"
        assert params["trust_level"] == "agent_draft"

    def test_invalid_reasoning_effort_tier_rejected(self):
        result = _handle_create_skill(
            name="write-issues",
            description="Write well-structured GitHub issues with context",
            procedure=VALID_PROCEDURE,
            thinking_tier="turbo",
        )

        assert result["created"] is False
        assert "Invalid thinking_tier" in result["error"]

    @patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]")
    @patch("brain.systems.memory.embeddings.embed_document", return_value=[0.1] * 384)
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_db_error_returns_error(self, MockUoW, mock_embed, mock_vec):
        """If DB fails, returns error dict instead of crashing."""
        MockUoW.side_effect = Exception("Connection refused")

        result = _handle_create_skill(
            name="write-issues",
            description="Write well-structured GitHub issues with context",
            procedure=VALID_PROCEDURE,
        )

        assert result["created"] is False
        assert "error" in result


class TestManageSkillUmbrella:
    """Test the public manage_skill umbrella surface."""

    def test_help_lists_skill_operations(self):
        result = json.loads(_handle_manage_skill(action="help"))

        assert result["tool"] == "manage_skill"
        assert "create" in result["operations"]
        assert "upsert_asset" in result["operations"]

    @patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]")
    @patch("brain.systems.memory.embeddings.embed_document", return_value=[0.1] * 384)
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_create_action_delegates_to_skill_creator(self, MockUoW, mock_embed, mock_vec):
        MockUoW.return_value = _make_uow({"id": 45})

        result = json.loads(_handle_manage_skill(
            action="create",
            name="write-issues",
            description="Write well-structured GitHub issues with context and acceptance criteria",
            procedure=VALID_PROCEDURE,
            user_requested=True,
        ))

        assert result["ok"] is True
        assert result["created"] is True
        assert result["skill_id"] == 45
        assert result["source_kind"] == "private_local"
