"""Only an explicit team credential may spend a personal subscription on team runs."""

import pytest

from illo.brain.run_manager import provider_auth
from illo.db import one
from illo.gateway.principal import Authority
from tests.chatgpt.conftest import save_grant


@pytest.mark.parametrize("fallback", [False, True])
async def test_explicit_team_credential(chatgpt_app, slack_client, fallback):
    app, _ = chatgpt_app
    ctx = app.state.ctx
    ctx.workspace.config.brain.tainted_credential = "team"
    await save_grant(ctx, access="owner-personal")
    ctx.workspace.config.brain.api_key_fallback = fallback
    for authority in [
        Authority("team", "team_channel"),
        Authority("team", "tainted", tainted=True),
        Authority("outsider", "outsider"),
    ]:
        for _ in range(2):
            auth = await provider_auth(ctx, authority, "openai_responses")
            assert auth.kind == "api_key" if fallback else auth is None
    notices = [
        c
        for c in slack_client.calls
        if c.get("text")
        == (
            "I have no model sign-in for team runs. Set brain.team_credential in illo.yaml, or allow the API key."
        )
    ]
    assert len(notices) == (0 if fallback else 1)
    async with ctx.pool.connection() as conn:
        assert bool(await one(conn, "SELECT 1 FROM sensors WHERE key='chatgpt:team'")) == (
            not fallback
        )
    ctx.workspace.config.brain.team_credential = "ada"
    auth = await provider_auth(ctx, Authority("team", "routine"), "openai_responses")
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
    data["brain"].update(auth="chatgpt", runtime="openai_responses")
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WorkspaceError, match="brain.team_credential"):
        load_workspace(root)


@pytest.mark.parametrize(
    "policy,kind", [("none", None), ("team", "chatgpt"), ("api_key", "api_key")]
)
async def test_tainted_credential_is_explicit(chatgpt_app, policy, kind):
    ctx = chatgpt_app[0].state.ctx
    ctx.workspace.config.brain.team_credential = "ada"
    await save_grant(ctx)
    # Set the option only when it exists, so the default proves the old failure.
    if policy != "none":
        ctx.workspace.config.brain.tainted_credential = policy
    ctx.workspace.config.brain.api_key_fallback = True
    auth = await provider_auth(ctx, Authority("team", "tainted", tainted=True), "openai_responses")
    assert (auth.kind if auth else None) == kind
