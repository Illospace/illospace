"""Tests for heuristic graduation and demotion."""
import pytest
from unittest.mock import patch, MagicMock

from brain.systems.feedback.heuristics import (
    graduate_heuristics,
    demote_heuristics,
    GRADUATION_CONFIDENCE,
    GRADUATION_MIN_VALIDATIONS,
    GRADUATION_MIN_SOURCES,
)


@pytest.fixture
def mock_uow():
    with patch("brain.systems.feedback.heuristics.UnitOfWork") as MockUoW:
        mock = MagicMock()
        MockUoW.return_value = mock
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        yield mock


def test_graduate_heuristics_promotes_qualified(mock_uow):
    """Heuristics meeting all criteria should be graduated."""
    qualified = [
        {
            "id": 1,
            "condition": "when debugging timeouts",
            "action": "check connection pool first",
            "confidence": 0.95,
            "validated_count": 12,
            "source_count": 4,
            "demoted_at": None,
        }
    ]
    mock_uow.session.execute.return_value.mappings.return_value.all.return_value = qualified

    result = graduate_heuristics("debugging")
    assert len(result) == 1
    assert result[0]["id"] == 1


def test_graduate_heuristics_skips_recently_demoted(mock_uow):
    """Heuristics demoted within cooldown period should not re-graduate."""
    from datetime import datetime, timezone, timedelta

    recently_demoted = [
        {
            "id": 2,
            "condition": "when X",
            "action": "do Y",
            "confidence": 0.95,
            "validated_count": 15,
            "source_count": 5,
            "demoted_at": datetime.now(timezone.utc) - timedelta(days=2),
        }
    ]
    mock_uow.session.execute.return_value.mappings.return_value.all.return_value = recently_demoted

    result = graduate_heuristics("some_skill")
    assert len(result) == 0  # Should skip -- cooldown not expired


def test_get_active_heuristics_excludes_graduated(mock_uow):
    """get_active_heuristics should not return graduated heuristics."""
    mock_uow.session.execute.return_value.mappings.return_value.all.return_value = []

    from brain.systems.feedback.heuristics import get_active_heuristics
    get_active_heuristics("test_skill")

    # Verify the SQL includes graduated filter
    assert mock_uow.session.execute.called
    sql = str(mock_uow.session.execute.call_args[0][0])
    assert "graduated" in sql.lower()
