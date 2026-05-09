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


# ── Low-tier title generation ──────────────────────────────────

class TestGenerateTitleLowTier:
    def test_uses_local_gpu_when_configured_low_tier_model_is_local(self):
        from brain.systems.cortex.title_generation import generate_display_title

        with patch("brain.platform.providers.model_policy.resolve_default_provider", return_value="openai") as mock_resolve_default_provider, \
             patch("brain.platform.providers.model_policy.get_model_for_tier", return_value="brain.platform.gpu/qwen3.5:4b") as mock_get_model_for_tier, \
             patch("brain.platform.gpu_client.get_client") as mock_get_client, \
             patch("brain.platform.integrations.completions.simple_text_completion") as mock_simple_text_completion:
            mock_get_client.return_value.generate.return_value = "Great Title"
            mock_get_client.return_value.is_ready.return_value = True

            assert generate_display_title("some raw text", user_id="user-1", org_id="org-1") == "Great Title"

        mock_resolve_default_provider.assert_called_once_with(user_id="user-1", org_id="org-1")
        mock_get_model_for_tier.assert_called_once_with(
            "low",
            provider="openai",
            include_provider_prefix=False,
            user_id="user-1",
            org_id="org-1",
        )
        mock_get_client.return_value.is_ready.assert_called_once_with("llm")
        mock_get_client.return_value.generate.assert_called_once()
        kwargs = mock_get_client.return_value.generate.call_args.kwargs
        assert kwargs["fallback_policy"] == "local-only"
        assert kwargs["think"] is False
        mock_simple_text_completion.assert_not_called()

    def test_uses_configured_api_low_tier_model_with_user_context(self):
        from brain.systems.cortex.title_generation import generate_display_title

        with patch("brain.platform.providers.model_policy.resolve_default_provider", return_value="openai") as mock_resolve_default_provider, \
             patch("brain.platform.providers.model_policy.get_model_for_tier", return_value="gpt-5-mini") as mock_get_model_for_tier, \
             patch("brain.platform.integrations.completions.simple_text_completion", return_value="Provider Title") as mock_simple_text_completion, \
             patch("brain.platform.gpu_client.get_client") as mock_get_client:
            assert generate_display_title("some raw text", user_id="user-1", org_id="org-1") == "Provider Title"

        mock_resolve_default_provider.assert_called_once_with(user_id="user-1", org_id="org-1")
        mock_get_model_for_tier.assert_called_once_with(
            "low",
            provider="openai",
            include_provider_prefix=False,
            user_id="user-1",
            org_id="org-1",
        )
        mock_simple_text_completion.assert_called_once()
        kwargs = mock_simple_text_completion.call_args.kwargs
        assert kwargs["model"] == "openai/gpt-5-mini"
        assert kwargs["user_id"] == "user-1"
        assert kwargs["org_id"] == "org-1"
        assert kwargs["system_prompt"]
        assert kwargs["reasoning_effort"] == "low"
        assert kwargs["operation_type"] == "title_generation"
        mock_get_client.assert_not_called()

    def test_returns_none_when_configured_api_title_invalid(self):
        from brain.systems.cortex.title_generation import generate_display_title

        with patch("brain.platform.providers.model_policy.resolve_default_provider", return_value="openai"), \
             patch("brain.platform.providers.model_policy.get_model_for_tier", return_value="gpt-5-mini"), \
             patch("brain.platform.integrations.completions.simple_text_completion", return_value=" x "):
            assert generate_display_title("some raw text") is None

    def test_normalizes_local_title_output(self):
        from brain.systems.cortex.title_generation import generate_display_title

        with patch("brain.platform.providers.model_policy.resolve_default_provider", return_value="openai"), \
             patch("brain.platform.providers.model_policy.get_model_for_tier", return_value="brain.platform.gpu/qwen3.5:4b"), \
             patch("brain.platform.gpu_client.get_client") as mock_get_client, \
             patch("brain.platform.integrations.completions.simple_text_completion") as mock_simple_text_completion:
            mock_get_client.return_value.is_ready.return_value = True
            mock_get_client.return_value.generate.return_value = 'Title: "Sharper Idea Framing"\n\nExplanation'

            assert generate_display_title("some raw text") == "Sharper Idea Framing"
        mock_simple_text_completion.assert_not_called()

    def test_returns_none_when_configured_local_worker_not_ready(self):
        from brain.systems.cortex.title_generation import generate_display_title

        with patch("brain.platform.providers.model_policy.resolve_default_provider", return_value="openai"), \
             patch("brain.platform.providers.model_policy.get_model_for_tier", return_value="brain.platform.gpu/qwen3.5:4b"), \
             patch("brain.platform.gpu_client.get_client") as mock_get_client, \
             patch("brain.platform.integrations.completions.simple_text_completion") as mock_simple_text_completion:
            mock_get_client.return_value.is_ready.return_value = False

            assert generate_display_title("some raw text") is None
        mock_get_client.return_value.generate.assert_not_called()
        mock_simple_text_completion.assert_not_called()


