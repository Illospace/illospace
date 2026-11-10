"""Rule 5: ask text cannot hide format controls from the owner."""

import pytest
from pydantic import ValidationError

from illo.channels.mcp.schemas import AskCreate
from illo.tools.asks import CreateArgs


@pytest.mark.parametrize("schema", [AskCreate, CreateArgs])
@pytest.mark.parametrize("field", ["question", "label", "detail", "proof"])
@pytest.mark.parametrize("control", ["\u202e", "\u200b"])
def test_rule_05_ask_refuses_format_controls(schema, field, control):
    args = {
        "owner": "ada",
        "question": "Which check first?",
        "options": [
            {"id": "tests", "label": "Tests", "detail": "Run the tests."},
            {"id": "lint", "label": "Lint"},
        ],
        "proof": {"text": "A check is required."},
    }
    if field == "question":
        args["question"] += control
    elif field == "proof":
        args["proof"]["text"] += control
    else:
        args["options"][0][field] += control
    with pytest.raises(ValidationError, match="format control"):
        schema.model_validate(args)
