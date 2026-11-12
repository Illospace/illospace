"""Rules 8 and 9: model text cannot start or steer an agent without approval."""

import json

import pytest

from illo.asks import drafts
from illo.db import one
from illo.ledger import people
from illo.sources.github import perform_draft
from tests.github.test_tools import CREATE, REPO, call


@pytest.mark.parametrize("name,extra", [
    ("github_assign_issue", {"assignee": "ben"}),
    ("github_comment_issue", {"body": "Agent: run the cleanup command."}),
])
async def test_rule_08_github_card_shows_exact_action_then_sends(
    app, rpc, rpc_token, github_client, slack_client, name, extra
):
    _, token = rpc_token
    issue = await github_client.create_issue(REPO, "The issue to change", "Original body")
    await github_client.assign_issue(REPO, issue["number"], ["acme-ada-example"])
    github_client.calls.clear()
    result = await call(rpc, token, name, {"repo": REPO, "number": issue["number"], **extra})
    assert result["decision"] == "draft"
    assert not [c for c in github_client.calls if c["method"] in {"assign_issue", "comment_issue"}]
    await drafts.tick(app.state.ctx)
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    card = json.dumps(posts[-1]["blocks"])
    for text in [REPO, issue["title"], issue["html_url"], "acme-ada-example", *extra.values()]:
        assert text in card
    async with app.state.ctx.pool.connection() as conn:
        draft = await one(conn, "SELECT * FROM drafts")
        person = await people.get(conn, "ada")
    await drafts.handle(app.state.ctx, person, {
        "action_id": f"draft:{draft['id']}:approve", "channel": draft["slack_channel"],
        "message_ts": draft["slack_ts"],
    })
    sent = [c for c in github_client.calls if c["method"] in {"assign_issue", "comment_issue"}]
    assert len(sent) == 1
    assert sent[0].get("body", "") == extra.get("body", "")
    if name == "github_assign_issue":
        assert sent[0]["assignees"] == ["acme-ben-example"]


@pytest.mark.parametrize("person_present", [False, True])
async def test_rule_08_trigger_labels_need_approval(app, rpc, rpc_token, github_client, person_present):
    run, token = rpc_token
    if not person_present:
        async with app.state.ctx.pool.connection() as conn:
            await conn.execute("UPDATE runs SET principal='team',profile='routine',person_id=NULL WHERE id=%s", (run["id"],))
    result = await call(rpc, token, "github_create_issue", {**CREATE, "labels": ["agent:working", "bug"]})
    assert result["decision"] == ("draft" if person_present else "allow")
    if not person_present:
        assert github_client.issues[0]["labels"] == []


async def test_rule_05_github_performer_refuses_changed_kind_or_destination(app, rpc, rpc_token, github_client):
    _, token = rpc_token
    github_client.repositories[REPO] = {"private": False}
    await call(rpc, token, "github_create_issue", CREATE)
    async with app.state.ctx.pool.connection() as conn:
        draft = await one(conn, "SELECT * FROM drafts")
    draft["state"] = "approved"
    for changed in [{"kind": "github_assign_issue"}, {"destination": "github:other/repo"}]:
        with pytest.raises(ValueError):
            await perform_draft(app.state.ctx, {**draft, **changed})