class TestTitleRoutes:
    def test_generate_title_threads_authenticated_user_context(self):
        import asyncio
        from brain.app.api.routers.cortex._misc import generate_title

        class FakeRequest:
            async def json(self):
                return {"text": "Summarize this thought"}

        user = {"id": "user-1", "org_id": "org-1"}

        with patch("brain.app.api.routers.cortex._misc.generate_display_title", return_value="Threaded Title") as mock_generate_title:
            result = asyncio.run(generate_title(FakeRequest(), user=user))

        assert result == {"title": "Threaded Title"}
        mock_generate_title.assert_called_once_with(
            "Summarize this thought",
            user_id="user-1",
            org_id="org-1",
        )

    def test_regenerate_idea_title_triggers_store_generation_with_overwrite(self):
        import asyncio
        from brain.app.api.routers.cortex._ideas import regenerate_idea_title
        from brain.systems.cortex.title_generation import StoredDisplayTitle

        db = MagicMock()
        idea = _make_idea(id="idea-1", title="Raw idea", display_title="Old Title", org_id="org-1")

        with patch("brain.app.api.routers.cortex._ideas._require_idea_for_user", return_value=idea) as mock_require, \
             patch(
                 "brain.app.api.routers.cortex._ideas.generate_and_store_idea_display_title",
                 return_value=StoredDisplayTitle(idea_id="idea-1", title="New Title", updated=True),
             ) as mock_generate_title, \
             patch("brain.app.api.routers.cortex._ideas._idea_read_with_author", return_value={"id": "idea-1"}) as mock_read:
            result = asyncio.run(regenerate_idea_title("idea-1", db=db, user={"id": "user-1", "org_id": "org-1"}))

        assert result == {"id": "idea-1"}
        mock_require.assert_called_once_with(db, "idea-1", {"id": "user-1", "org_id": "org-1"})
        mock_generate_title.assert_called_once_with(
            "idea-1",
            raw_title="Raw idea",
            user_id="user-1",
            org_id="org-1",
            overwrite=True,
        )
        db.refresh.assert_called_once_with(idea)
        mock_read.assert_called_once_with(idea, db)

    def test_regenerate_idea_title_surfaces_generation_failure(self):
        import asyncio
        from fastapi import HTTPException

        from brain.app.api.routers.cortex._ideas import regenerate_idea_title
        from brain.systems.cortex.title_generation import StoredDisplayTitle

        idea = _make_idea(id="idea-1", title="Raw idea", display_title="Old Title", org_id="org-1")

        with patch("brain.app.api.routers.cortex._ideas._require_idea_for_user", return_value=idea), \
             patch(
                 "brain.app.api.routers.cortex._ideas.generate_and_store_idea_display_title",
                 return_value=StoredDisplayTitle(idea_id="idea-1", skipped_reason="generation_failed"),
             ):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(regenerate_idea_title("idea-1", db=MagicMock(), user={"id": "user-1", "org_id": "org-1"}))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Title generation failed"

    @patch("brain.app.api.routers.cortex._misc.UnitOfWork")
    def test_backfill_titles_threads_authenticated_user_context(self, mock_uow_cls):
        from brain.app.api.routers.cortex._misc import backfill_titles
        from brain.systems.cortex.title_generation import StoredDisplayTitle

        idea_without_title = _make_idea(id="idea-1", title="Raw idea", display_title=None, archived_at=None)

        list_uow = MagicMock()
        list_uow.__enter__.return_value = list_uow
        list_uow.session.scalars.return_value.all.return_value = [idea_without_title]

        mock_uow_cls.return_value = list_uow

        with patch(
            "brain.app.api.routers.cortex._misc.generate_and_store_idea_display_title",
            return_value=StoredDisplayTitle(idea_id="idea-1", title="Generated Title", updated=True),
        ) as mock_generate_title:
            result = backfill_titles(user={"id": "user-1", "org_id": "org-1"})

        assert result == {"ok": True, "generated": 1, "total": 1}
        mock_generate_title.assert_called_once_with(
            "idea-1",
            raw_title="Raw idea",
            user_id="user-1",
            org_id="org-1",
        )

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


