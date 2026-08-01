"""SkillIOService — import/export orchestration."""
import pytest
from unittest.mock import MagicMock, patch
from brain.platform.db.services.skill_io import SkillIOService


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.list_active.return_value = []
    repo.get_by_name.return_value = None
    return repo


def test_export_empty(mock_repo):
    service = SkillIOService(mock_repo)
    result = service.export_all()
    assert result == []


@patch("brain.platform.db.services.skill_io.validate_skill_structure", return_value=[])
def test_import_creates_new(mock_validate, mock_repo):
    service = SkillIOService(mock_repo)
    result = service.import_batch([
        {
            "name": "new-skill",
            "procedure": "do things carefully and well " * 3,
            "provider": "openai",
            "model_name": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "service_tier": "priority",
            "auth_mode": "chatgpt",
        }
    ])
    assert result["imported"] == 1
    assert result["skipped"] == 0
    mock_repo.create.assert_called_once()
    _, kwargs = mock_repo.create.call_args
    assert "provider" not in kwargs
    assert "model_name" not in kwargs
    assert "reasoning_effort" not in kwargs
    assert "service_tier" not in kwargs
    assert "auth_mode" not in kwargs
    assert kwargs["thinking_tier"] == "xhigh"


def test_import_skips_invalid(mock_repo):
    service = SkillIOService(mock_repo)
    result = service.import_batch([
        {"name": "", "procedure": ""},
        {"name": "no-proc"},
    ])
    assert result["imported"] == 0
    assert result["skipped"] == 2


@patch("brain.platform.db.services.skill_io.validate_skill_structure", return_value=["too short"])
def test_import_skips_gate_violations(mock_validate, mock_repo):
    service = SkillIOService(mock_repo)
    result = service.import_batch([
        {"name": "bad-skill", "procedure": "x"}
    ])
    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert "too short" in result["errors"][0]


@patch("brain.platform.db.services.skill_io.validate_skill_structure", return_value=[])
def test_import_updates_existing(mock_validate, mock_repo):
    existing = MagicMock()
    existing.description = "old"
    existing.pitfalls = []
    existing.refinements = []
    existing.triggers = []
    existing.provider = None
    existing.model_name = None
    existing.reasoning_effort = None
    existing.service_tier = None
    existing.auth_mode = None
    existing.thinking_tier = "medium"
    mock_repo.get_by_name.return_value = existing

    service = SkillIOService(mock_repo)
    result = service.import_batch([
        {
            "name": "existing-skill",
            "procedure": "updated procedure " * 3,
            "description": "new desc",
            "provider": "openai",
            "model_name": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "service_tier": "priority",
            "auth_mode": "chatgpt",
        }
    ])
    assert result["imported"] == 1
    assert existing.description == "new desc"
    assert existing.provider is None
    assert existing.model_name is None
    assert existing.reasoning_effort is None
    assert existing.service_tier is None
    assert existing.auth_mode is None
    assert existing.thinking_tier == "high"
    mock_repo.create.assert_not_called()


@patch("brain.platform.db.services.skill_io.validate_skill_structure", return_value=[])
def test_import_keeps_only_thinking_tier(mock_validate, mock_repo):
    service = SkillIOService(mock_repo)
    result = service.import_batch([
        {
            "name": "thinking-only-skill",
            "procedure": "do things carefully and well " * 3,
            "thinking_tier": "xhigh",
        }
    ])
    assert result["imported"] == 1
    _, kwargs = mock_repo.create.call_args
    assert kwargs["thinking_tier"] == "xhigh"
    assert "reasoning_effort" not in kwargs


@patch("brain.platform.db.services.skill_io.validate_skill_structure", return_value=[])
def test_import_prefers_thinking_tier_over_legacy_reasoning_field(mock_validate, mock_repo):
    service = SkillIOService(mock_repo)
    result = service.import_batch([
        {
            "name": "conflicting-skill",
            "procedure": "do things carefully and well " * 3,
            "thinking_tier": "xhigh",
            "reasoning_effort": "low",
        }
    ])

    assert result["imported"] == 1
    assert result["skipped"] == 0
    _, kwargs = mock_repo.create.call_args
    assert kwargs["thinking_tier"] == "xhigh"
    assert "reasoning_effort" not in kwargs
