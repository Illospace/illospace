"""An explicit team sign-in pays for runs without a change to their authority."""

import json

import pytest

from illo.brain.client import BrainEvent
from illo.connectors import chatgpt
from illo.connectors.store import vault
from illo.db import all_rows, one
from illo.gateway import InboundEvent
from illo.ledger import chatgpt as grants
from illo.ledger import people, tokens
from illo.routines.digest import send_due
from illo.routines.scheduler import tick_routines
from tests.asks.test_digest import NOW, posts
from tests.chatgpt.conftest import save_grant
from tests.routines.test_scheduler import configure


@pytest.mark.parametrize("source", ["dm", "agent", "routine", "digest", "team", "tainted"])
async def test_team_grant_pays_for_each_run(
    team_chatgpt_app, replay, fake_brain, slack_client, source
):
    ctx = team_chatgpt_app[0].state.ctx
    await save_grant(ctx, access="ada-model-token")
    if source == "dm":
        await replay(sender="U_ACME_BEN")
    elif source == "agent":
        async with ctx.pool.connection() as conn:
            ben = await people.get(conn, "ben")
            token = await tokens.create(conn, ben["id"], "test agent")
        await ctx.gateway.receive(InboundEvent(
            channel="mcp", external_id="team-agent", kind="request", sender=token,
            text="Read the active lanes.",
        ))
    elif source == "routine":
        configure(ctx, principal="person:ben", destination="person:ben")
        await tick_routines(ctx, NOW)
    elif source == "digest":
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE people SET timezone=CASE WHEN key='ben' THEN 'UTC' ELSE 'America/New_York' END"
            )
        await send_due(ctx, NOW)
        assert len(posts(slack_client)) == 1
    else:
        if source == "tainted":
            ctx.workspace.config.brain.tainted_credential = "team"
        await replay(kind="channel", payload={
            "channel_id": "C_ACME_TEAM", "mentioned": True, "bot": source == "tainted",
        })
    assert len(fake_brain.requests) == 1
    request = fake_brain.requests[0]
    assert request.provider_auth.access_token.get_secret_value() == "ada-model-token"
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs WHERE id=%s", (request.run_id,))
    assert run["usage"]["auth_kind"] == "chatgpt"
    assert run["usage"]["credential_person"] == "ada"
    if source not in {"team", "tainted"}:
        assert run["principal"] == "person:ben"
        assert run["profile"] == ("agent_request" if source == "agent" else "person_dm")


