"""Inline alert assignment buttons, deterministic @Illo assign claim, and pending draft visibility."""

from datetime import UTC, datetime, timedelta

import pytest

from illo.asks import drafts
from illo.channels.slack.intake import SlackIntake
from illo.db import one
from illo.ledger import drafts as drafts_ledger
from illo.ledger import people, runs
from illo.ledger.digest_snapshot import build as build_digest_snapshot
from illo.routines.digest import fallback_text
from tests.github.test_triage import REPO, alert
from tests.tool_results import read_result


def _channel_click(draft_row, action_id, *, user_id="U_ACME_ADA", channel=None, message_ts=None):
    card = draft_row["content"]["outside_ticket"]["channel_card"]
    return {
        "type": "block_actions",
        "user": {"id": user_id},
        "channel": {"id": channel or card["channel"]},
        "message": {"ts": message_ts or card["ts"]},
        "actions": [{"action_id": action_id, "action_ts": "202.001"}],
    }


def test_parse_assign_command_tight_matching():
    parse = drafts.parse_assign_command
    assert parse("<@U_ACME_ILLO> can you assign it?") == (True, None)
    assert parse("<@U_ACME_ILLO> assign") == (True, None)
    assert parse("<@U_ACME_ILLO>, please assign this!") == (True, None)
    assert parse("<@U_ACME_ILLO> could you assign the ticket please?") == (True, None)
    assert parse("<@U_ACME_ILLO> assign it to ben") == (True, "ben")
    assert parse("<@U_ACME_ILLO> assign to <@U_ACME_BEN>") == (True, "<@U_ACME_BEN>")
    assert parse("<@U_ACME_ILLO> assign it to ben please") == (True, "ben")
    assert parse("<@U_ACME_ILLO> assign to me") == (True, "me")

    assert parse("<@U_ACME_ILLO> did a server agent pick it up?") == (False, None)
    assert parse("<@U_ACME_ILLO> why can't you assign it?") == (False, None)
    assert parse("<@U_ACME_ILLO> don't assign it yet") == (False, None)
    assert parse("<@U_ACME_ILLO> assign it to ben or ada?") == (False, None)


async def test_inline_alerts_button_assigns_syncs_both_cards_and_updates_task(
    app, fake_brain, github_client, slack_client
):
    ctx = app.state.ctx
    fake_brain.final_text = "Filed engine#1 for the report. Waiting for an owner."
    result = await alert(app, fake_brain)
    assert result["assignment"] == "waits_for_owner"

    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts WHERE kind='github_assign_issue'")
    card = row["content"]["outside_ticket"]["channel_card"]
    assert card["channel"] == "C_ACME_ALERTS"
    assert card["ts"]

    # Non-owner click or forged coordinates must do nothing.
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    await intake.receive(_channel_click(row, f"outside-assign:{row['id']}:approve", user_id="U_ACME_BEN"))
    await intake.receive(_channel_click(row, f"outside-assign:{row['id']}:approve", channel="C_OTHER"))
    await intake.receive(_channel_click(row, f"outside-assign:{row['id']}:approve", message_ts="999.999"))
    assert not [c for c in github_client.calls if c["method"] == "assign_issue"]

    # Owner click on the #alerts inline button assigns once and syncs both DM and channel cards.
    await intake.receive(_channel_click(row, f"outside-assign:{row['id']}:approve"))
    await intake.receive(_channel_click(row, f"outside-assign:{row['id']}:approve"))
    assigned = [c for c in github_client.calls if c["method"] == "assign_issue"]
    assert len(assigned) == 1
    assert assigned[0]["assignees"] == ["acme-ada-example"]

    async with ctx.pool.connection() as conn:
        updated = await one(conn, "SELECT * FROM drafts WHERE id=%s", (row["id"],))
        audit_row = await one(conn, "SELECT * FROM audit WHERE tool='github_assign_issue' ORDER BY id DESC LIMIT 1")
    assert updated["state"] == "sent"
    assert audit_row["decision"] == "allow"
    assert audit_row["principal"] == "person:ada"

    channel_updates = [
        c for c in slack_client.calls
        if c["method"] == "chat_update" and c.get("channel") == card["channel"] and c.get("ts") == card["ts"]
    ]
    assert any("Assigned to `acme-ada-example`." in str(c.get("blocks")) for c in channel_updates)
    dm_updates = [
        c for c in slack_client.calls
        if c["method"] == "chat_update" and c.get("channel") == row["slack_channel"]
    ]
    assert any("Assigned to Ada." in str(c.get("blocks")) for c in dm_updates)


