"""Rule 4 and decision 2: public repositories are outside destinations."""

import pytest

from illo.db import one
from tests.github.test_tools import CREATE, REPO, call


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("github_create_issue", CREATE),
        ("github_assign_issue", {"repo": REPO, "number": 1, "assignee": "ada"}),
        ("github_comment_issue", {"repo": REPO, "number": 1, "body": "Example"}),
        ("work_start", CREATE),
    ],
)
async def test_rule_04_public_write_becomes_draft(
    app, rpc, rpc_token, github_client, slack_client, name, args
):
    run, token = rpc_token
    github_client.repositories[REPO] = {"private": False}
    result = await call(rpc, token, name, args)
    assert result["decision"] == "draft"
    assert not [c for c in github_client.calls if c["method"] != "get_repository"]
    assert not slack_client.calls
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts")
        assert row["approver_person_id"] == run["person_id"]
        assert row["destination"] == f"github:{REPO}"
        assert row["content"]["tool"] == name
        assert (await one(conn, "SELECT decision FROM audit"))["decision"] == "draft"


async def test_rule_04_public_write_without_person_is_denied(app, rpc, rpc_token, github_client):
    run, token = rpc_token
    github_client.repositories[REPO] = {"private": False}
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET principal='team',profile='routine',person_id=NULL WHERE id=%s",
            (run["id"],),
        )
    assert (await call(rpc, token, "github_create_issue", CREATE))["decision"] == "deny"
    assert not github_client.issues


async def test_rule_04_unknown_visibility_is_denied(rpc, rpc_token, github_client):
    _, token = rpc_token
    github_client.repositories[REPO] = {}
    assert (await call(rpc, token, "github_create_issue", CREATE))["decision"] == "deny"
    assert not github_client.issues


async def test_rule_04_allowlist_checked_before_personal_draft(app, rpc, rpc_token, github_client):
    run, token = rpc_token
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    assert (await call(rpc, token, "github_create_issue", {**CREATE, "repo": "other/repo"}))[
        "decision"
    ] == "deny"
    assert not github_client.calls