class TestStoredIdeaTitleGeneration:
    @patch("brain.systems.cortex.title_generation._publish_generated_display_title")
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_generates_stores_and_publishes_title(self, mock_uow_cls, mock_publish):
        from brain.systems.cortex.title_generation import generate_and_store_idea_display_title

        idea = _make_idea(id="idea-1", title="Raw idea", display_title=None, org_id="org-1")
        read_uow = MagicMock()
        read_uow.__enter__.return_value = read_uow
        read_uow.session.get.return_value = idea
        write_uow = MagicMock()
        write_uow.__enter__.return_value = write_uow
        write_uow.session.execute.return_value.rowcount = 1
        mock_uow_cls.side_effect = [read_uow, write_uow]

        with patch("brain.systems.cortex.title_generation.generate_display_title", return_value="Generated Title") as mock_generate:
            result = generate_and_store_idea_display_title(
                "idea-1",
                raw_title="Raw idea",
                user_id="user-1",
                org_id="org-1",
            )

        assert result.updated is True
        assert result.title == "Generated Title"
        mock_generate.assert_called_once_with(
            "Raw idea",
            user_id="user-1",
            org_id="org-1",
        )
        write_uow.session.execute.assert_called_once()
        mock_publish.assert_called_once_with("idea-1", "Generated Title", org_id="org-1")

    @patch("brain.systems.cortex.title_generation._publish_generated_display_title")
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_does_not_overwrite_existing_display_title(self, mock_uow_cls, mock_publish):
        from brain.systems.cortex.title_generation import generate_and_store_idea_display_title

        idea = _make_idea(id="idea-1", title="Raw idea", display_title="Manual Title", org_id="org-1")
        read_uow = MagicMock()
        read_uow.__enter__.return_value = read_uow
        read_uow.session.get.return_value = idea
        mock_uow_cls.return_value = read_uow

        with patch("brain.systems.cortex.title_generation.generate_display_title") as mock_generate:
            result = generate_and_store_idea_display_title(
                "idea-1",
                raw_title="Raw idea",
                user_id="user-1",
                org_id="org-1",
            )

        assert result.updated is False
        assert result.skipped_reason == "already_titled"
        mock_generate.assert_not_called()
        read_uow.session.execute.assert_not_called()
        mock_publish.assert_not_called()

    @patch("brain.systems.cortex.title_generation._publish_generated_display_title")
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_overwrites_existing_display_title_when_requested(self, mock_uow_cls, mock_publish):
        from brain.systems.cortex.title_generation import generate_and_store_idea_display_title

        idea = _make_idea(id="idea-1", title="Raw idea", display_title="Manual Title", org_id="org-1")
        read_uow = MagicMock()
        read_uow.__enter__.return_value = read_uow
        read_uow.session.get.return_value = idea
        write_uow = MagicMock()
        write_uow.__enter__.return_value = write_uow
        write_uow.session.execute.return_value.rowcount = 1
        mock_uow_cls.side_effect = [read_uow, write_uow]

        with patch("brain.systems.cortex.title_generation.generate_display_title", return_value="Generated Title") as mock_generate:
            result = generate_and_store_idea_display_title(
                "idea-1",
                raw_title="Raw idea",
                user_id="user-1",
                org_id="org-1",
                overwrite=True,
            )

        assert result.updated is True
        assert result.title == "Generated Title"
        mock_generate.assert_called_once_with(
            "Raw idea",
            user_id="user-1",
            org_id="org-1",
        )
        write_uow.session.execute.assert_called_once()
        mock_publish.assert_called_once_with("idea-1", "Generated Title", org_id="org-1")

    @patch("brain.systems.cortex.title_generation._publish_generated_display_title")
    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_does_not_publish_stale_title_write(self, mock_uow_cls, mock_publish):
        from brain.systems.cortex.title_generation import generate_and_store_idea_display_title

        idea = _make_idea(id="idea-1", title="Raw idea", display_title=None, org_id="org-1")
        read_uow = MagicMock()
        read_uow.__enter__.return_value = read_uow
        read_uow.session.get.return_value = idea
        write_uow = MagicMock()
        write_uow.__enter__.return_value = write_uow
        write_uow.session.execute.return_value.rowcount = 0
        mock_uow_cls.side_effect = [read_uow, write_uow]

        with patch("brain.systems.cortex.title_generation.generate_display_title", return_value="Generated Title"):
            result = generate_and_store_idea_display_title(
                "idea-1",
                raw_title="Raw idea",
                user_id="user-1",
                org_id="org-1",
            )

        assert result.updated is False
        assert result.title == "Generated Title"
        assert result.skipped_reason == "stale"
        mock_publish.assert_not_called()