async def test_inline_alerts_leave_it_button_rejects_and_syncs_cards(
    app, fake_brain, github_client, slack_client
):
    ctx = app.state.ctx
    result = await alert(app, fake_brain)
    assert result["assignment"] == "waits_for_owner"

    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts WHERE kind='github_assign_issue'")
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    await intake.receive(_channel_click(row, f"outside-assign:{row['id']}:reject"))

    assert not [c for c in github_client.calls if c["method"] == "assign_issue"]
    async with ctx.pool.connection() as conn:
        updated = await one(conn, "SELECT * FROM drafts WHERE id=%s", (row["id"],))
    assert updated["state"] == "rejected"
    assert "Left unassigned." in str(slack_client.calls)


async def test_outside_assign_handler_refuses_non_outside_ticket_drafts(app, slack_client):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="person_dm",
            provider="fake",
            tool_limit=12,
            model="fake",
        )
        row = await drafts_ledger.create(
            conn,
            run_id=run["id"],
            approver_person_id=ada["id"],
            kind="slack_post_channel",
            destination="channel:C_ACME_TEAM",
            content={"tool": "slack_post_channel", "arguments": {"channel": "C_ACME_TEAM", "text": "Hi"}},
        )
    await drafts.handle_outside_assign(
        ctx, ada, {"action_id": f"outside-assign:{row['id']}:approve", "channel": "C_ACME_ALERTS", "message_ts": "1.1"}
    )
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT state FROM drafts WHERE id=%s", (row["id"],)))["state"] == "pending"


async def test_deterministic_assign_claim_runs_without_llm_and_blocks_bots_and_non_owners(
    app, fake_brain, github_client, slack_client
):
    ctx = app.state.ctx
    await alert(app, fake_brain, n=5)
    thread_ts = "1700000005.1"
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    brain_before = len(fake_brain.requests)

    # Bot or outside_workspace user cannot trigger the deterministic claim.
    bot_res = await intake.receive({
        "event": {
            "type": "message",
            "channel": "C_ACME_ALERTS",
            "ts": "1700000005.2",
            "thread_ts": thread_ts,
            "subtype": "bot_message",
            "bot_id": "B_MONITOR",
            "text": "<@U_ACME_ILLO> can you assign it?",
        }
    })
    assert bot_res["decision"] != "claimed"
    assert not [c for c in github_client.calls if c["method"] == "assign_issue"]

    # Non-owner team member gets a clear refusal without assigning or calling the LLM.
    brain_before = len(fake_brain.requests)
    ben_res = await intake.receive({
        "event": {
            "type": "message",
            "channel": "C_ACME_ALERTS",
            "ts": "1700000005.3",
            "thread_ts": thread_ts,
            "user": "U_ACME_BEN",
            "text": "<@U_ACME_ILLO> can you assign it?",
        }
    })
    assert ben_res["decision"] == "claimed"
    assert "Only Ada can assign this alert ticket." in ben_res["text"]
    assert len(fake_brain.requests) == brain_before
    assert not [c for c in github_client.calls if c["method"] == "assign_issue"]

    # Verified owner saying "@Illo can you assign it?" assigns deterministically with ZERO LLM calls.
    ada_res = await intake.receive({
        "event": {
            "type": "message",
            "channel": "C_ACME_ALERTS",
            "ts": "1700000005.4",
            "thread_ts": thread_ts,
            "user": "U_ACME_ADA",
            "text": "<@U_ACME_ILLO> can you assign it?",
        }
    })
    assert ada_res["decision"] == "claimed"
    assert "Assigned" in ada_res["text"] and "`acme-ada-example`" in ada_res["text"]
    assert len(fake_brain.requests) == brain_before
    assert len([c for c in github_client.calls if c["method"] == "assign_issue"]) == 1

    # Repeating the command is idempotent and does not call GitHub again.
    repeat_res = await intake.receive({
        "event": {
            "type": "message",
            "channel": "C_ACME_ALERTS",
            "ts": "1700000005.5",
            "thread_ts": thread_ts,
            "user": "U_ACME_ADA",
            "text": "<@U_ACME_ILLO> assign",
        }
    })
    assert repeat_res["decision"] == "claimed"
    assert "Already assigned" in repeat_res["text"]
    assert len([c for c in github_client.calls if c["method"] == "assign_issue"]) == 1


