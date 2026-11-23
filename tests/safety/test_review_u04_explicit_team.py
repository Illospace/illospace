"""Only an explicit team credential may spend a personal subscription on team runs."""

import pytest

from illo.db import one
from illo.gateway.principal import Authority
from tests.chatgpt.conftest import auth_for, save_grant


@pytest.mark.parametrize("fallback", [False, True])
async def test_explicit_team_credential(chatgpt_app, slack_client, fallback):
    app, _ = chatgpt_app
    ctx = app.state.ctx
    ctx.workspace.config.brain.chatgpt.tainted_credential = "team"
    await save_grant(ctx, access="owner-personal")
    ctx.workspace.config.brain.chatgpt.fallback = "openai" if fallback else "none"
    for authority in [
        Authority("team", "team_channel"),
        Authority("team", "tainted", tainted=True),
        Authority("outsider", "outsider"),
    ]:
        for _ in range(2):
            auth = await auth_for(ctx, authority)
            assert auth.kind == "api_key" if fallback else auth is None
    notices = [
        c
        for c in slack_client.calls
        if c.get("text")
        == (
            "I have no model sign-in for team runs. Set brain.chatgpt.team_credential in illo.yaml, or set brain.chatgpt.fallback."
        )
    ]
    assert len(notices) == (0 if fallback else 1)
    async with ctx.pool.connection() as conn:
        assert bool(await one(conn, "SELECT 1 FROM sensors WHERE key='chatgpt:team'")) == (
            not fallback
        )
    ctx.workspace.config.brain.chatgpt.team_credential = "ada"
    auth = await auth_for(ctx, Authority("team", "routine"))
    assert auth.access_token.get_secret_value() == "owner-personal"


def test_subscription_requires_team_credential(tmp_path):
    import shutil

    import yaml

    from illo.workspace import WorkspaceError, load_workspace
    from tests.conftest import ROOT

    root = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", root)
    path = root / "illo.yaml"
    data = yaml.safe_load(path.read_text())
    del data["brain"]["chatgpt"]["team_credential"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WorkspaceError, match="brain.chatgpt.team_credential"):
        load_workspace(root)


@pytest.mark.parametrize(
    "policy,kind", [("none", None), ("team", "chatgpt"), ("api_key", "api_key")]
)
async def test_tainted_credential_is_explicit(chatgpt_app, policy, kind):
    ctx = chatgpt_app[0].state.ctx
    ctx.workspace.config.brain.chatgpt.team_credential = "ada"
    await save_grant(ctx)
    # Set the option only when it exists, so the default proves the old failure.
    if policy != "none":
        ctx.workspace.config.brain.chatgpt.tainted_credential = policy
    ctx.workspace.config.brain.chatgpt.fallback = "openai"
    auth = await auth_for(ctx, Authority("team", "tainted", tainted=True))
    assert (auth.kind if auth else None) == kind
