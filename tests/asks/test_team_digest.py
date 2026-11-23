"""Team briefs keep private lanes and ask questions out of a shared channel."""

import asyncio
from datetime import timedelta

import pytest

from illo.brain.client import BrainEvent, ProviderAuth
from illo.db import one
from illo.ledger import tasks
from illo.ledger.digest_snapshot import TEAM_STATE_KEY
from illo.routines.scheduler import tick_routines
from illo.routines.team_digest import DEFAULT_INSTRUCTIONS, send_team_due
from illo.tools.checks import in_grant
from illo.workspace import Routine, TeamDigestConfig
from tests.asks.test_digest import NOW, posts
from tests.tool_results import read_result, snapshot_data


def configure(ctx):
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.timezone = "America/New_York"
    ctx.workspace.config.slack.channels[0].name = "software"
    ctx.workspace.config.digest.team = TeamDigestConfig(channel="software")
    return ctx.workspace.config


async def team_task(conn, workspace, title, *, done=False, due=None):
    viewer = tasks.Viewer("ada", "person:ada", human=True)
    row = await tasks.create(conn, workspace=workspace, viewer=viewer, list_key="launches",
                             title=title, holder="person:ada", due_at=due)
    if done:
        await tasks.done(conn, row["id"], workspace=workspace, viewer=viewer)
        await conn.execute("UPDATE tasks SET done_at=%s WHERE id=%s", (NOW - timedelta(hours=1), row["id"]))
    return row


async def seed_team(ctx):
    async with ctx.pool.connection() as conn:
        await team_task(conn, ctx.workspace, "Check-plan", due=NOW - timedelta(hours=1))


async def test_team_model_principal_credential_single_tool_and_once_per_local_day(
    app, fake_brain, slack_client, monkeypatch
):
    from illo.connectors import chatgpt

    ctx = app.state.ctx
    config = configure(ctx)
    config.brain.auth = "chatgpt"
    config.brain.runtime = "openai_responses"
    config.brain.team_credential = "ben"
    payers = []

    async def access(ctx, person, **kwargs):
        payers.append(person["key"])
        return ProviderAuth(
            kind="chatgpt", access_token="example-token", account_id="example-account"
        )

    monkeypatch.setattr(chatgpt, "access", access)
    fake_brain.script = [{"name": "team_snapshot", "arguments": {}}]
    fake_brain.final_text = "Morning, team. [Check the plan](https://example.com/plan)."
    await seed_team(ctx)
    due = NOW + timedelta(hours=5)
    await send_team_due(ctx, NOW)
    assert not posts(slack_client)
    await asyncio.gather(send_team_due(ctx, due), send_team_due(ctx, due))
    await send_team_due(ctx, due + timedelta(minutes=1))
    assert payers == ["ben"] and len(fake_brain.requests) == 1
    request = fake_brain.requests[0]
    assert request.user_message == DEFAULT_INSTRUCTIONS.replace("{team}", "Acme")
    assert request.provider_auth.kind == "chatgpt"
    assert read_result(fake_brain.results[0])["decision"] == "allow"
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs WHERE id=%s", (request.run_id,))
        event = await one(conn, "SELECT * FROM events WHERE id=%s", (run["event_id"],))
    assert run["principal"] == "team" and run["profile"] == "routine"
    assert run["person_id"] is None and not run["personal_read"] and not run["tainted"]
    assert run["usage"]["credential_person"] == "ben"
    granted = {
        name
        for name, tool in ctx.registry.tools.items()
        if in_grant({**run, "event_channel": event["channel"]}, tool)
    }
    assert {"team_snapshot", "task_list", "task_get", "lists", "shell", "github_get_issue", "search_all"} <= granted
    assert not {"web_fetch", "task_create", "deep_start", "slack_send_dm"} & granted
    assert request.deep and not request.web_search
    assert (
        posts(slack_client)[0]["text"]
        == "Morning, team. <https://example.com/plan|Check the plan>."
    )
    assert len(posts(slack_client)) == 1
    await send_team_due(ctx, due + timedelta(days=1))
    assert len(posts(slack_client)) == 2


