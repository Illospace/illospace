"""The workspace cannot request more turns than the brain service accepts."""

import pytest
from pydantic import ValidationError

from illo.workspace import BrainConfig


def test_rule_04_workspace_refuses_more_than_fifty_brain_turns():
    with pytest.raises(ValidationError, match="50"):
        BrainConfig(max_turns=51)
    assert BrainConfig(max_turns=50).max_turns == 50
