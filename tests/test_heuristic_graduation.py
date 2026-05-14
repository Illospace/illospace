"""Tests for heuristic graduation and demotion."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

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
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=False)
        yield mock


async def test_graduate_heuristics_promotes_qualified(mock_uow):
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
    result = MagicMock()
    result.mappings.return_value.all.return_value = qualified
    mock_uow.session.execute = AsyncMock(return_value=result)

    graduated = await graduate_heuristics("debugging")
    assert len(graduated) == 1
    assert graduated[0]["id"] == 1


async def test_graduate_heuristics_skips_recently_demoted(mock_uow):
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
    result = MagicMock()
    result.mappings.return_value.all.return_value = recently_demoted
    mock_uow.session.execute = AsyncMock(return_value=result)

    graduated = await graduate_heuristics("some_skill")
    assert len(graduated) == 0  # Should skip -- cooldown not expired


async def test_get_active_heuristics_excludes_graduated(mock_uow):
    """get_active_heuristics should not return graduated heuristics."""
    result = MagicMock()
    result.mappings.return_value.all.return_value = []
    mock_uow.session.execute = AsyncMock(return_value=result)

    from brain.systems.feedback.heuristics import get_active_heuristics
    await get_active_heuristics("test_skill")

    # Verify the SQL includes graduated filter
    assert mock_uow.session.execute.called
    sql = str(mock_uow.session.execute.call_args[0][0])
    assert "graduated" in sql.lower()