@pytest.mark.parametrize("failure", ["error", "timeout", "queue", "missing", "temporary"])
async def test_team_fallback_on_failure_and_timeout(
    app, fake_brain, slack_client, monkeypatch, failure
):
    from illo.brain import run_manager
    from illo.connectors import chatgpt

    ctx = app.state.ctx
    configure(ctx)
    await seed_team(ctx)
    if failure in {"missing", "temporary"}:

        async def auth(*args, **kwargs):
            assert kwargs["notify"] is False
            if failure == "temporary":
                raise chatgpt.TemporaryRefresh(chatgpt.TEMPORARY)
            return None

        monkeypatch.setattr(run_manager, "provider_auth", auth)
    elif failure == "queue":
        ctx.workspace.config.brain.deep.timeout_s = 0.01

        async def wait(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(ctx.gateway.run_manager, "execute", wait)
    else:

        async def fail(request):
            yield BrainEvent(type="started")
            if failure == "timeout":
                raise TimeoutError()
            raise RuntimeError("Example model failure")

        monkeypatch.setattr(fake_brain, "run", fail)
    due = NOW + timedelta(hours=5)
    await send_team_due(ctx, due)
    await send_team_due(ctx, due)
    assert len(posts(slack_client)) == 1
    assert posts(slack_client)[0]["text"].startswith(
        "Morning, team.\nWaiting on people:"
    )
    assert "Stuck:\n• #1 Check-plan: past the due time. Ada must act." in posts(slack_client)[0]["text"]


async def test_empty_team_day_is_recorded_without_model_or_delivery(app, fake_brain, slack_client):
    ctx = app.state.ctx
    configure(ctx)
    due = NOW + timedelta(hours=5)
    async with ctx.pool.connection() as conn:
        from illo.ledger import lanes

        await lanes.upsert(conn, source="t3", external_id="working", title="Working", state="working")
    await send_team_due(ctx, due)
    await seed_team(ctx)
    await send_team_due(ctx, due + timedelta(minutes=1))
    assert not fake_brain.requests and not posts(slack_client)
    async with ctx.pool.connection() as conn:
        assert (
            await one(conn, "SELECT last_status FROM routine_state WHERE key=%s", (TEAM_STATE_KEY,))
        )["last_status"] == "skipped"


@pytest.mark.parametrize("model", [True, False])
async def test_team_retry_uses_frozen_snapshot_text_and_delivery_key(
    app, fake_brain, slack_client, monkeypatch, model
):
    from illo.brain import run_manager

    ctx = app.state.ctx
    configure(ctx)
    await seed_team(ctx)
    if not model:

        async def no_auth(*args, **kwargs):
            return None

        monkeypatch.setattr(run_manager, "provider_auth", no_auth)
    else:
        fake_brain.script = [{"name": "team_snapshot", "arguments": {}}]
        original = fake_brain.run

        async def change(request):
            async with ctx.pool.connection() as conn:
                await conn.execute("UPDATE tasks SET title='Changed after snapshot'")
            async for event in original(request):
                yield event

        monkeypatch.setattr(fake_brain, "run", change)
    attempts = []
    post = slack_client.chat_postMessage

    async def uncertain(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("Example send failure")
        return await post(**kwargs)

    monkeypatch.setattr(slack_client, "chat_postMessage", uncertain)
    due = NOW + timedelta(hours=5)
    with pytest.raises(RuntimeError):
        await send_team_due(ctx, due)
    async with ctx.pool.connection() as conn:
        await conn.execute("TRUNCATE tasks CASCADE")
    await send_team_due(ctx, due)
    assert attempts[0] == attempts[1] and attempts[0]["client_msg_id"]
    assert len(posts(slack_client)) == 1
    if model:
        assert len(fake_brain.requests) == 1
        assert snapshot_data(read_result(fake_brain.results[0])["result"])["stuck"][0]["title"] == "Check-plan"


async def test_team_instruction_file_and_no_separate_routine(app, fake_brain, tmp_path):
    import shutil

    from illo.workspace import load_workspace

    root = tmp_path / "workspace"
    shutil.copytree(app.state.ctx.workspace.root, root)
    text = "Write for {team}. Call team_snapshot."
    (root / "routines/team-digest.md").write_text(text)
    ctx = app.state.ctx
    ctx.workspace = load_workspace(root)
    configure(ctx)
    assert ctx.workspace.team_digest_instructions == text
    assert all(r.path != "routines/team-digest.md" for r in ctx.workspace.routines)
    ctx.workspace.routines.append(
        Routine(
            name="team-digest",
            path="routines/team-digest.md",
            schedule="* * * * *",
            principal="team",
            destination="none",
        )
    )
    await tick_routines(ctx, NOW)
    await seed_team(ctx)
    await send_team_due(ctx, NOW + timedelta(hours=5))
    assert fake_brain.requests[0].user_message == text.replace("{team}", "Acme")
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM events WHERE channel='routine'")


async def test_team_tool_list_and_denials_use_actual_http(app, fake_brain, monkeypatch):
    import httpx

    ctx = app.state.ctx
    configure(ctx)
    await seed_team(ctx)
    fake_brain.script = [
        {"name": name, "arguments": {}}
        for name in ("team_snapshot", "digest_snapshot", "lanes_search", "brief_settings")
    ]
    original = fake_brain.run
    listed = []

    async def check_tools(request):
        async with httpx.AsyncClient(transport=fake_brain.transport) as client:
            response = await client.post(
                request.mcp_url,
                headers={"Authorization": "Bearer " + request.mcp_token},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            listed.extend(t["name"] for t in response.json()["result"]["tools"])
        async for event in original(request):
            yield event

    monkeypatch.setattr(fake_brain, "run", check_tools)
    await send_team_due(ctx, NOW + timedelta(hours=5))
    assert {"team_snapshot", "task_get", "task_list", "lists", "shell"} <= set(listed)
    assert not {"web_fetch", "deep_start", "task_create"} & set(listed)
    from illo.brain.prompts import PROFILE_TEXTS

    prompt = fake_brain.requests[-1].system_prompt
    assert PROFILE_TEXTS["team_digest"] in prompt
    assert "• Channel: Slack channel #software (`C_ACME_TEAM`), mode listen" in prompt
    assert "Open asks" not in prompt
    data = read_result(fake_brain.results[0])["result"]
    assert data.startswith('<outside_data source="team-snapshot">\n')
    assert data.endswith("\n</outside_data>")
    assert [read_result(r)["decision"] for r in fake_brain.results] == [
        "allow",
        "deny",
        "allow",
        "deny",
    ]


async def test_team_tool_keeps_complete_large_snapshot(app, rpc, rpc_token, monkeypatch):

    class Clock:
        @staticmethod
        def now(tz):
            return NOW

    monkeypatch.setattr("illo.tools.digest.datetime", Clock)
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        for i in range(20):
            await team_task(conn, ctx.workspace, ("The complete example title " * 30) + str(i), done=True)
    _, token = rpc_token
    result = read_result(
        (await rpc(token, "tools/call", {"name": "team_snapshot", "arguments": {}})).json()[
            "result"
        ]
    )
    assert result["decision"] == "allow"
    data = snapshot_data(result["result"])
    assert len(data["finished"]) == 7 and data["finished_total"] == 20
    assert all(len(r["title"]) > 700 for r in data["finished"])


async def test_team_channel_delivery_uses_mrkdwn_chunks_and_stable_part_keys(
    app, fake_brain, slack_client
):
    ctx = app.state.ctx
    configure(ctx)
    await seed_team(ctx)
    fake_brain.final_text = (
        "Morning, team.\n" + "[Review the plan](https://example.com/plan).\n" * 200
    )
    await send_team_due(ctx, NOW + timedelta(hours=5))
    sent = posts(slack_client)
    assert len(sent) > 1 and len({p["client_msg_id"] for p in sent}) == len(sent)
    assert all(len(p["text"]) <= 3500 and "[Review" not in p["text"] for p in sent)
    root = next(iter(slack_client.messages))
    assert all(p["thread_ts"] == root for p in sent[1:])
