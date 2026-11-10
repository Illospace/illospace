"""Rule 9: work starts with a ticket and no machine access."""

from tests.github.test_tools import CREATE, call


async def test_rule_09_start_uses_only_github_and_slack(app, rpc, rpc_token, github_client):
    _, token = rpc_token
    assert (await call(rpc, token, "work_start", CREATE))["decision"] == "allow"
    assert "create_issue" in {c["method"] for c in github_client.calls}
    names = app.state.ctx.registry.tools
    assert not any(n.startswith(("shell_", "ssh_", "machine_")) for n in names)
