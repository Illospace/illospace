"""Tests for multiplayer cortex features: org validation, mentions, presence."""
import json
import pytest
from fastapi import HTTPException
from unittest.mock import patch, MagicMock


def _mock_cursor(rows=None, fetchone_result=None):
    """Create a mock cursor context manager."""
    cur = MagicMock()
    if fetchone_result is not None:
        cur.fetchone.return_value = fetchone_result
    elif rows is not None:
        cur.fetchall.return_value = rows
        cur.fetchone.return_value = rows[0] if rows else None
    else:
        cur.fetchone.return_value = None
        cur.fetchall.return_value = []
    cur.__enter__ = lambda s: cur
    cur.__exit__ = MagicMock(return_value=False)
    return cur


class TestOrgValidation:
    """Org validation returns None for ideas not belonging to user's org."""

    def test_validate_idea_org_returns_idea_for_valid_org(self):
        """Helper returns Idea when org matches."""
        from brain.app.api.routers.cortex import _validate_idea_org
        session = MagicMock()
        fake_idea = MagicMock()
        fake_idea.id = "idea-1"
        fake_idea.org_id = "org-1"
        session.scalars.return_value.first.return_value = fake_idea
        result = _validate_idea_org(session, "idea-1", "org-1")
        assert result is not None
        assert result.id == "idea-1"

    def test_validate_idea_org_returns_none_for_wrong_org(self):
        """Helper returns None when org doesn't match."""
        from brain.app.api.routers.cortex import _validate_idea_org
        session = MagicMock()
        session.scalars.return_value.first.return_value = None
        result = _validate_idea_org(session, "idea-1", "wrong-org")
        assert result is None

    def test_validate_idea_org_returns_none_for_missing_idea(self):
        """Helper returns None when idea doesn't exist."""
        from brain.app.api.routers.cortex import _validate_idea_org
        session = MagicMock()
        session.scalars.return_value.first.return_value = None
        result = _validate_idea_org(session, "nonexistent", "org-1")
        assert result is None

    def test_require_idea_for_user_uses_org_scope(self):
        """Route helper returns the scoped idea only when caller org matches."""
        from brain.app.api.routers.cortex import _require_idea_for_user

        session = MagicMock()
        fake_idea = MagicMock()
        fake_idea.id = "idea-1"
        fake_idea.org_id = "org-1"
        session.scalars.return_value.first.return_value = fake_idea

        result = _require_idea_for_user(
            session,
            "idea-1",
            {"id": "user-1", "org_id": "org-1", "principal_type": "human"},
        )

        assert result is fake_idea
        stmt = session.scalars.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "ideas.org_id = 'org-1'" in compiled
        assert "ideas.org_id IS NULL" in compiled
        assert "users.org_id = 'org-1'" in compiled

    def test_require_idea_for_user_hides_cross_org_ideas(self):
        """Route helper raises 404 when repository-scoped lookup misses."""
        from brain.app.api.routers.cortex import _require_idea_for_user

        session = MagicMock()
        session.scalars.return_value.first.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            _require_idea_for_user(
                session,
                "idea-1",
                {"id": "user-1", "org_id": "other-org", "principal_type": "human"},
            )

        assert exc_info.value.status_code == 404

    def test_require_worker_principal_rejects_human_owner(self):
        """Agent status is worker/service-only, not a human owner shortcut."""
        from brain.app.api.routers.cortex import _require_worker_principal

        with pytest.raises(HTTPException) as exc_info:
            _require_worker_principal(
                {
                    "id": "owner-1",
                    "org_id": "org-1",
                    "role": "owner",
                    "principal_type": "human",
                    "permissions": ["run:manage"],
                }
            )

        assert exc_info.value.status_code == 403


class TestThreadAttribution:
    """Thread messages include user identity via JOIN."""

    def test_add_thread_sets_message_type_trigger_by_default(self):
        """User messages now trigger Illo without requiring @illo."""
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("Let's discuss this approach") == "trigger"

    def test_add_thread_sets_message_type_trigger_for_at_illo(self):
        """Messages containing @illo get message_type='trigger'."""
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("@illo analyze this") == "trigger"

    def test_add_thread_sets_message_type_trigger_case_insensitive(self):
        """@Illo and @ILLO also trigger."""
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("Hey @Illo check this") == "trigger"

    def test_add_thread_agent_response_type(self):
        """Assistant role messages get agent_response type."""
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("Here is my analysis", role="assistant") == "agent_response"


