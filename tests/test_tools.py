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


@pytest.mark.parametrize("profile", ["team_channel", "routine", "tainted"])
def test_run_without_a_person_has_no_draft_only_tools(profile):
    registry = Registry()
    registry.discover()
    listed = registry.list_tools(authority(principal="team", person_id=None, profile=profile))
    assert not any(registry.tools[t["name"]].risk == "send_outside" for t in listed)
    assert not {"github_assign_issue", "github_comment_issue"} & {t["name"] for t in listed}


@pytest.mark.parametrize("profile", ["person_dm", "agent_request"])
def test_person_run_keeps_assignment_and_comment_tools(profile):
    registry = Registry()
    registry.discover()
    assert {"github_assign_issue", "github_comment_issue"} <= {t["name"] for t in registry.list_tools(authority(profile=profile))}


async def test_google_tools_require_the_run_persons_grant(conn, workspace, ada, ben):
    from illo.connectors.config import Connector
    from illo.tools.registry import ToolContext

    registry = Registry()
    registry.discover()
    app = SimpleNamespace(workspace=workspace.model_copy(deep=True))
    ctx = ToolContext(conn, authority(person_id=ada["id"]), app)
    google = {"mail_search", "mail_get", "calendar_list", "drive_search", "drive_get", "drive_shared_search", "drive_shared_get"}

    async def names():
        return {t["name"] for t in await registry.list_tools_for(ctx)}

    assert not google & await names()
    app.workspace.connectors = [Connector(key="files", kind="google", scope="personal")]
    assert not google & await names()
    await conn.execute("INSERT INTO connector_grants(connector_key,person_id,secret,scopes) VALUES ('files',%s,%s,'{}')", (ben["id"], b"test"))
    assert not google & await names()
    await conn.execute("INSERT INTO connector_grants(connector_key,person_id,secret,scopes) VALUES ('files',%s,%s,'{}')", (ada["id"], b"test"))
    assert google - {"drive_shared_search", "drive_shared_get"} <= await names()
    await conn.execute("UPDATE connector_grants SET revoked_at=now() WHERE person_id=%s", (ada["id"],))
    assert not google & await names()
    app.workspace.connectors = [Connector(key="shared", kind="google", scope="team", drive_id="test-drive", team_grant=True)]
    assert not google & await names()
    await conn.execute("INSERT INTO connector_grants(connector_key,person_id,secret,scopes) VALUES ('shared',NULL,%s,'{}')", (b"test",))
    assert {"drive_shared_search", "drive_shared_get"} <= await names()
    app.workspace.connectors = [Connector(key="shared", kind="google", scope="team", drive_id="test-drive", auth={"type": "service_account", "secret": "test-account"})]
    await conn.execute("DELETE FROM connector_grants")
    assert {"drive_shared_search", "drive_shared_get"} <= await names()


async def test_connector_tools_need_a_service_allowed_for_the_run(conn, workspace):
    from illo.connectors.config import Connector
    from illo.tools.registry import ToolContext

    registry = Registry()
    registry.discover()
    app = SimpleNamespace(workspace=workspace.model_copy(deep=True))
    app.workspace.connectors = []
    ctx = ToolContext(conn, authority(), app)
    async def names():
        return {t["name"] for t in await registry.list_tools_for(ctx)}

    assert not {"connectors_list", "connectors_call"} & await names()
    config = Connector(key="service", kind="mcp", scope="team", url="https://api.example.com/mcp", tools={"read": {"risk": "read"}})
    app.workspace.connectors = [config]
    assert {"connectors_list", "connectors_call"} <= await names()
    ctx.run = authority(profile="tainted", tainted=True, person_id=None, principal="team")
    assert not {"connectors_list", "connectors_call"} & await names()
    config.tools[0].tainted = True
    assert {"connectors_list", "connectors_call"} <= await names()


async def test_http_tool_list_omits_missing_connectors(app, rpc, rpc_token):
    app.state.ctx.workspace = app.state.ctx.workspace.model_copy(update={"connectors": []})
    response = await rpc(rpc_token[1], "tools/list")
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert not {"mail_search", "calendar_list", "drive_search", "drive_shared_search", "connectors_call", "connectors_list"} & names


@pytest.mark.parametrize("channel", ["digest", "team_digest"])
def test_team_brief_grant_uses_only_the_team_snapshot(channel):
    registry = Registry()
    registry.discover()
    run = authority(principal="team", person_id=None, profile="routine", event_channel=channel)
    assert [t["name"] for t in registry.list_tools(run)] == ["team_snapshot"]
