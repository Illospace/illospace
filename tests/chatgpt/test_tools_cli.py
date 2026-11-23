"""Sign-in tools send the link only to the requester. The CLI lists no secrets."""

from argparse import Namespace

import pytest
from psycopg.conninfo import make_conninfo

from illo.cli import execute, parser
from illo.db import one
from tests.chatgpt.conftest import save_grant


@pytest.mark.parametrize("name", ["chatgpt_connect", "chatgpt_disconnect"])
async def test_model_cannot_change_signin(chatgpt_app, replay, fake_brain, slack_client, name):
    app, _ = chatgpt_app
    await save_grant(app.state.ctx)
    fake_brain.script = [{"name": name, "arguments": {}}]
    await replay(payload={"channel_id": "D_ADA"}, reply_to="thread-a")
    assert not [c for c in slack_client.calls if "https://auth.openai.com/" in c.get("text", "")]
    async with app.state.ctx.pool.connection() as conn:
        assert await one(conn, "SELECT 1 FROM connector_grants WHERE connector_key='chatgpt'")
        assert not await one(conn, "SELECT 1 FROM connector_oauth_states")
        assert not await one(
            conn, "SELECT 1 FROM audit WHERE tool LIKE 'chatgpt_%' AND decision='allow'"
        )


async def test_operator_status_has_no_secret(chatgpt_app, capsys):
    app, _ = chatgpt_app
    ctx = app.state.ctx
    await save_grant(ctx, access="secret-cli-marker")
    async with ctx.pool.connection() as conn:
        ctx.config.database_url = make_conninfo(
            **conn.info.get_parameters(), password=conn.info.password
        )
    assert parser().parse_args(["chatgpt", "status"]) == Namespace(
        command="chatgpt", action="status"
    )
    assert await execute(parser().parse_args(["chatgpt", "status"]), ctx.config) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0] == "Person credential: own; team credential: none"
    assert "ada  connected" in output and "ben  missing" in output
    assert "secret-cli-marker" not in output and "fake-refresh" not in output


@pytest.mark.parametrize("value", ["not-a-person", "inactive"])
def test_invalid_team_credential_rejected(tmp_path, value):
    import shutil

    from illo.workspace import WorkspaceError, load_workspace
    from tests.conftest import ROOT

    target = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", target)
    path = target / "illo.yaml"
    text = path.read_text().replace(
        "  runtime: echo", "  runtime: echo\n  team_credential: " + value
    )
    # The example has a runtime key. Keep this test tied to its actual config.
    if text == path.read_text():
        text = path.read_text().replace("brain:\n", "brain:\n  team_credential: " + value + "\n")
    path.write_text(text)
    with pytest.raises(WorkspaceError, match="brain.team_credential"):
        load_workspace(target)
