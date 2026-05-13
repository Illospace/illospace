"""Tests for automatic retrieval feedback (memory-DAG additions)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.systems.memory.retrieval_feedback import (
    BOOST_EXPLICIT,
    BOOST_SUCCESS,
    PENALTY_EXPLICIT,
    PENALTY_FAILURE,
    SALIENCE_CAP,
    SALIENCE_FLOOR,
    _adjust_salience_uow,
    apply_auto_feedback,
    apply_explicit_feedback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(salience: float = 5.0):
    """Return a mock Memory object with settable salience."""
    mem = MagicMock()
    mem.salience = salience
    return mem


def _make_uow(memory=None):
    """Return a mock UnitOfWork whose session.get returns *memory*."""
    uow = MagicMock()
    uow.session.get = AsyncMock(return_value=memory)
    return uow


# ---------------------------------------------------------------------------
# _adjust_salience_uow
# ---------------------------------------------------------------------------


class TestAdjustSalienceUow:
    async def test_boost(self):
        mem = _make_memory(5.0)
        uow = _make_uow(mem)
        await _adjust_salience_uow(uow, 1, 0.05)
        assert mem.salience == pytest.approx(5.05)

    async def test_penalty(self):
        mem = _make_memory(5.0)
        uow = _make_uow(mem)
        await _adjust_salience_uow(uow, 1, -0.03)
        assert mem.salience == pytest.approx(4.97)

    async def test_clamps_to_cap(self):
        mem = _make_memory(9.98)
        uow = _make_uow(mem)
        await _adjust_salience_uow(uow, 1, 0.05)
        assert mem.salience == SALIENCE_CAP

    async def test_clamps_to_floor(self):
        mem = _make_memory(1.01)
        uow = _make_uow(mem)
        await _adjust_salience_uow(uow, 1, -0.05)
        assert mem.salience == SALIENCE_FLOOR

    async def test_missing_memory_no_error(self):
        uow = _make_uow(None)
        await _adjust_salience_uow(uow, 999, 0.05)  # should not raise


# ---------------------------------------------------------------------------
# apply_auto_feedback
# ---------------------------------------------------------------------------


class TestApplyAutoFeedback:
    @patch("brain.systems.memory.retrieval_feedback.UnitOfWork")
    async def test_success_boosts(self, MockUoW):
        mem1 = _make_memory(5.0)
        mem2 = _make_memory(6.0)

        uow = MagicMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)
        uow.session.get = AsyncMock(side_effect=[mem1, mem2])
        uow.pool_stats.record_outcome = AsyncMock()
        MockUoW.return_value = uow

        await apply_auto_feedback([10, 20], ["recency"], success=True)

        assert mem1.salience == pytest.approx(5.0 + BOOST_SUCCESS)
        assert mem2.salience == pytest.approx(6.0 + BOOST_SUCCESS)
        uow.pool_stats.record_outcome.assert_called_once_with(
            "recency", hit=True, org_id=None
        )

    @patch("brain.systems.memory.retrieval_feedback.UnitOfWork")
    async def test_failure_penalizes(self, MockUoW):
        mem = _make_memory(5.0)

        uow = MagicMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)
        uow.session.get = AsyncMock(return_value=mem)
        uow.pool_stats.record_outcome = AsyncMock()
        MockUoW.return_value = uow

        await apply_auto_feedback([10], ["semantic"], success=False)

        assert mem.salience == pytest.approx(5.0 - PENALTY_FAILURE)
        uow.pool_stats.record_outcome.assert_called_once_with(
            "semantic", hit=False, org_id=None
        )

    @patch("brain.systems.memory.retrieval_feedback.UnitOfWork")
    async def test_pool_outcomes_recorded_per_tag(self, MockUoW):
        uow = MagicMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)
        uow.session.get = AsyncMock(return_value=_make_memory(5.0))
        uow.pool_stats.record_outcome = AsyncMock()
        MockUoW.return_value = uow

        await apply_auto_feedback([1], ["recency", "semantic"], success=True)

        assert uow.pool_stats.record_outcome.call_count == 2


# ---------------------------------------------------------------------------
# apply_explicit_feedback
# ---------------------------------------------------------------------------


class TestApplyExplicitFeedback:
    @patch("brain.systems.memory.retrieval_feedback.UnitOfWork")
    async def test_positive(self, MockUoW):
        mem = _make_memory(5.0)
        uow = MagicMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)
        uow.session.get = AsyncMock(return_value=mem)
        MockUoW.return_value = uow

        await apply_explicit_feedback([10], positive=True)

        assert mem.salience == pytest.approx(5.0 + BOOST_EXPLICIT)

    @patch("brain.systems.memory.retrieval_feedback.UnitOfWork")
    async def test_negative(self, MockUoW):
        mem = _make_memory(5.0)
        uow = MagicMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)
        uow.session.get = AsyncMock(return_value=mem)
        MockUoW.return_value = uow

        await apply_explicit_feedback([10], positive=False)

        assert mem.salience == pytest.approx(5.0 - PENALTY_EXPLICIT)
