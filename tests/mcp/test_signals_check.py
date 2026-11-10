import httpx
import pytest

from illo.channels.mcp.check import check
from illo.cli import parser
from illo.db import one
from tests.mcp.client import AgentClient


async def test_signal_deduplication(app, http):
    agent = await AgentClient.create(app, http)
    args = {
        "source": "acme-monitor",
        "dedupe_key": "acme-preview:1",
        "severity": "warning",
        "title": "The preview API failed",
        "body": "Three checks failed.",
        "data": {"checks": 3},
    }
    first = await agent.call("signal_submit", args)
    second = await agent.call("signal_submit", {**args, "body": "A changed duplicate."})
    assert first["signal_id"] == second["signal_id"]
    assert not first["duplicate"] and second["duplicate"]
    await agent.settle()
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM signals"))["n"] == 1
        assert (await one(conn, "SELECT count(*) AS n FROM events"))["n"] == 1
        assert (await one(conn, "SELECT count(*) AS n FROM runs"))["n"] == 1


async def test_check_uses_read_only_calls_and_needs_no_database_config(app, http, tmp_path, capsys):
    agent = await AgentClient.create(app, http)
    path = tmp_path / "token"
    path.write_text(agent.token)
    args = parser().parse_args(["mcp", "check", "--url", "http://test", "--token-file", str(path)])
    assert args.command == "mcp" and args.token_file == path
    assert await check(args.url, path, transport=httpx.ASGITransport(app=app)) == 0
    output = capsys.readouterr().out
    assert "MCP lane read: ok." in output and agent.token not in output
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM lanes"))["n"] == 0
        assert (await one(conn, "SELECT count(*) AS n FROM runs"))["n"] == 0


async def test_check_rejects_bad_token_without_printing_it(app, tmp_path):
    path = tmp_path / "token"
    path.write_text("invalid-token")
    with pytest.raises(ValueError, match="server rejected the token"):
        await check("http://test/mcp", path, transport=httpx.ASGITransport(app=app))
