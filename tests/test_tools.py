"""Test the grant data and each authority check independently."""

from datetime import date, datetime
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError, field_validator

from illo.channels.mcp.schemas import AskCancel, AskCreate
from illo.db import one
from illo.tools.checks import GRANT_PROFILES, check, destination, risk, scope
from illo.tools.personal import Calendar
from illo.tools.registry import Arguments, Registry, Tool, ToolContext, validation_reason


def tool(name="lanes_search", risk="read", target="ledger", personal=False):
    return SimpleNamespace(
        name=name,
        family=name.split("_", 1)[0],
        risk=risk,
        destination=lambda a: target,
        personal=personal,
    )


def authority(**changes):
    return {
        "principal": "person:ada",
        "person_id": 1,
        "profile": "person_dm",
        "tainted": False,
        "personal_read": False,
        **changes,
    }


@pytest.mark.parametrize("profile", GRANT_PROFILES)
def test_profile_lists_only_registered_tools(profile):
    registry = Registry()
    registry.discover()
    run = authority(profile=profile)
    names = {t["name"] for t in registry.list_tools(run)}
    assert names <= registry.tools.keys()
    assert ("people_list" in names) == (profile != "tainted")


def test_scope_denies_missing_grant():
    assert scope(authority(profile="team_channel"), tool("rules_remove_mine")).outcome == "deny"
    assert scope(authority(profile="unknown"), tool()).outcome == "deny"


def test_personal_tool_needs_person_and_untainted_run():
    assert scope(authority(), tool("mail_search", personal=True)).outcome == "allow"
    assert scope(authority(tainted=True), tool("mail_search", personal=True)).outcome == "deny"
    assert (
        scope(
            authority(principal="team", profile="team_channel"), tool("mail_search", personal=True)
        ).outcome
        == "deny"
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("person:ada", "allow"),
        ("ledger", "allow"),
        ("person:ben", "draft"),
        ("channel:C_ACME_TEAM", "draft"),
        ("github:acme-example/engine", "draft"),
        ("outside:ben@example.net", "draft"),
    ],
)
def test_personal_destination_check(target, expected):
    assert (
        destination(
            authority(personal_read=True), tool(risk="write_internal", target=target), {}
        ).outcome
        == expected
    )


def test_risk_outside_and_destructive():
    assert risk(authority(), tool(risk="send_outside")).outcome == "draft"
    assert risk(authority(), tool(risk="delete")).outcome == "deny"
    assert check(authority(), tool(risk="write_internal", target="unknown"), {}).outcome == "deny"


def test_registry_rejects_duplicate_or_destructive_tool():
    async def handler(ctx, args):
        return {}

    registry = Registry()
    entry = Tool("people_list", "people", "read", lambda a: "ledger", False, handler)
    registry.add(entry)
    with pytest.raises(ValueError, match="Duplicate"):
        registry.add(entry)
    with pytest.raises(ValueError, match="risk"):
        registry.add(Tool("delete_everything", "bad", "delete", lambda a: "ledger", False, handler))


PREFIX = "The tool arguments do not match its schema: "
OPTIONS = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]


def reason_for(model, args):
    with pytest.raises(ValidationError) as caught:
        model.model_validate(args)
    return validation_reason(caught.value, model)


def test_validation_reason_shows_an_argument_error():
    reason = reason_for(
        AskCreate, {"question": "First question? Second question?", "options": OPTIONS}
    )
    assert reason == PREFIX + "question: The question must be one short sentence"
    assert "Second question" not in reason


def test_validation_reason_caps_nested_errors_at_three():
    class Batch(Arguments):
        values: list[int]

    assert reason_for(Batch, {"values": ["bad"] * 5}) == PREFIX + (
        "values.0: Input should be a valid integer; "
        "values.1: Input should be a valid integer; "
        "values.2: Input should be a valid integer (and 2 more)"
    )


def test_validation_reason_handles_a_model_error_without_a_path():
    assert reason_for(AskCancel, {}) == PREFIX + "Give ask_id or source_ref"


def test_validation_reason_never_shows_input_values():
    class PrivateArgs(Arguments):
        count: int
        label: str = Field(max_length=3)

    private_text = "acme-private-token-example"
    reason = reason_for(PrivateArgs, {"count": private_text, "label": private_text})
    assert reason == PREFIX + (
        "count: Input should be a valid integer; label: String should have at most 3 characters"
    )
    assert private_text not in reason


@pytest.mark.parametrize("value", ["xyz", "privateZtoken", "private\ntext", "acme\u00e9x"])
def test_validation_reason_hides_a_plain_value_error(value):
    class Parsed(Arguments):
        when: str

        @field_validator("when")
        @classmethod
        def parse(cls, text):
            return datetime.fromisoformat(text.replace("Z", "+00:00"))

    reason = reason_for(Parsed, {"when": value})
    assert reason == PREFIX + "when: The value is not valid (value_error)"


def test_validation_reason_hides_input_in_a_parse_error():
    class Day(BaseModel):
        day: date

    reason = reason_for(Day, {"day": "2026-13-45"})
    assert reason == PREFIX + "day: The value is not valid (date_from_datetime_parsing)"


def test_calendar_dates_fail_with_a_fixed_message():
    reason = reason_for(Calendar, {"from": "acme-private-date", "to": "2026-01-01T00:00:00Z"})
    assert reason == PREFIX + "Use ordered dates with time zones"


def test_validation_reason_hides_keys_that_the_caller_chose():
    class Tags(Arguments):
        tags: dict[str, int]

    private_key = "acme-private-key"
    assert reason_for(Arguments, {private_key: 1}) == (
        PREFIX + "<key>: Extra inputs are not permitted"
    )
    assert reason_for(Tags, {"tags": {private_key: "x"}}) == (
        PREFIX + "tags.<key>: Input should be a valid integer"
    )
    nested = reason_for(AskCreate, {"question": "Q?", "options": [*OPTIONS, {private_key: 1}]})
    assert private_key not in nested
    assert "options.2.<key>: Extra inputs are not permitted" in nested


def test_validation_reason_survives_deep_input():
    deep = "acme-private-text"
    for _ in range(5000):
        deep = [deep]
    reason = reason_for(Arguments, {"acme-private-key": deep})
    assert reason == PREFIX + "<key>: Extra inputs are not permitted"


def test_validation_reason_caps_the_message_and_keeps_the_more_suffix():
    class Batch(Arguments):
        values: list[Literal["x" * 200]]

    reason = reason_for(Batch, {"values": ["acme-private-text"] * 5})
    assert len(reason) == 400
    assert reason.startswith(PREFIX + "values.0: Input should be ")
    assert reason.endswith("... (and 2 more)")
    assert "acme-private-text" not in reason


async def test_registry_validation_uses_the_shared_reason(conn, run):
    async def handler(ctx, args):
        pytest.fail("Invalid arguments must not reach the handler")

    registry = Registry()
    registry.add(
        Tool(
            "ask_cancel",
            "asks",
            "write_internal",
            lambda _: "ledger",
            False,
            handler,
            arguments=AskCancel,
        )
    )
    reason = reason_for(AskCancel, {})
    result = await registry.call(ToolContext(conn, run, None), "ask_cancel", {})
    assert result == {"decision": "deny", "reason": reason, "result": None}
    row = await one(conn, "SELECT decision, reason FROM audit WHERE run_id=%s", (run["id"],))
    assert row == {"decision": "deny", "reason": reason}
