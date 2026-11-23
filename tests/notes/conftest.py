"""Note tests write only to a temporary workspace."""

import shutil

import pytest

from illo.workspace import load_workspace
from tests.tool_results import read_result


@pytest.fixture
async def note_ctx(app, tmp_path):
    ctx = app.state.ctx
    root = tmp_path / "workspace"
    shutil.copytree(ctx.workspace.root, root)
    ctx.config.workspace_dir = root
    ctx.workspace = load_workspace(root)
    return ctx


@pytest.fixture
async def rpc_token(rpc_token, app):
    run, token = rpc_token
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET profile='agent_request' WHERE id=%s", (run["id"],))
    run["profile"] = "agent_request"
    return run, token


@pytest.fixture
async def proposal(note_ctx, rpc, rpc_token):
    run, token = rpc_token
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "notes_propose",
            "arguments": {
                "title": "Launch decision",
                "text": "Acme ships on Tuesday.",
                "about": "Launch",
            },
        },
    )
    result = read_result(response.json()["result"])
    assert result["decision"] == "allow"
    from illo.db import one

    async with note_ctx.pool.connection() as conn:
        return await one(
            conn, "SELECT * FROM note_proposals WHERE id=%s", (result["result"]["proposal_id"],)
        )
