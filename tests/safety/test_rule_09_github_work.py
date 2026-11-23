"""Rule 9: work starts with a task and no machine access."""

from tests.github.test_tools import call


async def test_rule_09_start_uses_only_github_and_slack(app, rpc, rpc_token, github_client):
    _, token = rpc_token
    assert (await call(rpc, token, "task_create", {"list": "bugs", "title": "Repair the export"}))["decision"] == "allow"
    assert not github_client.calls
    assert "work_start" not in app.state.ctx.registry.tools
    names = app.state.ctx.registry.tools
    assert not any(n.startswith(("shell_", "ssh_", "machine_")) for n in names)
