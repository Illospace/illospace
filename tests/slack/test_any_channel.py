"""A member channel needs no workspace entry. Private text stays behind membership checks."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from illo.channels.slack import tasks as surface
from illo.channels.slack.access import channel_private
from illo.channels.slack.channels import KEY, member_channels, resolve_channel
from illo.channels.slack.intake import JOIN_NOTICE, MEMBERS_CONTEXT, SlackIntake
from illo.db import all_rows, one
from illo.ledger import items, kv, people, runs, tasks
from illo.sources import search
from illo.tools.registry import ToolContext
from illo.workspace import TaskList, WorkspaceError, load_workspace

CHANNEL = "C_NEW"
PRIVATE = "G_NEW"
TEAM = ["U_ACME_ADA", "U_ACME_BEN", "U_ACME_CHLOE"]


@pytest.fixture
async def member_slack(slack_client):
    async def channels(**kwargs):
        await slack_client._record("users_conversations", **kwargs)
        return {"ok": True, "channels": [
            {"id": CHANNEL, "name": "new", "is_member": True},
            {"id": PRIVATE, "name": "small", "is_member": True},
        ]}

    async def info(**kwargs):
        await slack_client._record("conversations_info", **kwargs)
        return {"ok": True, "channel": {"is_member": True,
                "is_private": kwargs["channel"] == PRIVATE}}

    async def members(**kwargs):
        await slack_client._record("conversations_members", **kwargs)
        return {"members": ["U_ACME_ADA", "U_ACME_ILLO"]}

    slack_client.users_conversations = channels
    slack_client.conversations_info = info
    slack_client.conversations_members = members
    return slack_client


@pytest.mark.parametrize("kind,text", [("message", "<@U_ACME_ILLO> Check the plan"),
                                      ("app_mention", "Check the plan")])
async def test_unlisted_mention_starts_team_run(intake, message, app, fake_brain, member_slack, kind, text):
    result = await intake.receive(message(type=kind, channel=CHANNEL, channel_type="channel", text=text))
    assert result["decision"] == "run" and "error" not in result
    async with app.state.ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs")
        assert run["principal"] == "team" and run["profile"] == "team_channel"
        assert run["person_id"] is None and not run["tainted"]
    assert "You are in the channel #new." in fake_brain.requests[-1].system_prompt


@pytest.mark.parametrize("mode,listed", [("off", False), ("off", True), ("listen", False)])
async def test_unlisted_without_mention_and_disabled_channels(
    intake, message, app, fake_brain, member_slack, mode, listed
):
    ctx = app.state.ctx
    channel = "C_ACME_TEAM" if listed else CHANNEL
    if listed:
        ctx.workspace.config.slack.channel(channel).mode = mode
    else:
        ctx.workspace.config.slack.default_mode = mode
    result = await intake.receive(message(channel=channel, channel_type="channel", text="A plan"))
    assert result is None or result["decision"] == "ignore"
    assert not fake_brain.requests
    if mode == "off":
        result = await intake.receive(message(channel=channel, channel_type="channel", text="<@U_ACME_ILLO> Check"))
        assert result is None


@pytest.mark.parametrize("fields", [
    {"bot_id": "B_OTHER", "subtype": "bot_message"},
    {"bot_profile": {} , "user": None},
    {"workflow_id": "W_FORM"},
    {"user": "U_UNKNOWN"},
    {"user_team": "T_OTHER"},
])
async def test_unlisted_rejects_automation_unknown_and_outside_people(
    intake, message, app, fake_brain, member_slack, fields
):
    result = await intake.receive(message(channel=CHANNEL, channel_type="channel",
                                          text="<@U_ACME_ILLO> Start", **fields))
    assert result is None or result["decision"] == "ignore"
    assert not fake_brain.requests
    async with app.state.ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM runs")


async def test_unlisted_thread_follows_after_restart(intake, message, app, fake_brain, member_slack):
    await intake.receive(message(channel=CHANNEL, channel_type="channel", text="<@U_ACME_ILLO> Start"))
    restarted = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    result = await restarted.receive(message(channel=CHANNEL, channel_type="channel", text="Next step?",
        ts="1700000001.1", thread_ts="1700000000.000001"))
    assert result["decision"] == "run" and len(fake_brain.requests) == 2


async def test_unlisted_thread_does_not_grant_automation_authority(
    intake, message, app, fake_brain, member_slack
):
    member_slack.messages = {
        "100": {"user": "U_UNKNOWN", "text": "Outside instruction", "ts": "100"},
        "101": {"user": "U_ACME_ADA", "text": "Team context", "ts": "101"},
    }
    result = await intake.receive(message(channel=CHANNEL, channel_type="channel",
        text="<@U_ACME_ILLO> Check", thread_ts="1"))
    assert result["decision"] == "run"
    assert "Outside instruction" not in fake_brain.requests[-1].user_message
    assert "Team context" in fake_brain.requests[-1].user_message
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT profile FROM runs"))["profile"] == "team_channel"


async def test_unlisted_private_with_all_active_people_is_team(
    intake, message, app, fake_brain, member_slack
):
    async def members(**kwargs):
        return {"members": [*TEAM, "U_ACME_ILLO"]}
    member_slack.conversations_members = members
    result = await intake.receive(message(channel=PRIVATE, channel_type="group",
                                          text="<@U_ACME_ILLO> Nebula plan"))
    assert result["decision"] == "run"
    assert not await channel_private(app.state.ctx, PRIVATE)
    async with app.state.ctx.pool.connection() as conn:
        assert await one(conn, "SELECT 1 FROM items WHERE kind='slack'")
    assert MEMBERS_CONTEXT not in fake_brain.requests[-1].system_prompt
    assert await search.search(app.state.ctx, "person:ben", "Nebula", kinds=["slack"])


async def test_members_channel_runs_without_shared_text_or_personal_tools(
    intake, message, app, fake_brain, member_slack
):
    ctx = app.state.ctx
    ctx.workspace.lists = [TaskList(key="tasks", name="Tasks")]
    member_slack.messages = {"1": {"user": "U_ACME_ADA", "text": "Nebula private context", "ts": "1"}}
    fake_brain.script = [
        {"name": "slack_read_thread", "arguments": {"channel": PRIVATE, "thread_ts": "1"}},
        {"name": "mail_search", "arguments": {"query": "Nebula"}},
        {"name": "task_create", "arguments": {"list": "tasks", "title": "Check the release"}},
    ]
    result = await intake.receive(message(channel=PRIVATE, channel_type="group", thread_ts="1",
                                          text="<@U_ACME_ILLO> Make a task"))
    assert result["decision"] == "run" and "error" not in result
    assert MEMBERS_CONTEXT in fake_brain.requests[-1].system_prompt
    assert "Nebula private context" in fake_brain.requests[-1].user_message
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM items WHERE kind='slack'")
        run = await one(conn, "SELECT * FROM runs")
        assert run["profile"] == "team_channel" and not run["personal_read"]
        records = await all_rows(conn, "SELECT tool,decision FROM audit ORDER BY id")
        assert [(r["tool"], r["decision"]) for r in records] == [
            ("slack_read_thread", "allow"), ("mail_search", "deny"), ("task_create", "allow")]
        task = await one(conn, "SELECT * FROM tasks")
        assert task["visibility"] == "team" and task["owner_person_id"] is None
        row = await one(conn, "SELECT * FROM events WHERE channel='slack'")
        ref = f"slack-event:{row['id']}"
    assert not await search.search(ctx, "person:ben", "Nebula", kinds=["slack"])
    assert await search.get(ctx, "person:ben", ref) is None
    assert await search.get(ctx, "person:ben", f"slack:{PRIVATE}:1700000000.000001") is None
    async with ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        run = await runs.create(conn, principal="person:ben", person_id=ben["id"],
                                profile="person_dm", runtime="fake", model="fake")
        before = len([c for c in member_slack.calls if c["method"] == "conversations_replies"])
        read = await ctx.registry.call(ToolContext(conn, run, ctx), "slack_read_thread",
                                      {"channel": PRIVATE, "thread_ts": "1"})
        assert read["decision"] == "deny"
        assert len([c for c in member_slack.calls if c["method"] == "conversations_replies"]) == before


@pytest.mark.parametrize("response", [None, {"ok": False}, {"channel": {"is_private": False, "is_member": False}}])
async def test_unlisted_class_failure_records_missed_event(
    intake, message, app, fake_brain, member_slack, response
):
    async def fail(**kwargs):
        if response is None:
            raise RuntimeError("Slack failed")
        return response
    member_slack.conversations_info = fail
    result = await intake.receive(message(channel=CHANNEL, channel_type="channel", text="<@U_ACME_ILLO> Check"))
    assert result["decision"] == "ignore" and result["error"]
    assert not fake_brain.requests
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT * FROM events"))["status"] == "failed"
        assert not await one(conn, "SELECT 1 FROM items WHERE kind='slack'")
        assert (await one(conn, "SELECT * FROM sensors WHERE key=%s", (f"slack:privacy:{CHANNEL}",)))["last_error_at"]


@pytest.mark.parametrize("value", ["new", "#new", CHANNEL])
async def test_list_uses_unlisted_member_name_or_id(app, member_slack, value):
    ctx = app.state.ctx
    entry = TaskList(key="tasks", name="Tasks", slack_channel=value)
    ctx.workspace.lists = [entry]
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
        task = await tasks.create(conn, workspace=ctx.workspace,
            viewer=tasks.Viewer("ada", "person:ada", human=True), list_key="tasks", title="Check the plan")
        result = await surface._thread(ctx, conn, task, entry, person)
        assert result["slack_channel"] == CHANNEL and result["slack_ts"]


async def test_unknown_list_channel_gives_clear_error_and_sensor(app, member_slack):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        with pytest.raises(ValueError, match="Add Illo to the channel and check its name or ID"):
            await resolve_channel(ctx, "missing", conn=conn)
        sensor = await one(conn, "SELECT * FROM sensors WHERE key='slack:channel-name'")
        assert sensor["last_error_at"]
    assert not [c for c in member_slack.calls if c["method"] == "chat_postMessage"]


async def test_join_notice_once_per_channel_and_own_user_only(intake, app, member_slack):
    event = {"event": {"type": "member_joined_channel", "user": "U_ACME_ILLO", "channel": CHANNEL}}
    await asyncio.gather(intake.receive(event), intake.receive(event))
    await SlackIntake(app.state.ctx, "U_ACME_ILLO").receive(event)
    await intake.receive({"event": {**event["event"], "user": "U_ACME_ADA", "channel": PRIVATE}})
    posts = [c for c in member_slack.calls if c["method"] == "chat_postMessage"]
    assert len(posts) == 1 and posts[0]["text"] == JOIN_NOTICE and posts[0]["channel"] == CHANNEL


@pytest.mark.parametrize("viewer,visible", [("ada", True), ("ben", False)])
async def test_connect_status_names_builtin_connections_without_private_name_leak(app, member_slack, viewer, visible):
    ctx = app.state.ctx
    async def auth():
        return {"ok": True, "user_id": "U_ACME_ILLO", "user": "illo"}
    member_slack.auth_test = auth
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, viewer)
        run = await runs.create(conn, principal="person:" + viewer, person_id=person["id"],
                                profile="person_dm", runtime="fake", model="fake")
        result = await ctx.registry.call(ToolContext(conn, run, ctx), "connect_status", {})
        assert result["decision"] == "allow"
        slack = next(c for c in result["result"] if c["kind"] == "slack")
        github = next(c for c in result["result"] if c["kind"] == "github")
        assert slack["connected"] == "yes" and slack["bot"] == "illo"
        assert "#new" in slack["channels"]
        assert ("#small" in slack["channels"]) == visible
        assert ("1 private channel" in slack["channels"]) != visible
        assert slack["use"] == "Add Illo to a channel in Slack, then mention it."
        assert github["connected"] == "yes"
        assert github["repositories"] == ctx.workspace.config.github.repositories


async def test_channel_map_reads_all_pages_and_caches_concurrent_calls(app, member_slack):
    ctx = app.state.ctx
    pages = []
    async def channels(**kwargs):
        pages.append(kwargs.get("cursor"))
        assert kwargs["types"] == "public_channel,private_channel"
        if not kwargs.get("cursor"):
            return {"channels": [{"id": CHANNEL, "name": "new"}],
                    "response_metadata": {"next_cursor": "page2"}}
        return {"channels": [{"id": PRIVATE, "name": "small"}]}
    member_slack.users_conversations = channels
    results = await asyncio.gather(member_channels(ctx), member_channels(ctx))
    assert results == [{CHANNEL: "new", PRIVATE: "small"}] * 2
    assert pages == [None, "page2"]
    async with ctx.pool.connection() as conn:
        cached = await kv.get(conn, KEY)
        cached["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await kv.set(conn, KEY, cached)
    await member_channels(ctx)
    assert pages == [None, "page2", None, "page2"]


async def test_channel_map_failure_has_sensor_and_does_not_repeat_for_ten_minutes(app, member_slack):
    calls = []
    async def fail(**kwargs):
        calls.append(kwargs)
        return {"ok": False}
    member_slack.users_conversations = fail
    for _ in range(2):
        with pytest.raises(ValueError, match="Slack could not read member channels"):
            await member_channels(app.state.ctx)
    assert len(calls) == 1
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT * FROM sensors WHERE key=%s", (KEY,)))["last_error_at"]


def test_defaults_override_and_rule_path_validation(tmp_path):
    import shutil

    from tests.conftest import ROOT

    root = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", root)
    path = root / "illo.yaml"
    data = yaml.safe_load(path.read_text())
    data["slack"].update(default_mode="off", default_rule_files=["rules/team.md"])
    data["digest"]["team"] = {"channel": "new"}
    path.write_text(yaml.safe_dump(data))
    workspace = load_workspace(root)
    default = workspace.config.slack.channel(CHANNEL)
    assert default.mode == "off" and default.rule_files == ["rules/team.md"] and not default.listed
    assert workspace.config.slack.channel("C_ACME_TEAM").mode == "listen"
    assert any("Illo checks it in Slack at run time." in note for note in workspace.warnings)
    data["slack"]["default_rule_files"] = ["../unknown.md"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WorkspaceError, match="slack.default_rule_files"):
        load_workspace(root)


async def test_unknown_task_channel_notifies_requester_once(app, member_slack):
    from tests.tasks.test_slack import returned

    ctx = app.state.ctx
    next(e for e in ctx.workspace.lists if e.key == "launches").slack_channel = "missing"
    row = await returned(ctx)
    await surface.tick(ctx)
    await surface.tick(ctx)
    notices = [c for c in member_slack.calls if c["method"] == "chat_postMessage"]
    assert len(notices) == 1
    assert notices[0]["text"] == "Illo cannot find that channel. Add Illo to the channel and check its name or ID."
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT * FROM tasks WHERE id=%s", (row["id"],)))["slack_ts"] is None
        sensor = await one(conn, "SELECT * FROM sensors WHERE key='slack:channel-name'")
        assert sensor["last_error_at"] and sensor["detail"] == notices[0]["text"]


async def test_team_brief_uses_unlisted_channel_without_a_second_connection(app, member_slack, fake_brain):
    from illo.routines.team_digest import send_team_due
    from tests.asks.test_digest import NOW
    from tests.asks.test_team_digest import configure, seed_team

    ctx = app.state.ctx
    configure(ctx).digest.team.channel = "new"
    await seed_team(ctx)
    fake_brain.script = [{"name": "team_snapshot", "arguments": {}}]
    await asyncio.wait_for(send_team_due(ctx, NOW + timedelta(hours=5)), 5)
    posts = [c for c in member_slack.calls if c["method"] == "chat_postMessage"]
    assert posts and all(c["channel"] == CHANNEL for c in posts)


async def test_private_membership_api_failure_records_missed_event(
    intake, message, app, member_slack, fake_brain
):
    async def fail(**kwargs):
        return {"ok": False, "members": []}
    member_slack.conversations_members = fail
    result = await intake.receive(message(channel=PRIVATE, channel_type="group", text="<@U_ACME_ILLO> Check"))
    assert result["decision"] == "ignore" and result["error"]
    assert not fake_brain.requests
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT * FROM events"))["status"] == "failed"
        assert not await one(conn, "SELECT 1 FROM items WHERE kind='slack'")


async def test_failed_class_check_hides_old_index_text_from_search_and_mcp(app, http, member_slack):
    from tests.mcp.client import AgentClient

    ctx = app.state.ctx
    ref = f"slack:{CHANNEL}:1"
    async with ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref=ref, kind="slack", title="Nebula plan", body="Private old text")
    async def fail(**kwargs):
        raise RuntimeError("Slack failed")
    member_slack.conversations_info = fail
    assert not await search.search(ctx, "person:ben", "Nebula", kinds=["slack"])
    assert await search.get(ctx, "person:ben", ref) is None
    agent = await AgentClient.create(app, http, person="ben")
    result = await agent.call("illo_read", {"capability": "knowledge.get", "arguments": {"source_ref": ref}})
    assert result["items"] == []
    async with ctx.pool.connection() as conn:
        assert await one(conn, "SELECT 1 FROM items WHERE source_ref=%s", (ref,))


async def test_stale_private_item_alias_cannot_bypass_mcp_channel_check(app, http, member_slack):
    from tests.mcp.client import AgentClient

    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        row = await items.upsert(conn, source_ref=f"slack:{PRIVATE}:1", kind="slack",
                                 title="Nebula plan", body="Private text")
        await conn.execute("INSERT INTO item_aliases (alias,item_id) VALUES (%s,%s)", ("old-plan", row["id"]))
    agent = await AgentClient.create(app, http, person="ben")
    result = await agent.call("illo_read", {"capability": "knowledge.get", "arguments": {"source_ref": "old-plan"}})
    assert result["items"] == []
    assert await search.get(ctx, "person:ben", "old-plan") is None


async def test_inactive_person_cannot_start_unlisted_run(intake, message, app, member_slack, fake_brain):
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ada'")
    result = await intake.receive(message(channel=CHANNEL, channel_type="channel", text="<@U_ACME_ILLO> Check"))
    assert result is None and not fake_brain.requests


async def test_listed_private_channel_answers_with_team_profile(intake, message, app, member_slack, fake_brain):
    app.state.ctx.workspace.config.slack.channel("C_ACME_TEAM").visibility = "private"
    member_slack.messages = {"100": {"user": "U_UNKNOWN", "text": "Outside instruction", "ts": "100"}}
    result = await intake.receive(message(channel="C_ACME_TEAM", channel_type="channel",
        text="<@U_ACME_ILLO> Check", thread_ts="100"))
    assert result["decision"] == "run"
    assert MEMBERS_CONTEXT in fake_brain.requests[-1].system_prompt
    assert "Outside instruction" not in fake_brain.requests[-1].user_message
    async with app.state.ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs")
        assert run["profile"] == "team_channel" and not run["tainted"]
        assert not await one(conn, "SELECT 1 FROM items WHERE kind='slack'")


async def test_default_rules_apply_only_to_unlisted_channels(intake, message, app, member_slack, fake_brain):
    from illo.workspace import RuleFile

    ctx = app.state.ctx
    ctx.workspace.rules.append(RuleFile(name="new-channels", path="rules/new.md", body="Check the stated deadline."))
    ctx.workspace.config.slack.default_rule_files = ["rules/new.md"]
    await intake.receive(message(channel=CHANNEL, channel_type="channel", text="<@U_ACME_ILLO> Check"))
    assert "Check the stated deadline." in fake_brain.requests[-1].system_prompt
    assert "Acme has three people." not in fake_brain.requests[-1].system_prompt
    await intake.receive(message(channel="C_ACME_TEAM", channel_type="channel", text="<@U_ACME_ILLO> Check", ts="1700000001.1"))
    assert "Acme has three people." in fake_brain.requests[-1].system_prompt
    assert "Check the stated deadline." not in fake_brain.requests[-1].system_prompt


async def test_connect_status_reports_disconnected_builtins(app):
    ctx = app.state.ctx
    ctx.slack_client = ctx.github_client = None
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"],
                                profile="person_dm", runtime="fake", model="fake")
        result = await ctx.registry.call(ToolContext(conn, run, ctx), "connect_status", {})
        builtins = [c for c in result["result"] if c["kind"] in {"slack", "github"}]
        assert len(builtins) == 2 and all(c["connected"] == "no" for c in builtins)


async def test_members_channel_keeps_no_text_and_no_fault_without_mention(
    intake, message, app, fake_brain, member_slack
):
    ctx = app.state.ctx
    result = await intake.receive(message(channel=PRIVATE, channel_type="group",
                                          text="Nebula private words"))
    assert result == {"decision": "ignore"}
    assert not fake_brain.requests
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM events WHERE channel='slack'")
        assert not await one(conn, "SELECT 1 FROM items WHERE kind='slack'")
        # A private channel with no entry is a normal state. It must not show as a fault.
        sensor = await one(conn, "SELECT * FROM sensors WHERE key=%s", (f"slack:privacy:{PRIVATE}",))
        assert sensor and sensor["last_ok_at"] and not sensor["last_error_at"]
