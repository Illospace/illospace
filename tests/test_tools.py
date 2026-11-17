"""Test the grant data and each authority check independently."""

from types import SimpleNamespace

import pytest
from pydantic import Field, ValidationError

from illo.channels.mcp.schemas import AskCancel, AskCreate
from illo.db import one
from illo.tools.checks import GRANT_PROFILES, check, destination, risk, scope
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


def test_validation_reason_strips_value_error_prefix():
    with pytest.raises(ValidationError) as caught:
        AskCreate.model_validate(
            {
                "question": "First question? Second question?",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            }
        )
    assert validation_reason(caught.value) == (
        "The tool arguments do not match its schema: "
        "question: The question must be one short sentence"
    )


def test_validation_reason_caps_nested_errors_at_three():
    class Batch(Arguments):
        values: list[int]

    with pytest.raises(ValidationError) as caught:
        Batch.model_validate({"values": ["bad"] * 5})
    assert validation_reason(caught.value) == (
        "The tool arguments do not match its schema: "
        "values.0: Input should be a valid integer; "
        "values.1: Input should be a valid integer; "
        "values.2: Input should be a valid integer (and 2 more)"
    )


def test_validation_reason_handles_a_model_error_without_a_path():
    with pytest.raises(ValidationError) as caught:
        AskCancel.model_validate({})
    assert validation_reason(caught.value) == (
        "The tool arguments do not match its schema: Give ask_id or source_ref"
    )


def test_validation_reason_never_shows_input_values():
    class PrivateArgs(Arguments):
        count: int
        label: str = Field(max_length=3)

    private_text = "acme-private-token-example"
    with pytest.raises(ValidationError) as caught:
        PrivateArgs.model_validate({"count": private_text, "label": private_text})
    reason = validation_reason(caught.value)
    assert reason == (
        "The tool arguments do not match its schema: "
        "count: Input should be a valid integer; "
        "label: String should have at most 3 characters"
    )
    assert private_text not in reason


def test_validation_reason_hides_input_quoted_by_a_validator():
    from illo.tools.personal import Calendar

    private_text = "acme-private-date-example"
    with pytest.raises(ValidationError) as caught:
        Calendar.model_validate({"from": private_text, "to": "2026-01-01T00:00:00Z"})
    reason = validation_reason(caught.value)
    assert reason == (
        "The tool arguments do not match its schema: Invalid isoformat string: '<value>'"
    )
    assert private_text not in reason


def test_validation_reason_caps_and_cleans_path_segments():
    key = "extra\n\t\r\x00\u202e\u2028" + "x" * 80
    with pytest.raises(ValidationError) as caught:
        Arguments.model_validate({key: "acme-private-text"})
    assert validation_reason(caught.value) == (
        "The tool arguments do not match its schema: "
        f"{'extra' + 'x' * 35}: Extra inputs are not permitted"
    )


def test_validation_reason_caps_the_message_and_keeps_the_more_suffix():
    exc = ValidationError.from_exception_data(
        "LongErrors",
        [
            {
                "type": "value_error",
                "loc": ("entries", i),
                "input": "acme-private-text",
                "ctx": {"error": ValueError("Schema requirement. " * 40)},
            }
            for i in range(5)
        ],
    )
    reason = validation_reason(exc)
    assert len(reason) == 400
    assert reason.startswith("The tool arguments do not match its schema: entries.0: ")
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
    with pytest.raises(ValidationError) as caught:
        AskCancel.model_validate({})
    reason = validation_reason(caught.value)
    result = await registry.call(ToolContext(conn, run, None), "ask_cancel", {})
    assert result == {"decision": "deny", "reason": reason, "result": None}
    row = await one(conn, "SELECT decision, reason FROM audit WHERE run_id=%s", (run["id"],))
    assert row == {"decision": "deny", "reason": reason}