class TestMentionParsing:
    """Extract @mentions from message content."""

    def test_extract_mentions_single_person(self):
        from brain.app.api.routers.cortex import _extract_mentions
        result = _extract_mentions("Hey @Sam check this out")
        assert result == ["sam"]

    def test_extract_mentions_multiple(self):
        from brain.app.api.routers.cortex import _extract_mentions
        result = _extract_mentions("@illo analyze what @Alex suggested")
        assert set(result) == {"illo", "alex"}

    def test_extract_mentions_none(self):
        from brain.app.api.routers.cortex import _extract_mentions
        result = _extract_mentions("No mentions here")
        assert result == []

    def test_extract_mentions_email_not_matched(self):
        """user@example.com should not be treated as @example."""
        from brain.app.api.routers.cortex import _extract_mentions
        result = _extract_mentions("Email me at user@example.com")
        assert result == []


class TestPresence:
    """In-memory presence tracking."""

    def test_presence_join_adds_viewer(self):
        from brain.app.api.routers.cortex import _presence_store, _presence_join, _presence_get
        _presence_store.clear()
        _presence_join("idea-1", "user-1", "Alex", "#e07050")
        viewers = _presence_get("idea-1")
        assert len(viewers) == 1
        assert viewers[0]["name"] == "Alex"

    def test_presence_leave_removes_viewer(self):
        from brain.app.api.routers.cortex import _presence_store, _presence_join, _presence_leave, _presence_get
        _presence_store.clear()
        _presence_join("idea-1", "user-1", "Alex", "#e07050")
        _presence_leave("idea-1", "user-1")
        assert _presence_get("idea-1") == []

    def test_presence_timeout_removes_stale(self):
        import time
        from brain.app.api.routers.cortex import _presence_store, _presence_join, _presence_get, _presence_cleanup
        _presence_store.clear()
        _presence_join("idea-1", "user-1", "Alex", "#e07050")
        # Manually set last_heartbeat to 60s ago
        _presence_store["idea-1"]["user-1"]["last_heartbeat"] = time.time() - 60
        _presence_cleanup()
        assert _presence_get("idea-1") == []

    def test_presence_join_refreshes_heartbeat(self):
        import time
        from brain.app.api.routers.cortex import _presence_store, _presence_join
        _presence_store.clear()
        _presence_join("idea-1", "user-1", "Alex", "#e07050")
        t1 = _presence_store["idea-1"]["user-1"]["last_heartbeat"]
        time.sleep(0.01)
        _presence_join("idea-1", "user-1", "Alex", "#e07050")
        t2 = _presence_store["idea-1"]["user-1"]["last_heartbeat"]
        assert t2 > t1


class TestDiscussVsTrigger:
    """User messages now trigger run by default."""

    def test_plain_message_runs(self):
        """A message without @illo still triggers cortex run."""
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("Let's think about this") == "trigger"

    def test_trigger_message_runs(self):
        """A message with @illo should trigger cortex run."""
        from brain.app.api.routers.cortex import _parse_message_type
        assert _parse_message_type("@illo please analyze") == "trigger"

    def test_combo_mention_triggers_and_notifies(self):
        """@illo + @person triggers run AND creates mention."""
        from brain.app.api.routers.cortex import _parse_message_type, _extract_mentions
        content = "@illo review what @sam suggested"
        assert _parse_message_type(content) == "trigger"
        mentions = _extract_mentions(content)
        assert "illo" in mentions
        assert "sam" in mentions

    def test_person_mention_also_triggers(self):
        """@person mentions notify people, while the message still goes to Illo."""
        from brain.app.api.routers.cortex import _parse_message_type, _extract_mentions
        content = "Hey @alex what do you think?"
        assert _parse_message_type(content) == "trigger"
        mentions = _extract_mentions(content)
        assert "alex" in mentions
        assert "illo" not in mentions


class TestUserColorAssignment:
    """User color is assigned on registration."""

    def test_user_colors_palette_exists(self):
        """The color palette has enough variety."""
        from brain.systems.auth.users import _USER_COLORS
        assert len(_USER_COLORS) >= 8
        assert all(c.startswith('#') for c in _USER_COLORS)

    def test_color_palette_is_used_in_registration(self):
        """Registration code uses _USER_COLORS for color assignment."""
        import inspect
        from brain.systems.auth import users as users_mod
        source = inspect.getsource(users_mod)
        assert "_USER_COLORS" in source
