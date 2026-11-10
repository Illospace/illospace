"""Test the grant data and each authority check independently."""

from types import SimpleNamespace

import pytest

from illo.tools.checks import GRANT_PROFILES, check, destination, risk, scope
from illo.tools.registry import Registry, Tool


def tool(name="lanes_search", risk="read", target="ledger", personal=False):
    return SimpleNamespace(name=name, family=name.split("_", 1)[0], risk=risk,
                           destination=lambda a: target, personal=personal)


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
