"""Rule 3: a personal read cannot silently write to GitHub or the work channel."""

import pytest

from illo.db import one
from illo.tools.registry import Tool
from tests.github.test_tools import CREATE, call


@pytest.mark.parametrize(
    "name", ["github_create_issue", "github_comment_issue", "github_assign_issue"]
)
async def test_rule_03_github_after_personal_read(
    app, rpc, rpc_token, github_client, slack_client, name
):
    _, token = rpc_token

    async def personal(ctx, args):
        return {"body": "Example private data"}

    app.state.ctx.registry.tools.pop("mail_search", None)
    app.state.ctx.registry.add(
        Tool("mail_search", "personal", "read", lambda a: "ledger", True, personal)
    )
    assert (await call(rpc, token, "mail_search", {}))["decision"] == "allow"
    args = (
        CREATE
        if name in {"github_create_issue"}
        else {
            "repo": CREATE["repo"],
            "number": 1,
            **(
                {"body": "Example private data"}
                if name == "github_comment_issue"
                else {"assignee": "ada"}
            ),
        }
    )
    assert (await call(rpc, token, name, args))["decision"] == "draft"
    assert not github_client.issues and not slack_client.calls
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT * FROM drafts"))["approver_person_id"]


async def test_rule_03_private_ledger_item_makes_github_write_a_draft(
    app, rpc, rpc_token, github_client
):
    from illo.ledger import items

    run, token = rpc_token
    async with app.state.ctx.pool.connection() as conn:
        await items.upsert(
            conn,
            source_ref="example:private",
            title="Private fact",
            body="Private team member fact",
            owner_person_id=run["person_id"],
            visibility="private",
        )

    read = await call(rpc, token, "search_get", {"source_ref": "example:private"})
    assert "Private team member fact" in read["result"]
    assert (await call(rpc, token, "github_create_issue", CREATE))["decision"] == "draft"
    assert not github_client.issues


async def test_rule_03_private_lane_read_makes_github_write_a_draft(
    app, rpc, rpc_token, github_client
):
    from illo.ledger import lanes

    run, token = rpc_token
    async with app.state.ctx.pool.connection() as conn:
        lane = await lanes.upsert(
            conn,
            source="manual",
            external_id="example:private-lane",
            title="Private work",
            owner_person_id=run["person_id"],
            visibility="private",
        )
    read = await call(rpc, token, "lanes_get", {"id": lane["id"]})
    assert "Private work" in read["result"]
    assert (await call(rpc, token, "github_create_issue", CREATE))["decision"] == "draft"
    assert not github_client.issues
