"""Tests for cortex router ORM migration.

Verifies that the migrated cortex endpoints work correctly with
SQLAlchemy ORM instead of raw SQL. Tests mock at the UnitOfWork/session level.
"""
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock

from brain.platform.db.models.idea import Idea, IdeaConnection, IdeaStateLog, IdeaThread
from brain.platform.db.models.run import AgentRun, CortexEvent
from brain.platform.db.models.skill import Skill


def _make_idea(**kwargs):
    defaults = dict(
        id="idea-1", title="Test Idea", description="desc", status="emerged",
        origin="user_created", salience_score=5.0, position_x=0, position_y=0,
        position_sticky=False, parent_id=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        archived_at=None, user_id="user-1", org_id="org-1",
        display_title=None, read_at=None, working_memory=None,
        active_agents=0, agent_details=None, attachments=[], embedding=None,
        visibility=None, harvest_type=None, encoded_at=None,
    )
    defaults.update(kwargs)
    idea = MagicMock(spec=Idea)
    for k, v in defaults.items():
        setattr(idea, k, v)
    return idea


def _make_thread(**kwargs):
    defaults = dict(
        id=1, idea_id="idea-1", role="user", content="hello",
        attachments=[], metadata_=None, message_type="discuss",
        user_id="user-1", created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    msg = MagicMock(spec=IdeaThread)
    for k, v in defaults.items():
        setattr(msg, k, v)
    return msg


def _make_run(**kwargs):
    defaults = dict(
        id=1, idea_id="idea-1", event="test", message="test msg",
        priority=0, status="completed", worker_pid=None,
        consumer_type="worker", consumer_runtime="host:sha:runtime",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        error=None, skill_used="test_skill", skill_outcome="success",
        model_used="medium", thinking_used="medium",
        tokens_input=100, tokens_output=50, tokens_total=150,
        cache_read=0, cache_write=0, estimated_cost=0.001,
        created_at=datetime.now(timezone.utc),
        implicit_feedback_summary=None, implicit_feedback_tags=[],
        adaptations=[], postmortem=None, error_classification=None,
        system_prompt_chars=None, workers_used=[],
    )
    defaults.update(kwargs)
    d = MagicMock(spec=AgentRun)
    for k, v in defaults.items():
        setattr(d, k, v)
    return d


def _make_skill(**kwargs):
    defaults = dict(
        id=1, name="test_skill", description="Test", procedure="do things",
        archived=False, triggers=[], model_tier="medium", thinking_tier="medium",
    )
    defaults.update(kwargs)
    s = MagicMock(spec=Skill)
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


# ── Helper tests ───────────────────────────────────────────────

class TestRowToDict:
    def test_orm_object(self):
        from brain.app.api.routers.cortex import _row_to_dict
        idea = _make_idea()
        result = _row_to_dict(idea)
        assert result is not None
        assert "id" in result

    def test_none_returns_none(self):
        from brain.app.api.routers.cortex import _row_to_dict
        assert _row_to_dict(None) is None


class TestParseMessageType:
    def test_assistant_returns_agent_response(self):
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("anything", "assistant") == "agent_response"

    def test_at_illo_returns_trigger(self):
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("hey @illo do this", "user") == "trigger"

    def test_plain_user_text_returns_trigger(self):
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("just chatting", "user") == "trigger"


class TestInferFeedbackTags:
    def test_detects_memory_failure(self):
        from brain.app.api.routers.cortex import _infer_feedback_tags
        tags = _infer_feedback_tags("Illo does not remember what we discussed")
        assert "memory_failure" in tags

    def test_empty_for_clean_input(self):
        from brain.app.api.routers.cortex import _infer_feedback_tags
        tags = _infer_feedback_tags("Great work on the implementation!")
        assert tags == []


# ── Implicit feedback ──────────────────────────────────────────

class TestRecordImplicitFeedback:
    def test_no_tags_does_nothing(self):
        from brain.app.api.routers.cortex import _record_implicit_feedback
        # Should not raise
        _record_implicit_feedback("idea-1", "clean message", [])

    @patch("brain.app.api.routers.cortex._helpers.UnitOfWork")
    @patch("brain.app.api.routers.cortex._helpers.logger")
    def test_records_feedback_with_tags(self, mock_logger, mock_uow_cls):
        from brain.app.api.routers.cortex import _record_implicit_feedback

        run = _make_run()
        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.session.execute.return_value.scalars.return_value.first.return_value = run
        mock_uow_cls.return_value = mock_uow

        with patch("brain.app.cli.memory.add_memory"):
            _record_implicit_feedback("idea-1", "does not remember", ["memory_failure"])


# ── Feedback triggers ──────────────────────────────────────────

class TestCreateFeedbackTriggers:
    @patch("brain.app.api.routers.cortex._helpers.UnitOfWork")
    def test_appends_negative_trigger(self, mock_uow_cls):
        from brain.app.api.routers.cortex import _create_feedback_triggers

        skill = _make_skill(triggers=[])
        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        # get_by_name returns the skill for the first call
        mock_uow.skills.get_by_name.return_value = skill
        mock_uow.skills.list_active.return_value = [skill]
        mock_uow_cls.return_value = mock_uow

        _create_feedback_triggers("test_skill", "task summary", "")

        # Trigger should have been appended
        assert len(skill.triggers) == 1
        assert skill.triggers[0]["direction"] == "negative"


# ── Presence tracking (in-memory, no DB) ───────────────────────

class TestPresence:
    def test_join_and_get(self):
        from brain.app.api.routers.cortex import _presence_join, _presence_get, _presence_leave

        _presence_join("idea-1", "u1", "Alice", "#aaa")
        viewers = _presence_get("idea-1")
        assert len(viewers) == 1
        assert viewers[0]["name"] == "Alice"

        _presence_leave("idea-1", "u1")
        assert _presence_get("idea-1") == []


# ── Extract mentions ───────────────────────────────────────────

class TestExtractMentions:
    def test_extracts_mentions(self):
        from brain.app.api.routers.cortex import _extract_mentions
        assert _extract_mentions("hey @alice and @bob") == ["alice", "bob"]

    def test_no_mentions(self):
        from brain.app.api.routers.cortex import _extract_mentions
        assert _extract_mentions("no mentions here") == []


# ── GPU title generation ───────────────────────────────────────

class TestGenerateTitleGpu:
    @patch("brain.platform.integrations.completions.simple_text_completion")
    @patch("brain.platform.gpu_client.get_client")
    def test_returns_local_title_without_fallback(self, mock_get_client, mock_simple_text_completion):
        from brain.app.api.routers.cortex import _generate_title_gpu

        mock_get_client.return_value.generate.return_value = "Great Title"
        mock_get_client.return_value.is_ready.return_value = True

        assert _generate_title_gpu("some raw text") == "Great Title"
        mock_get_client.return_value.is_ready.assert_called_once_with("llm")
        mock_get_client.return_value.generate.assert_called_once()
        kwargs = mock_get_client.return_value.generate.call_args.kwargs
        assert kwargs["fallback_policy"] == "local-only"
        assert kwargs["think"] is False
        mock_simple_text_completion.assert_not_called()

    @patch("brain.platform.providers.model_policy.get_model_for_tier")
    @patch("brain.platform.providers.model_policy.resolve_default_provider", return_value="openai")
    @patch("brain.platform.integrations.completions.simple_text_completion", return_value="Fallback Title")
    @patch("brain.platform.gpu_client.get_client", side_effect=Exception("no GPU"))
    def test_falls_back_to_provider_low_tier_model(
        self,
        mock_get_client,
        mock_simple_text_completion,
        mock_resolve_default_provider,
        mock_get_model_for_tier,
    ):
        from brain.app.api.routers.cortex import _generate_title_gpu

        mock_get_model_for_tier.return_value = "openai/gpt-5-mini"

        assert _generate_title_gpu("some raw text", user_id="user-1", org_id="org-1") == "Fallback Title"
        mock_resolve_default_provider.assert_called_once_with(user_id="user-1", org_id="org-1")
        mock_get_model_for_tier.assert_called_once_with(
            "low",
            provider="openai",
            include_provider_prefix=True,
            user_id="user-1",
            org_id="org-1",
        )
        mock_simple_text_completion.assert_called_once()
        kwargs = mock_simple_text_completion.call_args.kwargs
        assert kwargs["model"] == "openai/gpt-5-mini"
        assert kwargs["user_id"] == "user-1"
        assert kwargs["org_id"] == "org-1"
        assert kwargs["system_prompt"]

    @patch("brain.platform.integrations.completions.simple_text_completion", return_value=" x ")
    @patch("brain.platform.gpu_client.get_client", side_effect=Exception("no GPU"))
    def test_returns_none_when_fallback_title_invalid(self, mock_get_client, mock_simple_text_completion):
        from brain.app.api.routers.cortex import _generate_title_gpu

        assert _generate_title_gpu("some raw text") is None

    @patch("brain.platform.integrations.completions.simple_text_completion")
    @patch("brain.platform.gpu_client.get_client")
    def test_normalizes_local_title_output(self, mock_get_client, mock_simple_text_completion):
        from brain.app.api.routers.cortex import _generate_title_gpu

        mock_get_client.return_value.is_ready.return_value = True
        mock_get_client.return_value.generate.return_value = 'Title: "Sharper Idea Framing"\n\nExplanation'

        assert _generate_title_gpu("some raw text") == "Sharper Idea Framing"
        mock_simple_text_completion.assert_not_called()

    @patch("brain.platform.integrations.completions.simple_text_completion", return_value="Fallback Title")
    @patch("brain.platform.gpu_client.get_client")
    def test_skips_local_generation_when_llm_worker_not_ready(self, mock_get_client, mock_simple_text_completion):
        from brain.app.api.routers.cortex import _generate_title_gpu

        mock_get_client.return_value.is_ready.return_value = False

        assert _generate_title_gpu("some raw text") == "Fallback Title"
        mock_get_client.return_value.generate.assert_not_called()
        mock_simple_text_completion.assert_called_once()


class TestTitleRoutes:
    def test_generate_title_threads_authenticated_user_context(self):
        import asyncio
        from brain.app.api.routers.cortex._misc import generate_title

        class FakeRequest:
            async def json(self):
                return {"text": "Summarize this thought"}

        user = {"id": "user-1", "org_id": "org-1"}

        with patch("brain.app.api.routers.cortex._misc._generate_title_gpu", return_value="Threaded Title") as mock_generate_title:
            result = asyncio.run(generate_title(FakeRequest(), user=user))

        assert result == {"title": "Threaded Title"}
        mock_generate_title.assert_called_once_with(
            "Summarize this thought",
            user_id="user-1",
            org_id="org-1",
        )

    @patch("brain.systems.cortex.events.publish")
    @patch("brain.app.api.routers.cortex._misc.UnitOfWork")
    def test_backfill_titles_threads_authenticated_user_context(self, mock_uow_cls, mock_publish):
        from brain.app.api.routers.cortex._misc import backfill_titles

        idea_without_title = _make_idea(id="idea-1", title="Raw idea", display_title=None, archived_at=None)

        list_uow = MagicMock()
        list_uow.__enter__.return_value = list_uow
        list_uow.session.scalars.return_value.all.return_value = [idea_without_title]

        update_uow = MagicMock()
        update_uow.__enter__.return_value = update_uow
        update_uow.session.get.return_value = idea_without_title

        mock_uow_cls.side_effect = [list_uow, update_uow]

        with patch("brain.app.api.routers.cortex._misc._generate_title_gpu", return_value="Generated Title") as mock_generate_title:
            result = backfill_titles(user={"id": "user-1", "org_id": "org-1"})

        assert result == {"ok": True, "generated": 1, "total": 1}
        mock_generate_title.assert_called_once_with(
            "Raw idea",
            user_id="user-1",
            org_id="org-1",
        )
        assert idea_without_title.display_title == "Generated Title"
        mock_publish.assert_called_once_with("title_generated", {"idea_id": "idea-1", "title": "Generated Title"})

    @patch("brain.app.api.routers.cortex._misc.UnitOfWork")
    def test_backfill_titles_scopes_query_to_org(self, mock_uow_cls):
        from brain.app.api.routers.cortex._misc import backfill_titles

        list_uow = MagicMock()
        list_uow.__enter__.return_value = list_uow
        list_uow.session.scalars.return_value.all.return_value = []
        mock_uow_cls.return_value = list_uow

        result = backfill_titles(user={"id": "user-1", "org_id": "org-1"})

        assert result == {"ok": True, "generated": 0, "total": 0}
        stmt = list_uow.session.scalars.call_args.args[0]
        compiled = str(stmt)
        assert "ideas.org_id" in compiled

    @patch("brain.app.api.routers.cortex._misc.UnitOfWork")
    def test_backfill_titles_scopes_query_to_user_without_org(self, mock_uow_cls):
        from brain.app.api.routers.cortex._misc import backfill_titles

        list_uow = MagicMock()
        list_uow.__enter__.return_value = list_uow
        list_uow.session.scalars.return_value.all.return_value = []
        mock_uow_cls.return_value = list_uow

        result = backfill_titles(user={"id": "user-1", "org_id": None})

        assert result == {"ok": True, "generated": 0, "total": 0}
        stmt = list_uow.session.scalars.call_args.args[0]
        compiled = str(stmt)
        assert "ideas.user_id" in compiled