@pytest.mark.parametrize("mode", ["missing", "broken"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_team_missing_names_payer_and_caps_owner_notice(
    team_chatgpt_app, replay, fake_brain, slack_client, monkeypatch, mode, fallback
):
    app, backend = team_chatgpt_app
    ctx = app.state.ctx
    original_open = slack_client.conversations_open

    async def open_dm(**kwargs):
        result = await original_open(**kwargs)
        result["channel"]["id"] = "D_" + kwargs["users"]
        return result

    monkeypatch.setattr(slack_client, "conversations_open", open_dm)
    ctx.workspace.config.brain.api_key_fallback = fallback
    # The notice recipient is the workspace owner, who can differ from the payer.
    ctx.workspace.config.default_owner = "ben"
    if mode == "broken":
        await save_grant(ctx, expires=0)
        backend.broken = True
    results = [await replay(sender="U_ACME_BEN") for _ in range(2)]
    expected = 'Illo has no model sign-in now. Ada must send "connect my ChatGPT" in a DM with Illo.'
    notices = [call for call in slack_client.calls if call.get("text") == expected]
    if fallback:
        assert all(request.provider_auth.kind == "api_key" for request in fake_brain.requests)
        async with ctx.pool.connection() as conn:
            rows = await all_rows(conn, "SELECT usage FROM runs")
        assert all(row["usage"]["auth_kind"] == "api_key" for row in rows)
        assert all("credential_person" not in row["usage"] for row in rows)
        assert not notices
    else:
        assert all(result["text"] == expected for result in results)
        assert not fake_brain.requests
        assert len(notices) == 1 and notices[0]["channel"] == "D_U_ACME_BEN"
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE kv SET value=jsonb_build_object('at',clock_timestamp()-interval '61 minutes') "
                "WHERE key='chatgpt:team-notice'"
            )
        await replay(sender="U_ACME_BEN")
        assert len([call for call in slack_client.calls if call.get("text") == expected]) == 2
    if mode == "broken":
        broken_notices = [call for call in slack_client.calls if call.get("text") == chatgpt.BROKEN]
        assert len(broken_notices) == 1
        assert broken_notices[0]["channel"] == "D_U_ACME_ADA"


@pytest.mark.parametrize("key", ["ada", "ben"])
@pytest.mark.parametrize("command", [
    "connect my ChatGPT", "connect my ChatGPT with a link", "ChatGPT status", "disconnect my ChatGPT",
])
async def test_team_dm_commands(team_chatgpt_app, replay, fake_brain, key, command):
    app, backend = team_chatgpt_app
    ctx = app.state.ctx
    backend.device = True
    ada = await save_grant(ctx)
    await save_grant(ctx, "ben")
    result = await replay(sender="U_ACME_" + key.upper(), text=command)
    assert result["decision"] == "claimed" and not fake_brain.requests
    async with ctx.pool.connection() as conn:
        states = await all_rows(conn, "SELECT person_id FROM connector_oauth_states")
        ada_grant = await grants.load(conn, ada["id"])
    if command.startswith("connect"):
        if key == "ben":
            assert result["text"] == (
                "Illo uses the sign-in of Ada for all runs. You do not need to connect your ChatGPT."
            )
            assert not states and not backend.requests
        else:
            assert [state["person_id"] for state in states] == [ada["id"]]
            assert ("one-time code" if command == "connect my ChatGPT" else "/oauth/authorize?") in result["text"]
    elif command == "ChatGPT status":
        assert result["text"].startswith("Illo uses the sign-in of Ada for all runs. ChatGPT is connected.")
    elif key == "ada":
        assert not ada_grant
        assert result["text"] == "ChatGPT is disconnected from Illo. Illo now has no model sign-in for all members."
    else:
        assert ada_grant
        assert result["text"] == (
            "Your ChatGPT sign-in is disconnected from Illo. Illo uses the sign-in of Ada for all runs."
        )


@pytest.mark.parametrize("mode", ["missing", "broken"])
async def test_team_status_reads_payers_grant(team_chatgpt_app, replay, mode):
    ctx = team_chatgpt_app[0].state.ctx
    await save_grant(ctx, "ben")
    if mode == "broken":
        ada = await save_grant(ctx)
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE connector_grants SET broken_at=clock_timestamp() WHERE person_id=%s", (ada["id"],))
    for key in ("ada", "ben"):
        result = await replay(sender="U_ACME_" + key.upper(), text="ChatGPT status")
        assert result["text"] == "Illo uses the sign-in of Ada for all runs. " + (
            "The ChatGPT sign-in stopped working." if mode == "broken" else "ChatGPT is not connected."
        )


@pytest.mark.parametrize("mode", ["fresh", "broken", "fallback"])
async def test_team_refresh_after_rejected_token(team_chatgpt_app, replay, mode):
    app, backend = team_chatgpt_app
    ctx = app.state.ctx
    ada = await save_grant(ctx, access="ada-rejected-token")
    await save_grant(ctx, "ben", access="ben-unused-token")
    backend.broken = mode != "fresh"
    ctx.workspace.config.brain.api_key_fallback = mode == "fallback"
    requests = []

    class RejectOnce:
        async def run(self, request):
            requests.append(request)
            if len(requests) == 1:
                yield BrainEvent(type="error", category="auth", code="invalid_token")
            else:
                yield BrainEvent(type="final", text="Ready.")

    ctx.brain_client = RejectOnce()
    result = await replay(sender="U_ACME_BEN")
    assert requests[0].provider_auth.access_token.get_secret_value() == "ada-rejected-token"
    assert backend.refresh_count == 1
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs")
        ada_grant = await grants.load(conn, ada["id"])
    assert run["principal"] == "person:ben" and run["token_hash"] is None
    if mode == "broken":
        assert result["text"] == chatgpt.team_missing(ctx.workspace)
        assert len(requests) == 1 and run["status"] == "failed"
    else:
        assert result["text"] == "Ready." and len(requests) == 2
        assert run["status"] == "completed"
        if mode == "fresh":
            assert requests[1].provider_auth.access_token.get_secret_value() == "fresh-access"
            assert json.loads(vault(ctx).decrypt(ada_grant["secret"]))["refresh_token"] == "rotated-refresh"
        else:
            assert requests[1].provider_auth.kind == "api_key"
    assert run["usage"]["auth_kind"] == ("api_key" if mode == "fallback" else "chatgpt")
    assert run["usage"].get("credential_person") == (None if mode == "fallback" else "ada")


async def test_team_status_tool_reads_payers_grant(team_chatgpt_app, replay, fake_brain):
    from tests.tool_results import read_result

    ctx = team_chatgpt_app[0].state.ctx
    await save_grant(ctx)
    fake_brain.script = [{"name": "chatgpt_status", "arguments": {}}]
    await replay(sender="U_ACME_BEN")
    result = read_result(fake_brain.results[0])
    assert result["decision"] == "allow"
    assert result["result"]["message"].startswith(
        "Illo uses the sign-in of Ada for all runs. ChatGPT is connected."
    )


async def test_team_mode_does_not_allow_tainted_runs_by_itself(team_chatgpt_app, replay, fake_brain):
    ctx = team_chatgpt_app[0].state.ctx
    await save_grant(ctx)
    ctx.workspace.config.brain.api_key_fallback = True
    await replay(kind="channel", payload={"channel_id": "C_ACME_TEAM", "mentioned": True, "bot": True})
    assert not fake_brain.requests


async def test_cli_prints_team_mode(team_chatgpt_app, tmp_path, capsys):
    import shutil

    import yaml
    from psycopg.conninfo import make_conninfo

    from illo.cli import execute, parser
    from tests.conftest import ROOT

    ctx = team_chatgpt_app[0].state.ctx
    await save_grant(ctx, access="team-cli-secret-marker")
    target = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", target)
    path = target / "illo.yaml"
    data = yaml.safe_load(path.read_text())
    data["brain"].update(
        auth="chatgpt", runtime="openai_responses", person_credential="team", team_credential="ada",
    )
    path.write_text(yaml.safe_dump(data))
    ctx.config.workspace_dir = target
    async with ctx.pool.connection() as conn:
        ctx.config.database_url = make_conninfo(**conn.info.get_parameters(), password=conn.info.password)
    assert await execute(parser().parse_args(["chatgpt", "status"]), ctx.config) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0] == "Person credential: team; team credential: ada"
    assert "ada  connected" in output and "ben  missing" in output
    assert "team-cli-secret-marker" not in output
