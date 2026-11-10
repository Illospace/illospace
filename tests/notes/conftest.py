"""Note tests write only to a temporary workspace."""

import shutil

import pytest

from illo.workspace import load_workspace


@pytest.fixture
async def note_ctx(app, tmp_path):
    ctx = app.state.ctx
    root = tmp_path / "workspace"
    shutil.copytree(ctx.workspace.root, root)
    ctx.config.workspace_dir = root
    ctx.workspace = load_workspace(root)
    return ctx


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
    result = response.json()["result"]["structuredContent"]
    assert result["decision"] == "allow"
    from illo.db import one

    async with note_ctx.pool.connection() as conn:
        return await one(
            conn, "SELECT * FROM note_proposals WHERE id=%s", (result["result"]["proposal_id"],)
        )
