"""Tests for trust_manager.py — trust level logic."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


def _make_trust(level=0, consecutive=0, total=0):
    return {
        "id": 1, "current_level": level, "consecutive_clean": consecutive,
        "total_completions": total, "total_bounced": 0,
        "total_user_caught": 0, "level_up_threshold": 5,
        "last_demotion_reason": None, "updated_at": "2026-03-04",
    }


@pytest.fixture
def mock_db():
    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_session = mock_uow.session

    # Default: return a trust row
    mock_session.execute.return_value.mappings.return_value.first.return_value = _make_trust()

    with patch("brain.systems.quality.trust.UnitOfWork", return_value=mock_uow):
        yield mock_session


class TestCheckRequirements:
    def test_probation_requires_everything(self, mock_db):
        mock_db.execute.return_value.mappings.return_value.first.return_value = _make_trust(level=0)

        from brain.systems.quality.trust import check_requirements
        req = check_requirements({"involves_code": False})
        assert req["requires_tests"] is True
        assert req["requires_verification"] is True
        assert "PROBATION" in req["reason"]

    def test_standard_code_requires_tests(self, mock_db):
        mock_db.execute.return_value.mappings.return_value.first.return_value = _make_trust(level=1)

        from brain.systems.quality.trust import check_requirements
        req = check_requirements({"involves_code": True})
        assert req["requires_tests"] is True

    def test_standard_noncode_skips_tests(self, mock_db):
        mock_db.execute.return_value.mappings.return_value.first.return_value = _make_trust(level=1)

        from brain.systems.quality.trust import check_requirements
        req = check_requirements({"involves_code": False})
        assert req["requires_tests"] is False

    def test_trusted_trivial_skips(self, mock_db):
        mock_db.execute.return_value.mappings.return_value.first.return_value = _make_trust(level=2)

        from brain.systems.quality.trust import check_requirements
        req = check_requirements({"involves_code": True, "lines_changed": 3, "config_only": True})
        assert req["requires_tests"] is False

    def test_trusted_nontrivial_requires_tests(self, mock_db):
        mock_db.execute.return_value.mappings.return_value.first.return_value = _make_trust(level=2)

        from brain.systems.quality.trust import check_requirements
        req = check_requirements({"involves_code": True, "lines_changed": 20})
        assert req["requires_tests"] is True


class TestGetTrustLevel:
    def test_returns_level_name(self, mock_db):
        mock_db.execute.return_value.mappings.return_value.first.return_value = _make_trust(level=1)

        from brain.systems.quality.trust import get_trust_level
        t = get_trust_level()
        assert t["level_name"] == "STANDARD"

    def test_empty_returns_probation(self, mock_db):
        mock_db.execute.return_value.mappings.return_value.first.return_value = None

        from brain.systems.quality.trust import get_trust_level
        t = get_trust_level()
        assert t["level_name"] == "PROBATION"
