"""A route whose provider or payer does not fit stops the start. A reload keeps the last
valid workspace. This replaces the check of the removed runtime override."""

import shutil

import httpx
import pytest
import yaml

from illo.app import create_app
from illo.config import Config
from tests.conftest import ROOT

CHANGES = [
    ({"routes": {"person_dm": {"provider": "missing"}}}, "the brain has no provider missing"),
    ({"routes": {"deep": {"effort": "max"}}}, "does not accept the effort max"),
    ({"chatgpt": {"team_credential": "ada", "fallback": "anthropic"}}, "brain.chatgpt.fallback"),
    ({"chatgpt": {"team_credential": "ada", "fallback": "chatgpt"}}, "brain.chatgpt.fallback"),
    ({"chatgpt": {}}, "brain.chatgpt.team_credential: ChatGPT needs an explicit person"),
    ({"chatgpt": {"team_credential": "ada", "tainted_credential": "api_key"}},
     "tainted_credential: api_key needs brain.chatgpt.fallback"),
]


def changed_workspace(tmp_path, change):
    root = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", root)
    path = root / "illo.yaml"
    data = yaml.safe_load(path.read_text())
    for key, value in change.items():
        if key == "routes":
            for kind, fields in value.items():
                data["brain"]["routes"].setdefault(kind, {}).update(fields)
        else:
            data["brain"][key] = value
    path.write_text(yaml.safe_dump(data))
    return root


@pytest.mark.parametrize("change,reason", CHANGES)
async def test_u13_startup_refuses_a_route_that_does_not_fit(pool, fake_brain, tmp_path,
                                                            change, reason):
    root = changed_workspace(tmp_path, change)
    app = create_app(Config(workspace_dir=root), pool=pool, brain_client=fake_brain)
    with pytest.raises(ValueError, match=reason):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup must reject the workspace")


@pytest.mark.parametrize("change,reason", CHANGES)
async def test_u13_reload_keeps_last_valid_workspace(app, tmp_path, caplog, change, reason):
    ctx = app.state.ctx
    previous, providers = ctx.workspace, ctx.providers
    ctx.config.workspace_dir = changed_workspace(tmp_path, change)
    with pytest.raises(ValueError, match=reason):
        await ctx.reload()
    assert ctx.workspace is previous and ctx.providers is providers
    assert "keeps the last valid workspace" in caplog.text


async def test_u13_startup_waits_while_the_brain_is_not_reachable(pool, fake_brain, tmp_path,
                                                                 monkeypatch):
    monkeypatch.setattr("illo.app.BRAIN_RETRY_S", 0)
    attempts = []
    describe = fake_brain.providers

    async def providers():
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("The fake brain is still starting")
        return await describe()

    monkeypatch.setattr(fake_brain, "providers", providers)
    app = create_app(Config(workspace_dir=changed_workspace(tmp_path, {})), pool=pool,
                     brain_client=fake_brain)
    async with app.router.lifespan_context(app):
        assert app.state.ctx.providers == fake_brain.catalog
    assert len(attempts) == 3


async def test_u13_reload_refuses_while_the_brain_is_not_reachable(app, monkeypatch):
    ctx = app.state.ctx
    previous = ctx.workspace

    async def providers():
        raise httpx.ConnectError("The fake brain is down")

    monkeypatch.setattr(ctx.brain_client, "providers", providers)
    with pytest.raises(ValueError, match="not reachable"):
        await ctx.reload()
    assert ctx.workspace is previous


def plain(**capabilities):
    """A custom provider on the Responses wire, as the brain describes it."""
    return {"name": "plain", "wire": "responses", "capabilities": {
        "images": True, "web_search": True, "efforts": ["low", "medium", "high"],
        "transcription": False, **capabilities}}


@pytest.mark.parametrize("fallback,change,reason", [
    # A missing grant or a subscription limit sends the deep run (effort high) to the fallback.
    (plain(efforts=["low", "medium"]), {}, "brain.routes.deep: plain does not accept the effort high"),
    (plain(images=False), {}, "brain.chatgpt.fallback: use a provider"),
    (plain(web_search=False), {}, "brain.chatgpt.fallback: use a provider"),
    # The voice provider must transcribe.
    (plain(), {"voice": "brain", "transcription": {"provider": "anthropic", "model": "m"}},
     "brain.transcription: the brain has no provider anthropic with transcription"),
])
def test_u13_the_fallback_and_the_voice_provider_must_serve_their_requests(
        tmp_path, fallback, change, reason):
    from illo.brain.routes import check_routes
    from illo.workspace import load_workspace
    from tests.workspaces import provider_catalog

    root = changed_workspace(tmp_path, {"chatgpt": {"team_credential": "ada", "fallback": "plain"}})
    path = root / "illo.yaml"
    data = yaml.safe_load(path.read_text())
    if change:
        data["voice"] = {**data.get("voice", {}), "transcription": change["voice"]}
        data["brain"]["transcription"] = change["transcription"]
    path.write_text(yaml.safe_dump(data))
    workspace = load_workspace(root)
    with pytest.raises(ValueError, match=reason):
        check_routes(workspace, {**provider_catalog(), "plain": fallback})
    # The same workspace passes with a fallback that serves each request.
    if not change:
        check_routes(workspace, {**provider_catalog(), "plain": plain(efforts=["low", "medium", "high"])})
