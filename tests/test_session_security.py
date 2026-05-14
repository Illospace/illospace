"""Tests for agent session user_id validation."""
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


def _make_uow(first_result=None):
    """Create a mock UnitOfWork with a mock session."""
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.session.execute.return_value.mappings.return_value.first.return_value = first_result
    return uow


class TestSessionLoad:
    """Session load must validate user_id ownership."""

    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_load_session_with_user_id(self, MockUoW):
        """Loading a session with user_id adds ownership check to query."""
        uow = _make_uow({
            "messages": json.dumps([{"role": "user", "content": "hello"}]),
            "system_prompt": "You are Illo.",
        })
        MockUoW.return_value = uow

        from brain.systems.sessions import _load_session
        messages, prompt = _load_session("sess-123", user_id="user-abc")

        # Verify execute was called with params including user_id
        call_args = uow.session.execute.call_args
        params = call_args[0][1]
        assert "user-abc" in params.values()
        assert len(messages) == 1

    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_load_session_wrong_user_returns_empty(self, MockUoW):
        """If user_id doesn't match, session is not returned."""
        uow = _make_uow(None)  # No row found
        MockUoW.return_value = uow

        from brain.systems.sessions import _load_session
        messages, prompt = _load_session("sess-123", user_id="wrong-user")
        assert messages == []
        assert prompt is None

    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_load_session_without_user_id_still_works(self, MockUoW):
        """Legacy calls without user_id continue to work (backward compat)."""
        uow = _make_uow({
            "messages": json.dumps([{"role": "user", "content": "hi"}]),
            "system_prompt": None,
        })
        MockUoW.return_value = uow

        from brain.systems.sessions import _load_session
        messages, prompt = _load_session("sess-456")

        call_args = uow.session.execute.call_args
        params = call_args[0][1]
        # Without user_id, only session_id param should be present
        assert "uid" not in params


class TestSessionSave:
    """Session save must store user_id."""

    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_save_session_includes_user_id(self, MockUoW):
        uow = _make_uow()
        MockUoW.return_value = uow

        from brain.systems.sessions import _save_session
        _save_session("sess-789", [{"role": "user", "content": "test"}],
                       "system", 100, 50, 10, 5, user_id="user-xyz")

        call_args = uow.session.execute.call_args
        params = call_args[0][1]
        assert params["uid"] == "user-xyz"

    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_save_session_without_user_id_backward_compat(self, MockUoW):
        """Saving without user_id still works (backward compat)."""
        uow = _make_uow()
        MockUoW.return_value = uow

        from brain.systems.sessions import _save_session
        _save_session("sess-legacy", [{"role": "user", "content": "test"}],
                       "system", 100, 50, 10, 5)

        call_args = uow.session.execute.call_args
        assert call_args is not None
