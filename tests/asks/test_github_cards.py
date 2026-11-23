"""GitHub draft cards show the complete action in readable text."""

import json

import pytest

from illo.asks import cards, drafts
from illo.db import one
from illo.ledger import people
from tests.github.test_tools import REPO, call


def draft(kind="github_assign_issue", **args):
    return {"id": 7, "state": "pending", "kind": kind, "destination": f"github:{REPO}",
            "content": {"tool": kind, "arguments": {"repo": REPO, "number": 12, **args},
                        "github_issue": {"title": "Export <fails>\u202e", "link": f"https://github.com/{REPO}/issues/12",
                                         "assignees": ["ada", "ben"]}}}


@pytest.mark.parametrize("kind,extra,labels", [
    ("github_assign_issue", {"assignee": "ada"}, ["Assign", "Do not assign"]),
    ("github_comment_issue", {"body": "First <line>\nSecond\u200b line"}, ["Send", "Do not send"]),
])
def test_github_action_preview(kind, extra, labels):
    row = draft(kind, **extra)
    preview = cards.draft_preview(row)
    assert "Repository: acme-example/engine" in preview
    assert "|engine#12>" in preview
    assert "> Export &lt;fails&gt;\\u202e" in preview
    assert "Assigned now: `ada`, `ben`" in preview
    assert '"arguments"' not in preview and '"github_issue"' not in preview
    blocks = cards.draft_card(row)
    assert [b["text"]["text"] for b in blocks[-1]["elements"]] == labels
    assert blocks[-1]["elements"][0]["action_id"] == "draft:7:approve"
    assert "\u202e" not in preview and "\u200b" not in preview
    if kind == "github_comment_issue":
        assert "> First &lt;line&gt;\n> Second\\u200b line" in preview
    else:
        assert preview.startswith("Assign <https://github.com/") and "to `ada`?" in preview


async def test_unassigned_preview_and_long_comment_are_complete():
    body = ("start " + "x" * 4000 + " end\n") * 12
    row = draft("github_comment_issue", body=body)
    row["content"]["github_issue"]["assignees"] = []
    assert "Assigned now: nobody" in cards.draft_preview(row)
    blocks = cards.draft_card(row)
    text = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    assert text.count("start ") == 12 and text.count(" end") == 12
    prefix = cards.draft_preview(row).split("> start ", 1)[0]
    assert text.count("x") - prefix.count("x") == 48000
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")
    assert len(blocks) <= 50 and blocks[-1]["type"] == "actions"


@pytest.mark.parametrize("kind", ["github_assign_issue", "github_comment_issue"])
def test_missing_snapshot_refuses(kind):
    row = draft(kind, assignee="ada" if kind == "github_assign_issue" else None, body="Hi")
    del row["content"]["github_issue"]
    assert cards.draft_preview(row) is None
    assert not any(b["type"] == "actions" for b in cards.draft_card(row))


def test_expanded_comment_cannot_hide_unreviewed_text():
    row = draft("github_comment_issue", body="\u200b" * 20001)
    assert cards.draft_preview(row) is None
    assert not any(b["type"] == "actions" for b in cards.draft_card(row))


@pytest.mark.parametrize("kind,extra,verb", [
    ("github_assign_issue", {"assignee": "ben"}, "approve"),
    ("github_comment_issue", {"body": "Reviewed"}, "approve"),
    ("github_assign_issue", {"assignee": "ben"}, "reject"),
])
async def test_old_card_digest_keeps_approval_path(app, rpc, rpc_token, github_client, slack_client, kind, extra, verb):
    issue = await github_client.create_issue(REPO, "Original issue", "Body")
    _, token = rpc_token
    result = await call(rpc, token, kind, {"repo": REPO, "number": issue["number"], **extra})
    assert result["decision"] == "draft"
    await drafts.tick(app.state.ctx)
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts")
        person = await people.get(conn, "ada")
        old_blocks = [cards.section(f"Draft for github:{REPO}"),
                      cards.section("> " + cards.escape(json.dumps(row["content"]))) ]
        await conn.execute("UPDATE drafts SET card_digest=%s WHERE id=%s", (cards.digest(old_blocks), row["id"]))
    await drafts.handle(app.state.ctx, person, {"action_id": f"draft:{row['id']}:{verb}",
                         "channel": row["slack_channel"], "message_ts": row["slack_ts"]})
    async with app.state.ctx.pool.connection() as conn:
        updated = await one(conn, "SELECT * FROM drafts")
    assert updated["state"] == ("sent" if verb == "approve" else "rejected")
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    label = "Assign" if kind == "github_assign_issue" else "Comment on"
    assert posts[-1]["text"] == f"{label} engine#1?"


def test_sent_owner_card_does_not_keep_the_unassigned_status():
    row = draft(assignee="ada")
    row["content"]["outside_ticket"] = {"owner": "Ada", "source": {"kind": "unknown"}}
    row["state"] = "sent"
    text = str(cards.draft_card(row))
    assert "Assigned to Ada." in text
    assert "Nobody is assigned." not in text and "Assigned now: nobody" not in text


def test_card_without_room_for_all_blocks_has_no_approve_button():
    row = draft("github_comment_issue", body=("\u200b" * 234 + "\n") * 60)
    assert cards.draft_preview(row) is None
    assert not any(b["type"] == "actions" for b in cards.draft_card(row))
