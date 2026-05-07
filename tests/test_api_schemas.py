import pytest
from pydantic import ValidationError
from brain.app.api.schemas.ideas import IdeaCreate, IdeaStatusUpdate
from brain.app.api.schemas.vault import SecretCreate

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

def test_secret_create_requires_value():
    with pytest.raises(ValidationError):
        SecretCreate(key_name="test", value="")
