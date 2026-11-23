"""The workspace cannot request more tool calls than the brain service accepts."""

import pytest
from pydantic import ValidationError

from brain.models import RunRequest
from illo.workspace import RouteSettings


def test_rule_04_a_route_cannot_ask_for_more_tool_calls_than_the_brain_accepts():
    brain_limit = RunRequest.model_fields["max_tool_calls"].metadata
    assert any(getattr(item, "le", None) == 200 for item in brain_limit)
    with pytest.raises(ValidationError, match="200"):
        RouteSettings(max_tool_calls=201)
    assert RouteSettings(max_tool_calls=200).max_tool_calls == 200
