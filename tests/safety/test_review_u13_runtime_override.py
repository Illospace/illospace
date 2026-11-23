"""Reject an incompatible effective runtime before the app accepts runs."""

import shutil

import pytest
import yaml

from illo.app import create_app
from illo.config import Config
from tests.conftest import ROOT


def subscription_workspace(tmp_path):
    root = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", root)
    path = root / "illo.yaml"
    data = yaml.safe_load(path.read_text())
    data["brain"].update(auth="chatgpt", runtime="openai_responses", team_credential="ada")
    path.write_text(yaml.safe_dump(data))
    return root


@pytest.mark.parametrize("override", ["claude", "echo", "openai_compat"])
async def test_u13_startup_refuses_runtime_override(pool, tmp_path, override):
    root = subscription_workspace(tmp_path)
    app = create_app(Config(workspace_dir=root, brain_runtime=override), pool=pool)
    with pytest.raises(ValueError, match="ChatGPT needs openai_responses"):
        async with app.router.lifespan_context(app):
            pass


async def test_u13_reload_keeps_last_valid_workspace(app, tmp_path):
    ctx = app.state.ctx
    previous = ctx.workspace
    ctx.config.brain_runtime = "claude"
    ctx.config.workspace_dir = subscription_workspace(tmp_path)
    with pytest.raises(ValueError, match="ChatGPT needs openai_responses"):
        await ctx.reload()
    assert ctx.workspace is previous