@pytest.mark.parametrize("is_private", [True, False])
async def test_assign_claim_handles_expired_draft_only_for_private_repos(
    app, fake_brain, github_client, is_private
):
    ctx = app.state.ctx
    await alert(app, fake_brain, n=9)
    thread_ts = "1700000009.1"
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE drafts SET state='expired', created_at=now()-interval '25 hours'"
        )
    github_client.repositories[REPO] = {"full_name": REPO, "private": is_private}
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    brain_before = len(fake_brain.requests)
    res = await intake.receive({
        "event": {
            "type": "message",
            "channel": "C_ACME_ALERTS",
            "ts": "1700000009.2",
            "thread_ts": thread_ts,
            "user": "U_ACME_ADA",
            "text": "<@U_ACME_ILLO> assign to ben",
        }
    })
    assert res["decision"] == "claimed"
    assert len(fake_brain.requests) == brain_before
    assigned = [c for c in github_client.calls if c["method"] == "assign_issue"]
    if is_private:
        assert len(assigned) == 1 and assigned[0]["assignees"] == ["acme-ben-example"]
        assert "Assigned" in res["text"] and "`acme-ben-example`" in res["text"]
    else:
        assert not assigned
        assert "public repository" in res["text"]


async def test_pending_drafts_visible_in_task_get_and_digest_snapshot(
    app, rpc, fake_brain, github_client
):
    ctx = app.state.ctx
    ctx.workspace.config.github.task_list = "bugs"
    result = await alert(app, fake_brain, n=12)
    task_id = result["task_id"]
    assert task_id

    # Even a tainted run calling task_get sees the pending outside-ticket assignment draft.
    async with ctx.pool.connection() as conn:
        tainted_run = await runs.create(
            conn,
            principal="team",
            person_id=None,
            profile="tainted",
            tainted=True,
            provider="fake",
            tool_limit=12,
            model="fake",
        )
        tainted_token = await runs.mint_token(conn, tainted_run["id"], 60)
    resp = await rpc(tainted_token, "tools/call", {"name": "task_get", "arguments": {"id": task_id}})
    task_view = read_result(resp.json()["result"])["result"]
    assert len(task_view["pending_drafts"]) == 1
    assert task_view["pending_drafts"][0]["assignee"] == "acme-ada-example"
    assert task_view["pending_drafts"][0]["approver"] == "Ada"

    # Digest snapshot includes the pending draft for the approver, and fallback_text renders it.
    now = datetime.now(UTC)
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ben = await people.get(conn, "ben")
        ada_snap = await build_digest_snapshot(conn, ada, now, ctx.workspace)
        ben_snap = await build_digest_snapshot(conn, ben, now, ctx.workspace)
    assert len(ada_snap["pending_drafts"]) == 1
    assert "Assign engine#1 to acme-ada-example" in ada_snap["pending_drafts"][0]["title"]
    assert not ben_snap["pending_drafts"]

    # Expired drafts (> 24h) do not clutter the snapshot.
    async with ctx.pool.connection() as conn:
        expired_snap = await build_digest_snapshot(conn, ada, now + timedelta(hours=25), ctx.workspace)
    assert not expired_snap["pending_drafts"]

    rendered = fallback_text({
        "name": "Ada",
        "needs_you": [],
        "your_agents": [],
        "finished": [],
        "finished_total": 0,
        "due": [],
        "asks": [],
        "pending_drafts": ada_snap["pending_drafts"],
        "faults": [],
    })
    assert "1 pending draft needs approval." in rendered
    assert "Pending drafts:" in rendered
    assert "Assign engine#1 to acme-ada-example" in rendered
