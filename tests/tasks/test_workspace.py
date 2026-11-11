"""List and agent files are team context and reload with the other files."""

import shutil
from pathlib import Path

import pytest

from illo.cli import parser
from illo.workspace import WorkspaceError, fingerprint, load_workspace

ROOT = Path(__file__).parents[2]


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / "workspace.example", tmp_path / "workspace")
    return tmp_path / "workspace"


def test_lists_agents_and_default_list(root):
    workspace = load_workspace(root)
    assert {entry.key for entry in workspace.lists} == {"bugs", "launches"}
    assert workspace.agents[0].wake.type == "poll"
    assert workspace.warnings == []
    shutil.rmtree(root / "lists")
    (root / "agents.yaml").unlink()
    workspace = load_workspace(root)
    assert workspace.lists[0].key == "tasks" and workspace.lists[0].done == "person"
    assert not workspace.agents


@pytest.mark.parametrize("file,text,reason", [
    ("agents.yaml", "agents: [{key: Bad, name: Build, owner: ada}]", "key"),
    ("agents.yaml", "agents: [{key: build, name: Build, owner: missing}]", "owner"),
    ("agents.yaml", "agents: [{key: build, name: Build, owner: ada, wake: {type: remote}}]", "wake"),
    ("agents.yaml", "agents: [{key: build, name: Build, owner: ada}, {key: build, name: Build, owner: ada}]", "duplicate"),
    ("lists/bad.md", "---\nname: Bad\nholders: [nobody]\n---\nText", "holders"),
    ("lists/bad.md", "---\nname: Bad\ndone: []\n---\nText", "done"),
    ("lists/bad.md", "---\ndescription: No name\n---\nText", "name"),
    ("lists/bad.md", "---\nname: Bad\nsilence: {agent_hours: 0}\n---\nText", "silence"),
    ("lists/bad.md", "---\nname: Bad\nowner: missing\n---\nText", "owner"),
])
def test_bad_files_name_the_path_and_reason(root, file, text, reason):
    path = root / file
    path.write_text(text)
    with pytest.raises(WorkspaceError, match=reason) as error:
        load_workspace(root)
    assert str(path) in str(error.value)


def test_unknown_references_warn_and_changes_change_fingerprint(root):
    before = fingerprint(root)
    (root / "lists/warn.md").write_text("---\nname: Warn\nholders: [agent:missing]\nslack_channel: nowhere\ndone: [custom:missing]\n---\nInstructions")
    workspace = load_workspace(root)
    assert fingerprint(root) != before
    assert len(workspace.warnings) == 3
    assert all("lists/warn.md" in warning for warning in workspace.warnings)
    before = fingerprint(root)
    with (root / "agents.yaml").open("a") as output:
        output.write("\n# An agent file change.\n")
    assert fingerprint(root) != before


def test_token_cli_accepts_agent():
    args = parser().parse_args(["token", "create", "--person", "ada", "--label", "build", "--scope", "agent", "--agent", "build"])
    assert args.agent == "build" and args.scope == "agent"


def test_effort_config_defaults_and_validation():
    from pydantic import ValidationError

    from illo.workspace import BrainConfig

    config = BrainConfig()
    assert config.timeout_s == 300
    assert config.effort == {"person_dm": "medium", "team_channel": "medium", "agent_request": "medium"}
    for effort in ({"person_dm": "extreme"}, {"unknown": "high"}):
        with pytest.raises(ValidationError):
            BrainConfig(effort=effort)


async def test_live_reload_keeps_old_lists_on_error(app, tmp_path):
    ctx = app.state.ctx
    shutil.copytree(ROOT / "workspace.example", tmp_path / "workspace")
    ctx.config.workspace_dir = tmp_path / "workspace"
    old = ctx.workspace
    (ctx.config.workspace_dir / "lists/bad.md").write_text("---\nname: Bad\ndone: []\n---\nText")
    with pytest.raises(WorkspaceError):
        await ctx.reload()
    assert ctx.workspace is old


async def test_token_cli_checks_owner_and_lists_agent(pool, root, capsys):
    from illo.cli import execute
    from illo.config import Config

    config = Config(database_url=pool.conninfo, workspace_dir=root)
    base = ["token", "create", "--person", "ada", "--label", "example", "--scope", "agent", "--agent"]
    assert await execute(parser().parse_args([*base, "build"]), config) == 0
    capsys.readouterr()
    await execute(parser().parse_args(["token", "list"]), config)
    assert "build" in capsys.readouterr().out
    for key in ("studio", "missing"):
        with pytest.raises(ValueError, match="agent must exist"):
            await execute(parser().parse_args([*base, key]), config)
    collector = ["token", "create", "--person", "ada", "--label", "example", "--agent", "build"]
    with pytest.raises(ValueError, match="agent scope"):
        await execute(parser().parse_args(collector), config)


async def test_doctor_prints_task_reference_warnings(pool, root, capsys):
    from illo.cli import doctor
    from illo.config import Config

    (root / "lists/warn.md").write_text("---\nname: Warn\nholders: [agent:missing]\ndone: [fake:missing]\n---\nInstructions")
    assert await doctor(Config(database_url=pool.conninfo, workspace_dir=root)) == 0
    output = capsys.readouterr().out
    assert "lists/warn.md: unknown holder agent:missing" in output
    assert "unknown check fake:missing" in output


def test_empty_lists_folder_changes_fingerprint(root):
    shutil.rmtree(root / "lists")
    absent = fingerprint(root)
    (root / "lists").mkdir()
    assert fingerprint(root) != absent
    assert not load_workspace(root).lists
