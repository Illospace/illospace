import pytest
from pydantic import ValidationError
from brain.app.api.schemas.ideas import IdeaCreate, IdeaStatusUpdate
from brain.app.api.schemas.vault import SecretCreate
from brain.systems.cortex.status import IDEA_STATUS_VALUES
from brain.systems.runs.tool_definitions import CORTEX_IDEA_TOOLS

def test_idea_create_requires_title():
    with pytest.raises(ValidationError):
        IdeaCreate(title="")

def test_idea_create_valid():
    idea = IdeaCreate(title="Test idea")
    assert idea.title == "Test idea"
    assert idea.status == "emerged"

def test_idea_status_update_validates_status():
    with pytest.raises(ValidationError):
        IdeaStatusUpdate(status="invalid_status")

def test_idea_status_update_valid():
    update = IdeaStatusUpdate(status="working")
    assert update.status == "working"


@pytest.mark.parametrize("status", ["building", "testing"])
def test_idea_status_update_accepts_canonical_expanded_statuses(status):
    update = IdeaStatusUpdate(status=status)
    assert update.status == status


def test_manage_idea_tool_status_enum_matches_canonical_statuses():
    manage_idea_tool = next(tool for tool in CORTEX_IDEA_TOOLS if tool["name"] == "manage_idea")
    assert manage_idea_tool["input_schema"]["properties"]["status"]["enum"] == list(
        IDEA_STATUS_VALUES
    )

def test_secret_create_requires_value():
    with pytest.raises(ValidationError):
        SecretCreate(key_name="test", value="")
